"""log_food — the data-model heart: catalog scaling, each-vs-mass units, the
unresolved path, same-slug accumulation, re-log-adds, and occasion labelling.
"""
import json
from datetime import timedelta

import pytest

from health.core import harness
from health.core.contracts import NormalizedMessage
from health.features import food
from health.store.connection import get_conn

from conftest import DEFAULT_TS, rows, text_block, tool_block

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


def test_macros_ref_qty_scales_user_macros(ctx, catalog):
    ctx()
    (logged,) = food.log_food(DAY, "breakfast", [{
        "name": "smoked salmon", "quantity": 63, "unit": "g", "macros_ref_qty": 100,
        "kcal": 184, "protein_g": 23.3, "carb_g": 0, "fat_g": 9.9, "fibre_g": 0,
    }])["logged"]
    assert logged["amount"] == 63
    assert logged["kcal"] == pytest.approx(115.92)             # 184 * 63/100
    assert logged["protein_g"] == pytest.approx(14.679)
    assert logged["fat_g"] == pytest.approx(6.237)


def test_macros_ref_qty_save_stores_reference_portion(ctx, catalog):
    ctx()
    food.log_food(DAY, "breakfast", [{
        "name": "smoked salmon", "quantity": 63, "unit": "g", "macros_ref_qty": 100,
        "save": True,
        "kcal": 184, "protein_g": 23.3, "carb_g": 0, "fat_g": 9.9, "fibre_g": 0,
    }])
    (saved,) = rows(get_conn(), "SELECT * FROM food_catalog WHERE slug='smoked_salmon'")
    assert saved["ref_qty"] == 100 and saved["kcal"] == 184    # the STATED reference, unscaled
    (relog,) = _log("smoked salmon", 100, "g")["logged"]       # later catalog hit scales cleanly
    assert relog["kcal"] == pytest.approx(184.0)


def test_log_food_supersedes_pending_estimate_for_same_food(ctx, catalog):
    """User answers an estimate proposal with real macros: committing the log must
    clear the stale pending so a later 'yes' can't double-log the same food."""
    ctx(text="63g smoked salmon", reply_handle="c1")
    food.propose_food_estimate(DAY, "breakfast", [{
        "name": "smoked salmon", "quantity": 63, "unit": "g",
        "kcal": 117, "protein_g": 12.8, "carb_g": 0, "fat_g": 7.4, "fibre_g": 0,
    }])
    ctx(text="per 100g: 184 kcal ... I ate 63g", reply_handle="c1")
    food.log_food(DAY, "breakfast", [{
        "name": "smoked salmon", "quantity": 63, "unit": "g", "macros_ref_qty": 100,
        "kcal": 184, "protein_g": 23.3, "carb_g": 0, "fat_g": 9.9, "fibre_g": 0,
    }])
    assert "c1" not in food._pending
    assert "error" in food.confirm_pending()


_MINCE_ITEM = {
    "name": "beef mince", "quantity": 200, "unit": "g",
    "kcal": 344, "protein_g": 42, "carb_g": 0, "fat_g": 20, "fibre_g": 0,
}


def test_confirm_pending_refused_in_proposing_turn(ctx):
    ctx(text="200g beef mince", reply_handle="c1")
    food.propose_food_estimate(DAY, "dinner", [_MINCE_ITEM])
    out = food.confirm_pending()
    assert "error" in out and out["pending"][0]["food_slug"] == "beef_mince"
    assert rows(get_conn(), "SELECT * FROM food_log") == []
    assert food._pending["c1"]["rows"][0]["food_slug"] == "beef_mince"


def test_confirm_pending_commits_on_a_later_turn(ctx):
    ctx(text="200g beef mince", reply_handle="c1")
    food.propose_food_estimate(DAY, "dinner", [_MINCE_ITEM])
    ctx(text="yes", reply_handle="c1")                         # a NEW message, same chat
    (logged,) = food.confirm_pending()["logged"]
    assert logged["estimated"] == 1
    assert logged["raw"] == "200g beef mince"                  # provenance: the food message, not the "yes"
    assert "c1" not in food._pending


def test_harness_cannot_self_confirm_estimate(fake_client):
    """End-to-end replay of the incident through harness.handle: propose and confirm in
    one turn logs nothing, and the user's real 'yes' next turn still commits."""
    fake_client.script(
        [tool_block("propose_food_estimate",
                    {"date": DAY, "meal": "dinner", "items": [_MINCE_ITEM]}, id="tu-1")],
        [tool_block("confirm_pending", {}, id="tu-2")],        # same-turn self-confirm
        [text_block("Estimated 344 kcal — confirm?")],
        [tool_block("confirm_pending", {}, id="tu-3")],        # turn 2, after the user's "yes"
        [text_block("Logged.")],
    )
    msg = NormalizedMessage(source="telegram", reply_handle="chat-1",
                            timestamp=DEFAULT_TS, text="200g beef mince")
    harness.handle(msg)
    assert rows(get_conn(), "SELECT * FROM food_log") == []    # the refusal wrote nothing

    refusal = json.loads(fake_client.calls[2]["messages"][-1]["content"][0]["content"])
    assert "error" in refusal and "CURRENT message" in refusal["error"]

    harness.handle(NormalizedMessage(source="telegram", reply_handle="chat-1",
                                     timestamp=DEFAULT_TS + timedelta(seconds=30), text="yes"))
    (row,) = rows(get_conn(), "SELECT * FROM food_log")
    assert row["estimated"] == 1 and row["raw"] == "200g beef mince"


def test_log_food_keeps_unrelated_pending(ctx, catalog):
    ctx(text="pizza", reply_handle="c1")
    food.propose_food_estimate(DAY, "dinner", [{
        "name": "pizza slice", "quantity": 2, "unit": "each",
        "kcal": 285, "protein_g": 12, "carb_g": 36, "fat_g": 10, "fibre_g": 2,
    }])
    ctx(text="1 banana", reply_handle="c1")
    _log("banana", 1, "each", meal="snack")                    # unrelated food mid-proposal
    assert food._pending["c1"]["rows"][0]["food_slug"] == "pizza_slice"
