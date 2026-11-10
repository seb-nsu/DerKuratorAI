"""Project merge: the source project is absorbed by the target project.

Deterministic (no LLM), on the RLS-filtered scenario context; every change goes to the ledger, so the merge is
written only as an approved proposal (four-eyes). The target takes over the money (budget, CapEx/OpEx, actuals,
EAC/ETC, benefit and return – the source's figures go to zero, nothing is counted twice), the earlier start and the
later end. The source is closed ('Closed Incomplete', phase Closed). Its rows (tasks, stories, resource, cost, benefit
and skill plans, risks, stakeholders) are re-keyed to the target, and its dependencies are re-pointed – a dependency
between source and target would point at the target itself, so it stays as it is and is reported (close it by hand).
Status reports, baselines, approvals and reported actual benefits stay with the source as its history.

Facts for the verdict: `merge` (re-baseline of the target; a second business unit or sponsor is a condition),
`budget_change` of the target (board threshold), `budget_overrun` if the combined forecast exceeds the combined
budget, and the deadline check of the source's successors against the later end date.
"""
from __future__ import annotations

from typing import Any

from app.crew.tools.context import (
    allowed_project_ids,
    clear_facts,
    get_table,
    record_change,
    record_fact,
    record_row_change,
)
from app.crew.tools.simulator_tools import (
    INPUT_PREFIX,
    _eur,
    _num,
    _open_project,
    _opt_num,
    _safe,
    _to_date,
    repeated_call,
)
from app.crew.tools.timeline_effects import deadline_check
from app.data_adapters.base import MERGE_REKEY_TABLES
from app.llm.tools import tool

# money fields the target takes over from the source (the source keeps 0)
MERGED_MONEY = ("budget_cost_eur", "total_planned_cost_eur", "planned_capital_eur", "planned_operating_eur",
                "actual_cost_eur", "estimate_at_completion_eur", "estimate_to_completion_eur", "planned_benefit_eur",
                "planned_return_eur")
DEPENDENCY_SIDES = ("predecessor_project_number", "successor_project_number")
_LABELS = {"Project_Tasks": "tasks", "Stories": "stories", "Resource_Plans": "resource plan rows",
           "Cost_Plans": "cost plan rows", "Benefit_Plans": "benefit tranches", "Skill_Demand": "skill demand rows",
           "Risks": "risks", "Stakeholders": "stakeholders"}


def _same(a: Any, b: Any) -> bool:
    return str(a or "").strip().casefold() == str(b or "").strip().casefold()


@_safe
def merge_projects(source_id: str, target_id: str) -> str:
    source = _open_project(source_id, needs=("budget_cost_eur",))
    if isinstance(source, str):
        return source
    target = _open_project(target_id, needs=("budget_cost_eur",))
    if isinstance(target, str):
        return target
    sid, tid = str(source["project_number"]), str(target["project_number"])
    if sid == tid:
        return f"{INPUT_PREFIX} Source and target of a merge must be two different projects."

    lines = [f"RESULT simulate_merge | {sid} ({source['project_name']}) is merged into {tid} ({target['project_name']})"]
    # 1. money and dates move to the target; the source keeps nothing (no double count in the portfolio)
    lines.append("| Field | " + f"{tid} before | {sid} | {tid} after |")
    lines.append("|---|---|---|---|")
    combined: dict[str, float] = {}
    for fld in MERGED_MONEY:
        s_val, t_val = _opt_num(source.get(fld)), _opt_num(target.get(fld))
        if s_val is None and t_val is None:
            continue
        combined[fld] = (t_val or 0.0) + (s_val or 0.0)
        lines.append(f"| {fld} | {_eur(t_val or 0)} | {_eur(s_val or 0)} | {_eur(combined[fld])} |")
        record_change(tid, fld, round(combined[fld], 2), "simulate_merge", f"incl. {sid}")
        if s_val:
            record_change(sid, fld, 0.0, "simulate_merge", f"merged into {tid}")
    starts = [d for d in (_to_date(source.get("planned_start_date")), _to_date(target.get("planned_start_date"))) if d]
    ends = [d for d in (_to_date(source.get("planned_end_date")), _to_date(target.get("planned_end_date"))) if d]
    t_start, t_end = _to_date(target.get("planned_start_date")), _to_date(target.get("planned_end_date"))
    if starts and min(starts) != t_start:
        record_change(tid, "planned_start_date", min(starts).isoformat(), "simulate_merge", f"earlier start of {sid}")
    new_end = max(ends) if ends else None
    if new_end and new_end != t_end:
        record_change(tid, "planned_end_date", new_end.isoformat(), "simulate_merge", f"later end of {sid}")
    lines.append(f"- Planned period of {tid}: {min(starts) if starts else '–'} – {new_end or '–'}"
                 + (f" (was until {t_end})" if t_end and new_end and new_end != t_end else ""))
    record_change(sid, "state", "Closed Incomplete", "simulate_merge", f"merged into {tid}")
    record_change(sid, "phase", "Closed", "simulate_merge", f"merged into {tid}")
    lines.append(f"- {sid} is closed ('Closed Incomplete', phase Closed) – its status reports, baselines, approvals and "
                 "reported actual benefits stay with it as history.")

    # 2. the source's rows move to the target
    moved = []
    for table, key_col in MERGE_REKEY_TABLES.items():
        rows = get_table(table, sid)
        if rows.empty or key_col not in rows.columns:
            continue
        for key in rows[key_col].astype(str).str.strip():
            record_row_change(table, sid, key, "simulate_merge", field="project_number", new_value=tid,
                              note=f"merged into {tid}")
        moved.append(f"{len(rows)} {_LABELS.get(table, table)}")
    lines.append(f"- Moved to {tid}: {', '.join(moved)}" if moved else f"- {sid} has no plan rows to move.")

    # 3. dependencies: re-point the source's links; a link between source and target stays and is reported
    internal, repointed = [], 0
    deps = get_table("Dependencies")
    if not deps.empty and all(c in deps.columns for c in DEPENDENCY_SIDES + ("dependency_number",)):
        for row in deps.to_dict(orient="records"):
            pred, succ = str(row["predecessor_project_number"]).strip(), str(row["successor_project_number"]).strip()
            if sid not in (pred, succ):
                continue
            if {pred, succ} == {sid, tid}:
                internal.append(str(row["dependency_number"]))
                continue
            side = "predecessor_project_number" if pred == sid else "successor_project_number"
            record_row_change("Dependencies", sid, str(row["dependency_number"]), "simulate_merge", field=side,
                              new_value=tid, note=f"{sid} merged into {tid}")
            repointed += 1
    if repointed:
        lines.append(f"- Dependencies re-pointed to {tid}: {repointed}")
    if internal:
        lines.append(f"- ℹ️ Dependency {', '.join(internal)} between {sid} and {tid} becomes internal to {tid} – it "
                     "is left unchanged; close it by hand after the merge.")

    # 4. checks on the combined project
    budget, eac = combined.get("budget_cost_eur", 0.0), combined.get("estimate_at_completion_eur")
    moved_budget = _num(source.get("budget_cost_eur"))
    record_fact("budget_change", project=tid, delta=round(moved_budget, 2), new_budget=round(budget, 2))
    if eac and budget and eac > budget:
        record_fact("budget_overrun", project=tid, eac=round(eac, 2), new_budget=round(budget, 2),
                    overrun=round(eac - budget, 2), overrun_pct=round((eac / budget - 1) * 100, 1))
        lines.append(f"⚠ The combined forecast (EAC {_eur(eac)}) exceeds the combined budget {_eur(budget)}.")
    s_end = _to_date(source.get("planned_end_date"))
    if s_end and new_end and new_end > s_end:  # the source's successors now wait for the target's end
        lines += deadline_check(sid, s_end, new_end)
        clear_facts("deadline_breach", project=sid, successor=tid)  # the link becomes internal (reported above)
    units = {str(p.get("business_unit") or "").strip() for p in (source, target)} - {""}
    other_sponsor = not _same(source.get("project_sponsor"), target.get("project_sponsor"))
    if len(units) > 1:
        lines.append(f"⚠ The projects belong to different business units ({' / '.join(sorted(units))}).")
    record_fact("merge", project=sid, successor=tid, budget=round(budget, 2), end=new_end.isoformat() if new_end else None,
                cross_unit=sorted(units) if len(units) > 1 else None,
                sponsors=[source.get("project_sponsor"), target.get("project_sponsor")] if other_sponsor else None,
                internal=internal, out_of_scope=sid not in allowed_project_ids())
    return "\n".join(lines)


@tool("simulate_merge")
def simulate_merge(source_project_id: str, target_project_id: str) -> str:
    """Merges the source project INTO the target project: the target takes over budget, actuals, forecast and benefit
    (the source keeps 0), the earlier start and later end, the source's tasks, plans, risks and stakeholders and its
    dependencies; the source is closed. Reports the combined forecast, board threshold, successor deadlines and
    whether two business units are involved. Writes nothing (proposal)."""
    if skipped := repeated_call("simulate_merge", source_project_id):
        return skipped
    return merge_projects(source_project_id, target_project_id)


MERGE_TOOLS = [simulate_merge]
