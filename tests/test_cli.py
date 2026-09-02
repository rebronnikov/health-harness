"""The terminal adapter: slash commands with no key, free text through the harness, and
one reply handle for the whole session — the line that keeps propose -> "yes" working.
"""
import io
from datetime import date

import pytest

from health import cli
from health.adapters import entries_view
from health.core import harness
from health.core.contracts import NormalizedMessage
from health.features import corrections

from conftest import DEFAULT_TS, log_steps

DAY = "2026-07-14"


@pytest.fixture(autouse=True)
def pinned_day(monkeypatch):
    monkeypatch.setenv("HEALTH_TODAY", DAY)


def _run(lines):
    out = io.StringIO()
    cli.run(lines, out=out)
    return out.getvalue()


def test_slash_commands_render_with_no_api_key(monkeypatch, ctx):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(harness, "handle", lambda msg: pytest.fail("the model was called"))
    ctx(text="9000 steps")
    log_steps(DAY, 9000)
    out = _run(["/today"])
    assert f"Today ({DAY})" in out
    assert "steps: 9000" in out
    assert "🔥 Calories: 0 / 2200" in out              # the rendered block, not a narration


def test_free_text_without_a_key_says_so_instead_of_failing(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(harness, "handle", lambda msg: pytest.fail("the model was called"))
    assert cli.NO_KEY_REPLY in _run(["40g oats"])


def test_free_text_goes_through_the_harness_on_one_handle(monkeypatch):
    seen = []
    monkeypatch.setattr(harness, "handle", lambda msg: seen.append(msg) or "ok")
    out = _run(["40g skyr", "yes"])
    assert out.count("ok\n") == 2
    assert [m.text for m in seen] == ["40g skyr", "yes"]
    assert {m.source for m in seen} == {cli.SOURCE}
    # The load-bearing line: history, food._pending and the listing are all keyed by it.
    assert {m.reply_handle for m in seen} == {cli.HANDLE}
    assert {m.timestamp.date() for m in seen} == {date.fromisoformat(DAY)}


def test_health_today_pins_the_date_but_keeps_the_clock(monkeypatch):
    assert cli.now().date() == date.fromisoformat(DAY)
    monkeypatch.delenv("HEALTH_TODAY")
    assert cli.now().date() == date.today()


def test_a_listing_and_a_correction_share_the_handle(monkeypatch, ctx):
    """`/last 2` remembers its numbering under the CLI's handle, so a following "delete 2"
    — routed to the harness on the same handle — sees the LAST LISTING fragment."""
    ctx(text="9000 steps")
    log_steps(DAY, 9000)
    _run(["/last 2"])
    msg = NormalizedMessage(source="cli", reply_handle=cli.HANDLE, timestamp=cli.now(),
                            text="delete 1")
    assert "1. steps: 9000" in corrections.listing_system_context(msg)


def test_a_failure_is_printed_and_the_session_goes_on(monkeypatch):
    def boom(msg):
        raise RuntimeError("no credit")
    monkeypatch.setattr(harness, "handle", boom)
    out = _run(["hello", "/today"])
    assert "nothing was logged" in out and "RuntimeError: no credit" in out
    assert f"Today ({DAY})" in out                    # the next line still ran


def test_render_command_covers_catalog_and_unknown(catalog, ctx):
    ctx()
    msg = NormalizedMessage(source="cli", reply_handle=cli.HANDLE, timestamp=DEFAULT_TS,
                            text="/catalog")
    assert "Food catalog · 3 items" in entries_view.render_command(msg)
    msg.text = "/bogus"
    assert "Unknown command /bogus" in entries_view.render_command(msg)
    assert entries_view.parse_command("/last@my_bot 3") == ("last", ["3"])
