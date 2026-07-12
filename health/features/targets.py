"""Daily targets — the single source of truth for the numbers + the "remaining
today" / "what to eat next" math. Pure and dependency-light: it imports nothing
from `features/` (so `food.py` can import it without a cycle) and touches neither
the DB nor the clock. Callers pass consumed sums + catalog rows in; it returns
plain data for the harness to phrase.

Design fit: the harness forbids Claude from doing arithmetic, so sizing a portion
to close a macro gap (e.g. "~20g protein = about one skyr portion") is computed
here, deterministically, and handed back for Claude to word.
"""
from __future__ import annotations

import re

# --- Targets (the owner's aligned daily numbers) ---------------------------------
# kcal is a CEILING (stay under); protein & fibre are FLOORS (hit at least);
# carbs & fat are TRACKED (show progress toward the number, no floor/ceiling).
# Change a number here + `./scripts/bot.sh restart`.
TARGETS = {"kcal": 2200, "protein_g": 150, "fibre_g": 28, "carb_g": 250, "fat_g": 70}

# --- Suggestion tuning (first-pass heuristics; refine in use) ------------------
SUGGEST_AFTER_HOUR = 16     # local hour; evening "what to eat" suggestions start at/after 16:00
PROTEIN_MET_TOL_G = 5       # treat the protein floor as met within this many grams (stop nagging)
FIBRE_MET_TOL_G = 3         # same, for fibre
TREAT_MIN_KCAL = 150        # only offer a treat if at least this much calorie room is left

_FLOOR_LABEL = {"fibre_g": "fibre", "protein_g": "protein"}


def remaining(consumed: dict) -> dict:
    """Frame consumed sums against the targets: a per-macro block the reply reads.

    kcal is a ceiling -> `remaining` is room left (negative once over budget).
    protein/fibre are floors -> `still_needed` is how much is left to hit the floor.
    carb/fat are tracked -> `remaining` is distance to the target (neutral; may go
    negative if over), with no floor/ceiling nagging.
    """
    t = TARGETS
    return {
        "kcal": {
            "target": t["kcal"],
            "consumed": round(consumed["kcal"], 1),
            "remaining": round(t["kcal"] - consumed["kcal"], 1),
            "direction": "ceiling",
        },
        "protein_g": {
            "target": t["protein_g"],
            "consumed": round(consumed["protein_g"], 1),
            "still_needed": round(max(0.0, t["protein_g"] - consumed["protein_g"]), 1),
            "direction": "floor",
        },
        "fibre_g": {
            "target": t["fibre_g"],
            "consumed": round(consumed["fibre_g"], 1),
            "still_needed": round(max(0.0, t["fibre_g"] - consumed["fibre_g"]), 1),
            "direction": "floor",
        },
        "carb_g": {
            "target": t["carb_g"],
            "consumed": round(consumed["carb_g"], 1),
            "remaining": round(t["carb_g"] - consumed["carb_g"], 1),
            "direction": "track",
        },
        "fat_g": {
            "target": t["fat_g"],
            "consumed": round(consumed["fat_g"], 1),
            "remaining": round(t["fat_g"] - consumed["fat_g"], 1),
            "direction": "track",
        },
    }


def suggest(rem: dict, catalog: list[dict]) -> dict | None:
    """Pick up to 5 catalog foods to close today's gaps, as a menu of realistic
    single-serving options. Priority: protein, then fibre; every option is one typical
    serving that fits the remaining calorie budget. If both floors are met and calories
    remain, offer a treat. Returns None when there's nothing to say.

    Shape: {"mode": <str>, "picks": [pick, ...], "kcal_room": <float>, "note"?: <str>}.
    Each pick is one serving of a saved food + `covers_g` (how much of the gap it closes).
    """
    kcal_left = rem["kcal"]["remaining"]
    protein_needed = rem["protein_g"]["still_needed"]
    fibre_needed = rem["fibre_g"]["still_needed"]
    fibre_unmet = fibre_needed > FIBRE_MET_TOL_G
    protein_unmet = protein_needed > PROTEIN_MET_TOL_G

    if kcal_left < 0:
        return _over_budget(protein_needed, fibre_needed, kcal_left, catalog)

    picks: list[dict] = []
    if protein_unmet and fibre_unmet:
        picks += _rank_servings(catalog, "protein_g", kcal_left, top=3)   # protein first...
        picks += _rank_servings(catalog, "fibre_g", kcal_left, top=2)     # ...then fill with fibre
    elif protein_unmet:
        picks += _rank_servings(catalog, "protein_g", kcal_left, top=5)
    elif fibre_unmet:
        picks += _rank_servings(catalog, "fibre_g", kcal_left, top=5)
    picks = _dedup(picks)[:5]
    if picks:
        mode = "+".join(m for m, on in (("protein", protein_unmet), ("fibre", fibre_unmet)) if on)
        return {"mode": mode, "picks": picks, "kcal_room": kcal_left}

    # Both floors met — spend leftover calories on a treat, if there's room.
    if kcal_left >= TREAT_MIN_KCAL:
        tpicks = _treat_picks(catalog, kcal_left, top=5)
        if tpicks:
            return {"mode": "treat", "picks": tpicks, "kcal_room": kcal_left}
    return None


# --- internals ----------------------------------------------------------------

def _over_budget(protein_needed: float, fibre_needed: float, kcal_left: float,
                 catalog: list[dict]) -> dict | None:
    # Already past the calorie ceiling. If a floor is still unmet, suggest the leanest
    # servings to close it (most nutrient per kcal — fewest extra calories) and flag the
    # overage. Protein first. If the floors are met, say nothing.
    if protein_needed > PROTEIN_MET_TOL_G:
        picks = _rank_servings(catalog, "protein_g", None, top=3, lean=True)
    elif fibre_needed > FIBRE_MET_TOL_G:
        picks = _rank_servings(catalog, "fibre_g", None, top=3, lean=True)
    else:
        return None
    if not picks:
        return None
    return {"mode": "over_budget", "picks": picks, "kcal_room": kcal_left,
            "note": "already over the calorie ceiling"}


def _rank_servings(catalog: list[dict], nutrient: str, kcal_room: float | None,
                   top: int, lean: bool = False) -> list[dict]:
    # One typical serving per food. Ranked by how much of the gap a serving covers
    # (nutrient delivered per serving) — or, when `lean`, by nutrient per kcal, to add
    # the fewest calories when we're already over budget.
    cands = [r for r in catalog if r[nutrient] > 0 and r["kcal"] > 0]
    if lean:
        cands.sort(key=lambda r: r[nutrient] / r["kcal"], reverse=True)
    else:
        cands.sort(key=lambda r: r[nutrient] * _typical_serving(r)["amount"] / r["ref_qty"],
                   reverse=True)
    picks: list[dict] = []
    for r in cands:
        p = _serving_pick(r, nutrient, kcal_room)
        if p:
            picks.append(p)
        if len(picks) >= top:
            break
    return picks


def _serving_pick(row: dict, nutrient: str, kcal_room: float | None) -> dict | None:
    # One typical serving, only if it fits the calorie room. `covers_g` is how much of
    # the target nutrient that serving delivers.
    amount = _round_amount(_typical_serving(row)["amount"], row["ref_unit"])
    if amount <= 0:
        return None
    if kcal_room is not None and row["kcal"] * amount / row["ref_qty"] > kcal_room:
        return None
    pick = _build_pick(row, amount, closes=_FLOOR_LABEL[nutrient])
    pick["covers_g"] = pick[nutrient]
    return pick


def _treat_picks(catalog: list[dict], kcal_left: float, top: int) -> list[dict]:
    # Least "staple" first: lowest (protein+fibre) per kcal — the fun foods. One typical
    # serving each, only those whose serving fits the leftover calories.
    cands = [r for r in catalog if r["kcal"] > 0]
    cands.sort(key=lambda r: (r["protein_g"] + r["fibre_g"]) / r["kcal"])
    picks: list[dict] = []
    for r in cands:
        amount = _round_amount(_typical_serving(r)["amount"], r["ref_unit"])
        if amount <= 0 or r["kcal"] * amount / r["ref_qty"] > kcal_left:
            continue
        picks.append(_build_pick(r, amount, closes="treat"))
        if len(picks) >= top:
            break
    return picks


def _build_pick(row: dict, amount: float, closes: str) -> dict:
    scale = amount / row["ref_qty"]
    serving = _typical_serving(row)
    return {
        "name": row["name"],
        "amount": amount,
        "unit": row["ref_unit"],
        "kcal": round(row["kcal"] * scale, 1),
        "protein_g": round(row["protein_g"] * scale, 1),
        "fibre_g": round(row["fibre_g"] * scale, 1),
        "closes": closes,
        "typical_serving": serving,
        "servings": round(amount / serving["amount"], 1) if serving["amount"] else None,
    }


def _typical_serving(row: dict) -> dict:
    """A natural serving to phrase suggestions against ("about one skyr portion").
    For `each` foods the reference portion IS the serving (1 can/fillet/slice). For
    g/ml foods, use an explicit "typical serving Ng" from notes when present, else
    fall back to the reference portion."""
    unit = row["ref_unit"]
    if unit in ("g", "ml"):
        m = re.search(
            r"(?:typical serving|serving\s*=|1 serving\s*=)\s*(\d+(?:\.\d+)?)\s*(g|ml)\b",
            row.get("notes", "") or "", re.I,
        )
        if m and m.group(2).lower() == unit:
            return {"amount": float(m.group(1)), "unit": unit}
    return {"amount": row["ref_qty"], "unit": unit}


def _round_amount(amount: float, unit: str) -> float:
    # Clean, edible portions: nearest 0.5 for countable items, nearest 5 for g/ml.
    if unit == "each":
        return max(0.5, round(amount * 2) / 2)
    return max(5.0, round(amount / 5) * 5)


def _dedup(picks: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for p in picks:
        if p["name"] in seen:
            continue
        seen.add(p["name"])
        out.append(p)
    return out
