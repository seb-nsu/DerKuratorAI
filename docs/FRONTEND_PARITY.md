# Front ends: rules and parity list

DerKurator AI has two front ends on the same rules and data (the former Streamlit front end has been removed):

- **NiceGUI** (`frontends/nicegui`, Python), the default of `python scripts/serve.py`.
- **React** (`frontends/react`, TypeScript), the default of the container image.

`FRONTEND=nicegui|react` picks the one that serves (`python scripts/serve.py`, also in the container).
Both talk only to the HTTP API (`app/api`, see `docs/API.md`). Neither holds business logic: approvals,
verdicts and row-level security run only behind the API.

## When React must follow

1. **The API is the only contract.** `frontends/react/openapi.json` is generated from the API
   (`python scripts/export_openapi.py`) and the React types from it (`npm run gen:api`).
   - `tests/test_api_contract.py` fails while the file is stale or a route has no typed answer.
   - CI rebuilds the types and the React app, so a contract change breaks the React build until its pages are adapted.
   - React must therefore be updated with every contract change.
2. **Parity list per page (below).** A new or changed page is built in NiceGUI first and accepted there. React
   follows by the next release at the latest.
3. **Switching to React** requires a complete parity list and the same Playwright end-to-end tests green against both
   front ends.
4. **Formatting lives behind the API where a table shows it as is.** The Approvals cells (Verdict with its shape
   symbol, Budget Δ, Submitted) come as `verdict_label`, `budget_delta_label` and `submitted_label` from
   `app/views/approvals.py`, so a format fix is made once. The remaining front-end copies
   (`eur` on KPIs and the project table, `cell`) move behind the API step by step, starting with the next page that
   changes anyway.

## Parity list

| Page | NiceGUI | React | API |
|---|---|---|---|
| Sign-in (centred card, demo accounts, deep link back) | ✓ | ✓ | ✓ |
| Page frame (navigation per role, scope line, reload, account, dark mode, phone menu; sign-out ends the session on the server, the token is renewed while the user works) | ✓ | ✓ | ✓ (`/auth/refresh`, `/auth/sign-out`) |
| Overview | ✓ | ✓ | ✓ (`own_view`, open items with Due/Deadline columns) |
| Steering Console (guided + free text, scenario memory off by default, forced simulation, history with Clear history, report viewer with tabs and charts, save / submit / apply with override, exports per report; deep link `?action=&project=`) | ✓ | ✓ | ✓ |
| Approvals (inbox with confirm/override/reject, decided + revert, mine + withdraw, delegations, deputy banner, SLA, escalations, ?proposal= deep link) | ✓ (proposal detail in the shared report viewer with exports and "Ask about this report") | ✓ (same) | ✓ |
| Projects + project detail (filters, ?project=&tab= deep link, all tabs, edit form, report benefit, console buttons) | ✓ | ✓ | ✓ (benefit `default_month`) |
| What-if comparison (A/B with deep link `?a=&b=`, charts, differences, delete) | ✓ | ✓ | ✓ |
| Planning (all sections per role, `?tab=`/`&demand=` deep links, up to three calculated scenarios as client state, question-box prefill) | ✓ | ✓ | ✓ |
| Data quality | ✓ | ✓ | ✓ |
| Navigation per role (`app/views/navigation.py`: PMO Overview, Projects, Steering Console, Approvals, Planning, Rules; PM Overview, My projects, Steering Console, My proposals, My demands; Data quality and What-if comparison as sub-pages with their own URL, opened from Projects and the Steering Console; a PM's scope line without the portfolio total) | ✓ | ✓ | ✓ (`parent` of a page, `scope.total` null for a PM) |
| Decision insights (PMO sub-page of the overview: status check of the sources, portfolio with probabilities, reference class, decision review with audit pack, ServiceNow transfers) | ✓ | ✓ | ✓ (`/insights/*`) |
| Rules (business rules and required fields; every PMO reads, only the PMO Head changes, reset with confirmation) | ✓ | ✓ | ✓ (`/governance/*`, `pmo_head` of the workspace user) |
| Admin Console (System status, Operations, Logs, Developer; all tabs, confirm dialogs, deep link `?tab=<area>.<section>`, sign-in again after an environment switch) | ✓ | ✓ (shadow results as plain text, not Markdown) | ✓ |
| Admin Console › Operations › Notifications (on/off, configured channels without secrets, outbox counts and last error, send test notification) | ✓ | ✓ | ✓ (`/admin/notifications`, `/admin/notifications/test`; channel table as `channels`) |
| Admin Console › System status › Graph backend (configured and active backend, Neo4j reachable, node/relationship counts, last sync, fallback warning; read-only) | ✓ | ✓ | ✓ (`graph_backend` of `/admin/status`) |
| Question box on every page | ✓ (`frontends/nicegui/ask_box.py`; hand-over to the Steering Console and Planning picked up there) | ✓ (`src/components/AskBox.tsx`; same) | ✓ (verdict `lead_reason`; pages `project`, `compare`, `demands`, `scenario`) |

## End-to-end tests

`tests/e2e` holds the Playwright suite that rule 3 refers to. Every test runs against **both** front ends (NiceGUI and
React) with the same steps and selectors: visible labels, headings and ARIA roles, no front-end-specific hooks.
It covers sign-in (demo accounts, wrong password, deep link back), navigation per role, the PM's project scope,
Steering Console verdicts, the four-eyes flow in Approvals (approve with confirmation, reject with a comment, own
proposal locked), the light/dark switch, the 390 px phone layout, the whole navigation next to the header buttons at
1024–1536 px (between 1024 and 1279 px both frames drop the navigation icons, below 1536 px the account name),
Planning for the Board and the Admin Console.

```bash
pip install -r requirements-dev.txt          # includes playwright
python -m playwright install chromium        # once; or PLAYWRIGHT_CHROMIUM=/path/to/chrome
(cd frontends/react && npm ci && npm run build)
pytest -m e2e                                # the default `pytest` run excludes the suite
```

- The fixture starts `scripts/serve.py` once per front end (`FRONTEND=nicegui|react`, ports 8720–8739) on a temp
  copy of the demo portfolio. Proposals, decisions and logs go to a temp folder (`DERKURATOR_WRITE_DIR`,
  `DERKURATOR_LOG_DIR`), so `data/` is not changed. The server runs offline with no API keys, and its clock is on the
  demo's reference date (`scripts/run_evals.EVAL_AS_OF`), so the verdicts are stable. The admin account gets a
  password generated for that run (`AUTH_ADMIN_PASSWORD`).
- Without Chromium or without the React build, the tests are skipped. `E2E_REQUIRED=1` makes them fail instead.
  `E2E_ARTIFACTS=<folder>` saves screenshots of failed tests.
- CI runs the suite in the `e2e` job, which builds React, installs Chromium and runs `pytest -m e2e`.
