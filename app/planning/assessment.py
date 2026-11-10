"""Change sets of the assessment: new scores of a demand (appended Demand_Scores rows plus the status Assessed once
every criterion has a score) and a change of the criterion weights (four-eyes: it decides which demands get a
budget)."""
from __future__ import annotations

from typing import Any

import pandas as pd

from app.core import clock
from app.data_adapters.dataset import DEMAND_KEY, PortfolioDataset
from app.planning.scoring import MANDATORY, criteria


def _next_numbers(ds: PortfolioDataset, demand: str, count: int) -> list[str]:
    existing = set(ds.demand_scores["score_number"].astype(str)) \
        if not ds.demand_scores.empty and "score_number" in ds.demand_scores.columns else set()
    out, n = [], 1
    while len(out) < count:
        number = f"SC-{demand}-{n:03d}"
        if number not in existing:
            out.append(number)
        n += 1
    return out


def score_changes(ds: PortfolioDataset, demand: str, scores: dict[str, float], by: str,
                  comments: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """Appended score rows for the criteria in `scores` (values outside the scale raise ValueError) and – for a
    complete assessment of a Submitted demand – the status change to Assessed."""
    model = criteria(ds).set_index("criterion")
    unknown = sorted(set(scores) - set(model.index))
    if unknown:
        raise ValueError(f"Unknown criterion: {', '.join(unknown)}")
    for crit, value in scores.items():
        low, high = model.at[crit, "scale_min"], model.at[crit, "scale_max"]
        if not low <= float(value) <= high:
            raise ValueError(f"{crit}: {value:g} is outside the scale {low:g}-{high:g}")
    row = ds.demands[ds.demands[DEMAND_KEY].astype(str) == demand]
    if row.empty:
        raise ValueError(f"Demand {demand} not found")
    today = clock.today().isoformat()
    numbers = _next_numbers(ds, demand, len(scores))
    changes: list[dict[str, Any]] = []
    for number, (crit, value) in zip(numbers, scores.items(), strict=True):
        new = {"score_number": number, DEMAND_KEY: demand, "criterion": crit, "score": float(value),
               "assessed_by": by, "assessed_on": today, "comment": (comments or {}).get(crit) or None}
        changes.append({"op": "append", "table": "Demand_Scores", "project_id": demand, "key": number,
                        "field": "Demand_Scores", "old": None, "new": f"{crit}: {value:g}", "row": new,
                        "source_tool": "assess_demand"})
    status = row.iloc[0].get("demand_status")
    done = set(scores)
    if not ds.demand_scores.empty and {DEMAND_KEY, "criterion"} <= set(ds.demand_scores.columns):
        done |= set(ds.demand_scores.loc[ds.demand_scores[DEMAND_KEY].astype(str) == demand, "criterion"].astype(str))
    if status == "Submitted" and row.iloc[0].get("demand_type") != MANDATORY and set(model.index) <= done:
        changes.append({"table": "Demands", "project_id": demand, "key": demand, "field": "demand_status",
                        "old": status, "new": "Assessed", "source_tool": "assess_demand"})
    return changes


def weight_changes(ds: PortfolioDataset, weights: dict[str, float]) -> list[dict[str, Any]]:
    """Row updates of the criterion weights that actually change (governed: submitted as a proposal)."""
    model = ds.scoring_model
    current = dict(zip(model["criterion"].astype(str), pd.to_numeric(model["weight"], errors="coerce"), strict=True)) \
        if not model.empty else {}
    changes = []
    for crit, weight in weights.items():
        if crit not in current:
            raise ValueError(f"Unknown criterion: {crit}")
        if weight < 0:
            raise ValueError(f"{crit}: a weight cannot be negative")
        if float(current[crit]) != float(weight):
            changes.append({"table": "Scoring_Model", "project_id": "", "key": crit, "field": "weight",
                            "old": float(current[crit]), "new": float(weight), "source_tool": "scoring_weights"})
    return changes
