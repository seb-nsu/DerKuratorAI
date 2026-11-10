"""Notifications by mail (SMTP) or Microsoft Teams (incoming webhook / Power Automate workflow) about the approval
inbox: a new proposal waits for a decision (the PMO; an annual plan the Portfolio Board), a proposal is overdue
(escalation) or expired, and the decision goes back to the requester (approved, rejected, sent back, outdated).

Off by default (`NOTIFY_ENABLED`). Never blocks or breaks an approval: the proposal store only records the event in the
outbox (`notifications.jsonl` in the write directory, append-only under a cross-process lock, like the other logs);
recipients, texts and the delivery follow after the write – in a background thread of the process that recorded it,
and by `scripts/send_notifications.py` on a schedule. Records:
    {"type": "event",    "key": "submitted:<proposal>", "event": ..., "proposal_id": ..., "level": ...}
    {"type": "expanded", "key": ..., "messages": n}                  recipients resolved and messages queued
    {"type": "queued",   "id": ..., "key": ..., "dedup": "<key>|<channel>|<to>", "channel": "mail|teams", "to": ...,
     "user": <account or null>, "subject": ..., "text": ..., "link": ...}
    {"type": "sent",     "id": ..., "attempt": n}
    {"type": "failed",   "id": ..., "attempt": n, "error": ..., "next_at": <retry time, null = given up>}
One message per event and recipient (the dedup key); a failed delivery is retried with exponential backoff up to
`NOTIFY_MAX_ATTEMPTS`. Texts are short and carry no secrets: project names, the change, the budget amount, the verdict
or decision and a link into the app (`APP_BASE_URL`) – a Project Manager's message only names projects in their own
scope (row-level security), the role channels (PMO, Board) see what those roles see in the app.

Recipients: the `email` of an account in the user file (AUTH_USERS_FILE) and the role mailboxes NOTIFY_PMO_TO /
NOTIFY_BOARD_TO (comma-separated); Teams per role (NOTIFY_TEAMS_PMO_URL, NOTIFY_TEAMS_BOARD_URL – secrets).
"""
from __future__ import annotations

import json
import logging
import os
import re
import smtplib
import ssl
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Iterator

import portalocker

from app.core import clock, logs

logger = logging.getLogger("derkurator.notifications")

SUBMITTED, ESCALATED, EXPIRED, DECIDED, TEST = "submitted", "escalated", "expired", "decided", "test"
MAIL, TEAMS = "mail", "teams"
PMO_CHANNEL, BOARD_CHANNEL = "PMO", "Portfolio Board"  # the `to` of a Teams message: the role, never the URL
RETRY_BASE_S, RETRY_MAX_S = 60, 6 * 3600
TIMEOUT_S = 15
MAX_LINES = 5  # projects and changes listed per message
_EMAIL_RE = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")


class NotificationError(RuntimeError):
    pass


class InvalidAddress(NotificationError):
    pass


# =========================================================================== settings
def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or "").strip() or default


def enabled() -> bool:
    return _env("NOTIFY_ENABLED").lower() in {"1", "true", "yes", "y", "on"}


@dataclass(frozen=True)
class Smtp:
    host: str
    port: int
    user: str
    password: str
    sender: str
    starttls: bool

    @property
    def configured(self) -> bool:
        return bool(self.host and self.sender)


def smtp_settings() -> Smtp:
    try:
        port = int(_env("NOTIFY_SMTP_PORT", "587"))
    except ValueError:
        port = 587
    return Smtp(_env("NOTIFY_SMTP_HOST"), port, _env("NOTIFY_SMTP_USER"), os.getenv("NOTIFY_SMTP_PASSWORD") or "",
                _env("NOTIFY_SMTP_FROM"), _env("NOTIFY_SMTP_STARTTLS", "true").lower() not in {"0", "false", "no", "off"})


def teams_urls() -> dict[str, str]:
    """Role channel -> webhook URL (only the configured ones). The URLs are secrets: never logged or shown."""
    urls = {PMO_CHANNEL: _env("NOTIFY_TEAMS_PMO_URL"), BOARD_CHANNEL: _env("NOTIFY_TEAMS_BOARD_URL")}
    return {role: url for role, url in urls.items() if url}


def role_mailboxes(channel: str) -> list[str]:
    raw = _env("NOTIFY_PMO_TO" if channel == PMO_CHANNEL else "NOTIFY_BOARD_TO")
    return [a.strip() for a in re.split(r"[,;]", raw) if _EMAIL_RE.match(a.strip())]


def max_attempts() -> int:
    try:
        return max(1, int(_env("NOTIFY_MAX_ATTEMPTS", "6")))
    except ValueError:
        return 6


def base_url() -> str:
    return _env("APP_BASE_URL").rstrip("/")


def channels_configured() -> bool:
    return smtp_settings().configured or bool(teams_urls())


def _scrub(text: str) -> str:
    """An error text without the secrets (SMTP password, webhook URLs) and on one short line."""
    text = str(text)
    for secret in (os.getenv("NOTIFY_SMTP_PASSWORD") or "", *teams_urls().values()):
        if secret:
            text = text.replace(secret, "***")
    text = re.sub(r"https?://\S+", "<url>", text)  # an HTTP error names the URL it called
    return re.sub(r"\s+", " ", text).strip()[:200]


# =========================================================================== outbox
def outbox_path() -> Path:
    return Path(logs.NOTIFY_OUTBOX)  # resolved at call time (tests redirect it)


@contextmanager
def _locked() -> Iterator[Path]:
    path = outbox_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with portalocker.Lock(str(path.with_name(path.name + ".lock")), mode="a", timeout=10, check_interval=0.05):
            yield path
    except portalocker.exceptions.LockException as exc:
        raise NotificationError("The notification outbox is busy.") from exc


def _records(path: Path) -> list[dict[str, Any]]:
    try:
        records, _ = logs.read_jsonl(path)
    except FileNotFoundError:
        return []
    return records


def _append(path: Path, *records: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps({"timestamp": clock.stamp(), "environment": logs.current_environment(), **record},
                                ensure_ascii=False, default=str) + "\n")


def _messages(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Message id -> merged state: status queued | sent | failed (given up), attempts, next_at, last error."""
    items: dict[str, dict[str, Any]] = {}
    for r in records:
        mid = str(r.get("id") or "")
        if r.get("type") == "queued" and mid:
            items[mid] = {**r, "status": "queued", "attempts": 0, "next_at": None}
        elif r.get("type") == "sent" and mid in items:
            items[mid].update(status="sent", attempts=r.get("attempt"), sent_at=r.get("timestamp"))
        elif r.get("type") == "failed" and mid in items and items[mid]["status"] != "sent":
            items[mid].update(status="queued" if r.get("next_at") else "failed", attempts=r.get("attempt"),
                              next_at=r.get("next_at"), error=r.get("error"), failed_at=r.get("timestamp"))
    return items


# =========================================================================== events (called by the proposal store)
def record_event(event: str, proposal_id: str, level: int | None = None, status: str | None = None) -> None:
    """Records one inbox event for delivery and starts the delivery in the background. Never raises: a notification
    must never block or break the decision that caused it."""
    if not enabled() or not channels_configured():
        return
    key = ":".join(str(part) for part in (event, proposal_id, level or status) if part)
    try:
        with _locked() as path:
            if any(r.get("type") == "event" and r.get("key") == key for r in _records(path)):
                return  # one notification per event
            _append(path, {"type": "event", "key": key, "event": event, "proposal_id": proposal_id,
                           **({"level": level} if level else {}), **({"status": status} if status else {})})
    except Exception:  # the proposal log already holds the event; the decision stands
        logger.exception("Notification event %s could not be recorded", key)
        return
    try:
        kick()
    except Exception:  # no thread now: the next event or the scheduled script delivers it
        logger.exception("Notification delivery could not be started")


_background = {"on": True}


def set_background(on: bool) -> None:
    """Scripts deliver in their own thread (a background thread would end with the script, maybe mid-send)."""
    _background["on"] = on


def kick() -> None:
    """Delivers what is due in a background thread (one at a time per outbox, see deliver)."""
    if not _background["on"]:
        return
    threading.Thread(target=_deliver_quietly, name="notifications", daemon=True).start()


def _deliver_quietly() -> None:
    try:
        deliver()
    except Exception:  # retried on the next event or by scripts/send_notifications.py
        logger.exception("Notification delivery failed")


# =========================================================================== recipients and texts
@dataclass(frozen=True)
class Recipient:
    channel: str  # mail | teams
    to: str  # mail address, or the role channel of a Teams webhook
    role: str  # the role whose view the text follows
    user: str | None = None  # the account (personal mail), None for a role mailbox or channel


def _accounts() -> list[Any]:
    from app.core.auth import AuthConfigError, load_users

    try:
        return list(load_users().values())
    except AuthConfigError:
        logger.warning("User file unreadable – notifications go to the role mailboxes only")
        return []


def _same(a: Any, b: Any) -> bool:
    return str(a or "").strip().casefold() == str(b or "").strip().casefold()


def _approvers(proposal: dict[str, Any]) -> list[Recipient]:
    """Who decides it: the Portfolio Board for an annual plan, else the PMO – never the requester themselves."""
    from app.core.proposals import PLAN
    from app.core.security import ROLE_BOARD, ROLE_PMO

    plan = proposal.get("kind") == PLAN
    role, channel = (ROLE_BOARD, BOARD_CHANNEL) if plan else (ROLE_PMO, PMO_CHANNEL)
    out = [Recipient(MAIL, u.email, role, u.user_id) for u in _accounts()
           if u.role == role and u.email and not _same(u.user_id, proposal.get("requester"))]
    out += [Recipient(MAIL, address, role) for address in role_mailboxes(channel)]
    if channel in teams_urls():
        out.append(Recipient(TEAMS, channel, role))
    return out


def _requester(proposal: dict[str, Any]) -> list[Recipient]:
    account = next((u for u in _accounts() if _same(u.user_id, proposal.get("requester"))), None)
    return [Recipient(MAIL, account.email, account.role, account.user_id)] if account and account.email else []


def recipients(event: str, proposal: dict[str, Any]) -> list[Recipient]:
    """Each recipient once (by channel and address) – an account whose address is also the role mailbox gets one mail."""
    if event in (SUBMITTED, ESCALATED):
        found = _approvers(proposal)
    elif event == EXPIRED:
        found = _requester(proposal) + _approvers(proposal)
    else:
        found = _requester(proposal)
    unique: dict[tuple[str, str], Recipient] = {}
    for r in found:
        unique.setdefault((r.channel, r.to.casefold()), r)
    return list(unique.values())


def _visible_projects(dataset: Any, role: str, user: str | None) -> tuple[set[str] | None, dict[str, str]]:
    """(project ids the recipient may see – None means all, names of those projects). Deny by default: without the
    data a Project Manager's message names no project."""
    from app.core.security import ROLE_BOARD, ROLE_PMO, apply_row_level_security

    if dataset is None or dataset.projects is None or dataset.projects.empty:
        return (None if role in (ROLE_PMO, ROLE_BOARD) else set()), {}
    projects = dataset.projects if role in (ROLE_PMO, ROLE_BOARD) else \
        apply_row_level_security(dataset.projects, role, user)
    names = dict(zip(projects["project_number"].astype(str), projects["project_name"].astype(str), strict=True))
    return (None if role in (ROLE_PMO, ROLE_BOARD) else set(names)), names


DECISION_WORDS = {"approved": "approved", "rejected": "rejected", "stale": "outdated – nothing was written"}


def compose(event: str, proposal: dict[str, Any], recipient: Recipient, dataset: Any = None,
            level: int | None = None) -> dict[str, str]:
    """Subject, text and link of one message, in the recipient's view: projects and changes outside their scope are
    left out (a Project Manager never learns the ids of other projects)."""
    from app.core.formatting import day, eur, field_label, format_value
    from app.core.proposals import PLAN, sla_state

    pid, plan = str(proposal.get("id")), proposal.get("kind") == PLAN
    visible, names = _visible_projects(dataset, recipient.role, recipient.user)
    projects = [p for p in proposal.get("projects") or [] if p and p != "None" and (visible is None or p in visible)]
    changes = [c for c in proposal.get("changes") or [] if visible is None or str(c.get("project_id")) in visible]
    title = "Annual plan" if plan else ", ".join(names.get(p, p) for p in projects[:2]) or f"Proposal {pid}"
    if len(projects) > 2 and not plan:
        title += f" and {len(projects) - 2} more"
    status = str(proposal.get("status") or "")
    word = "sent back" if plan and status == "rejected" else DECISION_WORDS.get(status, status)
    subject, headline = {
        SUBMITTED: ("Waiting for your decision", "A new proposal waits for your decision."),
        ESCALATED: (f"Overdue (escalation level {level or 1})",
                    f"This proposal is past its approval deadline{' twice' if (level or 1) > 1 else ''}."),
        EXPIRED: ("Expired without a decision", "This proposal expired without a decision; nothing was written."),
        DECIDED: (f"Your proposal was {word.split(' –')[0]}", f"Your proposal was {word}."),
    }[event]
    lines = [headline, "", f"Proposal: {pid}"]
    lines += [f"Project: {names.get(p, p)} ({p})" for p in projects[:MAX_LINES]]
    for c in changes[:MAX_LINES]:
        name = c.get("field") or c.get("table") or ""
        lines.append(f"Change: {field_label(name)}" if c.get("op") else
                     f"Change: {field_label(name)} {format_value(name, c.get('old'))} → {format_value(name, c.get('new'))}")
    if len(changes) > MAX_LINES:
        lines.append(f"… and {len(changes) - MAX_LINES} more change(s)")
    delta = 0.0
    for c in changes:
        if c.get("field") == "budget_cost_eur":
            try:
                delta += float(c.get("new") or 0) - float(c.get("old") or 0)
            except (TypeError, ValueError):
                continue
    if delta:
        lines.append(f"Budget change: {eur(delta, signed=True)}")
    lines.append(f"Verdict: {proposal.get('verdict') or 'no verdict'}")
    if event in (SUBMITTED, ESCALATED, EXPIRED) and recipient.user != proposal.get("requester"):
        lines.append(f"Requested by: {proposal.get('requester') or '–'}")
    if event in (SUBMITTED, ESCALATED):
        due = sla_state(proposal).get("due")
        if due is not None:
            lines.append(f"Due: {day(due)}")
    if event == DECIDED:
        lines.append(f"Decided by: {proposal.get('decided_by') or '–'}")
        if proposal.get("comment"):
            lines.append(f"Comment: {str(proposal['comment'])[:300]}")
    # the same paths in every front end; the annual plan is decided on the Planning page
    link = (f"{base_url()}/planning" if plan else f"{base_url()}/approvals?proposal={pid}") if base_url() else ""
    return {"subject": f"DerKurator AI: {subject} – {title}", "text": "\n".join(lines), "link": link}


def _expand(path: Path, records: list[dict[str, Any]]) -> int:
    """Turns recorded events into one queued message per recipient (dedup key); returns the number queued. An event
    about a proposal that no longer waits (decided before the delivery ran) sends no "waiting" message."""
    from app.core.proposals import OPEN, get_proposal

    done = {r.get("key") for r in records if r.get("type") == "expanded"}
    pending = [r for r in records if r.get("type") == "event" and r.get("key") not in done]
    if not pending:
        return 0
    known = {r.get("dedup") for r in records if r.get("type") == "queued"}
    dataset, loaded, queued = None, False, 0
    for event in pending:
        proposal = get_proposal(str(event.get("proposal_id")))
        out: list[dict[str, Any]] = []
        if proposal is not None and not (event["event"] in (SUBMITTED, ESCALATED) and proposal["status"] not in OPEN):
            if not loaded:
                dataset, loaded = _dataset(), True
            for r in recipients(event["event"], proposal):
                dedup = f"{event['key']}|{r.channel}|{r.to.casefold()}"
                if dedup in known:
                    continue
                known.add(dedup)
                out.append({"type": "queued", "id": uuid.uuid4().hex[:12], "key": event["key"], "dedup": dedup,
                            "channel": r.channel, "to": r.to, "user": r.user,
                            **compose(event["event"], proposal, r, dataset, event.get("level"))})
        with _locked():
            _append(path, *out, {"type": "expanded", "key": event["key"], "messages": len(out)})
        queued += len(out)
    return queued


def _dataset() -> Any:
    from app.data_adapters import get_data_provider

    try:
        return get_data_provider().load_dataset()
    except Exception:  # noqa: BLE001 - without data the texts name no project (deny by default)
        logger.warning("Portfolio data unreadable – notification texts without project names")
        return None


# =========================================================================== delivery
def _send_mail(message: dict[str, Any]) -> None:
    cfg = smtp_settings()
    if not cfg.configured:
        raise NotificationError("SMTP is not configured.")
    mail = EmailMessage()
    mail["From"], mail["To"], mail["Subject"] = cfg.sender, message["to"], message["subject"]
    mail.set_content(message["text"] + (f"\n\nOpen in DerKurator AI: {message['link']}" if message.get("link") else ""))
    cls = smtplib.SMTP_SSL if cfg.port == 465 else smtplib.SMTP
    with cls(cfg.host, cfg.port, timeout=TIMEOUT_S) as smtp:
        if cfg.starttls and cls is smtplib.SMTP:
            smtp.starttls(context=ssl.create_default_context())
        if cfg.user:
            smtp.login(cfg.user, cfg.password)
        smtp.send_message(mail)


def _post_json(url: str, payload: dict[str, Any]) -> int:
    """POSTs to the webhook and returns the HTTP status (the fake transport of the tests replaces this)."""
    import httpx

    return httpx.post(url, json=payload, timeout=TIMEOUT_S).status_code


def teams_card(message: dict[str, Any]) -> dict[str, Any]:
    """An Adaptive Card as Teams incoming webhooks and Power Automate workflows accept it."""
    body = [{"type": "TextBlock", "text": message["subject"], "weight": "Bolder", "wrap": True}]
    body += [{"type": "TextBlock", "text": line, "wrap": True, "spacing": "None"}
             for line in message["text"].splitlines() if line.strip()]
    card = {"type": "AdaptiveCard", "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "version": "1.4", "body": body}
    if message.get("link"):
        card["actions"] = [{"type": "Action.OpenUrl", "title": "Open in DerKurator AI", "url": message["link"]}]
    return {"type": "message",
            "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive", "content": card}]}


def _send_teams(message: dict[str, Any]) -> None:
    url = teams_urls().get(message["to"])
    if not url:
        raise NotificationError(f"No Teams webhook configured for {message['to']}.")
    code = _post_json(url, teams_card(message))
    if not 200 <= code < 300:
        raise NotificationError(f"Teams webhook answered HTTP {code}.")


def _retry_at(attempt: int, now: datetime) -> str | None:
    """The time of the next attempt (1, 2, 4, … minutes, at most RETRY_MAX_S), None once the attempts are used up."""
    if attempt >= max_attempts():
        return None
    return (now + timedelta(seconds=min(RETRY_MAX_S, RETRY_BASE_S * 2 ** (attempt - 1)))).isoformat(timespec="seconds")


def deliver(now: datetime | None = None, only: set[str] | None = None) -> dict[str, int]:
    """Queues the messages of recorded events, then sends every message that is due (new, or its retry time has
    come). One deliverer at a time across processes (a second one returns at once, so nothing is sent twice); the
    outbox lock is held only while records are read or written, never during a send. `only`: these message ids
    (the admin's test), whether notifications are on or not. Returns counts."""
    result = {"queued": 0, "sent": 0, "failed": 0}
    if only is None and not enabled():
        return result
    path = outbox_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        lock = portalocker.Lock(str(path.with_name(path.name + ".deliver.lock")), mode="a", timeout=0,
                                fail_when_locked=True)
        lock.acquire()
    except portalocker.exceptions.LockException:
        return result  # another process or thread delivers right now
    try:
        if only is None:
            with _locked():
                records = _records(path)
            result["queued"] = _expand(path, records)
        with _locked():
            items = _messages(_records(path))
        now = now or clock.now()
        for mid, item in items.items():
            if only is not None and mid not in only:
                continue
            if item["status"] != "queued" or (item.get("next_at") and clock.parse(item["next_at"]) > now):
                continue
            attempt = int(item.get("attempts") or 0) + 1
            try:
                (_send_teams if item.get("channel") == TEAMS else _send_mail)(item)
            except Exception as exc:  # noqa: BLE001 - every failure is recorded and retried
                error = _scrub(f"{exc.__class__.__name__}: {exc}")
                retry = None if str(item.get("key")).startswith(f"{TEST}:") else _retry_at(attempt, now)
                logger.warning("Notification %s (%s) failed, attempt %d%s: %s", mid, item.get("channel"), attempt,
                               "" if retry else " – given up", error)
                with _locked():
                    _append(path, {"type": "failed", "id": mid, "attempt": attempt, "error": error, "next_at": retry})
                result["failed"] += 1
                continue
            with _locked():
                _append(path, {"type": "sent", "id": mid, "attempt": attempt})
            result["sent"] += 1
    finally:
        lock.release()
    return result


# =========================================================================== admin: status and test
def status() -> dict[str, Any]:
    """What the Admin Console shows: on/off, the configured channels (never a secret, never a recipient address) and
    the outbox in counts – the System Admin sees no project data."""
    cfg = smtp_settings()
    try:
        records = _records(outbox_path())
    except OSError:
        records = []
    items = list(_messages(records).values())
    done = {r.get("key") for r in records if r.get("type") == "expanded"}
    failures = [i for i in items if i.get("error")]
    last = max(failures, key=lambda i: str(i.get("failed_at") or ""), default=None)
    sent = [str(i.get("sent_at")) for i in items if i["status"] == "sent" and i.get("sent_at")]
    teams = teams_urls()
    return {
        "enabled": enabled(),
        "smtp": {"configured": cfg.configured, "host": cfg.host or None, "port": cfg.port,
                 "sender": cfg.sender or None, "starttls": cfg.starttls, "login": bool(cfg.user),
                 "password_set": bool(cfg.password)},
        "teams_pmo": PMO_CHANNEL in teams, "teams_board": BOARD_CHANNEL in teams,
        "pmo_mailboxes": len(role_mailboxes(PMO_CHANNEL)), "board_mailboxes": len(role_mailboxes(BOARD_CHANNEL)),
        "accounts_with_email": sum(1 for u in _accounts() if u.email),
        "app_base_url": base_url() or None,
        "events_waiting": sum(1 for r in records if r.get("type") == "event" and r.get("key") not in done),
        "queued": sum(1 for i in items if i["status"] == "queued"),
        "retrying": sum(1 for i in items if i["status"] == "queued" and i.get("error")),
        "sent": sum(1 for i in items if i["status"] == "sent"),
        "failed": sum(1 for i in items if i["status"] == "failed"),
        "last_sent_at": max(sent, default=None),
        "last_error": None if last is None else {"at": last.get("failed_at"), "channel": last.get("channel"),
                                                 "error": last.get("error")},
    }


def send_test(to: str = "") -> dict[str, Any]:
    """Sends one test message per configured channel right away: mail to `to` (or the role mailboxes), every Teams
    webhook. Returns {"channels": [...], "sent": n, "failed": n, "errors": [...]}. Raises NotificationError when
    there is nothing to send to."""
    to = (to or "").strip()
    if to and not _EMAIL_RE.match(to):
        raise InvalidAddress("That is not a mail address.")
    targets: list[tuple[str, str]] = []
    if smtp_settings().configured:
        addresses = [to] if to else role_mailboxes(PMO_CHANNEL) + role_mailboxes(BOARD_CHANNEL)
        targets += [(MAIL, a) for a in dict.fromkeys(a.casefold() for a in addresses)]
    targets += [(TEAMS, role) for role in teams_urls()]
    if not targets:
        raise NotificationError("No channel to test: configure SMTP with a recipient (a mail address here or "
                                "NOTIFY_PMO_TO / NOTIFY_BOARD_TO) or a Teams webhook.")
    key = f"{TEST}:{uuid.uuid4().hex[:8]}"
    text = ("This is a test notification from DerKurator AI. Proposals waiting for a decision, deadline escalations "
            "and decisions will arrive like this" + ("." if enabled() else " once NOTIFY_ENABLED is on."))
    queued = [{"type": "queued", "id": uuid.uuid4().hex[:12], "key": key, "dedup": f"{key}|{channel}|{target}",
               "channel": channel, "to": target, "user": None, "subject": "DerKurator AI: test notification",
               "text": text, "link": base_url()} for channel, target in targets]
    with _locked() as path:
        _append(path, *queued)
    result = deliver(only={q["id"] for q in queued})
    if result["sent"] + result["failed"] < len(queued):
        raise NotificationError("Another delivery is running – the test messages are queued and follow with it.")
    items = _messages(_records(outbox_path()))
    errors = [f"{q['channel']}: {items[q['id']].get('error')}" for q in queued if items[q["id"]].get("error")]
    return {"channels": sorted({c for c, _ in targets}), "sent": result["sent"], "failed": result["failed"],
            "errors": errors}


send_test.__test__ = False  # not a pytest test despite its name
