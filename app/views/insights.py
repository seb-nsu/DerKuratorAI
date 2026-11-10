"""Decision insights page (PMO): the independent view on the portfolio that no single source tool gives – status
check of the sources, the portfolio with probabilities, the reference class of a project, the review of past
decisions with its hit rate, the audit pack of an approved decision and the transfer of approved changes back to
ServiceNow. One set of functions for the Streamlit page and the HTTP API; deterministic, no LLM."""
from __future__ import annotations

from datetime import date
from typing import Any

from app.core import clock
from app.core.proposals import APPROVED, get_proposal, list_proposals
from app.core.security import ROLE_ADMIN, ROLE_PMO
from app.crew.decision_review import review_decisions
from app.crew.tools.forecast_ranges import project_reference, simulate_portfolio
from app.crew.tools.status_check import check_dataset

PAGE_TITLE = "Decision insights"


def status_check(dataset: Any, today: date | None = None) -> dict[str, Any]:
    """Every open project with its reported and calculated status, mismatches first."""
    checks = [c.to_dict() for c in check_dataset(dataset, today or clock.today())]
    return {"checks": checks, "mismatches": sum(c["mismatch"] for c in checks),
            "stale": sum(c["stale"] for c in checks)}


def portfolio_forecast(dataset: Any, today: date | None = None) -> dict[str, Any]:
    return simulate_portfolio(dataset, today or clock.today())


def reference(dataset: Any, project_id: str) -> dict[str, Any]:
    """Raises KeyError for a project not in the dataset."""
    return project_reference(dataset, project_id)


def decision_review(dataset: Any, today: date | None = None) -> dict[str, Any]:
    """Approved proposals of the dataset's projects only (a proposal naming a project outside it is left out)."""
    visible = set(dataset.projects["project_number"].astype(str)) if not dataset.projects.empty else set()
    proposals = [p for p in list_proposals(status=APPROVED) if set(map(str, p.get("projects") or [])) <= visible]
    return review_decisions(proposals, dataset.projects, today or clock.today())


def audit_pack(proposal_id: str, user: str, role: str, dataset: Any) -> tuple[str, bytes]:
    from app.approval_flow import audit
    from app.crew.audit_pack import build_audit_pack

    name, data = build_audit_pack(proposal_id, user, role, dataset)
    audit("audit_pack_built", role=role, user_id=user, proposal_id=proposal_id, size=len(data))
    return name, data


def servicenow_transfer(proposal_id: str, user: str, role: str) -> list[dict[str, Any]]:
    """Writes an approved proposal's changes back to ServiceNow (when switched on). Raises PermissionError,
    KeyError or DataSourceError."""
    from app.approval_flow import audit
    from app.core.config import get_settings
    from app.data_adapters import _mapping_path
    from app.data_adapters.mapping import load_mapping
    from app.data_adapters.servicenow_writeback import write_back

    if role not in (ROLE_PMO, ROLE_ADMIN):
        raise PermissionError("Only the PMO can transfer approved changes to ServiceNow.")
    proposal = get_proposal(proposal_id)
    if proposal is None or proposal.get("status") != APPROVED:
        raise KeyError(proposal_id)
    path = _mapping_path(get_settings())
    if path is None:
        from app.data_adapters import DataSourceError

        raise DataSourceError("The configured data source has no ServiceNow source.")
    results = write_back(proposal["id"], proposal["changes"], load_mapping(path), user)
    counts: dict[str, int] = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    audit("servicenow_write_back", role=role, user_id=user, proposal_id=proposal["id"], results=counts)
    return results


def servicenow_transfers() -> list[dict[str, Any]]:
    """The transfer status of every write-back attempt (empty without a ServiceNow source)."""
    from app.data_adapters import servicenow_connectors
    from app.data_adapters.servicenow_writeback import transfer_status

    try:
        return [r for c in servicenow_connectors() for r in transfer_status(c)]
    except Exception:  # noqa: BLE001 - a broken mapping shows on the data pages, not here
        return []
