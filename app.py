import hashlib
import hmac
import itertools
import json
import logging
import os
import queue
import re
import secrets
import shutil
import threading
import time
import uuid
import zipfile
from html import escape
from io import BytesIO
from pathlib import Path

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

from fastapi import FastAPI, File, Form, UploadFile, Request, HTTPException
from fastapi.responses import (
    FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response, StreamingResponse,
)

import auth
import backgrounds
import billing_lib
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
# identity -> PayPal subscription id, linked the moment checkout is created
# (see billing_lib.create_subscription for why that's safe pre-approval).
# Tier is never trusted from this file alone -- get_account_tier always
# makes a live billing_lib.get_subscription() call with the id found here,
# same "never trust local storage for Pro status" principle the Stripe
# integration this replaced was built around.
PAYPAL_SUBS_FILE = JOBS_DIR / "paypal_subscriptions.json"
# email -> tier ("pro" | "pro_plus"), set manually by an admin (see the
# /admin/billing routes below) after a direct bank transfer payment shows
# up -- there's no processor API to check status against for this path, so
# unlike every other tier source in this file, this one genuinely IS the
# trusted source of truth rather than a link to something checked live.
# Keyed by email (not `identity`) since that's what an admin actually has
# on hand when a payment arrives, and only ever consulted for a *signed-in*
# account (see get_account_tier), so it can't be spoofed by an anonymous
# cookie claiming someone else's email.
MANUAL_GRANTS_FILE = JOBS_DIR / "manual_grants.json"


def _atomic_write_text(path: Path, text: str):
    """Path.write_text() truncates the file and then writes -- not atomic.
    This app can genuinely OOM under Whisper/ffmpeg's peak memory use (a
    documented, real risk, not hypothetical), and usage.json gets written on
    every single video processed -- a crash landing mid-write leaves a
    truncated file whose next json.loads() raises uncaught, 500ing every
    route that touches usage/API-key tracking until someone manually repairs
    the file on the server. Writing to a temp file in the same directory
    then os.replace()-ing over the target is atomic on POSIX (same
    filesystem): a crash mid-write leaves the temp file corrupt but the real
    file completely untouched."""
    tmp_path = path.with_name(f"{path.name}.tmp{os.getpid()}")
    tmp_path.write_text(text)
    os.replace(tmp_path, path)


JOBS_DIR.mkdir(exist_ok=True)
if not USAGE_FILE.exists():
    _atomic_write_text(USAGE_FILE, "{}")
if not API_KEYS_FILE.exists():
    _atomic_write_text(API_KEYS_FILE, "{}")
if not PAYPAL_SUBS_FILE.exists():
    _atomic_write_text(PAYPAL_SUBS_FILE, "{}")
if not MANUAL_GRANTS_FILE.exists():
    _atomic_write_text(MANUAL_GRANTS_FILE, "{}")

_paypal_subs_lock = threading.Lock()
_manual_grants_lock = threading.Lock()


def load_paypal_subs() -> dict:
    with _paypal_subs_lock:
        return json.loads(PAYPAL_SUBS_FILE.read_text())


def link_paypal_subscription(identity: str, subscription_id: str):
    with _paypal_subs_lock:
        data = json.loads(PAYPAL_SUBS_FILE.read_text())
        data[identity] = subscription_id
        _atomic_write_text(PAYPAL_SUBS_FILE, json.dumps(data, indent=2))


def load_manual_grants() -> dict:
    with _manual_grants_lock:
        return json.loads(MANUAL_GRANTS_FILE.read_text())


def set_manual_grant(email: str, tier: str):
    with _manual_grants_lock:
        data = json.loads(MANUAL_GRANTS_FILE.read_text())
        data[email.lower()] = tier
        _atomic_write_text(MANUAL_GRANTS_FILE, json.dumps(data, indent=2))


def remove_manual_grant(email: str):
    with _manual_grants_lock:
        data = json.loads(MANUAL_GRANTS_FILE.read_text())
        data.pop(email.lower(), None)
        _atomic_write_text(MANUAL_GRANTS_FILE, json.dumps(data, indent=2))


def is_admin(request: Request) -> bool:
    if not config.ADMIN_EMAILS:
        return False
    account = get_account(request)
    return bool(account and account.get("email", "").lower() in config.ADMIN_EMAILS)


app = FastAPI()


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    """Baseline response headers with no functional dependency on the rest
    of the app -- deliberately NOT including Content-Security-Policy here.
    Every HTML page in this app relies on inline <script>/<style> tags, so a
    CSP strict enough to matter would need a nonce-based refactor across all
    of them to avoid silently breaking the site; a CSP loose enough not to
    (allowing 'unsafe-inline') would give essentially none of CSP's real
    protection anyway. Worth doing properly as dedicated follow-up work, not
    as a bolt-on here."""
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    # DENY, not SAMEORIGIN -- nothing in this app is meant to be iframed,
    # including by itself, so there's no reason to allow same-origin framing
    # either (which SAMEORIGIN would still permit e.g. a compromised
    # subdomain or the site framing its own upload form for a clickjacking
    # trick against the paywall/upgrade buttons).
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    # This app never uses any of these; explicitly disabling them means an
    # embedded/compromised third-party script (e.g. if the AdSense slot ever
    # got compromised) can't invoke them either.
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=()"
    if config.COOKIE_SECURE:
        # Only when actually serving over HTTPS (see config.COOKIE_SECURE) --
        # sending this over plain HTTP does nothing (browsers ignore
        # Strict-Transport-Security on non-HTTPS responses) and there's no
        # reason to even try during local http://localhost:8000 dev.
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


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
            _atomic_write_text(USAGE_FILE, json.dumps(data))
            return False
        rec["used"] += 1
        data[key] = rec
        _atomic_write_text(USAGE_FILE, json.dumps(data))
        return True


def refund_use(key):
    with _usage_lock:
        data = json.loads(USAGE_FILE.read_text())
        rec = _period_rec(data, key)
        rec["used"] = max(0, rec["used"] - 1)
        data[key] = rec
        _atomic_write_text(USAGE_FILE, json.dumps(data))


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


def set_session_cookie(resp, name: str, value: str, max_age: int):
    """Every cookie this app sets goes through here so none of them can
    individually forget httponly/samesite/secure. secure is derived from
    config.SITE_URL's scheme (see there) rather than hardcoded True, so
    local dev over http://localhost:8000 still works — a browser silently
    drops a Secure cookie set over a plain HTTP response."""
    resp.set_cookie(name, value, max_age=max_age, httponly=True, samesite="lax", secure=config.COOKIE_SECURE)


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
    """'free' | 'pro' | 'pro_plus'. Three sources, checked in order:
    1. Admin allowlist (config.ADMIN_EMAILS) -- always Pro Plus.
    2. Manual grants (MANUAL_GRANTS_FILE) -- the active path while billing
       is direct bank transfer with no processor API to check against; an
       admin sets this by hand after seeing a payment land (see
       /admin/billing). This is the one tier source in this file that's
       genuinely trusted from local storage rather than checked live,
       because there's nothing live to check it against.
    3. PayPal (PAYPAL_SUBS_FILE) -- keyed off `identity` rather than a
       separate signed cookie, and always double-checked live against
       billing_lib.get_subscription() so a cancelled subscription stops
       granting Pro without depending on any webhook actually arriving."""
    if config.ADMIN_EMAILS:
        account = get_account(request)
        if account and account.get("email", "").lower() in config.ADMIN_EMAILS:
            return "pro_plus"

    account = get_account(request)
    if account:
        granted = load_manual_grants().get(account.get("email", "").lower())
        if granted in ("pro", "pro_plus"):
            return granted

    identity = get_identity(request)
    subscription_id = load_paypal_subs().get(identity)
    if not subscription_id:
        return "free"
    sub = billing_lib.get_subscription(subscription_id)
    if not sub or sub["status"] not in billing_lib.PAID_STATUSES:
        return "free"
    return billing_lib.tier_for_plan(sub["plan_id"])


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
        _atomic_write_text(API_KEYS_FILE, json.dumps(data, indent=2))


def generate_api_key(identity: str, tier: str = "free", max_keys: int | None = None) -> str | None:
    """Generate a new API key for a user. Tier can be 'free', 'pro', or
    'pro_plus'. If max_keys is set, the existing-count check and the insert
    happen under the same lock so two concurrent requests can't both slip
    past the cap the way a check-then-act version would; returns None if the
    cap is already hit."""
    api_key = f"pk_{uuid.uuid4().hex[:32]}"
    with _api_lock:
        data = json.loads(API_KEYS_FILE.read_text())
        if "keys" not in data:
            data["keys"] = {}
        if max_keys is not None:
            active_count = sum(
                1 for info in data["keys"].values()
                if info.get("identity") == identity and info.get("active")
            )
            if active_count >= max_keys:
                return None
        data["keys"][api_key] = {
            # A separate, non-secret id for referencing this key from the UI
            # (revoke button, etc.) without ever having to show the actual
            # key again — list_api_keys only shows a masked version of that.
            "key_id": uuid.uuid4().hex[:12],
            "identity": identity,
            "tier": tier,
            "created": time.time(),
            "period": current_period(),
            "usage_this_month": 0,
            "active": True,
        }
        _atomic_write_text(API_KEYS_FILE, json.dumps(data, indent=2))
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
            _atomic_write_text(API_KEYS_FILE, json.dumps(data, indent=2))  # persist rollover even on rejection
            return False

        key_data["usage_this_month"] += 1
        _atomic_write_text(API_KEYS_FILE, json.dumps(data, indent=2))
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
                watermark_text=job.get("watermark_text"), watermark_color=job.get("watermark_color"),
                background_path=job.get("background_path"),
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
            # Same idea for a custom split-screen background upload -- but
            # only when it's actually a job-scoped upload (lives inside this
            # job's own directory), never a shared stock library file from
            # assets/backgrounds/, which every future job needs to keep using.
            bg_path = job.get("background_path")
            if bg_path and str(job_dir) in bg_path:
                try:
                    Path(bg_path).unlink(missing_ok=True)
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
    return (BASE_DIR / "index.html").read_text().replace("__SITE_URL__", config.SITE_URL)


@app.get("/pricing", response_class=HTMLResponse)
def pricing():
    return (BASE_DIR / "pricing.html").read_text().replace("__SITE_URL__", config.SITE_URL)


@app.get("/api/docs", response_class=HTMLResponse)
def api_docs():
    return (BASE_DIR / "api_docs.html").read_text().replace("__SITE_URL__", config.SITE_URL)


@app.get("/settings", response_class=HTMLResponse)
def settings(request: Request):
    if config.GOOGLE_SIGNIN_CONFIGURED and not get_account(request):
        return RedirectResponse("/auth/google/login", status_code=303)
    return (BASE_DIR / "settings.html").read_text()


@app.get("/privacy", response_class=HTMLResponse)
def privacy():
    return (BASE_DIR / "privacy.html").read_text().replace("__CONTACT_EMAIL__", config.CONTACT_EMAIL).replace("__SITE_URL__", config.SITE_URL)


@app.get("/terms", response_class=HTMLResponse)
def terms():
    return (BASE_DIR / "terms.html").read_text().replace("__CONTACT_EMAIL__", config.CONTACT_EMAIL).replace("__SITE_URL__", config.SITE_URL)


@app.get("/healthz", response_class=PlainTextResponse)
def healthz():
    return "ok"


@app.get("/robots.txt", response_class=PlainTextResponse)
def robots():
    return (
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /jobs/\n"
        "Disallow: /settings\n"
        "Disallow: /api/\n"
        # /api/docs is the one /api/... route actually meant to be indexed
        # (it's in the sitemap) -- carved back out of the broad disallow
        # above rather than making that disallow narrower and risking
        # missing some other /api/... path that shouldn't be crawled.
        "Allow: /api/docs\n"
        f"Sitemap: {config.SITE_URL}/sitemap.xml\n"
    )


@app.get("/sitemap.xml")
def sitemap():
    # Every real, indexable, publicly-crawlable page -- not /settings (private/
    # authenticated, already noindex'd) and not /jobs/... or /api/... (already
    # disallowed above; no SEO value and the job ones are ephemeral anyway).
    pages = ["", "/pricing", "/api/docs", "/privacy", "/terms"]
    urls = "".join(f"<url><loc>{config.SITE_URL}{p}</loc></url>" for p in pages)
    xml = f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>'
    return Response(content=xml, media_type="application/xml")


@app.get("/api/backgrounds")
def api_backgrounds():
    """Split-screen (Pro/Pro Plus) stock background library, for the
    frontend to render as options. `available` reflects whether the actual
    video file has been dropped into assets/backgrounds/ yet (see that
    folder's README) -- registering an id in backgrounds.py doesn't require
    the file to exist, so the UI can show it as coming-soon instead of just
    omitting it."""
    return {"backgrounds": backgrounds.list_backgrounds()}


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
    set_session_cookie(resp, "clipai_cid", get_client_id(request), max_age=60 * 60 * 24 * 365)
    return resp


def get_request_ip(request: Request) -> str:
    """Used only for check_ip_rate_limit, so this needs to resist spoofing,
    not just be "a" client IP. Both supported deployments (Caddy on Oracle,
    Render's own edge) sit as exactly one reverse-proxy hop in front of this
    app, and a reverse proxy APPENDS the real peer IP to any pre-existing
    X-Forwarded-For header rather than replacing it -- so the last entry is
    the one our own proxy can vouch for, while every earlier entry is
    whatever the client itself sent and freely spoofable. Taking the FIRST
    entry (the naive/common approach) would let any client bypass the entire
    per-IP rate limit just by sending its own X-Forwarded-For header with a
    fake IP prepended."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[-1].strip()
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
                   caption_style: str = Form("bold"), watermark_text: str | None = Form(None),
                   watermark_color: str | None = Form(None), background_id: str | None = Form(None),
                   background_file: UploadFile | None = File(None)):
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

    # Custom watermark is a Pro Plus perk (settings.html gates the UI to it
    # too) -- silently ignored rather than erroring for any other tier, since
    # a lower-tier account could still have a stale value in localStorage.
    if tier != "pro_plus":
        watermark_text = None
        watermark_color = None
    elif watermark_text and len(watermark_text) > 40:
        raise HTTPException(status_code=400, detail="Watermark text is limited to 40 characters.")

    ext = Path(file.filename or "").suffix.lower()
    if ext not in config.ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Unsupported file type '{ext}'. Use MP4, MOV, M4V, WEBM, or MKV.")

    if dub_lang:
        import dub_lib
        if dub_lang not in dub_lib.DUB_LANGUAGES:
            raise HTTPException(status_code=400, detail="Unsupported dub language.")
        if not is_pro:
            raise HTTPException(status_code=402, detail="Dubbing is a Pro feature. Upgrade to Pro to dub clips into other languages.")

    # Split-screen (bottom-half background) is a Pro/Pro Plus perk, same
    # silently-ignored-for-lower-tiers treatment as the custom watermark --
    # not an error, since settings/UI state could be stale.
    if not is_pro:
        background_id = None
        background_file = None
    if background_id and dub_lang:
        raise HTTPException(status_code=400, detail="Split-screen background and dubbing can't be used together yet.")
    if background_id and background_id != "custom" and background_id not in backgrounds.STOCK_BACKGROUNDS:
        raise HTTPException(status_code=400, detail="Unknown background id.")
    if background_id and background_id != "custom" and not backgrounds.get_background_path(background_id):
        raise HTTPException(status_code=400, detail="That background isn't available yet — pick another one.")
    if background_id == "custom" and not background_file:
        raise HTTPException(status_code=400, detail="Upload a background video, or pick one from the library instead.")
    if background_id != "custom":
        background_file = None  # ignore a stray upload if a stock id was actually selected

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

    # Full UUID4 hex (128 bits), not a truncated slice -- job_id is the ONLY
    # thing gating unauthenticated access to a user's uploaded clips via
    # /job/{id}, /jobs/{id}/{filename}, and /jobs/{id}/download/all (none of
    # which check ownership or rate-limit reads). An 8-hex-char id (32 bits,
    # ~4.3B combinations) is brute-forceable in hours at a few hundred
    # requests/sec against those cheap lookup endpoints; 128 bits isn't.
    job_id = uuid.uuid4().hex
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

    # Resolve the actual background path now, at upload time -- a bad/oversized
    # custom background should fail fast with a clear error and never consume
    # a queue slot or a monthly quota unit, the same reasoning as every other
    # validation above happening before reserve_use.
    background_path = None
    if background_id and background_id != "custom":
        background_path = backgrounds.get_background_path(background_id)  # re-checked; can't race-vanish mid-request either way
    elif background_id == "custom" and background_file:
        bg_ext = Path(background_file.filename or "").suffix.lower()
        if bg_ext not in config.ALLOWED_EXTENSIONS:
            shutil.rmtree(job_dir, ignore_errors=True)
            refund_use(identity)
            raise HTTPException(status_code=400, detail=f"Unsupported background file type '{bg_ext}'.")
        bg_path = job_dir / f"background{bg_ext}"
        bg_max_bytes = config.BACKGROUND_UPLOAD_MB * 1024 * 1024
        bg_size = 0
        try:
            with open(bg_path, "wb") as f:
                while chunk := await background_file.read(1024 * 1024):
                    bg_size += len(chunk)
                    if bg_size > bg_max_bytes:
                        raise HTTPException(status_code=413, detail=f"Background video too large — the limit is {config.BACKGROUND_UPLOAD_MB}MB.")
                    f.write(chunk)
        except HTTPException:
            shutil.rmtree(job_dir, ignore_errors=True)
            refund_use(identity)
            raise
        except Exception:
            shutil.rmtree(job_dir, ignore_errors=True)
            refund_use(identity)
            log.exception("background upload failed")
            raise HTTPException(status_code=500, detail="Background upload failed — please try again.")

        bg_duration = pipeline_lib.probe_duration(str(bg_path))
        if bg_duration and bg_duration > config.MAX_BACKGROUND_DURATION_SEC:
            shutil.rmtree(job_dir, ignore_errors=True)
            refund_use(identity)
            raise HTTPException(status_code=400, detail=f"Background video is too long — the limit is {config.MAX_BACKGROUND_DURATION_SEC}s.")
        background_path = str(bg_path)

    _set_job(job_id, status="queued", identity=identity, is_pro=is_pro,
             input_path=str(input_path), created_at=time.time(), dub_lang=dub_lang,
             notify_email=notify_email, clip_format=clip_format, caption_style=caption_style,
             watermark_text=watermark_text, watermark_color=watermark_color, background_path=background_path)
    try:
        _enqueue_job(job_id, tier)
    except queue.Full:
        shutil.rmtree(job_dir, ignore_errors=True)
        with _jobs_lock:
            _jobs.pop(job_id, None)
        refund_use(identity)
        raise HTTPException(status_code=503, detail="We're at capacity right now — try again in a few minutes.")

    resp = JSONResponse({"job_id": job_id, "status": "queued"})
    set_session_cookie(resp, "clipai_cid", get_client_id(request), max_age=60 * 60 * 24 * 365)
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


# Every job_id this app generates is uuid.uuid4().hex -- exactly this shape.
# Validating the request path parameter against it up front means job_id can
# never be crafted to reference a sibling file that happens to live directly
# in JOBS_DIR (usage.json, api_keys.json -- the latter holds every live API
# key in plaintext) via something like job_id="x", filename="../api_keys.json".
# A pure containment check alone doesn't catch that: it resolves to
# JOBS_DIR/api_keys.json, which genuinely IS "inside JOBS_DIR", just not
# inside that specific job's own subdirectory -- caught in review by testing
# the fix against exactly this case, not something the naive version handled.
_JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def _job_dir_path(job_id: str) -> Path:
    if not _JOB_ID_RE.match(job_id):
        raise HTTPException(status_code=400, detail="Invalid job ID.")
    return JOBS_DIR / job_id


def _safe_job_path(job_id: str, *parts: str) -> Path:
    """Resolves this job's own subdirectory joined with `parts` and verifies
    the result is contained within THAT subdirectory specifically (not just
    somewhere generically inside JOBS_DIR) -- see _job_dir_path's docstring
    for why the distinction matters. This is a positive containment check
    (resolve + is_relative_to) rather than a blocklist of specific traversal
    substrings like "/" or ".." -- a blocklist has to correctly anticipate
    every bypass technique, while this can't be bypassed by anything that
    isn't literally still inside the directory once resolved, including e.g.
    an absolute path segment (which pathlib's / operator would otherwise let
    silently discard everything joined before it)."""
    job_dir = _job_dir_path(job_id).resolve()
    candidate = job_dir.joinpath(*parts).resolve()
    if not candidate.is_relative_to(job_dir):
        raise HTTPException(status_code=400, detail="Invalid path.")
    return candidate


@app.get("/jobs/{job_id}/{filename}")
def get_clip(job_id: str, filename: str):
    path = _safe_job_path(job_id, filename)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Not found.")
    return FileResponse(str(path))


@app.get("/jobs/{job_id}/download/all")
def download_all_clips(job_id: str):
    job_dir = _safe_job_path(job_id)
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
    set_session_cookie(resp, auth.STATE_COOKIE, state, max_age=600)
    return resp


@app.get("/auth/google/callback")
def google_callback(request: Request, code: str | None = None, state: str | None = None, error: str | None = None):
    if error:
        return RedirectResponse(f"{config.SITE_URL}/?auth_error=1", status_code=303)

    expected_state = request.cookies.get(auth.STATE_COOKIE)
    if not code or not state or not expected_state or not secrets.compare_digest(state, expected_state):
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
    set_session_cookie(resp, auth.ACCOUNT_COOKIE, account_token, max_age=auth.ACCOUNT_MAX_AGE)
    resp.delete_cookie(auth.STATE_COOKIE)
    return resp


@app.get("/auth/logout")
def logout():
    resp = RedirectResponse(f"{config.SITE_URL}/", status_code=303)
    resp.delete_cookie(auth.ACCOUNT_COOKIE)
    return resp


# ── Direct bank transfer billing ────────────────────────────────────────────
# No processor in the loop -- see config.BANK_PAYMENT_CONFIGURED's comment.
# Takes priority over PayPal in checkout/tier-check when configured.

_TIER_LABELS = {"pro": "Pro", "pro_plus": "Pro Plus"}
_TIER_PRICES = {"pro": "$15", "pro_plus": "$29"}


def _bank_transfer_reference(email: str, tier: str) -> str:
    """Deterministic per-(email, tier) code so the same customer always sees
    the same reference on repeat visits, but it's HMAC'd with
    SESSION_SECRET_KEY so an outsider can't compute or guess another
    customer's reference from their email alone."""
    digest = hmac.new(config.SESSION_SECRET_KEY.encode(), f"{email.lower()}:{tier}".encode(), hashlib.sha256).hexdigest()
    return digest[:8].upper()


def _render_bank_transfer_page(email: str, tier: str) -> str:
    html = (BASE_DIR / "bank_transfer.html").read_text()
    payid_row = (
        f'<div class="row"><span class="label">PayID</span><span class="value">{config.BANK_PAYID}</span></div>'
        if config.BANK_PAYID else ""
    )
    return (
        html
        .replace("__SITE_URL__", config.SITE_URL)
        .replace("__CONTACT_EMAIL__", config.CONTACT_EMAIL)
        .replace("__TIER_LABEL__", _TIER_LABELS[tier])
        .replace("__PRICE__", _TIER_PRICES[tier])
        .replace("__BANK_NAME__", config.BANK_NAME)
        .replace("__ACCOUNT_NAME__", config.BANK_ACCOUNT_NAME)
        .replace("__ACCOUNT_NUMBER__", config.BANK_ACCOUNT_NUMBER)
        .replace("__BSB__", config.BANK_BSB)
        .replace("__PAYID_ROW__", payid_row)
        .replace("__REFERENCE__", _bank_transfer_reference(email, tier))
        .replace("__EMAIL__", email)
    )


def _start_paypal_checkout(request: Request, plan_id: str):
    identity = get_identity(request)
    try:
        subscription_id, approve_url = billing_lib.create_subscription(
            plan_id, identity,
            return_url=f"{config.SITE_URL}/confirm-checkout",
            cancel_url=f"{config.SITE_URL}/",
        )
        # Safe to link immediately -- see create_subscription's docstring.
        # An abandoned/never-approved checkout just leaves a subscription id
        # that permanently reads as non-paid on every live status check.
        link_paypal_subscription(identity, subscription_id)
    except Exception:
        log.exception("paypal subscription creation failed")
        raise HTTPException(status_code=500, detail="Couldn't start checkout — billing isn't configured yet.")

    resp = RedirectResponse(approve_url, status_code=303)
    set_session_cookie(resp, "clipai_cid", get_client_id(request), max_age=60 * 60 * 24 * 365)
    return resp


def _checkout(request: Request, tier: str, plan_id: str):
    if config.BANK_PAYMENT_CONFIGURED:
        # The reference code is keyed by email, so an account is required
        # for this path regardless of GOOGLE_SIGNIN_CONFIGURED's normal
        # soft-requirement (there's no anonymous equivalent that a human
        # can manually match a bank deposit against).
        account = get_account(request)
        if not account:
            return RedirectResponse("/auth/google/login", status_code=303)
        return HTMLResponse(_render_bank_transfer_page(account["email"], tier))
    if config.GOOGLE_SIGNIN_CONFIGURED and not get_account(request):
        return RedirectResponse("/auth/google/login", status_code=303)
    return _start_paypal_checkout(request, plan_id)


@app.get("/create-checkout-session")
def create_checkout_session(request: Request):
    return _checkout(request, "pro", config.PAYPAL_PLAN_ID_PRO)


@app.get("/create-checkout-session-plus")
def create_checkout_session_plus(request: Request):
    """Checkout for Pro Plus tier."""
    return _checkout(request, "pro_plus", config.PAYPAL_PLAN_ID_PLUS)


@app.get("/billing-portal")
def billing_portal(request: Request):
    """PayPal has no per-subscription self-serve portal link the way Stripe
    / Lemon Squeezy did (no API returns one) — the closest equivalent is
    PayPal's own automatic-payments management page under the subscriber's
    own PayPal account, which they'd already be logged into to have
    subscribed in the first place."""
    identity = get_identity(request)
    if not load_paypal_subs().get(identity):
        return RedirectResponse(f"{config.SITE_URL}/", status_code=303)
    return RedirectResponse("https://www.paypal.com/myaccount/autopay/", status_code=303)


@app.get("/confirm-checkout")
def confirm_checkout(request: Request):
    """PayPal redirects here right after the buyer approves the
    subscription. The subscription id was already linked to `identity` at
    creation time (see _start_paypal_checkout), but approval itself can lag
    a moment behind the redirect, so this polls PayPal's live status
    briefly rather than trusting anything in the redirect URL itself."""
    identity = get_identity(request)
    subscription_id = load_paypal_subs().get(identity)
    if subscription_id:
        for _ in range(5):
            sub = billing_lib.get_subscription(subscription_id)
            if sub and sub["status"] in billing_lib.PAID_STATUSES:
                return RedirectResponse(f"{config.SITE_URL}/?upgraded=1", status_code=303)
            time.sleep(1)
    return RedirectResponse(f"{config.SITE_URL}/?upgrade_pending=1", status_code=303)


@app.post("/paypal/webhook")
async def paypal_webhook(request: Request):
    payload = await request.body()
    if not billing_lib.verify_webhook_signature(dict(request.headers), payload):
        raise HTTPException(status_code=400, detail="Invalid webhook signature.")

    event = json.loads(payload)
    event_type = event.get("event_type", "")

    # No local state to update on either event -- get_account_tier always
    # checks PayPal live, and the subscription id was already linked at
    # checkout creation, so this is purely for visibility/logging.
    if event_type in ("BILLING.SUBSCRIPTION.ACTIVATED", "BILLING.SUBSCRIPTION.CANCELLED"):
        log.info("paypal %s: %s", event_type, event.get("resource", {}).get("id"))

    return {"received": True}


# ── Admin: manual billing grants (direct bank transfer path) ────────────────

def _render_admin_billing_page() -> str:
    grants = load_manual_grants()
    if grants:
        rows = "".join(
            f'<tr><td>{escape(email)}</td><td class="tier-{escape(tier)}">{escape(_TIER_LABELS.get(tier, tier))}</td>'
            f'<td><form class="inline" method="post" action="/admin/billing/revoke">'
            f'<input type="hidden" name="email" value="{escape(email)}">'
            f'<button class="revoke" type="submit">Revoke</button></form></td></tr>'
            for email, tier in sorted(grants.items())
        )
        table = f"<table><tr><th>Email</th><th>Tier</th><th></th></tr>{rows}</table>"
    else:
        table = '<p class="empty">No manual grants yet.</p>'
    return (BASE_DIR / "admin_billing.html").read_text().replace("__GRANTS_TABLE__", table)


@app.get("/admin/billing", response_class=HTMLResponse)
def admin_billing(request: Request):
    if not is_admin(request):
        raise HTTPException(status_code=404)
    return _render_admin_billing_page()


@app.post("/admin/billing/grant")
def admin_billing_grant(request: Request, email: str = Form(...), tier: str = Form(...)):
    if not is_admin(request):
        raise HTTPException(status_code=404)
    if tier not in ("pro", "pro_plus"):
        raise HTTPException(status_code=400, detail="Invalid tier.")
    if not EMAIL_RE.match(email):
        raise HTTPException(status_code=400, detail="Invalid email.")
    set_manual_grant(email, tier)
    log.info("admin granted %s to %s", tier, email)
    return RedirectResponse("/admin/billing", status_code=303)


@app.post("/admin/billing/revoke")
def admin_billing_revoke(request: Request, email: str = Form(...)):
    if not is_admin(request):
        raise HTTPException(status_code=404)
    remove_manual_grant(email)
    log.info("admin revoked manual grant for %s", email)
    return RedirectResponse("/admin/billing", status_code=303)


# ── API Endpoints ───────────────────────────────────────────────────────────

MAX_API_KEYS_PER_IDENTITY = 5


@app.post("/api/keys")
def create_api_key(request: Request):
    """Generate a new API key for the signed-in user. Requires Google Sign-In.
    Capped per identity — otherwise nothing stops minting unlimited "free"
    keys (100 calls/mo each, with no expiry) to route around both the
    per-key monthly limit and, until the check below was added, the fact
    that /api/v1/process itself had no IP rate limiting at all."""
    if config.GOOGLE_SIGNIN_CONFIGURED and not get_account(request):
        raise HTTPException(status_code=401, detail="Sign in required to create API keys.")

    # Same reasoning as every other route that computes get_identity() for an
    # anonymous visitor: without persisting the cid cookie here too, a caller
    # that reaches this endpoint before any cookie-setting route (e.g. /usage,
    # which the settings page happens to call first) gets a fresh random
    # identity on every request -- the key would be created under one cid and
    # be permanently unlistable/unrevokable under the next.
    identity = get_identity(request)
    tier = get_account_tier(request)
    api_key = generate_api_key(identity, tier, max_keys=MAX_API_KEYS_PER_IDENTITY)
    if api_key is None:
        raise HTTPException(status_code=400, detail=f"Limit of {MAX_API_KEYS_PER_IDENTITY} API keys reached. Deactivate an existing key before creating another.")
    resp = JSONResponse({"api_key": api_key, "tier": tier})
    set_session_cookie(resp, "clipai_cid", get_client_id(request), max_age=60 * 60 * 24 * 365)
    return resp


@app.get("/api/keys")
def list_api_keys(request: Request):
    """List all API keys for the signed-in user."""
    if config.GOOGLE_SIGNIN_CONFIGURED and not get_account(request):
        raise HTTPException(status_code=401, detail="Sign in required.")

    identity = get_identity(request)
    data = load_api_keys()
    user_keys = [
        {
            "key_id": info.get("key_id"),  # used to revoke this key; the real key is never shown again
            "api_key": key[:12] + "***" + key[-4:],  # masked, display only
            "tier": info["tier"],
            "created": info["created"],
            "usage_this_month": info.get("usage_this_month", 0),
            "monthly_limit": API_TIER_LIMITS.get(info.get("tier", "free"), API_TIER_LIMITS["free"]),
            "active": info.get("active", True),
        }
        for key, info in data.get("keys", {}).items()
        if info.get("identity") == identity
    ]
    resp = JSONResponse({"keys": user_keys})
    set_session_cookie(resp, "clipai_cid", get_client_id(request), max_age=60 * 60 * 24 * 365)
    return resp


@app.delete("/api/keys/{key_id}")
def revoke_api_key(key_id: str, request: Request):
    """Revoke one of the signed-in user's own API keys — the way out of the
    MAX_API_KEYS_PER_IDENTITY cap in create_api_key. Looked up by key_id
    (never the real key, which list_api_keys never re-exposes) and scoped to
    the caller's own identity so one user can't revoke another's key."""
    if config.GOOGLE_SIGNIN_CONFIGURED and not get_account(request):
        raise HTTPException(status_code=401, detail="Sign in required.")

    identity = get_identity(request)
    with _api_lock:
        data = json.loads(API_KEYS_FILE.read_text())
        for info in data.get("keys", {}).values():
            if info.get("key_id") == key_id and info.get("identity") == identity:
                info["active"] = False
                _atomic_write_text(API_KEYS_FILE, json.dumps(data, indent=2))
                return {"revoked": True}
    raise HTTPException(status_code=404, detail="API key not found.")


@app.post("/api/v1/process")
async def api_process(request: Request, file: UploadFile = File(...), clip_format: str = Form("vertical")):
    """Process a video via API. Requires valid API key in Authorization header."""
    cleanup_old_jobs()

    # The monthly per-key cap (below) doesn't stop a key from bursting all of
    # it in a minute and swamping the single-worker queue — same IP-based
    # throttle /process uses, previously only applied to the web upload path
    # even though both land on the exact same queue.
    if not check_ip_rate_limit(get_request_ip(request)):
        raise HTTPException(status_code=429, detail="Too many uploads from this network — please try again later.")

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

    # Full UUID4 hex (128 bits), not a truncated slice -- job_id is the ONLY
    # thing gating unauthenticated access to a user's uploaded clips via
    # /job/{id}, /jobs/{id}/{filename}, and /jobs/{id}/download/all (none of
    # which check ownership or rate-limit reads). An 8-hex-char id (32 bits,
    # ~4.3B combinations) is brute-forceable in hours at a few hundred
    # requests/sec against those cheap lookup endpoints; 128 bits isn't.
    job_id = uuid.uuid4().hex
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
    """Get clip results from a completed job. Requires a valid API key, same
    as api_docs.html documents ("All requests must include a valid API key")
    — previously a *missing* Authorization header was let through untouched
    while an invalid one was correctly rejected, so a caller with no key at
    all had more access than one with an expired key."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header. Use: Authorization: Bearer <api_key>")
    api_key = auth_header[7:]
    key_info = get_api_key_info(api_key)
    if not key_info or not key_info.get("active"):
        raise HTTPException(status_code=401, detail="Invalid API key.")

    job = _get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")

    if job["status"] == "processing":
        # "progress" (a fabricated percentage) was never actually computed
        # anywhere -- always 0 -- despite api_docs.html showing a fake
        # example value. elapsed_seconds is real: the web /job/{id} endpoint
        # already tracks the same started_at for its own elapsed display.
        return {"status": "processing", "elapsed_seconds": round(time.time() - job.get("started_at", time.time()))}
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
