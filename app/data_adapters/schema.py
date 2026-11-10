"""Canonical portfolio schema: the tables every data source is mapped to, with key, required columns, column types,
allowed values and foreign keys. The mapping layer (app/data_adapters/mapping.py) checks loaded tables against it and
the Excel template (app/data_adapters/template.py) is generated from it."""
from __future__ import annotations

from dataclasses import dataclass, field

from app.core.required_fields import DEFAULT_CORE_FIELDS
from app.data_adapters.dataset import RESOURCE_PLAN_COLUMNS, TABLES

TEXT, NUMBER, DATE, BOOL = "text", "number", "date", "bool"


@dataclass(frozen=True)
class TableSchema:
    name: str
    description: str
    key: str | None
    columns: tuple[str, ...]
    required: tuple[str, ...] = ()
    allowed: dict[str, tuple[str, ...]] = field(default_factory=dict)
    foreign_keys: dict[str, str] = field(default_factory=dict)  # column -> referenced table (its key)

    def column_type(self, column: str) -> str:
        return column_type(column)


def column_type(column: str) -> str:
    """Type of a canonical column, derived from the naming convention of the schema."""
    if column.endswith("_date") or column in ("month", "valid_from", "date", "created_date", "updated"):
        return DATE
    if column in ("on_hold", "approver"):
        return BOOL
    if column.endswith(("_eur", "_hours", "_pct", "_fte", "_score", "_points")) or column in (
            "fte", "weight", "contribution", "rate_to_eur", "priority", "probability_pct", "version", "iteration",
            "story_points", "fiscal_year", "year", "quarter", "score", "scale_min", "scale_max", "duration_months",
            "available_fte", "year_from", "year_to"):
        return NUMBER
    return TEXT


PROJECT_FK = {"project_number": "Projects"}
DEMAND_TYPES = ("New", "Extension", "Replacement", "Mandatory")
DEMAND_STATUSES = ("Draft", "Submitted", "Assessed", "Planned", "Approved", "Deferred", "Rejected", "Converted")
CONFIDENCE = ("Rough", "Medium", "Fine")
DIRECTIONS = ("higher is better", "lower is better")
STATUS = ("Green", "Yellow", "Red")

_TABLES: tuple[TableSchema, ...] = (
    TableSchema(
        "Projects", "One row per project (ServiceNow pm_project).", "project_number",
        ("project_number", "project_name", "project_manager", "project_sponsor", "status", "state", "phase",
         "percent_complete", "planned_start_date", "actual_start_date", "approved_end_date", "planned_end_date",
         "execution_type", "priority", "portfolio", "business_unit", "business_capability", "business_case",
         "total_planned_cost_eur", "planned_capital_eur", "planned_operating_eur", "budget_cost_eur",
         "actual_cost_eur", "estimate_at_completion_eur", "estimate_to_completion_eur", "planned_benefit_eur",
         "planned_return_eur", "planned_roi_pct", "discount_rate_pct", "net_present_value_eur", "risk_score",
         "value_score", "currency"),
        required=DEFAULT_CORE_FIELDS,
        allowed={"status": STATUS, "execution_type": ("Agile", "Waterfall", "Hybrid")}),
    TableSchema(
        "Project_Tasks", "Plan tasks with their predecessor task (task chain).", "task_number",
        ("task_number", "project_number", "short_description", "dependency_task_number", "planned_start_date",
         "planned_end_date", "assigned_to", "assignment_group", "state", "percent_complete", "planned_effort_hours"),
        required=("task_number", "project_number", "state", "percent_complete", "planned_effort_hours"),
        foreign_keys=PROJECT_FK),
    TableSchema(
        "Resource_Plans", "Capacity and demand per project, team and month.", "resource_plan_number",
        RESOURCE_PLAN_COLUMNS, required=RESOURCE_PLAN_COLUMNS, foreign_keys=PROJECT_FK),
    TableSchema(
        "Stakeholders", "People involved in a project.", "stakeholder_number",
        ("stakeholder_number", "project_number", "user", "function", "influence", "level_of_interest", "engagement",
         "approver"),
        required=("stakeholder_number", "project_number", "user"), foreign_keys=PROJECT_FK),
    TableSchema(
        "Risks", "Project risks, optionally grouped into systemic clusters.", "risk_number",
        ("risk_number", "project_number", "short_description", "probability", "impact", "risk_status", "assigned_to",
         "due_date", "mitigation", "systemic_risk_cluster"),
        required=("risk_number", "project_number", "short_description", "impact"), foreign_keys=PROJECT_FK),
    TableSchema(
        "Stories", "Agile stories with story points.", "story_number",
        ("story_number", "project_number", "short_description", "assignment_group", "state", "sprint",
         "story_points"),
        required=("story_number", "project_number", "state", "story_points"), foreign_keys=PROJECT_FK),
    TableSchema(
        "Dependencies", "Project-to-project dependencies (predecessor delivers to successor).", "dependency_number",
        ("dependency_number", "predecessor_project_number", "successor_project_number", "dependency_type",
         "description", "required_by_date", "status"),
        required=("dependency_number", "predecessor_project_number", "successor_project_number"),
        foreign_keys={"predecessor_project_number": "Projects", "successor_project_number": "Projects"}),
    TableSchema(
        "Cost_Plans", "Planned and actual cost per project, month and cost type.", "cost_plan_number",
        ("cost_plan_number", "project_number", "month", "cost_type", "planned_cost_eur", "actual_cost_eur",
         "currency"),
        required=("cost_plan_number", "project_number", "cost_type", "planned_cost_eur", "actual_cost_eur"),
        allowed={"cost_type": ("CapEx", "OpEx")}, foreign_keys=PROJECT_FK),
    TableSchema(
        "Benefit_Plans", "Benefit tranches with their realisation start.", "benefit_plan_number",
        ("benefit_plan_number", "project_number", "benefit_type", "planned_benefit_eur", "realization_start_date",
         "owner", "currency"),
        required=("benefit_plan_number", "project_number", "planned_benefit_eur"), foreign_keys=PROJECT_FK),
    TableSchema(
        "Status_Reports", "RAG history per reporting date.", "status_number",
        ("status_number", "project_number", "status_date", "overall_health", "schedule", "cost", "resources",
         "scope"),
        required=("status_number", "project_number", "status_date"), foreign_keys=PROJECT_FK),
    TableSchema(
        "Baselines", "Approved plan versions per project.", "baseline_number",
        ("baseline_number", "project_number", "name", "created_date", "version", "baseline_budget_eur",
         "baseline_end_date", "baseline_eac_eur", "reason", "approved_by", "approval_date"),
        required=("baseline_number", "project_number"), foreign_keys=PROJECT_FK),
    TableSchema(
        "Approvals", "Approval records including stage-gate decisions.", "approval_number",
        ("approval_number", "project_number", "state", "approver", "short_description", "updated"),
        required=("approval_number", "project_number"), foreign_keys=PROJECT_FK),
    TableSchema(
        "Reserve_Ledger", "Draws on and returns to the management reserve.", "entry_number",
        ("entry_number", "date", "project_number", "amount_eur", "type", "reason", "approved_by", "proposal_id",
         "balance_after_eur"),
        required=("entry_number", "amount_eur")),
    TableSchema(
        "Team_Skills", "Skills and FTE per team.", None, ("assignment_group", "skill", "fte"),
        required=("assignment_group", "skill", "fte")),
    TableSchema(
        "Skill_Demand", "Demand hours per project, month and skill.", "skill_demand_number",
        ("skill_demand_number", "project_number", "month", "assignment_group", "skill", "demand_hours"),
        required=("project_number", "skill", "demand_hours"), foreign_keys=PROJECT_FK),
    TableSchema(
        "Benefit_Actuals", "Realised benefits reported after go-live.", "benefit_actual_number",
        ("benefit_actual_number", "project_number", "benefit_plan_number", "month", "actual_benefit_eur",
         "reported_by", "currency"),
        required=("benefit_actual_number", "project_number", "actual_benefit_eur"), foreign_keys=PROJECT_FK),
    TableSchema(
        "Exchange_Rates", "Currency to EUR rate with a valid-from date.", None,
        ("currency", "rate_to_eur", "valid_from"), required=("currency", "rate_to_eur")),
    TableSchema(
        "Strategic_Goals", "Strategic goals of the organisation with their weight.", "goal_id",
        ("goal_id", "goal_name", "weight"), required=("goal_id", "weight")),
    TableSchema(
        "Project_Goals", "Contribution (0-5) of a project to a strategic goal.", None,
        ("project_number", "goal_id", "contribution"), required=("project_number", "goal_id", "contribution"),
        foreign_keys={**PROJECT_FK, "goal_id": "Strategic_Goals"}),
    TableSchema(
        "Demands", "Planned initiatives that are no project yet: the master data of Projects plus planning fields.",
        "demand_number",
        ("demand_number", "demand_name", "requester", "project_sponsor", "demand_owner", "business_unit", "portfolio",
         "business_service", "business_capability", "execution_type", "priority", "tshirt_size", "project_summary",
         "business_case", "planned_benefit_eur", "planned_return_eur", "planned_roi_pct", "discount_rate_pct",
         "net_present_value_eur", "demand_type", "mandatory_reason", "strategic_goal", "earliest_start_date",
         "latest_start_date", "duration_months", "depends_on", "run_cost_after_go_live_eur", "demand_status",
         "converted_project_number"),
        required=("demand_number", "demand_name", "demand_owner", "business_unit", "portfolio", "demand_type",
                  "demand_status", "earliest_start_date", "duration_months"),
        allowed={"demand_type": DEMAND_TYPES, "demand_status": DEMAND_STATUSES,
                 "execution_type": ("Agile", "Waterfall", "Hybrid"), "tshirt_size": ("XS", "S", "M", "L", "XL")}),
    TableSchema(
        "Demand_Year_Plans", "CapEx, OpEx and benefit per demand, year and quarter.", None,
        ("demand_number", "year", "quarter", "capex_eur", "opex_eur", "benefit_eur", "confidence"),
        required=("demand_number", "year", "quarter", "capex_eur", "opex_eur"),
        allowed={"confidence": CONFIDENCE}, foreign_keys={"demand_number": "Demands"}),
    TableSchema(
        "Demand_Resource_Plans", "FTE per demand, year, quarter and team (optionally per skill).", None,
        ("demand_number", "year", "quarter", "assignment_group", "skill", "fte"),
        required=("demand_number", "year", "quarter", "assignment_group", "fte"),
        foreign_keys={"demand_number": "Demands"}),
    TableSchema(
        "Scoring_Model", "Assessment criteria of demands with weight, scale and direction.", "criterion",
        ("criterion", "weight", "scale_min", "scale_max", "direction"),
        required=("criterion", "weight", "scale_min", "scale_max", "direction"),
        allowed={"direction": DIRECTIONS}),
    TableSchema(
        "Demand_Scores", "Score of a demand per criterion (append-only, the latest assessment counts).",
        "score_number",
        ("score_number", "demand_number", "criterion", "score", "assessed_by", "assessed_on", "comment"),
        required=("demand_number", "criterion", "score"),
        foreign_keys={"demand_number": "Demands", "criterion": "Scoring_Model"}),
    TableSchema(
        "Budget_Envelopes", "Budget cap per year (or quarter) and business unit or portfolio.", None,
        ("year", "quarter", "scope_type", "scope", "capex_cap_eur", "opex_cap_eur", "reserve_pct", "cap_type",
         "approved_by", "approved_on"),
        required=("year", "scope_type", "scope", "capex_cap_eur", "opex_cap_eur"),
        allowed={"scope_type": ("business_unit", "portfolio"), "cap_type": ("hard", "soft")}),
    TableSchema(
        "Capacity_Limits", "Available FTE per team (optionally per skill) and year (or quarter).", None,
        ("year", "quarter", "assignment_group", "skill", "available_fte", "max_utilization_pct"),
        required=("year", "assignment_group", "available_fte")),
    TableSchema(
        "Planning_Scenarios", "Saved planning scenarios – one row per status, never overwritten.", "scenario_record",
        ("scenario_record", "scenario_id", "scenario_name", "year_from", "year_to", "budget_factor_pct", "scope",
         "forced_in", "excluded", "moves", "filling", "scenario_status", "verdict", "created_by", "created_on", "operation_id",
         "proposal_id", "approved_by", "approval_date"),
        required=("scenario_record", "scenario_id", "scenario_status"),
        allowed={"scenario_status": ("Draft", "Submitted", "Approved", "Archived"), "filling": ("rank", "value")}),
    TableSchema(
        "Planning_Items", "Decision per demand (or running project) in a saved scenario.", "item_record",
        ("item_record", "scenario_id", "scenario_status", "item_number", "year", "decision", "start_quarter",
         "capex_eur", "opex_eur", "fte", "reason"),
        required=("item_record", "scenario_id", "item_number", "decision"),
        allowed={"decision": ("In", "Out", "Deferred", "Forced-in")}),
)

SCHEMA: dict[str, TableSchema] = {t.name: t for t in _TABLES}
if set(SCHEMA) != set(TABLES.values()):  # every dataset table needs its schema
    raise RuntimeError("schema and dataset tables differ")

# Short field descriptions for the Excel template (fields not listed get their type only)
FIELD_DESCRIPTIONS: dict[str, str] = {
    "project_number": "Unique project id (e.g. PRJ-1001). Links all tables.",
    "project_name": "Project name as used in requests.",
    "project_manager": "Owning project manager; decides which projects a PM sees.",
    "project_sponsor": "Sponsor (governance required field).",
    "status": "Traffic light: Green, Yellow or Red.",
    "state": "Lifecycle state, e.g. Open, Work in Progress, Closed Complete.",
    "phase": "Stage-gate phase: Initiation, Planning, Executing, Closing, Closed.",
    "execution_type": "Agile, Waterfall or Hybrid.",
    "percent_complete": "Progress 0-100.",
    "budget_cost_eur": "Approved budget in EUR (or in the row's currency).",
    "actual_cost_eur": "Cost booked so far.",
    "estimate_at_completion_eur": "Forecast total cost (EAC).",
    "total_planned_cost_eur": "Planned total cost.",
    "planned_capital_eur": "Planned CapEx share.",
    "planned_operating_eur": "Planned OpEx share.",
    "planned_benefit_eur": "Planned benefit (governance required field).",
    "business_case": "Business case text (governance required field).",
    "priority": "1 = highest.",
    "value_score": "Business value 1-5.",
    "currency": "ISO currency of the row's amounts (default EUR).",
    "month": "First day of the month.",
    "cost_type": "CapEx or OpEx.",
    "assignment_group": "Team.",
    "predecessor_project_number": "Project that delivers.",
    "successor_project_number": "Project that waits for the delivery.",
    "required_by_date": "Date the successor needs the delivery.",
    "weight": "Weight of the strategic goal (> 0).",
    "contribution": "Contribution of the project to the goal, 0-5.",
    "rate_to_eur": "1 unit of the currency in EUR.",
    "demand_number": "Unique demand id (e.g. DMD-2027-001). Links the planning tables.",
    "demand_owner": "Project manager who owns the demand; decides which demands a PM sees.",
    "demand_type": "New, Extension, Replacement or Mandatory (regulation, contract, security).",
    "mandatory_reason": "Why a Mandatory demand cannot be dropped.",
    "demand_status": "Draft, Submitted, Assessed, Planned, Approved, Deferred, Rejected or Converted.",
    "earliest_start_date": "Earliest possible start.",
    "latest_start_date": "Latest acceptable start (a later start drops the demand).",
    "duration_months": "Planned duration in months.",
    "depends_on": "Demand or project numbers that must finish first, separated by ';'.",
    "run_cost_after_go_live_eur": "Yearly operating cost after go-live.",
    "year": "Calendar year.",
    "quarter": "Quarter 1-4.",
    "capex_eur": "Investment in the quarter.",
    "opex_eur": "Operating expense in the quarter.",
    "fte": "Full-time equivalents in the quarter.",
    "confidence": "Rough, Medium or Fine.",
    "criterion": "Assessment criterion.",
    "direction": "higher is better / lower is better (e.g. delivery risk).",
    "score": "Score on the criterion's scale.",
}


def describe(table: str, column: str) -> str:
    """Field description for the template: the text above plus the type and the allowed values."""
    spec = SCHEMA.get(table)
    parts = [FIELD_DESCRIPTIONS.get(column, "")]
    if spec and column in spec.allowed:
        parts.append("Allowed: " + ", ".join(spec.allowed[column]))
    if spec and column in spec.foreign_keys:
        parts.append(f"References {spec.foreign_keys[column]}")
    return " ".join(p for p in parts if p).strip()
