"""The system-of-record invariant: rebuilding the projection from the JSONL logs
reproduces exactly what live ingest produced. `project()` is the one upsert used
by both paths, so a faithful replay is the whole guarantee behind "JSONL is truth,
SQLite is disposable".
"""
import sqlite3

import pytest

from health import rebuild_db
from health.features import food, measurements, steps, weight
from health.store.connection import get_conn

DAY = "2026-07-14"
TABLES = {
    "steps": "date, source",
    "weight": "date, source",
    "measurements": "date, metric",
    "food_catalog": "slug",
    "food_log": "date, meal, food_slug, ingested_at",
}


def _snapshot(conn):
    return {t: [dict(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY {pk}")]
            for t, pk in TABLES.items()}


def _write_a_representative_mix(ctx):
    ctx(source="telegram", text="9000 steps")
    steps.log_steps(DAY, 9000)
    steps.log_steps(DAY, 9500)                                   # re-log -> overwrites in projection
    ctx(source="telegram", text="weight 72.6")
    weight.log_weight(DAY, 72.6)
    ctx(source="telegram", text="waist 78.8")
    measurements.log_measurement(DAY, "waist", 78.8)
    measurements.log_measurement(DAY, "waist", 78.1)            # re-log -> overwrites in projection
    ctx(source="seed", text="save skyr")
    food.save_food("skyr", 100, "g", 51, 9, 3.4, 0.1, 0, notes="typical serving 210g")
    food.save_food("skyr", 100, "g", 52, 9, 3.4, 0.1, 0)        # re-save -> upsert same slug
    ctx(source="telegram", text="210g skyr")
    food.log_food(DAY, "snack", [{"name": "skyr", "quantity": 210, "unit": "g"}])
    food.log_food(DAY, "snack", [{"name": "skyr", "quantity": 210, "unit": "g"}])  # second helping -> new row


def test_rebuild_reproduces_live_projection(ctx, tmp_path):
    _write_a_representative_mix(ctx)
    live = _snapshot(get_conn())

    rebuild_db.rebuild()                                         # replays tmp_path/*.jsonl -> rebuilt.sqlite

    rebuilt_conn = sqlite3.connect(str(tmp_path / "rebuilt.sqlite"))
    rebuilt_conn.row_factory = sqlite3.Row
    try:
        assert _snapshot(rebuilt_conn) == live                  # table-by-table identical
    finally:
        rebuilt_conn.close()

    # Sanity: the mix actually exercised overwrite + append, not a trivial one-row DB.
    assert len(live["steps"]) == 1                              # re-log collapsed
    assert len(live["food_catalog"]) == 1                      # re-save collapsed
    assert len(live["food_log"]) == 2                          # second helping preserved


def test_rebuild_does_not_replay_the_ingest_log(ctx, tmp_path):
    _write_a_representative_mix(ctx)
    rebuild_db.rebuild()
    rebuilt_conn = sqlite3.connect(str(tmp_path / "rebuilt.sqlite"))
    try:
        names = {r[0] for r in rebuilt_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        rebuilt_conn.close()
    assert "ingest_log" not in names                            # provenance sidecar is not a projected domain
    assert TABLES.keys() <= names


def test_rebuild_skip_bad_tail(ctx, tmp_path):
    ctx(source="telegram", text="9000 steps")
    steps.log_steps(DAY, 9000)
    with open(tmp_path / "steps.jsonl", "a") as f:              # simulate a crash mid-append
        f.write('{"date": "2026-07-15", "count": ')
    with pytest.raises(RuntimeError):
        rebuild_db.rebuild(skip_bad_tail=False)
    assert rebuild_db.rebuild(skip_bad_tail=True)               # tolerated -> returns the db path

    rebuilt_conn = sqlite3.connect(str(tmp_path / "rebuilt.sqlite"))
    try:
        count = rebuilt_conn.execute("SELECT COUNT(*) FROM steps").fetchone()[0]
    finally:
        rebuilt_conn.close()
    assert count == 1                                           # only the good line replayed
