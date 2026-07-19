"""Sign in with Google. Kept deliberately dependency-light: httpx (already a
transitive dep via stripe/huggingface_hub) for the OAuth HTTP calls, PyJWT for
verifying Google's signed ID token, itsdangerous for signing our own account
cookie so it can't be forged client-side."""
import logging
import secrets
import time

import httpx
import jwt
from itsdangerous import BadSignature, URLSafeTimedSerializer
from jwt import PyJWKClient

import config

log = logging.getLogger("clipai.auth")

GOOGLE_DISCOVERY_URL = "https://accounts.google.com/.well-known/openid-configuration"
ACCOUNT_COOKIE = "clipai_account"
STATE_COOKIE = "clipai_oauth_state"
ACCOUNT_MAX_AGE = 60 * 60 * 24 * 365

_discovery_cache = {"data": None, "at": 0.0}
_jwks_client = None
_serializer = URLSafeTimedSerializer(config.SESSION_SECRET_KEY, salt="clipai-account-v1")


def _discovery():
    global _discovery_cache
    if not _discovery_cache["data"] or time.time() - _discovery_cache["at"] > 3600:
        resp = httpx.get(GOOGLE_DISCOVERY_URL, timeout=10)
        resp.raise_for_status()
        _discovery_cache = {"data": resp.json(), "at": time.time()}
    return _discovery_cache["data"]


def _jwks():
    global _jwks_client
    if _jwks_client is None:
        _jwks_client = PyJWKClient(_discovery()["jwks_uri"])
    return _jwks_client


def build_login_url(redirect_uri: str, state: str) -> str:
    authorization_endpoint = _discovery()["authorization_endpoint"]
    params = httpx.QueryParams({
        "client_id": config.GOOGLE_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid email",
        "state": state,
        "prompt": "select_account",
    })
    return f"{authorization_endpoint}?{params}"


def new_state() -> str:
    return secrets.token_urlsafe(24)


def exchange_code_for_claims(code: str, redirect_uri: str) -> dict:
    """Full round trip: swap the auth code for tokens, then verify the ID
    token's signature against Google's current public keys (not just decode
    it — anyone can construct an unsigned-looking JWT, verifying the
    signature is what actually proves Google issued it)."""
    token_endpoint = _discovery()["token_endpoint"]
    resp = httpx.post(token_endpoint, data={
        "code": code,
        "client_id": config.GOOGLE_CLIENT_ID,
        "client_secret": config.GOOGLE_CLIENT_SECRET,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }, timeout=10)
    resp.raise_for_status()
    id_token = resp.json()["id_token"]

    signing_key = _jwks().get_signing_key_from_jwt(id_token)
    claims = jwt.decode(
        id_token, signing_key.key, algorithms=["RS256"],
        audience=config.GOOGLE_CLIENT_ID,
        issuer=["https://accounts.google.com", "accounts.google.com"],
    )
    return claims


def sign_account_cookie(sub: str, email: str) -> str:
    return _serializer.dumps({"sub": sub, "email": email})


def read_account_cookie(token: str) -> dict | None:
    if not token:
        return None
    try:
        return _serializer.loads(token, max_age=ACCOUNT_MAX_AGE)
    except BadSignature:
        return None
    except Exception:
        log.exception("failed to parse account cookie")
        return None


# clipai_customer holds a Stripe customer id and is trusted at face value
# (get_account_tier/billing_portal query Stripe directly with whatever value
# is in it, with zero further verification) -- but Stripe customer ids are
# NOT secrets by Stripe's own design (they show up in dashboard URLs,
# receipts, webhook payloads, support tickets...), so an unsigned cookie lets
# anyone who learns/guesses another customer's id set it as their own cookie
# and get that customer's Pro/Pro Plus tier for free -- or worse, open that
# customer's actual Stripe billing portal (their payment methods, invoices,
# ability to cancel their subscription) via billing_portal. Signed with a
# distinct salt from the account cookie so the two can't be swapped for
# each other even though both use the same underlying secret key.
_customer_serializer = URLSafeTimedSerializer(config.SESSION_SECRET_KEY, salt="clipai-customer-v1")
CUSTOMER_MAX_AGE = 60 * 60 * 24 * 365


def sign_customer_cookie(customer_id: str) -> str:
    return _customer_serializer.dumps({"customer_id": customer_id})


def read_customer_cookie(token: str) -> str | None:
    if not token:
        return None
    try:
        return _customer_serializer.loads(token, max_age=CUSTOMER_MAX_AGE)["customer_id"]
    except BadSignature:
        return None
    except Exception:
        log.exception("failed to parse customer cookie")
        return None
