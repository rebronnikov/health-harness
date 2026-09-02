"""Bulk-load the example catalog (`health/example_catalog.py`) into the food catalog.

The values are read straight off product labels and stored verbatim (no estimation).
Each ingredient is loaded through the SAME path a live Telegram message uses — a
synthetic `NormalizedMessage` context plus `food.save_food(...)` — so provenance
(`raw`/`source`/`ingested_at`) and the SQLite projection stay consistent with normal
ingest, and `rebuild_db` replays these lines faithfully.

Idempotent: the catalog dedupes on `slug` (a normalized name), so editing a value and
re-running just updates that row. Run with:

    python -m health.seed_catalog

To keep your own staples, edit `example_catalog.INGREDIENTS` — or save foods from the
bot ("save oats: 380 kcal, 13g protein, 67g carb, 7g fat, 10g fibre per 100g") and
never touch this file at all.
"""
from __future__ import annotations

from datetime import datetime

from health.core.contracts import NormalizedMessage, current_message
from health.example_catalog import INGREDIENTS
from health.features import food
from health.store.connection import init_db


def _label(ing: dict) -> str:
    """Human-readable provenance string stored as the row's `raw`."""
    return (
        f"seed: {ing['name']} — {ing['kcal']} kcal, {ing['protein_g']}g protein, "
        f"{ing['carb_g']}g carb, {ing['fat_g']}g fat, {ing['fibre_g']}g fibre "
        f"per {ing['ref_qty']:g}{ing['ref_unit']}"
    )


def load(ingredients: list[dict] = INGREDIENTS) -> list[dict]:
    """Save each ingredient under a synthetic seed context. Returns saved rows."""
    init_db()
    saved = []
    for ing in ingredients:
        msg = NormalizedMessage(
            source="seed",
            reply_handle=None,
            timestamp=datetime.now(),
            text=_label(ing),
        )
        token = current_message.set(msg)
        try:
            saved.append(food.save_food(**ing)["saved"])
        finally:
            current_message.reset(token)
    return saved


def main() -> None:
    rows = load()
    print(f"Loaded {len(rows)} ingredient(s) into the catalog:")
    for r in rows:
        print(
            f"  {r['name']}: {r['kcal']:g} kcal, {r['protein_g']:g}p / "
            f"{r['carb_g']:g}c / {r['fat_g']:g}f / {r['fibre_g']:g}fib "
            f"per {r['ref_qty']:g}{r['ref_unit']}"
        )


if __name__ == "__main__":
    main()
