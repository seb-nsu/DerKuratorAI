"""Board pack of the annual plan: an Excel workbook (verdict, ranking, decisions, budget, capacity, multi-year view,
scenario comparison, for a value-optimal plan the comparison with the rank order) and a print-ready HTML page (“Print → Save as PDF”). Everything comes from the planning tables –
no LLM text."""
from __future__ import annotations

import html
import io
from dataclasses import replace

import pandas as pd

from app.core import clock
from app.data_adapters.dataset import DEMAND_KEY, PortfolioDataset
from app.planning import store
from app.planning.scenario import KINDS, OUT, RANK, VALUE, ScenarioResult, compare, eur, run_scenario
from app.planning.scoring import ranking


def multi_year(result: ScenarioResult) -> pd.DataFrame:
    """Planned CapEx and OpEx per business unit and year (demands in the plan)."""
    items = result.items
    if items.empty:
        return pd.DataFrame()
    placed = items[items["decision"] != OUT]
    cols = [f"{label} {y}" for y in result.years for _, label in KINDS]
    frame = placed.groupby("business_unit")[cols].sum().reset_index().rename(columns={"business_unit": "Business unit"})
    total = {"Business unit": "Total", **{c: frame[c].sum() for c in cols}}
    return pd.concat([frame, pd.DataFrame([total])], ignore_index=True)


def summary_rows(result: ScenarioResult) -> list[tuple[str, str]]:
    v = result.verdict
    rows = [("Scenario", result.input.name), ("Planning years", f"{result.years[0]}–{result.years[-1]}"),
            ("Input", store.spec_text(result.input)), ("Verdict (rule-based)", v.label),
            ("Reference date", result.as_of.isoformat()), ("Created", clock.now().strftime("%Y-%m-%d %H:%M"))]
    rows += [("Reason", r) for r in v.reasons]
    rows += [("Condition", f"{c['condition']} (owner {c['owner_role']}, due {c['due']})") for c in v.condition_details()]
    return rows


def _ranking_frame(ds: PortfolioDataset) -> pd.DataFrame:
    r = ranking(ds)
    if r.empty:
        return r
    return r[["rank", DEMAND_KEY, "demand_name", "business_unit", "portfolio", "demand_type", "demand_status",
              "score", "net_present_value_eur"]].rename(columns=lambda c: c.replace("_", " ").capitalize())


def optimizer_comparison(ds: PortfolioDataset, result: ScenarioResult) -> tuple[list[str], pd.DataFrame,
                                                                                  pd.DataFrame] | None:
    """For a value-optimal plan: what it does differently from the rank order (None for a rank-order plan)."""
    if result.input.method != VALUE:
        return None
    return compare(run_scenario(ds, replace(result.input, method=RANK), result.as_of), result)


def board_pack_xlsx(ds: PortfolioDataset, result: ScenarioResult, compared: list[ScenarioResult] | None = None) -> bytes:
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        pd.DataFrame(summary_rows(result), columns=["Item", "Value"]).to_excel(writer, sheet_name="Summary", index=False)
        _ranking_frame(ds).to_excel(writer, sheet_name="Ranking", index=False)
        result.items.to_excel(writer, sheet_name="Decisions", index=False)
        result.budget.to_excel(writer, sheet_name="Budget", index=False)
        result.capacity.to_excel(writer, sheet_name="Capacity", index=False)
        multi_year(result).to_excel(writer, sheet_name="Multi-year", index=False)
        if compared and len(compared) > 1:
            store.comparison(compared).to_excel(writer, sheet_name="Comparison", index=False)
        if optimised := optimizer_comparison(ds, result):
            summary, figures, changes = optimised
            figures.to_excel(writer, sheet_name="Optimizer", index=False)
            pd.DataFrame({"Summary": summary}).to_excel(writer, sheet_name="Optimizer", index=False,
                                                        startrow=len(figures) + 2)
            changes.to_excel(writer, sheet_name="Optimizer", index=False, startrow=len(figures) + len(summary) + 4)
        for sheet in writer.sheets.values():
            for column in sheet.columns:
                width = max(len(str(c.value or "")) for c in column[:200])
                sheet.column_dimensions[column[0].column_letter].width = min(max(10, width + 2), 70)
    return buffer.getvalue()


def _table_html(frame: pd.DataFrame, money: tuple[str, ...] = ()) -> str:
    if frame.empty:
        return "<p class='muted'>–</p>"
    head = "".join(f"<th>{html.escape(str(c))}</th>" for c in frame.columns)
    body = []
    for row in frame.itertuples(index=False):
        cells = []
        for col, value in zip(frame.columns, row, strict=True):
            text = "" if value is None or (isinstance(value, float) and pd.isna(value)) else (
                eur(float(value)) if col in money or str(col).startswith(("CapEx", "OpEx")) else str(value))
            cells.append(f"<td>{html.escape(text)}</td>")
        body.append("<tr>" + "".join(cells) + "</tr>")
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"


def board_pack_html(ds: PortfolioDataset, result: ScenarioResult) -> str:
    """Self-contained print page (inline CSS, no scripts, A4 landscape)."""
    v = result.verdict
    colour = {"GO": "#0b6b0b", "CONDITIONAL GO": "#8a5a00", "NO-GO": "#9b1c1c"}.get(v.decision, "#44475a")
    items = result.items
    decisions = items[[DEMAND_KEY, "demand_name", "business_unit", "decision", "start", "reason"]].rename(columns={
        DEMAND_KEY: "Demand", "demand_name": "Name", "business_unit": "Business unit", "decision": "Decision",
        "start": "Start", "reason": "Reason"}) if not items.empty else items
    tight = result.budget[result.budget["Used %"].fillna(0) >= 90] if not result.budget.empty else result.budget
    tight = tight[["Scope type", "Scope", "Year", "Kind", "Limit", "Committed", "Planned", "Free", "Used %"]] \
        if not tight.empty else tight
    conditions = "".join(f"<li>{html.escape(c['condition'])} <span class='muted'>(owner {c['owner_role']}, due "
                         f"{c['due']})</span></li>" for c in v.condition_details())
    reasons = "".join(f"<li>{html.escape(r)}</li>" for r in v.reasons)
    optimiser = ""
    if optimised := optimizer_comparison(ds, result):
        summary, _, changes = optimised
        cols = [c for c in ("Demand", "Name", "Rank order", "Value-optimal", "Why (value-optimal)") if c in changes]
        optimiser = ("<h2>Value-optimal choice compared with the rank order</h2><ul>"
                     + "".join(f"<li>{html.escape(s)}</li>" for s in summary) + "</ul>"
                     + (_table_html(changes[cols]) if not changes.empty else ""))
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Board pack {html.escape(result.input.name)}</title>
<style>
@page {{ size: A4 landscape; margin: 14mm; }}
body {{ font-family: system-ui, sans-serif; color: #1f2330; font-size: 11px; }}
h1 {{ font-size: 18px; margin: 0 0 4px; }} h2 {{ font-size: 13px; margin: 16px 0 6px; }}
.verdict {{ display: inline-block; padding: 4px 10px; border-radius: 6px; color: #fff; background: {colour};
  font-weight: 600; }}
.muted {{ color: #6b7080; }}
table {{ border-collapse: collapse; width: 100%; }} th, td {{ border-bottom: 1px solid #e3e5ec; padding: 3px 6px;
  text-align: left; vertical-align: top; }} th {{ background: #f4f5f8; }}
</style></head><body>
<h1>Annual plan {result.years[0]}–{result.years[-1]} · {html.escape(result.input.name)}</h1>
<p><span class="verdict">{html.escape(v.label)}</span> <span class="muted">rule-based verdict · reference date
{result.as_of.isoformat()} · {html.escape(store.spec_text(result.input))}</span></p>
<ul>{reasons}{conditions}</ul>
<h2>Decision per demand</h2>{_table_html(decisions)}{optimiser}
<h2>Multi-year view (demands in the plan)</h2>{_table_html(multi_year(result))}
<h2>Tight or exceeded envelopes</h2>{_table_html(tight, ("Limit", "Committed", "Planned", "Free"))}
<p class="muted">DerKurator AI · created {clock.now().strftime("%Y-%m-%d %H:%M")} · all figures from the planning
engine, no AI-generated numbers.</p>
</body></html>"""
