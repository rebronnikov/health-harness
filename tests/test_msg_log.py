"""The conversation transcript: the only place the outbound reply and the tool calls
behind it are written down. Not a system of record — rebuild never reads it — so it is
also the one writer allowed to fail quietly.
"""
import json

from health.core import harness
from health.core.contracts import NormalizedMessage
from health.features import food
from health.store import msg_log

from conftest import DEFAULT_TS, text_block, tool_block

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
    assert kinds == ["in", "tool", "out"]
    entries = _transcript(tmp_path)
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
    assert [e["kind"] for e in entries] == ["in", "out"]
    assert entries[1]["text"] == "You're at 1,200 kcal today."


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


def test_the_transcript_is_not_a_system_of_record():
    """rebuild_db replays registered domains; `messages` is not one, and nothing projects
    from it. Deleting the transcript loses no health data."""
    from health.core import registry

    assert "messages" not in {f.domain for f in registry.ingesting_features()}
