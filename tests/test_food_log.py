"""log_food — the data-model heart: catalog scaling, each-vs-mass units, the
unresolved path, same-slug accumulation, re-log-adds, and occasion labelling.
"""
from datetime import timedelta

import pytest

from health.core import harness
from health.core.contracts import NormalizedMessage
from health.features import food
from health.store import writer
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
    """A real second helping still ADDS — that is why food_log's key carries ingested_at.
    It has to say so now, because an unflagged immediate repeat is the confirm-turn bug."""
    ctx()
    _log("banana", 1, "each", meal="snack")
    out = _log("banana", 1, "each", meal="snack", allow_duplicate=True)
    assert len(rows(get_conn(), "SELECT * FROM food_log")) == 2
    assert out["day_totals"]["kcal"] == pytest.approx(216.0)   # both helpings counted


def test_an_immediate_identical_relog_is_skipped_and_reported(ctx, catalog):
    ctx()
    _log("banana", 1, "each", meal="snack")
    out = _log("banana", 1, "each", meal="snack")              # the confirm turn, restating
    assert len(rows(get_conn(), "SELECT * FROM food_log")) == 1
    assert out["logged"] == []
    (skipped,) = out["duplicate_skipped"]
    assert skipped["food_name"] == "banana" and skipped["seconds_ago"] <= 2
    assert out["day_totals"]["kcal"] == pytest.approx(108.0)   # counted once


def test_the_same_food_in_a_different_meal_is_never_a_duplicate(ctx, catalog):
    ctx()
    _log("banana", 1, "each", meal="dinner")
    out = _log("banana", 1, "each", meal="lunch")
    assert len(rows(get_conn(), "SELECT * FROM food_log")) == 2
    assert out["duplicate_skipped"] == []


def test_a_repeat_outside_the_window_is_never_a_duplicate(ctx, catalog, monkeypatch):
    ctx()
    _log("banana", 1, "each", meal="snack")
    later = writer.now() + food._DUPLICATE_WINDOW + timedelta(seconds=1)
    monkeypatch.setattr(writer, "now", lambda: later)
    out = _log("banana", 1, "each", meal="snack")
    assert len(rows(get_conn(), "SELECT * FROM food_log")) == 2
    assert out["duplicate_skipped"] == []


def test_repeated_meal_label_is_one_meal_not_numbered(ctx, catalog):
    """A meal is often logged across several messages (the oats, then the coffee). Those
    used to become lunch-1 and lunch-2, splitting one sitting in two with no way back."""
    ctx()
    _log("banana", 1, "each", meal="lunch")
    _log("banana", 1, "each", meal="lunch")
    meals = {r["meal"] for r in rows(get_conn(), "SELECT meal FROM food_log")}
    assert meals == {"lunch"}


def test_a_second_sitting_is_whatever_the_user_calls_it(ctx, catalog):
    ctx()
    _log("banana", 1, "each", meal="breakfast")
    _log("banana", 1, "each", meal="second breakfast")         # passed through verbatim
    meals = {r["meal"] for r in rows(get_conn(), "SELECT meal FROM food_log")}
    assert meals == {"breakfast", "second breakfast"}


def test_missing_meal_logs_as_unlabelled_and_flags_it(ctx, catalog):
    """Food is never held hostage to a question: it lands immediately, marked for a label."""
    ctx()
    out = food.log_food(DAY, items=[{"name": "banana", "quantity": 1, "unit": "each"}])
    assert out["meals"] == [food.UNLABELLED_MEAL]
    assert out["needs_meal"] is True
    assert out["day_totals"]["kcal"] == pytest.approx(108.0)    # counted while it waits
    assert [r["meal"] for r in rows(get_conn(), "SELECT meal FROM food_log")] == ["unlabelled"]


def test_blank_meal_is_unlabelled_not_snack(ctx, catalog):
    ctx()
    _log("banana", 1, "each", meal="")                         # empty -> unlabelled, never guessed
    _log("banana", 1, "each", meal="latte")                    # any label kept as-is
    meals = {r["meal"] for r in rows(get_conn(), "SELECT meal FROM food_log")}
    assert meals == {"unlabelled", "latte"}


def _msg(text="which meal?"):
    return NormalizedMessage(source="test", reply_handle="chat-1",
                             timestamp=DEFAULT_TS, text=text)


def test_unlabelled_rows_are_named_in_the_system_context(ctx, catalog):
    """So a bare 'breakfast' next turn lands on a turn that knows what was asked —
    history is capped and expires, this is not."""
    ctx()
    assert food.log_system_context(_msg()) is None              # nothing waiting -> no noise
    food.log_food(DAY, items=[{"name": "skyr", "quantity": 40, "unit": "g"}])

    frag = food.log_system_context(_msg())
    assert "UNLABELLED" in frag and "skyr" in frag and DAY in frag


def test_unlabelled_context_is_bounded(ctx, catalog):
    """An unanswered question must not grow the system prompt without limit."""
    ctx()
    for day in range(1, 6):                                     # five separate unlabelled days
        food.log_food(f"2026-07-0{day}", items=[{"name": "skyr", "quantity": 40, "unit": "g"}])
    frag = food.log_system_context(_msg())
    assert frag.count("2026-07-0") == food._UNLABELLED_DAYS     # only the recent days spelled out
    assert "plus 3 earlier days" in frag


def test_day_totals_sums_and_frames_targets(ctx, catalog):
    ctx()
    _log("banana", 2, "each", meal="snack")
    totals = food.get_day_totals(DAY)
    assert totals["kcal"] == pytest.approx(216.0)              # rounded to 1dp
    assert totals["targets"] == food.targets.TARGETS
    assert totals["remaining"]["protein_g"]["direction"] == "floor"


def test_every_food_result_carries_the_finished_progress_block(ctx, catalog):
    """The reply's wording is rendered here, not composed by the model — so a result that
    reports the day must hand back the block, whichever executor produced it."""
    ctx()
    logged = _log("banana", 2, "each", meal="snack")
    expected = "🔥 Calories: 216 / 2200 (1984 left)"
    assert expected in logged["day_totals"]["progress_block"]
    assert expected in food.get_day_totals(DAY)["progress_block"]
    assert expected in food.suggest_food(DAY)["progress_block"]
    assert logged["day_totals"]["progress_block"].startswith("Today so far\n")
    # ...and the suggestion list is rendered the same way, wherever it comes from.
    assert food.suggest_food(DAY)["suggestions_block"].startswith("To close the gaps\n")


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
    with pytest.raises(ValueError, match="Nothing is pending"):
        food.confirm_pending(expect_food="smoked salmon")


_MINCE_ITEM = {
    "name": "beef mince", "quantity": 200, "unit": "g",
    "kcal": 344, "protein_g": 42, "carb_g": 0, "fat_g": 20, "fibre_g": 0,
}


def test_confirm_pending_refused_in_proposing_turn(ctx):
    ctx(text="200g beef mince", reply_handle="c1")
    food.propose_food_estimate(DAY, "dinner", [_MINCE_ITEM])
    with pytest.raises(ValueError, match="CURRENT message"):
        food.confirm_pending(expect_food="beef mince")
    assert rows(get_conn(), "SELECT * FROM food_log") == []
    assert food._pending["c1"]["rows"][0]["food_slug"] == "beef_mince"


def test_confirm_pending_commits_on_a_later_turn(ctx):
    ctx(text="200g beef mince", reply_handle="c1")
    food.propose_food_estimate(DAY, "dinner", [_MINCE_ITEM])
    ctx(text="yes", reply_handle="c1")                         # a NEW message, same chat
    (logged,) = food.confirm_pending(expect_food="beef mince")["logged"]
    assert logged["estimated"] == 1
    assert logged["raw"] == "200g beef mince"                  # provenance: the food message, not the "yes"
    assert "c1" not in food._pending


def test_harness_cannot_self_confirm_estimate(fake_client):
    """End-to-end replay of the incident through harness.handle: propose and confirm in
    one turn logs nothing, and the user's real 'yes' next turn still commits."""
    fake_client.script(
        [tool_block("propose_food_estimate",
                    {"date": DAY, "meal": "dinner", "items": [_MINCE_ITEM]}, id="tu-1")],
        [tool_block("confirm_pending", {"expect_food": "beef mince"}, id="tu-2")],   # same-turn
        [text_block("Estimated 344 kcal — confirm?")],
        [tool_block("confirm_pending", {"expect_food": "beef mince"}, id="tu-3")],   # turn 2
        [text_block("Logged.")],
    )
    msg = NormalizedMessage(source="telegram", reply_handle="chat-1",
                            timestamp=DEFAULT_TS, text="200g beef mince")
    harness.handle(msg)
    assert rows(get_conn(), "SELECT * FROM food_log") == []    # the refusal wrote nothing

    refusal = fake_client.calls[2]["messages"][-1]["content"][0]
    assert refusal["is_error"] and "CURRENT message" in refusal["content"]

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


# --- user-given macros: the basis must be stated ------------------------------------

def test_per_item_macros_for_a_count_must_state_their_basis(ctx, catalog):
    ctx(text="Latte x2\n83kcal")
    with pytest.raises(ValueError, match="macros_ref_qty is required"):
        _log("latte", 2, "each", meal="snack",
             kcal=83, protein_g=4.1, carb_g=5.6, fat_g=4.9, fibre_g=0)


def test_a_stated_per_item_basis_scales_to_the_count(ctx, catalog):
    ctx(text="two lattes, 83 kcal each")
    (logged,) = _log("latte", 2, "each", meal="snack", macros_ref_qty=1,
                     kcal=83, protein_g=4.1, carb_g=5.6, fat_g=4.9, fibre_g=0)["logged"]
    assert logged["kcal"] == pytest.approx(166.0)


def test_a_single_item_needs_no_basis(ctx, catalog):
    """Quantity 1 is the one case where per-one and per-all are the same number."""
    ctx(text="a latte, 83 kcal")
    (logged,) = _log("latte", 1, "each", meal="snack",
                     kcal=83, protein_g=4.1, carb_g=5.6, fat_g=4.9, fibre_g=0)["logged"]
    assert logged["kcal"] == pytest.approx(83.0)


def test_partial_macros_are_refused_not_silently_replaced_by_the_catalog(ctx, catalog):
    """`fibre_g` is not required by the schema, and `has_macros` demanded all five — so
    four-of-five fell through to the catalog branch and logged STALE catalog macros,
    discarding the label the user had just read out. No error, nothing in `unresolved`."""
    ctx(text="250g skyr, the tub says 63 kcal per 100g")
    with pytest.raises(ValueError, match="all-or-nothing"):
        _log("skyr", 250, "g", meal="snack", macros_ref_qty=100,
             kcal=63, protein_g=11, carb_g=4, fat_g=0.2)
    assert rows(get_conn(), "SELECT * FROM food_log") == []


def test_a_per_100g_basis_on_a_counted_food_is_refused(ctx, catalog):
    """Per-100g macros pinned to an `each` item divide every serving by 100 — and with
    `save` freeze "per 100 each" into the catalog, dividing every FUTURE log by 100 too."""
    ctx(text="2 slices of rye, 250 kcal per 100g, save it")
    with pytest.raises(ValueError, match="counted in `each`"):
        _log("rye bread", 2, "each", macros_ref_qty=100, save=True,
             kcal=250, protein_g=9, carb_g=45, fat_g=2, fibre_g=6)


def test_a_zero_basis_is_refused_rather_than_treated_as_absent(ctx, catalog):
    ctx(text="baked beans")
    with pytest.raises(ValueError, match="must be positive"):
        _log("baked beans", 200, "g", macros_ref_qty=0,
             kcal=50, protein_g=2, carb_g=8, fat_g=0, fibre_g=2)


# --- macros that don't add up -------------------------------------------------------

def test_calories_that_contradict_their_macros_are_flagged_but_still_logged(ctx, catalog):
    """Warn when per-portion calories and per-weight macros disagree."""
    ctx(text="Lentil stew as dinner\n191kcal\n15.2g carbs\n6.1g protein")
    out = _log("lentil stew", 185, "g", meal="dinner", macros_ref_qty=185,
               kcal=191, protein_g=6.1, carb_g=15.2, fat_g=0, fibre_g=5)
    assert len(out["logged"]) == 1                        # logged anyway
    (flag,) = out["macro_check"]
    assert flag["stated_kcal"] == 191 and flag["implied_by_macros_kcal"] == pytest.approx(95.2)


def test_ordinary_foods_never_trip_the_macro_check(ctx, catalog):
    ctx(text="220g skyr")
    assert _log("skyr", 220, "g", meal="breakfast")["macro_check"] == []
    assert _log("smoked salmon", 63, "g", meal="lunch", macros_ref_qty=100,
                kcal=184, protein_g=23.3, carb_g=0, fat_g=9.9,
                fibre_g=0)["macro_check"] == []


def test_an_unknown_unit_is_refused_rather_than_passed_through(ctx, catalog):
    """`tbsp` used to stay `tbsp`: it matches no catalog entry and, once saved, freezes a
    reference portion nothing can scale."""
    ctx(text="2 tbsp of skyr")
    with pytest.raises(ValueError, match="not a unit"):
        _log("skyr", 2, "tbsp")
