"""Checks the text the Advisory wrote before it reaches the reader: every amount and percentage must come from the
facts the agents were given (request, portfolio table, tool results, verdict), and a verdict word outside the
recommendation line must be the rule-based verdict. The check changes nothing in the paper – it reports what it finds,
so a figure the model computed or invented is visible as such."""
from __future__ import annotations

import re

from app.crew.verdict import Verdict

_UNITS = {"k": 1e3, "tsd": 1e3, "thousand": 1e3, "m": 1e6, "mio": 1e6, "million": 1e6, "millions": 1e6,
          "bn": 1e9, "billion": 1e9}
_NUM = r"(\d{1,3}(?:[,.]\d{3})+(?:\.\d+)?|\d+(?:[.,]\d+)?)"
_UNIT = r"\s*(k|tsd|thousand|mio|millions?|bn|billion|m)?\b"
_MONEY_RE = re.compile(rf"(?:€|eur\s?)\s?{_NUM}{_UNIT}|{_NUM}{_UNIT}\s*(?:€|eur\b|euro)", re.IGNORECASE)
_PCT_RE = re.compile(rf"{_NUM}\s*(?:%|percent|prozent)", re.IGNORECASE)
_ANY_RE = re.compile(rf"{_NUM}{_UNIT}", re.IGNORECASE)
_VERDICT_RE = re.compile(r"\b(CONDITIONAL GO|NO[- ]GO|GO)\b")
_RECOMMENDATION = "**Recommendation:**"


def _value(number: str) -> tuple[float, float]:
    """(value, half the step of its last shown digit) – '1.85' -> (1.85, 0.005); '1,850,000' -> (1850000, 0.5)."""
    text = number
    if re.fullmatch(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?", text):
        text = text.replace(",", "")
    elif re.fullmatch(r"\d{1,3}(?:\.\d{3})+", text):  # German thousands
        text = text.replace(".", "")
    else:
        text = text.replace(",", ".")
    decimals = len(text.split(".")[1]) if "." in text else 0
    return float(text), 0.5 * 10 ** -decimals


def _source_values(sources: str) -> list[float]:
    return [_value(n)[0] * _UNITS.get((u or "").lower(), 1) for n, u in _ANY_RE.findall(sources)]


def _known(value: float, step: float, known: list[float]) -> bool:
    tolerance = max(step, abs(value) * 0.005)
    return any(abs(abs(value) - abs(k)) <= tolerance for k in known)


def unknown_figures(text: str, sources: str) -> list[str]:
    """Amounts and percentages of `text` that no number in `sources` matches (to the precision shown)."""
    known = _source_values(sources)
    found: list[str] = []
    for m in _MONEY_RE.finditer(text):
        number, unit = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
        value, step = _value(number)
        factor = _UNITS.get((unit or "").lower(), 1)
        if not _known(value * factor, step * factor, known):
            found.append(m.group(0).strip())
    for m in _PCT_RE.finditer(text):
        value, step = _value(m.group(1))
        if not _known(value, step, known):
            found.append(m.group(0).strip())
    return list(dict.fromkeys(found))


def foreign_verdict_words(text: str, verdict: Verdict, sources: str = "") -> list[str]:
    """Verdict words outside the recommendation line that are neither the rule-based verdict nor in the sources (a
    reason may name the NO-GO a threshold would cause)."""
    own = verdict.decision.replace("NO GO", "NO-GO")
    allowed = {own} | {w.replace("NO GO", "NO-GO") for w in _VERDICT_RE.findall(sources)}
    words = []
    for line in text.splitlines():
        if _RECOMMENDATION in line:
            continue
        words += [w.replace("NO GO", "NO-GO") for w in _VERDICT_RE.findall(line)]
    return list(dict.fromkeys(w for w in words if w not in allowed))


def check_advisory(text: str, sources: str, verdict: Verdict) -> list[str]:
    """Findings for the reader – empty when the text sticks to the facts."""
    notes = []
    figures = unknown_figures(text, sources)
    if figures:
        shown = ", ".join(figures[:5]) + (f" and {len(figures) - 5} more" if len(figures) > 5 else "")
        notes.append(f"Advisory check: the AI-written text names {shown}, which no tool result or input contains – "
                     "read these as the AI's own arithmetic, not as calculated figures.")
    words = foreign_verdict_words(text, verdict, sources)
    if words:
        notes.append(f"Advisory check: the AI-written text mentions {', '.join(words)} although the rule-based verdict "
                     f"is {verdict.label} – the verdict above applies.")
    return notes
