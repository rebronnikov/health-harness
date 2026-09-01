"""Confirmation requires the previously displayed estimate and the intended food."""
from datetime import timedelta

import pytest

from health.core import harness, history
from health.core.contracts import NormalizedMessage
from health.features import food
from health.store.connection import get_conn

from conftest import DEFAULT_TS, rows, text_block, tool_block

DAY = "2026-07-14"
SALAD = [{"name": "greek salad", "quantity": 57, "unit": "g",
          "kcal": 104, "protein_g": 3, "carb_g": 7, "fat_g": 6, "fibre_g": 1}]


def _msg(text, at=DEFAULT_TS, handle="c1"):
    return NormalizedMessage(source="telegram", reply_handle=handle, timestamp=at, text=text)


# --- the catch-up: the user has already seen the numbers ---------------------------

def test_same_turn_confirm_commits_when_the_previous_reply_showed_the_numbers(ctx):
    """Allow one confirmation when the previous reply already showed the estimate."""
    first = ctx(text="57g of Greek Salad", reply_handle="c1")
    history.save(first, "Here's my estimate for 57g: ~104 kcal, 3g protein, 7g carbs.")

    ctx(text="Yes please", reply_handle="c1")
    food.propose_food_estimate(DAY, "lunch", SALAD)      # the model catching up
    (logged,) = food.confirm_pending()["logged"]         # same turn — but they have seen it

    assert logged["estimated"] == 1
    assert logged["food_slug"] == "greek_salad"
    assert "c1" not in food._pending


def test_same_turn_confirm_still_refused_when_the_previous_reply_did_not(ctx):
    first = ctx(text="57g of Greek Salad", reply_handle="c1")
    history.save(first, "Which meal was that?")          # a reply carrying no estimate

    ctx(text="Yes please", reply_handle="c1")
    food.propose_food_estimate(DAY, "lunch", SALAD)
    with pytest.raises(ValueError, match="CURRENT message"):
        food.confirm_pending()
    assert rows(get_conn(), "SELECT * FROM food_log") == []
    assert food._pending["c1"]["rows"][0]["food_slug"] == "greek_salad"   # survives


def test_every_item_must_have_been_shown(ctx):
    """One recognisable figure can't wave a multi-item proposal through."""
    first = ctx(text="a salad and a roll", reply_handle="c1")
    history.save(first, "The salad is about 104 kcal.")   # the roll's 250 was never shown

    ctx(text="yes", reply_handle="c1")
    food.propose_food_estimate(DAY, "lunch", SALAD + [
        {"name": "cheese toastie", "quantity": 1, "unit": "each",
         "kcal": 250, "protein_g": 6, "carb_g": 15, "fat_g": 19, "fibre_g": 1}])
    with pytest.raises(ValueError, match="CURRENT message"):
        food.confirm_pending()
    assert rows(get_conn(), "SELECT * FROM food_log") == []


# --- the meal named while confirming ------------------------------------------------

def test_confirm_overrides_the_proposed_meal(ctx):
    ctx(text="Wild Rice & Walnut salad, 60g", reply_handle="c1")
    food.propose_food_estimate(DAY, "snack", SALAD)
    ctx(text="Log it for lunch", reply_handle="c1")
    (logged,) = food.confirm_pending(meal="lunch")["logged"]
    assert logged["meal"] == "lunch"


def test_confirm_keeps_the_proposed_meal_when_none_is_given(ctx):
    ctx(text="Wild Rice & Walnut salad, 60g", reply_handle="c1")
    food.propose_food_estimate(DAY, "snack", SALAD)
    ctx(text="yes", reply_handle="c1")
    assert food.confirm_pending()["logged"][0]["meal"] == "snack"


# --- confirming the wrong food, and confirming a dead proposal ----------------------

def test_confirm_refuses_a_food_that_is_not_the_pending_one(ctx):
    ctx(text="beetroot salad 60g", reply_handle="c1")
    food.propose_food_estimate(DAY, "lunch", SALAD)
    ctx(text="yes log it", reply_handle="c1")
    with pytest.raises(ValueError, match="greek salad"):
        food.confirm_pending(expect_food="pear rocket salad")
    assert rows(get_conn(), "SELECT * FROM food_log") == []
    assert "c1" in food._pending                          # still there for its own yes


def test_confirm_accepts_a_matching_expect_food(ctx):
    ctx(text="57g greek salad", reply_handle="c1")
    food.propose_food_estimate(DAY, "lunch", SALAD)
    ctx(text="yes", reply_handle="c1")
    assert food.confirm_pending(expect_food="Greek Salad")["logged"]


def test_an_abandoned_proposal_goes_stale(ctx):
    ctx(text="57g greek salad", reply_handle="c1")
    food.propose_food_estimate(DAY, "lunch", SALAD)
    ctx(text="yes", reply_handle="c1", timestamp=DEFAULT_TS + food.PENDING_EXPIRY + timedelta(minutes=1))
    with pytest.raises(ValueError, match="too old"):
        food.confirm_pending()
    assert rows(get_conn(), "SELECT * FROM food_log") == []
    assert "c1" not in food._pending                       # dropped, not left to mislead


def test_the_pending_fragment_hides_a_stale_proposal(ctx):
    ctx(text="57g greek salad", reply_handle="c1")
    food.propose_food_estimate(DAY, "lunch", SALAD)
    assert "PENDING" in food.log_system_context(_msg("anything"))
    late = _msg("anything", at=DEFAULT_TS + food.PENDING_EXPIRY + timedelta(minutes=1))
    assert food.log_system_context(late) is None


# --- end to end through the harness --------------------------------------------------

def test_narrated_estimate_then_yes_logs_in_one_confirmation(fake_client):
    fake_client.script(
        [text_block("Estimate for 57g: ~104 kcal, 3g protein. Log it?")],   # turn 1: no tool
        [tool_block("propose_food_estimate",
                    {"date": DAY, "meal": "lunch", "items": SALAD}, id="tu-1")],
        [tool_block("confirm_pending", {}, id="tu-2")],                    # same turn
        [text_block("Logged.")],
    )
    harness.handle(_msg("57g of Greek Salad"))
    harness.handle(_msg("Yes please", at=DEFAULT_TS + timedelta(seconds=30)))

    (row,) = rows(get_conn(), "SELECT * FROM food_log")
    assert row["estimated"] == 1 and row["food_slug"] == "greek_salad"
    assert row["raw"] == "57g of Greek Salad"       # provenance: the food message
