"""Exercise — the three shapes of training one tool has to cover (weights, bodyweight,
timed), the append-per-entry key, and the RPE bound.
"""
import pytest

from health.features import corrections, exercise, review
from health.store.connection import get_conn

from conftest import rows

DAY = "2026-07-14"


def _log(entries, date=DAY):
    return exercise.log_exercise(date, entries)


def test_weights_entry_reads_the_numbers_off_the_message(ctx):
    ctx(text="bench 3x10 at 40kg, felt 7")
    (logged,) = _log([{"exercise": "bench press", "sets": 3, "reps": 10,
                       "weight_kg": 40, "rpe": 7}])["logged"]
    assert logged["exercise"] == "bench press"
    assert (logged["sets"], logged["reps"], logged["weight_kg"], logged["rpe"]) == (3, 10, 40.0, 7)
    assert logged["duration_min"] is None                      # not a timed movement


def test_bodyweight_entry_has_no_weight(ctx):
    ctx(text="30 press-ups")
    (logged,) = _log([{"exercise": "press-ups", "sets": 3, "reps": 10}])["logged"]
    assert logged["weight_kg"] is None and logged["rpe"] is None


def test_timed_entry_has_no_reps(ctx):
    ctx(text="3 x 60s plank")
    (logged,) = _log([{"exercise": "plank", "sets": 3, "duration_min": 1}])["logged"]
    assert logged["reps"] is None and logged["duration_min"] == 1.0


def test_several_exercises_in_one_message(ctx):
    ctx(text="bench 3x10 @40, squats 3x8 @60")
    out = _log([
        {"exercise": "bench press", "sets": 3, "reps": 10, "weight_kg": 40},
        {"exercise": "squat", "sets": 3, "reps": 8, "weight_kg": 60},
    ])
    assert out["count"] == 2
    assert len(rows(get_conn(), "SELECT * FROM exercise")) == 2


def test_the_same_movement_twice_in_a_day_adds_a_row(ctx):
    """Two working sets hours apart, not a correction of the first — overwriting would
    lose training history that can't be reconstructed."""
    ctx(text="bench 3x10 @40")
    _log([{"exercise": "bench press", "sets": 3, "reps": 10, "weight_kg": 40}])
    ctx(text="more bench, 2x5 @50")
    _log([{"exercise": "bench press", "sets": 2, "reps": 5, "weight_kg": 50}])
    stored = rows(get_conn(), "SELECT * FROM exercise ORDER BY ingested_at")
    assert [r["weight_kg"] for r in stored] == [40.0, 50.0]


def test_notes_are_kept(ctx):
    ctx(text="squats, felt strong today")
    (logged,) = _log([{"exercise": "squat", "notes": "felt strong today"}])["logged"]
    assert logged["notes"] == "felt strong today"


def test_name_is_normalised(ctx):
    ctx()
    (logged,) = _log([{"exercise": "  Bench Press  "}])["logged"]
    assert logged["exercise"] == "bench press"


@pytest.mark.parametrize("bad", [0, 11, 40])
def test_rpe_outside_the_scale_raises_and_writes_nothing(ctx, bad):
    ctx()
    with pytest.raises(ValueError, match="rpe must be 1-10"):
        _log([{"exercise": "bench press", "rpe": bad}])
    assert rows(get_conn(), "SELECT * FROM exercise") == []


def test_an_entry_with_no_name_raises(ctx):
    ctx()
    with pytest.raises(ValueError, match="needs an `exercise` name"):
        _log([{"sets": 3}])


# --- reachable by the rest of the system ------------------------------------

def test_exercise_is_correctable(ctx):
    """`date` in the dedupe key is what admits a domain to query_log/edit/delete."""
    ctx(text="bench 3x10 @40")
    (logged,) = _log([{"exercise": "bench press", "sets": 3, "reps": 10, "weight_kg": 40}])["logged"]

    found = corrections.query_log("exercise", DAY, DAY)
    (row,) = found["rows"]
    assert set(row["key"]) == {"date", "exercise", "ingested_at"}
    assert "weight_kg" in found["editable_fields"] and "rpe" in found["editable_fields"]
    assert "exercise" not in found["editable_fields"]          # identity: delete + re-log

    ctx(text="that was 45kg")
    corrections.edit_entry("exercise", row["key"], logged["ingested_at"], {"weight_kg": 45})
    (stored,) = rows(get_conn(), "SELECT * FROM exercise")
    assert stored["weight_kg"] == 45.0


def test_exercise_appears_in_the_weekly_summary(ctx):
    ctx(text="training")
    _log([{"exercise": "bench press", "sets": 3, "reps": 10, "weight_kg": 40, "rpe": 7}])
    _log([{"exercise": "bench press", "sets": 2, "reps": 5, "weight_kg": 50, "rpe": 9}],
         date="2026-07-15")
    _log([{"exercise": "plank", "sets": 3, "duration_min": 1}])

    out = review.get_summary("2026-07-13", "2026-07-19")["exercise"]
    assert out["days_logged"] == 2 and out["entries"] == 3
    assert out["by_exercise"]["bench press"]["total_sets"] == 5
    assert out["by_exercise"]["bench press"]["heaviest_kg"] == 50.0
    assert out["by_exercise"]["plank"]["total_minutes"] == 3.0
    assert out["average_rpe"] == 8.0                           # (7 + 9) / 2, planks had none


def test_empty_range_reports_nothing_rather_than_zeroes_everywhere(ctx):
    ctx()
    out = review.get_summary("2026-06-01", "2026-06-07")["exercise"]
    assert out == {"days_logged": 0, "entries": 0, "by_exercise": {}, "average_rpe": None}
