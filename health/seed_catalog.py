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
    # fibre supplement; digestible macros negligible (logged for the fibre).
    {"name": "psyllium husk", "ref_qty": 8, "ref_unit": "g",
     "kcal": 0, "protein_g": 0, "carb_g": 0, "fat_g": 0, "fibre_g": 7,
     "notes": ("standard serving 2 tbsp = 8g; near-pure fibre supplement, "
               "digestible macros negligible")},
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
    # "bulk" in the name -> distinct slug so a later whey (other brand) stays separate.
    {"name": "whey protein", "ref_qty": 32, "ref_unit": "g",
     "kcal": 122, "protein_g": 23, "carb_g": 5.1, "fat_g": 1.1, "fibre_g": 0.4,
     "notes": "whey protein; standard serving 32g"},
    {"name": "skyr", "ref_qty": 100, "ref_unit": "g",
     "kcal": 51, "protein_g": 9, "carb_g": 3.4, "fat_g": 0.1, "fibre_g": 0,
     "notes": "skyr yoghurt; typical serving 210g"},
    {"name": "tuna", "ref_qty": 1, "ref_unit": "each",
     "kcal": 109, "protein_g": 24.9, "carb_g": 0, "fat_g": 1, "fibre_g": 0,
     "notes": "canned tuna in spring water; per can"},
    # stored per 100g so weighed portions scale exactly (1 fillet ~= 100g); log actual grams.
    # Meat and fish use a stated reference basis. Cooked conversions keep the yield in the notes.
    {"name": "salmon fillet", "ref_qty": 100, "ref_unit": "g",
     "kcal": 250, "protein_g": 29.6, "carb_g": 0, "fat_g": 14.2, "fibre_g": 0,
     "notes": ("COOKED weight — log the cooked grams. Raw pack panel was 195 kcal, "
               "23.1g protein, 11.1g fat per 100g; converted at a 0.78 cooking yield")},
    {"name": "chicken breast", "ref_qty": 100, "ref_unit": "g",
     "kcal": 149, "protein_g": 31.8, "carb_g": 0, "fat_g": 2.2, "fibre_g": 0,
     "notes": ("chicken breast fillets, COOKED weight — log the cooked grams. Raw pack "
               "panel was 107 kcal, 22.9g protein, 1.6g fat per 100g; converted at a 0.72 "
               "cooking yield, which lands on the UK CoFID grilled-breast figure of 148")},
    {"name": "cheddar cheese", "ref_qty": 30, "ref_unit": "g",
     "kcal": 125, "protein_g": 7.6, "carb_g": 0, "fat_g": 10.5, "fibre_g": 0},
    {"name": "buckwheat", "ref_qty": 100, "ref_unit": "g",
     "kcal": 92, "protein_g": 3.4, "carb_g": 17, "fat_g": 0.6, "fibre_g": 2.7,
     "notes": ("cooked; per 100g cooked (log cooked weight); standard cooked "
               "buckwheat groats. Dry/raw label was 345 kcal/100g")},
    {"name": "chickpeas", "ref_qty": 200, "ref_unit": "g",
     "kcal": 142, "protein_g": 7.9, "carb_g": 17.5, "fat_g": 2.9, "fibre_g": 7.2,
     "notes": "canned, in water"},
    {"name": "blueberries", "ref_qty": 100, "ref_unit": "g",
     "kcal": 36, "protein_g": 0.7, "carb_g": 7.3, "fat_g": 0, "fibre_g": 1.2,
     "notes": "typical serving 50g"},
    # per banana = per-100g * 1.2 (1 medium banana ~= 120g peeled).
    # Fibre is the one figure NOT off the Waitrose label, which omits it — read literally
    # that would make bananas a zero-fibre food. 1.4g/100g is
    # the UK value: M&S publish it against an identical 1.2/20.3/0.1 panel, and McCance &
    # Widdowson 2015 agrees. USDA's 2.6g/100g is a different measurement basis and would
    # make this the only non-UK-label item in a catalog the fibre floor is judged against.
    {"name": "banana", "ref_qty": 1, "ref_unit": "each",
     "kcal": 108, "protein_g": 1.44, "carb_g": 24.4, "fat_g": 0.12, "fibre_g": 1.68,
     "notes": ("bananas; 1 banana ~= 120g peeled; per 100g: 90 kcal, "
               "1.2g protein, 20.3g carb, 0.1g fat, 1.4g fibre (fibre from the M&S "
               "Rainforest Alliance panel + McCance & Widdowson 2015 — the label omits it)")},
    # per egg = per-100g * 0.5 (1 medium egg ~= 58g whole, ~50g edible).
    {"name": "eggs", "ref_qty": 1, "ref_unit": "each",
     "kcal": 71.5, "protein_g": 7.05, "carb_g": 0, "fat_g": 4.8, "fibre_g": 0,
     "notes": ("free range white eggs; 1 medium egg ~= 58g "
               "whole (~50g edible); per 100g: 143 kcal, 14.1g protein, 9.6g fat")},
    # Fibre was seeded as 0 because the label omits it. An omitted nutrient is not zero —
    # the same reading error that under-reported bananas for 42 rows. Tomato-and-pepper
    # base, ~1.7g/100g on UK reference values.
    {"name": "shakshuka", "ref_qty": 100, "ref_unit": "g",
     "kcal": 92, "protein_g": 1.7, "carb_g": 10, "fat_g": 4.7, "fibre_g": 1.7,
     "notes": ("typical serving 170g; label lists no fibre, so 1.7g/100g is the UK "
               "reference value for a tomato/pepper base, not a label figure")},

    # --- entries that reached the catalog through the bot's save_food, corrected here --
    # so the file is the reviewable record of every value, not just the seeded ones.

    # Already a COOKED panel (Waitrose publish an "as sold" 136 kcal column and a
    # "prepared" 170 kcal one, and 170/25.3/7.5 is the prepared one). Left alone; only the
    # note is made explicit, so nobody converts it a second time.
    {"name": "beef mince, 5% fat", "ref_qty": 100, "ref_unit": "g",
     "kcal": 170, "protein_g": 25.3, "carb_g": 0.5, "fat_g": 7.5, "fibre_g": 0.5,
     "notes": ("COOKED weight — log the cooked grams. Read off the pack's 'prepared' "
               "column on the label; the raw column is 136 kcal, 21g protein, "
               "5g fat per 100g. Carbs and fibre were both stored as 0 before that — the "
               "label gives 0.5g of each")},

    # Raw panel; converted like the chicken. 0.70 is the usual braising/stewing yield.
    {"name": "beef diced steak", "ref_qty": 100, "ref_unit": "g",
     "kcal": 174, "protein_g": 29.9, "carb_g": 0, "fat_g": 5.7, "fibre_g": 0,
     "notes": ("COOKED weight — log the cooked grams. Raw pack panel was 122 kcal, "
               "20.9g protein, 4g fat per 100g; converted at a 0.70 braising yield")},

    # Ready to eat, so there is no cooking loss to correct — said out loud because the
    # other fish and meat entries now carry a basis and silence would read as an omission.
    {"name": "smoked salmon", "ref_qty": 100, "ref_unit": "g",
     "kcal": 184, "protein_g": 23.3, "carb_g": 0, "fat_g": 9.9, "fibre_g": 0,
     "notes": "ready to eat — no cooking loss; the pack panel applies as weighed"},

    # The label's carbohydrate value does not establish a fibre value.
    {"name": "kombucha", "ref_qty": 100, "ref_unit": "ml",
     "kcal": 11, "protein_g": 0, "carb_g": 2.9, "fat_g": 0, "fibre_g": 0,
     "notes": "fermented tea; no meaningful fibre (2.9g was the carb figure miscopied)"},

    # Was 42 kcal/100g — roughly a single 17g portion's values entered as a per-100g row,
    # so every log of it counted about a sixth of what was eaten.
    {"name": "soft cheese, garlic & herbs", "ref_qty": 100, "ref_unit": "g",
     "kcal": 245, "protein_g": 5.5, "carb_g": 3.6, "fat_g": 23, "fibre_g": 0.1,
     "notes": "soft cheese; per 100g (the previous 42 kcal row was one portion's values)"},

    # Was 8 kcal/100g with 0 carbs, which is not a real label panel.
    {"name": "chestnut mushroom", "ref_qty": 100, "ref_unit": "g",
     "kcal": 22, "protein_g": 3.1, "carb_g": 3.3, "fat_g": 0.5, "fibre_g": 1.0,
     "notes": "raw; UK reference values"},

    # Two 1g fish-oil capsules are ~2g of fat, so this is not a zero-calorie supplement
    # creatine and vitamin D, which contribute no macros.
    {"name": "fish oil",
     "ref_qty": 1, "ref_unit": "each",
     "kcal": 18, "protein_g": 0, "carb_g": 0, "fat_g": 2, "fibre_g": 0,
     "notes": "1 serving = 2 capsules ~= 2g fish oil; small but not zero, unlike creatine"},

    # --- repeat foods that were never saved, so every log re-estimated them ------------
    # Illustrative values per drink.
    {"name": "latte", "ref_qty": 1, "ref_unit": "each",
     "kcal": 96, "protein_g": 5, "carb_g": 8, "fat_g": 4, "fibre_g": 0,
     "notes": "illustrative values per drink"},

    # Illustrative values per labelled serving.
    {"name": "sea bass", "ref_qty": 100, "ref_unit": "g",
     "kcal": 129, "protein_g": 24.9, "carb_g": 0, "fat_g": 2.7, "fibre_g": 0,
     "notes": ("COOKED weight — log the cooked grams. Raw is ~97 kcal, 18.7g protein, "
               "2g fat per 100g; converted at a 0.75 cooking yield")},
    {"name": "cottage cheese", "ref_qty": 100, "ref_unit": "g",
     "kcal": 67, "protein_g": 13, "carb_g": 2.5, "fat_g": 0.5, "fibre_g": 0,
     "notes": "per 100g; label lists no fibre"},
    # count-based snack; stored per slice (each), 28g/slice recorded in notes.
    {"name": "protein bar", "ref_qty": 1, "ref_unit": "each",
     "kcal": 118, "protein_g": 2.2, "carb_g": 9.5, "fat_g": 7.8, "fibre_g": 0,
     "notes": "1 slice = 28g; label lists no fibre"},
    {"name": "bulgur wheat with red and white quinoa", "ref_qty": 100, "ref_unit": "g",
     "kcal": 130, "protein_g": 4.9, "carb_g": 24, "fat_g": 1.2, "fibre_g": 2.1,
     "notes": "Waitrose; per 100g cooked/as-prepared (log cooked weight)"},
    {"name": "white, black and red quinoa", "ref_qty": 100, "ref_unit": "g",
     "kcal": 115, "protein_g": 4.3, "carb_g": 16.4, "fat_g": 2, "fibre_g": 7.1,
     "notes": "Waitrose; per 100g cooked/as-prepared (log cooked weight)"},
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
