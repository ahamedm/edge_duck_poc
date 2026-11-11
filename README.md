# edge_duck — Edge OLAP Budgeting PoC

Proof of concept for **OLAP with DuckDB at the edge (browser)**: a Flask/DuckDB 2.0
server serves prior-year Budgets & Actuals as **Parquet over HTTP**; the browser runs
**duckdb-wasm** for all slicing/aggregation, with **AlpineJS** as the UI layer.

## Architecture

```
┌────────────────────── Browser (Edge) ──────────────────────┐
│  AlpineJS UI (grids, calendars, SQL console)               │
│  duckdb-wasm — in-browser OLAP engine                      │
│    • holds Parquet slices fetched over HTTP                │
│    • all slicing / pivoting / ad-hoc SQL happens here      │
│    • scenario cell edits → staged → PUT back to server     │
└───────────────────────────▲───────────────────────────────┘
              Parquet / JSON over HTTP
┌───────────────────────────┴───────────────────────────────┐
│  Flask API (authoritative)                                 │
│    • Reference data (generic tree) CRUD                    │
│    • Financial calendar engine (Jan–Dec | Apr–Mar)         │
│    • Prior Budget & Actuals served at requested level      │
│    • Scenario CRUD (copy-on-create from prior data)        │
│    • /api/query: read-only SQL passthrough (safeguarded)   │
│  DuckDB 2.0 (single-file DB) — source of truth             │
└────────────────────────────────────────────────────────────┘
```

**Principle:** the server DuckDB is the *store*; edge duckdb-wasm is an *ephemeral
compute cache*. Every persisted write round-trips through the API; the edge DB is
rebuildable at any time from server Parquet exports.

## Data model

### Reference data — generic self-referencing tree (`ref_node`)

`node_type ∈ {LOB, COST_CENTRE, NOMINAL, SUB_NOMINAL}`, `parent_id` FK, materialized
`path` (`'LOB1/CC12/NOM3'`) for fast subtree filters.

### Financial calendar (configurable)

`fiscal_calendar(start_month ∈ {1, 4})` with a derived `calendar_period` map:

- Jan–Dec: FY2026 P01 = Jan 2026 … P12 = Dec 2026
- Apr–Mar: FY2026 P01 = Apr 2026 … P12 = Mar 2027

Facts are keyed by **fiscal year + period number**, so both calendar conventions coexist.

### Facts — mixed grain

`budget_actual` stores detail rows (nominal/sub-nominal) *and* CC-level rows
(sentinel `0` for not-applicable level columns — DuckDB PKs are NOT NULL). A CC
whose Budget is held at Cost-Centre level has CC-level BUDGET rows; its Actuals
are still recorded at detail level. Rollups to any requested level are plain
`SUM(...) GROUP BY`, and CC-level totals always reconcile with detail totals.

### Scenarios

`scenario` (type BUDGET|FORECAST, calendar, base_year, seed_level, seed_version,
scope) + `scenario_line`. Creation **copies prior-year data at the requested
hierarchy level** into scenario years `base_year+1 .. base_year+3`. Edits are
bulk-upserted via `PUT /api/scenarios/{id}/lines`.

## API

| Endpoint | Purpose |
|---|---|
| `GET /api/reference/tree` | whole reference tree (JSON) |
| `POST /api/reference` | create node |
| `GET /api/calendars` | calendars + derived periods |
| `POST /api/calendars` | create calendar (`start_month` 1 or 4, year range) |
| `GET /api/facts/prior?calendar=&years=&level=CC\|NOMINAL\|SUBNOMINAL[&lob=&cost_centre=]` | prior Budget+Actuals at requested level → **Parquet** |
| `GET /api/scenarios` | list scenarios |
| `POST /api/scenarios` | create from prior data (`seed_level`, `seed_version`, `scope_node_id`) |
| `GET /api/scenarios/{id}[?level=]` | scenario lines at level → **Parquet** |
| `PUT /api/scenarios/{id}/lines` | bulk cell upsert |
| `POST /api/scenarios/{id}/status` | DRAFT ⇄ FINAL |
| `POST /api/query` | read-only SQL over exposed views (`v_prior_facts`, `v_ref_tree`, `v_calendar`, `v_scenarios`, `v_scenario_lines`) — single SELECT/WITH, LIMIT enforced, external access + filesystems disabled, 10 s timeout |
| `GET /api/schema` | catalog of exposed views |

## Run

```bash
uv sync            # or: uv add flask duckdb pyarrow
uv run python main.py
# → http://localhost:5010
```

First run seeds a deterministic synthetic dataset into `data/edge_duck.duckdb`:
3 LoBs / 7 cost centres / 5 nominals / 14 sub-nominals, FY2023–FY2025 Actuals +
Budgets under both Jan–Dec and Apr–Mar calendars (~11k fact rows), plus one demo
scenario. Delete `data/edge_duck.duckdb` to reseed.

## UI (3 tabs)

1. **Prior Year** — pick calendar, level, LoB/CC, prior years → data is fetched as
   Parquet and loaded into the **browser's DuckDB**; grid (periods × nodes) with
   Budget / Actuals / Variance is computed on the edge engine.
2. **Scenarios** — create from prior data (level + version + scope), open, edit
   period cells inline (staged), save via bulk upsert, mark FINAL.
3. **SQL Query** — ad-hoc SQL against the **edge** tables (`prior_facts`,
   `scenario_lines`, `ref_tree`) or the **server** views via `/api/query`.

## PoC limitations (by design)

- Single-user, no auth; Flask dev server.
- Edge DB is in-memory only — reloads refetch Parquet (no OPFS persistence).
- `/api/query` guard is a whitelist, not a sandbox.
- Seed amounts are synthetic; no FX/multi-currency, no locked periods beyond DRAFT/FINAL.
