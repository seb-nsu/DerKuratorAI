"""Report exports and the data behind them: change set in business columns, the projects a report is about, their
dependency edges (projects outside the reader's scope as one neutral node), before/after deltas, the Markdown
simulation report and the changes CSV. The SteerCo Pack is in steerco_pack.py."""
from __future__ import annotations

import re
from collections import deque
from typing import Any

import pandas as pd

from app.core import clock
from app.core.formatting import eur, field_label, format_delta, format_value
from app.data_adapters.dataset import PortfolioDataset

CHANGE_COLUMNS = ["Project", "Change", "Current", "Proposed", "Δ", "Approval", "Note"]
RESTRICTED_LABEL = "Restricted project"  # projects outside the reader's scope: no id, no count, no name
DELTA_FIELDS: dict[str, tuple[str, str]] = {  # field -> (label, unit)
    "budget_cost_eur": ("Approved budget", "€"),
    "planned_capital_eur": ("CapEx plan", "€"),
    "planned_operating_eur": ("OpEx plan", "€"),
    "allocated_fte": ("Allocated FTE", "FTE"),
}
_GRAPH_HINTS = ("depend", "bottleneck", "downstream", "upstream", "domino", "blocks")
MAX_GRAPH_NODES = 40
RESTRICTED_NODE = RESTRICTED_LABEL  # all projects outside the RLS scope as one node: no id, no count


def change_rows(changes: list[dict], names: dict[str, str] | None = None) -> list[dict]:
    """A change set in business columns (screen, Markdown export, SteerCo Pack): project with name, readable field,
    current / proposed value, numeric delta and approval path – no table names, tools or run metadata."""
    from app.core.security import is_governed_change

    names = names or {}
    return [{"Project": f"{c.get('project_id')} – {names[str(c.get('project_id'))]}" if str(c.get("project_id")) in names
             else str(c.get("project_id")),
             "Change": field_label(str(c.get("field"))), "Current": format_value(str(c.get("field")), c.get("old")),
             "Proposed": format_value(str(c.get("field")), c.get("new")),
             "Δ": format_delta(str(c.get("field")), c.get("old"), c.get("new")),
             "Approval": "Four-eyes (PMO)" if is_governed_change(c) else "Direct",
             "Note": c.get("note") or ""} for c in changes or []]


def project_names(ds) -> dict[str, str]:
    """Project number -> project name of a dataset (empty without one)."""
    if ds is None or ds.projects is None or ds.projects.empty:
        return {}
    return dict(zip(ds.projects["project_number"].astype(str), ds.projects["project_name"].astype(str), strict=True))


def change_table(changes: list[dict[str, Any]], ds: PortfolioDataset | None) -> pd.DataFrame:
    """Change set as a business table with project name, numeric delta and the approval path per row."""
    return pd.DataFrame(change_rows(changes, project_names(ds)), columns=CHANGE_COLUMNS).astype(str)


# =========================================================================== report messages
def run_message(run: dict[str, Any]) -> dict[str, Any]:
    """A what-if run kept by the API as the report message the viewer and the exports read."""
    return {"role": "assistant", "content": run.get("paper") or "",
            "meta": {"request": run.get("request"), "mode": run.get("mode", "?"), "warnings": run.get("warnings") or [],
                     "plan": run.get("plan"), "proposed_changes": run.get("changes") or [],
                     "forced": bool(run.get("forced")), "verdict": run.get("verdict")}}


def proposal_message(proposal: dict) -> dict:
    """A stored proposal as the report message of the PMO inbox."""
    return {"content": proposal.get("paper", ""),
            "meta": {"request": proposal.get("request"), "mode": "proposal", "proposed_changes": proposal["changes"],
                     "forced": proposal.get("forced"), "proposal_id": proposal["id"],
                     # the stored verdict record: the inbox shows the same conditions, owners and due date as the console;
                     # an older proposal without a record still shows the verdict the inbox table shows
                     **({"verdict": proposal["verdict_record"]} if isinstance(proposal.get("verdict_record"), dict)
                        else {"verdict": {"label": proposal["verdict"]}} if proposal.get("verdict") else {})}}


# =========================================================================== focus
def focus_projects(meta: dict[str, Any], known: set[str], text: str = "") -> list[str]:
    """Projects a scenario is about: orchestrator plan, proposed changes and project numbers in the request/paper."""
    found: list[str] = []
    plan = meta.get("plan") or {}
    for sub in plan.get("sub_intents") or []:
        found += [str(p) for p in sub.get("project_ids") or []]
    found += [str(c.get("project_id")) for c in meta.get("proposed_changes") or []]
    haystack = f"{meta.get('request', '')}\n{text}"
    found += [pid for pid in sorted(known) if pid and re.search(rf"(?<![\w-]){re.escape(pid)}(?![\w-])", haystack)]
    return list(dict.fromkeys(p for p in found if p in known))


def wants_dependency_graph(meta: dict[str, Any], text: str = "") -> bool:
    """True if the Analyst (graph analysis) was involved or the paper reports dependencies."""
    plan = meta.get("plan") or {}
    if plan.get("requires_graph_analysis") or any(s.get("intent_type") == "graph_analysis" for s in plan.get("sub_intents") or []):
        return True
    return any(h in text.lower() for h in _GRAPH_HINTS)


def dependency_edges(ds: PortfolioDataset, focus: list[str], hops: int = 2) -> pd.DataFrame:
    """Dependency edges within `hops` of the focus projects (successor depends on predecessor)."""
    cols = ["successor", "predecessor", "type", "status"]
    deps = ds.dependencies
    if deps.empty or not focus or not {"successor_project_number", "predecessor_project_number"} <= set(deps.columns):
        return pd.DataFrame(columns=cols)
    edges = pd.DataFrame({
        "successor": deps["successor_project_number"].astype(str),
        "predecessor": deps["predecessor_project_number"].astype(str),
        "type": deps.get("dependency_type", pd.Series("", index=deps.index)).fillna("").astype(str),
        "status": deps.get("status", pd.Series("", index=deps.index)).fillna("").astype(str),
    })
    seen, queue = set(focus), deque((p, 0) for p in focus)
    while queue:
        node, depth = queue.popleft()
        if depth >= hops or len(seen) >= MAX_GRAPH_NODES:
            continue
        linked = edges[(edges["successor"] == node) | (edges["predecessor"] == node)]
        for other in pd.concat([linked["successor"], linked["predecessor"]]).unique():
            if other not in seen and len(seen) < MAX_GRAPH_NODES:
                seen.add(other)
                queue.append((other, depth + 1))
    sub = edges[edges["successor"].isin(seen) & edges["predecessor"].isin(seen)].copy()
    visible = set(ds.projects["project_number"].astype(str))
    hidden = ~sub["successor"].isin(visible) | ~sub["predecessor"].isin(visible)
    for col in ("successor", "predecessor"):  # a PM never learns which or how many projects lie outside the scope
        sub.loc[~sub[col].isin(visible), col] = RESTRICTED_NODE
    sub.loc[hidden, ["type", "status"]] = ""
    return sub.drop_duplicates().reset_index(drop=True)[cols]


def scenario_deltas(changes: list[dict[str, Any]]) -> pd.DataFrame:
    """Before/after values of the budget and FTE fields a scenario proposes to change."""
    rows = []
    for c in changes or []:
        field = str(c.get("field", ""))
        if field not in DELTA_FIELDS:
            continue
        before, after = pd.to_numeric(pd.Series([c.get("old"), c.get("new")]), errors="coerce").tolist()
        if pd.isna(after):
            continue
        label, unit = DELTA_FIELDS[field]
        before = 0.0 if pd.isna(before) else float(before)
        rows.append({"project": str(c.get("project_id")), "metric": label, "unit": unit,
                     "before": before, "after": float(after), "delta": float(after) - before})
    return pd.DataFrame(rows, columns=["project", "metric", "unit", "before", "after", "delta"])


def _unit_value(unit: str, value: float, signed: bool = False) -> str:
    if unit == "€":
        return eur(value, signed=signed)
    sign = "+" if signed and value > 0 else "−" if value < 0 else ""
    return f"{sign}{abs(value):,.1f} {unit}".strip()


# =========================================================================== export
def _md_cell(value: Any) -> str:
    return str("" if value is None else value).replace("|", "\\|").replace("\n", " ")


def md_table(df: pd.DataFrame) -> str:
    if df.empty:
        return "_none_"
    head = "| " + " | ".join(map(_md_cell, df.columns)) + " |"
    sep = "| " + " | ".join("---" for _ in df.columns) + " |"
    body = ["| " + " | ".join(_md_cell(v) for v in row) + " |" for row in df.itertuples(index=False)]
    return "\n".join([head, sep, *body])


def build_report_markdown(messages: list[dict[str, Any]], ds: PortfolioDataset, role: str, user_id: str,
                          environment: str) -> str:
    """Structured Markdown export of the Steering Console: conversation, proposed changes, budget/FTE deltas and
    dependency graphs (as edge tables)."""
    known = set(ds.project_numbers)
    out = [
        "# DerKurator AI – Simulation Report", "",
        f"- **Generated:** {clock.now():%Y-%m-%d %H:%M}",
        f"- **Environment:** {environment}",
        f"- **Exported by:** {user_id} ({role})",
        f"- **Projects in scope:** {len(known)}",
        "", "> Decision support only. Numbers come from deterministic simulation tools; items marked AI ESTIMATE or "
        "HEURISTIC PLACEHOLDER are indicative. Changes take effect only after Approve & Apply.", "",
    ]
    for i, msg in enumerate(messages, start=1):
        if msg.get("role") == "user":
            out += [f"## {i}. Request", "", str(msg.get("content", "")), ""]
            continue
        meta = msg.get("meta") or {}
        out += [f"## {i}. Decision paper", ""]
        status = ([" FORCED SIMULATION"] if meta.get("forced") else []) + (
            [f" applied {meta.get('applied_at')}"] if meta.get("applied") else [])
        if status:  # run mode and runtime are technical details – System Admin only (Admin Console → Logs)
            out += [f"_{' ·'.join(status).strip()}_", ""]
        out += [str(msg.get("content", "")), ""]
        for w in meta.get("warnings") or []:
            out.append(f"> Note: {w}")
        changes = meta.get("proposed_changes") or []
        if changes:
            names = project_names(ds)
            out += ["", "### Proposed changes", "",
                    md_table(pd.DataFrame(change_rows(changes, names), columns=CHANGE_COLUMNS))]
        deltas = scenario_deltas(changes)
        if not deltas.empty:
            shown = pd.DataFrame([{"Project": r.project, "Metric": r.metric, "Before": _unit_value(r.unit, r.before),
                                   "After": _unit_value(r.unit, r.after),
                                   "Δ": _unit_value(r.unit, r.delta, signed=True)} for r in deltas.itertuples()])
            out += ["", "### Before / after (chart data)", "", md_table(shown)]
        text = str(msg.get("content", ""))
        if wants_dependency_graph(meta, text):
            edges = dependency_edges(ds, focus_projects(meta, known, text))
            if not edges.empty:
                out += ["", "### Dependency graph (successor depends on predecessor)", "", md_table(edges)]
        out.append("")
    return "\n".join(out).strip() + "\n"


def _csv_safe(value: Any) -> Any:
    """Neutralises spreadsheet formula injection (=, +, -, @ at the start of text cells)."""
    return f"'{value}" if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r") else value


def changes_csv(messages: list[dict[str, Any]]) -> str:
    rows = [{k: _csv_safe(v) for k, v in {"message": i, "request": (m.get("meta") or {}).get("request", ""), **c}.items()}
            for i, m in enumerate(messages, start=1) for c in (m.get("meta") or {}).get("proposed_changes") or []]
    return pd.DataFrame(rows).to_csv(index=False) if rows else "message,request\n"
