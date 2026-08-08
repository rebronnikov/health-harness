"""get_summary — the multi-day arithmetic Claude is forbidden from doing itself.

Data is seeded through the real executors rather than raw SQL, so what's asserted is
what live ingest actually produces (including corrections, which the summary reads
through the projection).
"""
import pytest

from health.core import harness
from health.core.contracts import NormalizedMessage
from health.features import corrections, food, measurements, review, steps, targets, weight

from conftest import DEFAULT_TS, log_steps, text_block, tool_block

MON, TUE, WED = "2026-07-13", "2026-07-14", "2026-07-15"
WEEK = ("2026-07-13", "2026-07-19")


def _seed_three_days(ctx, catalog):
    ctx(text="seed")
    food.log_food(MON, "breakfast", [{"name": "skyr", "quantity": 200, "unit": "g"}])
    food.log_food(TUE, "breakfast", [{"name": "skyr", "quantity": 100, "unit": "g"}])
    food.log_food(TUE, "lunch", [{"name": "banana", "quantity": 2, "unit": "each"}])
    log_steps(MON, 9000)
    log_steps(TUE, 12000)
    weight.log_weight(MON, 72.8)
    weight.log_weight(WED, 71.8)
    measurements.log_measurement(MON, "waist", 79.4)
    measurements.log_measurement(WED, "waist", 78.1)
    measurements.log_measurement(MON, "chest", 93.7)


def test_food_per_day_sums_and_period_average(ctx, catalog):
    _seed_three_days(ctx, catalog)
    out = review.get_summary(*WEEK)["food"]

    assert [d["date"] for d in out["per_day"]] == [MON, TUE]     # only days with rows
    assert out["per_day"][0]["kcal"] == pytest.approx(102.0)     # skyr 51/100g x2
    assert out["per_day"][1]["kcal"] == pytest.approx(267.0)     # 51 + 2 bananas at 108
    assert out["days_logged"] == 2
    assert out["average"]["kcal"] == pytest.approx(184.5)
    assert out["targets"] == targets.TARGETS


def test_days_in_range_is_separate_from_days_logged(ctx, catalog):
    """The gap signal: an average over 2 of 7 days is not a weekly average."""
    _seed_three_days(ctx, catalog)
    out = review.get_summary(*WEEK)
    assert out["days_in_range"] == 7
    assert out["food"]["days_logged"] == 2


def test_ceiling_and_floor_counters(ctx, catalog):
    ctx(text="seed")
    # One day well over the kcal ceiling and over both floors, one day under everything.
    food.log_food(MON, "dinner", [{
        "name": "feast", "quantity": 1, "unit": "each",
        "kcal": targets.TARGETS["kcal"] + 500, "protein_g": targets.TARGETS["protein_g"] + 10,
        "carb_g": 300, "fat_g": 90, "fibre_g": targets.TARGETS["fibre_g"] + 5,
    }])
    food.log_food(TUE, "snack", [{"name": "skyr", "quantity": 100, "unit": "g"}])

    out = review.get_summary(*WEEK)["food"]
    assert out["days_over_kcal_ceiling"] == 1
    assert out["days_over_fat_ceiling"] == 1          # 90g on the feast day, ceiling is 75
    assert out["days_protein_floor_met"] == 1
    assert out["days_fibre_floor_met"] == 1


def test_floors_count_as_met_within_the_suggestion_tolerance(ctx, catalog):
    """Consistency with the evening suggestions: a day the bot called done must not come
    back as missed in the weekly summary."""
    ctx(text="seed")
    food.log_food(MON, "dinner", [{
        "name": "close enough", "quantity": 1, "unit": "each", "kcal": 500,
        "protein_g": targets.TARGETS["protein_g"] - targets.PROTEIN_MET_TOL_G,
        "carb_g": 0, "fat_g": 0,
        "fibre_g": targets.TARGETS["fibre_g"] - targets.FIBRE_MET_TOL_G,
    }])
    out = review.get_summary(*WEEK)["food"]
    assert out["days_protein_floor_met"] == 1
    assert out["days_fibre_floor_met"] == 1


def test_steps_totals_and_best_day(ctx, catalog):
    _seed_three_days(ctx, catalog)
    out = review.get_summary(*WEEK)["steps"]
    assert out == {"days_logged": 2, "total": 21000, "average": 10500,
                   "best": {"date": TUE, "count": 12000}}


def test_weight_delta_is_signed_from_first_to_last(ctx, catalog):
    _seed_three_days(ctx, catalog)
    out = review.get_summary(*WEEK)["weight"]
    assert out["first"] == {"date": MON, "weight_kg": 72.8}
    assert out["last"] == {"date": WED, "weight_kg": 71.8}
    assert out["delta_kg"] == -1.0                              # negative = lost
    assert (out["min_kg"], out["max_kg"]) == (71.8, 72.8)


def test_measurements_are_the_latest_per_metric(ctx, catalog):
    _seed_three_days(ctx, catalog)
    out = review.get_summary(*WEEK)["measurements"]
    assert out["waist"] == {"date": WED, "value_cm": 78.1}      # not the MON reading
    assert out["chest"] == {"date": MON, "value_cm": 93.7}


def test_empty_range_returns_nulls_not_zeros(ctx, catalog):
    _seed_three_days(ctx, catalog)
    out = review.get_summary("2026-06-01", "2026-06-07")
    assert out["days_in_range"] == 7
    assert out["food"]["per_day"] == [] and out["food"]["average"] is None
    assert out["steps"]["average"] is None
    assert out["weight"]["delta_kg"] is None
    assert out["measurements"] == {}


def test_summary_reflects_a_correction(ctx, catalog):
    """It reads the projection, which corrections have already been applied to — so a
    deleted row is genuinely gone from the week."""
    ctx(text="seed")
    (row,) = food.log_food(MON, "breakfast",
                           [{"name": "skyr", "quantity": 200, "unit": "g"}])["logged"]
    assert review.get_summary(*WEEK)["food"]["per_day"][0]["kcal"] == pytest.approx(102.0)

    ctx(text="delete that")
    corrections.delete_entry(
        "food_log", {k: row[k] for k in ("date", "meal", "food_slug", "ingested_at")},
        row["ingested_at"])
    assert review.get_summary(*WEEK)["food"]["per_day"] == []


def test_review_declares_no_system_of_record():
    """Read-only: no schema, no domain, no projector — rebuild never sees it."""
    from health.core import registry

    assert review.FEATURE.domain is None and review.FEATURE.schema is None
    assert review.FEATURE not in registry.ingesting_features()
    assert "get_summary" in registry.all_executors()


def test_harness_routes_a_week_question_to_get_summary(ctx, catalog, fake_client):
    _seed_three_days(ctx, catalog)
    fake_client.script(
        [tool_block("get_summary", {"start_date": WEEK[0], "end_date": WEEK[1]}, id="tu-1")],
        [text_block("Averaging 185 kcal over the 2 days you logged.")],
    )
    reply = harness.handle(NormalizedMessage(source="telegram", reply_handle="chat-1",
                                             timestamp=DEFAULT_TS, text="how was my week?"))
    assert reply.startswith("Averaging")
    assert fake_client.calls[0]["messages"][0]["content"] == "how was my week?"

