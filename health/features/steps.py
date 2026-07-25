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
        "Log the user's step counts. Takes a LIST of days, so a whole backlog pasted from "
        "another app ('14 Jul 8,213 / 15 Jul 10,455 / ...') goes in one call — send one "
        "entry per day rather than calling this repeatedly. A single day is a list of one. "
        "One row per (date, source); re-logging a day overwrites that day's count. Read "
        "the numbers straight off the message: strip thousands separators, but never add, "
        "average or otherwise compute a count yourself."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "entries": {
                "type": "array",
                "description": "One entry per day.",
                "items": {
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
            },
        },
        "required": ["entries"],
    },
}]


def log_steps(entries: list[dict] | None = None) -> dict:
    # Dumb executor: build the SI-unit fields, hand off to the shared writer, once per day.
    # A list rather than a single day because steps arrive in backlogs — they're logged in
    # another app and pasted here days later. The harness would dispatch N parallel calls
    # happily enough, but one call is one lock pass and can't be split across turns.
    logged = [writer.record("steps", {"date": e["date"], "count": int(e["count"])})
              for e in entries or []]
    return {"logged": logged, "days": len(logged)}


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
