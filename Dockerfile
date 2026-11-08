FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN mkdir -p jobs && echo '{}' > usage.json

# Cap thread fan-out so ffmpeg/ctranslate2 don't thrash a 1-vCPU free instance.
ENV OMP_NUM_THREADS=2

# "tiny" keeps memory usage low enough for free-tier hosts (512MB) — "small" OOMs there.
# Override with WHISPER_MODEL=small env var on a host with more RAM for better accuracy.
ENV WHISPER_MODEL=tiny

# Warm the Whisper model into the image so the first real request isn't slow.
# (needs real network access here, so this runs before HF_HUB_OFFLINE is set below)
RUN python3 -c "from faster_whisper import WhisperModel; WhisperModel('tiny', device='cpu', compute_type='int8')"

# Only go offline for HF lookups at runtime, now that the model is cached in the image.
ENV HF_HUB_OFFLINE=1

# Dubbing: pre-download Piper TTS voices (small/low-quality variants to keep
# image size and runtime memory down — see dub_lib.py), same warm-into-the-image
# idea as Whisper above. Translation uses deep-translator (a free web API call
# at request time, no model to warm — see dub_lib.py for why: Argos Translate's
# spaCy/Stanza dependency alone cost ~600-900MB, blowing well past this host's
# 512MB cap before Piper or Whisper were even in the picture).
ENV PIPER_VOICES_DIR=/app/voices
RUN mkdir -p /app/voices && \
    python3 -m piper.download_voices --download-dir /app/voices es_ES-carlfm-x_low && \
    python3 -m piper.download_voices --download-dir /app/voices fr_FR-siwis-low && \
    python3 -m piper.download_voices --download-dir /app/voices pt_BR-faber-medium

EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=5s --start-period=60s CMD python3 -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/healthz')" || exit 1
# --proxy-headers so request.url.scheme / client IP reflect the real client behind Render/Caddy.
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
