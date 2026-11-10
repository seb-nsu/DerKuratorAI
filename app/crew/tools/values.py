"""Value helpers shared by the simulation tools: tolerant number and date parsing, euro text, calendar constant."""
from __future__ import annotations

import math
from datetime import date
from typing import Any

import pandas as pd

DAYS_PER_MONTH = 30.44  # calendar constant, not a business assumption


def num(value: Any, default: float | None = None) -> float | None:
    """`value` as float; `default` for missing, NaN or non-numeric values."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return default if math.isnan(number) else number


def to_date(value: Any) -> date | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    ts = pd.to_datetime(value, errors="coerce")
    return None if pd.isna(ts) else ts.date()


def eur(value: float) -> str:
    return f"€{value:,.0f}" if value >= 0 else f"-€{abs(value):,.0f}"
