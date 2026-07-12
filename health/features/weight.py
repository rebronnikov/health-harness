"""Body-weight feature — mirrors steps.py (see its docstring for the shape).

Same logic as steps: a thin executor builds SI-unit fields (kg) and hands them to
writer.record(); one idempotent project() upsert serves both live ingest and
rebuild_db. Dedupe key is (date, source): one weight per day per source, re-logging
the same day overwrites. Weight is fractional, so weight_kg is REAL, not INTEGER.
"""
from __future__ import annotations

import sqlite3

from health.core.contracts import Feature
from health.store import writer

SCHEMA = """
CREATE TABLE IF NOT EXISTS weight (
  date            TEXT NOT NULL,
  source          TEXT NOT NULL,
  weight_kg       REAL NOT NULL,
  raw             TEXT NOT NULL,
  ingested_at     TEXT NOT NULL,
  schema_version  INTEGER NOT NULL,
  PRIMARY KEY (date, source)          -- dedupe key, enforced by the DB
);
"""

TOOLS = [{
    "name": "log_weight",
    "description": (
        "Log the user's body weight for a day. One row per (date, source); "
        "re-logging the same day overwrites the previous weight. Pass the weight "
        "in kilograms as a plain number — do not do any arithmetic yourself."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "date": {
                "type": "string",
                "description": "ISO date YYYY-MM-DD. Use today if the user didn't specify one.",
            },
            "weight_kg": {
                "type": "number",
                "description": "Body weight in kilograms.",
            },
        },
        "required": ["date", "weight_kg"],
    },
}]


def log_weight(date: str, weight_kg: float) -> dict:
    # Dumb executor: build the SI-unit fields, hand off to the shared writer.
    return writer.record("weight", {"date": date, "weight_kg": float(weight_kg)})


def project(conn: sqlite3.Connection, row: dict) -> None:
    # The single projection routine — used by live ingest AND rebuild_db. Idempotent.
    conn.execute(
        "INSERT INTO weight(date, source, weight_kg, raw, ingested_at, schema_version) "
        "VALUES (:date, :source, :weight_kg, :raw, :ingested_at, :schema_version) "
        "ON CONFLICT(date, source) DO UPDATE SET "
        "  weight_kg=excluded.weight_kg, raw=excluded.raw, "
        "  ingested_at=excluded.ingested_at, schema_version=excluded.schema_version",
        row,
    )


EXECUTORS = {"log_weight": log_weight}

FEATURE = Feature(
    name="weight",
    tools=TOOLS,
    executors=EXECUTORS,
    schema=SCHEMA,
    domain="weight",
    dedupe_key=("date", "source"),
    project=project,
)
