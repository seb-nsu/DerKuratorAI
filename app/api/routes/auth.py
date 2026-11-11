"""Sign-in: user id and password against the user file (lockouts per account), answer a token.

A token lasts the session timeout (`auth.session_timeout_minutes`). The front ends renew it while the user works
(POST /auth/refresh, at most 12 hours after the sign-in), so the timeout acts on inactivity. Signing
out (POST /auth/sign-out) revokes the session on the server, not only in the browser."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.api import tokens
from app.api.deps import current_user, environment, token_claims, unauthorized
from app.core.auth import AuthConfigError, User, sign_in
from app.core.runtime_config import get_runtime_config

router = APIRouter(tags=["auth"])


class Credentials(BaseModel):
    user_id: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=1, max_length=1000)


class UserInfo(BaseModel):
    user_id: str
    role: str
    display_name: str
    demo: bool
    pmo_head: bool = False  # changes the governance rules (business rules, required fields)


class TokenAnswer(BaseModel):
    access_token: str
    token_type: str
    expires_in: int
    user: UserInfo


def user_json(user: User) -> UserInfo:
    return UserInfo(user_id=user.user_id, role=user.role, display_name=user.display_name, demo=user.demo,
                    pmo_head=user.pmo_head)


@router.post("/auth/token")
def token(body: Credentials) -> TokenAnswer:
    try:
        result = sign_in(body.user_id, body.password)
    except AuthConfigError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Sign-in is not configured.") from exc
    if not result.ok or result.user is None:
        raise unauthorized(result.message)
    lifetime = _lifetime()
    try:
        access = tokens.issue(result.user.user_id, lifetime, environment())
    except tokens.TokenConfigError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Sign-in is not configured.") from exc
    return TokenAnswer(access_token=access, token_type="bearer",  # noqa: S106 - the OAuth token type, no secret
                       expires_in=int(lifetime), user=user_json(result.user))


class SignedOut(BaseModel):
    message: str


def _lifetime() -> float:
    return get_runtime_config().auth.session_timeout_minutes * 60


@router.post("/auth/refresh")
def refresh(claims: dict = Depends(token_claims), user: User = Depends(current_user)) -> TokenAnswer:
    """A fresh token of the same session while the current one is still valid (sliding session timeout)."""
    if not tokens.renewable(claims):
        raise unauthorized("Session expired – please sign in again.")
    lifetime = _lifetime()
    access = tokens.issue(user.user_id, lifetime, environment(), renews=claims)
    return TokenAnswer(access_token=access, token_type="bearer",  # noqa: S106 - the OAuth token type, no secret
                       expires_in=int(lifetime), user=user_json(user))


@router.post("/auth/sign-out")
def sign_out(claims: dict = Depends(token_claims)) -> SignedOut:
    """Ends the session on the server: this token and every renewal of it are refused from now on."""
    tokens.revoke(claims.get("sid"), _lifetime())
    return SignedOut(message="Signed out.")


@router.get("/me")
def me(user: User = Depends(current_user)) -> UserInfo:
    return user_json(user)
