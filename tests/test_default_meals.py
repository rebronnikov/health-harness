"""Catalog defaults split selected foods into their configured meals."""
import pytest

from health.features import food
from health.store.connection import get_conn

from conftest import rows

DAY = "2026-07-14"


@pytest.fixture
def staples(ctx):
    """The catalog entries the defaults name, with their real reference portions."""
    ctx(text="seed")
    food.save_food("creatine", 1, "each", 0, 0, 0, 0, 0)
    food.save_food("psyllium husk", 8, "g", 0, 0, 0, 0, 7)
    food.save_food("protein bar", 1, "each", 118, 2.1, 11.4, 7.3, 0.3)
    food.save_food("jumbo oats", 40, "g", 148, 5.2, 24, 3.4, 3.6)   # no default meal


def _meals():
    return {r["food_slug"]: r["meal"]
            for r in rows(get_conn(), "SELECT food_slug, meal FROM food_log")}


def test_a_defaulted_food_needs_no_meal_and_asks_nothing(ctx, staples):
    ctx()
    out = food.log_food(DAY, items=[{"name": "creatine", "quantity": 1, "unit": "each"}])
    assert out["meals"] == ["supplements"]
    assert out["needs_meal"] is False               # the question this whole change removes


def test_the_default_outranks_a_named_meal(ctx, staples):
    """The deliberate, surprising half — see _resolve_meal."""
    ctx()
    food.log_food(DAY, "breakfast", [{"name": "creatine", "quantity": 1, "unit": "each"}])
    assert _meals()["creatine"] == "supplements"


def test_one_message_can_write_two_meals(ctx, staples):
    ctx()
    out = food.log_food(DAY, "breakfast", [
        {"name": "jumbo oats", "quantity": 50, "unit": "g"},
        {"name": "creatine", "quantity": 1, "unit": "each"},
        {"name": "psyllium husk", "quantity": 8, "unit": "g"},
    ])
    assert out["meals"] == ["breakfast", "supplements"]
    assert out["needs_meal"] is False
    assert _meals() == {"jumbo_oats": "breakfast",
                        "creatine": "supplements",
                        "psyllium_husk": "supplements"}


def test_snack_defaults_apply_too(ctx, staples):
    ctx()
    food.log_food(DAY, "dinner", [
        {"name": "protein bar", "quantity": 1, "unit": "each"}])
    assert _meals()["protein_bar"] == "snack"


def test_an_ordinary_food_still_waits_for_its_label(ctx, staples):
    """Everything not on the list keeps the old behaviour — logged, flagged, asked about."""
    ctx()
    out = food.log_food(DAY, items=[{"name": "jumbo oats", "quantity": 50, "unit": "g"}])
    assert out["meals"] == [food.UNLABELLED_MEAL]
    assert out["needs_meal"] is True


def test_a_mixed_message_asks_only_about_the_unlabelled_row(ctx, staples):
    ctx()
    out = food.log_food(DAY, items=[
        {"name": "jumbo oats", "quantity": 50, "unit": "g"},
        {"name": "creatine", "quantity": 1, "unit": "each"},
    ])
    assert out["meals"] == ["supplements", food.UNLABELLED_MEAL]
    assert out["needs_meal"] is True               # owed for the oats, not the creatine
    assert _meals()["creatine"] == "supplements"


def test_every_default_key_is_a_real_slug():
    """The guardrail. A renamed or mistyped name doesn't error — it just stops matching and
    falls through to the old behaviour, which is invisible until the owner notices the question
    came back. These are the catalog's own display names, so they must round-trip."""
    for name, expected in [
        ("psyllium husk", "supplements"),
        ("creatine", "supplements"),
        ("vitamin D", "supplements"),
        ("fish oil", "supplements"),
        ("protein bar", "snack"),
        ("dates", "snack"),
    ]:
        assert food.DEFAULT_MEALS[food._slug(name)] == expected
    assert len(food.DEFAULT_MEALS) == 6            # nothing silently collapsed on a slug clash


def test_estimates_are_not_defaulted(ctx, staples):
    """confirm_pending is for foods with no catalog entry, and its `meal` is an explicit
    correction — a default must never outrank it. Applying the map there too is the obvious
    wrong move, so this pins it."""
    ctx(text="a handful of something", reply_handle="c1")
    food.propose_food_estimate(DAY, "breakfast", [{
        "name": "creatine", "quantity": 1, "unit": "each",
        "kcal": 0, "protein_g": 0, "carb_g": 0, "fat_g": 0, "fibre_g": 0}])
    ctx(text="yes", reply_handle="c1")
    assert food.confirm_pending()["meal"] == "breakfast"
