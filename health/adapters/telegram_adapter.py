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
import logging
from datetime import datetime

from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

from health.config import ALLOWED_CHAT_ID, TELEGRAM_BOT_TOKEN
from health.core import harness
from health.core.contracts import NormalizedMessage

log = logging.getLogger(__name__)


def build_app() -> Application:
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
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
