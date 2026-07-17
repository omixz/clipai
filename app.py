import json
import logging
import os
import shutil
import uuid
from pathlib import Path

import stripe
from fastapi import FastAPI, File, UploadFile, Request, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

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


def load_usage():
    return json.loads(USAGE_FILE.read_text())


def save_usage(data):
    USAGE_FILE.write_text(json.dumps(data))


def get_client_id(request: Request) -> str:
    return request.cookies.get("clipai_cid") or str(uuid.uuid4())


def get_client_record(usage_data, cid):
    # NOTE: usage.json only tracks the free-tier counter now — see check_pro_status
    # for why "pro" is never stored here.
    return usage_data.get(cid, {"used": 0})


def check_pro_status(request: Request) -> bool:
    """Pro status is checked live against Stripe via a customer ID cookie set
    after a successful checkout — NOT read from local storage. Render's free
    tier has no persistent disk, so usage.json is wiped on every redeploy and
    every spin-down/spin-up after idle. A paying customer's access must not
    depend on that file surviving, or they'd silently lose Pro the next time
    the service restarts."""
    customer_id = request.cookies.get("clipai_customer")
    if not customer_id:
        return False
    try:
        subs = stripe.Subscription.list(customer=customer_id, status="active", limit=1)
        return len(subs.data) > 0
    except Exception:
        log.exception("Stripe subscription lookup failed for customer %s", customer_id)
        return False


@app.get("/", response_class=HTMLResponse)
def index():
    return (BASE_DIR / "index.html").read_text()


@app.get("/usage")
def usage(request: Request):
    cid = get_client_id(request)
    rec = get_client_record(load_usage(), cid)
    is_pro = check_pro_status(request)
    remaining = None if is_pro else max(0, config.FREE_LIMIT - rec["used"])
    return {"client_id": cid, "used": rec["used"], "pro": is_pro,
             "limit": config.FREE_LIMIT, "remaining": remaining}


@app.post("/process")
async def process(request: Request, file: UploadFile = File(...)):
    cid = get_client_id(request)
    usage_data = load_usage()
    rec = get_client_record(usage_data, cid)
    is_pro = check_pro_status(request)

    if not is_pro and rec["used"] >= config.FREE_LIMIT:
        raise HTTPException(status_code=402, detail="Free plan limit reached (1 video). Upgrade to Pro for unlimited clips.")

    ext = Path(file.filename or "").suffix.lower()
    if ext not in config.ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Unsupported file type '{ext}'. Use MP4, MOV, M4V, WEBM, or MKV.")

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
        raise
    except Exception as e:
        shutil.rmtree(job_dir, ignore_errors=True)
        log.exception("upload failed")
        raise HTTPException(status_code=500, detail="Upload failed — please try again.")

    try:
        result = pipeline_lib.process_video(str(input_path), str(job_dir), n_clips=3)
    except Exception as e:
        log.exception("processing failed for job %s", job_id)
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail="Processing failed — the video may be too short, silent, or an unsupported codec.")

    if not result["clips"]:
        raise HTTPException(status_code=422, detail="Couldn't find any usable clips in that video — try a longer or louder source.")

    if not is_pro:
        rec["used"] += 1
        usage_data[cid] = rec
        save_usage(usage_data)

    for clip in result["clips"]:
        clip["url"] = f"/jobs/{job_id}/{clip['file']}"

    remaining = None if is_pro else max(0, config.FREE_LIMIT - rec["used"])
    resp = JSONResponse({"job_id": job_id, "duration": result["duration"], "clips": result["clips"],
                          "pro": is_pro, "remaining_free": remaining})
    resp.set_cookie("clipai_cid", cid, max_age=60 * 60 * 24 * 365, httponly=True, samesite="lax")
    return resp


@app.get("/jobs/{job_id}/{filename}")
def get_clip(job_id: str, filename: str):
    if "/" in job_id or "/" in filename or ".." in job_id or ".." in filename:
        raise HTTPException(status_code=400, detail="Invalid path.")
    path = JOBS_DIR / job_id / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="Not found.")
    return FileResponse(str(path))


# ── Stripe billing ──────────────────────────────────────────────────────────

@app.get("/create-checkout-session")
def create_checkout_session(request: Request):
    cid = get_client_id(request)
    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            line_items=[{"price": config.STRIPE_PRICE_ID, "quantity": 1}],
            success_url=f"{config.SITE_URL}/confirm-checkout?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{config.SITE_URL}/",
            client_reference_id=cid,
        )
    except Exception as e:
        log.exception("stripe checkout session creation failed")
        raise HTTPException(status_code=500, detail="Couldn't start checkout — billing isn't configured yet.")

    resp = RedirectResponse(session.url, status_code=303)
    resp.set_cookie("clipai_cid", cid, max_age=60 * 60 * 24 * 365, httponly=True, samesite="lax")
    return resp


@app.get("/confirm-checkout")
def confirm_checkout(session_id: str):
    """Stripe redirects here right after a successful checkout. We look the
    session up (server-to-server, so this can't be spoofed by editing the URL)
    and, if it really did complete, store the Stripe customer ID in a cookie.
    From then on, Pro status is checked live against that customer ID — see
    check_pro_status — so it survives restarts/redeploys without needing our
    own database."""
    try:
        session = stripe.checkout.Session.retrieve(session_id)
    except Exception:
        log.exception("failed to retrieve checkout session %s", session_id)
        return RedirectResponse(f"{config.SITE_URL}/?upgrade_error=1", status_code=303)

    resp = RedirectResponse(f"{config.SITE_URL}/?upgraded=1", status_code=303)
    if session.payment_status in ("paid", "no_payment_required") and session.customer:
        resp.set_cookie("clipai_customer", session.customer, max_age=60 * 60 * 24 * 365,
                          httponly=True, samesite="lax")
    return resp


@app.post("/stripe/webhook")
async def stripe_webhook(request: Request):
    """Not required for Pro status anymore (that's checked live via Stripe —
    see check_pro_status), but Stripe still expects a webhook endpoint to
    exist for the events you configure, and it's useful for logging/alerting
    on cancellations."""
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
