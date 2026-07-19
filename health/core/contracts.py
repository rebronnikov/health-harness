"""The interfaces every layer plugs into. Imported by both the core and every
feature — keep it dependency-free (no imports from store/adapters/features) so
there are no cycles.
"""
from __future__ import annotations

import sqlite3
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Iterator, Protocol


@dataclass
class NormalizedMessage:
    """Channel-agnostic input. Adapters translate raw I/O into this; nothing
    downstream of an adapter knows which channel it came from."""

    source: str                              # "telegram" | "cli" | ...
    reply_handle: Any                        # opaque token the adapter replies with
    timestamp: datetime
    text: str | None = None
    attachment: bytes | None = None          # future: HR/CSV exports, images
    attachment_type: str | None = None


class InputAdapter(Protocol):
    def listen(self) -> Iterator[NormalizedMessage]: ...
    def reply(self, msg: NormalizedMessage, text: str) -> None: ...


@dataclass
class Feature:
    """Everything a capability declares. Adding a feature means writing one of
    these and adding it to registry.FEATURES — nothing else in the system changes.

    The `domain`/`dedupe_key`/`project` trio is what lets the JSONL system-of-record
    be projected into SQLite and rebuilt from scratch. `project` is the single
    idempotent upsert used by BOTH live ingest and rebuild_db — one function, two
    callers — which is what guarantees a rebuild reproduces live-ingest state.
    """

    name: str
    tools: list[dict]                        # Anthropic tool schemas
    executors: dict[str, Callable]           # tool_name -> function
    schema: str | None = None                # DDL fragment, applied idempotently
    domain: str | None = None                # data/<domain>.jsonl + table basis
    dedupe_key: tuple[str, ...] = ()          # column names forming the dedupe key
    project: Callable[[sqlite3.Connection, dict], None] | None = None
                                             # idempotent upsert of one canonical row
    system_context: Callable[[Any], str | None] | None = None
                                             # optional per-message system-prompt fragment, given
                                             # the in-flight NormalizedMessage. Lets a feature
                                             # surface *structured* cross-turn state (e.g. a food
                                             # estimate awaiting the user's "yes") precisely,
                                             # rather than hoping it survives in the harness's
                                             # bounded chat transcript (core/history.py).

    def dedupe_value(self, row: dict) -> tuple:
        return tuple(row[k] for k in self.dedupe_key)


# Set by harness.handle() for the duration of one message's tool loop, so that
# "dumb" executors can read runtime-authored provenance (raw text, source, time)
# instead of trusting Claude to echo it. Reset in a finally so it never leaks to
# the next message.
current_message: ContextVar[NormalizedMessage] = ContextVar("current_message")


def get_context() -> NormalizedMessage:
    """Return the NormalizedMessage for the in-flight turn.

    Raises LookupError if called outside a harness turn — a loud bug, not a
    silent default (provenance must never be fabricated).
    """
    return current_message.get()
