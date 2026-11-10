# ADR-001: Graph database – keep KùzuDB 0.11.3 pinned for v1.x

- **Status:** Superseded on 2026-10-05 by the switch to NetworkX (branch `experiment/addon`, see *Update* at the end); Neo4j added as an optional second backend on 2026-10-07
- **Date:** 2026-10-01
- **Scope:** v1.x pilot (single tenant, local)

## Context

DerKurator answers dependency questions (multi-hop dominoes, successor deadlines, shared teams, risk clusters) with
an embedded KùzuDB graph. KùzuDB **0.11.3 is the last release**: the upstream project is archived and receives no
fixes and no security updates.

How the product uses it:

- **Embedded and read-only for analysis.** Schema and seed live in `app/graph/kuzu_seed.py`. The graph is derived
  data: it is rebuilt from the data source whenever the source fingerprint changes, into a temporary directory that
  is swapped in atomically.
- **23 parameterised Cypher queries** in `app/crew/tools/kuzu_tools.py`, including variable-length paths
  (`DEPENDS_ON*1..n`, `BLOCKS*1..15`).
- **Connection handling in the same module:** the cached read-only database, the reader/writer and seed locks, and
  the fingerprint.
- **Status display** in the Admin Console (Operations → graph database).
- **Single entry point.** All queries go through `run_query()` and the tool functions in `kuzu_tools.py`. Other
  modules (pipeline, portfolio and timeline tools, UI) call those functions, never Cypher directly; only the
  seed writes Cypher of its own.

## Decision

Keep `kuzu==0.11.3` pinned for v1.x.

- It is local and embedded, with no network exposure and no server.
- It is read-only at runtime: business writes go to the system of record, never to the graph.
- It can be rebuilt at any time. The graph is re-seeded from the data source, so losing or corrupting the database
  costs nothing but a reseed.

The remaining risk is bounded: no upstream fixes, and in the long run possibly no wheels for new Python versions.

## Triggers for a switch

Any one of these reopens the decision:

1. A Python version the product must support has no 0.11.3 wheel.
2. A known security vulnerability in KùzuDB 0.11.3.
3. A portfolio larger than about 5,000 projects.
4. A need for multi-user write access to the graph.

## Options

| Option | What changes | Effort |
|---|---|---|
| **LadybugDB** (community fork of Kùzu) | Package name and import; Cypher and file format largely the same. Check first: release cadence, wheels, licence | low (≈ 1 day) |
| **NetworkX in memory** | Graph built from the `PortfolioDataset`; the 23 queries become Python functions. Enough for portfolios up to a few thousand projects | medium (≈ 3–4 days) |
| **DuckDB with recursive CTEs** | SQL instead of Cypher; one database for tables and graph | medium (≈ 4–5 days) |
| **Neo4j / Memgraph as a server** | Cypher stays; needs a server (so far excluded for the pilot) | high, only with an infrastructure decision |

## Migration path (any option)

1. The tool functions in `kuzu_tools.py` (`downstream_impact`, `upstream_bottlenecks`, `shared_resources`,
   `cluster_risk`, `team_bottlenecks`, `task_level_impact`, `successor_deadlines`, `top_portfolio_risks`,
   `check_connection`) become the interface, a `GraphBackend`.
2. A backend switch replaces only their implementation and the seed (`kuzu_seed.py`). Callers and the verdict engine stay unchanged, because the
   tools already report their findings as structured facts.
3. **Acceptance:** the existing graph tests and the golden verdict cases (`tests/test_verdict_golden.py`) must
   pass unchanged on the new backend.

## Recommendation

Pin for the pilot. When a trigger fires, evaluate LadybugDB first, because it is the smallest change. If it fails
the check (no wheels, unclear licence, no releases), move to NetworkX: it has no native dependency at all.

## Consequences

- `requirements.txt` keeps `kuzu==0.11.3`; dependency upgrades must not move it.
- The four triggers are reviewed at each release.
- New graph features go through the tool functions above, never through Cypher in other modules, so the switch
  stays limited to the tool module and the seed.

## Update 2026-10-05: replaced by NetworkX (branch `experiment/addon`)

The switch was made before the annual planning work, which routes project dependencies through the graph as well.
LadybugDB was not evaluated: NetworkX has no native dependency, and the portfolio sizes in sight are far below the
limit where an in-memory graph becomes a problem.

- **What replaced what.** `app/graph/kuzu_seed.py` and `scripts/seed_kuzu.py` are gone; `app/graph/portfolio_graph.py`
  builds the graph from the `PortfolioDataset` and keeps it per process. The Cypher queries in
  `app/crew/tools/kuzu_tools.py` became Python functions in `app/crew/tools/graph_tools.py` with the same names and
  the same output, so callers and the verdict engine did not change. The dependency graph in the report uses Plotly
  instead of pyvis.
- **Equivalence.** All graph tool outputs were recorded on the demo and the golden portfolio, for the PMO and three
  project managers (298 outputs), before the switch and compared after it: 297 are identical; the one difference is
  the order of two teams' projects with exactly the same peak utilisation, which is now sorted by project id. The
  golden verdict cases pass unchanged.
- **Speed.** About 1 s to build the graph for a 308-project copy of the test portfolio (≈ 46,000 rows), against about
  20 s for the KùzuDB seed; 0.05 s for the 11-project demo.
- **What was given up.** No query language (new questions are Python code), no database file that survives a
  restart (the graph is rebuilt from the source, which was already the case), no cross-process sharing (each process
  builds its own graph; this matters only with several app processes). The reader/writer lock, the seed lock and the
  staging directory are no longer needed.
- **When to reconsider.** A portfolio far above 5,000 projects, several app processes that must share one graph, or
  ad-hoc graph queries by users. Then a server database (Neo4j, Memgraph) or LadybugDB is the next step.

## Update 2026-10-07: Neo4j as an optional second backend

**Why.** The company already runs Neo4j for another team. A pilot may use that instance instead of only the graph in
the app's process, so the graph can be inspected with the tools the Neo4j team knows, and a later move to a shared
graph has a tested path. No new infrastructure is needed for the pilot; NetworkX stays the default, and the demo,
the default CI job and Streamlit Community Cloud keep running without Neo4j.

**Decision.** `GRAPH_BACKEND=networkx|neo4j` (default `networkx`) with `NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD`
(secret) and `NEO4J_DATABASE` (default `neo4j`).

- **One interface, two implementations.** The graph tools ask a `GraphBackend` (`app/graph/backends.py`) for plain
  rows: dependents and predecessors up to five levels, the edges between two projects, shared teams, risks, overloaded
  allocations and months, open tasks with their blocking chain, successor deadlines and blocking dependencies. The
  NetworkX implementation is the former code; the Neo4j implementation (`app/graph/neo4j_backend.py`) runs the same
  queries in Cypher (`ANY SHORTEST` path patterns, so Neo4j 5.21 or later). Scope filter, aggregation and wording
  stay in `graph_tools.py`, once for both.
- **Neo4j is only a projection.** The graph is still built from the portfolio data by `portfolio_graph.py` (same
  fingerprint, rebuild, pin and reset). Every rebuild writes it into Neo4j in batches (`UNWIND`, 1,000 rows); an
  unchanged graph (same content key) is not written again. All nodes carry a `DerKurator_` label and the key of their
  version; a new version is written beside the old one, marked complete last, then the old ones are deleted. Nothing
  outside these labels is touched, so a shared instance is safe – a database of its own is still preferred.
- **Never a wrong answer.** A query reads, then checks that its version is still complete. If Neo4j is unreachable,
  refuses the login, is too old, or a query fails, the in-memory graph answers instead – same data, same result. The
  reason is logged once, shown in the Admin Console (System status › Graph backend) and Neo4j is tried again after
  30 s. The password is never logged or shown.
- **Acceptance.** `tests/test_graph_backends.py` runs every graph tool on every project of the demo portfolio and of a
  portfolio built to break things (cycles, parallel dependencies, self-dependencies, task cycles, umlauts, missing
  values), for the PMO and a project manager, plus a 300-project chain, on both backends; the outputs and the
  verdict facts must be identical. CI runs them in the job `neo4j` against a `neo4j:5-community` container
  (marker `neo4j`; skipped without `NEO4J_URI`).
- **Measured 2026-10-07** (Neo4j 5.26 Community on the same machine): the demo portfolio (248 nodes, 590
  relationships) builds in 0.06 s and is written in 0.10 s (median of 5; up to 0.8 s on the very first run, which
  creates the indexes); an unchanged graph costs 0.01 s. A 300-project portfolio (4,813 nodes, 13,799
  relationships) is written in about 1 s. A dependents query takes about 3 ms in Neo4j.

**Consequences.** `neo4j` (Python driver) is pinned in `requirements.txt`. A new graph question needs a method on the
interface with both implementations and a parity case. With Neo4j each process still builds its own graph in memory
(the fallback); the projection is shared, and processes with the same data share one version.
