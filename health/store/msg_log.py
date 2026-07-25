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
