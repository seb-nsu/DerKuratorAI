"""Executive interpretation of data quality.

The checks themselves stay deterministic (Data Steward, no LLM). This layer phrases the
results for management: via LLM (advisory model), otherwise rule-based. The LLM only receives
aggregated metrics, rule names, field names and project IDs – no person names.
"""
from __future__ import annotations

import hashlib
import json
import logging
from collections import Counter
from dataclasses import dataclass
from typing import Any

from app.core.runtime_config import get_runtime_config
from app.core.security import mask_pii_data
from app.crew.tools.data_steward_tools import DataQualityReport

logger = logging.getLogger("derkurator.dq")
AI_LABEL = "⚠️ [AI-GENERATED]"


@dataclass(frozen=True)
class DQInterpretation:
    text: str
    source: str  # "llm" | "rules"
    model: str | None = None


def summarize(report: DataQualityReport) -> dict[str, Any]:
    """Hashable, PII-free summary (basis for the cache key and the LLM prompt)."""
    rules = Counter(i.rule.split(":")[0] for i in report.issues)
    tags = Counter(i.rule.split(":", 1)[1] for i in report.issues if i.rule.startswith("DQ-Profile:"))
    fields = Counter(i.column for i in report.errors if i.project_id != "*")
    per_project = Counter(i.project_id for i in report.issues if i.project_id != "*")
    return {
        "projects": report.total_rows,
        "index": report.quality_index,
        "completeness": report.completeness_pct,
        "without_findings": report.warning_free_pct,
        "errors": len(report.errors),
        "warnings": len(report.warnings),
        "blocked": report.blocked_project_ids,
        "rules": dict(rules.most_common(8)),
        "dq_tags": dict(tags),
        "missing_fields": dict(fields.most_common(6)),
        "most_affected": [pid for pid, _ in per_project.most_common(3)],
        "required_fields": len(report.required_fields),
    }


def fingerprint(summary: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(summary, sort_keys=True, default=str).encode()).hexdigest()[:16]


def rules_interpretation(s: dict[str, Any]) -> str:
    level = "reliable" if s["index"] >= 90 else "partially reliable" if s["index"] >= 70 else "critical"
    lines = [
        f"**Overall picture:** The portfolio data is **{level}** – {s['index']:.0f} % of the {s['projects']} projects are "
        f"released for simulation, required fields are {s['completeness']:.0f} % complete.",
    ]
    if s["blocked"]:
        lines.append(f"**Decision risk:** {len(s['blocked'])} project(s) ({', '.join(s['blocked'])}) are blocked; "
                     "budget and schedule decisions on them rely on qualitative estimates only.")
    if s["missing_fields"]:
        top = ", ".join(f"`{f}` ({n}×)" for f, n in list(s["missing_fields"].items())[:3])
        lines.append(f"**Most frequent gaps:** {top}.")
    if s["rules"].get("Budget Overrun"):
        lines.append(f"**Financials:** {s['rules']['Budget Overrun']} budget overrun signal(s) – forecasts exceed budgets.")
    if s["rules"].get("Resource Overload"):
        lines.append(f"**Capacity:** {s['rules']['Resource Overload']} project(s) with team overload > 100 %.")
    steps = []
    if s["blocked"]:  # only recommend a clean-up that exists
        steps.append("have project managers clean up the master data of the blocked projects before the next steering "
                     "committee")
    if s["rules"].get("Budget Overrun") or s["rules"].get("Resource Overload"):
        steps.append("address the overrun and overload signals in the portfolio review")
    recommendation = "; ".join(steps) if steps else "no clean-up needed – keep the data current"
    lines.append(f"**Recommendation:** {recommendation[0].upper()}{recommendation[1:]}.")
    return "\n\n".join(lines)


def interpret(summary: dict[str, Any], use_llm: bool = True, model: str | None = None) -> DQInterpretation:
    """`model`: defaults to the Data Steward's main model from the env config (shadow runs pass their own)."""
    cfg = get_runtime_config()
    model = model or cfg.data_steward_model
    if use_llm and cfg.missing_key_for_model(model) is None:
        try:
            from app.llm.client import complete

            text = complete(model, (
                "You are a Chief Data Officer. Write a concise executive interpretation of the data quality of an "
                "IT project portfolio for a board (English, Markdown, max. 5 short paragraphs with bold headings). "
                "Name the decision risk and concrete actions. Use only the figures provided, never invent numbers."
            ), mask_pii_data(json.dumps(summary, ensure_ascii=False)), purpose="data quality interpretation")
            if text.strip():
                return DQInterpretation(f"{AI_LABEL} {mask_pii_data(text.strip())}", "llm", model)
        except Exception as exc:  # noqa: BLE001 - interpretation is optional, rule-based fallback
            logger.warning("LLM interpretation of data quality failed (%s) – rule-based text", exc)
    return DQInterpretation(rules_interpretation(summary), "rules")
