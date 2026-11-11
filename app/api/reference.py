from __future__ import annotations

from flask import Blueprint, jsonify

from ..db import get_db

bp = Blueprint("reference", __name__)

NODE_TYPES = ("LOB", "COST_CENTRE", "NOMINAL", "SUB_NOMINAL")


@bp.get("/api/reference/tree")
def tree():
    rows = get_db().execute(
        "SELECT id, node_type, code, name, parent_id, path FROM ref_node ORDER BY path, node_type"
    ).fetchall()
    return jsonify([
        {"id": r[0], "node_type": r[1], "code": r[2], "name": r[3], "parent_id": r[4], "path": r[5]}
        for r in rows
    ])


@bp.post("/api/reference")
def create_node():
    from flask import request

    body = request.get_json(force=True)
    node_type, code, name = body.get("node_type"), body.get("code"), body.get("name")
    if node_type not in NODE_TYPES:
        return jsonify({"error": f"node_type must be one of {NODE_TYPES}"}), 400
    if not code or not name:
        return jsonify({"error": "code and name are required"}), 400
    parent_id = body.get("parent_id")
    con = get_db()
    if node_type == "LOB" and parent_id is not None:
        return jsonify({"error": "LOB is a root node and cannot have a parent"}), 400
    if parent_id is not None:
        parent = con.execute("SELECT path FROM ref_node WHERE id = ?", [parent_id]).fetchone()
        if not parent:
            return jsonify({"error": "parent not found"}), 404
        path = f"{parent[0]}/{code}"
    else:
        path = code
    try:
        new_id = con.execute(
            "INSERT INTO ref_node (node_type, code, name, parent_id, path) VALUES (?, ?, ?, ?, ?) RETURNING id",
            [node_type, code, name, parent_id, path],
        ).fetchone()[0]
    except Exception as exc:  # duplicate code / bad parent
        return jsonify({"error": str(exc)}), 409
    return jsonify({"id": new_id, "path": path}), 201
