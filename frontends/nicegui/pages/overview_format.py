"""Number formats of the Overview and Data quality pages – the same styles as the Streamlit pages ("€9.92 M",
"€420,000", "85 %"). Display only: every figure itself comes from the API."""
from __future__ import annotations


def eur(value: float | None) -> str:
    if value is None:
        return "–"
    return f"{'−' if value < 0 else ''}€{abs(value):,.0f}"


def eur_m(value: float | None, digits: int = 2) -> str:
    if value is None:
        return "–"
    return f"{'−' if value < 0 else ''}€{abs(value) / 1e6:,.{digits}f} M"


def pct(value: float | None, digits: int = 0) -> str:
    return "–" if value is None else f"{value:,.{digits}f} %"


def short(value: float) -> str:
    """A limit as the business rules write it: 100, 110.5."""
    return f"{value:g}"
