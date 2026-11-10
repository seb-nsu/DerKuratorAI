"""The page assistant: a question box under a page, answered from the facts the page shows (app/crew/ask.py).

A question that asks for a change or a planning scenario ("cut … by 10 %", "what drops out in 2027 at −10 %?") is
calculated right in the box by the rule-based pipeline (app/crew/quick.py) – verdict and changes, no LLM – and the
result points to where the next step happens: the Steering Console (decision paper, proposal) or the planning
scenarios (pre-filled, saved as a draft for the Portfolio Board). Every other question is answered from the facts.
The facts are built only when a question is asked. Each question is audited without its text (page, mode, length)."""
from __future__ import annotations

import logging
from collections.abc import Callable

import streamlit as st

from app.crew.ask import Answer, ask, llm_ready, readable
from app.crew.quick import QuickResult
from app.ui.common import verdict_callout
from app.views.assist import (
    AI_NOTE,
    CONSOLE,
    EMPTY_HINT,
    HANDOFF_HINT,
    HISTORY,
    OFFLINE_HINT,
    RULES_NOTE,
    audit_question,
    has_console,
    needs_console,
    next_step,
    planning_prefill,
    quick,
)

logger = logging.getLogger("derkurator.ui.ask")


def _audit(page: str, mode: str, model: str, question: str) -> None:
    audit_question(page, mode, model, len(question), (st.session_state.get("auth_user") or {}).get("user_id"))


def _hand_over(question: str) -> None:
    from app.ui.shell import go_to

    st.session_state["pending_prompt"] = question
    go_to("console")


def _open_planning(spec) -> None:
    """Pre-fills the planning scenario inputs with the calculated question and opens the Planning page."""
    from app.ui.shell import go_to

    inputs = planning_prefill(spec)
    st.session_state.update(
        scn_name=inputs["name"], scn_from=inputs["year_from"], scn_to=inputs["year_to"],
        scn_factor=inputs["budget_factor_pct"], scn_scope=inputs["scope"], scn_method=inputs["method"],
        scn_forced=inputs["forced_in"], scn_forced_reason=inputs["forced_reason"], scn_excluded=inputs["excluded"],
        scn_later=inputs["later"], scn_earlier=inputs["earlier"])
    go_to("planning")


def _quick(question: str, project_id: str | None) -> QuickResult | None:
    """The reader's own view, as in the Steering Console; None for a plain question or a reader without data."""
    from app.ui.common import load_dataset

    role, user_id = st.session_state.get("role"), st.session_state.get("user_id")
    if not role:
        return None
    return quick(question, load_dataset(), role, user_id, project_id)


def _has_console() -> bool:
    return has_console(st.session_state.get("role"))


def _needs_console(question: str) -> bool:
    from app.ui.common import load_dataset

    return needs_console(question, lambda: load_dataset().projects)


def _render_result(key: str, i: int, result: QuickResult) -> None:
    from app.crew.paper import scenario_section_md

    verdict = result.verdict
    if verdict is None:  # blocked, or a planning question for a role that does not plan
        st.markdown(result.paper)
    else:
        verdict_callout(verdict.label, verdict.reasons)
        st.caption(":material/gavel: Calculated by the rules – not by the AI")
        for reason in verdict.reasons[2:4]:  # the first two are in the callout
            st.markdown(f"- {reason}")
        for condition in verdict.conditions[:3]:
            st.markdown(f"- Condition: {condition}")
        if result.changes:
            st.markdown(scenario_section_md(result.changes, []).split("\n", 1)[1])
        with st.expander("Rule-based paper", icon=":material/description:"):
            st.markdown(result.paper)
    step = next_step(result, st.session_state.get("role"))
    if step is None:
        return
    st.caption(step["text"])
    if step["target"] == CONSOLE:
        if st.button("Continue in the Steering Console", icon=":material/query_stats:", key=f"ask_console_{key}_{i}"):
            _hand_over(result.request)
    elif step["target"] == "planning":
        if st.button("Open in Planning › Plan scenarios", icon=":material/event_note:", key=f"ask_plan_{key}_{i}"):
            _open_planning(result.planning_input)


def render_ask_box(key: str, subject: str, facts: Callable[[], str], *, title: str = "Ask about this page",
                   examples: tuple[str, ...] = (), expanded: bool = False, placeholder: str = "Ask a question …",
                   project_id: str | None = None) -> None:
    """`facts` builds the facts text (only when asked); `subject` names what the page is about for the model.
    `project_id`: the page's project – a change question that names no project is about it."""
    history: list[tuple[str, Answer | QuickResult]] = st.session_state.setdefault(f"ask_hist_{key}", [])
    ready, _ = llm_ready()
    with st.expander(title, icon=":material/forum:", expanded=expanded or bool(history)):
        with st.form(f"ask_form_{key}", clear_on_submit=True, border=False):
            question = st.text_input("Question", key=f"ask_q_{key}", placeholder=placeholder,
                                     label_visibility="collapsed", max_chars=600)
            sent = st.form_submit_button("Ask", icon=":material/send:")
        if examples:
            cols = st.columns(len(examples))
            for i, (col, example) in enumerate(zip(cols, examples, strict=True)):
                if col.button(example, key=f"ask_ex_{key}_{i}", type="tertiary"):
                    question, sent = example, True
        if sent and question.strip():
            result = None
            with st.spinner("Calculating ..."):
                try:
                    result = _quick(question, project_id)
                except Exception as exc:  # noqa: BLE001 - the question is still answered from the facts
                    logger.warning("Quick calculation failed for %s (%s)", key, exc.__class__.__name__)
            if result is None:
                with st.spinner("Reading the facts of this page ..."):
                    try:
                        text = facts()
                    except Exception as exc:  # noqa: BLE001 - a page without facts still answers, saying so
                        logger.warning("Assistant facts failed for %s (%s)", key, exc.__class__.__name__)
                        text = "The facts of this page could not be collected."
                    result = ask(question, text, subject)
                _audit(key, result.mode, result.model, question)
            else:
                _audit(key, "calculation", "", question)
            history.insert(0, (question.strip(), result))
            del history[HISTORY:]
        for i, (q, answer) in enumerate(history):
            st.markdown(f"**{q}**")
            if isinstance(answer, QuickResult):
                _render_result(key, i, answer)
            else:
                st.markdown(answer.text)
                note = AI_NOTE if answer.mode == "llm" else RULES_NOTE
                st.caption(note + (" · facts shortened" if answer.truncated else ""))
                if _has_console() and _needs_console(q):
                    st.caption(HANDOFF_HINT)
                    if st.button("Calculate in the Steering Console", icon=":material/query_stats:",
                                 key=f"ask_handoff_{key}_{i}"):
                        _hand_over(q)
                with st.popover("Facts", icon=":material/fact_check:"):
                    st.markdown(readable(answer.facts) or "–")
            if i < len(history) - 1:
                st.divider()
        if not history:
            st.caption(EMPTY_HINT + ("" if ready else OFFLINE_HINT))
