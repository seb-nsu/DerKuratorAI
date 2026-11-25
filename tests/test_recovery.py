"""Red trigger (app/recovery): the watch (transitions, first run, idempotency), the store (hash chain, supersede,
dismiss, expire), the donor rules, the package optimiser, the verdict facts, the learning loop and the SteerCo
section – on synthetic data; the analysis of a demo project end to end in tests/test_api_recovery.py."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.core import clock
from app.core.business_rules import BusinessRules
from app.crew.decision_review import review_recommendations
from app.crew.verdict import compute_verdict
from app.recovery import store, watch
from app.recovery.levers import DONOR_STOP, TRANSFER, eligible_donors
from app.recovery.package import choose_mix
from app.views.steerco_pack import pack_data, render_pack_html

TODAY = date(2026, 11, 24)
RULES = BusinessRules(recovery_enabled=1)


def _red(dataset_factory, project_factory, eac: float = 420000, status: str = "Green"):
    """P-1001 with the given forecast (budget 420k) next to a green P-1002."""
    return dataset_factory([project_factory(estimate_at_completion_eur=eac, status=status),
                            project_factory(project_number="P-1002", project_name="Beta Billing",
                                            project_manager="Rowan Ellis")])


# --- watch -------------------------------------------------------------------------------------------------------

def test_first_scan_only_sets_the_baseline(dataset_factory, project_factory):
    result = watch.scan(_red(dataset_factory, project_factory, eac=650000), TODAY, RULES, known=set())
    assert result.baseline and result.triggers == []  # a red project at the start is no transition
    assert watch.memory()["projects"]["P-1001"]["calculated"] == "Red"


def test_turning_calculated_red_triggers_once(dataset_factory, project_factory):
    watch.scan(_red(dataset_factory, project_factory), TODAY, RULES, known=set())
    red = _red(dataset_factory, project_factory, eac=650000)
    first = watch.scan(red, TODAY, RULES, known=set())
    assert [(t.project_id, t.trigger) for t in first.triggers] == [("P-1001", watch.CALCULATED)]
    assert first.triggers[0].gap_eur == 230000 and first.triggers[0].previous["calculated"] != "Red"
    assert watch.scan(red, TODAY, RULES, known=set()).triggers == []  # still red: no new transition


def test_same_key_is_skipped_by_a_second_worker(dataset_factory, project_factory):
    watch.scan(_red(dataset_factory, project_factory), TODAY, RULES, known=set())
    red = _red(dataset_factory, project_factory, eac=650000)
    key = f"P-1001|{watch.CALCULATED}|{watch.data_state(red.projects.to_dict('records')[0], _check(red))}"
    watch.reset()
    watch.scan(_red(dataset_factory, project_factory), TODAY, RULES, known=set())
    result = watch.scan(red, TODAY, RULES, known={key})
    assert result.triggers == [] and result.skipped == [key]


def _check(dataset):
    from app.crew.tools.status_check import check_dataset

    return next(c for c in check_dataset(dataset, TODAY, RULES) if c.project_id == "P-1001")


def test_reported_red_triggers_and_calculated_red_can_be_switched_off(dataset_factory, project_factory):
    off = BusinessRules(recovery_enabled=1, recovery_trigger_calculated=0)
    watch.scan(_red(dataset_factory, project_factory), TODAY, off, known=set())
    assert watch.scan(_red(dataset_factory, project_factory, eac=650000), TODAY, off, known=set()).triggers == []
    reported = watch.scan(_red(dataset_factory, project_factory, eac=650000, status="Red"), TODAY, off, known=set())
    assert [t.trigger for t in reported.triggers] == [watch.REPORTED]


def test_gap_growing_after_the_analysis_triggers_again(dataset_factory, project_factory):
    watch.scan(_red(dataset_factory, project_factory), TODAY, RULES, known=set())
    watch.scan(_red(dataset_factory, project_factory, eac=650000), TODAY, RULES, known=set())
    watch.mark_analysed("P-1001", 230000)
    small = watch.scan(_red(dataset_factory, project_factory, eac=660000), TODAY, RULES, known=set())
    assert small.triggers == []  # +10k is below both the 25 % and the minimum gap
    grown = watch.scan(_red(dataset_factory, project_factory, eac=800000), TODAY, RULES, known=set())
    assert [t.trigger for t in grown.triggers] == [watch.REGAP]


def test_closed_project_leaves_the_memory(dataset_factory, project_factory):
    watch.scan(_red(dataset_factory, project_factory), TODAY, RULES, known=set())
    closed = dataset_factory([project_factory(state="Closed"),
                              project_factory(project_number="P-1002", project_name="Beta Billing")])
    watch.scan(closed, TODAY, RULES, known=set())
    assert "P-1001" not in watch.memory()["projects"]


def test_scan_log_keeps_the_last_scans(dataset_factory, project_factory):
    watch.scan(_red(dataset_factory, project_factory), TODAY, RULES, known=set(), source="test")
    assert watch.scan_log()[-1]["source"] == "test"


# --- store -------------------------------------------------------------------------------------------------------

def _rec(pid: str = "P-1001", **extra) -> dict:
    return {"project_id": pid, "project_name": "Alpha Platform", "options": [
        {"key": "A", "label": "Least intervention", "steps": [{"action": "reserve_draw", "project": pid}]},
        {"key": "R", "label": "Reference", "steps": []}], "causes": [{"kind": "cost", "label": "Cost", "gap_eur": 1}],
        "recommended": "A", **extra}


def test_store_is_idempotent_and_supersedes_the_open_one():
    first = store.create(_rec(), "P-1001|calculated|abc")
    assert first["status"] == store.OPEN
    assert store.create(_rec(), "P-1001|calculated|abc") is None  # a repeated scan stores nothing
    second = store.create(_rec(), "P-1001|regap|def")
    assert store.get_recommendation(first["id"])["status"] == store.SUPERSEDED
    assert second["status"] == store.OPEN and store.known_keys() == {"P-1001|calculated|abc", "P-1001|regap|def"}
    assert store.verify()["ok"] and store.verify()["chained"] == 3  # created, superseded, created


def test_dismiss_needs_a_reason_and_closes_the_recommendation():
    rec = store.create(_rec(), "k1")
    with pytest.raises(store.RecoveryStoreError):
        store.dismiss(rec["id"], "PMO-ADMIN", "PMO", "  ")
    done = store.dismiss(rec["id"], "PMO-ADMIN", "PMO", "Sponsor funds it")
    assert done["status"] == store.DISMISSED and done["dismiss_reason"] == "Sponsor funds it"
    with pytest.raises(store.RecoveryStoreError):
        store.adopt(rec["id"], "PMO-ADMIN", "PMO", "A", "run")


def test_adopt_and_proposal_link():
    rec = store.create(_rec(), "k1")
    adopted = store.adopt(rec["id"], "PMO-ADMIN", "PMO", "A", "run-1")
    assert adopted["status"] == store.ADOPTED and adopted["adopted_option"] == "A" and adopted["run_id"] == "run-1"
    store.link_proposal(rec["id"], "PR-1", "PMO-ADMIN")
    assert store.get_recommendation(rec["id"])["proposals"] == ["PR-1"]


def test_expire_closes_old_open_recommendations():
    rec = store.create(_rec(), "k1")
    assert store.expire(clock.now() + timedelta(days=13), ttl_days=14) == []
    assert store.expire(clock.now() + timedelta(days=15), ttl_days=14) == [rec["id"]]
    assert store.get_recommendation(rec["id"])["status"] == store.EXPIRED
    assert store.expire(clock.now() + timedelta(days=30), ttl_days=14) == []  # idempotent


def test_tampered_store_breaks_the_chain():
    store.create(_rec(), "k1")
    path = store.store_path()
    path.write_text(path.read_text(encoding="utf-8").replace("Alpha Platform", "Omega"), encoding="utf-8")
    assert store.verify()["ok"] is False


# --- donors and the package --------------------------------------------------------------------------------------

def test_donor_rules(dataset_factory, project_factory):
    projects = dataset_factory([
        project_factory(project_number="RED", priority=3),
        project_factory(project_number="LOW", priority=8, value_score=2),
        project_factory(project_number="LOWER", priority=9, value_score=5),
        project_factory(project_number="HIGH", priority=2),  # higher priority than the red project
        project_factory(project_number="PROTECTED", priority=1),
        project_factory(project_number="HOLD", priority=9, state="On Hold"),
        project_factory(project_number="BLOCKED", priority=9),
        project_factory(project_number="ALSO-RED", priority=9),
        project_factory(project_number="NOPRIO", priority=None),
    ]).projects
    red_row = projects[projects["project_number"] == "RED"].to_dict("records")[0]
    donors = eligible_donors(projects, red_row, {"RED", "ALSO-RED"}, {"BLOCKED"}, BusinessRules())
    assert [d["project_number"] for d in donors] == ["LOWER", "LOW"]  # lowest priority first


def _cand(cid: str, lever: str, value: float, collateral: int = 0, donors: tuple = ()) -> dict:
    return {"id": cid, "lever": lever, "donors": list(donors), "touches_others": bool(donors),
            "money_to_project": value, "result": {"collateral_points": collateral}}


def test_optimiser_prefers_donor_money_over_the_reserve_and_avoids_collateral():
    items = [_cand("t1", TRANSFER, 60000, donors=("D1",)), _cand("s1", DONOR_STOP, 200000, collateral=1, donors=("D2",))]
    chosen, reserve, solved = choose_mix(items, 100000, 500000, 3)
    assert solved and [c["id"] for c in chosen] == ["t1"] and reserve == 40000
    greedy, reserve_g, solved_g = choose_mix(items, 100000, 500000, 3, solver=False)
    assert not solved_g and [c["id"] for c in greedy] == ["t1"] and reserve_g == 40000


def test_optimiser_respects_the_donor_limit():
    items = [_cand(f"t{i}", TRANSFER, 30000, donors=(f"D{i}",)) for i in range(4)]
    chosen, reserve, _ = choose_mix(items, 120000, 0, 2)
    assert len(chosen) <= 2 and reserve == 0


# --- verdict facts -----------------------------------------------------------------------------------------------

@pytest.mark.parametrize(("fact", "owner_text"), [
    ({"type": "collateral_red", "successor": "P-2", "reason": "forecast over budget"}, "sponsor of P-2"),
    ({"type": "gap_not_closed", "project": "P-1", "remaining": 120000}, "remaining gap"),
    ({"type": "donor_strategic", "successor": "P-3", "goal": "Cloud first", "weight": 3}, "strategic goal"),
    ({"type": "threshold_near", "project": "P-1", "amount": 480000, "threshold": 500000}, "board threshold"),
])
def test_recovery_facts_are_conditions_never_a_silent_go(fact, owner_text):
    verdict = compute_verdict(["Simulation ok"], [], [fact], rules=BusinessRules())
    assert verdict.decision.startswith("CONDITIONAL") and any(owner_text in c for c in verdict.conditions)


# --- learning loop and SteerCo section ---------------------------------------------------------------------------

def test_learning_loop_judges_adopted_recommendations_after_six_months():
    recs = [
        {"id": "a", "project_id": "P-1", "status": "adopted", "adopted_at": "2026-01-10T09:00:00", "adopted_option": "A"},
        {"id": "b", "project_id": "P-2", "status": "adopted", "adopted_at": "2026-02-01T09:00:00", "adopted_option": "B"},
        {"id": "c", "project_id": "P-3", "status": "adopted", "adopted_at": "2026-10-01T09:00:00", "adopted_option": "A"},
        {"id": "d", "project_id": "P-4", "status": "dismissed", "recommended": "A", "dismiss_reason": "Sponsor pays",
         "options": [{"key": "A", "steps": [{"action": "reserve_draw"}, {"action": "budget_transfer"}]}]},
    ]
    status = {"P-1": {"reported": "Green", "calculated": "Yellow"}, "P-2": {"reported": "Red", "calculated": "Red"},
              "P-3": {"reported": "Green", "calculated": "Green"}}
    loop = review_recommendations(recs, status, TODAY)
    assert [(a["recommendation_id"], a["review_stage"], a["recovered"]) for a in loop["adopted"]] == [
        ("a", "6 months", True), ("b", "6 months", False), ("c", None, None)]  # c is too young to judge
    assert loop["judged"] == 2 and loop["recovery_rate_pct"] == 50.0
    assert [(m["measure"], m["dismissed"], m["reasons"]) for m in loop["dismissed_by_measure"]] == [
        ("budget_transfer", 1, ["Sponsor pays"]), ("reserve_draw", 1, ["Sponsor pays"])]


def test_steerco_pack_lists_red_projects_from_the_data():
    row = {"project_id": "P-1001", "project_name": "Alpha <Platform>", "causes": ["Cost overrun"], "gap_eur": 230000,
           "status": "open", "option_key": "A", "option": "Least intervention", "verdict": "CONDITIONAL GO"}
    page = render_pack_html(pack_data({"content": "", "meta": {}}, red_projects=[row]))
    assert "Red projects and recommended countermeasures" in page and "Alpha &lt;Platform&gt;" in page
    assert "Red projects" not in render_pack_html(pack_data({"content": "", "meta": {}}))


def test_notice_gives_the_pm_no_option_details():
    from app.core.security import ROLE_PM, ROLE_PMO
    from app.recovery.notify import compose

    rec = {"id": "r1", **_rec(), "options": [{"key": "A", "label": "Least intervention", "steps": [
        {"action": "budget_transfer", "project": "P-9", "target": "P-1001"}], "result": {"verdict": {"label": "GO"}}}]}
    pmo, pm = compose(rec, ROLE_PMO), compose(rec, ROLE_PM)
    assert "Recommended: option A" in pmo["text"] and "Least intervention" not in pm["text"]
    assert "second PMO approves" in pm["text"] and "P-9" not in pm["text"] + pmo["text"]
