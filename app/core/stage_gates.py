"""Stage-gate model: the phases of a gate, the names of the criteria and the configured gate per phase transition
(`stage_gates` in data/env_<env>.json). The checks themselves live in app/crew/tools/gate_checks.py; this module has no
dependency on the tools, so the configuration can validate it."""
from __future__ import annotations

from typing import Any

from app.data_adapters.dataset import PHASES

CRITERION_NAMES = ("business_case", "sponsor", "baseline", "no_data_quality_errors", "no_unmitigated_high_risk",
                   "resource_plan_next_3_months", "work_nearly_complete", "work_complete", "benefit_plan")
MANDATORY, ADVISORY = "mandatory", "advisory"
# Free-text phases of the source mapped onto the stage-gate model
PHASE_ALIASES = {
    "initiation": "Initiation", "initiating": "Initiation", "idea": "Initiation", "demand": "Initiation",
    "concept": "Initiation", "planning": "Planning", "plan": "Planning", "design": "Planning",
    "executing": "Executing", "execution": "Executing", "execute": "Executing", "build": "Executing",
    "delivery": "Executing", "implementation": "Executing", "closing": "Closing", "close": "Closing",
    "closure": "Closing", "closed": "Closed", "complete": "Closed", "completed": "Closed",
}
DEFAULT_STAGE_GATES: dict[str, list[dict[str, str]]] = {
    "Initiation -> Planning": [
        {"criterion": "business_case", "kind": MANDATORY},
        {"criterion": "sponsor", "kind": MANDATORY},
    ],
    "Planning -> Executing": [
        {"criterion": "business_case", "kind": MANDATORY},
        {"criterion": "sponsor", "kind": MANDATORY},
        {"criterion": "baseline", "kind": MANDATORY},
        {"criterion": "no_data_quality_errors", "kind": MANDATORY},
        {"criterion": "no_unmitigated_high_risk", "kind": ADVISORY},
        {"criterion": "resource_plan_next_3_months", "kind": ADVISORY},
    ],
    "Executing -> Closing": [
        {"criterion": "no_data_quality_errors", "kind": MANDATORY},
        {"criterion": "work_nearly_complete", "kind": MANDATORY},
        {"criterion": "no_unmitigated_high_risk", "kind": ADVISORY},
    ],
    "Closing -> Closed": [
        {"criterion": "work_complete", "kind": MANDATORY},
        {"criterion": "benefit_plan", "kind": ADVISORY},
    ],
}


def normalize_phase(value: Any) -> str | None:
    """'Execution', ' executing ' -> 'Executing'; None if the text is no stage-gate phase."""
    text = str(value or "").strip()
    if text in PHASES:
        return text
    return PHASE_ALIASES.get(text.casefold())


def transition(from_phase: str, to_phase: str) -> str:
    return f"{from_phase} -> {to_phase}"


def validate_stage_gates(config: Any) -> dict[str, list[dict[str, str]]]:
    """Checks a `stage_gates` configuration; raises ValueError naming the first problem."""
    if not isinstance(config, dict):
        raise ValueError("stage_gates must be an object of transitions")
    valid = {transition(a, b) for a, b in zip(PHASES, PHASES[1:], strict=False)}
    for name, items in config.items():
        if name not in valid:
            raise ValueError(f"unknown stage-gate transition '{name}' (allowed: {', '.join(sorted(valid))})")
        if not isinstance(items, list):
            raise ValueError(f"{name}: criteria must be a list")
        for item in items:
            if not isinstance(item, dict) or item.get("criterion") not in CRITERION_NAMES:
                raise ValueError(f"{name}: unknown criterion {item!r}")
            if item.get("kind") not in (MANDATORY, ADVISORY):
                raise ValueError(f"{name}: kind must be mandatory or advisory")
    return config
