# Changelog

All notable changes to DerKurator AI are documented in this file.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Removed
- **Streamlit front end.** NiceGUI and React are the only front ends; both run on the HTTP API. Removed: the Streamlit
  app (`app/main.py`, `app/ui/`), `.streamlit/config.toml`, `start_streamlit.bat`, `scripts/smoke_test.py`, the
  `streamlit` dependency, the AppTest UI tests and `FRONTEND=streamlit`. The tests of shared helpers now import them
  from `app/views` directly. `scripts/serve.py` (and `Start.bat`, the dev container) starts NiceGUI by default; the
  container image, both compose files and `render.yaml` default to React with the health check `/api/v1/health`. The
  logo moved to `frontends/nicegui/assets/`.

### Changed
- **New, fully invented demo portfolio** (`data/DerKurator_Demo_Portfolio.json`/`.xlsx`): new project and demand names, people, teams, portfolios, business units,
  services and project texts; the ServiceNow table layout, the figures and the intentional data-quality cases stay,
  so every scenario behaves as before. Tests, evaluation cases, mapping files, fixtures and documentation follow.
- Internal release audits (`docs/audits/`) removed; `.gitignore` also covers logs, local runtime files and IDE folders.

## [1.6.0] – 2026-11-09

First release with the work of the `experiment/addon` branch (merged into `main` on 2026-11-01) and the decision
layer for existing tools (E1–E8).


- CI uses fewer Actions minutes: no run for documentation-only changes or draft pull requests, the Neo4j, React,
  end-to-end and container jobs run on a pull request only when their files changed, a push to `main` runs only the
  suite and the container image, the Playwright browser is cached, and the separate smoke job is gone (the container
  job runs the same smoke test inside the image). Manual runs (`workflow_dispatch`) start every job.
- Python 3.14 for CI, the container image and the dev container; the pinned set also still passes on 3.12 (e.g. for
  Streamlit Community Cloud).

PMO use cases and the write journal (S12), any data source through a mapping file (S17), annual and multi-year
portfolio planning (S19–S21, value-optimal plan S21b), the ServiceNow connector (S18) and the dependency graph in memory (S30).

### Added
- **Decision layer for existing tools (E1–E8):** a portfolio from several sources with `source_system`, key prefixes
  and a duplicate check across sources; the status check "reported green, calculated red" (business rules
  `status_*`, also a Data Steward check); reference class forecasts and a seeded Monte Carlo of the open portfolio
  (`simulation_runs`, `confidence_level_pct`, `reference_class_min_projects`); the review of approved decisions after
  six and twelve months with a hit rate per verdict; the rule lab (`python scripts/rule_lab.py rule=value`: which
  reference verdicts a threshold change would flip, exit 1 on a wrong GO); the audit pack per approved decision (ZIP
  with paper, verdict, decision-log records, chain check, snapshot and SHA-256 manifest); read-only Microsoft Planner
  and monday.com sources (`scripts/work_sync.py`); write-back of approved changes to ServiceNow, off by default, with
  a live conflict check and the transfer status after the next sync. PMO sub-page "Decision insights" (Streamlit)
  and `/api/v1/insights/*`.
- **Decision insights in NiceGUI and React:** the PMO sub-page now exists in all three front ends with the same five
  sections (status check, portfolio with probabilities, reference class of a project, review of past decisions with
  the audit pack download, transfers to ServiceNow), linked from the PMO overview. End-to-end test
  `tests/e2e/test_insights.py`.
- **Front ends before real use (after the merge review):** sign-out of NiceGUI and React ends the session on the
  server (`POST /auth/sign-out`, every token of the session answers 401), and the token is renewed while the user works
  (`POST /auth/refresh`, at most 12 hours per sign-in), so the session timeout acts on inactivity as in Streamlit.
  Calculations in the API (what-if, question box, plan scenarios) are limited to two at a time, one per user and 20 per
  user and minute (429 "busy" beyond that). The NiceGUI session cookie is `Secure` in PROD. `scripts/serve.py` and the
  API refuse more than one server process (`WEB_CONCURRENCY`). The Approvals cells (verdict, budget Δ, submitted)
  come formatted from the API for all three front ends. `chat_store` moved to `app/core` (the API no longer imports
  the Streamlit package). README section "Front ends"; the NiceGUI environment badge no longer wraps on a phone.
- **Scheduler scripts for the pilot:** `scripts/approval_deadlines.py` records escalations and expiries without anyone
  opening the inbox; `scripts/backup_data.py` copies the data folder (under the inbox lock), checks the hash chain of
  the copy, keeps the newest backups and verifies a restored decision log (`--verify`).
- **Business rule `cancel_conditional_benefit_eur`** (default €1,000,000): a project stop that gives up this much
  planned benefit is a CONDITIONAL GO; the stop condition names the exit costs.
- **Assessment age** in the demand table ("120 days", "▲ re-assess" beyond the limit).
- **LLM operation in the EU:** `LLM_DATA_RESIDENCY=eu` calls only providers with an own endpoint
  (`ANTHROPIC_BASE_URL`, `OPENAI_BASE_URL`: an EU gateway, Azure OpenAI v1 or a local model server); every other
  provider counts as missing and its agent answers offline. Admin Console › System status shows the endpoint host per
  provider. Data flows and the pilot checklist in `docs/DATA_PROTECTION.md`.
- **Pilot operations handbook** (`docs/PILOT_OPERATIONS.md`): what lives where, backup and restore, what to do with a
  write-journal finding, role assignment.
- **Structured orchestrator plan:** the orchestrator also fills action and values (amount, percentage, months, cost
  type) for a budget change, transfer, stop, hold or delay. The code runs a complete plan deterministically – only
  with values the request states, with sign and cost type read from the request – and the Simulator assesses the
  result (same agents as before). Anything incomplete or unsure stays with the Simulator and its tools.
- **Advisory check:** amounts and percentages in the AI-written text that no input or tool result contains, and
  verdict words outside the recommendation line that differ from the rule-based verdict, are reported as warnings
  on the report. The text itself is not changed.
- **Ask about the page:** a question box on the project detail, the overview (portfolio briefing), the planning
  pages, every report and inbox proposal, the A/B comparison and the data quality page. The model answers only from
  the facts the code collects from the reader's own view (names masked, labelled as AI, no own verdict); a change
  question is handed to the Steering Console; offline a digest of the matching facts answers. Audited without the
  question text.
- **Planning analysis:** per demand the whole dependency chain, the effect of a delay of one to four quarters on its
  successors, and (PMO, Board) its base-plan decision and its position in the budget envelopes and team limits; an
  Analysis tab with the hot spots of the plan and the demands others wait for.
- **Operations:** sign-in lockouts and the attempt budget are shared by all worker processes (`data/state/`, no names
  on disk); a worker picks up settings saved by another worker; "Reload from .env" now really re-reads `.env` without
  a restart (values set outside `.env` still win). The decision log is a hash chain with a verification in the
  Admin Console (Logs → Verify the hash chain); archiving links the new log to the archive.
- **Planning in the evaluation:** ten cases of the new category `planning` – eight annual-plan questions (cut, base
  plan, value-optimal, forced in, excluded, more budget; English and German) answered by the planning engine, and two
  everyday questions with planning words that must reach the agents. Enforced offline like the reference cases.
- **Load test of the optimizer:** 300 demands in three business units with predecessors are solved exactly in a few
  seconds (limit 20 s; measured 2.6 s for 300 and 4.5 s for 600 demands in a cloud container).
- **Writes beyond Projects:** append-only rows in `Baselines`, `Approvals` and `Reserve_Ledger`, field updates of
  risks (status, mitigation, probability) – compare-and-write, four-eyes, snapshot and restore as for project fields.
  `phase` and `approved_end_date` are governed fields.
- **Resume after a hold:** restart date, returning team, ramp-up cost, new end date, team load and successor dates.
- **Cost stress test:** raises the remaining cost of every project in scope by a percentage and ranks the projects by
  their forecast overrun; CONDITIONAL GO if a project tips to NO-GO. Writes nothing.
- **Re-baselining:** a new baseline version with a mandatory reason, variance against the original and the current
  baseline, board approval above `rebaseline_board_threshold_pct`, planning-quality condition above
  `max_rebaselines_per_year`.
- **Stage gates:** configurable criteria per phase transition (`stage_gates` in `env_<stage>.json`); a failed
  mandatory criterion is a NO-GO, a failed advisory one a condition. The approval is appended to `Approvals`.
- **Management reserve:** draw and return with balance, floor (`reserve_floor_pct`) and ledger.
- **Skills:** capacity, demand and utilisation per skill (`Team_Skills`, `Skill_Demand`), skill transfer between the
  teams of two projects, skill bottlenecks next to the team bottlenecks.
- **Risk response:** Mitigate / Accept / Avoid / Transfer with exposure before and after on a configurable risk
  scale, a condition when the response costs more than it removes or a high/high risk is accepted.
- Scenario builder inputs for all seven, EN/DE parser keywords, agent tool descriptions, project detail with
  baseline history, risk exposure and shortcuts.
- **Write journal:** intent and committed records with one operation id, `pending_apply` reservation of a proposal
  during its write, `reconcile_operations()` and red rows with "Mark as resolved" in Admin Console › System status.
- Tests: 18 golden cases (GC-41 to GC-58), 30 offline eval cases (`pmo` PM1–PM14, PT1–PT2, German PG1–PG7,
  paraphrases PP1–PP7), `tests/test_append_writes.py`, `tests/test_pmo_tools.py`, `tests/test_write_journal.py`.

- **Approval governance:** approval deadline per proposal (`approval_sla_days`) with escalation at one and two
  deadlines, expiry after `approval_expiry_days`, withdrawing an own open proposal, and delegation of the approval
  right to another PMO account for a period (at most `delegation_max_days`); the deputy decides on behalf of the
  delegator, never on their own or the delegator's proposals. A project manager or the Board never deputises (the
  inbox would show them projects outside their scope); an older delegation to such an account counts for nothing. Approvals page with the views Pending, Decided, Mine and Delegations.
- **Benefit realisation:** actual benefits per tranche and month (`Benefit_Actuals`, appended by the PM in the new
  Benefits tab of the project page), a benefit realisation review (NO-GO below `benefit_realization_nogo_pct`,
  corrective plan below `benefit_realization_min_pct`, condition for a tranche unreported after
  `benefit_report_grace_days`).
- **Multi-currency:** `Exchange_Rates` (currency, rate to EUR, valid from); rows with another `currency` are shown in
  EUR and written back in their own currency; a project without a rate is blocked by the Data Steward.
- **Strategy score and value-optimal portfolio cut:** `Strategic_Goals` (weight) and `Project_Goals` (contribution
  0–5) give each project a strategy score 0–100 (fallback: value score × 20). The portfolio cut has a second method
  that finds the stops with the least strategic value lost (exact min-cover knapsack) and compares them with the
  ranked list.
- **Project merge:** the target takes over budget, actuals, forecast and benefit, the earlier start and later end,
  the source's tasks, plans, risks, stakeholders and dependencies; the source is closed. Always a condition
  (re-baseline of the target), plus the board threshold, the combined forecast, successor deadlines and a second
  business unit or sponsor. Re-keying rows is governed (four-eyes).
- **Dependency graph in memory (NetworkX) instead of KùzuDB:** built from the portfolio data on first use and rebuilt
  when the source changes (about 1 s for 300 projects instead of about 20 s); same tool names and output (checked on
  298 recorded outputs), golden cases unchanged. The dependency graph in reports is a Plotly chart instead of pyvis.
  `KUZU_DB_PATH`, the seed script and the packages `kuzu` and `pyvis` are gone; ADR-001 is updated.
- Tests: golden cases GC-59 to GC-65, offline eval cases PM15–PM19, PG8–PG9, PT3–PT4,
  `tests/test_approval_governance.py`, `tests/test_benefits_and_currency.py`, `tests/test_strategy_cut.py`,
  `tests/test_project_merge.py`.

- **Any data source through a mapping file (S17):** a YAML mapping (`MAPPING_FILE`, default
  `data/mappings/servicenow_demo.yaml`) maps the raw tables of a JSON file, an Excel workbook or a CSV folder to the
  canonical schema – column names, values, units, defaults, year columns unpivoted to rows, generated keys and one
  source per table. Every load writes an import report (Admin Console); a required Projects column that is missing
  blocks in the Data Steward. Writes go back into the source when every table can be inverted, otherwise into
  `<mapping>.store.json`. The JSON and Excel adapters are the identity mapping. Excel template with field
  descriptions and value lists: `scripts/make_excel_template.py`. See `docs/DATA_MAPPING.md`.
- **Demands, assessment and rank (S19):** initiatives that are no project yet (`Demands`) with quarterly figures
  (`Demand_Year_Plans`) and capacity (`Demand_Resource_Plans`). The PMO scores them against a weighted model
  (`Scoring_Model`, `Demand_Scores` append-only); the rank is deterministic: mandatory first, then score, NPV,
  latest start, number. A Project Manager sees only own demands and no rank. Weight changes are four-eyes. The Data
  Steward checks demands (rule "Planning"). New page **Planning**.
- **Planning scenarios (S20):** budget envelopes per business unit or portfolio and year (`Budget_Envelopes`, cap,
  reserve, hard or soft) and capacity limits per team (`Capacity_Limits`). Running projects bind money (cost plans,
  the rest of the forecast to complete) and capacity first; then the demands fill the envelopes in rank order,
  deferred by whole years within their latest start, otherwise Out with the limit, area, year and amount. The PMO can
  force a demand in, exclude it or move it. Rule-based planning verdict: NO-GO for a hard limit or capacity of the
  binding year, a missing mandatory demand or a violated dependency; ANALYSIS INCOMPLETE for a forced demand without
  figures or assessment or a circular dependency; CONDITIONAL GO for a soft limit, a preview year at
  `planning_preview_alert_pct` or over its limit, a forced demand ahead of a better one, run cost after go-live above
  `planning_run_cost_alert_pct`, an assessment older than `planning_score_max_age_days` or a rough estimate of an L/XL
  demand. Scenario workshop with comparison of up to three scenarios, budget traffic light and capacity heatmap;
  drafts are saved append-only (`Planning_Scenarios`, `Planning_Items`). Planning questions in the chat
  ("What drops out in 2027 at −10 %?") are parsed deterministically and answered by the planning engine.
- **Approval and conversion (S21):** new role **Portfolio Board** (demo account `BOARD-CHAIR`) that reads the whole
  portfolio and approves only the annual plan; the PMO submits a saved scenario, the Board approves it through the
  proposal workflow (four-eyes, one operation id in the write journal). An approved plan sets its demands to
  Approved (start in the binding year), Planned or Deferred. An approved demand becomes a project – master data,
  quarterly money as monthly cost plans, capacity as resource plans, first baseline, link in both directions – after a
  second PMO approves. Board pack as Excel and as a print-ready page (PDF via the browser).
- **Value-optimal plan (S21b):** a second proposal next to the rank order – the free demands are chosen together
  (exact 0-1 programme, HiGHS via scipy): most mandatory demands, then the highest score sum, within the same
  envelopes, capacity limits and dependencies. Tab "Optimizer vs. rank order" with key figures, a summary and one
  row per demand that differs, each with the limit behind it; also in the chat (*"Optimise the annual plan with
  10 % more budget"*), in saved scenarios (`filling`) and as the sheet "Optimizer" in the board pack. Falls back to
  the rank order if the solver finds no solution. Tests: `tests/test_planning_optimizer.py`.
- **ServiceNow connector (S18):** `DATA_SOURCE=servicenow` reads the SPM tables through the Table API into a local
  snapshot – paged, delta sync on `sys_updated_on` with an overlap window, deleted records removed, one sync at a
  time, retry on rate limits; the portfolio loads from the snapshot through `data/mappings/servicenow_spm.yaml`
  (projects, tasks, risks, status reports). Credentials only from secrets, read-only (approved changes go to the
  mapping's store). Sync in Admin Console › System status or `python scripts/servicenow_sync.py`. A required table
  a mapping leaves out loads empty with an import finding; a raw column named like a mapping target gives way to the
  mapped one. Tests against recorded-format Table API responses: `tests/test_servicenow.py`.
- **Simple admin access:** `AUTH_ADMIN_PASSWORD` (at least 12 characters, optionally `AUTH_ADMIN_USER`, default
  `ADMIN`) adds a System Admin account without a hashed user entry; the password is hashed in memory only.
- Tests: `tests/test_data_mapping.py`, `tests/test_planning_demands.py`, `tests/test_planning_scenario.py` (one golden
  case per planning rule, 0 wrong GOs), `tests/test_planning_chat.py` (no evaluation case is read as a planning
  question), `tests/test_planning_approval.py`, `tests/test_planning_ui.py`.

### Changed
- **Pilot readiness after the full application audit (2026-10-06):**
  - Test runs never write into the live data folder: the system tests of the Admin Console write to a temporary
    folder (`DERKURATOR_WRITE_DIR`) and the test session fails when a test changes `data/`.
  - With two PMO accounts an active delegation no longer blocks anything: the deputy decides the delegator's own
    proposals in their own name. The own-proposal check ignores upper and lower case.
  - Benefit realisation counts only the actuals of due tranches (plus unassigned ones); actuals of tranches not yet due
    are shown as realised early. Before, early actuals could turn a shortfall into more than 100 %.
  - The cost of delay of a project already past its planned end uses the team cost (FTE × rate) instead of the whole
    remaining estimate per month.
  - PROD and TEST use Claude models (reachable through an own endpoint); PROD reads a missing `LLM_DATA_RESIDENCY` as
    `eu`. The shared demo accounts appear only on the synthetic demo portfolio. CI also runs on `experiment/addon`.
  - Every browser session has its own scenario context (no process-wide fallback any more). A reload or a link to an
    app page shows the sign-in and then opens that page instead of Streamlit's "Page not found".
  - The LLM call log keeps five rotated files. The planning capacity heatmap uses a colour-blind safe scale; the
    inbox hides the proposal id; systemic risk clusters with the same figures form one card.
  - Default `planning_score_max_age_days` is 180 days (was 365). Rule limits are cross-checked: hold duration plus
    restart ramp-up never exceed the max. schedule shift, the benefit NO-GO threshold never exceeds the corrective-plan
    threshold.
- **Frontend clean-up after the UX review** (accessibility, consistency, safer approvals):
  - The approval inbox decides only a proposal the PMO picked; its table shows the request, project names, budget
    change and the verdict with a shape symbol. Reject and *Send back* need a comment. Approving the annual plan
    opens a confirmation; a NO-GO / incomplete plan needs an override with a reason, as in the PMO inbox.
  - Actions that discard settings or data ask first (business rules and required fields to defaults, reload settings,
    reset counters, full ServiceNow sync, withdraw, revert, end a delegation); a log-clearing confirmation resets after
    use. One label and icon per action on every page.
  - Contrast to WCAG AA: darker orange, grey and green theme text, darker status colours in charts and bars, a visible
    focus outline; RAG is also shown by shape and word (tables, dependency graph, budget lights), the skill heatmap is
    one hue, Before/After bars differ by pattern. Streamlit's running indicator is back; long actions show a spinner.
  - The report leads with a recommendation callout and the management summary opens with the rule-based
    recommendation in every mode; empty key-figure cards are hidden; changes are counted as decisions (a budget cut
    that also moves CapEx, OpEx and total cost counts once). Submitting a NO-GO result is a secondary action with a
    required reason, shown to the PMO with the proposal.
  - Names: console results are *what-ifs* (page *What-if comparison*), planning has *plan scenarios*, the guided
    builder a *scenario type*. Proposal states read "Waiting for approval", "Being applied" …
  - Tables show business labels, "–" for empty cells and dates without "00:00:00"; one format for euros, millions,
    percentages and timestamps. Offline question answers show five readable facts with their section.
  - Feedback after an action also appears as a toast; "why not" notes are info boxes; disabled buttons say why.
  - Projects open by selecting a row and have a shareable link; the project page has seven tabs (Edit only for the
    owner), master data and governed fields have their own buttons. The dependency graph draws direct links first.
  - The Portfolio Board lands on its plan status (waiting, plan in force, base plan, demands); the PMO's assessment,
    weights and conversion sit in one *PMO tools* tab; the board pack is built only when downloaded. A Project
    Manager's overview shows their open proposals instead of the management reserve; start examples use own projects.
  - Demo buttons on the sign-in page sign in with one click; the header shows the display name.
- **No agent framework any more:** the agents run on an own, small loop (`app/llm`) that calls Anthropic and OpenAI
  (also Azure OpenAI, local servers and Gemini through its OpenAI-compatible endpoint) directly through their SDKs.
  Same agents, prompts, tools, limits and fallbacks as before; CrewAI, LangChain and LiteLLM (and with them chromadb)
  are gone from the dependencies. Structured results come from a forced tool call validated against the model. The
  MCP connector now hands server tools to an agent as ordinary tools. Run mode in logs and results: `agents`
  (stored runs with `crewai` still read).
- **Leaner tracing:** Arize Phoenix and the OpenInference instrumentors are gone. Every LLM call now writes one line
  to `data/llm_calls.jsonl` (caller, model, duration, tokens, estimated cost, outcome – no prompt or answer text);
  Admin Console › System status shows calls, errors, median duration, tokens and cost per agent and model. Optional:
  with `OTEL_EXPORTER_OTLP_ENDPOINT` set, spans of the pipeline steps and of every LLM call plus token, duration and
  cost metrics go over OTLP/HTTP to the company's collector (Datadog, Grafana, Splunk), likewise without content.
  `PHOENIX_*` variables are no longer read.
- **Question boxes calculate:** a change or planning question asked in any question box (“what if the budget is cut
  by 20 %?”, “what drops out in 2027 at −10 %?”) is calculated right there by the rule-based pipeline – verdict,
  reasons and changes, no LLM step – and the result points to the next step: the Steering Console for the decision
  paper and the proposal, or the planning scenarios with the inputs filled in. On a project page a question that
  names no project is about that project.
- Hint texts of inputs corrected where they no longer matched the behaviour (four-eyes wording, who maintains the
  business rules, risk score range 1–5, capacity limit, proposal sources, PII masking scope, shadow run wording).
- **Code structure:** the workspace pages are split into one module per page (`overview.py`, `console.py`,
  `approvals_ui.py`, `projects_ui.py`), the pipeline into parser, guard rails and paper; the stage-gate model moved to
  `app/core/stage_gates.py`; the pages call the analysis core through a service layer (`app/services.py`). Remaining
  lazy imports against the layering are listed in ARCHITECTURE.md. No change in behaviour.
- PII masking compiles its name patterns once per registry state and skips texts that cannot contain a name: about 70×
  faster on the demo portfolio (about 1 s less per request), same output.
- The test suite runs the offline evaluation once per session (evaluation and demo coverage share it) and checks each
  password hash once per session.
- Demo data: baseline values (version 1), 17 team skills and 564 skill-demand rows (synthetic, documented in
  `scripts/migrate_s12_data.py`).

## [1.5.2] – 2026-10-03

### Changed (2026-10-22)
- The demo accounts include `DEMO-ADMIN` (System Admin, public demo password), so reviewers of the hosted demo reach
  the Admin Console with one click on the sign-in page, like the other demo accounts. Valid only in DEV and TEST.

### Fixed
- **Planning scenarios:** a duplicate demand number no longer stops the scenario (the Data Steward blocks the number,
  the first row stands for it); an approved plan records the Board member and the date (“approved by” was empty).
  Soft limits are documented: the engine never plans a free demand over one; only bound money or a forced demand
  exceeds it, and that needs a board resolution.
- **Planning page, project manager's view:** a demand's predecessors outside the manager's scope are no longer named
  (a neutral note instead, no count), and the demand data-quality check no longer reports them by number.
- **Approved changes in a store** (mapping sources that are not written back, e.g. ServiceNow): the store records the
  source value each change replaced. Once the source system holds a different value for that field, the source
  applies again and the import report says so – a stored change never masks the source for good.
- **Planning questions in the chat:** "demand", "Vorhaben" and "envelope" alone no longer route a question to the
  planning engine; they need a planning year or a percentage (e.g. "Which teams have the highest skill demand?" goes to
  the agents again).
- A budget cut worded as "trim", "take … out of", "slash", "remove", "withdraw", "scale back" (German "rausnehmen",
  "wegnehmen", "abspecken") was not recognised as a cut. When the LLM simulated such a request as an increase, the
  direction check let it pass and the verdict was GO (paid eval 2026-10-03: P1 and P2 in one of three runs). The
  request is now asked back instead. Regression test with a Simulator that raises the budget.

### Documentation
- README, architecture and evaluation reports brought to v1.5.2: offline report regenerated for all 70 cases, LLM
  report of 2026-10-03 added as `docs/evals/EVAL_REPORT_LLM_2026-10-03.md` with a note on the closed wrong GOs,
  outdated deployment notes removed.

## [1.5.1] – 2026-10-02

Demo cases: every case of the engine can be shown live on the demo portfolio.

### Added
- Eval category `demo` (D1–D8): a demo case for each verdict rule that no other case showed on the demo portfolio –
  budget transfer, team overload, missed portfolio-cut target, heuristic estimate, Force Simulation with tool
  warning, negative NPV on a hold, indicative result on incomplete master data, overrun from a delay.
- Eval category `approval` (AP1–AP7) with `tests/test_demo_approvals.py`: the four-eyes flows on the demo data –
  PM proposal approved, PMO proposal approved by the second PMO, NO-GO approved only with override and reason,
  rejection with reason, revert of an approval, and the refused self-approval, self-rejection and PM decision.
- `tests/test_demo_coverage.py`: every verdict rule, guided scenario type and approval outcome has a demo case.
- Golden cases for the two stop rules (sponsor review on a hard dependent, indicative result on a blocked project);
  36 golden cases. Eval cases can set `force`; the `demo` category is enforced in the offline eval test.

## [1.5.0] – 2026-10-02

Hardening before the pilot from the v1.3.0 teardown: business rules a steering committee would challenge, a clear
approval flow with per-approval revert, pilot security and compliance, argument checks for free-text requests,
business language throughout the UI, and tests, CI and docs that hold it in place.

### Changed – business rules
- **Portfolio cut** picks the smallest combination of stops that reaches the target (tie-breakers: completion,
  benefit forgone, dependents) and reports its surplus.
- **Project stop** is a CONDITIONAL GO from a sunk-cost share (`cancel_conditional_sunk_pct`) or with a hard
  dependent (sponsor confirmation, impairment); stop + transfer moves the net release after exit costs.
- **Chained schedule steps** are path-independent: ETC and NPV follow the forecast, a reverse step credits the
  earlier cost of delay.
- **Hold:** deferred spend capped by the remaining cost, burn = ETC / max(remaining, 1 month), remobilisation capped
  by the hold. FTE from a stopped project creates no source utilisation.
- Euro budget changes and transfer targets respect `max_budget_change_pct`. New business rules
  `delay_cost_factor_pct`, `overrun_tolerance_eur`, `max_hold_months`, `cancel_conditional_sunk_pct`,
  `condition_due_days`; the builder's limits come from the rules.
- Invalid tool input is ANALYSIS INCOMPLETE (input error), not NO-GO; a NO-GO lists every violation; conditions carry
  owner role and due date.
- Demo data is generated by `scripts/build_demo_portfolio.py` (dependency dates, EAC ≥ actual, NPV from benefit
  tranches, varied story points); new Data Steward error "EAC below actual cost".

### Changed – approval flow
- A proposal stores the net change per field; a change set of manual changes only is a hand-over ("Record
  hand-over", nothing is written).
- **Revert this approval** (Approvals › Decided) proposes the inverse change set as its own four-eyes proposal;
  fields changed since the approval block it.

### Security and compliance
- A Project Manager learns nothing about projects outside the scope – no id, name, status, budget, load or count –
  only "Dependent projects outside your scope are affected – the PMO coordinates." Graph and cockpit show one
  neutral "Restricted project" entry. The PMO sees everything.
- One-line security log values; strict, cached user file (no empty or duplicate ids); a sign-in attempt budget per
  minute across all accounts; restore, environment switch and settings changes check the System Admin role in the
  core.
- Evidence chain: every analysis has a run id (with user and data version) that the proposal and the apply cite; a
  failed write records `data_write_failed`; a write whose record fails says so with an incident id.
- **Purge user** (System Admin) removes a user's chats and saved scenarios and pseudonymises the user id in
  proposals, incidents and archives; README has a data retention table.
- Incidents (UI errors, failed analyses, failed audit writes) go to `data/incidents.jsonl` (Admin Console › Logs ›
  Incidents) instead of the decision log. All stored timestamps carry the UTC offset.

### Added – language layer
- After the Simulator, every budget change is compared with the sizes the request states (digits or words such as
  "a fifth", "half a million", "200k"); a mismatch is asked back instead of decided. A plan for a project the request
  does not name is asked back; a repeated call of the same tool for the same project is dropped. No extra LLM step.
- Four paraphrase eval cases for unit errors (55 cases).

### Changed – UI
- One format for field labels, euro amounts, percentage points, FTE and timestamps on screen, in the paper and in the
  exports; tool names in the paper become business titles.
- Scenario comparison ordered older vs newer; the SteerCo pack takes its conditions from the rule engine and shows
  the live proposal status; clearer composer captions, sentence-case labels, project table with an Open button.

### Tests, CI and maintenance
- New tests for the critical paths: Approve & Apply end to end (value, snapshot, record), a failing LLM crew
  (warning and offline verdict), scenario context per thread, fail-closed RLS and PII masking, a broken Kùzu handle,
  Force Simulation by a PM.
- Golden cases check the exact conditions; ten budget and schedule cases check EAC, new budget, overrun and ROI
  against hand-calculated values.
- Frozen clock instead of `sleep` and fixed reference dates in tests; one clock module (`app/core/clock.py`).
- CI: coverage lower bound 84 % plus at least 80 % of the changed lines (diff coverage); ruff adds timezone,
  broad-except, self-comparison, loop-variable and unused-noqa rules; pip-audit as a warning.
- Evidence is returned in offline and LLM mode alike; dead code and duplicate helpers removed; the smoke test logs to
  a temporary folder; remaining German script and log strings translated.

## [1.4.0] – 2026-10-02

Must-fix items from the v1.3.0 teardown: correct schedule and ROI figures, a dependency check that no longer depends
on the model, honest eval metrics and a public demo that cannot be locked out.

### Fixed
- **Capacity changes** change the duration, not the effort: the burn rate scales with the capacity, so a stretched
  schedule no longer books the remaining cost twice (ERP at −50 % capacity: EAC unchanged instead of €2.72 M).
- **Acceleration** in the method-aware schedule tool is rejected beyond the remaining duration and its extra
  capacity is booked into the forecast; the builder's schedule slider stops at the remaining duration.
- **ROI after a budget cut** is computed on the expected cost (max of EAC and the new budget); a cut below the
  forecast no longer creates return or ROI, and both are written only when they really change.
- **Dependency check in LLM mode** runs deterministically for every changed project, also in guided plans and when
  the model plans no graph analysis.
- **Guided plans in LLM mode** run with the builder's exact values; the Simulator assesses the result without tools,
  and a guided change request can no longer end as NO DECISION REQUIRED.
- **Hosted demo:** the Admin Console refuses to switch to PROD while the demo accounts are active (which would have
  locked every new session out), and the environment switch needs a confirmation.

### Changed
- The dependency condition follows the direction of the change: only a net cut of budget or FTE, a later end, a stop
  or a hold puts dependents at risk. E4 and AN3 now expect GO; two new golden cases (34).
- Offline, a change request without a size is asked back; the remaining offline fallback is labelled "heuristic
  placeholder (no AI)" instead of "AI estimate".
- The verdict label is the verdict alone (it fits every badge); the deciding rule is `Verdict.rule` and the first
  reason.
- Proposals and the approval audit records store the full verdict (reasons, conditions, thresholds in force, as_of),
  not only its label.
- Eval: P5 expects NO-GO like G4; a stale expectation stops the run; "Parser took precedence" with "of which the LLM
  plan disagreed" replaces "plans corrected"; wrong GO defined, ANALYSIS INCOMPLETE split into expected and
  unexpected, test-covered cases counted apart, "conditions complete" per mode; AN3 and AN4 moved to `clear`.
- The demo accounts contain no admin; an owner adds a real admin via `AUTH_EXTRA_USERS` (file or JSON). System tests
  are disabled in the Admin Console under `FORCE_OFFLINE_MODE`.
- The repository ships no proposals, chats or locks (git-ignored runtime state); **Reset demo data** (DEV/TEST, Admin
  Console › Operations) restores the first snapshot and archives proposals, chats and saved scenarios.
- Dev container on Python 3.12 with default security settings, TEST and offline mode.
- 583 automated tests (was 554).

## [1.3.0] – 2026-10-02

Sign-in per identity, code quality gates in CI and a deployable demo build.

### Added
- **Sign-in per identity.** A sign-in page replaces the role selector; the role comes from the user file only.
  Local accounts with PBKDF2-SHA256 password hashes (600,000 iterations), account lockout after repeated failures,
  session timeout and sign-out in the header. Sign-in, failure, lockout and sign-out are logged as security events.
  Lockout and timeout are set per environment (`auth` in `env_<env>.json`).
- Demo accounts (`data/demo_users.json`) for DEV and TEST with a public demo password shown on the sign-in page.
  PROD refuses the demo accounts and needs its own user file (`AUTH_USERS_FILE`); switching an instance to PROD
  signs a demo session out. `scripts/hash_password.py` creates password hashes.
- `requirements-dev.txt` with `ruff`, `pytest-cov` and `coverage`; `ruff` and coverage configuration in
  `pyproject.toml`.
- `scripts/smoke_test.py`: starts the app headless, checks the health endpoint and the start page, renders the
  sign-in page and signs in with a demo account.
- README section "Deployment" for the demo on Streamlit Community Cloud (private app, offline mode, no API key).

### Changed
- CI runs `ruff`, the suite with coverage (at least 86 % overall; verdict engine 100 %, proposals 90 %, security
  93 %) and uploads test results and the coverage report. The deploy placeholder job is replaced by the smoke test.
- Lint findings fixed: unused variables, import order, `zip(..., strict=True)` where lengths must match.
- 554 automated tests (was 527).

## [1.2.0] – 2026-10-01

Fixes from the release audit of v1.1.0, a golden verdict test set, new evaluation cases and the graph database
decision.

### Changed
- A NO-GO or ANALYSIS INCOMPLETE proposal can only be approved with an explicit override and a reason, enforced in
  the approval and the direct apply path. Approve & Apply in the PMO inbox opens a confirmation dialog.
- Requesters can no longer reject their own proposal; a stale proposal logs a warning. Promoting DEV → TEST needs a
  confirmation. Snapshots referenced by an approval are never pruned.
- **A delay raises the forecast.** Every schedule simulation (direct and method-aware) adds the cost of delay to the
  EAC and judges it by the forecast-overrun rule: NO-GO above `eac_overrun_nogo_pct`. On the demo data this moves
  E5, G4, M1 and S2 from CONDITIONAL GO to NO-GO (E5: EAC €432,600 → €746,189 vs. budget €420,000, 77.7 %).
- The method-aware forecast applies the same ±`max_timeline_shift_months` guardrail as the direct shift.
- Within one request the last evaluation per project and field wins: a cut followed by an increase is judged on the
  net effect, and a back-transfer, a stopped project or a graph that answers again clears earlier findings. Resource
  transfers start from the scenario, not from the original data.
- A portfolio cut checks the dependents of every stopped project (domino rule).
- Results are computed for a reference date (`as_of`), stored with the verdict and shown as "As of" in the report;
  the same date gives the same verdict on any day.
- Tool warnings and graph outages reach the verdict as structured facts instead of parsed text.
- "Builds on the previous scenario" is off after a NO-GO or incomplete step. Remaining emojis replaced by icons and
  status words; top risks say "blocked" only behind a hard dependency. Markdown export and SteerCo Pack use the
  business change columns.
- Data Steward checks are cached per data state, the proposal log is parsed once per file version, and one cached
  read-only graph database is used per process.
- Comments and docstrings are in English throughout.

### Added
- Admin Console → Logs → Analysis runs (role, mode, duration, verdict, plan outline; no names).
- Golden verdict cases (`tests/test_verdict_golden.py`): 32 scenarios on a synthetic portfolio that hit every
  verdict rule and guided scenario type and cover every branch of `compute_verdict`.
- Evaluation: cases T1–T3 (delays), C1–C3 (portfolio cuts) and F1 (net effect in one request), 51 cases in total;
  fixed reference date 2026-10-01, `--ids` to run a subset, and the share of plans the parser corrected.
- [ADR-001](docs/adr/ADR-001-graph-database.md): KùzuDB 0.11.3 stays pinned for v1.x, with triggers and an exit
  path (LadybugDB first, otherwise NetworkX).

### Fixed
- Version and test-count drift in the docs, unused code and the "project cockpit" wording.
- Tests no longer write to `data/` or the real log files.

## [1.1.0] – 2026-10-01

### Changed
- The workspace is split into six pages with a top navigation: Overview, Steering Console, Approvals, Projects,
  Scenarios and Data quality. Sign-in, environment and data reload sit in the header; the sidebar is gone.
- The Steering Console shows the analysis history on the left and one report at a time on the right: verdict, status
  and actions in the header, key figures, and tabs for summary, decision paper (by section), changes, impact,
  dependencies and data quality. The dependencies tab appears only when the analysis covered dependencies; the data
  quality tab lists only the findings for the projects the request names or changes. The guided builder and free
  text share one composer.
- The PMO approval inbox is a queue table with the same report viewer and a decision bar; decided proposals have
  their own view with the full history.
- Projects open as a detail page instead of a pop-up, with a data-quality tab and the edit form as the last tab.
  The project table has search and filters.
- The Admin Console is grouped into System status, Configuration, Operations, Logs and Quality; the technical status
  moved there from the sidebar.
- Confirmations for Approve & Apply, clearing the history and deleting a scenario open as dialogs.

### Added
- The rule-based reasons of the verdict and the thresholds in force are shown in the app (before: SteerCo Pack only);
  all conditions are listed instead of the first three.
- Every earlier report keeps its interactive dependency graph.
- Exports per report (SteerCo Pack, Markdown, CSV) next to the exports of the whole history.
- "Simulate scenario for this project" on the project detail page and "Open project" on the top risks.

## [1.0.3] – 2026-10-01

### Changed
- The verdict appears as soon as the calculation is done; the decision paper follows progressively. Figures in the
  paper are written by the code.
- The Simulator and the Analyst give short assessments instead of repeating tool results; the Advisory receives the
  tool results verbatim.
- Every agent has a time limit as a safety net (orchestrator 60 s, simulator 150 s, analyst 120 s, advisory 150 s).
- The evaluation report shows the median time to verdict.

## [1.0.2] – 2026-09-29

### Added
- Evaluation of the language layer: 44 natural-language cases and a runner that compares the offline parser and the
  LLM pipeline (`scripts/run_evals.py`, report in `docs/evals/EVAL_REPORT.md`).
- The offline evaluation runs in the test suite: reference and access cases, prompt-injection verdicts and "no wrong
  GO" are enforced on every run.

### Fixed
- A stopped project is no longer checked against its old forecast.
- Dependency questions are recognised more reliably.
- Pure dependency questions no longer return a conditional verdict.
- Claude models no longer receive a temperature (not supported by the provider).
- The LLM plan is checked by deterministic guard rails: change requests never end without a decision, certain parser
  plans win, vague amounts are asked back, data-quality blocks work in both paths.
- The evaluation separates failed LLM calls from LLM results.

### Security
- Refused write and approval attempts are logged as security events.

## [1.0.1] – 2026-09-28

### Fixed
- Unknown project names in change requests now return ANALYSIS INCOMPLETE with suggestions.

## [1.0.0] – 2026-09-28 · Enterprise Release

The first release intended for a controlled pilot: governance is enforced server-side, every verdict is
deterministic, and the interface is designed for executive audiences.

### Added
- **Deterministic Verdict Engine** – GO / CONDITIONAL GO / NO-GO / ANALYSIS INCOMPLETE computed in Python from
  structured tool facts and the configured business rules, in a fixed and documented rule order. The LLM's
  recommendation line is overwritten if it deviates; the rule basis (rule, values, threshold) is appended to every
  decision paper.
- **Configurable business rules per environment** – EAC overrun NO-GO threshold, board approval threshold, domino
  threshold, discount rate, FTE cost rate, hold retention and more, maintained in the Admin Console.
- **Separation of duties** – governed fields (budgets, CapEx/OpEx, planned benefit, EAC, actual cost, planned end
  date, project state) change only through a proposal approved by a second person. Applies to Project Managers and
  between PMO identities (new *PMO (Full Access) – Deputy* identity).
- **Server-side write authorisation** – every write is checked for role, project ownership, an open proposal of
  another person and an identical change set, independent of the UI.
- **Advanced PMO scenarios** – put a project on hold, prioritised portfolio cut list to a savings target, successor
  required-by deadline checks on every schedule slip, benefit timing and NPV.
- **Scenario memory and A/B comparison** – continued multi-step scenarios; save scenarios and compare verdicts,
  budget impact and change sets side by side.
- **Project Cockpit** – pop-up dialog per project with master data, status history, cost S-curve, deadlines,
  dependencies and risks.
- **Approval inbox and SteerCo Pack** – PMO review of proposals with decision paper; one-page, print-ready executive
  summary.
- Fully pinned `requirements.txt`; English README, architecture documentation and this changelog.
- GitHub Actions pipeline: runs the test suite on Python 3.12 for every pull request and push to `main`.

### Changed
- **UI polish** – corporate theme (navy primary instead of red), Material icons instead of emojis, business labels
  instead of database column names, technical details (intent routing JSON, runtime, raw exceptions) hidden from
  business users.
- **Confirmations for irreversible actions** – Approve & Apply, clear chat and delete scenario require an explicit
  confirmation.
- **Master-data editing** – free fields and governed fields are shown separately; the save button switches live
  between *Save Changes* and *Submit Proposal*.
- **Facts supersede each other** – a corrective scenario step (e.g. restoring a cut budget) clears an earlier NO-GO;
  budget changes accumulate across steps for the board-approval rule.

### Fixed
- Parser failures and unknown projects are reported as *ANALYSIS INCOMPLETE* instead of a guardrail NO-GO.
- A missing EAC no longer passes the overrun rule silently – it yields a CONDITIONAL GO with a clear condition.
- Restoring the oldest snapshot at the retention limit no longer fails.

### Security
- Audit writes are blocking – if the audit record cannot be written, the data write is aborted.
- The system log is archived before it is cleared; snapshots referenced by an approval are never pruned.

## [0.9.0] · Stability & Audit

Hardening of the write path and of data protection.

### Added
- **Snapshots and rollbacks** – byte-exact snapshot of the data source before every write, with change set, inverse
  and checksum; restore from the Admin Console, itself undoable.
- **Compare-and-write** – a write is refused if the data changed since the scenario was calculated; nothing is
  written on conflict.
- **Cross-process locks** – serialised data-source writes, restores, graph seeding, proposal and scenario logs; a
  reader/writer lock between graph queries and rebuilds.
- **PII masking** – people's names pseudonymised before any LLM call and in generated papers; the graph holds no
  personal names.
- Row-level security across all linked tables; `[RESTRICTED PROJECT]` for projects outside a user's scope.
- UI error boundaries with incident IDs – no tracebacks for end users.
- Persistent, append-only chat history per identity; per-request FinOps cost tracking.
- Environment staging (DEV → TEST → PROD) with config promotion; business audit trail per changed field.

### Fixed
- Cycle-safe, depth-bounded graph queries; content-based detection of stale graph data.
- Zero budgets treated as missing instead of producing unapproved budget proposals.
- Internal code defects reported as incomplete analysis instead of business guardrail violations.

## [0.8.0] · Graph & Agents

The first end-to-end version of the digital twin.

### Added
- **KùzuDB integration** – embedded openCypher graph of projects, tasks, teams, risks and dependencies, seeded
  automatically from the portfolio export; downstream/upstream dominoes, shared resources, team bottlenecks and risk
  clusters.
- **CrewAI orchestration** – Orchestrator, Data Steward, Simulator, Analyst and Advisory agents producing an Executive
  Decision Paper; configurable model per agent with shadow models for comparison.
- **Deterministic simulation tools** – budget change and transfer (CapEx/OpEx, ROI), resource transfer, schedule
  shifts (agile velocity vs. waterfall effort), project cancellation, with guardrails returned as explicit errors.
- **Data Steward quality gate** – rule-based checks that block incomplete projects from quantitative simulation.
- **Offline mode** – a deterministic rule parser drives the same tools when no LLM is available.
- JSON and Excel adapters for ServiceNow exports; Approve & Apply with audit log; local tracing with Arize Phoenix.
