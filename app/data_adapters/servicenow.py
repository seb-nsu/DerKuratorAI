"""ServiceNow connector: the SPM tables through the Table API into a local snapshot; the portfolio loads from the
snapshot (fast, no network on a page load) and the mapping layer translates it like any other source.

Sync (`ServiceNowConnector.sync`, the Admin Console button or `python scripts/servicenow_sync.py`):
- per table one paged read of /api/now/table/<table> with keyset paging (ordered by sys_id, every page asks for the
  records after the last sys_id read) until an empty page or X-Total-Count says nothing is left: a record that
  changes or disappears during the read never shifts an unchanged one out of it, and an instance that caps
  `sysparm_limit` below `page_size` only means more pages; the number read is compared with X-Total-Count and a
  difference is reported;
- with `sysparm_display_value=all` every field arrives as value and display value; the snapshot keeps the value under
  the field name and, where it differs, the display value as `<field>_display` (the number of a referenced project,
  the name of a user or group, the label of a state);
- the first run reads everything; every later run reads only the records changed since the last one (delta on
  sys_updated_on, with an overlap window so that a time-zone difference between the API and the query never loses a
  record – records are merged by sys_id, so reading one twice changes nothing); a table whose snapshot file is
  missing or unreadable is read completely;
- records deleted in ServiceNow are removed from the snapshot by an id-only read (`prune_deleted`, default on), on a
  delta sync only once the reconcile interval has passed (SERVICENOW_RECONCILE_HOURS or `reconcile_hours`, default
  24; 0 = every sync) and only after the id list was read completely;
- 429 and 5xx answers, timeouts and network errors are retried with exponential backoff (Retry-After honoured), with
  a request timeout (`timeout_seconds`) and a time limit for the whole sync (`max_minutes`);
- one sync at a time (file lock); the table files are written only after every table was read, each one atomically,
  the sync state last. A failed sync returns a report with the error and leaves the snapshot as it was.

Credentials are read only from the process environment, never from the mapping file: SERVICENOW_INSTANCE
(https://<name>.service-now.com; not a secret, so the mapping option `instance` may hold it, the variable wins) and
either SERVICENOW_TOKEN (OAuth bearer token, used when set) or SERVICENOW_USER and SERVICENOW_PASSWORD (basic auth).
They belong in the host's secrets or in `.env` (loaded at start by app/core/config.py). A mapping file with a user,
password, token, client secret or API key is refused. Secrets are never logged, written to the snapshot or the sync
report, nor shown in an error message (`Credentials` masks them in its repr). The connector only reads: approved
changes go to the mapping's own store (or an export); writing them back to ServiceNow is a separate, switched-on
step (app/data_adapters/servicenow_writeback.py)."""
from __future__ import annotations

import base64
import functools
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import portalocker

from app.data_adapters.base import DataSourceError
from app.data_adapters.connectors import Connector, _atomic_write

logger = logging.getLogger(__name__)

API_PATH = "/api/now/table/"
STATE_FILE = "_sync_state.json"
LOCK_FILE = "_sync.lock"
DEFAULT_PAGE_SIZE = 1000
DEFAULT_OVERLAP_MINUTES = 24 * 60  # covers any time-zone offset between the stored value and the query
DEFAULT_RECONCILE_HOURS = 24.0
DEFAULT_TIMEOUT_SECONDS = 60.0  # per request
DEFAULT_MAX_MINUTES = 30.0  # the whole sync, retries and their waits included
RETRIES = 5  # waits 1, 2, 4, 8, 16 s unless the answer sends Retry-After
MAX_BACKOFF_SECONDS = 60.0
RETRY_STATUS = (429, 500, 502, 503, 504)
ENV_INSTANCE, ENV_USER, ENV_PASSWORD, ENV_TOKEN = ("SERVICENOW_INSTANCE", "SERVICENOW_USER", "SERVICENOW_PASSWORD",
                                                   "SERVICENOW_TOKEN")
ENV_RECONCILE_HOURS = "SERVICENOW_RECONCILE_HOURS"
SECRET_OPTIONS = ("user", "username", "password", "token", "client_secret", "api_key")
SN_TIME = "%Y-%m-%d %H:%M:%S"

# (url, headers) -> (HTTP status, response headers, body)
Transport = Callable[[str, dict[str, str]], tuple[int, dict[str, str], bytes]]


def urllib_transport(url: str, headers: dict[str, str],
                     timeout: float = DEFAULT_TIMEOUT_SECONDS) -> tuple[int, dict[str, str], bytes]:
    """The HTTP call of the standard library (honours HTTPS_PROXY); HTTP errors are returned, not raised, a network
    error or timeout raises ServiceNowUnreachable."""
    request = urllib.request.Request(url, headers=headers, method="GET")  # noqa: S310 – https URL from the settings
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read() or b""
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ServiceNowUnreachable(f"ServiceNow is not reachable: {getattr(exc, 'reason', exc)}") from exc


class ServiceNowUnreachable(DataSourceError):
    """A network error or timeout: retried like a 5xx answer."""


def _header(headers: dict[str, str], name: str) -> str | None:
    return next((str(v) for k, v in headers.items() if k.lower() == name.lower()), None)


def _id(row: dict[str, Any]) -> str:
    cell = row.get("sys_id")
    return str((cell.get("value") if isinstance(cell, dict) else cell) or "")


@dataclass(frozen=True)
class Credentials:
    instance: str
    user: str = ""
    password: str = ""
    token: str = ""

    @classmethod
    def from_env(cls, instance: str | None = None) -> Credentials:
        url = (os.getenv(ENV_INSTANCE) or instance or "").strip().rstrip("/")
        if not url:
            raise DataSourceError(f"No ServiceNow instance: set {ENV_INSTANCE} (https://<name>.service-now.com).")
        if not url.startswith("https://"):
            url = "https://" + url.removeprefix("http://")
        creds = cls(url, (os.getenv(ENV_USER) or "").strip(), os.getenv(ENV_PASSWORD) or "",
                    (os.getenv(ENV_TOKEN) or "").strip())
        if not creds.token and not (creds.user and creds.password):
            raise DataSourceError(f"No ServiceNow credentials: set {ENV_USER} and {ENV_PASSWORD}, or {ENV_TOKEN}, as "
                                  "secrets – never in the mapping file.")
        return creds

    def headers(self) -> dict[str, str]:
        auth = f"Bearer {self.token}" if self.token else \
            "Basic " + base64.b64encode(f"{self.user}:{self.password}".encode()).decode()
        return {"Accept": "application/json", "Authorization": auth}

    def __repr__(self) -> str:  # never print a secret
        return f"Credentials(instance={self.instance!r}, user={self.user!r}, secret=***)"


class ServiceNowClient:
    """Keyset-paged reads of the Table API with retry on rate limits, server errors and timeouts, within a time limit
    for the whole sync (`max_seconds`, counted from the client's creation)."""

    def __init__(self, credentials: Credentials, transport: Transport | None = None, page_size: int = DEFAULT_PAGE_SIZE,
                 sleep: Callable[[float], None] = time.sleep, *, retries: int = RETRIES,
                 max_seconds: float = DEFAULT_MAX_MINUTES * 60, clock: Callable[[], float] = time.monotonic):
        self.credentials = credentials
        self.transport = transport or urllib_transport
        self.page_size = max(1, int(page_size))
        self.sleep = sleep
        self.retries = max(0, int(retries))
        self.max_seconds = float(max_seconds)
        self.clock = clock
        self.deadline = clock() + self.max_seconds
        self.requests = 0
        self.total: int | None = None  # X-Total-Count of the last read (None: the instance sent none)
        self.read = 0  # records of the last read

    def _out_of_time(self) -> DataSourceError:
        return DataSourceError(f"The ServiceNow sync reached its time limit of {self.max_seconds / 60:g} min "
                               f"(option max_minutes) after {self.requests} request(s).")

    def _wait(self, headers: dict[str, str], attempt: int) -> float:
        try:
            wait = float(_header(headers, "Retry-After") or "")
        except ValueError:  # missing or an HTTP date: exponential backoff
            wait = -1.0
        return wait if wait >= 0 else min(2.0 ** attempt, MAX_BACKOFF_SECONDS)

    def _get(self, url: str) -> tuple[dict[str, Any], dict[str, str]]:
        for attempt in range(self.retries + 1):
            if self.clock() >= self.deadline:
                raise self._out_of_time()
            self.requests += 1
            try:
                status, headers, body = self.transport(url, self.credentials.headers())
            except ServiceNowUnreachable as exc:
                status, headers, body, failure = 0, {}, b"", str(exc)
            if status == 200:
                try:
                    return json.loads(body.decode("utf-8") or "{}"), headers
                except (ValueError, UnicodeDecodeError) as exc:
                    raise DataSourceError(f"ServiceNow sent no valid JSON ({exc}).") from exc
            if status in (401, 403):
                raise DataSourceError(f"ServiceNow refused the credentials (HTTP {status}) – check the user's roles "
                                      "(read access to the SPM tables) and the secrets.")
            retryable = status == 0 or status in RETRY_STATUS
            if retryable and attempt < self.retries:
                wait = self._wait(headers, attempt)
                if self.clock() + wait >= self.deadline:
                    raise self._out_of_time()
                self.sleep(wait)
                continue
            tries = f" after {attempt + 1} attempt(s)" if retryable else ""
            if status == 0:
                raise DataSourceError(f"{failure}{tries}.")
            detail = body.decode("utf-8", "replace")[:200] if body else ""
            raise DataSourceError(f"ServiceNow answered HTTP {status} for {url.split('?')[0]}{tries}: {detail}")
        raise DataSourceError("ServiceNow did not answer.")  # pragma: no cover – the loop always returns or raises

    def pages(self, table: str, query: str = "", fields: list[str] | None = None,
              display: bool = True) -> Iterator[list[dict[str, Any]]]:
        """The records of `query` ordered by sys_id, page by page: each page asks for the records after the last
        sys_id read (sys_id>last) until an empty page, or until X-Total-Count (the records left) says nothing is left.
        Afterwards `total` holds the X-Total-Count of the first page and `read` the number of records read."""
        self.total, self.read, last = None, 0, ""
        while True:
            parts = [query] if query else []
            if last:
                parts.append(f"sys_id>{last}")
            params = {"sysparm_query": "^".join([*parts, "ORDERBYsys_id"]), "sysparm_limit": str(self.page_size),
                      "sysparm_exclude_reference_link": "true",
                      "sysparm_display_value": "all" if display else "false"}
            if fields:
                params["sysparm_fields"] = ",".join(fields)
            url = f"{self.credentials.instance}{API_PATH}{urllib.parse.quote(table)}?{urllib.parse.urlencode(params)}"
            data, headers = self._get(url)
            rows = data.get("result") or []
            if not isinstance(rows, list):
                raise DataSourceError(f"ServiceNow table {table}: 'result' is not a list.")
            try:
                left = int(_header(headers, "X-Total-Count") or "")
            except ValueError:
                left = None
            if not last:
                self.total = left
            if not rows:
                return
            self.read += len(rows)
            yield rows
            if left is not None and len(rows) >= left:
                return
            newest = max(_id(r) for r in rows)
            if not newest or newest <= last:  # no sys_id, or the instance ignored the order: paging would not end
                raise DataSourceError(f"ServiceNow table {table}: the pages are not ordered by sys_id – the read "
                                      "cannot continue safely.")
            last = newest


def flatten(record: dict[str, Any]) -> dict[str, Any]:
    """One record of `sysparm_display_value=all`: the value under the field name, a differing display value as
    `<field>_display`."""
    row: dict[str, Any] = {}
    for name, cell in record.items():
        if isinstance(cell, dict) and ("value" in cell or "display_value" in cell):
            value, display = cell.get("value"), cell.get("display_value")
            row[name] = value
            if display not in (None, "") and display != value:
                row[f"{name}_display"] = display
        else:
            row[name] = cell
    return row


@dataclass
class TableSync:
    table: str
    full: bool
    fetched: int = 0
    new: int = 0
    updated: int = 0
    deleted: int = 0
    total: int = 0
    watermark: str | None = None
    expected: int | None = None  # X-Total-Count of the read (None: the instance sent none)
    reconciled: bool = False  # deleted records checked by the id-only read (or a full read)
    warnings: list[str] = field(default_factory=list)


@dataclass
class SyncReport:
    instance: str
    started: str
    finished: str = ""
    requests: int = 0
    tables: list[TableSync] = field(default_factory=list)
    error: str = ""  # set: the sync failed and the snapshot is unchanged

    @property
    def ok(self) -> bool:
        return not self.error

    @property
    def warnings(self) -> list[str]:
        return [f"{t.table}: {w}" for t in self.tables for w in t.warnings]

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "ok": self.ok, "warnings": self.warnings}

    def summary(self) -> str:
        if self.error:
            return (f"ServiceNow sync {self.instance} failed – {self.error} The snapshot is unchanged "
                    f"({self.requests} request(s)).")
        parts = [f"{t.table}: {t.fetched} read, {t.new} new, {t.updated} changed, {t.deleted} deleted"
                 + (" (full)" if t.full else "") for t in self.tables]
        warnings = "".join(f" Warning – {w}" for w in self.warnings)
        return (f"ServiceNow sync {self.instance} – " + "; ".join(parts) + f" – {self.requests} request(s)."
                + warnings)


def _number(options: dict[str, Any], name: str, default: float, env: str | None = None) -> float:
    raw = (os.getenv(env) if env else None) or options.get(name)
    try:
        value = float(default if raw in (None, "") else raw)
    except (TypeError, ValueError):
        value = -1.0
    if value < 0:
        raise DataSourceError(f"ServiceNow setting {env or name} must be a number of at least 0, not {raw!r}.")
    return value


def _due(reconciled_at: str | None, interval: timedelta, now: datetime) -> bool:
    try:
        return now - datetime.fromisoformat(str(reconciled_at)) >= interval
    except (TypeError, ValueError):  # never reconciled (or an unreadable stamp)
        return True


def _table_specs(options: dict[str, Any]) -> dict[str, dict[str, Any]]:
    tables = options.get("tables")
    if isinstance(tables, list):
        tables = {str(t): {} for t in tables}
    if not isinstance(tables, dict) or not tables:
        raise DataSourceError("The ServiceNow source needs 'options: {tables: ...}' – the tables to read.")
    specs = {}
    for name, raw in tables.items():
        spec = dict(raw or {})
        unknown = set(spec) - {"query", "fields", "prune_deleted"}
        if unknown:
            raise DataSourceError(f"Unknown setting(s) for ServiceNow table {name}: {', '.join(sorted(unknown))}")
        specs[str(name)] = spec
    return specs


class ServiceNowConnector(Connector):
    """`path` is the snapshot folder (one <table>.json per ServiceNow table plus the sync state). Options: instance,
    tables ({name: {query, fields, prune_deleted}}), page_size, overlap_minutes, reconcile_hours (the variable
    SERVICENOW_RECONCILE_HOURS wins), timeout_seconds (per request), max_minutes (the whole sync)."""
    kind = "servicenow"
    typed = False  # the Table API sends every value as text: the mapping layer types numbers

    def __init__(self, path: str | Path, *, writable: bool = False, options: dict[str, Any] | None = None):
        super().__init__(path, writable=False, options=options)  # never writes back to ServiceNow
        secrets = sorted(k for k in self.options if str(k).lower() in SECRET_OPTIONS)
        if secrets:
            raise DataSourceError(f"The mapping file must not contain {', '.join(secrets)} – ServiceNow credentials "
                                  f"come from the secrets {ENV_USER}/{ENV_PASSWORD} or {ENV_TOKEN}.")
        self.specs = _table_specs(self.options)

    # ------------------------------------------------------------------ snapshot
    def _file(self, table: str) -> Path:
        return self.path / f"{table}.json"

    def state(self) -> dict[str, Any]:
        file = self.path / STATE_FILE
        if not file.exists():
            return {}
        try:
            return json.loads(file.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return {}

    def read(self) -> dict[str, pd.DataFrame]:
        if not self.state():
            raise DataSourceError(f"No ServiceNow snapshot in {self.path} yet – run the sync first (Admin Console › "
                                  "System status, or python scripts/servicenow_sync.py).")
        tables = {}
        for table in self.specs:
            file = self._file(table)
            rows = json.loads(file.read_text(encoding="utf-8")) if file.exists() else []
            tables[table] = pd.DataFrame(rows)
        return tables

    def files(self) -> list[Path]:
        return [p for p in [*(self._file(t) for t in self.specs), self.path / STATE_FILE] if p.exists()]

    def metadata(self) -> dict[str, Any]:
        state = self.state()
        return {"source": "ServiceNow", "instance": state.get("instance", ""), "synced_at": state.get("synced_at", "")}

    def label(self, table: str) -> str:
        return f"ServiceNow {table}"

    def _write_json(self, path: Path, data: Any) -> None:
        def dump(tmp: Path) -> None:
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")

        _atomic_write(path, dump)

    # ------------------------------------------------------------------ sync
    def client(self, transport: Transport | None = None) -> ServiceNowClient:
        timeout = _number(self.options, "timeout_seconds", DEFAULT_TIMEOUT_SECONDS) or DEFAULT_TIMEOUT_SECONDS
        minutes = _number(self.options, "max_minutes", DEFAULT_MAX_MINUTES) or DEFAULT_MAX_MINUTES
        return ServiceNowClient(Credentials.from_env(self.options.get("instance")),
                                transport or functools.partial(urllib_transport, timeout=timeout),
                                int(self.options.get("page_size") or DEFAULT_PAGE_SIZE), max_seconds=minutes * 60)

    def sync(self, client: ServiceNowClient | None = None, full: bool = False) -> SyncReport:
        """Reads the changes since the last sync into the snapshot (everything on the first run, with full, or for a
        table without a readable snapshot file). Nothing is written before every table was read: an error ends in a
        report with `error` set (logged, not raised) and the snapshot as it was."""
        client = client or self.client()
        self.path.mkdir(parents=True, exist_ok=True)
        overlap = timedelta(minutes=float(self.options.get("overlap_minutes", DEFAULT_OVERLAP_MINUTES)))
        reconcile = timedelta(hours=_number(self.options, "reconcile_hours", DEFAULT_RECONCILE_HOURS,
                                            ENV_RECONCILE_HOURS))
        report = SyncReport(client.credentials.instance, datetime.now(UTC).isoformat(timespec="seconds"))
        try:
            with portalocker.Lock(str(self.path / LOCK_FILE), timeout=120):
                state = self.state()
                if state.get("instance") and state["instance"] != client.credentials.instance:
                    full = True  # another instance: never mix two systems in one snapshot
                known = state.get("tables", {})
                now = datetime.now(UTC)
                staged = {}
                for table, spec in self.specs.items():
                    result, rows, reconciled_at = self._sync_table(client, table, spec, known.get(table, {}), full,
                                                                   overlap, reconcile, now)
                    report.tables.append(result)
                    staged[table] = rows, reconciled_at
                for table, (rows, _) in staged.items():
                    self._write_json(self._file(table), rows)
                state = {"instance": client.credentials.instance,
                         "synced_at": datetime.now(UTC).isoformat(timespec="microseconds"),
                         "tables": {**known, **{t.table: {"watermark": t.watermark, "rows": t.total,
                                                          "reconciled_at": staged[t.table][1]}
                                                for t in report.tables}}}
                self._write_json(self.path / STATE_FILE, state)
        except portalocker.exceptions.LockException:
            report.error = "Another ServiceNow sync is still running."
        except DataSourceError as exc:
            report.error = str(exc)
        report.finished, report.requests = datetime.now(UTC).isoformat(timespec="seconds"), client.requests
        if report.error:
            logger.error(report.summary())
        else:
            (logger.warning if report.warnings else logger.info)(report.summary())
        return report

    def _snapshot(self, file: Path) -> dict[str, dict[str, Any]] | None:
        """The snapshot rows of one table by sys_id; None without a readable file."""
        try:
            rows = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return {r["sys_id"]: r for r in rows if isinstance(r, dict) and r.get("sys_id")} if isinstance(rows, list) \
            else None

    def _sync_table(self, client: ServiceNowClient, table: str, spec: dict[str, Any], known: dict[str, Any],
                    full: bool, overlap: timedelta, reconcile: timedelta,
                    now: datetime) -> tuple[TableSync, list[dict[str, Any]], str | None]:
        """One table: the result, the new snapshot rows and the time of the last check for deleted records."""
        watermark = None if full else known.get("watermark")
        since = None
        if watermark:
            try:
                since = datetime.strptime(watermark, SN_TIME).replace(tzinfo=UTC) - overlap
            except ValueError:  # an unreadable watermark: read the table completely
                watermark = None
        existing = self._snapshot(self._file(table)) if watermark else None
        if existing is None:  # first run, full sync, or the snapshot file is missing: read everything
            watermark, since, existing = None, None, {}
        result = TableSync(table, full=watermark is None)
        base = str(spec.get("query") or "").strip()
        parts = [base] if base else []
        if since:
            parts.append(f"sys_updated_on>=javascript:gs.dateGenerate('{since:%Y-%m-%d}','{since:%H:%M:%S}')")
        fields = spec.get("fields")
        fields = sorted(set(fields) | {"sys_id", "sys_updated_on"}) if fields else None
        newest = watermark
        for page in client.pages(table, "^".join(parts), fields):
            for record in page:
                row = flatten(record)
                sys_id = row.get("sys_id")
                if not sys_id:
                    continue
                result.fetched += 1
                if sys_id in existing:
                    result.updated += existing[sys_id] != row
                else:
                    result.new += 1
                existing[sys_id] = row
                stamp = str(row.get("sys_updated_on") or "")
                if stamp and (newest is None or stamp > newest):
                    newest = stamp
        result.expected = client.total
        if client.total is not None and client.read != client.total:
            result.warnings.append(f"{client.read} record(s) read, X-Total-Count reported {client.total} (records "
                                   "changed during the read, or the instance stopped early).")
        reconciled_at = known.get("reconciled_at")
        if result.full:  # a complete read replaces the snapshot: nothing deleted can stay in it
            reconciled_at, result.reconciled = now.isoformat(timespec="seconds"), True
        elif spec.get("prune_deleted", True) and _due(reconciled_at, reconcile, now):
            alive = {_id(r) for page in client.pages(table, base, ["sys_id"], display=False) for r in page}
            if client.total is not None and client.read < client.total:
                result.warnings.append(f"id list incomplete ({client.read} of {client.total}): no record deleted, "
                                       "checked again on the next sync.")
            else:
                gone = [s for s in existing if s not in alive]
                for sys_id in gone:
                    del existing[sys_id]
                result.deleted = len(gone)
                reconciled_at, result.reconciled = now.isoformat(timespec="seconds"), True
        rows = [existing[k] for k in sorted(existing)]
        result.total, result.watermark = len(rows), newest
        return result, rows, reconciled_at
