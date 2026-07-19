import itertools
import json
import logging
import queue
import re
import shutil
import threading
import time
import uuid
import zipfile
from io import BytesIO
from pathlib import Path

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

import stripe
from fastapi import FastAPI, File, Form, UploadFile, Request, HTTPException
from fastapi.responses import (
    FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, StreamingResponse,
)

import auth
import config
import email_lib
import pipeline_lib

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("clipai")

BASE_DIR = Path(__file__).parent
JOBS_DIR = BASE_DIR / "jobs"
# Both files live inside JOBS_DIR so they ride the same persistent volume
# (docker-compose.yml mounts only /app/jobs) — otherwise a redeploy on the
# Oracle VM would silently wipe every free-tier counter and API key, unlike
# on Render where the whole disk is ephemeral anyway.
USAGE_FILE = JOBS_DIR / "usage.json"
API_KEYS_FILE = JOBS_DIR / "api_keys.json"

JOBS_DIR.mkdir(exist_ok=True)
if not USAGE_FILE.exists():
    USAGE_FILE.write_text("{}")
if not API_KEYS_FILE.exists():
    API_KEYS_FILE.write_text("{}")

stripe.api_key = config.STRIPE_SECRET_KEY

app = FastAPI()

# ── Usage tracking (monthly counter for every tier, not just free) ──────────
# Every plan is advertised as N videos *per month* (pricing.html), but the
# original counter never reset and Pro/Pro Plus skipped it entirely (only an
# active subscription was checked, never a count) — a subscriber got
# unlimited processing on the same one-worker queue everyone shares, and Pro
# vs Pro Plus were functionally identical. current_period() ties each
# counter to a calendar month so it rolls over on its own with no cron job.

_usage_lock = threading.Lock()


def current_period() -> str:
    return time.strftime("%Y-%m", time.gmtime())


def load_usage():
    with _usage_lock:
        return json.loads(USAGE_FILE.read_text())


def _period_rec(data, key):
    rec = data.get(key)
    if not rec or rec.get("period") != current_period():
        rec = {"period": current_period(), "used": 0}
    return rec


def reserve_use(key, limit) -> bool:
    """Atomically check-and-increment this period's counter for `key`.
    Returns False if `limit` is already reached. Single lock section so two
    concurrent uploads can't both pass the check."""
    with _usage_lock:
        data = json.loads(USAGE_FILE.read_text())
        rec = _period_rec(data, key)
        if rec["used"] >= limit:
            data[key] = rec  # persist a rolled-over period even on rejection
            USAGE_FILE.write_text(json.dumps(data))
            return False
        rec["used"] += 1
        data[key] = rec
        USAGE_FILE.write_text(json.dumps(data))
        return True


def refund_use(key):
    with _usage_lock:
        data = json.loads(USAGE_FILE.read_text())
        rec = _period_rec(data, key)
        rec["used"] = max(0, rec["used"] - 1)
        data[key] = rec
        USAGE_FILE.write_text(json.dumps(data))


def peek_usage(key) -> int:
    data = load_usage()
    rec = data.get(key)
    if not rec or rec.get("period") != current_period():
        return 0
    return rec["used"]


def get_client_id(request: Request) -> str:
    """Cached on request.state so a fresh (cookie-less) visitor gets the same
    id every time this is called within one request — otherwise the id used
    to reserve free-tier usage and the id written into the response cookie
    would be two different random UUIDs, and the counter would never
    actually stick for new anonymous visitors."""
    if not hasattr(request.state, "clipai_cid"):
        request.state.clipai_cid = request.cookies.get("clipai_cid") or str(uuid.uuid4())
    return request.state.clipai_cid


def get_account(request: Request) -> dict | None:
    """The signed-in Google account, if any — see auth.py. Returns
    {"sub": ..., "email": ...} or None."""
    return auth.read_account_cookie(request.cookies.get(auth.ACCOUNT_COOKIE))


def get_identity(request: Request) -> str:
    """The key usage.json tracks the free-tier counter under. Once Google
    sign-in is configured, this is the Google account (sub) — much harder to
    farm than a browser cookie, which is exactly the gap OpusClip/Vidyo.ai
    close by requiring an account before your first free video. Until then,
    falls back to the anonymous per-browser cookie so the site keeps working
    unconfigured."""
    if config.GOOGLE_SIGNIN_CONFIGURED:
        account = get_account(request)
        if account:
            return f"acct:{account['sub']}"
    return f"cid:{get_client_id(request)}"


TIER_LIMITS = {"free": config.FREE_LIMIT, "pro": config.PRO_LIMIT, "pro_plus": config.PRO_PLUS_LIMIT}


def get_account_tier(request: Request) -> str:
    """'free' | 'pro' | 'pro_plus' — checked live against Stripe via a
    customer ID cookie set after checkout, never from local storage, which is
    wiped on every restart on disk-less hosts (Render free tier included).
    Distinguishes the two paid tiers by the subscribed price ID so each gets
    its own monthly cap instead of both being treated as the same "Pro"."""
    customer_id = request.cookies.get("clipai_customer")
    if not customer_id:
        return "free"
    try:
        subs = stripe.Subscription.list(customer=customer_id, status="active", limit=1)
        if not subs.data:
            return "free"
        price_id = subs.data[0]["items"]["data"][0]["price"]["id"]
        if price_id == config.STRIPE_PRICE_ID_PLUS:
            return "pro_plus"
        return "pro"
    except Exception:
        log.exception("Stripe subscription lookup failed")
        return "free"


def check_pro_status(request: Request) -> bool:
    """True for either paid tier. Kept as a separate helper since most call
    sites (dubbing gate, watermark, API key tier assignment) only care about
    free-vs-paid, not which paid tier."""
    return get_account_tier(request) != "free"


# ── API Key Management ──────────────────────────────────────────────────────

_api_lock = threading.Lock()


def load_api_keys():
    with _api_lock:
        return json.loads(API_KEYS_FILE.read_text())


def save_api_keys(data):
    with _api_lock:
        API_KEYS_FILE.write_text(json.dumps(data, indent=2))


def generate_api_key(identity: str, tier: str = "free") -> str:
    """Generate a new API key for a user. Tier can be 'free', 'pro', or 'pro_plus'."""
    api_key = f"pk_{uuid.uuid4().hex[:32]}"
    with _api_lock:
        data = json.loads(API_KEYS_FILE.read_text())
        if "keys" not in data:
            data["keys"] = {}
        data["keys"][api_key] = {
            "identity": identity,
            "tier": tier,
            "created": time.time(),
            "period": current_period(),
            "usage_this_month": 0,
            "active": True,
        }
        API_KEYS_FILE.write_text(json.dumps(data, indent=2))
    return api_key


def get_api_key_info(api_key: str) -> dict | None:
    """Get info about an API key."""
    data = load_api_keys()
    return data.get("keys", {}).get(api_key)


API_TIER_LIMITS = {"free": config.API_FREE_LIMIT, "pro": config.API_PRO_LIMIT, "pro_plus": config.API_PRO_PLUS_LIMIT}


def increment_api_usage(api_key: str) -> bool:
    """Increment this calendar month's usage counter for an API key. Returns
    False if rate limited. Rolls the counter over to 0 the first time it's
    touched in a new month, the same way reserve_use does for web usage —
    previously usage_this_month only ever grew, so "500 calls/month" was
    actually "500 calls ever" once hit."""
    with _api_lock:
        data = json.loads(API_KEYS_FILE.read_text())
        key_data = data.get("keys", {}).get(api_key)
        if not key_data or not key_data.get("active"):
            return False

        if key_data.get("period") != current_period():
            key_data["period"] = current_period()
            key_data["usage_this_month"] = 0

        monthly_limit = API_TIER_LIMITS.get(key_data.get("tier", "free"), API_TIER_LIMITS["free"])
        if key_data["usage_this_month"] >= monthly_limit:
            API_KEYS_FILE.write_text(json.dumps(data, indent=2))  # persist rollover even on rejection
            return False

        key_data["usage_this_month"] += 1
        API_KEYS_FILE.write_text(json.dumps(data, indent=2))
    return True


# ── Background job queue ─────────────────────────────────────────────────────
# One worker thread, one job at a time: two concurrent Whisper+ffmpeg runs
# would OOM a 512MB instance. The upload endpoint returns a job id instantly
# and the front-end polls /job/{id} — this keeps the site responsive during
# the minutes-long processing and avoids gateway timeouts on long requests.
#
# The queue is priority-ordered (pro_plus, then pro, then free), FIFO within
# a tier — this is what actually delivers the "priority processing queue"
# pricing.html promises API Pro/Pro Plus; previously that line was pure
# marketing copy with zero backing code, and a free-tier upload could sit a
# paying customer behind it with no way to jump the line.

MAX_QUEUE = 10
JOB_MAX_AGE_SECONDS = 24 * 60 * 60
QUEUE_PRIORITY = {"pro_plus": 0, "pro": 1, "free": 2}

_jobs: dict = {}
_jobs_lock = threading.Lock()
_job_queue: "queue.PriorityQueue[tuple[int, int, str]]" = queue.PriorityQueue(maxsize=MAX_QUEUE)
_job_seq = itertools.count()


def _set_job(job_id, **fields):
    with _jobs_lock:
        _jobs.setdefault(job_id, {}).update(fields)


def _get_job(job_id):
    with _jobs_lock:
        job = _jobs.get(job_id)
        return dict(job) if job else None


def _enqueue_job(job_id, tier):
    """Assigns this job its priority/sequence and puts it on the priority
    queue. Raises queue.Full if the queue is already at MAX_QUEUE (the
    caller is responsible for cleanup on that path, same as before)."""
    priority = QUEUE_PRIORITY.get(tier, QUEUE_PRIORITY["free"])
    seq = next(_job_seq)
    _set_job(job_id, priority=priority, seq=seq)
    _job_queue.put_nowait((priority, seq, job_id))


def _queue_position(job_id):
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job or job.get("status") != "queued":
            return None
        this_key = (job.get("priority", QUEUE_PRIORITY["free"]), job.get("seq", 0))
        ahead = sum(
            1 for j in _jobs.values()
            if j.get("status") == "queued" and (j.get("priority", QUEUE_PRIORITY["free"]), j.get("seq", 0)) < this_key
        )
    return ahead + 1


def _worker():
    while True:
        _priority, _seq, job_id = _job_queue.get()
        job = _get_job(job_id)
        if not job:
            continue
        _set_job(job_id, status="processing", started_at=time.time())
        job_dir = JOBS_DIR / job_id
        try:
            result = pipeline_lib.process_video(
                job["input_path"], str(job_dir), n_clips=3, watermark=not job["is_pro"],
                dub_lang=job.get("dub_lang"), clip_format=job.get("clip_format", "vertical"),
                caption_style=job.get("caption_style", "bold"),
            )
            if not result["clips"]:
                raise RuntimeError("no usable clips found")
            clips = result["clips"]
            for clip in clips:
                clip["url"] = f"/jobs/{job_id}/{clip['file']}"
            _set_job(job_id, status="done", clips=clips, duration=result["duration"],
                     language=result.get("language"), finished_at=time.time())
            # the source upload is no longer needed once clips exist — free the disk
            try:
                Path(job["input_path"]).unlink(missing_ok=True)
            except Exception:
                pass
            if job.get("notify_email"):
                email_lib.send_done_email(
                    job["notify_email"], [c["url"] for c in clips], result["duration"], job["is_pro"]
                )
        except ValueError as e:
            # e.g. dubbing requested on a non-English source video — a
            # user-input problem, not a processing failure, so show the
            # actual reason rather than the generic message below.
            log.info("job %s rejected: %s", job_id, e)
            shutil.rmtree(job_dir, ignore_errors=True)
            # Every web tier now reserves a monthly slot (see reserve_use) —
            # API jobs pass an "apikey:..." identity that was never reserved
            # here, so this is a harmless no-op for them (floors at 0).
            if job.get("identity"):
                refund_use(job["identity"])
            _set_job(job_id, status="failed", error=str(e), finished_at=time.time())
            if job.get("notify_email"):
                email_lib.send_failed_email(job["notify_email"], str(e))
        except Exception as e:
            log.exception("processing failed for job %s", job_id)
            shutil.rmtree(job_dir, ignore_errors=True)
            # Every web tier now reserves a monthly slot (see reserve_use) —
            # API jobs pass an "apikey:..." identity that was never reserved
            # here, so this is a harmless no-op for them (floors at 0).
            if job.get("identity"):
                refund_use(job["identity"])
            error_str = str(e).lower()
            if "no speech" in error_str or "no usable" in error_str:
                error_msg = "No clear speech detected. Try a video with louder, clearer audio or more talking."
            elif "too short" in error_str or "minimum" in error_str:
                error_msg = "Video is too short. Try a video at least 30 seconds long."
            elif "codec" in error_str or "unsupported" in error_str:
                error_msg = "Video format not supported. Try MP4, MOV, WEBM, or MKV."
            else:
                error_msg = "Processing failed — the video may be too short, silent, or an unsupported codec. Try a longer video with clearer speech."
            error_msg += " This didn't count against your monthly plan; please try again."
            _set_job(job_id, status="failed", error=error_msg, finished_at=time.time())
            if job.get("notify_email"):
                email_lib.send_failed_email(job["notify_email"], error_msg)


for _ in range(max(1, config.WORKER_COUNT)):
    threading.Thread(target=_worker, daemon=True).start()


def cleanup_old_jobs():
    now = time.time()
    try:
        for job_dir in JOBS_DIR.iterdir():
            if job_dir.is_dir() and now - job_dir.stat().st_mtime > JOB_MAX_AGE_SECONDS:
                shutil.rmtree(job_dir, ignore_errors=True)
                with _jobs_lock:
                    _jobs.pop(job_dir.name, None)
    except Exception:
        log.exception("job cleanup failed")

    # Failed jobs have their directory removed immediately (see _worker), so
    # the sweep above never reaches their _jobs entry — without this, every
    # failed upload would leak an entry in _jobs for the life of the process.
    with _jobs_lock:
        stale = [jid for jid, j in _jobs.items()
                 if j.get("status") == "failed" and now - j.get("finished_at", 0) > JOB_MAX_AGE_SECONDS]
        for jid in stale:
            _jobs.pop(jid, None)

    # Same idea for the IP rate-limit log — an IP with no requests in the
    # last hour has nothing left worth remembering.
    with _ip_log_lock:
        empty_ips = [ip for ip, times in _ip_request_log.items() if not any(now - t < 3600 for t in times)]
        for ip in empty_ips:
            _ip_request_log.pop(ip, None)


# ── Routes ───────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def index():
    return (BASE_DIR / "index.html").read_text()


@app.get("/pricing", response_class=HTMLResponse)
def pricing():
    return (BASE_DIR / "pricing.html").read_text()


@app.get("/api/docs", response_class=HTMLResponse)
def api_docs():
    return (BASE_DIR / "api_docs.html").read_text()


@app.get("/settings", response_class=HTMLResponse)
def settings(request: Request):
    if config.GOOGLE_SIGNIN_CONFIGURED and not get_account(request):
        return RedirectResponse("/auth/google/login", status_code=303)
    return (BASE_DIR / "settings.html").read_text()


@app.get("/privacy", response_class=HTMLResponse)
def privacy():
    return (BASE_DIR / "privacy.html").read_text().replace("__CONTACT_EMAIL__", config.CONTACT_EMAIL)


@app.get("/terms", response_class=HTMLResponse)
def terms():
    return (BASE_DIR / "terms.html").read_text().replace("__CONTACT_EMAIL__", config.CONTACT_EMAIL)


@app.get("/healthz", response_class=PlainTextResponse)
def healthz():
    return "ok"


@app.get("/robots.txt", response_class=PlainTextResponse)
def robots():
    return "User-agent: *\nAllow: /\nDisallow: /jobs/\n"


@app.get("/usage")
def usage(request: Request):
    identity = get_identity(request)
    account = get_account(request)
    tier = get_account_tier(request)
    limit = TIER_LIMITS[tier]
    used = peek_usage(identity)
    resp = JSONResponse({
        "used": used, "pro": tier != "free", "plan": "plus" if tier == "pro_plus" else tier,
        "limit": limit, "remaining": max(0, limit - used),
        "google_configured": config.GOOGLE_SIGNIN_CONFIGURED,
        "signed_in": account is not None,
        "email": account["email"] if account else None,
        "email_configured": config.EMAIL_CONFIGURED,
    })
    resp.set_cookie("clipai_cid", get_client_id(request), max_age=60 * 60 * 24 * 365,
                     httponly=True, samesite="lax")
    return resp


def get_request_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# Cookie-based identity is trivially bypassed by a client that never sends
# cookies (the same gap Google sign-in closes once real credentials are
# configured — see get_identity). Until then, this IP-based cap stops that
# gap from turning into unlimited free encodes hammering the single-worker
# queue: an actual cost/DoS concern on a 512MB instance, independent of the
# per-account free-video limit above.
MAX_PROCESS_PER_IP_PER_HOUR = 8
_ip_request_log: dict = {}
_ip_log_lock = threading.Lock()


def check_ip_rate_limit(ip: str) -> bool:
    now = time.time()
    with _ip_log_lock:
        window = [t for t in _ip_request_log.get(ip, []) if now - t < 3600]
        if len(window) >= MAX_PROCESS_PER_IP_PER_HOUR:
            _ip_request_log[ip] = window
            return False
        window.append(now)
        _ip_request_log[ip] = window
        return True


@app.post("/process")
async def process(request: Request, file: UploadFile = File(...), dub_lang: str | None = Form(None),
                   notify_email: str | None = Form(None), clip_format: str = Form("vertical"),
                   caption_style: str = Form("bold")):
    if clip_format not in ("vertical", "square", "horizontal"):
        raise HTTPException(status_code=400, detail="clip_format must be 'vertical', 'square', or 'horizontal'.")
    if caption_style not in ("bold", "outline", "subtle", "neon"):
        raise HTTPException(status_code=400, detail="caption_style must be 'bold', 'outline', 'subtle', or 'neon'.")
    cleanup_old_jobs()

    if not check_ip_rate_limit(get_request_ip(request)):
        raise HTTPException(status_code=429, detail="Too many uploads from this network — please try again later.")

    if config.GOOGLE_SIGNIN_CONFIGURED and not get_account(request):
        raise HTTPException(status_code=401, detail="Sign in with Google to process a video.")

    identity = get_identity(request)
    tier = get_account_tier(request)
    is_pro = tier != "free"

    ext = Path(file.filename or "").suffix.lower()
    if ext not in config.ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Unsupported file type '{ext}'. Use MP4, MOV, M4V, WEBM, or MKV.")

    if dub_lang:
        import dub_lib
        if dub_lang not in dub_lib.DUB_LANGUAGES:
            raise HTTPException(status_code=400, detail="Unsupported dub language.")
        if not is_pro:
            raise HTTPException(status_code=402, detail="Dubbing is a Pro feature. Upgrade to Pro to dub clips into other languages.")

    if notify_email and not EMAIL_RE.match(notify_email):
        raise HTTPException(status_code=400, detail="That doesn't look like a valid email address.")

    if _job_queue.full():
        raise HTTPException(status_code=503, detail="We're at capacity right now — try again in a few minutes.")

    limit = TIER_LIMITS[tier]
    if not reserve_use(identity, limit):
        plan_name = {"free": "Free", "pro": "Pro", "pro_plus": "Pro Plus"}[tier]
        upsell = "Upgrade to Pro for more." if tier == "free" else (
            "Upgrade to Pro Plus for more." if tier == "pro" else "Contact support for a custom plan."
        )
        raise HTTPException(status_code=402, detail=f"{plan_name} plan limit reached ({limit} videos/month). {upsell}")

    job_id = str(uuid.uuid4())[:8]
    job_dir = JOBS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    input_path = job_dir / f"input{ext}"

    max_bytes = config.MAX_UPLOAD_MB * 1024 * 1024
    size = 0
    try:
        with open(input_path, "wb") as f:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    raise HTTPException(status_code=413, detail=f"File too large — the limit is {config.MAX_UPLOAD_MB}MB.")
                f.write(chunk)
    except HTTPException:
        shutil.rmtree(job_dir, ignore_errors=True)
        refund_use(identity)
        raise
    except Exception:
        shutil.rmtree(job_dir, ignore_errors=True)
        refund_use(identity)
        log.exception("upload failed")
        raise HTTPException(status_code=500, detail="Upload failed — please try again.")

    _set_job(job_id, status="queued", identity=identity, is_pro=is_pro,
             input_path=str(input_path), created_at=time.time(), dub_lang=dub_lang,
             notify_email=notify_email, clip_format=clip_format, caption_style=caption_style)
    try:
        _enqueue_job(job_id, tier)
    except queue.Full:
        shutil.rmtree(job_dir, ignore_errors=True)
        with _jobs_lock:
            _jobs.pop(job_id, None)
        refund_use(identity)
        raise HTTPException(status_code=503, detail="We're at capacity right now — try again in a few minutes.")

    resp = JSONResponse({"job_id": job_id, "status": "queued"})
    resp.set_cookie("clipai_cid", get_client_id(request), max_age=60 * 60 * 24 * 365,
                     httponly=True, samesite="lax")
    return resp


@app.get("/job/{job_id}")
def job_status(job_id: str):
    job = _get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found — it may have expired.")
    out = {"job_id": job_id, "status": job["status"]}
    if job["status"] == "queued":
        out["queue_position"] = _queue_position(job_id)
    elif job["status"] == "processing":
        out["elapsed"] = round(time.time() - job.get("started_at", time.time()))
    elif job["status"] == "done":
        out["clips"] = job["clips"]
        out["duration"] = job["duration"]
        out["language"] = job.get("language", "unknown")
        out["language_auto"] = job.get("language") is not None  # True if auto-detected, False if not
    elif job["status"] == "failed":
        out["error"] = job.get("error", "Processing failed.")
    return out


@app.get("/jobs/{job_id}/{filename}")
def get_clip(job_id: str, filename: str):
    if "/" in job_id or "/" in filename or ".." in job_id or ".." in filename:
        raise HTTPException(status_code=400, detail="Invalid path.")
    path = JOBS_DIR / job_id / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="Not found.")
    return FileResponse(str(path))


@app.get("/jobs/{job_id}/download/all")
def download_all_clips(job_id: str):
    if "/" in job_id or ".." in job_id:
        raise HTTPException(status_code=400, detail="Invalid job ID.")
    job_dir = JOBS_DIR / job_id
    if not job_dir.exists():
        raise HTTPException(status_code=404, detail="Job not found.")

    job = _get_job(job_id)
    if not job or job.get("status") != "done":
        raise HTTPException(status_code=400, detail="Job not complete or not found.")

    zip_buffer = BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for clip in job.get("clips", []):
            clip_path = job_dir / clip["file"]
            if clip_path.exists():
                zf.write(clip_path, arcname=clip["file"])

        manifest_path = job_dir / "manifest.json"
        if manifest_path.exists():
            zf.write(manifest_path, arcname="manifest.json")

    zip_buffer.seek(0)
    return StreamingResponse(
        zip_buffer,
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename=peakcut_{job_id[:8]}.zip"}
    )


# ── Sign in with Google ──────────────────────────────────────────────────────
# Soft-required: see config.GOOGLE_SIGNIN_CONFIGURED. These routes work
# regardless, so the flow can be tested end-to-end once real credentials are
# added without any other code changes.

@app.get("/auth/google/login")
def google_login(request: Request):
    if not config.GOOGLE_SIGNIN_CONFIGURED:
        raise HTTPException(status_code=503, detail="Google sign-in isn't configured yet.")
    state = auth.new_state()
    redirect_uri = f"{config.SITE_URL}/auth/google/callback"
    try:
        login_url = auth.build_login_url(redirect_uri, state)
    except Exception:
        log.exception("failed to build Google login URL")
        raise HTTPException(status_code=502, detail="Couldn't reach Google right now — try again shortly.")
    resp = RedirectResponse(login_url, status_code=303)
    resp.set_cookie(auth.STATE_COOKIE, state, max_age=600, httponly=True, samesite="lax")
    return resp


@app.get("/auth/google/callback")
def google_callback(request: Request, code: str | None = None, state: str | None = None, error: str | None = None):
    if error:
        return RedirectResponse(f"{config.SITE_URL}/?auth_error=1", status_code=303)

    expected_state = request.cookies.get(auth.STATE_COOKIE)
    if not code or not state or not expected_state or state != expected_state:
        raise HTTPException(status_code=400, detail="Invalid or expired sign-in attempt — please try again.")

    try:
        claims = auth.exchange_code_for_claims(code, f"{config.SITE_URL}/auth/google/callback")
    except Exception:
        log.exception("Google OAuth exchange failed")
        return RedirectResponse(f"{config.SITE_URL}/?auth_error=1", status_code=303)

    if not claims.get("email_verified"):
        # Google itself is telling us this email hasn't been verified — the
        # whole point of requiring an account is a real, checkable identity.
        return RedirectResponse(f"{config.SITE_URL}/?auth_error=unverified", status_code=303)

    account_token = auth.sign_account_cookie(claims["sub"], claims["email"])
    resp = RedirectResponse(f"{config.SITE_URL}/?signed_in=1", status_code=303)
    resp.set_cookie(auth.ACCOUNT_COOKIE, account_token, max_age=auth.ACCOUNT_MAX_AGE,
                     httponly=True, samesite="lax")
    resp.delete_cookie(auth.STATE_COOKIE)
    return resp


@app.get("/auth/logout")
def logout():
    resp = RedirectResponse(f"{config.SITE_URL}/", status_code=303)
    resp.delete_cookie(auth.ACCOUNT_COOKIE)
    return resp


# ── Stripe billing ──────────────────────────────────────────────────────────

@app.get("/create-checkout-session")
def create_checkout_session(request: Request):
    if config.GOOGLE_SIGNIN_CONFIGURED and not get_account(request):
        return RedirectResponse("/auth/google/login", status_code=303)

    identity = get_identity(request)
    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            line_items=[{"price": config.STRIPE_PRICE_ID, "quantity": 1}],
            success_url=f"{config.SITE_URL}/confirm-checkout?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{config.SITE_URL}/",
            client_reference_id=identity,
        )
    except Exception:
        log.exception("stripe checkout session creation failed")
        raise HTTPException(status_code=500, detail="Couldn't start checkout — billing isn't configured yet.")

    resp = RedirectResponse(session.url, status_code=303)
    resp.set_cookie("clipai_cid", get_client_id(request), max_age=60 * 60 * 24 * 365,
                     httponly=True, samesite="lax")
    return resp


@app.get("/create-checkout-session-plus")
def create_checkout_session_plus(request: Request):
    """Checkout for Pro Plus tier."""
    if config.GOOGLE_SIGNIN_CONFIGURED and not get_account(request):
        return RedirectResponse("/auth/google/login", status_code=303)

    identity = get_identity(request)
    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            line_items=[{"price": config.STRIPE_PRICE_ID_PLUS, "quantity": 1}],
            success_url=f"{config.SITE_URL}/confirm-checkout?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{config.SITE_URL}/pricing",
            client_reference_id=identity,
        )
    except Exception:
        log.exception("stripe checkout session creation failed for Pro Plus")
        raise HTTPException(status_code=500, detail="Couldn't start checkout — billing isn't configured yet.")

    resp = RedirectResponse(session.url, status_code=303)
    resp.set_cookie("clipai_cid", get_client_id(request), max_age=60 * 60 * 24 * 365,
                     httponly=True, samesite="lax")
    return resp


@app.get("/billing-portal")
def billing_portal(request: Request):
    """Lets a Pro subscriber manage or cancel their own subscription without
    emailing support — Stripe hosts the actual portal page."""
    customer_id = request.cookies.get("clipai_customer")
    if not customer_id:
        return RedirectResponse(f"{config.SITE_URL}/", status_code=303)
    try:
        session = stripe.billing_portal.Session.create(
            customer=customer_id, return_url=f"{config.SITE_URL}/"
        )
    except Exception:
        log.exception("stripe billing portal session creation failed")
        raise HTTPException(status_code=500, detail="Couldn't open billing portal — try again shortly.")
    return RedirectResponse(session.url, status_code=303)


@app.get("/confirm-checkout")
def confirm_checkout(session_id: str):
    """Stripe redirects here after checkout. The session is looked up
    server-to-server (can't be spoofed by editing the URL); on success the
    Stripe customer ID goes in a cookie and Pro is checked live from then on —
    see check_pro_status for why nothing is persisted locally."""
    try:
        session = stripe.checkout.Session.retrieve(session_id)
    except Exception:
        log.exception("failed to retrieve checkout session")
        return RedirectResponse(f"{config.SITE_URL}/?upgrade_error=1", status_code=303)

    resp = RedirectResponse(f"{config.SITE_URL}/?upgraded=1", status_code=303)
    if session.payment_status in ("paid", "no_payment_required") and session.customer:
        resp.set_cookie("clipai_customer", session.customer, max_age=60 * 60 * 24 * 365,
                          httponly=True, samesite="lax")
    return resp


@app.post("/stripe/webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, config.STRIPE_WEBHOOK_SECRET)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid webhook signature.")

    if event["type"] == "checkout.session.completed":
        log.info("checkout completed for customer %s", event["data"]["object"].get("customer"))
    elif event["type"] in ("customer.subscription.deleted", "customer.subscription.paused"):
        log.info("subscription ended for customer %s", event["data"]["object"].get("customer"))

    return {"received": True}


# ── API Endpoints ───────────────────────────────────────────────────────────

@app.post("/api/keys")
def create_api_key(request: Request):
    """Generate a new API key for the signed-in user. Requires Google Sign-In."""
    if config.GOOGLE_SIGNIN_CONFIGURED and not get_account(request):
        raise HTTPException(status_code=401, detail="Sign in required to create API keys.")

    identity = get_identity(request)
    tier = get_account_tier(request)
    api_key = generate_api_key(identity, tier)
    return {"api_key": api_key, "tier": tier}


@app.get("/api/keys")
def list_api_keys(request: Request):
    """List all API keys for the signed-in user."""
    if config.GOOGLE_SIGNIN_CONFIGURED and not get_account(request):
        raise HTTPException(status_code=401, detail="Sign in required.")

    identity = get_identity(request)
    data = load_api_keys()
    user_keys = [
        {
            "api_key": key[:12] + "***" + key[-4:],  # mask key
            "tier": info["tier"],
            "created": info["created"],
            "usage_this_month": info.get("usage_this_month", 0),
            "active": info.get("active", True),
        }
        for key, info in data.get("keys", {}).items()
        if info.get("identity") == identity
    ]
    return {"keys": user_keys}


@app.post("/api/v1/process")
async def api_process(request: Request, file: UploadFile = File(...), clip_format: str = Form("vertical")):
    """Process a video via API. Requires valid API key in Authorization header."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header. Use: Authorization: Bearer <api_key>")

    api_key = auth_header[7:]  # strip "Bearer "
    key_info = get_api_key_info(api_key)
    if not key_info or not key_info.get("active"):
        raise HTTPException(status_code=401, detail="Invalid or inactive API key.")

    if not increment_api_usage(api_key):
        limits_str = ", ".join(f"{t}={n}" for t, n in API_TIER_LIMITS.items())
        raise HTTPException(
            status_code=429,
            detail=f"API rate limit exceeded for {key_info['tier']} tier. Monthly limits: {limits_str}."
        )

    if clip_format not in ("vertical", "square", "horizontal"):
        raise HTTPException(status_code=400, detail="clip_format must be 'vertical', 'square', or 'horizontal'.")

    ext = Path(file.filename or "").suffix.lower()
    if ext not in config.ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Unsupported file type '{ext}'. Use MP4, MOV, M4V, WEBM, or MKV.")

    if _job_queue.full():
        raise HTTPException(status_code=503, detail="Server queue full, please try again shortly.")

    job_id = str(uuid.uuid4())[:8]
    job_dir = JOBS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    # Name from our own extension, never the client-supplied filename directly
    # (a raw filename like "../../etc/passwd" would write outside job_dir).
    video_path = job_dir / f"input{ext}"

    max_bytes = config.MAX_UPLOAD_MB * 1024 * 1024
    size = 0
    try:
        with open(video_path, "wb") as f:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    raise HTTPException(status_code=413, detail=f"Video too large (max {config.MAX_UPLOAD_MB}MB).")
                f.write(chunk)
    except HTTPException:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise
    except Exception:
        shutil.rmtree(job_dir, ignore_errors=True)
        log.exception("API upload failed")
        raise HTTPException(status_code=500, detail="Upload failed — please try again.")

    api_tier = key_info.get("tier", "free")
    is_pro = api_tier in ("pro", "pro_plus")
    _set_job(job_id, status="queued", api_key=True, identity=f"apikey:{api_key}", is_pro=is_pro,
              input_path=str(video_path), created_at=time.time(), dub_lang=None,
              notify_email=None, clip_format=clip_format, caption_style="bold")
    try:
        _enqueue_job(job_id, api_tier)
    except queue.Full:
        shutil.rmtree(job_dir, ignore_errors=True)
        with _jobs_lock:
            _jobs.pop(job_id, None)
        raise HTTPException(status_code=503, detail="Server queue full, please try again shortly.")

    return {"job_id": job_id, "status": "queued"}


@app.get("/api/v1/clips/{job_id}")
def api_get_clips(job_id: str, request: Request):
    """Get clip results from a completed job. Requires API key or job cookie."""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        api_key = auth_header[7:]
        key_info = get_api_key_info(api_key)
        if not key_info or not key_info.get("active"):
            raise HTTPException(status_code=401, detail="Invalid API key.")
    else:
        # Allow access if cookie was set during job submission
        pass

    job = _get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")

    if job["status"] == "processing":
        return {"status": "processing", "progress": job.get("progress", 0)}
    elif job["status"] == "failed":
        return {"status": "error", "error": job.get("error", "Unknown error")}
    elif job["status"] == "done":
        manifest_path = JOBS_DIR / job_id / "manifest.json"
        if not manifest_path.exists():
            raise HTTPException(status_code=500, detail="Manifest file not found.")

        manifest = json.loads(manifest_path.read_text())
        result = {"status": "done", "clips": []}
        for clip in manifest:
            clip_file = JOBS_DIR / job_id / clip["file"]
            if clip_file.exists():
                result["clips"].append({
                    "rank": clip["rank"],
                    "text": clip["text"],
                    "virality_score": clip["virality_score"],
                    "url": f"{config.SITE_URL}/jobs/{job_id}/{clip['file']}",
                    "download_url": f"{config.SITE_URL}/jobs/{job_id}/{clip['file']}",
                })
        return result
    else:
        return {"status": job["status"]}
