from __future__ import annotations

import io

import pyarrow.parquet as pq
from flask import Blueprint, Response, request

from ..db import get_db

bp = Blueprint("facts", __name__)

LEVELS = {"CC": 0, "NOMINAL": 1, "SUBNOMINAL": 2}


def _level_group(level: str) -> str:
    """GROUP BY expression per requested hierarchy level."""
    if level == "CC":
        return "cost_centre_id, cc_code, cc_name, lob_code"
    if level == "NOMINAL":
        # CC-level budget rows (nominal_id NULL) roll into a visible NULL bucket
        return "cost_centre_id, cc_code, cc_name, lob_code, nominal_id, nominal_code, nominal_name"
    return ("cost_centre_id, cc_code, cc_name, lob_code, nominal_id, nominal_code, nominal_name, "
            "sub_nominal_id, sub_nominal_code, sub_nominal_name")


@bp.get("/api/facts/prior")
def prior_facts():
    calendar_id = request.args.get("calendar", type=int)
    years = [int(y) for y in request.args.get("years", "").split(",") if y.strip()]
    level = (request.args.get("level") or "SUBNOMINAL").upper()
    cost_centre = request.args.get("cost_centre", type=int)
    lob = request.args.get("lob", type=int)
    if calendar_id is None:
        return Response("{'error': 'calendar is required'}", status=400, mimetype="application/json")
    if level not in LEVELS:
        return Response(f"{{'error': 'level must be one of {list(LEVELS)}'}}",
                        status=400, mimetype="application/json")

    con = get_db()
    where, params = ["calendar_id = ?"], [calendar_id]
    if years:
        where.append(f"fiscal_year IN ({', '.join('?' * len(years))})")
        params += years
    if cost_centre:
        where.append("cost_centre_id = ?")
        params.append(cost_centre)
    if lob:
        where.append("cost_centre_id IN (SELECT id FROM ref_node WHERE parent_id = ?)")
        params.append(lob)

    cols = {
        "CC": ("fiscal_year, period_num, version, cost_centre_id, cc_code, cc_name, lob_code, "
               "SUM(amount) AS amount"),
        "NOMINAL": ("fiscal_year, period_num, version, cost_centre_id, cc_code, cc_name, lob_code, "
                    "nominal_id, nominal_code, nominal_name, SUM(amount) AS amount"),
        "SUBNOMINAL": ("fiscal_year, period_num, version, cost_centre_id, cc_code, cc_name, lob_code, "
                       "nominal_id, nominal_code, nominal_name, sub_nominal_id, sub_nominal_code, sub_nominal_name, "
                       "SUM(amount) AS amount"),
    }[level]
    order = {
        "CC": "fiscal_year, period_num, cc_code",
        "NOMINAL": "fiscal_year, period_num, cc_code, nominal_code NULLS LAST",
        "SUBNOMINAL": "fiscal_year, period_num, cc_code, nominal_code NULLS LAST, sub_nominal_code NULLS LAST",
    }[level]
    sql = (
        f"SELECT {cols} FROM v_prior_facts WHERE {' AND '.join(where)} "
        f"GROUP BY {_level_group(level)}, fiscal_year, period_num, version "
        f"ORDER BY {order}"
    )
    table = con.execute(sql, params).to_arrow_table()

    buf = io.BytesIO()
    pq.write_table(table, buf, compression="snappy")
    return Response(
        buf.getvalue(),
        mimetype="application/octet-stream",
        headers={"X-Edge-Level": level, "X-Edge-Rows": str(table.num_rows)},
    )
