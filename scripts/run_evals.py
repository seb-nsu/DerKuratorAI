"""Evaluation runner: sends evals/cases.jsonl through the deterministic offline parser and/or the LLM pipeline and
writes docs/evals/EVAL_REPORT.md and docs/evals/eval_results.json.

    python scripts/run_evals.py --mode offline            # no network, no keys
    python scripts/run_evals.py --mode llm --repeat 3     # needs the API keys of the configured models
    python scripts/run_evals.py --mode both --repeat 3
    python scripts/run_evals.py --ids T1,T2,C1     # selected cases only (earlier steps of a multi-step case included)

Per case and mode it records: plan correct (the resulting change set equals the change set of the case's exact
intended plan – action, project and key parameter), verdict correct, actual verdict, duration, LLM cost and, for cases
with an expected security event, whether exactly that event was logged.

Isolation: logs, proposals, snapshots, chats and the graph live in a temp directory; the demo portfolio is read from a
temp copy. The real data/ folder is never written. The report never contains API keys or person names.

LLM mode records how each plan was produced: `llm`, or `fallback_offline` when an LLM call failed and the pipeline fell
back to the offline parser (only the error class is kept). Fallback runs never count as LLM results. A pre-flight call
stops the run before the full case set if the LLM is not reachable.

Every case runs with the fixed reference date EVAL_AS_OF (or the case's own `as_of`), so date-dependent figures (burn
rate, remaining months, NPV) and therefore the verdicts do not drift with the system date. In LLM mode the report also
counts how often the deterministic parser took precedence and how often the LLM plan disagreed with it, so the LLM's
plan accuracy is not overstated. A case whose exact plan no longer yields its expected verdict stops the run
(StaleExpectation).

Exit codes: 0 = ok, 1 = at least one wrong GO (expected NO-GO / ANALYSIS INCOMPLETE, actual GO), 2 = LLM mode requested
without the required API keys, 3 = LLM pre-flight failed or more than 10 % of the LLM runs fell back to offline.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core import clock  # noqa: E402 - after the project root is importable

CASES_PATH = ROOT / "evals" / "cases.jsonl"
PORTFOLIO = ROOT / "data" / "DerKurator_Demo_Portfolio.json"
OUT_DIR = ROOT / "docs" / "evals"
EVAL_AS_OF = date(2026, 10, 1)  # reference date of the demo portfolio's expected values
UNSAFE_FOR_GO = ("NO-GO", "ANALYSIS INCOMPLETE")  # expected verdicts that must never come out as GO
KEY_FIELDS = ("openai_api_key", "anthropic_api_key", "google_api_key")
MAX_FALLBACK_SHARE = 0.10  # a higher share of offline fallbacks marks the LLM results as incomplete
_FALLBACK_RE = re.compile(r"(?:LLM pipeline failed|CrewAI pipeline failed|Orchestrator fallback to rule-based parser) \((\w+)")
_SECRET_RE = re.compile(r"(sk-[A-Za-z0-9_\-]{6,}|AIza[0-9A-Za-z_\-]{10,}|x-api-key[^,}\s]*)")


@dataclass
class Run:
    id: str
    category: str
    mode: str
    repeat: int
    expected: str
    actual: str
    verdict_correct: bool
    plan_correct: bool | None  # None: not applicable (pytest-covered case)
    log_ok: bool | None = None  # None: no security event expected
    duration_s: float = 0.0
    cost_usd: float = 0.0
    via: str = "request"
    plan_source: str = "offline"  # offline | llm | fallback_offline | pytest
    error: str | None = None  # error class of a failed LLM call (never its message)
    verdict_s: float | None = None  # seconds until the rule-based verdict was known (before the decision paper)
    parser_corrected: bool = False  # the deterministic parser took precedence and the LLM plan disagreed
    parser_precedence: bool = False  # the deterministic parser ran the simulation (LLM plan agreed or not)
    conditions_complete: bool | None = None  # every condition of the exact plan is present (None: no conditions)

    @property
    def wrong_go(self) -> bool:
        """GO where NO-GO or ANALYSIS INCOMPLETE was expected – the unsafe miss. GO instead of CONDITIONAL GO is
        counted separately (a missed condition)."""
        return self.actual == "GO" and self.expected in UNSAFE_FOR_GO


@dataclass
class Evaluation:
    cases: list[dict[str, Any]]
    runs: list[Run] = field(default_factory=list)
    models: dict[str, str] = field(default_factory=dict)
    repeat: int = 1


# =========================================================================== setup
def load_cases(path: Path = CASES_PATH) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def select_cases(cases: list[dict[str, Any]], ids: list[str] | None) -> list[dict[str, Any]]:
    """The cases named in `ids` plus every earlier step they build on (`previous`), in file order."""
    if not ids:
        return cases
    by_id = {c["id"]: c for c in cases}
    unknown = [i for i in ids if i not in by_id]
    if unknown:
        raise SystemExit(f"Unknown case id(s): {', '.join(unknown)}")
    wanted: set[str] = set()
    for start in ids:
        cid = start
        while cid and cid not in wanted:
            wanted.add(cid)
            cid = by_id[cid].get("previous")
    return [c for c in cases if c["id"] in wanted]


def case_as_of(case: dict[str, Any]) -> date:
    return date.fromisoformat(case["as_of"]) if case.get("as_of") else EVAL_AS_OF


@contextmanager
def isolated_environment() -> Iterator[dict[str, Any]]:
    """Temp dir for every file the pipeline or the write path may touch, a temp copy of the portfolio and a graph
    built from it. All patched module attributes are restored afterwards."""
    from app.core import logs, notifications
    from app.data_adapters import JsonPortfolioAdapter
    from app.graph import portfolio_graph

    tmp = Path(tempfile.mkdtemp(prefix="derkurator_eval_"))
    patched: list[tuple[Any, str, Any]] = []

    def patch(obj: Any, name: str, value: Any) -> None:
        patched.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    try:
        for name in ("APP_LOG", "AUDIT_LOG", "SHADOW_LOG", "BUSINESS_AUDIT_LOG", "PROPOSALS_LOG", "SCENARIOS_LOG",
                     "NOTIFY_OUTBOX"):
            patch(logs, name, tmp / f"{name.lower()}.log")
        for name in ("SNAPSHOT_DIR", "CHAT_DIR", "ARCHIVE_DIR"):
            patch(logs, name, tmp / name.lower())
        patch(notifications, "enabled", lambda: False)  # evaluation proposals never reach a real mailbox
        source = tmp / "portfolio.json"
        shutil.copy(PORTFOLIO, source)
        dataset = JsonPortfolioAdapter(source).load_dataset()
        portfolio_graph.pin(dataset, source="evaluation")
        yield {"tmp": tmp, "source": source, "dataset": dataset}
    finally:
        portfolio_graph.reset()
        for obj, name, value in reversed(patched):
            setattr(obj, name, value)
        shutil.rmtree(tmp, ignore_errors=True)


@contextmanager
def llm_mode(enabled: bool, keys: dict[str, str]) -> Iterator[None]:
    """Offline = no keys in the runtime config (in memory only); LLM = the keys found at start."""
    from app.core.runtime_config import get_runtime_config, update_runtime_config

    before = {k: getattr(get_runtime_config(), k) for k in KEY_FIELDS}
    update_runtime_config(**(keys if enabled else {k: "" for k in KEY_FIELDS}))
    try:
        yield
    finally:
        update_runtime_config(**before)


def _view(dataset, case: dict[str, Any]):
    from app.core.security import ROLE_PM, ROLE_PMO, apply_rls_to_dataset

    role = ROLE_PM if case["role"] == "pm" else ROLE_PMO
    return role, apply_rls_to_dataset(dataset, role, case["user"])


# =========================================================================== ground truth change sets
def _guided_plan(expected: dict | list, names: dict[str, str]):
    from app.crew.scenarios import build_guided_scenario
    from app.crew.tasks import IntentPlan

    sub = []
    for item in expected if isinstance(expected, list) else [expected]:
        params = dict(item)
        action, project, target = params.pop("action"), params.pop("project"), params.pop("target", None)
        if action == "budget_change":
            delta = params["delta"]
            params = {"value": abs(delta), "is_perc": params["is_perc"], "cost_type": params["cost_type"],
                      "direction": "decrease" if delta < 0 else "increase"}
        sub += build_guided_scenario(action, project, names, target=target, **params).plan.sub_intents
    return IntentPlan(summary="ground truth", sub_intents=sub, requires_simulation=True, requires_graph_analysis=True)


def change_set(changes: list[dict[str, Any]]) -> dict[tuple, Any]:
    """Final value per (project, table, field) – chained steps collapse; numbers compared to the euro/0.1 FTE."""
    out: dict[tuple, Any] = {}
    for c in changes or []:
        value = c.get("new")
        try:
            value = round(float(value), 1)
        except (TypeError, ValueError):
            value = str(value)[:10] if value is not None else None
        out[(str(c.get("project_id")), c.get("table", "Projects"), c.get("field"))] = value
    return out


class StaleExpectation(SystemExit):
    """A case's expected verdict differs from what its own exact plan yields – the case is outdated, not the app."""


def condition_kinds(conditions: list[str] | tuple[str, ...]) -> set[str]:
    """Conditions without their figures, so "close the gap of €1,200 on X" and "… €1,250 on X" are the same kind."""
    return {re.sub(r"[€$]?\d[\d.,]*\s*%?", "#", c).strip() for c in conditions or ()}


def ground_truth(cases: list[dict[str, Any]], dataset, conditions: dict[str, set[str]] | None = None
                 ) -> dict[str, dict[tuple, Any]]:
    """The change set each case's exact intended plan produces (offline, no parser), incl. previous steps. The exact
    plan must also yield the case's expected verdict – otherwise the expectation is stale and the run stops.
    `conditions` (optional) receives the condition kinds of each exact plan."""
    from app.crew.pipeline import run_steering_pipeline

    names = dict(zip(dataset.projects["project_number"], dataset.projects["project_name"], strict=True))
    truth, states, stale = {}, {}, []
    for case in cases:
        if case.get("run_via") == "pytest" or case["expected_plan"] is None:
            truth[case["id"]] = {}
            continue
        role, view = _view(dataset, case)
        result = run_steering_pipeline(case["request"], view, role, case["user"],
                                       plan=_guided_plan(case["expected_plan"], names),
                                       base_state=states.get(case.get("previous")), as_of=case_as_of(case),
                                       force=bool(case.get("force")))
        truth[case["id"]], states[case["id"]] = change_set(result.proposed_changes), result.scenario_state
        decision = result.verdict.decision if result.verdict else result.mode.upper()
        if decision != case["expected_verdict"]:
            stale.append(f"{case['id']}: expected {case['expected_verdict']}, its exact plan yields {decision}")
        if conditions is not None:
            conditions[case["id"]] = condition_kinds(result.verdict.conditions if result.verdict else ())
    if stale:
        raise StaleExpectation("Stale expectations in evals/cases.jsonl – fix the case, not the app: " + "; ".join(stale))
    return truth


# =========================================================================== execution
def _approval_attempt(case: dict[str, Any], result, env: dict[str, Any]) -> bool:
    """Submits the change set as a proposal of `user` and tries to approve it as `approve_as`. True if the approval
    was refused AND exactly one security event with the expected reason code was logged."""
    from app.core import proposals
    from app.core.security import ROLE_PMO
    from app.data_adapters import JsonPortfolioAdapter, WriteNotAuthorizedError

    events: list[str] = []
    handler = logging.Handler()
    handler.emit = lambda record: events.append(record.getMessage())
    sec = logging.getLogger("derkurator.security")
    level = sec.level
    sec.addHandler(handler)
    sec.setLevel(logging.INFO)
    try:
        if not result.proposed_changes:
            return False
        entry = proposals.submit_proposal(case["user"], ROLE_PMO, case["request"], result.decision_paper,
                                          result.verdict.label if result.verdict else None, result.proposed_changes)
        try:
            JsonPortfolioAdapter(env["source"]).apply_changes(result.proposed_changes, user=case["approve_as"],
                                                              role=ROLE_PMO, proposal_id=entry["id"])
            return False  # an approval that went through is a failure of the case
        except WriteNotAuthorizedError:
            refusals = [e for e in events if " refused" in e]
            return len(refusals) == 1 and f"reason={case['expected_log_reason']}" in refusals[0]
    finally:
        sec.removeHandler(handler)
        sec.setLevel(level)


def _pytest_result(node_id: str) -> bool:
    cmd = [sys.executable, "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider", node_id]
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)  # noqa: S603 - node id from our own case file
    return proc.returncode == 0


def run_mode(cases: list[dict[str, Any]], mode: str, repeat: int, env: dict[str, Any], truth: dict,
             pytest_results: dict[str, bool], truth_conditions: dict[str, set[str]] | None = None) -> list[Run]:
    from app.core import cost_tracker
    from app.crew.guard_rails import _PLAN_CONFIRMED, _PLAN_CORRECTED
    from app.crew.pipeline import run_steering_pipeline

    corrected = _PLAN_CORRECTED.split("{", 1)[0]  # "Plan corrected by the deterministic parser (…): ..."
    confirmed = _PLAN_CONFIRMED.split("{", 1)[0]
    truth_conditions = truth_conditions or {}
    runs: list[Run] = []
    for rep in range(1, repeat + 1):
        states: dict[str, Any] = {}
        for case in cases:
            if case.get("run_via") == "pytest":
                ok = pytest_results.get(case["id"], False)
                runs.append(Run(case["id"], case["category"], mode, rep, case["expected_verdict"],
                                case["expected_verdict"] if ok else "TEST FAILED", ok, None,
                                ok if case.get("expected_log_reason") else None, via="pytest", plan_source="pytest"))
                continue
            role, view = _view(env["dataset"], case)
            started = time.perf_counter()
            with cost_tracker.track_request() as usage:
                result = run_steering_pipeline(case["request"], view, role, case["user"],
                                               base_state=states.get(case.get("previous")), as_of=case_as_of(case),
                                               force=bool(case.get("force")))
            duration = time.perf_counter() - started
            source, error = plan_source(result, mode)
            states[case["id"]] = result.scenario_state
            actual = result.verdict.decision if result.verdict else result.mode.upper()
            log_ok = _approval_attempt(case, result, env) if case.get("approve_as") else None
            expected_conditions = truth_conditions.get(case["id"]) or set()
            complete = (expected_conditions <= condition_kinds(result.verdict.conditions if result.verdict else ())
                        if expected_conditions else None)
            is_corrected = any(w.startswith(corrected) for w in result.warnings)
            runs.append(Run(case["id"], case["category"], mode, rep, case["expected_verdict"], actual,
                            actual == case["expected_verdict"], change_set(result.proposed_changes) == truth[case["id"]],
                            log_ok, round(duration, 2), round(usage.as_dict()["cost_usd"], 6),
                            plan_source=source, error=error, verdict_s=result.verdict_s,
                            parser_corrected=is_corrected,
                            parser_precedence=is_corrected or any(w.startswith(confirmed) for w in result.warnings),
                            conditions_complete=complete))
    return runs


def plan_source(result, mode: str) -> tuple[str, str | None]:
    """How the plan of a run was produced – from the pipeline's own signals: `result.mode` is "offline" after a
    failed crew, and a warning names the error class when the orchestrator (or the whole crew) fell back."""
    if mode != "llm":
        return "offline", None
    for warning in result.warnings:
        m = _FALLBACK_RE.search(warning)
        if m:
            return "fallback_offline", m.group(1)
    return ("llm", None) if result.mode == "agents" else ("fallback_offline", "unknown")


def llm_results(runs: list[Run]) -> list[Run]:
    """LLM-mode runs that really ran through the LLM (fallbacks excluded; pytest-covered cases kept, as offline)."""
    return [r for r in runs if r.plan_source != "fallback_offline"]


def fallbacks(runs: list[Run]) -> list[Run]:
    return [r for r in runs if r.plan_source == "fallback_offline"]


def incomplete(runs: list[Run]) -> bool:
    llm_runs = [r for r in runs if r.mode == "llm" and r.via == "request"]
    return bool(llm_runs) and len(fallbacks(llm_runs)) / len(llm_runs) > MAX_FALLBACK_SHARE


def preflight() -> str | None:
    """One real LLM request before the full case set. Returns None if it ran through the LLM, else a redacted
    error message."""
    from app.core.runtime_config import get_runtime_config
    from app.crew.pipeline import run_steering_pipeline

    cfg = get_runtime_config()
    keys = {k: getattr(cfg, k) for k in KEY_FIELDS}
    case = next(c for c in load_cases() if c.get("run_via") != "pytest" and c["expected_plan"] is not None)
    with isolated_environment() as env, llm_mode(True, keys):
        role, view = _view(env["dataset"], case)
        result = run_steering_pipeline(case["request"], view, role, case["user"], as_of=case_as_of(case))
    source, _error = plan_source(result, "llm")
    if source == "llm":
        return None
    detail = next((w for w in result.warnings if _FALLBACK_RE.search(w)), f"pipeline mode '{result.mode}'")
    return _SECRET_RE.sub("[redacted]", detail)[:400]


def evaluate(mode: str = "offline", repeat: int = 1, include_pytest_cases: bool = True,
             cases: list[dict[str, Any]] | None = None) -> Evaluation:
    """Runs the evaluation. LLM mode requires `llm_available` with the keys configured at start."""
    from app.core.runtime_config import AGENT_ROLES, get_runtime_config

    cases = cases if cases is not None else load_cases()
    if not include_pytest_cases:
        cases = [c for c in cases if c.get("run_via") != "pytest"]
    cfg = get_runtime_config()
    keys = {k: getattr(cfg, k) for k in KEY_FIELDS}
    evaluation = Evaluation(cases=cases, repeat=repeat,
                            models={r: cfg.model_for_role(r) for r in AGENT_ROLES})
    pytest_results = {c["id"]: _pytest_result(c["test"]) for c in cases if c.get("run_via") == "pytest"}
    with isolated_environment() as env:
        conditions: dict[str, set[str]] = {}
        with llm_mode(False, keys):
            truth = ground_truth(cases, env["dataset"], conditions)
            if mode in ("offline", "both"):  # deterministic: once
                evaluation.runs += run_mode(cases, "offline", 1, env, truth, pytest_results, conditions)
        if mode in ("llm", "both"):
            with llm_mode(True, keys):
                evaluation.runs += run_mode(cases, "llm", repeat, env, truth, pytest_results, conditions)
    return evaluation


# =========================================================================== report
def _pct(part: int, whole: int) -> str:
    return f"{part / whole * 100:.0f} % ({part}/{whole})" if whole else "–"


def summary(runs: list[Run]) -> dict[str, Any]:
    """Headline figures over the requests that ran through the pipeline; pytest-covered cases are counted apart."""
    covered = [r for r in runs if r.via == "pytest"]
    runs = [r for r in runs if r.via != "pytest"]
    executed = runs
    planned = [r for r in runs if r.plan_correct is not None]
    with_conditions = [r for r in runs if r.conditions_complete is not None]
    durations = sorted(r.duration_s for r in executed)
    verdicts = [r.verdict_s for r in executed if r.verdict_s is not None]
    p95 = durations[min(len(durations) - 1, int(round(0.95 * (len(durations) - 1))))] if durations else 0.0
    return {
        "runs": len(runs),
        "plan_accuracy": _pct(sum(r.plan_correct for r in planned), len(planned)),
        "verdict_accuracy": _pct(sum(r.verdict_correct for r in runs), len(runs)),
        "wrong_go": sum(r.wrong_go for r in runs),
        "missed_conditions": sum(r.actual == "GO" and r.expected == "CONDITIONAL GO" for r in runs),
        "analysis_incomplete_expected": _pct(sum(r.actual == r.expected == "ANALYSIS INCOMPLETE" for r in executed),
                                             len(executed)),
        "analysis_incomplete_unexpected": _pct(sum(r.actual == "ANALYSIS INCOMPLETE" != r.expected for r in executed),
                                               len(executed)),
        "conditions_complete": _pct(sum(bool(r.conditions_complete) for r in with_conditions), len(with_conditions)),
        "pytest_cases": _pct(sum(r.verdict_correct for r in covered), len(covered)),
        "security_events_ok": _pct(sum(bool(r.log_ok) for r in runs if r.log_ok is not None),
                                   sum(r.log_ok is not None for r in runs)),
        "median_s": round(statistics.median(durations), 2) if durations else 0.0,
        "median_verdict_s": round(statistics.median(verdicts), 2) if verdicts else "–",
        "p95_s": round(p95, 2),
        "cost_per_request_usd": round(statistics.mean(r.cost_usd for r in executed), 4) if executed else 0.0,
        "parser_precedence": _pct(sum(r.parser_precedence for r in executed), len(executed)),
        "llm_plan_disagreed": f"{sum(r.parser_corrected for r in executed)} of "
                              f"{sum(r.parser_precedence for r in executed)}",
    }


def consistency(runs: list[Run]) -> tuple[int, int]:
    """(cases with the same verdict in every repeat, cases with more than one repeat) – LLM mode."""
    by_case: dict[str, set[str]] = {}
    counts: dict[str, int] = {}
    for r in runs:
        by_case.setdefault(r.id, set()).add(r.actual)
        counts[r.id] = counts.get(r.id, 0) + 1
    multi = [cid for cid, n in counts.items() if n > 1]
    return sum(len(by_case[cid]) == 1 for cid in multi), len(multi)


def render_report(ev: Evaluation) -> str:
    modes = [m for m in ("offline", "llm") if any(r.mode == m for r in ev.runs)]
    all_llm = [r for r in ev.runs if r.mode == "llm"]
    by_mode = {m: (llm_results(all_llm) if m == "llm" else [r for r in ev.runs if r.mode == m]) for m in modes}
    requests = {c["id"]: c["request"] for c in ev.cases}
    wrong = [r for r in ev.runs if r.wrong_go]
    lines = ["# Evaluation report", ""]
    if incomplete(ev.runs):
        failed_llm = fallbacks(all_llm)
        lines += [f"> **LLM results incomplete:** {len(failed_llm)} of {sum(r.via == 'request' for r in all_llm)} LLM "
                  f"runs fell back to the offline parser (more than {MAX_FALLBACK_SHARE:.0%}). The LLM numbers below "
                  "only cover the runs that reached the LLM.", ""]
    if wrong:
        lines += ["> **WRONG GO – a request that must be NO-GO or ANALYSIS INCOMPLETE was answered GO:**", ">"]
        lines += [f"> - `{r.id}` ({r.mode}{', offline fallback' if r.plan_source == 'fallback_offline' else ''}, "
                  f"run {r.repeat}): expected {r.expected}, actual **GO** – "
                  f"\"{requests[r.id]}\"" for r in wrong]
        lines.append("")
    lines += [f"- **Date:** {clock.now():%Y-%m-%d %H:%M} · **reference date (as_of):** {EVAL_AS_OF.isoformat()} "
              "unless a case sets its own",
              f"- **Cases:** {len(ev.cases)} · **modes:** {', '.join(modes)} · **LLM repeats:** "
              f"{ev.repeat if 'llm' in modes else '–'}",
              "- **Models per agent (LLM mode):** " + ", ".join(f"{a} `{m}`" for a, m in ev.models.items()),
              "- **Portfolio:** unchanged demo portfolio; expected values derived from the exact intended plans "
              "(see `evals/README.md`)", ""]

    lines += ["## Summary", "", "| Metric | " + " | ".join(modes) + " |", "|---|" + "---|" * len(modes)]
    sums = {m: summary(by_mode[m]) for m in modes}
    for key, label in (("plan_accuracy", "Plan accuracy (action + project + key parameter)"),
                       ("verdict_accuracy", "Verdict accuracy (requests run through the pipeline)"),
                       ("wrong_go", "Wrong GOs (GO where NO-GO or ANALYSIS INCOMPLETE was expected)"),
                       ("missed_conditions", "GO instead of CONDITIONAL GO"),
                       ("conditions_complete", "Conditions complete (all conditions of the exact plan present)"),
                       ("analysis_incomplete_expected", "ANALYSIS INCOMPLETE, expected (ask-back)"),
                       ("analysis_incomplete_unexpected", "ANALYSIS INCOMPLETE, unexpected"),
                       ("security_events_ok", "Expected security events logged"),
                       ("median_verdict_s", "Median time to verdict (s)"),
                       ("median_s", "Median duration (s)"), ("p95_s", "p95 duration (s)"),
                       ("cost_per_request_usd", "LLM cost per request (USD)"),
                       ("parser_precedence", "Parser took precedence (certain request, run deterministically)"),
                       ("llm_plan_disagreed", "… of which the LLM plan disagreed"),
                       ("pytest_cases", "Cases covered by tests (not in the headline)")):
        lines.append(f"| {label} | " + " | ".join(str(sums[m][key]) for m in modes) + " |")
    if "llm" in modes:
        executed = [r for r in all_llm if r.via == "request"]
        lines.append("| LLM calls failed → offline fallback (excluded above) | " + " | ".join(
            "–" if m != "llm" else f"{len(fallbacks(executed))} of {len(executed)}" for m in modes) + " |")
    lines.append("")

    categories = list(dict.fromkeys(c["category"] for c in ev.cases))
    lines += ["## By category", "", "Verdict accuracy · plan accuracy per category – where the LLM adds value "
              "shows up in `paraphrase`, `german`, `vague`, `multi_part` and `multi_step`.", "",
              "| Category | Cases | " + " | ".join(f"{m} verdict | {m} plan" for m in modes) + " |",
              "|---|---|" + "---|---|" * len(modes)]
    for cat in categories:
        cells = []
        for m in modes:
            rs = [r for r in by_mode[m] if r.category == cat]
            planned = [r for r in rs if r.plan_correct is not None]
            cells += [_pct(sum(r.verdict_correct for r in rs), len(rs)),
                      _pct(sum(r.plan_correct for r in planned), len(planned))]
        lines.append(f"| {cat} | {sum(c['category'] == cat for c in ev.cases)} | " + " | ".join(cells) + " |")
    lines.append("")

    if "llm" in modes and ev.repeat > 1:
        same, total = consistency(by_mode["llm"])  # fallback runs excluded
        lines += ["## Consistency (LLM mode)", "",
                  f"Same verdict in all {ev.repeat} repeats: **{_pct(same, total)}** of the cases.", ""]

    lines += ["## Failed cases", ""]
    failed = [r for r in ev.runs if r.plan_source != "fallback_offline"
              and (not r.verdict_correct or r.plan_correct is False or r.log_ok is False)]
    if not failed:
        lines.append("None.")
    else:
        lines += ["| Mode | Run | Case | Category | Request | Expected | Actual | Plan | Security event |",
                  "|---|---|---|---|---|---|---|---|---|"]
        for r in failed:
            plan = "–" if r.plan_correct is None else ("ok" if r.plan_correct else "**wrong**")
            log = "–" if r.log_ok is None else ("ok" if r.log_ok else "**missing**")
            actual = f"**{r.actual}**" if not r.verdict_correct else r.actual
            lines.append(f"| {r.mode} | {r.repeat} | {r.id} | {r.category} | {requests[r.id]} | {r.expected} | "
                         f"{actual} | {plan} | {log} |")
    if fallbacks(all_llm):
        lines += ["", "## LLM calls that fell back to offline", "",
                  "Not counted as LLM results. Only the error class is recorded.", "",
                  "| Run | Case | Category | Error |", "|---|---|---|---|"]
        lines += [f"| {r.repeat} | {r.id} | {r.category} | {r.error} |" for r in fallbacks(all_llm)]
    lines += ["", "Cases marked `pytest` in `evals/cases.jsonl` are counted from their covering tests and reported "
              "in their own row, not in the headline accuracy."]
    return "\n".join(lines) + "\n"


def write_outputs(ev: Evaluation, out_dir: Path = OUT_DIR) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    report, results = out_dir / "EVAL_REPORT.md", out_dir / "eval_results.json"
    report.write_text(render_report(ev), encoding="utf-8")
    modes = sorted({r.mode for r in ev.runs})
    results.write_text(json.dumps({
        "date": clock.stamp(), "cases": len(ev.cases), "repeat": ev.repeat,
        "models": ev.models, "llm_results_incomplete": incomplete(ev.runs),
        "summary": {m: summary(llm_results([r for r in ev.runs if r.mode == m]) if m == "llm"
                               else [r for r in ev.runs if r.mode == m]) for m in modes},
        "llm_fallbacks": len(fallbacks([r for r in ev.runs if r.mode == "llm"])),
        "runs": [asdict(r) | {"wrong_go": r.wrong_go} for r in ev.runs],
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    return report, results


# =========================================================================== CLI
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate the offline parser and the LLM pipeline on evals/cases.jsonl")
    parser.add_argument("--mode", choices=("offline", "llm", "both"), default="offline")
    parser.add_argument("--repeat", type=int, default=1, help="repeats per case in LLM mode (report: 3)")
    parser.add_argument("--out", type=Path, default=OUT_DIR)
    parser.add_argument("--ids", default="", help="comma-separated case IDs, e.g. T1,T2,C1 (default: all cases)")
    args = parser.parse_args(argv)
    ids = [i.strip() for i in args.ids.split(",") if i.strip()]
    logging.basicConfig(level=logging.ERROR)  # keep the console readable; nothing is written to data/app.log

    from app.core.runtime_config import get_runtime_config

    cfg = get_runtime_config()
    if args.mode in ("llm", "both") and not cfg.llm_available:
        missing = ", ".join(cfg.missing_keys_for_models()) or "FORCE_OFFLINE_MODE is set"
        print(f"LLM mode needs the API keys of the configured models – missing: {missing}. "
              "Set them in .env (or run --mode offline).", file=sys.stderr)
        return 2
    if args.mode in ("llm", "both"):
        error = preflight()  # one real call – never spend on the full set if the LLM is not reachable
        if error:
            print(f"LLM pre-flight failed – no evaluation run: {error}", file=sys.stderr)
            return 3
    evaluation = evaluate(args.mode, max(1, args.repeat), cases=select_cases(load_cases(), ids))
    report, results = write_outputs(evaluation, args.out)
    for mode in sorted({r.mode for r in evaluation.runs}):
        runs = [r for r in evaluation.runs if r.mode == mode]
        s = summary(llm_results(runs) if mode == "llm" else runs)
        extra = f" · offline fallbacks {len(fallbacks(runs))}" if mode == "llm" else ""
        print(f"{mode:8} plan {s['plan_accuracy']:>14} · verdict {s['verdict_accuracy']:>14} · wrong GO {s['wrong_go']}{extra}")
    print(f"Report: {_shown(report)} · results: {_shown(results)}")
    if any(r.wrong_go for r in evaluation.runs):
        return 1
    if incomplete(evaluation.runs):
        print("LLM results incomplete: too many LLM calls fell back to offline (see report).", file=sys.stderr)
        return 3
    return 0


def _shown(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:  # --out outside the repository
        return str(path)


if __name__ == "__main__":
    raise SystemExit(main())
