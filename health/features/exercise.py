"""Training log — one row per exercise per entry.

Mirrors steps.py/measurements.py (see steps.py's docstring for the shape). The granularity
is deliberate: one row per exercise, carrying the summary the user actually says out loud
("bench 3x10 at 40kg, felt 7"), not one row per set. Per-set rows would model varying sets
exactly, but they'd also mean typing every set; the summary form is what gets logged in
practice, and a set that genuinely differs is just another entry.

Every measure except the exercise name is optional, because the same tool covers three
shapes of training: weights (sets/reps/weight_kg), bodyweight work (sets/reps, no weight),
and things measured in time (a plank, a class — duration_min, maybe no reps at all). RPE
is the subjective 1-10 "how hard was that", the number no device records.

Dedupe key is (date, exercise, ingested_at) — the append-per-entry shape food_log uses,
not the overwrite shape steps uses. Mentioning bench twice in a day means two pieces of
work, not a correction of the first, and silently overwriting the earlier one would lose
training history that can't be reconstructed. Corrections handle genuine mistakes:
`date` in the key is what makes this domain visible to query_log/edit_entry/delete_entry.
"""
from __future__ import annotations

import sqlite3

from health.core.contracts import Feature
from health.store import writer

SCHEMA = """
CREATE TABLE IF NOT EXISTS exercise (
  date            TEXT NOT NULL,
  exercise        TEXT NOT NULL,      -- lowercase movement name: bench press, squat, plank...
  source          TEXT NOT NULL,
  sets            INTEGER,            -- all four measures are optional: weights, bodyweight
  reps            INTEGER,            -- and timed work each use a different subset
  weight_kg       REAL,
  duration_min    REAL,
  rpe             INTEGER,            -- subjective effort, 1-10
  notes           TEXT NOT NULL DEFAULT '',
  raw             TEXT NOT NULL,
  ingested_at     TEXT NOT NULL,
  schema_version  INTEGER NOT NULL,
  PRIMARY KEY (date, exercise, ingested_at)  -- one row per entry; logging the same
                                             -- movement again ADDS, never overwrites
);
"""

RPE_MIN, RPE_MAX = 1, 10

TOOLS = [{
    "name": "log_exercise",
    "description": (
        "Log training the user did: one entry per exercise, with whatever they mentioned. "
        "'bench 3x10 at 40kg, felt 7' is sets=3, reps=10, weight_kg=40, rpe=7 — read the "
        "numbers straight off the message and never multiply or total anything yourself. "
        "Pass several exercises in one call when the message lists several. Every field "
        "except the exercise name is optional: leave weight_kg out for bodyweight work, "
        "use duration_min for timed work (a 60-second plank is duration_min 1), and only "
        "set rpe when they actually said how hard it felt (1 = easy, 10 = maximal). Put "
        "any aside they make — 'felt strong today', 'last set was a grind' — in notes. "
        "Each call ADDS entries: logging the same exercise again is a second piece of "
        "work, not a correction of the first."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "date": {
                "type": "string",
                "description": "ISO date YYYY-MM-DD. Use today if the user didn't specify one.",
            },
            "entries": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "exercise": {"type": "string", "description": "Movement name, e.g. 'bench press', 'squat', 'plank'."},
                        "sets": {"type": "integer", "description": "Number of sets, if stated."},
                        "reps": {"type": "integer", "description": "Reps per set, if stated. Omit for timed work."},
                        "weight_kg": {"type": "number", "description": "Load in kg. Omit for bodyweight work. Convert nothing — if they gave lbs, note that in notes and leave this out."},
                        "duration_min": {"type": "number", "description": "Minutes, for timed work (plank, yoga, a class)."},
                        "rpe": {"type": "integer", "description": "Subjective effort 1-10, only if the user said how hard it felt."},
                        "notes": {"type": "string", "description": "Anything else worth keeping, close to their words."},
                    },
                    "required": ["exercise"],
                },
            },
        },
        "required": ["date", "entries"],
    },
}]


def _int_or_none(value) -> int | None:
    return None if value is None else int(value)


def _float_or_none(value) -> float | None:
    return None if value is None else float(value)


def _rpe(value) -> int | None:
    # A bounded scale is only useful if it's actually bounded; an out-of-range value is a
    # misread, and the error goes back to the model rather than into the log.
    if value is None:
        return None
    rpe = int(value)
    if not RPE_MIN <= rpe <= RPE_MAX:
        raise ValueError(f"rpe must be {RPE_MIN}-{RPE_MAX} (got {rpe}); it is how hard the "
                         "work felt, not a weight or a rep count.")
    return rpe


def log_exercise(date: str, entries: list[dict] | None = None) -> dict:
    # Dumb executor: normalize the movement name, coerce types, hand each entry off.
    logged = []
    for entry in entries or []:
        name = str(entry.get("exercise") or "").strip().lower()
        if not name:
            raise ValueError("every entry needs an `exercise` name.")
        logged.append(writer.record("exercise", {
            "date": date,
            "exercise": name,
            "sets": _int_or_none(entry.get("sets")),
            "reps": _int_or_none(entry.get("reps")),
            "weight_kg": _float_or_none(entry.get("weight_kg")),
            "duration_min": _float_or_none(entry.get("duration_min")),
            "rpe": _rpe(entry.get("rpe")),
            "notes": str(entry.get("notes") or ""),
        }))
    return {"logged": logged, "count": len(logged)}


def project(conn: sqlite3.Connection, row: dict) -> None:
    # The single projection routine — used by live ingest AND rebuild_db. Idempotent.
    # Defaults first so a JSONL line written before a column existed still replays.
    row = {"sets": None, "reps": None, "weight_kg": None, "duration_min": None,
           "rpe": None, "notes": "", **row}
    conn.execute(
        "INSERT INTO exercise(date, exercise, source, sets, reps, weight_kg, duration_min, "
        "  rpe, notes, raw, ingested_at, schema_version) "
        "VALUES (:date, :exercise, :source, :sets, :reps, :weight_kg, :duration_min, "
        "  :rpe, :notes, :raw, :ingested_at, :schema_version) "
        "ON CONFLICT(date, exercise, ingested_at) DO UPDATE SET "
        "  source=excluded.source, sets=excluded.sets, reps=excluded.reps, "
        "  weight_kg=excluded.weight_kg, duration_min=excluded.duration_min, "
        "  rpe=excluded.rpe, notes=excluded.notes, raw=excluded.raw, "
        "  schema_version=excluded.schema_version",
        row,
    )


EXECUTORS = {"log_exercise": log_exercise}

FEATURE = Feature(
    name="exercise",
    tools=TOOLS,
    executors=EXECUTORS,
    schema=SCHEMA,
    domain="exercise",
    dedupe_key=("date", "exercise", "ingested_at"),
    project=project,
)
