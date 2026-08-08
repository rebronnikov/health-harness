"""Seeing what was logged, then pointing at it: recent_entries across every domain,
query_log's limit/date defaults, and the numbered listing that makes "delete 2" resolve.
"""
from datetime import timedelta

import pytest

from health.adapters import telegram_adapter as tg
from health.core.contracts import NormalizedMessage
from health.features import corrections, exercise, food, steps, weight
from health.store.connection import get_conn

from conftest import DEFAULT_TS, log_steps, rows

DAY = "2026-07-14"
YESTERDAY = "2026-07-13"


def _seed_a_mixed_day(ctx):
    ctx(text="9000 steps")
    log_steps(DAY, 9000)
    ctx(text="weight 72.6")
    weight.log_weight(DAY, 72.6)
    ctx(text="40g skyr")
    food.log_food(DAY, "breakfast", [{"name": "skyr", "quantity": 40, "unit": "g"}])
    ctx(text="bench 3x10 @40")
    exercise.log_exercise(DAY, [{"exercise": "bench press", "sets": 3, "reps": 10,
                                 "weight_kg": 40}])


def _msg(text="delete 2", handle="chat-1", timestamp=DEFAULT_TS):
    return NormalizedMessage(source="test", reply_handle=handle,
                             timestamp=timestamp, text=text)


# --- recent_entries ---------------------------------------------------------

def test_recent_entries_spans_every_domain_newest_first(ctx, catalog):
    _seed_a_mixed_day(ctx)
    ctx(text="what have I logged?")
    out = corrections.recent_entries()

    assert [e["domain"] for e in out["entries"]] == [
        "exercise", "food_log", "weight", "steps"]        # newest write first
    assert [e["n"] for e in out["entries"]] == [1, 2, 3, 4]
    assert out["start_date"] == out["end_date"] == DAY


def test_recent_entries_carries_the_key_needed_to_delete(ctx, catalog):
    _seed_a_mixed_day(ctx)
    ctx(text="what have I logged?")
    entry = next(e for e in corrections.recent_entries()["entries"]
                 if e["domain"] == "food_log")

    ctx(text="drop it")
    corrections.delete_entry("food_log", entry["key"], entry["ingested_at"])
    assert rows(get_conn(), "SELECT * FROM food_log") == []


def test_recent_entries_limit_and_truncation(ctx, catalog):
    _seed_a_mixed_day(ctx)
    ctx(text="last 2 things")
    out = corrections.recent_entries(limit=2)
    assert len(out["entries"]) == 2
    assert out["total_in_range"] == 4 and out["truncated"] is True


def test_recent_entries_window_counts_the_end_date_as_day_one(ctx, catalog):
    ctx(text="yesterday's steps")
    log_steps(YESTERDAY, 8000)
    _seed_a_mixed_day(ctx)

    ctx(text="today")
    assert corrections.recent_entries(days=1)["total_in_range"] == 4
    ctx(text="last two days")
    assert corrections.recent_entries(days=2)["total_in_range"] == 5


def test_recent_entries_can_end_on_an_earlier_day(ctx, catalog):
    ctx(text="yesterday's steps")
    log_steps(YESTERDAY, 8000)
    _seed_a_mixed_day(ctx)

    ctx(text="what about yesterday")
    out = corrections.recent_entries(days=1, end_date=YESTERDAY)
    assert out["total_in_range"] == 1
    assert out["entries"][0]["summary"] == "steps: 8000"


def test_summaries_are_readable_per_domain(ctx, catalog):
    _seed_a_mixed_day(ctx)
    ctx(text="show me")
    got = {e["domain"]: e["summary"] for e in corrections.recent_entries()["entries"]}
    assert got["steps"] == "steps: 9000"
    assert got["weight"] == "weight: 72.6"
    assert got["food_log"] == "food_log: breakfast skyr 40 g 20.4"
    assert got["exercise"] == "exercise: bench press 3 10 40"


# --- the numbered listing ---------------------------------------------------

def test_the_listing_is_remembered_for_the_next_turn(ctx, catalog):
    _seed_a_mixed_day(ctx)
    ctx(text="what have I logged?", reply_handle="chat-1")
    corrections.recent_entries()

    frag = corrections.listing_system_context(_msg())
    assert "LAST LISTING" in frag
    assert "1. exercise:" in frag and "ingested_at=" in frag
    assert '"food_slug": "skyr"' in frag                  # the key, ready to pass through


def test_the_listing_is_per_chat(ctx, catalog):
    _seed_a_mixed_day(ctx)
    ctx(text="what have I logged?", reply_handle="chat-1")
    corrections.recent_entries()
    assert corrections.listing_system_context(_msg(handle="chat-2")) is None


def test_a_stale_listing_is_dropped_rather_than_misread(ctx, catalog):
    """An hour later "3" means something else, and acting on it would delete the wrong
    row — the one mistake corrections can't make cheap, because it goes unnoticed."""
    _seed_a_mixed_day(ctx)
    ctx(text="what have I logged?", reply_handle="chat-1")
    corrections.recent_entries()

    later = DEFAULT_TS + corrections.LISTING_EXPIRY + timedelta(minutes=1)
    assert corrections.listing_system_context(_msg(timestamp=later)) is None


def test_an_empty_listing_clears_the_reference(ctx, catalog):
    _seed_a_mixed_day(ctx)
    ctx(text="what have I logged?", reply_handle="chat-1")
    corrections.recent_entries()
    ctx(text="and last month?", reply_handle="chat-1")
    corrections.recent_entries(end_date="2026-06-01")
    assert corrections.listing_system_context(_msg()) is None


# --- query_log defaults and limit -------------------------------------------

def test_query_log_defaults_to_today(ctx, catalog):
    ctx(text="yesterday")
    log_steps(YESTERDAY, 8000)
    _seed_a_mixed_day(ctx)

    ctx(text="did I log steps?")
    out = corrections.query_log("steps")
    assert out["start_date"] == out["end_date"] == DAY
    assert [r["count"] for r in out["rows"]] == [9000]


def test_query_log_limit_is_honoured_and_capped(ctx, catalog):
    ctx(text="lots of skyr")
    for _ in range(4):
        food.log_food(DAY, "snack", [{"name": "skyr", "quantity": 40, "unit": "g"}])

    assert len(corrections.query_log("food_log", limit=2)["rows"]) == 2
    assert corrections.query_log("food_log", limit=2)["truncated"] is True
    over_cap = corrections.query_log("food_log", limit=10_000)
    assert len(over_cap["rows"]) == 4 and over_cap["truncated"] is False


# --- the deterministic slash commands ---------------------------------------

@pytest.mark.parametrize("command,args,expected_days,expected_end,expected_kind", [
    ("today", [], 1, None, "day"),
    ("yesterday", [], 1, YESTERDAY, "day"),
    # A week spans days a single day's totals can't describe, so no block.
    ("week", [], 7, None, None),
    # N arbitrary entries is not a date window, so no totals block belongs under it.
    ("last", ["3"], 30, None, None),
])
def test_slash_command_windows(command, args, expected_days, expected_end, expected_kind):
    kwargs, heading, kind = tg._entries_window(command, args, DEFAULT_TS)
    assert kwargs["days"] == expected_days
    assert kwargs.get("end_date") == expected_end
    assert kind == expected_kind
    assert heading


def test_last_command_limit_comes_from_the_argument():
    kwargs, heading, _ = tg._entries_window("last", ["3"], DEFAULT_TS)
    assert kwargs["limit"] == 3 and heading == "Last 3 entries"
    assert tg._entries_window("last", [], DEFAULT_TS)[0]["limit"] == 5      # default
    assert tg._entries_window("last", ["999"], DEFAULT_TS)[0]["limit"] == 50  # capped
    assert tg._entries_window("last", ["banana"], DEFAULT_TS)[0]["limit"] == 5


def test_render_entries_numbers_the_lines(ctx, catalog):
    _seed_a_mixed_day(ctx)
    ctx(text="/today")
    text = tg.render_entries("Today", corrections.recent_entries())
    assert "1. exercise: bench press 3 10 40" in text
    assert "4. steps: 9000" in text
    assert "delete 2" in text                            # tells the user the affordance


def test_render_entries_when_nothing_is_logged(ctx, catalog):
    ctx(text="/today")
    assert "Nothing logged." in tg.render_entries("Today", corrections.recent_entries())


def test_the_listing_carries_the_standard_progress_block(ctx, catalog):
    """/today used to print rows and no totals at all. The block goes below the numbered
    list and above the "delete 2" affordance, so the numbering stays next to the rows."""
    _seed_a_mixed_day(ctx)
    ctx(text="/today")
    listing = corrections.recent_entries()
    block = tg._progress_block("day", listing)
    text = tg.render_entries("Today", listing, block)

    assert block.startswith("Today so far\n")
    assert text.index("4. steps: 9000") < text.index("Today so far") < text.index("delete 2")


def test_the_block_describes_the_day_the_listing_covered(ctx, catalog):
    """The date comes off the listing, never recomputed — a block that described a
    different day from the rows above it would be worse than no block."""
    ctx(text="yesterday's food")
    food.log_food(YESTERDAY, "lunch", [{"name": "banana", "quantity": 1, "unit": "each"}])
    _seed_a_mixed_day(ctx)

    ctx(text="/yesterday")
    yday = corrections.recent_entries(days=1, end_date=YESTERDAY)
    assert "🔥 Calories: 108 / 2200" in tg._progress_block("day", yday)   # not today's 20.4


def test_no_block_for_an_arbitrary_number_of_entries(ctx, catalog):
    _seed_a_mixed_day(ctx)
    ctx(text="/last 3")
    listing = corrections.recent_entries(limit=3)
    assert tg._progress_block(None, listing) is None
    assert "Today so far" not in tg.render_entries("Last 3 entries", listing, None)
