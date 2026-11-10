"""Mapping layer: translates the raw tables of one or more connectors into the canonical schema and back.

A mapping file (YAML) names the sources and, per canonical table, where it comes from and how it is translated:

    name: Customer portfolio
    sources:
      main:   {type: excel, path: portfolio.xlsx}
      budget: {type: csv, path: budget_csv, writable: false, options: {sep: ";", decimal: ","}}
    default_source: main
    store: portfolio.store.json          # where approved changes go when the sources cannot take them
    tables:
      Projects:
        from: Vorhaben                   # sheet / CSV file / JSON array (default: the canonical name)
        columns: {Nr: project_number, Bezeichnung: project_name, "Budget (TEUR)": budget_cost_eur}
        values: {status: {grün: Green, gelb: Yellow, rot: Red}}
        units: {budget_cost_eur: 1000}   # canonical value = source value x factor
        defaults: {execution_type: Waterfall}
      Cost_Plans:
        source: budget
        from: Kosten
        columns: {Projekt: project_number, Art: cost_type}
        unpivot: {columns: "^\\d{4}$", to: month, value: planned_cost_eur, year_to_date: true}
        key: {generate: "{project_number}-{cost_type}-{month:%Y}"}

One canonical table can also come from several sources at once (a portfolio over several tools): the table lists
one block per source, each with its own translation. The rows are combined and carry their source in the column
`source_system`; a `key_prefix` of a source keeps its keys (and the references inside it) apart from the others, and
`duplicates` names the fields that mark the same project in two sources:

    sources:
      snow: {type: servicenow, path: snow_snapshot, key_prefix: "SN-", label: ServiceNow}
      bu2:  {type: excel, path: bu2.xlsx, key_prefix: "BU2-", label: Business unit 2}
    tables:
      Projects:
        - {source: snow, from: pm_project, columns: {number: project_number}}
        - {source: bu2, from: Projekte, columns: {Nr: project_number, Name: project_name}}
    duplicates: {Projects: [project_name, project_sponsor]}

Relative paths are resolved against the folder of the mapping file. Loading involves no LLM: every step is a plain,
deterministic table operation, and everything it drops or doubts is listed in the import report."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from app.data_adapters.base import DataSourceError
from app.data_adapters.connectors import CONNECTORS, NOT_A_TABLE, Connector
from app.data_adapters.schema import DATE, NUMBER, SCHEMA
from app.data_adapters.servicenow import ServiceNowConnector
from app.data_adapters.work_management import MondayConnector, PlannerConnector

CONNECTOR_TYPES: dict[str, type[Connector]] = {**CONNECTORS, ServiceNowConnector.kind: ServiceNowConnector,
                                               PlannerConnector.kind: PlannerConnector,
                                               MondayConnector.kind: MondayConnector}

TABLE_OPTIONS = {"source", "from", "columns", "values", "units", "defaults", "unpivot", "key", "keep_unmapped"}
SOURCE_COLUMN = "source_system"  # the source of a row when a table comes from several sources
# fields compared when the same project comes from two sources
CONTRADICTION_FIELDS = ("budget_cost_eur", "estimate_at_completion_eur", "planned_end_date", "approved_end_date",
                        "status")


@dataclass
class TableMapping:
    table: str  # canonical name
    source: str
    source_table: str
    columns: dict[str, str] = field(default_factory=dict)  # source column -> canonical column
    values: dict[str, dict[Any, Any]] = field(default_factory=dict)  # canonical column -> {source value: value}
    units: dict[str, float] = field(default_factory=dict)  # canonical column -> factor
    defaults: dict[str, Any] = field(default_factory=dict)
    unpivot: dict[str, Any] | None = None
    key_template: str | None = None
    keep_unmapped: bool = True

    @property
    def invertible(self) -> bool:
        """Approved changes can be written back to the source only through a one-to-one mapping."""
        values_one_to_one = all(len(set(m.values())) == len(m) for m in self.values.values())
        return self.unpivot is None and self.key_template is None and values_one_to_one

    def source_column(self, canonical: str) -> str:
        return next((s for s, c in self.columns.items() if c == canonical), canonical)

    def to_source_value(self, canonical: str, value: Any) -> Any:
        mapping = self.values.get(canonical)
        if mapping:
            value = next((s for s, c in mapping.items() if c == value), value)
        factor = self.units.get(canonical)
        if factor and isinstance(value, (int, float)) and not isinstance(value, bool):
            value = round(value / factor, 6)
        return value

    def to_source_row(self, row: dict[str, Any]) -> dict[str, Any]:
        return {self.source_column(c): self.to_source_value(c, v) for c, v in row.items()}


@dataclass
class Mapping:
    name: str
    connectors: dict[str, Connector]
    tables: dict[str, TableMapping]
    store: Path | None = None
    origin: Path | None = None  # the mapping file (None: built in code)
    more: dict[str, list[TableMapping]] = field(default_factory=dict)  # further sources of a table (federation)
    key_prefixes: dict[str, str] = field(default_factory=dict)  # source -> prefix of its keys
    labels: dict[str, str] = field(default_factory=dict)  # source -> name shown as the row's source
    duplicates: dict[str, list[str]] = field(default_factory=dict)  # table -> fields that mark the same row

    def parts(self, table: str) -> list[TableMapping]:
        """Every source block of one canonical table, the first one first."""
        first = self.tables.get(table)
        return ([first] if first else []) + list(self.more.get(table, []))

    def all_parts(self) -> list[TableMapping]:
        return [tm for table in self.tables for tm in self.parts(table)]

    @property
    def federated(self) -> bool:
        """True when at least one table comes from more than one source."""
        return any(self.more.values())

    def label(self, source: str) -> str:
        return self.labels.get(source, source)

    def files(self) -> list[Path]:
        files = [f for c in self.connectors.values() for f in c.files()]
        return files + [p for p in (self.store, self.origin) if p is not None]


# --------------------------------------------------------------------------- building a mapping
def identity_mapping(connector: Connector, name: str | None = None) -> Mapping:
    """Every canonical table under its own name from one source, columns unchanged (the demo export format)."""
    return Mapping(name=name or f"{connector.kind} ({connector.path.name})", connectors={"main": connector},
                   tables={t: TableMapping(t, "main", t) for t in SCHEMA})


def _resolve(base: Path, value: str | None) -> Path | None:
    if not value:
        return None
    path = Path(value)
    return path if path.is_absolute() else (base / path).resolve()


def load_mapping(path: str | Path) -> Mapping:
    """Reads and checks a mapping file. Raises DataSourceError with a plain explanation for every mistake."""
    import yaml

    path = Path(path)
    if not path.exists():
        raise DataSourceError(f"Mapping file not found: {path}")
    try:
        spec = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise DataSourceError(f"Mapping file {path.name} is not valid YAML: {exc}") from exc
    if not isinstance(spec, dict) or not isinstance(spec.get("sources"), dict) or not spec["sources"]:
        raise DataSourceError(f"Mapping file {path.name} needs a 'sources' section.")
    base = path.parent
    connectors: dict[str, Connector] = {}
    prefixes: dict[str, str] = {}
    labels: dict[str, str] = {}
    for name, src in spec["sources"].items():
        if not isinstance(src, dict) or src.get("type") not in CONNECTOR_TYPES or not src.get("path"):
            raise DataSourceError(f"Source '{name}' needs a type ({', '.join(CONNECTOR_TYPES)}) and a path.")
        connectors[str(name)] = CONNECTOR_TYPES[src["type"]](_resolve(base, src["path"]),
                                                        writable=bool(src.get("writable", True)),
                                                        options=src.get("options") or {})
        if src.get("key_prefix"):
            prefixes[str(name)] = str(src["key_prefix"])
        if src.get("label"):
            labels[str(name)] = str(src["label"])
    default = str(spec.get("default_source") or next(iter(connectors)))
    tables_spec = spec.get("tables") or {}
    if not isinstance(tables_spec, dict):
        raise DataSourceError("'tables' must map canonical table names to their settings.")
    tables: dict[str, TableMapping] = {}
    more: dict[str, list[TableMapping]] = {}
    for table, raw_opts in tables_spec.items():
        if table not in SCHEMA:
            raise DataSourceError(f"Unknown table '{table}' in {path.name}. Known: {', '.join(SCHEMA)}")
        blocks = raw_opts if isinstance(raw_opts, list) else [raw_opts]
        if not blocks:
            raise DataSourceError(f"Table {table} lists no source.")
        parts = [_table_mapping(table, opts or {}, default, connectors) for opts in blocks]
        if len({tm.source for tm in parts}) < len(parts):
            raise DataSourceError(f"Table {table} names the same source twice.")
        tables[table] = parts[0]
        if len(parts) > 1:
            more[table] = parts[1:]
    duplicates = spec.get("duplicates") or {}
    if not isinstance(duplicates, dict) or any(t not in SCHEMA or not isinstance(f, list) or not f
                                               for t, f in duplicates.items()):
        raise DataSourceError("'duplicates' must map a canonical table to the list of fields that mark the same row.")
    store = _resolve(base, spec.get("store")) or path.with_suffix(".store.json")
    return Mapping(name=str(spec.get("name") or path.stem), connectors=connectors, tables=tables, store=store,
                   origin=path, more=more, key_prefixes=prefixes, labels=labels,
                   duplicates={str(t): [str(c) for c in f] for t, f in duplicates.items()})


def _table_mapping(table: str, opts: dict[str, Any], default: str, connectors: dict[str, Connector]) -> TableMapping:
    """One source block of a canonical table, checked."""
    if not isinstance(opts, dict):
        raise DataSourceError(f"The settings of {table} must be a mapping or a list of mappings.")
    unknown = set(opts) - TABLE_OPTIONS
    if unknown:
        raise DataSourceError(f"Unknown setting(s) for {table}: {', '.join(sorted(unknown))}")
    source = str(opts.get("source") or default)
    if source not in connectors:
        raise DataSourceError(f"Table {table} uses the unknown source '{source}'.")
    unpivot = opts.get("unpivot")
    if unpivot is not None and not ({"columns", "to", "value"} <= set(unpivot)):
        raise DataSourceError(f"unpivot of {table} needs 'columns', 'to' and 'value'.")
    return TableMapping(
        table=table, source=source, source_table=str(opts.get("from") or table),
        columns={str(k): str(v) for k, v in (opts.get("columns") or {}).items()},
        values={str(k): dict(v) for k, v in (opts.get("values") or {}).items()},
        units={str(k): float(v) for k, v in (opts.get("units") or {}).items()},
        defaults=dict(opts.get("defaults") or {}), unpivot=unpivot,
        key_template=(opts.get("key") or {}).get("generate"), keep_unmapped=bool(opts.get("keep_unmapped", True)))


# --------------------------------------------------------------------------- import report
@dataclass
class ImportReport:
    mapping: str
    tables: dict[str, dict[str, Any]] = field(default_factory=dict)
    findings: list[dict[str, str]] = field(default_factory=list)  # severity, table, column, message

    def table(self, name: str, source: str) -> dict[str, Any]:
        entry = self.tables.setdefault(name, {"source": source, "read": 0, "loaded": 0, "dropped": 0, "warnings": []})
        if source not in entry["source"].split(" + "):  # a second source of the same table
            entry["source"] = f"{entry['source']} + {source}"
        return entry

    def warn(self, table: str, column: str, message: str, finding: bool = False, severity: str = "WARNING",
             project: str = "*") -> None:
        self.tables[table]["warnings"].append(message)
        if finding:
            self.findings.append({"severity": severity, "table": table, "column": column, "message": message,
                                  "project": project})

    def to_dict(self) -> dict[str, Any]:
        return {"mapping": self.mapping, "tables": self.tables, "findings": self.findings}


# --------------------------------------------------------------------------- applying a mapping
def _parse_number(value: Any, decimal: str) -> Any:
    """A number from text ("1.234,5" with decimal=","); anything unparseable is returned unchanged."""
    if not isinstance(value, str):
        return value
    text = value.strip().replace(" ", "").replace(" ", "")
    if decimal == ",":
        text = text.replace(".", "").replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return value


def _is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, float) and pd.isna(value)) or (isinstance(value, str) and not value.strip())


def _blank_rows(df: pd.DataFrame) -> pd.Series:
    mask = df.isna()
    for col in df.columns:
        if df[col].dtype == object:
            mask[col] = mask[col] | df[col].map(lambda v: isinstance(v, str) and not v.strip())
    return mask.all(axis=1) if len(df.columns) else pd.Series(False, index=df.index)


def _type_columns(df: pd.DataFrame, connector: Connector, tm: TableMapping, report: ImportReport) -> pd.DataFrame:
    """Text sources (CSV) and scaled columns get real numbers and ISO dates; values that do not parse stay as they
    are and are reported (the Data Steward judges them like any other bad value)."""
    decimal = connector.options.get("decimal", ".")
    date_format = connector.options.get("date_format")
    spec = SCHEMA[tm.table]
    for col in df.columns:
        kind = spec.column_type(col)
        typed_here = not connector.typed or col in tm.units
        if kind == NUMBER and typed_here:
            parsed = df[col].map(lambda v: _parse_number(v, decimal))
            bad = parsed.map(lambda v: isinstance(v, str) and bool(v.strip()))
            if bad.any():
                report.warn(tm.table, col, f"{int(bad.sum())} value(s) in column '{col}' are not numbers "
                            f"(e.g. '{parsed[bad].iloc[0]}').", finding=True)
            df[col] = parsed
        elif kind == DATE and not connector.typed and date_format:
            def to_iso(v: Any) -> Any:
                if _is_blank(v):
                    return None
                try:
                    return pd.to_datetime(str(v).strip(), format=date_format).date().isoformat()
                except (ValueError, TypeError):
                    return v
            df[col] = df[col].map(to_iso)
    for col, factor in tm.units.items():
        if col in df.columns:
            df[col] = df[col].map(lambda v, f=factor: round(v * f, 6)
                                  if isinstance(v, (int, float)) and not isinstance(v, bool) and not pd.isna(v) else v)
    return df


def _unpivot(df: pd.DataFrame, tm: TableMapping) -> pd.DataFrame:
    spec = tm.unpivot or {}
    pattern = re.compile(str(spec["columns"]))
    year_cols = [c for c in df.columns if pattern.search(str(c))]
    if not year_cols:
        raise DataSourceError(f"unpivot of {tm.table}: no source column matches '{spec['columns']}'.")
    ids = [c for c in df.columns if c not in year_cols]
    long = df.melt(id_vars=ids, value_vars=year_cols, var_name=spec["to"], value_name=spec["value"])
    long = long[~long[spec["value"]].map(_is_blank)].reset_index(drop=True)
    if spec.get("year_to_date"):  # "2027" -> first day of that year, so the column is a real date (month)
        long[spec["to"]] = long[spec["to"]].map(lambda y: f"{str(y).strip()[:4]}-01-01")
    return long


def _generate_keys(df: pd.DataFrame, tm: TableMapping, key: str) -> pd.DataFrame:
    def render(row: pd.Series) -> str:
        values = {k: (pd.Timestamp(v) if k in ("month",) or str(k).endswith("_date") else v) for k, v in row.items()}
        return str(tm.key_template).format(**values)

    df[key] = df.apply(render, axis=1)
    return df


def map_table(raw: pd.DataFrame, tm: TableMapping, connector: Connector, report: ImportReport) -> pd.DataFrame:
    """One source table -> one canonical table, with everything that was dropped or doubted in the report."""
    spec = SCHEMA[tm.table]
    entry = report.table(tm.table, connector.label(tm.source_table))
    df = raw.copy()
    df.columns = [str(c).strip() for c in df.columns]
    entry["read"] += len(df)
    blank = _blank_rows(df)
    if blank.any():  # trailing formatted rows of a sheet: counted, not a finding
        entry["dropped"] += int(blank.sum())
        report.warn(tm.table, "*", f"{int(blank.sum())} empty row(s) skipped.")
        df = df[~blank].reset_index(drop=True)
    for source_col in tm.columns:
        if source_col not in df.columns:
            report.warn(tm.table, tm.columns[source_col], f"Column '{source_col}' (mapped to "
                        f"{tm.columns[source_col]}) not found in {connector.label(tm.source_table)}.", finding=True)
    if tm.unpivot:
        df = _unpivot(df, tm)
    clash = [c for c in df.columns if c in set(tm.columns.values()) and c not in tm.columns]
    if clash:  # a source column already named like a mapping target (e.g. a reference's id): the mapped one wins
        df = df.drop(columns=clash)
    df = df.rename(columns=tm.columns)
    if not tm.keep_unmapped:
        df = df[[c for c in df.columns if c in set(tm.columns.values()) | set(spec.columns)]]
    for col, mapping in tm.values.items():
        if col not in df.columns:
            continue
        unknown = sorted({str(v) for v in df[col] if not _is_blank(v) and v not in mapping
                          and v not in mapping.values()})
        if unknown:
            report.warn(tm.table, col, f"Value(s) {', '.join(unknown[:5])} in column '{col}' have no translation.",
                        finding=True)
        df[col] = df[col].map(lambda v, m=mapping: m.get(v, v))
    df = _type_columns(df, connector, tm, report)
    for col, value in tm.defaults.items():
        if col not in df.columns:
            df[col] = value
        else:
            df[col] = df[col].map(lambda v, d=value: d if _is_blank(v) else v)
    if tm.key_template and spec.key:
        try:
            df = _generate_keys(df, tm, spec.key)
        except (KeyError, ValueError) as exc:
            raise DataSourceError(f"Key template of {tm.table} cannot be filled: {exc}") from exc
    if len(df):
        for col in spec.required:
            if col not in df.columns:
                df[col] = None
                severity = "ERROR" if tm.table == "Projects" else "WARNING"
                report.warn(tm.table, col, f"Required column '{col}' missing in {connector.label(tm.source_table)}"
                            " – added empty.", finding=True, severity=severity)
        if spec.foreign_keys.get("project_number") == "Projects" and "project_number" in df.columns:
            orphan = df["project_number"].map(_is_blank)
            if orphan.any():  # a project row without a project belongs nowhere
                report.warn(tm.table, "project_number", f"{int(orphan.sum())} row(s) without project_number in "
                            f"{connector.label(tm.source_table)} were dropped.", finding=True)
                entry["dropped"] += int(orphan.sum())
                df = df[~orphan].reset_index(drop=True)
        if spec.key and spec.key in df.columns:
            keys = df[spec.key].map(lambda v: None if _is_blank(v) else str(v).strip())
            if keys.isna().any():
                report.warn(tm.table, spec.key, f"{int(keys.isna().sum())} row(s) without {spec.key}.")
            duplicated = keys[keys.notna() & keys.duplicated()]
            if len(duplicated):
                report.warn(tm.table, spec.key, f"Duplicate {spec.key}: {', '.join(sorted(set(duplicated))[:5])}.")
        for col, allowed in spec.allowed.items():
            if col in df.columns:
                bad = sorted({str(v) for v in df[col] if not _is_blank(v) and v not in allowed})
                if bad:
                    report.warn(tm.table, col, f"Value(s) {', '.join(bad[:5])} in '{col}' are not allowed "
                                f"({', '.join(allowed)}).")
    entry["loaded"] += len(df)
    return df


def check_foreign_keys(tables: dict[str, pd.DataFrame], report: ImportReport) -> None:
    """Rows pointing at a project (or goal) that is not in the data: reported, kept (the Data Steward and the
    graph treat them as before)."""
    for name, df in tables.items():
        spec = SCHEMA[name]
        for col, ref in spec.foreign_keys.items():
            target = tables.get(ref)
            ref_key = SCHEMA[ref].key
            if df.empty or col not in df.columns or target is None or ref_key not in target.columns:
                continue
            known = set(target[ref_key].dropna().astype(str).str.strip())
            values = df[col].dropna().astype(str).str.strip()
            orphans = sorted(set(values) - known)
            if orphans:
                report.warn(name, col, f"{len(orphans)} {col} value(s) not found in {ref}: {', '.join(orphans[:5])}.")


def _with_prefix(df: pd.DataFrame, table: str, prefix: str) -> pd.DataFrame:
    """The key of a table and its references to other tables, prefixed for one source (values already carrying the
    prefix stay as they are)."""
    spec = SCHEMA[table]
    for col in [c for c in (spec.key, *spec.foreign_keys) if c and c in df.columns]:
        df[col] = df[col].map(lambda v: v if _is_blank(v) or str(v).startswith(prefix) else f"{prefix}{str(v).strip()}")
    return df


def _combine(table: str, frames: list[tuple[TableMapping, pd.DataFrame]], mapping: Mapping) -> pd.DataFrame:
    """The rows of one table from several sources; a key that two sources both use is an error, never an
    overwrite."""
    key = SCHEMA[table].key
    if key:
        owner: dict[str, str] = {}
        for tm, df in frames:
            if key not in df.columns:
                continue
            for value in df[key].dropna().astype(str).str.strip().unique():
                if value in owner and owner[value] != tm.source:
                    raise DataSourceError(
                        f"{table}: {key} {value} comes from the sources '{owner[value]}' and '{tm.source}'. "
                        "Give one of them a key_prefix in the mapping file.")
                owner[value] = tm.source
    return pd.concat([df for _, df in frames], ignore_index=True, sort=False)


def _norm(value: Any) -> str:
    return "" if _is_blank(value) else " ".join(str(value).lower().split())


def _differs(a: Any, b: Any) -> bool:
    if _is_blank(a) or _is_blank(b):
        return False
    try:
        return abs(float(a) - float(b)) > 1e-6
    except (TypeError, ValueError):
        pass
    text_a, text_b = str(a).strip(), str(b).strip()
    if re.match(r"\d{4}-\d{2}-\d{2}", text_a) and re.match(r"\d{4}-\d{2}-\d{2}", text_b):
        return text_a[:10] != text_b[:10]  # a date and a timestamp of the same day are the same
    return text_a != text_b


def check_duplicates(tables: dict[str, pd.DataFrame], mapping: Mapping, report: ImportReport) -> None:
    """Rows of different sources with the same `duplicates` fields: the same project held in two tools. Both rows
    stay; the finding names both, and every compared field in which they disagree."""
    for table, fields in mapping.duplicates.items():
        df = tables.get(table)
        key = SCHEMA[table].key
        if df is None or df.empty or SOURCE_COLUMN not in df.columns or not set(fields) <= set(df.columns):
            continue
        groups: dict[tuple[str, ...], list[dict[str, Any]]] = {}
        for row in df.to_dict("records"):
            match = tuple(_norm(row.get(f)) for f in fields)
            if all(match):
                groups.setdefault(match, []).append(row)
        for rows in groups.values():
            if len({r[SOURCE_COLUMN] for r in rows}) < 2:
                continue
            first, *others = rows
            for other in others:
                if other[SOURCE_COLUMN] == first[SOURCE_COLUMN]:
                    continue
                a, b = first.get(key), other.get(key)
                names = f"{a} ({first[SOURCE_COLUMN]}) and {b} ({other[SOURCE_COLUMN]})"
                report.warn(table, ", ".join(fields), f"Same {table[:-1].lower()} in two sources: {names}.",
                            finding=True, project=str(a))
                for col in CONTRADICTION_FIELDS:
                    if col in df.columns and _differs(first.get(col), other.get(col)):
                        report.warn(table, col, f"{a} and {b} disagree on {col}: {first.get(col)} "
                                    f"({first[SOURCE_COLUMN]}) vs. {other.get(col)} ({other[SOURCE_COLUMN]}).",
                                    finding=True, project=str(a))


def load(mapping: Mapping) -> tuple[dict[str, pd.DataFrame], dict[str, Any], ImportReport]:
    """Reads every source once and maps it. Returns canonical tables, the metadata of the first source and the
    import report."""
    report = ImportReport(mapping.name)
    raw: dict[str, dict[str, pd.DataFrame]] = {}
    tables: dict[str, pd.DataFrame] = {}
    for table in mapping.tables:
        frames: list[tuple[TableMapping, pd.DataFrame]] = []
        for tm in mapping.parts(table):
            connector = mapping.connectors[tm.source]
            if tm.source not in raw:
                raw[tm.source] = connector.read()
            frame = raw[tm.source].get(tm.source_table)
            if frame is None:
                continue
            if frame is NOT_A_TABLE:
                raise DataSourceError(f"Table '{tm.source_table}' is not an array.")
            df = map_table(frame, tm, connector, report)
            if tm.source in mapping.key_prefixes:
                df = _with_prefix(df, table, mapping.key_prefixes[tm.source])
            if mapping.federated:
                df[SOURCE_COLUMN] = mapping.label(tm.source)
            frames.append((tm, df))
        if frames:
            tables[table] = frames[0][1] if len(frames) == 1 else _combine(table, frames, mapping)
            if len(frames) > 1:
                report.tables[table]["by_source"] = {mapping.label(tm.source): len(df) for tm, df in frames}
    check_foreign_keys(tables, report)
    check_duplicates(tables, mapping, report)
    first = next(iter(mapping.connectors.values()))
    return tables, first.metadata(), report
