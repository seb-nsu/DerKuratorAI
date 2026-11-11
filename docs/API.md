# HTTP API

The API (`app/api/`, FastAPI) is the back end of both front ends (NiceGUI, React). It contains no business logic:
each route checks identity and scope, calls the shared page logic and core modules (`app/views/`, pipeline, verdict
engine, proposal store, shared approval flow `app/approval_flow.py`) and returns JSON.

## Start

```bash
uvicorn app.api.main:app --host 127.0.0.1 --port 8000
```

Interactive documentation: `http://127.0.0.1:8000/api/v1/docs`; the contract is `/api/v1/openapi.json`.

Set `API_TOKEN_SECRET` (at least 32 characters, in `.env` or the host's secrets). PROD refuses every sign-in without
it. DEV and TEST then use a random secret per process, so tokens end when the process restarts (and several workers
need the secret in every environment, see "Limits"). The NiceGUI front end
also needs `NICEGUI_STORAGE_SECRET` (at least 32 characters) in PROD; it keeps its sessions, which hold the API token,
in `NICEGUI_STORAGE_PATH` (default `data/nicegui`).

## Sign-in and tokens

`POST /api/v1/auth/token` with `user_id` and `password` checks the user file and the lockouts per account and
returns a bearer token. The token lasts as long as the session timeout (`auth.session_timeout_minutes`).

The token carries only the user id, the environment and a session id. The role is read from the user file on every
request, so a removed or changed account takes effect at once. A token from one environment is invalid in another.

- **Renewal.** `POST /api/v1/auth/refresh` returns a fresh token of the same session while the current one is valid.
  NiceGUI and React call it on their next API call once less than 10 minutes remain, so the session timeout acts on
  inactivity. One sign-in is renewed for at most 12 hours.
- **Sign-out.** `POST /api/v1/auth/sign-out` revokes the session: the token and every renewal of it answer 401 from
  then on. The deny-list is held in memory until the last token of the session would have expired.
- **Where the token lives.** NiceGUI keeps it on the server, behind an `HttpOnly`, `SameSite=Lax` session cookie
  (`Secure` in PROD). React keeps it in the browser's `localStorage` on purpose, so a link opened in a new tab stays
  signed in; the pages render Markdown as elements (no raw HTML), so no stored script can read it. An `HttpOnly`
  cookie for React is an option for later.

## Routes (v1)

| Route | Who | What |
|---|---|---|
| `GET /health` | everyone | Liveness and environment |
| `POST /auth/token`, `GET /me` | everyone / signed in | Sign-in, own identity |
| `POST /auth/refresh`, `POST /auth/sign-out` | signed in | Renew the token of the session, sign the session out on the server |
| `GET /projects`, `GET /projects/{id}` | PMO, PM, Portfolio Board | Projects in the caller's scope (row-level security), with data-quality status; outside the scope it answers 404 |
| `POST /steering/runs` | PMO, PM | One what-if through the pipeline, from free text (`request`) or a guided scenario (`guided`: action, project, target, params – checked against the builder's options and bounds, the plan is built server-side). The result stays on the server under `run_id`; `base_run_id` continues an earlier what-if; `force` is PMO only. Each run is added to the caller's chat history |
| `GET /steering/guided` | PMO, PM | Guided scenario catalog and the projects of the caller's scope to pick from (blocked ones flagged), the business rules in use |
| `GET /steering/guided/{action}?project=` | PMO, PM | The inputs of one guided scenario for a project: defaults, bounds (e.g. the lowest schedule shift), options (teams, skills, risks, phases) and hints |
| `POST /steering/runs/{run_id}/apply` | PMO, PM | Approve & apply of the latest step when it needs no approval (approval mode `apply`); only manual changes are recorded as a hand-over. A NO-GO needs `override` plus a `comment`; governed changes answer 409 (submit a proposal) |
| `POST /steering/runs/{run_id}/save` | PMO, PM | Saves the latest step as a what-if for the A/B comparison |
| `GET /steering/chat`, `DELETE /steering/chat` | PMO, PM | The caller's analysis history (the same in both front ends) with each analysis's status, and whether the next request can build on the latest step (`continuation`, NO-GO starts fresh by default); clear it |
| `GET /whatifs`, `GET /whatifs/{id}`, `DELETE /whatifs/{id}` | PMO: all, PM: own | Saved what-ifs limited to the caller's current scope; delete by the owner or the PMO (audited) |
| `GET /whatifs/compare?a=&b=` | PMO: all, PM: own | A/B comparison: key figures, differences, budget impact per project, with the page's charts as Plotly figure JSON |
| `GET /proposals`, `GET /proposals/{id}` | PMO: all, others: their own | Proposals with deadline state; `?status=submitted` is the PMO inbox (escalated first) |
| `POST /proposals` | PMO, PM | Submits a run by its `run_id` (never a change set from the client). A NO-GO needs a `justification` |
| `POST /proposals/{id}/approve`, `/reject` | PMO (not the requester) | Four-eyes decision: compare-and-write with a snapshot, audit. A NO-GO needs `override` plus a comment; a rejection needs a comment |
| `GET /planning` | PMO, PM, Portfolio Board | Planning horizon, reference date, demands in scope and the page sections of the caller's role |
| `GET /planning/demands` | PMO, PM, Portfolio Board | Demands in the caller's scope with money per planning year and data-quality findings; filters `business_unit`, `status`, `year`. A PM sees only own demands and no rank |
| `GET /planning/demands/{id}`, `/demands/{id}/slip?quarters=` | PMO, PM, Portfolio Board | Demand detail (master data, annual figures, capacity, assessment, dependency chain; PMO and Board also the plan position) and what a delay of 1–4 quarters pushes along; outside the scope 404 |
| `GET /planning/analysis`, `GET /planning/board` | PMO, Portfolio Board | Base plan: verdict, budget and capacity hot spots, critical chains; the Board's landing figures |
| `GET /planning/criteria`, `GET /planning/assessment`, `GET /planning/demands/{id}/assessment` | PMO | Scoring model, demands open for an assessment, the assessment form with the current scores |
| `POST /planning/demands/{id}/assessment` | PMO | Appends the scores (checked against each scale), written directly as on the page, audited |
| `POST /planning/weights` | PMO | A weight change as a four-eyes proposal; a second PMO decides it with `POST /proposals/{id}/approve` |
| `GET /planning/conversions`, `POST /planning/conversions` | PMO | Approved demands waiting for their conversion; the conversion into a project as a four-eyes proposal |
| `GET /planning/scenario-options` | PMO | The choices of the scenario inputs (demands, scopes, years, budget factor, methods) |
| `POST /planning/scenarios/calculate`, `/scenarios/compare` | PMO | One scenario (verdict, decisions, budget, capacity with the heat map as Plotly figure JSON, optimizer vs. rank order) or 2–3 side by side. Stateless: the client sends the input |
| `GET /planning/scenarios`, `POST /planning/scenarios` | PMO, Board (read) / PMO | Saved scenarios; saves a draft (recalculated on today's data), audited |
| `POST /planning/scenarios/{id}/submit` | PMO | Recalculates the draft and submits the annual plan to the Portfolio Board (proposal of kind `plan`) |
| `GET /planning/plans`, `GET /planning/plans/{id}` | Portfolio Board: all, PMO: its own | Annual plans with paper, change set and whether approving needs an override; `?status=submitted` is the Board's inbox |
| `POST /planning/plans/{id}/approve`, `/reject` | Portfolio Board | The plan decision: approve writes the plan with a snapshot (a NO-GO only with `override` and a comment), reject needs a comment; audited. Never by the requester |
| `GET /planning/reports`, `POST /planning/reports` | PMO, Portfolio Board | Plans a board pack can show; the multi-year view (`json`) or the board pack (`xlsx`, `html`). Only the PMO sends a scenario input or a comparison |
| `GET /admin/status` | System Admin | Environment, data source, PII masking, LLM routing, graph, graph backend (NetworkX or Neo4j with its health), tracing, data residency, sign-in settings |
| `GET /admin/settings`, `PUT /admin/settings` | System Admin | Main and shadow model per agent (catalog models only), temperature, PII masking, orchestrator prompt. API keys are write-only: answers carry only `set` and `key_hint` |
| `POST /admin/settings/reload`, `POST /admin/caches/clear` | System Admin | Reload from `.env` and the environment file (`confirm`); drop cached model clients |
| `GET /admin/models`, `POST /admin/models`, `POST /admin/models/test` | System Admin | Model catalog; add a model after a live test call; test call only |
| `GET /admin/environment`, `POST /admin/environment`, `POST /admin/environment/promote` | System Admin | Switch the environment (`confirm`; PROD refused while the demo accounts are active; tokens of the old environment end); promote DEV to TEST or TEST to PROD (`confirm`) |
| `GET /admin/required-fields` | System Admin | Data Steward required fields, read-only (the PMO Head changes them under `/governance`) |
| `GET /admin/mcp`, `PUT /admin/mcp`, `POST /admin/mcp/{name}/probe` | System Admin | MCP integration settings (token only as set / not set) and connection test |
| `GET /admin/business-rules` | System Admin | Business rules, read-only (the PMO Head changes them under `/governance`) |
| `GET /governance/business-rules`, `PUT /governance/business-rules`, `POST /governance/business-rules/reset` | PMO (read), PMO Head (change) | Business rules with group, unit, bounds and default; values outside the bounds answer 422; a new team capacity limit rebuilds the graph; reset needs `confirm`; the core checks the PMO Head again |
| `GET /governance/required-fields`, `PUT /governance/required-fields`, `POST /governance/required-fields/reset` | PMO (read), PMO Head (change) | Data Steward required fields (identity fields stay locked); reset needs `confirm` |
| `GET /admin/finops`, `POST /admin/finops/reset` | System Admin | Estimated LLM costs per model (the only place costs appear); reset needs `confirm` |
| `GET /admin/notifications`, `POST /admin/notifications/test` | System Admin | Notifications on/off, the configured channels (never a secret or a recipient) and the outbox in counts (`queued`, `retrying`, `sent`, `failed`, last error); a test message per configured channel, sent right away to `to` or the role mailboxes, audited without the address (an invalid address 422, nothing to send to 409) |
| `GET /admin/servicenow`, `POST /admin/servicenow/sync` | System Admin | Last sync per table; change sync, or a full sync with `confirm` |
| `GET /admin/journal`, `POST /admin/journal/resolve` | System Admin | Write journal findings; mark one resolved with a mandatory comment |
| `GET /admin/database`, `POST /admin/database/rebuild-graph` | System Admin | Graph metadata and import report (counts only, no project content); rebuild the dependency graph |
| `GET /admin/snapshots`, `POST /admin/snapshots`, `POST /admin/snapshots/{id}/restore` | System Admin | Snapshots of the data source; restore needs `confirm`, saves the current state first and rebuilds the graph |
| `POST /admin/demo-reset`, `POST /admin/purge-user` | System Admin | Demo reset (DEV and TEST only) and erasure of one user's data; both need `confirm`; a purge is logged by pseudonym only |
| `GET /admin/system-tests`, `POST /admin/system-tests/run`, `GET /admin/system-tests/report` | System Admin | Last saved test results, a test run (not with `FORCE_OFFLINE_MODE`), the HTML report |
| `GET /admin/logs/{business-audit,analysis-runs,audit,incidents,shadow,system,llm-calls}` | System Admin | The console's logs (newest first; filters `event`, `mode`, `level`); LLM calls without prompt or answer text |
| `POST /admin/logs/audit/verify-chain`, `POST /admin/logs/{audit,shadow,system}/clear` | System Admin | Check the decision log's hash chain (audited); clear a log after archiving it (`confirm`) |
| `GET /projects/{id}` (detail) | PMO, PM, Portfolio Board | Everything the project detail page shows: key figures, master data, status history, costs with the Plotly figure JSON, baselines, benefit realisation, deadlines, dependencies (outside the scope one neutral row), risks with exposure, data quality, tabs and access level |
| `GET /projects/{id}/edit-form` | PMO, PM, Portfolio Board | The master-data form: sections `direct` and `governed`, per field kind, options, range, shown value, a stored value outside the range, required/missing and whether the caller may change it |
| `POST /projects/{id}/master-data` | owning PM | Saves one section with the changed fields only: `direct` is written (snapshot, audit, business audit), `governed` becomes one proposal for the PMO |
| `POST /projects/{id}/benefit-actuals` | owning PM, PMO | Reports an actual benefit (appended Benefit_Actuals row, no proposal) |
| `GET /proposals?view=pending\|decided\|mine` | PMO: all views, others: `mine` | The views of the Approvals page with SLA columns (`deadline_label`, `due_date`), status words, budget change, who decided (deputy with delegator) and the names of the listed projects in the caller's scope |
| `GET /proposals/{id}` (detail) | PMO, requester | Adds the decision hints (`own`, `proxy_own`, `acting_for`, override needed, hand-over), `can_decide`, `can_withdraw` and the revert state (PMO) |
| `POST /proposals/{id}/withdraw` | requester | Takes an open proposal back; nothing is written (audited) |
| `POST /proposals/{id}/revert` | PMO | Proposes the values before an approval as a new proposal (four-eyes again); fields changed since then block it |
| `GET /approvals/overview` | PMO, PM, Portfolio Board | Views with counts, the delegation the caller deputises under today (banner), escalations and expiries recorded by this view, deadline rules |
| `GET /delegations`, `POST /delegations`, `POST /delegations/{id}/revoke` | PMO | Delegation of the approval right to another PMO for a limited period: list with state, possible deputies, grant, end early (audited) |
| `GET /overview` | PMO, PM | Overview page: scope line (a PM never sees the portfolio size), KPI ribbon, risk/value matrix and overloaded teams, each chart as data plus Plotly figure JSON |
| `GET /overview/top-risks` | PMO, PM | Alert bar: blocked projects and systemic risk clusters in scope; a blocker outside the scope is only "projects outside your scope" |
| `GET /overview/open-items` | PMO, PM | The caller's own proposals still waiting for the PMO, with deadline state |
| `GET /overview/reserve` | PMO | Management reserve against its floor and the skill heatmap (data and figure) |
| `GET /data-quality` | PMO, PM | Data Steward figures, quality bars and findings of the scope; `?severity=Errors\|Warnings`, `?project=` (repeatable) |
| `GET /data-quality/interpretation` | PMO, PM | Executive interpretation of the findings (AI when available, else rule-based) |
| `GET /assist/pages` | PMO, PM, Portfolio Board | The question boxes of the caller's pages: title, examples, whether an AI answers right now |
| `POST /assist/ask` | PMO, PM, Portfolio Board | Question box of a page (`overview`, `data_quality`, `report` with `run_id` or `proposal_id`, `plan`, `demand` with `demand_id`, `project` with `project_id` – a change question naming no project is about it –, `compare` with the saved what-ifs `a` and `b`, `demands` with the table filters `business_unit`, `status`, `year`, `scenario` (PMO) with the calculated `scenarios` – recalculated on today's data – and the shown `scenario`). A change or planning question is calculated by the rules (verdict, changes, next step); any other is answered from facts the server builds from the caller's scope. Audited without the question text |
| `GET /steering/runs/{run_id}/report`, `GET /proposals/{id}/report` | owner of the run / who may see the proposal | The report viewer: verdict, key figures, change count, tabs (Summary, Decision paper with its sections, Changes, Impact, Dependencies, Data quality) with the charts as Plotly figure JSON, in the caller's scope; a run adds its status badge and the console's actions (save, submit, apply or hand over, the note shown instead) |
| `GET /steering/chat/export/{markdown\|csv}` | PMO, PM | The whole analysis history as the simulation report (Markdown) or every proposed change (CSV); audited |
| `GET /steering/runs/{run_id}/export/{kind}`, `GET /proposals/{id}/export/{kind}` | owner of the run / who may see the proposal | Download (`steerco-pack` HTML, `markdown`, `csv`) of a report, in the caller's scope; audited |

Every admin route answers 403 for every other role; the System Admin sees settings and logs only, never project data.
An admin action answers `kind` and `message` (plus its own fields) with 200, or 409 (422 for invalid input) when it
fails. Actions that discard data or settings need `"confirm": true` in the body and are audited.

Unexpected errors answer 500 with an incident id. The traceback goes only to the system log and
`data/incidents.jsonl`. LLM costs never appear in these answers; they stay in the Admin Console.

## Limits

What-if runs (status and result), the latest step per user, sign-outs and the per-user calculation counters live in
the API state store (`app/api/state_store.py`), chosen with `STATE_STORE`:

| `STATE_STORE` | Where | Use |
|---|---|---|
| `memory` (default) | the server process | one process (`WEB_CONCURRENCY` unset or 1) |
| `file` | one JSON file per entry in `data/state/api/` (`DERKURATOR_WRITE_DIR`), written under a cross-process lock and replaced atomically | several workers on one host, or several replicas sharing one data volume |

Entries expire with the session timeout (runs, latest step), with the last token of a signed-out session (sign-outs)
or after a minute (counters). The 60-second data cache and the dependency graph stay per process but compare the
source fingerprint (size and modification time, then content) on every use, so a write of another worker is seen at
once.

Several workers are allowed only for the React front end or the API alone, with `STATE_STORE=file` and the same
`API_TOKEN_SECRET` (at least 32 characters) in every worker: `scripts/serve.py` with `FRONTEND=react` and
`WEB_CONCURRENCY=4` then starts four uvicorn workers. Otherwise `scripts/serve.py` and the API refuse to start with
`WEB_CONCURRENCY` above 1 and name what is missing. NiceGUI keeps each user's session in the process that holds its
websocket, so it always runs in one process; scale it with several replicas behind a load balancer with
sticky sessions (see `docs/PILOT_OPERATIONS.md`, "Several users at once"). The file store suits one host or a shared
volume with a few replicas; a network store (e.g. Redis or Postgres) behind the same interface is the production option
for many replicas, it is not part of the pilot.

Calculations (`POST /steering/runs`, `/assist/ask`, `/planning/scenarios/calculate`, `/planning/scenarios/compare`)
run at most `API_HEAVY_SLOTS` (default 2) at a time per process, one per user and at most `API_HEAVY_PER_MINUTE`
(default 20) per user and minute. "One per user" and the per-minute count go through the state store, so with
`STATE_STORE=file` they hold across all workers; the slots stay per process (four workers run up to four times
`API_HEAVY_SLOTS` calculations). Beyond that they answer 429 with a message and `Retry-After`; a request waits up to
20 s for a free slot. A "running" mark of a worker that died ends after 10 minutes. Sign-in has its own lockouts (per
account and globally, shared through `data/state/sign_in.json`).
