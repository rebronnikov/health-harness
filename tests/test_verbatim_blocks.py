"""Rendered lines are repaired or stripped before history is saved."""
from health.core import harness, history
from health.core.contracts import NormalizedMessage
from health.core.registry import verbatim_block_keys
from health.features import food

from conftest import DEFAULT_TS, text_block, tool_block

DAY = "2026-07-14"
BLOCK = ("Today so far\n"
         "🔥 Calories: 866 / 2200 (1334 left)\n"
         "🍞 Carbs: 78g / 250g (172g left)\n"
         "🌾 Fibre: 13g / 28g (15g left)")
DRIFTED = BLOCK.replace("🍞 Carbs", "🌾 Carbs").replace("🌾 Fibre", "🌰 Fibre")


def test_food_declares_its_rendered_blocks():
    assert {"progress_block", "suggestions_block"} <= verbatim_block_keys()


def test_a_swapped_icon_is_restored():
    assert harness._restore_blocks(f"Logged.\n\n{DRIFTED}\n\nFibre's the gap.", [BLOCK]) == (
        f"Logged.\n\n{BLOCK}\n\nFibre's the gap.")


def test_a_correct_reply_is_untouched():
    reply = f"Logged.\n\n{BLOCK}\n\nFibre's the gap."
    assert harness._restore_blocks(reply, [BLOCK]) == reply


def test_prose_around_the_block_is_untouched():
    """Repair is line-wise on the block's own lines; the steer sentence is the model's."""
    out = harness._restore_blocks(f"{DRIFTED}\n\nLean picks from here.", [BLOCK])
    assert out.endswith("Lean picks from here.")


def test_a_line_whose_numbers_differ_is_left_alone():
    """A changed number is a bug to surface, not one to silently overwrite — the ASCII
    skeleton no longer matches, so the line simply isn't recognised as that block's."""
    wrong = "🍞 Carbs: 99g / 250g (151g left)"
    assert harness._restore_blocks(wrong, [BLOCK]) == wrong


def test_blocks_are_found_however_deeply_a_result_nests_them():
    """The food executors return theirs under `day_totals` and `projected_day_totals`; a
    top-level-only scan would miss every one."""
    found = []
    harness._collect_blocks(
        {"logged": [{"kcal": 1}], "day_totals": {"progress_block": BLOCK}},
        verbatim_block_keys(), found)
    assert found == [BLOCK]


def test_the_repaired_reply_is_what_history_and_the_transcript_keep(fake_client, ctx):
    """The drift fed itself: the mutated reply went into history and came back next turn as
    the example to follow. Repairing before the save is what breaks that loop."""
    ctx(text="seed", reply_handle="c1")
    food.save_food("greek salad", 100, "g", 180, 6, 14, 12, 2)
    fake_client.script(
        [tool_block("log_food", {"date": DAY, "meal": "lunch", "items": [
            {"name": "greek salad", "quantity": 50, "unit": "g"}]}, id="tu-1")],
        [text_block("Logged.\n\nToday so far\n"
                    "🔥 Calories: 90 / 2200 (2110 left)\n"
                    "🌾 Carbs: 7g / 250g (243g left)")],      # the model's swapped icon
    )
    msg = NormalizedMessage(source="telegram", reply_handle="c1",
                            timestamp=DEFAULT_TS, text="50g greek salad")
    reply = harness.handle(msg)

    assert "🍞 Carbs" in reply and "🌾 Carbs" not in reply
    assert "🍞 Carbs" in history.last_reply(
        NormalizedMessage(source="telegram", reply_handle="c1",
                          timestamp=DEFAULT_TS, text="next"))
