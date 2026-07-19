"""NOWPayments integration -- crypto billing alongside direct bank transfer.
Genuinely no ID/tax verification needed to receive funds (crypto lands in a
wallet, not a bank account a processor has to KYC), the thing bank transfer
can't offer: an actual webhook, so this path can auto-grant Pro the moment
a payment confirms instead of needing an admin to notice and click Grant.

NOWPayments has no native recurring subscriptions (it's invoice-based, one
payment per invoice) -- same limitation as bank transfer, just automated:
a customer pays again next month, the webhook grants again.
"""
import hashlib
import hmac
import json
import logging

import httpx

import config

log = logging.getLogger("clipai.crypto")

API_BASE = "https://api.nowpayments.io/v1"
TIMEOUT = 15

# Statuses NOWPayments considers a completed payment. "confirmed" and
# "finished" both mean the funds are in -- finished is after NOWPayments'
# own payout step, which isn't relevant to just deciding "did this person
# pay," so either is treated as success here.
PAID_STATUSES = {"confirmed", "finished"}


def _headers() -> dict:
    return {"x-api-key": config.NOWPAYMENTS_API_KEY, "Content-Type": "application/json"}


def create_invoice(price_amount: float, order_id: str, order_description: str, ipn_callback_url: str,
                    success_url: str, cancel_url: str) -> str:
    """Creates a hosted NOWPayments invoice and returns its URL -- the
    customer picks which crypto to pay with on that page, we don't have to
    know in advance."""
    body = {
        "price_amount": price_amount,
        "price_currency": "usd",
        "order_id": order_id,
        "order_description": order_description,
        "ipn_callback_url": ipn_callback_url,
        "success_url": success_url,
        "cancel_url": cancel_url,
    }
    resp = httpx.post(f"{API_BASE}/invoice", headers=_headers(), json=body, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()["invoice_url"]


def verify_ipn_signature(payload: dict, signature_header: str) -> bool:
    """NOWPayments signs the IPN body as HMAC-SHA512 of the JSON-serialized
    payload with keys sorted alphabetically (including nested objects --
    json.dumps(..., sort_keys=True) handles that recursively) and compact
    separators, keyed with the IPN secret. Sent as the x-nowpayments-sig
    header. Verifying against the parsed-then-resorted payload rather than
    the raw request bytes is deliberate and matches NOWPayments' own
    documented approach -- their signer doesn't promise byte-for-byte
    stability of what it received, only of a canonical re-serialization."""
    if not signature_header or not config.NOWPAYMENTS_IPN_SECRET:
        return False
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    digest = hmac.new(config.NOWPAYMENTS_IPN_SECRET.encode(), serialized.encode(), hashlib.sha512).hexdigest()
    return hmac.compare_digest(digest, signature_header)
