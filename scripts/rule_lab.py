"""Rule lab (E6): before a threshold change is saved, run the evaluation cases through the deterministic offline
pipeline twice – with the saved business rules and with the candidate rules – and list every verdict that would
change. A change that turns an expected NO-GO or ANALYSIS INCOMPLETE into a GO is a wrong GO and fails the check.

The candidate rules apply in this run only (app.core.business_rules.candidate_rules); nothing is saved, the
portfolio is a temporary copy and no LLM is called.

    python scripts/rule_lab.py eac_overrun_nogo_pct=25 domino_conditional_threshold=3 [--cases E1,E2]
Exit code 1 when the candidate rules produce a wrong GO, 2 when they are invalid."""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SAFE = ("NO-GO", "ANALYSIS INCOMPLETE")


@dataclass
class LabResult:
    changes: dict[str, Any]
    cases: int = 0
    changed: list[dict[str, Any]] = field(default_factory=list)
    wrong_gos: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"changes": self.changes, "cases": self.cases, "changed": self.changed, "wrong_gos": self.wrong_gos,
                "headline": self.headline}

    @property
    def headline(self) -> str:
        rules = ", ".join(f"{k} = {v}" for k, v in self.changes.items())
        text = f"With {rules}: {len(self.changed)} of {self.cases} reference verdicts change"
        return text + (f", {len(self.wrong_gos)} of them a wrong GO." if self.wrong_gos else ", no wrong GO.")


def _verdicts(cases: list[dict[str, Any]], env: dict[str, Any]) -> dict[str, str]:
    from app.crew.pipeline import run_steering_pipeline
    from scripts import run_evals

    out: dict[str, str] = {}
    states: dict[str, Any] = {}
    for case in cases:
        role, view = run_evals._view(env["dataset"], case)
        result = run_steering_pipeline(case["request"], view, role, case["user"],
                                       base_state=states.get(case.get("previous")),
                                       as_of=run_evals.case_as_of(case), force=bool(case.get("force")))
        states[case["id"]] = result.scenario_state
        out[case["id"]] = result.verdict.decision if result.verdict else result.mode.upper()
    return out


def run_lab(changes: dict[str, Any], case_ids: list[str] | None = None) -> LabResult:
    """Both runs over the request cases of the evaluation set. Raises pydantic.ValidationError for invalid rules."""
    from app.core.business_rules import candidate_rules, current_rules
    from scripts import run_evals

    candidate = current_rules().merged(changes)
    cases = [c for c in run_evals.select_cases(run_evals.load_cases(), case_ids) if c.get("run_via") != "pytest"]
    result = LabResult(changes=dict(changes), cases=len(cases))
    with run_evals.isolated_environment() as env, run_evals.llm_mode(False, {}):
        base = _verdicts(cases, env)
        with candidate_rules(candidate):
            new = _verdicts(cases, env)
    for case in cases:
        before, after = base[case["id"]], new[case["id"]]
        if before == after:
            continue
        row = {"id": case["id"], "category": case.get("category"), "expected": case.get("expected_verdict"),
               "before": before, "after": after}
        result.changed.append(row)
        if after == "GO" and case.get("expected_verdict") in SAFE:
            result.wrong_gos.append(row)
    return result


def _parse_changes(items: list[str]) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    for item in items:
        key, _, value = item.partition("=")
        if not key or not value:
            raise ValueError(f"Expected name=value, got '{item}'.")
        changes[key.strip()] = float(value) if "." in value else int(value)
    return changes


def main(argv: list[str] | None = None) -> int:
    from pydantic import ValidationError

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("changes", nargs="+", help="rule=value, e.g. eac_overrun_nogo_pct=25")
    parser.add_argument("--cases", help="comma-separated case ids (default: every request case)")
    parser.add_argument("--json", action="store_true", help="print the result as JSON")
    args = parser.parse_args(argv)
    try:
        result = run_lab(_parse_changes(args.changes), args.cases.split(",") if args.cases else None)
    except (ValueError, ValidationError) as exc:
        print(f"Invalid rules: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
    else:
        print(result.headline)
        for row in result.changed:
            flag = "  WRONG GO" if row in result.wrong_gos else ""
            print(f"- {row['id']} ({row['category']}): {row['before']} -> {row['after']} "
                  f"(expected {row['expected']}){flag}")
    return 1 if result.wrong_gos else 0


if __name__ == "__main__":
    sys.exit(main())
