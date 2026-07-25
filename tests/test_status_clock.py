"""The evening suggestion gate in food._status reads the in-flight message clock
(so Claude never reasons about time). Suggestions appear only for TODAY and only
at/after SUGGEST_AFTER_HOUR (16:00).
"""
from datetime import timezone
from datetime import datetime

from health.features import food

TODAY = "2026-07-14"
YESTERDAY = "2026-07-13"
EVENING = datetime(2026, 7, 14, 20, 0, tzinfo=timezone.utc)


def test_no_suggestions_before_evening(ctx, catalog):
    ctx()                                                       # DEFAULT_TS is noon
    assert "suggestions" not in food.get_day_totals(TODAY)


def test_suggestions_appear_in_the_evening_for_today(ctx, catalog):
    ctx(timestamp=EVENING)                                      # nothing logged -> floors wide open
    assert "suggestions" in food.get_day_totals(TODAY)


def test_no_suggestions_for_a_past_date(ctx, catalog):
    ctx(timestamp=EVENING)
    assert "suggestions" not in food.get_day_totals(YESTERDAY)  # gate requires date == today


# --- suggest_food: the same suggestions, asked for directly ----------------------

def test_suggest_food_bypasses_the_evening_gate(ctx, catalog):
    """Unprompted advice at 9am is noise; ASKING at 9am is not. suggest_food answers at
    any hour, while get_day_totals stays quiet until the evening."""
    ctx()                                                       # noon — the gate is shut
    assert "suggestions" not in food.get_day_totals(TODAY)
    out = food.suggest_food(TODAY)
    assert out["suggestions"] is not None
    assert out["suggestions"]["fits"]


def test_suggest_food_defaults_to_today(ctx, catalog):
    ctx()
    assert food.suggest_food()["date"] == TODAY                 # from the message clock, not the model


def test_suggest_food_reports_the_room_it_sized_against(ctx, catalog):
    ctx()
    food.log_food(TODAY, "lunch", [{"name": "salmon", "quantity": 200, "unit": "g"}])
    room = food.suggest_food(TODAY)["suggestions"]["room"]
    assert room["fat_g"] == 70 - 22.2                            # 2x the 11.1g per 100g
    assert set(room) == {"kcal", "fat_g", "carb_g"}
