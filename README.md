# Peakcut

Free AI video clipper: upload a video, get back the best auto-picked, auto-captioned
short clips — cut on full sentences and reframed for TikTok/Reels/Shorts. No paid APIs — self-hosted Whisper for transcription, ffmpeg for rendering,
a rule-based (audio loudness + speech pattern) scorer for picking highlights.

Verified working end-to-end, including as a built Docker image run in a clean
container (upload → transcribe → score → render → download all confirmed over
real HTTP requests, not just unit-tested pieces).

## Run locally

```bash
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8000
```

Needs `ffmpeg` (5.0+) installed on the system (`apt-get install ffmpeg` / `brew install ffmpeg`).
Open http://localhost:8000

## Run with Docker

```bash
docker build -t peakcut .
docker run -p 8000:8000 --env-file .env peakcut
```

The Dockerfile warms the Whisper model into the image at build time, so the
first real request isn't stuck downloading a ~250MB model.

## How it works

1. `POST /process` validates options, reserves one video from the caller's monthly allowance and returns a job id instantly. A single background worker processes jobs one at a time from a **priority queue** (paid plans first; two concurrent Whisper+ffmpeg runs would OOM a 512MB instance). The front-end polls `GET /job/{id}`, which reports the real stage (`transcribing` → `finding_moments` → `rendering`) and queue position. Failed jobs are refunded automatically.
2. `pipeline_lib.transcribe()` runs self-hosted faster-whisper (CPU, int8, VAD filter on to skip silence) — no API key, no per-minute cost.
3. `loudness_timeline()` measures audio loudness for the whole file in **one ffmpeg pass** (previously one ffmpeg process per segment).
4. `score_candidates()` builds **sentence-aware windows**: runs of consecutive segments that start and end on a sentence boundary or natural pause, sized by the chosen clip length (short 12–30s, medium 25–60s, long 45–90s). Each window is scored on loudness, text signals, a hook bonus for strong openers (questions, numbers, "here's why…"), completeness and speaking pace.
5. `pick_top_n()` selects the best non-overlapping windows (3 / 5 / 8 per plan).
6. `render_segment()` cuts each window (padded so no word is clipped), reframes it (`crop` fill, `blur` show-everything, or `fit` letterbox), burns in word-timed captions (filler words removed) and the watermark on the free plan. Each clip also gets an editable `.srt`, a suggested title and hashtags. "Download all" returns a ZIP with clips, captions and `clips.json`.

Uploads are deleted as soon as clips are rendered; clips are deleted after 24 hours. Job ids are 64-bit random, job status/ZIP endpoints are owner-only, and only finished `.mp4`/`.srt` files are ever served.

## Process on your own hardware

Users can make clips on their own computer instead of the shared server:

1. **Settings → Your computers → Pair a computer** issues a device token (stored hashed, max 3 per user). The page shows the exact command to run.
2. On that computer: download `/worker/bundle.zip` (or clone this repo), `pip install -r requirements-worker.txt`, then
   `python peakcut_worker.py --server https://your-site --token pd_…`. It polls `/worker/claim` and processes that user's **My computer** uploads locally.
   - Whisper runs on CUDA automatically, using the `small` model on GPU and `base` on CPU.
   - Rendering uses NVENC, Quick Sync, VideoToolbox or AMF when ffmpeg supports them.
   - Finished clips are uploaded to `/worker/jobs/{id}/result` and re-validated server-side.
3. Jobs processed on your own computer **don't use the monthly allowance**. They're capped at `OWN_DEVICE_DAILY_LIMIT` per day (default 30) to bound storage, and can request up to 8 clips.
4. **Leases.** A worker holds a 3-minute lease that its heartbeat extends. If it disappears, the job goes back in the queue, and after 3 lost leases it fails with a refund.
5. **Offline.** `python peakcut_worker.py --local video.mp4 --out clips/` works fully offline, with no account.

**Owner capacity.** Set `WORKER_TOKEN` and run the same worker with that token on your own GPU box to drain the *cloud* queue. With `LOCAL_WORKER_ENABLED=0`, the 512MB host becomes a pure web front and all processing happens on your hardware.

## Marketing autopilot (TikTok + Instagram)

`autopilot/` renders a minimal black-and-white reel and posts it to TikTok and Instagram twice a day through Buffer, entirely from GitHub Actions (`.github/workflows/marketing.yml`). See [`autopilot/README.md`](autopilot/README.md) for the 10-minute setup.

## Plans, limits and ads

| | Free | Pro ($15) | Pro Plus ($29) |
|---|---|---|---|
| Videos / month (`FREE_LIMIT`, `PRO_LIMIT`, `PRO_PLUS_LIMIT`) | 5 | 20 | 50 |
| Clips per video (`config.MAX_CLIPS`) | 3 | 5 | 8 |
| Watermark | "Made with Peakcut" | none | optional custom text |
| Dubbing / priority queue | – | ✓ | ✓ |
| API videos / month (`config.API_LIMITS`) | 10 | 500 | 2000 |

- Allowances reset on the 1st of each month (UTC). Counters live in `usage.json` — wiped on redeploy on disk-less hosts, which is low stakes (it can only give people *more* videos).
- **Plan status is never stored locally.** After Stripe Checkout, `/confirm-checkout` verifies the session server-side and sets a *signed* `clipai_customer` cookie. Every check then asks Stripe for the customer's subscriptions (cached 60s; `active`, `trialing` and `past_due` count; the price id decides Pro vs Pro Plus). Signed-in users without the cookie are matched by email, so a subscriber gets their plan back on a new device just by signing in. Webhooks invalidate the cache.
- Already-subscribed users who click upgrade are sent to the billing portal instead of creating a second subscription. Checkout accepts promotion codes (create them in Stripe for launch campaigns).
- API keys need Google sign-in configured (otherwise identity is a resettable cookie). Keys are stored as SHA-256 hashes, max 5 per user, revocable, and usage is counted per user per month across all keys.
- The free-tier ad slot uses AdSense once approved.

### One-time setup to actually take payments and show ads

1. **Stripe**: create a product + recurring price ($10-20/mo suggested) in the Stripe Dashboard. Copy the secret key, the price ID, and (after adding a webhook endpoint at `https://yourdomain/stripe/webhook` listening for `checkout.session.completed` and `customer.subscription.deleted`) the webhook signing secret. Put all three in `config.py` or as environment variables.
2. **AdSense**: sign up at https://adsense.google.com with the live URL. Once approved, put the publisher ID in `config.py` and uncomment the AdSense script tag in `index.html`'s `<head>`.
3. **Google Sign-In**: create an OAuth Client ID (Google Cloud Console > APIs & Services > Credentials > Create Credentials > OAuth client ID > Web application), adding `{SITE_URL}/auth/google/callback` as an authorized redirect URI. Put `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` in `config.py`/env vars, and set `SESSION_SECRET_KEY` to a long random string (`openssl rand -hex 32`) — it signs the account cookie, so treat it like a secret.

## Sign in with Google

- This is a **soft requirement**, same pattern as Stripe/AdSense above: while `GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET` are still the `REPLACE_ME` placeholders, the site works exactly as before (anonymous, cookie-tracked, `FREE_LIMIT` videos/month). The moment real credentials are supplied, `config.GOOGLE_SIGNIN_CONFIGURED` flips to `True` and two things change: `/process` requires a signed-in Google account, and the free-tier counter switches from per-browser-cookie to per-Google-account (`acct:{sub}`) — closing the "clear your cookies for another free video" loophole, the same way OpusClip/Vidyo.ai gate their free tiers.
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

Unlike Render's free tier, `docker-compose.yml` gives the app container a real
persistent volume for `jobs/` — it survives restarts here, though Pro status
still shouldn't be trusted from local storage (see `check_pro_status` above)
since a VM can still be rebuilt or migrated.
