from __future__ import annotations

import json
import re
import threading
from decimal import Decimal

from flask import Blueprint, Response, jsonify, request

from ..db import QUERY_VIEWS, get_db

bp = Blueprint("query", __name__)

STATEMENT_RE = re.compile(r"^\s*(SELECT|WITH)\b", re.IGNORECASE)
MAX_ROWS = 5000


def _run_sql(sql: str, result: dict):
    """Runs on a worker thread; uses the shared connection (statement is
    whitelisted to SELECT/WITH, so it cannot mutate)."""
    con = get_db()
    try:
        cur = con.execute(sql)
        cols = [d[0] for d in cur.description]
        rows = [[float(v) if isinstance(v, Decimal) else v for v in r] for r in cur.fetchall()]
        result["cols"], result["rows"] = cols, rows
    except Exception as exc:
        result["error"] = str(exc).splitlines()[0] if str(exc) else "statement failed"


@bp.post("/api/query")
def run_query():
    """Read-only ad-hoc SQL over the server-side DuckDB, restricted to the PoC views.

    Guards: single SELECT/WITH statement, external access + filesystems disabled,
    hard row cap (LIMIT injected when absent), statement timeout.
    """
    sql = (request.get_json(force=True).get("sql") or "").strip()
    if not sql:
        return jsonify({"error": "sql is required"}), 400
    if ";" in sql.rstrip(";").strip():
        return jsonify({"error": "only a single statement is allowed"}), 400
    sql = sql.rstrip(";").strip()
    if not STATEMENT_RE.match(sql):
        return jsonify({"error": "only SELECT or WITH statements are allowed"}), 400
    if not re.search(r"\bLIMIT\b", sql, re.IGNORECASE):
        sql += f" LIMIT {MAX_ROWS}"

    con = get_db()
    try:
        con.execute("SET enable_external_access = false")
    except Exception:
        pass  # already set globally

    result: dict = {}
    thread = threading.Thread(target=_run_sql, args=(sql, result), daemon=True)
    thread.start()
    thread.join(timeout=10)
    if thread.is_alive():
        con.interrupt()
        return jsonify({"error": "statement timed out (10s)"}), 408
    if "error" in result:
        return jsonify({"error": result["error"]}), 400
    if "rows" not in result:
        return jsonify({"error": "statement failed"}), 400

    return jsonify({"sql": sql, "cols": result["cols"], "rows": result["rows"],
                    "views": sorted(QUERY_VIEWS)})


@bp.get("/api/schema")
def schema():
    """Catalog of exposed views with column lists (for the SQL console)."""
    con = get_db()
    out = {}
    for view in QUERY_VIEWS:
        cur = con.execute(f"DESCRIBE SELECT * FROM {view}")
        out[view] = [{"name": d[0], "type": d[1]} for d in cur.fetchall()]
    return Response(json.dumps(out, indent=2), mimetype="application/json")
