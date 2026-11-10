"""Admin Console – operations: FinOps (LLM costs, Admin only), notifications, ServiceNow sync, write journal, database
and import report, snapshots, demo reset, purge of a user's data and the in-app system tests. System Admin only;
destructive actions need confirm = true and are audited as in the Streamlit console."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from app.api import admin_schemas as schemas
from app.api.admin_common import Confirmation, renamed, require_confirm, respond
from app.api.deps import admin
from app.core import journal
from app.core.auth import User
from app.core.config import get_settings
from app.core.snapshots import MAX_SNAPSHOTS
from app.data_adapters import DataSourceError
from app.views import admin as views

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(admin)])
logger = logging.getLogger("derkurator.api.admin")


# =========================================================================== FinOps
@router.get("/finops")
def finops() -> schemas.FinOpsView:
    return schemas.FinOpsView.model_validate(views.finops())


@router.post("/finops/reset")
def reset_finops(body: Confirmation, response: Response, user: User = Depends(admin)) -> schemas.ActionResult:
    require_confirm(body)
    return respond(views.reset_costs(user.user_id), response)


# =========================================================================== notifications
class NotificationTestRequest(BaseModel):
    to: str = Field("", max_length=200)  # empty: the role mailboxes; Teams webhooks are always tested


@router.get("/notifications")
def notifications() -> schemas.NotificationsView:
    return schemas.NotificationsView.model_validate(views.notification_status())


@router.post("/notifications/test")
def test_notification(body: NotificationTestRequest, response: Response,
                      user: User = Depends(admin)) -> schemas.NotificationTest:
    return respond(views.send_test_notification(body.to, user.user_id), response, schemas.NotificationTest)


# =========================================================================== ServiceNow and write journal
class SyncRequest(Confirmation):
    full: bool = False  # a full sync reads every table completely: needs confirm = true


@router.get("/servicenow")
def servicenow() -> schemas.ServiceNowView:
    try:
        rows = views.servicenow_rows()
    except DataSourceError as exc:
        return schemas.ServiceNowView(configured=True, error=str(exc), tables=[])
    return schemas.ServiceNowView(configured=rows is not None, error=None, tables=renamed(rows or [], {
        "Instance": "instance", "Table": "table", "Rows": "rows", "Changed up to": "changed_up_to",
        "Last sync": "last_sync"}))


@router.post("/servicenow/sync")
def sync_servicenow(body: SyncRequest, response: Response, user: User = Depends(admin)) -> schemas.ActionResult:
    if body.full:
        require_confirm(body)
    return respond(views.run_servicenow_sync(body.full, user.user_id, user.role), response, data_changed=True)


class Resolution(BaseModel):
    operation_id: str = Field(min_length=1, max_length=100)
    kind: str = Field(min_length=1, max_length=100)
    comment: str = Field(min_length=1, max_length=2000)


@router.get("/journal")
def write_journal() -> schemas.JournalView:
    return schemas.JournalView(findings=[{**f, "label": journal.KINDS.get(f["kind"], f["kind"])}
                                         for f in views.journal_findings()])


@router.post("/journal/resolve")
def resolve_journal_finding(body: Resolution, response: Response, user: User = Depends(admin)) -> schemas.ActionResult:
    outcome = views.resolve_journal_finding(body.operation_id, body.kind, user.user_id, user.role, body.comment)
    if outcome.data.get("not_found"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, outcome.message)
    return respond(outcome, response)


# =========================================================================== database
@router.get("/database")
def database() -> schemas.DatabaseView:
    from app.graph.portfolio_graph import current_metadata

    try:
        report, report_error = views.import_report(), None
    except DataSourceError as exc:
        report, report_error = None, str(exc)
    return schemas.DatabaseView(
        data_source=get_settings().data_source, graph=current_metadata(), import_report_error=report_error,
        import_report=None if not report else schemas.ImportReport(
            mapping=report.get("mapping"), findings=len(report.get("findings", [])),
            tables=renamed(views.import_report_rows(report), {"Table": "table", "Source": "source", "Read": "read",
                                                              "Loaded": "loaded", "Skipped": "skipped",
                                                              "Notes": "notes"}),
            notes=[schemas.ImportNote(table=table, note=note) for table, t in report["tables"].items()
                   for note in t["warnings"]]))


@router.post("/database/rebuild-graph")
def rebuild_graph(response: Response, user: User = Depends(admin)) -> schemas.GraphRebuilt:
    logger.info("API | Graph rebuild requested | role=admin")
    return respond(views.rebuild_graph(user.user_id, get_settings().data_source), response, schemas.GraphRebuilt,
                   data_changed=True)


# =========================================================================== snapshots and data lifecycle
class PurgeRequest(Confirmation):
    user_id: str = Field(min_length=1, max_length=200)


@router.get("/snapshots")
def snapshots() -> schemas.SnapshotsView:
    source = views.source_path()
    return schemas.SnapshotsView(
        source=source.name if source else None, max_snapshots=MAX_SNAPSHOTS,
        demo_reset_available=views.demo_reset_available(),
        snapshots=[schemas.SnapshotRow(id=s["id"], created_at=s.get("created_at"), reason=s.get("reason"),
                                       user=s.get("user"), environment=s.get("environment"),
                                       size_kb=round((s.get("size") or 0) / 1024, 1),
                                       changes=[schemas.SnapshotChange(**{
                                           k: c.get(k) for k in ("project_id", "table", "field", "old", "new",
                                                                 "source_tool")}) for c in s.get("changes") or []])
                   for s in views.snapshots_of_source()])


@router.post("/snapshots", status_code=status.HTTP_201_CREATED)
def create_snapshot(response: Response, user: User = Depends(admin)) -> schemas.SnapshotCreated:
    return respond(views.take_snapshot(user.user_id), response, schemas.SnapshotCreated)


@router.post("/snapshots/{snapshot_id}/restore")
def restore_snapshot(snapshot_id: str, body: Confirmation, response: Response,
                     user: User = Depends(admin)) -> schemas.SnapshotRestored:
    require_confirm(body)
    if snapshot_id not in {s["id"] for s in views.snapshots_of_source()}:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Snapshot not found.")
    return respond(views.restore(snapshot_id, user.user_id, user.role), response, schemas.SnapshotRestored,
                   data_changed=True)


@router.post("/demo-reset")
def demo_reset(body: Confirmation, response: Response, user: User = Depends(admin)) -> schemas.DemoReset:
    require_confirm(body)
    if not views.demo_reset_available():
        raise HTTPException(status.HTTP_409_CONFLICT, "Reset demo data is not available in this environment.")
    return respond(views.reset_demo(user.user_id), response, schemas.DemoReset, data_changed=True)


@router.post("/purge-user")
def purge_user(body: PurgeRequest, response: Response, user: User = Depends(admin)) -> schemas.UserPurged:
    require_confirm(body)
    return respond(views.purge(body.user_id, user.user_id, user.role), response, schemas.UserPurged)


# =========================================================================== system tests
@router.get("/system-tests")
def system_tests() -> schemas.SystemTestsView:
    """Results of the last saved run (JUnit file) and whether runs are allowed on this instance."""
    import xml.etree.ElementTree as ET
    from datetime import UTC, datetime

    from app.core import logs
    from app.core.system_tests import junit_path, parse_junit

    last = report_saved_at = None
    try:
        counts, cases = parse_junit(junit_path())
        stamp = datetime.fromtimestamp(junit_path().stat().st_mtime, tz=UTC).isoformat()
        last = schemas.SavedTestRun(counts=counts, cases=cases, saved_at=stamp)
    except (FileNotFoundError, OSError, ET.ParseError):
        pass
    try:
        report_saved_at = datetime.fromtimestamp(logs.TEST_REPORT.stat().st_mtime, tz=UTC).isoformat()
    except OSError:
        pass
    return schemas.SystemTestsView(enabled=not get_settings().force_offline_mode, last_run=last,
                                   report_saved_at=report_saved_at)


@router.post("/system-tests/run")
def run_system_tests(user: User = Depends(admin)) -> schemas.SystemTestRunResult:
    from app.core.system_tests import SystemTestsBusy

    if get_settings().force_offline_mode:
        raise HTTPException(status.HTTP_409_CONFLICT, "System tests are disabled on this instance (FORCE_OFFLINE_MODE).")
    logger.info("API | System tests requested | role=admin")
    try:
        run = views.run_tests(user.user_id)
    except SystemTestsBusy as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return schemas.SystemTestRunResult(passed=run.passed, status=run.status_text, returncode=run.returncode,
                                       duration_s=round(run.duration_s, 1), started_at=str(run.started_at),
                                       counts=run.counts, cases=run.cases, stdout=run.stdout, stderr=run.stderr)


@router.get("/system-tests/report", response_class=HTMLResponse)
def system_test_report() -> HTMLResponse:
    """The pytest-html report of the last run (a self-contained HTML file, download)."""
    from app.core import logs

    try:
        html = logs.TEST_REPORT.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No test report yet.") from exc
    return HTMLResponse(html, headers={"Content-Disposition": 'attachment; filename="test_report.html"'})
