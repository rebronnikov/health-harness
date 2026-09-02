"""The example food catalog: what `seed_catalog` loads and what `seed_demo` logs from.

Each entry is stated PER its reference portion (`ref_qty` `ref_unit`, one of g | ml | each),
exactly like the `save_food` tool. The macro figures are readings off UK product labels;
the `notes` say what basis a figure is on, because an implicit basis is how a catalog goes
quietly wrong:

- **Meat and fish are stored on a COOKED basis, and every entry says so.** A pack panel is
  raw, but food is weighed after cooking, so logging a cooked portion against a raw panel
  under-counts it by the cooking loss on every row. Each converted entry keeps the raw
  panel and the yield in its note, so nobody converts it a second time.
- **An omitted nutrient is not zero.** UK labels may leave fibre out; reading that as 0
  makes a banana a zero-fibre food. Where a label omits a figure, the note says where the
  number came from instead.
- **`each` foods scale by count, not weight**, so a counted item (an egg, a banana, a
  slice) carries its typical weight in the note for the reader, not for the executor.

Six of these carry a fixed meal label in `food.DEFAULT_MEALS` (the supplements and the
two snacks), so the demo can show one message writing two meals.
"""
from __future__ import annotations

INGREDIENTS: list[dict] = [
    {"name": "jumbo oats", "ref_qty": 40, "ref_unit": "g",
     "kcal": 148, "protein_g": 4.5, "carb_g": 26, "fat_g": 2.3, "fibre_g": 3.3,
     "notes": "per 40g dry; typical serving 40g"},
    {"name": "milk", "ref_qty": 100, "ref_unit": "ml",
     "kcal": 46, "protein_g": 3.5, "carb_g": 4.6, "fat_g": 1.5, "fibre_g": 0,
     "notes": "semi-skimmed; typical serving 350ml"},
    {"name": "skyr", "ref_qty": 100, "ref_unit": "g",
     "kcal": 51, "protein_g": 9, "carb_g": 3.4, "fat_g": 0.1, "fibre_g": 0,
     "notes": "skyr yoghurt; typical serving 210g"},
    # per egg = per-100g label * 0.5 (1 medium egg ~= 58g whole, ~50g edible).
    {"name": "eggs", "ref_qty": 1, "ref_unit": "each",
     "kcal": 71.5, "protein_g": 7.05, "carb_g": 0, "fat_g": 4.8, "fibre_g": 0,
     "notes": ("free range medium eggs; 1 egg ~= 58g whole (~50g edible); per 100g: "
               "143 kcal, 14.1g protein, 9.6g fat")},
    # per banana = per-100g label * 1.2 (1 medium banana ~= 120g peeled). Fibre is the one
    # figure NOT off the label, which omits it — read literally that makes bananas a
    # zero-fibre food, when used as the only source. 1.4g/100g
    # is the UK value: another retailer publishes it against an identical 1.2/20.3/0.1
    # panel, and McCance & Widdowson 2015 agrees. USDA's 2.6g/100g is a different
    # measurement basis and would make this the only non-UK-label item in a catalog the
    # fibre floor is judged against.
    {"name": "banana", "ref_qty": 1, "ref_unit": "each",
     "kcal": 108, "protein_g": 1.44, "carb_g": 24.4, "fat_g": 0.12, "fibre_g": 1.68,
     "notes": ("1 banana ~= 120g peeled; per 100g: 90 kcal, 1.2g protein, 20.3g carb, "
               "0.1g fat, 1.4g fibre (fibre from McCance & Widdowson 2015 — the label "
               "omits it)")},
    {"name": "blueberries", "ref_qty": 100, "ref_unit": "g",
     "kcal": 36, "protein_g": 0.7, "carb_g": 7.3, "fat_g": 0, "fibre_g": 1.2,
     "notes": "typical serving 50g"},
    # carbs stored as 0.5 (label says "< 0.5g"); fibre counted separately.
    {"name": "chia seeds", "ref_qty": 15, "ref_unit": "g",
     "kcal": 64, "protein_g": 3.8, "carb_g": 0.5, "fat_g": 4.2, "fibre_g": 4.8},
    {"name": "mixed nuts", "ref_qty": 25, "ref_unit": "g",
     "kcal": 165, "protein_g": 4, "carb_g": 1.1, "fat_g": 15.7, "fibre_g": 1.9,
     "notes": "almonds, walnuts, brazil nuts, hazelnuts, cashews"},
    # per 45g slice = per-100g label * 0.45 (1 slice = 45g, loaf = 650g).
    {"name": "sourdough", "ref_qty": 1, "ref_unit": "each",
     "kcal": 135.9, "protein_g": 4.95, "carb_g": 21.6, "fat_g": 3.87, "fibre_g": 1.85,
     "notes": ("seeded sourdough; 1 slice = 45g; per 100g: 302 kcal, 11g protein, "
               "48g carb, 8.6g fat, 4.1g fibre")},
    {"name": "cheddar cheese", "ref_qty": 30, "ref_unit": "g",
     "kcal": 125, "protein_g": 7.6, "carb_g": 0, "fat_g": 10.5, "fibre_g": 0},
    {"name": "tuna", "ref_qty": 1, "ref_unit": "each",
     "kcal": 109, "protein_g": 24.9, "carb_g": 0, "fat_g": 1, "fibre_g": 0,
     "notes": "canned tuna in spring water; per drained can"},
    {"name": "chickpeas", "ref_qty": 200, "ref_unit": "g",
     "kcal": 142, "protein_g": 7.9, "carb_g": 17.5, "fat_g": 2.9, "fibre_g": 7.2,
     "notes": "canned, in water; typical serving 100g"},
    {"name": "buckwheat", "ref_qty": 100, "ref_unit": "g",
     "kcal": 92, "protein_g": 3.4, "carb_g": 17, "fat_g": 0.6, "fibre_g": 2.7,
     "notes": ("cooked; per 100g cooked (log cooked weight); standard cooked "
               "buckwheat groats. Dry/raw label was 345 kcal/100g")},
    # Fibre was seeded as 0 because the label omits it. An omitted nutrient is not zero —
    # A tomato-and-pepper base has about 1.7g/100g in UK reference values.
    {"name": "shakshuka", "ref_qty": 100, "ref_unit": "g",
     "kcal": 92, "protein_g": 1.7, "carb_g": 10, "fat_g": 4.7, "fibre_g": 1.7,
     "notes": ("typical serving 170g; label lists no fibre, so 1.7g/100g is the UK "
               "reference value for a tomato/pepper base, not a label figure")},

    # Meat and fish use a stated reference basis. Cooked conversions keep the yield in the notes.
    {"name": "chicken breast", "ref_qty": 100, "ref_unit": "g",
     "kcal": 149, "protein_g": 31.8, "carb_g": 0, "fat_g": 2.2, "fibre_g": 0,
     "notes": ("chicken breast fillets, COOKED weight — log the cooked grams. Raw pack "
               "panel was 107 kcal, 22.9g protein, 1.6g fat per 100g; converted at a 0.72 "
               "cooking yield, which lands on the UK CoFID grilled-breast figure of 148")},
    # stored per 100g so weighed portions scale exactly (1 fillet ~= 100g); log actual grams.
    {"name": "salmon fillet", "ref_qty": 100, "ref_unit": "g",
     "kcal": 250, "protein_g": 29.6, "carb_g": 0, "fat_g": 14.2, "fibre_g": 0,
     "notes": ("COOKED weight — log the cooked grams. Raw pack panel was 195 kcal, "
               "23.1g protein, 11.1g fat per 100g; converted at a 0.78 cooking yield")},
    # Raw panel; converted like the chicken. 0.70 is the usual braising/stewing yield.
    {"name": "beef diced steak", "ref_qty": 100, "ref_unit": "g",
     "kcal": 174, "protein_g": 29.9, "carb_g": 0, "fat_g": 5.7, "fibre_g": 0,
     "notes": ("COOKED weight — log the cooked grams. Raw pack panel was 122 kcal, "
               "20.9g protein, 4g fat per 100g; converted at a 0.70 braising yield")},
    # Already a COOKED panel: the pack publishes an "as sold" column and a "prepared" one,
    # and these are the prepared figures. Left unconverted; the note says so, so nobody
    # converts it a second time.
    {"name": "beef mince, 5% fat", "ref_qty": 100, "ref_unit": "g",
     "kcal": 170, "protein_g": 25.3, "carb_g": 0.5, "fat_g": 7.5, "fibre_g": 0.5,
     "notes": ("COOKED weight — log the cooked grams. Read off the pack's 'prepared' "
               "column; the raw column is 136 kcal, 21g protein, 5g fat per 100g")},
    # Ready to eat, so there is no cooking loss to correct — said out loud because the
    # other fish and meat entries carry a basis and silence would read as an omission.
    {"name": "smoked salmon", "ref_qty": 100, "ref_unit": "g",
     "kcal": 184, "protein_g": 23.3, "carb_g": 0, "fat_g": 9.9, "fibre_g": 0,
     "notes": "ready to eat — no cooking loss; the pack panel applies as weighed"},

    # --- the six foods with a fixed meal label (food.DEFAULT_MEALS) -------------------
    # fibre supplement; digestible macros negligible (logged for the fibre).
    {"name": "psyllium husk", "ref_qty": 8, "ref_unit": "g",
     "kcal": 0, "protein_g": 0, "carb_g": 0, "fat_g": 0, "fibre_g": 7,
     "notes": "standard serving 8g; near-pure fibre supplement, digestible macros negligible"},
    # supplements logged for adherence; these two contribute nothing to the totals.
    {"name": "creatine", "ref_qty": 1, "ref_unit": "each",
     "kcal": 0, "protein_g": 0, "carb_g": 0, "fat_g": 0, "fibre_g": 0,
     "notes": "creatine monohydrate; no calories or macros"},
    {"name": "vitamin D", "ref_qty": 1, "ref_unit": "each",
     "kcal": 0, "protein_g": 0, "carb_g": 0, "fat_g": 0, "fibre_g": 0,
     "notes": "oral spray; no calories or macros"},
    # Two 1g capsules are ~2g of fat, so this is not a zero-calorie supplement the way
    # creatine and vitamin D, which contribute no macros.
    {"name": "fish oil", "ref_qty": 1, "ref_unit": "each",
     "kcal": 18, "protein_g": 0, "carb_g": 0, "fat_g": 2, "fibre_g": 0,
     "notes": "1 serving = 2 capsules ~= 2g fish oil; small but not zero, unlike creatine"},
    {"name": "protein bar", "ref_qty": 1, "ref_unit": "each",
     "kcal": 214, "protein_g": 20, "carb_g": 18, "fat_g": 7.5, "fibre_g": 4,
     "notes": "1 bar = 60g"},
    {"name": "dates", "ref_qty": 100, "ref_unit": "g",
     "kcal": 284, "protein_g": 2.2, "carb_g": 66, "fat_g": 0.4, "fibre_g": 7.5,
     "notes": "pitted; typical serving 40g (about two)"},
]
