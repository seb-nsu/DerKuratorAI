"""Decision insights (PMO, a sub-page of the overview): status check of the sources, the portfolio with
probabilities, the reference class of a project, the review of past decisions with the audit pack and the transfers to
ServiceNow. Same sections as the React page; every figure comes from /api/v1/insights."""
from __future__ import annotations

from nicegui import ui

from frontends.nicegui import api
from frontends.nicegui.components import card, notify, page_header, section
from frontends.nicegui.pages.overview import boundary, caption, info, metric
from frontends.nicegui.pages.overview_format import eur_m
from frontends.nicegui.pages.planning_parts import install, table


def _dash(value) -> str:
    return "–" if value in (None, "") else str(value)


def status_check(result: dict) -> None:
    caption(f"{result['mismatches']} project(s) reported better than their numbers · {result['stale']} without a "
            "current status report")
    table([{"Project": c["project_id"], "Name": c["project_name"], "Reported": _dash(c.get("reported")),
            "Calculated": c["calculated"], "Check": c["label"], "Why": "; ".join(c["reasons"]),
            "Source": _dash(c.get("source"))} for c in result["checks"]],
          scroll=True, caption="Status check of the sources")


def forecast(result: dict) -> None:
    summary = result["summary"]
    if not result["projects"]:
        info("No open project with a budget to simulate.")
        return
    with ui.element("div").classes("dk-grid-3 w-full"):
        for label, value in (("Within budget", f"{summary['probability_within_budget_pct']:g} %"),
                             (f"Cost at {summary['confidence_pct']:g} %", eur_m(summary["p_conf_cost_eur"])),
                             ("Reserve needed", eur_m(summary["reserve_needed_eur"]))):
            with card():
                metric(label, value)
    caption(f"{summary['headline']} {summary['runs']} runs, seed {summary['seed']}.")
    table([{"Project": p["project_id"], "Name": p["project_name"], "Budget": p["budget_eur"],
            "Within budget %": p["probability_within_budget_pct"], "On time %": p.get("probability_on_time_pct"),
            "Compared with": f"{p['reference_size']} ({p['reference_basis']})"} for p in result["projects"]],
          formats={"Budget": "eur", "Within budget %": "dec1", "On time %": "dec1"}, scroll=True,
          caption="Portfolio with probabilities")


def reference_text(result: dict) -> list[str]:
    lines = [result["headline"]] if result.get("headline") else []
    if "buffer_eur" in result:
        days = f", {result['buffer_days']} days" if "buffer_days" in result else ""
        lines.append(f"Budget at {result['confidence_pct']:g} % confidence: {eur_m(result['budget_at_confidence_eur'])} "
                     f"(buffer {eur_m(result['buffer_eur'])}{days}).")
    return lines or ["Too few comparable projects for a reference class."]


async def reference(projects: list[str]) -> None:
    if not projects:
        caption("No open project.")
        return

    @ui.refreshable
    async def answer(project_id: str) -> None:
        async with boundary():
            result = await api.get(f"/insights/reference/{project_id}")
            for line in reference_text(result):
                ui.label(line)

    choice = ui.select(projects, label="Project", value=projects[0]).props("outlined dense").classes("w-64")
    choice.on_value_change(lambda e: answer.refresh(e.value))
    await answer(choice.value)


def _held(value) -> str:
    return "–" if value is None else ("yes" if value else "no")


async def _download_pack(proposal_id: str | None) -> None:
    if not proposal_id:
        return
    path = f"/insights/audit-pack/{proposal_id}"
    await api._renew_if_due(path)  # the same token renewal as every other call
    headers = {"Authorization": f"Bearer {api.token()}"} if api.token() else {}
    async with api._client() as client:  # the answer is a ZIP, not JSON
        response = await client.get(path, headers=headers)
    if not response.is_success:
        try:
            message = response.json().get("detail")
        except ValueError:
            message = None
        notify("error", str(message or f"Request failed ({response.status_code})."))
        return
    disposition = response.headers.get("content-disposition", "")
    name = disposition.split('filename="', 1)[1].split('"', 1)[0] if 'filename="' in disposition else \
        f"audit_pack_{proposal_id}.zip"
    ui.download.content(response.content, name, "application/zip")


def decision_review(review: dict) -> None:
    caption(f"{review['due_for_review']} decision(s) due for review · {review['conditions_overdue']} condition(s) "
            "past their due date")
    if review["hit_rate"]:
        table([{"Verdict": h["verdict"], "Decisions": h["decisions"], "Held": h["held"],
                "Hit rate %": h["hit_rate_pct"]} for h in review["hit_rate"]],
              formats={"Hit rate %": "dec1"}, caption="Hit rate per verdict")
    if not review["decisions"]:
        info("No approved decision yet.")
        return
    table([{"Proposal": d["proposal_id"], "Decided": d["decided_on"], "Verdict": d["verdict"],
            "Review": d["review_stage"] or f"next {d['next_review']}", "Held": _held(d["held"]),
            "Outcome": "; ".join(o.get("text", "") for o in d["outcomes"])} for d in review["decisions"]],
          scroll=True, caption="Approved decisions")
    proposals = [d["proposal_id"] for d in review["decisions"]]
    with ui.row().classes("items-end gap-2 q-mt-sm"):
        choice = ui.select(proposals, label="Approved proposal", value=proposals[0]).props("outlined dense") \
            .classes("w-64")
        ui.button("Download audit pack", icon="inventory_2", on_click=lambda: _download_pack(choice.value)) \
            .props("outline no-caps")


def transfers(rows: list[dict]) -> None:
    if not rows:
        caption("No transfer to ServiceNow yet (write-back is off unless the mapping and the host switch it on).")
        return
    table([{"Proposal": r.get("proposal_id"), "Project": r.get("key"), "Field": r.get("field"),
            "Value": r.get("value"), "Status": r.get("status"), "Check": r.get("check"),
            "Note": r.get("reason") or ""} for r in rows], scroll=True, caption="Transfers to ServiceNow")


async def render(info_: dict, query: dict) -> None:
    install()  # the business table style of the planning pages
    ui.add_css(".dk-grid-3 { display: grid; gap: 16px; grid-template-columns: repeat(3, minmax(0, 1fr)); } "
               "@media (max-width: 700px) { .dk-grid-3 { grid-template-columns: 1fr; } }")
    page_header("Decision insights", "Independent of the source tools · deterministic, no AI",
                link=("overview", "arrow_back", "Back to the overview"))
    checks: dict | None = None
    section("Status check of the sources")
    async with boundary():
        checks = await api.get("/insights/status-check")
        status_check(checks)
    section("Portfolio with probabilities")
    async with boundary():
        forecast(await api.get("/insights/forecast"))
    section("Reference class of a project")
    async with boundary():
        await reference([c["project_id"] for c in (checks or {}).get("checks", [])])
    section("Review of past decisions")
    async with boundary():
        decision_review(await api.get("/insights/decision-review"))
    section("Transfers to ServiceNow")
    async with boundary():
        transfers(await api.get("/insights/servicenow-transfers"))
