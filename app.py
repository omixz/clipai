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
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse

import auth
import config
import email_lib
import pipeline_lib

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("clipai")

BASE_DIR = Path(__file__).parent
JOBS_DIR = BASE_DIR / "jobs"
USAGE_FILE = BASE_DIR / "usage.json"
API_KEYS_FILE = BASE_DIR / "api_keys.json"

JOBS_DIR.mkdir(exist_ok=True)
if not USAGE_FILE.exists():
    USAGE_FILE.write_text("{}")
if not API_KEYS_FILE.exists():
    API_KEYS_FILE.write_text("{}")

stripe.api_key = config.STRIPE_SECRET_KEY

app = FastAPI()

# ── Usage tracking (free-tier counter only — Pro is checked live via Stripe) ──

_usage_lock = threading.Lock()


def load_usage():
    with _usage_lock:
        return json.loads(USAGE_FILE.read_text())


def save_usage(data):
    with _usage_lock:
        USAGE_FILE.write_text(json.dumps(data))


def reserve_free_use(cid) -> bool:
    """Atomically check-and-increment the free counter. Returns False if the
    limit is already reached. Single lock section so two concurrent uploads
    can't both pass the check."""
    with _usage_lock:
        data = json.loads(USAGE_FILE.read_text())
        rec = data.get(cid, {"used": 0})
        if rec["used"] >= config.FREE_LIMIT:
            return False
        rec["used"] += 1
        data[cid] = rec
        USAGE_FILE.write_text(json.dumps(data))
        return True


def refund_free_use(cid):
    with _usage_lock:
        data = json.loads(USAGE_FILE.read_text())
        rec = data.get(cid, {"used": 0})
        rec["used"] = max(0, rec["used"] - 1)
        data[cid] = rec
        USAGE_FILE.write_text(json.dumps(data))


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


def check_pro_status(request: Request) -> bool:
    """Pro status is checked live against Stripe via a customer ID cookie set
    after checkout — never from local storage, which is wiped on every restart
    on disk-less hosts (Render free tier included)."""
    customer_id = request.cookies.get("clipai_customer")
    if not customer_id:
        return False
    try:
        subs = stripe.Subscription.list(customer=customer_id, status="active", limit=1)
        return len(subs.data) > 0
    except Exception:
        log.exception("Stripe subscription lookup failed")
        return False


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
            "usage_this_month": 0,
            "active": True,
        }
        API_KEYS_FILE.write_text(json.dumps(data, indent=2))
    return api_key


def get_api_key_info(api_key: str) -> dict | None:
    """Get info about an API key."""
    data = load_api_keys()
    return data.get("keys", {}).get(api_key)


def increment_api_usage(api_key: str) -> bool:
    """Increment usage counter for an API key. Returns False if rate limited."""
    info = get_api_key_info(api_key)
    if not info:
        return False

    limits = {"free": 100, "pro": 500, "pro_plus": 2000}
    monthly_limit = limits.get(info.get("tier", "free"), 100)

    with _api_lock:
        data = json.loads(API_KEYS_FILE.read_text())
        key_data = data.get("keys", {}).get(api_key)
        if not key_data or not key_data.get("active"):
            return False

        key_data["usage_this_month"] = key_data.get("usage_this_month", 0) + 1
        if key_data["usage_this_month"] > monthly_limit:
            return False

        API_KEYS_FILE.write_text(json.dumps(data, indent=2))
    return True


# ── Background job queue ─────────────────────────────────────────────────────
# One worker thread, one job at a time: two concurrent Whisper+ffmpeg runs
# would OOM a 512MB instance. The upload endpoint returns a job id instantly
# and the front-end polls /job/{id} — this keeps the site responsive during
# the minutes-long processing and avoids gateway timeouts on long requests.

MAX_QUEUE = 10
JOB_MAX_AGE_SECONDS = 24 * 60 * 60

_jobs: dict = {}
_jobs_lock = threading.Lock()
_job_queue: "queue.Queue[str]" = queue.Queue(maxsize=MAX_QUEUE)


def _set_job(job_id, **fields):
    with _jobs_lock:
        _jobs.setdefault(job_id, {}).update(fields)


def _get_job(job_id):
    with _jobs_lock:
        job = _jobs.get(job_id)
        return dict(job) if job else None


def _queue_position(job_id):
    with _jobs_lock:
        queued = [jid for jid, j in _jobs.items() if j.get("status") == "queued"]
    try:
        return queued.index(job_id) + 1
    except ValueError:
        return None


def _worker():
    while True:
        job_id = _job_queue.get()
        job = _get_job(job_id)
        if not job:
            continue
        _set_job(job_id, status="processing", started_at=time.time())
        job_dir = JOBS_DIR / job_id
        try:
            result = pipeline_lib.process_video(
                job["input_path"], str(job_dir), n_clips=3, watermark=not job["is_pro"],
                dub_lang=job.get("dub_lang"),
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
            if not job["is_pro"]:
                refund_free_use(job["identity"])
            _set_job(job_id, status="failed", error=str(e), finished_at=time.time())
            if job.get("notify_email"):
                email_lib.send_failed_email(job["notify_email"], str(e))
        except Exception as e:
            log.exception("processing failed for job %s", job_id)
            shutil.rmtree(job_dir, ignore_errors=True)
            if not job["is_pro"]:
                refund_free_use(job["identity"])
            error_str = str(e).lower()
            if "no speech" in error_str or "no usable" in error_str:
                error_msg = "No clear speech detected. Try a video with louder, clearer audio or more talking."
            elif "too short" in error_str or "minimum" in error_str:
                error_msg = "Video is too short. Try a video at least 30 seconds long."
            elif "codec" in error_str or "unsupported" in error_str:
                error_msg = "Video format not supported. Try MP4, MOV, WEBM, or MKV."
            else:
                error_msg = "Processing failed — the video may be too short, silent, or an unsupported codec. Try a longer video with clearer speech."
            error_msg += " Your free video was not used up; please try again."
            _set_job(job_id, status="failed", error=error_msg, finished_at=time.time())
            if job.get("notify_email"):
                email_lib.send_failed_email(job["notify_email"], error_msg)


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
    rec = load_usage().get(identity, {"used": 0})
    is_pro = check_pro_status(request)
    remaining = None if is_pro else max(0, config.FREE_LIMIT - rec["used"])
    resp = JSONResponse({
        "used": rec["used"], "pro": is_pro, "limit": config.FREE_LIMIT, "remaining": remaining,
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
                   notify_email: str | None = Form(None), clip_format: str = Form("vertical")):
    if clip_format not in ("vertical", "square", "horizontal"):
        raise HTTPException(status_code=400, detail="clip_format must be 'vertical', 'square', or 'horizontal'.")
    cleanup_old_jobs()

    if not check_ip_rate_limit(get_request_ip(request)):
        raise HTTPException(status_code=429, detail="Too many uploads from this network — please try again later.")

    if config.GOOGLE_SIGNIN_CONFIGURED and not get_account(request):
        raise HTTPException(status_code=401, detail="Sign in with Google to process a video.")

    identity = get_identity(request)
    is_pro = check_pro_status(request)

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

    if not is_pro and not reserve_free_use(identity):
        raise HTTPException(status_code=402, detail=f"Free plan limit reached ({config.FREE_LIMIT} videos/month). Upgrade to Pro for unlimited clips.")

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
        if not is_pro:
            refund_free_use(identity)
        raise
    except Exception:
        shutil.rmtree(job_dir, ignore_errors=True)
        if not is_pro:
            refund_free_use(identity)
        log.exception("upload failed")
        raise HTTPException(status_code=500, detail="Upload failed — please try again.")

    _set_job(job_id, status="queued", identity=identity, is_pro=is_pro,
             input_path=str(input_path), created_at=time.time(), dub_lang=dub_lang,
             notify_email=notify_email, clip_format=clip_format)
    try:
        _job_queue.put_nowait(job_id)
    except queue.Full:
        shutil.rmtree(job_dir, ignore_errors=True)
        with _jobs_lock:
            _jobs.pop(job_id, None)
        if not is_pro:
            refund_free_use(identity)
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
    return FileResponse(
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
    tier = "pro" if check_pro_status(request) else "free"
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
        raise HTTPException(
            status_code=429,
            detail=f"API rate limit exceeded for {key_info['tier']} tier. Monthly limits: free=100, pro=500, pro_plus=2000."
        )

    if clip_format not in ("vertical", "square", "horizontal"):
        raise HTTPException(status_code=400, detail="clip_format must be 'vertical', 'square', or 'horizontal'.")

    if file.size and file.size > config.MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(status_code=413, detail=f"Video too large (max {config.MAX_UPLOAD_MB}MB).")

    job_id = str(uuid.uuid4())[:8]
    job_dir = JOBS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    video_path = job_dir / file.filename
    with open(video_path, "wb") as f:
        content = await file.read()
        f.write(content)

    _set_job(job_id, status="queued", api_key=True, clip_format=clip_format)
    try:
        _job_queue.put_nowait(job_id)
    except queue.Full:
        _set_job(job_id, status="error", error="Server queue full, please try again shortly.")
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
    elif job["status"] == "error":
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
