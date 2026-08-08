"""The conversation transcript — what was said, what ran, what was answered.

Nothing else keeps this. `writer.record()` stores the message text as `raw` on rows it
writes, so a turn that logs nothing leaves no trace at all: a question, a "yes", a
correction that failed a guard, a reply the bot got wrong. `ingest_log.jsonl` holds a
hash of the text, not the text. `history.py` holds the last few turns in memory and drops
them on restart. And `bot.err.log` carries no message content by design.

So this file exists to answer "what actually happened?" after the fact — the only place
the outbound reply and the tool calls behind it are written down.

It is NOT a system of record: `rebuild_db` never reads it, nothing projects from it, and
deleting it loses no health data. That is deliberate. It's a debugging and recall
surface, and keeping it out of the ingest path means a failure to write a transcript line
can never cost a logged meal.

One file per day (`data/messages/2026-07-25.jsonl`), appended live. A day boundary is the
natural place to look ("what did I send on Tuesday?"), it bounds any single file, and
appending as we go means a crash loses at most the turn in flight — buffering a whole day
in memory would lose the day.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime

from health.config import DATA_DIR

log = logging.getLogger(__name__)

MESSAGES_DIR = "messages"

# Independent of writer's lock: a transcript append must never wait on, or be waited on
# by, an ingest write.
_lock = threading.Lock()


def _path(when: datetime):
    return DATA_DIR / MESSAGES_DIR / f"{when.date().isoformat()}.jsonl"


def append(kind: str, when: datetime, source: str, chat, **fields) -> None:
    """Append one transcript line. Never raises.

    Swallowing errors is the right trade here and only here: this is an observability
    side-channel, and a full disk or a bad path must not turn a successfully logged meal
    into a user-visible failure. The failure is logged instead, which is where someone
    looking for a missing transcript would go.
    """
    line = {
        "at": when.isoformat(),
        "kind": kind,
        "source": source,
        "chat": str(chat),
        **fields,
    }
    try:
        with _lock:
            path = _path(when)
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(line, ensure_ascii=False, sort_keys=True, default=str) + "\n")
                f.flush()
    except Exception:                       # noqa: BLE001 — see the docstring
        log.exception("failed to append to the message transcript")


def inbound(msg) -> None:
    append("in", msg.timestamp, msg.source, msg.reply_handle, text=msg.text or "")


def outbound(msg, text: str) -> None:
    append("out", msg.timestamp, msg.source, msg.reply_handle, text=text)


def tool_call(msg, name: str, args: dict, result=None, error: str | None = None) -> None:
    """One executor call. `result` is what went back to the model; on failure `error`
    carries the message the model saw instead — the pair is what makes "why did it do
    that?" answerable a week later."""
    append("tool", msg.timestamp, msg.source, msg.reply_handle,
           tool=name, args=args, result=result, error=error)


def usage(msg, **fields) -> None:
    """One model call's token counts — the only cost signal the bot has.

    A distinct kind from `tool`/`server_tool` because it describes the *call*, not the
    conversation: nothing here was said, dispatched or answered. One line per API call, so
    a turn that took two round-trips writes two — which is what makes "what does a message
    cost?" answerable at all. `health/report_usage.py` is the read side.

    It lives in the transcript rather than a file of its own because this module already
    owns the per-day path, the lock, and the never-raise contract — and because
    `tests/conftest.py` redirects `DATA_DIR` per writer module: a second writer would have
    to be added there and to `test_isolation_guardrail.py`, or it could escape the sandbox
    and write to the owner's real `data/`.
    """
    append("usage", msg.timestamp, msg.source, msg.reply_handle, **fields)


def server_tool(msg, name: str, **fields) -> None:
    """One web_search / web_fetch call or its outcome — a *summary*, not the payload.

    A distinct kind from `tool`: these ran on Anthropic's side, the harness never
    dispatched them, and calling them the same thing would have the transcript claim the
    bot executed code it didn't. The call and its result are separate lines paired by
    `id`, because a result can arrive in a later response than the call when a paused
    turn resumes — pairing them here would mean carrying state across loop iterations to
    no benefit in a file that is read top to bottom anyway.
    """
    append("server_tool", msg.timestamp, msg.source, msg.reply_handle,
           tool=name, **fields)
