"""Catalog removal through Telegram and replay of version-pinned corrections."""
import asyncio
import json
import sqlite3
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from health import rebuild_db
from health.adapters import telegram_adapter
from health.features import food
from health.store import writer
from health.store.connection import get_conn

from conftest import rows, system_text, text_block, tool_block

DAY = "2026-07-14"


def test_telegram_removes_saved_food_and_refreshes_catalog(ctx, catalog, fake_client, monkeypatch):
    ctx()
    saved_name = "Orchard Oat Bites"
    food.save_food(saved_name, 100, "g", 280, 2, 70, 0, 7)
    reply = f"Removed {saved_name} from your catalog."
    fake_client.script(
        ([tool_block("remove_catalog_food", {"name": saved_name})], "tool_use"),
        [text_block(reply)],
    )
    chat = SimpleNamespace(id=123)
    telegram = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
    monkeypatch.setattr(telegram_adapter, "ALLOWED_CHAT_ID", chat.id)
    message = SimpleNamespace(
        effective_chat=chat,
        effective_message=SimpleNamespace(text="Please take the orchard oat bites out of my catalog"),
    )

    asyncio.run(telegram_adapter._on_catalog(message, telegram))
    assert saved_name in telegram.bot.send_message.await_args.kwargs["text"]
    asyncio.run(telegram_adapter._on_message(message, telegram))
    telegram.bot.send_message.assert_awaited_with(chat_id=chat.id, text=reply)
    asyncio.run(telegram_adapter._on_catalog(message, telegram))
    assert saved_name not in telegram.bot.send_message.await_args.kwargs["text"]

    first_call, followup = fake_client.calls
    assert saved_name in system_text(first_call)
    assert "ask one question naming the candidates" in system_text(first_call)
    assert "couldn't find that food in the catalog" in system_text(first_call)
    assert "query_log to find the row, then edit_entry or delete_entry" in system_text(first_call)
    result = followup["messages"][-1]["content"][0]
    assert json.loads(result["content"]) == {
        "removed": {"name": saved_name, "slug": "orchard_oat_bites"},
    }


def test_remove_catalog_food_only_accepts_an_exact_normalized_name(ctx, tmp_path):
    ctx()
    saved = food.save_food("Waitrose Skyr", 100, "g", 51, 9, 3.4, 0.1)["saved"]
    corrections = tmp_path / "corrections.jsonl"
    with pytest.raises(ValueError, match="No saved food named"):
        food.remove_catalog_food("skyr")
    assert not corrections.exists()

    request = "Remove Waitrose Skyr from the catalog"
    ctx(source="telegram", text=request)
    food.remove_catalog_food("  WAITROSE-skyr  ")
    with pytest.raises(ValueError, match="No saved food named"):
        food.remove_catalog_food("Waitrose Skyr")

    (line,) = corrections.read_text().splitlines()
    event = json.loads(line)
    assert event["action"] == "delete"
    assert event["target_domain"] == "food_catalog"
    assert event["target_key"] == {"slug": saved["slug"]}
    assert event["target_ingested_at"] == saved["ingested_at"]
    assert event["raw"] == request and event["source"] == "telegram"


def test_remove_catalog_food_checks_the_version_before_appending(ctx, catalog, tmp_path, monkeypatch):
    ctx()
    record = writer.record

    def resave_before_removal(domain, fields, **kwargs):
        if domain == "corrections":
            food.save_food("skyr", 100, "g", 55, 9, 3.4, 0.1)
        return record(domain, fields, **kwargs)

    monkeypatch.setattr(writer, "record", resave_before_removal)
    with pytest.raises(ValueError, match="updated during removal"):
        food.remove_catalog_food("skyr")
    assert not (tmp_path / "corrections.jsonl").exists()
    assert rows(get_conn(), "SELECT kcal FROM food_catalog WHERE slug = 'skyr'") == [{"kcal": 55}]


def test_rebuild_preserves_catalog_removal_and_later_resave(ctx, catalog):
    ctx()
    item = {"name": "skyr", "quantity": 100, "unit": "g"}
    food.log_food(DAY, "breakfast", [item])
    history = rows(get_conn(), "SELECT * FROM food_log")
    food.remove_catalog_food("skyr")
    food.remove_catalog_food("banana")
    assert food.log_food(DAY, "lunch", [item])["unresolved"][0]["name"] == "skyr"
    food.save_food("skyr", 100, "g", 55, 9, 3.4, 0.1)

    with closing(sqlite3.connect(rebuild_db.rebuild())) as rebuilt:
        rebuilt.row_factory = sqlite3.Row
        assert rows(rebuilt, "SELECT * FROM food_log") == history
        assert rows(rebuilt, "SELECT slug, kcal FROM food_catalog ORDER BY slug") == [
            {"slug": "salmon", "kcal": 195}, {"slug": "skyr", "kcal": 55},
        ]
