"""Makes the synthetic demo portfolio internally consistent – the only way the demo data is changed.

    python scripts/build_demo_portfolio.py           # rewrites data/DerKurator_Demo_Portfolio.json/.xlsx
    python scripts/build_demo_portfolio.py --check   # exit code 1 if the files are not consistent yet

Rules (deterministic, idempotent – a second run changes nothing):
1. Dependencies: the "On Track" dependencies get a required-by date after the predecessor's planned end and before
   the successor's end (DEPENDENCY_DATES); the "At Risk" ones stay late on purpose.
2. EAC is never below the actual cost: such a project gets EAC = actual cost + ETC.
3. NPV = present value of the benefit tranches (Benefit_Plans, the project's discount rate, valued at
   NPV_VALUATION_DATE) minus the total planned cost – instead of planned_return / 1.075.
4. Story points vary per project (1–13, seeded by the project number) and their done share follows the project's
   percent_complete, so velocity forecasts differ between projects.
The intentional data-quality cases (Data_Quality_Cases) are kept: missing fields, past-due project, actual cost above
budget, missing business case and sponsor, resource overload.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
JSON_PATH = ROOT / "data" / "DerKurator_Demo_Portfolio.json"
XLSX_PATH = ROOT / "data" / "DerKurator_Demo_Portfolio.xlsx"

NPV_VALUATION_DATE = date(2026, 10, 1)
DEPENDENCY_DATES = {
    "DEP-DEMO-0002": "2027-04-15",  # Ledger ends 2027-03-31, Tariff Invoicing ends 2027-04-30
    "DEP-DEMO-0003": "2026-12-15",  # Tracking Hub (ended 2026-08-31, past due) -> Vendor Exchange ends 2027-06-25
    "DEP-DEMO-0006": "2027-05-31",  # Tariff Invoicing ends 2027-04-30, Vendor Exchange ends 2027-06-25
}
POINTS = (1, 2, 3, 5, 8, 13)
DONE_STATES = {"Complete"}


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def fix_dependencies(data: dict[str, Any]) -> None:
    for dep in data["Dependencies"]:
        if dep["dependency_number"] in DEPENDENCY_DATES:
            dep["required_by_date"] = DEPENDENCY_DATES[dep["dependency_number"]]


def fix_eac(data: dict[str, Any]) -> None:
    for p in data["Projects"]:
        eac, actual, etc = (_num(p.get(c)) for c in ("estimate_at_completion_eur", "actual_cost_eur",
                                                     "estimate_to_completion_eur"))
        if eac is not None and actual is not None and eac < actual:
            p["estimate_at_completion_eur"] = round(actual + (etc or 0.0), 2)


def npv(project: dict[str, Any], tranches: list[dict[str, Any]]) -> float | None:
    rate = (_num(project.get("discount_rate_pct")) or 0.0) / 100
    cost = _num(project.get("total_planned_cost_eur"))
    if cost is None:
        return None
    pv = 0.0
    for t in tranches:
        amount, when = _num(t.get("planned_benefit_eur")), _date(t.get("realization_start_date"))
        if amount and when:
            years = max((when - NPV_VALUATION_DATE).days / 365.25, 0.0)
            pv += amount / (1 + rate) ** years
    return round(pv - cost, 2) if pv else None


def fix_npv(data: dict[str, Any]) -> None:
    for p in data["Projects"]:
        tranches = [b for b in data["Benefit_Plans"] if b["project_number"] == p["project_number"]]
        value = npv(p, tranches)
        if value is not None:
            p["net_present_value_eur"] = value


def _done_share(index: list[int], done: list[int]) -> float:
    total = sum(POINTS[k] for k in index)
    return sum(POINTS[index[i]] for i in done) / total if total else 0.0


def fix_story_points(data: dict[str, Any]) -> None:
    for p in data["Projects"]:
        stories = [s for s in data["Stories"] if s["project_number"] == p["project_number"]]
        if not stories:
            continue
        rng = random.Random(p["project_number"])  # noqa: S311 - reproducible demo data, not a secret
        index = [rng.randrange(len(POINTS)) for _ in stories]
        target = (_num(p.get("percent_complete")) or 0.0) / 100
        done = [i for i, s in enumerate(stories) if s["state"] in DONE_STATES]
        open_ = [i for i, s in enumerate(stories) if s["state"] not in DONE_STATES]
        for _ in range(500):  # nudge one story a step at a time until the done share matches the progress
            gap = _done_share(index, done) - target
            if abs(gap) < 0.02 or not done or not open_:
                break
            up, down = (open_, done) if gap > 0 else (done, open_)
            raisable = [i for i in up if index[i] < len(POINTS) - 1]
            lowerable = [i for i in down if index[i] > 0]
            if raisable:
                index[rng.choice(raisable)] += 1
            elif lowerable:
                index[rng.choice(lowerable)] -= 1
            else:
                break
        for i, s in enumerate(stories):
            s["story_points"] = POINTS[index[i]]


def build(data: dict[str, Any]) -> dict[str, Any]:
    out = json.loads(json.dumps(data))
    fix_dependencies(out)
    fix_eac(out)
    fix_npv(out)
    fix_story_points(out)
    return out


def write_xlsx(data: dict[str, Any], path: Path) -> None:
    """Writes the changed tables into the existing workbook (header row decides the column; other sheets and the
    overview formulas stay untouched)."""
    from openpyxl import load_workbook

    wb = load_workbook(path)
    for sheet, key in (("Projects", "project_number"), ("Dependencies", "dependency_number"),
                       ("Stories", "story_number")):
        ws = wb[sheet]
        header = {str(c.value): c.column for c in ws[1] if c.value is not None}
        rows = {str(r[key]): r for r in data[sheet]}
        for row in ws.iter_rows(min_row=2):
            record = rows.get(str(row[header[key] - 1].value))
            if record is None:
                continue
            for field, value in record.items():
                if field in header:
                    cell = ws.cell(row=row[0].row, column=header[field])
                    if field.endswith("_date") and isinstance(cell.value, date) and isinstance(value, str) and value:
                        cell.value = date.fromisoformat(value[:10])
                    else:
                        cell.value = value
    wb.save(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="only check, write nothing")
    args = parser.parse_args(argv)
    data = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    built = build(data)
    if built == data:
        print("Demo portfolio is consistent.")
        return 0
    if args.check:
        print("Demo portfolio is not consistent – run scripts/build_demo_portfolio.py.")
        return 1
    JSON_PATH.write_text(json.dumps(built, ensure_ascii=False, indent=2), encoding="utf-8")
    if XLSX_PATH.exists():
        write_xlsx(built, XLSX_PATH)
    print(f"Rewrote {JSON_PATH.name} and {XLSX_PATH.name}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
