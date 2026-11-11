"""Display helpers shared by the pages: status symbols, decision count, question digest, summary lead."""
from app.core.formatting import rule_label, status_text
from app.crew.ask import digest
from app.crew.verdict import NO_DECISION, NO_GO, Verdict, lead_summary
from app.views.reports import decision_count


def test_status_text_pairs_every_status_with_a_shape():
    assert status_text("NO-GO") == "■ NO-GO"
    assert status_text("CONDITIONAL GO") == "▲ CONDITIONAL GO"
    assert status_text("Green") == "● Green"
    assert status_text(None) == "–"


def test_a_budget_cut_with_its_follow_on_fields_is_one_decision():
    cut = [{"project_id": "P-1", "field": f} for f in ("budget_cost_eur", "total_planned_cost_eur",
                                                        "planned_capital_eur", "planned_operating_eur")]
    assert decision_count(cut) == 1
    assert decision_count(cut + [{"project_id": "P-2", "field": "planned_end_date"}]) == 2
    assert decision_count([{"project_id": "P-3", "field": "planned_capital_eur"}]) == 1  # never zero for a project


def test_rule_label_hides_internal_rule_ids():
    assert rule_label("DQ-Profile:missing_business_case") == "Data profile"
    assert rule_label("Completeness") == "Completeness"


def test_digest_shows_at_most_five_facts_with_their_section():
    facts = "## Project P-1\n" + "\n".join(f"- Budget line {i}: €{i}" for i in range(9)) + "\n## Risks\nnone"
    text = digest(facts, "What is the budget?")
    assert text.count("\n- ") == 5 and "**Project P-1** · Budget line 0" in text
    assert "4 more fact(s)" in text and "##" not in text


def test_the_summary_opens_with_the_rule_based_recommendation():
    paper = "# Paper\n**Recommendation:** NO-GO\n\n## 1. Management Summary\n- Cut by 20 %.\n"
    verdict = Verdict(NO_GO, NO_GO, ("Budget does not cover the forecast.", "P-1: forecast exceeds the budget."))
    led = lead_summary(paper, verdict)
    assert "## 1. Management Summary\n- **Recommendation: NO-GO** – Budget does not cover the forecast. P-1" in led
    assert lead_summary(led, verdict) == led  # idempotent
    analysis = Verdict(NO_DECISION, NO_DECISION, ("Analysis only.",))
    assert lead_summary(paper, analysis) == paper


def test_a_long_reason_is_cut_and_a_paper_without_summary_stays_as_is():
    from app.crew.verdict import lead_reason

    assert lead_reason(["x" * 300]).endswith("…") and len(lead_reason(["x" * 300])) == 240
    verdict = Verdict(NO_GO, NO_GO, ("Rule.",))
    assert lead_summary("# Paper without sections", verdict) == "# Paper without sections"
