"""Deterministic data quality checks (no LLM) for the Data Steward.

Severities:
  ERROR   -> quality gate: the project is locked for quantitative simulations (graph analysis remains possible).
  WARNING -> business finding, reported in the decision paper.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Literal

import pandas as pd

from app.core import clock
from app.core.required_fields import DEFAULT_CORE_FIELDS, GOVERNANCE_FIELDS, load_required_fields
from app.core.stage_gates import normalize_phase
from app.data_adapters.dataset import CLOSED_PROJECT_STATES, PK, PortfolioDataset

Severity = Literal["ERROR", "WARNING"]

# Default required fields (active unless the admin configures otherwise – see app/core/required_fields.py)
PROJECT_REQUIRED = DEFAULT_CORE_FIELDS
# Governance/business-case required fields (per Data_Quality_Cases: "FAIL completeness")
GOVERNANCE_REQUIRED = GOVERNANCE_FIELDS
NUMERIC_FIELDS = (
    "percent_complete", "budget_cost_eur", "actual_cost_eur", "estimate_at_completion_eur", "total_planned_cost_eur",
    "planned_capital_eur", "planned_operating_eur", "planned_benefit_eur",
)
ALLOWED_VALUES = {"status": ("Green", "Yellow", "Red"), "execution_type": ("Agile", "Waterfall", "Hybrid")}

# data_quality_profile tags -> (severity, explanation)
DQ_PROFILE_TAGS: dict[str, tuple[Severity, str]] = {
    "missing_financial_field": ("ERROR", "Financial field missing (completeness)"),
    "past_due_active": ("ERROR", "Active project with end date in the past (date logic)"),
    "missing_business_case": ("ERROR", "Business case missing (quality gate)"),
    "planning_incomplete": ("ERROR", "Planning incomplete (e.g. sponsor/actual start missing)"),
    "over_budget": ("WARNING", "Costs above budget (financial logic)"),
    "dependency_risk": ("WARNING", "Critical project dependency"),
    "resource_overload": ("WARNING", "Resource overload > 100 %"),
}


@dataclass
class DataQualityIssue:
    severity: Severity
    project_id: str
    column: str
    message: str
    rule: str = ""


@dataclass
class DataQualityReport:
    total_rows: int
    dataset: PortfolioDataset | None = None
    issues: list[DataQualityIssue] = field(default_factory=list)
    required_fields: list[str] = field(default_factory=list)
    completeness_pct: float = 0.0  # Share of filled required-field cells

    # ------------------------------------------------------------------ Scores (0–100)
    @property
    def quality_index(self) -> float:
        """Data quality index = share of projects without ERROR (released for simulations)."""
        if not self.total_rows:
            return 0.0
        return round((self.total_rows - len(self.blocked_project_ids)) / self.total_rows * 100, 1)

    @property
    def warning_free_pct(self) -> float:
        """Share of projects with no findings at all (neither ERROR nor WARNING)."""
        if not self.total_rows:
            return 0.0
        affected = {i.project_id for i in self.issues if i.project_id != "*"}
        return round((self.total_rows - len(affected)) / self.total_rows * 100, 1)

    @property
    def errors(self) -> list[DataQualityIssue]:
        return [i for i in self.issues if i.severity == "ERROR"]

    @property
    def warnings(self) -> list[DataQualityIssue]:
        return [i for i in self.issues if i.severity == "WARNING"]

    @property
    def blocked_project_ids(self) -> list[str]:
        return sorted({i.project_id for i in self.errors if i.project_id != "*"})

    @property
    def blocked_reasons(self) -> dict[str, str]:
        reasons: dict[str, list[str]] = {}
        for i in self.errors:
            if i.project_id != "*":
                reasons.setdefault(i.project_id, []).append(i.rule or i.column)
        return {pid: ", ".join(dict.fromkeys(r)) for pid, r in reasons.items()}

    @property
    def passed(self) -> bool:
        return not self.errors

    @property
    def can_proceed(self) -> bool:
        """Pipeline runs as long as there are no structural errors and projects exist.
        Locked projects are rejected by the simulation tools themselves."""
        structural = any(i.project_id == "*" and i.severity == "ERROR" for i in self.issues)
        return not structural and self.dataset is not None and not self.dataset.projects.empty

    def project_flags(self) -> dict[str, str]:
        """Compact traffic light per project for UI/LLM context."""
        flags = {}
        for i in self.issues:
            if i.project_id == "*":
                continue
            current = flags.get(i.project_id)
            if i.severity == "ERROR" or current is None:
                flags[i.project_id] = "⛔ blocked" if i.severity == "ERROR" else "⚠ warning"
        return flags

    def project_status(self) -> dict[str, str]:
        """Status value per project for UI tables and filters (no display symbols): Blocked or Warnings."""
        return {pid: "Blocked" if "blocked" in flag else "Warnings" for pid, flag in self.project_flags().items()}

    def to_markdown(self, max_rows: int = 40) -> str:
        scores = (f"Data Quality Index {self.quality_index:.0f} % · required-field completeness "
                  f"{self.completeness_pct:.0f} % · without findings {self.warning_free_pct:.0f} %")
        if not self.issues:
            return f"✅ Data Steward: {self.total_rows} projects checked – no findings. {scores}"
        lines = [
            f"**Data Steward:** {self.total_rows} projects checked · {len(self.errors)} errors · "
            f"{len(self.warnings)} warnings · blocked for simulation: {', '.join(self.blocked_project_ids) or '–'}",
            f"**{scores}**",
            "",
            "| Severity | Project | Rule | Field | Finding |",
            "|---|---|---|---|---|",
        ]
        ordered = sorted(self.issues, key=lambda i: (i.severity != "ERROR", i.project_id, i.rule))
        lines += [f"| {i.severity} | {i.project_id} | {i.rule} | {i.column} | {i.message} |" for i in ordered[:max_rows]]
        if len(ordered) > max_rows:
            lines.append(f"| … | | | | {len(ordered) - max_rows} more findings |")
        return "\n".join(lines)


# --------------------------------------------------------------------------- helpers
def _has_value(value) -> bool:
    if value is None:
        return False
    try:
        if pd.isna(value):
            return False
    except (TypeError, ValueError):
        pass
    return not (isinstance(value, str) and not value.strip())


def _pid(row: dict, idx: int) -> str:
    return str(row.get(PK)).strip() if _has_value(row.get(PK)) else f"Row {idx + 1}"


def _num(value) -> float | None:
    v = pd.to_numeric(value, errors="coerce")
    return None if pd.isna(v) else float(v)


def _rows(df: pd.DataFrame) -> list[dict]:
    return df.to_dict(orient="records") if df is not None and not df.empty else []


# --------------------------------------------------------------------------- project checks
def check_required_columns(ds: PortfolioDataset, required: list[str] | None = None) -> list[DataQualityIssue]:
    cols = set(ds.projects.columns)
    return [
        DataQualityIssue("ERROR", "*", c, "Required column missing in the data source", "Schema")
        for c in (required if required is not None else load_required_fields())
        if c not in cols
    ]


def check_import(ds: PortfolioDataset) -> list[DataQualityIssue]:
    """Findings of the import report (mapped sources): missing required columns, unmapped source columns, values that
    are no numbers or have no translation, skipped empty rows."""
    report = (ds.metadata or {}).get("import_report") or {}
    return [DataQualityIssue("ERROR" if f.get("severity") == "ERROR" else "WARNING", str(f.get("project") or "*"),
                             str(f.get("column", "*")),
                             f"{f.get('table')}: {f.get('message')}", "Import")
            for f in report.get("findings", [])]


def check_completeness(ds: PortfolioDataset, required: list[str] | None = None) -> list[DataQualityIssue]:
    """Checks dynamically against the required fields configured by the admin."""
    required = required if required is not None else load_required_fields()
    issues = []
    for idx, row in enumerate(_rows(ds.projects)):
        for col in required:
            if _has_value(row.get(col)):
                continue
            if col in GOVERNANCE_FIELDS:
                issues.append(DataQualityIssue("ERROR", _pid(row, idx), col, "Governance required field is empty", "Governance Completeness"))
            else:
                issues.append(DataQualityIssue("ERROR", _pid(row, idx), col, "Required field is empty", "Completeness"))
    return issues


def completeness_ratio(ds: PortfolioDataset, required: list[str]) -> float:
    """Share of filled required-field cells in percent."""
    rows = _rows(ds.projects)
    cells = len(rows) * len(required)
    if not cells:
        return 100.0
    filled = sum(1 for row in rows for col in required if _has_value(row.get(col)))
    return round(filled / cells * 100, 1)


def check_numeric_and_values(ds: PortfolioDataset) -> list[DataQualityIssue]:
    issues = []
    for idx, row in enumerate(_rows(ds.projects)):
        pid = _pid(row, idx)
        for col in NUMERIC_FIELDS:
            raw = row.get(col)
            if not _has_value(raw):
                continue
            num = _num(raw)
            if num is None:
                issues.append(DataQualityIssue("ERROR", pid, col, f"Not a numeric value: '{raw}'", "Numeric"))
            elif num < 0:
                issues.append(DataQualityIssue("ERROR", pid, col, f"Negative value: {num:g}", "Numeric"))
        pct = _num(row.get("percent_complete"))
        if pct is not None and pct > 100:
            issues.append(DataQualityIssue("ERROR", pid, "percent_complete", f"Progress {pct:g} % > 100 %", "Numeric"))
        for col, allowed in ALLOWED_VALUES.items():
            if _has_value(row.get(col)) and str(row[col]) not in allowed:
                issues.append(DataQualityIssue("ERROR", pid, col, f"Invalid value '{row[col]}' (allowed: {', '.join(allowed)})", "Domain"))
    return issues


def check_budget_overrun(ds: PortfolioDataset) -> list[DataQualityIssue]:
    """Budget overrun check: EAC > budget (forecast) or actual cost > budget. An EAC below the actual cost is an
    impossible forecast (the money is already spent) and blocks the project."""
    issues = []
    for idx, row in enumerate(_rows(ds.projects)):
        pid = _pid(row, idx)
        budget, eac, actual = (_num(row.get(c)) for c in ("budget_cost_eur", "estimate_at_completion_eur", "actual_cost_eur"))
        if eac and actual is not None and eac < actual - 0.5:  # EAC 0 counts as missing (own rule), not as a forecast
            issues.append(DataQualityIssue(
                "ERROR", pid, "estimate_at_completion_eur",
                f"EAC below actual cost: EAC €{eac:,.0f} < actual €{actual:,.0f} – the forecast cannot be lower than "
                "what is already spent", "Forecast Logic",
            ))
        if budget and eac is not None and eac > budget:
            issues.append(DataQualityIssue(
                "WARNING", pid, "estimate_at_completion_eur",
                f"Forecast overrun: EAC €{eac:,.0f} > budget €{budget:,.0f} (+{(eac / budget - 1) * 100:.1f} %)",
                "Budget Overrun",
            ))
        if budget and actual is not None and actual > budget:
            issues.append(DataQualityIssue(
                "WARNING", pid, "actual_cost_eur",
                f"Actual costs €{actual:,.0f} already above budget €{budget:,.0f}", "Budget Overrun",
            ))
    return issues


def check_logic(ds: PortfolioDataset, today: date | None = None) -> list[DataQualityIssue]:
    """Logic check: progress > 0 without actual start; active project with an end date in the past."""
    today_ts = pd.Timestamp(today or clock.today())
    issues = []
    for idx, row in enumerate(_rows(ds.projects)):
        pid = _pid(row, idx)
        pct = _num(row.get("percent_complete")) or 0
        if pct > 0 and not _has_value(row.get("actual_start_date")):
            issues.append(DataQualityIssue(
                "ERROR", pid, "actual_start_date",
                f"Data inconsistency: progress {pct:g} % but no actual_start_date", "Logic Check",
            ))
        end = pd.to_datetime(row.get("planned_end_date"), errors="coerce")
        active = str(row.get("state") or "") not in CLOSED_PROJECT_STATES
        if active and pd.notna(end) and end < today_ts and pct < 100:
            issues.append(DataQualityIssue(
                "ERROR", pid, "planned_end_date",
                f"Active project past due: planned end {end:%Y-%m-%d}, progress {pct:g} %", "Past Due",
            ))
        capex, opex, total = (_num(row.get(c)) for c in ("planned_capital_eur", "planned_operating_eur", "total_planned_cost_eur"))
        if total == 0 and ((capex or 0) > 0 or (opex or 0) > 0):
            issues.append(DataQualityIssue(
                "ERROR", pid, "total_planned_cost_eur",
                f"Total planned cost is 0 although CapEx/OpEx are planned ({(capex or 0) + (opex or 0):,.0f})",
                "Financial Logic",
            ))
        elif None not in (capex, opex, total) and abs(capex + opex - total) > 1:
            issues.append(DataQualityIssue(
                "WARNING", pid, "total_planned_cost_eur", f"CapEx + OpEx ({capex + opex:,.0f}) ≠ total cost ({total:,.0f})",
                "CapEx/OpEx Split",
            ))
    return issues


def check_quality_profile(ds: PortfolioDataset) -> list[DataQualityIssue]:
    """Reads the data_quality_profile field and reports the specific error tag."""
    issues = []
    if "data_quality_profile" not in ds.projects.columns:
        return issues
    for idx, row in enumerate(_rows(ds.projects)):
        tag = str(row.get("data_quality_profile") or "").strip()
        if not tag or tag.lower() == "valid":
            continue
        severity, meaning = DQ_PROFILE_TAGS.get(tag, ("WARNING", "Unknown quality tag"))
        issues.append(DataQualityIssue(severity, _pid(row, idx), "data_quality_profile", f"{meaning} (quality tag “{tag.replace('_', ' ')}”)", f"DQ-Profile:{tag}"))
    return issues


# --------------------------------------------------------------------------- related-table checks
def check_tasks(ds: PortfolioDataset) -> list[DataQualityIssue]:
    tk = ds.tasks
    if tk.empty:
        return []
    issues = []
    known_tasks = set(tk["task_number"].astype(str))
    known_projects = set(ds.project_numbers)
    for row in _rows(tk):
        pid, tid = str(row.get(PK)), str(row.get("task_number"))
        dep = row.get("dependency_task_number")
        if _has_value(dep) and str(dep) not in known_tasks:
            issues.append(DataQualityIssue("WARNING", pid, "dependency_task_number", f"{tid}: predecessor {dep} does not exist", "Task Integrity"))
        if pid not in known_projects:
            issues.append(DataQualityIssue("WARNING", pid, PK, f"{tid}: unknown project", "Referential Integrity"))
        pct = _num(row.get("percent_complete")) or 0
        if pct > 0 and not _has_value(row.get("actual_start_date")):
            issues.append(DataQualityIssue("WARNING", pid, "actual_start_date", f"{tid}: progress {pct:g} % without actual start", "Task Logic"))
        if str(row.get("state")) == "Closed Complete" and pct < 100:
            issues.append(DataQualityIssue("WARNING", pid, "percent_complete", f"{tid}: 'Closed Complete' with {pct:g} %", "Task Logic"))
    return issues


def check_resources(ds: PortfolioDataset) -> list[DataQualityIssue]:
    rp = ds.resource_plans
    if rp.empty:
        return []
    issues = []
    rp = rp.assign(utilization_pct=pd.to_numeric(rp["utilization_pct"], errors="coerce"),
                   capacity_hours=pd.to_numeric(rp["capacity_hours"], errors="coerce"),
                   demand_hours=pd.to_numeric(rp["demand_hours"], errors="coerce"))
    from app.core.business_rules import current_rules

    over = rp[rp["utilization_pct"] > current_rules().team_capacity_limit_pct]  # governance config
    for pid, grp in over.groupby(PK):
        worst = grp.loc[grp["utilization_pct"].idxmax()]
        issues.append(DataQualityIssue(
            "WARNING", str(pid), "utilization_pct",
            f"{grp['assignment_group'].nunique()} team(s) overloaded in {len(grp)} plan months; peak {worst['assignment_group']} "
            f"{worst['utilization_pct']:.1f} % ({worst['month']:%m/%Y})",
            "Resource Overload",
        ))
    calc = (rp["demand_hours"] / rp["capacity_hours"] * 100).round(1)
    bad = rp[(rp["capacity_hours"] > 0) & ((calc - rp["utilization_pct"]).abs() > 1.0)]
    for row in _rows(bad):
        issues.append(DataQualityIssue(
            "WARNING", str(row.get(PK)), "utilization_pct",
            f"{row.get('resource_plan_number')}: utilization_pct does not match demand/capacity", "Resource Consistency",
        ))
    return issues


def check_dependencies(ds: PortfolioDataset) -> list[DataQualityIssue]:
    dp = ds.dependencies
    if dp.empty or "status" not in dp.columns:
        return []
    issues = []
    visible = set(ds.projects[PK].astype(str)) if PK in ds.projects.columns else set()
    hidden: list[str] = []  # successors waiting for a project outside the reader's scope
    for row in _rows(dp[dp["status"].astype(str) == "At Risk"]):
        successor = str(row.get("successor_project_number"))
        if str(row.get("predecessor_project_number")) not in visible:
            hidden.append(successor)
            continue
        issues.append(DataQualityIssue(
            "WARNING", successor, "dependency",
            f"{row.get('dependency_type')} dependency on {row.get('predecessor_project_number')} is 'At Risk'", "Dependency Risk",
        ))
    for successor in dict.fromkeys(hidden):  # one finding per project: no id and no count of the hidden ones
        issues.append(DataQualityIssue(
            "WARNING", successor, "dependency",
            "Dependent projects outside your scope are 'At Risk' – the PMO coordinates", "Dependency Risk"))
    return issues


RISK_LEVELS = ("Low", "Moderate", "High")


def check_pmo_tables(ds: PortfolioDataset) -> list[DataQualityIssue]:
    """The data the PMO tools read: phase on the stage-gate model, risks on the risk scale, unique baseline versions
    and skill demand of a skill its team has. Warnings only – the tools report what they cannot use."""
    issues = []
    if "phase" in ds.projects.columns:
        for row in _rows(ds.projects):
            if _has_value(row.get("phase")) and normalize_phase(row.get("phase")) is None:
                issues.append(DataQualityIssue("WARNING", str(row.get(PK)), "phase",
                                               f"Phase '{row.get('phase')}' is no stage-gate phase", "Phase Model"))
    rk = ds.risks
    for column in ("probability", "impact"):
        if not rk.empty and column in rk.columns:
            for row in _rows(rk[~rk[column].astype(str).isin(RISK_LEVELS)]):
                issues.append(DataQualityIssue("WARNING", str(row.get(PK)), column,
                                               f"{row.get('risk_number')}: {column} '{row.get(column)}' is not "
                                               f"{'/'.join(RISK_LEVELS)}", "Risk Scale"))
    bl = ds.baselines
    if not bl.empty and {"version", PK}.issubset(bl.columns):
        dup = bl[bl["version"].notna() & bl.duplicated([PK, "version"], keep=False)]
        for pid, grp in dup.groupby(PK):
            issues.append(DataQualityIssue("WARNING", str(pid), "version",
                                           f"Baseline version(s) {', '.join(sorted({str(v) for v in grp['version']}))} "
                                           "recorded more than once", "Baseline History"))
    ts, sd = ds.team_skills, ds.skill_demand
    if not ts.empty and not sd.empty and {"assignment_group", "skill"}.issubset(sd.columns):
        known = set(zip(ts["assignment_group"].astype(str), ts["skill"].astype(str), strict=True))
        bad = sd[[(t, k) not in known for t, k in zip(sd["assignment_group"].astype(str), sd["skill"].astype(str),
                                                       strict=True)]]
        for (pid, team, skill), _grp in bad.groupby([PK, "assignment_group", "skill"]):
            issues.append(DataQualityIssue("WARNING", str(pid), "skill",
                                           f"Skill demand for {skill} in {team}, which has no such skill",
                                           "Skill Consistency"))
    return issues


def check_currency(ds: PortfolioDataset) -> list[DataQualityIssue]:
    """Amounts in a currency without an exchange rate stay unconverted: a project is blocked (its figures would be
    calculated as EUR), a plan or actual row is a warning."""
    issues = []
    for attr, label in (("projects", "Projects"), ("cost_plans", "Cost_Plans"), ("benefit_plans", "Benefit_Plans"),
                        ("benefit_actuals", "Benefit_Actuals")):
        df = getattr(ds, attr)
        if df.empty or "fx_rate" not in df.columns:
            continue
        missing = df[df["fx_rate"].isna()]
        for row in _rows(missing):
            if attr == "projects":
                issues.append(DataQualityIssue("ERROR", str(row.get(PK)), "currency",
                                               f"No exchange rate for {row.get('currency')} – the amounts cannot be "
                                               "converted to EUR", "Exchange Rate"))
            else:
                issues.append(DataQualityIssue("WARNING", str(row.get(PK)), "currency",
                                               f"{label}: no exchange rate for {row.get('currency')} – the row stays "
                                               "unconverted", "Exchange Rate"))
    return issues


def check_strategy(ds: PortfolioDataset) -> list[DataQualityIssue]:
    """Goal mappings that the strategy score ignores: an unknown goal or a contribution outside 0-5 (warnings – the
    score then uses the remaining mappings or the value score)."""
    pg = ds.project_goals
    if pg.empty or not {PK, "goal_id", "contribution"} <= set(pg.columns):
        return []
    known = set(ds.strategic_goals["goal_id"].astype(str).str.strip()) \
        if not ds.strategic_goals.empty and "goal_id" in ds.strategic_goals.columns else set()
    issues = []
    for row in _rows(pg):
        goal, value = str(row.get("goal_id") or "").strip(), pd.to_numeric(row.get("contribution"), errors="coerce")
        if goal not in known:
            issues.append(DataQualityIssue("WARNING", str(row.get(PK)), "goal_id",
                                           f"Project_Goals: unknown strategic goal '{goal}' – ignored in the strategy "
                                           "score", "Strategy"))
        elif pd.isna(value) or not 0 <= value <= 5:
            issues.append(DataQualityIssue("WARNING", str(row.get(PK)), "contribution",
                                           f"Project_Goals: contribution to {goal} must be 0-5 (is "
                                           f"{row.get('contribution')})", "Strategy"))
    return issues


def check_reported_status(ds: PortfolioDataset, today: date | None = None) -> list[DataQualityIssue]:
    """Status check of the sources: a project that reports a better traffic light than its numbers give (budget,
    spending against progress, end date), and a project whose last status report is too old."""
    from app.crew.tools.status_check import check_dataset

    issues = []
    for check in check_dataset(ds, today or clock.today()):
        if check.mismatch:
            issues.append(DataQualityIssue("WARNING", check.project_id, "status",
                                           f"Status {check.label}: {'; '.join(check.reasons)}", "Status Check"))
        elif check.stale:
            issues.append(DataQualityIssue("WARNING", check.project_id, "status",
                                           f"Status not updated: last status report {check.last_report}",
                                           "Status Check"))
    return issues


PROJECT_CHECKS: tuple[Callable[[PortfolioDataset], list[DataQualityIssue]], ...] = (
    check_numeric_and_values, check_budget_overrun, check_logic, check_quality_profile,
    check_tasks, check_resources, check_dependencies, check_pmo_tables, check_currency,
    check_strategy, check_reported_status,
)
DATED_CHECKS = (check_logic, check_reported_status)


def run_all_checks(dataset: PortfolioDataset, required_fields: list[str] | None = None,
                   as_of: date | None = None) -> DataQualityReport:
    required = list(required_fields) if required_fields is not None else load_required_fields()
    if dataset is None or dataset.projects.empty:
        return DataQualityReport(total_rows=0, dataset=dataset, required_fields=required)
    imported = check_import(dataset)
    issues = check_required_columns(dataset, required)
    if issues:  # without required columns, row checks are pointless
        return DataQualityReport(total_rows=len(dataset.projects), dataset=dataset, issues=imported + issues,
                                 required_fields=required)
    issues = imported
    try:
        issues.extend(check_completeness(dataset, required))
    except Exception as exc:  # noqa: BLE001
        issues.append(DataQualityIssue("WARNING", "*", "check_completeness", f"Check aborted: {exc}", "Internal"))
    for check in PROJECT_CHECKS:
        try:
            issues.extend(check(dataset, as_of) if check in DATED_CHECKS else check(dataset))
        except Exception as exc:  # noqa: BLE001
            issues.append(DataQualityIssue("WARNING", "*", check.__name__, f"Check aborted: {exc}", "Internal"))
    # Only report findings for visible projects (dependencies can reference foreign IDs)
    visible = set(dataset.project_numbers) | {"*"}
    issues = [i for i in issues if i.project_id in visible or i.project_id.startswith("Row ")]
    return DataQualityReport(total_rows=len(dataset.projects), dataset=dataset, issues=issues,
                             required_fields=required, completeness_pct=completeness_ratio(dataset, required))
