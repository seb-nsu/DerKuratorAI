"""Guard rails around the LLM plan: the deterministic parser corrects or confirms the plan, asks back when a quantity
or project is missing, and checks that the simulated sizes match the request."""
from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import Any

import pandas as pd

from app.crew.paper import _parser_error
from app.crew.parser import (
    _ACCEL_KW,
    _BENEFIT_KW,
    _CANCEL_KW,
    _DELAY_KW,
    _DOWN_KW,
    _GATE_KW,
    _HOLD_KW,
    _MERGE_KW,
    _MOVE_KW,
    _PROJECT_ID_RE,
    _REBASELINE_KW,
    _RESERVE_KW,
    _RESOURCE_KW,
    _RESUME_KW,
    _RETURN_KW,
    _RISK_RE,
    _SAVINGS_KW,
    _SKILL_KW,
    _STOPWORDS,
    _STRESS_KW,
    _UP_KW,
    ASK_QUANTITY,
    ASK_QUANTITY_MESSAGE,
    CAPACITY_HINTS,
    SCOPE_HINTS,
    UNRESOLVED_PROJECT,
    _has,
    _parse_amount_eur,
    _parse_cost_type,
    _parse_date,
    _parse_fte,
    _parse_months,
    _parse_percent,
    _parse_response,
    _parse_skill,
    _to_float,
    closest_project_names,
    find_projects_in_text,
    has_quantity,
    heuristic_plan,
)
from app.crew.tasks import IntentPlan, SubIntent
from app.crew.tools import context as ctx

logger = logging.getLogger(__name__)


_PMO_ACTIONS = ("resume", "cost_stress", "rebaseline", "stage_gate", "reserve_draw", "reserve_return", "skill_transfer",
                "risk_response", "benefit_review", "merge")
_SIM_ACTIONS = ("cancel", "budget_transfer", "resource_transfer", "schedule", "budget_change", "unstructured",
                "on_hold", "portfolio_cut", UNRESOLVED_PROJECT, ASK_QUANTITY) + _PMO_ACTIONS
_NEEDS_QUANTITY = ("budget_change", "schedule", "resource_transfer", "portfolio_cut", "budget_transfer", "cost_stress",
                   "reserve_draw", "reserve_return", "skill_transfer")
_QUESTION = {
    "budget_change": "By how much should the budget of {name} change?",
    "schedule": "By how many months (or by what percentage of capacity or scope) should {name} change?",
    "resource_transfer": "How many FTE should be moved from {name}?",
    "portfolio_cut": "What is the savings target in EUR?",
    "budget_transfer": "How much budget should be moved from {name}?",
    "cost_stress": "By what percentage should the remaining costs rise in the stress test?",
    "reserve_draw": "How much should {name} draw from the management reserve?",
    "reserve_return": "How much should {name} return to the management reserve?",
    "skill_transfer": "How many FTE of the skill should be moved from {name}?",
}
_PLAN_CORRECTED = "Plan corrected by the deterministic parser (the AI plan differed): {intent}."
_PLAN_CONFIRMED = "Simulation run by the deterministic parser (the AI plan agreed): {intent}."
_PLAN_STRUCTURED = "Simulation run deterministically from the AI's structured plan (values checked against the request): {intent}."
STRUCTURED = "structured_plan"  # marks an intent the code ran from the orchestrator's typed fields
NO_SIMULATION_MESSAGE = ("INPUT ERROR: The request asks for a change, but no simulation was run. "
                         "Please restate it.")


_UNIT_FACTORS = {"k": 1e3, "tsd": 1e3, "tausend": 1e3, "thousand": 1e3, "m": 1e6, "mio": 1e6, "million": 1e6,
                 "millions": 1e6, "millionen": 1e6, "mrd": 1e9, "bn": 1e9, "billion": 1e9}
_NUMBER_RE = re.compile(r"(\d+(?:[.,]\d+)*)\s*(k|tsd|tausend|thousand|mio|millions?|millionen|mrd|bn|billion|m)?"
                        r"(?![a-zäöüß])(\s*%)?")


def _numbers(text: str) -> set[float]:
    """Explicit numbers of a text, project IDs aside, with their unit applied ("€800k" = 800000). Signs are dropped:
    "cut by 20 %" and "-20 %" are the same value."""
    tl = _PROJECT_ID_RE.sub(" ", text.lower())
    return {abs(_to_float(n)) * _UNIT_FACTORS.get(unit or "", 1) for n, unit, _ in _NUMBER_RE.findall(tl)}


def _same_values(request: str, description: str, ids: list[str]) -> bool:
    """Every number of the request appears in the plan description – a percentage also as the euro amount it means
    for the project's budget ("cut by 10 %" = "cut by €185,000" for a €1.85 M budget)."""
    planned = _numbers(description)
    budget = None
    if ids:
        try:
            budget = float(ctx.get_project(ids[0])["budget_cost_eur"])
        except (ctx.ProjectNotFound, KeyError, TypeError, ValueError):
            budget = None
    tl = _PROJECT_ID_RE.sub(" ", request.lower())
    for n, unit, pct in _NUMBER_RE.findall(tl):
        value = abs(_to_float(n)) * _UNIT_FACTORS.get(unit or "", 1)
        if any(abs(value - v) <= max(abs(v) * 0.005, 0.01) for v in planned):
            continue
        if pct and budget and any(abs(value / 100 * budget - v) <= max(budget * 0.005, 1) for v in planned):
            continue
        return False
    return True


# Sizes a request states in words. Euro phrases first: "half a million" is an amount, not 50 %.
_EUR_PHRASES = {"half a million": 5e5, "quarter of a million": 2.5e5, "quarter million": 2.5e5, "a million": 1e6,
                "one million": 1e6, "eine million": 1e6, "halbe million": 5e5}
_PCT_WORDS = {"half": 50, "halve": 50, "third": 100 / 3, "quarter": 25, "fifth": 20, "tenth": 10,
              "hälfte": 50, "haelfte": 50, "halbieren": 50, "drittel": 100 / 3, "viertel": 25, "fünftel": 20,
              "fuenftel": 20, "zehntel": 10}
_PCT_WORD_RE = re.compile(r"(?<![a-zäöüß])(" + "|".join(_PCT_WORDS) + r")s?(?![a-zäöüß])")
_EUR_NUMBER_RE = re.compile(r"(\d+(?:[.,]\d+)*)\s*(k|tsd|tausend|thousand|mio|millions?|millionen|mrd|bn|billion|m)?"
                            r"(?![a-zäöüß\d])\s*(%|prozent|percent|€|eur|euro|fte|month|monat|week|year|jahr|day|tag)?")
_BUDGET_TOOLS = ("simulate_budget_change", "simulate_budget_transfer")
_BUDGET_FIELDS = ("budget_cost_eur", "planned_capital_eur", "planned_operating_eur", "total_planned_cost_eur")


def request_sizes(request: str) -> tuple[list[float], list[float]]:
    """(percentages, euro amounts) the request states, in digits or words – "a fifth" = 20 %, "200k" = €200,000,
    "half a million" = €500,000. Counts of FTE, months and the like are left out."""
    tl = _PROJECT_ID_RE.sub(" ", request.lower())
    euros = []
    for phrase in sorted(_EUR_PHRASES, key=len, reverse=True):  # "half a million" before "a million"
        if phrase in tl:
            euros.append(_EUR_PHRASES[phrase])
            tl = tl.replace(phrase, " ")
    percents = [float(_PCT_WORDS[w]) for w in _PCT_WORD_RE.findall(tl)]
    for number, unit, suffix in _EUR_NUMBER_RE.findall(tl):
        value = _to_float(number) * _UNIT_FACTORS.get(unit or "", 1)
        if suffix in ("%", "prozent", "percent"):
            percents.append(value)
        elif suffix in ("€", "eur", "euro") or unit or (not suffix and value >= 1000):
            euros.append(value)
    return percents, euros


def size_mismatch(request: str, changes: list[dict[str, Any]]) -> str | None:
    """A budget change the Simulator agent made whose size or direction the request does not state: a percentage in
    the request needs a delta of that share, an amount the same amount (±1 %), "cut" a negative and "increase" a
    positive delta. Returns the question to ask back, or None. Only budget tools are checked; a request without a
    stated size is not."""
    percents, euros = request_sizes(request)
    if not (percents or euros):
        return None
    tl = request.lower()
    down, up = _has(tl, _DOWN_KW), _has(tl, _UP_KW)
    deltas: dict[str, list[tuple[float, float]]] = {}
    for c in changes:
        if c.get("source_tool") not in _BUDGET_TOOLS or c.get("field") not in _BUDGET_FIELDS:
            continue
        try:
            old, new = float(c.get("old")), float(c.get("new"))
        except (TypeError, ValueError):
            continue
        if new != old:
            deltas.setdefault(str(c["project_id"]), []).append((old, new - old))
    for pid, moves in deltas.items():
        def fits(old: float, delta: float) -> bool:
            share = abs(delta) / abs(old) * 100 if old else None
            return (any(abs(abs(delta) - e) <= e * 0.01 for e in euros)
                    or (share is not None and any(abs(share - p) <= max(p * 0.01, 0.05) for p in percents)))

        transfer = any(c.get("source_tool") == "simulate_budget_transfer" and str(c["project_id"]) == pid
                       for c in changes)
        wrong_way = not transfer and ((down and not up and all(d > 0 for _, d in moves))
                                      or (up and not down and all(d < 0 for _, d in moves)))
        if wrong_way or not any(fits(old, d) for old, d in moves):
            old, delta = moves[0]
            stated = ", ".join([f"{p:g} %" for p in percents] + [f"€{e:,.0f}" for e in euros])
            return (f"INPUT ERROR: The simulated budget change of {pid} ({'+' if delta > 0 else '-'}€{abs(delta):,.0f}) "
                    f"does not match the request ({stated}). Please confirm the size and direction of the change.")
    return None


def _intent_sentence(intent: SubIntent) -> str:
    """A parser intent in words: "budget change of General Ledger Consolidation", "resource transfer from A to B"."""
    label = intent.description.split(":", 1)[0].replace("_", " ")
    names = [_project_name(pid) for pid in intent.project_ids]
    if len(names) >= 2:
        return f"{label} from {names[0]} to {names[1]}"
    return f"{label} of {names[0]}" if names else label


def _stated(value: float | None, request: str, percent: bool | None = None) -> bool:
    """The value appears in the request itself (unit applied, sign ignored) – the model copied it, not computed it.
    `percent`: True = only percentages count, False = only plain numbers and amounts, None = both."""
    if value is None:
        return False
    tl = _PROJECT_ID_RE.sub(" ", request.lower())
    numbers = [abs(_to_float(n)) * _UNIT_FACTORS.get(unit or "", 1) for n, unit, pct in _NUMBER_RE.findall(tl)
               if percent is None or bool(pct) == percent]
    return any(abs(abs(value) - n) <= max(abs(n) * 0.005, 0.01) for n in numbers)


def _direction(request: str) -> int:
    """+1 / -1 from the request's own words, 0 if it says neither (or both)."""
    tl = request.lower()
    down, up = _has(tl, _DOWN_KW), _has(tl, _UP_KW)
    return 0 if down == up else (-1 if down else 1)


def structured_parameters(intent: SubIntent, ids: list[str], request: str) -> dict[str, Any] | None:
    """Exact tool parameters from the orchestrator's typed fields, or None when the code cannot run them safely: an
    action outside the structured set, a missing project, a value the request does not state, or a direction the
    request does not say. Sign, unit and cost type come from the request – never from the model."""
    action = intent.action
    if not action or not ids:
        return None
    tl = request.lower()
    params: dict[str, Any] = {"action": action, "source": STRUCTURED}
    if action == "cancel":
        return params if _has(tl, _CANCEL_KW) else None
    if action == "budget_change":
        sign = _direction(request)
        if sign == 0:
            return None
        if intent.percent is not None and intent.amount_eur is None and _stated(intent.percent, request, True):
            value, is_perc = intent.percent, True
        elif intent.amount_eur is not None and intent.percent is None and _stated(intent.amount_eur, request, False):
            value, is_perc = intent.amount_eur, False
        else:
            return None
        return {**params, "delta": sign * abs(value), "is_perc": is_perc, "cost_type": _parse_cost_type(request)}
    if action == "budget_transfer":
        if len(ids) < 2 or not _has(tl, _MOVE_KW) or (intent.amount_eur is not None and not _stated(intent.amount_eur, request, False)):
            return None
        return {**params, **({"amount": abs(intent.amount_eur)} if intent.amount_eur else {}),
                "cost_type": _parse_cost_type(request)}
    if action == "on_hold":
        if not _has(tl, _HOLD_KW) or (intent.months is not None and not _stated(intent.months, request, False)):
            return None
        return {**params, **({"months": abs(intent.months)} if intent.months else {})}
    if action == "schedule":
        delay, accel = _has(tl, _DELAY_KW), _has(tl, _ACCEL_KW)
        if delay == accel or not _stated(intent.months, request, False) or intent.percent is not None \
                or re.search(r"\d\s*(%|prozent|percent)", tl):
            return None  # capacity or scope changes stay with the Simulator
        return {**params, "months_delta": abs(intent.months) * (-1 if accel else 1)}
    return None


def certain_parser_intent(request: str, projects: pd.DataFrame) -> SubIntent | None:
    """The parser's simulation intent if it is certain: exactly one simulation intent, the project(s) it needs and
    exactly one explicit number, which is the value the intent uses. Anything else is left to the LLM."""
    sims = [i for i in heuristic_plan(request, projects).sub_intents if i.intent_type == "simulation"]
    if len(sims) != 1 or len(re.findall(r"\d+(?:[.,]\d+)*", _PROJECT_ID_RE.sub(" ", request.lower()))) != 1:
        return None
    intent, tl = sims[0], request.lower()
    found = len(find_projects_in_text(request, projects))
    kind = intent.description.split(":", 1)[0]
    moves = _has(tl, _DELAY_KW) or _has(tl, _ACCEL_KW)
    schedule_values = [_parse_percent(request, CAPACITY_HINTS), _parse_percent(request, SCOPE_HINTS),
                       _parse_months(request) if moves else None]
    certain = {
        "budget_change": found == 1 and (_parse_percent(request) is not None or _parse_amount_eur(request) is not None),
        "schedule": found == 1 and sum(v is not None for v in schedule_values) == 1,
        "on_hold": found == 1 and _parse_months(request) is not None,
        "budget_transfer": found == 2 and bool(_parse_amount_eur(request)) and not _has(tl, _CANCEL_KW),
        "resource_transfer": found == 2 and _parse_fte(request) is not None,
        "portfolio_cut": found == 0 and bool(_parse_amount_eur(request)),
        "cost_stress": found == 0 and _parse_percent(request) is not None,
        "reserve_draw": found == 1 and bool(_parse_amount_eur(request)),
        "reserve_return": found == 1 and bool(_parse_amount_eur(request)),
        "rebaseline": found == 1 and bool(_parse_amount_eur(request) or _parse_date(request)),
        "skill_transfer": found == 2 and _parse_fte(request) is not None and _parse_skill(request) is not None,
        "benefit_review": found == 1 and _has(tl, _BENEFIT_KW),
        "merge": found == 2 and _has(tl, _MERGE_KW),
    }
    return intent if certain.get(kind) else None


def _action_family(intent: SubIntent) -> str:
    """Action of a plan intent: the parser's own label, else read from the LLM's description."""
    head = str((intent.parameters or {}).get("action") or intent.description.split(":", 1)[0]).strip().lower()
    if head in _SIM_ACTIONS:
        return head
    d = intent.description.lower()
    if _RISK_RE.search(d) and _parse_response(d):
        return "risk_response"
    if _has(d, _BENEFIT_KW):
        return "benefit_review"
    if _has(d, _MERGE_KW) and len(intent.project_ids) >= 2:
        return "merge"
    if _has(d, _REBASELINE_KW):
        return "rebaseline"
    if _has(d, _RESUME_KW):
        return "resume"
    if _has(d, _STRESS_KW):
        return "cost_stress"
    if _has(d, _GATE_KW):
        return "stage_gate"
    if _has(d, _RESERVE_KW):
        return "reserve_return" if _has(d, _RETURN_KW) else "reserve_draw"
    if _has(d, _SKILL_KW) and len(intent.project_ids) >= 2:
        return "skill_transfer"
    if _has(d, _HOLD_KW):
        return "on_hold"
    if "portfolio" in d and (_has(d, _SAVINGS_KW) or _has(d, _DOWN_KW)):
        return "portfolio_cut"
    if _has(d, _CANCEL_KW):
        return "cancel"
    if _has(d, _RESOURCE_KW):  # before "transfer": "transfer of 1 FTE" moves people, not budget
        return "resource_transfer"
    if _has(d, ("transfer", "reallocat", "umschicht")) or ("budget" in d and _has(d, ("move", "shift"))):
        return "budget_transfer"
    if _has(d, _DELAY_KW) or _has(d, _ACCEL_KW) or _has(d, ("schedule", "month", "monat", "zeitplan")) \
            or _has(d, CAPACITY_HINTS + SCOPE_HINTS):
        return "schedule"
    if _has(d, ("budget", "capex", "opex", "cost", "kosten")):
        return "budget_change"
    return "unstructured"


def _resolved_ids(intent: SubIntent) -> list[str]:
    ids = []
    for ref in intent.project_ids:
        try:
            ids.append(ctx.resolve_project_id(ref))
        except ctx.ProjectNotFound:
            continue
    return ids


def _project_name(pid: str) -> str:
    try:
        return str(ctx.get_project(pid)["project_name"])
    except (ctx.ProjectNotFound, KeyError, TypeError):
        return pid


def _plan_conflict(request: str, intents: list[tuple[SubIntent, str, list[str]]]) -> str | None:
    """Two changes on the same project that cannot both apply, or a request that asks for opposite directions."""
    tl = request.lower()
    accelerate = _has(tl, _ACCEL_KW)
    delay = _has(tl, tuple(k for k in _DELAY_KW if k != "verschieb"))  # "verschieben" also means "move earlier"
    by_project: dict[str, set[str]] = {}
    for _, family, ids in intents:
        for pid in ids[:1]:  # the changed project; a transfer's target is not changed by the action itself
            by_project.setdefault(pid, set()).add(family)
    for pid, families in by_project.items():
        # "Stop X and move its budget to Y" may be planned as a budget change of X – that is not a conflict
        clashes = families & ({"schedule", "on_hold"} | (set() if _has(tl, _MOVE_KW) else {"budget_change"}))
        if "cancel" in families and clashes:
            return f"{pid} cannot be stopped and get a {sorted(clashes)[0].replace('_', ' ')} at the same time"
        if "on_hold" in families and accelerate:
            return f"{pid} cannot be put on hold and accelerated at the same time"
        if "schedule" in families and accelerate and delay:
            return f"{pid} cannot be delayed and accelerated at the same time"
    return None


def _ambiguous_project(request: str, pid: str, projects: pd.DataFrame) -> list[str] | None:
    """None if the request names the project the LLM chose; else the candidate names to ask back with (up to 3).
    Only called when the parser recognised no project in the request."""
    tl = request.lower()
    names = dict(zip(projects["project_number"].astype(str), projects["project_name"].astype(str), strict=True)) \
        if not projects.empty else {}
    words = lambda n: {t for t in re.findall(r"[a-z0-9äöüß]+", n.lower()) if len(t) >= 3 and t not in _STOPWORDS}  # noqa: E731
    matched = [t for t in words(names.get(pid, "")) if re.search(rf"(?<![a-z0-9äöüß]){re.escape(t)}(?![a-z0-9äöüß])", tl)]
    if not matched:
        return closest_project_names(request, projects)
    candidates = sorted({n for n in names.values() if any(t in words(n) for t in matched)})
    return candidates[:3] if len(candidates) > 1 else None


def _ask_back(message: str) -> str:
    logger.info("Guard rail asked back instead of simulating")
    return _parser_error(message)


def apply_plan_guard_rails(request: str, plan: IntentPlan, projects: pd.DataFrame,
                           execute: Callable[[str, list[SubIntent]], Any]) -> tuple[IntentPlan, IntentPlan, list[str]]:
    """Checks the orchestrator's plan before any specialist runs. Returns (plan for the specialists, plan of record,
    notes for the paper). Intents the parser is certain about are executed here with `execute` (the pipeline's
    deterministic tool runner) and are not passed to the Simulator again. `projects` must be the caller's
    RLS-filtered view."""
    notes: list[str] = []
    others = [i for i in plan.sub_intents if i.intent_type != "simulation"]
    sims = [(i, _action_family(i), _resolved_ids(i)) for i in plan.sub_intents if i.intent_type == "simulation"]
    if any(s[1] == ASK_QUANTITY for s in sims):  # the rule-based fallback plan already decided to ask back
        _ask_back(ASK_QUANTITY_MESSAGE)
    sims = [s for s in sims if s[1] not in (UNRESOLVED_PROJECT, ASK_QUANTITY)]
    certain = certain_parser_intent(request, projects)
    executed: list[SubIntent] = []

    def result(kept: list[tuple[SubIntent, str, list[str]]]) -> tuple[IntentPlan, IntentPlan, list[str]]:
        kept_intents = [s[0] for s in kept]
        specialists = plan.model_copy(update={"sub_intents": others + kept_intents,
                                              "requires_simulation": bool(kept_intents)})
        record = plan.model_copy(update={"sub_intents": others + kept_intents + executed,
                                         "requires_simulation": bool(kept_intents or executed)})
        return specialists, record, notes

    if certain is not None:
        family, ids = certain.description.split(":", 1)[0], list(certain.project_ids)
        # Every LLM simulation of the same project (or the same portfolio cut) is replaced by the parser's intent
        replaced = [s for s in sims if (set(s[2]) & set(ids)) or (not ids and s[1] == family) or
                    (not s[2] and s[1] in (family, "unstructured"))]
        kept = [s for s in sims if s not in replaced]
        conflict = _plan_conflict(request, kept + [(certain, family, ids)])
        if conflict:
            _ask_back(f"INPUT ERROR: Conflicting changes – {conflict}. Please restate the request.")
            return result([])
        execute(request, [certain])
        executed.append(certain)
        agrees = any(s[1] == family and s[2][:len(ids)] == ids and _same_values(request, s[0].description, ids)
                     for s in replaced)  # same action, project and value
        notes.append((_PLAN_CONFIRMED if agrees else _PLAN_CORRECTED).format(intent=_intent_sentence(certain)))
        logger.info("Guard rail: parser intent %s executed deterministically (LLM agreed: %s)", family, agrees)
        sims = kept

    conflict = _plan_conflict(request, sims)
    if conflict:
        _ask_back(f"INPUT ERROR: Conflicting changes – {conflict}. Please restate the request.")
        return result([])

    kept = []
    parser_found = find_projects_in_text(request, projects)
    in_scenario = {str(c.get("project_id")) for c in ctx.get_ledger()}  # "cut it again" refers to an earlier step
    stop_and_move = _has(request.lower(), _CANCEL_KW)  # the amount is the stopped project's released budget
    for intent, family, ids in sims:
        foreign = [pid for pid in ids if pid not in parser_found and pid not in in_scenario]
        if parser_found and foreign:  # the request names projects, the LLM plans for another one
            _ask_back(f"INPUT ERROR: The plan changes {', '.join(_project_name(p) for p in foreign)}, which the request "
                      f"does not name (it names {', '.join(_project_name(p) for p in parser_found)}). Please confirm "
                      "the project.")
            continue
        if ids and not parser_found and ids[0] not in in_scenario and family != "risk_response":
            # the LLM chose a project the request does not name (a risk response names the risk, not the project)
            candidates = _ambiguous_project(request, ids[0], projects)
            if candidates is not None:
                _ask_back("INPUT ERROR: The request does not name the project unambiguously. Please name it by its "
                          "name or ID." + (f" Closest matches: {', '.join(candidates)}." if candidates else ""))
                continue
        needs_amount = family in _NEEDS_QUANTITY and not (stop_and_move and family in ("budget_transfer", "budget_change"))
        if needs_amount and not has_quantity(request):
            question = _QUESTION[family].format(name=_project_name(ids[0]) if ids else "the project")
            _ask_back(f"INPUT ERROR: The request does not say how much. {question}")
            continue
        reason = ctx.blocked_reason(ids[0]) if ids and family != "unstructured" else None
        if reason:  # quality gate: decided here, not by whether the LLM plans a simulation for a blocked project
            ctx.record_tool_result("simulation", "quality_gate", (
                f"ERROR: Data quality – {ids[0]} is blocked for quantitative simulation by the Data Steward ({reason}). "
                "Please fix the master data; a qualitative estimate via simulate_unstructured_scenario is possible."))
            continue
        structured = structured_parameters(intent, ids, request)
        if structured is not None:  # complete and checked: the code runs it, the Simulator assesses the result
            run = intent.model_copy(update={"project_ids": ids, "parameters": structured,
                                            "description": f"{structured['action']}: {intent.description}"})
            execute(request, [run])
            executed.append(run)
            notes.append(_PLAN_STRUCTURED.format(intent=_intent_sentence(run)))
            logger.info("Structured plan: %s executed deterministically", structured["action"])
            continue
        kept.append((intent, family, ids))
    return result(kept)


def _strip_fences(text: str) -> str:
    text = (text or "").strip()
    m = re.fullmatch(r"```(?:markdown|md)?\s*(.*?)\s*```", text, flags=re.DOTALL)
    return m.group(1).strip() if m else text
