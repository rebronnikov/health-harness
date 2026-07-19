"""health.core.history — the bounded per-chat transcript.

Regression cover for the 2026-07-15 salad loop: the bot asked a question, the answer
landed on a turn with no memory of it, and a bare "40g" got matched against whatever the
catalog held. These tests pin the four properties that make recall safe rather than the
recall itself: per-chat isolation, a turn cap, idle expiry, and text-only storage.
"""
from datetime import timedelta

from health.core import history
from health.core.contracts import NormalizedMessage

from conftest import DEFAULT_TS


def _msg(text, handle="chat-1", ts=DEFAULT_TS):
    return NormalizedMessage(source="test", reply_handle=handle, timestamp=ts, text=text)


def test_empty_for_a_new_chat():
    assert history.load(_msg("hi")) == []


def test_recalls_the_previous_turn():
    history.save(_msg("150g of salad: beetroot, chickpeas"), "How much beetroot?")
    assert history.load(_msg("90g")) == [
        {"role": "user", "content": "150g of salad: beetroot, chickpeas"},
        {"role": "assistant", "content": "How much beetroot?"},
    ]


def test_history_never_bleeds_between_chats():
    history.save(_msg("my weight is 72.6", handle="chat-1"), "Logged.")
    assert history.load(_msg("and today?", handle="chat-2")) == []


def test_capped_at_max_turns():
    for i in range(history.MAX_TURNS + 5):
        history.save(_msg(f"msg {i}"), f"reply {i}")
    kept = history.load(_msg("next"))
    assert len(kept) == 2 * history.MAX_TURNS                  # user+assistant per turn
    assert kept[0] == {"role": "user", "content": "msg 5"}     # oldest 5 turns dropped


def test_idle_thread_expires():
    """A stale question must not steer a fresh message — the subtler form of the bug."""
    history.save(_msg("150g of salad"), "How much yoghurt?")
    later = _msg("40g", ts=DEFAULT_TS + history.IDLE_EXPIRY + timedelta(seconds=1))
    assert history.load(later) == []


def test_active_thread_survives_inside_the_window():
    history.save(_msg("150g of salad"), "How much yoghurt?")
    soon = _msg("20g", ts=DEFAULT_TS + history.IDLE_EXPIRY - timedelta(seconds=1))
    assert len(history.load(soon)) == 2


def test_expiry_is_measured_from_the_last_turn_not_the_first():
    """A long back-and-forth is one live thread; only silence expires it."""
    step = history.IDLE_EXPIRY - timedelta(minutes=1)
    history.save(_msg("first", ts=DEFAULT_TS), "reply")
    history.save(_msg("second", ts=DEFAULT_TS + step), "reply")
    assert len(history.load(_msg("third", ts=DEFAULT_TS + 2 * step))) == 4


def test_stores_plain_text_only():
    """Replaying tool_use/tool_result blocks risks an orphaned tool_use (an API error)
    and re-feeds stale tool results, so only the final assistant text is kept."""
    history.save(_msg("40g jumbo oats"), "Logged 40g of jumbo oats.")
    for m in history.load(_msg("next")):
        assert isinstance(m["content"], str)
        assert set(m) == {"role", "content"}


def test_empty_reply_is_not_recorded():
    history.save(_msg("hello"), "")
    assert history.load(_msg("next")) == []
