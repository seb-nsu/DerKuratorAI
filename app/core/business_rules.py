"""Governance configuration: the organisation's business assumptions and verdict thresholds.

Stored per environment in data/env_<env>.json under "business_rules" (validated, bounded), maintained in the
Admin Console ("📐 Business rules") and read at call time by the simulation/graph tools, the Data Steward and the
verdict engine – no business constant is hard-coded in the tools any more. The same values are injected into the
agents' prompts as "Business assumptions" so that explanations use the organisation's rates.
"""
from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

__all__ = ["BusinessRules", "ValidationError", "business_assumptions_md", "current_rules", "RULE_LABELS"]


class BusinessRules(BaseModel):
    """Immutable, validated business rules. Defaults match the values used before the rules became configurable."""
    model_config = ConfigDict(extra="ignore", frozen=True)

    # --- simulation assumptions
    fte_monthly_cost_eur: float = Field(12_500.0, ge=0, le=1_000_000)
    exit_cost_rate_pct: float = Field(8.0, ge=0, le=100)
    sprint_length_days: int = Field(14, ge=1, le=60)
    delay_cost_factor_pct: float = Field(100.0, ge=0, le=300)
    # --- guardrails
    max_budget_change_pct: float = Field(100.0, ge=1, le=1_000)
    max_timeline_shift_months: int = Field(36, ge=1, le=240)
    # --- capacity
    team_capacity_limit_pct: float = Field(100.0, ge=50, le=200)
    overload_alert_pct: float = Field(110.0, ge=50, le=300)
    # --- deep simulation
    default_discount_rate_pct: float = Field(7.5, ge=0, le=50)
    hold_retention_pct: float = Field(20.0, ge=0, le=100)
    hold_restart_ramp_months: float = Field(1.0, ge=0, le=12)
    max_hold_months: int = Field(24, ge=1, le=60)
    cut_protected_max_priority: int = Field(1, ge=0, le=5)
    cancel_conditional_sunk_pct: float = Field(25.0, ge=0, le=100)
    cancel_conditional_benefit_eur: float = Field(1_000_000.0, ge=0, le=1e12)
    # --- verdict engine
    domino_conditional_threshold: int = Field(2, ge=1, le=100)
    eac_overrun_nogo_pct: float = Field(15.0, ge=0, le=100)
    board_approval_threshold_eur: float = Field(500_000.0, ge=0, le=1e12)
    overrun_tolerance_eur: float = Field(1_000.0, ge=0, le=1e9)
    condition_due_days: int = Field(14, ge=1, le=365)
    # --- re-baselining
    rebaseline_board_threshold_pct: float = Field(20.0, ge=0, le=500)
    max_rebaselines_per_year: int = Field(2, ge=0, le=24)
    # --- management reserve
    management_reserve_eur: float = Field(1_500_000.0, ge=0, le=1e12)
    reserve_floor_pct: float = Field(20.0, ge=0, le=100)
    # --- risk response (exposure = probability × impact)
    risk_probability_low_pct: float = Field(20.0, ge=0, le=100)
    risk_probability_moderate_pct: float = Field(50.0, ge=0, le=100)
    risk_probability_high_pct: float = Field(80.0, ge=0, le=100)
    risk_impact_low_eur: float = Field(25_000.0, ge=0, le=1e12)
    risk_impact_moderate_eur: float = Field(100_000.0, ge=0, le=1e12)
    risk_impact_high_eur: float = Field(250_000.0, ge=0, le=1e12)
    risk_response_min_benefit_ratio: float = Field(1.0, ge=0, le=100)
    # --- benefit realisation
    benefit_realization_min_pct: float = Field(80.0, ge=0, le=200)
    benefit_realization_nogo_pct: float = Field(50.0, ge=0, le=200)
    benefit_report_grace_days: int = Field(90, ge=0, le=730)
    # --- approval governance
    approval_sla_days: int = Field(5, ge=1, le=90)
    approval_expiry_days: int = Field(30, ge=0, le=365)
    delegation_max_days: int = Field(30, ge=1, le=365)
    # --- annual planning
    planning_preview_alert_pct: float = Field(90.0, ge=0, le=200)
    planning_run_cost_alert_pct: float = Field(10.0, ge=0, le=100)
    planning_score_max_age_days: int = Field(180, ge=1, le=3650)
    # --- status check of the sources (reported traffic light against the numbers)
    status_cost_yellow_pct: float = Field(5.0, ge=0, le=100)
    status_cost_red_pct: float = Field(10.0, ge=0, le=100)
    status_burn_gap_yellow_pct: float = Field(15.0, ge=0, le=100)
    status_burn_gap_red_pct: float = Field(30.0, ge=0, le=100)
    status_slip_yellow_days: int = Field(20, ge=0, le=730)
    status_slip_red_days: int = Field(60, ge=0, le=730)
    status_report_max_age_days: int = Field(45, ge=1, le=365)
    # --- forecast ranges (Monte Carlo and reference classes)
    simulation_runs: int = Field(2000, ge=100, le=20_000)
    confidence_level_pct: float = Field(80.0, ge=50, le=99)
    reference_class_min_projects: int = Field(5, ge=2, le=100)

    @model_validator(mode="after")
    def _limits_fit_together(self) -> "BusinessRules":
        # a hold moves the end date by its duration plus the restart ramp: it never goes further than a direct shift
        if self.max_hold_months + self.hold_restart_ramp_months > self.max_timeline_shift_months:
            raise ValueError(f"Max. hold duration ({self.max_hold_months}) plus restart ramp-up "
                             f"({self.hold_restart_ramp_months:g}) must not exceed the max. schedule shift "
                             f"({self.max_timeline_shift_months} months).")
        for low, high in (("status_cost_yellow_pct", "status_cost_red_pct"),
                          ("status_burn_gap_yellow_pct", "status_burn_gap_red_pct"),
                          ("status_slip_yellow_days", "status_slip_red_days")):
            if getattr(self, low) > getattr(self, high):
                raise ValueError(f"The yellow threshold of the status check ({low}) must not be above the red one.")
        if self.benefit_realization_nogo_pct > self.benefit_realization_min_pct:
            raise ValueError("The NO-GO threshold of the benefit realisation must not be above the corrective-plan "
                             "threshold.")
        return self

    def merged(self, changes: dict[str, Any]) -> "BusinessRules":
        """Validated copy with changes applied (raises pydantic.ValidationError)."""
        return BusinessRules.model_validate({**self.model_dump(), **changes})


# (label, unit, help text) per rule: single source for the Admin Console form and the assumptions text
RULE_LABELS: dict[str, tuple[str, str, str]] = {
    "fte_monthly_cost_eur": ("Blended cost per FTE-month", "€", "Used for burn rates, cost of delay and capacity cost effects."),
    "exit_cost_rate_pct": ("Exit / contract cost when stopping a project", "% of remaining budget", "Cancellation scenario."),
    "sprint_length_days": ("Sprint length", "days", "Velocity and agile schedule forecasts."),
    "delay_cost_factor_pct": ("Cost of delay", "% of the monthly burn rate", "A delay costs this share of the burn rate per month (100 = the team stays fully on board)."),
    "max_budget_change_pct": ("Max. budget change per scenario", "%", "Guardrail: larger percentage changes are rejected."),
    "max_timeline_shift_months": ("Max. schedule shift per scenario", "months", "Guardrail for schedule shifts."),
    "team_capacity_limit_pct": ("Team capacity limit", "% utilisation", "Above this a team counts as overloaded (graph, Data Steward, charts)."),
    "overload_alert_pct": ("Overload alert after a resource transfer", "% utilisation", "Transfers pushing a team above this are flagged."),
    "default_discount_rate_pct": ("Default discount rate for NPV", "% p.a.", "Used when a project has no discount_rate_pct."),
    "hold_retention_pct": ("Capacity kept while a project is on hold", "% of current FTE", "Standby team that keeps know-how (cost continues)."),
    "max_hold_months": ("Max. hold duration", "months", "Longer pauses are rejected – consider a cancellation instead. Together with the restart ramp-up at most the max. schedule shift."),
    "hold_restart_ramp_months": ("Restart ramp-up after a hold", "months", "Added to the hold duration; costs one monthly burn per month."),
    "cut_protected_max_priority": ("Projects protected from the portfolio cut list up to priority", "(1 = highest, 0 = none)", "These projects are never proposed for a cut."),
    "cancel_conditional_sunk_pct": ("Project stop needs sponsor confirmation from sunk costs of", "% of budget", "A stop with sunk costs at or above this, or with a hard dependent, is a CONDITIONAL GO (impairment to book)."),
    "cancel_conditional_benefit_eur": ("Project stop needs sponsor confirmation from planned benefit of", "€ given up (0 = off)", "A stop that gives up this much planned benefit or more is a CONDITIONAL GO; the exit costs are named in the condition."),
    "domino_conditional_threshold": ("Domino threshold for CONDITIONAL GO", "dependent projects", "Verdict engine."),
    "eac_overrun_nogo_pct": ("Forecast overrun that means NO-GO", "% of new budget", "EAC above the new budget by more than this -> NO-GO; any smaller overrun -> CONDITIONAL GO."),
    "board_approval_threshold_eur": ("Board approval threshold", "€ per project (0 = off)", "Budget changes above this need board approval (condition)."),
    "overrun_tolerance_eur": ("Forecast gap tolerance", "€", "Smaller gaps between forecast and budget raise no condition (rounding)."),
    "condition_due_days": ("Due date of a condition", "days after the reference date", "Each condition gets an owner and this due date."),
    "rebaseline_board_threshold_pct": ("Re-baseline needs board approval above", "% deviation from the original baseline", "Cumulative budget or schedule deviation from baseline 1 (0 = off)."),
    "max_rebaselines_per_year": ("Max. re-baselines per project in 12 months", "re-baselines", "More is a CONDITIONAL GO with a planning-quality note."),
    "management_reserve_eur": ("Management reserve (start amount)", "€", "Portfolio reserve that projects draw on without a board request."),
    "reserve_floor_pct": ("Management reserve floor", "% of the start amount", "A draw that leaves less than this is a CONDITIONAL GO."),
    "risk_probability_low_pct": ("Risk probability 'Low'", "%", "Exposure = probability × impact."),
    "risk_probability_moderate_pct": ("Risk probability 'Moderate'", "%", "Exposure = probability × impact."),
    "risk_probability_high_pct": ("Risk probability 'High'", "%", "Exposure = probability × impact."),
    "risk_impact_low_eur": ("Risk impact 'Low'", "€", "Cost if the risk occurs."),
    "risk_impact_moderate_eur": ("Risk impact 'Moderate'", "€", "Cost if the risk occurs."),
    "risk_impact_high_eur": ("Risk impact 'High'", "€", "Cost if the risk occurs."),
    "benefit_realization_min_pct": ("Benefit realisation below which a corrective plan is needed", "% of the due benefit", "Actual vs. planned benefit of the tranches already due (CONDITIONAL GO below)."),
    "benefit_realization_nogo_pct": ("Benefit realisation that means NO-GO", "% of the due benefit (0 = off)", "Below this the business case no longer holds."),
    "benefit_report_grace_days": ("Grace period for reporting a due benefit", "days", "A tranche due longer than this without any actual is a condition."),
    "approval_sla_days": ("Approval deadline", "days after submission", "An open proposal past this is escalated (level 2 after twice the time)."),
    "approval_expiry_days": ("Open proposals expire after", "days (0 = never)", "Expired proposals are closed without a write – the data may have changed since."),
    "delegation_max_days": ("Max. length of an approval delegation", "days", "A PMO can hand their approval right to a deputy for at most this long."),
    "planning_preview_alert_pct": ("Preview year planned to", "% of the limit", "A preview year (after the binding year) planned to this share or more is a condition of the plan."),
    "planning_run_cost_alert_pct": ("Run cost after go-live raising OpEx by more than", "% of the OpEx limit", "The new yearly run cost of the planned demands of an area exceeding this share of its OpEx limit is a condition."),
    "planning_score_max_age_days": ("Assessment older than", "days", "A planned demand assessed longer ago needs a fresh assessment (condition)."),
    "status_cost_yellow_pct": ("Status check: forecast overrun for yellow", "% over budget", "The calculated status is yellow when the forecast (EAC) exceeds the budget by this much."),
    "status_cost_red_pct": ("Status check: forecast overrun for red", "% over budget", "The calculated status is red from this overrun."),
    "status_burn_gap_yellow_pct": ("Status check: spending ahead of progress for yellow", "percentage points", "Share of the budget spent minus share of the work done."),
    "status_burn_gap_red_pct": ("Status check: spending ahead of progress for red", "percentage points", "Share of the budget spent minus share of the work done."),
    "status_slip_yellow_days": ("Status check: end date slip for yellow", "days after the approved end", "Planned end date against the approved end date."),
    "status_slip_red_days": ("Status check: end date slip for red", "days after the approved end", "Planned end date against the approved end date; an open project past its planned end is red too."),
    "status_report_max_age_days": ("Status check: status report older than", "days", "A project whose last status report is older is listed as not updated."),
    "simulation_runs": ("Monte Carlo runs", "runs", "Number of random portfolio outcomes behind every probability."),
    "confidence_level_pct": ("Confidence level of forecast ranges", "%", "P80 by default: the value that 80 % of the outcomes stay below."),
    "reference_class_min_projects": ("Min. size of a reference class", "projects", "Smaller classes widen to the whole portfolio and say so."),
    "risk_response_min_benefit_ratio": ("Min. exposure reduction per € of risk response", "ratio", "A response that removes less exposure than this × its cost is a CONDITIONAL GO."),
}


# Candidate rules for one calculation only (the rule lab): set per context, never seen by other requests
_candidate: ContextVar[BusinessRules | None] = ContextVar("candidate_business_rules", default=None)


@contextmanager
def candidate_rules(rules: BusinessRules) -> Iterator[BusinessRules]:
    """Within the block, current_rules() answers `rules` in this context only (threads and other requests keep the
    saved rules)."""
    token = _candidate.set(rules)
    try:
        yield rules
    finally:
        _candidate.reset(token)


def current_rules() -> BusinessRules:
    """Rules of the active environment (runtime config). Falls back to defaults if the config is not available."""
    candidate = _candidate.get()
    if candidate is not None:
        return candidate
    try:
        from app.core.runtime_config import get_runtime_config

        return get_runtime_config().business_rules
    except Exception:  # noqa: BLE001 - tools must never fail because of configuration
        return BusinessRules()


def _fmt(key: str, value: float) -> str:
    label, unit, _ = RULE_LABELS[key]
    if unit.startswith("€"):
        return f"{label}: €{value:,.0f}{unit[1:]}"
    if value == 1:  # "1 month", "1 dependent project"
        unit = re.sub(r"\b(months|days|projects)\b", lambda m: m.group(1)[:-1], unit)
    return f"{label}: {value:g} {unit}"


def business_assumptions_md(rules: BusinessRules | None = None) -> str:
    """Markdown list for agent prompts and decision papers."""
    rules = rules or current_rules()
    return "\n".join(f"- {_fmt(key, getattr(rules, key))}" for key in RULE_LABELS)
