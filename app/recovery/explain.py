"""The written part of a recommendation. The code always writes it (offline, from the calculated figures), so the red
trigger works fully without an LLM. With a configured model, the Advisory model may add a short readable text and
ideas for levers the catalogue does not know. It may not calculate, rank or change a verdict: every figure of its text
is checked against the calculated figures (app/crew/advisory_check.py) and findings are shown next to it; ideas appear
only as "[AI ESTIMATE]" hints, never as an option. No person is named (PII masking as everywhere)."""
from __future__ import annotations

import json
import logging
from typing import Any

from app.core.security import mask_pii_data

logger = logging.getLogger("derkurator.recovery.explain")

IDEA_MARKER = "[AI ESTIMATE]"


def _eur(value: Any) -> str:
    try:
        return f"€{float(value):,.0f}"
    except (TypeError, ValueError):
        return "–"


def _pct(value: Any) -> str:
    return "–" if value is None else f"{float(value):g} %"


def option_line(o: dict[str, Any]) -> str:
    r = o.get("result") or {}
    closure = r.get("closure") or {}
    parts = [f"cost gap closed {_pct(closure.get('cost_pct'))}" if "cost_pct" in closure else "",
             f"schedule gap closed {_pct(closure.get('schedule_pct'))}" if "schedule_pct" in closure else "",
             f"collateral: {', '.join(r.get('collateral') or []) or 'none'}",
             f"portfolio within budget {_pct(r.get('probability_before_pct'))} → {_pct(r.get('probability_after_pct'))}"
             if r.get("probability_after_pct") is not None else ""]
    verdict = (r.get("verdict") or {}).get("label") or r.get("decision") or "–"
    return f"- **{o['key']} {o['label']}** – {verdict}; " + "; ".join(p for p in parts if p)


def offline_text(rec: dict[str, Any]) -> str:
    impact = rec.get("impact") or {}
    budget = impact.get("budget") or {}
    lines = [f"## Red trigger: {rec['project_id']} ({rec['project_name']})",
             f"**Trigger:** {rec.get('trigger_label')} · reported {rec.get('reported') or '–'}, calculated "
             f"{rec.get('calculated')} · as of {rec.get('as_of')}", "", "### Situation"]
    lines += [f"- {c['text']}" for c in rec.get("causes") or []] or ["- No cause in the numbers."]
    if rec.get("report_hint"):
        lines.append(f"- Latest status report (free text, a hint only): {rec['report_hint']}")
    lines += ["", "### Impact on the portfolio"]
    deps = impact.get("dependents") or []
    if deps:
        lines.append("- Dependents: " + ", ".join(f"{d['id']}{' (red)' if d.get('red') else ''}" for d in deps))
    if impact.get("upstream_red"):
        lines.append(f"- Held back by red predecessor(s): {', '.join(impact['upstream_red'])}")
    for b in impact.get("deadline_breaches") or []:
        lines.append(f"- {b['successor']} needs the result by {b['required_by']} – {b['days_late']} days late at the "
                     "expected delay")
    for t in impact.get("teams") or []:
        if t.get("red_neighbours"):
            lines.append(f"- {t['team']} is shared with red project(s) {', '.join(t['red_neighbours'])} – moving its "
                         "capacity takes it from a red neighbour")
    if budget:
        lines.append(f"- Cost gap {_eur(budget.get('gap_eur'))}; reserve {_eur(budget.get('reserve_balance_eur'))}, "
                     f"free above the floor {_eur(budget.get('reserve_free_eur'))}")
    portfolio = impact.get("portfolio") or {}
    if portfolio.get("probability_within_budget_pct") is not None:
        lines.append(f"- Portfolio within budget: {_pct(portfolio['probability_within_budget_pct'])} (Monte Carlo, "
                     f"seed {portfolio.get('seed')})")
    lines += ["", "### Options (each calculated as one package)"]
    lines += [option_line(o) for o in rec.get("options") or []]
    flagged = [o["key"] for o in rec.get("options") or [] if o.get("threshold_flag")]
    if flagged:
        lines.append(f"- ⚠ Option(s) {', '.join(flagged)}: a single step stays just below the board approval threshold "
                     "– named on purpose; the board threshold applies to the package sum.")
    if rec.get("recommended"):
        lines += ["", f"**Recommended option: {rec['recommended']}** (best gap closure with the least collateral among "
                      "the options with GO or CONDITIONAL GO). A person decides; adopting opens it in the Steering "
                      "Console, the proposal is submitted by that person and approved by a second PMO."]
    elif rec.get("options"):
        lines += ["", "**No option qualifies** (no package with GO or CONDITIONAL GO) – the PMO decides on the "
                      "reference and the rejected measures below."]
    if rec.get("rejected"):
        lines += ["", "### Checked and rejected"]
        lines += [f"- {r['label']}: {r['why']}" for r in rec["rejected"]]
    if rec.get("not_possible"):
        lines += ["", "### Not possible with the data"]
        lines += [f"- {n['label']}: {n['reason']}" for n in rec["not_possible"]]
    if rec.get("forced"):
        lines += ["", f"_Master data of {', '.join(rec['forced'])} is incomplete (Data Steward); its figures are "
                      "indicative and every option carries the condition to complete them._"]
    return mask_pii_data("\n".join(lines))


def _llm_text(rec: dict[str, Any], facts_md: str) -> tuple[str | None, list[str], list[str], str | None]:
    from app.core.runtime_config import get_runtime_config
    from app.crew.advisory_check import check_advisory
    from app.crew.verdict import Verdict

    cfg = get_runtime_config()
    if not cfg.llm_available:
        return None, [], [], None
    from app.llm import client as llm_client
    from app.observability import llm_calls

    model = cfg.model_for_role("advisory")
    system = ("You explain a recovery recommendation of a project portfolio tool to a PMO. Use only the figures given. "
              "Do not calculate, rank or change any verdict or option. Name no person. Write 120 words at most. "
              "After the text, under the heading 'Ideas', list at most two further levers the facts suggest that "
              "the option list does not contain, each starting with '[AI ESTIMATE]'.")
    facts = mask_pii_data(facts_md)
    client = llm_client.client_for(model, purpose="recovery explanation")
    with llm_calls.caller("recovery"):
        reply = client.chat(system, [client.user(facts)])
    text = mask_pii_data(reply.text or "").strip()
    body, _, ideas = text.partition("Ideas")
    hints = [line.strip(" -*") for line in ideas.splitlines() if IDEA_MARKER in line]
    label = next((o["result"].get("verdict") or {}).get("label") for o in rec.get("options") or []
                 if o["key"] == rec.get("recommended")) if rec.get("recommended") else None
    verdict = Verdict(label or "NO DECISION REQUIRED", label or "NO DECISION REQUIRED")
    sources = facts + "\n" + json.dumps(rec.get("options"), default=str)
    findings = check_advisory(body, sources, verdict)
    return body.strip(), hints, findings, model


def explain(rec: dict[str, Any], use_llm: bool = True) -> dict[str, Any]:
    offline = offline_text(rec)
    out: dict[str, Any] = {"offline": offline, "llm": None, "ideas": [], "findings": [], "model": None}
    if not use_llm:
        return out
    try:
        text, ideas, findings, model = _llm_text(rec, offline)
        out.update(llm=text, ideas=ideas, findings=findings, model=model)
    except Exception as exc:  # noqa: BLE001 - the offline text stands
        logger.warning("Recovery explanation by the LLM failed (%s) – offline text only", exc.__class__.__name__)
    return out
