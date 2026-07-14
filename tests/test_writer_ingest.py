"""The single ingest discipline: runtime-stamped provenance, the deliberate
JSONL-then-projection-then-sidecar write order, and the contextvar guard.
"""
import hashlib
import json

import pytest

from health.features import steps
from health.store import writer
from health.store.connection import get_conn

from conftest import rows

DAY = "2026-07-14"


def _jsonl(tmp_path, name):
    p = tmp_path / name
    return [json.loads(line) for line in p.read_text().splitlines()] if p.exists() else []


def test_provenance_is_stamped_by_runtime_not_the_tool(ctx):
    ctx(source="telegram", text="9000 steps")
    row = steps.log_steps(DAY, 9000)                             # tool passes only date+count
    assert row["raw"] == "9000 steps"                           # from the in-flight message
    assert row["source"] == "telegram"
    assert row["schema_version"] == 1
    assert isinstance(row["ingested_at"], str) and "T" in row["ingested_at"]


def test_record_writes_jsonl_projection_and_sidecar(ctx, tmp_path):
    ctx(source="telegram", text="9000 steps")
    row = steps.log_steps(DAY, 9000)

    domain_lines = _jsonl(tmp_path, "steps.jsonl")
    assert domain_lines == [row]                                # system of record

    db = rows(get_conn(), "SELECT date, count, source, raw FROM steps")
    assert db == [{"date": DAY, "count": 9000, "source": "telegram", "raw": "9000 steps"}]

    (sidecar,) = _jsonl(tmp_path, "ingest_log.jsonl")           # provenance audit line
    assert sidecar["domain"] == "steps"
    assert sidecar["dedupe_key"] == [DAY, "telegram"]
    assert sidecar["raw_hash"] == hashlib.sha256(b"9000 steps").hexdigest()
    assert sidecar["parser_version"] and sidecar["model_version"]


def test_jsonl_is_written_before_projection(ctx, tmp_path, monkeypatch):
    ctx(source="telegram", text="9000 steps")

    def boom(conn, row):
        raise RuntimeError("projection failed")

    monkeypatch.setattr(steps.FEATURE, "project", boom)          # Feature is a plain dataclass
    with pytest.raises(RuntimeError):
        steps.log_steps(DAY, 9000)

    assert len(_jsonl(tmp_path, "steps.jsonl")) == 1             # record survives in the system of record
    assert rows(get_conn(), "SELECT * FROM steps") == []        # projection never committed
    assert _jsonl(tmp_path, "ingest_log.jsonl") == []           # sidecar comes AFTER the projection


def test_record_outside_a_turn_raises_lookuperror():
    # No ctx() -> the current_message contextvar is unset; provenance can't be fabricated.
    with pytest.raises(LookupError):
        writer.record("steps", {"date": DAY, "count": 1})


def test_raw_override_wins_over_message_text(ctx):
    ctx(source="seed", text="the yes message")
    row = writer.record("steps", {"date": DAY, "count": 1}, raw="the original food message")
    assert row["raw"] == "the original food message"
