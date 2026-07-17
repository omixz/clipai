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

# Warm the Whisper model into the image so the first real request isn't slow.
# (needs real network access here, so this runs before HF_HUB_OFFLINE is set below)
RUN python3 -c "from faster_whisper import WhisperModel; WhisperModel('tiny', device='cpu', compute_type='int8')"

# Only go offline for HF lookups at runtime, now that the model is cached in the image.
ENV HF_HUB_OFFLINE=1

EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
