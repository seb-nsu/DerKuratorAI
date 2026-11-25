"""HTTP API of the red trigger on the demo portfolio (clock on the demo reference day, PRJ-DEMO-1005 calculated red):
'Analyse now' and its options, the roles (PMO, the Project Manager of the red project, another Project Manager),
the anonymisation of other projects, the adopt rule of 24.11. (a PM adopts only an option that changes their own
project alone), the link to the proposal, dismiss, the scan with the trigger on, the SteerCo section, the learning
loop on the insights page – and that the analysis is deterministic."""
from __future__ import annotations

from app.core import logs
from app.core.runtime_config import update_runtime_config
from app.core.security import ROLE_PMO
from app.recovery import store
from tests import test_api

source, client = test_api.source, test_api.client
sign_in, what_if = test_api.sign_in, test_api.what_if
PM, PMO = test_api.PM, test_api.PMO
RED, RED_PM = "PRJ-DEMO-1005", "Kenji Watanabe"


def _analyse(client, headers, pid=RED) -> dict:
    response = client.post("/api/v1/recovery/analyse", headers=headers, json={"project_id": pid})
    assert response.status_code == 201, response.text
    return response.json()


def test_analyse_gives_package_options_with_a_reference(client):
    rec = _analyse(client, sign_in(client, PMO))
    keys = [o["key"] for o in rec["options"]]
    assert keys[-1] == "R" and len(keys) >= 2 and rec["recommended"] in keys[:-1]
    for option in rec["options"][:-1]:  # each option calculated as one package through the verdict engine
        assert option["steps"] and (option["result"]["verdict"] or {}).get("label")
        assert "cost_pct" in option["result"]["closure"]
    assert rec["status"] == "open" and rec["trigger"] == "manual" and rec["causes"]
    assert rec["explanation"]["offline"] and rec["explanation"]["llm"] is None  # offline in tests
    events = [e["event"] for e in logs.read_jsonl(logs.AUDIT_LOG)[0]]
    assert "recovery_analysed" in events
    listed = client.get("/api/v1/recovery/recommendations", headers=sign_in(client, PMO)).json()
    assert listed["enabled"] is False and [i["id"] for i in listed["recommendations"]] == [rec["id"]]
    assert listed["recommendations"][0]["headline"].startswith("🔴")


def test_analysis_is_deterministic(source):
    from app.data_adapters import get_data_provider
    from app.recovery.engine import analyse

    dataset = get_data_provider().load_dataset()
    first = analyse(RED, dataset, explain_with_llm=False)
    second = analyse(RED, dataset, explain_with_llm=False)
    assert first["fingerprint"] == second["fingerprint"]


def test_project_manager_roles_and_anonymisation(client):
    rec = _analyse(client, sign_in(client, PMO))
    other = sign_in(client, PM)  # Priya Raman: not her project
    assert client.get("/api/v1/recovery/recommendations", headers=other).json()["recommendations"] == []
    assert client.get(f"/api/v1/recovery/recommendations/{rec['id']}", headers=other).status_code == 404
    assert client.post("/api/v1/recovery/analyse", headers=other, json={"project_id": RED}).status_code == 403
    owner = sign_in(client, RED_PM)
    detail = client.get(f"/api/v1/recovery/recommendations/{rec['id']}", headers=owner)
    assert detail.status_code == 200
    donors = {s["project"] for o in rec["options"] for s in o["steps"]} - {RED}
    assert donors and not any(d in detail.text for d in donors)  # a donor never shows up for the PM
    assert "tool_texts" not in detail.json()["impact"]
    assert client.get(f"/api/v1/recovery/projects/{RED}", headers=owner).json()["recommendations"]
    assert client.get(f"/api/v1/recovery/projects/{RED}", headers=other).status_code == 404


def test_pm_adopts_only_own_options_and_the_proposal_links_back(client):
    rec = _analyse(client, sign_in(client, PMO))
    owner = sign_in(client, RED_PM)
    foreign = next(o["key"] for o in rec["options"] if o["steps"] and not o["adoptable_by_pm"])
    own = next(o["key"] for o in rec["options"] if o["adoptable_by_pm"])
    url = f"/api/v1/recovery/recommendations/{rec['id']}/adopt"
    assert client.post(url, headers=owner, json={"option": foreign}).status_code == 403
    assert client.post(url, headers=owner, json={"option": "R"}).status_code == 404  # the reference has no steps
    adopted = client.post(url, headers=owner, json={"option": own})
    assert adopted.status_code == 200, adopted.text
    run_id = adopted.json()["run_id"]
    assert adopted.json()["change_set"] and store.get_recommendation(rec["id"])["status"] == "adopted"
    proposal = client.post("/api/v1/proposals", headers=owner, json={"run_id": run_id})
    assert proposal.status_code in (200, 201), proposal.text
    assert proposal.json()["recommendation_id"] == rec["id"]
    assert store.get_recommendation(rec["id"])["proposals"] == [proposal.json()["id"]]


def test_pmo_adopts_a_package_with_a_donor(client):
    pmo = sign_in(client, PMO)
    rec = _analyse(client, pmo)
    foreign = next(o for o in rec["options"] if o["steps"] and not o["adoptable_by_pm"])
    result = client.post(f"/api/v1/recovery/recommendations/{rec['id']}/adopt", headers=pmo,
                         json={"option": foreign["key"]}).json()
    assert result["steps"] == len(foreign["steps"]) and result["verdict"]["label"]
    touched = {c["project_id"] for c in result["change_set"]}
    assert RED in touched and len(touched) >= 2


def test_dismiss_needs_the_pmo_and_a_reason(client):
    rec = _analyse(client, sign_in(client, PMO))
    url = f"/api/v1/recovery/recommendations/{rec['id']}/dismiss"
    assert client.post(url, headers=sign_in(client, RED_PM), json={"reason": "no"}).status_code == 403
    assert client.post(url, headers=sign_in(client, PMO), json={"reason": ""}).status_code == 422
    done = client.post(url, headers=sign_in(client, PMO), json={"reason": "The sponsor funds the gap"})
    assert done.status_code == 200 and done.json()["status"] == "dismissed"
    assert client.post(url, headers=sign_in(client, PMO), json={"reason": "again"}).status_code == 409
    adopt = client.post(f"/api/v1/recovery/recommendations/{rec['id']}/adopt", headers=sign_in(client, PMO),
                        json={"option": rec["recommended"]})
    assert adopt.status_code == 409


def test_scan_does_nothing_while_off_and_finds_the_transition_when_on(client, monkeypatch):
    from app.core import notifications

    notices = []
    monkeypatch.setattr(notifications, "record_red_trigger", lambda rec_id, key: notices.append((rec_id, key)))
    pmo = sign_in(client, PMO)
    assert client.post("/api/v1/recovery/scan", headers=pmo).json() == {"enabled": False, "triggers": [], "stored": []}
    assert client.post("/api/v1/recovery/scan", headers=sign_in(client, PM)).status_code == 403
    update_runtime_config(business_rules={"recovery_enabled": 1}, role=ROLE_PMO, pmo_head=True)
    first = client.post("/api/v1/recovery/scan", headers=pmo).json()
    assert first["enabled"] and first["baseline"] and first["stored"] == []  # the first run only remembers
    from app.recovery import watch

    with __import__("app.core.shared_state", fromlist=["update"]).update(watch.STATE) as memory:
        memory["projects"][RED]["calculated"] = "Green"  # as if it had been green at the last scan
        memory["projects"][RED]["reported"] = "Green"
    second = client.post("/api/v1/recovery/scan", headers=pmo).json()
    assert [t["project_id"] for t in second["triggers"]] == [RED] and len(second["stored"]) == 1
    assert client.post("/api/v1/recovery/scan", headers=pmo).json()["stored"] == []  # idempotent
    alerts = client.get("/api/v1/recovery/alerts", headers=sign_in(client, RED_PM)).json()
    assert alerts["enabled"] and [a["project_id"] for a in alerts["recommendations"]] == [RED]
    assert [n[0] for n in notices] == second["stored"]  # one notice per stored recommendation
    scans = client.get("/api/v1/recovery/scans", headers=sign_in(client, "DEMO-ADMIN")).json()
    assert scans["chain"]["ok"] and len(scans["scans"]) >= 3


def test_steerco_pack_and_learning_loop_show_the_recommendations(client):
    pmo = sign_in(client, PMO)
    rec = _analyse(client, pmo)
    client.post(f"/api/v1/recovery/recommendations/{rec['id']}/dismiss", headers=pmo,
                json={"reason": "Sponsor funds it"})
    second = _analyse(client, pmo)  # a new open recommendation for the pack
    run_id = what_if(client, pmo, 10)["run_id"]
    pack = client.get(f"/api/v1/steering/runs/{run_id}/export/steerco-pack", headers=pmo).text
    assert "Red projects and recommended countermeasures" in pack and RED in pack
    pm = sign_in(client, PM)
    pm_pack = client.get(f"/api/v1/steering/runs/{what_if(client, pm, 10)['run_id']}/export/steerco-pack",
                         headers=pm).text
    assert "Red projects" not in pm_pack  # the section is for the PMO and the board
    loop = client.get("/api/v1/insights/decision-review", headers=pmo).json()["recommendations"]
    assert loop["dismissed_by_measure"] and all(m["reasons"] == ["Sponsor funds it"]
                                                for m in loop["dismissed_by_measure"])
    assert second["id"] != rec["id"]
