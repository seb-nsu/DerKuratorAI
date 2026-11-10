"""Writes the Excel template of the canonical portfolio schema (one sheet per table plus a field description).

Usage: python scripts/make_excel_template.py [target.xlsx]   (default: data/Portfolio_Template.xlsx)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.data_adapters.template import write_template

if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data/Portfolio_Template.xlsx")
    print(f"Template written: {write_template(target)}")
