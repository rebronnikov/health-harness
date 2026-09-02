"""The demo seeder writes through the executors, so it is also a rebuild round-trip on
demo data — the README's "try it" claim, checked.
"""
from datetime import date

import pytest

import json

from health import rebuild_db, seed_demo
from health.features import food
from health.store import writer
from health.store.connection import get_conn, open_conn

from conftest import rows

TODAY = date(2026, 7, 14)
TABLES = {
    "steps": "date, source",
    "weight": "date, source",
    "measurements": "date, metric",
    "food_catalog": "slug",
    "food_log": "date, meal, food_slug, ingested_at",
    "exercise": "date, exercise, ingested_at",
}


def _snapshot(conn):
    return {t: rows(conn, f"SELECT * FROM {t} ORDER BY {pk}") for t, pk in TABLES.items()}


def test_seeds_the_window_through_the_executors():
    out = seed_demo.seed(days=3, today=TODAY)
    logged = rows(get_conn(), "SELECT date, raw FROM food_log")
    assert {r["date"] for r in logged} == {"2026-07-12", "2026-07-13", "2026-07-14"}
    assert all(r["raw"].startswith(("breakfast:", "lunch:", "dinner:", "snack:")) for r in logged)
    # Provenance is stamped by writer.record from the bound context, never by the seeder.
    record = [json.loads(line) for line in (writer.DATA_DIR / "food_log.jsonl").read_text().splitlines()]
    assert len(record) == len(logged)
    assert {r["source"] for r in record} == {seed_demo.SOURCE}
    assert out["food_rows"] == len(logged)
    assert rows(get_conn(), "SELECT COUNT(*) AS n FROM steps")[0]["n"] == 3
    assert rows(get_conn(), "SELECT COUNT(*) AS n FROM weight")[0]["n"] == 3
    assert rows(get_conn(), "SELECT COUNT(*) AS n FROM exercise")[0]["n"] > 0
    assert rows(get_conn(), "SELECT COUNT(*) AS n FROM measurements")[0]["n"] == 1
    assert "Today so far" in out["progress_block"]


def test_the_breakfast_message_writes_two_meals():
    seed_demo.seed(days=1, today=TODAY)
    meals = {r["meal"] for r in rows(get_conn(), "SELECT meal FROM food_log")}
    assert {"breakfast", "supplements"} <= meals


def test_today_is_left_with_room_to_suggest_into():
    out = seed_demo.seed(days=2, today=TODAY)
    rem = food.targets.remaining(
        food._day_totals(get_conn(), TODAY.isoformat()))
    assert rem["kcal"]["remaining"] > 300
    assert rem["protein_g"]["still_needed"] > 0 and rem["fibre_g"]["still_needed"] > 0
    assert out["progress_block"].startswith("Today so far\n🔥 Calories: ")


def test_rebuild_reproduces_the_demo():
    seed_demo.seed(days=4, today=TODAY)
    live = _snapshot(get_conn())
    assert live["food_log"] and live["exercise"]
    rebuilt = open_conn(rebuild_db.rebuild())
    try:
        assert _snapshot(rebuilt) == live
    finally:
        rebuilt.close()


def test_refuses_to_double_seed_without_reset():
    first = seed_demo.seed(days=2, today=TODAY)
    with pytest.raises(SystemExit, match="--reset"):
        seed_demo.seed(days=2, today=TODAY)
    again = seed_demo.seed(days=2, today=TODAY, reset_first=True)
    assert again["food_rows"] == first["food_rows"]
    assert rows(get_conn(), "SELECT COUNT(*) AS n FROM food_log")[0]["n"] == first["food_rows"]


def test_main_prints_the_block_and_honours_the_pinned_date(capsys, monkeypatch):
    monkeypatch.setenv("HEALTH_TODAY", "2026-07-20")
    seed_demo.main(["--days", "2"])
    out = capsys.readouterr().out
    assert "Seeded 2 day(s), 2026-07-19 to 2026-07-20" in out
    assert "\nToday so far\n🔥 Calories: " in out
