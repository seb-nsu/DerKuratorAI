# Claude Code project rules: DerKurator AI

## 1. Scope and edits

- Make surgical edits (targeted search and replace or inserts); do not rewrite whole files unless necessary.
- Do not touch, reformat or "optimise" files, functions or modules outside the scope of the current request.
- Keep terminal output and explanations concise: show the plan, make the change, report errors.

## 2. Architecture

- Dependency graph: NetworkX in memory (`app/graph/portfolio_graph.py`), built from the portfolio data; KùzuDB was
  replaced per `docs/adr/ADR-001-graph-database.md`. Neo4j is an optional second backend (`GRAPH_BACKEND=neo4j`,
  `app/graph/neo4j_backend.py`), only a projection of that graph: a new graph query goes into both backends of
  `app/graph/backends.py` with a parity case in `tests/test_graph_backends.py`. NetworkX stays the default; demo,
  tests and CI need no Neo4j and no Docker. Docker only packages the app (`Dockerfile`; `docker-compose.yml`
  internal, `docker-compose.demo.yml` and `render.yaml` public demo).
- UI: NiceGUI (`frontends/nicegui`) and React (`frontends/react`), both only over the HTTP API (`app/api`, page logic
  in `app/views`); start with `scripts/serve.py` (`FRONTEND=nicegui|react`). Streamlit is gone; do not reintroduce it.
  Every page except sign-in requires a signed-in identity (`app/core/auth.py`); the role comes from the user file only.
- AI orchestration: an own agent loop on the Anthropic and OpenAI SDKs (`app/llm/`), no agent framework. The LLM
  interprets language; Python tools calculate and the rule-based verdict engine (`app/crew/verdict.py`) decides.
- Data: every source goes through the mapping layer (`app/data_adapters/mapping.py`, `docs/DATA_MAPPING.md`); a new
  table needs its entry in `dataset.py` (TABLES) and `schema.py`.
- Planning (`app/planning/`) is deterministic without an LLM; the annual plan is approved only by the Portfolio Board
  (`ROLE_BOARD`, proposals of kind `plan`).

## 3. Operations

- Model names come from `data/supported_models.json` (the catalog of the Admin Console). The only model names in
  code are the defaults per agent in `app/core/config.py` (overridable via `ORCHESTRATOR_MODEL`, `ANALYST_MODEL`,
  `SIMULATOR_MODEL`, `ADVISORY_MODEL`); each of them must be listed in the catalog. No model names elsewhere.
- Never write plain-text passwords to files, logs or the repository; create hashes with `scripts/hash_password.py`
  (the admin password from `AUTH_ADMIN_PASSWORD` lives only in `.env` or the host's secrets).
- Before a push, run what CI runs: `ruff check .` and `pytest --cov` (coverage bounds in `pyproject.toml` and
  `.github/workflows/ci-cd-pipeline.yml`).
- Tests and evaluations run offline; paid LLM evaluation runs (`scripts/run_evals.py --mode llm|both`) only on
  request.
