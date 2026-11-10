"""Assessment and ranking of demands.

Score: per criterion of the Scoring_Model the latest score of a demand is normalised to 0-1 on the criterion's scale
(turned round for "lower is better", e.g. delivery risk), weighted and summed to 0-100. A demand is assessed when it
has a score for every criterion. Mandatory demands are marked, not scored.

Rank (always unique and reproducible): mandatory demands first, then the score (high first, unassessed demands
last), then the NPV (high first), then the latest start (early first), then the demand number."""
from __future__ import annotations

import pandas as pd

from app.data_adapters.dataset import DEMAND_KEY, PortfolioDataset

MANDATORY = "Mandatory"
LOWER_IS_BETTER = "lower is better"
# Demands that take part in a planning scenario; Approved ones are already committed, Converted ones are projects
ELIGIBLE_STATUSES = ("Submitted", "Assessed", "Planned", "Deferred")
COMMITTED_STATUSES = ("Approved",)


def _num(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s, errors="coerce")


def criteria(ds: PortfolioDataset) -> pd.DataFrame:
    """The scoring model with numeric weight and scale; criteria without a positive weight or a valid scale drop out."""
    sm = ds.scoring_model
    if sm.empty or not {"criterion", "weight", "scale_min", "scale_max"} <= set(sm.columns):
        return pd.DataFrame(columns=["criterion", "weight", "scale_min", "scale_max", "direction"])
    out = sm.assign(criterion=sm["criterion"].astype(str).str.strip(), weight=_num(sm["weight"]),
                    scale_min=_num(sm["scale_min"]), scale_max=_num(sm["scale_max"]),
                    direction=sm.get("direction", pd.Series("higher is better", index=sm.index)).fillna(
                        "higher is better").astype(str).str.strip())
    out = out[(out["weight"] > 0) & (out["scale_max"] > out["scale_min"])]
    return out.drop_duplicates("criterion", keep="last")[["criterion", "weight", "scale_min", "scale_max",
                                                          "direction"]].reset_index(drop=True)


def latest_scores(ds: PortfolioDataset) -> pd.DataFrame:
    """The latest score per demand and criterion (Demand_Scores is append-only; a later row replaces an earlier)."""
    sc = ds.demand_scores
    cols = [DEMAND_KEY, "criterion", "score", "assessed_by", "assessed_on", "comment"]
    if sc.empty or not {DEMAND_KEY, "criterion", "score"} <= set(sc.columns):
        return pd.DataFrame(columns=cols)
    sc = sc.assign(**{DEMAND_KEY: sc[DEMAND_KEY].astype(str).str.strip(),
                      "criterion": sc["criterion"].astype(str).str.strip(), "score": _num(sc["score"])})
    sc = sc.reset_index(drop=True).assign(_order=range(len(sc)))
    if "assessed_on" in sc.columns:
        sc = sc.assign(_on=pd.to_datetime(sc["assessed_on"], errors="coerce")) \
            .sort_values(["_on", "_order"], na_position="first")
    latest = sc.drop_duplicates([DEMAND_KEY, "criterion"], keep="last")
    return latest.reindex(columns=cols).reset_index(drop=True)


def score_breakdown(ds: PortfolioDataset) -> pd.DataFrame:
    """One row per demand and criterion: score, normalised points and weighted contribution (score composition)."""
    model, scores = criteria(ds), latest_scores(ds)
    if model.empty or scores.empty:
        return pd.DataFrame(columns=[DEMAND_KEY, "criterion", "score", "points", "weight", "contribution"])
    total = float(model["weight"].sum())
    merged = scores.merge(model, on="criterion", how="inner")
    norm = ((merged["score"] - merged["scale_min"]) / (merged["scale_max"] - merged["scale_min"])).clip(0, 1)
    norm = norm.where(merged["direction"] != LOWER_IS_BETTER, 1 - norm)
    merged["points"] = (norm * 100).round(1)
    merged["contribution"] = (norm * merged["weight"] / total * 100).round(2)
    return merged[[DEMAND_KEY, "criterion", "score", "points", "weight", "contribution", "assessed_by",
                   "assessed_on", "comment"]]


def ranking(ds: PortfolioDataset) -> pd.DataFrame:
    """Demands with score, assessment state, mandatory flag and the unique rank (see module doc)."""
    d = ds.demands
    if d.empty or DEMAND_KEY not in d.columns:
        return pd.DataFrame(columns=[DEMAND_KEY, "score", "assessed", "mandatory", "rank"])
    view = d.copy()
    view[DEMAND_KEY] = view[DEMAND_KEY].astype(str).str.strip()
    model = criteria(ds)
    parts = score_breakdown(ds)
    scored = parts.groupby(DEMAND_KEY).agg(score=("contribution", "sum"), scored=("criterion", "nunique"))
    view["score"] = view[DEMAND_KEY].map(scored["score"].round(1))
    view["criteria_scored"] = view[DEMAND_KEY].map(scored["scored"]).fillna(0).astype(int)
    view["assessed"] = (len(model) > 0) & (view["criteria_scored"] >= len(model))
    view.loc[~view["assessed"], "score"] = float("nan")
    view["mandatory"] = view.get("demand_type", pd.Series(index=view.index, dtype=object)).astype(str) == MANDATORY
    npv = _num(view["net_present_value_eur"]) if "net_present_value_eur" in view.columns else pd.Series(0.0, index=view.index)
    latest = pd.to_datetime(view["latest_start_date"], errors="coerce") if "latest_start_date" in view.columns \
        else pd.Series(pd.NaT, index=view.index)
    order = pd.DataFrame({
        "m": (~view["mandatory"]).astype(int),
        "unscored": view["score"].isna().astype(int),
        "score": -view["score"].fillna(0),
        "npv": -npv.fillna(float("-inf")).clip(lower=-1e18),
        "latest": latest.fillna(pd.Timestamp.max),
        "id": view[DEMAND_KEY]}, index=view.index)
    view = view.loc[order.sort_values(["m", "unscored", "score", "npv", "latest", "id"]).index]
    view["rank"] = range(1, len(view) + 1)
    return view.reset_index(drop=True)
