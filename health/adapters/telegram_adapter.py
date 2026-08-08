"""The only channel-specific code. Translates Telegram updates to/from
NormalizedMessage and enforces the security boundary.

A bot is publicly reachable by anyone who finds its handle, so the allowlist is
the one thing standing between the owner's health data and a stranger: every message
whose chat id is not ALLOWED_CHAT_ID is dropped silently (no reply — replying
would confirm the bot exists). PTB v22 is async; harness.handle makes a blocking
Anthropic call, so it runs in a worker thread to keep the event loop responsive.
"""
from __future__ import annotations

import asyncio
import html
import logging
from datetime import datetime, timedelta

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from health.config import ALLOWED_CHAT_ID, TELEGRAM_BOT_TOKEN
from health.core import harness
from health.core.contracts import NormalizedMessage, current_message
from health.features import corrections, food
from health.store import msg_log

log = logging.getLogger(__name__)

TELEGRAM_MAX_CHARS = 4096  # hard limit on a single sendMessage
_CATALOG_CHUNK = 3900      # split the table below this, leaving room for <pre> tags


def build_app() -> Application:
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("catalog", _on_catalog))
    for command in ("today", "yesterday", "week", "last"):
        app.add_handler(CommandHandler(command, _on_entries))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _on_message))
    return app


async def _on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat is None or chat.id != ALLOWED_CHAT_ID:
        return                                   # silent drop — the security boundary
    message = update.effective_message
    if message is None or not message.text:
        return

    msg = NormalizedMessage(
        source="telegram",
        reply_handle=chat.id,
        timestamp=datetime.now(),                # ingest time, not Telegram's message date
        text=message.text,
    )
    # harness.handle is sync (blocking Anthropic call); run it off the event loop.
    try:
        reply = await asyncio.to_thread(harness.handle, msg)
    except Exception as e:
        # Never leave the user hanging on a pipeline failure (billing, rate limit,
        # network). Nothing was written, so resending after the cause is fixed is
        # safe. Only the owner's chat reaches here, so showing the raw reason is fine.
        log.exception("harness.handle failed")
        detail = str(e)
        if len(detail) > 250:
            detail = detail[:250] + "…"
        reply = f"⚠️ Couldn't process that — nothing was logged.\n{type(e).__name__}: {detail}"
        msg_log.outbound(msg, reply)         # the harness never got to record this one
    for part in split_reply(reply):
        await ctx.bot.send_message(chat_id=chat.id, text=part)


def split_reply(text: str, limit: int = TELEGRAM_MAX_CHARS) -> list[str]:
    """Split a reply into messages Telegram will accept, preferring line boundaries.

    sendMessage rejects anything over 4096 characters, so a long reply — a multi-day
    summary, say — would fail outright and the user would get nothing. Splitting on
    newlines keeps a per-day list readable; a single line longer than the limit is cut
    hard, because there is nothing better to do with it.
    """
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:                 # one absurdly long line: hard-cut it
            if current:
                parts.append(current)
                current = ""
            parts.append(line[:limit])
            line = line[limit:]
        if current and len(current) + 1 + len(line) > limit:
            parts.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        parts.append(current)
    return parts


def _catalog_messages(rows: list[dict]) -> list[str]:
    """Render the catalog as one or more Telegram-HTML <pre> blocks. Escapes the table
    text (only & < > matter in HTML) and, defensively, splits on row boundaries so no
    single message exceeds Telegram's 4096-char limit — a no-op at today's ~24 items."""
    escaped = html.escape(food.render_catalog_table(rows), quote=False)
    chunks: list[list[str]] = [[]]
    length = 0
    for line in escaped.split("\n"):
        if chunks[-1] and length + len(line) + 1 > _CATALOG_CHUNK:
            chunks.append([])
            length = 0
        chunks[-1].append(line)
        length += len(line) + 1
    return [f"<pre>{chr(10).join(c)}</pre>" for c in chunks]


def _command_message(chat_id, text: str) -> NormalizedMessage:
    """A NormalizedMessage for a slash command, so commands share the same runtime context
    and transcript as ordinary messages. Commands never write health data, so this is
    provenance for reads and recall only."""
    return NormalizedMessage(source="telegram", reply_handle=chat_id,
                             timestamp=datetime.now(), text=text)


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
    # Both reads in the one worker thread: they share the bound message context, and
    # neither should run on the event loop.
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


async def _on_entries(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat is None or chat.id != ALLOWED_CHAT_ID:
        return                                   # silent drop — the security boundary
    text = update.effective_message.text or "/today"
    command = text.split()[0].lstrip("/").split("@")[0]
    # recent_entries reads the clock and remembers the numbering per chat, both from the
    # message context — so bind one here exactly as the harness does per turn.
    msg = _command_message(chat.id, text)
    msg_log.inbound(msg)
    try:
        kwargs, heading, kind = _entries_window(command, ctx.args or [], msg.timestamp)
        token = current_message.set(msg)
        try:
            listing, progress = await asyncio.to_thread(_list_and_total, kwargs, kind)
        finally:
            current_message.reset(token)
        reply = render_entries(heading, listing, progress)
        msg_log.outbound(msg, reply)
        for part in split_reply(reply):
            await ctx.bot.send_message(chat_id=chat.id, text=part)
    except Exception as e:
        log.exception("/%s failed", command)
        detail = str(e)
        if len(detail) > 250:
            detail = detail[:250] + "…"
        reply = f"⚠️ Couldn't list your entries.\n{type(e).__name__}: {detail}"
        msg_log.outbound(msg, reply)
        await ctx.bot.send_message(chat_id=chat.id, text=reply)


async def _on_catalog(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat is None or chat.id != ALLOWED_CHAT_ID:
        return                                   # silent drop — the security boundary
    msg = _command_message(chat.id, "/catalog")
    msg_log.inbound(msg)
    try:
        rows = await asyncio.to_thread(food.list_catalog)
        if not rows:
            msg_log.outbound(msg, "Your food catalog is empty.")
            await ctx.bot.send_message(chat_id=chat.id, text="Your food catalog is empty.")
            return
        messages = _catalog_messages(rows)
        msg_log.outbound(msg, f"[catalog listing, {len(rows)} items]")
        if len(messages) > 1:
            log.info("catalog split across %d messages (%d items)", len(messages), len(rows))
        for text in messages:
            await ctx.bot.send_message(chat_id=chat.id, text=text, parse_mode=ParseMode.HTML)
    except Exception as e:
        # Mirror _on_message: never leave the user hanging; only the owner's chat reaches here.
        log.exception("/catalog failed")
        detail = str(e)
        if len(detail) > 250:
            detail = detail[:250] + "…"
        reply = f"⚠️ Couldn't build the catalog.\n{type(e).__name__}: {detail}"
        msg_log.outbound(msg, reply)
        await ctx.bot.send_message(chat_id=chat.id, text=reply)
