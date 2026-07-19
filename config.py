# ── ONE-TIME SETUP ──────────────────────────────────────────────────────────
# 1. PayPal (billing -- swapped in for Lemon Squeezy, which (like Stripe
#    before it) needed identity/tax verification that wasn't available;
#    PayPal has a lighter bar to start, though it will still ask for tax
#    info once volume grows -- deferred, not solved, by this swap):
#    - PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET: developer.paypal.com > Apps &
#      Credentials > create a REST API app. Use the Sandbox app's credentials
#      first (PAYPAL_MODE=sandbox) to test end-to-end before going live.
#    - Create a Pro and a Pro Plus subscription Product + Plan (Billing >
#      Plans in the dashboard, or the /v1/billing/plans API) and paste each
#      Plan's id below.
#    - PAYPAL_WEBHOOK_ID: Apps & Credentials > your app > Webhooks > add an
#      endpoint at {SITE_URL}/paypal/webhook subscribed to at least
#      BILLING.SUBSCRIPTION.ACTIVATED and BILLING.SUBSCRIPTION.CANCELLED
#      (tier checks are always live against PayPal directly, so this is for
#      logging/visibility, not something Pro status depends on arriving).
# 2. AdSense: sign up at https://adsense.google.com with this site's live URL.
#    Once approved, paste your publisher ID below and uncomment the script
#    tag in index.html's <head>.
# 3. Google Sign-In: create an OAuth Client ID (Google Cloud Console >
#    APIs & Services > Credentials > Create Credentials > OAuth client ID >
#    Web application). Add {SITE_URL}/auth/google/callback as an authorized
#    redirect URI. Paste the client ID + secret below. Also set
#    SESSION_SECRET_KEY to a long random string (e.g. `openssl rand -hex 32`)
#    — it signs the account cookie that both sign-in AND Pro-tier lookups key
#    off of (see get_identity/get_account_tier in app.py), so losing it
#    invalidates every signed-in session and every logged-in Pro subscriber's
#    access. This one is NOT optional the moment either Google Sign-In or
#    PayPal is configured: leaving it at the placeholder default
#    doesn't fail open, but auth.py deliberately refuses to trust ANY signed
#    cookie while it's unset, so sign-in and Pro status will look completely
#    broken (never signed in, never Pro) until you set a real value.
#    Sign-in is a soft requirement: while GOOGLE_CLIENT_ID is still the
#    placeholder below, the site keeps working exactly as it does now
#    (anonymous, cookie-tracked, 1 free video). The moment you fill in real
#    credentials, sign-in becomes required and the free-video limit switches
#    from "per browser cookie" to "per Google account" — closing the
#    clear-your-cookies-and-get-another-free-video loophole, which is exactly
#    how OpusClip/Vidyo.ai gate their free tiers too.
# 4. Completion email: sign up free at https://resend.com (3,000 emails/mo,
#    100/day, no card needed), verify a sending domain (or use their shared
#    onboarding domain for testing), and paste the API key below.
# ─────────────────────────────────────────────────────────────────────────
import os

PAYPAL_CLIENT_ID = os.environ.get("PAYPAL_CLIENT_ID", "")
PAYPAL_CLIENT_SECRET = os.environ.get("PAYPAL_CLIENT_SECRET", "")
PAYPAL_MODE = os.environ.get("PAYPAL_MODE", "sandbox")  # "sandbox" or "live"
PAYPAL_PLAN_ID_PRO = os.environ.get("PAYPAL_PLAN_ID_PRO", "")
PAYPAL_PLAN_ID_PLUS = os.environ.get("PAYPAL_PLAN_ID_PLUS", "")
PAYPAL_WEBHOOK_ID = os.environ.get("PAYPAL_WEBHOOK_ID", "")
PAYPAL_CONFIGURED = bool(PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET)

# Direct bank transfer (Australian domestic transfer -- BSB + account
# number, not a US routing number) -- no processor in the loop at all.
# Customers transfer straight to this account; there's no API to check
# payment status against, so upgrades are granted manually (see the
# /admin/billing routes in app.py) once a payment with the matching
# reference actually shows up. Takes priority over PayPal in
# get_account_tier/checkout when configured, since it's the path that
# actually works without ID verification.
BANK_NAME = os.environ.get("BANK_NAME", "")
BANK_ACCOUNT_NAME = os.environ.get("BANK_ACCOUNT_NAME", "")
BANK_ACCOUNT_NUMBER = os.environ.get("BANK_ACCOUNT_NUMBER", "")
BANK_BSB = os.environ.get("BANK_BSB", "")
BANK_PAYID = os.environ.get("BANK_PAYID", "")  # optional -- PayID/Osko transfers settle near-instantly via Australia's NPP, unlike a standard transfer
BANK_PAYMENT_CONFIGURED = bool(BANK_ACCOUNT_NUMBER and BANK_BSB)

ADSENSE_PUBLISHER_ID = os.environ.get("ADSENSE_PUBLISHER_ID", "ca-pub-5158161193547085")

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "REPLACE_ME.apps.googleusercontent.com")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "REPLACE_ME")
SESSION_SECRET_KEY = os.environ.get("SESSION_SECRET_KEY", "dev-only-insecure-secret-REPLACE_ME")

GOOGLE_SIGNIN_CONFIGURED = (
    GOOGLE_CLIENT_ID != "REPLACE_ME.apps.googleusercontent.com"
    and GOOGLE_CLIENT_SECRET != "REPLACE_ME"
)

RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "re_REPLACE_ME")
EMAIL_FROM = os.environ.get("EMAIL_FROM", "Peakcut <onboarding@resend.dev>")
EMAIL_CONFIGURED = RESEND_API_KEY != "re_REPLACE_ME"

CONTACT_EMAIL = os.environ.get("CONTACT_EMAIL", "support@peakcut.example")

SITE_URL = os.environ.get("SITE_URL", "http://localhost:8000")
# Every cookie this app sets should be marked Secure once actually deployed
# behind HTTPS (Caddy/Render both terminate TLS) -- derived from SITE_URL's
# scheme rather than hardcoded True so http://localhost:8000 still works for
# local dev (a browser silently drops a Secure cookie set over plain HTTP).
COOKIE_SECURE = SITE_URL.startswith("https://")

# Monthly video caps per web tier — must match what pricing.html advertises.
# Pro/Pro Plus used to be unlimited in practice (only an active subscription
# was checked, never a count), which both undersold Pro Plus vs Pro and let
# a single $15/mo account hammer the one-worker queue indefinitely.
FREE_LIMIT = int(os.environ.get("FREE_LIMIT", "5"))
PRO_LIMIT = int(os.environ.get("PRO_LIMIT", "20"))
PRO_PLUS_LIMIT = int(os.environ.get("PRO_PLUS_LIMIT", "50"))

# Monthly call caps per API tier (used by increment_api_usage in app.py).
API_FREE_LIMIT = int(os.environ.get("API_FREE_LIMIT", "100"))
API_PRO_LIMIT = int(os.environ.get("API_PRO_LIMIT", "500"))
API_PRO_PLUS_LIMIT = int(os.environ.get("API_PRO_PLUS_LIMIT", "2000"))

MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "300"))
ALLOWED_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}
# MAX_VIDEO_DURATION_MIN (default 45) lives in pipeline_lib.py, not here —
# it's read independently there, same as WHISPER_MODEL, so the pipeline has
# no import-time dependency on config.py.

# Split-screen (Pro/Pro Plus): a second, much smaller/shorter upload for the
# bottom-half background loop. Deliberately far stricter than MAX_UPLOAD_MB —
# it's just a looping visual layer, not something that needs to be long or
# high-res, and every split-screen job already costs an extra ffmpeg
# composite pass on top of the normal render.
BACKGROUND_UPLOAD_MB = int(os.environ.get("BACKGROUND_UPLOAD_MB", "50"))
MAX_BACKGROUND_DURATION_SEC = int(os.environ.get("MAX_BACKGROUND_DURATION_SEC", "60"))

# How many jobs run at once. pipeline_lib and dub_lib cache their
# Whisper model / Piper voices per-thread (threading.local), so this is
# safe to raise — match it to your host's OCPU count (see README's Oracle
# section for memory tradeoffs).
WORKER_COUNT = int(os.environ.get("WORKER_COUNT", "1"))

# Comma-separated Google account emails (case-insensitive) that always get
# Pro Plus, bypassing Stripe entirely — for the site owner's own personal
# use/testing, not a general grant mechanism. Checked against the *signed*
# account cookie set after a real Google sign-in (see auth.py), so this
# can't be spoofed by a client claiming an arbitrary email — only someone
# who actually controls that Google account can trigger it.
ADMIN_EMAILS = {
    e.strip().lower() for e in os.environ.get("ADMIN_EMAILS", "").split(",") if e.strip()
}
