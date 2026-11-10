"""Signed access tokens of the HTTP API (HMAC-SHA256, standard library only).

A token carries the user id and its expiry, never the role: every request looks the user up in the user file again,
so a changed or removed account takes effect at once. The secret comes from `API_TOKEN_SECRET` (`.env` or the host's
secrets, at least 32 characters). DEV and TEST without it use a random secret per process (tokens end with a
restart); PROD without it issues and accepts no token.

Every sign-in starts a session id (`sid`) that renewed tokens keep. Signing out revokes the session: its tokens are
refused until the last of them would have expired anyway. The deny-list lives in the API state store, like the
what-if runs (`STATE_STORE=file` shares it between workers; they then also need the same `API_TOKEN_SECRET`).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from typing import Any

from app.api.state_store import store

logger = logging.getLogger("derkurator.api.tokens")

MIN_SECRET_LENGTH = 32
MAX_SESSION_S = 12 * 3600  # renewals never stretch one sign-in beyond a working day
_process_secret = secrets.token_urlsafe(48)


REVOKED = "revoked"  # state store space: session id -> signed out (forgotten once no token of it is valid any more)


class TokenError(Exception):
    """Missing, malformed, forged or expired token (the message is safe to show)."""


class TokenConfigError(RuntimeError):
    """No usable signing secret in this environment (details go to the log only)."""


def _secret(environment: str) -> bytes:
    configured = os.getenv("API_TOKEN_SECRET", "")
    if len(configured) >= MIN_SECRET_LENGTH:
        return configured.encode("utf-8")
    if environment in ("DEV", "TEST"):
        return _process_secret.encode("utf-8")
    logger.error("API_TOKEN_SECRET missing or shorter than %d characters in %s", MIN_SECRET_LENGTH, environment)
    raise TokenConfigError("API sign-in is not configured.")


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(body: str, environment: str) -> str:
    return _b64(hmac.new(_secret(environment), body.encode("ascii"), hashlib.sha256).digest())


def issue(user_id: str, lifetime_s: float, environment: str, now: float | None = None,
          renews: dict[str, Any] | None = None) -> str:
    """A token for `user_id`; a renewal passes the claims of the token it replaces (same session, same sign-in
    time)."""
    now = time.time() if now is None else now
    renews = renews or {}
    claims = {"sub": user_id, "env": environment, "sid": renews.get("sid") or secrets.token_urlsafe(12),
              "auth": int(renews.get("auth") or now), "iat": int(now), "exp": int(now + lifetime_s)}
    body = _b64(json.dumps(claims, separators=(",", ":")).encode("utf-8"))
    return f"{body}.{_sign(body, environment)}"


def claims(token: str, environment: str, now: float | None = None) -> dict[str, Any]:
    """The claims of a valid token. A token of another environment is invalid (no DEV token in PROD), so is one of a
    signed-out session."""
    now = time.time() if now is None else now
    body, _, signature = (token or "").partition(".")
    if not body or not signature:
        raise TokenError("Sign-in required.")
    if not hmac.compare_digest(signature, _sign(body, environment)):
        raise TokenError("Invalid token.")
    try:
        decoded = json.loads(_unb64(body))
    except (ValueError, UnicodeDecodeError) as exc:
        raise TokenError("Invalid token.") from exc
    if decoded.get("env") != environment or not isinstance(decoded.get("sub"), str):
        raise TokenError("Invalid token.")
    if now >= float(decoded.get("exp", 0)):
        raise TokenError("Session expired – please sign in again.")
    if _is_revoked(decoded.get("sid"), now):
        raise TokenError("Signed out – please sign in again.")
    return decoded


def verify(token: str, environment: str, now: float | None = None) -> str:
    """Returns the user id of a valid token."""
    return claims(token, environment, now)["sub"]


def renewable(decoded: dict[str, Any], now: float | None = None) -> bool:
    """A token may be renewed while its sign-in is younger than MAX_SESSION_S."""
    now = time.time() if now is None else now
    return now - float(decoded.get("auth") or decoded.get("iat") or 0) < MAX_SESSION_S


def revoke(session_id: str | None, lifetime_s: float, now: float | None = None) -> None:
    """Signs a session out: every token of it is refused from now on (kept until a token issued now would expire)."""
    if not session_id:
        return
    now = time.time() if now is None else now
    store().put(REVOKED, session_id, True, now + lifetime_s, now=now)


def _is_revoked(session_id: Any, now: float) -> bool:
    if not isinstance(session_id, str):
        return False
    return store().get(REVOKED, session_id, now=now) is not None


def clear_revoked() -> None:
    """Forget all sign-outs (tests)."""
    store().clear(REVOKED)
