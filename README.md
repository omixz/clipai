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

1. `POST /process` accepts a video upload, reserves that tier's monthly quota, and returns a job id instantly; a single background worker processes jobs one at a time (two concurrent Whisper+ffmpeg runs would OOM a 512MB instance) while the front-end polls `GET /job/{id}` for queued/processing/done/failed. The site stays fully responsive during the minutes-long processing and long uploads can't die at a gateway timeout. The queue is priority-ordered (Pro Plus, then Pro, then Free, FIFO within a tier), a max video length (`MAX_VIDEO_DURATION_MIN`, default 45) is checked via `ffprobe` before Whisper even starts, and every `ffmpeg`/`ffprobe` subprocess call has a timeout — all so one oversized or hung upload can't wedge the single queue for everyone behind it.
2. `pipeline_lib.transcribe()` runs self-hosted faster-whisper (small model, CPU, int8) — no API key, no per-minute cost.
3. `score_candidates()` scores every transcript segment by real audio loudness (`ffmpeg astats`) plus cheap text signals (punctuation, contrast words, punchy short words) — no LLM call.
4. `pick_top_n()` selects the top 3 non-overlapping segments.
5. `render_clip()` cuts each segment, converts to vertical 1080x1920, burns in short punchy word-chunk captions (timed to actual speech via Whisper's word-level timestamps) plus a watermark (free plan only).

## Free tier, Pro plan, and ads

- `config.py` holds every setting you need to fill in — Stripe keys, AdSense publisher ID, per-tier plan limits, max upload size. Every placeholder is marked `REPLACE_ME` / `REPLACE-ME`.
- Every tier is capped at a monthly video count — `FREE_LIMIT` (default 5), `PRO_LIMIT` (default 20), `PRO_PLUS_LIMIT` (default 50), matching what `pricing.html` advertises. The counter is keyed by identity (Google account once Sign-In is configured, otherwise a per-browser cookie) and tagged with the current `YYYY-MM` period so it resets itself on the 1st with no cron job — see `reserve_use`/`current_period` in `app.py`. It lives in `jobs/usage.json`, which rides the same Docker volume as everything else in `jobs/` so an Oracle VM redeploy doesn't wipe it (on Render's ephemeral free tier it gets wiped on every redeploy/spin-down regardless — low stakes there for the free tier, but see the Oracle section below for why it matters more on a persistent VM).
- **Pro/Pro Plus tier is deliberately NOT stored in that same file, only the video count is.** The "Upgrade" buttons hit `/create-checkout-session[-plus]` → Stripe Checkout → `/confirm-checkout`, which verifies the session server-side and stores the Stripe customer ID in a cookie (`clipai_customer`). From then on, every request checks that customer's subscription live against Stripe's API and reads which price ID they're on to tell Pro from Pro Plus (`get_account_tier` in `app.py`) instead of trusting a local flag. This matters because a local flag would get wiped by the same disk-persistence issue above — and unlike a video counter, losing a paying customer's tier on every restart is a real problem, not a minor one. `/stripe/webhook` is still wired up for logging, but tier detection no longer depends on it firing.
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

### Oracle Cloud Free Tier (more RAM than Render's free 512MB)

Oracle's Always Free tier includes an Ampere A1 (ARM) VM with up to 24GB RAM —
real headroom for the Whisper + dubbing pipeline, versus Render's 512MB cap.
Trade-off: it's a raw VM, not a PaaS, so you're responsible for the reverse
proxy and HTTPS yourself (both handled by the files in this repo).

1. Create an [Oracle Cloud](https://www.oracle.com/cloud/free/) account (needs
   card verification — free tier stays free, but they require one on file).
2. Create a Compute instance: shape **VM.Standard.A1.Flex** (Always Free
   eligible), image **Ubuntu**, at least 2 OCPU / 12GB RAM. Open ports 80 and
   443 in the instance's attached **Security List** (Console > Networking >
   Virtual Cloud Networks > your VCN > Security Lists) — this is separate from
   the OS firewall.
3. Get a free hostname pointing at the VM's public IP — a bare IP can't get a
   valid HTTPS certificate. [DuckDNS](https://www.duckdns.org) works with no
   domain purchase needed. Put that hostname in `Caddyfile`.
4. SSH into the VM and run `deploy/oracle-bootstrap.sh` — installs Docker,
   clones this repo to `/opt/peakcut`, and brings up `docker-compose.yml`
   (the app + a Caddy reverse proxy that gets HTTPS automatically via Let's
   Encrypt). Copy `.env.example` to `.env` and fill in real secrets first.
5. **Update the Google OAuth Client's authorized redirect URIs** (Google Cloud
   Console > Credentials) to include `https://your-hostname/auth/google/callback`
   — sign-in will 400 without this, since Google validates the redirect URI
   exactly against what's registered.

Redeploying after a code change: `cd /opt/peakcut && git pull && sudo docker
compose up -d --build` — no CI/CD wired up, matching how deploys to Render
were done manually via API throughout this project.

**Using the extra RAM.** The one-worker-at-a-time design (see "How it works"
above) is deliberate and stays on by default even here — it's what keeps
memory use predictable, not just what fits Render's 512MB. On Oracle's extra
headroom you have two independent knobs, and they trade off differently:

- `WHISPER_MODEL=base` or `small` (env var, default `tiny`) — better
  transcription accuracy, same one-job-at-a-time safety, no code changes.
  This is the one to reach for first.
- `WORKER_COUNT=2` (env var, default `1`) — processes that many jobs
  concurrently instead of one at a time. This is **not a supported/tested
  configuration**: `pipeline_lib.py` caches a single global Whisper model
  instance and `dub_lib.py` caches Piper voices in a plain dict, neither
  guarded by a lock, so concurrent workers share that state across threads.
  It's exposed for experimentation on a box with real headroom, not something
  this project has verified is safe under load — raise it deliberately, watch
  memory, and be ready to drop it back to 1.

Unlike Render's free tier, `docker-compose.yml` gives the app container a real
persistent volume for `jobs/` — it survives restarts here, though Pro status
still shouldn't be trusted from local storage (see `check_pro_status` above)
since a VM can still be rebuilt or migrated.
