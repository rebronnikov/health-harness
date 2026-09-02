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

from health.config import TARGETS  # noqa: F401  (re-exported: callers read targets.TARGETS)

# --- Targets ---------------------------------------------------------------------
# The numbers live in `health.config` (DEFAULT_TARGETS, overridable per key with
# TARGET_* env vars), so this module has no environment to read and stays pure.
# The semantics are fixed here: kcal and fat are CEILINGS (stay under); protein and
# fibre are FLOORS (hit at least); carbs are TRACKED (progress toward the number,
# no floor/ceiling). Changing a number means editing `.env` + restarting the bot.

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


# --- the standard progress block ----------------------------------------------
# One fixed shape for reporting a day against the targets. It is rendered here,
# deterministically, rather than phrased by the model: narrating `remaining()` drifted
# differently between replies, making results difficult to compare.
PROGRESS_ROWS = (
    ("🔥 Calories", "kcal", ""),        # nutrition-label order
    ("🥩 Protein", "protein_g", "g"),
    ("🍞 Carbs", "carb_g", "g"),
    ("🥑 Fat", "fat_g", "g"),
    ("🌾 Fibre", "fibre_g", "g"),
)

DAY_HEADING = "Today so far"
# The heading `propose_food_estimate` renders under: a projection is not a log, and the
# heading is what stops the block being read as one. A constant because BLOCK_LINE below
# has to know every heading this module can emit.
PROJECTION_HEADING = "Would put today at"


def render_progress(rem: dict, heading: str = DAY_HEADING) -> str:
    """`Label: consumed / target (delta)`, one line per macro under a heading.

    Everything is rounded to whole units and the delta is derived from that rounded pair,
    so the three numbers on a line always add up — a block built to be scanned can't
    afford "25g / 28g (3.4g left)". Per-portion macros still get a decimal below 10 (see
    `food.render_catalog_table`), where 0.4g is a big relative error; a daily total is not.

    Under target reads "left" on every row — room left on a ceiling, still to eat on a
    floor, distance to the number on carbs — so one word covers all three directions and
    every line parses the same way. Past target is where they split: a breached ceiling
    (or carb target) names the overshoot, a floor just says "met", since exceeding a floor
    is what a floor is for.
    """
    lines = [heading]
    for label, key, unit in PROGRESS_ROWS:
        r = rem[key]
        consumed, target = round(r["consumed"]), round(r["target"])
        lines.append(
            f"{label}: {consumed}{unit} / {target}{unit} "
            f"({_delta(target - consumed, unit, r['direction'])})"
        )
    return "\n".join(lines)


def _delta(left: int, unit: str, direction: str) -> str:
    """The parenthesised part of a row. `left` is target - consumed, so a positive value
    means short of the target and a negative one means past it."""
    if direction == "floor":
        return "met" if left <= 0 else f"{left}{unit} left"
    return f"{left}{unit} left" if left >= 0 else f"{-left}{unit} over"


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


# --- the standard suggestions block --------------------------------------------
# The other half of `render_progress`: `suggest()` already does the arithmetic, so the
# reply has nothing left to decide but wording — which is exactly what drifts. The model
# used to format these bullets itself from the raw dict and invented a fresh heading each
# time ("Ideas to close the gaps:", "Some options to hit your protein:"). Rendered here,
# the list reads the same every evening and the prompt only echoes it.
#
# `off_catalog_ok` is deliberately NOT rendered: those picks are named from the model's own
# knowledge, carry no macros, and can't come out of a catalog-shaped renderer.
SUGGEST_HEADINGS = {
    "treat": "Room for a treat",
    # Nothing fits by construction here, so the heading carries the caveat itself rather
    # than leaving every pick to sit under "Doesn't quite fit".
    "over_budget": "Already past the calorie ceiling — the leanest options",
}
DEFAULT_SUGGEST_HEADING = "To close the gaps"
OVER_HEADING = "Doesn't quite fit"

# What ONE LINE of a block rendered by this module looks like, for the harness's
# fabrication check (`Feature.block_signature`). Two alternatives: a heading exactly, or a
# macro row — `Calories: 1846 / 2200`, matched on the plain word and the `N / N` shape so a
# swapped icon still matches and ordinary prose ("protein is the one to chase") does not.
#
# It exists because the verbatim-block repair is blind in the one case that matters most:
# it compares the reply against blocks a tool RETURNED, so a turn where NO tool ran has an
# empty comparison set and passes silently when no tool returned a block.
BLOCK_LINE = re.compile(
    r"^\s*(?:"
    + "|".join(re.escape(h) for h in (
        DAY_HEADING, PROJECTION_HEADING, DEFAULT_SUGGEST_HEADING, OVER_HEADING,
        *SUGGEST_HEADINGS.values(),
    ))
    + r")\s*:?\s*$"
    r"|(?:"
    + "|".join(re.escape(label.partition(" ")[2]) for label, _, _ in PROGRESS_ROWS)
    + r")\s*:\s*-?[\d.]+\s*\w*\s*/\s*-?[\d.]+"
)

# Icon, plain word and unit per macro, derived from PROGRESS_ROWS so the two blocks can
# never disagree about what fibre is called or which icon it wears — editing that one
# constant still moves everything.
_MACRO_WORDS = {
    key: (label.partition(" ")[0], label.partition(" ")[2].lower(), unit)
    for label, key, unit in PROGRESS_ROWS
}
# What a pick's `closes` tag reports against. A treat closes no gap; it spends calories.
_CLOSES_MACRO = {"protein": "protein_g", "fibre": "fibre_g", "treat": "kcal"}


def render_suggestions(sugg: dict) -> str:
    """The finished suggestion list, echoed verbatim like `render_progress`.

    Fitting picks come first under a heading chosen by mode; anything that would breach a
    ceiling follows under its own heading with the breach named on the line. The split is
    never collapsed — "this closes protein but puts you past fat" is the user's call to
    make, and a breaching pick printed among the clean ones reads as a recommendation.
    """
    fits, over = sugg["fits"], sugg["over"]
    heading = SUGGEST_HEADINGS.get(sugg["mode"], DEFAULT_SUGGEST_HEADING)
    lines: list[str] = []
    if fits:
        lines += [heading, *(_pick_line(p) for p in fits)]
        if over:
            lines += ["", OVER_HEADING, *(_pick_line(p) for p in over)]
    else:
        # Nothing fits. In over_budget mode the heading already explains why; in any other
        # mode the caveat has to come from OVER_HEADING or the breach loses its framing.
        lines += [heading if sugg["mode"] == "over_budget" else OVER_HEADING,
                  *(_pick_line(p) for p in over)]
    return "\n".join(lines)


def _pick_line(pick: dict) -> str:
    """`<icon> <portion> (<what it does>[, <what it breaks>])`."""
    icon, _, _ = _MACRO_WORDS[_CLOSES_MACRO[pick["closes"]]]
    inner = [_covers(pick)]
    if pick["over"]:
        inner.append(_breach(pick["over"]))
    return f"{icon} {_portion(pick)} ({', '.join(inner)})"


def _portion(pick: dict) -> str:
    """How much of it. Counted foods take the count in front ("1× tuna"); weighed foods
    take the amount after an em dash ("smoked salmon — 100g").

    A count deliberately gets no noun: the catalog stores `each` without recording whether
    one is a can, a fillet or a slice, and inventing the word is the drift this block
    exists to stop.
    """
    if pick["unit"] == "each":
        return f"{pick['amount']:g}× {pick['name']}"
    return f"{pick['name']} — {pick['amount']:g}{pick['unit']}"


def _covers(pick: dict) -> str:
    # A treat has no gap to close (both floors were met to get here) — its cost is the
    # only number worth printing, and `_treat_picks` sets no `covers_g` to print instead.
    if pick["closes"] == "treat":
        return f"~{_at_least_one(pick['kcal'])} kcal"
    return f"~{_at_least_one(pick['covers_g'])}g {pick['closes']}"


def _breach(over: dict) -> str:
    """"4g past fat", or "4g past fat and 12g past carbs" — in `_ROOM_MACROS` order,
    which is what `_overshoot` builds the map in."""
    return " and ".join(
        f"{_at_least_one(amount)}{_MACRO_WORDS[macro][2]} past {_MACRO_WORDS[macro][1]}"
        for macro, amount in over.items()
    )


def _at_least_one(value: float) -> int:
    """Whole units, never rounded down to zero. Every number this block prints exists by
    construction — a pick covers something, a breach breaches something — so "0g past fat"
    would read as "it fits". Unlike the progress block there is nothing here that has to
    add up, so whole units are free.
    """
    return max(1, round(value))


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
