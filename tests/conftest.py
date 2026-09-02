"""Shared fixtures + the test-isolation harness.

THE LOAD-BEARING BIT (read CLAUDE.md before touching): `DATA_DIR` and
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
os.environ.setdefault("MODEL", "claude-sonnet-5")
# Pin the targets to the shipped defaults: `config` loads `.env` with override=False,
# so values set here win over a developer's own TARGET_* and the rendered-block
# assertions stay byte-stable regardless of whose machine runs the suite.
# (`test_isolation_guardrail` asserts these equal `config.DEFAULT_TARGETS`.)
os.environ.update({"TARGET_KCAL": "2200", "TARGET_PROTEIN_G": "150", "TARGET_FIBRE_G": "28",
                   "TARGET_CARB_G": "250", "TARGET_FAT_G": "70"})

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from health import rebuild_db
from health.core import contracts, history, registry
from health.features import corrections, food
from health.store import connection, msg_log, writer

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
    advancing clock, and cleared in-process state (pending estimate, chat history)."""
    # Redirect every by-value copy of the data paths to this test's tmp dir.
    monkeypatch.setattr(writer, "DATA_DIR", tmp_path)
    monkeypatch.setattr(msg_log, "DATA_DIR", tmp_path)         # the transcript keeps its own copy
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
    food._offer.clear()
    corrections._listing.clear()
    history.clear()
    yield
    conn.close()
    food._pending.clear()
    food._offer.clear()
    corrections._listing.clear()
    history.clear()


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


# --- faking the Anthropic client -------------------------------------------------

class _Block:
    """Stand-in for one Anthropic content block. The harness only ever reads `.type`,
    `.text` (text blocks) and `.id`/`.name`/`.input` (tool_use blocks)."""

    def __init__(self, type_, **attrs):
        self.type = type_
        for k, v in attrs.items():
            setattr(self, k, v)


def text_block(text):
    return _Block("text", text=text)


def tool_block(name, input, id="tu-1"):
    return _Block("tool_use", id=id, name=name, input=input)


def raw_block(type_, **attrs):
    """A block of any other type — server_tool_use, web_search_tool_result, thinking.
    The harness must pass these through untouched rather than dispatch them."""
    return _Block(type_, **attrs)


def usage_block(input_tokens=0, output_tokens=0,
                cache_creation_input_tokens=0, cache_read_input_tokens=0):
    """The `usage` object every real response carries. Zeroed by default so a test that
    doesn't care about cost still exercises the recording path; pass real counts to assert
    on the cache telemetry."""
    return _Block("usage", input_tokens=input_tokens, output_tokens=output_tokens,
                  cache_creation_input_tokens=cache_creation_input_tokens,
                  cache_read_input_tokens=cache_read_input_tokens,
                  cache_creation=None, server_tool_use=None)


def system_text(call) -> str:
    """Flatten a recorded request's system blocks into one string. The harness sends two —
    a cached static block and a volatile one — so an assertion about prompt *content*
    shouldn't have to care which block a phrase landed in."""
    return "\n".join(b["text"] for b in call["system"])


class FakeAnthropic:
    """Scripted Anthropic client: returns queued responses and records every request.
    `calls[i]["messages"]` is what makes the harness's context assembly assertable."""

    def __init__(self):
        self.responses = []
        self.calls = []
        self.messages = self

    def script(self, *blocks_per_response):
        """Queue one response per argument; each argument is a list of content blocks, or a
        (blocks, stop_reason) / (blocks, stop_reason, container_id) /
        (blocks, stop_reason, container_id, usage) tuple when a non-default stop_reason, a
        server-side container, or specific token counts matter. An explicit `None` in the
        usage slot models a response that carries no usage at all."""
        self.responses.extend(blocks_per_response)

    def create(self, **kwargs):
        # Snapshot `messages`: the harness keeps appending to the very list it passed,
        # so recording it by reference would show post-call mutations.
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})
        if not self.responses:
            raise AssertionError("FakeAnthropic ran out of scripted responses")
        scripted = self.responses.pop(0)
        if not isinstance(scripted, tuple):
            scripted = (scripted,)
        # Explicit length checks, not tuple padding: padding can't express "slot present
        # but None", which is exactly what the no-usage case needs.
        blocks = scripted[0]
        stop_reason = scripted[1] if len(scripted) > 1 else None
        container = scripted[2] if len(scripted) > 2 else None
        usage = scripted[3] if len(scripted) > 3 else usage_block()
        return _Block("message", content=blocks,
                      stop_reason=stop_reason or "end_turn",
                      # Real responses carry no `container` unless a container-backed
                      # server tool ran; None models that.
                      container=_Block("container", id=container) if container else None,
                      usage=usage)


@pytest.fixture
def fake_client(monkeypatch):
    """Replace the harness's module-level Anthropic client with a scripted fake."""
    from health.core import harness

    fake = FakeAnthropic()
    monkeypatch.setattr(harness, "client", fake)
    return fake


# --- small helpers used across test modules -------------------------------------

def rows(conn, sql, params=()):
    """Fetch rows as plain dicts (sqlite3.Row -> dict) for easy assertions."""
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def log_steps(date, count):
    """One day of steps, returning that single stored row.

    The executor takes a list (steps arrive as pasted backlogs), but most tests care
    about one day and its row, so this keeps them readable.
    """
    from health.features import steps
    return steps.log_steps([{"date": date, "count": count}])["logged"][0]
