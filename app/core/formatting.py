"""One way to write numbers and data fields for business readers – screen tables, metrics, dialogs, the decision
paper and the exports all use it: business labels instead of field names, € with thousands separators, percentages
and their changes in percentage points, no raw floats."""
from __future__ import annotations

import math
import re
from typing import Any

# Labels of fields that are not editable on the project page (the editable ones come from EDITABLE_FIELDS)
_EXTRA_LABELS = {
    "total_planned_cost_eur": "Total planned cost (€)",
    "planned_roi_pct": "Planned ROI (%)",
    "planned_return_eur": "Planned return (€)",
    "allocated_fte": "Allocated FTE",
    "project_manager": "Project manager",
    "estimate_to_completion_eur": "Estimate to complete (€)",
    "net_present_value_eur": "Net present value (€)",
    "approved_end_date": "Approved end date",
    "risk_status": "Risk response",
    "mitigation": "Mitigation",
    "probability": "Probability",
    "skill_fte": "Skill FTE",
    "Baselines": "New baseline version",
    "Approvals": "Stage-gate approval",
    "Reserve_Ledger": "Management reserve booking",
}
# Tool result headers ("RESULT simulate_budget_change | …") as business titles
TOOL_TITLES = {
    "simulate_budget_change": "Budget change", "simulate_budget_transfer": "Budget transfer",
    "simulate_resource_transfer": "Resource transfer", "simulate_timeline_shift": "Schedule shift",
    "simulate_agile_vs_waterfall_shift": "Schedule forecast", "simulate_project_cancellation": "Project stop",
    "simulate_unstructured_scenario": "Qualitative estimate", "simulate_on_hold": "Project on hold",
    "simulate_portfolio_cut": "Portfolio cut", "get_downstream_impact": "Dependent projects",
    "get_upstream_bottlenecks": "Predecessors", "find_shared_resources": "Shared teams",
    "detect_cluster_risk": "Risk clusters", "identify_team_bottlenecks": "Team bottlenecks",
    "get_task_level_impact": "Task impact", "simulate_resume": "Resume after hold",
    "simulate_cost_stress": "Cost stress test", "simulate_rebaseline": "Re-baseline",
    "simulate_stage_gate": "Stage gate", "simulate_reserve_draw": "Management reserve draw",
    "simulate_reserve_return": "Management reserve return", "simulate_risk_response": "Risk response",
    "simulate_skill_transfer": "Skill transfer", "analyse_skill_capacity": "Skill capacity",
    "simulate_benefit_review": "Benefit realisation",
    "simulate_merge": "Project merge",
}
_RESULT_RE = re.compile(r"RESULT (" + "|".join(TOOL_TITLES) + r") \| ")
_TOOL_RE = re.compile(r"\b(" + "|".join(TOOL_TITLES) + r")\b")
_VIA_TOOL_RE = re.compile(r" via (?:" + "|".join(TOOL_TITLES) + r")\b")  # a hint for the agents, not for readers


def field_labels() -> dict[str, str]:
    from app.data_adapters.project_edit import EDITABLE_FIELDS  # local: keeps this module free of the adapters

    return {**{name: spec.label for name, spec in EDITABLE_FIELDS.items()}, **_EXTRA_LABELS}


def field_label(field: str) -> str:
    return field_labels().get(field) or str(field).replace("_", " ").capitalize()


def rule_label(rule: str) -> str:
    """A Data Steward rule for readers: the profile check's internal id becomes "Data profile"."""
    text = str(rule or "")
    return "Data profile" if text.startswith("DQ-Profile:") else text or "–"


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(number) else number


def eur(value: float | None, signed: bool = False) -> str:
    """Whole euros with thousands separators: "€420,000"; `signed` adds "+" to gains; None is "–"."""
    if value is None:
        return "–"
    sign = "+" if signed and value > 0 else "−" if value < 0 else ""
    return f"{sign}€{abs(value):,.0f}"


def eur_m(value: float, digits: int = 2) -> str:
    """Millions for key figures: "€9.92 M"."""
    return f"{'−' if value < 0 else ''}€{abs(value) / 1e6:,.{digits}f} M"


def pct(value: float, digits: int = 0) -> str:
    """One percent style everywhere: "85 %"."""
    return f"{value:,.{digits}f} %"


def day(value: object) -> str:
    """A date without time: "2026-10-02"."""
    return str(value or "")[:10] or "–"


def format_value(field: str, value: Any) -> str:
    """A field value for display: € amounts, percentages, FTE; text and dates unchanged; empty as "–"."""
    number = _number(value)
    if number is None:
        return "–" if value is None or (isinstance(value, float) and math.isnan(value)) or value == "" else str(value)
    if field.endswith("_eur"):
        return eur(number)
    if field.endswith("_pct"):
        return f"{number:,.1f} %"
    if "fte" in field:
        return f"{number:,.1f} FTE"
    return f"{number:,.0f}" if abs(number) >= 1000 else f"{number:g}"


def format_delta(field: str, old: Any, new: Any) -> str:
    """The change between two values: +€/−€, ± pp for percentages, ± FTE; "–" when not numeric."""
    a, b = _number(old), _number(new)
    if a is None or b is None:
        return "–"
    diff = b - a
    if field.endswith("_eur"):
        return eur(diff, signed=True)
    sign = "+" if diff > 0 else "−" if diff < 0 else ""
    if field.endswith("_pct"):
        return f"{sign}{abs(diff):,.1f} pp"
    if "fte" in field:
        return f"{sign}{abs(diff):,.1f} FTE"
    return f"{sign}{abs(diff):,.0f}" if abs(diff) >= 1000 else f"{sign}{abs(diff):g}"


def timestamp(value: object) -> str:
    """One timestamp format everywhere: "2026-10-02 18:40" (ISO date, minutes, no seconds)."""
    text = str(value or "").replace("T", " ")
    return text[:16] if text else "–"


# Text symbols for tables (dataframes cannot render badges): shape + word, never colour alone
STATUS_SYMBOLS = {"GO": "✓", "CONDITIONAL": "▲", "NO-GO": "■", "INCOMPLETE": "?", "NONE": "–",
                  "Green": "●", "Yellow": "▲", "Red": "■"}


def status_text(label) -> str:
    """A verdict or RAG value for a table cell with a shape symbol: "■ NO-GO", "▲ Yellow"."""
    from app.views.steerco_pack import verdict_key  # local: keeps this module free of the views

    text = str(label or "–")
    if text in STATUS_SYMBOLS:
        return f"{STATUS_SYMBOLS[text]} {text}"
    if text in ("–", "", "None"):
        return "–"
    return f"{STATUS_SYMBOLS[verdict_key(text)]} {text}"


def business_text(text: str) -> str:
    """Tool result headers and tool names in a paper as business titles."""
    text = _VIA_TOOL_RE.sub("", text or "")
    text = _RESULT_RE.sub(lambda m: f"{TOOL_TITLES[m.group(1)]} | ", text)
    return _TOOL_RE.sub(lambda m: TOOL_TITLES[m.group(1)].lower(), text)
