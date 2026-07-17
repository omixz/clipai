# ClipAI

Free AI video clipper: upload a video, get back the top 3 auto-picked, auto-captioned
short clips. No paid APIs — self-hosted Whisper for transcription, ffmpeg for rendering,
a rule-based (audio loudness + speech pattern) scorer for picking highlights.

Verified working end-to-end, including as a built Docker image run in a clean
container (upload → transcribe → score → render → download all confirmed over
real HTTP requests, not just unit-tested pieces).

## Run locally

```bash
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8000
```

Needs `ffmpeg` installed on the system (`apt-get install ffmpeg` / `brew install ffmpeg`).
Open http://localhost:8000

## Run with Docker

```bash
docker build -t clipai .
docker run -p 8000:8000 --env-file .env clipai
```

The Dockerfile warms the Whisper model into the image at build time, so the
first real request isn't stuck downloading a ~250MB model.

## How it works

1. `POST /process` accepts a video upload.
2. `pipeline_lib.transcribe()` runs self-hosted faster-whisper (small model, CPU, int8) — no API key, no per-minute cost.
3. `score_candidates()` scores every transcript segment by real audio loudness (`ffmpeg astats`) plus cheap text signals (punctuation, contrast words, punchy short words) — no LLM call.
4. `pick_top_n()` selects the top 3 non-overlapping segments.
5. `render_clip()` cuts each segment, converts to vertical 1080x1920, burns in short punchy word-chunk captions (timed to actual speech via Whisper's word-level timestamps) plus a watermark (free plan only).

## Free tier, Pro plan, and ads

- `config.py` holds every setting you need to fill in — Stripe keys, AdSense publisher ID, free-plan limit, max upload size. Every placeholder is marked `REPLACE_ME` / `REPLACE-ME`.
- Free users are capped at `FREE_LIMIT` (default 1) video, tracked via a cookie (`clipai_cid`) in `usage.json`. This is fine for an MVP but not abuse-proof — clearing cookies resets the counter. Real accounts are the next step before this scales.
- The "Upgrade to Pro" button hits `/create-checkout-session`, which creates a real Stripe Checkout session and redirects there. `/stripe/webhook` listens for `checkout.session.completed` (marks the client Pro) and subscription-cancelled events (un-marks them). **Until you fill in the three Stripe values in `config.py`, checkout will fail gracefully with a clear error** — verified, it doesn't crash the app.
- The free-tier ad slot in `index.html` is a placeholder until AdSense approves the site and you paste in your publisher ID.

### One-time setup to actually take payments and show ads

1. **Stripe**: create a product + recurring price ($10-20/mo suggested) in the Stripe Dashboard. Copy the secret key, the price ID, and (after adding a webhook endpoint at `https://yourdomain/stripe/webhook` listening for `checkout.session.completed` and `customer.subscription.deleted`) the webhook signing secret. Put all three in `config.py` or as environment variables.
2. **AdSense**: sign up at https://adsense.google.com with the live URL. Once approved, put the publisher ID in `config.py` and uncomment the AdSense script tag in `index.html`'s `<head>`.

## Deploying this for real

This needs a host that runs a persistent process with ffmpeg installed — static
hosts (Vercel, Netlify) won't work since this isn't a static site, and serverless
functions have execution time limits too short for video encoding plus no
persistent disk to cache the ~250MB Whisper model between requests.

**`render.yaml` is included** for a one-click deploy on [Render](https://render.com)
(has a real free tier): create a free Render account, connect this repo, and it
picks up the Dockerfile and env var slots automatically. Railway and Fly.io both
also support deploying an arbitrary Dockerfile if you'd rather use those.

First request after a cold start will still take a few seconds (loading the
warmed-but-not-yet-in-memory model) — normal, not a bug.
