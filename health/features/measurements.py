"""Body-measurements feature — a generic (date, metric) scalar log in cm.

Mirrors steps.py/weight.py (see steps.py's docstring for the shape), adding one
dimension: `metric` (waist, hips, chest, biceps, …) so a single tool covers every
body measurement instead of one feature per body part. A thin executor builds the
SI-unit fields (cm) and hands them to writer.record(); one idempotent project()
upsert serves both live ingest and rebuild_db. Dedupe key is (date, metric): one
value per metric per day, re-logging the same metric/day overwrites (regardless of
source). Measurements are fractional, so value_cm is REAL, not INTEGER.
"""
from __future__ import annotations

import sqlite3

from health.core.contracts import Feature
from health.store import writer

SCHEMA = """
CREATE TABLE IF NOT EXISTS measurements (
  date            TEXT NOT NULL,
  metric          TEXT NOT NULL,      -- lowercase body-part slug: waist, hips, chest, biceps...
  source          TEXT NOT NULL,
  value_cm        REAL NOT NULL,
  raw             TEXT NOT NULL,
  ingested_at     TEXT NOT NULL,
  schema_version  INTEGER NOT NULL,
  PRIMARY KEY (date, metric)          -- dedupe key, enforced by the DB (one value per metric/day)
);
"""

TOOLS = [{
    "name": "log_measurement",
    "description": (
        "Log a body measurement in centimetres. One row per (date, metric); "
        "re-logging the same metric on the same day overwrites the previous value. "
        "'metric' is a lowercase body-part name (waist, hips, chest, biceps, thigh, "
        "neck, ...). Pass the value in centimetres as a plain number — do not do any "
        "arithmetic yourself."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "date": {
                "type": "string",
                "description": "ISO date YYYY-MM-DD. Use today if the user didn't specify one.",
            },
            "metric": {
                "type": "string",
                "description": "Lowercase body-part name, e.g. waist, hips, chest, biceps.",
            },
            "value_cm": {
                "type": "number",
                "description": "Measurement in centimetres.",
            },
        },
        "required": ["date", "metric", "value_cm"],
    },
}]


def log_measurement(date: str, metric: str, value_cm: float) -> dict:
    # Dumb executor: normalize the metric slug, build the SI-unit fields, hand off.
    return writer.record(
        "measurements",
        {"date": date, "metric": str(metric).strip().lower(), "value_cm": float(value_cm)},
    )


def project(conn: sqlite3.Connection, row: dict) -> None:
    # The single projection routine — used by live ingest AND rebuild_db. Idempotent.
    conn.execute(
        "INSERT INTO measurements(date, metric, source, value_cm, raw, ingested_at, schema_version) "
        "VALUES (:date, :metric, :source, :value_cm, :raw, :ingested_at, :schema_version) "
        "ON CONFLICT(date, metric) DO UPDATE SET "
        "  source=excluded.source, value_cm=excluded.value_cm, raw=excluded.raw, "
        "  ingested_at=excluded.ingested_at, schema_version=excluded.schema_version",
        row,
    )


EXECUTORS = {"log_measurement": log_measurement}

FEATURE = Feature(
    name="measurements",
    tools=TOOLS,
    executors=EXECUTORS,
    schema=SCHEMA,
    domain="measurements",
    dedupe_key=("date", "metric"),
    project=project,
)
