"""Stripe Checkout + Subscriptions billing integration -- the primary/only
billing path once STRIPE_SECRET_KEY is configured (see config.py). Takes
over from bank transfer/PayPal/NOWPayments entirely when set -- see
app.py's _checkout_methods().

Mirrors the same security property every prior integration here was built
around: nothing about a user's paid tier is trusted from local storage or a
client-supplied value. A Stripe customer id gets linked to a user's
identity once Checkout completes (see /confirm-checkout and /stripe/webhook
in app.py), but get_subscription() is a live API call made on every tier
check, so a cancelled subscription stops granting Pro the moment Stripe's
own status says so.
"""
import logging

import stripe

import config

log = logging.getLogger("clipai.billing")

stripe.api_key = config.STRIPE_SECRET_KEY

# "trialing" counts as paid -- a subscription with a trial period already
# has a card on file and will be charged at trial end, so treating it as
# free until then would undersell it.
PAID_STATUSES = {"active", "trialing"}


def create_checkout_session(price_id: str, identity: str, email: str | None, success_url: str, cancel_url: str) -> str:
    """Creates a Stripe Checkout Session for a new subscription and returns
    its hosted URL. `client_reference_id` carries `identity` through to the
    webhook/`/confirm-checkout` return trip -- unlike PayPal, there's no
    customer id to link ahead of time here (Stripe creates one during
    Checkout itself)."""
    session = stripe.checkout.Session.create(
        mode="subscription",
        line_items=[{"price": price_id, "quantity": 1}],
        client_reference_id=identity,
        customer_email=email,
        success_url=success_url,
        cancel_url=cancel_url,
    )
    return session.url


def get_checkout_session(session_id: str) -> dict | None:
    """Live lookup used right after Stripe redirects back to
    /confirm-checkout -- returns the customer id + client_reference_id from
    the actual session record rather than trusting anything in the redirect
    URL itself."""
    try:
        session = stripe.checkout.Session.retrieve(session_id)
        return {"customer_id": session.customer, "identity": session.client_reference_id}
    except Exception:
        log.exception("stripe checkout session lookup failed for %s", session_id)
        return None


def get_subscription(customer_id: str) -> dict | None:
    """Live lookup of a customer's current subscription status/price.
    Returns None on any failure or if the customer has no subscription --
    callers treat that the same as "not paid", never as "paid". A customer
    only ever has one relevant subscription here (Pro or Pro Plus, never
    both), so the first one returned is enough."""
    try:
        subs = stripe.Subscription.list(customer=customer_id, status="all", limit=1)
        if not subs.data:
            return None
        sub = subs.data[0]
        price_id = sub["items"]["data"][0]["price"]["id"]
        return {"status": sub.status, "price_id": price_id}
    except Exception:
        log.exception("stripe subscription lookup failed for customer %s", customer_id)
        return None


def tier_for_price(price_id: str) -> str:
    if price_id == config.STRIPE_PRICE_ID_PLUS:
        return "pro_plus"
    if price_id == config.STRIPE_PRICE_ID_PRO:
        return "pro"
    return "free"


def create_billing_portal_session(customer_id: str, return_url: str) -> str:
    session = stripe.billing_portal.Session.create(customer=customer_id, return_url=return_url)
    return session.url


def verify_webhook_event(payload: bytes, sig_header: str) -> dict | None:
    """Stripe's signature scheme is a local HMAC compare against
    STRIPE_WEBHOOK_SECRET (unlike PayPal, which needs a round trip to
    PayPal's own verification API) -- stripe's construct_event does the
    HMAC + timestamp-tolerance check and returns the parsed event, or raises
    on any mismatch."""
    try:
        return stripe.Webhook.construct_event(payload, sig_header, config.STRIPE_WEBHOOK_SECRET)
    except Exception:
        log.exception("stripe webhook signature verification failed")
        return None
