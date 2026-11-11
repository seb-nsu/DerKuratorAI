"""Steering Console logic behind the HTTP API (app/api/routes/steering.py), independent of any front end: the
guided scenario form (parameters, bounds, options), the chat messages of a run, the scenario memory ("builds on the
previous step"), the status of a run, saving a what-if and the direct apply of a change set that needs no approval."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Any

import pandas as pd

from app.approval_flow import audit, written_audit_failed
from app.core import clock
from app.core.formatting import field_label, rule_label
from app.core.proposals import SUBMITTED, get_proposal, requires_override
from app.core.runtime_config import get_runtime_config
from app.core.scenario_store import save_scenario
from app.core.security import log_security_event
from app.core.stage_gates import normalize_phase
from app.crew.scenarios import (
    COST_TYPE_LABELS,
    CUT_METHOD_LABELS,
    PORTFOLIO_ACTIONS,
    RISK_RESPONSES,
    SCENARIOS,
    STRESS_COST_TYPES,
)
from app.data_adapters import DataSourceError, PortfolioDataset, get_data_provider
from app.data_adapters.dataset import PHASES
from app.views.whatifs import budget_impact

logger = logging.getLogger("derkurator.views.console")

TARGET_REQUIRED = ("budget_transfer", "resource_transfer", "skill_transfer", "merge")  # a second project to pick
TARGET_OPTIONAL = ("cancel",)  # optional: transfer the free budget to it
MAX_TEXT = 500
SCENARIO_NOTES = {
    "portfolio_cut": "Across all projects in your scope – the tool ranks candidates by priority, value score and risk of "
                     "not performing. Proposes complete project stops (cut list) until the net budget release reaches "
                     "the target.",
    "cost_stress": "Across all open projects in your scope – nothing is written, the result ranks the projects by their "
                   "forecast overrun after the increase.",
    "cancel": "Calculates sunk costs, CapEx impairment risk, released budget, FTE and task domino.",
    "merge": "The target takes over budget, actuals, forecast, benefit, plans, risks and dependencies of the source "
             "project; the source is closed. Needs PMO approval.",
    "benefit_review": "Compares the benefit tranches already due with the actual benefits reported – writes nothing. "
                      "Actuals are reported in the project's Benefits tab.",
}


EXAMPLE_PROMPTS = [
    "Stop Tariff Invoicing Overhaul and move the budget to General Ledger Consolidation",
    "Cut the CapEx of General Ledger Consolidation by 10 %",
    "What happens if Vendor Interface Exchange gets 25 % more scope?",
    "Where do we have team bottlenecks and concentration risks?",
]


def example_prompts(ds: PortfolioDataset) -> list[str]:
    """Start examples built from the projects in the user's own scope (a PM never sees a foreign project name)."""
    names = [str(n) for n in ds.projects.get("project_name", pd.Series(dtype=str)).dropna()]
    if not names:
        return [EXAMPLE_PROMPTS[-1]]
    if {"General Ledger Consolidation", "Tariff Invoicing Overhaul", "Vendor Interface Exchange"} <= set(names):
        return EXAMPLE_PROMPTS
    first = names[0]
    examples = [f"Cut the CapEx of {first} by 10 %", f"What happens if {first} gets 25 % more scope?"]
    if len(names) > 1:
        examples.insert(0, f"Stop {names[1]} and move the budget to {first}")
    return [*examples, EXAMPLE_PROMPTS[-1]]


# --------------------------------------------------------------------------- guided scenario form
def min_schedule_months(ds: PortfolioDataset, project: str | None, floor: int | None = None) -> int:
    """Lowest selectable schedule shift: an acceleration must leave at least half a month of remaining duration and
    stays within the schedule guardrail (business rule)."""
    if floor is None:
        floor = -get_runtime_config().business_rules.max_timeline_shift_months
    if not project or ds.projects.empty:
        return floor
    row = ds.projects[ds.projects["project_number"] == project]
    end = pd.to_datetime(row["planned_end_date"].iloc[0], errors="coerce") if not row.empty else pd.NaT
    if pd.isna(end):
        return floor
    remaining = (end.date() - clock.today()).days / 30.44
    return max(floor, -max(int(remaining - 0.5), 0))


def project_row(ds: PortfolioDataset, project: str | None) -> dict[str, Any]:
    row = ds.projects[ds.projects["project_number"].astype(str) == str(project)]
    return row.iloc[0].to_dict() if not row.empty else {}


def project_teams(ds: PortfolioDataset, project: str | None) -> list[str]:
    alloc = ds.current_allocation()
    return sorted(alloc[alloc["project_number"] == project]["assignment_group"].astype(str).unique()) \
        if not alloc.empty else []


def stress_scopes(ds: PortfolioDataset) -> list[str]:
    """Portfolios and business units of the scope (the optional filter of the cost stress test)."""
    return sorted(set(ds.projects.get("portfolio", pd.Series(dtype=str)).dropna().astype(str))
                  | set(ds.projects.get("business_unit", pd.Series(dtype=str)).dropna().astype(str)))


def gate_phases(ds: PortfolioDataset, project: str | None) -> tuple[str | None, list[str]]:
    """(current phase, the selectable target phase – the next one)."""
    current = project_row(ds, project)
    phase = normalize_phase(current.get("phase"))
    following = list(PHASES[PHASES.index(phase) + 1:PHASES.index(phase) + 2]) if phase in PHASES else []
    return phase or current.get("phase"), following


def skill_names(ds: PortfolioDataset) -> list[str]:
    return sorted(set(ds.team_skills["skill"].astype(str))) if not ds.team_skills.empty else []


def risk_labels(ds: PortfolioDataset, project: str | None) -> dict[str, str]:
    risks = ds.risks[ds.risks["project_number"].astype(str) == str(project)] if not ds.risks.empty else ds.risks
    return {str(r["risk_number"]): f"{r['risk_number']} – {r.get('short_description', '')} "
                                   f"({r.get('probability')}/{r.get('impact')})"
            for r in risks.to_dict(orient="records")}


def _choice(name: str, label: str, options: dict[Any, str] | list, default: Any = None, **extra: Any) -> dict[str, Any]:
    pairs = options.items() if isinstance(options, dict) else ((o, o) for o in options)
    opts = [{"value": v, "label": str(text)} for v, text in pairs]
    return {"name": name, "label": label, "kind": "choice", "options": opts,
            "default": opts[0]["value"] if default is None and opts else default, **extra}


def _number(name: str, label: str, default: float, step: float, minimum: float = 0.0, maximum: float | None = None,
            kind: str = "number", **extra: Any) -> dict[str, Any]:
    return {"name": name, "label": label, "kind": kind, "default": default, "min": minimum, "max": maximum,
            "step": step, **extra}


def _text(name: str, label: str, required: bool = False) -> dict[str, Any]:
    return {"name": name, "label": label, "kind": "text", "default": "", "required": required, "max_length": MAX_TEXT}


def _date(name: str, label: str) -> dict[str, Any]:
    return {"name": name, "label": label, "kind": "date", "default": None}


def scenario_params(action: str, ds: PortfolioDataset, project: str | None) -> list[dict[str, Any]]:
    """The inputs of a guided scenario as the builder offers them: defaults, bounds and options (options and
    bounds that depend on the project are computed for `project` in the scope `ds`)."""
    rules = get_runtime_config().business_rules
    if action == "budget_change":
        return [_choice("cost_type", "Budget type", COST_TYPE_LABELS),
                _choice("direction", "Direction", {"decrease": "Cut", "increase": "Increase"}),
                _choice("is_perc", "Unit", {True: "%", False: "€"}),
                _number("value", "Value", 10.0, 5.0, unit_defaults={"%": {"default": 10.0, "step": 5.0},
                                                                    "€": {"default": 50000.0, "step": 10000.0}})]
    if action == "budget_transfer":
        return [_number("amount", "Amount (€, 0 = entire free budget)", 100000.0, 10000.0),
                _choice("cost_type", "Budget type", COST_TYPE_LABELS)]
    if action == "resource_transfer":
        return [_number("fte", "FTE", 1.0, 0.5),
                _choice("team", "Team (optional)", {"": "all teams", **{t: t for t in project_teams(ds, project)}})]
    if action == "schedule":
        return [_number("months_delta", "Schedule (months)", 0, 1, min_schedule_months(ds, project),
                        rules.max_timeline_shift_months, kind="integer",
                        help="Acceleration is limited by the remaining duration of the project."),
                _number("scope_change_pct", "Scope (%)", 0, 5, -50, 100, kind="integer"),
                _number("capacity_change_pct", "Capacity (%)", 0, 5, -50, 50, kind="integer")]
    if action == "on_hold":
        return [_number("months", "Hold duration (months)", 3, 1, 1, rules.max_hold_months, kind="integer"),
                _number("retention_pct", "Capacity kept on standby (%)", int(rules.hold_retention_pct), 5, 0, 100,
                        kind="integer")]
    if action == "portfolio_cut":
        return [_number("target_eur", "Savings target (€)", 2_000_000.0, 100_000.0),
                _choice("method", "Method", CUT_METHOD_LABELS)]
    if action == "resume":
        return [_date("restart_date", "Restart date"),
                _number("fte", "Returning FTE (0 = the team before the hold)", 0.0, 0.5, zero_is_none=True)]
    if action == "cost_stress":
        return [_number("pct", "Cost increase (%)", 8.0, 1.0),
                _choice("cost_type", "Applies to", STRESS_COST_TYPES),
                _choice("scope", "Scope (optional)", {"": "whole scope", **{s: s for s in stress_scopes(ds)}})]
    if action == "rebaseline":
        return [_number("new_budget_eur", "New budget (€, 0 = unchanged)", 0.0, 50_000.0),
                _date("new_end_date", "New end date (empty = unchanged)"),
                _text("reason", "Reason (mandatory)", required=True)]
    if action == "stage_gate":
        _, following = gate_phases(ds, project)
        return [_choice("target_phase", "Target phase", {p: p or "next phase" for p in following or [""]})]
    if action == "reserve_draw":
        return [_choice("direction", "Direction", {"draw": "Draw", "return": "Return"}),
                _number("amount", "Amount (€)", 200_000.0, 10_000.0),
                _text("reason", "Reason")]
    if action == "skill_transfer":
        return [_choice("skill", "Skill", skill_names(ds) or [""]),
                _number("fte", "FTE", 1.0, 0.5),
                _number("months", "Months", 3, 1, 1, 12, kind="integer")]
    if action == "risk_response":
        labels = risk_labels(ds, project)
        return [_choice("risk_number", "Risk", labels or {"": "no risks"}),
                _choice("response", "Response", list(RISK_RESPONSES)),
                _number("cost_eur", "Cost (€)", 0.0, 5_000.0),
                _choice("funding", "Funded from", {"project": "Project budget", "reserve": "Reserve"})]
    return []  # cancel, merge, benefit_review: no inputs


def scenario_notes(action: str, ds: PortfolioDataset, project: str | None, blocked: set[str], can_force: bool,
                   force: bool = False) -> list[str]:
    """The hints the builder shows next to the inputs (what the scenario does, why a project is blocked)."""
    notes = [SCENARIO_NOTES[action]] if action in SCENARIO_NOTES else []
    if action == "on_hold":
        rules = get_runtime_config().business_rules
        notes.append(f"No exit costs; restart ramp-up {rules.hold_restart_ramp_months:g} month(s). Successor deadlines "
                     "and NPV are checked.")
    if action == "resume" and project and str(project_row(ds, project).get("state")) != "On Hold":
        notes.append("This project is not on hold. Resume works for a paused project – or after a hold simulated "
                     "earlier in this what-if (\"Builds on the previous step\").")
    if action == "stage_gate" and project:
        phase, _ = gate_phases(ds, project)
        notes.append(f"Current phase: {phase or 'unknown'}")
    if project in blocked and action in ("cancel", "on_hold"):
        notes.append("The Data Steward blocks this project's master data – a stop or a hold is still calculated, with "
                     "indicative figures and a condition to confirm them.")
    elif project in blocked and not force:
        notes.append("This project is blocked for quantitative simulation by the Data Steward – the result shows the "
                     "quality gate finding and the graph analysis."
                     + (" The PMO can force the simulation (Options & rules)." if can_force else ""))
    return notes


def scenario_catalog() -> list[dict[str, Any]]:
    """Every guided scenario: label, whether it needs a project and a target project, and its note."""
    return [{"action": action, "label": label,
             "project": "none" if action in PORTFOLIO_ACTIONS else "required",
             "project_label": "Source project" if action in TARGET_REQUIRED else "Project",
             "target": "required" if action in TARGET_REQUIRED else "optional" if action in TARGET_OPTIONAL else "none",
             "target_label": "Target project" if action in TARGET_REQUIRED else
             "Transfer free budget to (optional)" if action in TARGET_OPTIONAL else None,
             "note": SCENARIO_NOTES.get(action)}
            for action, label in SCENARIOS.items()]


class GuidedInputError(ValueError):
    """An input of a guided scenario outside its options or bounds (the message is safe to show)."""


def _check_value(spec: dict[str, Any], value: Any) -> Any:
    kind, label = spec["kind"], spec["label"]
    if kind == "choice":
        allowed = [o["value"] for o in spec["options"]]
        if value not in allowed:
            raise GuidedInputError(f"{label}: choose one of the offered options.")
        return value
    if kind == "text":
        text = "" if value is None else str(value)
        if len(text) > spec["max_length"]:
            raise GuidedInputError(f"{label}: at most {spec['max_length']} characters.")
        return text
    if kind == "date":
        if value in (None, ""):
            return None
        try:
            return date.fromisoformat(str(value)).isoformat()
        except ValueError as exc:
            raise GuidedInputError(f"{label}: give a date as YYYY-MM-DD.") from exc
    if isinstance(value, bool):
        raise GuidedInputError(f"{label}: give a number.")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise GuidedInputError(f"{label}: give a number.") from exc
    if pd.isna(number) or number in (float("inf"), float("-inf")):
        raise GuidedInputError(f"{label}: give a number.")
    if kind == "integer":
        if number != int(number):
            raise GuidedInputError(f"{label}: give a whole number.")
        number = int(number)
    if spec.get("min") is not None and number < spec["min"]:
        raise GuidedInputError(f"{label}: at least {spec['min']:g}.")
    if spec.get("max") is not None and number > spec["max"]:
        raise GuidedInputError(f"{label}: at most {spec['max']:g}.")
    return None if spec.get("zero_is_none") and not number else number


def guided_inputs(action: str, project: str | None, target: str | None, params: dict[str, Any],
                  ds: PortfolioDataset) -> tuple[str | None, str | None, dict[str, Any]]:
    """Checks a guided scenario from a client against the builder's options and bounds in the scope `ds` and fills in
    the defaults. Returns (project, target, parameters). LookupError: a project outside the scope (the caller answers
    'not found'); GuidedInputError: an input the builder would not offer."""
    if action not in SCENARIOS:
        raise GuidedInputError(f"Unknown scenario: {action}")
    visible = set(ds.projects["project_number"].astype(str)) if not ds.projects.empty else set()
    if action in PORTFOLIO_ACTIONS:
        project = target = None
    else:
        if not project:
            raise GuidedInputError("Please choose a project.")
        if action not in TARGET_REQUIRED + TARGET_OPTIONAL:
            target = None
        for pid in (project, target):
            if pid and str(pid) not in visible:
                raise LookupError(pid)
    specs = {s["name"]: s for s in scenario_params(action, ds, project)}
    unknown = sorted(set(params) - set(specs))
    if unknown:
        raise GuidedInputError(f"Unknown input for this scenario: {', '.join(unknown)}")
    checked = {name: _check_value(spec, params[name]) if name in params else
               (None if spec.get("zero_is_none") and not spec["default"] else spec["default"])
               for name, spec in specs.items()}
    if action == "budget_change" and "value" not in params:  # the builder's default follows the unit
        checked["value"] = specs["value"]["unit_defaults"]["%" if checked["is_perc"] else "€"]["default"]
    return project, target, checked


# --------------------------------------------------------------------------- chat messages and scenario memory
def user_message(display: str, base_state: dict | None, force: bool, stamp: str) -> dict[str, Any]:
    step = f" · *step {len(base_state.get('turns') or []) + 1}*" if base_state else ""
    return {"role": "user", "content": display + step + (" · *forced simulation*" if force else ""), "ts": stamp}


def assistant_message(request: str, result: Any, run_id: str, force: bool, base_state: dict | None) -> dict[str, Any]:
    """The chat message of a finished run (decision paper plus everything the report viewer and the actions need)."""
    steward = result.steward_report
    return {
        "role": "assistant", "content": result.decision_paper, "ts": clock.now().strftime("%Y-%m-%d %H:%M"),
        "meta": {
            "request": request, "mode": result.mode, "duration_s": result.duration_s, "warnings": result.warnings,
            "steward_md": steward.to_markdown() if steward else "",
            "dq_findings": [{"Severity": i.severity, "Project": i.project_id, "Rule": rule_label(i.rule),
                             "Field": field_label(i.column), "Finding": i.message} for i in steward.issues]
            if steward else [],
            "plan": result.intent_plan.model_dump() if result.intent_plan else None,
            "proposed_changes": result.proposed_changes, "applied": False, "forced": force,
            "verdict": result.verdict.to_dict() if result.verdict else None,  # rule-based
            "scenario_state": result.scenario_state,  # memory for the next step
            "continued": bool(base_state),
            "run_id": run_id,
        },
    }


def previous_verdict_blocks(messages: list[dict]) -> str | None:
    """'NO-GO' / 'INCOMPLETE' if the latest result was rejected: the next step then starts from the original data by
    default (a NO-GO step is rarely the base of the next idea, e.g. −20 % NO-GO, then −10 % on its own)."""
    for message in reversed(messages):
        if message.get("role") == "assistant":
            decision = str(((message.get("meta") or {}).get("verdict") or {}).get("decision") or "")
            return "NO-GO" if decision == "NO-GO" else "INCOMPLETE" if "INCOMPLETE" in decision else None
    return None


def last_scenario_message(messages: list[dict]) -> dict | None:
    """Scenario memory: the latest assistant message if it can still be continued (not applied, not submitted as a
    proposal)."""
    for message in reversed(messages):
        if message.get("role") != "assistant":
            continue
        meta = message.get("meta") or {}
        if not meta.get("scenario_state"):
            return None
        return None if meta.get("applied") or meta.get("handed_over") or meta.get("proposal_id") else message
    return None


def last_scenario_state(messages: list[dict]) -> dict | None:
    message = last_scenario_message(messages)
    return (message.get("meta") or {}).get("scenario_state") if message else None


def run_state(meta: dict, is_last: bool) -> dict[str, Any]:
    """Status of an analysis: applied / handed_over / proposal / outdated / draft / analysis."""
    changes = meta.get("proposed_changes") or []
    if meta.get("applied"):
        return {"state": "applied", "override": bool(meta.get("override")), "applied_at": meta.get("applied_at", "")}
    if meta.get("handed_over"):
        return {"state": "handed_over"}
    if meta.get("proposal_id"):
        proposal = get_proposal(meta["proposal_id"]) or {}
        return {"state": "proposal", "proposal_id": meta["proposal_id"],
                "proposal_status": proposal.get("status", meta.get("proposal_status", SUBMITTED)),
                "decided_by": proposal.get("decided_by"), "override": bool(proposal.get("override")),
                "comment": proposal.get("comment")}
    if changes and not is_last:
        return {"state": "outdated"}
    return {"state": "draft" if changes else "analysis"}


def run_badge(meta: dict, state: dict) -> dict[str, str]:
    """The status badge of an analysis (label, tone, Material icon) from its `run_state`."""
    from app.core.proposals import APPROVED, REJECTED, STALE
    from app.views.approvals import STATUS_LABELS

    if state["state"] == "applied":
        label = "Approved with override" if state["override"] else "Approved"
        return {"label": f"{label} · applied {state['applied_at']}".strip(),
                "tone": "warning" if state["override"] else "success", "icon": "check_circle"}
    if state["state"] == "handed_over":
        return {"label": "Approved · manual execution", "tone": "success", "icon": "assignment_turned_in"}
    if state["state"] == "proposal":
        status = state["proposal_status"]
        by = f" by {state['decided_by']}" if state["decided_by"] else ""
        if state["override"]:
            return {"label": f"Proposal {meta['proposal_id']} · Approved with override{by}", "tone": "warning",
                    "icon": "outbox"}
        tone = "success" if status == APPROVED else "danger" if status in (REJECTED, STALE) else "info"
        return {"label": f"Proposal {meta['proposal_id']} · {STATUS_LABELS.get(status, str(status).capitalize())}{by}",
                "tone": tone, "icon": "outbox"}
    if state["state"] == "outdated":
        return {"label": "Outdated", "tone": "neutral", "icon": "history"}
    if state["state"] == "draft":
        return {"label": "Draft · not submitted", "tone": "neutral", "icon": "edit_note"}
    return {"label": "Analysis", "tone": "info", "icon": "insights"}


# --------------------------------------------------------------------------- actions on a result
def save_what_if(name: str, role: str, user_id: str, request: str, scenario_state: dict | None, verdict: dict | None,
                 proposed_changes: list[dict], projects: pd.DataFrame) -> dict[str, Any]:
    """Saves a result for the A/B comparison (with its budget impact on today's data) and audits it. Raises
    ScenarioStoreError (the message is safe to show)."""
    state = scenario_state or {}
    entry = save_scenario(name, user_id, role, state.get("turns") or [request or ""], verdict, proposed_changes or [],
                          budget_impact(proposed_changes or [], projects),
                          environment=get_runtime_config().environment)
    audit("scenario_saved", role=role, user_id=user_id, scenario_id=entry["id"], name=entry["name"],
          changes=len(entry["changes"]), verdict=(verdict or {}).get("label"))
    return entry


def verdict_label(verdict: dict | str | None) -> str | None:
    return verdict.get("label") if isinstance(verdict, dict) else verdict


@dataclass(frozen=True)
class ApplyOutcome:
    """`kind`: success | warning (written, but the audit record failed) | error (nothing written).
    `closed_as`: applied (fields written) or handed_over (only manual changes) once the write went through."""
    kind: str
    message: str
    closed_as: str | None = None
    applied_at: str | None = None
    applied: int = 0
    manual: int = 0
    snapshot_id: str | None = None
    override: bool = False
    system_error: bool = False

    @property
    def done(self) -> bool:
        return self.closed_as is not None


def apply_change_set(changes: list[dict], role: str, user_id: str, *, request: str | None, verdict: dict | str | None,
                     forced: bool, run_id: str | None, override_reason: str = "") -> ApplyOutcome:
    """Approve & apply of a change set without governed fields (approval mode 'apply'): compare-and-write with a
    snapshot, then the audit record. Only manual changes (e.g. resource plans) write nothing and are closed as a
    hand-over. The caller has checked the approval mode and the scope."""
    overridden = requires_override(verdict_label(verdict))
    if overridden and not override_reason.strip():
        return ApplyOutcome("error", "A NO-GO / incomplete verdict is applied only with an explicit override and a "
                                     "reason – nothing was written.")
    try:
        # compare-and-write: fails with DataConflictError if someone changed the data since the simulation
        result = get_data_provider().apply_changes(changes, user=user_id, reason="approve_apply", role=role)
    except DataSourceError as exc:
        return ApplyOutcome("error", f"Apply failed: {exc}")
    except Exception:  # no raw exception text for business users
        logger.exception("Approve & Apply failed")
        return ApplyOutcome("error", "The changes could not be applied due to a system error – nothing was written. "
                                     "Please contact your System Admin.", system_error=True)
    # nothing written (only manual changes, e.g. resource plans) – closed as a manual hand-over, never "applied"
    done = {"closed_as": "applied" if result.applied else "handed_over",
            "applied_at": clock.now().strftime("%Y-%m-%d %H:%M"), "applied": result.applied,
            "manual": len(result.manual), "snapshot_id": result.snapshot_id, "override": overridden}
    try:  # the data is written from here on: a failure below must never read as "nothing happened"
        audit("scenario_applied", role=role, user_id=user_id, request=request, changes=changes, applied=result.applied,
              manual=result.manual, forced=forced, snapshot_id=result.snapshot_id, override=overridden,
              verdict=verdict_label(verdict), verdict_record=verdict if isinstance(verdict, dict) else None,
              comment=override_reason.strip(), run_id=run_id)
        log_security_event("Scenario approved with verdict override (Approve & Apply)" if overridden else
                           "Scenario approved (Approve & Apply)", role=role, user=user_id, fields=result.applied,
                           manual=len(result.manual), override=overridden)
    except Exception:  # noqa: BLE001 - written, but the record behind it failed
        return ApplyOutcome("warning", written_audit_failed(result.applied, result.snapshot_id), **done)
    text = (f"{result.applied} field change(s) written to the data source (snapshot {result.snapshot_id} created, "
            "audit log written)." if result.applied else
            "Approval recorded in the audit log – no field was written to the data source.")
    if result.manual:
        text += f" {len(result.manual)} change(s) require manual execution (e.g. resource plans)."
    return ApplyOutcome("success", text, **done)
