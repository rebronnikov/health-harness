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
from datetime import datetime

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from health.adapters import entries_view
from health.config import ALLOWED_CHAT_ID, TELEGRAM_BOT_TOKEN
from health.core import harness
from health.core.contracts import NormalizedMessage
from health.features import food
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
        # safe. Only the allowlisted chat reaches here, so showing the raw reason is fine.
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


async def _on_entries(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat is None or chat.id != ALLOWED_CHAT_ID:
        return                                   # silent drop — the security boundary
    text = update.effective_message.text or "/today"
    command = text.split()[0].lstrip("/").split("@")[0]
    msg = _command_message(chat.id, text)
    try:
        # The whole command — context binding, reads, rendering, transcript — is
        # channel-neutral and blocking, so it runs off the event loop like the harness.
        reply = await asyncio.to_thread(entries_view.render_command, msg)
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
        # Mirror _on_message: never leave the user hanging; only the allowlisted chat reaches here.
        log.exception("/catalog failed")
        detail = str(e)
        if len(detail) > 250:
            detail = detail[:250] + "…"
        reply = f"⚠️ Couldn't build the catalog.\n{type(e).__name__}: {detail}"
        msg_log.outbound(msg, reply)
        await ctx.bot.send_message(chat_id=chat.id, text=reply)
