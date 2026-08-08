"""The prompt cache breakpoint — where it sits, and what must never cross it.

Prompt caching is a prefix match on exact bytes, and it fails SILENTLY: a per-request value
in the cached block doesn't error, doesn't warn, and doesn't fail a request — it just makes
every call a fresh write at 1.25x instead of a read at 0.1x. There is no signal short of
`report_usage`'s hit% column. So the placement is pinned here, structurally, rather than
left to whoever next edits the prompt.
"""
import json
from datetime import datetime, timedelta, timezone

from health.core import harness, prompts
from health.core.contracts import NormalizedMessage
from health.core.registry import all_tools

from conftest import DEFAULT_TS, system_text, text_block


def _msg(text, handle="chat-1", ts=DEFAULT_TS):
    return NormalizedMessage(source="telegram", reply_handle=handle,
                             timestamp=ts, text=text)


def test_the_breakpoint_is_on_the_static_block_and_nowhere_else(fake_client):
    """The whole saving is this one dict in this one position.

    Asserting the exact shape catches the two ways it goes wrong: a third block appearing
    before the cached one, and a "simplification" to the top-level `cache_control=`
    parameter — which auto-places on the LAST cacheable block, i.e. the volatile one, so
    every request writes a fresh entry and reads none while still looking like it worked.
    """
    fake_client.script([text_block("ok")])
    harness.handle(_msg("hi"))

    system = fake_client.calls[0]["system"]
    assert [b.get("cache_control") for b in system] == [{"type": "ephemeral"}, None]
    assert system[0]["text"] == prompts.STATIC_PROMPT
    assert "cache_control" not in fake_client.calls[0]        # not the top-level parameter


def test_no_volatile_value_reaches_the_cached_block(fake_client):
    """The clock is why the prefix was uncacheable in the first place — it used to be the
    first 120 characters of the system prompt, ahead of 8,400 characters of static text."""
    ts = datetime(2026, 7, 14, 9, 5, tzinfo=timezone.utc)
    fake_client.script([text_block("ok")])
    harness.handle(_msg("hi", ts=ts))

    cached, volatile = (b["text"] for b in fake_client.calls[0]["system"])
    assert "2026-07-14" not in cached and "09:05" not in cached
    assert "2026-07-14" in volatile and "09:05" in volatile


def test_the_cached_block_is_identical_across_turns_chats_and_state(ctx, catalog, fake_client):
    """Same bytes, same cache key. A saved food, a different chat and a later timestamp
    must all land in the volatile block — if any of them reached the cached one, the entry
    would be per-chat or per-turn and would essentially never be read."""
    fake_client.script([text_block("ok")], [text_block("ok")])
    harness.handle(_msg("hi"))
    harness.handle(_msg("hello", handle="chat-2", ts=DEFAULT_TS + timedelta(minutes=7)))

    first, second = (c["system"][0]["text"] for c in fake_client.calls)
    assert first == second == prompts.STATIC_PROMPT


def test_the_two_calls_of_one_turn_present_the_same_prefix(ctx, catalog, fake_client):
    """`system` is built once, before the loop. Rebuilding it per iteration would mean the
    second call of every turn wrote its own entry instead of reading the first's — the
    single cheapest hit available, since it lands milliseconds later."""
    from conftest import tool_block

    fake_client.script(
        [tool_block("get_day_totals", {"date": "2026-07-14"})],
        [text_block("ok")],
    )
    harness.handle(_msg("how am I doing?"))

    assert len(fake_client.calls) == 2
    assert fake_client.calls[0]["system"] == fake_client.calls[1]["system"]


def test_caching_costs_the_model_no_context(ctx, catalog, fake_client):
    """The rearrangement must be invisible to the model: every fragment that reached it
    before still reaches it, just after the breakpoint instead of before."""
    fake_client.script([text_block("ok")])
    harness.handle(_msg("hi"))

    volatile = fake_client.calls[0]["system"][1]["text"]
    assert "SAVED FOODS" in volatile            # the catalog fragment
    assert "skyr" in volatile                   # ...with its rows
    assert "Today is" in volatile               # and the clock


def test_every_load_bearing_rule_still_reaches_the_model(fake_client):
    """Assembly-agnostic, unlike test_prompts.py: this reads the bytes that actually leave
    the process, so a future re-split can't drop a rule silently."""
    fake_client.script([text_block("ok")])
    harness.handle(_msg("hi"))

    sent = system_text(fake_client.calls[0])
    for rule in ("never do arithmetic",                     # the chassis invariant
                 "at most ONE short question",              # the 2026-07-15 salad loop
                 "THE STEER",                               # the 2026-07-26 silent replies
                 "EXACTLY as given",                        # the progress block
                 "explicit zeros for all five macros",      # ladder step 1
                 "never re-check them against the web",     # ladder step 2
                 "that producer's OWN site first",          # ladder step 3
                 "waitrose.com first",                      # ladder step 4
                 "never infer it from the clock"):          # the meal rule
        assert rule in sent, f"missing from the sent prompt: {rule!r}"


def test_the_cached_prefix_clears_the_model_minimum():
    """Below the model's minimum a breakpoint silently does nothing — no error, just
    `cache_creation_input_tokens: 0`. The minimum is 1024 tokens on Sonnet 5 (512 on Opus
    5), so a prompt or tool-schema trim shouldn't be able to cross it unnoticed."""
    prefix_chars = (len(json.dumps(all_tools() + harness.SERVER_TOOLS))
                    + len(prompts.STATIC_PROMPT))
    # ~3.5 chars/token is conservative for JSON-heavy text, which tokenises denser.
    assert prefix_chars > 4 * 1024
