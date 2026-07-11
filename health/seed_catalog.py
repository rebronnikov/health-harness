"""Bulk-load sample foods into the food catalog.

The values below are read straight off product labels and stored verbatim (no
estimation). Each ingredient is loaded through the SAME path a live Telegram
message uses — a synthetic `NormalizedMessage` context plus `food.save_food(...)`
— so provenance (`raw`/`source`/`ingested_at`) and the SQLite projection stay
consistent with normal ingest, and `rebuild_db` replays these lines faithfully.

Idempotent: the catalog dedupes on `slug` (a normalized name), so editing a
value and re-running just updates that row. Run with:

    python -m health.seed_catalog        # or: python health/seed_catalog.py

Each entry's macros are stated PER the reference portion (`ref_qty` `ref_unit`),
exactly like the `save_food` tool. `ref_unit` is one of: g | ml | each.
"""
from __future__ import annotations

from datetime import datetime

from health.core.contracts import NormalizedMessage, current_message
from health.features import food
from health.store.connection import init_db

# One dict per ingredient, matching food.save_food(**ingredient). Fill from labels.
# Example shape (delete once real staples are in):
#   {"name": "canned tuna in brine, drained", "ref_qty": 1, "ref_unit": "each",
#    "kcal": 116, "protein_g": 26, "carb_g": 0, "fat_g": 1, "fibre_g": 0},
INGREDIENTS: list[dict] = [
    # carbs stored as 0.5 (label says "< 0.5g"); fibre counted separately.
    {"name": "chia seeds", "ref_qty": 15, "ref_unit": "g",
     "kcal": 64, "protein_g": 3.8, "carb_g": 0.5, "fat_g": 4.2, "fibre_g": 4.8},
    {"name": "mixed nuts", "ref_qty": 25, "ref_unit": "g",
     "kcal": 165, "protein_g": 4, "carb_g": 1.1, "fat_g": 15.7, "fibre_g": 1.9,
     "notes": "almonds, walnuts, brazil nuts, hazelnuts, cashews"},
    {"name": "yeast flakes", "ref_qty": 5, "ref_unit": "g",
     "kcal": 17, "protein_g": 2.4, "carb_g": 1, "fat_g": 0.25, "fibre_g": 1,
     "notes": ("micronutrients per 5g: 2.1mg B1, 0.9mg B2, 21mg B3, 7mg B5, "
               "1.7mg B6, 9.8mcg B7, 260mcg B9, 2.2mcg B12, 6mg zinc")},
    {"name": "jumbo oats", "ref_qty": 40, "ref_unit": "g",
     "kcal": 148, "protein_g": 4.5, "carb_g": 26, "fat_g": 2.3, "fibre_g": 3.3,
     "notes": "per 40g: 1.4mg iron, 0.14mg thiamin (B1)"},
    # per 45g slice = label per-100g values * 0.45 (1 slice = 45g, loaf = 650g).
    {"name": "sourdough", "ref_qty": 1, "ref_unit": "each",
     "kcal": 135.9, "protein_g": 4.95, "carb_g": 21.6, "fat_g": 3.87, "fibre_g": 1.85,
     "notes": ("seeded sourdough; 1 slice = 45g; per 100g: 302 kcal, 11g protein, "
               "48g carb, 8.6g fat, 4.1g fibre")},
    {"name": "milk", "ref_qty": 100, "ref_unit": "ml",
     "kcal": 46, "protein_g": 3.5, "carb_g": 4.6, "fat_g": 1.5, "fibre_g": 0,
     "notes": "typical serving 350ml"},
    # supplement; logged for adherence, contributes nothing to macro totals.
    {"name": "creatine", "ref_qty": 1, "ref_unit": "each",
     "kcal": 0, "protein_g": 0, "carb_g": 0, "fat_g": 0, "fibre_g": 0,
     "notes": "creatine monohydrate; no calories or macros"},
    {"name": "skyr", "ref_qty": 100, "ref_unit": "g",
     "kcal": 51, "protein_g": 9, "carb_g": 3.4, "fat_g": 0.1, "fibre_g": 0,
     "notes": "skyr yoghurt; typical serving 210g"},
    {"name": "tuna", "ref_qty": 1, "ref_unit": "each",
     "kcal": 109, "protein_g": 24.9, "carb_g": 0, "fat_g": 1, "fibre_g": 0,
     "notes": "canned tuna in spring water; per can"},
    {"name": "salmon fillet", "ref_qty": 1, "ref_unit": "each",
     "kcal": 195, "protein_g": 23.1, "carb_g": 0, "fat_g": 11.1, "fibre_g": 0,
     "notes": "1 fillet = 100g; home"},
    {"name": "chicken breast", "ref_qty": 100, "ref_unit": "g",
     "kcal": 107, "protein_g": 22.9, "carb_g": 0, "fat_g": 1.6, "fibre_g": 0,
     "notes": "chicken breast fillets"},
    {"name": "cheddar cheese", "ref_qty": 30, "ref_unit": "g",
     "kcal": 125, "protein_g": 7.6, "carb_g": 0, "fat_g": 10.5, "fibre_g": 0},
    {"name": "buckwheat", "ref_qty": 100, "ref_unit": "g",
     "kcal": 345, "protein_g": 12, "carb_g": 75, "fat_g": 3, "fibre_g": 10.5,
     "notes": "dry/uncooked (log dry weight)"},
    {"name": "chickpeas", "ref_qty": 200, "ref_unit": "g",
     "kcal": 142, "protein_g": 7.9, "carb_g": 17.5, "fat_g": 2.9, "fibre_g": 7.2,
     "notes": "canned, in water"},
    {"name": "blueberries", "ref_qty": 100, "ref_unit": "g",
     "kcal": 36, "protein_g": 0.7, "carb_g": 7.3, "fat_g": 0, "fibre_g": 1.2,
     "notes": "typical serving 50g"},
    {"name": "shakshuka", "ref_qty": 100, "ref_unit": "g",
     "kcal": 92, "protein_g": 1.7, "carb_g": 10, "fat_g": 4.7, "fibre_g": 0,
     "notes": "typical serving 170g; label lists no fibre"},
]


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
    if not INGREDIENTS:
        print("No ingredients defined yet — add them to INGREDIENTS and re-run.")
        return
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
