"""
YouTube Helper API
==================
API เล็กๆ ที่ห่อ yt-dlp/ffmpeg ไว้เบื้องหลัง สำหรับให้ n8n Cloud (หรือเครื่องมือ
no-code อื่นๆ ที่รันคำสั่งระบบเองไม่ได้) เรียกใช้งานผ่าน HTTP แทน

Endpoints:
  POST /subtitles   { "youtube_url": "..." }
       -> { "transcript": "[00:00:01.000] ข้อความ...\n[00:00:05.000] ..." }

  POST /clip        { "youtube_url": "...", "start_ts": "00:00:10.000", "end_ts": "00:00:40.000" }
       -> ไฟล์วิดีโอ .mp4 (binary, media_type=video/mp4)

ทุก endpoint ต้องแนบ header: x-api-key: <API_KEY ที่ตั้งไว้ใน env var>

รันทดสอบในเครื่อง:
    pip install -r requirements.txt
    export API_KEY="your-secret-key"
    uvicorn main:app --host 0.0.0.0 --port 8000
"""

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask

app = FastAPI(title="YouTube Helper API")

API_KEY = os.environ.get("API_KEY", "")

# YouTube มักบล็อก IP ของเซิร์ฟเวอร์คลาวด์ (Railway/Render ฯลฯ) ว่าเป็นบอท
# ต้องแนบ cookies จากเบราว์เซอร์จริงที่ล็อกอิน YouTube ไว้ถึงจะผ่าน
# เอาเนื้อหาไฟล์ cookies.txt (Netscape format) มาใส่ env var ชื่อ YT_COOKIES
COOKIES_FILE = "/tmp/yt_cookies.txt"


def write_cookies_file():
    cookies_content = os.environ.get("YT_COOKIES", "")
    if cookies_content.strip():
        Path(COOKIES_FILE).write_text(cookies_content, encoding="utf-8")
        return True
    return False


HAS_COOKIES = write_cookies_file()


def cookie_args() -> list[str]:
    return ["--cookies", COOKIES_FILE] if HAS_COOKIES else []


def check_api_key(x_api_key: str | None):
    if not API_KEY:
        # ถ้าไม่ได้ตั้งค่า API_KEY ไว้เลย ให้ปฏิเสธการใช้งานทั้งหมด (ป้องกันลืมตั้งค่าแล้วเปิดสาธารณะ)
        raise HTTPException(status_code=500, detail="เซิร์ฟเวอร์ยังไม่ได้ตั้งค่า API_KEY")
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="API key ไม่ถูกต้อง")


class SubtitleRequest(BaseModel):
    youtube_url: str


class ClipRequest(BaseModel):
    youtube_url: str
    start_ts: str  # รูปแบบ HH:MM:SS.mmm
    end_ts: str    # รูปแบบ HH:MM:SS.mmm


def parse_vtt(vtt_path: Path) -> str:
    import webvtt

    lines = []
    seen = set()
    for caption in webvtt.read(str(vtt_path)):
        text = caption.text.strip().replace("\n", " ")
        key = (caption.start, text)
        if not text or key in seen:
            continue
        seen.add(key)
        lines.append(f"[{caption.start}] {text}")
    return "\n".join(lines)


@app.post("/subtitles")
def get_subtitles(req: SubtitleRequest, x_api_key: str | None = Header(default=None)):
    check_api_key(x_api_key)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        out_template = str(tmp_path / "source.%(ext)s")
        cmd = [
            "yt-dlp",
            "--skip-download",
            "--write-auto-sub", "--write-sub",
            "--sub-lang", "th,en",
            "--sub-format", "vtt",
            "--convert-subs", "vtt",
            *cookie_args(),
            "-o", out_template,
            req.youtube_url,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            raise HTTPException(status_code=502, detail=f"yt-dlp ล้มเหลว: {result.stderr[-500:]}")

        sub_files = list(tmp_path.glob("source*.vtt"))
        if not sub_files:
            raise HTTPException(status_code=404, detail="ไม่พบคำบรรยาย (subtitle) สำหรับวิดีโอนี้")

        transcript = parse_vtt(sub_files[0])
        if not transcript.strip():
            raise HTTPException(status_code=404, detail="คำบรรยายว่างเปล่า")

        return {"transcript": transcript}


TS_PATTERN = re.compile(r"^\d{1,2}:\d{2}:\d{2}(\.\d+)?$")


@app.post("/clip")
def get_clip(req: ClipRequest, x_api_key: str | None = Header(default=None)):
    check_api_key(x_api_key)

    if not TS_PATTERN.match(req.start_ts) or not TS_PATTERN.match(req.end_ts):
        raise HTTPException(status_code=400, detail="รูปแบบเวลาต้องเป็น HH:MM:SS.mmm")

    tmp = tempfile.mkdtemp()
    tmp_path = Path(tmp)
    out_path = tmp_path / "clip.mp4"

    try:
        section = f"*{req.start_ts}-{req.end_ts}"
        cmd = [
            "yt-dlp",
            "-f", "bv*[ext=mp4]+ba[ext=m4a]/mp4",
            "--merge-output-format", "mp4",
            "--download-sections", section,
            "--force-keyframes-at-cuts",
            *cookie_args(),
            "-o", str(out_path),
            req.youtube_url,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        if result.returncode != 0 or not out_path.exists():
            shutil.rmtree(tmp, ignore_errors=True)
            raise HTTPException(status_code=502, detail=f"ตัดคลิปล้มเหลว: {result.stderr[-500:]}")

        # ส่งไฟล์กลับ แล้วลบโฟลเดอร์ชั่วคราวทิ้งหลังส่งเสร็จ (ผ่าน background task)
        return FileResponse(
            path=str(out_path),
            media_type="video/mp4",
            filename="clip.mp4",
            background=BackgroundTask(shutil.rmtree, tmp, ignore_errors=True),
        )
    except HTTPException:
        raise
    except Exception as e:
        shutil.rmtree(tmp, ignore_errors=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/health")
def health():
    return {"status": "ok", "cookies_loaded": HAS_COOKIES}
