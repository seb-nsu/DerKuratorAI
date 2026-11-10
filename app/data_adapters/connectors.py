"""Connectors: deliver the raw tables of one source (JSON file, Excel workbook, folder of CSV files) under their
source names and write changes back in the source's own names. They know nothing about the canonical schema – the
mapping layer (app/data_adapters/mapping.py) translates in both directions.

A write is a dict {source_table: TableWrite}; every connector applies it in one save of its file(s)."""
from __future__ import annotations

import json
import logging
import os
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from app.data_adapters.base import DataSourceError

logger = logging.getLogger(__name__)
# Marker for a JSON entry that is not an array of rows: an error only if the mapping reads it as a table
NOT_A_TABLE = pd.DataFrame({"__not_a_table__": []})


@dataclass
class TableWrite:
    """Changes of one source table: field updates of existing rows by key, and appended rows."""
    key: str
    update: dict[str, dict[str, Any]] = field(default_factory=dict)
    append: list[dict[str, Any]] = field(default_factory=list)
    create_columns: bool = True  # Excel: False skips an unknown column of an update with a warning


class Connector(ABC):
    kind = "abstract"
    typed = True  # False: values arrive as text (CSV) and are typed by the mapping layer

    def __init__(self, path: str | Path, *, writable: bool = True, options: dict[str, Any] | None = None):
        self.path = Path(path)
        self.writable = writable
        self.options = dict(options or {})

    @abstractmethod
    def read(self) -> dict[str, pd.DataFrame]:
        """All tables of the source, keyed by their source name."""

    def metadata(self) -> dict[str, Any]:
        return {}

    def files(self) -> list[Path]:
        """The files whose content makes up the source (for change detection)."""
        return [self.path]

    def write(self, changes: dict[str, TableWrite]) -> None:
        raise DataSourceError(f"The {self.kind} source {self.path.name} is read-only.")

    def label(self, table: str) -> str:
        return f"{self.path.name}:{table}"


def _key(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _apply_to_records(rows: list[dict[str, Any]], change: TableWrite, table: str) -> list[dict[str, Any]]:
    """Applies one TableWrite to a list of row dicts in place (shared by the JSON and CSV connectors)."""
    found = set()
    for row in rows:
        key = _key(row.get(change.key))
        if key in change.update:
            row.update(change.update[key])
            found.add(key)
    missing = set(change.update) - found
    if missing:
        logger.warning("Rows not found in %s, skipped: %s", table, ", ".join(sorted(missing)))
    rows.extend(dict(r) for r in change.append)
    return rows


def _atomic_write(path: Path, write) -> None:
    """Writes via a unique temp file and an atomic replace: no half-written file on abort, and concurrent writers
    never share (or delete) each other's temp file."""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        write(tmp)
        tmp.replace(path)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise DataSourceError(f"Writing {path.name} failed: {exc}") from exc


# --------------------------------------------------------------------------- JSON
class JsonConnector(Connector):
    """A JSON object with one array of row objects per table (the ServiceNow export format)."""
    kind = "json"

    def _raw(self) -> dict[str, Any]:
        if not self.path.exists():
            raise DataSourceError(f"JSON file not found: {self.path}")
        try:
            with self.path.open(encoding="utf-8") as fh:
                data = json.load(fh)
        except json.JSONDecodeError as exc:
            raise DataSourceError(f"JSON file is invalid ({self.path.name}, line {exc.lineno}): {exc.msg}") from exc
        except OSError as exc:
            raise DataSourceError(f"JSON file could not be read: {exc}") from exc
        if not isinstance(data, dict):
            raise DataSourceError("JSON root element must be an object with table arrays.")
        return data

    def read(self) -> dict[str, pd.DataFrame]:
        tables = {}
        for name, rows in self._raw().items():
            if name == "dataset_metadata" or rows is None:
                continue
            if not isinstance(rows, list):
                tables[name] = NOT_A_TABLE
                continue
            tables[name] = pd.DataFrame(rows)
        return tables

    def metadata(self) -> dict[str, Any]:
        return dict(self._raw().get("dataset_metadata", {}) or {})

    def write(self, changes: dict[str, TableWrite]) -> None:
        if not self.writable:
            super().write(changes)
        data = self._raw()
        for table, change in changes.items():
            target = data.setdefault(table, [])
            if not isinstance(target, list):
                raise DataSourceError(f"Table '{table}' is not an array.")
            _apply_to_records(target, change, table)

        def dump(tmp: Path) -> None:
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)

        _atomic_write(self.path, dump)


# --------------------------------------------------------------------------- Excel
class ExcelConnector(Connector):
    """One worksheet per table, the header in the first row."""
    kind = "excel"

    def read(self) -> dict[str, pd.DataFrame]:
        if not self.path.exists():
            raise DataSourceError(f"Excel file not found: {self.path}")
        try:
            xls = pd.ExcelFile(self.path, engine="openpyxl")
            return {name: pd.read_excel(xls, sheet_name=name, dtype=object) for name in xls.sheet_names}
        except Exception as exc:
            raise DataSourceError(f"Excel file could not be read: {exc}") from exc

    def metadata(self) -> dict[str, Any]:
        return {"source_file": self.path.name}

    def write(self, changes: dict[str, TableWrite]) -> None:
        if not self.writable:
            super().write(changes)
        from openpyxl import load_workbook

        try:
            wb = load_workbook(self.path)
        except Exception as exc:
            raise DataSourceError(f"Excel file could not be opened: {exc}") from exc
        for table, change in changes.items():
            if change.update:
                _update_sheet(wb, table, change)
            if change.append:
                _append_sheet(wb, table, change.append)
        try:
            wb.save(self.path)
        except PermissionError as exc:
            raise DataSourceError(f"Excel file is locked (open in Excel?): {self.path.name}") from exc
        except OSError as exc:
            raise DataSourceError(f"Write failed: {exc}") from exc


def _header(ws) -> dict[str, int]:
    return {str(c.value).strip(): c.column for c in ws[1] if c.value is not None}


def _append_sheet(wb, table: str, rows: list[dict[str, Any]]) -> None:
    """Appends rows to a sheet; a missing sheet or column is created (the header is the first row)."""
    ws = wb[table] if table in wb.sheetnames else wb.create_sheet(table)
    header = _header(ws) if ws.max_row >= 1 and ws.cell(1, 1).value is not None else {}
    for row in rows:
        for col in row:
            if col not in header:
                header[col] = len(header) + 1
                ws.cell(row=1, column=header[col], value=col)
        target = ws.max_row + 1
        for col, value in row.items():
            ws.cell(row=target, column=header[col], value=value)


def _update_sheet(wb, table: str, change: TableWrite) -> None:
    if table not in wb.sheetnames:
        raise DataSourceError(f"Worksheet '{table}' is missing.")
    ws = wb[table]
    header = _header(ws)
    if change.key not in header:
        raise DataSourceError(f"Column '{change.key}' is missing in sheet '{table}'.")
    for row in ws.iter_rows(min_row=2):
        key = _key(row[header[change.key] - 1].value)
        for column, value in change.update.get(key, {}).items():
            if column not in header:
                if not change.create_columns:
                    logger.warning("Column %s missing in Excel – skipped", column)
                    continue
                header[column] = len(header) + 1
                ws.cell(row=1, column=header[column], value=column)
            ws.cell(row=row[0].row, column=header[column], value=value)


# --------------------------------------------------------------------------- CSV folder
class CsvFolderConnector(Connector):
    """A folder with one <table>.csv per table, read-only (approved changes go to the mapping's own store). Options: sep (default ","), encoding (default utf-8), decimal
    ("." or ","), date_format (e.g. "%d.%m.%Y" for German dates)."""
    kind = "csv"
    typed = False

    def _files(self) -> dict[str, Path]:
        if not self.path.is_dir():
            raise DataSourceError(f"CSV folder not found: {self.path}")
        return {p.stem: p for p in sorted(self.path.glob("*.csv"))}

    def read(self) -> dict[str, pd.DataFrame]:
        tables = {}
        for name, file in self._files().items():
            try:
                tables[name] = pd.read_csv(file, dtype=object, keep_default_na=False, na_values=[""],
                                           sep=self.options.get("sep", ","),
                                           encoding=self.options.get("encoding", "utf-8"))
            except Exception as exc:
                raise DataSourceError(f"CSV file {file.name} could not be read: {exc}") from exc
        return tables

    def files(self) -> list[Path]:
        try:
            return list(self._files().values())
        except DataSourceError:
            return []

    def label(self, table: str) -> str:
        return f"{self.path.name}/{table}.csv"


CONNECTORS: dict[str, type[Connector]] = {"json": JsonConnector, "excel": ExcelConnector, "csv": CsvFolderConnector}
