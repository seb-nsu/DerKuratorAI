"""The page frame every front end draws: what the sign-in page may show, and per signed-in user the pages of their
role, the scope line and the open-inbox count – the same rules for every front end."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.api.deps import clear_data_cache, current_user, environment, full_dataset
from app.core import auth, clock
from app.core.auth import User
from app.core.proposals import PLAN, SUBMITTED, list_proposals
from app.core.security import ROLE_ADMIN, ROLE_BOARD, ROLE_PM, ROLE_PMO, apply_rls_to_dataset, can_approve
from app.views.navigation import pages_for

router = APIRouter(tags=["shell"])


class PageLink(BaseModel):
    id: str
    title: str
    icon: str  # Material Symbols name
    parent: str | None = None  # a sub-page: opened from this page, not listed in the navigation


class DemoAccount(BaseModel):
    user_id: str
    display_name: str
    role: str
    role_label: str


class DemoInfo(BaseModel):
    password: str
    accounts: list[DemoAccount]


class SignInInfo(BaseModel):
    environment: str
    configured: bool
    demo: DemoInfo | None


class WorkspaceUser(BaseModel):
    user_id: str
    display_name: str
    role: str
    role_label: str
    pmo_head: bool = False  # changes the business rules and required fields on the Rules page


class ScopeLine(BaseModel):
    visible: int
    total: int | None  # None for a Project Manager: the size of the whole portfolio is not theirs to know


class Workspace(BaseModel):
    environment: str
    production: bool
    user: WorkspaceUser
    pages: list[PageLink]
    loaded_at: str
    scope: ScopeLine | None
    pending_approvals: int


class Reloaded(BaseModel):
    reloaded: bool


logger = logging.getLogger("derkurator.api.shell")

ROLE_LABELS = {ROLE_ADMIN: "System Admin", ROLE_PMO: "PMO", ROLE_PM: "Project Manager", ROLE_BOARD: "Portfolio Board"}
@router.get("/sign-in-info")
def sign_in_info() -> SignInInfo:
    """Environment badge, whether sign-in works at all, and the demo accounts (DEV/TEST on demo data only)."""
    env = environment()
    try:
        users = auth.load_users()
    except auth.AuthConfigError as exc:
        logger.error("Sign-in not configured: %s", exc)  # details for the System Admin only
        return SignInInfo(environment=env, configured=False, demo=None)
    password = auth.demo_password()
    demo = None
    if password and auth.demo_allowed(env):
        demo = {"password": password, "accounts": [
            {"user_id": u.user_id, "display_name": u.display_name, "role": u.role,
             "role_label": ROLE_LABELS.get(u.role, u.role)} for u in users.values() if u.demo]}
    return SignInInfo(environment=env, configured=True, demo=demo)


@router.get("/workspace")
def workspace(user: User = Depends(current_user)) -> Workspace:
    """Pages of the role, the scope line ("n of m projects visible") and the open PMO inbox count."""
    info = {"environment": environment(), "production": environment() == "PROD",
            "user": {"user_id": user.user_id, "display_name": user.display_name or user.user_id, "role": user.role,
                     "role_label": ROLE_LABELS.get(user.role, user.role), "pmo_head": user.pmo_head},
            "pages": pages_for(user.role), "loaded_at": clock.now().strftime("%H:%M")}
    if user.role == ROLE_ADMIN:
        return Workspace(**info, scope=None, pending_approvals=0)
    full = full_dataset()
    visible = apply_rls_to_dataset(full, user.role, user.user_id)
    pending = len([p for p in list_proposals(status=SUBMITTED) if p.get("kind") != PLAN]) \
        if can_approve(user.role) else 0
    return Workspace(**info, scope=ScopeLine(visible=len(visible.projects),
                                                     total=None if user.role == ROLE_PM else len(full.projects)),
                     pending_approvals=pending)


@router.post("/data/reload")
def reload_data(user: User = Depends(current_user)) -> Reloaded:
    """The header's reload button: the next request reads the data source again."""
    logger.info("API | Data cache cleared | user=%s", user.user_id)
    clear_data_cache()
    return Reloaded(reloaded=True)
