"""Request dependencies: the signed-in user (re-read from the user file on every request), role gates and the
portfolio data in the caller's scope."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.api import tokens
from app.core.auth import AuthConfigError, User, load_users
from app.core.runtime_config import get_runtime_config
from app.core.security import AVAILABLE_ROLES, ROLE_PM, ROLE_PMO, apply_rls_to_dataset, can_approve
from app.data_adapters import PortfolioDataset, get_data_provider
from app.graph.portfolio_graph import current_source_fingerprint

_bearer = HTTPBearer(auto_error=False)
DATA_TTL_S = 60.0  # the same window as the Streamlit pages' data cache
_data_lock = threading.Lock()
_data_cache: dict = {}


def environment() -> str:
    return get_runtime_config().environment


def unauthorized(message: str) -> HTTPException:
    return HTTPException(status.HTTP_401_UNAUTHORIZED, message, headers={"WWW-Authenticate": "Bearer"})


def token_claims(credentials: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> dict:
    """The claims of the caller's valid token (user id, session id, sign-in time)."""
    if credentials is None:
        raise unauthorized("Sign-in required.")
    try:
        return tokens.claims(credentials.credentials, environment())
    except tokens.TokenError as exc:
        raise unauthorized(str(exc)) from exc
    except tokens.TokenConfigError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Sign-in is not configured.") from exc


def current_user(claims: dict = Depends(token_claims)) -> User:
    try:
        user = load_users().get(claims["sub"].casefold())
    except AuthConfigError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Sign-in is not configured.") from exc
    if user is None:  # account removed since the token was issued
        raise unauthorized("Sign-in required.")
    return user


def data_user(user: User = Depends(current_user)) -> User:
    """Roles with portfolio data access; the System Admin sees no project data by design."""
    if user.role not in AVAILABLE_ROLES:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Your role has no access to portfolio data.")
    return user


def requester(user: User = Depends(data_user)) -> User:
    """Roles that may run what-ifs and submit proposals (PMO and Project Manager)."""
    if user.role not in (ROLE_PMO, ROLE_PM):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Your role cannot run what-ifs.")
    return user


def pmo(user: User = Depends(data_user)) -> User:
    """Any PMO account: reads the governance rules (business rules, required fields)."""
    if user.role != ROLE_PMO:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Reserved for the PMO.")
    return user


def pmo_head(user: User = Depends(pmo)) -> User:
    """The PMO Head only: changes the governance rules."""
    if not user.pmo_head:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Reserved for the PMO Head.")
    return user


def approver(user: User = Depends(data_user)) -> User:
    if not can_approve(user.role):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the PMO decides on proposals.")
    return user


def full_dataset() -> PortfolioDataset:
    """The whole portfolio (for PII registration and server-side checks only – never returned as is). Cached for
    DATA_TTL_S; a write of another worker (or replica) shows at once, because a changed source fingerprint (size and
    mtime, then content) ends the cache early."""
    now = time.monotonic()
    with _data_lock:
        cached, previous = _data_cache.get("ds"), _data_cache.get("fingerprint")
        fresh = _data_cache.get("at") is not None and now - _data_cache["at"] < DATA_TTL_S
    fingerprint = current_source_fingerprint(previous)  # taken BEFORE loading: a later edit is detected
    if fresh and cached is not None and (fingerprint or {}).get("sha256") == (previous or {}).get("sha256"):
        return cached
    ds = get_data_provider().load_dataset()
    with _data_lock:
        _data_cache.update(at=now, ds=ds, fingerprint=fingerprint)
    return ds


def clear_data_cache() -> None:
    with _data_lock:
        _data_cache.clear()


@dataclass(frozen=True)
class Scope:
    user: User
    full: PortfolioDataset
    visible: PortfolioDataset


def scope(user: User = Depends(data_user)) -> Scope:
    full = full_dataset()
    return Scope(user, full, apply_rls_to_dataset(full, user.role, user.user_id))


def records(df) -> list[dict]:
    """A table as JSON-safe rows (dates as ISO text, missing values as null)."""
    import json

    if df is None or df.empty:
        return []
    return json.loads(df.to_json(orient="records", date_format="iso"))


def project_view(visible: PortfolioDataset):
    """The project table of the workspace pages: enriched metrics plus the data-quality status per project.
    Returns (view, steward report)."""
    from app.crew.tools.data_steward_tools import run_all_checks

    view = visible.project_view()
    steward = run_all_checks(visible)
    view["data_quality"] = view["project_number"].map(steward.project_status()).fillna("OK")
    return view, steward


def admin(user: User = Depends(current_user)) -> User:
    """The System Admin only: settings and logs, no project data."""
    from app.core.security import ROLE_ADMIN

    if user.role != ROLE_ADMIN:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Reserved for the System Admin.")
    return user
