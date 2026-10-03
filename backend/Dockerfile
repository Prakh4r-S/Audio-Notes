FROM python:3.12-slim

# ffmpeg/ffprobe are required to validate, decode and split audio.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app

ENV PYTHONUNBUFFERED=1
# Render injects $PORT. The API process also runs the queue worker unless
# RUN_WORKER_IN_PROCESS=false (then run `python -m app.worker` as a separate service).
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers"]
