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
# kcal and fat are CEILINGS (stay under); protein & fibre are FLOORS (hit at least);
# carbs are TRACKED (show progress toward the number, no floor/ceiling).
# Change a number here + `./scripts/bot.sh restart`.
TARGETS = {"kcal": 2200, "protein_g": 150, "fibre_g": 28, "carb_g": 250, "fat_g": 70}

# --- Suggestion tuning (first-pass heuristics; refine in use) ------------------
SUGGEST_AFTER_HOUR = 16     # local hour; evening "what to eat" suggestions start at/after 16:00
PROTEIN_MET_TOL_G = 5       # treat the protein floor as met within this many grams (stop nagging)
FIBRE_MET_TOL_G = 3         # same, for fibre
TREAT_MIN_KCAL = 150        # only offer a treat if at least this much calorie room is left
MIN_FITTING_PICKS = 2       # below this many fitting options, the reply may range off-catalog

_FLOOR_LABEL = {"fibre_g": "fibre", "protein_g": "protein"}

# What a serving is checked against for "does this still fit today?". kcal and fat are
# the real ceilings; carbs are tracked rather than capped, but blowing past the carb
# number is still worth naming, so a pick reports its overshoot on all three.
_ROOM_MACROS = ("kcal", "fat_g", "carb_g")


def remaining(consumed: dict) -> dict:
    """Frame consumed sums against the targets: a per-macro block the reply reads.

    kcal and fat are ceilings -> `remaining` is room left (negative once over budget).
    protein/fibre are floors -> `still_needed` is how much is left to hit the floor.
    carbs are tracked -> `remaining` is distance to the target (neutral; may go
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
            "direction": "ceiling",
        },
    }


def suggest(rem: dict, catalog: list[dict]) -> dict | None:
    """Pick catalog foods to close today's gaps, as a menu of realistic single-serving
    options split by whether they still fit inside the day's ceilings.

    Priority is protein, then fibre. Every option is one typical serving, checked against
    the room left on calories, fat and carbs. Options that fit come back in `fits`;
    options that would close the gap but breach a ceiling come back in `over` with the
    breach quantified, because "this closes protein but puts you 4g past fat" is a
    decision the user should get to make rather than one silently taken for them. If both
    floors are met and calories remain, offer a treat. Returns None when there's nothing
    worth saying.

    Shape: {"mode", "fits": [pick...], "over": [pick...], "room": {...},
            "off_catalog_ok": bool, "note"?: str}. Each pick is one serving of a saved
    food + `covers_g` (how much of the gap it closes) + `over` ({} when it fits).
    """
    room = {m: rem[m]["remaining"] for m in _ROOM_MACROS}
    protein_needed = rem["protein_g"]["still_needed"]
    fibre_needed = rem["fibre_g"]["still_needed"]
    fibre_unmet = fibre_needed > FIBRE_MET_TOL_G
    protein_unmet = protein_needed > PROTEIN_MET_TOL_G

    if room["kcal"] < 0:
        return _over_budget(protein_needed, fibre_needed, room, catalog)

    picks: list[dict] = []
    if protein_unmet and fibre_unmet:
        picks += _rank_servings(catalog, "protein_g", room, top=3)   # protein first...
        picks += _rank_servings(catalog, "fibre_g", room, top=2)     # ...then fill with fibre
    elif protein_unmet:
        picks += _rank_servings(catalog, "protein_g", room, top=5)
    elif fibre_unmet:
        picks += _rank_servings(catalog, "fibre_g", room, top=5)
    if picks:
        mode = "+".join(m for m, on in (("protein", protein_unmet), ("fibre", fibre_unmet)) if on)
        return _split(mode, picks, room, off_catalog=True)

    # Both floors met — spend leftover calories on a treat, if there's room.
    if room["kcal"] >= TREAT_MIN_KCAL:
        tpicks = _treat_picks(catalog, room, top=5)
        if tpicks:
            return _split("treat", tpicks, room, off_catalog=False)
    return None


# --- internals ----------------------------------------------------------------

def _split(mode: str, picks: list[dict], room: dict, off_catalog: bool,
           note: str | None = None) -> dict | None:
    """Partition ranked picks into what fits and what doesn't, and cap both lists.

    `off_catalog_ok` tells the reply it may name a food the user hasn't saved. It is only
    ever set when the catalog itself came up short — a well-stocked catalog should answer
    with the user's own foods, whose macros are known exactly, rather than with something
    the bot would have to estimate all over again.
    """
    picks = _dedup(picks)
    fits = [p for p in picks if not p["over"]][:5]
    fit_names = {p["name"] for p in fits}
    over = [p for p in picks if p["over"] and p["name"] not in fit_names][:3]
    if not fits and not over:
        return None
    out = {
        "mode": mode,
        "fits": fits,
        "over": over,
        "room": room,
        "off_catalog_ok": off_catalog and len(fits) < MIN_FITTING_PICKS,
    }
    if note:
        out["note"] = note
    return out


def _over_budget(protein_needed: float, fibre_needed: float, room: dict,
                 catalog: list[dict]) -> dict | None:
    # Already past the calorie ceiling. If a floor is still unmet, suggest the leanest
    # servings to close it (most nutrient per kcal — fewest extra calories) and flag the
    # overage. Protein first. If the floors are met, say nothing. Every pick lands in
    # `over` by construction (there is no calorie room left to fit inside), which is the
    # honest framing: nothing fits, these cost the least.
    if protein_needed > PROTEIN_MET_TOL_G:
        picks = _rank_servings(catalog, "protein_g", room, top=3, lean=True)
    elif fibre_needed > FIBRE_MET_TOL_G:
        picks = _rank_servings(catalog, "fibre_g", room, top=3, lean=True)
    else:
        return None
    if not picks:
        return None
    return _split("over_budget", picks, room, off_catalog=False,
                  note="already over the calorie ceiling")


def _rank_servings(catalog: list[dict], nutrient: str, room: dict,
                   top: int, lean: bool = False) -> list[dict]:
    # One typical serving per food. Ranked by how much of the gap a serving covers
    # (nutrient delivered per serving) — or, when `lean`, by nutrient per kcal, to add
    # the fewest calories when we're already over budget. Picks that breach a ceiling are
    # kept (a couple of them) rather than dropped: on a tight day they may be all there is.
    cands = [r for r in catalog if r[nutrient] > 0 and r["kcal"] > 0]
    if lean:
        cands.sort(key=lambda r: r[nutrient] / r["kcal"], reverse=True)
    else:
        cands.sort(key=lambda r: r[nutrient] * _typical_serving(r)["amount"] / r["ref_qty"],
                   reverse=True)
    fits: list[dict] = []
    over: list[dict] = []
    for r in cands:
        p = _serving_pick(r, nutrient, room)
        if p is None:
            continue
        (over if p["over"] else fits).append(p)
        if len(fits) >= top and len(over) >= 2:
            break
    return fits[:top] + over[:2]


def _serving_pick(row: dict, nutrient: str, room: dict) -> dict | None:
    # One typical serving, tagged with whatever it would push past. `covers_g` is how
    # much of the target nutrient that serving delivers.
    amount = _round_amount(_typical_serving(row)["amount"], row["ref_unit"])
    if amount <= 0:
        return None
    pick = _build_pick(row, amount, closes=_FLOOR_LABEL[nutrient], room=room)
    pick["covers_g"] = pick[nutrient]
    return pick


def _treat_picks(catalog: list[dict], room: dict, top: int) -> list[dict]:
    # Least "staple" first: lowest (protein+fibre) per kcal — the fun foods. One typical
    # serving each; a treat that breaches a ceiling is kept but tagged, so "you have the
    # calories but not the fat" is sayable.
    cands = [r for r in catalog if r["kcal"] > 0]
    cands.sort(key=lambda r: (r["protein_g"] + r["fibre_g"]) / r["kcal"])
    picks: list[dict] = []
    for r in cands:
        amount = _round_amount(_typical_serving(r)["amount"], r["ref_unit"])
        if amount <= 0:
            continue
        picks.append(_build_pick(r, amount, closes="treat", room=room))
        if len(picks) >= top:
            break
    return picks


def _overshoot(row: dict, amount: float, room: dict) -> dict:
    """Grams (or kcal) by which this serving would breach each ceiling. Empty when it
    fits. A macro whose room is ALREADY negative counts the whole serving plus the
    existing overage, so the number is always "how far past the target you'd end up"."""
    scale = amount / row["ref_qty"]
    out = {}
    for macro, left in room.items():
        added = row[macro] * scale
        if added > left:
            out[macro] = round(added - left, 1)
    return out


def _build_pick(row: dict, amount: float, closes: str, room: dict) -> dict:
    scale = amount / row["ref_qty"]
    serving = _typical_serving(row)
    return {
        "name": row["name"],
        "amount": amount,
        "unit": row["ref_unit"],
        "kcal": round(row["kcal"] * scale, 1),
        "protein_g": round(row["protein_g"] * scale, 1),
        "carb_g": round(row["carb_g"] * scale, 1),
        "fat_g": round(row["fat_g"] * scale, 1),
        "fibre_g": round(row["fibre_g"] * scale, 1),
        "closes": closes,
        "over": _overshoot(row, amount, room),
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
