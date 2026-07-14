"""The deferred-commit estimate flow: propose_food_estimate stashes but writes
nothing; confirm_pending commits with estimated=1 and the ORIGINAL food message
as provenance (not the later 'yes'); the pending slot is per-chat and cleared.
"""
from health.features import food
from health.core.contracts import NormalizedMessage
from health.store.connection import get_conn

from conftest import DEFAULT_TS, rows

DAY = "2026-07-14"
PIZZA = [{"name": "pizza slice", "quantity": 2, "unit": "each",
          "kcal": 285, "protein_g": 12, "carb_g": 36, "fat_g": 10, "fibre_g": 2}]


def test_propose_writes_nothing_but_stashes(ctx):
    ctx(text="two slices of pizza", reply_handle="c1")
    out = food.propose_food_estimate(DAY, "dinner", PIZZA)
    assert out["needs_confirmation"] is True
    assert rows(get_conn(), "SELECT * FROM food_log") == []       # NOTHING committed on propose
    assert food._pending["c1"]["original_raw"] == "two slices of pizza"


def test_propose_projects_current_plus_estimate(ctx, catalog):
    ctx(text="pizza", reply_handle="c1")
    food.log_food(DAY, "lunch", [{"name": "banana", "quantity": 1, "unit": "each"}])  # 108 kcal on the day
    out = food.propose_food_estimate(DAY, "dinner", PIZZA)                            # + 285 est
    assert out["projected_day_totals"]["kcal"] == 393.0                               # 108 + 285


def test_confirm_writes_estimated_rows_with_original_provenance(ctx):
    ctx(text="two slices of pizza", reply_handle="c1")
    food.propose_food_estimate(DAY, "dinner", PIZZA)
    ctx(text="yes", reply_handle="c1")                            # a later, context-free turn
    out = food.confirm_pending()
    (logged,) = out["logged"]
    assert logged["estimated"] == 1
    assert logged["kcal"] == 285
    assert logged["raw"] == "two slices of pizza"                 # NOT "yes" — provenance is the food message
    assert len(rows(get_conn(), "SELECT * FROM food_log")) == 1


def test_confirm_clears_pending(ctx):
    ctx(text="pizza", reply_handle="c1")
    food.propose_food_estimate(DAY, "dinner", PIZZA)
    ctx(text="yes", reply_handle="c1")
    food.confirm_pending()
    assert "c1" not in food._pending                              # slot consumed
    assert food.confirm_pending()["error"]                        # second confirm finds nothing


def test_confirm_with_nothing_pending_errors(ctx):
    ctx(text="yes", reply_handle="empty")
    out = food.confirm_pending()
    assert "error" in out
    assert rows(get_conn(), "SELECT * FROM food_log") == []


def test_pending_state_is_per_chat(ctx):
    ctx(text="pizza", reply_handle="c1")
    food.propose_food_estimate(DAY, "dinner", PIZZA)
    other = NormalizedMessage(source="test", reply_handle="c2", timestamp=DEFAULT_TS, text="yes")
    assert food.pending_system_context(other) is None            # a different chat sees no pending
    mine = NormalizedMessage(source="test", reply_handle="c1", timestamp=DEFAULT_TS, text="yes")
    frag = food.pending_system_context(mine)
    assert frag is not None and frag.startswith("PENDING:")
