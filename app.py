import hashlib
import hmac
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
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

import stripe
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response

import auth
import config
import email_lib
import pipeline_lib

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
SAFE_FILENAME_RE = re.compile(r"^peakcut_[A-Za-z0-9_.-]+\.(mp4|srt)$")
JOB_ID_RE = re.compile(r"^[a-f0-9]{8,32}$")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("clipai")

BASE_DIR = Path(__file__).parent.resolve()
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

CID_COOKIE_AGE = 60 * 60 * 24 * 365
CLIP_FORMATS = tuple(pipeline_lib.FORMATS)
CAPTION_STYLES = tuple(pipeline_lib.CAPTION_STYLES)
LAYOUTS = pipeline_lib.LAYOUTS
CLIP_LENGTHS = tuple(pipeline_lib.CLIP_LENGTHS)
PLAN_LIMITS = {"free": config.FREE_LIMIT, "pro": config.PRO_LIMIT, "plus": config.PRO_PLUS_LIMIT}
PLAN_LABELS = {"free": "Free", "pro": "Pro", "plus": "Pro Plus"}


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    return resp


def _month_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _next_reset_iso() -> str:
    now = datetime.now(timezone.utc)
    year, month = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
    return datetime(year, month, 1, tzinfo=timezone.utc).date().isoformat()


def _json_load(path: Path) -> dict:
    try:
        return json.loads(path.read_text() or "{}")
    except (json.JSONDecodeError, OSError):
        log.exception("could not read %s — starting fresh", path.name)
        return {}


def _json_save(path: Path, data: dict):
    # Write-then-rename so a crash mid-write can't leave a truncated file
    # that breaks every later request.
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    tmp.replace(path)


# ── Usage tracking (monthly, per plan) ───────────────────────────────────────
# Previously the counter never reset, so "5 videos/month" was really "5 ever",
# and paid plans had no cap at all despite pricing.html promising 20/50.

_usage_lock = threading.Lock()


def _current_rec(data: dict, key: str) -> dict:
    rec = data.get(key) or {}
    if rec.get("month") != _month_key():
        rec = {"month": _month_key(), "used": 0}
    return rec


def get_used(identity: str) -> int:
    with _usage_lock:
        return _current_rec(_json_load(USAGE_FILE), identity)["used"]


def reserve_use(identity: str, plan: str) -> bool:
    """Atomically check-and-increment this month's counter."""
    limit = PLAN_LIMITS.get(plan, config.FREE_LIMIT)
    with _usage_lock:
        data = _json_load(USAGE_FILE)
        rec = _current_rec(data, identity)
        if rec["used"] >= limit:
            return False
        rec["used"] += 1
        data[identity] = rec
        _json_save(USAGE_FILE, data)
        return True


def refund_use(identity: str):
    with _usage_lock:
        data = _json_load(USAGE_FILE)
        rec = _current_rec(data, identity)
        rec["used"] = max(0, rec["used"] - 1)
        data[identity] = rec
        _json_save(USAGE_FILE, data)


def _day_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def reserve_own_daily(identity: str) -> bool:
    key = f"own:{identity}"
    with _usage_lock:
        data = _json_load(USAGE_FILE)
        rec = data.get(key) or {}
        if rec.get("day") != _day_key():
            rec = {"day": _day_key(), "used": 0}
        if rec["used"] >= config.OWN_DEVICE_DAILY_LIMIT:
            return False
        rec["used"] += 1
        data[key] = rec
        _json_save(USAGE_FILE, data)
        return True


def refund_own_daily(identity: str):
    key = f"own:{identity}"
    with _usage_lock:
        data = _json_load(USAGE_FILE)
        rec = data.get(key) or {}
        if rec.get("day") == _day_key():
            rec["used"] = max(0, rec.get("used", 0) - 1)
            data[key] = rec
            _json_save(USAGE_FILE, data)


# Back-compat names used by older code/tests.
def reserve_free_use(cid) -> bool:
    return reserve_use(cid, "free")


def refund_free_use(cid):
    refund_use(cid)


def get_client_id(request: Request) -> str:
    """Cached on request.state so a cookie-less visitor gets one stable id per
    request (the id used for usage and the one written to the cookie match)."""
    if not hasattr(request.state, "clipai_cid"):
        cid = request.cookies.get("clipai_cid") or ""
        if not re.fullmatch(r"[0-9a-f-]{36}", cid):
            cid = str(uuid.uuid4())
        request.state.clipai_cid = cid
    return request.state.clipai_cid


def set_cid_cookie(resp, request: Request):
    resp.set_cookie("clipai_cid", get_client_id(request), max_age=CID_COOKIE_AGE,
                    httponly=True, samesite="lax", secure=config.SITE_URL.startswith("https"))


def get_account(request: Request) -> dict | None:
    return auth.read_account_cookie(request.cookies.get(auth.ACCOUNT_COOKIE))


def get_identity(request: Request) -> str:
    """Key for usage counting: the Google account once sign-in is configured,
    else the anonymous per-browser cookie."""
    if config.GOOGLE_SIGNIN_CONFIGURED:
        account = get_account(request)
        if account:
            return f"acct:{account['sub']}"
    return f"cid:{get_client_id(request)}"


# ── Plans (checked live against Stripe, cached briefly) ──────────────────────
# Pro status is never persisted locally (disk is wiped on free hosts), but it
# also used to hit Stripe's API on *every* page load and upload, even when
# Stripe wasn't configured at all. Now: skipped when unconfigured, cached 60s.

_plan_cache: dict = {}
_email_customer_cache: dict = {}
_cache_lock = threading.Lock()
PLAN_CACHE_SECONDS = 60


def invalidate_plan_cache(customer_id: str | None = None):
    with _cache_lock:
        if customer_id:
            _plan_cache.pop(customer_id, None)
        else:
            _plan_cache.clear()
        _email_customer_cache.clear()


def entitlements_for_customer(customer_id: str | None) -> dict:
    """{"plan": free|pro|plus, "hardware": bool} from the customer's live
    Stripe subscriptions. "hardware" is the own-hardware feature: included
    with Pro/Pro Plus, or bought on its own as the add-on price."""
    free = {"plan": "free", "hardware": False}
    if not customer_id or not config.STRIPE_CONFIGURED:
        return dict(free)
    now = time.time()
    with _cache_lock:
        hit = _plan_cache.get(customer_id)
        if hit and hit[1] > now:
            return dict(hit[0])
    ent = dict(free)
    try:
        subs = stripe.Subscription.list(customer=customer_id, status="all", limit=10)
        for sub in subs.data:
            # past_due keeps access during Stripe's retry window — cutting a
            # paying customer off over one failed card retry is a churn driver.
            if sub["status"] not in ("active", "trialing", "past_due"):
                continue
            for item in sub["items"]["data"]:
                price = item["price"]["id"]
                if config.STRIPE_PRICE_ID_HARDWARE and price == config.STRIPE_PRICE_ID_HARDWARE:
                    ent["hardware"] = True
                elif price == config.STRIPE_PRICE_ID_PLUS:
                    ent["plan"] = "plus"
                elif ent["plan"] == "free":
                    ent["plan"] = "pro"
    except Exception:
        log.exception("Stripe subscription lookup failed")
        # Don't cache failures — and fall back to a previous good value if any.
        with _cache_lock:
            hit = _plan_cache.get(customer_id)
        return dict(hit[0]) if hit else dict(free)
    if ent["plan"] != "free":
        ent["hardware"] = True
    with _cache_lock:
        _plan_cache[customer_id] = (dict(ent), now + PLAN_CACHE_SECONDS)
    return ent


def plan_for_customer(customer_id: str | None) -> str:
    return entitlements_for_customer(customer_id)["plan"]


def customer_for_email(email: str | None) -> str | None:
    """Lets a paying user get Pro back on a new device / after clearing
    cookies just by signing in with the same email they paid with."""
    if not email or not config.STRIPE_CONFIGURED:
        return None
    now = time.time()
    with _cache_lock:
        hit = _email_customer_cache.get(email)
        if hit and hit[1] > now:
            return hit[0]
    found = None
    try:
        customers = stripe.Customer.list(email=email, limit=10)
        best_rank = 0
        for c in customers.data:
            ent = entitlements_for_customer(c["id"])
            rank = {"free": 0, "pro": 2, "plus": 3}[ent["plan"]] or (1 if ent["hardware"] else 0)
            if found is None or rank > best_rank:
                found, best_rank = c["id"], rank
    except Exception:
        log.exception("Stripe customer lookup by email failed")
        return None
    with _cache_lock:
        _email_customer_cache[email] = (found, now + 300)
    return found


def get_customer_id(request: Request) -> str | None:
    cid = auth.read_customer_cookie(request.cookies.get(auth.CUSTOMER_COOKIE))
    if cid:
        return cid
    account = get_account(request)
    return customer_for_email(account["email"]) if account else None


def get_entitlements(request: Request) -> dict:
    if not hasattr(request.state, "entitlements"):
        request.state.entitlements = entitlements_for_customer(get_customer_id(request))
    return request.state.entitlements


def get_plan(request: Request) -> str:
    return get_entitlements(request)["plan"]


def has_own_hardware(request: Request) -> bool:
    return get_entitlements(request)["hardware"]


HARDWARE_LOCKED_MSG = ("Processing on your own computer is included with Pro and Pro Plus, "
                       "or you can unlock it on its own (see Pricing).")


def check_pro_status(request: Request) -> bool:
    return get_plan(request) != "free"


def set_customer_cookie(resp, customer_id: str):
    resp.set_cookie(auth.CUSTOMER_COOKIE, auth.sign_customer_cookie(customer_id), max_age=CID_COOKIE_AGE,
                    httponly=True, samesite="lax", secure=config.SITE_URL.startswith("https"))


# ── API keys ─────────────────────────────────────────────────────────────────
# Fixes: keys were stored in plaintext; anyone could mint unlimited keys (each
# with its own 100/month allowance — an unlimited free tier); the tier was
# frozen at creation so a cancelled Pro kept Pro limits forever; and the
# "monthly" counter never reset. Now keys are stored hashed, capped per user,
# usage is per user per month across all keys, and the tier is checked live.

_api_lock = threading.Lock()


def _hash_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode()).hexdigest()


def _load_api_data() -> dict:
    data = _json_load(API_KEYS_FILE)
    data.setdefault("keys", {})
    data.setdefault("usage", {})
    # One-time migration of legacy plaintext keys to hashed storage.
    legacy = [k for k in data["keys"] if k.startswith("pk_")]
    for k in legacy:
        info = data["keys"].pop(k)
        info["prefix"] = k[:10]
        data["keys"][_hash_key(k)] = info
    return data


def load_api_keys():
    with _api_lock:
        return _load_api_data()


def generate_api_key(identity: str, email: str | None = None, customer_id: str | None = None) -> str:
    api_key = f"pk_{uuid.uuid4().hex}{uuid.uuid4().hex[:8]}"
    with _api_lock:
        data = _load_api_data()
        data["keys"][_hash_key(api_key)] = {
            "identity": identity, "email": email, "customer_id": customer_id,
            "prefix": api_key[:10], "created": time.time(), "active": True,
        }
        _json_save(API_KEYS_FILE, data)
    return api_key


def get_api_key_info(api_key: str) -> dict | None:
    if not api_key or not api_key.startswith("pk_"):
        return None
    with _api_lock:
        info = _load_api_data()["keys"].get(_hash_key(api_key))
    return dict(info) if info else None


def api_plan(key_info: dict) -> str:
    customer = key_info.get("customer_id") or customer_for_email(key_info.get("email"))
    return plan_for_customer(customer)


def api_usage(identity: str) -> int:
    with _api_lock:
        return _current_rec(_load_api_data()["usage"], identity)["used"]


def reserve_api_use(identity: str, plan: str) -> bool:
    limit = config.API_LIMITS.get(plan, config.API_LIMITS["free"])
    with _api_lock:
        data = _load_api_data()
        rec = _current_rec(data["usage"], identity)
        if rec["used"] >= limit:
            return False
        rec["used"] += 1
        data["usage"][identity] = rec
        _json_save(API_KEYS_FILE, data)
        return True


def refund_api_use(identity: str):
    with _api_lock:
        data = _load_api_data()
        rec = _current_rec(data["usage"], identity)
        rec["used"] = max(0, rec["used"] - 1)
        data["usage"][identity] = rec
        _json_save(API_KEYS_FILE, data)


def _bearer_key(request: Request) -> str | None:
    auth_header = request.headers.get("Authorization", "")
    return auth_header[7:].strip() if auth_header.startswith("Bearer ") else None


# ── Background job queue ─────────────────────────────────────────────────────
# Jobs are processed either by the built-in worker thread (one at a time —
# two concurrent Whisper+ffmpeg runs OOM a 512MB instance) or by external
# workers running peakcut_worker.py on someone's own hardware:
#   * a user's paired computer processes that user's "own hardware" jobs;
#   * the site owner's WORKER_TOKEN machines help drain the cloud queue.
# Every claim goes through _try_claim() under one lock, so a job can never be
# processed twice. External claims hold a lease that progress updates extend;
# if a worker vanishes, the lease expires and the job goes back in the queue.
# Paid jobs jump ahead of free ones (priority 0 vs 1).

MAX_QUEUE = 10
MAX_OWN_QUEUED_PER_USER = 5
LEASE_SECONDS = 180
JOB_MAX_AGE_SECONDS = config.JOB_RETENTION_HOURS * 60 * 60

_jobs: dict = {}
_jobs_lock = threading.Lock()
_job_queue: "queue.PriorityQueue[tuple[int, int, str]]" = queue.PriorityQueue()
_seq = itertools.count()


def _set_job(job_id, **fields):
    with _jobs_lock:
        _jobs.setdefault(job_id, {}).update(fields)


def _get_job(job_id):
    with _jobs_lock:
        job = _jobs.get(job_id)
        return dict(job) if job else None


def _queued_cloud_count() -> int:
    with _jobs_lock:
        return sum(1 for j in _jobs.values() if j.get("status") == "queued" and j.get("device", "cloud") == "cloud")


def _cloud_queue_full() -> bool:
    return _queued_cloud_count() >= MAX_QUEUE


def _enqueue(job_id: str, priority: int):
    """Marks a job ready. Cloud jobs also go on the built-in worker's queue;
    own-hardware jobs wait for the user's paired computer to claim them."""
    job = _get_job(job_id) or {}
    if job.get("device", "cloud") == "cloud" and _cloud_queue_full():
        raise queue.Full
    seq = next(_seq)
    _set_job(job_id, priority=priority, seq=seq, status="queued", claimed_by=None, lease_until=None)
    if job.get("device", "cloud") == "cloud" and config.LOCAL_WORKER_ENABLED:
        _job_queue.put_nowait((priority, seq, job_id))


def _try_claim(job_id: str, claimer: str) -> bool:
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job or job.get("status") != "queued":
            return False
        job.update(status="processing", claimed_by=claimer, started_at=time.time(),
                   stage="transcribing", progress=2,
                   lease_until=None if claimer == "internal" else time.time() + LEASE_SECONDS)
        return True


def _expire_leases():
    """Requeue jobs whose external worker stopped reporting (crashed, went
    offline, lost network). Their stage/progress reset so the UI is honest."""
    now = time.time()
    with _jobs_lock:
        expired = [jid for jid, j in _jobs.items()
                   if j.get("status") == "processing" and j.get("lease_until") and j["lease_until"] < now]
    for jid in expired:
        job = _get_job(jid)
        log.warning("lease expired for job %s (worker %s) — requeueing", jid, job.get("claimed_by"))
        attempts = job.get("attempts", 0) + 1
        if attempts >= 3:
            _fail_job(jid, "The computer processing this video stopped responding three times.")
            continue
        _set_job(jid, attempts=attempts)
        try:
            _enqueue(jid, job.get("priority", 1))
        except queue.Full:
            _set_job(jid, status="queued", claimed_by=None, lease_until=None)


def _queue_position(job_id):
    with _jobs_lock:
        me = _jobs.get(job_id)
        if not me or me.get("status") != "queued":
            return None
        key = (me.get("priority", 1), me.get("seq", 0))
        device = me.get("device", "cloud")
        ahead = sum(1 for j in _jobs.values()
                    if j.get("status") == "queued" and j.get("device", "cloud") == device
                    and (device == "cloud" or j.get("identity") == me.get("identity"))
                    and (j.get("priority", 1), j.get("seq", 0)) < key)
    return ahead + 1


def _refund_job(job):
    identity = job.get("identity")
    if not identity:
        return
    if job.get("device") == "own":
        refund_own_daily(identity)
        return
    if not job.get("counted", True):
        return
    if job.get("api_key"):
        refund_api_use(identity)
    else:
        refund_use(identity)


def _friendly_error(e: Exception) -> str:
    error_str = str(e).lower()
    if "no speech" in error_str or "no usable" in error_str:
        return "No clear speech detected. Try a video with louder, clearer audio or more talking."
    if "too short" in error_str or "minimum" in error_str:
        return "Video is too short. Try a video at least 30 seconds long."
    if "codec" in error_str or "unsupported" in error_str or "invalid data" in error_str:
        return "Video format not supported. Try MP4, MOV, WEBM, or MKV."
    return "Processing failed — the video may be too short, silent, or an unsupported codec. Try a longer video with clearer speech."


def _finish_job(job_id: str, clips: list, duration, language):
    """Shared by the built-in worker and external workers."""
    job = _get_job(job_id) or {}
    for clip in clips:
        clip["url"] = f"/jobs/{job_id}/{clip['file']}"
        clip["srt_url"] = f"/jobs/{job_id}/{clip['srt']}" if clip.get("srt") else None
        clip.pop("error", None)
        clip.pop("ok", None)
    _set_job(job_id, status="done", clips=clips, duration=duration, language=language,
             finished_at=time.time(), stage="done", progress=100, lease_until=None)
    try:
        if job.get("input_path"):
            Path(job["input_path"]).unlink(missing_ok=True)
    except Exception:
        pass
    if job.get("notify_email"):
        email_lib.send_done_email(job["notify_email"], [c["url"] for c in clips], duration or 0, job.get("is_pro", False))


def _fail_job(job_id: str, error_msg: str, user_error: bool = False):
    job = _get_job(job_id) or {}
    shutil.rmtree(JOBS_DIR / job_id, ignore_errors=True)
    _refund_job(job)
    if job.get("counted", True):
        error_msg += " This video didn't count toward your monthly limit."
    _set_job(job_id, status="failed", error=error_msg, finished_at=time.time(), lease_until=None)
    if job.get("notify_email"):
        email_lib.send_failed_email(job["notify_email"], error_msg)


def _worker():
    while True:
        _, _, job_id = _job_queue.get()
        if not _try_claim(job_id, "internal"):
            continue  # already taken by an external worker, or cancelled
        job = _get_job(job_id)
        job_dir = JOBS_DIR / job_id
        try:
            if "input_path" not in job:
                raise RuntimeError("internal error: job has no input file")
            result = pipeline_lib.process_video(
                job["input_path"], str(job_dir), n_clips=job.get("n_clips", 3),
                watermark=not job.get("is_pro", False),
                dub_lang=job.get("dub_lang"), clip_format=job.get("clip_format", "vertical"),
                caption_style=job.get("caption_style", "bold"), layout=job.get("layout", "crop"),
                clip_length=job.get("clip_length", "medium"),
                watermark_text=job.get("watermark_text"), watermark_color=job.get("watermark_color"),
                progress_cb=lambda stage, pct: _set_job(job_id, stage=stage, progress=pct),
            )
            # Clips whose render failed used to be returned anyway, as links
            # to files that didn't exist.
            clips = [c for c in result["clips"] if c.get("ok")]
            if not clips:
                raise RuntimeError("no usable clips found")
            _finish_job(job_id, clips, result["duration"], result.get("language"))
        except Exception as e:
            if isinstance(e, ValueError):
                # User-input problem (e.g. dubbing a non-English video) — show the real reason.
                log.info("job %s rejected: %s", job_id, e)
                _fail_job(job_id, str(e), user_error=True)
            else:
                log.exception("processing failed for job %s", job_id)
                _fail_job(job_id, _friendly_error(e))


if config.LOCAL_WORKER_ENABLED:
    threading.Thread(target=_worker, daemon=True).start()
else:
    log.info("LOCAL_WORKER_ENABLED=0 — cloud jobs wait for external workers (peakcut_worker.py with WORKER_TOKEN).")


def cleanup_old_jobs():
    now = time.time()
    try:
        for job_dir in JOBS_DIR.iterdir():
            if job_dir.is_dir() and now - job_dir.stat().st_mtime > JOB_MAX_AGE_SECONDS:
                with _jobs_lock:
                    status = _jobs.get(job_dir.name, {}).get("status")
                if status in ("queued", "processing"):
                    continue
                shutil.rmtree(job_dir, ignore_errors=True)
                with _jobs_lock:
                    _jobs.pop(job_dir.name, None)
    except Exception:
        log.exception("job cleanup failed")

    with _jobs_lock:
        stale = [jid for jid, j in _jobs.items()
                 if j.get("status") in ("failed", "done") and now - j.get("finished_at", now) > JOB_MAX_AGE_SECONDS]
        # Own-hardware jobs whose computer never showed up: give up after a day.
        stale += [jid for jid, j in _jobs.items()
                  if j.get("status") == "queued" and j.get("device") == "own"
                  and now - j.get("created_at", now) > JOB_MAX_AGE_SECONDS]
        for jid in stale:
            _jobs.pop(jid, None)
    for jid in stale:
        shutil.rmtree(JOBS_DIR / jid, ignore_errors=True)

    with _ip_log_lock:
        empty_ips = [ip for ip, times in _ip_request_log.items() if not any(now - t < 3600 for t in times)]
        for ip in empty_ips:
            _ip_request_log.pop(ip, None)


def _janitor():
    ticks = 0
    while True:
        time.sleep(30)
        ticks += 1
        try:
            _expire_leases()
            if ticks % 20 == 0:
                cleanup_old_jobs()
        except Exception:
            log.exception("janitor tick failed")


threading.Thread(target=_janitor, daemon=True).start()


# ── Own hardware: paired computers ───────────────────────────────────────────
# A user pairs a computer in Settings and gets a device token (pd_...).
# peakcut_worker.py on that computer polls /worker/claim with it and
# processes that user's "my computer" jobs locally (GPU if present), then
# uploads the finished clips. Those jobs don't use the monthly allowance —
# the user's hardware did the work — but are capped per day so storage
# stays bounded. Tokens are stored hashed, like API keys.

DEVICE_ONLINE_SECONDS = 90


def _devices_for(identity: str) -> list:
    with _api_lock:
        data = _load_api_data()
    now = time.time()
    return [
        {"id": h[:12], "name": d.get("name", "My computer"), "created": d.get("created"),
         "last_seen": d.get("last_seen"), "online": bool(d.get("last_seen") and now - d["last_seen"] < DEVICE_ONLINE_SECONDS),
         "info": d.get("info", {})}
        for h, d in data.get("devices", {}).items() if d.get("identity") == identity and d.get("active", True)
    ]


def _worker_principal(request: Request) -> dict:
    """Who is calling /worker/*: a user's paired computer, or the site
    owner's WORKER_TOKEN machine. Raises 401 otherwise."""
    token = _bearer_key(request) or ""
    if config.WORKER_TOKEN and hmac.compare_digest(token, config.WORKER_TOKEN):
        return {"kind": "owner", "id": "owner", "identity": None}
    if token.startswith("pd_"):
        h = _hash_key(token)
        with _api_lock:
            data = _load_api_data()
            dev = data.get("devices", {}).get(h)
            if dev and dev.get("active", True):
                now = time.time()
                if now - (dev.get("last_seen") or 0) > 20:  # throttle disk writes
                    dev["last_seen"] = now
                    _json_save(API_KEYS_FILE, data)
                return {"kind": "device", "id": "dev:" + h[:12], "identity": dev["identity"], "name": dev.get("name")}
    raise HTTPException(status_code=401, detail="Invalid worker token. Pair this computer again in Settings.")


def _require_claim(request: Request, job_id: str) -> tuple[dict, dict]:
    who = _worker_principal(request)
    if not JOB_ID_RE.match(job_id):
        raise HTTPException(status_code=404, detail="Job not found.")
    job = _get_job(job_id)
    if not job or job.get("status") != "processing" or job.get("claimed_by") != who["id"]:
        raise HTTPException(status_code=409, detail="This job isn't claimed by this worker (it may have timed out).")
    return who, job


# ── Pages ────────────────────────────────────────────────────────────────────

def _page(name: str) -> str:
    return (BASE_DIR / name).read_text().replace("__CONTACT_EMAIL__", config.CONTACT_EMAIL)


@app.get("/", response_class=HTMLResponse)
def index():
    return _page("index.html")


@app.get("/pricing", response_class=HTMLResponse)
def pricing():
    return _page("pricing.html")


@app.get("/api/docs", response_class=HTMLResponse)
def api_docs():
    return _page("api_docs.html").replace("__SITE_URL__", config.SITE_URL)


@app.get("/settings", response_class=HTMLResponse)
def settings(request: Request):
    if config.GOOGLE_SIGNIN_CONFIGURED and not get_account(request):
        return RedirectResponse("/auth/google/login", status_code=303)
    return _page("settings.html")


@app.get("/privacy", response_class=HTMLResponse)
def privacy():
    return _page("privacy.html")


@app.get("/terms", response_class=HTMLResponse)
def terms():
    return _page("terms.html")


@app.get("/healthz", response_class=PlainTextResponse)
def healthz():
    return "ok"


@app.get("/robots.txt", response_class=PlainTextResponse)
def robots():
    return f"User-agent: *\nAllow: /\nDisallow: /jobs/\nDisallow: /job/\nSitemap: {config.SITE_URL}/sitemap.xml\n"


@app.get("/sitemap.xml")
def sitemap():
    urls = "".join(f"<url><loc>{config.SITE_URL}{p}</loc></url>"
                   for p in ("/", "/pricing", "/api/docs", "/privacy", "/terms"))
    xml = f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>'
    return Response(xml, media_type="application/xml")


@app.get("/usage")
def usage(request: Request):
    identity = get_identity(request)
    account = get_account(request)
    plan = get_plan(request)
    used = get_used(identity)
    limit = PLAN_LIMITS[plan]
    resp = JSONResponse({
        "used": used, "limit": limit, "remaining": max(0, limit - used),
        "plan": plan, "plan_label": PLAN_LABELS[plan], "pro": plan != "free",
        "max_clips": config.MAX_CLIPS[plan], "resets_on": _next_reset_iso(),
        "retention_hours": config.JOB_RETENTION_HOURS,
        "google_configured": config.GOOGLE_SIGNIN_CONFIGURED,
        "signed_in": account is not None,
        "email": account["email"] if account else None,
        "email_configured": config.EMAIL_CONFIGURED,
        "stripe_configured": config.STRIPE_CONFIGURED,
        "contact_email": config.CONTACT_EMAIL,
        "devices": _devices_for(identity),
        "own_hardware": has_own_hardware(request),
        "hardware_addon_available": config.STRIPE_CONFIGURED and bool(config.STRIPE_PRICE_ID_HARDWARE),
        "buffer_url": config.BUFFER_URL,
        "own_daily_limit": config.OWN_DEVICE_DAILY_LIMIT,
    })
    set_cid_cookie(resp, request)
    # Migrate legacy unsigned customer cookies / remember an email-found customer.
    raw = request.cookies.get(auth.CUSTOMER_COOKIE)
    customer = get_customer_id(request)
    if customer and (not raw or raw.startswith("cus_")):
        set_customer_cookie(resp, customer)
    return resp


def get_request_ip(request: Request) -> str:
    """Best-effort real client IP: Cloudflare's CF-Connecting-IP first, then
    X-Forwarded-For. Both are only trustworthy if the origin is unreachable
    except through the proxy (see README)."""
    cf_ip = request.headers.get("cf-connecting-ip")
    if cf_ip:
        return cf_ip.strip()
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


MAX_PROCESS_PER_IP_PER_HOUR = 8
_ip_request_log: dict = {}
_ip_log_lock = threading.Lock()


def check_ip_rate_limit(ip: str, limit: int = MAX_PROCESS_PER_IP_PER_HOUR) -> bool:
    now = time.time()
    with _ip_log_lock:
        window = [t for t in _ip_request_log.get(ip, []) if now - t < 3600]
        if len(window) >= limit:
            _ip_request_log[ip] = window
            return False
        window.append(now)
        _ip_request_log[ip] = window
        return True


def _validate_options(clip_format, caption_style, layout, clip_length):
    if clip_format not in CLIP_FORMATS:
        raise HTTPException(status_code=400, detail="clip_format must be 'vertical', 'square', or 'horizontal'.")
    if caption_style not in CAPTION_STYLES:
        raise HTTPException(status_code=400, detail=f"caption_style must be one of: {', '.join(CAPTION_STYLES)}.")
    if layout not in LAYOUTS:
        raise HTTPException(status_code=400, detail="layout must be 'crop', 'blur', or 'fit'.")
    if clip_length not in CLIP_LENGTHS:
        raise HTTPException(status_code=400, detail="clip_length must be 'short', 'medium', or 'long'.")


def _validate_ext(filename: str | None) -> str:
    ext = Path(filename or "").suffix.lower()
    if ext not in config.ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Unsupported file type '{ext}'. Use MP4, MOV, M4V, WEBM, or MKV.")
    return ext


async def _save_upload(file: UploadFile, dest: Path):
    max_bytes = config.MAX_UPLOAD_MB * 1024 * 1024
    size = 0
    with open(dest, "wb") as f:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > max_bytes:
                raise HTTPException(status_code=413, detail=f"File too large — the limit is {config.MAX_UPLOAD_MB}MB.")
            f.write(chunk)
    if size == 0:
        raise HTTPException(status_code=400, detail="The uploaded file is empty.")


def _new_job_id() -> str:
    # 64 bits: clip URLs are shareable by id, so ids must not be guessable
    # (the old 8-hex-char ids were only 32 bits).
    return uuid.uuid4().hex[:16]


@app.post("/process")
async def process(request: Request, file: UploadFile = File(...), dub_lang: str | None = Form(None),
                  notify_email: str | None = Form(None), clip_format: str = Form("vertical"),
                  caption_style: str = Form("bold"), layout: str = Form("crop"),
                  clip_length: str = Form("medium"), n_clips: int = Form(3),
                  watermark_text: str | None = Form(None), watermark_color: str | None = Form(None),
                  device: str = Form("cloud")):
    _validate_options(clip_format, caption_style, layout, clip_length)
    if device not in ("cloud", "own"):
        raise HTTPException(status_code=400, detail="device must be 'cloud' or 'own'.")
    if device == "own" and not has_own_hardware(request):
        raise HTTPException(status_code=402, detail=HARDWARE_LOCKED_MSG)

    if not check_ip_rate_limit(get_request_ip(request)):
        raise HTTPException(status_code=429, detail="Too many uploads from this network — please try again later.")

    if config.GOOGLE_SIGNIN_CONFIGURED and not get_account(request):
        raise HTTPException(status_code=401, detail="Sign in with Google to process a video.")

    identity = get_identity(request)
    plan = get_plan(request)
    is_pro = plan != "free"
    ext = _validate_ext(file.filename)

    dub_lang = dub_lang or None
    if dub_lang:
        import dub_lib
        if dub_lang not in dub_lib.DUB_LANGUAGES:
            raise HTTPException(status_code=400, detail="Unsupported dub language.")
        if not is_pro:
            raise HTTPException(status_code=402, detail="Dubbing is a Pro feature. Upgrade to Pro to dub clips into other languages.")

    notify_email = (notify_email or "").strip() or None
    if notify_email and not EMAIL_RE.match(notify_email):
        raise HTTPException(status_code=400, detail="That doesn't look like a valid email address.")

    # On your own computer you can ask for as many clips as any plan allows.
    max_clips = max(config.MAX_CLIPS.values()) if device == "own" else config.MAX_CLIPS[plan]
    n_clips = max(1, min(int(n_clips or 3), max_clips))

    # Custom watermark is a Pro Plus feature; anything else is ignored rather
    # than rejected so a stale localStorage preset can't block an upload.
    wm_text = wm_color = None
    if plan == "plus" and watermark_text and watermark_text.strip():
        wm_text = re.sub(r"[^\w .,!?@#&'+:/-]", "", watermark_text.strip())[:30] or None
        if watermark_color and re.fullmatch(r"#[0-9a-fA-F]{6}", watermark_color):
            wm_color = watermark_color

    if device == "own":
        with _jobs_lock:
            waiting = sum(1 for j in _jobs.values() if j.get("identity") == identity
                          and j.get("device") == "own" and j.get("status") in ("queued", "processing"))
        if waiting >= MAX_OWN_QUEUED_PER_USER:
            raise HTTPException(status_code=429, detail=f"You already have {waiting} videos waiting for your computer — let it catch up first.")
        if not reserve_own_daily(identity):
            raise HTTPException(status_code=429, detail=f"You've processed {config.OWN_DEVICE_DAILY_LIMIT} videos on your own computer today — that's the daily cap. Try again tomorrow.")
        reserve, refund = (lambda: True), (lambda: refund_own_daily(identity))
    else:
        if _cloud_queue_full():
            raise HTTPException(status_code=503, detail="We're at capacity right now — try again in a few minutes, or process on your own computer.")
        reserve, refund = (lambda: reserve_use(identity, plan)), (lambda: refund_use(identity))

    if not reserve():
        limit = PLAN_LIMITS[plan]
        msg = (f"You've used all {limit} videos on the {PLAN_LABELS[plan]} plan this month "
               f"(resets {_next_reset_iso()}).")
        if plan != "plus":
            msg += " Upgrade for more videos per month"
        if has_own_hardware(request):
            msg += " Videos processed on your own computer don't count toward this."
        raise HTTPException(status_code=402, detail=msg)

    job_id = _new_job_id()
    job_dir = JOBS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    input_path = job_dir / f"input{ext}"

    try:
        await _save_upload(file, input_path)
    except HTTPException:
        shutil.rmtree(job_dir, ignore_errors=True)
        refund()
        raise
    except Exception:
        shutil.rmtree(job_dir, ignore_errors=True)
        refund()
        log.exception("upload failed")
        raise HTTPException(status_code=500, detail="Upload failed — please try again.")

    _set_job(job_id, status="queued", identity=identity, owner_cid=get_client_id(request), is_pro=is_pro,
             plan=plan, input_path=str(input_path), created_at=time.time(), dub_lang=dub_lang,
             notify_email=notify_email, clip_format=clip_format, caption_style=caption_style,
             layout=layout, clip_length=clip_length, n_clips=n_clips,
             watermark_text=wm_text, watermark_color=wm_color, filename=(file.filename or "")[:120],
             device=device, counted=device == "cloud")
    try:
        _enqueue(job_id, 0 if is_pro else 1)
    except queue.Full:
        shutil.rmtree(job_dir, ignore_errors=True)
        with _jobs_lock:
            _jobs.pop(job_id, None)
        refund()
        raise HTTPException(status_code=503, detail="We're at capacity right now — try again in a few minutes.")

    resp = JSONResponse({"job_id": job_id, "status": "queued"})
    set_cid_cookie(resp, request)
    return resp


def _owns_job(request: Request, job: dict) -> bool:
    key = _bearer_key(request)
    if key:
        info = get_api_key_info(key)
        return bool(info and info.get("active") and info["identity"] == job.get("identity"))
    if job.get("identity") == get_identity(request):
        return True
    cid = request.cookies.get("clipai_cid")
    return bool(cid and cid == job.get("owner_cid"))


def _require_job(request: Request, job_id: str) -> dict:
    if not JOB_ID_RE.match(job_id):
        raise HTTPException(status_code=404, detail="Job not found — it may have expired.")
    job = _get_job(job_id)
    if not job or not _owns_job(request, job):
        raise HTTPException(status_code=404, detail="Job not found — it may have expired.")
    return job


@app.get("/job/{job_id}")
def job_status(job_id: str, request: Request):
    job = _require_job(request, job_id)
    out = {"job_id": job_id, "status": job["status"], "device": job.get("device", "cloud")}
    if job["status"] == "queued":
        out["queue_position"] = _queue_position(job_id)
        if job.get("device") == "own":
            out["device_online"] = any(d["online"] for d in _devices_for(job["identity"]))
    elif job["status"] == "processing":
        out["elapsed"] = round(time.time() - job.get("started_at", time.time()))
        out["stage"] = job.get("stage")
        out["progress"] = job.get("progress", 0)
        if job.get("worker_name"):
            out["worker_name"] = job["worker_name"]
    elif job["status"] == "done":
        out["clips"] = job["clips"]
        out["duration"] = job["duration"]
        out["language"] = job.get("language") or "unknown"
        out["expires_at"] = round(job.get("finished_at", time.time()) + JOB_MAX_AGE_SECONDS)
    elif job["status"] == "failed":
        out["error"] = job.get("error", "Processing failed.")
    return out


@app.get("/jobs/{job_id}/download/all")
def download_all_clips(job_id: str, request: Request):
    job = _require_job(request, job_id)
    if job.get("status") != "done":
        raise HTTPException(status_code=400, detail="Job isn't finished yet.")
    job_dir = JOBS_DIR / job_id
    if not job_dir.exists():
        raise HTTPException(status_code=404, detail="These clips have expired.")

    # FileResponse expects a filesystem path — it was being handed a BytesIO,
    # so "Download All as ZIP" always failed with a 500.
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:  # mp4 is already compressed
        for clip in job.get("clips", []):
            for name in (clip.get("file"), clip.get("srt")):
                if name and (job_dir / name).exists():
                    zf.write(job_dir / name, arcname=name)
        zf.writestr("clips.json", json.dumps(
            [{k: c.get(k) for k in ("rank", "start", "end", "virality_score", "title", "hashtags", "text", "file", "srt")}
             for c in job.get("clips", [])], indent=2))
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="peakcut_{job_id[:8]}.zip"'})


@app.get("/jobs/{job_id}/{filename}")
def get_clip(job_id: str, filename: str, download: int = 0):
    # Only finished clip/caption files are served — never the raw upload,
    # which used to be readable at /jobs/<id>/input.mp4 while queued.
    if not JOB_ID_RE.match(job_id) or not SAFE_FILENAME_RE.match(filename):
        raise HTTPException(status_code=404, detail="Not found.")
    path = JOBS_DIR / job_id / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Not found — clips expire after 24 hours.")
    media = "video/mp4" if filename.endswith(".mp4") else "application/x-subrip"
    headers = {"Cache-Control": "private, max-age=3600"}
    if download or filename.endswith(".srt"):
        headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return FileResponse(str(path), media_type=media, headers=headers)


# ── Own hardware: pairing + worker API ───────────────────────────────────────

def _device_owner_identity(request: Request) -> str:
    if config.GOOGLE_SIGNIN_CONFIGURED and not get_account(request):
        raise HTTPException(status_code=401, detail="Sign in to pair a computer.")
    return get_identity(request)


@app.get("/devices")
def list_devices(request: Request):
    identity = _device_owner_identity(request)
    return {"devices": _devices_for(identity), "daily_limit": config.OWN_DEVICE_DAILY_LIMIT,
            "own_hardware": has_own_hardware(request)}


@app.post("/devices")
async def create_device(request: Request):
    identity = _device_owner_identity(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not has_own_hardware(request):
        raise HTTPException(status_code=402, detail=HARDWARE_LOCKED_MSG)
    name = re.sub(r"[^\w .'-]", "", str(body.get("name") or "My computer"))[:40] or "My computer"
    if len(_devices_for(identity)) >= config.MAX_DEVICES_PER_USER:
        raise HTTPException(status_code=400, detail=f"You can pair up to {config.MAX_DEVICES_PER_USER} computers. Remove one first.")
    token = f"pd_{uuid.uuid4().hex}{uuid.uuid4().hex[:8]}"
    with _api_lock:
        data = _load_api_data()
        data.setdefault("devices", {})[_hash_key(token)] = {
            "identity": identity, "name": name, "created": time.time(), "last_seen": None, "active": True}
        _json_save(API_KEYS_FILE, data)
    resp = JSONResponse({"token": token, "name": name, "server": config.SITE_URL})
    set_cid_cookie(resp, request)  # anonymous users: pin the identity the device belongs to
    return resp


@app.delete("/devices/{device_id}")
def delete_device(device_id: str, request: Request):
    identity = _device_owner_identity(request)
    with _api_lock:
        data = _load_api_data()
        for h, d in data.get("devices", {}).items():
            if len(device_id) >= 12 and h.startswith(device_id) and d.get("identity") == identity:
                d["active"] = False
                _json_save(API_KEYS_FILE, data)
                return {"removed": True}
    raise HTTPException(status_code=404, detail="Computer not found.")


def _job_spec(job_id: str, job: dict) -> dict:
    return {
        "job_id": job_id,
        "input_url": f"/worker/jobs/{job_id}/input",
        "input_ext": Path(job.get("input_path", "x.mp4")).suffix,
        "options": {k: job.get(k) for k in ("n_clips", "dub_lang", "clip_format", "caption_style", "layout",
                                            "clip_length", "watermark_text", "watermark_color")},
        "watermark": not job.get("is_pro", False),
        "lease_seconds": LEASE_SECONDS,
    }


@app.post("/worker/claim")
async def worker_claim(request: Request):
    """Hands the next job to an external worker, or 204 if there's none."""
    who = _worker_principal(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    info = {k: str(body.get(k, ""))[:80] for k in ("hostname", "gpu", "whisper_model", "encoder", "version")}
    if who["kind"] == "device":
        with _api_lock:
            data = _load_api_data()
            for h, d in data.get("devices", {}).items():
                if who["id"] == "dev:" + h[:12]:
                    d["info"] = info
            _json_save(API_KEYS_FILE, data)
    _expire_leases()
    with _jobs_lock:
        if who["kind"] == "device":
            candidates = [(j.get("seq", 0), jid) for jid, j in _jobs.items()
                          if j.get("status") == "queued" and j.get("device") == "own" and j.get("identity") == who["identity"]]
        else:
            candidates = [((j.get("priority", 1), j.get("seq", 0)), jid) for jid, j in _jobs.items()
                          if j.get("status") == "queued" and j.get("device", "cloud") == "cloud"]
    for _, jid in sorted(candidates):
        if _try_claim(jid, who["id"]):
            _set_job(jid, worker_name=who.get("name") or info.get("hostname") or "worker")
            log.info("job %s claimed by %s", jid, who["id"])
            return _job_spec(jid, _get_job(jid))
    return Response(status_code=204)


@app.get("/worker/jobs/{job_id}/input")
def worker_input(job_id: str, request: Request):
    _, job = _require_claim(request, job_id)
    path = Path(job.get("input_path", ""))
    if not path.is_file():
        raise HTTPException(status_code=410, detail="The upload is gone.")
    return FileResponse(str(path), media_type="application/octet-stream")


@app.post("/worker/jobs/{job_id}/progress")
async def worker_progress(job_id: str, request: Request):
    _require_claim(request, job_id)
    try:
        body = await request.json()
    except Exception:
        body = {}
    stage = body.get("stage") if body.get("stage") in ("transcribing", "finding_moments", "rendering", "uploading") else None
    progress = max(0, min(99, int(body.get("progress") or 0)))
    fields = {"progress": progress, "lease_until": time.time() + LEASE_SECONDS}
    if stage:
        fields["stage"] = "rendering" if stage == "uploading" else stage
    _set_job(job_id, **fields)
    return {"ok": True, "lease_seconds": LEASE_SECONDS}


@app.post("/worker/jobs/{job_id}/fail")
async def worker_fail(job_id: str, request: Request):
    _require_claim(request, job_id)
    try:
        body = await request.json()
    except Exception:
        body = {}
    reason = re.sub(r"[\x00-\x1f<>]", " ", str(body.get("error") or ""))[:300]
    _fail_job(job_id, _friendly_error(RuntimeError(reason)) if not body.get("user_error") else reason)
    return {"ok": True}


MAX_RESULT_FILES = 2 * max(config.MAX_CLIPS.values())


@app.post("/worker/jobs/{job_id}/result")
async def worker_result(job_id: str, request: Request):
    """Receives finished clips from an external worker. Everything in the
    manifest is re-validated here: the worker runs on a machine we don't
    control, so it's treated like any other upload."""
    _, job = _require_claim(request, job_id)
    form = await request.form(max_files=MAX_RESULT_FILES + 1)
    try:
        manifest = json.loads(form.get("manifest") or "")
        duration = float(form.get("duration") or 0)
        language = re.sub(r"[^a-z-]", "", str(form.get("language") or ""))[:8] or None
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid manifest.")
    if not isinstance(manifest, list) or not manifest or len(manifest) > max(config.MAX_CLIPS.values()):
        raise HTTPException(status_code=400, detail="Manifest must list 1-8 clips.")

    uploads = {f.filename: f for f in form.getlist("files") if hasattr(f, "filename")}
    job_dir = JOBS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    max_bytes = config.MAX_UPLOAD_MB * 1024 * 1024
    clips = []
    try:
        for i, c in enumerate(manifest, 1):
            name, srt = str(c.get("file", "")), c.get("srt")
            if not SAFE_FILENAME_RE.match(name) or not name.endswith(".mp4") or name not in uploads:
                raise HTTPException(status_code=400, detail=f"Clip {i}: missing or invalid file.")
            if srt is not None and (not SAFE_FILENAME_RE.match(str(srt)) or not str(srt).endswith(".srt") or srt not in uploads):
                srt = None
            for fname in filter(None, (name, srt)):
                dest, size = job_dir / fname, 0
                with open(dest, "wb") as out:
                    while chunk := await uploads[fname].read(1024 * 1024):
                        size += len(chunk)
                        if size > max_bytes:
                            raise HTTPException(status_code=413, detail="Clip file too large.")
                        out.write(chunk)
            clips.append({
                "rank": i, "start": round(float(c.get("start", 0)), 2), "end": round(float(c.get("end", 0)), 2),
                "virality_score": max(0, min(100, int(c.get("virality_score", 50)))),
                "score": float(c.get("score", 0) or 0),
                "text": str(c.get("text", ""))[:3000], "title": str(c.get("title", ""))[:120],
                "hashtags": [str(h)[:40] for h in (c.get("hashtags") or [])][:8],
                "file": name, "srt": srt,
            })
    except HTTPException:
        raise
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid manifest values.")
    (job_dir / "manifest.json").write_text(json.dumps(clips, indent=2))
    _finish_job(job_id, clips, duration, language)
    log.info("job %s finished by external worker (%d clips)", job_id, len(clips))
    return {"ok": True, "clips": len(clips)}


@app.get("/worker/bundle.zip")
def worker_bundle():
    """Everything a computer needs to run peakcut_worker.py, in one download."""
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in ("peakcut_worker.py", "pipeline_lib.py", "dub_lib.py", "requirements-worker.txt"):
            zf.write(BASE_DIR / name, arcname=f"peakcut-worker/{name}")
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": 'attachment; filename="peakcut-worker.zip"'})


# ── Account ──────────────────────────────────────────────────────────────────

@app.post("/account/delete")
def delete_account(request: Request):
    """Self-serve data deletion (Settings > Danger zone used to be an alert
    telling people to email support). Does not cancel a subscription — that
    lives in Stripe's billing portal."""
    identity = get_identity(request)
    cid_identity = f"cid:{get_client_id(request)}"
    with _usage_lock:
        data = _json_load(USAGE_FILE)
        data.pop(identity, None)
        data.pop(cid_identity, None)
        _json_save(USAGE_FILE, data)
    with _api_lock:
        data = _load_api_data()
        data["keys"] = {h: k for h, k in data["keys"].items() if k.get("identity") != identity}
        data["devices"] = {h: d for h, d in data.get("devices", {}).items() if d.get("identity") != identity}
        data["usage"].pop(identity, None)
        _json_save(API_KEYS_FILE, data)
    with _jobs_lock:
        mine = [jid for jid, j in _jobs.items()
                if j.get("identity") in (identity, cid_identity) and j.get("status") in ("done", "failed")]
        for jid in mine:
            _jobs.pop(jid, None)
    for jid in mine:
        shutil.rmtree(JOBS_DIR / jid, ignore_errors=True)
    resp = JSONResponse({"deleted": True, "jobs_removed": len(mine)})
    resp.delete_cookie(auth.ACCOUNT_COOKIE)
    resp.delete_cookie("clipai_cid")
    return resp


# ── Sign in with Google ──────────────────────────────────────────────────────

@app.get("/auth/google/login")
def google_login(request: Request, next: str | None = None):
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
    # Only same-site relative paths, so this can't become an open redirect.
    if next and re.fullmatch(r"/[A-Za-z0-9/_-]*", next):
        resp.set_cookie("clipai_next", next, max_age=600, httponly=True, samesite="lax")
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
        return RedirectResponse(f"{config.SITE_URL}/?auth_error=unverified", status_code=303)

    next_path = request.cookies.get("clipai_next")
    target = next_path if next_path and re.fullmatch(r"/[A-Za-z0-9/_-]*", next_path) else "/?signed_in=1"
    account_token = auth.sign_account_cookie(claims["sub"], claims["email"])
    resp = RedirectResponse(f"{config.SITE_URL}{target}", status_code=303)
    resp.set_cookie(auth.ACCOUNT_COOKIE, account_token, max_age=auth.ACCOUNT_MAX_AGE,
                    httponly=True, samesite="lax", secure=config.SITE_URL.startswith("https"))
    resp.delete_cookie(auth.STATE_COOKIE)
    resp.delete_cookie("clipai_next")
    return resp


@app.get("/auth/logout")
def logout():
    resp = RedirectResponse(f"{config.SITE_URL}/", status_code=303)
    resp.delete_cookie(auth.ACCOUNT_COOKIE)
    return resp


# ── Stripe billing ──────────────────────────────────────────────────────────

def _start_checkout(request: Request, price_id: str, cancel_path: str, addon: bool = False):
    if config.GOOGLE_SIGNIN_CONFIGURED and not get_account(request):
        return RedirectResponse("/auth/google/login?next=/pricing", status_code=303)
    if not config.STRIPE_CONFIGURED:
        raise HTTPException(status_code=503, detail="Billing isn't configured yet.")

    plan = get_plan(request)
    customer = get_customer_id(request)
    if addon and has_own_hardware(request):
        return RedirectResponse("/settings#computers", status_code=303)  # already unlocked
    if not addon and plan != "free" and customer:
        # Already subscribed: switching plans happens in the billing portal,
        # not a second subscription (which would double-charge).
        return RedirectResponse("/billing-portal", status_code=303)

    identity = get_identity(request)
    account = get_account(request)
    params = dict(
        mode="subscription",
        line_items=[{"price": price_id, "quantity": 1}],
        success_url=f"{config.SITE_URL}/confirm-checkout?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{config.SITE_URL}{cancel_path}",
        client_reference_id=identity[:200],
        allow_promotion_codes=True,  # launch / creator discount codes
    )
    if customer:
        params["customer"] = customer
    elif account:
        params["customer_email"] = account["email"]
    try:
        session = stripe.checkout.Session.create(**params)
    except Exception:
        log.exception("stripe checkout session creation failed")
        raise HTTPException(status_code=500, detail="Couldn't start checkout — please try again shortly.")

    resp = RedirectResponse(session.url, status_code=303)
    set_cid_cookie(resp, request)
    return resp


@app.get("/create-checkout-session")
def create_checkout_session(request: Request):
    return _start_checkout(request, config.STRIPE_PRICE_ID, "/pricing")


@app.get("/create-checkout-session-plus")
def create_checkout_session_plus(request: Request):
    return _start_checkout(request, config.STRIPE_PRICE_ID_PLUS, "/pricing")


@app.get("/create-checkout-session-hardware")
def create_checkout_session_hardware(request: Request):
    """The own-hardware feature on its own, for Free users who don't need Pro."""
    if not config.STRIPE_PRICE_ID_HARDWARE:
        raise HTTPException(status_code=503, detail="The own-hardware add-on isn't on sale yet — it's included with Pro.")
    return _start_checkout(request, config.STRIPE_PRICE_ID_HARDWARE, "/pricing", addon=True)


@app.get("/billing-portal")
def billing_portal(request: Request):
    """Self-serve cancel / switch plan / update card / invoices. The #1
    complaint about other clippers is not being able to cancel."""
    customer_id = get_customer_id(request)
    if not customer_id:
        if config.GOOGLE_SIGNIN_CONFIGURED and not get_account(request):
            return RedirectResponse("/auth/google/login?next=/settings", status_code=303)
        return RedirectResponse(f"{config.SITE_URL}/pricing?no_subscription=1", status_code=303)
    try:
        session = stripe.billing_portal.Session.create(customer=customer_id, return_url=f"{config.SITE_URL}/settings")
    except Exception:
        log.exception("stripe billing portal session creation failed")
        raise HTTPException(status_code=500, detail="Couldn't open billing portal — try again shortly.")
    invalidate_plan_cache(customer_id)
    return RedirectResponse(session.url, status_code=303)


@app.get("/confirm-checkout")
def confirm_checkout(session_id: str):
    """Looked up server-to-server, so it can't be spoofed by editing the URL."""
    try:
        session = stripe.checkout.Session.retrieve(session_id)
    except Exception:
        log.exception("failed to retrieve checkout session")
        return RedirectResponse(f"{config.SITE_URL}/?upgrade_error=1", status_code=303)

    if session.payment_status in ("paid", "no_payment_required") and session.customer:
        invalidate_plan_cache(session.customer)
        resp = RedirectResponse(f"{config.SITE_URL}/?upgraded=1", status_code=303)
        set_customer_cookie(resp, session.customer)
        return resp
    return RedirectResponse(f"{config.SITE_URL}/?upgrade_error=1", status_code=303)


@app.post("/stripe/webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, config.STRIPE_WEBHOOK_SECRET)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid webhook signature.")

    obj = event["data"]["object"]
    customer = obj.get("customer") if hasattr(obj, "get") else None
    if event["type"] == "checkout.session.completed":
        log.info("checkout completed for customer %s", customer)
    elif event["type"].startswith("customer.subscription."):
        log.info("%s for customer %s", event["type"], customer)
    invalidate_plan_cache(customer)
    return {"received": True}


# ── API ──────────────────────────────────────────────────────────────────────

def _api_require_signin(request: Request) -> dict:
    # Without real accounts, "identity" is a browser cookie anyone can reset,
    # which made every API allowance infinitely farmable.
    if not config.GOOGLE_SIGNIN_CONFIGURED:
        raise HTTPException(status_code=503, detail="API keys need Google sign-in to be configured on this server.")
    account = get_account(request)
    if not account:
        raise HTTPException(status_code=401, detail="Sign in required to manage API keys.")
    return account


@app.post("/api/keys")
def create_api_key(request: Request):
    account = _api_require_signin(request)
    identity = get_identity(request)
    data = load_api_keys()
    mine = [k for k in data["keys"].values() if k.get("identity") == identity and k.get("active")]
    if len(mine) >= config.MAX_API_KEYS_PER_USER:
        raise HTTPException(status_code=400, detail=f"You can have up to {config.MAX_API_KEYS_PER_USER} active keys. Revoke one first.")
    api_key = generate_api_key(identity, email=account["email"], customer_id=get_customer_id(request))
    plan = get_plan(request)
    return {"api_key": api_key, "tier": plan, "monthly_limit": config.API_LIMITS[plan]}


@app.get("/api/keys")
def list_api_keys(request: Request):
    _api_require_signin(request)
    identity = get_identity(request)
    plan = get_plan(request)
    data = load_api_keys()
    keys = [
        {"id": h[:12], "prefix": info.get("prefix", "pk_") + "…", "created": info["created"],
         "active": info.get("active", True)}
        for h, info in data["keys"].items() if info.get("identity") == identity and info.get("active", True)
    ]
    return {"keys": keys, "tier": plan, "used_this_month": api_usage(identity),
            "monthly_limit": config.API_LIMITS[plan], "resets_on": _next_reset_iso()}


@app.delete("/api/keys/{key_id}")
def revoke_api_key(key_id: str, request: Request):
    _api_require_signin(request)
    identity = get_identity(request)
    with _api_lock:
        data = _load_api_data()
        for h, info in data["keys"].items():
            if h.startswith(key_id) and len(key_id) >= 12 and info.get("identity") == identity:
                info["active"] = False
                _json_save(API_KEYS_FILE, data)
                return {"revoked": True}
    raise HTTPException(status_code=404, detail="Key not found.")


@app.post("/api/v1/process")
async def api_process(request: Request, file: UploadFile = File(...), clip_format: str = Form("vertical"),
                      caption_style: str = Form("bold"), layout: str = Form("crop"),
                      clip_length: str = Form("medium"), n_clips: int = Form(3)):
    api_key = _bearer_key(request)
    if not api_key:
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header. Use: Authorization: Bearer <api_key>")
    key_info = get_api_key_info(api_key)
    if not key_info or not key_info.get("active"):
        raise HTTPException(status_code=401, detail="Invalid or inactive API key.")

    # Validate everything before counting usage, so a typo doesn't burn a call.
    _validate_options(clip_format, caption_style, layout, clip_length)
    ext = _validate_ext(file.filename)
    if _cloud_queue_full():
        raise HTTPException(status_code=503, detail="Server queue full, please try again shortly.", headers={"Retry-After": "120"})
    if not check_ip_rate_limit(f"api:{key_info['identity']}", limit=30):
        raise HTTPException(status_code=429, detail="Too many requests — max 30 videos per hour.", headers={"Retry-After": "600"})

    plan = api_plan(key_info)
    identity = key_info["identity"]
    if not reserve_api_use(identity, plan):
        raise HTTPException(status_code=429, detail=(
            f"Monthly API limit reached for the {PLAN_LABELS[plan]} tier ({config.API_LIMITS[plan]} videos). "
            f"Resets {_next_reset_iso()}."))

    job_id = _new_job_id()
    job_dir = JOBS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    video_path = job_dir / f"input{ext}"
    try:
        await _save_upload(file, video_path)
    except HTTPException:
        shutil.rmtree(job_dir, ignore_errors=True)
        refund_api_use(identity)
        raise
    except Exception:
        shutil.rmtree(job_dir, ignore_errors=True)
        refund_api_use(identity)
        log.exception("API upload failed")
        raise HTTPException(status_code=500, detail="Upload failed — please try again.")

    _set_job(job_id, status="queued", api_key=True, clip_format=clip_format, caption_style=caption_style,
             layout=layout, clip_length=clip_length, n_clips=max(1, min(int(n_clips or 3), config.MAX_CLIPS[plan])),
             input_path=str(video_path), identity=identity, is_pro=plan != "free", plan=plan,
             created_at=time.time())
    try:
        _enqueue(job_id, 0 if plan != "free" else 1)
    except queue.Full:
        shutil.rmtree(job_dir, ignore_errors=True)
        with _jobs_lock:
            _jobs.pop(job_id, None)
        refund_api_use(identity)
        raise HTTPException(status_code=503, detail="Server queue full, please try again shortly.", headers={"Retry-After": "120"})

    return {"job_id": job_id, "status": "queued"}


@app.get("/api/v1/clips/{job_id}")
def api_get_clips(job_id: str, request: Request):
    """Poll a job. Requires the API key (or browser session) that submitted it."""
    key = _bearer_key(request)
    if key:
        info = get_api_key_info(key)
        if not info or not info.get("active"):
            raise HTTPException(status_code=401, detail="Invalid API key.")
    job = _require_job(request, job_id)

    status = job["status"]
    if status == "queued":
        return {"status": "queued", "queue_position": _queue_position(job_id)}
    if status == "processing":
        return {"status": "processing", "stage": job.get("stage"), "progress": job.get("progress", 0)}
    if status == "failed":
        # Docs (and their example client) check for "error"; the worker used
        # to record "failed", so API clients polled forever.
        return {"status": "error", "error": job.get("error", "Processing failed.")}
    clips = []
    for clip in job.get("clips", []):
        url = f"{config.SITE_URL}/jobs/{job_id}/{clip['file']}"
        clips.append({
            "rank": clip["rank"], "start": clip["start"], "end": clip["end"],
            "text": clip["text"], "title": clip.get("title"), "hashtags": clip.get("hashtags", []),
            "virality_score": clip["virality_score"], "url": url, "download_url": url + "?download=1",
            "srt_url": f"{config.SITE_URL}/jobs/{job_id}/{clip['srt']}" if clip.get("srt") else None,
        })
    return {"status": "done", "duration": job.get("duration"), "language": job.get("language"),
            "expires_at": round(job.get("finished_at", time.time()) + JOB_MAX_AGE_SECONDS), "clips": clips}
