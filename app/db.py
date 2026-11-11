"""DuckDB connection management + schema DDL for the edge_duck PoC.

Conventions:
- Reference hierarchy is a generic self-referencing tree (ref_node).
- Facts use sentinel 0 for "not applicable" level columns (DuckDB PKs are NOT NULL,
  so mixed-grain rows cannot use NULL in key columns).
- 0 nominal_id / sub_nominal_id => the row lives at Cost-Centre level.
"""
from __future__ import annotations

import os
import threading

import duckdb

DB_PATH = os.environ.get("EDGE_DUCK_DB", os.path.join(os.path.dirname(__file__), "..", "data", "edge_duck.duckdb"))

_local = threading.local()

SCHEMA = """
CREATE SEQUENCE IF NOT EXISTS seq_ref_node START 1;
CREATE SEQUENCE IF NOT EXISTS seq_scenario START 1;

CREATE TABLE IF NOT EXISTS ref_node (
    id        INTEGER PRIMARY KEY DEFAULT nextval('seq_ref_node'),
    node_type TEXT NOT NULL CHECK (node_type IN ('LOB','COST_CENTRE','NOMINAL','SUB_NOMINAL')),
    code      TEXT NOT NULL,
    name      TEXT NOT NULL,
    parent_id INTEGER REFERENCES ref_node(id),
    path      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ref_parent ON ref_node(parent_id);
CREATE INDEX IF NOT EXISTS idx_ref_path   ON ref_node(path);

CREATE TABLE IF NOT EXISTS fiscal_calendar (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    start_month INTEGER NOT NULL CHECK (start_month IN (1, 4))
);

CREATE TABLE IF NOT EXISTS calendar_period (
    calendar_id INTEGER NOT NULL REFERENCES fiscal_calendar,
    fiscal_year INTEGER NOT NULL,
    period_num  INTEGER NOT NULL CHECK (period_num BETWEEN 1 AND 12),
    label       TEXT NOT NULL,
    month       INTEGER NOT NULL,
    start_date  DATE NOT NULL,
    end_date    DATE NOT NULL,
    PRIMARY KEY (calendar_id, fiscal_year, period_num)
);

CREATE TABLE IF NOT EXISTS budget_actual (
    calendar_id    INTEGER NOT NULL REFERENCES fiscal_calendar,
    fiscal_year    INTEGER NOT NULL,
    period_num     INTEGER NOT NULL,
    cost_centre_id INTEGER NOT NULL REFERENCES ref_node(id),
    nominal_id     INTEGER NOT NULL DEFAULT 0,
    sub_nominal_id INTEGER NOT NULL DEFAULT 0,
    version        TEXT NOT NULL CHECK (version IN ('ACTUAL','BUDGET')),
    amount         DECIMAL(18,2) NOT NULL,
    PRIMARY KEY (calendar_id, fiscal_year, period_num, cost_centre_id, nominal_id, sub_nominal_id, version)
);

CREATE TABLE IF NOT EXISTS scenario (
    id          INTEGER PRIMARY KEY DEFAULT nextval('seq_scenario'),
    name        TEXT NOT NULL,
    type        TEXT NOT NULL CHECK (type IN ('BUDGET','FORECAST')),
    calendar_id INTEGER NOT NULL REFERENCES fiscal_calendar,
    base_year   INTEGER NOT NULL,
    seed_level  TEXT NOT NULL CHECK (seed_level IN ('CC','NOMINAL','SUBNOMINAL')),
    seed_version TEXT NOT NULL CHECK (seed_version IN ('ACTUAL','BUDGET')),
    scope_node_id INTEGER,
    status      TEXT NOT NULL DEFAULT 'DRAFT' CHECK (status IN ('DRAFT','FINAL')),
    created_at  TIMESTAMP NOT NULL DEFAULT now(),
    updated_at  TIMESTAMP NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS scenario_line (
    scenario_id    INTEGER NOT NULL REFERENCES scenario,
    fiscal_year    INTEGER NOT NULL,
    period_num     INTEGER NOT NULL,
    cost_centre_id INTEGER NOT NULL REFERENCES ref_node(id),
    nominal_id     INTEGER NOT NULL DEFAULT 0,
    sub_nominal_id INTEGER NOT NULL DEFAULT 0,
    amount         DECIMAL(18,2) NOT NULL,
    PRIMARY KEY (scenario_id, fiscal_year, period_num, cost_centre_id, nominal_id, sub_nominal_id)
);
"""

VIEWS = """
-- Facts with names resolved; sentinel 0 exposed as NULL for readability.
CREATE VIEW IF NOT EXISTS v_prior_facts AS
SELECT b.calendar_id, b.fiscal_year, b.period_num, b.version,
       cc.id   AS cost_centre_id, cc.code AS cc_code, cc.name AS cc_name,
       lob.code AS lob_code, lob.name AS lob_name,
       CASE WHEN b.nominal_id = 0 THEN NULL ELSE n.id END     AS nominal_id,
       CASE WHEN b.nominal_id = 0 THEN NULL ELSE n.code END   AS nominal_code,
       CASE WHEN b.nominal_id = 0 THEN NULL ELSE n.name END   AS nominal_name,
       CASE WHEN b.sub_nominal_id = 0 THEN NULL ELSE s.id END AS sub_nominal_id,
       CASE WHEN b.sub_nominal_id = 0 THEN NULL ELSE s.code END AS sub_nominal_code,
       CASE WHEN b.sub_nominal_id = 0 THEN NULL ELSE s.name END AS sub_nominal_name,
       b.amount
FROM budget_actual b
JOIN ref_node cc  ON cc.id = b.cost_centre_id
LEFT JOIN ref_node n ON n.id = b.nominal_id
LEFT JOIN ref_node s ON s.id = b.sub_nominal_id
LEFT JOIN ref_node lob ON lob.id = cc.parent_id;

CREATE VIEW IF NOT EXISTS v_ref_tree AS
SELECT n.id, n.node_type, n.code, n.name, n.parent_id, n.path,
       p.code AS parent_code, p.name AS parent_name
FROM ref_node n LEFT JOIN ref_node p ON p.id = n.parent_id;

CREATE VIEW IF NOT EXISTS v_calendar AS
SELECT c.id AS calendar_id, c.name AS calendar_name, c.start_month,
       p.fiscal_year, p.period_num, p.label, p.month, p.start_date, p.end_date
FROM fiscal_calendar c JOIN calendar_period p ON p.calendar_id = c.id;

CREATE VIEW IF NOT EXISTS v_scenarios AS
SELECT s.id, s.name, s.type, s.calendar_id, c.name AS calendar_name, c.start_month,
       s.base_year, s.seed_level, s.seed_version,
       sc.code AS scope_code, sc.name AS scope_name,
       s.status, s.created_at, s.updated_at,
       (SELECT count(*) FROM scenario_line l WHERE l.scenario_id = s.id) AS line_count
FROM scenario s
JOIN fiscal_calendar c ON c.id = s.calendar_id
LEFT JOIN ref_node sc ON sc.id = s.scope_node_id;

CREATE VIEW IF NOT EXISTS v_scenario_lines AS
SELECT l.scenario_id, l.fiscal_year, l.period_num,
       cc.id   AS cost_centre_id, cc.code AS cc_code, cc.name AS cc_name,
       CASE WHEN l.nominal_id = 0 THEN NULL ELSE n.id END     AS nominal_id,
       CASE WHEN l.nominal_id = 0 THEN NULL ELSE n.code END   AS nominal_code,
       CASE WHEN l.nominal_id = 0 THEN NULL ELSE n.name END   AS nominal_name,
       CASE WHEN l.sub_nominal_id = 0 THEN NULL ELSE s.id END AS sub_nominal_id,
       CASE WHEN l.sub_nominal_id = 0 THEN NULL ELSE s.code END AS sub_nominal_code,
       CASE WHEN l.sub_nominal_id = 0 THEN NULL ELSE s.name END AS sub_nominal_name,
       l.amount
FROM scenario_line l
JOIN ref_node cc ON cc.id = l.cost_centre_id
LEFT JOIN ref_node n ON n.id = l.nominal_id
LEFT JOIN ref_node s ON s.id = l.sub_nominal_id;
"""

# Views the /api/query endpoint exposes to ad-hoc SQL.
QUERY_VIEWS = ("v_prior_facts", "v_ref_tree", "v_calendar", "v_scenarios", "v_scenario_lines")


def get_db() -> duckdb.DuckDBPyConnection:
    """One connection per thread (Flask dev server is threaded)."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
        conn = duckdb.connect(DB_PATH)
        _local.conn = conn
    return conn


def init_schema() -> None:
    con = get_db()
    con.execute(SCHEMA)
    con.execute(VIEWS)


def db_exists() -> bool:
    try:
        con = duckdb.connect(DB_PATH, read_only=True)
        con.execute("SELECT 1 FROM fiscal_calendar LIMIT 1")
        con.close()
        return True
    except Exception:
        return False
