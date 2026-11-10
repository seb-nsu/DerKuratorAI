"""Projects page logic shared by every front end: what the project detail page shows (key figures, master data, status
history, costs and baselines, benefits, deadlines, dependencies, risks), the master-data edit form (fields, ranges,
clamping, who may edit what) and saving it – non-governed fields are written directly, a change set with a governed
field goes to the PMO inbox as one proposal. Projects outside the reader's scope appear only as a neutral row (no id,
name or count)."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import pandas as pd

from app.core import clock, logs
from app.core.formatting import eur
from app.core.proposals import ProposalError, submit_proposal
from app.core.security import ROLE_PMO, apply_rls_to_dataset, is_governed_change, log_security_event
from app.data_adapters.dataset import BASE_CURRENCY, PortfolioDataset
from app.data_adapters.project_edit import (
    EDITABLE_FIELDS,
    access_level,
    diff_and_validate,
    field_editable,
    is_missing,
    missing_required,
    split_governed,
)
from app.views.exports import RESTRICTED_LABEL, project_names
from app.views.palette import SERIES_COLOR

logger = logging.getLogger("derkurator.views.projects")





RESTRICTED = RESTRICTED_LABEL  # projects outside the scope: one row per direction, no id, no count
RESTRICTED_NOTE = "Outside your scope – the PMO coordinates."
CLOSED_RISK_STATES = {"Closed", "Retired", "Resolved"}


def to_number(value: Any) -> float | None:
    """A number, or None for empty / non-numeric values."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(number) else number


def to_day(value: Any) -> date | None:
    """A calendar day, or None for empty / unparsable values."""
    ts = pd.to_datetime(value, errors="coerce")
    return None if pd.isna(ts) else ts.date()


def master_data_frames(project: dict[str, Any]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Read-only master data of the project detail page: (freely editable fields, governed fields that need PMO approval)."""
    def shown(field: str) -> str:
        value, kind = project.get(field), EDITABLE_FIELDS[field].kind
        if value is None or (isinstance(value, float) and pd.isna(value)) or str(value).strip() in ("", "NaT", "nan"):
            return "–"
        if kind == "number":
            return eur(to_number(value))
        if kind == "percent":
            return f"{to_number(value) or 0:.0f} %"
        if kind == "date":
            day = to_day(value)
            return day.isoformat() if day else "–"
        return str(value)

    rows = [{"Field": spec.label, "Value": shown(f), "_governed": is_governed_change({"field": f})}
            for f, spec in EDITABLE_FIELDS.items() if f != "business_case"]
    frame = pd.DataFrame(rows, columns=["Field", "Value", "_governed"])
    return (frame[~frame["_governed"]].drop(columns="_governed").reset_index(drop=True),
            frame[frame["_governed"]].drop(columns="_governed").reset_index(drop=True))


def cockpit_deadlines(pid: str, project: dict[str, Any], ds: PortfolioDataset, today: date | None = None) -> pd.DataFrame:
    today = today or clock.today()
    visible = set(ds.project_numbers)
    rows = []

    def add(when, kind, item, status=""):
        d = to_day(when)
        if d is not None:
            rows.append({"date": d.isoformat(), "days": (d - today).days, "type": kind, "item": item, "status": status})

    add(project.get("planned_end_date"), "Project end", "Planned end date")
    deps = ds.dependencies
    if not deps.empty and {"successor_project_number", "predecessor_project_number"} <= set(deps.columns):
        for d in deps.to_dict(orient="records"):
            succ, pred = str(d.get("successor_project_number")), str(d.get("predecessor_project_number"))
            if pred == pid:
                add(d.get("required_by_date"), "Delivers to", succ if succ in visible else RESTRICTED, str(d.get("status") or ""))
            elif succ == pid:
                add(d.get("required_by_date"), "Needs from", pred if pred in visible else RESTRICTED, str(d.get("status") or ""))
    risks = ds.risks
    if not risks.empty and "due_date" in risks.columns:
        for r in risks[risks["project_number"].astype(str) == pid].to_dict(orient="records"):
            if str(r.get("risk_status")) not in CLOSED_RISK_STATES:
                add(r.get("due_date"), "Risk due", str(r.get("short_description") or r.get("risk_number")), str(r.get("impact") or ""))
    plans = ds.benefit_plans
    if not plans.empty and "realization_start_date" in plans.columns:
        for b in plans[plans["project_number"].astype(str) == pid].to_dict(orient="records"):
            add(b.get("realization_start_date"), "Benefit starts", f"{b.get('benefit_type')} {eur(to_number(b.get('planned_benefit_eur')))}")
    frame = pd.DataFrame(rows, columns=["date", "days", "type", "item", "status"]).sort_values("date")
    # restricted dependencies: only the earliest date per direction, so their number stays hidden
    hidden = frame["item"] == RESTRICTED
    frame = pd.concat([frame[~hidden], frame[hidden].drop_duplicates("type")]).sort_values("date", kind="stable")
    return frame.reset_index(drop=True)


def cockpit_dependencies(pid: str, ds: PortfolioDataset) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(incoming: projects this one waits for, outgoing: projects waiting for this one)."""
    cols = ["project", "type", "status", "required_by", "description"]
    deps, visible = ds.dependencies, set(ds.project_numbers)
    if deps.empty or not {"successor_project_number", "predecessor_project_number"} <= set(deps.columns):
        return pd.DataFrame(columns=cols), pd.DataFrame(columns=cols)
    names = project_names(ds)

    def rows(frame, other_col):
        out, restricted = [], []
        for d in frame.to_dict(orient="records"):
            other = str(d.get(other_col))
            when = to_day(d.get("required_by_date"))
            if other not in visible:  # collected into one neutral row below
                restricted.append(when)
                continue
            out.append({"project": f"{other} – {names.get(other, '')}", "type": d.get("dependency_type"),
                        "status": d.get("status"), "required_by": when.isoformat() if when else "–",
                        "description": d.get("description")})
        if restricted:
            first = min((w for w in restricted if w), default=None)
            out.append({"project": RESTRICTED, "type": "–", "status": "–",
                        "required_by": first.isoformat() if first else "–", "description": RESTRICTED_NOTE})
        return pd.DataFrame(out, columns=cols)

    incoming = deps[deps["successor_project_number"].astype(str) == pid]
    outgoing = deps[deps["predecessor_project_number"].astype(str) == pid]
    return rows(incoming, "predecessor_project_number"), rows(outgoing, "successor_project_number")


def cockpit_cost_curve(pid: str, ds: PortfolioDataset) -> pd.DataFrame:
    cp = ds.cost_plans
    if cp.empty or not {"month", "planned_cost_eur", "actual_cost_eur"} <= set(cp.columns):
        return pd.DataFrame(columns=["month", "Planned (cumulative)", "Actual (cumulative)"])
    mine = cp[cp["project_number"].astype(str) == pid].assign(
        planned=lambda d: pd.to_numeric(d["planned_cost_eur"], errors="coerce").fillna(0),
        actual=lambda d: pd.to_numeric(d["actual_cost_eur"], errors="coerce"))
    monthly = mine.groupby("month").agg(planned=("planned", "sum"), actual=("actual", "sum")).sort_index()
    monthly["actual"] = monthly["actual"].where(monthly["actual"] > 0)  # future months: no actuals yet
    return pd.DataFrame({"month": monthly.index, "Planned (cumulative)": monthly["planned"].cumsum().values,
                         "Actual (cumulative)": monthly["actual"].cumsum().values})


def baseline_frame(pid: str, project: dict[str, Any], ds: PortfolioDataset) -> pd.DataFrame:
    """Baseline versions of a project, oldest first, with the variance of each against version 1."""
    bl = ds.baselines
    mine = bl[bl["project_number"].astype(str) == pid].copy() if not bl.empty and "project_number" in bl.columns else bl
    if mine.empty:
        return pd.DataFrame()
    if "version" not in mine.columns:
        mine["version"] = range(1, len(mine) + 1)
    mine = mine.sort_values("version")
    first = mine.iloc[0]
    budget0 = to_number(first.get("baseline_budget_eur")) or to_number(project.get("budget_cost_eur"))
    rows = []
    for r in mine.to_dict(orient="records"):
        budget = to_number(r.get("baseline_budget_eur"))
        rows.append({"Version": int(to_number(r.get("version")) or 0), "Created": to_day(r.get("created_date")),
                     "Budget": eur(budget), "End": to_day(r.get("baseline_end_date")),
                     "Δ budget vs. v1": (f"{(budget / budget0 - 1) * 100:+.1f} %" if budget and budget0 else "–"),
                     "Reason": r.get("reason") or "", "Approved by": r.get("approved_by") or ""})
    return pd.DataFrame(rows)


def risk_exposure_frame(risks: pd.DataFrame) -> pd.DataFrame:
    """Risks with their exposure in € (probability × impact on the configured risk scale), highest first."""
    from app.services import risk_exposure

    if risks.empty:
        return risks
    out = risks.copy()
    out["exposure_eur"] = [risk_exposure(p, i) for p, i in zip(out.get("probability"), out.get("impact"), strict=True)]
    return out.sort_values("exposure_eur", ascending=False)



# =========================================================================== detail page sections
def key_figures(project: dict[str, Any], flag: str | None, today: date | None = None) -> dict[str, Any]:
    """The header of the detail page: budget, actual, EAC, progress and planned end with their context lines, and the
    data-quality flag ('Blocked' = blocked for simulation, another flag = data warnings, None = data ok)."""
    budget, actual, eac = (to_number(project.get(k)) for k in ("budget_cost_eur", "actual_cost_eur",
                                                           "estimate_at_completion_eur"))
    end = to_day(project.get("planned_end_date"))
    return {"budget": budget, "actual": actual, "eac": eac, "progress": to_number(project.get("percent_complete")) or 0,
            "planned_end": end,
            "actual_pct_of_budget": actual / budget * 100 if budget and actual is not None else None,
            "eac_vs_budget": eac - budget if eac is not None and budget else None,
            "days_to_end": (end - (today or clock.today())).days if end else None,
            "data_quality": "blocked" if flag == "Blocked" else "warnings" if flag else "ok",
            "rag": project.get("status"), "state": project.get("state"), "execution_type": project.get("execution_type"),
            "phase": project.get("phase")}


def status_history(pid: str, ds: PortfolioDataset) -> pd.DataFrame:
    """Status reports of the project, newest first."""
    sr = ds.status_reports
    mine = sr[sr["project_number"].astype(str) == pid] if not sr.empty and "project_number" in sr.columns else sr
    if mine.empty:
        return pd.DataFrame()
    cols = [c for c in ("status_date", "overall_health", "schedule", "cost", "scope", "resources",
                        "issues_decisions") if c in mine.columns]
    table = mine.sort_values("status_date", ascending=False)[cols].copy()
    table["status_date"] = pd.to_datetime(table["status_date"], errors="coerce").dt.date
    return table


RISK_COLUMNS = ("risk_number", "short_description", "impact", "probability", "exposure_eur", "risk_status",
                "systemic_risk_cluster", "due_date", "mitigation")


def project_risks(pid: str, ds: PortfolioDataset) -> pd.DataFrame:
    """The project's risks with their exposure, highest first, in the columns of the Risks tab."""
    rk = ds.risks
    mine = risk_exposure_frame(rk[rk["project_number"].astype(str) == pid] if not rk.empty else rk)
    if mine.empty:
        return mine
    return mine[[c for c in RISK_COLUMNS if c in mine.columns]]


def cost_curve_figure(curve: pd.DataFrame, budget: float | None, eac: float | None):
    """Cumulative planned and actual cost with the budget and EAC lines (the Costs tab chart)."""
    import plotly.express as px

    fig = px.line(curve, x="month", y=["Planned (cumulative)", "Actual (cumulative)"], markers=True,
                  labels={"value": "€", "month": "", "variable": ""},
                  color_discrete_sequence=["#8a8a86", SERIES_COLOR])
    if budget:
        fig.add_hline(y=budget, line_dash="dash", line_color="#1F3A5F", annotation_text="Budget",
                      annotation_position="top left")
    if eac:
        fig.add_hline(y=eac, line_dash="dot", line_color="#5f666e", annotation_text="EAC",
                      annotation_position="bottom right")
    fig.update_layout(height=320, margin=dict(l=10, r=10, t=10, b=10), legend=dict(orientation="h", y=1.1))
    return fig


def cost_curve_caption(curve: pd.DataFrame) -> str | None:
    """The cost chart in one sentence (text alternative)."""
    last = curve.dropna(subset=["Actual (cumulative)"]).tail(1)
    if last.empty:
        return None
    row = last.iloc[0]
    return (f"Actual cost to {row['month']}: {eur(float(row['Actual (cumulative)']))} against "
            f"{eur(float(row['Planned (cumulative)']))} planned by then · planned in total "
            f"{eur(float(curve['Planned (cumulative)'].iloc[-1]))}.")


# =========================================================================== benefits
def rows_of(table: pd.DataFrame, pid: str) -> pd.DataFrame:
    if table.empty or "project_number" not in table.columns:
        return table
    return table[table["project_number"].astype(str) == pid]


def benefit_frame(pid: str, project: dict[str, Any], ds: PortfolioDataset) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Planned tranches against the actual benefits (table of the Benefits tab) and the realisation result."""
    from app.services import benefit_realisation

    result = benefit_realisation(rows_of(ds.benefit_plans, pid), rows_of(ds.benefit_actuals, pid), project,
                                 clock.today())
    frame = pd.DataFrame([{
        "Tranche": t["plan"] or "–", "Type": t["type"], "Due": t["due"].isoformat(), "Planned": eur(t["planned"]),
        "Actual": eur(t["actual"]),
        "Status": ("not due yet" if not t["is_due"] else
                   f"{t['actual'] / t['planned'] * 100:.0f} % realised" if t["actual"] > 0 else "due – no actual yet"),
    } for t in result["tranches"]])
    return frame, result


def realisation_colour(pct: float, rules: Any) -> str:
    """red below the NO-GO threshold, orange below the minimum, else green."""
    return ("red" if rules.benefit_realization_nogo_pct and pct < rules.benefit_realization_nogo_pct
            else "orange" if pct < rules.benefit_realization_min_pct else "green")


def can_report_benefit(role: str, user_id: str, project: dict[str, Any]) -> bool:
    """The owning Project Manager and the PMO report actual benefits."""
    return access_level(role, user_id, project) == "edit" or role == ROLE_PMO


def benefit_plan_options(pid: str, ds: PortfolioDataset) -> list[str]:
    plans = rows_of(ds.benefit_plans, pid)
    return [""] + (list(plans["benefit_plan_number"].astype(str)) if "benefit_plan_number" in plans.columns else [])


def benefit_currencies(ds: PortfolioDataset) -> list[str]:
    rates = sorted(set(ds.exchange_rates["currency"].astype(str).str.upper())) \
        if not ds.exchange_rates.empty and "currency" in ds.exchange_rates.columns else []
    return [BASE_CURRENCY, *[r for r in rates if r != BASE_CURRENCY]]


def next_benefit_number(pid: str, ds: PortfolioDataset) -> str:
    existing = set(ds.benefit_actuals["benefit_actual_number"].astype(str)) \
        if not ds.benefit_actuals.empty and "benefit_actual_number" in ds.benefit_actuals.columns else set()
    n = 1
    while f"BAC-{pid}-{n:03d}" in existing:
        n += 1
    return f"BAC-{pid}-{n:03d}"


def benefit_actual_change(pid: str, ds: PortfolioDataset, user_id: str, plan: str | None, month: date | None,
                          amount: float, currency: str | None, comment: str | None) -> dict[str, Any]:
    """An actual benefit as an appended Benefit_Actuals row (no proposal needed, like a status report)."""
    month = month or clock.today()
    row = {"benefit_actual_number": next_benefit_number(pid, ds), "project_number": pid,
           "benefit_plan_number": plan or None, "month": month.replace(day=1).isoformat(),
           "actual_benefit_eur": amount, "currency": currency or BASE_CURRENCY, "reported_by": user_id,
           "comment": (comment or "").strip() or None}
    return {"op": "append", "table": "Benefit_Actuals", "project_id": pid, "field": "Benefit_Actuals", "row": row,
            "source_tool": "report_benefit"}


def report_benefit(change: dict[str, Any], role: str, user_id: str) -> tuple[str, str, Any]:
    """Writes an actual benefit. Returns (kind, message, write result or None)."""
    from app.data_adapters import DataConflictError, DataSourceError, get_data_provider
    from app.data_adapters.base import WriteNotAuthorizedError

    row = change["row"]
    if float(row.get("actual_benefit_eur") or 0) <= 0:
        return "error", "Please enter the actual benefit (> 0).", None
    try:
        result = get_data_provider().apply_changes([change], user=user_id, reason="benefit_actual", role=role)
    except (WriteNotAuthorizedError, DataConflictError, DataSourceError) as exc:
        return "error", f"Actual benefit not saved: {exc}", None
    return "success", (f"Actual benefit {row['benefit_actual_number']} of {eur(row['actual_benefit_eur'])} "
                       f"{row['currency']} saved (snapshot {result.snapshot_id})."), result


# =========================================================================== master-data edit form
DATE_BOUNDS = (date(2000, 1, 1), date(2100, 12, 31))


def clamp(value, low, high):
    """(value inside [low, high], was_clamped). An input refuses out-of-range values instead of clamping them."""
    clamped = value if low is None else max(value, low)
    clamped = clamped if high is None else min(clamped, high)
    return clamped, clamped != value


def field_range(field_name: str) -> tuple[Any, Any, Any]:
    """(min, max, step) of a field's input; None where there is no bound."""
    kind = EDITABLE_FIELDS[field_name].kind
    if kind == "date":
        return DATE_BOUNDS[0], DATE_BOUNDS[1], None
    if kind == "int":  # the only whole-number field is the risk score
        return 1, 5, 1
    if kind in ("number", "percent"):
        return 0.0, (100.0 if kind == "percent" else None), (1.0 if kind == "percent" else 1000.0)
    return None, None, None


def select_options(field_name: str, value: Any) -> list[str]:
    """The options of a select field; a stored value outside them stays selectable (shown as it is)."""
    spec = EDITABLE_FIELDS[field_name]
    return list(spec.options) + ([str(value)] if not is_missing(value) and str(value) not in spec.options else [])


def display_value(field_name: str, value: Any) -> tuple[Any, Any]:
    """(value the input shows, stored original if it lay outside the input's range and was clamped, else None)."""
    kind = EDITABLE_FIELDS[field_name].kind
    if is_missing(value):
        return None, None
    if kind in ("select", "text", "textarea"):
        return str(value), None
    low, high, _step = field_range(field_name)
    if kind == "date":
        ts = pd.to_datetime(value, errors="coerce")
        if ts is None or pd.isna(ts):
            return None, None
        shown = ts.date()
    elif kind == "int":
        shown = int(round(float(value)))
    else:
        shown = float(value)
    clamped, was_clamped = clamp(shown, low, high)
    return clamped, (shown if was_clamped else None)


def form_sections() -> dict[str, list[str]]:
    """Two sections, two buttons: 'direct' is saved at once (with the business case), 'governed' goes to the PMO as a
    proposal – what is saved directly never waits for a governed change, and vice versa."""
    return {"direct": [f for f in EDITABLE_FIELDS if f != "business_case" and not is_governed_change({"field": f})]
            + ["business_case"],
            "governed": [f for f in EDITABLE_FIELDS if is_governed_change({"field": f})]}


def _json_value(value: Any) -> Any:
    return value.isoformat() if isinstance(value, date) else value


def edit_form(project: dict[str, Any], role: str, user_id: str) -> dict[str, Any]:
    """The edit form of the detail page: per section its fields with label, kind, options, range, the value shown,
    a clamped stored value, whether it is required and missing, and whether the caller may change it."""
    access = access_level(role, user_id, project)
    missing = set(missing_required(project))
    sections = {}
    for name, fields in form_sections().items():
        rows = []
        for f in fields:
            spec = EDITABLE_FIELDS[f]
            shown, original = display_value(f, project.get(f))
            low, high, step = field_range(f)
            editable = access == "edit" and (f == "business_case" or field_editable(f, project, role, user_id))
            rows.append({"field": f, "label": spec.label, "kind": spec.kind,
                         "options": select_options(f, project.get(f)) if spec.kind == "select" else None,
                         "min": _json_value(low), "max": _json_value(high), "step": step,
                         "value": _json_value(shown), "stored_out_of_range": _json_value(original),
                         "missing": f in missing, "editable": editable,
                         "fill_only_while_empty": spec.governed})
        sections[name] = rows
    return {"access": access, "sections": sections,
            "missing_required": [EDITABLE_FIELDS[f].label if f in EDITABLE_FIELDS else f for f in sorted(missing)]}


def _shown_unchanged(field_name: str, sent: Any, shown: Any) -> bool:
    try:
        if EDITABLE_FIELDS[field_name].kind == "date":
            ts = pd.to_datetime(sent, errors="coerce")
            return not pd.isna(ts) and ts.date() == shown
        return float(sent) == float(shown)
    except (TypeError, ValueError):
        return False


def section_edits(project: dict[str, Any], section: str, values: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """The edits of one form section from the values a client sent: the section's fields take the sent value, every
    other field keeps its stored value (like the page, whose two buttons each save one section). A clamped field
    sent back with its shown value keeps its stored value (never a silent write of the display value). Returns
    (edits, errors)."""
    fields = form_sections().get(section)
    if fields is None:
        return {}, [f"Unknown section '{section}'."]
    errors = [f"{f}: not a field of this section." for f in values if f not in fields]
    edits = {f: project.get(f) for f in EDITABLE_FIELDS}
    for f, sent in values.items():
        if f not in fields:
            continue
        spec = EDITABLE_FIELDS[f]
        _shown, original = display_value(f, project.get(f))
        if original is not None and not is_missing(sent) and _shown_unchanged(f, sent, _shown):
            continue  # untouched clamped field
        if spec.kind == "date" and not is_missing(sent):
            ts = pd.to_datetime(sent, errors="coerce")
            if pd.isna(ts):
                errors.append(f"{spec.label}: invalid value '{sent}'.")
                continue
            if not DATE_BOUNDS[0] <= ts.date() <= DATE_BOUNDS[1]:
                errors.append(f"{spec.label}: must be between {DATE_BOUNDS[0]} and {DATE_BOUNDS[1]}.")
                continue
        edits[f] = sent
    return edits, errors


@dataclass(frozen=True)
class SaveOutcome:
    """`kind`: success | info | error; `reason` of an error: validation | permission | proposal | data_source |
    system. `written`: the data source changed."""
    kind: str
    message: str
    reason: str = ""
    errors: list[str] = field(default_factory=list)
    written: bool = False
    proposal_id: str | None = None
    applied: int | None = None
    snapshot_id: str | None = None


def propose_master_data(pid: str, changes: list[dict], role: str, user_id: str) -> tuple[dict, str]:
    """A master-data edit that touches a governed field (state, planned end, EAC, actuals, benefit, budgets) is never
    written directly – the whole change set goes to the PMO inbox as one proposal. Returns (proposal, message).
    Raises ProposalError."""
    labels = [EDITABLE_FIELDS[c["field"]].label if c["field"] in EDITABLE_FIELDS else c["field"] for c in changes]
    paper = (f"# Master data change {pid}\n\nRequested by {user_id} on the project detail page.\n\n"
             "| Field | Old | New |\n|---|---|---|\n"
             + "\n".join(f"| {label} | {c.get('old')} | {c.get('new')} |" for label, c in zip(labels, changes, strict=True)))
    entry = submit_proposal(user_id, role, f"Master data change {pid}: {', '.join(labels)}", paper, None, changes)
    from app.approval_flow import audit

    audit("proposal_submitted", role=role, user_id=user_id, proposal_id=entry["id"], request=entry["request"],
          changes=changes)
    log_security_event("Change proposal submitted", role=role, user=user_id, proposal=entry["id"], changes=len(changes))
    return entry, (f"{len(changes)} change(s) of {pid} ({', '.join(labels)}) submitted to the PMO as proposal "
                   f"{entry['id']} – they are written after approval.")


def save_master_data(pid: str, project: dict[str, Any], edits: dict[str, Any], role: str, user_id: str,
                     dataset: PortfolioDataset) -> SaveOutcome:
    """Saves the edits of the detail page: validated against the field rules, re-checked against the caller's scope
    in `dataset` (the whole current portfolio – never trust the rendered form alone). A change set with a governed
    field goes to the PMO inbox as one proposal; otherwise it is written with a snapshot, audited, and the dependency
    graph is rebuilt."""
    from app.approval_flow import audit
    from app.data_adapters import DataSourceError, get_data_provider

    changes, errors = diff_and_validate(project, edits, role, user_id)
    if errors:
        return SaveOutcome("error", "Changes not saved:\n\n" + "\n".join(f"- {e}" for e in errors), "validation",
                           errors=errors)
    if not changes:
        return SaveOutcome("info", "No changes to save.")
    fresh = apply_rls_to_dataset(dataset, role, user_id)
    if pid not in fresh.project_numbers:
        return SaveOutcome("error", "No permission for this project.", "permission")
    _direct, governed = split_governed(changes)
    if governed:  # if any governed field changes, the whole change set goes to the PMO inbox as one proposal
        try:
            entry, note = propose_master_data(pid, changes, role, user_id)
        except ProposalError as exc:
            return SaveOutcome("error", f"Proposal not submitted – nothing was saved: {exc}", "proposal")
        return SaveOutcome("success", note, proposal_id=entry["id"])
    try:
        result = get_data_provider().apply_changes(changes, user=user_id, reason="project_edit", role=role)
    except DataSourceError as exc:  # curated business message (conflict, permission, audit) – no internals
        logger.error("Project save failed for %s: %s", pid, exc)
        return SaveOutcome("error", f"Changes not saved: {exc}", "data_source")
    except Exception:  # raw exception text never reaches business users
        logger.exception("Project save failed unexpectedly")
        return SaveOutcome("error", "Changes could not be saved due to a system error. Please contact your System "
                                    "Admin.", "system")
    audit("project_edited", role=role, user_id=user_id, project_id=pid, changes=changes, applied=result.applied,
          snapshot_id=result.snapshot_id)
    for change in changes:  # dedicated business trail: "User X changed field Y from NULL to Z on Project ID 123"
        logs.append_business_audit(user_id, pid, change["field"], change.get("old"), change.get("new"))
    log_security_event("Project master data edited", user=user_id, project=pid,
                       fields=",".join(c["field"] for c in changes))
    note = ""
    try:
        from app.graph.portfolio_graph import rebuild

        rebuild()  # synchronous: the next analysis sees the saved data (a rebuild takes well under a second)
    except Exception as exc:  # noqa: BLE001 - data is saved; the graph also rebuilds itself on next access
        logger.warning("Graph update after project edit failed: %s", exc)
        note = " The dependency graph will be refreshed automatically on next access."
    return SaveOutcome("success", f"{result.applied} field(s) of {pid} saved and dependency graph updated.{note}",
                       written=True, applied=result.applied, snapshot_id=result.snapshot_id)
