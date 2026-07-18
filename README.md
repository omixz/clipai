# Peakcut

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
docker build -t peakcut .
docker run -p 8000:8000 --env-file .env peakcut
```

The Dockerfile warms the Whisper model into the image at build time, so the
first real request isn't stuck downloading a ~250MB model.

## How it works

1. `POST /process` accepts a video upload, reserves the free-tier quota, and returns a job id instantly; a single background worker processes jobs one at a time (two concurrent Whisper+ffmpeg runs would OOM a 512MB instance) while the front-end polls `GET /job/{id}` for queued/processing/done/failed. The site stays fully responsive during the minutes-long processing and long uploads can't die at a gateway timeout.
2. `pipeline_lib.transcribe()` runs self-hosted faster-whisper (small model, CPU, int8) — no API key, no per-minute cost.
3. `score_candidates()` scores every transcript segment by real audio loudness (`ffmpeg astats`) plus cheap text signals (punctuation, contrast words, punchy short words) — no LLM call.
4. `pick_top_n()` selects the top 3 non-overlapping segments.
5. `render_clip()` cuts each segment, converts to vertical 1080x1920, burns in short punchy word-chunk captions (timed to actual speech via Whisper's word-level timestamps) plus a watermark (free plan only).

## Free tier, Pro plan, and ads

- `config.py` holds every setting you need to fill in — Stripe keys, AdSense publisher ID, free-plan limit, max upload size. Every placeholder is marked `REPLACE_ME` / `REPLACE-ME`.
- Free users are capped at `FREE_LIMIT` (default 1) video. Once Google Sign-In is configured (see below), usage is tracked per Google account (`acct:{sub}`); until then it falls back to a per-browser cookie (`clipai_cid`), which is fine for an MVP but not abuse-proof since clearing cookies resets the counter. Either way the counter lives in `usage.json`, which gets wiped on every redeploy/spin-down on a host with no persistent disk (Render's free tier included) — low stakes for a free counter.
- **Pro status is deliberately NOT stored in that same file.** The "Upgrade to Pro" button hits `/create-checkout-session` → Stripe Checkout → `/confirm-checkout`, which verifies the session server-side and stores the Stripe customer ID in a cookie (`clipai_customer`). From then on, every request checks that customer's subscription status live against Stripe's API (`check_pro_status` in `app.py`) instead of trusting a local flag. This matters because a local flag would get wiped by the same disk-persistence issue above — and unlike the free counter, losing a paying customer's access on every restart is a real problem, not a minor one. `/stripe/webhook` is still wired up for logging, but Pro access no longer depends on it firing.
- The free-tier ad slot in `index.html` is a placeholder until AdSense approves the site and you paste in your publisher ID.

### One-time setup to actually take payments and show ads

1. **Stripe**: create a product + recurring price ($10-20/mo suggested) in the Stripe Dashboard. Copy the secret key, the price ID, and (after adding a webhook endpoint at `https://yourdomain/stripe/webhook` listening for `checkout.session.completed` and `customer.subscription.deleted`) the webhook signing secret. Put all three in `config.py` or as environment variables.
2. **AdSense**: sign up at https://adsense.google.com with the live URL. Once approved, put the publisher ID in `config.py` and uncomment the AdSense script tag in `index.html`'s `<head>`.
3. **Google Sign-In**: create an OAuth Client ID (Google Cloud Console > APIs & Services > Credentials > Create Credentials > OAuth client ID > Web application), adding `{SITE_URL}/auth/google/callback` as an authorized redirect URI. Put `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` in `config.py`/env vars, and set `SESSION_SECRET_KEY` to a long random string (`openssl rand -hex 32`) — it signs the account cookie, so treat it like a secret.

## Sign in with Google

- This is a **soft requirement**, same pattern as Stripe/AdSense above: while `GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET` are still the `REPLACE_ME` placeholders, the site works exactly as before (anonymous, cookie-tracked, 1 free video). The moment real credentials are supplied, `config.GOOGLE_SIGNIN_CONFIGURED` flips to `True` and two things change: `/process` requires a signed-in Google account, and the free-tier counter switches from per-browser-cookie to per-Google-account (`acct:{sub}`) — closing the "clear your cookies for another free video" loophole, the same way OpusClip/Vidyo.ai gate their free tiers.
- The OAuth flow is hand-rolled (`auth.py`) with `httpx` + `PyJWT` rather than `authlib`, to avoid a `cryptography` version conflict with the Debian-managed OS package. It fetches Google's discovery document and JWKS live, and verifies the ID token's signature before trusting any claim.
- "Verification" is Google's own `email_verified` claim on the ID token — sign-in is rejected (redirect to `?auth_error=unverified`) if Google reports the email as unverified. This avoids needing a separate email/SMTP-based OTP system.
- The account cookie (`clipai_account`) is signed with `itsdangerous` using `SESSION_SECRET_KEY`, so it can't be forged or edited client-side.
- Routes: `GET /auth/google/login`, `GET /auth/google/callback`, `GET /auth/logout`.

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
