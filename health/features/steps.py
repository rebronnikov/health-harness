"""Reference feature — the template every later feature copies.

Shape of a feature: SCHEMA (DDL) + TOOLS (Anthropic schemas) + a thin executor +
a `project()` upsert + the `FEATURE = Feature(...)` line. The executor is dumb: it
builds SI-unit fields and hands them to writer.record(); it never sees raw text,
time, source, or SQL. `project()` is the one idempotent upsert used by both live
ingest (via writer) and rebuild_db.
"""
from __future__ import annotations

import sqlite3

from health.core.contracts import Feature
from health.store import writer

SCHEMA = """
CREATE TABLE IF NOT EXISTS steps (
  date            TEXT NOT NULL,
  source          TEXT NOT NULL,
  count           INTEGER NOT NULL,
  raw             TEXT NOT NULL,
  ingested_at     TEXT NOT NULL,
  schema_version  INTEGER NOT NULL,
  PRIMARY KEY (date, source)          -- dedupe key, enforced by the DB
);
"""

TOOLS = [{
    "name": "log_steps",
    "description": (
        "Log the user's step count for a day. One row per (date, source); "
        "re-logging the same day overwrites the previous count. Pass the step "
        "count as a plain integer — do not do any arithmetic yourself."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "date": {
                "type": "string",
                "description": "ISO date YYYY-MM-DD. Use today if the user didn't specify one.",
            },
            "count": {
                "type": "integer",
                "description": "Step count as a whole number.",
            },
        },
        "required": ["date", "count"],
    },
}]


def log_steps(date: str, count: int) -> dict:
    # Dumb executor: build the SI-unit fields, hand off to the shared writer.
    return writer.record("steps", {"date": date, "count": int(count)})


def project(conn: sqlite3.Connection, row: dict) -> None:
    # The single projection routine — used by live ingest AND rebuild_db. Idempotent.
    conn.execute(
        "INSERT INTO steps(date, source, count, raw, ingested_at, schema_version) "
        "VALUES (:date, :source, :count, :raw, :ingested_at, :schema_version) "
        "ON CONFLICT(date, source) DO UPDATE SET "
        "  count=excluded.count, raw=excluded.raw, "
        "  ingested_at=excluded.ingested_at, schema_version=excluded.schema_version",
        row,
    )


EXECUTORS = {"log_steps": log_steps}

FEATURE = Feature(
    name="steps",
    tools=TOOLS,
    executors=EXECUTORS,
    schema=SCHEMA,
    domain="steps",
    dedupe_key=("date", "source"),
    project=project,
)
