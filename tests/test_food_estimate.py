"""The deferred-commit estimate flow: propose_food_estimate stashes but writes
nothing; confirm_pending commits with estimated=1 and the ORIGINAL food message
as provenance (not the later 'yes'); the pending slot is per-chat and cleared.
"""
import pytest

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


def test_the_projected_block_says_it_is_a_projection(ctx, catalog):
    """Nothing is written yet, so the block under an estimate must not read like the day's
    log — same shape, different heading."""
    ctx(text="pizza", reply_handle="c1")
    food.log_food(DAY, "lunch", [{"name": "banana", "quantity": 1, "unit": "each"}])
    block = food.propose_food_estimate(DAY, "dinner", PIZZA)["projected_day_totals"]["progress_block"]
    assert block.startswith("Would put today at\n")
    assert "🔥 Calories: 393 / 2200 (1807 left)" in block        # the projection, not the 108 logged


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
    with pytest.raises(ValueError):                               # second confirm finds nothing
        food.confirm_pending()


def test_confirm_with_nothing_pending_raises(ctx):
    ctx(text="yes", reply_handle="empty")
    with pytest.raises(ValueError, match="Nothing is pending"):
        food.confirm_pending()
    assert rows(get_conn(), "SELECT * FROM food_log") == []


def test_pending_state_is_per_chat(ctx):
    ctx(text="pizza", reply_handle="c1")
    food.propose_food_estimate(DAY, "dinner", PIZZA)
    other = NormalizedMessage(source="test", reply_handle="c2", timestamp=DEFAULT_TS, text="yes")
    assert food.log_system_context(other) is None                # a different chat sees no pending
    mine = NormalizedMessage(source="test", reply_handle="c1", timestamp=DEFAULT_TS, text="yes")
    frag = food.log_system_context(mine)
    assert frag is not None and frag.startswith("PENDING:")


# --- the offer to save a confirmed estimate ---------------------------------

def _msg(handle="c1", text="yes"):
    return NormalizedMessage(source="test", reply_handle=handle,
                             timestamp=DEFAULT_TS, text=text)


def _confirm_pizza(ctx):
    ctx(text="two slices of pizza", reply_handle="c1")
    food.propose_food_estimate(DAY, "dinner", PIZZA)
    ctx(text="yes", reply_handle="c1")
    return food.confirm_pending()


def test_confirming_an_estimate_offers_to_save_it(ctx):
    """The food that cost a round-trip to guess is exactly the one worth remembering."""
    out = _confirm_pizza(ctx)
    (offer,) = out["catalog_offer"]
    assert offer["name"] == "pizza slice"
    # Two slices were eaten, but a catalog entry is stored per one — a serving is not a
    # reference portion, and "per 2 each" reads as a mistake in the listing.
    assert offer["ref_qty"] == 1 and offer["ref_unit"] == "each"
    assert offer["kcal"] == 142.5 and offer["fibre_g"] == 1

    frag = food.log_system_context(_msg())
    assert "SAVE OFFER" in frag and "pizza slice" in frag


def test_a_weighed_food_is_offered_per_100g(ctx):
    """Mass foods save per 100 g so they scale the way the catalog documents. Logged
    150g of mince would otherwise have been offered — and saved — as 'per 150 g'."""
    ctx(text="150g beef mince, 5% fat", reply_handle="c1")
    food.propose_food_estimate(DAY, "lunch", [{
        "name": "beef mince 5% fat", "quantity": 150, "unit": "g",
        "kcal": 188, "protein_g": 30.5, "carb_g": 0, "fat_g": 7.5, "fibre_g": 0}])
    ctx(text="looks right", reply_handle="c1")

    (offer,) = food.confirm_pending()["catalog_offer"]
    assert offer["ref_qty"] == 100 and offer["ref_unit"] == "g"
    assert offer["kcal"] == 125.33 and offer["protein_g"] == 20.33
    assert "per 100g" in food.log_system_context(_msg())


def test_no_offer_for_a_food_already_in_the_catalog(ctx, catalog):
    ctx(text="some skyr", reply_handle="c1")
    food.propose_food_estimate(DAY, "snack", [{
        "name": "skyr", "quantity": 100, "unit": "g",
        "kcal": 51, "protein_g": 9, "carb_g": 3.4, "fat_g": 0.1, "fibre_g": 0}])
    ctx(text="yes", reply_handle="c1")
    assert food.confirm_pending()["catalog_offer"] is None
    assert "SAVE OFFER" not in (food.log_system_context(_msg()) or "")


def test_accepting_the_offer_saves_one_catalog_row_and_clears_it(ctx):
    _confirm_pizza(ctx)
    ctx(text="yes save it", reply_handle="c1")
    food.save_food("pizza slice", 2, "each", 285, 12, 36, 10, 2)

    saved = rows(get_conn(), "SELECT * FROM food_catalog")
    assert len(saved) == 1 and saved[0]["kcal"] == 285
    assert "SAVE OFFER" not in (food.log_system_context(_msg()) or "")   # asked and answered


def test_better_numbers_replace_the_estimate_when_saving(ctx):
    _confirm_pizza(ctx)
    ctx(text="actually the box says 300 kcal per slice", reply_handle="c1")
    food.save_food("pizza slice", 1, "each", 300, 13, 38, 11, 2.5)

    (saved,) = rows(get_conn(), "SELECT * FROM food_catalog")
    assert saved["ref_qty"] == 1 and saved["kcal"] == 300         # the label wins over the guess


def test_declining_leaves_the_log_alone(ctx):
    """The offer is a convenience; the food is logged either way."""
    _confirm_pizza(ctx)
    assert len(rows(get_conn(), "SELECT * FROM food_log")) == 1
    assert rows(get_conn(), "SELECT * FROM food_catalog") == []
