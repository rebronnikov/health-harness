"""Bounded, in-process conversation history — one short transcript per chat.

The harness used to start every turn from a bare `[{"role": "user", ...}]`, which made
any cross-turn exchange impossible: the bot would ask "how much yoghurt?" and the answer
landed on a turn with no memory of the question. A bare "40g" then got matched against
whatever the catalog happened to hold. Features route around this with `system_context`
fragments (food's pending estimate), but that only carries state a feature knows it owns —
it can't carry the conversation itself.

This is deliberately the weakest thing that fixes that:

- **Ephemeral.** A module-level dict, exactly like food's `_pending`. Never written to
  JSONL or SQLite, never read by an executor, lost on restart. It feeds the *model's*
  context only, so the provenance invariant is untouched: `writer.record` still stamps
  raw/source/ingested_at from the live message, never from anything here.
- **Bounded twice.** `MAX_TURNS` caps context cost; `IDLE_EXPIRY` stops an hour-old thread
  from silently steering a fresh message — the subtler version of the original bug.
- **Text only.** We store the user's text and the final assistant reply, never the
  `tool_use`/`tool_result` blocks from the loop. Replaying those would risk an orphaned
  tool_use (an API error) and re-feed stale tool results. A flat transcript is all the
  model needs to resolve "1. Log 40g".
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

MAX_TURNS = 10                          # user+assistant pairs kept per chat
# Silence longer than this starts a fresh thread. 90 rather than 30 minutes because the
# bot may now ask a question: an answer that lands after a meal, a commute, or a meeting
# has to still find the question it answers. The staleness risk it guards against is a
# bare "40g" steering off an hours-old thread, and an hour and a half is still well
# inside the window where a reply plausibly belongs to what came before.
IDLE_EXPIRY = timedelta(minutes=90)

# reply_handle -> {"last": datetime, "messages": [ {role, content}, ... ]}
_history: dict[Any, dict] = {}


def load(msg) -> list[dict]:
    """Prior turns for this chat, oldest first — empty if the thread is new or stale."""
    entry = _history.get(msg.reply_handle)
    if entry is None:
        return []
    if msg.timestamp - entry["last"] > IDLE_EXPIRY:
        del _history[msg.reply_handle]   # stale thread: drop it rather than let it mislead
        return []
    return list(entry["messages"])


def _last(msg, role: str) -> str | None:
    """The most recent message of `role` in this chat's recalled thread.

    A read-only view over `load()`, so `IDLE_EXPIRY` applies identically and there is no
    second notion of freshness to keep in step. The current turn is not in here yet — it is
    saved only once a reply exists — so these read the turn BEFORE the one in flight.
    """
    for m in reversed(load(msg)):
        if m["role"] == role:
            return m["content"]
    return None


def last_reply(msg) -> str | None:
    """What the bot last said to this chat, or None if the thread is new or stale.

    It exists so a feature can verify at RUNTIME that the user was actually shown something
    before acting on their agreement to it — `food.confirm_pending` uses it to tell a model
    catching up on an estimate the user has already seen from a model confirming numbers it
    invented this turn. Reading the conversation is the only way to know that, and reading
    it here keeps the answer runtime-authored rather than something the model asserts about
    itself.
    """
    return _last(msg, "assistant")


def last_user_message(msg) -> str | None:
    """What the user last said before the message in flight.

    Used for provenance in exactly one place: when an estimate is staged on the very
    message that confirms it, the food was described in the turn before, so that is the
    text `raw` should carry — not "Yes please".
    """
    return _last(msg, "user")


def save(msg, reply: str) -> None:
    """Append this turn (the user's text + the assistant's final reply) to the chat's
    transcript, trimming to the most recent MAX_TURNS pairs."""
    if not (msg.text and reply):
        return                           # nothing useful to recall
    entry = _history.setdefault(msg.reply_handle, {"last": msg.timestamp, "messages": []})
    entry["messages"] += [
        {"role": "user", "content": msg.text},
        {"role": "assistant", "content": reply},
    ]
    del entry["messages"][: -2 * MAX_TURNS]
    entry["last"] = msg.timestamp


def clear() -> None:
    """Drop every chat's history. For tests; the bot only ever clears by restarting."""
    _history.clear()
