"""Writes the synthetic planning demo (demands for 2027-2029, assessment, budget envelopes, capacity limits) into the
demo portfolio – deterministic and idempotent, like scripts/build_demo_portfolio.py.

    python scripts/build_planning_demo.py           # rewrites the planning tables of the demo JSON and workbook
    python scripts/build_planning_demo.py --check   # exit code 1 if they differ from what this script builds

The planning tables are replaced as a whole; every other table stays untouched.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
JSON_PATH = ROOT / "data" / "DerKurator_Demo_Portfolio.json"
XLSX_PATH = ROOT / "data" / "DerKurator_Demo_Portfolio.xlsx"

CRITERIA = (  # criterion, weight, direction
    ("Strategic contribution", 30, "higher is better"),
    ("Economic value", 25, "higher is better"),
    ("Risk of not acting", 20, "higher is better"),
    ("Delivery risk", 15, "lower is better"),
    ("Urgency", 10, "higher is better"),
)
GOALS = (("G1", "Seamless customer journeys", 30), ("G2", "Lean operations", 25),
         ("G3", "Resilience and regulation", 25), ("G4", "Insight-led steering", 20))

# number, name, owner, business unit, portfolio, type, mandatory reason, start (year, quarter), months,
# capex, opex, {team: fte}, npv, scores (in CRITERIA order) or None, goal, depends_on, run cost p.a., size,
# confidence, status, latest start, assessed on
DEMANDS: tuple[tuple[Any, ...], ...] = (
    ("DMD-2027-001", "Liquidity Planning Tool", "Priya Raman", "Shared Services", "Core Business Systems",
     "New", None, (2027, 1), 9, 600_000, 150_000, {"Team Orion": 3}, 900_000, (5, 4, 3, 2, 4), "G2", None, 60_000,
     "M", "Medium", "Assessed", "2028-01-01", "2026-09-15"),
    ("DMD-2027-002", "Digital Tax Reporting", "Priya Raman", "Shared Services", "Core Business Systems",
     "Mandatory", "Regulation (EU e-invoicing)", (2027, 1), 12, 450_000, 200_000, {"Team Orion": 2, "Team Maple": 1},
     150_000, None, "G3", None, 40_000, "M", "Fine", "Submitted", "2027-07-01", None),
    ("DMD-2027-003", "Customer App Relaunch", "Tomasz Wolski", "Retail Customers", "Digital Channels",
     "Extension", None, (2027, 2), 12, 900_000, 300_000, {"Team Harbor": 4}, 1_400_000, (4, 5, 3, 3, 3), "G1",
     "PRJ-DEMO-1003", 120_000, "L", "Medium", "Assessed", "2028-06-30", "2026-09-15"),
    ("DMD-2027-004", "Privileged Access Vault", "Kenji Watanabe", "IT Infrastructure", "Security & Resilience",
     "Mandatory", "Security (audit finding)", (2027, 1), 9, 500_000, 150_000, {"Team Willow": 3}, 0, None, "G3", None,
     50_000, "M", "Fine", "Submitted", "2027-04-01", None),
    ("DMD-2027-005", "Data Catalogue Foundation", "Yusuf Demir", "Field Services", "Data & Insights", "New", None,
     (2027, 2), 12, 800_000, 250_000, {"Team Summit": 4}, 700_000, (5, 3, 2, 4, 2), "G4", None, 150_000, "L", "Medium",
     "Assessed", "2028-06-30", "2026-09-15"),
    ("DMD-2027-006", "Equipment Health Monitoring", "Yusuf Demir", "Field Services", "Data & Insights", "New",
     None, (2028, 2), 9, 400_000, 100_000, {"Team Summit": 2}, 600_000, (3, 4, 2, 3, 2), "G4", "DMD-2027-005", 30_000,
     "M", "Medium", "Assessed", "2029-03-31", "2026-09-15"),
    ("DMD-2027-007", "Dynamic Tariff Engine", "Rowan Ellis", "Commercial Management", "Commerce & Pricing",
     "Replacement", None, (2027, 1), 12, 700_000, 200_000, {"Team Quartz": 3}, 1_100_000, (4, 4, 4, 3, 3), "G2", None,
     80_000, "L", "Fine", "Assessed", "2027-10-01", "2026-09-15"),
    ("DMD-2027-008", "Vendor Self-Registration", "Tomasz Wolski", "Vendor Network", "Commerce & Pricing",
     "New", None, (2027, 2), 6, 250_000, 80_000, {"Team Quartz": 2}, 400_000, (3, 3, 2, 2, 3), "G2", None, 20_000, "S",
     "Medium", "Assessed", "2028-06-30", "2026-09-15"),
    ("DMD-2027-009", "Learning Management Platform", "Lukas Berger", "Shared Services", "Workplace Services", "Replacement",
     None, (2027, 2), 12, 650_000, 250_000, {"Team Maple": 3}, 500_000, (3, 3, 3, 3, 2), "G2", None, 90_000, "L",
     "Rough", "Assessed", "2028-06-30", "2026-09-15"),
    ("DMD-2027-010", "Unified Messaging Rollout", "Lukas Berger", "Shared Services", "Workplace Services",
     "Extension", None, (2027, 3), 6, 300_000, 150_000, {"Team Maple": 2}, 250_000, (2, 3, 2, 2, 2), "G2", None,
     70_000, "M", "Medium", "Assessed", "2028-09-30", "2026-09-15"),
    ("DMD-2027-011", "Procurement Workflow Automation", "Priya Raman", "Shared Services", "Core Business Systems",
     "New", None, (2027, 2), 9, 380_000, 120_000, {"Team Orion": 2}, 350_000, (2, 3, 3, 3, 2), "G2", None, 30_000, "M",
     "Medium", "Assessed", "2027-06-30", "2026-09-15"),
    ("DMD-2027-012", "Customer Churn Insights", "Tomasz Wolski", "Retail Customers", "Data & Insights", "New", None,
     (2028, 1), 12, 600_000, 200_000, {"Team Summit": 3, "Team Harbor": 1}, 800_000, (4, 4, 2, 3, 3), "G1", None,
     100_000, "L", "Medium", "Assessed", "2029-03-31", "2026-09-15"),
    ("DMD-2027-013", "Threat Detection Automation", "Kenji Watanabe", "IT Infrastructure", "Security & Resilience", "New", None,
     (2027, 3), 9, 350_000, 200_000, {"Team Willow": 2}, 300_000, (4, 2, 4, 2, 3), "G3", None, 60_000, "M", "Medium",
     "Assessed", "2028-06-30", "2025-08-01"),
    ("DMD-2027-014", "Technician Mobile Toolkit", "Rowan Ellis", "Field Services", "Digital Channels", "New", None,
     (2028, 1), 9, 450_000, 120_000, {"Team Harbor": 2}, 500_000, (3, 3, 2, 3, 2), "G1", None, 40_000, "M", "Medium",
     "Assessed", "2029-06-30", "2026-09-15"),
    ("DMD-2027-015", "Depot Automation Pilot", "Yusuf Demir", "Field Services", "Digital Channels", "New", None,
     (2027, 1), 6, 500_000, 50_000, {"Team Summit": 2}, 200_000, (2, 2, 1, 4, 1), "G2", None, 30_000, "M", "Medium",
     "Assessed", "2027-06-30", "2026-09-15"),
    ("DMD-2027-016", "Revenue Accounting Upgrade", "Rowan Ellis", "Commercial Management", "Core Business Systems",
     "Mandatory", "Regulation (IFRS 15 audit)", (2027, 3), 6, 300_000, 100_000, {"Team Orion": 1, "Team Quartz": 1}, 0,
     None, "G3", None, 20_000, "S", "Fine", "Submitted", "2028-01-01", None),
    ("DMD-2027-017", "Policy Search Assistant", "Kenji Watanabe", "IT Infrastructure", "Digital Channels", "New",
     None, (2027, 2), 9, 400_000, 150_000, {"Team Willow": 2}, 450_000, None, "G2", None, 50_000, "M", "Rough",
     "Submitted", "2028-06-30", None),
    ("DMD-2027-018", "Employee Portal Refresh", "Lukas Berger", "Shared Services", "Workplace Services", "Extension",
     None, (2027, 3), 6, 200_000, 60_000, {"Team Maple": 1}, 100_000, None, "G2", None, 10_000, "S", "Rough", "Draft",
     "2028-06-30", None),
    ("DMD-2027-019", "Legacy Print Server Migration", "Lukas Berger", "Shared Services", "Workplace Services", "Replacement",
     None, (2027, 1), 3, 80_000, 20_000, {"Team Maple": 1}, 0, None, "G2", None, 5_000, "XS", "Rough", "Rejected",
     "2027-06-30", None),
)

# year, scope type, scope, capex cap, opex cap, reserve %, cap type
ENVELOPES: tuple[tuple[Any, ...], ...] = tuple(
    (year, "business_unit", unit, capex, opex, 10, "hard")
    for year in (2027, 2028, 2029)
    for unit, capex, opex in (("Shared Services", 1_700_000, 750_000), ("Retail Customers", 1_100_000, 500_000),
                              ("Field Services", 1_100_000, 450_000), ("Commercial Management", 1_100_000, 400_000),
                              ("Vendor Network", 400_000, 300_000), ("IT Infrastructure", 900_000, 450_000))
) + tuple((year, "portfolio", "Security & Resilience", 900_000, 450_000, 0, "hard") for year in (2027, 2028, 2029)) \
  + tuple((year, "portfolio", "Data & Insights", 900_000, 400_000, 0, "soft") for year in (2027, 2028, 2029))

SPONSORS = {"Shared Services": "Helena Brandt", "Retail Customers": "Marco Bianchi", "Field Services": "Clara Dubois",
            "Commercial Management": "Marco Bianchi", "Vendor Network": "Ingrid Solberg",
            "IT Infrastructure": "Fatima Okafor"}
CAPACITY = {"Team Orion": 8, "Team Harbor": 8, "Team Maple": 8, "Team Summit": 8, "Team Quartz": 8, "Team Willow": 8}
MAX_UTILIZATION = 85
PLANNING_TABLES = ("Strategic_Goals", "Scoring_Model", "Demands", "Demand_Year_Plans", "Demand_Resource_Plans",
                   "Demand_Scores", "Budget_Envelopes", "Capacity_Limits")


def _quarters(start: tuple[int, int], months: int) -> list[tuple[int, int]]:
    year, quarter = start
    out = []
    for _ in range(math.ceil(months / 3)):
        out.append((year, quarter))
        year, quarter = (year + 1, 1) if quarter == 4 else (year, quarter + 1)
    return out


def planning_tables() -> dict[str, list[dict[str, Any]]]:
    goals = [{"goal_id": g, "goal_name": n, "weight": w, "valid_from_year": 2026, "valid_to_year": 2030}
             for g, n, w in GOALS]
    model = [{"criterion": c, "weight": w, "scale_min": 1, "scale_max": 5, "direction": d} for c, w, d in CRITERIA]
    demands, years, resources, scores = [], [], [], []
    for (number, name, owner, unit, portfolio, kind, reason, start, months, capex, opex, teams, npv, score, goal,
         depends, run_cost, size, confidence, status, latest, assessed_on) in DEMANDS:
        quarters = _quarters(start, months)
        benefit = round(npv * 1.6) if npv else 0
        demands.append({
            "demand_number": number, "demand_name": name, "requester": unit,
            "project_sponsor": SPONSORS[unit], "demand_owner": owner, "business_unit": unit, "portfolio": portfolio,
            "business_service": name, "business_capability": portfolio, "execution_type": "Agile" if months <= 9 else "Hybrid",
            "priority": 1 if kind == "Mandatory" else 2, "tshirt_size": size,
            "project_summary": f"{name}: initiative of {unit}.",
            "business_case": f"{name} – {'mandatory: ' + reason if reason else 'value case approved by the business'}.",
            "planned_benefit_eur": benefit, "planned_return_eur": benefit - capex - opex,
            "planned_roi_pct": round((benefit - capex - opex) / (capex + opex) * 100, 1),
            "discount_rate_pct": 7.5, "net_present_value_eur": npv, "demand_type": kind, "mandatory_reason": reason,
            "strategic_goal": goal, "earliest_start_date": f"{start[0]}-{3 * start[1] - 2:02d}-01",
            "latest_start_date": latest, "duration_months": months, "depends_on": depends,
            "run_cost_after_go_live_eur": run_cost, "demand_status": status, "converted_project_number": None,
        })
        for year, quarter in quarters:
            years.append({"demand_number": number, "year": year, "quarter": quarter,
                          "capex_eur": round(capex / len(quarters), 2), "opex_eur": round(opex / len(quarters), 2),
                          "benefit_eur": 0, "confidence": confidence})
            for team, fte in teams.items():
                resources.append({"demand_number": number, "year": year, "quarter": quarter, "assignment_group": team,
                                  "skill": None, "fte": fte})
        if score:
            for i, (criterion, _, _) in enumerate(CRITERIA):
                scores.append({"score_number": f"SC-{number[-3:]}-{i + 1}", "demand_number": number,
                               "criterion": criterion, "score": score[i], "assessed_by": "PMO-ADMIN",
                               "assessed_on": assessed_on, "comment": None})
    envelopes = [{"year": y, "quarter": None, "scope_type": t, "scope": s, "capex_cap_eur": c, "opex_cap_eur": o,
                  "reserve_pct": r, "cap_type": k, "approved_by": "Portfolio Board", "approved_on": "2026-06-30"}
                 for y, t, s, c, o, r, k in ENVELOPES]
    capacity = [{"year": y, "quarter": None, "assignment_group": team, "skill": None, "available_fte": fte,
                 "max_utilization_pct": MAX_UTILIZATION} for y in (2027, 2028, 2029) for team, fte in CAPACITY.items()]
    return {"Strategic_Goals": goals, "Scoring_Model": model, "Demands": demands, "Demand_Year_Plans": years,
            "Demand_Resource_Plans": resources, "Demand_Scores": scores, "Budget_Envelopes": envelopes,
            "Capacity_Limits": capacity}


def write_xlsx(tables: dict[str, list[dict[str, Any]]], path: Path) -> None:
    """Replaces the planning sheets of the workbook (other sheets stay untouched)."""
    from openpyxl import load_workbook

    wb = load_workbook(path)
    for name, rows in tables.items():
        if name in wb.sheetnames:
            del wb[name]
        ws = wb.create_sheet(name)
        header = list(rows[0]) if rows else []
        ws.append(header)
        for row in rows:
            ws.append([row.get(c) for c in header])
    wb.save(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="only check, write nothing")
    args = parser.parse_args(argv)
    data = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    tables = planning_tables()
    if all(data.get(name) == rows for name, rows in tables.items()):
        print("Planning demo is up to date.")
        return 0
    if args.check:
        print("Planning demo differs – run scripts/build_planning_demo.py.")
        return 1
    data.update(tables)
    JSON_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    if XLSX_PATH.exists():
        write_xlsx(tables, XLSX_PATH)
    print(f"Wrote the planning tables into {JSON_PATH.name} and {XLSX_PATH.name}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
