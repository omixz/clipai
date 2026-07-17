# ── ONE-TIME SETUP ──────────────────────────────────────────────────────────
# 1. Stripe: create a product + recurring price ($10-20/mo) in your Stripe
#    Dashboard, then fill in the three values below from Developers > API keys
#    and Developers > Webhooks (after pointing a webhook at /stripe/webhook
#    for the checkout.session.completed and customer.subscription.deleted
#    events).
# 2. AdSense: sign up at https://adsense.google.com with this site's live URL.
#    Once approved, paste your publisher ID below and uncomment the script
#    tag in index.html's <head>.
# ─────────────────────────────────────────────────────────────────────────
import os

STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "sk_test_REPLACE_ME")
STRIPE_PRICE_ID = os.environ.get("STRIPE_PRICE_ID", "price_REPLACE_ME")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "whsec_REPLACE_ME")
ADSENSE_PUBLISHER_ID = os.environ.get("ADSENSE_PUBLISHER_ID", "ca-pub-REPLACE_ME")

SITE_URL = os.environ.get("SITE_URL", "http://localhost:8000")
FREE_LIMIT = int(os.environ.get("FREE_LIMIT", "1"))
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "300"))
ALLOWED_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}
