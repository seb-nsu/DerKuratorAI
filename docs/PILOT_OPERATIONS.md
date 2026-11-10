# Pilot operations handbook

One page for the team that runs a DerKurator AI pilot: what lives where, how to back it up and restore it, what to do
when the write journal reports a gap, and who gets which role. Data protection and the EU setup are in
[DATA_PROTECTION.md](DATA_PROTECTION.md).

## What lives where

All paths are below the instance's `data/` folder unless the variable in brackets moves them.

| What | Path | Notes |
|---|---|---|
| Portfolio source | `JSON_PATH`, `EXCEL_PATH`, or the files of `MAPPING_FILE` | Written only by approved proposals |
| Approved changes of a mapped source | `<mapping>.store.json` next to the mapping (or its `store:` entry) | The source wins once it moved on after the approval (import report says so) |
| ServiceNow snapshot | the snapshot folder of `SERVICENOW_MAPPING_FILE` | One `<table>.json` per table plus the sync state; rebuilt by a full sync |
| Proposals (open and decided), delegations, deadlines | `pending_changes.jsonl` | Event log, append-only |
| Saved what-ifs (A/B comparison) | `saved_scenarios.jsonl` | |
| Notification outbox (mail, Teams) | `notifications.jsonl` | Event log, append-only: events, queued messages with recipient, sent and failed attempts |
| Chats of the Steering Console | `chat_sessions/` | Archived by Reset demo data |
| Snapshots before every write | `snapshots/` (+ `manifest.jsonl`) | Newest 50 kept, snapshots of open proposals always |
| Decision log | `decision_log.jsonl` | Hash chain; Admin Console › Logs › Audit logs › Verify the hash chain |
| Business audit log | `business_audit.log` | |
| System log, incidents, LLM call log | `app.log`, `incidents.jsonl`, `llm_calls.jsonl` (`DERKURATOR_LOG_DIR`; no prompt or answer text) | For the System Admin; rotated at 5 MB (app log 3, LLM call log 5 older files) |
| Shared sign-in state | `state/sign_in.json` | Lockouts across worker processes; hashed account keys, no names |
| Shared API state (`STATE_STORE=file` only) | `state/api/` | What-if runs, sign-outs and calculation counters of the HTTP API; short-lived, expire on their own |
| Settings per stage | `env_dev.json`, `env_test.json`, `env_prod.json` | Models, business rules, stage gates, sign-in limits |
| Model registry | `supported_models.json` | |
| Archive | `archive/` | Cleared logs and reset demo data – part of every backup |
| Accounts | `AUTH_USERS_FILE` (PROD), `demo_users.json` (DEV/TEST) | Never the demo file in PROD |

## Backup and restore

**Backup:** `python scripts/backup_data.py --target <backup folder>` copies the whole `data/` folder (including the
archive of cleared logs) and the portfolio source file if it lives elsewhere into a dated folder, holds the approval
inbox while copying, checks the hash chain of the copy and keeps the newest seven (`--keep` changes that). Schedule it
daily at night (cron, Task Scheduler); exit code 1 means the hash chain of the copy is broken. The decision log must
be copied whole – a shortened file breaks its hash chain.

**Restore one write:** Admin Console › Operations › Snapshots: pick the snapshot before the write, confirm. The
current state is snapshotted first, so the restore can be undone; the dependency graph rebuilds itself. Only the
System Admin restores, and it is audited.

**Restore everything:** stop the app, put the backup of `data/` back, run `python scripts/backup_data.py --verify`
(hash chain of the restored decision log), start the app.

## When the write journal reports a gap

Admin Console › System status › Write journal lists writes whose records are incomplete. Nothing is repaired
automatically.

| Finding | Meaning | What to do |
|---|---|---|
| Write started without a committed record | A write broke off (crash, restart). The journal says whether the source changed since the snapshot | Compare the source with the snapshot of the write. Changed and wrong: restore that snapshot. Unchanged: nothing was written. Mark resolved with a comment |
| Proposal written without a decision | The data was written, the decision record is missing | Check the change in the source; record the decision with the PMO who approved, mark resolved |
| Proposal reserved for a write that never committed | An approval stopped before writing | Mark resolved – this releases the reservation and the proposal is back in the inbox for a new decision |

## Roles

| Role | Who | Notes |
|---|---|---|
| Project Manager | Each PM, user ID = their name in the portfolio | Sees only their own projects and demands |
| PMO | Two or more PMO members | Four-eyes: a PMO never decides their own proposal |
| Deputy for approvals | Only another PMO account | Granted under Approvals › Delegations for a period; decides on behalf of the absent PMO; the absent PMO's own proposals in the deputy's own name, so two PMO accounts are enough |
| Portfolio Board | The board chair (`BOARD`) | Approves the annual plan |
| System Admin | The operator (`AUTH_ADMIN_PASSWORD` or an account in `AUTH_EXTRA_USERS`) | Settings, snapshots, logs; sees no project data by design |

Accounts come from `AUTH_USERS_FILE` with password hashes (`scripts/hash_password.py`). Remove the accounts of people
who leave the pilot; their proposals stay in the log.

## Notifications (mail, Microsoft Teams)

So the PMO does not have to open the inbox to see what waits, the app can send a short message by mail or to a Teams
channel. Off by default. Events and recipients:

| Event | Goes to |
|---|---|
| New proposal waiting for a decision | The PMO (an annual plan: the Portfolio Board), never its requester |
| Proposal past the approval deadline (escalation level 1 and 2) | The same approvers |
| Proposal expired without a decision | The requester and the approvers |
| Decision: approved, rejected (an annual plan: sent back), outdated (data changed, nothing written) | The requester |

A message names the project(s), the change and the budget amount, the verdict or decision with its comment and a link
into the app (`<APP_BASE_URL>/approvals?proposal=<id>`, an annual plan `/planning`); never the decision paper or a
secret. A Project Manager's mail names only projects in their own scope; the PMO and Board channels see what those
roles see in the app.

**Setup** (in `.env` or the host's secrets, then restart or Admin Console › Reload from .env):

| Setting | Meaning |
|---|---|
| `NOTIFY_ENABLED=true` | Turns notifications on |
| `NOTIFY_SMTP_HOST`, `NOTIFY_SMTP_PORT` (587), `NOTIFY_SMTP_FROM` | Mail server and sender; port 465 uses TLS from the start |
| `NOTIFY_SMTP_USER`, `NOTIFY_SMTP_PASSWORD` | Login (the password is a secret) |
| `NOTIFY_SMTP_STARTTLS` (`true`) | STARTTLS on port 587 |
| `NOTIFY_PMO_TO`, `NOTIFY_BOARD_TO` | Role mailboxes, comma-separated |
| `NOTIFY_TEAMS_PMO_URL`, `NOTIFY_TEAMS_BOARD_URL` | Teams incoming webhook or Power Automate workflow ("When a Teams webhook request is received") per role – secrets; the message is an Adaptive Card |
| `APP_BASE_URL` | The address users open, for the links (e.g. `https://kurator.example.com`) |
| `NOTIFY_MAX_ATTEMPTS` (6) | Attempts per message before it counts as failed |

Personal mails go to the `email` of an account in `AUTH_USERS_FILE` (optional per entry;
`python scripts/hash_password.py "Jane Doe" "PMO (Full Access)" jane.doe@example.com` prints it with the entry).
Without SSO these accounts and the role mailboxes are the only source of addresses; with SSO the directory supplies
them later. Each address gets one message per event, also when an account's address is the role mailbox too.

**How it is delivered.** A decision never waits for a mail server: the event is written to the outbox after the
proposal log, and the delivery runs in the background right after it. A failed delivery is retried after 1, 2, 4, 8,
16 minutes; after the last attempt the message counts as failed. Schedule `python scripts/send_notifications.py`
every five minutes – it sends what a restart or an unreachable server left behind (exit code 1 when a delivery
failed); `scripts/approval_deadlines.py` queues and sends the escalations and expiries itself.

**Check it:** Admin Console › Operations › Notifications shows on/off, which channels are configured (never the
secrets), the outbox in counts (sent, waiting, retrying, failed) and the last error; *Send test notification* sends
one message per channel right away (audited as `notification_test_sent`, without the address). A purge removes the
user's messages from the outbox; a demo reset archives the outbox with the proposals.

## Several users at once

The app runs as one server process by default, whichever front end serves (`FRONTEND=streamlit|nicegui|react`,
started with `python scripts/serve.py` or the `start_*.bat` files; see README "Front ends"). Every browser session has
its own scenario context (no session reads another one's analysis); approvals and writes are serialized by file locks,
so two PMOs never approve the same proposal twice.
Measured 2026-10-06 on a developer machine with the demo data, offline mode: 8 sessions signing in at the same time,
first page after the sign-in in 1–3 s, a page switch in 1.3–3.7 s, no errors in the system log. With LLM agents a
request takes longer (median about 50 s per request), but sessions do not wait for each other.

**More than one process.**

| Front end | Several workers in one container (`WEB_CONCURRENCY`) | Several replicas |
|---|---|---|
| React | Yes, with `STATE_STORE=file` and one `API_TOKEN_SECRET` | Yes, on one shared data volume with the same two settings; any load balancing |
| NiceGUI | No (refused) – each user's session lives in the process that holds its websocket | Only behind sticky sessions (the same user always reaches the same replica), one data volume, `STATE_STORE=file`, one `API_TOKEN_SECRET` and one `NICEGUI_STORAGE_SECRET` |
| Streamlit | No (refused) – same reason | Only behind sticky sessions, one data volume |

With `STATE_STORE=file` the what-if runs, sign-outs and per-user calculation counters of the API are files in
`data/state/api/` (see `docs/API.md`, "Limits"), so a run started on one worker is answered by another and a
sign-out holds everywhere. Approvals, writes, the decision log and the sign-in lockouts are shared through file locks
in any case; the data cache and the dependency graph notice a write of another worker by the source fingerprint. Each
worker writes to the same `app.log`; when it rotates, a worker may keep writing into the rotated file until its next
restart, so ship logs centrally once several workers run. Many replicas on several hosts need a network store (e.g.
Redis or Postgres) instead of the file store; that is part of the production setup, not of the pilot.

Limits: in Streamlit a browser reload signs the user out (there is no session cookie before SSO); NiceGUI and React
keep the sign-in across a reload until the session timeout of inactivity. A link to a page or a project (`?project=`)
leads to the sign-in and then opens that page. The API allows two calculations (what-if, plan scenario, question box)
at a time per process, one per user and 20 per user and minute (`API_HEAVY_SLOTS`, `API_HEAVY_PER_MINUTE`); more
answer "busy, try again". A per-IP limit at the reverse proxy stays advisable.

## Container

One image serves every front end (`Dockerfile`; `FRONTEND` picks it, Streamlit by default). The data folder lives on
a volume, the secrets come from the environment, never from the image:

```bash
docker build -t derkurator .
docker run -d -p 127.0.0.1:8080:8080 --env-file .env -e FRONTEND=react -v derkurator-data:/app/data derkurator
```

- The image runs as user `kurator` (uid 10001), which owns `/app/data`. A named volume takes over that owner; a bind
  mount of a host folder must be writable for uid 10001.
- A named volume keeps its files across image updates – including `env_*.json`, `supported_models.json` and the demo
  portfolio of the first start. Changes to them in a new release are made in the Admin Console or copied in by hand.
- The health check asks `/_stcore/health` (Streamlit) or `/api/v1/health` (NiceGUI, React) every 30 s after a 30 s
  start period; `docker ps` shows `healthy` once it answers.
- The image defaults to `ENVIRONMENT=PROD`: without `AUTH_USERS_FILE` and `API_TOKEN_SECRET` Streamlit and React
  start but refuse every sign-in, and NiceGUI does not start without `NICEGUI_STORAGE_SECRET`. For a demo, pass
  `-e ENVIRONMENT=TEST`.
- Verified 2026-10-07 (Docker 29, image built from this Dockerfile): for each of `streamlit`, `nicegui` and `react`
  the container became `healthy`, the sign-in page answered HTTP 200 and the demo account `Priya Raman` signed in and
  saw the Overview in Chromium. With `FRONTEND=react`, `WEB_CONCURRENCY=2`, `STATE_STORE=file` and an
  `API_TOKEN_SECRET`, one container ran two workers: six what-ifs, 24 report reads, a proposal and a sign-out spread
  over both without a "run not found"; NiceGUI with `WEB_CONCURRENCY=2`, and React without the file store, refused to
  start with the reason. CI repeats the build and the three starts in the job "Container image"
  (`scripts/container_smoke.py`), without pushing the image.

## Dependency graph in Neo4j (optional)

By default the dependency graph lives only in the app's memory (NetworkX). A pilot can also use the company's Neo4j:
the app then writes a copy of the graph into Neo4j on every rebuild and runs the graph queries there. The answers
are the same; if Neo4j is unreachable or fails, the in-memory graph answers and the Admin Console says so.

**What to ask the Neo4j team**

- **A database of its own** (for example `derkurator`). That needs **Neo4j Enterprise** (or Aura); Community has a
  single user database and no per-database rights. On a shared Community instance the app stays inside its own labels
  (`DerKurator_Project`, `DerKurator_Task`, `DerKurator_Team`, `DerKurator_Risk`, `DerKurator_Meta`) and deletes only
  those – workable, but every account there can read and change everything.
- **An account with rights on that database only:** read and write nodes and relationships, and create indexes and
  constraints (the app creates four indexes and one uniqueness constraint once, all named `derkurator_*`). No admin
  rights, no other databases.
- **Version:** Neo4j **5.21 or later** (5.26 LTS recommended); older versions are refused and the in-memory graph
  answers.
- **Connection:** the Bolt URI (`neo4j+s://…` with TLS, or `bolt://` inside the company network) and that the app's
  host may reach port 7687.
- Size: a few thousand nodes for a portfolio of a few hundred projects; one DerKurator installation per database.

**Switching it on** – in `.env` or the host's secrets:

```bash
GRAPH_BACKEND=neo4j
NEO4J_URI=neo4j+s://graph.example.com:7687
NEO4J_USER=derkurator
NEO4J_PASSWORD=...          # secret: never in a file of the repository, never shown or logged by the app
NEO4J_DATABASE=derkurator   # default: neo4j
```

Then Admin Console › Developer › Models, prompt & shadowing › Reload from .env and environment file
(or a restart) and Operations › Rebuild dependency graph.
Admin Console › System status › Graph backend shows the server, whether it is reachable, the node and relationship
counts (and whether they match the in-memory graph), the last sync and which backend answers. A yellow warning there
means Neo4j is configured but not used; the app retries every 30 s. Switching back: `GRAPH_BACKEND=networkx` (the
copy in Neo4j can stay or be deleted with `MATCH (n) WHERE any(l IN labels(n) WHERE l STARTS WITH 'DerKurator_')
DETACH DELETE n`).

## Regular checks

- Scheduled: `python scripts/approval_deadlines.py` every hour (escalations and expiries happen on time, not only when
  someone opens the inbox), with notifications on `python scripts/send_notifications.py` every five minutes, and
  `python scripts/backup_data.py --target <folder>` every night.
- Daily: Admin Console › System status (LLM agents active, write journal empty, data residency as agreed; with
  Neo4j: no warning under Graph backend).
- Weekly: Verify the hash chain (Logs › Audit logs); look at Incidents; Data quality page for blocked projects.
- After every key or endpoint change: Admin Console › Developer › Models, prompt & shadowing › Reload from .env and environment file, then one test request.
- ServiceNow (once connected): the sync schedule comes with the ServiceNow rollout. A scheduled
  `python scripts/servicenow_sync.py` exits with 1 when a sync failed (the snapshot then stays as it was; the reason
  is in the output and in the audit log as `servicenow_sync_failed`). A warning about `X-Total-Count` means records
  changed during the read or the instance stopped early: run the sync again, and a *Full sync* if it persists.
  Deleted records are checked once per `SERVICENOW_RECONCILE_HOURS` (default 24).
