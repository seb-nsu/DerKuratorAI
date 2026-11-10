"""Planning questions in the chat (“What drops out in 2027 at −10 %?”): a deterministic parser turns the question into
the input of a planning scenario, the scenario engine answers, the paper is written from its tables. No LLM decides –
the parser understands English and German like the change-request parser."""
from __future__ import annotations

import re
from datetime import date

import pandas as pd

from app.core.security import ROLE_PMO
from app.data_adapters.dataset import DEMAND_KEY, PortfolioDataset
from app.planning.model import horizon
from app.planning.scenario import DEFERRED, FORCED, IN, OUT, RANK, VALUE, ScenarioInput, ScenarioResult, compare, eur
from app.planning.steward import known_scopes

PLANNING_ROLES = (ROLE_PMO,)  # a scenario covers the whole portfolio
# a planning question names one of these (or a demand) …
_STRONG_KW = ("annual plan", "annual planning", "multi-year", "multiyear", "planning scenario", "planning year",
              "portfolio plan", "budget envelope", "jahresplan", "jahresplanung", "mehrjahres", "planungsszenario",
              "planungsjahr", "budgetrahmen", "portfolioplan")
# … or one of these everyday words together with a planning year or a percentage ("demand" is also skill demand)
_WEAK_KW = ("demand", "vorhaben", "envelope")
# … or asks what falls out of / fits into a planning year without naming a project
_OUT_KW = ("drops out", "drop out", "falls out", "fall out", "left out", "what fits", "fits into", "make the cut",
           "fällt", "faellt", "fallen raus", "rausfällt", "rausfaellt", "passt", "bleibt drin", "bleiben drin")
_PRJ_RE = re.compile(r"prj[-\s]?(?:demo[-\s]?)?\d{3,4}", re.IGNORECASE)
_PCT_RE = re.compile(r"([+\-−–]?)\s*(\d+(?:[.,]\d+)?)\s*(?:%|percent|prozent)", re.IGNORECASE)
_YEAR_RE = re.compile(r"\b(20\d\d)\b")
_DOWN_KW = ("cut", "less", "lower", "reduce", "smaller", "shrink", "minus", "kürz", "kuerz", "weniger", "senk",
            "kleiner", "reduzier")
_UP_KW = ("more", "increase", "raise", "larger", "bigger", "plus", "mehr", "erhöh", "erhoeh", "größer", "groesser")
_FORCE_KW = ("force", "must be in", "include", "anyway", "regardless", "trotzdem", "zwingend", "aufnehm", "reinnehm",
             "muss rein", "mit aufnehmen")
_EXCLUDE_KW = ("exclude", "without", "drop ", "remove", "ohne", "streich", "rausnehm", "herausnehm", "weglass")
_LATER_KW = ("later", "defer", "postpone", "push back", "später", "spaeter", "verschieb", "nach hinten")
_EARLIER_KW = ("earlier", "pull forward", "sooner", "früher", "frueher", "vorzieh")
_VALUE_KW = ("optimal", "optimis", "optimiz", "optimier", "most value", "best value", "maximum value", "knapsack",
             "meisten wert", "höchsten wert", "hoechsten wert", "größten nutzen", "groessten nutzen")


def _has(text: str, words: tuple[str, ...]) -> bool:
    return any(w in text for w in words)


def mentioned_demands(text: str, ds: PortfolioDataset) -> list[str]:
    """Demand numbers and full demand names in order of mention."""
    d = ds.demands
    if d.empty or DEMAND_KEY not in d.columns:
        return []
    tl, found = text.casefold(), {}
    for number, name in zip(d[DEMAND_KEY].astype(str), d.get("demand_name", pd.Series(index=d.index, dtype=object)),
                            strict=True):
        for cand in (number.casefold(), str(name).casefold() if isinstance(name, str) else None):
            if cand and (pos := tl.find(cand)) >= 0:
                found[number] = min(found.get(number, pos), pos)
    return sorted(found, key=found.get)


def is_planning_question(text: str, ds: PortfolioDataset, project_ids: list[str], as_of: date | None = None) -> bool:
    """A question about the annual plan – never a change request to a named project (an explicit project number
    always wins)."""
    tl = text.casefold()
    if _PRJ_RE.search(tl):
        return False
    if _has(tl, _STRONG_KW) or mentioned_demands(text, ds):
        return True
    first, last = horizon(as_of)
    planning_year = any(first <= int(y) <= last for y in _YEAR_RE.findall(tl))
    if _has(tl, _WEAK_KW) and (planning_year or _PCT_RE.search(tl)):
        return True
    return not project_ids and _has(tl, _OUT_KW) and planning_year


def _segment(tl: str, pos: int, length: int) -> str:
    """The part of the question around a mention, between separators – where its verb stands."""
    seps = (",", ";", " and ", " und ", ". ", "?")
    start = max(tl.rfind(sep, 0, pos) for sep in seps) + 1
    ends = [i for i in (tl.find(sep, pos + length) for sep in seps) if i >= 0]
    return tl[start:min(ends) if ends else len(tl)]


def parse_question(text: str, ds: PortfolioDataset, as_of: date | None = None) -> ScenarioInput:
    tl = text.casefold().replace("−", "-").replace("–", "-")
    factor = 0.0
    if m := _PCT_RE.search(tl):
        value = float(m.group(2).replace(",", "."))
        sign = m.group(1)
        if sign in ("-", "−", "–") or (not sign and _has(tl, _DOWN_KW) and not _has(tl, _UP_KW)):
            value = -value
        factor = value
    first, _ = horizon(as_of)
    demands = mentioned_demands(text, ds)
    plain = tl
    for did in demands:  # a demand number holds a year that is no planning year
        plain = plain.replace(did.casefold(), " ")
    years = sorted({int(y) for y in _YEAR_RE.findall(plain) if int(y) >= first - 1})
    year_from = years[0] if years else None
    year_to = years[-1] if len(years) > 1 else None
    scope = None
    for kind, names in sorted(known_scopes(ds).items()):
        for name in sorted(names, key=len, reverse=True):
            if name and re.search(rf"(?<![\w-]){re.escape(name.casefold())}(?![\w-])", tl):
                scope = f"{kind}:{name}"
                break
        if scope:
            break
    forced, excluded, moves = {}, {}, {}
    names = dict(zip(ds.demands[DEMAND_KEY].astype(str), ds.demands["demand_name"].astype(str).str.casefold(),
                     strict=True)) if demands else {}
    for did in demands:
        mention = did.casefold() if did.casefold() in tl else names[did]
        clause = _segment(tl, tl.find(mention), len(mention))
        if _has(clause, _EXCLUDE_KW):
            excluded[did] = "named in the question"
        elif _has(clause, _LATER_KW):
            moves[did] = 1
        elif _has(clause, _EARLIER_KW):
            moves[did] = -1
        elif _has(clause, _FORCE_KW):
            forced[did] = "named in the question"
    name = "Chat: " + text.strip()[:60]
    return ScenarioInput(name=name, year_from=year_from, year_to=year_to, budget_factor_pct=factor, scope=scope,
                         forced_in=forced, excluded=excluded, moves=moves, method=VALUE if _has(tl, _VALUE_KW) else RANK)


def _table(frame: pd.DataFrame, columns: list[str]) -> str:
    if frame.empty:
        return "–"
    head = "| " + " | ".join(columns) + " |\n|" + "---|" * len(columns)
    rows = ["| " + " | ".join("" if pd.isna(v) else str(v).replace("|", "/") for v in r) + " |"
            for r in frame[columns].itertuples(index=False)]
    return head + "\n" + "\n".join(rows)


def paper(result: ScenarioResult, question_input: ScenarioInput, rank_result: ScenarioResult | None = None) -> str:
    """The answer in the chat – written from the scenario tables (deterministic). With `rank_result` (a value-optimal
    question) it adds what the optimizer does differently from the rank order."""
    spec, items, binding = result.input, result.items, result.binding_year
    lines = [f"## Planning scenario {result.years[0]}–{result.years[-1]}", "",
             "**Understood as:** " + _understood(spec), ""]
    if items.empty:
        lines += ["No demand takes part in this scenario (none assessed or submitted within these years)."]
        return "\n".join(lines)
    counts = items["decision"].value_counts()
    lines += [f"**Result:** {counts.get(IN, 0) + counts.get(FORCED, 0)} in, {counts.get(DEFERRED, 0)} deferred, "
              f"{counts.get(OUT, 0)} out.", ""]
    out = items[items["decision"] == OUT]
    if not out.empty:
        lines += ["### Out of the plan", _table(out.assign(Demand=out[DEMAND_KEY], Name=out["demand_name"],
                                                            Reason=out["reason"]), ["Demand", "Name", "Reason"]), ""]
    deferred = items[items["decision"] == DEFERRED]
    if not deferred.empty:
        lines += ["### Deferred", _table(deferred.assign(Demand=deferred[DEMAND_KEY], Name=deferred["demand_name"],
                                                        Start=deferred["start"], Reason=deferred["reason"]),
                                         ["Demand", "Name", "Start", "Reason"]), ""]
    placed = items[items["decision"].isin([IN, FORCED])]
    if not placed.empty:
        money = placed[f"CapEx {binding}"] + placed[f"OpEx {binding}"]
        lines += [f"### In the plan ({eur(float(money.sum()))} in {binding})",
                  _table(placed.assign(Demand=placed[DEMAND_KEY], Name=placed["demand_name"], Start=placed["start"],
                                       Decision=placed["decision"]), ["Demand", "Name", "Start", "Decision"]), ""]
    tight = result.budget[(result.budget["Year"] == binding) & (result.budget["Used %"].fillna(0) >= 90)] \
        if not result.budget.empty else result.budget
    if not tight.empty:
        lines += [f"### Tight envelopes {binding}", _table(tight.assign(
            Area=tight["Scope type"] + " " + tight["Scope"], Used=tight["Used %"].map(lambda u: f"{u:.0f} %"),
            Free=tight["Free"].map(eur)), ["Area", "Kind", "Used", "Free"]), ""]
    if rank_result is not None:
        summary, _, changes = compare(rank_result, result)
        lines += ["### What the optimizer does differently from the rank order", *[f"- {s}" for s in summary], ""]
        if not changes.empty:
            lines += [_table(changes, ["Demand", "Name", "Rank order", "Value-optimal", "Why (value-optimal)"]), ""]
    how = "value-optimal choice" if result.input.method == VALUE else "rank order"
    lines += [f"Calculated by the planning engine ({how}, budget envelopes, capacity limits) – the verdict is "
              "rule-based. Open the Planning page to change, compare or save the scenario."]
    return "\n".join(lines)


def _understood(spec: ScenarioInput) -> str:
    parts = [f"budget envelopes {spec.budget_factor_pct:+g} %" if spec.budget_factor_pct else "budget envelopes as "
             "approved"]
    if spec.scope:
        parts.append(spec.scope.replace("_", " ").replace(":", " "))
    if spec.forced_in:
        parts.append("forced in " + ", ".join(spec.forced_in))
    if spec.excluded:
        parts.append("without " + ", ".join(spec.excluded))
    if spec.moves:
        parts.append(", ".join(f"{d} {'later' if m > 0 else 'earlier'}" for d, m in spec.moves.items()))
    if spec.method == VALUE:
        parts.append("value-optimal choice of the demands")
    return " · ".join(parts)
