# Data protection and LLM operation in the EU

What leaves the application, where it goes, and how to keep every LLM call inside the EU. For the data protection
officer of a pilot customer and for the operator who sets up the instance.

## What is sent to an LLM provider

| Call | What it contains | Names |
|---|---|---|
| Orchestrator, Simulator, Analyst, Advisory (Steering Console) | The request, a compact portfolio table (ID, project name, RAG, budget, actuals, EAC, CapEx/OpEx, ROI, end date, FTE, utilisation, data-quality flag – at most 25 rows), the tool results of the scenario, the rule-based verdict | Masked |
| Shadow crew (optional, Admin Console) | The same as above, to alternative models | Masked |
| Data quality interpretation (Data quality page) | Counts, rule names and missing fields of the Data Steward findings, IDs of blocked and most affected projects | No names |
| Page assistant ("Ask about …") | The question and the facts of the page the reader sees (their scope only) | Masked |

Not sent: passwords, keys, the audit trail, the full dataset, projects outside the reader's scope. The verdict is
computed in code; no model decides.

**Masking:** before a call, real names of project managers, sponsors, assignees, stakeholders and e-mail addresses
are replaced by placeholders (`[PM_MASKED]` …); the answer is masked again before it is shown. The switch is in the
Admin Console (on by default; the System status shows a red warning when it is off).

**Offline mode:** without a key, with `FORCE_OFFLINE_MODE=true`, or when the residency setting blocks every
configured provider, nothing is sent: a deterministic parser and rule-based texts answer.

## Keeping every call in the EU

Set in `.env` or in the secrets of the hosting platform:

```
LLM_DATA_RESIDENCY=eu
ANTHROPIC_BASE_URL=https://<your EU gateway for Claude>
OPENAI_BASE_URL=https://<your-resource>.openai.azure.com/openai/v1   # or a local model server
```

With `LLM_DATA_RESIDENCY=eu` the application calls a provider only when an own endpoint is configured for it.
A model of any other provider counts as "missing" – its agent falls back to the offline mode, and the Admin Console
› System status names the missing endpoint. Google models have no endpoint setting and are never called under `eu`.
An unknown value of `LLM_DATA_RESIDENCY` is read as `eu`; in PROD a missing value is read as `eu` too (set
`LLM_DATA_RESIDENCY=any` there only on purpose). The PROD model set (`data/env_prod.json`) uses Claude models, which run
through `ANTHROPIC_BASE_URL`.

Options for the endpoint:

| Option | Setting | Note |
|---|---|---|
| Claude through an EU gateway | `ANTHROPIC_BASE_URL` | A gateway that speaks the Anthropic Messages API and forwards to an EU region (e.g. a proxy in front of Amazon Bedrock or Google Vertex AI in an EU region) |
| Azure OpenAI in an EU region | `OPENAI_BASE_URL` = the resource's v1 endpoint, `OPENAI_API_KEY` = the Azure key | Register the deployment names as models in the Admin Console (provider `openai`) |
| Local model on own servers | `OPENAI_BASE_URL` = the OpenAI-compatible server (vLLM, Ollama …), `OPENAI_API_KEY` = any non-empty value | Register the model in the Admin Console; quality and speed depend on the model – run the offline and a small live evaluation first |

**What the app cannot check:** the region of an endpoint. `LLM_DATA_RESIDENCY=eu` is the operator's statement that
the configured endpoints process in the EU; the contract with the provider (data processing agreement, region,
retention, no training on inputs) has to confirm it.

## Stored by the application

Everything stays on the instance (folder `data/` unless configured otherwise): the portfolio source or its local
snapshot, proposals, saved scenarios and chats, snapshots before every write, the decision log (hash chain), the
business audit log, the system log, incidents, and the shared sign-in state (hashed account keys, no names). The
page assistant's questions are audited without their text. See [PILOT_OPERATIONS.md](PILOT_OPERATIONS.md) for the
paths, backup and retention.

**Notifications (optional, off by default):** with mail or Teams on, the outbox (`data/notifications.jsonl`) keeps each
message with its recipient address, and the message leaves the instance through the configured mail server or Teams
webhook: project names, the change and amount, the verdict or decision and a link – never the decision paper. A
Project Manager's message names only their own projects. A purge removes the user's messages from the outbox.

**LLM call log and tracing:** every LLM call is written to `data/llm_calls.jsonl` with caller, model, duration,
tokens, estimated cost and outcome – never the prompt or the answer. The optional OpenTelemetry export
(`OTEL_EXPORTER_OTLP_ENDPOINT`) sends the same fields as spans and metrics to the company's collector; it carries no
content either, so it needs no own data-protection review beyond where the collector runs.

## Checklist before a pilot with customer data

1. Data processing agreement with the LLM provider (or the gateway operator), EU region confirmed in writing.
2. `LLM_DATA_RESIDENCY=eu` and the endpoints set; Admin Console › System status shows "allowed" only for them.
3. PII masking on (default).
4. OpenTelemetry export off, or pointed at the company's own collector.
5. Retention of `data/` agreed (decision log and audit: as long as the decisions need proof; chats and scenarios:
   archive with Reset or delete after the pilot).
6. Accounts per person from `AUTH_USERS_FILE` (no demo passwords in TEST or PROD).
