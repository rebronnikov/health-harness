"""Type at the bot from a terminal — the same pipeline as Telegram, minus Telegram.

    printf '/today\\n' | python -m health.cli          # no API key needed
    python -m health.cli                              # interactive; one message per line

Slash commands (`/today`, `/yesterday`, `/week`, `/last N`, `/catalog`) are rendered
without a model call and need no key. Anything else is a message for the model and goes
through `harness.handle`, exactly as a Telegram message would.

Six decisions this file encodes, each of which is easy to get wrong:

- **One `reply_handle` for the whole session (`HANDLE`).** Conversation history, food's
  pending estimate and the numbered listing behind "delete 2" are all keyed by it, so a
  fresh handle per line would silently break the propose -> "yes" -> confirm flow —
  which is the single best thing to demonstrate. This is the load-bearing line here.
- **`timestamp` is naive local time**, like the Telegram adapter's: food's evening
  suggestion gate reads `.hour` off it.
- **`HEALTH_TODAY=YYYY-MM-DD` pins the date** the slash commands and the timestamp
  resolve to (the time of day stays real). It is what makes the seeded demo and its CI
  round-trip independent of the clock.
- **`harness` is imported lazily**, inside the free-text branch — it constructs an
  `Anthropic()` client at module scope, and importing it eagerly would make `/today`
  demand a key it never uses.
- **No reply splitting** — a terminal has no 4096-character limit.
- **`init_db()` runs at startup**: `get_conn()` opens the projection but creates no
  schema, and `/today` on an empty data directory is the first thing a stranger types.
"""
from __future__ import annotations

import os
import sys
from datetime import date, datetime
from typing import Iterable, TextIO

from health.adapters import entries_view
from health.core.contracts import NormalizedMessage
from health.store.connection import init_db

SOURCE = "cli"
HANDLE = "cli"          # one conversation per process — see the module docstring
PROMPT = "> "

NO_KEY_REPLY = (
    "ANTHROPIC_API_KEY is not set, so free-text messages can't reach the model. "
    "Slash commands (/today, /yesterday, /week, /last N, /catalog) work without it."
)


def now() -> datetime:
    """Naive local time, with the date replaced by HEALTH_TODAY when that is set."""
    current = datetime.now()
    pinned = os.environ.get("HEALTH_TODAY")
    if pinned:
        current = datetime.combine(date.fromisoformat(pinned), current.time())
    return current


def reply_to(text: str) -> str:
    """One line in, one reply out. Slash commands never touch the model."""
    msg = NormalizedMessage(source=SOURCE, reply_handle=HANDLE, timestamp=now(), text=text)
    if text.startswith("/"):
        return entries_view.render_command(msg)
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return NO_KEY_REPLY
    from health.core import harness          # lazy: constructs the Anthropic client
    return harness.handle(msg)


def run(lines: Iterable[str], out: TextIO = sys.stdout, prompt: str = "") -> None:
    """Drive the bot over an iterable of lines, writing each reply (plus a blank line) to
    `out`. A failure prints and moves on — nothing was logged, exactly as the Telegram
    adapter tells its user — rather than ending the session."""
    init_db()
    out.write(prompt)
    out.flush()
    for line in lines:
        text = line.strip()
        if text:
            try:
                reply = reply_to(text)
            except Exception as e:           # mirror telegram_adapter: never crash the session
                detail = str(e)
                if len(detail) > 250:
                    detail = detail[:250] + "…"
                reply = f"⚠️ Couldn't process that — nothing was logged.\n{type(e).__name__}: {detail}"
            out.write(reply + "\n\n")
        out.write(prompt)
        out.flush()


def main() -> None:
    interactive = sys.stdin.isatty()
    run(sys.stdin, prompt=PROMPT if interactive else "")
    if interactive:
        sys.stdout.write("\n")


if __name__ == "__main__":
    main()
