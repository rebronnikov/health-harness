"""The system prompt is pure and deliberately thin: it injects only today's date
and the current local time, which the food/meal routing rules depend on.
"""
from datetime import datetime

from health.core.prompts import build_system_prompt


def test_build_system_prompt_injects_date_and_time():
    now = datetime(2026, 7, 14, 9, 5)
    out = build_system_prompt(now=now)
    assert "2026-07-14" in out
    assert "09:05" in out


def test_build_system_prompt_is_pure():
    now = datetime(2026, 7, 14, 9, 5)
    assert build_system_prompt(now=now) == build_system_prompt(now=now)
