"""PayPal Subscriptions billing integration -- replaces Lemon Squeezy (which
in turn replaced Stripe; neither's identity verification worked out). PayPal
has a much lighter bar to start receiving payments than a merchant-of-record
like Stripe/Lemon Squeezy, though PayPal will still ask for tax info once
volume grows -- a real constraint, not solved here, just deferred.

Mirrors the same security property both prior integrations were built
around: nothing about a user's paid tier is trusted from local storage or a
client-supplied value. A subscription id gets linked to a user's identity
the moment checkout is created (safe to do immediately -- see
create_subscription's docstring for why), but get_subscription() is a live
API call made on every tier check, so a cancelled/expired subscription
stops granting Pro the moment PayPal's own status says so.
"""
import base64
import logging
import time

import httpx

import config

log = logging.getLogger("clipai.billing")

API_BASE = "https://api-m.paypal.com" if config.PAYPAL_MODE == "live" else "https://api-m.sandbox.paypal.com"
TIMEOUT = 10

# ACTIVE is the only status a paying subscriber is in day-to-day. Unlike
# Lemon Squeezy, PayPal flips straight to CANCELLED (not "active until period
# end") the moment a subscription is cancelled, so there's no equivalent of
# LS's "cancelled but still active" grace window to account for here.
PAID_STATUSES = {"ACTIVE"}

_token_cache = {"token": None, "expires_at": 0.0}


def _access_token() -> str:
    if _token_cache["token"] and time.time() < _token_cache["expires_at"] - 60:
        return _token_cache["token"]
    creds = base64.b64encode(f"{config.PAYPAL_CLIENT_ID}:{config.PAYPAL_CLIENT_SECRET}".encode()).decode()
    resp = httpx.post(
        f"{API_BASE}/v1/oauth2/token",
        headers={"Authorization": f"Basic {creds}", "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "client_credentials"},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    _token_cache["token"] = data["access_token"]
    _token_cache["expires_at"] = time.time() + data["expires_in"]
    return _token_cache["token"]


def _headers() -> dict:
    return {"Authorization": f"Bearer {_access_token()}", "Content-Type": "application/json"}


def create_subscription(plan_id: str, identity: str, return_url: str, cancel_url: str) -> tuple[str, str]:
    """Creates a PayPal subscription and returns (subscription_id,
    approve_url). Safe to link identity -> subscription_id immediately
    (before the customer has actually approved it) because the subscription
    starts in APPROVAL_PENDING, which isn't in PAID_STATUSES -- tier checks
    stay 'free' until PayPal's own status flips to ACTIVE, so an abandoned
    checkout just leaves a harmless, permanently-free-tier'd link."""
    body = {
        "plan_id": plan_id,
        "custom_id": identity,
        "application_context": {
            "brand_name": "Peakcut",
            "user_action": "SUBSCRIBE_NOW",
            "return_url": return_url,
            "cancel_url": cancel_url,
        },
    }
    resp = httpx.post(f"{API_BASE}/v1/billing/subscriptions", headers=_headers(), json=body, timeout=TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    approve_url = next(l["href"] for l in data["links"] if l["rel"] == "approve")
    return data["id"], approve_url


def get_subscription(subscription_id: str) -> dict | None:
    """Live lookup of a subscription's current status/plan. Returns None on
    any failure (network error, deleted subscription, bad id) -- callers
    treat that the same as "not paid", never as "paid"."""
    try:
        resp = httpx.get(
            f"{API_BASE}/v1/billing/subscriptions/{subscription_id}", headers=_headers(), timeout=TIMEOUT
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        return {"status": data["status"], "plan_id": data["plan_id"]}
    except Exception:
        log.exception("paypal subscription lookup failed for %s", subscription_id)
        return None


def tier_for_plan(plan_id: str) -> str:
    if plan_id == config.PAYPAL_PLAN_ID_PLUS:
        return "pro_plus"
    if plan_id == config.PAYPAL_PLAN_ID_PRO:
        return "pro"
    return "free"


def verify_webhook_signature(headers: dict, body: bytes) -> bool:
    """PayPal doesn't use a local HMAC compare like Stripe/Lemon Squeezy --
    verification is itself an authenticated API call (POST
    /v1/notifications/verify-webhook-signature) that checks the
    transmission signature against PayPal's own certificate. Slower (a
    network round trip per webhook) but there's no local-secret-based
    shortcut PayPal exposes instead."""
    if not config.PAYPAL_WEBHOOK_ID:
        return False
    try:
        import json
        verify_body = {
            "auth_algo": headers.get("paypal-auth-algo", ""),
            "cert_url": headers.get("paypal-cert-url", ""),
            "transmission_id": headers.get("paypal-transmission-id", ""),
            "transmission_sig": headers.get("paypal-transmission-sig", ""),
            "transmission_time": headers.get("paypal-transmission-time", ""),
            "webhook_id": config.PAYPAL_WEBHOOK_ID,
            "webhook_event": json.loads(body),
        }
        resp = httpx.post(
            f"{API_BASE}/v1/notifications/verify-webhook-signature",
            headers=_headers(), json=verify_body, timeout=TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json().get("verification_status") == "SUCCESS"
    except Exception:
        log.exception("paypal webhook signature verification failed")
        return False
