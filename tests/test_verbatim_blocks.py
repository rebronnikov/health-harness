"""Rendered lines are repaired or stripped before history is saved."""
from health.core import harness, history
from health.core.contracts import NormalizedMessage
from health.core.registry import block_signatures, verbatim_block_keys
from health.features import food

from conftest import DEFAULT_TS, text_block, tool_block

DAY = "2026-07-14"
BLOCK = ("Today so far\n"
         "🔥 Calories: 866 / 2200 (1334 left)\n"
         "🍞 Carbs: 78g / 250g (172g left)\n"
         "🌾 Fibre: 13g / 28g (15g left)")
DRIFTED = BLOCK.replace("🍞 Carbs", "🌾 Carbs").replace("🌾 Fibre", "🌰 Fibre")
SIGS = block_signatures()


def enforce(reply, blocks=(BLOCK,)):
    return harness._enforce_blocks(reply, list(blocks), SIGS)


def test_food_declares_its_rendered_blocks():
    assert {"progress_block", "suggestions_block"} <= verbatim_block_keys()


def test_a_swapped_icon_is_restored():
    assert enforce(f"Logged.\n\n{DRIFTED}\n\nFibre's the gap.") == (
        f"Logged.\n\n{BLOCK}\n\nFibre's the gap.")


def test_a_correct_reply_is_untouched():
    reply = f"Logged.\n\n{BLOCK}\n\nFibre's the gap."
    assert enforce(reply) == reply


def test_prose_around_the_block_is_untouched():
    """Repair is line-wise on the block's own lines; the steer sentence is the model's."""
    out = enforce(f"{DRIFTED}\n\nLean picks from here.")
    assert out.endswith("Lean picks from here.")


def test_a_line_whose_numbers_differ_is_stripped_and_disowned():
    """A changed number is not repairable — the ASCII skeleton no longer matches any line a
    tool returned, so the harness cannot know what was meant. It used to be left alone,
    which shipped the wrong figure; it is now removed and the reply says it was not real.
    The user runs a deficit off these numbers and cannot audit them by eye."""
    out = enforce("🍞 Carbs: 99g / 250g (151g left)")
    assert "99g" not in out
    assert harness.FABRICATION_NOTE.strip() in out


def test_a_block_no_tool_returned_is_stripped(caplog):
    fabricated = (f"Logged — another protein bar as a snack.\n\n{BLOCK}\n\n"
                  "Very little room left tonight.")
    out = enforce(fabricated, blocks=())
    assert "866" not in out and "Today so far" not in out
    assert "Very little room left tonight." in out          # prose is never touched
    assert harness.FABRICATION_NOTE.strip() in out


def test_a_superseded_block_in_the_same_turn_is_not_a_fabrication():
    """A turn that confirms an estimate AND logs a food returns two blocks for one day, and
    the reply shows only the later. The earlier one shares the heading line, which used to
    trip the old "altered beyond line repair" warning; both are canon, so neither does."""
    superseded = BLOCK.replace("866", "758").replace("1434", "1542")
    out = enforce(f"Logged.\n\n{BLOCK}", blocks=(superseded, BLOCK))
    assert harness.FABRICATION_NOTE.strip() not in out
    assert BLOCK in out


def test_the_steer_sentence_is_never_mistaken_for_a_block_line():
    """The signature has to be narrow enough that ordinary prose naming a macro survives —
    the prompt REQUIRES that sentence, so a false positive would strip it every turn."""
    steer = "Protein and fibre are the ones to chase, and fat is over half spent."
    assert enforce(steer, blocks=()) == steer


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
