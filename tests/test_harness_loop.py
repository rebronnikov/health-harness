"""harness.handle × the tool-use loop itself — the parts that aren't about any feature.

Three things the loop has to get right once server-side tools are in play: a paused turn
is resumed rather than mistaken for a short answer, blocks the harness doesn't own are
passed through instead of dispatched, and a model that keeps calling tools is cut off
with an honest reply rather than an unbounded spend.
"""
from datetime import timedelta

from health.core import harness
from health.core.contracts import NormalizedMessage
from health.core.registry import all_executors, all_tools

from health.store.connection import get_conn

from conftest import DEFAULT_TS, raw_block, rows, text_block, tool_block

DAY = "2026-07-14"


def _msg(text, handle="chat-1", ts=DEFAULT_TS):
    return NormalizedMessage(source="telegram", reply_handle=handle, timestamp=ts, text=text)


def test_pause_turn_resumes_instead_of_answering(fake_client):
    """A server tool that pauses mid-answer returns stop_reason 'pause_turn' with partial
    content. Re-sending the paused turn resumes it; treating it as final would ship a
    truncated reply (and, here, no reply text at all)."""
    paused = [raw_block("server_tool_use", id="srv-1", name="web_search",
                        input={"query": "bounty bar nutrition"})]
    fake_client.script((paused, "pause_turn"), [text_block("A Bounty bar is ~275 kcal.")])

    reply = harness.handle(_msg("how many calories in a bounty bar?"))

    assert reply == "A Bounty bar is ~275 kcal."
    assert len(fake_client.calls) == 2
    assert fake_client.calls[1]["messages"] == [       # the paused turn, no injected user turn
        {"role": "user", "content": "how many calories in a bounty bar?"},
        {"role": "assistant", "content": paused},
    ]


def test_pause_turn_leaves_only_the_final_text_in_history(fake_client):
    paused = [raw_block("server_tool_use", id="srv-1", name="web_search", input={"query": "x"})]
    fake_client.script((paused, "pause_turn"), [text_block("~275 kcal.")], [text_block("Sure.")])
    harness.handle(_msg("bounty bar?"))
    harness.handle(_msg("thanks", ts=DEFAULT_TS + timedelta(seconds=30)))

    assert fake_client.calls[2]["messages"] == [
        {"role": "user", "content": "bounty bar?"},
        {"role": "assistant", "content": "~275 kcal."},
        {"role": "user", "content": "thanks"},
    ]


def test_server_tool_blocks_are_never_dispatched_locally(fake_client):
    """Server tools run on Anthropic's side, so their blocks arrive already resolved.
    The harness dispatches `tool_use` only — anything else falls through to the model,
    including the in-band error objects a failed search returns."""
    fake_client.script([
        raw_block("server_tool_use", id="srv-1", name="web_search", input={"query": "x"}),
        raw_block("web_search_tool_result", tool_use_id="srv-1",
                  content={"error_code": "max_uses_exceeded"}),
        text_block("Couldn't look that up — I'll estimate instead."),
    ])

    reply = harness.handle(_msg("bounty bar?"))

    assert reply == "Couldn't look that up — I'll estimate instead."
    assert len(fake_client.calls) == 1            # no local tool round-trip was attempted


def test_a_container_backed_turn_carries_its_container_id(fake_client):
    paused = [raw_block("server_tool_use", id="srv-1", name="web_search", input={"query": "x"})]
    fake_client.script(
        (paused, "pause_turn", "cntr-1"),
        [tool_block("get_day_totals", {"date": DAY})],
        [text_block("~188 kcal for 150g.")],
    )

    reply = harness.handle(_msg("150g beef mince, look it up"))

    assert reply == "~188 kcal for 150g."
    # The search's own request can't know the id yet; every request after it must.
    assert "container" not in fake_client.calls[0]
    assert [c["container"] for c in fake_client.calls[1:]] == ["cntr-1", "cntr-1"]


def test_a_turn_without_a_server_tool_sends_no_container(fake_client):
    """The key is absent, not None: a turn that never searched has no container to resume,
    and the API rejects an unknown one."""
    fake_client.script([tool_block("get_day_totals", {"date": DAY})], [text_block("ok")])
    harness.handle(_msg("how am I doing?"))

    assert all("container" not in c for c in fake_client.calls)


def test_server_tools_are_offered_after_the_registry_tools(fake_client):
    """Registry tools keep a stable prefix; server tools are appended. Neither has an
    executor by design — the registry never sees them."""
    fake_client.script([text_block("ok")])
    harness.handle(_msg("hi"))

    tools = fake_client.calls[0]["tools"]
    n = len(harness.SERVER_TOOLS)
    assert tools[-n:] == harness.SERVER_TOOLS
    assert tools[:-n] == all_tools()
    assert {"web_search", "web_fetch"}.isdisjoint(all_executors())


def test_search_is_localised_to_the_uk_without_a_hard_allowlist(fake_client):
    """A UK bias, not a filter: an allowlist would return nothing for a food no UK grocer
    lists, and the grocers most likely to have it are the ones that block bots."""
    by_name = {t["name"]: t for t in harness.SERVER_TOOLS}
    assert by_name["web_search"]["user_location"] == {"type": "approximate", "country": "GB"}
    assert "allowed_domains" not in by_name["web_search"]
    assert "web_fetch" in by_name              # search finds it, fetch reads the real table


def test_loop_cap_returns_an_honest_reply(fake_client):
    """A model that keeps calling tools is cut off at MAX_LOOP_ITERATIONS. The reply says
    so rather than summarising work we can't account for."""
    fake_client.script(*[
        [tool_block("get_day_totals", {"date": DAY}, id=f"tu-{i}")]
        for i in range(harness.MAX_LOOP_ITERATIONS)
    ])

    reply = harness.handle(_msg("how am I doing?"))

    assert reply == harness.LOOP_LIMIT_REPLY
    assert len(fake_client.calls) == harness.MAX_LOOP_ITERATIONS


def test_loop_cap_reply_does_not_enter_history(fake_client):
    """The cut-off turn is a failure, not an exchange — a retry starts clean."""
    fake_client.script(*[
        [tool_block("get_day_totals", {"date": DAY}, id=f"tu-{i}")]
        for i in range(harness.MAX_LOOP_ITERATIONS)
    ])
    harness.handle(_msg("how am I doing?"))
    fake_client.script([text_block("216 kcal so far.")])
    harness.handle(_msg("how am I doing?", ts=DEFAULT_TS + timedelta(seconds=30)))

    assert fake_client.calls[-1]["messages"] == [{"role": "user", "content": "how am I doing?"}]


def test_a_paused_turn_still_dispatches_its_client_tools(fake_client, catalog):
    paused = [raw_block("server_tool_use", id="srv-1", name="web_search",
                        input={"query": "skyr nutrition"}),
              tool_block("log_food", {"date": DAY, "meal": "snack",
                                      "items": [{"name": "skyr", "quantity": 100,
                                                 "unit": "g"}]}, id="tu-1")]
    fake_client.script((paused, "pause_turn"), [text_block("Logged the skyr.")])

    reply = harness.handle(_msg("100g skyr, and check the label"))

    assert reply == "Logged the skyr."
    (row,) = rows(get_conn(), "SELECT * FROM food_log")
    assert row["food_slug"] == "skyr" and row["kcal"] == 51.0
    # The tool_result has to go back, or the resumed request carries an orphaned tool_use.
    assert fake_client.calls[1]["messages"][-1]["content"][0]["type"] == "tool_result"


def test_the_loop_cap_does_not_claim_nothing_was_written():
    """It said "nothing further was executed", which reads as "nothing was executed" — and
    the obvious response, re-sending, double-counts everything that landed in the twelve
    iterations before the cap."""
    assert "may" in harness.LOOP_LIMIT_REPLY and "/today" in harness.LOOP_LIMIT_REPLY
    assert "nothing" not in harness.LOOP_LIMIT_REPLY.lower()
