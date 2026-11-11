"""The report viewer's content, independent of any front end and served by the HTTP API:
one decision report (a Steering Console analysis or a proposal) as the viewer shows it – verdict, key figures, the
change count, the tabs Summary · Decision paper · Changes · Impact · Dependencies · Data quality and their charts as
Plotly figure JSON. Everything reads the caller's scope: projects outside it appear as one neutral node."""
from __future__ import annotations

import re
from typing import Any

import pandas as pd

from app.core.business_rules import RULE_LABELS
from app.core.formatting import field_label, format_value
from app.data_adapters.dataset import PortfolioDataset
from app.views.charts import delta_chart, dependency_graph_figure, figure_json
from app.views.exports import (
    change_table,
    dependency_edges,
    focus_projects,
    project_names,
    scenario_deltas,
    wants_dependency_graph,
)
from app.views.steerco_pack import metric_texts, pack_data

# Fields a budget change carries along (plan split, total cost, return, ROI): one decision, several fields
FOLLOW_ON_FIELDS = frozenset({"total_planned_cost_eur", "planned_capital_eur", "planned_operating_eur",
                              "planned_benefit_eur", "planned_roi_pct", "planned_return_eur"})
FULL_PAPER = "Full paper"
OFFLINE_NOTICE = "Offline mode"
DQ_COLUMNS = ["Severity", "Project", "Rule", "Field", "Finding"]
CHANGES_HELP = ("Changes counted as decisions: a budget cut that also moves CapEx, OpEx and total cost counts once. "
                "Governed changes (budget, schedule, state, phase …) need the approval of a PMO other than the "
                "requester (four-eyes principle).")
DEPENDENCY_LABELS = {"successor": "Project (waits)", "predecessor": "Depends on", "type": "Type", "status": "Status"}


def decision_count(changes: list[dict]) -> int:
    """Changes as decisions, not fields: a budget cut that also moves CapEx, OpEx and total cost counts once."""
    per_project: dict[str, int] = {}
    for c in changes or []:
        pid = str(c.get("project_id"))
        per_project[pid] = per_project.get(pid, 0) + (0 if c.get("field") in FOLLOW_ON_FIELDS else 1)
    return sum(max(1, n) for n in per_project.values())


def business_change_rows(changes: list[dict]) -> list[dict[str, str]]:
    """A change set as business rows (Project · Change · Current · Proposed · Note) – no table / tool internals."""
    return [{"Project": str(c.get("project_id")), "Change": field_label(str(c.get("field"))),
             "Current": format_value(str(c.get("field")), c.get("old")),
             "Proposed": format_value(str(c.get("field")), c.get("new")), "Note": str(c.get("note") or "")}
            for c in changes or []]


def paper_sections(paper: str) -> list[tuple[str, str]]:
    """(title, markdown) per '## ' section of a decision paper; the preamble (title, request line) is dropped from
    the section list because the report header already shows it. A paper without sections is one 'Paper' section."""
    parts = re.split(r"(?m)^##\s+", paper or "")
    sections = []
    for part in parts[1:]:
        title, _, body = part.partition("\n")
        sections.append((title.strip(), body.strip()))
    return sections or [("Paper", (paper or "").strip())]


def requested_projects(meta: dict[str, Any]) -> list[str]:
    """Projects the request itself names or changes (plan + change set) – not every project the paper mentions."""
    found = [str(p) for sub in (meta.get("plan") or {}).get("sub_intents") or [] for p in sub.get("project_ids") or []]
    found += [str(c.get("project_id")) for c in meta.get("proposed_changes") or []]
    return list(dict.fromkeys(found))


def report_findings(meta: dict[str, Any], focus: list[str]) -> pd.DataFrame:
    """Data Steward findings for the projects of this report only (not the whole portfolio). New reports store them
    structured (`dq_findings`); older ones are read from the stored Data Steward table."""
    rows = meta.get("dq_findings")
    if rows is None:
        rows = []
        for line in str(meta.get("steward_md") or "").splitlines():
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) == 5 and cells[0] in ("ERROR", "WARNING"):
                rows.append(dict(zip(DQ_COLUMNS, cells, strict=True)))
    wanted = set(focus)
    return pd.DataFrame([r for r in rows if str(r.get("Project")) in wanted], columns=DQ_COLUMNS).astype(str)


def rule_rows(rules: dict[str, Any]) -> pd.DataFrame:
    """The business rules stored with a verdict as (Rule, Value) rows."""
    rows = []
    for name, value in (rules or {}).items():
        label, unit, _ = RULE_LABELS.get(name, (field_label(name), "", ""))
        shown = f"{value:,.0f}" if isinstance(value, (int, float)) and abs(value) >= 1000 else f"{value:g}" \
            if isinstance(value, (int, float)) else str(value)
        rows.append({"Rule": label, "Value": f"{shown} {unit}".strip()})
    return pd.DataFrame(rows, columns=["Rule", "Value"])


def _records(frame: pd.DataFrame) -> list[dict[str, str]]:
    return [{str(k): str(v) for k, v in row.items()} for row in frame.to_dict(orient="records")]


def _dependencies(ds: PortfolioDataset, edges: pd.DataFrame, focus: list[str]) -> dict[str, Any]:
    """The Dependencies tab: the direct links of the report's projects by default, all levels on request."""
    direct = edges[edges["successor"].isin(focus) | edges["predecessor"].isin(focus)]
    names = project_names(ds)

    def table(frame: pd.DataFrame) -> list[dict[str, str]]:
        shown = frame.copy()
        for col in ("successor", "predecessor"):
            shown[col] = shown[col].map(lambda p: f"{p} – {names[p]}" if p in names else p)
        return _records(shown.rename(columns=DEPENDENCY_LABELS))

    default = edges if direct.empty else direct
    has_all = len(direct) < len(edges) and not direct.empty
    return {
        "count": len(edges), "direct_count": len(default),
        "caption": f"Around {', '.join(focus[:3])}{' …' if len(focus) > 3 else ''} · arrow = depends on · red, solid = "
                   "at-risk dependency · bold border = project in this report · shape = RAG (● Green, ▲ Yellow, "
                   "■ Red)",
        "toggle_label": f"Show all levels ({len(edges)} links)" if has_all else None,
        "direct": {"figure": figure_json(dependency_graph_figure(ds, default, focus)), "rows": table(default)},
        "all": {"figure": figure_json(dependency_graph_figure(ds, edges, focus)), "rows": table(edges)}
        if has_all else None,
    }


def _summary(data: dict[str, Any], meta: dict[str, Any]) -> dict[str, Any]:
    warnings = [str(w) for w in meta.get("warnings") or []]
    # the offline mode is how the demo runs, not a problem: a neutral note at the end instead of a warning box
    offline = [w for w in warnings if w.startswith(OFFLINE_NOTICE)]
    steps = [w for w in warnings if w.startswith("Step ") and " of a what-if" in w]  # context, no problem
    verdict = meta.get("verdict") if isinstance(meta.get("verdict"), dict) else {}
    rules = verdict.get("rules")
    return {"warnings": [w for w in warnings if w not in offline and w not in steps], "steps": steps,
            "forced": bool(meta.get("forced")), "offline": bool(offline), "as_of": verdict.get("as_of"),
            "reasons": list(data.get("reasons") or []), "conditions": list(data["conditions"]),
            "flags": list(data["flags"]), "rules": _records(rule_rows(rules)) if rules else []}


def report_view(message: dict[str, Any], ds: PortfolioDataset | None, *, requester: str = "",
                environment: str = "") -> dict[str, Any]:
    """Everything the report viewer shows for one report message (an analysis or a proposal as message)."""
    from app.core.security import is_governed_change
    from app.crew.verdict import lead_reason

    meta = message.get("meta") or {}
    paper = str(message.get("content") or "")
    data = pack_data(message, requester=requester, environment=environment)
    changes = meta.get("proposed_changes") or []
    known = set(ds.project_numbers) if ds is not None else set()
    focus = focus_projects(meta, known, paper) if ds is not None else []
    # Dependencies only when the request is about them (stop, domino, bottleneck ...), as the paper then reports them
    show_deps = ds is not None and bool(focus) and wants_dependency_graph(meta, paper)
    edges = dependency_edges(ds, focus) if show_deps else pd.DataFrame()
    findings = report_findings(meta, requested_projects(meta))
    changed = list(dict.fromkeys(str(c.get("project_id")) for c in changes))
    referenced = [p for p in focus if p not in changed]
    governed = sum(1 for c in changes if is_governed_change(c))
    deltas = scenario_deltas(changes)
    impact = [figure_json(fig) for fig in (delta_chart(deltas, unit) for unit in ("€", "FTE")) if fig is not None] \
        if not deltas.empty else []

    tabs = [{"id": "summary", "label": "Summary"}, {"id": "paper", "label": "Decision paper"},
            {"id": "changes", "label": "Changes"}, {"id": "impact", "label": "Impact"}]
    if not edges.empty:
        tabs.append({"id": "dependencies", "label": f"Dependencies ({len(edges)})"})
    if not findings.empty:
        tabs.append({"id": "data_quality", "label": f"Data quality ({len(findings)})"})
    sections = paper_sections(paper)
    return {
        "title": meta.get("request") or data["title"],
        "verdict": {"label": data["verdict"], "key": data["verdict_key"], "lead": lead_reason(data.get("reasons"))},
        # only the key figures this report changes; an empty "–" card says nothing
        "metrics": [{"label": label, "value": value, "detail": detail}
                    for label, value, detail in metric_texts(data["metrics"]) if value != "–"],
        "changes": {"decisions": decision_count(changes), "fields": len(changes), "projects": len(changed),
                    "governed": governed, "referenced": len(referenced), "help": CHANGES_HELP,
                    "caption": f"{len(changes)} field(s) in {len(changed)} project(s) · {governed} governed"
                               + (f" · {len(referenced)} referenced" if referenced else "")},
        "summary": _summary(data, meta),
        "tabs": tabs,
        "paper": paper,
        "paper_sections": [{"title": t, "body": b} for t, b in sections] if len(sections) > 1 else [],
        "change_table": _records(change_table(changes, ds)) if changes else [],
        "impact": {"figures": impact, "caption": "Before vs. after per project" if impact else
                   "No budget or FTE shift in this report (e.g. schedule, stop or analysis scenario)."},
        "dependencies": _dependencies(ds, edges, focus) if not edges.empty else None,
        "data_quality": {"caption": "Data Steward findings for the projects this request names or changes, as of the "
                                    "analysis. The portfolio-wide view is on the Data Quality page.",
                         "rows": _records(findings)} if not findings.empty else None,
    }
