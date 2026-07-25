"""harness.handle × history — that prior turns actually reach the model.

The 2026-07-15 salad loop was a wiring bug, not a logic bug: every turn started from a
bare [{"role": "user", ...}], so a follow-up answered a question the model couldn't see.
These tests assert on the `messages` the harness hands the API, which is the exact thing
that was wrong.
"""
from datetime import timedelta

from health.core import harness, history
from health.core.contracts import NormalizedMessage

from conftest import DEFAULT_TS, text_block, tool_block


def _msg(text, handle="chat-1", ts=DEFAULT_TS):
    return NormalizedMessage(source="telegram", reply_handle=handle, timestamp=ts, text=text)


def test_first_turn_sends_only_the_users_message(fake_client):
    fake_client.script([text_block("Logged.")])
    harness.handle(_msg("40g jumbo oats"))
    assert fake_client.calls[0]["messages"] == [{"role": "user", "content": "40g jumbo oats"}]


def test_followup_turn_carries_the_previous_exchange(fake_client):
    """The regression. Turn 2's request must contain turn 1 — otherwise "90g" is a
    naked number and the model matches it against whatever the catalog happens to hold."""
    fake_client.script([text_block("How much beetroot?")], [text_block("Logged.")])
    harness.handle(_msg("150g of salad: beetroot, chickpeas"))
    harness.handle(_msg("90g", ts=DEFAULT_TS + timedelta(seconds=30)))

    assert fake_client.calls[1]["messages"] == [
        {"role": "user", "content": "150g of salad: beetroot, chickpeas"},
        {"role": "assistant", "content": "How much beetroot?"},
        {"role": "user", "content": "90g"},
    ]


def test_tool_blocks_never_reach_the_next_turn(ctx, catalog, fake_client):
    """Only the final assistant text is recalled. Replaying a stored tool_use without its
    tool_result is an API error, and stale tool results would re-feed old estimates."""
    fake_client.script(
        [tool_block("get_day_totals", {"date": "2026-07-14"})],   # turn 1 runs a tool...
        [text_block("You're at 216 kcal.")],                      # ...then answers
        [text_block("Sure.")],                                    # turn 2
    )
    harness.handle(_msg("how am I doing?"))
    harness.handle(_msg("thanks", ts=DEFAULT_TS + timedelta(seconds=30)))

    recalled = fake_client.calls[2]["messages"]                   # turn 2's request
    assert recalled == [
        {"role": "user", "content": "how am I doing?"},
        {"role": "assistant", "content": "You're at 216 kcal."},
        {"role": "user", "content": "thanks"},
    ]


def test_separate_chats_do_not_share_context(fake_client):
    fake_client.script([text_block("Logged.")], [text_block("Logged.")])
    harness.handle(_msg("my weight is 72.6", handle="chat-1"))
    harness.handle(_msg("and today?", handle="chat-2"))
    assert fake_client.calls[1]["messages"] == [{"role": "user", "content": "and today?"}]


def test_stale_thread_does_not_steer_a_fresh_message(fake_client):
    fake_client.script([text_block("How much yoghurt?")], [text_block("Logged.")])
    harness.handle(_msg("150g of salad"))
    harness.handle(_msg("40g", ts=DEFAULT_TS + history.IDLE_EXPIRY + timedelta(minutes=1)))
    assert fake_client.calls[1]["messages"] == [{"role": "user", "content": "40g"}]


def test_refusal_returns_failsafe_not_empty_reply(fake_client):
    """Safety classifiers can end a turn with stop_reason 'refusal' and no text
    (HTTP 200). The user must still get a real reply, and the empty turn must not
    enter history."""
    fake_client.script(([], "refusal"), [text_block("Logged.")])
    reply = harness.handle(_msg("40g jumbo oats"))
    assert "nothing was logged" in reply.lower()

    harness.handle(_msg("40g jumbo oats", ts=DEFAULT_TS + timedelta(seconds=10)))
    assert fake_client.calls[1]["messages"] == [           # refused turn left no trace
        {"role": "user", "content": "40g jumbo oats"},
    ]


def test_context_is_not_polluted_by_a_failed_turn(fake_client):
    """A turn that raises never reaches history.save, so a retry starts clean rather than
    inheriting a half-finished exchange."""
    fake_client.script([text_block("ok")])
    harness.handle(_msg("first"))
    try:
        harness.handle(_msg("second", ts=DEFAULT_TS + timedelta(seconds=10)))  # no script left
    except AssertionError:
        pass
    fake_client.script([text_block("ok")])
    harness.handle(_msg("third", ts=DEFAULT_TS + timedelta(seconds=20)))
    assert fake_client.calls[-1]["messages"] == [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "third"},
    ]
