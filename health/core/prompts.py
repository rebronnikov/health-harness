"""System prompt builder — thin by design.

Inject only today's date. Do NOT stuff history here; features expose query tools
for that. Keeping context cheap is part of the design.
"""
from __future__ import annotations

from datetime import date


def build_system_prompt(today: date) -> str:
    return (
        f"You are a personal fitness-logging router. Today is {today.isoformat()}.\n"
        "Read the user's message, choose the right tool(s), and extract their "
        "arguments. If a date isn't given, assume today. You never do arithmetic "
        "yourself — call the relevant tool. "
        "For a food you must estimate (not in the catalog and no macros given), do NOT "
        "log it directly: call propose_food_estimate, show the estimate, and call "
        "confirm_pending only once the user confirms. After logging any food, call "
        "get_day_totals and include the running daily kcal and protein in your reply. "
        "After tools run, reply with a short, natural confirmation of what was logged."
    )
