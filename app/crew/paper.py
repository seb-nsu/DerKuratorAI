"""Decision paper: the offline paper from the tool results, and the code-written sections (scenario, notes) that are
inserted into the LLM paper, plus the streamed advisory text."""
from __future__ import annotations

import logging
import re
from typing import Any

from app.core.business_rules import business_assumptions_md
from app.core.formatting import business_text, field_label, format_delta, format_value
from app.core.runtime_config import get_runtime_config
from app.core.security import mask_pii_data
from app.crew.tools import context as ctx
from app.crew.tools import simulator_tools
from app.crew.verdict import (
    GRAPH_DOWN_MARKERS,
    GUARDRAIL_ERROR_RE,
    INTERNAL_ERROR_RE,
    compute_verdict,
    condition_owner_text,
)

logger = logging.getLogger(__name__)


def _parser_error(message: str) -> str:
    """Errors raised by the offline parser itself (not by a tool) are recorded like tool results, so the verdict
    engine sees exactly what the paper shows. They carry "INPUT ERROR:", which yields ANALYSIS INCOMPLETE, never a
    guardrail NO-GO."""
    ctx.record_tool_result("simulation", "offline_parser", message)
    return message


def forced_notice(forced: list[str]) -> str:
    return (f"⚠️ **FORCED SIMULATION** – the PMO overrode the Data Steward quality gate for {', '.join(forced)}. "
            "Results for these projects are based on incomplete or inconsistent master data and are indicative only.")


def _render_offline_paper(request, plan, sim_results, graph_results, steward, role, forced: list[str]) -> str:
    all_results = sim_results + graph_results
    # The verdict comes from the deterministic rule engine (same engine as in LLM mode). It covers
    # the whole scenario – the context holds the tool results of earlier steps too (lists = this step only).
    decision = compute_verdict(ctx.get_tool_results("simulation") or sim_results,
                               ctx.get_tool_results("graph") or graph_results, ctx.get_facts(), forced,
                               as_of=ctx.as_of().isoformat())
    verdict = decision.label
    conditions = "\n".join(f"   - Condition: {d['condition']} ({condition_owner_text(d)})"
                            for d in decision.condition_details())

    def block(results: list[str]) -> str:
        unique = list(dict.fromkeys(results))
        return "\n\n".join(r.replace("\n", "  \n") for r in unique) if unique else "_No results._"

    changes = ctx.get_ledger()
    change_rows = "\n".join(
        f"| {c['project_id']} | {field_label(c['field'])} | {format_value(c['field'], c['old'])} | "
        f"{format_value(c['field'], c['new'])} | {format_delta(c['field'], c['old'], c['new'])} |" for c in changes
    )
    summary = [f"- Request read as **{len(plan.sub_intents)} step(s)** ({plan.summary})."]
    summary += [f"- {line}" for line in list(dict.fromkeys(r.splitlines()[0] for r in all_results))[:5]]
    notes = [forced_notice(forced)] if forced else []
    notes.append(steward.to_markdown(max_rows=15))
    notes += _result_notes(sim_results, graph_results)
    notes.append("- ℹ️ Offline mode: deterministic rule-based parser instead of LLM agents (no API keys or LLM error).")
    changes_md = (
        "| Project | Change | Current | Proposed | Δ |\n|---|---|---|---|---|\n" + change_rows
        if changes else "_No data changes proposed._"
    )

    return business_text(f"""# Executive Decision Paper
**Request:** {request} | **Date:** {ctx.as_of():%Y-%m-%d} | **Role:** {role} | **Recommendation:** {verdict}

## 1. Management Summary
{chr(10).join(summary)}

## 2. Scenario Analysis (quantitative)
{block(sim_results)}

## 3. Dependencies & Systemic Risks
{block(graph_results)}

## 4. Proposed Changes (require approval)
{changes_md}

## 5. Recommendation & Next Steps
1. **{verdict}** – record the decision in the next steering committee (owner: PMO).
{conditions + chr(10) if conditions else ""}2. Inform affected project managers about downstream and task effects (owner: PMO, 5 business days).
3. After approval: execute via "Approve & Apply" and re-baseline the affected projects.

{decision.to_markdown()}

## 6. Data Quality & Notes
{chr(10).join(notes)}

**Business assumptions (governance config):**
{business_assumptions_md()}
""")


def _result_notes(sim_results: list[str], graph_results: list[str]) -> list[str]:
    """Data-quality and system notes derived from the tool results (shared by the offline and the LLM paper)."""
    notes = []
    # A code defect ("INTERNAL ERROR:") is never a guardrail violation – it makes the analysis incomplete
    internal_errors = [r for r in sim_results + graph_results if INTERNAL_ERROR_RE.search(r)]
    guardrail_errors = [r for r in sim_results if GUARDRAIL_ERROR_RE.search(r)]
    graph_down = any(r.startswith("ERROR:") and any(m in r for m in GRAPH_DOWN_MARKERS) for r in graph_results)
    if internal_errors:
        notes.append(f"- 🛠️ {len(internal_errors)} internal error(s) – a system defect prevented part of the analysis. "
                     "This is not a business verdict; the System Admin has the details in the system log.")
    if guardrail_errors:
        notes.append(f"- ❌ {len(guardrail_errors)} guardrail / quality gate violation(s) – see scenario analysis.")
    if graph_down:
        notes.append("- ⚠ Graph database unavailable – dependencies unchecked.")
    return notes


# =========================================================================== Paper assembly (LLM mode)
# The Advisory writes the explanation (header, sections 1, 2, 4, 5, 6); the code writes the figures (section 3) and the
# data-quality notes (section 7) from what the tools recorded – the numbers in the paper never pass through an LLM.
_PAPER_SECTION_RE = re.compile(r"(?m)^##\s+(\d)\.[^\n]*$")


def _cell(value: Any) -> str:
    if isinstance(value, bool) or value is None:
        return "–" if value is None else str(value)
    if isinstance(value, (int, float)):
        return f"{value:,.0f}" if abs(value) >= 1000 else f"{value:g}"
    return str(value).replace("|", "/").replace("\n", " ")


def scenario_section_md(changes: list[dict[str, Any]], sim_results: list[str]) -> str:
    """Section 3: the changes of this scenario (ledger) as a before/after table, plus the tools' headline per result."""
    lines = ["## 3. Scenario Analysis (quantitative)"]
    if changes:
        lines += ["| Project | Change | Before | After | Δ |", "|---|---|---|---|---|"]
        lines += [f"| {c.get('project_id')}{' · ' + str(c['key']) if c.get('key') else ''} | {field_label(str(c.get('field')))} | "
                  f"{_cell(format_value(str(c.get('field')), c.get('old')))} | "
                  f"{_cell(format_value(str(c.get('field')), c.get('new')))} | "
                  f"{format_delta(str(c.get('field')), c.get('old'), c.get('new'))} |" for c in changes]
    else:
        lines.append("_No data changes in this scenario._")
    headlines = list(dict.fromkeys(r.splitlines()[0] for r in sim_results if r.strip()))[:6]
    if headlines:
        lines += [""] + [f"- {h}" for h in headlines]
    return "\n".join(lines)


def notes_section_md(steward_md: str, sim_results: list[str], graph_results: list[str], extra: list[str]) -> str:
    """Section 7: Data Steward findings, system and guardrail notes, AI-estimate flags, plan guardrail notes."""
    notes = [steward_md.strip()] if steward_md.strip() else []
    notes += _result_notes(sim_results, graph_results)
    estimates = sum(simulator_tools.KI_PREFIX in r for r in sim_results)
    if estimates:
        notes.append(f"- {simulator_tools.KI_PREFIX} {estimates} qualitative estimate(s) – not calculated.")
    notes += [f"- ℹ️ {n}" for n in extra]
    return "## 7. Data Quality & Notes\n" + "\n".join(notes)


def assemble_paper(llm_paper: str, scenario_md: str, notes_md: str) -> str:
    """Puts the Advisory's sections and the code's sections 3 and 7 into the paper outline (1–7). Sections 3 and 7 the
    Advisory wrote anyway are replaced; a paper without recognisable sections keeps its text and gets 3 and 7 appended."""
    text = (llm_paper or "").strip()
    matches = list(_PAPER_SECTION_RE.finditer(text))
    code = {3: scenario_md, 7: notes_md}
    if not matches:
        return business_text("\n\n".join(p for p in (text, scenario_md, notes_md) if p))
    sections = {}
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        sections.setdefault(int(m.group(1)), text[m.start():end].strip())
    head = text[:matches[0].start()].strip()
    parts = [head] if head else []
    parts += [code.get(n) or sections[n] for n in sorted(set(sections) | set(code))]
    return business_text("\n\n".join(parts))


def stream_advisory(description: str, variant: str, on_paper) -> str:
    """Streams the Advisory's text with the same prompt and model as the agent task. `on_paper` receives the text
    shown so far – PII-masked, and only complete lines, so a name split across two chunks is never shown unmasked.
    Returns the full raw text (masked again with the whole paper later)."""
    from app.crew import agents as ag
    from app.llm import client as llm_client
    from app.observability import llm_calls

    model = get_runtime_config().model_for_role("advisory", variant)
    client = llm_client.client_for(model, purpose="advisory stream")
    state = {"shown": "", "pending": ""}

    def on_text(text: str) -> None:
        state["pending"] += text
        if "\n" in state["pending"]:
            complete, state["pending"] = state["pending"].rsplit("\n", 1)
            state["shown"] += mask_pii_data(complete + "\n")
            on_paper(state["shown"])

    with llm_calls.caller("advisory" if variant == "main" else f"advisory.{variant}"):
        raw = client.stream(ag.advisory_system_prompt(), [client.user(description)], on_text)
    if state["pending"]:
        on_paper(state["shown"] + mask_pii_data(state["pending"]))
    return raw
