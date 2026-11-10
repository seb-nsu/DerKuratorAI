"""The data provider over a mapping: connectors deliver raw tables, the mapping translates them into the canonical
schema, approved changes travel the same way back.

Where approved changes are written:
- one writable JSON or Excel source whose tables are all mapped one to one: back into that source, translated through
  the inverse mapping (source names, source units, source values);
- otherwise (several sources, a read-only source, a CSV folder, an unpivoted or generated table): into the mapping's
  own store, a JSON file in canonical form that is laid over the sources on every load.
Either way the base class takes the snapshot and writes the intent/committed records of the write journal."""
from __future__ import annotations

import json
import logging
import math
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from app.data_adapters import mapping as mp
from app.data_adapters.base import WRITABLE_ROW_FIELDS, DataSourceError, PortfolioDataProvider
from app.data_adapters.connectors import CsvFolderConnector, TableWrite, _atomic_write
from app.data_adapters.dataset import REQUIRED_TABLES
from app.data_adapters.schema import SCHEMA

logger = logging.getLogger(__name__)
PROJECTS_KEY = "project_number"


def _key_column(table: str) -> str | None:
    if table == "Projects":
        return PROJECTS_KEY
    if table in WRITABLE_ROW_FIELDS:
        return WRITABLE_ROW_FIELDS[table][0]
    return SCHEMA[table].key if table in SCHEMA else None


class MappedPortfolioAdapter(PortfolioDataProvider):
    source_name = "mapped"

    def __init__(self, mapping: mp.Mapping | str | Path):
        super().__init__()
        self.mapping = mapping if isinstance(mapping, mp.Mapping) else mp.load_mapping(mapping)
        self.last_import_report: mp.ImportReport | None = None
        if self.write_mode == "store":
            self._ensure_store()

    # ------------------------------------------------------------------ where writes go
    @property
    def write_mode(self) -> str:
        sources = {tm.source for tm in self.mapping.all_parts()} or set(self.mapping.connectors)
        if len(sources) == 1:
            connector = self.mapping.connectors[next(iter(sources))]
            if (connector.writable and not isinstance(connector, CsvFolderConnector)
                    and all(tm.invertible for tm in self.mapping.all_parts())):
                return "source"
        return "store"

    @property
    def _target_connector(self):
        source = next(iter({tm.source for tm in self.mapping.tables.values()} or set(self.mapping.connectors)))
        return self.mapping.connectors[source]

    @property
    def source_path(self) -> Path | None:
        if self.write_mode == "source":
            return self._target_connector.path
        return self.mapping.store

    def source_files(self) -> list[Path]:
        """Every file whose content makes up the data (change detection for the graph and the UI caches)."""
        return self.mapping.files()

    # ------------------------------------------------------------------ read
    def load_tables(self) -> tuple[dict[str, pd.DataFrame], dict[str, Any]]:
        tables, metadata, report = mp.load(self.mapping)
        for table in REQUIRED_TABLES:  # a table the mapping leaves out (the source does not hold it): empty, reported
            if table not in self.mapping.tables and table not in tables:
                tables[table] = pd.DataFrame(columns=list(SCHEMA[table].columns))
                report.table(table, "–")
                report.warn(table, "*", f"Table {table} is not part of the mapping {self.mapping.name} – loaded empty.",
                            finding=True)
        if self.write_mode == "store":
            tables = _overlay(tables, self._read_store(), report)
        self.last_import_report = report
        return tables, {**metadata, "import_report": report.to_dict()}

    # ------------------------------------------------------------------ write
    def _write_project_fields(self, updates: dict[str, dict[str, Any]]) -> None:
        self._write_all(updates, {}, {})

    def _write_rows(self, appends: dict[str, list[dict[str, Any]]],
                    row_updates: dict[str, dict[str, dict[str, Any]]]) -> None:
        self._write_all({}, appends, row_updates)

    def _write_changes(self, updates: dict[str, dict[str, Any]], appends: dict[str, list[dict[str, Any]]],
                       row_updates: dict[str, dict[str, dict[str, Any]]]) -> None:
        if appends or row_updates:
            self._write_all(updates, appends, row_updates)
        else:
            self._write_project_fields(updates)

    def _write_all(self, updates: dict[str, dict[str, Any]], appends: dict[str, list[dict[str, Any]]],
                   row_updates: dict[str, dict[str, dict[str, Any]]]) -> None:
        """Project fields, appended rows and row updates in one write of the target (source file or store)."""
        per_table: dict[str, dict[str, Any]] = {}
        if updates:
            per_table["Projects"] = {"update": updates, "append": []}
        for table, by_key in row_updates.items():
            per_table.setdefault(table, {"update": {}, "append": []})["update"] = by_key
        for table, rows in appends.items():
            per_table.setdefault(table, {"update": {}, "append": []})["append"] = list(rows)
        if self.write_mode == "source":
            self._write_source(per_table)
        else:
            self._write_store(per_table)

    def _write_source(self, per_table: dict[str, dict[str, Any]]) -> None:
        connector = self._target_connector
        changes: dict[str, TableWrite] = {}
        for table, change in per_table.items():
            tm = self.mapping.tables.get(table) or mp.TableMapping(table, "main", table)
            key = _key_column(table) or PROJECTS_KEY
            changes[tm.source_table] = TableWrite(
                key=tm.source_column(key),
                update={k: tm.to_source_row(fields) for k, fields in change["update"].items()},
                append=[tm.to_source_row(r) for r in change["append"]],
                create_columns=table != "Projects")
        connector.write(changes)

    # ------------------------------------------------------------------ own store
    def _ensure_store(self) -> None:
        store = self.mapping.store
        if store is not None and not store.exists():
            store.parent.mkdir(parents=True, exist_ok=True)
            store.write_text(json.dumps({"updates": {}, "appends": {}}, indent=2), encoding="utf-8")

    def _read_store(self) -> dict[str, Any]:
        store = self.mapping.store
        if store is None or not store.exists():
            return {"updates": {}, "appends": {}, "source_values": {}}
        try:
            data = json.loads(store.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DataSourceError(f"Change store {store.name} could not be read: {exc}") from exc
        return {"updates": data.get("updates") or {}, "appends": data.get("appends") or {},
                "source_values": data.get("source_values") or {}}

    def _write_store(self, per_table: dict[str, dict[str, Any]]) -> None:
        store = self.mapping.store
        if store is None:
            raise DataSourceError("This mapping has no store for approved changes.")
        data = self._read_store()
        # The source value at the time of the approval: once the source moves away from it, the source wins again
        source = mp.load(self.mapping)[0] if any(c["update"] for c in per_table.values()) else {}
        for table, change in per_table.items():
            for key, fields in change["update"].items():
                data["updates"].setdefault(table, {}).setdefault(str(key), {}).update(fields)
                seen = data["source_values"].setdefault(table, {}).setdefault(str(key), {})
                for column in fields:
                    if column not in seen:  # the first approval records the value it replaced
                        found, value = _source_value(source.get(table), table, key, column)
                        if found:
                            seen[column] = value
            data["appends"].setdefault(table, []).extend(change["append"])

        def dump(tmp: Path) -> None:
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

        _atomic_write(store, dump)


def _plain(value: Any) -> Any:
    """JSON-safe scalar for the store (no NaN, no numpy or pandas types)."""
    if value is None or (isinstance(value, float) and math.isnan(value)) or value is pd.NaT:
        return None
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, (pd.Timestamp, datetime, date)):
        return value.isoformat()
    return value


def _same(a: Any, b: Any) -> bool:
    a, b = _plain(a), _plain(b)
    if a is None or b is None:
        return a is None and b is None
    try:
        return math.isclose(float(a), float(b), rel_tol=0, abs_tol=1e-9)
    except (TypeError, ValueError):
        return str(a).strip() == str(b).strip()


def _source_value(df: pd.DataFrame | None, table: str, key: Any, column: str) -> tuple[bool, Any]:
    """(found, value) of one field of one row in the mapped source table."""
    key_col = _key_column(table)
    if df is None or key_col is None or key_col not in df.columns or column not in df.columns:
        return False, None
    rows = df[df[key_col].astype(str).str.strip() == str(key).strip()]
    return (True, _plain(rows.iloc[0][column])) if not rows.empty else (False, None)


def _overlay(tables: dict[str, pd.DataFrame], store: dict[str, Any], report: Any = None) -> dict[str, pd.DataFrame]:
    """Lays the approved changes of the store over the mapped source tables (appends first, then field updates). A
    field whose source value changed since the approval is left to the source – the source moved on – and reported."""
    out = dict(tables)
    recorded = store.get("source_values") or {}
    for table, rows in store["appends"].items():
        if rows:
            out[table] = pd.concat([out.get(table, pd.DataFrame()), pd.DataFrame(rows)], ignore_index=True)
    for table, by_key in store["updates"].items():
        key = _key_column(table)
        df = out.get(table)
        if df is None or key is None or key not in df.columns:
            logger.warning("Stored changes for %s cannot be applied (table or key column missing)", table)
            continue
        df = df.copy()
        keys = df[key].astype(str).str.strip()
        for row_key, fields in by_key.items():
            mask = keys == str(row_key).strip()
            if not mask.any():
                logger.warning("Stored change for %s %s: row not found", table, row_key)
                continue
            before = recorded.get(table, {}).get(str(row_key), {})
            for column, value in fields.items():
                if column in before and column in df.columns and not _same(df.loc[mask, column].iloc[0], before[column]):
                    if report is not None:
                        report.table(table, "store")
                        report.warn(table, column, f"{row_key}: the source changed {column} since the approved change "
                                    "– the source value applies.", finding=True)
                    continue
                if column not in df.columns:
                    df[column] = None
                df[column] = df[column].astype(object)
                df.loc[mask, column] = value
        out[table] = df
    return out
