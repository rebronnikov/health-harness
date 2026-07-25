"""The adapter's reply splitter — the pure half of the only channel-specific code.

Telegram's sendMessage rejects anything over 4096 characters. The generic reply path
sent one message and no more, so a long enough reply (a multi-day summary) failed
outright and the user got nothing back. /catalog already split its table; this is the
same idea for ordinary replies.
"""
from health.adapters.telegram_adapter import TELEGRAM_MAX_CHARS, split_reply


def test_a_short_reply_is_one_message():
    assert split_reply("Logged 40g of oats.") == ["Logged 40g of oats."]
    assert split_reply("x" * TELEGRAM_MAX_CHARS) == ["x" * TELEGRAM_MAX_CHARS]


def test_a_long_reply_splits_on_line_boundaries():
    lines = [f"{d:02d} Jul — 2100 kcal, 165g protein, 31g fibre" for d in range(1, 200)]
    parts = split_reply("\n".join(lines))

    assert len(parts) > 1
    assert all(len(p) <= TELEGRAM_MAX_CHARS for p in parts)
    assert "\n".join(parts) == "\n".join(lines)          # nothing lost, nothing duplicated
    for part in parts:                                    # no line was cut in half
        for line in part.split("\n"):
            assert line in lines


def test_one_absurdly_long_line_is_hard_cut():
    """No line boundary to use, so the only alternative is dropping it."""
    text = "y" * (TELEGRAM_MAX_CHARS * 2 + 10)
    parts = split_reply(text)
    assert all(len(p) <= TELEGRAM_MAX_CHARS for p in parts)
    assert "".join(parts) == text


def test_a_long_line_among_short_ones_loses_nothing():
    """Lines before the monster are untouched; its leftover tail simply carries on into
    the next message alongside what follows it."""
    long = "z" * (TELEGRAM_MAX_CHARS + 5)
    parts = split_reply("\n".join(["before", long, "after"]))

    assert all(len(p) <= TELEGRAM_MAX_CHARS for p in parts)
    assert parts[0] == "before"
    assert "".join(parts[1:]) == f"{long}\nafter"
