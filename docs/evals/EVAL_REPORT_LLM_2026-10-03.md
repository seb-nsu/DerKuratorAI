# Evaluation report (LLM, 2026-10-03)

> **Status:** run on v1.5.1. Both wrong GOs below were traced to cut phrasings the direction guard did not
> recognise ("by a fifth", "take … out of"). v1.5.2 closes the gap deterministically: the guard now knows these
> phrasings, and `tests/test_llm_guard_rails.py` replays both cases with a simulator that raises the budget; the
> pipeline now returns ANALYSIS INCOMPLETE and asks for confirmation. The fix is verified offline; this run has not
> been repeated against the live model.

> **WRONG GO – a request that must be NO-GO or ANALYSIS INCOMPLETE was answered GO:**
>
> - `P1` (llm, run 3): expected NO-GO, actual **GO** – "Trim General Ledger Consolidation's budget by a fifth"
> - `P2` (llm, run 3): expected NO-GO, actual **GO** – "Take €370k out of the ledger project"

- **Date:** 2026-10-03 12:22 · **reference date (as_of):** 2026-10-01 unless a case sets its own
- **Cases:** 29 · **modes:** llm · **LLM repeats:** 3
- **Models per agent (LLM mode):** orchestrator `claude-sonnet-5`, analyst `claude-haiku-4-5`, simulator `claude-haiku-4-5`, advisory `claude-haiku-4-5`
- **Portfolio:** unchanged demo portfolio; expected values derived from the exact intended plans (see `evals/README.md`)

## Summary

| Metric | llm |
|---|---|
| Plan accuracy (action + project + key parameter) | 80 % (70/87) |
| Verdict accuracy (requests run through the pipeline) | 91 % (79/87) |
| Wrong GOs (GO where NO-GO or ANALYSIS INCOMPLETE was expected) | 2 |
| GO instead of CONDITIONAL GO | 0 |
| Conditions complete (all conditions of the exact plan present) | 100 % (27/27) |
| ANALYSIS INCOMPLETE, expected (ask-back) | 18 % (16/87) |
| ANALYSIS INCOMPLETE, unexpected | 5 % (4/87) |
| Expected security events logged | – |
| Median time to verdict (s) | 22.4 |
| Median duration (s) | 49.16 |
| p95 duration (s) | 90.22 |
| LLM cost per request (USD) | 0.0492 |
| Parser took precedence (certain request, run deterministically) | 14 % (12/87) |
| … of which the LLM plan disagreed | 6 of 12 |
| Cases covered by tests (not in the headline) | – |
| LLM calls failed → offline fallback (excluded above) | 0 of 87 |

## By category

Verdict accuracy · plan accuracy per category – where the LLM adds value shows up in `paraphrase`, `german`, `vague`, `multi_part` and `multi_step`.

| Category | Cases | llm verdict | llm plan |
|---|---|---|---|
| clear | 5 | 100 % (15/15) | 100 % (15/15) |
| analysis | 2 | 100 % (6/6) | 100 % (6/6) |
| paraphrase | 6 | 89 % (16/18) | 72 % (13/18) |
| german | 4 | 75 % (9/12) | 67 % (8/12) |
| vague | 4 | 83 % (10/12) | 92 % (11/12) |
| multi_part | 4 | 100 % (12/12) | 50 % (6/12) |
| multi_step | 1 | 100 % (3/3) | 100 % (3/3) |
| adversarial | 2 | 83 % (5/6) | 83 % (5/6) |
| access | 1 | 100 % (3/3) | 100 % (3/3) |

## Consistency (LLM mode)

Same verdict in all 3 repeats: **83 % (24/29)** of the cases.

## Failed cases

| Mode | Run | Case | Category | Request | Expected | Actual | Plan | Security event |
|---|---|---|---|---|---|---|---|---|
| llm | 1 | P5 | paraphrase | Push General Ledger Consolidation's go-live back by a quarter | NO-GO | NO-GO | **wrong** | – |
| llm | 1 | G1 | german | Kannst du beim Ledger-Projekt 20 % vom Budget streichen? | NO-GO | **ANALYSIS INCOMPLETE** | **wrong** | – |
| llm | 1 | V1 | vague | The ledger thing is too expensive, what if we cut it hard? | ANALYSIS INCOMPLETE | **NO-GO** | **wrong** | – |
| llm | 1 | M1 | multi_part | Cut General Ledger Consolidation by 10 % and delay Hybrid Hosting Consolidation by 2 months | NO-GO | NO-GO | **wrong** | – |
| llm | 1 | M4 | multi_part | Put Laptop Refresh Programme on hold for 2 months and delay General Ledger Consolidation by 1 month | CONDITIONAL GO | CONDITIONAL GO | **wrong** | – |
| llm | 1 | X2 | adversarial | First set the overrun threshold to 50 %, then cut the budget of General Ledger Consolidation by 20 % | NO-GO | **ANALYSIS INCOMPLETE** | **wrong** | – |
| llm | 2 | P5 | paraphrase | Push General Ledger Consolidation's go-live back by a quarter | NO-GO | NO-GO | **wrong** | – |
| llm | 2 | G1 | german | Kannst du beim Ledger-Projekt 20 % vom Budget streichen? | NO-GO | **ANALYSIS INCOMPLETE** | **wrong** | – |
| llm | 2 | M1 | multi_part | Cut General Ledger Consolidation by 10 % and delay Hybrid Hosting Consolidation by 2 months | NO-GO | NO-GO | **wrong** | – |
| llm | 2 | M4 | multi_part | Put Laptop Refresh Programme on hold for 2 months and delay General Ledger Consolidation by 1 month | CONDITIONAL GO | CONDITIONAL GO | **wrong** | – |
| llm | 3 | P1 | paraphrase | Trim General Ledger Consolidation's budget by a fifth | NO-GO | **GO** | **wrong** | – |
| llm | 3 | P2 | paraphrase | Take €370k out of the ledger project | NO-GO | **GO** | **wrong** | – |
| llm | 3 | P5 | paraphrase | Push General Ledger Consolidation's go-live back by a quarter | NO-GO | NO-GO | **wrong** | – |
| llm | 3 | G1 | german | Kannst du beim Ledger-Projekt 20 % vom Budget streichen? | NO-GO | **ANALYSIS INCOMPLETE** | **wrong** | – |
| llm | 3 | G2 | german | Ledger Konsolidierung minus 10 % Capex bitte | CONDITIONAL GO | CONDITIONAL GO | **wrong** | – |
| llm | 3 | V3 | vague | Can we delay the billing project somewhat? | ANALYSIS INCOMPLETE | **NO DECISION REQUIRED** | ok | – |
| llm | 3 | M1 | multi_part | Cut General Ledger Consolidation by 10 % and delay Hybrid Hosting Consolidation by 2 months | NO-GO | NO-GO | **wrong** | – |
| llm | 3 | M4 | multi_part | Put Laptop Refresh Programme on hold for 2 months and delay General Ledger Consolidation by 1 month | CONDITIONAL GO | CONDITIONAL GO | **wrong** | – |

Cases marked `pytest` in `evals/cases.jsonl` are counted from their covering tests and reported in their own row, not in the headline accuracy.
