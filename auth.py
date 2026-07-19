"""Sign in with Google. Kept deliberately dependency-light: httpx (already a
transitive dep via huggingface_hub, and also used directly for PayPal API
calls) for the OAuth HTTP calls, PyJWT for verifying Google's signed ID
token, itsdangerous for signing our own account cookie so it can't be
forged client-side."""
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

# SESSION_SECRET_KEY signs this account cookie, which Pro-tier lookups now
# key off of too (see get_identity/get_account_tier in app.py) -- if a
# deployer sets up Google Sign-In but forgets to also set this one
# (plausible: it's not required for the OAuth handshake to look like it
# works end-to-end), it silently stays at this hardcoded default, which is
# visible in this public repo. Anyone who's read the source could then forge
# a validly-"signed" cookie claiming to be any user -- full account takeover
# / free Pro access for the taking, and nothing about a working-looking
# sign-in flow would reveal it. Rather than just warn and hope someone reads
# the logs, read_account_cookie refuses to trust ANY cookie while this is
# true -- sign-in and billing degrade to "doesn't work" instead of "is
# forgeable", which is a failure a deployer will actually notice while
# testing.
SESSION_SECRET_IS_DEFAULT = config.SESSION_SECRET_KEY == "dev-only-insecure-secret-REPLACE_ME"
if SESSION_SECRET_IS_DEFAULT:
    log.warning(
        "SESSION_SECRET_KEY is still the default placeholder -- signed cookies "
        "(Google sign-in, and Pro status since it's keyed off the signed-in "
        "account) will be refused entirely until a real secret is set. Set "
        "SESSION_SECRET_KEY (openssl rand -hex 32)."
    )


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
    if not token or SESSION_SECRET_IS_DEFAULT:
        return None
    try:
        return _serializer.loads(token, max_age=ACCOUNT_MAX_AGE)
    except BadSignature:
        return None
    except Exception:
        log.exception("failed to parse account cookie")
        return None
