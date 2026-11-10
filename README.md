# DerKurator AI

**A deterministic, agentic PMO digital twin for portfolio steering committees.**

DerKurator AI turns a ServiceNow portfolio export into a living model of your project portfolio. PMO leads and project
managers ask "what if" questions in plain language – *"Cut the CapEx of General Ledger Consolidation by 10 %"*,
*"Put the Plant Network Hardening on hold for 3 months"*, *"Where do we have team bottlenecks?"* – and receive an
**Executive Decision Paper** with a **GO / CONDITIONAL GO / NO-GO** verdict, quantified impact, dependency dominoes
and a print-ready SteerCo Pack.

What makes it different: **the AI never decides and never calculates.** Every number comes from deterministic Python
tools and a dependency graph; every verdict comes from a rule engine configured by the PMO. The language models only
understand the request, orchestrate the tools and explain the result.

> Version 1.6.0 (see CHANGELOG) · Python 3.14 · Streamlit, NiceGUI and React · Anthropic/OpenAI SDK · NetworkX · 1,428 automated tests

---

## The "Honest AI" paradigm

Most AI copilots let a language model produce the numbers and the recommendation – which makes them impossible to
audit. DerKurator AI separates the two jobs strictly:

| Responsibility | Who does it | Why |
|---|---|---|
| Understand the request, pick the scenario, write the narrative | **LLM agents (own agent loop on the provider SDKs)** | Language is what LLMs are good at |
| Budget, CapEx/OpEx, ROI, NPV, FTE, sprint and schedule maths | **Deterministic Python tools** | Reproducible, testable, exact |
| Dependency dominoes, successor deadlines, team bottlenecks, risk clusters | **Dependency graph (NetworkX, in memory)** | Multi-hop and cyclic relations are graph problems ([ADR-001](docs/adr/ADR-001-graph-database.md): KùzuDB replaced) |
| GO / CONDITIONAL GO / NO-GO | **Deterministic Verdict Engine** | Same facts + same rules = same verdict, every time |

How this is enforced, not just promised:

- **Facts, not prose, drive the verdict.** Tools record structured facts (budget overrun, missed successor deadline,
  negative NPV, team overload, domino size …). The verdict engine (`app/crew/verdict.py`) evaluates them against the
  governance rules in a fixed, documented order. Facts describe the *current* scenario state, so a corrective step
  (e.g. restoring a cut budget) clears an earlier NO-GO.
- **The LLM cannot overrule the engine.** If a model writes a different recommendation, it is overwritten and the
  rule-based verdict basis – rule, values and threshold – is appended to the paper.
- **Every threshold is configurable, never hard-coded.** EAC overrun NO-GO threshold (default 15 %), board approval
  threshold (€500,000), domino threshold, discount rate, FTE cost rate and more are set per environment in the Admin
  Console.
- **Uncertainty is labelled.** Qualitative estimates carry `[AI ESTIMATE]`; a missing EAC makes the overrun check
  impossible and yields CONDITIONAL GO – never a silent pass; unknown projects or unparseable requests yield
  **ANALYSIS INCOMPLETE**, never a fake NO-GO – with a request to restate it and up to three suggested project
  names from the user's own scope.
- **Quality gate before maths.** A deterministic Data Steward blocks projects with incomplete or inconsistent master
  data from quantitative simulation (the PMO can force a run; it is audited and flagged in the paper).
- **Works without any LLM.** Without API keys, a deterministic rule parser calls the very same tools and engine.

---

## Enterprise security & governance

**Sign-in per identity**
- Every identity signs in with its own account; the role comes only from the user file, never from the browser.
  Without a sign-in only the sign-in page is reachable.
- Passwords are stored as PBKDF2-SHA256 hashes (600,000 iterations) and compared in constant time. Five failed
  attempts lock an account for five minutes; 30 minutes without activity sign the session out (both configurable
  per environment); all accounts together get a budget of sign-in attempts per minute, so a flood of attempts can't
  exhaust the server. Logins, failures, lockouts, logouts and timeouts are security events – never with the password.
  Every logged value is reduced to one line (control characters replaced, 200 characters at most).
- Demo accounts are valid only in DEV and TEST; PROD refuses them.

**Separation of duties (four-eyes principle)**
- A Project Manager edits free master data of their own projects directly (name, sponsor, phase, RAG status, business
  case, …). Changes to **governed fields** – budgets, CapEx/OpEx, planned benefit, EAC, actual cost, planned end date,
  project state – are submitted as a **proposal** to the PMO inbox.
- The PMO is bound by the same rule: governed changes proposed by one PMO identity must be approved by a **second**
  PMO identity. Nobody approves their own proposal.
- Enforcement is **server-side**: every write passes `authorize()` in the data adapter (role, project ownership,
  open proposal of another person, identical change set). The UI is never trusted alone.

**Row-level security & privacy**
- Project Managers only ever see their own projects – across every linked table and in the graph. Of foreign projects
  a PM sees neither id, name, figures nor their number – only "Dependent projects outside your scope are affected –
  the PMO coordinates." The PMO sees every project. Demands follow the same rule: a PM sees the demands they own.
- The **Portfolio Board** reads the whole portfolio and approves only the annual plan; it writes nothing else, and
  nobody else can approve a plan.
- **PII masking:** names of managers, sponsors, assignees and stakeholders are pseudonymised (`[PM_MASKED]`,
  `[SPONSOR_MASKED]`, …) before any data reaches an LLM, and again in the generated paper. The graph contains no
  personal names.

**Compare-and-write with snapshots and rollback**
- Every write is a compare-and-write: if the data changed since the scenario was calculated, nothing is written and
  the proposal is marked stale.
- A byte-exact **snapshot** of the data source is taken before every write (with the change set and its inverse);
  the System Admin can restore any snapshot, and the restore itself is undoable. Snapshots referenced by an approval
  are never pruned.
- All writes, restores and log operations serialise on **cross-process locks**.

**Audit trail**
- Audit writes are blocking: if the audit record cannot be written, the data write is aborted.
- Decision log (`data/decision_log.jsonl`, decisions only), business audit trail (one line per changed field),
  incident log (`data/incidents.jsonl`, system errors with an incident id) and security events in the system log.
  Timestamps carry the UTC offset. Clearing a log archives it first.
- Every refused write or approval attempt leaves exactly one security event (user, role, project IDs, field names and
  a reason code such as `self_approval` or `foreign_project` – never values or other people's names).
- Evidence chain: every analysis gets a run id (with user, role and data version); the proposal and the apply cite
  it. A write that fails after its audit record is followed by `data_write_failed`; a write whose decision record
  fails says so ("written, audit failed", with an incident id) instead of claiming that nothing happened.
- Audit records contain the request text in plain language. PII masking applies to what is sent to an LLM and to
  the paper, not to the decision log.

**Data retention and erasure**

| Store | Content | Kept | How it is deleted |
|---|---|---|---|
| `data/decision_log.jsonl` | decisions, writes, analysis runs (incl. request text) | until cleared by the System Admin (archived first) | Admin Console → Audit logs → Clear (archive in `data/archive/`) |
| `data/snapshots/` | full copies of the data source before each write | newest 50; snapshots cited by an approval or write are never pruned | restore/prune by the System Admin |
| `data/pending_changes.jsonl` | proposals with requester and decision | indefinitely (approval evidence) | "Purge user" pseudonymises the user id |
| `data/chat_sessions/` | chat history per identity | indefinitely | "Purge user" removes them; demo reset archives them |
| `data/saved_scenarios.jsonl` | saved scenarios | until deleted by owner or PMO | "Purge user" removes the user's scenarios |
| `data/incidents.jsonl` | system errors: incident id, section, error type, role, user id | until removed on the host | "Purge user" pseudonymises the user id |
| `data/app.log` | system and security events | rotating, 4 files of 5 MB | Admin Console → System logs → Clear |
| `data/archive/` | archived logs and demo-reset stores | until removed on the host | "Purge user" pseudonymises the user id there too |

"Purge user" (Admin Console → Snapshots, System Admin only, with confirmation) handles an erasure request; the purge
itself is recorded in the decision log with the pseudonym only.

---

## Quickstart

**Requirements:** Python 3.14 (3.12 also works), Windows / macOS / Linux. No database server – the dependency graph is built in memory; Docker is optional (see "Deployment"). Optional: an existing Neo4j 5.21+ as a second graph backend (`GRAPH_BACKEND=neo4j`, see [docs/PILOT_OPERATIONS.md](docs/PILOT_OPERATIONS.md)).
```powershell
# 1. Clone and create a virtual environment
cd derkurator-ai
python -m venv .venv
.\.venv\Scripts\Activate.ps1          # macOS/Linux: source .venv/bin/activate

# 2. Install the pinned dependencies
pip install -r requirements.txt

# 3. Configure
Copy-Item .env.example .env           # macOS/Linux: cp .env.example .env
#    add at least one API key (OPENAI_API_KEY, ANTHROPIC_API_KEY or GOOGLE_API_KEY)
#    – or none at all: the app then runs in deterministic offline mode

# 4. Run
streamlit run app/main.py
```

Open **http://localhost:8501**. The app binds to localhost only and sends no usage statistics. The graph database is
built automatically on first access from the bundled test portfolio (`data/DerKurator_Demo_Portfolio.json`).

**Front ends.** Besides Streamlit there are two new front ends on the same rules and data: **NiceGUI** (Python) and
**React** (TypeScript). Both talk only to the HTTP API (`app/api`, FastAPI; routes, sign-in and limits in
[`docs/API.md`](docs/API.md)), which runs in the same process. `FRONTEND=streamlit|nicegui|react` picks the one that
serves, `python scripts/serve.py` starts it (also the container's command):

```powershell
$env:FRONTEND = "nicegui"; python scripts/serve.py    # macOS/Linux: FRONTEND=nicegui python scripts/serve.py
```

NiceGUI and React answer on **http://127.0.0.1:8080** (`HOST`/`PORT` change it), Streamlit keeps 8501. React needs
Node.js 22 once to build (`cd frontends/react; npm ci; npm run build`). On Windows the files `start_streamlit.bat`,
`start_nicegui.bat` and `start_react.bat` in the repository root do all of this and open the browser. The server runs
in one process; only React may run several workers (`WEB_CONCURRENCY`), with `STATE_STORE=file` and an
`API_TOKEN_SECRET` (see `docs/API.md`, "Limits"). The container image (`Dockerfile`, `docker-compose.yml`) serves each
of the three front ends; how to run it and what was verified is in `docs/PILOT_OPERATIONS.md`, "Container". In PROD
set `API_TOKEN_SECRET` and, for NiceGUI,
`NICEGUI_STORAGE_SECRET` (each at least 32 characters). What each front end covers, and the rules for keeping them in
step, are in [`docs/FRONTEND_PARITY.md`](docs/FRONTEND_PARITY.md).

**Sign in.** In DEV and TEST the sign-in page lists the demo accounts (`data/demo_users.json`) with their public
demo password `kurator-demo`; a button there signs you in as that account. To switch identity, sign out and sign in
again. The navigation follows the role (one list for all three front ends, `app/views/navigation.py`): the PMO has
Overview, Projects, Steering Console, Approvals, Planning and Rules; a project manager has Overview, My projects,
Steering Console, My proposals and My demands. Data quality opens from Projects, the What-if comparison from the
Steering Console. A project's detail page has its own link (`?project=<project number>`), opened only within the
reader's scope.

| Account | What you see |
|---|---|
| An admin account you create (see below) | Admin Console only – models, keys, snapshots, logs, system tests. No portfolio data. |
| `PMO-ADMIN` (PMO Head) / `PMO-DEPUTY` | Whole portfolio, approval inbox, scenario simulation, forced simulation, annual planning; the Rules page (business rules, required fields), which only the PMO Head (`"pmo_head": true` in the user file) changes |
| `BOARD-CHAIR` (Portfolio Board) | Planning page: approves or sends back the annual plan, ranking, saved plan scenarios, board pack. Approvals page: only its own plan decisions (*Mine*); the plan in force shows on the planning page |
| A project manager's name, e.g. `Priya Raman` | Only their own projects; free master data editable, governed changes as proposals |

**Real accounts** (required in PROD): create one entry per person with `python scripts/hash_password.py "<user>"
"<role>"` (the password is asked without echo), collect them in a JSON file `{"users": [...]}` outside the repo and
set `AUTH_USERS_FILE` to its path. A project manager's user ID must equal their name in the portfolio.

**Admin account.** The demo accounts include `DEMO-ADMIN` (one click on the sign-in page), so reviewers of the hosted
demo can open the Admin Console. It never shows a stored API key, but every visitor can change models, rules and demo
data – put no API key into the Secrets of a public demo. For a private admin, the simple way: set `AUTH_ADMIN_PASSWORD`
(at least 12 characters) in `.env` or in the Secrets and sign in as `ADMIN` with that password (another user id via
`AUTH_ADMIN_USER`). The password is hashed in memory and never written to a file. Alternatively create a hashed entry
with `python scripts/hash_password.py "<your admin id>" "System Admin"` and set
`AUTH_EXTRA_USERS` to the file path or to the JSON `{"users": [ ... ]}` itself (on Streamlit Community Cloud: a line
in the app's Secrets). The extra accounts are merged with the demo accounts and must not be demo entries. PROD
refuses the demo accounts; the Admin Console refuses to switch to PROD while they are active.

**Run the tests**

```powershell
pytest                    # full suite, no network, no real API keys
python run_tests.py       # same, plus HTML report in data/test_report.html (also runnable from the Admin Console)
```

**Golden verdict cases.** `tests/test_verdict_golden.py` runs 36 fixed scenarios
(`tests/golden/verdict_cases.json`) on a synthetic portfolio (`tests/golden/portfolio_golden.json`, independent of
`data/`) with a fixed reference date, through the real pipeline without parser or LLM. Together they hit every
verdict rule and every guided scenario type, and cover every branch of `compute_verdict`. Each case checks the exact
conditions; ten budget and schedule cases also check EAC, new budget, overrun and ROI against values calculated by
hand. They are the regression net of the business logic: a changed rule, formula or threshold turns several cases
red.

**Development**

```powershell
pip install -r requirements-dev.txt   # runtime dependencies plus ruff and pytest-cov
ruff check .                          # bug classes, security, timezone and noqa rules (pyproject.toml); no auto-format
pytest --cov                          # coverage report; fails below the lower bound in pyproject.toml
python run_tests.py --cov             # the HTML report with coverage
```

A **dev container** (`.devcontainer/devcontainer.json`, e.g. for GitHub Codespaces) uses Python 3.14 like CI, installs
`requirements.txt` and starts the app in TEST and offline mode with the default security settings.

The UI uses a light theme by design (`.streamlit/config.toml`); it does not follow the browser's dark mode.

A GitHub Actions pipeline (`.github/workflows/ci-cd-pipeline.yml`) runs on Python 3.14 for every pull request to and
push to `main`: ruff and the full suite with coverage (overall lower bound 84 %, at least 80 % of the lines a pull
request changes; `verdict.py` 100 %, `proposals.py` 90 %, `security.py` 93 %), the Neo4j parity tests, the React type
check and build, the Playwright end-to-end tests against NiceGUI and React, and the **container image**: it is built and
started once per front end, and inside it the smoke test (`scripts/smoke_test.py`) starts the app headless in TEST and
offline mode, checks health, the sign-in page and a demo sign-in, and logs no traceback.

To save Actions minutes, documentation-only changes and draft pull requests use no minutes, a pull request runs the
Neo4j, React, end-to-end and container jobs only when their files changed, and a push to `main` (the merge of an
already tested pull request) runs only the suite and the container image. *Actions > CI/CD Pipeline > Run workflow*
starts all jobs by hand.

## Deployment (demo on Streamlit Community Cloud)

The hosted demo runs in **TEST** and **offline mode** – no API key, no LLM costs. Data changes (approvals, edits) live
only in the running instance and reset on restart. Steps that only the repository owner can do:

1. Connect [Streamlit Community Cloud](https://share.streamlit.io) to the GitHub account.
2. Create the app: repository `<your-account>/DerKurator-AI`, branch `main`, file `app/main.py`, Python 3.12 (Advanced
   settings).
3. Secrets (App settings → Secrets, never in the repo):
   ```toml
   ENVIRONMENT = "TEST"
   FORCE_OFFLINE_MODE = "true"
   ```
4. Optional: add your own admin account as `AUTH_ADMIN_PASSWORD = "…"` in the Secrets (see "Admin account" above).
   The demo accounts already include `DEMO-ADMIN` for reviewers.
5. Share the URL with the reviewers. The sign-in page shows the demo accounts. A private repository makes the app private
   by default; then invite the viewers by e-mail, or make the app public on purpose.
6. Before a demo session, open the app once: Community Cloud puts apps to sleep after 12 hours without traffic.

The demo is shared: all visitors who sign in with the same demo account see the same inbox, chats and scenarios. The
application log is also written to the console, so on Community Cloud it appears in the app's log as well, visible
to the repository owner.

A live LLM demo needs its own API key with a hard spend limit in the provider console – a product-owner decision.

## Deployment (demo on Render, Blueprint)

`render.yaml` is a [Render Blueprint](https://render.com/docs/blueprint-spec): Render builds the `Dockerfile` and
serves the demo over HTTPS – no own server, domain or proxy. It runs in **TEST** and **offline mode** like the other
demos.

1. Push the branch with `render.yaml` to GitHub.
2. [Render Dashboard](https://dashboard.render.com) › **New** › **Blueprint** › connect the GitHub account (private
   repositories work through Render's GitHub app) › repository `<your-account>/DerKurator-AI`, branch with `render.yaml`.
3. Apply; the first build takes a few minutes. The URL is shown at the service (`https://derkurator-demo.onrender.com`
   or similar).
4. Optional: service › **Environment** › `AUTH_ADMIN_PASSWORD` for your own admin account. Never add API keys.

Every push to that branch deploys again (`autoDeployTrigger: commit`). On the **free** plan the service sleeps after
15 minutes without traffic (the next visit waits about a minute; open it before a demo session) and its file system
is ephemeral: a spin-down, restart or deploy brings the demo back to the bundled portfolio. A paid plan
(`plan: 0.5c-512mb`, or `1c-2g` for more memory) has no sleep. `FRONTEND: nicegui` or `react` switches the front end;
change `healthCheckPath` to `/api/v1/health` with it.

## Deployment (public demo with Docker)

`docker-compose.demo.yml` runs the same demo on any host with Docker (a small VPS is enough): the app in **TEST** and
**offline mode** behind [Caddy](https://caddyserver.com) (`deploy/Caddyfile`), which fetches the HTTPS certificate
from Let's Encrypt itself. Only Caddy is reachable from outside (ports 80 and 443); the app container publishes no
port.

1. Point a DNS A/AAAA record (e.g. `kurator-demo.example.com`) at the host; open ports 80 and 443.
2. On the host:
   ```bash
   git clone https://github.com/<your-account>/DerKurator-AI.git && cd DerKurator-AI
   cp .env.demo.example .env.demo      # set DEMO_DOMAIN; FRONTEND and AUTH_ADMIN_PASSWORD optional
   docker compose -f docker-compose.demo.yml --env-file .env.demo up -d --build
   ```
3. Open `https://<DEMO_DOMAIN>`; the sign-in page shows the demo accounts (password `kurator-demo`).

| Task | Command (with `-f docker-compose.demo.yml --env-file .env.demo`) |
|---|---|
| Logs | `docker compose … logs -f derkurator-demo` |
| Back to the bundled portfolio (all demo changes gone) | `docker compose … up -d --force-recreate derkurator-demo` |
| Update to the latest code | `git pull && docker compose … up -d --build` |
| Stop | `docker compose … down` |

- `.env.demo` holds no API keys and is never committed; never pass your development `.env` to a public demo.
- The container has no volume on purpose: a restart keeps the changes, a recreate drops them. Within a running
  instance the Admin Console's **Reset demo data** does the same (needs `AUTH_ADMIN_PASSWORD`).
- `FRONTEND=nicegui` or `react` switches the front end; all three serve on the same address.
- One replica only (what-if runs and the dependency graph are held per process).
- Local test without a domain: `DEMO_DOMAIN=localhost` (Caddy's own certificate, the browser warns once).
- The demo password is public; for a closed reviewer group enable `basic_auth` in `deploy/Caddyfile`. Caddy sends
  `noindex`, so search engines do not list the demo.

The internal hosting template (`docker-compose.yml`, PROD with real accounts behind the company proxy) is separate.

### Configuration (`.env`)

| Variable | Purpose |
|---|---|
| `ENVIRONMENT` | Stage `DEV` / `TEST` / `PROD` (default `DEV`) – selects `data/env_<stage>.json` |
| `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY` | Provider keys; missing keys → offline mode |
| `GEMINI_API_KEY` | Alias for `GOOGLE_API_KEY` (used if `GOOGLE_API_KEY` is not set) |
| `ORCHESTRATOR_MODEL`, `SIMULATOR_MODEL`, `ANALYST_MODEL`, `ADVISORY_MODEL` | Default model per agent (see `data/supported_models.json`; changeable per environment in the Admin Console) |
| `DATA_SOURCE` | `json` (default), `excel`, `mapped` (any source through a mapping file, see `docs/DATA_MAPPING.md`) or `servicenow` (Table API into a local snapshot) |
| `JSON_PATH`, `EXCEL_PATH` | Path to the ServiceNow export |
| `MAPPING_FILE` | Mapping file for `mapped` (default `data/mappings/servicenow_demo.yaml`) |
| `SERVICENOW_INSTANCE`, `SERVICENOW_USER`, `SERVICENOW_PASSWORD` or `SERVICENOW_TOKEN` | ServiceNow access for `servicenow` – secrets only, read access to the SPM tables is enough |
| `SERVICENOW_MAPPING_FILE` | Mapping for `servicenow` (default `data/mappings/servicenow_spm.yaml`) |
| `AUTH_ADMIN_PASSWORD`, `AUTH_ADMIN_USER` | Simple admin account (password ≥ 12 characters, user id default `ADMIN`) |
| `API_TOKEN_SECRET` | Signing secret of the HTTP API tokens (≥ 32 characters; required in PROD, see `docs/API.md`) |
| `FRONTEND` | `streamlit` (default), `nicegui` or `react` for `python scripts/serve.py`, the `start_*.bat` files and the container (see `docs/FRONTEND_PARITY.md`) |
| `STATE_STORE`, `WEB_CONCURRENCY` | Where the API keeps what-if runs, sign-outs and calculation counters: `memory` (default, one process) or `file` (`data/state/api/`, shared by several workers or replicas on one volume); number of server processes, above 1 only for React with `file` (see `docs/API.md`, "Limits") |
| `NICEGUI_STORAGE_SECRET`, `NICEGUI_STORAGE_PATH` | NiceGUI front end: cookie signing secret (≥ 32 characters, required in PROD) and the folder of its sessions, which hold API tokens (default `data/nicegui`) |
| `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_EXPORTER_OTLP_HEADERS`, `OTEL_SERVICE_NAME` | Optional OpenTelemetry export (OTLP/HTTP) of spans and metrics to the company's collector – no prompt or answer text. Without it, every LLM call is still logged to `data/llm_calls.jsonl` |
| `GRAPH_BACKEND` | `networkx` (default, in memory) or `neo4j`: the dependency graph is also written into Neo4j and queried there, with the in-memory graph as fallback (see `docs/PILOT_OPERATIONS.md`) |
| `NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD`, `NEO4J_DATABASE` | Neo4j access for `neo4j` – the password only in `.env` or the secrets; database default `neo4j`, a database of its own is preferred |
| `FORCE_OFFLINE_MODE` | `true` forces the deterministic mode even with keys present |
| `LLM_DATA_RESIDENCY` | `any` (default) or `eu`: only providers with an own endpoint are called, the rest count as missing (see `docs/DATA_PROTECTION.md`) |
| `ANTHROPIC_BASE_URL`, `OPENAI_BASE_URL` | Own endpoint per provider: an EU gateway, Azure OpenAI (v1 API) or a local OpenAI-compatible model server |
| `NOTIFY_ENABLED`, `NOTIFY_SMTP_*`, `NOTIFY_PMO_TO`, `NOTIFY_BOARD_TO`, `NOTIFY_TEAMS_PMO_URL`, `NOTIFY_TEAMS_BOARD_URL`, `APP_BASE_URL` | Optional notifications by mail or Microsoft Teams about the approval inbox (off by default; password and webhook URLs are secrets) – see `docs/PILOT_OPERATIONS.md` › Notifications |

Models are whitelisted in `data/supported_models.json` (the only model registry – new models are added in the Admin
Console after a successful live test call).

---

## What you can do

- **Budget scenarios** – change, cut or transfer budget (total, CapEx, OpEx) with ROI recalculation and guardrails
  (CapEx below capitalised cost, budget below actuals, …).
- **Schedule scenarios** – delay or accelerate; agile forecasts from story-point velocity, waterfall from remaining
  effort; every slip checks successor **required-by deadlines** and re-values the benefit timing (**NPV**).
- **Portfolio manoeuvres** – stop a project (sunk cost, CapEx impairment risk, released budget, task domino), put a
  project **on hold** (standby capacity, restart ramp-up), or build a prioritised **portfolio cut list** to reach a
  savings target.
- **Resource moves** – shift FTE between projects with team overload detection, or capacity of one **skill**
  (e.g. SAP FI) between the teams of two projects; the skill view shows bottlenecks a team average hides.
- **PMO actions** – **resume** a paused project (restart date, returning team, ramp-up), a portfolio-wide **cost
  stress test** ("what if staff costs rise by 8 %?" – nothing is written), **re-baselining** with baseline history
  and board threshold, **stage gates** with configurable mandatory and advisory criteria, a **management reserve**
  to draw from and return to (balance, floor, ledger), and the **response to a risk** (Mitigate / Accept / Avoid /
  Transfer) with exposure before and after.
- **Portfolio steering** – a **benefit realisation review** after go-live (actual benefits are reported in the
  project's Benefits tab), amounts in **several currencies** (converted with `Exchange_Rates`), a **strategy score**
  from weighted strategic goals with a **value-optimal cut list** next to the ranked one, and **merging** one project
  into another.
- **Annual and multi-year planning** – demands (initiatives that are no project yet) with quarterly figures and
  capacity, a weighted assessment and a deterministic rank; planning scenarios that fill the budget envelopes and
  team capacity of the next three years after what running projects already bind, with a rule-based verdict, a
  reason for every Out (limit, area, year, amount), forced in / exclude / move, a comparison of up to three scenarios,
  budget traffic light and capacity heatmap, a value-optimal plan as a second proposal with what it does differently
  from the rank order and why; the Portfolio Board approves the plan, an approved demand becomes a
  project, and the board pack is an Excel workbook or a print page. Planning questions work in the chat too
  (*"What drops out in 2027 at −10 %?"*).
- **Planning analysis** – per demand its whole dependency chain (what it needs first, what waits for it, directly
  and indirectly), what a delay of one to four quarters pushes along and whether each successor still fits its latest
  start, and – for the PMO and the Board – its decision in the base plan and its money and people next to the budget
  envelopes and team limits. An Analysis tab shows the hot spots of the whole plan (envelopes and team quarters used
  to 90 % or more) and the demands others wait for. A Project Manager sees only their own demands; links outside
  their scope are a neutral hint, never named or counted.
- **Operations** – a pilot handbook (`docs/PILOT_OPERATIONS.md`) and the data protection overview with the EU setup
  (`docs/DATA_PROTECTION.md`).
- **Ask about the page** – a question box on the project detail, the overview (portfolio briefing), the planning
  pages (demands, a demand, the plan, a scenario), every report and inbox proposal, the A/B comparison and the data
  quality page. The code collects the facts the page shows the reader; the model answers only from them (masked
  names, at most ~180 words, labelled as AI) and never gives a verdict of its own. A question that asks for a change
  ("cut … by 10 %") gets a button that hands it to the Steering Console. Without a key or in offline mode the answer
  is a digest of the matching facts. Each question is audited without its text.
- **Decision layer for existing tools** – the PMO's "Decision insights" page: projects reported green whose numbers
  say red, the portfolio with probabilities (Monte Carlo over reference classes), the review of past decisions with
  a hit rate, an audit pack per approved decision; projects from several sources at once, tasks from Microsoft
  Planner and monday.com, and (switched on) approved changes back to ServiceNow. `scripts/rule_lab.py` shows which
  verdicts a threshold change would flip before it is saved. See `docs/DATA_MAPPING.md`.
- **Any data source** – a YAML mapping connects a JSON file, an Excel workbook or a CSV folder with other column
  names, values and units; an import report shows what was mapped and what is missing. **ServiceNow** directly:
  the SPM tables through the Table API into a local snapshot, later syncs read only the changes.
- **Approval governance** – approval deadlines with escalation and expiry, withdrawing an own proposal and a
  **deputy** for the approval right during an absence – only another PMO account, since a deputy sees the whole
  inbox (four-eyes still applies).
- **Continued scenarios** – "…and now also cut project B by 5 %" builds on the manipulated data of the previous step.
- **A/B comparison** – save scenarios and compare verdicts, budget impact and change sets side by side.
- **Report viewer** – every analysis opens on one screen: verdict, status and actions in the header, key figures, then
  tabs for summary (rule-based reasons, conditions), decision paper by section, changes, impact, dependencies and
  data quality. The PMO approval inbox uses the same viewer.
- **Project detail** – a page per project with master data, status history, cost S-curve against budget and EAC,
  deadlines, dependencies, risks with their exposure, baseline history, data-quality findings and the edit form;
  shortcuts open the stage-gate check, a risk response or a re-baseline in the scenario builder.
- **SteerCo Pack** – one-page, print-ready executive summary (browser "Print → Save as PDF").
- **Approvals with override** – a proposal whose rule-based verdict is NO-GO or incomplete can be approved only with
  an explicit "Override NO-GO verdict" and a reason; the override is enforced in the backend and recorded in the
  proposal log, the audit trail and the security log. The requester can neither approve nor reject their own
  proposal.
- **Write journal** – every write leaves an intent and a committed record with one operation id; an approval reserves
  its proposal before the write, so no second PMO can approve it in parallel. Admin Console › System status lists any
  gap (a write without its committed record, a written proposal without a decision) for a person to check.

**Clean demo start:** the repository ships no proposals, chats or saved scenarios – the Approvals page starts empty,
and runtime files (`data/pending_changes.jsonl`, `data/chat_sessions/`, `data/snapshots/`, locks) are git-ignored. In
DEV and TEST, Admin Console › Operations › Snapshots offers **Reset demo data**: the portfolio goes back to its first
snapshot (the state before the instance's first write), proposals, chats and saved scenarios are archived to
`data/archive/`, and the reset is audited. It does not exist in PROD.

---

## Evaluation

The language layer is measured, not claimed: 119 cases (`evals/cases.jsonl`) – the PRD reference cases E1–E13,
delays and portfolio cuts, paraphrases, German, vague, multi-part and multi-step requests, prompt injections, access
attempts, a demo case for every verdict rule, the four-eyes approval flows, the PMO actions and annual-plan questions
(plus everyday questions with planning words that must stay with the agents) on the demo portfolio. They run through
the deterministic offline parser and the LLM pipeline. Expected values come from executing each case's exact intended
plan, never from the parser under test (see [`evals/README.md`](evals/README.md)).

```powershell
python scripts/run_evals.py --mode offline             # no network, no keys – also enforced by pytest
python scripts/run_evals.py --mode both --repeat 3     # adds the LLM pipeline (needs the API keys)
```

Latest published runs, both on 2026-10-03 for the 70 cases of v1.5.2 (orchestrator `claude-sonnet-5`, other agents `claude-haiku-4-5`):

- **Offline, all 70 cases** (v1.5.2) – [`docs/evals/EVAL_REPORT.md`](docs/evals/EVAL_REPORT.md)
- **LLM, the 29 hardest requests × 3 repeats** (v1.5.1) –
  [`docs/evals/EVAL_REPORT_LLM_2026-10-03.md`](docs/evals/EVAL_REPORT_LLM_2026-10-03.md)

| | Offline parser (60 requests) | LLM pipeline (29 hard requests × 3) |
|---|---|---|
| Verdict accuracy | 80 % | **91 %** |
| Plan accuracy (action, project, key parameter) | 73 % | 80 % |
| Conditions complete | 83 % | **100 %** |
| Wrong GOs (a NO-GO or incomplete request answered GO) | **0** | 2 – closed in v1.5.2 |
| Same verdict across the 3 repeats | – | 83 % |
| Time to verdict (median) | 0.5 s | 22 s |
| Duration (median / p95) | 0.5 s / 0.5 s | 49 s / 90 s |
| LLM cost per request | – | $0.049 |

The LLM sample is deliberately hard: paraphrases, German, vague and multi-part requests. On the six paraphrases P1–P6
the parser alone reaches the right verdict once, the LLM in 16 of 18 runs; on the four German requests it is 2 of 4
against 9 of 12. On clear requests both modes are at 100 %, and so are
the demo cases, the approval flows and the prompt injections offline. The 10 cases marked `pytest` are counted from
their covering tests and reported apart from the headline.

**The two wrong GOs.** In one of three runs the LLM read "Trim … by a fifth" and "Take €370k out of …" as budget
increases. The direction guard did not know these phrasings, so the increase passed and the rule engine correctly
judged the increase as GO. v1.5.2 adds the missing cut words (English and German) and a regression test that replays
both requests with a simulator that raises the budget; the pipeline now asks back instead of answering. The fix is
verified offline; the LLM run has not been repeated since.

Any wrong GO is listed at the top of the report and makes the runner exit with a non-zero code.

## Production roadmap

v1.x is a single-tenant pilot release with local accounts (PBKDF2). The path to production:

1. **Entra ID SSO** – replace the local accounts with Microsoft Entra ID (OIDC, e.g. `st.login`); roles (Project
   Manager, PMO, System Admin) come from group claims, never from the client.
2. **Headless architecture** – the governance core already runs behind a **FastAPI** service with **NiceGUI** and
   **React** front ends (see "Front ends" above). Open: making React the default once parity and the end-to-end tests
   hold, and a network store (e.g. Redis or Postgres) for runs and sign-outs once replicas run on several hosts (one
   host or one shared volume already works with `STATE_STORE=file`).
3. **Live enterprise data** – switch from file exports to the ServiceNow / Jira **MCP connectors** (already
   configurable in the Admin Console, deliberately not yet wired into the agents).
4. **Tamper-evident audit** – hash-chained audit records and central log shipping (SIEM).

See [`ARCHITECTURE.md`](ARCHITECTURE.md) for the system design and [`CHANGELOG.md`](CHANGELOG.md) for the version
history.
