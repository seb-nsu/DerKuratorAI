"""Planning page (AppTest): PMO sees every demand with rank, assessment and weights; a Project Manager only their
own demands, without rank."""
from __future__ import annotations

from tests.test_admin_console import _app, app_sandbox  # noqa: F401 - the fixture is used by name


def _texts(at) -> str:
    return " ".join(str(m.value) for m in list(at.markdown) + list(at.caption) + list(at.header) + list(at.subheader))


class TestPlanningPage:
    def test_pmo_sees_all_demands_with_rank_and_assessment(self, app_sandbox):  # noqa: F811
        at = _app("planning")
        assert "Planning" in [h.value for h in at.header]
        table = at.dataframe[0].value
        assert "Rank" in table.columns and len(table) == 19
        assert {t.label for t in at.tabs} >= {"Demands", "Plan scenarios", "PMO tools"}

    def test_pm_sees_only_own_demands(self, app_sandbox):  # noqa: F811
        at = _app("planning", login="Priya Raman")
        table = at.dataframe[0].value
        assert "Rank" not in table.columns
        assert set(table["Demand"]) == {"DMD-2027-001", "DMD-2027-002", "DMD-2027-011"}
        assert "Assessment" not in {t.label for t in at.tabs}

    def test_detail_opens(self, app_sandbox):  # noqa: F811
        at = _app("planning", plan_detail="DMD-2027-005")
        assert "DMD-2027-005 · Data Catalogue Foundation" in [s.value for s in at.subheader]

    def test_scenario_workshop_calculates_compares_and_saves_a_draft(self, app_sandbox):  # noqa: F811
        at = _app("planning")
        assert {"Plan scenarios", "Saved plan scenarios"} <= {t.label for t in at.tabs}
        at.button(key="scn_run").click().run()
        assert not at.exception, at.exception
        assert any("CONDITIONAL GO" in m.value and "Base plan" in m.value for m in at.markdown)
        at.text_input(key="scn_name").set_value("Cut 10 %").run()
        at.slider(key="scn_factor").set_value(-10).run()
        at.button(key="scn_run").click().run()
        assert not at.exception, at.exception
        compared = next(d.value for d in at.dataframe if "Scenario" in d.value.columns)
        assert list(compared["Scenario"]) == ["Base plan", "Cut 10 %"]
        assert compared.set_index("Scenario").at["Cut 10 %", "Out"] >= compared.set_index("Scenario").at[
            "Base plan", "Out"]
        at.button(key="scn_save").click().run()
        assert not at.exception, at.exception
        saved = next(d.value for d in at.dataframe if "Scenario id" in d.value.columns)
        assert list(saved["Scenario status"]) == ["Draft"] and saved.at[0, "Scenario name"] == "Cut 10 %"
        at.text_input(key="scn_name").set_value("Optimised").run()
        at.slider(key="scn_factor").set_value(10).run()
        at.radio(key="scn_method").set_value("value").run()
        at.button(key="scn_run").click().run()
        assert not at.exception, at.exception
        assert "Optimizer vs. rank order" in {t.label for t in at.tabs}
        assert any(m.value.startswith("- Score sum of the plan") for m in at.markdown)

    def test_pm_has_no_scenario_workshop(self, app_sandbox):  # noqa: F811
        at = _app("planning", login="Priya Raman")
        assert "Plan scenarios" not in {t.label for t in at.tabs}

    def test_planning_question_in_the_chat(self, app_sandbox):  # noqa: F811
        at = _app("console", pending_prompt="What drops out in 2027 at -10 %?")
        last = [m for m in at.session_state["messages"] if m["role"] == "assistant"][-1]
        assert last["meta"]["mode"] == "planning" and last["meta"]["verdict"]["decision"]
        assert "Out of the plan" in last["content"]

    def test_plan_from_draft_to_board_approval_to_project(self, app_sandbox):  # noqa: F811
        at = _app("planning")
        at.text_input(key="scn_name").set_value("Board plan").run()
        at.button(key="scn_run").click().run()
        at.button(key="scn_save").click().run()
        assert not at.exception, at.exception
        board_plan = next(o for o in at.selectbox(key="saved_pick").options if o.endswith("Board plan"))
        at.selectbox(key="saved_pick").set_value(board_plan.split(" · ")[0]).run()
        at.button(key="saved_submit").click().run()
        assert not at.exception, at.exception
        assert any("submitted to the Portfolio Board" in m.value for m in at.success), [e.value for e in at.error]

        board = _app("planning", login="BOARD-CHAIR")
        assert [t.label for t in board.tabs][:2] == ["Plan approval", "Demands"]
        assert "Steering Console" not in str(board.session_state["_pages"])
        next(b for b in board.button if b.key.startswith("plan_ok_")).click().run()
        assert not any("Annual plan approved" in m.value for m in board.success)  # confirmation first
        next(b for b in board.button if b.key.startswith("plan_confirm_")).click().run()
        assert not board.exception, board.exception
        assert any("Annual plan approved" in m.value for m in board.success), [e.value for e in board.error]
        assert any("Approved plan in force" in c.value and "approved by BOARD-CHAIR" in c.value for c in board.caption)
        assert {"report_xlsx", "report_html"} <= {d.key for d in board.get("download_button")}

        pmo = _app("planning", pmo_tool="conversion")
        pmo.selectbox(key="convert_pick").set_value("DMD-2027-002").run()
        pmo.button(key="convert_submit").click().run()
        assert not pmo.exception, pmo.exception
        assert any("Conversion of DMD-2027-002" in m.value for m in pmo.success), [e.value for e in pmo.error]
