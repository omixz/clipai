FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN mkdir -p jobs && echo '{}' > usage.json

# "tiny" keeps memory usage low enough for free-tier hosts (512MB) — "small" OOMs there.
# Override with WHISPER_MODEL=small env var on a host with more RAM for better accuracy.
ENV WHISPER_MODEL=tiny
ENV HF_HUB_OFFLINE=1

# Warm the Whisper model into the image so the first real request isn't slow.
RUN python3 -c "from faster_whisper import WhisperModel; WhisperModel('tiny', device='cpu', compute_type='int8')"

EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
