# ── ONE-TIME SETUP ──────────────────────────────────────────────────────────
# 1. Stripe: create a product + recurring price ($10-20/mo) in your Stripe
#    Dashboard, then fill in the three values below from Developers > API keys
#    and Developers > Webhooks (after pointing a webhook at /stripe/webhook
#    for the checkout.session.completed and customer.subscription.deleted
#    events).
# 2. AdSense: sign up at https://adsense.google.com with this site's live URL.
#    Once approved, paste your publisher ID below and uncomment the script
#    tag in index.html's <head>.
# 3. Google Sign-In: create an OAuth Client ID (Google Cloud Console >
#    APIs & Services > Credentials > Create Credentials > OAuth client ID >
#    Web application). Add {SITE_URL}/auth/google/callback as an authorized
#    redirect URI. Paste the client ID + secret below. Also set
#    SESSION_SECRET_KEY to a long random string (e.g. `openssl rand -hex 32`)
#    — it signs the account cookie, so losing it invalidates every signed-in
#    session, and anyone who has it could forge one.
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

STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "sk_test_REPLACE_ME")
STRIPE_PRICE_ID = os.environ.get("STRIPE_PRICE_ID", "price_REPLACE_ME")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "whsec_REPLACE_ME")
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
FREE_LIMIT = int(os.environ.get("FREE_LIMIT", "1"))
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "300"))
ALLOWED_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}
