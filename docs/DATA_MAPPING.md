# Generic data connection

A new data source or a customer workbook with its own structure needs no code, only a mapping file.

```
Connector (raw tables)  →  Mapping (YAML)  →  Canonical schema  →  PortfolioDataset
   JSON / Excel / CSV        names, values,      key, required        everything else
                             units, unpivot      columns, types,      (simulation, graph,
                                                 allowed values,      Data Steward, UI)
                                                 foreign keys
```

Loading is deterministic table work: no LLM is involved.

| Part | File |
|---|---|
| Canonical schema (28 tables, incl. the planning tables) | `app/data_adapters/schema.py` |
| Connectors | `app/data_adapters/connectors.py` |
| Mapping and import report | `app/data_adapters/mapping.py` |
| Data provider over a mapping | `app/data_adapters/mapped_adapter.py` |
| Excel template | `app/data_adapters/template.py`, `python scripts/make_excel_template.py` |
| Demo mapping | `data/mappings/servicenow_demo.yaml` |
| ServiceNow connector and SPM mapping | `app/data_adapters/servicenow.py`, `data/mappings/servicenow_spm.yaml` |

`DATA_SOURCE=json` and `DATA_SOURCE=excel` are the same machinery with the identity mapping (every table under its own
name, columns unchanged). `DATA_SOURCE=mapped` reads `MAPPING_FILE`, `DATA_SOURCE=servicenow` reads
`SERVICENOW_MAPPING_FILE` (see [ServiceNow](#servicenow)).

## Mapping file

```yaml
name: Customer portfolio
sources:
  book:   {type: excel, path: portfolio.xlsx}
  budget: {type: csv, path: budget_csv, options: {sep: ";", decimal: ",", date_format: "%d.%m.%Y"}}
  export: {type: json, path: export.json, writable: false}
default_source: book
store: portfolio.store.json        # optional, default: <mapping name>.store.json
tables:
  Projects:
    from: Vorhaben                 # sheet / CSV file name / JSON array (default: the canonical name)
    columns: {Nr: project_number, Bezeichnung: project_name, Projektleiter: project_manager,
              "Budget (TEUR)": budget_cost_eur}
    values: {status: {grün: Green, gelb: Yellow, rot: Red}}
    units: {budget_cost_eur: 1000} # canonical value = source value × factor
    defaults: {execution_type: Waterfall}
  Cost_Plans:
    source: budget
    from: Kosten
    columns: {Projekt: project_number, Art: cost_type}
    unpivot: {columns: "^\\d{4}$", to: month, value: planned_cost_eur, year_to_date: true}
    units: {planned_cost_eur: 1000}
    defaults: {actual_cost_eur: 0}
    key: {generate: "{project_number}-{cost_type}-{month:%Y}"}
  Risks: {}                        # same name and columns as the schema
```

- Relative paths are resolved against the folder of the mapping file.
- Columns that the mapping does not name are kept under their own name (`keep_unmapped: false` drops them).
- Each table can come from a different source (federation): projects from JSON, budgets from Excel, and so on.
- Tables that the mapping does not list are not loaded. Projects, Project_Tasks, Resource_Plans, Stakeholders and
  Risks are required.
- `unpivot` turns year (or month) columns into rows. `year_to_date: true` turns "2027" into 2027-01-01.

## Writing approved changes

| Situation | Target |
|---|---|
| One writable JSON or Excel source, every table mapped one to one | Back into the source, through the inverse mapping (source sheet and column names, source units, source values) |
| Several sources, a read-only source, a CSV folder, an unpivoted table or a generated key | The mapping's own store, a JSON file in canonical form that is laid over the sources on every load |

Either way the write goes through compare-and-write, the snapshot and the write journal (intent and committed record).
The customer's files are never changed in store mode. The store also records the source value each approved change
replaced: once the source holds a different value for that field (the change was transferred, or someone decided
differently there), the source value applies again and the import report lists the field.

## Several sources for one table

A table can list several source blocks; their rows are combined into one table (example
`data/mappings/multi_source_demo.yaml`: the ServiceNow export plus the Excel project list of a business unit).

```yaml
tables:
  Projects:
    - {source: snow}                 # the table as it is in that source
    - source: bu2
      from: Projekte
      columns: {Nr: project_number, Bezeichnung: project_name, ...}
```

- Every row carries its source in `source_system` (the source's `label`, else its name).
- `key_prefix` on a source prefixes its keys and its references to other tables (`BU2-` + number), so two systems
  that both count from 1 never collide. A key that two sources both use is refused with a message naming the prefix,
  never overwritten.
- `duplicates: {Projects: [project_name, project_sponsor]}` reports the projects both sources hold (same values in
  those fields) as Data Steward findings, and every field in which the two copies disagree (budget, forecast, end
  dates, status).
- A federated mapping always writes approved changes to the mapping's store, never into one of the sources; the
  import report counts the rows read and loaded per source.

## Microsoft Planner and monday.com

Two read-only sources for the tasks of a team tool, synced into a local snapshot like ServiceNow
(`python scripts/work_sync.py`, suitable for a scheduler). Example `data/mappings/team_tools.yaml`: projects from
ServiceNow, Project_Tasks from ServiceNow, Planner and monday.com together.

```yaml
sources:
  planner:
    type: planner
    path: ../team_tools_snapshot/planner
    options: {plans: {"<plan id>": {project: PRJ-DEMO-1002}}}
  monday:
    type: monday
    path: ../team_tools_snapshot/monday
    options: {boards: {"<board id>": {project: PRJ-DEMO-1001}}}
```

- **Planner** (Microsoft Graph v1.0): plan, buckets and tasks per plan (`planner_plans`, `planner_buckets`,
  `planner_tasks`). Graph has no delta query for Planner in v1.0: every sync reads the plans completely and reports
  what is new, changed (`@odata.etag`) or gone. Assignees appear only as a count. Credentials: `MS_GRAPH_TOKEN`, or
  `MS_GRAPH_TENANT_ID` + `MS_GRAPH_CLIENT_ID` + `MS_GRAPH_CLIENT_SECRET` (application permission Tasks.Read.All).
- **monday.com** (GraphQL API, cursor paging): `monday_boards` and `monday_items`, every column value of an item as
  `col_<column id>` (its text). Column ids differ per board – map the ones you need. Credential: `MONDAY_TOKEN`.
- `project` names the DerKurator project a plan or board belongs to; its rows carry it as `project_number`.
- Retry with backoff on 429/5xx, one sync at a time, files written only after everything was read; a failed sync
  leaves the snapshot unchanged. A mapping file with a token or secret is refused.

## Import report

Every load produces a report per table: rows read, loaded and skipped, plus notes. The Admin Console shows it under
*Database management → Import report*. These notes become Data Steward findings (rule "Import"):

| Finding | Severity |
|---|---|
| Required column of Projects missing (added empty) | ERROR: no quantitative simulation |
| Required column of another table missing (added empty) | WARNING |
| Mapped source column not found | WARNING |
| Values that are not numbers (text sources, scaled columns) | WARNING |
| Values without a translation in `values` | WARNING |
| Rows of a project table without project_number (dropped) | WARNING |

Duplicate keys, values outside the allowed list and references to unknown projects are listed in the report only:
the Data Steward checks them on the loaded data as before.

## ServiceNow

`DATA_SOURCE=servicenow` loads the SPM tables of a ServiceNow instance. The app never calls ServiceNow on a page load:
a **sync** reads the Table API into a local snapshot folder (one JSON file per table), and the portfolio loads from
the snapshot through the mapping like any other source (300 projects with 3,000 tasks load in well under a second).

- **Sync:** Admin Console › System status › *Sync ServiceNow* (changes) or *Full sync*, or
  `python scripts/servicenow_sync.py [--full]` from a scheduler (exit code 1 when a sync failed). Every sync is
  audited, a failed one as `servicenow_sync_failed`.
- **Paging:** keyset paging ordered by `sys_id` – every page asks for the records after the last `sys_id` read
  (`sys_id>…`, `sysparm_limit` = `page_size`) until an empty page or `X-Total-Count` says nothing is left. A record
  that changes or disappears during the read never pushes an unchanged one out of it, and an instance that caps the
  page size below `page_size` only means more pages. The number of records read is compared with `X-Total-Count`;
  a difference is listed as a warning in the sync report (the Admin Console shows it as a warning).
- **Delta:** the first run reads everything, later runs only the records changed since the last one. An overlap
  window (`overlap_minutes`, default one day) covers time-zone differences; records are merged by `sys_id`, so
  reading one twice changes nothing. A table whose snapshot file is missing or unreadable is read completely.
- **Deleted records** are removed by an id-only read (`prune_deleted: false` per table switches it off): on a delta
  sync only once the reconcile interval has passed (`SERVICENOW_RECONCILE_HOURS` or the option `reconcile_hours`,
  default 24; `0` checks on every sync), and only when the id list was read completely – otherwise nothing is
  deleted and the next sync checks again. A full sync replaces the snapshot and counts as a check.
- **Errors:** HTTP 429 and 5xx, timeouts and network errors are retried with exponential backoff (1, 2, 4, 8, 16 s;
  a `Retry-After` header wins), each request within `timeout_seconds` (default 60), the whole sync within
  `max_minutes` (default 30). The table files are written only after every table was read (each one atomically, the
  sync state last): a failed sync ends in a report with the error, and the snapshot stays as it was.
- **References:** with `sysparm_display_value=all` a reference keeps its sys_id under the field name and its display
  value as `<field>_display` (the number of a project, the name of a user or group, the label of a choice).
- **Credentials** only from the environment (the host's secrets or `.env`): `SERVICENOW_INSTANCE` (the mapping
  option `instance` may hold it too, the variable wins) and `SERVICENOW_TOKEN` (OAuth bearer token, used when set)
  or `SERVICENOW_USER` + `SERVICENOW_PASSWORD` (basic auth). A mapping file with a user, password, token, client
  secret or API key is refused; secrets never appear in a log, the snapshot or the sync report. A read-only
  integration user with access to the mapped tables is enough.
- **Read-only:** approved changes go to the mapping's store (`store:`), never back to ServiceNow.

```yaml
sources:
  servicenow:
    type: servicenow
    path: ../servicenow_snapshot          # snapshot folder
    options:
      page_size: 1000
      reconcile_hours: 24                 # check for deleted records at most this often
      timeout_seconds: 60                 # per request
      max_minutes: 30                     # the whole sync
      tables:
        pm_project: {query: "active=true"}
        risk: {query: "task.sys_class_name=pm_project", fields: [number, task, impact, probability]}
```

`data/mappings/servicenow_spm.yaml` maps `pm_project`, `pm_project_task`, `risk` and `project_status` and is a
starting point: check the field names against your instance (custom fields, release differences). Tables the mapping
leaves out (stakeholders, monthly resource and cost plans, dependencies) load empty and are listed in the import
report; they can come from a second source such as an Excel workbook in the same mapping file.

### Write-back of approved changes

Off by default. With `write_back: true` in the ServiceNow source's options **and** `SERVICENOW_WRITE_BACK=true` on
the host, the PMO can transfer an approved proposal to ServiceNow (Decision insights page, or
`POST /api/v1/insights/servicenow-transfer/{id}`). Per change the record is found in the snapshot, its current value
is read live and compared with the value the proposal started from: a different value is a conflict and nothing is
sent; otherwise one PATCH of the Table API with the source value. Fields mapped from a display value (references)
are listed for manual transfer. Every attempt is logged in `_write_back.jsonl` in the snapshot folder; after the
next sync the page shows each transfer as confirmed, waiting or differing. A change is never sent twice. The user of
`SERVICENOW_USER` then needs write access to the SPM tables.

## Excel template

`python scripts/make_excel_template.py data/Portfolio_Template.xlsx` writes one sheet per table (required columns
highlighted, drop-down lists for allowed values) and a *Fields* sheet with type, required flag, key and description
of every column. A filled template loads with `DATA_SOURCE=excel`, no mapping file needed.
