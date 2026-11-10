"""Write-back to ServiceNow (E5): the changes of an approved proposal through the Table API (PATCH) into the records
they came from. Off unless both the mapping's ServiceNow source says `write_back: true` and the host sets
SERVICENOW_WRITE_BACK=true; the approval itself never depends on it – the change is in DerKurator's store either way.

Per change: the record is found in the snapshot by its number (sys_id), its current value is read live from
ServiceNow and compared with the value the proposal started from – a different value is a conflict and nothing is
sent (someone changed it in ServiceNow meanwhile); otherwise one PATCH with the source value (reverse value
translation and unit of the mapping). A field mapped from a display value (`*_display`, a reference) or through a
mapping that is not one-to-one is not written and is listed for manual transfer.

Every attempt is a line in `_write_back.jsonl` in the snapshot folder (sent, conflict, failed, manual) – never a
secret. `transfer_status` compares the sent values with the snapshot after the next sync: confirmed, waiting for
the next sync, or differs. A change already sent is never sent twice."""
from __future__ import annotations

import json
import os
import re
import urllib.parse
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.data_adapters.base import DataSourceError
from app.data_adapters.mapping import Mapping, TableMapping
from app.data_adapters.schema import SCHEMA
from app.data_adapters.servicenow import API_PATH, Credentials, ServiceNowConnector
from app.data_adapters.work_management import HttpClient, Transport

LOG_FILE = "_write_back.jsonl"
ENV_WRITE_BACK = "SERVICENOW_WRITE_BACK"
SENT, CONFLICT, FAILED, MANUAL = "sent", "conflict", "failed", "manual"
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}")


def enabled(connector: ServiceNowConnector) -> bool:
    return bool(connector.options.get("write_back")) and os.getenv(ENV_WRITE_BACK, "").strip().lower() == "true"


def _same(a: Any, b: Any) -> bool:
    if a in (None, "") and b in (None, ""):
        return True
    try:
        return abs(float(a) - float(b)) < 0.005
    except (TypeError, ValueError):
        pass
    left, right = str(a or "").strip(), str(b or "").strip()
    if ISO_DATE.match(left) and ISO_DATE.match(right):  # a date against a date-time of the same day
        return left[:10] == right[:10]
    return left == right


def _source_value(tm: TableMapping, field: str, value: Any) -> Any:
    value = tm.to_source_value(field, value)
    return value.isoformat() if hasattr(value, "isoformat") else value


def _log(connector: ServiceNowConnector) -> Path:
    return connector.path / LOG_FILE


def read_log(connector: ServiceNowConnector) -> list[dict[str, Any]]:
    try:
        lines = _log(connector).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _append(connector: ServiceNowConnector, record: dict[str, Any]) -> None:
    connector.path.mkdir(parents=True, exist_ok=True)
    with _log(connector).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def _snapshot_rows(connector: ServiceNowConnector, table: str) -> list[dict[str, Any]]:
    try:
        return json.loads((connector.path / f"{table}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def plan_changes(changes: list[dict[str, Any]], mapping: Mapping, connector_name: str) -> list[dict[str, Any]]:
    """Each change with its ServiceNow table, record (sys_id), column and value – or the reason it cannot be
    written automatically."""
    connector = mapping.connectors[connector_name]
    planned = []
    for change in changes:
        table, field = str(change.get("table") or "Projects"), str(change.get("field"))
        key_value = str(change.get("key") or change.get("project_id")) if table != "Projects" else \
            str(change.get("project_id"))
        base = {"table": table, "key": key_value, "field": field, "old": change.get("old"), "new": change.get("new")}
        tm = next((p for p in mapping.parts(table) if p.source == connector_name), None)
        if tm is None or table not in SCHEMA or not SCHEMA[table].key:
            planned.append({**base, "status": MANUAL, "reason": "the table does not come from ServiceNow"})
            continue
        column = tm.source_column(field)
        if column.endswith("_display") or not tm.invertible or field not in tm.columns.values():
            planned.append({**base, "status": MANUAL, "reason": f"{field} is not a plain ServiceNow field "
                                                                "(reference, translation or not mapped)"})
            continue
        key_column = tm.source_column(SCHEMA[table].key)
        record = next((r for r in _snapshot_rows(connector, tm.source_table)
                       if str(r.get(key_column) or "").strip() == key_value), None)
        if record is None or not record.get("sys_id"):
            planned.append({**base, "status": MANUAL, "reason": f"{key_value} not in the ServiceNow snapshot"})
            continue
        planned.append({**base, "sn_table": tm.source_table, "sys_id": record["sys_id"], "column": column,
                        "expected": _source_value(tm, field, change.get("old")),
                        "value": _source_value(tm, field, change.get("new"))})
    return planned


def write_back(proposal_id: str, changes: list[dict[str, Any]], mapping: Mapping, user: str,
               transport: Transport | None = None, sleep=None) -> list[dict[str, Any]]:
    """Writes the changes of one approved proposal to ServiceNow; returns one result per change (also logged).
    Raises DataSourceError when write-back is off or the mapping has no ServiceNow source."""
    name = next((n for n, c in mapping.connectors.items() if isinstance(c, ServiceNowConnector)), None)
    if name is None:
        raise DataSourceError("The configured mapping has no ServiceNow source.")
    connector = mapping.connectors[name]
    if not enabled(connector):
        raise DataSourceError(f"Write-back to ServiceNow is off: set write_back: true on the ServiceNow source and "
                              f"{ENV_WRITE_BACK}=true on the host.")
    credentials = Credentials.from_env(connector.options.get("instance"))
    client = HttpClient("ServiceNow", transport, **({"sleep": sleep} if sleep else {}))
    headers = {**credentials.headers(), "Content-Type": "application/json"}
    done = {(r.get("proposal_id"), r.get("sys_id"), r.get("column")) for r in read_log(connector)
            if r.get("status") == SENT}
    results = []
    for item in plan_changes(changes, mapping, name):
        record = {"at": datetime.now(UTC).isoformat(timespec="seconds"), "proposal_id": proposal_id, "user": user,
                  "synced_before": connector.state().get("synced_at"), **item}
        if item.get("status") == MANUAL:
            results.append(record)
            _append(connector, record)
            continue
        if (proposal_id, item["sys_id"], item["column"]) in done:
            results.append({**record, "status": "already sent"})
            continue
        url = (f"{credentials.instance}{API_PATH}{urllib.parse.quote(item['sn_table'])}/"
               f"{urllib.parse.quote(item['sys_id'])}")
        try:
            live = client.call("GET", f"{url}?sysparm_fields={urllib.parse.quote(item['column'])}", headers)
            current = (live.get("result") or {}).get(item["column"])
            if not _same(current, item["expected"]):
                record.update(status=CONFLICT, reason=f"ServiceNow holds {current!r}, the proposal started from "
                                                      f"{item['expected']!r} – changed meanwhile, nothing sent")
            else:
                client.call("PATCH", url, headers, json.dumps({item["column"]: item["value"]}).encode("utf-8"))
                record.update(status=SENT)
        except DataSourceError as exc:
            record.update(status=FAILED, reason=str(exc)[:300])
        results.append(record)
        _append(connector, record)
    return results


def transfer_status(connector: ServiceNowConnector) -> list[dict[str, Any]]:
    """Every sent value against the snapshot: confirmed (the sync after the transfer shows it), waiting (no sync
    since) or differs (the sync shows another value)."""
    synced = str(connector.state().get("synced_at") or "")
    tables: dict[str, dict[str, dict[str, Any]]] = {}
    out = []
    for record in read_log(connector):
        if record.get("status") != SENT:
            out.append({**record, "check": record.get("status")})
            continue
        table = record["sn_table"]
        if table not in tables:
            tables[table] = {str(r.get("sys_id")): r for r in _snapshot_rows(connector, table)}
        snap = tables[table].get(record["sys_id"])
        if synced == str(record.get("synced_before") or ""):  # no sync since the transfer
            check = "waiting for the next sync"
        elif snap is not None and _same(snap.get(record["column"]), record["value"]):
            check = "confirmed"
        else:
            check = "differs"
        out.append({**record, "check": check})
    return out
