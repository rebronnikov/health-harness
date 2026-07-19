"""steps + weight: the simple (date, source) overwrite features. Re-logging a day
overwrites in the projection but still appends to the JSONL log of record.
"""
import json

from health.features import measurements, steps, weight
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


def test_log_measurement_is_real_and_normalizes_metric(ctx):
    ctx(source="telegram")
    row = measurements.log_measurement(DAY, " Waist ", 78.8)
    assert row["value_cm"] == 78.8 and isinstance(row["value_cm"], float)
    assert row["metric"] == "waist"                            # stripped + lowercased


def test_measurement_relog_same_metric_overwrites_but_appends_jsonl(ctx, tmp_path):
    ctx(source="telegram")
    measurements.log_measurement(DAY, "waist", 78.8)
    measurements.log_measurement(DAY, "waist", 77.0)
    db = rows(get_conn(), "SELECT value_cm FROM measurements")
    assert db == [{"value_cm": 77.0}]                          # one row, last value wins
    assert len(_lines(tmp_path, "measurements.jsonl")) == 2    # both appended to the record


def test_distinct_metrics_coexist_same_day(ctx):
    ctx(source="telegram")
    measurements.log_measurement(DAY, "waist", 78.8)
    measurements.log_measurement(DAY, "hips", 94.1)
    vals = {r["metric"]: r["value_cm"]
            for r in rows(get_conn(), "SELECT metric, value_cm FROM measurements")}
    assert vals == {"waist": 78.8, "hips": 94.1}


def test_measurement_relog_different_source_overwrites(ctx):
    # Key is (date, metric) only — a re-log overwrites regardless of source (one
    # value per metric per day).
    ctx(source="cli")
    measurements.log_measurement(DAY, "waist", 78.8)
    ctx(source="telegram")
    measurements.log_measurement(DAY, "waist", 77.6)
    db = rows(get_conn(), "SELECT source, value_cm FROM measurements")
    assert db == [{"source": "telegram", "value_cm": 77.6}]     # single row, latest wins
