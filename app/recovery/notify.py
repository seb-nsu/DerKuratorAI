"""Notifications of the red trigger: one outbox event per trigger (idempotency key), to the PMO role and the
project manager of the red project. Texts follow the recipient's view (row-level security): a project manager's
message names only their own project and never a donor project of another manager."""
from __future__ import annotations

from typing import Any


def notify_recommendation(recommendation: dict[str, Any]) -> None:
    from app.core.notifications import record_red_trigger

    record_red_trigger(recommendation["id"], recommendation.get("key") or recommendation["id"])


def _recipients(rec: dict[str, Any], dataset: Any) -> list[Any]:
    from app.core import notifications as n
    from app.core.security import ROLE_PM, ROLE_PMO

    out = [n.Recipient(n.MAIL, u.email, ROLE_PMO, u.user_id) for u in n._accounts() if u.role == ROLE_PMO and u.email]
    out += [n.Recipient(n.MAIL, address, ROLE_PMO) for address in n.role_mailboxes(n.PMO_CHANNEL)]
    if n.PMO_CHANNEL in n.teams_urls():
        out.append(n.Recipient(n.TEAMS, n.PMO_CHANNEL, ROLE_PMO))
    manager = None
    if dataset is not None and not dataset.projects.empty:
        rows = dataset.projects[dataset.projects["project_number"].astype(str) == str(rec.get("project_id"))]
        manager = str(rows.iloc[0].get("project_manager") or "") if not rows.empty else None
    if manager:
        out += [n.Recipient(n.MAIL, u.email, ROLE_PM, u.user_id) for u in n._accounts()
                if u.role == ROLE_PM and u.email and n._same(u.user_id, manager)]
    unique: dict[tuple[str, str], Any] = {}
    for r in out:
        unique.setdefault((r.channel, r.to.casefold()), r)
    return list(unique.values())


def compose(rec: dict[str, Any], role: str) -> dict[str, str]:
    from app.core import notifications as n
    from app.core.security import ROLE_PMO

    pid, name = rec.get("project_id"), rec.get("project_name") or rec.get("project_id")
    options = [o for o in rec.get("options") or [] if o.get("key") != "R"]
    lines = [f"{name} ({pid}) turned red ({rec.get('trigger_label') or 'red trigger'}).", ""]
    lines += [f"Cause: {c['label']}" for c in rec.get("causes") or []][:n.MAX_LINES]
    if role == ROLE_PMO:
        lines.append(f"Options calculated: {len(options)}")
        best = next((o for o in options if o["key"] == rec.get("recommended")), None)
        if best:
            verdict = ((best.get("result") or {}).get("verdict") or {}).get("label") or "–"
            lines.append(f"Recommended: option {best['key']} – {best['label']} ({verdict})")
    else:  # the project manager: no option details, they may name projects outside the scope
        lines.append("The PMO has a recommendation with calculated options for your project.")
    lines.append("Nothing is changed until a person submits a proposal and a second PMO approves it.")
    link = f"{n.base_url()}/recovery?id={rec['id']}" if n.base_url() else ""
    return {"subject": f"DerKurator AI: Project turned red – {name}", "text": "\n".join(lines), "link": link}


def messages_for(event: dict[str, Any], dataset: Any) -> list[tuple[Any, dict[str, str]]]:
    from app.recovery import store

    rec = store.get_recommendation(str(event.get("recommendation_id")))
    if rec is None or rec.get("status") != store.OPEN:
        return []
    return [(r, compose(rec, r.role)) for r in _recipients(rec, dataset)]
