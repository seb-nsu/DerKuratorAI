"""One-off data migration for the PMO use cases (re-baselining, skills) – run once per data source, idempotent.

1. Baselines: the existing rows (name and date only) become version 1 with values: baseline budget = the project's
   approved budget, baseline end = its approved end date, reason "Baseline at project start", approved by the
   approver of the project mandate (Approvals, iteration 1) on the baseline's creation date.
2. Skills (synthetic): Team_Skills gives each of the six teams 2–3 skills whose FTE add up to the team's FTE in the
   reference month; Skill_Demand splits every resource-plan row's demand over the team's skills with a fixed,
   documented skew – so that SAP FI is over 110 % in Team Quartz in October 2026 while the team as a whole is below
   100 % (the case the skill view exists for).

Usage: python -m scripts.migrate_s12_data [path.json|path.xlsx ...] (default: both demo files in data/)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd

DATA = Path(__file__).resolve().parents[1] / "data"
DEFAULT_FILES = (DATA / "DerKurator_Demo_Portfolio.json", DATA / "DerKurator_Demo_Portfolio.xlsx")
REFERENCE_MONTH = "2026-10-01"

# team -> [(skill, share of the team's FTE, share of the team's demand)]
SKILLS: dict[str, list[tuple[str, float, float]]] = {
    "Team Orion": [("Cloud Platform", 1 / 3, 0.34), ("Cloud Security", 1 / 3, 0.36), ("DevOps", 1 / 3, 0.30)],
    "Team Harbor": [("Data Engineering", 0.5, 0.55), ("Analytics", 0.5, 0.45)],
    "Team Maple": [("Frontend", 0.375, 0.36), ("Backend", 0.375, 0.40), ("UX Design", 0.25, 0.24)],
    "Team Summit": [("SAP FI", 0.5, 0.55), ("SAP CO", 0.25, 0.22), ("Integration", 0.25, 0.23)],
    "Team Quartz": [("SAP FI", 1 / 3, 0.45), ("Integration", 1 / 3, 0.30), ("Testing", 1 / 3, 0.25)],
    "Team Willow": [("Cloud Security", 0.25, 0.27), ("Identity & Access", 5 / 12, 0.40), ("Network", 1 / 3, 0.33)],
}


def migrate_baselines(tables: dict[str, list[dict[str, Any]]]) -> int:
    projects = {p["project_number"]: p for p in tables.get("Projects", [])}
    mandate = {a["project_number"]: a.get("approver") for a in tables.get("Approvals", []) if a.get("iteration") in (1, "1")}
    changed = 0
    for row in tables.get("Baselines", []):
        if row.get("version") is not None:
            continue
        p = projects.get(row["project_number"], {})
        row.update(version=1, baseline_budget_eur=p.get("budget_cost_eur"),
                   baseline_end_date=p.get("approved_end_date") or p.get("planned_end_date"),
                   baseline_eac_eur=None, reason="Baseline at project start",
                   approved_by=mandate.get(row["project_number"]), approval_date=row.get("created_date"))
        changed += 1
    return changed


def build_skills(tables: dict[str, list[dict[str, Any]]]) -> tuple[list[dict], list[dict]]:
    plans = pd.DataFrame(tables["Resource_Plans"])
    plans["month"] = pd.to_datetime(plans["month"]).dt.strftime("%Y-%m-%d")
    team_fte = plans[plans["month"] == REFERENCE_MONTH].groupby("assignment_group")["allocated_fte"].sum()
    team_skills = []
    for team, skills in SKILLS.items():
        fte = float(team_fte.get(team, 0)) or 2.0
        for skill, share, _ in skills:
            team_skills.append({"assignment_group": team, "skill": skill, "fte": round(fte * share, 2)})
    demand, n = [], 0
    for row in plans.sort_values("resource_plan_number").to_dict(orient="records"):
        for skill, _, weight in SKILLS[row["assignment_group"]]:
            n += 1
            demand.append({"skill_demand_number": f"SKD-DEMO-{n:05d}", "project_number": row["project_number"],
                           "month": row["month"], "assignment_group": row["assignment_group"], "skill": skill,
                           "demand_hours": round(float(row["demand_hours"]) * weight, 1)})
    return team_skills, demand


def migrate_tables(tables: dict[str, list[dict[str, Any]]]) -> str:
    baselines = migrate_baselines(tables)
    team_skills, demand = build_skills(tables)
    tables["Team_Skills"], tables["Skill_Demand"] = team_skills, demand
    return f"{baselines} baseline(s) migrated, {len(team_skills)} team skills, {len(demand)} skill demand rows"


def migrate_json(path: Path) -> str:
    data = json.loads(path.read_text(encoding="utf-8"))
    note = migrate_tables(data)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return note


def migrate_excel(path: Path) -> str:
    """Only the Baselines sheet and the two skill sheets are touched (openpyxl keeps every other sheet as it is)."""
    from openpyxl import load_workbook

    sheets = pd.read_excel(path, sheet_name=None, dtype=object, engine="openpyxl")
    tables = {name: df.where(df.notna(), None).to_dict(orient="records") for name, df in sheets.items()}
    for rows in tables.values():  # dates as ISO text, as in the JSON
        for row in rows:
            for key, value in row.items():
                if isinstance(value, pd.Timestamp):
                    row[key] = value.date().isoformat()
    note = migrate_tables(tables)
    wb = load_workbook(path)
    for name in ("Baselines", "Team_Skills", "Skill_Demand"):
        if name in wb.sheetnames:
            del wb[name]
        ws = wb.create_sheet(name)
        rows = tables[name]
        header = list(dict.fromkeys(k for r in rows for k in r))
        ws.append(header)
        for r in rows:
            ws.append([r.get(k) for k in header])
    wb.save(path)
    return note


def main(paths: list[str]) -> None:
    for raw in paths or [str(p) for p in DEFAULT_FILES]:
        path = Path(raw)
        note = migrate_excel(path) if path.suffix.lower() in (".xlsx", ".xlsm") else migrate_json(path)
        print(f"{path.name}: {note}")


if __name__ == "__main__":
    main(sys.argv[1:])
