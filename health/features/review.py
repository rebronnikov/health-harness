"""Read-only analytics over the SQLite projection — "how was my week?".

The first feature that only reads. It declares no schema, domain or project, which
`Feature`'s defaults already allow and `ingesting_features()` filters out, so it adds a
tool without adding a system of record.

It exists because of the arithmetic boundary. Claude is forbidden from adding or
averaging anything, so a multi-day question has no answer unless something computes it —
handing over seven days of rows and asking for a mean is exactly the failure mode the
rule prevents. Everything here is computed in SQL and Python and handed back as finished
numbers for Claude to phrase.

Reads the projection, never the JSONL: the projection is what corrections have already
been applied to, so a deleted row is genuinely gone from a summary.
"""
from __future__ import annotations

import sqlite3
from datetime import date

from health.core.contracts import Feature
from health.features import targets
from health.store.connection import get_conn

_MACROS = ("kcal", "protein_g", "carb_g", "fat_g", "fibre_g")


def _days_in_range(start_date: str, end_date: str) -> int:
    try:
        span = (date.fromisoformat(end_date) - date.fromisoformat(start_date)).days + 1
    except ValueError:
        return 0
    return max(0, span)


def _food(conn: sqlite3.Connection, start_date: str, end_date: str) -> dict:
    rows = conn.execute(
        "SELECT date, "
        "       SUM(kcal)      AS kcal, "
        "       SUM(protein_g) AS protein_g, "
        "       SUM(carb_g)    AS carb_g, "
        "       SUM(fat_g)     AS fat_g, "
        "       SUM(fibre_g)   AS fibre_g "
        "FROM food_log WHERE date BETWEEN ? AND ? GROUP BY date ORDER BY date",
        (start_date, end_date),
    ).fetchall()
    per_day = [{"date": r["date"], **{k: round(r[k], 1) for k in _MACROS}} for r in rows]

    out = {"per_day": per_day, "days_logged": len(per_day), "targets": targets.TARGETS}
    if not per_day:
        return {**out, "average": None, "days_over_kcal_ceiling": 0,
                "days_protein_floor_met": 0, "days_fibre_floor_met": 0}

    t = targets.TARGETS
    # Floors count as met within the same tolerance the evening suggestions use, so a
    # summary can't say "missed" on a day the bot already called done.
    return {
        **out,
        "average": {k: round(sum(d[k] for d in per_day) / len(per_day), 1) for k in _MACROS},
        "days_over_kcal_ceiling": sum(1 for d in per_day if d["kcal"] > t["kcal"]),
        "days_protein_floor_met": sum(
            1 for d in per_day if d["protein_g"] >= t["protein_g"] - targets.PROTEIN_MET_TOL_G),
        "days_fibre_floor_met": sum(
            1 for d in per_day if d["fibre_g"] >= t["fibre_g"] - targets.FIBRE_MET_TOL_G),
    }


def _steps(conn: sqlite3.Connection, start_date: str, end_date: str) -> dict:
    # One value per day: steps are keyed (date, source), and two sources are two readings
    # of the same walking, so the fullest one is the day's count — never their sum.
    rows = conn.execute(
        "SELECT date, MAX(count) AS count FROM steps "
        "WHERE date BETWEEN ? AND ? GROUP BY date ORDER BY date",
        (start_date, end_date),
    ).fetchall()
    if not rows:
        return {"days_logged": 0, "total": 0, "average": None, "best": None}
    counts = [r["count"] for r in rows]
    best = max(rows, key=lambda r: r["count"])
    return {
        "days_logged": len(rows),
        "total": sum(counts),
        "average": round(sum(counts) / len(counts)),
        "best": {"date": best["date"], "count": best["count"]},
    }


def _weight(conn: sqlite3.Connection, start_date: str, end_date: str) -> dict:
    # Same reasoning as steps: one value per day, averaged if two sources disagree.
    rows = conn.execute(
        "SELECT date, AVG(weight_kg) AS kg FROM weight "
        "WHERE date BETWEEN ? AND ? GROUP BY date ORDER BY date",
        (start_date, end_date),
    ).fetchall()
    if not rows:
        return {"days_logged": 0, "first": None, "last": None,
                "delta_kg": None, "min_kg": None, "max_kg": None}
    kgs = [r["kg"] for r in rows]
    return {
        "days_logged": len(rows),
        "first": {"date": rows[0]["date"], "weight_kg": round(rows[0]["kg"], 2)},
        "last": {"date": rows[-1]["date"], "weight_kg": round(rows[-1]["kg"], 2)},
        "delta_kg": round(rows[-1]["kg"] - rows[0]["kg"], 2),   # negative = lost
        "min_kg": round(min(kgs), 2),
        "max_kg": round(max(kgs), 2),
    }


def _measurements(conn: sqlite3.Connection, start_date: str, end_date: str) -> dict:
    # The latest reading per metric inside the range — a body measurement is a level,
    # not something you average over a week.
    rows = conn.execute(
        "SELECT metric, date, value_cm FROM measurements m WHERE date BETWEEN ? AND ? "
        "  AND date = (SELECT MAX(date) FROM measurements m2 "
        "              WHERE m2.metric = m.metric AND m2.date BETWEEN ? AND ?) "
        "ORDER BY metric",
        (start_date, end_date, start_date, end_date),
    ).fetchall()
    return {r["metric"]: {"date": r["date"], "value_cm": r["value_cm"]} for r in rows}


def get_summary(start_date: str, end_date: str) -> dict:
    conn = get_conn()
    return {
        "start_date": start_date,
        "end_date": end_date,
        "days_in_range": _days_in_range(start_date, end_date),
        "food": _food(conn, start_date, end_date),
        "steps": _steps(conn, start_date, end_date),
        "weight": _weight(conn, start_date, end_date),
        "measurements": _measurements(conn, start_date, end_date),
    }


TOOLS = [{
    "name": "get_summary",
    "description": (
        "Totals, averages and trends across a DATE RANGE — 'how was my week?', 'am I "
        "hitting my protein?', 'is my weight moving?'. Every number is computed for you: "
        "NEVER add, average, or recompute any of them yourself, and never work out a "
        "figure the result doesn't contain — just narrate what comes back. "
        "For one day's totals use get_day_totals instead; for individual entries ('what "
        "did I eat on Tuesday?') use query_log. Compare `days_logged` with "
        "`days_in_range` and say so when the period has gaps, since averages over a "
        "half-logged week mislead. Keep the reply short enough to read on a phone: lead "
        "with the averages against the targets, then anything notable."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "start_date": {"type": "string", "description": "ISO date YYYY-MM-DD, inclusive."},
            "end_date": {"type": "string", "description": "ISO date YYYY-MM-DD, inclusive."},
        },
        "required": ["start_date", "end_date"],
    },
}]


FEATURE = Feature(
    name="review",
    tools=TOOLS,
    executors={"get_summary": get_summary},
    # No schema/domain/project: read-only, so it owns no system of record.
)
