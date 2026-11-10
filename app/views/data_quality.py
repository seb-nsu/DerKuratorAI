"""Data quality page: Data Steward key figures, quality bars, the findings table and its executive interpretation –
all from the reader's scope-filtered Data Steward report."""
from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

import pandas as pd

from app.core.formatting import field_label, rule_label
from app.core.runtime_config import get_runtime_config
from app.crew.tools.data_steward_tools import DataQualityReport

FINDING_COLUMNS = ["Severity", "Project", "Rule", "Field", "Finding"]
INDEX_HELP = "Share of projects without ERROR findings, i.e. released for quantitative simulation."




def quality_level(value: float) -> tuple[str, str]:
    """(RAG status, word) of a quality percentage: Good from 90 %, Fair from 70 %, else Poor."""
    return ("Green", "Good") if value >= 90 else ("Yellow", "Fair") if value >= 70 else ("Red", "Poor")


def quality_bars(steward: DataQualityReport) -> list[dict[str, Any]]:
    """The three quality bars of the panel: label, value, status, word and detail line."""
    bars = [("Released for simulation", steward.quality_index,
             f"{steward.total_rows - len(steward.blocked_project_ids)} of {steward.total_rows} projects"),
            ("Required-field completeness", steward.completeness_pct,
             f"{len(steward.required_fields)} required fields checked"),
            ("Projects without findings", steward.warning_free_pct,
             f"{len(steward.errors)} errors · {len(steward.warnings)} warnings")]
    return [{"label": label, "value": value, "status": quality_level(value)[0], "word": quality_level(value)[1],
             "detail": detail} for label, value, detail in bars]


def summary(steward: DataQualityReport) -> dict[str, Any]:
    """Key figures of the panel and the findings header."""
    blocked = list(steward.blocked_project_ids)
    return {"projects_checked": steward.total_rows, "quality_index": steward.quality_index,
            "completeness_pct": steward.completeness_pct, "warning_free_pct": steward.warning_free_pct,
            "errors": len(steward.errors), "warnings": len(steward.warnings), "blocked_projects": blocked,
            "required_fields": len(steward.required_fields)}


def findings_frame(steward: DataQualityReport) -> pd.DataFrame:
    ordered = sorted(steward.issues, key=lambda i: (i.severity != "ERROR", i.project_id, i.rule))
    return pd.DataFrame([{"Severity": i.severity, "Project": i.project_id, "Rule": rule_label(i.rule),
                          "Field": field_label(i.column), "Finding": i.message} for i in ordered],
                        columns=FINDING_COLUMNS)


def filter_findings(frame: pd.DataFrame, severity: str = "All", projects: list[str] | None = None) -> pd.DataFrame:
    """The findings filter: severity All / Errors / Warnings, optionally some projects only."""
    if severity == "Errors":
        frame = frame[frame["Severity"] == "ERROR"]
    elif severity == "Warnings":
        frame = frame[frame["Severity"] != "ERROR"]
    if projects:
        frame = frame[frame["Project"].isin(projects)]
    return frame


def interpretation_inputs(steward: DataQualityReport) -> tuple[str, str, bool]:
    """(summary JSON, model, LLM available) – the cache key of the executive interpretation."""
    from app.crew.dq_interpretation import summarize

    cfg = get_runtime_config()
    model = cfg.data_steward_model
    return json.dumps(summarize(steward), sort_keys=True, default=str), model, cfg.missing_key_for_model(model) is None


def interpret(summary_json: str, model: str, llm_on: bool):
    """The executive interpretation of a Data Steward summary (AI when available, else rule-based)."""
    from app.crew.dq_interpretation import interpret as interpret_dq

    return interpret_dq(json.loads(summary_json), use_llm=llm_on, model=model)


@lru_cache(maxsize=32)
def cached_interpretation(summary_json: str, model: str, llm_on: bool):
    """Once per data state and model, so the interpretation is not written again on every request."""
    return interpret(summary_json, model, llm_on)
