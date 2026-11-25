"""Recovery page (red trigger): open recommendations with trigger, causes, gap, impact, the options side by side,
the rejected measures and the actions Adopt / Dismiss / Recalculate; the alert band of the overview; the
recommendation tab of the project detail. One set of functions behind the HTTP API (app/api/routes/recovery.py).

Row-level security on read: the scan runs over the whole portfolio, but a Project Manager sees only the
recommendations of their own projects, and every project outside their scope is replaced by "another project in the
portfolio" – a donor never learns from the recommendation that it might be stopped (risk R6 of the concept).

Who may adopt (decision of 24.11.2026): the PMO, and the Project Manager of the red project for an option that only
changes their own project. Approval stays with a second PMO person through the proposal flow."""
from __future__ import annotations

import json
import re
from datetime import date
from typing import Any

from app.core import clock
from app.core.security import ROLE_PM, ROLE_PMO, apply_rls_to_dataset
from app.recovery import store

PAGE_TITLE = "Recovery"
OTHER_PROJECT = "another project in the portfolio"
HIDDEN_KEYS = ("tool_texts",)  # verbatim graph texts are PMO detail


class RecoveryAccessError(PermissionError):
    pass


def visible_ids(full: Any, role: str, user_id: str) -> set[str] | None:
    """None = every project (PMO); else the Project Manager's own projects."""
    if role == ROLE_PMO:
        return None
    visible = apply_rls_to_dataset(full, role, user_id)
    return set(visible.projects["project_number"].astype(str)) if not visible.projects.empty else set()


def _hide(rec: dict[str, Any], allowed: set[str], full: Any) -> dict[str, Any]:
    """Replaces every project id and name outside `allowed` by OTHER_PROJECT (deep, in the JSON text)."""
    names = dict(zip(full.projects["project_number"].astype(str), full.projects["project_name"].astype(str),
                     strict=True))
    hidden = {pid: name for pid, name in names.items() if pid not in allowed}
    clean = {k: v for k, v in rec.items() if k not in ("history",)}
    impact = dict(clean.get("impact") or {})
    for key in HIDDEN_KEYS:
        impact.pop(key, None)
    clean["impact"] = impact
    text = json.dumps(clean, default=str)
    for pid, name in sorted(hidden.items(), key=lambda kv: -len(kv[0])):
        text = text.replace(pid, OTHER_PROJECT)
        if name and len(name) > 3:
            text = re.sub(re.escape(name), OTHER_PROJECT, text)
    return json.loads(text)


def public(rec: dict[str, Any], allowed: set[str] | None, full: Any) -> dict[str, Any]:
    out = dict(rec) if allowed is None else _hide(rec, allowed, full)
    out["options"] = [{**o, "adoptable_by_pm": _only_own(o, rec["project_id"])} for o in out.get("options") or []]
    out.pop("history", None)
    return out


def _only_own(option: dict[str, Any], project_id: str) -> bool:
    steps = option.get("steps") or []
    return bool(steps) and all(s.get("project") == project_id and not s.get("target") for s in steps)


def summary(rec: dict[str, Any]) -> dict[str, Any]:
    """One row of the list / alert band."""
    options = [o for o in rec.get("options") or [] if o.get("key") != "R"]
    gap = max((c.get("gap_eur") or 0 for c in rec.get("causes") or [] if c.get("kind") == "cost"), default=0.0)
    days = max((c.get("gap_days") or 0 for c in rec.get("causes") or [] if c.get("kind") == "schedule"), default=0)
    return {"id": rec["id"], "project_id": rec["project_id"], "project_name": rec.get("project_name"),
            "status": rec["status"], "trigger": rec.get("trigger"), "trigger_label": rec.get("trigger_label"),
            "reported": rec.get("reported"), "calculated": rec.get("calculated"), "as_of": rec.get("as_of"),
            "created_at": rec.get("created_at"), "causes": [c.get("label") for c in rec.get("causes") or []],
            "gap_eur": gap, "gap_days": days, "options": len(options), "recommended": rec.get("recommended"),
            "headline": headline(rec)}


def headline(rec: dict[str, Any]) -> str:
    n = len([o for o in rec.get("options") or [] if o.get("key") != "R"])
    how = "calculated" if rec.get("calculated") == "Red" and rec.get("reported") != "Red" else "reported"
    if rec.get("trigger") == "regap":
        how = "gap grew"
    return f"🔴 {rec.get('project_name')} turned red ({how}) – {n} option{'' if n == 1 else 's'}"


def list_recommendations(full: Any, role: str, user_id: str, status: str | None = None) -> list[dict[str, Any]]:
    allowed = visible_ids(full, role, user_id)
    items = store.list_recommendations(status=status)
    if allowed is not None:
        items = [i for i in items if str(i.get("project_id")) in allowed]
    return [summary(public(i, allowed, full) | {"status": i["status"], "id": i["id"]}) for i in items]


def get(rec_id: str, full: Any, role: str, user_id: str) -> dict[str, Any]:
    rec = store.get_recommendation(rec_id)
    allowed = visible_ids(full, role, user_id)
    if rec is None or (allowed is not None and str(rec.get("project_id")) not in allowed):
        raise KeyError(rec_id)
    return public(rec, allowed, full)


def for_project(project_id: str, full: Any, role: str, user_id: str) -> list[dict[str, Any]]:
    """The recommendations of one project, newest first (project detail, tab Recommendations)."""
    allowed = visible_ids(full, role, user_id)
    if allowed is not None and str(project_id) not in allowed:
        raise KeyError(project_id)
    return [summary(public(i, allowed, full) | {"status": i["status"], "id": i["id"]})
            for i in store.list_recommendations(project_id=str(project_id))]


def alerts(full: Any, role: str, user_id: str) -> list[dict[str, Any]]:
    """The open recommendations for the alert band of the overview (a PM: their own projects)."""
    return list_recommendations(full, role, user_id, status=store.OPEN)


def analyse(project_id: str, user_id: str, role: str, today: date | None = None, full: Any = None) -> dict[str, Any]:
    """'Analyse now' / 'Recalculate' (PMO only)."""
    from app.approval_flow import audit
    from app.recovery.engine import analyse_now

    if role != ROLE_PMO:
        raise RecoveryAccessError("Only the PMO starts a recovery analysis.")
    rec = analyse_now(project_id, by=user_id, dataset=full, today=today or clock.today())
    audit("recovery_analysed", role=role, user_id=user_id, recommendation_id=rec["id"], project_id=project_id)
    return rec


def check_adopt(rec: dict[str, Any], option_key: str, role: str, user_id: str, full: Any) -> dict[str, Any]:
    option = next((o for o in rec.get("options") or [] if o.get("key") == option_key), None)
    if option is None or not option.get("steps"):
        raise KeyError(option_key)
    if rec.get("status") in store.CLOSED:
        raise store.RecoveryStoreError(f"The recommendation is {rec['status']} – recalculate it first.")
    if role == ROLE_PMO:
        return option
    allowed = visible_ids(full, role, user_id) or set()
    if role == ROLE_PM and str(rec.get("project_id")) in allowed and _only_own(option, rec["project_id"]):
        return option
    raise RecoveryAccessError("A Project Manager adopts only an option that changes their own project alone – the "
                              "PMO adopts the others.")


def dismiss(rec_id: str, reason: str, user_id: str, role: str) -> dict[str, Any]:
    from app.approval_flow import audit

    if role != ROLE_PMO:
        raise RecoveryAccessError("Only the PMO dismisses a recommendation.")
    rec = store.dismiss(rec_id, user_id, role, reason)
    audit("recovery_dismissed", role=role, user_id=user_id, recommendation_id=rec_id, reason=reason)
    return rec


def steerco_rows(full: Any) -> list[dict[str, Any]]:
    """For the SteerCo pack: the open and adopted recommendations from the data (never from the LLM text)."""
    rows = []
    for rec in store.list_recommendations():
        if rec["status"] not in (store.OPEN, store.ADOPTED):
            continue
        best = next((o for o in rec.get("options") or [] if o.get("key") == (rec.get("adopted_option")
                                                                          or rec.get("recommended"))), None)
        rows.append({**summary(rec), "option": best.get("label") if best else None,
                     "option_key": best.get("key") if best else None,
                     "verdict": ((best or {}).get("result") or {}).get("verdict", {}).get("label")
                     if best and (best.get("result") or {}).get("verdict") else None})
    return rows
