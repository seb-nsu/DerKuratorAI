"""Deterministic verdict engine: GO / CONDITIONAL GO / NO-GO is computed here in Python – never by an LLM.

Inputs are only what the deterministic tools recorded in the scenario context (verbatim tool results + structured
facts, see context.record_tool_result / record_fact) plus the PMO's forced-simulation list and the business rules of
the active environment. The Advisory agent receives the result and may only explain it; `enforce_verdict` rewrites
the paper's recommendation line if a model deviates, and appends the rule-based verdict basis.

Rule order (first match decides the verdict; all CONDITIONAL reasons are collected as conditions):
  1. INTERNAL ERROR in any tool result                         -> ANALYSIS INCOMPLETE (internal error)
     INPUT ERROR (request not understood, project not found,
                  invalid tool input)                         -> ANALYSIS INCOMPLETE (input not understood)
  2. guardrail ERROR in a simulation result
       – all of them data-quality blocks                       -> NO-GO (data quality – quality gate)
       – otherwise                                             -> NO-GO (guardrail violated)
  3. forecast overrun > eac_overrun_nogo_pct of the new budget -> NO-GO (budget does not cover the forecast)
     mandatory stage-gate criterion not met                    -> NO-GO (gate criteria not met)
     benefit realisation below benefit_realization_nogo_pct    -> NO-GO (business case not realised)
  4. any condition (forced run, domino >= threshold of a change that cuts budget, team or schedule of the project
     with the dependents, project stop with sunk costs >= cancel_conditional_sunk_pct, a hard dependent or planned
     benefit given up >= cancel_conditional_benefit_eur,
     graph unavailable, AI estimate or heuristic placeholder,
     smaller overrun, missing EAC, negative ROI, team overload, budget change above the board threshold,
     missed successor deadline, NPV turning negative, portfolio cut / unmet savings target,
     restart team differing from the team before the hold, projects tipping to NO-GO in a cost stress test,
     re-baseline above the board threshold or too often, advisory stage-gate criterion not met,
     management reserve below its floor, risk response costing more than it removes, accepted high/high risk,
     benefit realisation below benefit_realization_min_pct, a due benefit tranche without any actual,
     project merge (re-baseline of the target; second business unit or sponsor),
     other tool warnings)                                      -> CONDITIONAL GO
  5. simulation results without findings                       -> GO
  6. analysis only (no simulation)                             -> NO DECISION REQUIRED (analysis)
The label is the verdict alone; the rule in parentheses is `Verdict.rule` and the first reason.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from typing import Any

from app.core.business_rules import BusinessRules, current_rules
from app.crew.tools.context import RESTRICTED_DEPENDENTS

GUARDRAIL_ERROR_RE = re.compile(r"(?:^|\n|\] )ERROR:")
INTERNAL_ERROR_RE = re.compile(r"INTERNAL ERROR:")
# Parser failures, unknown projects and invalid tool input are an incomplete analysis, never a business guardrail violation
INPUT_ERROR_RE = re.compile(r"INPUT ERROR:")
GRAPH_DOWN_MARKERS = ("dependency graph", "Graph not built", "Graph database", "Graph query")  # paper notes only
AI_ESTIMATE_MARKER = "[AI ESTIMATE]"
HEURISTIC_MARKER = "[HEURISTIC PLACEHOLDER"  # offline fallback of a qualitative scenario
WARNING_FACTS = ("benefit_missing", "ramp_up", "no_capacity_kept", "skill_gap")  # structured tool warnings become conditions
# group 2 = the verdict text only (trailing blanks before "|" or the line end stay untouched)
_VERDICT_LINE_RE = re.compile(r"(\*\*Recommendation:\*\*[ \t]*)([^\n|]*?)([ \t]*(?:\||\n|$))", re.IGNORECASE)

GO, CONDITIONAL_GO, NO_GO = "GO", "CONDITIONAL GO", "NO-GO"
INCOMPLETE, NO_DECISION = "ANALYSIS INCOMPLETE", "NO DECISION REQUIRED"


PM, SPONSOR, PMO = "PM", "Sponsor", "PMO"  # who closes a condition


@dataclass(frozen=True)
class Verdict:
    decision: str                       # GO | CONDITIONAL GO | NO-GO | ANALYSIS INCOMPLETE | NO DECISION REQUIRED
    label: str                          # decision incl. qualifier, e.g. "NO-GO (guardrail violated)"
    reasons: tuple[str, ...] = ()       # why (rule hits)
    conditions: tuple[str, ...] = ()    # checkable conditions for CONDITIONAL GO
    rules: dict[str, Any] = field(default_factory=dict)
    as_of: str | None = None            # reference date of the calculation (ISO), stored with the decision
    rule: str = ""                      # the rule that decided a NO-GO / INCOMPLETE / NO DECISION, e.g. "guardrail violated"
    owners: tuple[str, ...] = ()        # owner role per condition (PM, Sponsor, PMO), aligned with `conditions`
    due: str | None = None              # due date of every condition: as_of + condition_due_days (ISO)

    def condition_details(self) -> list[dict[str, Any]]:
        """Each condition with its owner role and due date (paper, inbox, export)."""
        return [{"condition": c, "owner_role": self.owners[i] if i < len(self.owners) else PMO, "due": self.due}
                for i, c in enumerate(self.conditions)]

    def to_markdown(self) -> str:
        lines = [f"**Verdict (rule-based, not AI-generated): {self.label}**"]
        lines += [f"- Reason: {r}" for r in self.reasons]
        lines += [f"- Condition: {d['condition']} ({condition_owner_text(d)})" for d in self.condition_details()]
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        # `rules` = the thresholds in force when the verdict was computed, so a stored decision stays reproducible
        # after the business rules change
        return {"decision": self.decision, "label": self.label, "rule": self.rule, "reasons": list(self.reasons),
                "conditions": list(self.conditions), "condition_details": self.condition_details(),
                "rules": dict(self.rules), "as_of": self.as_of, "due": self.due}



def _benefit_given_up(fact: dict[str, Any], rules: Any) -> bool:
    """A stop gives up at least the configured planned benefit (the rule is off at 0)."""
    threshold = rules.cancel_conditional_benefit_eur
    return bool(threshold) and (fact.get("benefit_forgone") or 0) >= threshold

def condition_owner_text(detail: dict[str, Any]) -> str:
    """'owner: Sponsor · due 2026-10-15' – one wording for paper, inbox and export."""
    owner = f"owner: {detail.get('owner_role') or PMO}"
    return f"{owner} · due {detail['due']}" if detail.get("due") else owner


class _Conditions:
    """Conditions with their owner role, kept in order and without duplicates."""

    def __init__(self) -> None:
        self.items: dict[str, str] = {}

    def add(self, text: str, owner: str) -> None:
        self.items.setdefault(text, owner)

    def texts(self) -> tuple[str, ...]:
        return tuple(self.items)

    def owners(self) -> tuple[str, ...]:
        return tuple(self.items.values())


def _decided(decision: str, rule: str, reasons: tuple[str, ...], conditions: tuple[tuple[str, str], ...],
             snapshot: dict[str, Any]) -> Verdict:
    """A verdict decided by one rule: the label is the verdict alone (it fits every badge), the rule is the first
    reason. `conditions`: (text, owner role) pairs."""
    return Verdict(decision, decision, (f"{rule[0].upper()}{rule[1:]}.", *reasons), tuple(c for c, _ in conditions),
                   snapshot, rule=rule, owners=tuple(o for _, o in conditions))


def _eur(value: float) -> str:
    return f"€{value:,.0f}"


def _error_text(result: str, marker: str = "ERROR:") -> str:
    return next(line for line in result.splitlines() if marker in line).split(marker, 1)[1].strip()


def compute_verdict(sim_results: list[str], graph_results: list[str], facts: list[dict[str, Any]],
                    forced: list[str] | None = None, rules: BusinessRules | None = None,
                    as_of: str | None = None, adverse: set[str] | None = None) -> Verdict:
    """`adverse`: projects whose net change can hurt their dependents (context.adverse_projects). Only their domino
    is a condition; None treats every simulated project as adverse."""
    rules = rules or current_rules()
    verdict = _compute(sim_results, graph_results, facts, forced, rules, adverse)
    if not as_of:
        return verdict
    due = (date.fromisoformat(as_of) + timedelta(days=rules.condition_due_days)).isoformat() if verdict.conditions else None
    return replace(verdict, as_of=as_of, due=due)


def _compute(sim_results: list[str], graph_results: list[str], facts: list[dict[str, Any]],
             forced: list[str] | None, rules: BusinessRules, adverse: set[str] | None = None) -> Verdict:
    forced = list(forced or [])
    all_results = list(sim_results) + list(graph_results)
    snapshot = rules.model_dump()

    # 1. system defects
    internal = [r for r in all_results if INTERNAL_ERROR_RE.search(r)]
    if internal:
        return _decided(INCOMPLETE, "internal error",
                        (f"{len(internal)} tool result(s) failed with an internal error – no business verdict possible.",),
                        (("Re-run the analysis after the System Admin has checked the incident in the system log.", PMO),),
                        snapshot)
    invalid = [r for r in all_results if INPUT_ERROR_RE.search(r)]
    if invalid:
        texts = list(dict.fromkeys(_error_text(r, "INPUT ERROR:") for r in invalid))
        return _decided(INCOMPLETE, "input not understood",
                        (f"{len(invalid)} request part(s) could not be evaluated: {texts[0]}", *texts[1:]),
                        (("Rephrase the request with a valid project ID and amount, then re-run the analysis.", PM),),
                        snapshot)

    # 2. guardrails / quality gate – every violation is listed, the first one leads
    guardrails = [r for r in sim_results if GUARDRAIL_ERROR_RE.search(r)]
    if guardrails:
        dq = [r for r in guardrails if "Data quality" in r]
        texts = list(dict.fromkeys(_error_text(r) for r in guardrails))
        if len(dq) == len(guardrails):
            return _decided(NO_GO, "data quality – quality gate",
                            (f"{len(dq)} simulation(s) blocked by the Data Steward quality gate: {texts[0]}", *texts[1:]),
                            (("Fix the master data of the blocked project(s), then re-run the scenario.", PM),), snapshot)
        return _decided(NO_GO, "guardrail violated", (f"{len(guardrails)} guardrail violation(s): {texts[0]}", *texts[1:]),
                        (), snapshot)

    # 3. hard budget rule – gaps below the tolerance are rounding, not a finding
    overruns = [f for f in facts if f.get("type") == "budget_overrun"
                and (f.get("overrun") or 0) >= rules.overrun_tolerance_eur]
    hard = sorted((f for f in overruns if (f.get("overrun_pct") or 0) > rules.eac_overrun_nogo_pct),
                  key=lambda x: -(x.get("overrun_pct") or 0))
    if hard:
        return _decided(NO_GO, "budget does not cover the forecast",
                        tuple(f"{f['project']}: forecast (EAC {_eur(f['eac'])}) exceeds the new budget "
                              f"{_eur(f['new_budget'])} by {f['overrun_pct']:g} % – above the NO-GO threshold of "
                              f"{rules.eac_overrun_nogo_pct:g} %." for f in hard),
                        (), snapshot)
    gates = [f for f in facts if f.get("type") == "gate_mandatory_failed"]
    if gates:
        return _decided(NO_GO, "gate criteria not met",
                        tuple(f"{f['project']} ({f['transition']}): gate criteria not met: {', '.join(f['criteria'])}."
                              for f in gates),
                        tuple((f"Meet the mandatory criteria of {f['project']} ({', '.join(f['criteria'])}) and re-run "
                               "the gate check.", PM) for f in gates), snapshot)

    failed = [f for f in facts if f.get("type") == "benefit_case_failed"]
    if failed:
        return _decided(NO_GO, "business case not realised",
                        tuple(f"{f['project']}: only {f['pct']:g} % of the due benefit is realised ({_eur(f['actual'])} of "
                              f"{_eur(f['planned'])}) – below {rules.benefit_realization_nogo_pct:g} %." for f in failed),
                        tuple((f"Re-assess the business case of {f['project']} with its sponsor (continue, re-scope or "
                               "stop the benefit tracking).", SPONSOR) for f in failed), snapshot)

    # 4. conditions
    reasons: list[str] = []
    conditions = _Conditions()
    if forced:
        reasons.append(f"Forced simulation for {', '.join(forced)} (data-quality gate overridden by the PMO).")
        conditions.add(f"Fix the master data of {', '.join(forced)} and confirm the figures before execution.", PM)
    for f in overruns:
        reasons.append(f"{f['project']}: forecast exceeds the new budget by {_eur(f['overrun'])} ({f['overrun_pct']:g} %).")
        conditions.add(f"Close the forecast gap of {_eur(f['overrun'])} on {f['project']} (scope, contingency or budget).", PM)
    informed: list[str] = []
    threshold = rules.board_approval_threshold_eur
    above = {f["project"] for f in facts if f.get("type") == "budget_change" and threshold
             and abs(f.get("delta") or 0) > threshold}
    # a transfer moves the same money twice (source and target): one board decision per transfer
    transfers = [f for f in facts if f.get("type") == "budget_transfer" and {f.get("project"), f.get("successor")} & above]
    in_transfer = {p for f in transfers for p in (f.get("project"), f.get("successor"))}
    for f in facts:
        # a domino is a condition of a change that can hurt the dependents (cut, delay, stop, hold, fewer FTE) – a
        # pure dependency question or a change in their favour needs no decision; the paper still lists them
        domino = (f.get("type") == "domino" and sim_results
                  and (f.get("affected") or 0) >= rules.domino_conditional_threshold)
        if domino and adverse is not None and f.get("project") not in adverse:
            informed.append(f["project"] if f.get("restricted") else f"{f['project']} ({f['affected']})")
        elif domino and f.get("restricted"):  # a PM learns neither id nor count of projects outside the scope
            reasons.append(f"{f['project']}: {RESTRICTED_DEPENDENTS}")
            conditions.add(RESTRICTED_DEPENDENTS, PMO)
        elif domino:
            reasons.append(f"{f['project']}: {f['affected']} dependent project(s) affected (threshold "
                           f"{rules.domino_conditional_threshold}).")
            conditions.add(f"Align the {f['affected']} dependent project(s) of {f['project']} before execution.", PMO)
        elif f.get("type") == "eac_missing":  # no EAC = overrun rule not checkable, never a silent pass
            reasons.append(f"{f['project']}: no estimate at completion (EAC) – the forecast-overrun rule could not be checked.")
            conditions.add(f"Provide a current EAC for {f['project']} and re-run the scenario before execution.", PM)
        elif f.get("type") == "negative_roi":
            reasons.append(f"{f['project']}: ROI turns negative ({f['new_roi']:g} %).")
            conditions.add(f"Re-validate the business case of {f['project']} with its sponsor.", SPONSOR)
        elif f.get("type") == "overload":
            util = f.get("utilisation_pct")
            what = f"skill '{f['skill']}'" if f.get("skill") else "team"
            reasons.append(f"{f['project']}: {what} utilisation after the transfer "
                           f"{'unbounded' if util is None else f'{util:g} %'} (alert above {rules.overload_alert_pct:g} %).")
            conditions.add(f"Secure additional capacity or re-plan work for {f['project']}"
                           + (f" ({f['skill']})." if f.get("skill") else "."), PMO)
        elif f.get("type") == "resume" and f.get("fte_changed"):
            reasons.append(f"{f['project']} restarts with {f['fte']:g} FTE instead of {f['fte_before']:g} FTE before the hold.")
            conditions.add(f"Confirm the restart team of {f['project']} ({f['fte']:g} FTE) with the resource managers.", PM)
        elif f.get("type") == "cost_stress" and f.get("flips"):
            reasons.append(f"Portfolio stress: {len(f['flips'])} project(s) would breach the EAC rule at "
                           f"+{f['pct']:g} % cost: {', '.join(f['flips'])}.")
            conditions.add(f"Plan contingency or scope options for {', '.join(f['flips'])} against a cost increase of "
                           f"{f['pct']:g} %.", PMO)
        elif f.get("type") == "rebaseline" and (f.get("board") or f.get("too_many")):
            if f.get("board"):
                reasons.append(f"{f['project']}: re-baseline deviates {f['deviation_pct']:g} % from the original baseline "
                               f"(board threshold {rules.rebaseline_board_threshold_pct:g} %).")
                conditions.add(f"Board approval required for the re-baseline of {f['project']}.", SPONSOR)
            if f.get("too_many"):
                reasons.append(f"{f['project']}: re-baseline no. {f['count']} within 12 months (limit {f['limit']}) – "
                               "planning quality.")
                conditions.add(f"Review the planning quality of {f['project']} with the PMO before the next re-baseline.",
                               PMO)
        elif f.get("type") == "gate_advisory_failed":
            reasons.append(f"{f['project']}: advisory gate criterion not met – {f['criterion']} ({f['value']}).")
            conditions.add(f"{f['project']}: {f['criterion']} – resolve or accept before the next phase.", PM)
        elif f.get("type") == "reserve_floor":
            reasons.append(f"Management reserve falls to {_eur(f['balance'])}, below its floor of {_eur(f['floor'])}.")
            conditions.add("Confirm the draw below the reserve floor and plan its replenishment.", PMO)
        elif f.get("type") == "risk_response_low_benefit":
            reasons.append(f"{f['project']}: the response to {f['successor']} costs {_eur(f['cost'])} but removes only "
                           f"{_eur(f['reduction'])} of exposure (ratio {f['ratio']:g}).")
            conditions.add(f"Justify the response to {f['successor']} or choose a cheaper one.", PM)
        elif f.get("type") == "risk_accept_high":
            reasons.append(f"{f['project']}: high/high risk {f['successor']} is accepted.")
            conditions.add(f"Risk owner sign-off for accepting {f['successor']}.", SPONSOR)
        elif f.get("type") == "benefit_shortfall":
            reasons.append(f"{f['project']}: benefit realisation {f['pct']:g} % is below "
                           f"{rules.benefit_realization_min_pct:g} % (shortfall {_eur(f['shortfall'])}).")
            conditions.add(f"Corrective plan for the benefit shortfall of {f['project']} ({_eur(f['shortfall'])}).",
                           SPONSOR)
        elif f.get("type") == "benefit_unreported":
            reasons.append(f"{f['project']}: benefit tranche {f['successor']} is due since {f['due']} ({f['days']} days) "
                           "without any actual.")
            conditions.add(f"Report the actual benefit of tranche {f['successor']} of {f['project']}.", PM)
        elif f.get("type") == "merge":  # a merge is never unconditional: the target needs a new baseline
            reasons.append(f"{f['project']} is merged into {f['successor']} (combined budget {_eur(f['budget'])}"
                           + (f", end {f['end']})." if f.get("end") else ")."))
            conditions.add(f"Re-baseline {f['successor']} after the merge (budget {_eur(f['budget'])}"
                           + (f", end {f['end']})." if f.get("end") else ")."), PMO)
            if f.get("cross_unit"):
                reasons.append(f"The merge spans two business units ({' / '.join(f['cross_unit'])}).")
                conditions.add(f"Agree the funding and the reporting line of {f['successor']} between "
                               f"{' and '.join(f['cross_unit'])}.", SPONSOR)
            elif f.get("sponsors"):
                conditions.add(f"Agree the sponsor of the merged project {f['successor']} "
                               f"({' or '.join(str(s) for s in f['sponsors'] if s)}).", SPONSOR)
        elif f.get("type") == "deadline_breach":  # successor required-by dates
            if f.get("restricted"):
                reasons.append(f"{f['project']}: the new end date misses required-by dates of dependent projects outside "
                               "your scope – the PMO coordinates.")
                conditions.add(RESTRICTED_DEPENDENTS, PMO)
            elif f.get("new"):
                reasons.append(f"{f['project']}: new end date misses the required-by date {f['required_by']} of "
                               f"{f['successor']} ({f['dependency_type']}, {f['days_late']} days late).")
                conditions.add(f"Agree a new delivery date or a mitigation with {f['successor']} "
                               f"(required by {f['required_by']}).", PM)
            else:
                reasons.append(f"{f['project']}: {f['successor']} already waits beyond {f['required_by']}; lateness grows "
                               f"from {f['days_late_before']} to {f['days_late']} days.")
                conditions.add(f"Escalate the already late {f['dependency_type']} dependency of {f['successor']}.", PMO)
        elif f.get("type") == "on_hold":  # a pause is never unconditional: restart date and standby team must be agreed
            reasons.append(f"{f['project']} is paused for {f['months']:g} months (extra cost {_eur(f['extra_cost'])}).")
            conditions.add(f"Confirm the restart date ({f['restart']}) and the standby team "
                           f"({f['standby_fte']:g} FTE) of {f['project']} with the sponsor.", SPONSOR)
        elif f.get("type") == "cancel" and ((f.get("sunk_pct") or 0) >= rules.cancel_conditional_sunk_pct
                                            or (f.get("hard_dependents") or 0) > 0 or _benefit_given_up(f, rules)):
            why = []
            if (f.get("sunk_pct") or 0) >= rules.cancel_conditional_sunk_pct:
                why.append(f"{f['sunk_pct']:g} % of its budget is already spent (threshold "
                           f"{rules.cancel_conditional_sunk_pct:g} %)")
            if f.get("hard_dependents"):
                why.append("other projects depend on it (hard dependency)")
            if _benefit_given_up(f, rules):
                why.append(f"it gives up {_eur(f['benefit_forgone'])} of planned benefit (threshold "
                           f"{_eur(rules.cancel_conditional_benefit_eur)})")
            reasons.append(f"Stopping {f['project']}: {' and '.join(why)}.")
            exit_cost = f" and the exit costs of {_eur(f['exit_cost'])}" if f.get("exit_cost") else ""
            conditions.add(f"Confirm the stop of {f['project']} with the sponsor and book the impairment of "
                           f"{_eur(f.get('impairment') or 0)}{exit_cost}.", SPONSOR)
        elif f.get("type") == "indicative":  # stop or hold of a project the quality gate blocks
            reasons.append(f"{f['project']}: master data incomplete ({f.get('reason') or 'quality gate'}) – the "
                           "figures are indicative.")
            conditions.add(f"Master data of {f['project']} incomplete – figures indicative; complete the data and "
                           "confirm the figures before execution.", PM)
        elif f.get("type") == "npv_negative":
            reasons.append(f"{f['project']}: NPV turns negative ({f['npv_after']:,.0f} €).")
            conditions.add(f"Re-validate the business case of {f['project']} (NPV below zero after the change).", SPONSOR)
        elif f.get("type") == "portfolio_cut" and (f.get("achieved") or 0) < (f.get("target") or 0):
            reasons.append(f"Savings target not reached: {_eur(f['achieved'])} of {_eur(f['target'])}.")
            conditions.add(f"Find the missing {_eur(f['target'] - f['achieved'])} outside the cut list "
                           "(e.g. budget cuts or protected projects).", PMO)
        elif f.get("type") == "portfolio_cut" and f.get("projects"):
            reasons.append(f"Portfolio cut stops {len(f['projects'])} project(s): {', '.join(f['projects'])}.")
            conditions.add("Confirm each project stop with its sponsor and check the task / dependency domino.", SPONSOR)
        elif f.get("type") == "budget_change" and f["project"] in above and f["project"] not in in_transfer:
            reasons.append(f"{f['project']}: budget change of {_eur(abs(f['delta']))} exceeds the board approval threshold "
                           f"of {_eur(threshold)}.")
            conditions.add(f"Obtain board approval for the budget change of {f['project']}.", SPONSOR)
        elif f.get("type") == "budget_transfer" and f in transfers:
            reasons.append(f"Budget transfer of {_eur(abs(f['delta']))} from {f['project']} to {f['successor']} exceeds "
                           f"the board approval threshold of {_eur(threshold)}.")
            conditions.add(f"Obtain board approval for the budget transfer from {f['project']} to {f['successor']}.", SPONSOR)
    # structured fact only: a graph tool that answers again clears its earlier outage
    if any(f.get("type") == "graph_unavailable" for f in facts):
        reasons.append("Graph database unavailable – dependencies unchecked.")
        conditions.add("Re-check project dependencies once the graph database is available.", PMO)
    if any(AI_ESTIMATE_MARKER in r for r in sim_results):
        reasons.append("Part of the result is a qualitative AI estimate, not a calculation.")
        conditions.add("Validate the AI estimate with the project team before execution.", PM)
    if any(HEURISTIC_MARKER in r for r in sim_results):
        reasons.append("Part of the result is a heuristic placeholder (no AI, no calculation).")
        conditions.add("Quantify the change (amount, percentage or months) and re-run the scenario before execution.", PM)
    # remaining tool warnings that no rule above covers (ramp-up risk, missing benefit, no standby capacity) – recorded
    # by the tools as structured facts, never parsed from the report text
    for f in facts:
        if f.get("type") in WARNING_FACTS and f.get("warning"):
            reasons.append(f"Tool warning: {f['warning']}")
            conditions.add(f"Resolve: {f['warning']}", PM)

    if reasons:
        return Verdict(CONDITIONAL_GO, CONDITIONAL_GO, tuple(dict.fromkeys(reasons)), conditions.texts(), snapshot,
                       owners=conditions.owners())
    if sim_results:
        go = ["All simulations within guardrails; no rule triggered a condition."]
        stress = [f for f in facts if f.get("type") == "cost_stress"]
        if stress:
            go.append(f"Portfolio stress: no project would newly breach the EAC rule at +{stress[-1]['pct']:g} % cost.")
        if informed:
            go.append(f"Dependent projects informed, not at risk (the change does not reduce budget, team or "
                      f"schedule): {', '.join(informed)}.")
        return Verdict(GO, GO, tuple(go), (), snapshot)
    return _decided(NO_DECISION, "analysis", ("Analysis only – no change was simulated.",), (), snapshot)


def verdict_rules_md() -> str:
    """The rule order (module docstring) for the Admin Console – one source of truth."""
    rules = (__doc__ or "").split("Rule order", 1)[-1]
    return "```text\nRule order" + rules.rstrip() + "\n```"


def verdict_from_context(forced: list[str] | None = None, rules: BusinessRules | None = None) -> Verdict:
    """Verdict of the current scenario run – from what the tools recorded (identical in offline and LLM mode)."""
    from app.crew.tools import context as ctx

    return compute_verdict(ctx.get_tool_results("simulation"), ctx.get_tool_results("graph"), ctx.get_facts(),
                           forced, rules, as_of=ctx.as_of().isoformat(), adverse=ctx.adverse_projects())


def enforce_verdict(paper: str, verdict: Verdict) -> tuple[str, bool]:
    """Makes the paper's recommendation line equal the computed verdict (the LLM may not change it) and appends the
    rule-based verdict basis. Returns (paper, deviated) – deviated=True if the model had written a different verdict."""
    paper = paper or ""
    match = _VERDICT_LINE_RE.search(paper)
    deviated = False
    if match:
        written = match.group(2).strip()
        deviated = written.upper() != verdict.label.upper()
        paper = paper[:match.start(2)] + verdict.label + paper[match.end(2):]
    else:
        deviated = True
        lines = paper.split("\n", 1)
        head, rest = (lines[0], lines[1] if len(lines) > 1 else "") if paper.startswith("#") else ("", paper)
        paper = f"{head}\n**Recommendation:** {verdict.label}\n{rest}".lstrip("\n")
    if "Verdict (rule-based, not AI-generated)" not in paper:
        paper = paper.rstrip() + "\n\n---\n" + verdict.to_markdown() + "\n"
    return paper, deviated


def lead_reason(reasons) -> str:
    """The verdict's reason in one line for a summary: the deciding rule and its first finding."""
    text = " ".join(str(r).strip() for r in list(reasons or [])[:2] if str(r).strip())
    return text if len(text) <= 240 else text[:239].rstrip() + "…"


_SUMMARY_RE = re.compile(r"^##\s*1\.\s*Management Summary[^\n]*\n", re.MULTILINE)


def lead_summary(paper: str, verdict: Verdict) -> str:
    """The management summary opens with the rule-based recommendation (written by the code in every mode)."""
    match = _SUMMARY_RE.search(paper or "")
    if verdict.decision == NO_DECISION or not match or paper[match.end():].lstrip().startswith("- **Recommendation:"):
        return paper
    reason = lead_reason(verdict.reasons)
    line = f"- **Recommendation: {verdict.label}**" + (f" – {reason}" if reason else "")
    return paper[:match.end()] + line + "\n" + paper[match.end():]

