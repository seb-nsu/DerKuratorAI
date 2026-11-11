"""SteerCo Pack: a one-page, print-ready executive summary of a decision paper.

Contents at a glance: verdict, three key metrics (budget Δ, FTE Δ, ROI Δ – computed from the change set, never
from LLM text), conditions / next steps, flags (forced simulation, AI estimates, internal errors) and the change set.
`pack_data` is pure (testable); `render_pack_html` returns a self-contained HTML page (inline CSS, no scripts, print
styles for A4) – "Print → Save as PDF" in any browser produces the PDF. All dynamic text is HTML-escaped.
"""
from __future__ import annotations

import html
import math
import re
from typing import Any

from app.core import clock
from app.core.formatting import eur, timestamp
from app.crew.shadow import extract_verdict
from app.crew.verdict import condition_owner_text
from app.views.exports import CHANGE_COLUMNS, change_rows

VERDICT_STYLES = {  # key -> (label colour, background, colour name)
    "GO": ("#0b6b0b", "#e3f4e3", "green"),
    "CONDITIONAL": ("#8a5a00", "#fff3d6", "orange"),
    "NO-GO": ("#9b1c1c", "#fde4e4", "red"),
    "INCOMPLETE": ("#44475a", "#ececf1", "gray"),
    "NONE": ("#44475a", "#ececf1", "gray"),
}
_ITEM_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+(.*)$")


def verdict_key(verdict: str | None) -> str:
    v = (verdict or "").upper()
    if "INCOMPLETE" in v:
        return "INCOMPLETE"
    if "NO-GO" in v or "NO GO" in v:
        return "NO-GO"
    if "CONDITIONAL" in v:
        return "CONDITIONAL"
    if re.search(r"\bGO\b", v):
        return "GO"
    return "NONE"


def _num(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(number) else number


def _field_deltas(changes: list[dict[str, Any]], field: str) -> dict[str, tuple[float, float]]:
    """Per project: (first old, last new) of a field – chained tool changes collapse to the net effect."""
    out: dict[str, tuple[float, float]] = {}
    for c in changes or []:
        if c.get("field") != field:
            continue
        old, new = _num(c.get("old")), _num(c.get("new"))
        if new is None:
            continue
        pid = str(c.get("project_id"))
        first = out[pid][0] if pid in out else (old if old is not None else 0.0)
        out[pid] = (first, new)
    return out


def key_metrics(changes: list[dict[str, Any]]) -> dict[str, Any]:
    budget = _field_deltas(changes, "budget_cost_eur")
    fte = _field_deltas(changes, "allocated_fte")
    roi = _field_deltas(changes, "planned_roi_pct")
    budget_d = {p: n - o for p, (o, n) in budget.items()}
    fte_d = {p: n - o for p, (o, n) in fte.items()}
    roi_d = {p: n - o for p, (o, n) in roi.items()}
    return {
        "budget_net": sum(budget_d.values()), "budget_moved": sum(d for d in budget_d.values() if d > 0),
        "budget_projects": budget_d,
        "fte_net": sum(fte_d.values()), "fte_moved": sum(d for d in fte_d.values() if d > 0), "fte_projects": fte_d,
        "roi": {p: {"old": o, "new": n, "delta_pp": n - o} for p, (o, n) in roi.items()},
        "has_budget": bool(budget_d), "has_fte": bool(fte_d), "has_roi": bool(roi_d),
    }


def extract_conditions(paper: str, max_items: int = 6) -> list[str]:
    """Bullet/numbered items of the 'Recommendation & Conditions' / 'Next Steps' sections of the paper."""
    items: list[str] = []
    for section in re.split(r"(?m)^#{2,3}\s+", paper or "")[1:]:
        title, _, body = section.partition("\n")
        if not re.search(r"recommendation|condition|next steps", title, re.IGNORECASE):
            continue
        for line in body.splitlines():
            match = _ITEM_RE.match(line)
            if match:
                text = re.sub(r"\*\*(.+?)\*\*", r"\1", match.group(1)).strip()
                if text and text not in items:
                    items.append(text)
    return items[:max_items]


def _proposal_flag(proposal_id: str, fallback: str | None) -> str:
    """The proposal's current state (the stored proposal, not the state at the time the report was written)."""
    from app.core.proposals import APPROVED, REJECTED, STALE, get_proposal

    proposal = get_proposal(proposal_id) or {}
    status = proposal.get("status") or fallback
    decided = timestamp(proposal.get("decided_at")) if proposal.get("decided_at") else ""
    if status == APPROVED:
        return f"Approved by the PMO{f' on {decided}' if decided else ''} (proposal {proposal_id})."
    if status == REJECTED:
        return f"Rejected by the PMO{f' on {decided}' if decided else ''} (proposal {proposal_id})."
    if status == STALE:
        return f"Proposal {proposal_id} is outdated – the data changed since the simulation."
    return f"Submitted for PMO approval (proposal {proposal_id})."


def pack_data(message: dict[str, Any], requester: str = "", environment: str = "") -> dict[str, Any]:
    meta = message.get("meta") or {}
    paper = str(message.get("content") or "")
    changes = meta.get("proposed_changes") or []
    engine = meta.get("verdict") if isinstance(meta.get("verdict"), dict) else None  # rule-based verdict
    verdict = (engine or {}).get("label") or extract_verdict(paper) or "No recommendation stated"
    details = (engine or {}).get("condition_details") or []
    # conditions come only from the verdict engine (its record, else the "- Condition:" lines it wrote into the
    # paper) – never from next steps or process boilerplate, so the inbox and the console show the same list
    conditions = ([f"{d['condition']} ({condition_owner_text(d)})" for d in details]
                  or list((engine or {}).get("conditions") or [])
                  or list(dict.fromkeys(re.findall(r"(?m)^\s*-\s*Condition:\s*(.+?)\s*$", paper))))
    flags = []
    if meta.get("forced"):
        flags.append("Forced simulation – the PMO overrode the data-quality gate; results for blocked projects are indicative.")
    if "[AI ESTIMATE]" in paper:
        flags.append("Contains AI estimates (qualitative, not calculated).")
    if "[HEURISTIC PLACEHOLDER" in paper:
        flags.append("Contains a heuristic placeholder (no AI, not calculated).")
    if "INTERNAL ERROR" in paper:
        flags.append("Analysis incomplete – a system error prevented part of the calculation.")
    if meta.get("applied"):
        flags.append(f"Applied to the data source on {meta.get('applied_at')}.")
    elif meta.get("handed_over"):
        flags.append(f"Approved on {meta.get('applied_at')} for manual execution – no data was written.")
    elif meta.get("proposal_id"):
        flags.append(_proposal_flag(meta["proposal_id"], meta.get("proposal_status")))
    return {
        "title": meta.get("request") or "Steering scenario", "verdict": verdict, "verdict_key": verdict_key(verdict),
        "metrics": key_metrics(changes), "conditions": conditions, "flags": flags,
        # stored proposals carry only the verdict label, so fall back to the rule-based reasons written into the paper
        "reasons": list((engine or {}).get("reasons") or []) or re.findall(r"(?m)^\s*-\s*Reason:\s*(.+?)\s*$", paper),
        "changes": changes, "requester": requester, "environment": environment, "mode": meta.get("mode", "?"),
        "generated": clock.now().strftime("%Y-%m-%d %H:%M"),
    }


def metric_texts(metrics: dict[str, Any]) -> list[tuple[str, str, str]]:
    """(label, value, detail) for the three key metric cards."""
    budget = (eur(metrics["budget_net"], signed=True), f"{eur(metrics['budget_moved'])} moved between projects"
              if metrics["budget_moved"] and abs(metrics["budget_net"]) < 1 else
              f"{len(metrics['budget_projects'])} project(s)") if metrics["has_budget"] else ("–", "no budget change")
    fte = (f"{metrics['fte_net']:+.1f} FTE", f"{metrics['fte_moved']:.1f} FTE moved" if metrics["fte_moved"] else
           f"{len(metrics['fte_projects'])} project(s)") if metrics["has_fte"] else ("–", "no capacity change")
    if metrics["has_roi"]:
        pid, roi = next(iter(metrics["roi"].items()))
        more = f" (+{len(metrics['roi']) - 1} more)" if len(metrics["roi"]) > 1 else ""
        roi_text = (f"{roi['delta_pp']:+.1f} pp", f"{pid}: {roi['old']:.1f} % → {roi['new']:.1f} %{more}")
    else:
        roi_text = ("–", "no ROI change")
    return [("Budget Δ", *budget), ("FTE Δ", *fte), ("ROI Δ", *roi_text)]


def render_pack_html(data: dict[str, Any]) -> str:
    e = html.escape
    fg, bg, _ = VERDICT_STYLES[data["verdict_key"]]
    cards = "".join(f'<div class="card"><div class="label">{e(label)}</div><div class="value">{e(value)}</div>'
                    f'<div class="detail">{e(detail)}</div></div>' for label, value, detail in metric_texts(data["metrics"]))
    conditions = "".join(f"<li>{e(c)}</li>" for c in data["conditions"]) or "<li>No conditions stated.</li>"
    reasons = "".join(f"<li>{e(r)}</li>" for r in data.get("reasons") or [])
    flags = "".join(f"<li>{e(f)}</li>" for f in data["flags"])
    rows = "".join(
        "<tr>" + "".join(f"<td>{e(str(r[k]) if r[k] not in (None, '') else '–')}</td>" for k in CHANGE_COLUMNS) + "</tr>"
        for r in change_rows(data["changes"])) or f'<tr><td colspan="{len(CHANGE_COLUMNS)}">No data changes proposed.</td></tr>'
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>SteerCo Pack – {e(data['title'])}</title>
<style>
  @page {{ size: A4; margin: 16mm; }}
  body {{ font-family: "Segoe UI", Arial, sans-serif; color: #1f1f1d; margin: 0; padding: 24px; background: #fff; }}
  header {{ display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; border-bottom: 2px solid #1f1f1d; padding-bottom: 12px; }}
  h1 {{ font-size: 20px; margin: 0 0 4px; }} .sub {{ color: #5c5c58; font-size: 12px; }}
  .verdict {{ color: {fg}; background: {bg}; border: 2px solid {fg}; border-radius: 8px; padding: 10px 16px; font-weight: 700; font-size: 18px; white-space: nowrap; }}
  .cards {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; margin: 18px 0; }}
  .card {{ border: 1px solid #d9d9d6; border-radius: 8px; padding: 12px; }}
  .label {{ font-size: 12px; color: #5c5c58; text-transform: uppercase; letter-spacing: .04em; }}
  .value {{ font-size: 24px; font-weight: 700; margin: 4px 0; }} .detail {{ font-size: 12px; color: #5c5c58; }}
  h2 {{ font-size: 14px; margin: 18px 0 6px; text-transform: uppercase; letter-spacing: .04em; }}
  ul {{ margin: 0; padding-left: 18px; }} li {{ margin: 3px 0; font-size: 13px; }}
  .flags li {{ color: #8a5a00; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
  th, td {{ border-bottom: 1px solid #e4e4e1; padding: 5px 6px; text-align: left; }} th {{ background: #f4f4f2; }}
  footer {{ margin-top: 20px; font-size: 11px; color: #5c5c58; border-top: 1px solid #d9d9d6; padding-top: 8px; }}
  @media print {{ body {{ padding: 0; }} .cards, table, ul {{ break-inside: avoid; }} }}
</style></head>
<body>
<header><div><h1>{e(data['title'])}</h1>
<div class="sub">SteerCo Pack · generated {e(data['generated'])} · environment {e(data['environment'] or '–')} · prepared by {e(data['requester'] or '–')}</div></div>
<div class="verdict">{e(data['verdict'])}</div></header>
<section class="cards">{cards}</section>
{f'<h2>Why this verdict (rule-based)</h2><ul>{reasons}</ul>' if reasons else ''}
<h2>Conditions &amp; next steps</h2><ul>{conditions}</ul>
{f'<h2>Flags</h2><ul class="flags">{flags}</ul>' if flags else ''}
<h2>Change set</h2>
<table><thead><tr>{''.join(f'<th>{e(c)}</th>' for c in CHANGE_COLUMNS)}</tr></thead><tbody>{rows}</tbody></table>
<footer>DerKurator AI – decision support. Figures are calculated by deterministic tools; items marked AI estimate are
indicative. Changes take effect only after approval (four-eyes principle).</footer>
</body></html>"""
