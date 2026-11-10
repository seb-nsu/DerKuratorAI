"""Saved planning scenarios: appended rows of Planning_Scenarios (one per status, never overwritten) and Planning_Items
(one per demand and year). A saved scenario keeps its input, so it can be recalculated against today's data; the
items record what the plan said when it was saved."""
from __future__ import annotations

import json
from typing import Any

import pandas as pd

from app.core import clock
from app.data_adapters.dataset import DEMAND_KEY, PortfolioDataset
from app.planning.scenario import KINDS, METHODS, OUT, RANK, VALUE, ScenarioInput, ScenarioResult

DRAFT, SUBMITTED, APPROVED, ARCHIVED = "Draft", "Submitted", "Approved", "Archived"
STATUS_ORDER = {DRAFT: 0, SUBMITTED: 1, APPROVED: 2, ARCHIVED: 3}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _loads(value: Any, default: Any) -> Any:
    if value is None or (isinstance(value, float) and pd.isna(value)) or value == "":
        return default
    try:
        return json.loads(value) if isinstance(value, str) else value
    except ValueError:
        return default


def next_scenario_id(ds: PortfolioDataset, year: int) -> str:
    existing = set(ds.planning_scenarios["scenario_id"].astype(str)) \
        if not ds.planning_scenarios.empty and "scenario_id" in ds.planning_scenarios.columns else set()
    n = 1
    while f"SCN-{year}-{n:03d}" in existing:
        n += 1
    return f"SCN-{year}-{n:03d}"


def scenario_rows(result: ScenarioResult, scenario_id: str, status: str, by: str) -> tuple[dict, list[dict]]:
    """The Planning_Scenarios row and the Planning_Items rows of a calculated scenario."""
    spec, today = result.input, clock.today().isoformat()
    head = {"scenario_record": f"{scenario_id}-{status}", "scenario_id": scenario_id, "scenario_name": spec.name,
            "year_from": result.years[0], "year_to": result.years[-1], "budget_factor_pct": spec.budget_factor_pct,
            "scope": spec.scope or "", "forced_in": _json(spec.forced_in), "excluded": _json(spec.excluded),
            "moves": _json(spec.moves), "filling": spec.method, "scenario_status": status, "verdict": result.verdict.label,
            "created_by": by, "created_on": today}
    if status == APPROVED:  # filled at the write with the Board member and the date (an appended row is stamped)
        head.update(approved_by="", approval_date="")
    items = []
    for row in result.items.to_dict(orient="records") if not result.items.empty else []:
        years = [y for y in result.years if any(row.get(f"{label} {y}") for _, label in KINDS)] \
            if row["decision"] != OUT else [result.years[0]]
        for year in years:
            items.append({"item_record": f"{scenario_id}-{status}-{row[DEMAND_KEY]}-{year}", "scenario_id": scenario_id,
                          "scenario_status": status, "item_number": row[DEMAND_KEY], "year": year,
                          "decision": row["decision"], "start_quarter": row.get("start") or "",
                          "capex_eur": round(float(row.get(f"CapEx {year}") or 0), 2),
                          "opex_eur": round(float(row.get(f"OpEx {year}") or 0), 2),
                          "fte": round(float(row.get("fte_peak") or 0), 2), "reason": row.get("reason") or ""})
    return head, items


def save_changes(ds: PortfolioDataset, result: ScenarioResult, by: str, status: str = DRAFT,
                 scenario_id: str | None = None) -> list[dict[str, Any]]:
    """Append changes that store the scenario with the given status (a Draft is written by the PMO directly; the
    Approved plan goes through the Portfolio Board's approval)."""
    scenario_id = scenario_id or next_scenario_id(ds, result.years[0])
    head, items = scenario_rows(result, scenario_id, status, by)
    changes = [{"op": "append", "table": "Planning_Scenarios", "project_id": "", "key": head["scenario_record"],
                "field": "Planning_Scenarios", "old": None, "new": f"{scenario_id} {spec_text(result.input)} ({status})",
                "row": head, "source_tool": "planning_scenario"}]
    changes += [{"op": "append", "table": "Planning_Items", "project_id": "", "key": i["item_record"],
                 "field": "Planning_Items", "old": None, "new": f"{i['item_number']} {i['year']}: {i['decision']}",
                 "row": i, "source_tool": "planning_scenario"} for i in items]
    return changes


def spec_text(spec: ScenarioInput) -> str:
    parts = [f"“{spec.name}”"]
    if spec.budget_factor_pct:
        parts.append(f"budget {spec.budget_factor_pct:+g} %")
    if spec.scope:
        parts.append(spec.scope.replace("_", " ").replace(":", " "))
    if spec.forced_in:
        parts.append("forced in " + ", ".join(sorted(spec.forced_in)))
    if spec.excluded:
        parts.append("excluded " + ", ".join(sorted(spec.excluded)))
    if spec.moves:
        parts.append("moved " + ", ".join(f"{d} {m:+d} y" for d, m in sorted(spec.moves.items())))
    if spec.method != RANK:
        parts.append(METHODS.get(spec.method, spec.method).lower())
    return " · ".join(parts)


def saved_scenarios(ds: PortfolioDataset) -> pd.DataFrame:
    """The latest status of every saved scenario (newest first)."""
    s = ds.planning_scenarios
    if s.empty or not {"scenario_id", "scenario_status"} <= set(s.columns):
        return pd.DataFrame()
    s = s.assign(_order=s["scenario_status"].map(STATUS_ORDER).fillna(-1))
    latest = s.sort_values(["scenario_id", "_order"]).groupby("scenario_id").tail(1).drop(columns="_order")
    return latest.sort_values("scenario_id", ascending=False).reset_index(drop=True)


def approved_plan(ds: PortfolioDataset) -> pd.Series | None:
    """The approved plan with the latest planning years (None if no plan is approved)."""
    saved = saved_scenarios(ds)
    if saved.empty:
        return None
    approved = saved[saved["scenario_status"] == APPROVED]
    if approved.empty:
        return None
    return approved.sort_values(["year_from", "scenario_id"]).iloc[-1]


def input_of(row: pd.Series | dict) -> ScenarioInput:
    """The input of a saved scenario – to recalculate it against today's data."""
    def num(value: Any) -> int | None:
        number = pd.to_numeric(value, errors="coerce")
        return None if pd.isna(number) else int(number)

    factor = pd.to_numeric(row.get("budget_factor_pct"), errors="coerce")
    scope = row.get("scope")
    return ScenarioInput(name=str(row.get("scenario_name") or row.get("scenario_id")), year_from=num(row.get("year_from")),
                         year_to=num(row.get("year_to")), budget_factor_pct=0.0 if pd.isna(factor) else float(factor),
                         scope=scope if isinstance(scope, str) and scope else None,
                         forced_in=_loads(row.get("forced_in"), {}), excluded=_loads(row.get("excluded"), {}),
                         moves={k: int(v) for k, v in _loads(row.get("moves"), {}).items()},
                         method=VALUE if str(row.get("filling") or "").strip() == VALUE else RANK)


def items_of(ds: PortfolioDataset, scenario_id: str, status: str) -> pd.DataFrame:
    i = ds.planning_items
    if i.empty or not {"scenario_id", "scenario_status"} <= set(i.columns):
        return pd.DataFrame()
    return i[(i["scenario_id"].astype(str) == scenario_id) & (i["scenario_status"] == status)].reset_index(drop=True)


def comparison(results: list[ScenarioResult]) -> pd.DataFrame:
    """Side-by-side key figures of 2-3 calculated scenarios."""
    rows = []
    for r in results:
        items = r.items if not r.items.empty else pd.DataFrame(columns=["decision", "npv_eur"])
        placed = items[items["decision"] != OUT]
        binding = r.years[0]
        rows.append({"Scenario": r.input.name, "Filling": METHODS.get(r.input.method, r.input.method),
                     "Verdict": r.verdict.label,
                     "In": int((items["decision"].isin(["In", "Forced-in"])).sum()),
                     "Deferred": int((items["decision"] == "Deferred").sum()), "Out": int((items["decision"] == OUT).sum()),
                     f"CapEx {binding}": float(placed.get(f"CapEx {binding}", pd.Series(dtype=float)).sum()),
                     f"OpEx {binding}": float(placed.get(f"OpEx {binding}", pd.Series(dtype=float)).sum()),
                     "NPV planned": float(placed["npv_eur"].sum()),
                     "Conditions": len(r.verdict.conditions)})
    return pd.DataFrame(rows)

