"""Deterministic intent parser: understands a change request in English and German (projects, amounts, percentages,
months, teams, PMO actions) and turns it into an IntentPlan. Used offline and as the guard rail of the LLM plan."""
from __future__ import annotations

import logging
import re
from collections import Counter
from datetime import date
from difflib import SequenceMatcher

import pandas as pd

from app.core.stage_gates import normalize_phase
from app.crew.tasks import IntentPlan, SubIntent
from app.crew.tools import context as ctx

logger = logging.getLogger(__name__)


# =========================================================================== Heuristics (intent parser)
# Free-text parser understands English and German (keywords match at word start, i.e. as prefixes).
_STOPWORDS = {"app", "and", "the", "der", "die", "das", "program", "project"}
_CANCEL_KW = ("stop", "abbrech", "abbruch", "cancel", "einstell", "beend", "kill", "terminate", "halt")
_MOVE_KW = ("verschieb", "umschicht", "transfer", "übertrag", "uebertrag", "gib", "geben", " zu ", " nach ", " an ",
            "move", "shift", "reallocat", "give", " to ")
_RESOURCE_KW = ("fte", "personal", "ressourc", "mitarbeiter", "entwickler", "köpfe", "resource", "people", "staff",
                "developer", "headcount")
_DELAY_KW = ("verzöger", "verzoeger", "delay", "später", "spaeter", "nach hinten", "verschieb", "rutscht", "later",
             "slip", "postpone", "push back")
_ACCEL_KW = ("früher", "frueher", "beschleunig", "vorzieh", "schneller", "earlier", "accelerat", "pull forward",
             "faster", "sooner")
_UP_KW = ("erhöh", "erhoeh", "aufstock", "mehr", "plus", "zusätzlich", "zusaetzlich", "increase", "raise", "add",
          "more", "top up", "grow")
_DOWN_KW = ("kürz", "kuerz", "reduzier", "senk", "streich", "minus", "spar", "weniger", "abzieh", "verlier", "ausfall",
            "fällt", "rausnehm", "herausnehm", "wegnehm", "abspeck", "cut", "reduce", "decrease", "lower", "lose", "loses",
            "drop", "less", "shrink", "trim", "slash", "remove", "withdraw", "scale back", " out of ")
_GRAPH_KW = ("abhängig", "abhaengig", "domino", "blockiert", "wartet", "task", "depend", "blocked", "waits")
_TEAM_KW = ("engpass", "engpäss", "engpaess", "überlast", "ueberlast", "auslastung", "bottleneck", "überbucht",
            "overload", "utili", "overbooked")
_CLUSTER_KW = ("klumpen", "cluster", "systemisch", "konzentration", "systemic", "concentration")
_HOLD_KW = ("on hold", "hold", "pause", "pausier", "freeze", "einfrier", "ruhen", "zurückstell", "zurueckstell")
# a cut that minimises the strategic value lost (knapsack) instead of the priority ranking
_VALUE_CUT_KW = ("value-optimal", "value optimal", "optimal", "knapsack", "strategic value", "strategisch",
                 "strategic fit", "strategy score", "wertoptimal", "least value")
_SAVINGS_KW = ("savings", "einspar", "sparziel", "cut list", "kürzungsliste", "kuerzungsliste", "save ")
_RESUME_KW = ("resume", "restart", "reactivat", "unpause", "re-start", "wieder aufnehm", "wiederaufnahm", "fortsetz",
              "reaktivier", "neustart", "neu start", "wieder anlauf", "wieder anfahr", "wieder starten")
_STRESS_KW = ("stress", "inflation", "teuerung", "kostensteiger", "preissteiger", "lohnsteiger", "tarifsteiger",
              "cost increase", "costs rise", "costs go up", "price increase", "wage increase", "costs increase")
_REBASELINE_KW = ("rebaselin", "re-baselin", "new baseline", "neue baseline", "neubaselin", "baseline neu",
                  "baseline anpass", "baseline update", "update the baseline", "neuer basisplan", "basisplan neu")
_GATE_KW = ("stage gate", "stage-gate", "gate", "phasentor", "next phase", "nächste phase", "naechste phase",
            "phasenwechsel", "phase transition", "phasenübergang", "phasenuebergang")
_RESERVE_KW = ("management reserve", "managementreserve", "management-reserve", "reserve", "rücklage", "ruecklage",
               "contingency")
_RETURN_KW = ("return", "give back", "pay back", "release", "zurück", "zurueck", "freigeb", "rückführ", "rueckfuehr")
_SKILL_KW = ("skill", "kompetenz", "qualifikation", "fähigkeit", "faehigkeit")
_MERGE_KW = ("merge", "zusammenleg", "zusammenführ", "zusammenfuehr", "fusionier", "absorb", "aufgehen in",
             "consolidate into", "fold into")
_BENEFIT_KW = ("benefit realis", "benefit realiz", "benefits realis", "benefits realiz", "realised benefit",
               "realized benefit", "benefit review", "benefit tracking", "nutzenrealisierung", "nutzen realisiert",
               "realisierte nutzen", "realisierten nutzen", "nutzenprüfung", "nutzenpruefung", "nutzen-review",
               "nutzen erreicht", "benefits achieved", "benefit achieved")
_RISK_RESPONSE_KW = {"Mitigate": ("mitigat", "mitigier", "minder", "reduce the risk", "risiko senk", "risiko reduz"),
                     "Accept": ("accept", "akzeptier", "hinnehm"),
                     "Avoid": ("avoid", "vermeid", "eliminat"),
                     "Transfer": ("transfer the risk", "insur", "versicher", "risiko übertrag", "risiko uebertrag",
                                  "outsourc", "auslager")}
_LEVEL_WORDS = {"Low": ("low", "niedrig", "gering"), "Moderate": ("moderate", "medium", "mittel"),
                "High": ("high", "hoch")}
_RISK_RE = re.compile(r"rsk[-\s]?(?:demo[-\s]?)?(\d{3,5})", re.IGNORECASE)
DEFAULT_HOLD_MONTHS = 3


CAPACITY_HINTS = ("kapazit", "team", "ressourc", "fte", "capacity", "resource")
SCOPE_HINTS = ("scope", "umfang", "backlog", "story")


def _has(text: str, keywords: tuple[str, ...]) -> bool:
    """Keyword match at word start (prefix): 'cut' matches 'cuts' but not 'execute'."""
    return any(k in text if k.startswith(" ") else re.search(rf"(?<![a-zäöüß]){re.escape(k)}", text) for k in keywords)
_CLUSTER_NAMES = {
    "capacity": ("capacity", "kapazität", "kapazitaet"), "supplier": ("supplier", "lieferant", "dienstleister"),
    "data": ("data", "daten"), "governance": ("governance",), "requirements": ("requirement", "anforderung"),
    "scope": ("scope", "umfang"),
}


def _to_float(raw: str) -> float:
    s = raw.strip()
    if re.fullmatch(r"\d{1,3}(,\d{3})+(\.\d+)?", s):  # English thousands separator: 100,000 / 1,250,000.50
        s = s.replace(",", "")
    elif "." in s and "," in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(\.\d{3})+", s):
        s = s.replace(".", "")
    return float(s)


def _tokens(name: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9äöüß\-]+", name.lower()) if t not in _STOPWORDS and len(t) >= 4]


def find_projects_in_text(text: str, projects: pd.DataFrame) -> list[str]:
    """Project IDs in order of mention (ID, full name or unique name token)."""
    tl = text.lower()
    positions: dict[str, int] = {}
    ids = projects["project_number"].astype(str).tolist() if not projects.empty else []

    # Always include explicit IDs – even outside the permission, so the tools
    # visibly report "no permission" instead of silently ignoring the reference.
    for m in re.finditer(r"prj[-\s]?(?:demo[-\s]?)?(\d{3,4})", tl):
        digits = m.group(1)
        pid = next((i for i in ids if i.endswith(digits)), f"PRJ-DEMO-{digits}")
        positions[pid] = min(positions.get(pid, m.start()), m.start())

    names = dict(zip(ids, projects["project_name"].astype(str), strict=True)) if ids else {}
    token_counts = Counter(t for n in names.values() for t in set(_tokens(n)))
    for pid, name in names.items():
        for cand in [name.lower()] + [t for t in _tokens(name) if token_counts[t] == 1]:
            m = re.search(rf"(?<![\w-]){re.escape(cand)}(?![\w-])", tl)
            if m:
                positions[pid] = min(positions.get(pid, m.start()), m.start())
    return sorted(positions, key=positions.get)


def _parse_amount_eur(text: str) -> float | None:
    tl = text.lower()
    factor = {"mio": 1e6, "mio.": 1e6, "millionen": 1e6, "million": 1e6, "millions": 1e6, "m": 1e6, "mrd": 1e9,
              "bn": 1e9, "k": 1e3, "tsd": 1e3, "tsd.": 1e3, "tausend": 1e3, "thousand": 1e3}
    unit = r"(mio\.?|millionen|millions?|mrd|bn|m\b|k\b|tsd\.?|tausend|thousand)"
    m = re.search(rf"(?:€|eur\b)\s*(\d+(?:[.,]\d+)*)\s*{unit}?", tl)  # English style: €100k / EUR 1.5m
    if m:
        return _to_float(m.group(1)) * factor.get((m.group(2) or "").strip(), 1)
    m = re.search(rf"(\d+(?:[.,]\d+)*)\s*{unit}\s*(?:€|eur|euro)?", tl)
    if m:
        return _to_float(m.group(1)) * factor.get(m.group(2).strip(), 1)
    m = re.search(r"(\d+(?:[.,]\d+)*)\s*(?:€|eur\b|euro)", tl)
    return _to_float(m.group(1)) if m else None


def _parse_percent(text: str, near: tuple[str, ...] = ()) -> float | None:
    """Percentage value; with `near` only if one of the keywords is in the same clause."""
    for m in re.finditer(r"([+-]?\d+(?:[.,]\d+)?)\s*(?:%|prozent|percent)", text.lower()):
        window = text.lower()[max(0, m.start() - 40): m.end() + 40]
        if not near or any(k in window for k in near):
            return _to_float(m.group(1))
    return None


def _parse_fte(text: str) -> float | None:
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:fte|personen|mitarbeiter|entwickler|köpfe|people|staff|developers?|heads)", text.lower())
    return _to_float(m.group(1)) if m else None


def _parse_months(text: str) -> int | None:
    m = re.search(r"([+-]?\d+)\s*(?:monat|month)", text.lower())
    return int(m.group(1)) if m else None


def _parse_team(text: str) -> str:
    m = re.search(r"team\s+([a-zäöü]+)", text, flags=re.IGNORECASE)
    return f"Team {m.group(1).capitalize()}" if m else ""


def _parse_cost_type(text: str) -> str:
    tl = text.lower()
    if "capex" in tl or "investiv" in tl:
        return "capex"
    if "opex" in tl or "betrieb" in tl or "operating" in tl:
        return "opex"
    return "total"


def _signed(value: float, text: str) -> float:
    tl = text.lower()
    if _has(tl, _DOWN_KW):
        return -abs(value)
    return abs(value) if _has(tl, _UP_KW) else value


def _parse_date(text: str) -> str | None:
    """An explicit date: 2027-03-31, 31.03.2027 or 31/03/2027 -> ISO text."""
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if m:
        y, mo, d = m.groups()
    else:
        m = re.search(r"(\d{1,2})[./](\d{1,2})[./](\d{4})", text)
        if not m:
            return None
        d, mo, y = m.groups()
    try:
        return date(int(y), int(mo), int(d)).isoformat()
    except ValueError:
        return None


def _parse_phase(text: str) -> str | None:
    """The stage-gate phase the request moves to ("into Executing", "in die Umsetzung")."""
    for word in re.findall(r"[a-zäöüß]+", text.lower()):
        phase = normalize_phase(word) or {"umsetzung": "Executing", "abschluss": "Closing", "planung": "Planning",
                                          "initiierung": "Initiation", "abgeschlossen": "Closed"}.get(word)
        if phase and word not in ("plan", "close", "build", "demand", "idea", "concept", "design"):
            return phase
    return None


def _parse_risk(text: str) -> str | None:
    m = _RISK_RE.search(text)
    if not m:
        return None
    risks = ctx.get_table("Risks")
    digits = m.group(1)
    known = [str(r) for r in risks["risk_number"]] if not risks.empty else []
    return next((r for r in known if r.endswith(digits)), f"RSK-DEMO-{digits}")


def _risk_project(risk: str) -> list[str]:
    risks = ctx.get_table("Risks")
    match = risks[risks["risk_number"].astype(str) == risk] if not risks.empty else risks
    return [str(match.iloc[0]["project_number"])] if not match.empty else []


def _parse_response(text: str) -> str | None:
    tl = text.lower()
    return next((r for r, kws in _RISK_RESPONSE_KW.items() if _has(tl, kws)), None)


def _parse_level(text: str) -> str | None:
    """The probability a risk response leaves ("down to low", "auf niedrig")."""
    m = re.search(r"(?:to|auf|on|zu)\s+(?:a\s+|an\s+)?([a-zäöü]+)", text.lower())
    words = [m.group(1)] if m else []
    for word in words:
        level = next((lv for lv, kws in _LEVEL_WORDS.items() if word.startswith(kws)), None)
        if level:
            return level
    return None


def _known_skills() -> list[str]:
    try:
        table = ctx.get_dataset().team_skills
    except Exception:  # noqa: BLE001 - no skill data, no skill reading
        return []
    return sorted(set(table["skill"].astype(str)), key=len, reverse=True) if not table.empty else []


def _parse_skill(text: str) -> str | None:
    tl = text.lower()
    return next((s for s in _known_skills() if re.search(rf"(?<![\w]){re.escape(s.lower())}(?![\w])", tl)), None)


UNRESOLVED_PROJECT = "unresolved_project"
ASK_QUANTITY = "ask_quantity"
ASK_QUANTITY_MESSAGE = ("INPUT ERROR: The request does not say what to change or by how much. Please state the change "
                        "(stop, hold, budget, schedule or FTE) and its size – the amount, percentage or months.")


def is_unresolved_change_request(request: str, projects: pd.DataFrame) -> bool:
    """A change request (budget up/down, stop, hold, delay/accelerate by months) that names no recognisable
    project. It must never be answered as a pure analysis: the verdict would read NO DECISION REQUIRED although the
    user asked for a change. Analysis questions (dependencies, clusters, team bottlenecks) are not change requests."""
    return not find_projects_in_text(request, projects) and is_change_request(request)


def is_change_request(request: str) -> bool:
    """The request asks for a change (budget up/down, stop, hold, delay/accelerate by months) – with or without a
    recognisable project. Analysis questions (dependencies, clusters, team bottlenecks) are not change requests."""
    tl = request.lower()
    has = lambda kws: _has(tl, kws)  # noqa: E731
    if has(_GRAPH_KW) or has(_CLUSTER_KW) or has(_TEAM_KW):
        return False
    if has(_SAVINGS_KW) and _parse_amount_eur(request):  # portfolio cut – needs no project name
        return False
    if _has(tl, _STRESS_KW) and _parse_percent(request) is not None:  # a stress test changes nothing
        return False
    if _has(tl, _BENEFIT_KW):  # a review changes nothing
        return False
    if (_has(tl, _RESUME_KW) or _has(tl, _REBASELINE_KW) or _has(tl, _GATE_KW) or _has(tl, _MERGE_KW)
            or (_has(tl, _RESERVE_KW) and _parse_amount_eur(request))):
        return True
    budget_topic = "budget" in tl or "capex" in tl or "opex" in tl
    months = _parse_months(request)
    return ((budget_topic and (has(_UP_KW) or has(_DOWN_KW))) or has(_CANCEL_KW) or has(_HOLD_KW)
            or (months is not None and (has(_DELAY_KW) or has(_ACCEL_KW))))


def closest_project_names(request: str, projects: pd.DataFrame, limit: int = 3, cutoff: float = 0.3) -> list[str]:
    """Up to `limit` project names most similar to a phrase of the request. `projects` must be the caller's
    RLS-filtered view, so a Project Manager is never shown the names of projects outside their scope."""
    names = sorted(set(projects["project_name"].dropna().astype(str))) if not projects.empty else []
    words = re.findall(r"[\wäöüß\-]+", request.lower())
    scored = []
    for name in names:
        size = max(1, len(name.split()))  # compare with request phrases of the same length as the name
        phrases = [" ".join(words[i:i + size]) for i in range(max(1, len(words) - size + 1))]
        score = max(SequenceMatcher(None, phrase, name.lower()).ratio() for phrase in phrases)
        if score >= cutoff:
            scored.append((score, name))
    return [name for _, name in sorted(scored, key=lambda s: (-s[0], s[1]))[:limit]]


def unresolved_project_message(request: str, projects: pd.DataFrame) -> str:
    """One line (the verdict engine quotes the first line of an INPUT ERROR)."""
    message = ("INPUT ERROR: No project you have access to was recognised in the request. Please name the project "
               "by its name or ID (e.g. PRJ-DEMO-1002).")
    suggestions = closest_project_names(request, projects)
    return message + (f" Closest matches: {', '.join(suggestions)}." if suggestions else "")


def heuristic_plan(request: str, projects: pd.DataFrame) -> IntentPlan:
    tl = request.lower()
    found = find_projects_in_text(request, projects)
    intents: list[SubIntent] = []
    has = lambda kws: _has(tl, kws)  # noqa: E731
    sim = lambda d, ids: intents.append(SubIntent(intent_type="simulation", description=d, project_ids=ids))  # noqa: E731
    graph = lambda d, ids: intents.append(SubIntent(intent_type="graph_analysis", description=d, project_ids=ids))  # noqa: E731

    # A savings target without named projects is a portfolio cut; a pause is an on hold (never a cancellation)
    if has(_SAVINGS_KW) and not found and _parse_amount_eur(request):
        sim("portfolio_cut", [])
        graph("cut_dependents", [])
    # PMO actions first: each one is the whole change, so the generic budget/schedule readings stay off
    risk = _parse_risk(request)
    risk_response = risk is not None and _parse_response(request) is not None
    if risk_response:
        sim(f"risk_response:{risk}", _risk_project(risk))
    resume = bool(found) and has(_RESUME_KW)  # "resume after the hold" is a resume, not a hold
    if resume:
        sim(f"resume:{found[0]}", [found[0]])
    stress = not found and has(_STRESS_KW) and _parse_percent(request) is not None
    if stress:
        sim("cost_stress", [])
    rebaseline = bool(found) and has(_REBASELINE_KW)
    if rebaseline:
        sim(f"rebaseline:{found[0]}", [found[0]])
    gate = bool(found) and not rebaseline and (has(_GATE_KW) or (_parse_phase(request) is not None and has(
        ("advance", "move", "überführ", "ueberfuehr", "wechsel", "bring", "ready for", "bereit für", "proceed"))))
    if gate:
        sim(f"stage_gate:{found[0]}", [found[0]])
    reserve = bool(found) and has(_RESERVE_KW) and not risk_response and not rebaseline
    if reserve:
        sim(f"{'reserve_return' if has(_RETURN_KW) else 'reserve_draw'}:{found[0]}", [found[0]])
    skill = _parse_skill(request)
    skill_move = len(found) >= 2 and skill is not None and _parse_fte(request) is not None
    if skill_move:
        sim(f"skill_transfer:{found[0]}->{found[1]}", found[:2])
    benefit = bool(found) and has(_BENEFIT_KW) and not (resume or rebaseline or gate or reserve)
    if benefit:
        sim(f"benefit_review:{found[0]}", [found[0]])
    merge = len(found) >= 2 and has(_MERGE_KW) and not (risk_response or rebaseline or gate or reserve or skill_move)
    if merge:  # "merge A into B": the first project named is absorbed by the second
        sim(f"merge:{found[0]}->{found[1]}", found[:2])
    pmo = risk_response or resume or rebaseline or gate or reserve or skill_move or benefit or merge

    hold = bool(found) and has(_HOLD_KW) and not pmo
    if hold:
        sim(f"on_hold:{found[0]}", [found[0]])
    cancel = bool(found) and has(_CANCEL_KW) and not hold and not pmo
    if cancel:
        sim(f"cancel:{found[0]}", [found[0]])
    if len(found) >= 2 and "budget" in tl and has(_MOVE_KW) and not pmo:
        sim(f"budget_transfer:{found[0]}->{found[1]}", found[:2])
    if len(found) >= 2 and has(_RESOURCE_KW) and _parse_fte(request) is not None and not pmo:
        sim(f"resource_transfer:{found[0]}->{found[1]}", found[:2])
    capacity = _parse_percent(request, CAPACITY_HINTS)
    scope = _parse_percent(request, SCOPE_HINTS)
    months = _parse_months(request)
    if found and not cancel and not hold and not pmo and (capacity is not None or scope is not None or (months is not None and (has(_DELAY_KW) or has(_ACCEL_KW)))):
        sim(f"schedule:{found[0]}", [found[0]])
    budget_topic = "budget" in tl or "capex" in tl or "opex" in tl
    already_budget = any(i.description.startswith(("budget_transfer", "cancel")) for i in intents)
    if found and budget_topic and not already_budget and not pmo and (has(_UP_KW) or has(_DOWN_KW)):
        sim(f"budget_change:{found[0]}", [found[0]])
    if found and not intents and not has(_GRAPH_KW) and not has(_CLUSTER_KW) and not has(_TEAM_KW):
        # an unsized change is asked back, as in LLM mode; only a sized one gets the qualitative fallback
        sim(f"{ASK_QUANTITY}:{found[0]}" if not has_quantity(request) else f"unstructured:{found[0]}", [found[0]])
    if not intents and is_unresolved_change_request(request, projects):  # ask back instead of a silent analysis
        sim(UNRESOLVED_PROJECT, [])
    if skill_move or (not intents and has(_SKILL_KW)):
        intents.append(SubIntent(intent_type="graph_analysis", description="skill_capacity", project_ids=[],
                                 parameters={"skill": skill}))

    if found:
        graph("downstream/upstream", found[:3])
    if cancel:
        graph("task_impact", [found[0]])
    if has(_TEAM_KW) or (has(_RESOURCE_KW) and len(found) >= 2):
        graph("team_bottlenecks", [])
    if (has(_CLUSTER_KW) or not found) and not stress and not risk_response \
            and not any(i.description == "skill_capacity" for i in intents):
        graph("cluster_risk", [])

    return IntentPlan(
        summary=f"rule-based reading, projects: {', '.join(found) or 'none'}",
        sub_intents=intents,
        requires_simulation=any(i.intent_type == "simulation" for i in intents),
        requires_graph_analysis=any(i.intent_type == "graph_analysis" for i in intents),
    )


# =========================================================================== Guard rails around the LLM plan
# The LLM plan is checked deterministically before any specialist runs: a plan the parser is certain about wins,
# a missing amount is asked back instead of invented, and a data-quality block never depends on the LLM.
_QUANTITY_WORDS = (
    "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve", "twenty", "thirty",
    "forty", "fifty", "sixty", "seventy", "eighty", "ninety", "hundred", "thousand", "million", "billion", "grand",
    "dozen", "half", "halve", "double", "triple", "third", "quarter", "fifth", "tenth",
    "zwei", "drei", "vier", "fünf", "fuenf", "sechs", "sieben", "acht", "neun", "zehn", "elf", "zwölf", "zwoelf",
    "zwanzig", "dreißig", "dreissig", "vierzig", "fünfzig", "fuenfzig", "hundert", "tausend", "millionen",
    "hälfte", "haelfte", "halb", "halbieren", "verdoppeln", "drittel", "viertel", "fünftel", "fuenftel", "zehntel",
    "quartal",
)  # "one" / "ein" are left out: they rarely size a change and would make vague requests look precise
_QUANTITY_RE = re.compile(r"(?<![a-zäöüß])(?:" + "|".join(_QUANTITY_WORDS) + r")s?(?![a-zäöüß])")
_PROJECT_ID_RE = re.compile(r"prj[-\s]?(?:demo[-\s]?)?\d{3,4}")


def has_quantity(request: str) -> bool:
    """True if the request states a size – digits (project IDs aside) or a number word ("a fifth", "Viertel")."""
    tl = _PROJECT_ID_RE.sub(" ", request.lower())
    return bool(re.search(r"\d", tl) or _QUANTITY_RE.search(tl))
