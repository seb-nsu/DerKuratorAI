"""Microsoft Planner and monday.com connectors (E8): the plans or boards of a team tool into a local snapshot, read-only;
the portfolio loads from the snapshot and the mapping layer translates it like any other source. Typical use is a
second source next to ServiceNow or Excel (docs/DATA_MAPPING.md, "Several sources for one table"): the tasks of a
project come from the team's tool, the project itself from the portfolio system.

Sync (`sync()`, or `python scripts/work_sync.py`):
- Planner: Microsoft Graph v1.0 – per configured plan the plan, its buckets and its tasks (/planner/plans/{id}/tasks,
  paged by @odata.nextLink). Graph offers no delta query for Planner in v1.0, so every sync reads the plans
  completely; the change against the snapshot (new, changed by @odata.etag, deleted) is reported per table.
- monday.com: the GraphQL API (https://api.monday.com/v2) – per configured board its items with their column values,
  paged by the items_page cursor; a changed `updated_at` or column value counts as a change.
- 429 and 5xx answers, timeouts and network errors are retried with exponential backoff (Retry-After honoured);
  one sync at a time (file lock); the table files are written only after everything was read, each one atomically.
  A failed sync returns a report with the error and leaves the snapshot as it was.

Each plan or board can name the DerKurator project it belongs to (`project: PRJ-...`); its rows then carry it as
`project_number`, so a mapping can take Project_Tasks from them. People appear only as ids (Planner assignments) or as
the text monday shows in a people column – map such a column only where the portfolio needs it.

Credentials only from the process environment (or .env), never from the mapping file:
- Planner: MS_GRAPH_TOKEN (a bearer token), or MS_GRAPH_TENANT_ID + MS_GRAPH_CLIENT_ID + MS_GRAPH_CLIENT_SECRET for
  the client-credentials flow (application permission Tasks.Read.All, or Group.Read.All on older tenants);
- monday.com: MONDAY_TOKEN (a personal or app API token with boards:read).
A mapping file with a token, password or secret is refused; secrets never reach a log, the snapshot or a report."""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import portalocker

from app.data_adapters.base import DataSourceError
from app.data_adapters.connectors import Connector, _atomic_write

logger = logging.getLogger(__name__)

STATE_FILE = "_sync_state.json"
LOCK_FILE = "_sync.lock"
RETRIES = 5
MAX_BACKOFF_SECONDS = 60.0
RETRY_STATUS = (429, 500, 502, 503, 504)
TIMEOUT_SECONDS = 60.0
SECRET_OPTIONS = ("user", "username", "password", "token", "client_secret", "api_key", "secret")
GRAPH = "https://graph.microsoft.com/v1.0"
GRAPH_LOGIN = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
MONDAY_API = "https://api.monday.com/v2"
MONDAY_API_VERSION = "2024-10"
MONDAY_PAGE = 500
ENV_GRAPH_TOKEN, ENV_GRAPH_TENANT, ENV_GRAPH_CLIENT, ENV_GRAPH_SECRET = (
    "MS_GRAPH_TOKEN", "MS_GRAPH_TENANT_ID", "MS_GRAPH_CLIENT_ID", "MS_GRAPH_CLIENT_SECRET")
ENV_MONDAY_TOKEN = "MONDAY_TOKEN"  # noqa: S105 – the name of the variable, not a secret

# (method, url, headers, body) -> (HTTP status, response headers, body)
Transport = Callable[[str, str, dict[str, str], bytes | None], tuple[int, dict[str, str], bytes]]


def urllib_transport(method: str, url: str, headers: dict[str, str], body: bytes | None,
                     timeout: float = TIMEOUT_SECONDS) -> tuple[int, dict[str, str], bytes]:
    """The HTTP call of the standard library (honours HTTPS_PROXY); HTTP errors are returned, a network error raises
    Unreachable."""
    request = urllib.request.Request(url, data=body, headers=headers, method=method)  # noqa: S310 – fixed https URLs
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read() or b""
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise Unreachable(f"{urllib.parse.urlsplit(url).netloc} is not reachable: "
                          f"{getattr(exc, 'reason', exc)}") from exc


class Unreachable(DataSourceError):
    """A network error or timeout: retried like a 5xx answer."""


class HttpClient:
    """JSON requests with retry on rate limits, server errors and network errors. `requests` counts the calls."""

    def __init__(self, system: str, transport: Transport | None = None, sleep: Callable[[float], None] = time.sleep,
                 retries: int = RETRIES):
        self.system = system
        self.transport = transport or urllib_transport
        self.sleep = sleep
        self.retries = max(0, int(retries))
        self.requests = 0

    def call(self, method: str, url: str, headers: dict[str, str], body: bytes | None = None) -> dict[str, Any]:
        for attempt in range(self.retries + 1):
            self.requests += 1
            try:
                status, answer_headers, payload = self.transport(method, url, headers, body)
            except Unreachable as exc:
                status, answer_headers, payload, failure = 0, {}, b"", str(exc)
            if status == 200:
                try:
                    return json.loads(payload.decode("utf-8") or "{}")
                except (ValueError, UnicodeDecodeError) as exc:
                    raise DataSourceError(f"{self.system} sent no valid JSON ({exc}).") from exc
            if status in (401, 403):
                raise DataSourceError(f"{self.system} refused the credentials (HTTP {status}) – check the token and "
                                      "its read permissions.")
            if (status == 0 or status in RETRY_STATUS) and attempt < self.retries:
                retry_after = next((v for k, v in answer_headers.items() if k.lower() == "retry-after"), "")
                try:
                    wait = float(retry_after)
                except ValueError:
                    wait = min(2.0 ** attempt, MAX_BACKOFF_SECONDS)
                self.sleep(wait)
                continue
            if status == 0:
                raise DataSourceError(f"{failure} after {attempt + 1} attempt(s).")
            detail = payload.decode("utf-8", "replace")[:200] if payload else ""
            raise DataSourceError(f"{self.system} answered HTTP {status} for {url.split('?')[0]}: {detail}")
        raise DataSourceError(f"{self.system} did not answer.")  # pragma: no cover


@dataclass
class TableChange:
    table: str
    fetched: int = 0
    new: int = 0
    updated: int = 0
    deleted: int = 0
    total: int = 0


@dataclass
class WorkSyncReport:
    system: str
    started: str
    finished: str = ""
    requests: int = 0
    tables: list[TableChange] = field(default_factory=list)
    error: str = ""  # set: the sync failed and the snapshot is unchanged

    @property
    def ok(self) -> bool:
        return not self.error

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "ok": self.ok}

    def summary(self) -> str:
        if self.error:
            return f"{self.system} sync failed – {self.error} The snapshot is unchanged ({self.requests} request(s))."
        parts = [f"{t.table}: {t.fetched} read, {t.new} new, {t.updated} changed, {t.deleted} deleted"
                 for t in self.tables]
        return f"{self.system} sync – " + "; ".join(parts) + f" – {self.requests} request(s)."


def _specs(options: dict[str, Any], name: str) -> dict[str, dict[str, Any]]:
    """`plans` / `boards` as a list of ids or as {id: {project: ...}}."""
    raw = options.get(name)
    if isinstance(raw, list):
        raw = {str(i): {} for i in raw}
    if not isinstance(raw, dict) or not raw:
        raise DataSourceError(f"The source needs 'options: {{{name}: ...}}' – the {name} to read.")
    specs = {}
    for key, raw_spec in raw.items():
        spec = dict(raw_spec or {})
        unknown = set(spec) - {"project"}
        if unknown:
            raise DataSourceError(f"Unknown setting(s) for {name[:-1]} {key}: {', '.join(sorted(unknown))}")
        specs[str(key)] = spec
    return specs


class SnapshotConnector(Connector):
    """A read-only source that syncs from an API into one <table>.json per table plus the sync state. Subclasses name
    their tables and fetch the rows (each with an `id`)."""
    system = "abstract"
    tables: tuple[str, ...] = ()
    spec_option = ""
    typed = False  # dates and numbers arrive as text: the mapping layer types them

    def __init__(self, path: str | Path, *, writable: bool = False, options: dict[str, Any] | None = None):
        super().__init__(path, writable=False, options=options)  # never writes back
        secrets = sorted(k for k in self.options if str(k).lower() in SECRET_OPTIONS)
        if secrets:
            raise DataSourceError(f"The mapping file must not contain {', '.join(secrets)} – {self.system} "
                                  "credentials come from the secrets.")
        self.specs = _specs(self.options, self.spec_option)

    def _file(self, table: str) -> Path:
        return self.path / f"{table}.json"

    def state(self) -> dict[str, Any]:
        try:
            return json.loads((self.path / STATE_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def read(self) -> dict[str, pd.DataFrame]:
        if not self.state():
            raise DataSourceError(f"No {self.system} snapshot in {self.path} yet – run the sync first "
                                  "(python scripts/work_sync.py).")
        return {t: pd.DataFrame(json.loads(self._file(t).read_text(encoding="utf-8")) if self._file(t).exists()
                                else []) for t in self.tables}

    def files(self) -> list[Path]:
        return [p for p in [*(self._file(t) for t in self.tables), self.path / STATE_FILE] if p.exists()]

    def metadata(self) -> dict[str, Any]:
        return {"source": self.system, "synced_at": self.state().get("synced_at", "")}

    def label(self, table: str) -> str:
        return f"{self.system} {table}"

    def headers(self, client: HttpClient) -> dict[str, str]:
        raise NotImplementedError

    def fetch(self, client: HttpClient, headers: dict[str, str]) -> dict[str, list[dict[str, Any]]]:
        raise NotImplementedError

    def _old(self, table: str) -> dict[str, dict[str, Any]]:
        try:
            rows = json.loads(self._file(table).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return {str(r["id"]): r for r in rows if isinstance(r, dict) and r.get("id")} if isinstance(rows, list) else {}

    def sync(self, transport: Transport | None = None, sleep: Callable[[float], None] = time.sleep) -> WorkSyncReport:
        """Reads every plan or board and replaces the snapshot. Errors end in a report with `error` set (logged, not
        raised) and the snapshot as it was."""
        client = HttpClient(self.system, transport, sleep)
        report = WorkSyncReport(self.system, datetime.now(UTC).isoformat(timespec="seconds"))
        try:
            self.path.mkdir(parents=True, exist_ok=True)
            with portalocker.Lock(str(self.path / LOCK_FILE), timeout=120):
                fetched = self.fetch(client, self.headers(client))
                for table in self.tables:
                    old, rows = self._old(table), fetched.get(table, [])
                    new = {str(r["id"]): r for r in rows}
                    report.tables.append(TableChange(
                        table, fetched=len(rows), new=sum(1 for k in new if k not in old),
                        updated=sum(1 for k, r in new.items() if k in old and old[k] != r),
                        deleted=sum(1 for k in old if k not in new), total=len(new)))
                for table in self.tables:
                    rows = [r for _, r in sorted({str(r["id"]): r for r in fetched.get(table, [])}.items())]
                    _atomic_write(self._file(table), lambda tmp, rows=rows: tmp.write_text(
                        json.dumps(rows, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8"))
                state = {"system": self.system, "synced_at": datetime.now(UTC).isoformat(timespec="seconds"),
                         "tables": {t.table: t.total for t in report.tables}}
                _atomic_write(self.path / STATE_FILE, lambda tmp: tmp.write_text(json.dumps(state), encoding="utf-8"))
        except portalocker.exceptions.LockException:
            report.error = f"Another {self.system} sync is still running."
        except DataSourceError as exc:
            report.error = str(exc)
        report.finished, report.requests = datetime.now(UTC).isoformat(timespec="seconds"), client.requests
        (logger.error if report.error else logger.info)(report.summary())
        return report


def _date(value: Any) -> str | None:
    return str(value)[:10] if value else None


PLANNER_STATE = {0: "Open", 50: "Work in Progress", 100: "Closed Complete"}


class PlannerConnector(SnapshotConnector):
    """`path` is the snapshot folder. Options: plans (a list of plan ids, or {plan id: {project: PRJ-...}}).
    Tables: planner_plans, planner_buckets, planner_tasks."""
    kind = "planner"
    system = "Microsoft Planner"
    tables = ("planner_plans", "planner_buckets", "planner_tasks")
    spec_option = "plans"

    def headers(self, client: HttpClient) -> dict[str, str]:
        token = (os.getenv(ENV_GRAPH_TOKEN) or "").strip()
        if not token:
            tenant, client_id, secret = (os.getenv(ENV_GRAPH_TENANT) or "").strip(), \
                (os.getenv(ENV_GRAPH_CLIENT) or "").strip(), os.getenv(ENV_GRAPH_SECRET) or ""
            if not (tenant and client_id and secret):
                raise DataSourceError(f"No Microsoft Graph credentials: set {ENV_GRAPH_TOKEN}, or {ENV_GRAPH_TENANT}, "
                                      f"{ENV_GRAPH_CLIENT} and {ENV_GRAPH_SECRET}, as secrets.")
            body = urllib.parse.urlencode({"grant_type": "client_credentials", "client_id": client_id,
                                           "client_secret": secret,
                                           "scope": "https://graph.microsoft.com/.default"}).encode()
            answer = client.call("POST", GRAPH_LOGIN.format(tenant=urllib.parse.quote(tenant)),
                                 {"Content-Type": "application/x-www-form-urlencoded"}, body)
            token = str(answer.get("access_token") or "")
            if not token:
                raise DataSourceError("Microsoft Graph sent no access token.")
        return {"Accept": "application/json", "Authorization": f"Bearer {token}"}

    def _pages(self, client: HttpClient, headers: dict[str, str], url: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        while url:
            data = client.call("GET", url, headers)
            rows.extend(data.get("value") or [])
            url = data.get("@odata.nextLink") or ""
            if url and not url.startswith(GRAPH):
                raise DataSourceError("Microsoft Graph sent a next link to another host – the read stops.")
        return rows

    def fetch(self, client: HttpClient, headers: dict[str, str]) -> dict[str, list[dict[str, Any]]]:
        out: dict[str, list[dict[str, Any]]] = {t: [] for t in self.tables}
        for plan_id, spec in self.specs.items():
            base = f"{GRAPH}/planner/plans/{urllib.parse.quote(plan_id)}"
            plan = client.call("GET", base, headers)
            project = spec.get("project")
            out["planner_plans"].append({"id": plan.get("id") or plan_id, "title": plan.get("title"),
                                         "created_date": _date(plan.get("createdDateTime")),
                                         "project_number": project})
            buckets = {b.get("id"): b.get("name") for b in self._pages(client, headers, f"{base}/buckets")}
            out["planner_buckets"] += [{"id": k, "plan_id": plan_id, "name": v} for k, v in buckets.items() if k]
            for task in self._pages(client, headers, f"{base}/tasks"):
                pct = int(task.get("percentComplete") or 0)
                out["planner_tasks"].append({
                    "id": task.get("id"), "plan_id": plan_id, "plan_title": plan.get("title"),
                    "project_number": project, "bucket": buckets.get(task.get("bucketId")),
                    "title": task.get("title"), "percent_complete": pct,
                    "state": PLANNER_STATE.get(pct, "Work in Progress"), "priority": task.get("priority"),
                    "start_date": _date(task.get("startDateTime")), "due_date": _date(task.get("dueDateTime")),
                    "completed_date": _date(task.get("completedDateTime")),
                    "assignees": len(task.get("assignments") or {}), "etag": task.get("@odata.etag")})
        out["planner_tasks"] = [t for t in out["planner_tasks"] if t["id"]]
        return out


MONDAY_ITEMS = ("items_page(limit: %d) { cursor items { id name updated_at group { title } "
                "column_values { id text } } }")


class MondayConnector(SnapshotConnector):
    """`path` is the snapshot folder. Options: boards (a list of board ids, or {board id: {project: PRJ-...}}).
    Tables: monday_boards, monday_items – every column value of an item as `col_<column id>` (its text)."""
    kind = "monday"
    system = "monday.com"
    tables = ("monday_boards", "monday_items")
    spec_option = "boards"

    def headers(self, client: HttpClient) -> dict[str, str]:
        token = (os.getenv(ENV_MONDAY_TOKEN) or "").strip()
        if not token:
            raise DataSourceError(f"No monday.com credentials: set {ENV_MONDAY_TOKEN} as a secret.")
        return {"Content-Type": "application/json", "Accept": "application/json", "Authorization": token,
                "API-Version": MONDAY_API_VERSION}

    def _query(self, client: HttpClient, headers: dict[str, str], query: str) -> dict[str, Any]:
        data = client.call("POST", MONDAY_API, headers, json.dumps({"query": query}).encode("utf-8"))
        if data.get("errors") or data.get("error_message"):
            message = data.get("error_message") or "; ".join(str(e.get("message")) for e in data["errors"])
            raise DataSourceError(f"monday.com refused the query: {message[:200]}")
        return data.get("data") or {}

    def fetch(self, client: HttpClient, headers: dict[str, str]) -> dict[str, list[dict[str, Any]]]:
        out: dict[str, list[dict[str, Any]]] = {t: [] for t in self.tables}
        for board_id, spec in self.specs.items():
            if not board_id.isdigit():
                raise DataSourceError(f"monday.com board ids are numbers, not '{board_id}'.")
            data = self._query(client, headers, f"query {{ boards(ids: [{board_id}]) {{ id name "
                                                f"{MONDAY_ITEMS % MONDAY_PAGE} }} }}")
            boards = data.get("boards") or []
            if not boards:
                raise DataSourceError(f"monday.com board {board_id} not found (or not shared with the token).")
            board, project = boards[0], spec.get("project")
            out["monday_boards"].append({"id": str(board.get("id")), "name": board.get("name"),
                                         "project_number": project})
            page = board.get("items_page") or {}
            while True:
                for item in page.get("items") or []:
                    row = {"id": str(item.get("id")), "board_id": board_id, "board_name": board.get("name"),
                           "project_number": project, "name": item.get("name"),
                           "group": (item.get("group") or {}).get("title"), "updated_at": item.get("updated_at")}
                    row.update({f"col_{c.get('id')}": c.get("text") for c in item.get("column_values") or []})
                    out["monday_items"].append(row)
                cursor = page.get("cursor")
                if not cursor:
                    break
                next_page = MONDAY_ITEMS.replace("items_page(limit: %d)", "next_items_page(limit: %d, cursor: %s)")
                more = self._query(client, headers, "query { %s }" % (next_page % (MONDAY_PAGE, json.dumps(cursor))))
                page = more.get("next_items_page") or {}
        return out
