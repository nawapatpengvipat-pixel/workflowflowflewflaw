FROM python:3.12-slim

# ffmpeg จำเป็นสำหรับ yt-dlp ตอน merge/ตัดคลิป
# nodejs จำเป็นสำหรับ yt-dlp ในการถอดรหัส YouTube "n challenge" เวอร์ชันใหม่
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    nodejs \
    npm \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# บังคับอัปเดต yt-dlp เป็นเวอร์ชันล่าสุดเสมอตอน build เพราะ YouTube เปลี่ยนกฎบ่อยมาก
RUN pip install --no-cache-dir -U yt-dlp

COPY main.py .

# Railway/Render ส่ง PORT มาทาง env var — ใช้ shell form เพื่ออ่านค่า $PORT ได้
ENV PORT=8000
CMD uvicorn main:app --host 0.0.0.0 --port ${PORT}
