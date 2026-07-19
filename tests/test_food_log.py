"""log_food — the data-model heart: catalog scaling, each-vs-mass units, the
unresolved path, same-slug accumulation, re-log-adds, and occasion labelling.
"""
import pytest

from health.features import food
from health.store.connection import get_conn

from conftest import rows

DAY = "2026-07-14"


def _log(name, quantity, unit, meal="lunch", **extra):
    item = {"name": name, "quantity": quantity, "unit": unit, **extra}
    return food.log_food(DAY, meal, [item])


def test_catalog_scaling_grams(ctx, catalog):
    ctx()
    out = _log("skyr", 210, "g")                                # per-100g food scaled x2.1
    assert out["unresolved"] == []
    (logged,) = out["logged"]
    assert logged["kcal"] == pytest.approx(107.1)              # 51 * 2.1
    assert logged["protein_g"] == pytest.approx(18.9)          # 9 * 2.1
    assert logged["estimated"] == 0
    assert logged["catalog_slug"] == "skyr"


def test_each_scales_by_count(ctx, catalog):
    ctx()
    (logged,) = _log("banana", 2, "each")["logged"]            # `each` scales by count, not weight
    assert logged["kcal"] == pytest.approx(216.0)              # 108 * 2
    assert logged["amount"] == 2


def test_each_food_logged_in_grams_is_unresolved(ctx, catalog):
    ctx()
    out = _log("banana", 100, "g")                             # banana is `each`; grams don't match
    (miss,) = out["unresolved"]
    assert miss["name"] == "banana"
    assert out["logged"] == []
    assert rows(get_conn(), "SELECT * FROM food_log") == []


def test_mass_food_logged_as_each_is_unresolved(ctx, catalog):
    ctx()
    out = _log("salmon", 1, "each")                            # salmon is per-100g; `each` mismatches
    (miss,) = out["unresolved"]
    assert miss["name"] == "salmon"
    assert out["logged"] == []


def test_unresolved_reason_tells_claude_what_to_do(ctx, catalog):
    """A bare name left Claude to invent an explanation, and it asked the user about
    grams-vs-`each` instead of re-logging. The reason must name the catalog's own unit
    (the fix path) for a mismatch, and say "estimate" for an unknown food."""
    ctx()
    (mismatch,) = _log("salmon", 1, "each")["unresolved"]      # catalog has it, wrong unit family
    assert "100 g" in mismatch["reason"] and "estimat" in mismatch["reason"]

    (unknown,) = _log("pizza", 1, "each")["unresolved"]        # not in the catalog at all
    assert "not in the catalog" in unknown["reason"]


def test_unknown_food_without_macros_is_unresolved(ctx, catalog):
    ctx()
    out = _log("pizza", 1, "each")                             # not in catalog, no macros given
    (miss,) = out["unresolved"]
    assert miss["name"] == "pizza"
    assert out["logged"] == []


def test_user_given_macros_used_verbatim(ctx, catalog):
    ctx()
    (logged,) = food.log_food(DAY, "lunch", [{
        "name": "mystery stew", "quantity": 1, "unit": "each",
        "kcal": 300, "protein_g": 20, "carb_g": 25, "fat_g": 12, "fibre_g": 4,
    }])["logged"]
    assert logged["kcal"] == 300 and logged["protein_g"] == 20
    assert logged["catalog_slug"] is None                      # user macros -> not resolved from catalog


def test_user_macros_with_save_creates_catalog_entry(ctx, catalog):
    ctx()
    (logged,) = food.log_food(DAY, "lunch", [{
        "name": "protein bar", "quantity": 1, "unit": "each", "save": True,
        "kcal": 200, "protein_g": 20, "carb_g": 20, "fat_g": 6, "fibre_g": 3,
    }])["logged"]
    assert logged["catalog_slug"] == "protein_bar"
    saved = rows(get_conn(), "SELECT * FROM food_catalog WHERE slug='protein_bar'")
    assert len(saved) == 1 and saved[0]["ref_qty"] == 1 and saved[0]["ref_unit"] == "each"


def test_accumulate_same_slug_in_one_call(ctx, catalog):
    ctx()
    out = food.log_food(DAY, "snack", [
        {"name": "banana", "quantity": 1, "unit": "each"},
        {"name": "banana", "quantity": 1, "unit": "each"},
    ])
    (logged,) = out["logged"]                                  # merged into ONE occasion row
    assert logged["amount"] == 2
    assert logged["kcal"] == pytest.approx(216.0)
    assert len(rows(get_conn(), "SELECT * FROM food_log")) == 1


def test_relog_adds_a_second_row(ctx, catalog):
    ctx()
    _log("banana", 1, "each", meal="snack")
    out = _log("banana", 1, "each", meal="snack")              # advancing clock -> distinct ingested_at
    assert len(rows(get_conn(), "SELECT * FROM food_log")) == 2
    assert out["day_totals"]["kcal"] == pytest.approx(216.0)   # both helpings counted


def test_occasion_label_numbers_main_meals(ctx, catalog):
    ctx()
    _log("banana", 1, "each", meal="lunch")
    _log("banana", 1, "each", meal="lunch")
    meals = {r["meal"] for r in rows(get_conn(), "SELECT meal FROM food_log")}
    assert meals == {"lunch-1", "lunch-2"}


def test_occasion_label_snack_bucket_and_empty(ctx, catalog):
    ctx()
    _log("banana", 1, "each", meal="latte")                    # non-main label kept as-is
    _log("banana", 1, "each", meal="")                         # empty -> "snack"
    meals = {r["meal"] for r in rows(get_conn(), "SELECT meal FROM food_log")}
    assert meals == {"latte", "snack"}


def test_day_totals_sums_and_frames_targets(ctx, catalog):
    ctx()
    _log("banana", 2, "each", meal="snack")
    totals = food.get_day_totals(DAY)
    assert totals["kcal"] == pytest.approx(216.0)              # rounded to 1dp
    assert totals["targets"] == food.targets.TARGETS
    assert totals["remaining"]["protein_g"]["direction"] == "floor"
