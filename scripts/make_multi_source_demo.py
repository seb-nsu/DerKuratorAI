"""Writes the second source of the multi-source demo (data/mappings/multi_source_demo.yaml): the project list of a
business unit that keeps its portfolio in Excel, with German sheet and column names and amounts in TEUR.

Two of its projects are also in the ServiceNow demo export (same name and sponsor, different budget or end date), and
two are reported green although their numbers are red – the cases the duplicate check and the status check show.

    python scripts/make_multi_source_demo.py [data/mappings/bu2_portfolio.xlsx]
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DEFAULT = ROOT / "data" / "mappings" / "bu2_portfolio.xlsx"

COLUMNS = ("Nr", "Bezeichnung", "Projektleitung", "Auftraggeber", "Ampel", "Status", "Phase", "Methode",
           "Fortschritt %", "Start", "Ende genehmigt", "Ende geplant", "Budget (TEUR)", "Ist (TEUR)",
           "Prognose (TEUR)", "Plankosten (TEUR)", "CapEx (TEUR)", "OpEx (TEUR)", "Nutzen (TEUR)", "Business Case",
           "Portfolio", "Bereich")

ROWS = (
    ("01", "Warehouse Scanner Rollout", "Selin Kaya", "Johanna Reiter", "grün", "Work in Progress", "Executing",
     "Waterfall", 40, "2026-01-05", "2026-09-30", "2026-12-18", 300, 285, 410, 300, 180, 120, 450,
     "Replaces end-of-life scanners in four warehouses.", "Logistics", "Logistics"),
    ("02", "Route Optimisation Engine", "Mats Holm", "Johanna Reiter", "grün", "Work in Progress", "Executing",
     "Agile", 55, "2026-02-02", "2027-01-29", "2027-01-29", 520, 610, 780, 520, 260, 260, 900,
     "Cuts fuel cost by optimising delivery routes daily.", "Logistics", "Logistics"),
    ("03", "Yard Management System", "Selin Kaya", "Dario Conti", "gelb", "Work in Progress", "Executing",
     "Hybrid", 35, "2026-04-06", "2027-03-31", "2027-04-30", 640, 230, 670, 640, 400, 240, 800,
     "Shortens truck waiting times at the main hub.", "Logistics", "Logistics"),
    ("04", "Cold Chain Monitoring", "Aylin Yilmaz", "Dario Conti", "grün", "Work in Progress", "Executing",
     "Agile", 62, "2026-03-02", "2026-12-18", "2026-12-18", 280, 160, 275, 280, 120, 160, 350,
     "Sensor data for temperature-sensitive goods.", "Logistics", "Logistics"),
    ("05", "Carrier Portal", "Mats Holm", "Johanna Reiter", "rot", "Work in Progress", "Executing", "Agile", 28,
     "2026-05-04", "2027-02-26", "2027-05-28", 390, 210, 520, 390, 150, 240, 420,
     "Self-service booking for external carriers.", "Logistics", "Logistics"),
    ("06", "General Ledger Consolidation", "Pia Rahm", "Helena Brandt", "gelb", "Work in Progress", "Executing",
     "Waterfall", 64, "2026-02-02", "2027-03-17", "2027-03-31", 2100, 980, 2150, 2100, 1300, 800, 2600,
     "Logistics share of the group ERP harmonisation.", "Core Business Systems", "Logistics"),
    ("07", "Vendor Interface Exchange", "Tomek Wolski", "Ingrid Solberg", "grün", "Work in Progress", "Executing",
     "Agile", 34, "2026-08-03", "2027-06-11", "2027-09-30", 890, 165, 872.2, 890, 400, 490, 1200,
     "Logistics partners on the group API marketplace.", "Digital Channels", "Logistics"),
    ("08", "Dock Door Automation", "Aylin Yilmaz", "Dario Conti", "grün", "Work in Progress", "Planning",
     "Waterfall", 5, "2026-09-07", "2027-06-30", "2027-06-30", 450, 20, 450, 450, 300, 150, 520,
     "Automatic door assignment for inbound trucks.", "Logistics", "Logistics"),
    ("09", "Returns Processing App", "Selin Kaya", "Johanna Reiter", "gelb", "Work in Progress", "Executing", "Agile",
     48, "2026-03-16", "2026-11-27", "2027-01-15", 210, 130, 240, 210, 60, 150, 260,
     "Mobile app for faster returns handling.", "Logistics", "Logistics"),
    ("10", "Fleet Telematics Upgrade", "Mats Holm", "Dario Conti", "grün", "Closed", "Closed", "Waterfall", 100,
     "2025-06-02", "2026-03-31", "2026-05-29", 350, 418, 418, 350, 250, 100, 380,
     "New telematics units for the own fleet.", "Logistics", "Logistics"),
)


def write(path: Path = DEFAULT) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame(list(ROWS), columns=list(COLUMNS))
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        frame.to_excel(writer, sheet_name="Projekte", index=False)
    return path


if __name__ == "__main__":
    print(write(Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT))
