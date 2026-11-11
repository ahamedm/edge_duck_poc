from __future__ import annotations

from flask import Blueprint, jsonify, request

from ..calendar_engine import generate_periods, validate_start_month
from ..db import get_db

bp = Blueprint("calendars", __name__)

DEFAULT_YEARS = list(range(2022, 2027))


@bp.get("/api/calendars")
def list_calendars():
    con = get_db()
    cals = con.execute("SELECT id, name, start_month FROM fiscal_calendar ORDER BY id").fetchall()
    periods = con.execute(
        "SELECT calendar_id, fiscal_year, period_num, label, month, start_date, end_date "
        "FROM calendar_period ORDER BY calendar_id, fiscal_year, period_num"
    ).fetchall()
    period_map: dict[int, list] = {}
    for cal_id, fy, pnum, label, month, sd, ed in periods:
        period_map.setdefault(cal_id, []).append(
            {"fiscal_year": fy, "period_num": pnum, "label": label,
             "month": month, "start_date": str(sd), "end_date": str(ed)}
        )
    return jsonify([
        {"id": c[0], "name": c[1], "start_month": c[2], "periods": period_map.get(c[0], [])}
        for c in cals
    ])


@bp.post("/api/calendars")
def create_calendar():
    body = request.get_json(force=True)
    try:
        start_month = validate_start_month(body.get("start_month"))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    name = body.get("name") or ("Jan-Dec" if start_month == 1 else "Apr-Mar")
    from_year = int(body.get("from_fiscal_year", DEFAULT_YEARS[0]))
    to_year = int(body.get("to_fiscal_year", DEFAULT_YEARS[-1]))
    if to_year < from_year:
        return jsonify({"error": "to_fiscal_year must be >= from_fiscal_year"}), 400

    con = get_db()
    next_id = con.execute("SELECT coalesce(max(id), 0) + 1 FROM fiscal_calendar").fetchone()[0]
    if con.execute("SELECT 1 FROM fiscal_calendar WHERE name = ?", [name]).fetchone():
        return jsonify({"error": f"calendar '{name}' already exists"}), 409
    con.execute("INSERT INTO fiscal_calendar VALUES (?, ?, ?)", [next_id, name, start_month])
    for fy in range(from_year, to_year + 1):
        for p in generate_periods(start_month, fy):
            con.execute(
                "INSERT INTO calendar_period VALUES (?, ?, ?, ?, ?, ?, ?)",
                [next_id, fy, p.period_num, p.label, p.month, p.start_date, p.end_date],
            )
    return jsonify({"id": next_id, "name": name, "start_month": start_month}), 201
