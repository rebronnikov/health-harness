"""The deterministic slash commands, rendered end to end with no model call.

`/today`, `/yesterday`, `/week`, `/last N` and `/catalog` are reads over the projection
plus a fixed rendering. They lived inside the Telegram adapter, which meant that
importing them pulled in `python-telegram-bot` — so a terminal, a test or a CI job
could not show what a day looks like without a bot token in scope. Nothing here knows
which channel asked: the Telegram adapter and `health.cli` both call `render_command`
and do only their own channel's I/O around it.

The numbered listing is what makes a follow-up like "delete 2" resolve: `recent_entries`
remembers the numbering per chat for `LISTING_EXPIRY`, so the command has to run under
the same bound message context the harness gives an ordinary turn.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from health.core.contracts import NormalizedMessage, current_message
from health.features import corrections, food
from health.store import msg_log

COMMANDS = ("today", "yesterday", "week", "last", "catalog")


def parse_command(text: str | None) -> tuple[str, list[str]]:
    """`"/last 3"` -> `("last", ["3"])`. Tolerates the `@botname` suffix Telegram appends in
    groups and an empty text (which reads as `/today`)."""
    words = (text or "").split()
    if not words:
        return "today", []
    return words[0].lstrip("/").split("@")[0].lower(), words[1:]


def _entries_window(command: str, args: list[str], today: datetime) -> tuple[dict, str, str | None]:
    """Translate a slash command into recent_entries arguments, a heading, and whether a
    progress block belongs under the listing ("day", or none).

    Deterministic on purpose: /today should cost nothing and never depend on the model
    reading a date correctly. The natural-language route to the same data is the
    recent_entries tool, which Claude calls itself.

    Only the single-day commands get a block. /last N is N arbitrary entries rather than a
    date window, and /week spans days a single day's totals can't describe.
    """
    if command == "yesterday":
        day = (today.date() - timedelta(days=1)).isoformat()
        return {"days": 1, "end_date": day, "limit": 50}, f"Yesterday ({day})", "day"
    if command == "week":
        return {"days": 7, "limit": 50}, "Last 7 days", None
    if command == "last":
        n = 5
        if args and args[0].lstrip("-").isdigit():
            n = max(1, min(int(args[0]), 50))
        return {"days": 30, "limit": n}, f"Last {n} entries", None
    return {"days": 1, "limit": 50}, f"Today ({today.date().isoformat()})", "day"


def _progress_block(kind: str | None, listing: dict) -> str | None:
    """The standard totals block for the day the listing actually covered.

    The date comes off the listing rather than being recomputed here, so the block can
    never describe a different day from the rows printed above it.
    """
    if kind == "day":
        return food.get_day_totals(listing["end_date"])["progress_block"]
    return None


def _list_and_total(kwargs: dict, kind: str | None) -> tuple[dict, str | None]:
    # Both reads under the one bound message context: recent_entries takes "today" and
    # the per-chat numbering from it, and the block's evening gate reads its clock.
    listing = corrections.recent_entries(**kwargs)
    return listing, _progress_block(kind, listing)


def render_entries(heading: str, listing: dict, progress: str | None = None) -> str:
    """The numbered list the user reads and then points at ("delete 2"), with the standard
    progress block under it. Nothing logged means no block — a row of zeros against the
    targets says less than "Nothing logged." already does."""
    entries = listing["entries"]
    if not entries:
        return f"{heading}\n\nNothing logged."
    lines = [heading, ""]
    lines += [f"{e['n']}. {e['summary']}" for e in entries]
    if listing["truncated"]:
        lines.append(f"\n(showing {len(entries)} of {listing['total_in_range']})")
    if progress:
        lines.append(f"\n{progress}")
    lines.append("\nSay e.g. “delete 2” or “3 was 40g” to fix one.")
    return "\n".join(lines)


def render_command(msg: NormalizedMessage) -> str:
    """One slash command, end to end: transcript in, context bound, the reads, the rendered
    reply, transcript out — the same shape as `harness.handle`, minus the model.

    Raises on failure rather than phrasing one: how a failure is shown is the adapter's
    business (Telegram sends a warning message, a terminal prints it), and the adapter
    also records that reply in the transcript, so nothing is lost by raising here.
    """
    command, args = parse_command(msg.text)
    msg_log.inbound(msg)
    token = current_message.set(msg)         # runtime clock + per-chat numbering
    try:
        if command == "catalog":
            rows = food.list_catalog()
            reply = food.render_catalog_table(rows) if rows else "Your food catalog is empty."
        elif command in COMMANDS:
            kwargs, heading, kind = _entries_window(command, args, msg.timestamp)
            listing, progress = _list_and_total(kwargs, kind)
            reply = render_entries(heading, listing, progress)
        else:
            reply = (f"Unknown command /{command}. Commands: "
                     + ", ".join(f"/{c}" for c in COMMANDS) + ".")
    finally:
        current_message.reset(token)
    msg_log.outbound(msg, reply)
    return reply
