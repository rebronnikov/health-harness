"""The single ingest discipline.

Every feature executor calls `record()` and nothing else — they never touch JSONL,
provenance, time, or SQL. This keeps the JSONL-as-system-of-record contract in ONE
place and keeps the core feature-agnostic (this module has no per-domain branches;
it is parameterized entirely by Feature metadata from the registry).

Write order is deliberate: the canonical JSONL line is appended FIRST, so a crash
mid-write leaves the row in the system of record and the projection self-heals on
the next `rebuild_db`. The reverse (projection ahead of the record) can never happen.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections.abc import Callable
from datetime import datetime, timezone

from health.config import DATA_DIR, MODEL_VERSION, PARSER_VERSION
from health.core.contracts import get_context
from health.store.connection import get_conn

SCHEMA_VERSION = 1

# Serializes the shared connection + append handles across to_thread workers.
_lock = threading.Lock()


def now() -> datetime:
    """The ingest clock. One function so anything comparing against an `ingested_at` reads
    the SAME clock that stamped it — food's duplicate window does, and a second source of
    time there would silently never match under a patched clock (or a clock skew)."""
    return datetime.now(timezone.utc)


def record(domain: str, fields: dict, raw: str | None = None, *,
           precondition: Callable[[sqlite3.Connection], None] | None = None) -> dict:
    """Append one canonical row to data/<domain>.jsonl, upsert it into the SQLite
    projection, and log provenance. Returns the stored row for the executor to echo.

    `fields` carries only the feature-supplied, SI-unit values (e.g. {"date":..., "count":...}).
    `raw`, `source`, `ingested_at`, and `schema_version` are stamped here from the
    runtime — never authored by the model.

    `raw` defaults to the in-flight message text. A feature may override it with a
    DIFFERENT runtime-captured message (e.g. a deferred-commit flow where the food
    estimate was proposed on an earlier turn and committed on the user's "yes"). It is
    still runtime-supplied, never model-authored, so the provenance invariant holds.

    A precondition can raise before any append. It runs under the writer lock and
    a SQLite write transaction so the checked row cannot change before projection.
    """
    from health.core.registry import feature_by_domain  # lazy: avoid import cycle

    feature = feature_by_domain(domain)
    ctx = get_context()
    raw = raw if raw is not None else (ctx.text or "")
    row = {
        "schema_version": SCHEMA_VERSION,
        **fields,
        "raw": raw,
        "ingested_at": now().isoformat(),
        "source": ctx.source,
    }
    dedupe = feature.dedupe_value(row)
    raw_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()

    with _lock, get_conn() as conn:
        if precondition is not None:
            conn.execute("BEGIN IMMEDIATE")
            precondition(conn)
        _append_jsonl(DATA_DIR / f"{domain}.jsonl", row)          # 1. system of record (first)
        feature.project(conn, row)                              # 2. projection (idempotent)
        _append_jsonl(DATA_DIR / "ingest_log.jsonl", {            # 3. provenance sidecar (audit)
            "raw_hash": raw_hash,
            "domain": domain,
            "dedupe_key": list(dedupe),
            "parser_version": PARSER_VERSION,
            "model_version": MODEL_VERSION,
            "ingested_at": row["ingested_at"],
        })
    return row


def _append_jsonl(path, obj: dict) -> None:
    # sort_keys -> stable, diffable lines; flush -> durable past the Python buffer.
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False, sort_keys=True) + "\n")
        f.flush()
