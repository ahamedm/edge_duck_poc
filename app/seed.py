"""Deterministic synthetic seed data: reference tree, calendars, prior-year
Budget + Actuals at mixed grain, and one demo scenario.

Run once when the DuckDB file does not exist yet (see main.py).
"""
from __future__ import annotations

import random

from .calendar_engine import generate_periods
from .db import get_db

PRIOR_YEARS = (2023, 2024, 2025)  # relative to planning year 2026

LOBS = [("LOB-MFG", "Manufacturing", [("MFG-PLANT-A", "Plant A"), ("MFG-PLANT-B", "Plant B")]),
        ("LOB-RET", "Retail", [("RET-STORES", "Store Network"), ("RET-ONLINE", "Online Channel")]),
        ("LOB-SVC", "Services", [("SVC-CONSULT", "Consulting"), ("SVC-SUPPORT", "Managed Support"), ("SVC-TRAIN", "Training Academy")])]

NOMINALS = [("N-STAFF", "Staff Costs", [("SN-SALARIES", "Salaries"), ("SN-PENSION", "Pension & Benefits"), ("SN-CONTRACT", "Contract Labour")]),
            ("N-PREM", "Premises", [("SN-RENT", "Rent & Rates"), ("SN-UTIL", "Utilities"), ("SN-MAINT", "Maintenance")]),
            ("N-IT", "IT & Systems", [("SN-SOFTWARE", "Software Licences"), ("SN-HARDWARE", "Hardware"), ("SN-CLOUD", "Cloud & Hosting")]),
            ("N-TRAVEL", "Travel & Expenses", [("SN-FLIGHTS", "Flights & Hotels"), ("SN-MILEAGE", "Mileage & Subsistence")]),
            ("N-MKTG", "Marketing", [("SN-ADVERT", "Advertising"), ("SN-EVENTS", "Events & Sponsorship")])]


def seed_ref_tree(con) -> dict:
    """Returns {cc_code: (cc_id, weight, cc_level_budget_only: bool)}."""
    rnd = random.Random(42)
    cc_info: dict[str, tuple[int, float, bool]] = {}
    for lob_code, lob_name, ccs in LOBS:
        lob_id = con.execute(
            "INSERT INTO ref_node (node_type, code, name, parent_id, path) VALUES ('LOB', ?, ?, NULL, ?) RETURNING id",
            [lob_code, lob_name, lob_code],
        ).fetchone()[0]
        lob_path = lob_code
        for cc_code, cc_name in ccs:
            cc_id = con.execute(
                "INSERT INTO ref_node (node_type, code, name, parent_id, path) VALUES ('COST_CENTRE', ?, ?, ?, ?) RETURNING id",
                [cc_code, cc_name, lob_id, f"{lob_path}/{cc_code}"],
            ).fetchone()[0]
            weight = round(rnd.uniform(0.6, 2.0), 2)
            # every 3rd cost centre gets its Budget at CC level only (mixed grain demo)
            cc_level_only = rnd.choice([False, False, True])
            cc_info[cc_code] = (cc_id, weight, cc_level_only)
    for nom_code, nom_name, subs in NOMINALS:
        nom_id = con.execute(
            "INSERT INTO ref_node (node_type, code, name, parent_id, path) VALUES ('NOMINAL', ?, ?, NULL, ?) RETURNING id",
            [nom_code, nom_name, nom_code],
        ).fetchone()[0]
        for sn_code, sn_name in subs:
            con.execute(
                "INSERT INTO ref_node (node_type, code, name, parent_id, path) VALUES ('SUB_NOMINAL', ?, ?, ?, ?)",
                [sn_code, sn_name, nom_id, f"{nom_code}/{sn_code}"],
            )
    return cc_info


def seed_calendars(con) -> None:
    for cal_id, name, start_month in [(1, "Jan-Dec", 1), (2, "Apr-Mar", 4)]:
        con.execute("INSERT INTO fiscal_calendar VALUES (?, ?, ?)", [cal_id, name, start_month])
        for fy in range(2022, 2027):
            for p in generate_periods(start_month, fy):
                con.execute(
                    "INSERT INTO calendar_period VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [cal_id, fy, p.period_num, p.label, p.month, p.start_date, p.end_date],
                )


def seed_facts(con, cc_info: dict) -> None:
    rnd = random.Random(7)
    nominal_weights: dict[int, float] = {}  # sub_nominal_id -> share of CC spend
    rows = con.execute("SELECT id, parent_id FROM ref_node WHERE node_type = 'SUB_NOMINAL'").fetchall()
    subs = [(sid, pid) for sid, pid in rows]
    sn_parent = {sid: pid for sid, pid in subs}
    total = sum(1.0 for _ in subs)
    for sid, _pid in subs:
        nominal_weights[sid] = 1.0 / total

    seasonal = {1: 0.9, 2: 0.85, 3: 1.0, 4: 1.05, 5: 1.0, 6: 0.95,
                7: 0.9, 8: 1.1, 9: 1.05, 10: 1.0, 11: 1.15, 12: 1.3}

    def amt(base: float, p: int, jitter: float) -> float:
        return round(base * seasonal[p] * (1.0 + rnd.uniform(-jitter, jitter)), 2)

    for cal_id in (1, 2):
        for fy in PRIOR_YEARS:
            growth = 1.0 + 0.03 * (fy - PRIOR_YEARS[0])
            batch: list[tuple] = []
            for cc_code, (cc_id, cc_w, cc_budget_only) in cc_info.items():
                base = 80_000.0 * cc_w * growth
                # Actuals are always recorded at sub-nominal detail level.
                for p in range(1, 13):
                    for sn_id, share in nominal_weights.items():
                        batch.append((cal_id, fy, p, cc_id, sn_parent[sn_id], sn_id, "ACTUAL", amt(base * share, p, 0.08)))
                if cc_budget_only:
                    # Budget kept at Cost-Centre level for this CC (mixed grain).
                    for p in range(1, 13):
                        batch.append((cal_id, fy, p, cc_id, 0, 0, "BUDGET", amt(base, p, 0.03)))
                else:
                    for p in range(1, 13):
                        for sn_id, share in nominal_weights.items():
                            batch.append((cal_id, fy, p, cc_id, sn_parent[sn_id], sn_id, "BUDGET", amt(base * share, p, 0.03)))
            con.executemany(
                "INSERT INTO budget_actual VALUES (?,?,?,?,?,?,?,?)", batch)
    con.execute("CHECKPOINT")


def seed_demo_scenario(con, cc_info: dict) -> None:
    """FY2026 Budget scenario seeded from FY2025 Actuals at sub-nominal level, all CCs."""
    sid = con.execute(
        "INSERT INTO scenario (name, type, calendar_id, base_year, seed_level, seed_version, scope_node_id) "
        "VALUES ('FY2026 Budget (from FY2025 Actuals)', 'BUDGET', 1, 2025, 'SUBNOMINAL', 'ACTUAL', NULL) RETURNING id"
    ).fetchone()[0]
    for target_year in (2026, 2027, 2028):
        con.execute(
            """
            INSERT INTO scenario_line
            SELECT ?, ?, period_num, cost_centre_id, nominal_id, sub_nominal_id, amount
            FROM budget_actual
            WHERE calendar_id = 1 AND fiscal_year = 2025 AND version = 'ACTUAL'
              AND sub_nominal_id <> 0
            """,
            [sid, target_year],
        )
    con.execute("UPDATE scenario SET updated_at = now() WHERE id = ?", [sid])


def run_seed() -> None:
    con = get_db()
    cc_info = seed_ref_tree(con)
    seed_calendars(con)
    seed_facts(con, cc_info)
    seed_demo_scenario(con, cc_info)
    n_facts = con.execute("SELECT count(*) FROM budget_actual").fetchone()[0]
    print(f"Seeded: {con.execute('SELECT count(*) FROM ref_node').fetchone()[0]} ref nodes, "
          f"{n_facts} fact rows, "
          f"{con.execute('SELECT count(*) FROM scenario_line').fetchone()[0]} scenario lines.")
