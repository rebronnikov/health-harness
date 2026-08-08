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


def test_the_rendered_block_travels_with_the_suggestions(ctx, catalog):
    """The list is rendered here for the same reason the progress block is: the reply
    only echoes it. Structured `suggestions` stays alongside for `off_catalog_ok`, which
    the model does still have to word itself."""
    ctx(timestamp=EVENING)
    out = food.get_day_totals(TODAY)
    assert out["suggestions_block"].startswith("To close the gaps\n")
    ctx()                                                       # back to noon: neither key
    assert "suggestions_block" not in food.get_day_totals(TODAY)


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
    assert out["suggestions_block"].startswith("To close the gaps\n")


def test_suggest_food_omits_the_block_when_there_is_nothing_to_say(ctx, catalog):
    """`suggest` can legitimately come back with nothing — here the day is past the calorie
    ceiling and the fixture catalog holds no fibre-bearing food to close the one open floor
    with. The block key must then be absent rather than an empty string."""
    ctx()
    food.log_food(TODAY, "dinner", [{"name": "salmon", "quantity": 1200, "unit": "g"}])
    out = food.suggest_food(TODAY)                              # 2340 kcal (over), 277g protein
    assert out["suggestions"] is None
    assert "suggestions_block" not in out


def test_suggest_food_defaults_to_today(ctx, catalog):
    ctx()
    assert food.suggest_food()["date"] == TODAY                 # from the message clock, not the model


def test_suggest_food_reports_the_room_it_sized_against(ctx, catalog):
    ctx()
    food.log_food(TODAY, "lunch", [{"name": "salmon", "quantity": 200, "unit": "g"}])
    room = food.suggest_food(TODAY)["suggestions"]["room"]
    assert room["fat_g"] == 70 - 22.2                            # 2x the 11.1g per 100g
    assert set(room) == {"kcal", "fat_g", "carb_g"}
