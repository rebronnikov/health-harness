"""steps + weight: the simple (date, source) overwrite features. Re-logging a day
overwrites in the projection but still appends to the JSONL log of record.
"""
import json

from health.features import steps, weight
from health.store.connection import get_conn

from conftest import rows

DAY = "2026-07-14"


def _lines(tmp_path, name):
    return (tmp_path / name).read_text().splitlines()


def test_log_steps_coerces_to_int(ctx):
    ctx(source="telegram")
    row = steps.log_steps(DAY, 9000)
    assert row["count"] == 9000 and isinstance(row["count"], int)


def test_relog_same_day_overwrites_projection_but_appends_jsonl(ctx, tmp_path):
    ctx(source="telegram")
    steps.log_steps(DAY, 9000)
    steps.log_steps(DAY, 9500)
    db = rows(get_conn(), "SELECT count FROM steps")
    assert db == [{"count": 9500}]                              # one row, last count wins
    assert len(_lines(tmp_path, "steps.jsonl")) == 2            # both appended to the record


def test_distinct_sources_coexist_same_day(ctx):
    ctx(source="phone")
    steps.log_steps(DAY, 9000)
    ctx(source="watch")
    steps.log_steps(DAY, 12000)
    counts = {r["source"]: r["count"] for r in rows(get_conn(), "SELECT source, count FROM steps")}
    assert counts == {"phone": 9000, "watch": 12000}


def test_log_weight_is_real_and_overwrites(ctx):
    ctx(source="telegram")
    row = weight.log_weight(DAY, 72.6)
    assert row["weight_kg"] == 72.6 and isinstance(row["weight_kg"], float)
    weight.log_weight(DAY, 72.0)
    db = rows(get_conn(), "SELECT weight_kg FROM weight")
    assert db == [{"weight_kg": 72.0}]
