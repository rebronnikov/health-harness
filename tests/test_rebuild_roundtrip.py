"""The system-of-record invariant: rebuilding the projection from the JSONL logs
reproduces exactly what live ingest produced. `project()` is the one upsert used
by both paths, so a faithful replay is the whole guarantee behind "JSONL is truth,
SQLite is disposable".
"""
import sqlite3

import pytest

from health import rebuild_db
from health.features import corrections, exercise, food, measurements, steps, weight
from health.store.connection import get_conn

from conftest import log_steps

DAY = "2026-07-14"
TABLES = {
    "steps": "date, source",
    "weight": "date, source",
    "measurements": "date, metric",
    "food_catalog": "slug",
    "food_log": "date, meal, food_slug, ingested_at",
    "exercise": "date, exercise, ingested_at",
}


def _snapshot(conn):
    return {t: [dict(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY {pk}")]
            for t, pk in TABLES.items()}


_FOOD_KEY = ("date", "meal", "food_slug", "ingested_at")


def _write_a_representative_mix(ctx):
    ctx(source="telegram", text="9000 steps")
    log_steps(DAY, 9000)
    superseded = log_steps(DAY, 9500)                      # re-log -> overwrites in projection
    ctx(source="telegram", text="weight 72.6")
    weight.log_weight(DAY, 72.6)
    ctx(source="telegram", text="waist 78.8")
    measurements.log_measurement(DAY, "waist", 78.8)
    measurements.log_measurement(DAY, "waist", 78.1)            # re-log -> overwrites in projection
    ctx(source="telegram", text="bench 3x10 @40, plank 3x60s")
    exercise.log_exercise(DAY, [
        {"exercise": "bench press", "sets": 3, "reps": 10, "weight_kg": 40, "rpe": 7},
        {"exercise": "plank", "sets": 3, "duration_min": 1},     # nullable columns must replay
    ])
    ctx(source="seed", text="save skyr")
    food.save_food("skyr", 100, "g", 51, 9, 3.4, 0.1, 0, notes="typical serving 210g")
    food.save_food("skyr", 100, "g", 52, 9, 3.4, 0.1, 0)        # re-save -> upsert same slug
    food.save_food("banana", 1, "each", 108, 1.44, 24.4, 0.12, 0)
    ctx(source="telegram", text="210g skyr")
    (first,) = food.log_food(DAY, "snack", [{"name": "skyr", "quantity": 210, "unit": "g"}])["logged"]
    (second,) = food.log_food(DAY, "snack", [{"name": "skyr", "quantity": 210, "unit": "g",
                                              "allow_duplicate": True}])["logged"]

    # --- corrections: an edit and a delete that must survive replay ---
    ctx(source="telegram", text="the first skyr was 105g")
    corrections.edit_entry("food_log", {k: first[k] for k in _FOOD_KEY},
                           first["ingested_at"], {"amount": 105})
    ctx(source="telegram", text="scratch the second one")
    corrections.delete_entry("food_log", {k: second[k] for k in _FOOD_KEY},
                             second["ingested_at"])

    # --- the relabel divergence case: label a day's unlabelled rows, THEN log another ---
    # Live, the second banana is still unlabelled at day's end. Replay writes both banana
    # rows before any correction, so a relabel that swept "everything unlabelled on this
    # date" would catch the second one too. Pinning the exact rows is what prevents it.
    ctx(source="telegram", text="a banana")
    food.log_food(DAY, items=[{"name": "banana", "quantity": 1, "unit": "each"}])
    ctx(source="telegram", text="breakfast")
    corrections.set_meal(DAY, "breakfast")
    ctx(source="telegram", text="another banana")
    food.log_food(DAY, items=[{"name": "banana", "quantity": 2, "unit": "each"}])

    # --- the divergence case: correct an overwrite domain, THEN write over it ---
    # Live order is edit-then-re-log, so the re-log wins. Replay applies every correction
    # after every steps row, so without the ingested_at guard this edit would land last
    # and the rebuilt count would be 9800 instead of 9200.
    ctx(source="telegram", text="that was 9800 steps")
    corrections.edit_entry("steps", {"date": DAY, "source": superseded["source"]},
                           superseded["ingested_at"], {"count": 9800})
    ctx(source="telegram", text="no, 9200")
    log_steps(DAY, 9200)


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

    # Sanity: the mix actually exercised overwrite + append + correction, not a trivial DB.
    assert len(live["steps"]) == 1                              # re-log collapsed
    assert len(live["food_catalog"]) == 2                      # skyr re-save collapsed, + banana
    assert len(live["food_log"]) == 3                          # 2 skyr (1 deleted) + 2 bananas
    skyr = [r for r in live["food_log"] if r["food_slug"] == "skyr"]
    assert len(skyr) == 1 and skyr[0]["amount"] == 105         # the edit landed


def test_a_relabel_does_not_sweep_up_later_unlabelled_rows(ctx, tmp_path):
    """set_meal writes a dedupe-key column, so its replay guard gets its own proof.

    Live: log a banana (unlabelled) -> set_meal breakfast -> log another banana, which
    stays unlabelled. Replay writes both bananas before any correction, so an unpinned
    relabel would label both and diverge.
    """
    _write_a_representative_mix(ctx)
    live = {r["amount"]: r["meal"]
            for r in get_conn().execute("SELECT amount, meal FROM food_log WHERE food_slug='banana'")}
    assert live == {1: "breakfast", 2: "unlabelled"}

    rebuild_db.rebuild()

    rebuilt_conn = sqlite3.connect(str(tmp_path / "rebuilt.sqlite"))
    rebuilt_conn.row_factory = sqlite3.Row
    try:
        rebuilt = {r["amount"]: r["meal"] for r in rebuilt_conn.execute(
            "SELECT amount, meal FROM food_log WHERE food_slug='banana'")}
    finally:
        rebuilt_conn.close()
    assert rebuilt == live


def test_a_superseded_correction_replays_as_a_no_op(ctx, tmp_path):
    """The whole reason every event pins the target's `ingested_at`.

    Live: log 9500 -> correct to 9800 -> re-log 9200, ending at 9200. Replay applies
    corrections after every steps row, so an unguarded UPDATE would end at 9800. The
    guard makes the correction match nothing, because the row it named is gone.
    """
    _write_a_representative_mix(ctx)
    assert get_conn().execute("SELECT count FROM steps").fetchone()["count"] == 9200

    rebuild_db.rebuild()

    rebuilt_conn = sqlite3.connect(str(tmp_path / "rebuilt.sqlite"))
    rebuilt_conn.row_factory = sqlite3.Row
    try:
        assert rebuilt_conn.execute("SELECT count FROM steps").fetchone()["count"] == 9200
    finally:
        rebuilt_conn.close()


def test_corrections_owns_no_table(ctx, tmp_path):
    """It projects into other features' tables; `data/corrections.jsonl` is the record."""
    _write_a_representative_mix(ctx)
    rebuild_db.rebuild()
    assert (tmp_path / "corrections.jsonl").exists()

    rebuilt_conn = sqlite3.connect(str(tmp_path / "rebuilt.sqlite"))
    try:
        names = {r[0] for r in rebuilt_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        rebuilt_conn.close()
    assert "corrections" not in names


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
    log_steps(DAY, 9000)
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
