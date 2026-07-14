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
