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

from health.config import ALLOWED_CHAT_ID, TELEGRAM_BOT_TOKEN
from health.core import harness
from health.core.contracts import NormalizedMessage
from health.features import food

log = logging.getLogger(__name__)

TELEGRAM_MAX_CHARS = 4096  # hard limit on a single sendMessage
_CATALOG_CHUNK = 3900      # split the table below this, leaving room for <pre> tags


def build_app() -> Application:
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("catalog", _on_catalog))
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
    await ctx.bot.send_message(chat_id=chat.id, text=reply)


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


async def _on_catalog(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat is None or chat.id != ALLOWED_CHAT_ID:
        return                                   # silent drop — the security boundary
    try:
        rows = await asyncio.to_thread(food.list_catalog)
        if not rows:
            await ctx.bot.send_message(chat_id=chat.id, text="Your food catalog is empty.")
            return
        messages = _catalog_messages(rows)
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
        await ctx.bot.send_message(
            chat_id=chat.id,
            text=f"⚠️ Couldn't build the catalog.\n{type(e).__name__}: {detail}",
        )
