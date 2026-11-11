from __future__ import annotations

import io

import pyarrow.parquet as pq
from flask import Blueprint, Response, jsonify, request

from ..db import get_db

bp = Blueprint("scenarios", __name__)

LEVELS = {
    "CC": {
        "cols": "cost_centre_id, cc_code, cc_name",
        "group": "cost_centre_id, cc_code, cc_name",
    },
    "NOMINAL": {
        "cols": "cost_centre_id, cc_code, cc_name, nominal_id, nominal_code, nominal_name",
        "group": "cost_centre_id, cc_code, cc_name, nominal_id, nominal_code, nominal_name",
    },
    "SUBNOMINAL": {
        "cols": ("cost_centre_id, cc_code, cc_name, nominal_id, nominal_code, nominal_name, "
                 "sub_nominal_id, sub_nominal_code, sub_nominal_name"),
        "group": ("cost_centre_id, cc_code, cc_name, nominal_id, nominal_code, nominal_name, "
                  "sub_nominal_id, sub_nominal_code, sub_nominal_name"),
    },
}
LEVEL_GROUP = {k: v["group"] for k, v in LEVELS.items()}
LEVEL_COLS = {k: v["cols"] for k, v in LEVELS.items()}


def _parquet_response(table) -> Response:
    buf = io.BytesIO()
    pq.write_table(table, buf, compression="snappy")
    return Response(buf.getvalue(), mimetype="application/octet-stream",
                    headers={"X-Edge-Rows": str(table.num_rows)})


@bp.get("/api/scenarios")
def list_scenarios():
    rows = get_db().execute(
        "SELECT id, name, type, calendar_id, calendar_name, base_year, seed_level, seed_version, "
        "scope_code, scope_name, status, line_count, updated_at "
        "FROM v_scenarios ORDER BY created_at DESC"
    ).fetchall()
    return jsonify([
        {"id": r[0], "name": r[1], "type": r[2], "calendar_id": r[3], "calendar_name": r[4],
         "base_year": r[5], "seed_level": r[6], "seed_version": r[7], "scope_code": r[8],
         "scope_name": r[9], "status": r[10], "line_count": r[11], "updated_at": str(r[12])}
        for r in rows
    ])


@bp.post("/api/scenarios")
def create_scenario():
    """Create a scenario by copying prior-year data at the chosen hierarchy level.

    Body: {name, type: BUDGET|FORECAST, calendar_id, base_year,
           seed_level: CC|NOMINAL|SUBNOMINAL, seed_version: ACTUAL|BUDGET,
           scope_node_id?: id of a LoB or Cost Centre to restrict}
    Scenario years = base_year+1 .. base_year+3, amounts copied flat from the base year.
    """
    body = request.get_json(force=True)
    name = body.get("name")
    stype = body.get("type")
    calendar_id = body.get("calendar_id")
    base_year = body.get("base_year")
    seed_level = (body.get("seed_level") or "").upper()
    seed_version = (body.get("seed_version") or "ACTUAL").upper()
    scope_node_id = body.get("scope_node_id")
    if not name:
        return jsonify({"error": "name is required"}), 400
    if stype not in ("BUDGET", "FORECAST"):
        return jsonify({"error": "type must be BUDGET or FORECAST"}), 400
    if seed_level not in LEVEL_GROUP:
        return jsonify({"error": "seed_level must be CC, NOMINAL or SUBNOMINAL"}), 400
    if seed_version not in ("ACTUAL", "BUDGET"):
        return jsonify({"error": "seed_version must be ACTUAL or BUDGET"}), 400

    con = get_db()
    if not con.execute("SELECT 1 FROM fiscal_calendar WHERE id = ?", [calendar_id]).fetchone():
        return jsonify({"error": "calendar not found"}), 404
    if scope_node_id is not None:
        scope = con.execute(
            "SELECT node_type FROM ref_node WHERE id = ?", [scope_node_id]).fetchone()
        if not scope:
            return jsonify({"error": "scope_node_id not found"}), 404
        if scope[0] not in ("LOB", "COST_CENTRE"):
            return jsonify({"error": "scope_node_id must be a LoB or Cost Centre"}), 400

    con.execute("BEGIN TRANSACTION")
    try:
        sid = con.execute(
            "INSERT INTO scenario (name, type, calendar_id, base_year, seed_level, seed_version, scope_node_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING id",
            [name, stype, calendar_id, base_year, seed_level, seed_version, scope_node_id],
        ).fetchone()[0]

        agg = {
            "CC": "cost_centre_id, 0, 0, SUM(amount)",
            "NOMINAL": "cost_centre_id, nominal_id, 0, SUM(amount)",
            "SUBNOMINAL": "cost_centre_id, nominal_id, sub_nominal_id, SUM(amount)",
        }[seed_level]
        group = {
            "CC": "cost_centre_id",
            "NOMINAL": "cost_centre_id, nominal_id",
            "SUBNOMINAL": "cost_centre_id, nominal_id, sub_nominal_id",
        }[seed_level]

        for target_year in (base_year + 1, base_year + 2, base_year + 3):
            scope_sql, scope_params = "", []
            if scope_node_id is not None:
                scope_sql = ("AND cost_centre_id IN "
                             "(SELECT id FROM ref_node WHERE id = ? OR path LIKE (SELECT path || '/%' FROM ref_node WHERE id = ?))")
                scope_params = [scope_node_id, scope_node_id]
            con.execute(
                f"""
                INSERT INTO scenario_line
                SELECT ?, ?, period_num, {agg}
                FROM budget_actual
                WHERE calendar_id = ? AND fiscal_year = ? AND version = ?
                  AND (nominal_id <> 0 OR sub_nominal_id <> 0 OR ? = 'CC')
                  {scope_sql}
                GROUP BY period_num, {group}
                """,
                [sid, target_year, calendar_id, base_year, seed_version, seed_level, *scope_params],
            )
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return jsonify({"id": sid, "name": name, "seed_level": seed_level}), 201


@bp.get("/api/scenarios/<int:sid>")
def read_scenario(sid: int):
    level = (request.args.get("level") or "").upper()
    con = get_db()
    scen = con.execute(
        "SELECT id, name, type, calendar_id, calendar_name, base_year, seed_level, seed_version, "
        "scope_code, scope_name, status, line_count, updated_at FROM v_scenarios WHERE id = ?",
        [sid],
    ).fetchone()
    if not scen:
        return jsonify({"error": "scenario not found"}), 404
    meta = {"id": scen[0], "name": scen[1], "type": scen[2], "calendar_id": scen[3],
            "calendar_name": scen[4], "base_year": scen[5], "seed_level": scen[6],
            "seed_version": scen[7], "scope_code": scen[8], "scope_name": scen[9],
            "status": scen[10], "line_count": scen[11], "updated_at": str(scen[12])}

    lvl = (level or scen[6]).upper()
    if lvl not in LEVELS:
        return jsonify({"error": "level must be CC, NOMINAL or SUBNOMINAL"}), 400

    order = {
        "CC": "cc_code",
        "NOMINAL": "cc_code, nominal_code NULLS LAST",
        "SUBNOMINAL": "cc_code, nominal_code NULLS LAST, sub_nominal_code NULLS LAST",
    }[lvl]
    table = con.execute(
        f"SELECT {LEVEL_COLS[lvl]}, fiscal_year, period_num, SUM(amount) AS amount "
        f"FROM v_scenario_lines WHERE scenario_id = {sid} "
        f"GROUP BY {LEVEL_GROUP[lvl]}, fiscal_year, period_num "
        f"ORDER BY {order}, fiscal_year, period_num"
    ).to_arrow_table()
    return _parquet_response(table)


@bp.put("/api/scenarios/<int:sid>/lines")
def upsert_lines(sid: int):
    """Bulk upsert scenario cells.
    Body: {lines: [{fiscal_year, period_num, cost_centre_id, nominal_id?, sub_nominal_id?, amount}]}
    nominal_id / sub_nominal_id default to 0 (cost-centre level).
    """
    con = get_db()
    if not con.execute("SELECT 1 FROM scenario WHERE id = ?", [sid]).fetchone():
        return jsonify({"error": "scenario not found"}), 404
    body = request.get_json(force=True)
    lines = body.get("lines") or []
    if not lines:
        return jsonify({"error": "lines is required"}), 400

    rows = []
    for ln in lines:
        try:
            rows.append((
                sid, int(ln["fiscal_year"]), int(ln["period_num"]), int(ln["cost_centre_id"]),
                int(ln.get("nominal_id") or 0), int(ln.get("sub_nominal_id") or 0),
                round(float(ln["amount"]), 2),
            ))
        except (KeyError, TypeError, ValueError) as exc:
            return jsonify({"error": f"bad line {ln}: {exc}"}), 400

    con.execute("BEGIN TRANSACTION")
    try:
        con.executemany(
            """
            INSERT INTO scenario_line (scenario_id, fiscal_year, period_num, cost_centre_id,
                                       nominal_id, sub_nominal_id, amount)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (scenario_id, fiscal_year, period_num, cost_centre_id, nominal_id, sub_nominal_id)
            DO UPDATE SET amount = excluded.amount
            """,
            rows,
        )
        con.execute("UPDATE scenario SET updated_at = now() WHERE id = ?", [sid])
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return jsonify({"updated": len(rows)})


@bp.post("/api/scenarios/<int:sid>/status")
def set_status(sid: int):
    status = (request.get_json(force=True).get("status") or "").upper()
    if status not in ("DRAFT", "FINAL"):
        return jsonify({"error": "status must be DRAFT or FINAL"}), 400
    con = get_db()
    if not con.execute("SELECT 1 FROM scenario WHERE id = ?", [sid]).fetchone():
        return jsonify({"error": "scenario not found"}), 404
    con.execute("UPDATE scenario SET status = ?, updated_at = now() WHERE id = ?", [status, sid])
    return jsonify({"id": sid, "status": status})
