import json
import logging
import queue
import shutil
import threading
import time
import uuid
from pathlib import Path

import stripe
from fastapi import FastAPI, File, UploadFile, Request, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse

import auth
import config
import pipeline_lib

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("clipai")

BASE_DIR = Path(__file__).parent
JOBS_DIR = BASE_DIR / "jobs"
USAGE_FILE = BASE_DIR / "usage.json"

JOBS_DIR.mkdir(exist_ok=True)
if not USAGE_FILE.exists():
    USAGE_FILE.write_text("{}")

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
                job["input_path"], str(job_dir), n_clips=3, watermark=not job["is_pro"]
            )
            if not result["clips"]:
                raise RuntimeError("no usable clips found")
            clips = result["clips"]
            for clip in clips:
                clip["url"] = f"/jobs/{job_id}/{clip['file']}"
            _set_job(job_id, status="done", clips=clips, duration=result["duration"],
                     finished_at=time.time())
            # the source upload is no longer needed once clips exist — free the disk
            try:
                Path(job["input_path"]).unlink(missing_ok=True)
            except Exception:
                pass
        except Exception:
            log.exception("processing failed for job %s", job_id)
            shutil.rmtree(job_dir, ignore_errors=True)
            if not job["is_pro"]:
                refund_free_use(job["identity"])
            _set_job(job_id, status="failed",
                     error="Processing failed — the video may be too short, silent, or an unsupported codec. Your free video was not used up.",
                     finished_at=time.time())


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
async def process(request: Request, file: UploadFile = File(...)):
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

    if _job_queue.full():
        raise HTTPException(status_code=503, detail="We're at capacity right now — try again in a few minutes.")

    if not is_pro and not reserve_free_use(identity):
        raise HTTPException(status_code=402, detail="Free plan limit reached (1 video). Upgrade to Pro for unlimited clips.")

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
             input_path=str(input_path), created_at=time.time())
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
