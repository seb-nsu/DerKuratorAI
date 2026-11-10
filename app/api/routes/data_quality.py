"""Data quality page: the deterministic Data Steward checks of the caller's scope – key figures, quality bars and the
findings table with its filters – and the executive interpretation (AI when available, else rule-based)."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.api.deps import Scope, requester, scope
from app.core.auth import User
from app.crew.tools.data_steward_tools import run_all_checks
from app.views import data_quality

router = APIRouter(tags=["data quality"])


class DataQualitySummary(BaseModel):
    projects_checked: int
    quality_index: float  # share of projects without ERROR findings (released for simulation)
    completeness_pct: float
    warning_free_pct: float
    errors: int
    warnings: int
    blocked_projects: list[str]  # project ids in the caller's scope
    required_fields: int


class DataQualityBar(BaseModel):
    label: str
    value: float
    status: Literal["Green", "Yellow", "Red"]
    word: Literal["Good", "Fair", "Poor"]
    detail: str


class DataQualityFinding(BaseModel):
    severity: str
    project: str  # "*" for a finding about the data source as a whole
    rule: str
    field: str
    finding: str


class DataQualityPage(BaseModel):
    summary: DataQualitySummary
    index_help: str
    bars: list[DataQualityBar]
    findings: list[DataQualityFinding]
    findings_shown: int
    findings_total: int
    projects: list[str]  # the project filter's options


class DataQualityInterpretation(BaseModel):
    text: str
    source: Literal["llm", "rules"]
    ai_generated: bool
    model: str | None  # only for an AI interpretation


def _steward(user: User = Depends(requester), ctx: Scope = Depends(scope)):
    """The Data Steward report of the caller's scope (PMO and Project Manager)."""
    return run_all_checks(ctx.visible)


@router.get("/data-quality")
def data_quality_page(severity: Literal["All", "Errors", "Warnings"] = "All",
                      project: list[str] | None = Query(None, description="Only these projects (repeatable)"),
                      steward=Depends(_steward)) -> DataQualityPage:
    frame = data_quality.findings_frame(steward)
    shown = data_quality.filter_findings(frame, severity, project)
    return DataQualityPage(
        summary=DataQualitySummary(**data_quality.summary(steward)), index_help=data_quality.INDEX_HELP,
        bars=[DataQualityBar(**b) for b in data_quality.quality_bars(steward)],
        findings=[DataQualityFinding(**{k.lower(): str(v) for k, v in row.items()})
                  for row in shown.to_dict(orient="records")],
        findings_shown=len(shown), findings_total=len(frame), projects=sorted(frame["Project"].astype(str).unique()))


@router.get("/data-quality/interpretation")
def interpretation(steward=Depends(_steward)) -> DataQualityInterpretation:
    """Phrased for management from aggregated, name-free figures; the checks themselves stay rule-based."""
    result = data_quality.cached_interpretation(*data_quality.interpretation_inputs(steward))
    llm = result.source == "llm"
    return DataQualityInterpretation(text=result.text, source=result.source, ai_generated=llm,
                                     model=result.model if llm else None)
