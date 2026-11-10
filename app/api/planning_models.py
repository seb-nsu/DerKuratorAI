"""Response models of the planning routes (app/api/routes/planning.py): the OpenAPI contract the front ends generate
their types from. `Row` is a table row whose columns come from the data source; `Figure` is Plotly figure JSON."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

Row = dict[str, Any]
Figure = dict[str, Any]


class ConditionDetail(BaseModel):
    condition: str
    owner_role: str
    due: str | None = None


class Verdict(BaseModel):
    """The rule-based verdict (never an LLM); `rules` are the thresholds in force when it was calculated."""
    decision: str
    label: str
    rule: str = ""
    reasons: list[str] = []
    conditions: list[str] = []
    condition_details: list[ConditionDetail] = []
    rules: dict[str, Any] = {}
    as_of: str | None = None
    due: str | None = None


class ScenarioInput(BaseModel):
    name: str
    year_from: int | None = None
    year_to: int | None = None
    budget_factor_pct: float = 0.0
    scope: str | None = None
    forced_in: dict[str, str] = {}
    excluded: dict[str, str] = {}
    moves: dict[str, int] = {}
    method: str


class ScenarioHeader(BaseModel):
    name: str
    input: ScenarioInput
    input_text: str
    years: list[int]
    binding_year: int
    as_of: str
    verdict: Verdict
    estimated_commitments: bool
    solved: bool


class DecisionCounts(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    in_: int = Field(alias="In")
    forced_in: int = Field(alias="Forced-in")
    deferred: int = Field(alias="Deferred")
    out: int = Field(alias="Out")


class OptimizerView(BaseModel):
    """What the value-optimal choice does differently from the rank order, and why."""
    summary: list[str]
    figures: list[Row]
    changes: list[Row]


class ScenarioView(ScenarioHeader):
    decision_counts: DecisionCounts
    items: list[Row]
    budget: list[Row]
    capacity: list[Row]
    capacity_grid: list[Row]
    capacity_chart: Figure | None = None
    optimizer: OptimizerView | None = None


class PlanningContext(BaseModel):
    first_year: int
    last_year: int
    reference_date: str
    demands: int
    sections: list[str]
    methods: dict[str, str]


class Finding(BaseModel):
    demand: str | None = None
    severity: str | None = None
    field: str | None = None
    message: str


class DemandFilters(BaseModel):
    business_units: list[str] = []
    statuses: list[str] = []
    years: list[str] = []


class DemandList(BaseModel):
    demands: list[Row]
    total: int
    rank_shown: bool
    filters: DemandFilters
    data_quality_findings: list[Finding]


class DemandHeader(BaseModel):
    demand_number: str
    demand_name: str | None = None
    demand_status: str | None = None
    demand_type: str | None = None
    business_unit: str | None = None
    portfolio: str | None = None
    demand_owner: str | None = None
    duration_months: float | None = None
    net_present_value_eur: float | None = None
    run_cost_after_go_live_eur: float | None = None
    mandatory_reason: str | None = None


class MasterDataRow(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    field: str = Field(alias="Field")
    value: str = Field(alias="Value")
    label: str


class AnnualTotals(BaseModel):
    capex_eur: float
    opex_eur: float


class AssessmentView(BaseModel):
    mandatory: bool
    parts: list[Row]
    score: float | None = None


class PlanPosition(BaseModel):
    """The demand in the base plan and next to the envelopes and team limits (PMO and Portfolio Board only)."""
    decision: str | None = None
    reason: str | None = None
    budget: list[Row]
    capacity: list[Row]


class DemandDetail(BaseModel):
    demand: DemandHeader
    master_data: list[MasterDataRow]
    annual_figures: list[Row]
    annual_totals: AnnualTotals
    capacity: list[Row]
    assessment: AssessmentView
    dependencies: list[Row]
    outside_scope_links: bool
    can_slip: bool
    position: PlanPosition | None = None


class SlipImpact(BaseModel):
    quarters: int
    moves: list[Row]
    moved: int
    late: int


class PlanAnalysis(ScenarioHeader):
    hot_share: float
    budget_hot_spots: list[Row]
    capacity_hot_spots: list[Row]
    critical_chains: list[Row]


class PlanInForce(BaseModel):
    scenario_id: str
    scenario_name: str
    year_from: int
    year_to: int
    approved_by: str | None = None


class BoardLanding(BaseModel):
    waiting: int
    plan_in_force: PlanInForce | None = None
    base_plan_verdict: str
    demands: int
    in_base_plan: int


class Criterion(BaseModel):
    criterion: str
    weight: float
    scale_min: float
    scale_max: float
    direction: str


class CriteriaList(BaseModel):
    criteria: list[Criterion]


class AssessableDemand(BaseModel):
    demand_number: str
    demand_name: str | None = None
    demand_status: str | None = None
    score: float | None = None


class AssessmentCandidates(BaseModel):
    demands: list[AssessableDemand]


class CriterionInput(Criterion):
    value: float  # the current score, else the lowest score


class AssessmentForm(BaseModel):
    demand: str
    criteria: list[CriterionInput]


class WriteResult(BaseModel):
    message: str
    applied: int
    snapshot_id: str | None = None


class ProposalSummary(BaseModel):
    id: str
    timestamp: str | None = None
    requester: str | None = None
    requester_role: str | None = None
    status: str
    verdict: str | None = None
    request: str | None = None
    projects: list[str] = []
    changes: list[Row] = []
    forced: bool | None = None
    justification: str | None = None
    comment: str | None = None
    decided_by: str | None = None
    decided_at: str | None = None
    escalation_level: int | None = None
    snapshot_id: str | None = None
    reverts: str | None = None
    override: bool | None = None
    on_behalf_of: str | None = None
    kind: str | None = None
    deadline: str
    due: str | None = None


class ProposalAnswer(BaseModel):
    message: str
    proposal: ProposalSummary | None = None


class ApprovedDemand(BaseModel):
    demand_number: str
    demand_name: str | None = None


class ConvertedDemand(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    demand: str = Field(alias="Demand")
    name: str | None = Field(None, alias="Name")
    project: str | None = Field(None, alias="Project")


class Conversions(BaseModel):
    approved: list[ApprovedDemand]
    converted: list[ConvertedDemand]


class Candidate(BaseModel):
    demand: str
    label: str


class YearRange(BaseModel):
    default: int
    min: int
    max: int


class FactorRange(YearRange):
    step: int


class ScenarioOptions(BaseModel):
    candidates: list[Candidate]
    scopes: list[str]
    methods: dict[str, str]
    year_from: YearRange
    year_to: YearRange
    budget_factor_pct: FactorRange
    max_compared: int


class ComparisonView(BaseModel):
    comparison: list[Row]
    scenarios: list[ScenarioHeader]


class SavedScenario(BaseModel):
    scenario_id: str
    scenario_name: str | None = None
    year_from: int | None = None
    year_to: int | None = None
    scenario_status: str
    verdict: str | None = None
    created_by: str | None = None
    created_on: str | None = None
    submittable: bool


class SavedScenarios(BaseModel):
    scenarios: list[SavedScenario]


class DraftSaved(BaseModel):
    message: str
    scenario_id: str
    applied: int
    verdict: str


class PlanSummary(ProposalSummary):
    requires_override: bool
    change_count: int


class PlanList(BaseModel):
    plans: list[PlanSummary]


class PlanDetail(ProposalSummary):
    paper: str | None = None
    verdict_record: Verdict | None = None
    requires_override: bool
    change_set: list[Row]


class DecisionOutcome(BaseModel):
    kind: str  # success | warning | error
    message: str
    snapshot_id: str | None = None


class ReportOptions(BaseModel):
    options: list[str]


class MultiYearReport(ScenarioHeader):
    multi_year: list[Row]
