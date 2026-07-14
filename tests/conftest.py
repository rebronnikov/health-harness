"""Shared fixtures + the test-isolation harness.

THE LOAD-BEARING BIT (read `plan`/CLAUDE.md before touching): `DATA_DIR` and
`DB_PATH` are resolved at import time and copied by value into four modules
(`config`, `writer`, `connection`, `rebuild_db`). Setting DATA_DIR/DB_PATH in the
environment BELOW must happen before the first `health` import, so every one of
those copies resolves to a throwaway temp dir — that is what guarantees no test
ever writes to the owner's real `data/`. The per-test `isolate` fixture then gives
each test its own fresh dir + DB (and redirects rebuild_db's own copies).
"""
# ── MUST be first: redirect all data paths before importing anything from health ──
import os
import tempfile
from pathlib import Path

_SESSION_TMP = Path(tempfile.mkdtemp(prefix="health-tests-"))
os.environ["DATA_DIR"] = str(_SESSION_TMP)                     # all 4 DATA_DIR/DB_PATH copies land here
os.environ["DB_PATH"] = str(_SESSION_TMP / "session.sqlite")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-not-real")   # so importing harness's Anthropic() can't raise
os.environ.setdefault("MODEL", "claude-sonnet-4-6")

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from health import rebuild_db
from health.core import contracts, registry
from health.features import food
from health.store import connection, writer

# Noon on the app's "today" — deliberately NOT evening, so the food suggestion
# gate (hour >= 16) is off unless a test overrides the timestamp.
DEFAULT_TS = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)


class _AdvancingClock:
    """Drop-in for `writer.datetime`. `record()` only ever calls `.now(tz)`; we
    return a strictly-increasing aware datetime so successive writes get distinct
    `ingested_at` values. Without this, two writes in the same real microsecond
    collide on food_log's (date, meal, food_slug, ingested_at) PK — a second
    helping would overwrite the first, and rebuild replay would be ambiguous."""

    def __init__(self, base: datetime):
        self._base = base
        self._tick = 0

    def now(self, tz=None):
        self._tick += 1
        return (self._base + timedelta(seconds=self._tick)).astimezone(tz or timezone.utc)


@pytest.fixture(autouse=True)
def isolate(monkeypatch, tmp_path):
    """Per-test hermetic sandbox: fresh JSONL dir + fresh SQLite projection, an
    advancing clock, and a cleared pending-estimate slot."""
    # Redirect every by-value copy of the data paths to this test's tmp dir.
    monkeypatch.setattr(writer, "DATA_DIR", tmp_path)
    monkeypatch.setattr(connection, "DATA_DIR", tmp_path)
    monkeypatch.setattr(connection, "DB_PATH", tmp_path / "health.sqlite")
    monkeypatch.setattr(rebuild_db, "DATA_DIR", tmp_path)
    # rebuild() does os.remove(DB_PATH) then re-opens it — keep it OFF the live DB file.
    monkeypatch.setattr(rebuild_db, "DB_PATH", tmp_path / "rebuilt.sqlite")

    # A file DB (not :memory:) so the rebuild round-trip's second connection sees the data.
    conn = sqlite3.connect(str(tmp_path / "health.sqlite"), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    registry.init_schema(conn)
    conn.commit()
    monkeypatch.setattr(connection, "_conn", conn)            # get_conn() returns this everywhere

    monkeypatch.setattr(writer, "datetime", _AdvancingClock(DEFAULT_TS))
    food._pending.clear()
    yield
    conn.close()
    food._pending.clear()


@pytest.fixture
def ctx():
    """Set the `current_message` contextvar for a turn (mirrors seed_catalog.load).
    Returns a setter; stacked calls simulate successive turns. All tokens are reset
    at teardown so context never leaks into the next test."""
    tokens = []

    def _set(text="msg", source="test", reply_handle="chat-1", timestamp=DEFAULT_TS):
        msg = contracts.NormalizedMessage(
            source=source, reply_handle=reply_handle, timestamp=timestamp, text=text
        )
        tokens.append(contracts.current_message.set(msg))
        return msg

    yield _set
    for token in reversed(tokens):
        contracts.current_message.reset(token)


@pytest.fixture
def catalog(ctx):
    """A small saved-food catalog for log/targets/suggestion tests.
    skyr: per-100g mass food with a 'typical serving' note; banana: an `each`
    food (scales by count); salmon: per-100g mass food (unit-mismatch fodder)."""
    ctx(source="seed")
    food.save_food("skyr", 100, "g", 51, 9, 3.4, 0.1, 0, notes="typical serving 210g")
    food.save_food("banana", 1, "each", 108, 1.44, 24.4, 0.12, 0)
    food.save_food("salmon", 100, "g", 195, 23.1, 0, 11.1, 0)
    return food


# --- small helpers used across test modules -------------------------------------

def rows(conn, sql, params=()):
    """Fetch rows as plain dicts (sqlite3.Row -> dict) for easy assertions."""
    return [dict(r) for r in conn.execute(sql, params).fetchall()]
