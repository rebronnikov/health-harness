"""The conversation transcript: the only place the outbound reply and the tool calls
behind it are written down. Not a system of record — rebuild never reads it — so it is
also the one writer allowed to fail quietly.
"""
import json

from health.core import harness
from health.core.contracts import NormalizedMessage
from health.features import food
from health.store import msg_log

from conftest import DEFAULT_TS, raw_block, text_block, tool_block, usage_block

DAY = "2026-07-14"


def _msg(text):
    return NormalizedMessage(source="telegram", reply_handle="chat-1",
                             timestamp=DEFAULT_TS, text=text)


def _transcript(tmp_path, day=DEFAULT_TS):
    path = tmp_path / msg_log.MESSAGES_DIR / f"{day.date().isoformat()}.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_a_logging_turn_records_message_tool_and_reply(ctx, catalog, fake_client, tmp_path):
    fake_client.script(
        [tool_block("log_food", {"date": DAY, "meal": "lunch",
                                 "items": [{"name": "banana", "quantity": 1, "unit": "each"}]})],
        [text_block("Logged 1 banana — 108 kcal.")],
    )
    harness.handle(_msg("a banana"))

    kinds = [e["kind"] for e in _transcript(tmp_path)]
    # One `usage` per API call, so the shape also reads as the turn's cost: message in,
    # one call, one tool, a second call, reply out.
    assert kinds == ["in", "usage", "tool", "usage", "out"]
    entries = [e for e in _transcript(tmp_path) if e["kind"] != "usage"]
    assert entries[0]["text"] == "a banana"
    assert entries[1]["tool"] == "log_food"
    assert entries[1]["args"]["items"][0]["name"] == "banana"
    assert entries[1]["result"]["logged"][0]["kcal"] == 108
    assert entries[2]["text"].startswith("Logged 1 banana")


def test_a_turn_that_logs_nothing_is_still_recorded(ctx, catalog, fake_client, tmp_path):
    """The gap this closes: a question, a 'yes', a reply that got it wrong — none of it
    touched a domain row before, so none of it existed anywhere afterwards."""
    fake_client.script([text_block("You're at 1,200 kcal today.")])
    harness.handle(_msg("how am I doing?"))

    entries = _transcript(tmp_path)
    assert [e["kind"] for e in entries] == ["in", "usage", "out"]
    assert entries[2]["text"] == "You're at 1,200 kcal today."


def test_a_failing_tool_records_what_the_model_saw(ctx, catalog, fake_client, tmp_path):
    fake_client.script(
        [tool_block("set_meal", {"date": DAY, "meal": "breakfast"})],   # nothing unlabelled
        [text_block("Nothing was waiting for a label.")],
    )
    harness.handle(_msg("that was breakfast"))

    (tool,) = [e for e in _transcript(tmp_path) if e["kind"] == "tool"]
    assert tool["result"] is None
    assert "ValueError" in tool["error"] and "waiting for a meal label" in tool["error"]


def test_the_reply_is_recorded_even_when_the_model_returns_nothing(ctx, fake_client, tmp_path):
    fake_client.script(([], "refusal"))
    reply = harness.handle(_msg("something refused"))
    out = [e for e in _transcript(tmp_path) if e["kind"] == "out"]
    assert out and out[0]["text"] == reply == harness.FAILSAFE_REPLY


def _search_result(title, url):
    """One hit as the API returns it — note `encrypted_content`, which must never reach
    the transcript."""
    return raw_block("web_search_result", title=title, url=url,
                     page_age="2026-07-01", encrypted_content="BLOB" * 500)


def test_a_web_lookup_records_the_query_and_the_pages(ctx, fake_client, tmp_path):
    """Server tools have no executor, so nothing in the dispatcher sees them — a turn that
    searched the web used to read as a turn that did nothing. That gap is how the
    container_id bug stayed invisible until it 400'd."""
    fake_client.script(
        [raw_block("server_tool_use", id="srv-1", name="web_search",
                   input={"query": "pret iced cappuccino nutrition"}),
         raw_block("web_search_tool_result", tool_use_id="srv-1",
                   content=[_search_result("Pret — Menu", "https://pret.com/menu")]),
         text_block("Per the Pret site: 140 kcal.")],
    )
    harness.handle(_msg("iced cappuccino pret"))

    entries = _transcript(tmp_path)
    assert [e["kind"] for e in entries] == ["in", "usage", "server_tool", "server_tool", "out"]
    call, result = entries[2], entries[3]
    assert call["tool"] == "web_search"
    assert call["input"]["query"] == "pret iced cappuccino nutrition"
    assert result["tool"] == "web_search_tool_result"
    assert result["id"] == call["id"] == "srv-1"        # the pair is joined by tool_use_id
    assert result["result"] == [{"title": "Pret — Menu",
                                 "url": "https://pret.com/menu"}]


def test_a_lookup_records_what_it_read_not_what_it_downloaded(ctx, fake_client, tmp_path):
    """A search hit carries `encrypted_content` and a fetch carries the whole retrieved
    document. Writing either verbatim would put multi-KB blobs into a file appended on
    every turn — so the transcript keeps the URL, never the payload."""
    fake_client.script(
        [raw_block("server_tool_use", id="srv-1", name="web_search", input={"query": "x"}),
         raw_block("web_search_tool_result", tool_use_id="srv-1",
                   content=[_search_result("A page", "https://example.com/a")]),
         raw_block("server_tool_use", id="srv-2", name="web_fetch",
                   input={"url": "https://example.com/a"}),
         raw_block("web_fetch_tool_result", tool_use_id="srv-2",
                   content=raw_block("web_fetch_result", url="https://example.com/a",
                                     retrieved_at="2026-07-14T09:00:00Z",
                                     content="THE WHOLE PAGE " * 500)),
         text_block("ok")],
    )
    harness.handle(_msg("look it up"))

    (fetch,) = [e for e in _transcript(tmp_path) if e.get("tool") == "web_fetch_tool_result"]
    assert fetch["result"] == {"url": "https://example.com/a",
                               "retrieved_at": "2026-07-14T09:00:00Z"}
    raw = (tmp_path / msg_log.MESSAGES_DIR / f"{DEFAULT_TS.date().isoformat()}.jsonl").read_text()
    assert "BLOB" not in raw and "THE WHOLE PAGE" not in raw


def test_a_failed_lookup_records_its_error_code(ctx, fake_client, tmp_path):
    """Server-tool failures are in-band — HTTP 200 with an error object where the results
    would be. The transcript is the only place that survives the turn."""
    fake_client.script(
        [raw_block("server_tool_use", id="srv-1", name="web_search", input={"query": "x"}),
         raw_block("web_search_tool_result", tool_use_id="srv-1",
                   content={"error_code": "max_uses_exceeded"}),
         text_block("Couldn't look that up.")],
    )
    harness.handle(_msg("look it up"))

    (result,) = [e for e in _transcript(tmp_path) if e.get("tool", "").endswith("_tool_result")]
    assert result["result"] == {"error_code": "max_uses_exceeded"}


def test_a_long_result_list_is_capped(ctx, fake_client, tmp_path):
    hits = [_search_result(f"hit {i}", f"https://example.com/{i}") for i in range(25)]
    fake_client.script(
        [raw_block("server_tool_use", id="srv-1", name="web_search", input={"query": "x"}),
         raw_block("web_search_tool_result", tool_use_id="srv-1", content=hits),
         text_block("ok")],
    )
    harness.handle(_msg("look it up"))

    (result,) = [e for e in _transcript(tmp_path) if e.get("tool", "").endswith("_tool_result")]
    assert len(result["result"]) == harness.MAX_SERVER_RESULTS


def test_a_client_tool_is_not_recorded_twice(ctx, catalog, fake_client, tmp_path):
    """`tool_use` is dispatched here and logged by the dispatcher; only server blocks go
    through the transcriber."""
    fake_client.script(
        [tool_block("get_day_totals", {"date": DAY})],
        [text_block("ok")],
    )
    harness.handle(_msg("how am I doing?"))

    assert [e["kind"] for e in _transcript(tmp_path)] == ["in", "usage", "tool", "usage", "out"]


def test_files_are_per_day(ctx, catalog, fake_client, tmp_path):
    from datetime import timedelta

    fake_client.script([text_block("ok")], [text_block("ok")])
    harness.handle(_msg("monday"))
    tomorrow = DEFAULT_TS + timedelta(days=1)
    harness.handle(NormalizedMessage(source="telegram", reply_handle="chat-1",
                                     timestamp=tomorrow, text="tuesday"))

    assert [e["text"] for e in _transcript(tmp_path) if e["kind"] == "in"] == ["monday"]
    assert [e["text"] for e in _transcript(tmp_path, tomorrow) if e["kind"] == "in"] == ["tuesday"]


def test_a_broken_transcript_never_breaks_a_log(ctx, catalog, monkeypatch):
    """It is an observability side-channel: a full disk must not turn a logged meal into
    a user-visible failure."""
    monkeypatch.setattr(msg_log, "_path", lambda when: (_ for _ in ()).throw(OSError("no disk")))
    msg_log.inbound(_msg("40g skyr"))                      # must not raise

    ctx()
    out = food.log_food(DAY, "breakfast", [{"name": "skyr", "quantity": 40, "unit": "g"}])
    assert out["logged"][0]["amount"] == 40


def test_every_model_call_records_its_token_usage(ctx, catalog, fake_client, tmp_path):
    """`response.usage` used to be discarded, which made prompt caching unverifiable — a
    cache hit is only ever visible as `cache_read_input_tokens` — and left "is this getting
    cheaper?" with no answer but the credit balance."""
    fake_client.script(
        ([tool_block("get_day_totals", {"date": DAY})], None, None,
         usage_block(input_tokens=900, output_tokens=1200, cache_creation_input_tokens=7700)),
        ([text_block("ok")], None, None,
         usage_block(input_tokens=950, output_tokens=300, cache_read_input_tokens=7700)),
    )
    harness.handle(_msg("how am I doing?"))

    rows = [e for e in _transcript(tmp_path) if e["kind"] == "usage"]
    assert len(rows) == 2                                  # one per API call, not per turn
    assert (rows[0]["cache_write_tokens"], rows[0]["cache_read_tokens"]) == (7700, 0)
    assert (rows[1]["cache_write_tokens"], rows[1]["cache_read_tokens"]) == (0, 7700)
    assert rows[0]["input_tokens"] == 900 and rows[0]["output_tokens"] == 1200
    assert rows[0]["model"] == harness.MODEL and rows[0]["effort"] == harness.EFFORT


def test_a_paused_turn_records_the_call_it_cost(ctx, fake_client, tmp_path):
    """The pause_turn branch `continue`s before every other record in the loop. A call that
    costs money and shows nothing in the cost report is the anomaly you would never find."""
    paused = [raw_block("server_tool_use", id="srv-1", name="web_search", input={"query": "x"})]
    fake_client.script((paused, "pause_turn"), [text_block("~275 kcal.")])
    harness.handle(_msg("bounty bar?"))

    assert len([e for e in _transcript(tmp_path) if e["kind"] == "usage"]) == 2


def test_a_refused_turn_records_the_call_it_cost(ctx, fake_client, tmp_path):
    """Same gap on the other early exit: a refusal returns before the normal path."""
    fake_client.script(([], "refusal"))
    harness.handle(_msg("something refused"))

    assert len([e for e in _transcript(tmp_path) if e["kind"] == "usage"]) == 1


def test_a_response_without_usage_is_not_fatal(ctx, fake_client, tmp_path):
    """Telemetry is an observability side-channel; an unfamiliar response shape must never
    cost a logged meal."""
    fake_client.script(([text_block("ok")], None, None, None))
    assert harness.handle(_msg("hi")) == "ok"
    assert [e for e in _transcript(tmp_path) if e["kind"] == "usage"] == []


def test_the_transcript_is_not_a_system_of_record():
    """rebuild_db replays registered domains; `messages` is not one, and nothing projects
    from it. Deleting the transcript loses no health data."""
    from health.core import registry

    assert "messages" not in {f.domain for f in registry.ingesting_features()}
