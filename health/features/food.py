"""Food / macro / calorie tracking — two features in one module.

`food_catalog` holds reusable foods (macros per a reference portion). `food_log`
holds consumption events, one row per logged eating event (a re-log of the same food
adds a new row rather than overwriting — see the PRIMARY KEY). A logged row stores the
*resolved* macros for the amount eaten, so it is self-contained: rebuild_db replays
stored numbers and never needs the catalog, and editing a catalog entry never
rewrites history.

Arithmetic boundary (mirrors the global "never do arithmetic yourself" rule for
Claude): Claude maps language -> (food, quantity, unit) and, for unknown foods,
*estimates* macro values. This module's executors do all deterministic scaling
(amount / ref_qty) and summation.

Estimates are gated: a food whose MACROS we have to estimate is NOT written on the spot.
It is stashed in an ephemeral per-chat pending slot and only committed once the user has
SEEN the numbers and agreed to them. That is the actual rule; "not from the proposing
message" is only the usual way to check it, and checking it that way alone cost the user an
extra confirmation on most estimates through 2026-08 — the model kept showing an estimate
as prose and calling propose_food_estimate only after he had already said yes, so the
refusal fired on a proposal he had plainly seen. confirm_pending now reads the previous
reply (core/history.py) to tell that catch-up apart from the 2026-07-25 incident it
guards against, where the model chained propose → confirm in one turn and committed an
estimate that had appeared nowhere. Catalog hits and user-supplied macros auto-commit, as
everything else does.

Known hole, and it bites harder now the bot is forbidden to ask before logging: the gate
keys on estimated *macros*, not estimated *amounts*. A catalog hit whose quantity Claude
guessed ("chickpeas" with no weight) auto-commits, and food_log has no correction path —
a later "make it 40g" adds a row rather than replacing one. See ROADMAP.md.
"""
from __future__ import annotations

import logging
import re
import sqlite3
from datetime import datetime, timedelta
from typing import Any

from health.core import history
from health.core.contracts import Feature, get_context
from health.features import targets
from health.store import writer
from health.store.connection import get_conn

log = logging.getLogger(__name__)

CATALOG_SCHEMA = """
CREATE TABLE IF NOT EXISTS food_catalog (
  slug            TEXT NOT NULL,
  name            TEXT NOT NULL,
  ref_qty         REAL NOT NULL,      -- size of the reference portion
  ref_unit        TEXT NOT NULL,      -- g | ml | each
  kcal            REAL NOT NULL,      -- macros are per (ref_qty, ref_unit)
  protein_g       REAL NOT NULL,
  carb_g          REAL NOT NULL,
  fat_g           REAL NOT NULL,
  fibre_g         REAL NOT NULL,
  notes           TEXT NOT NULL DEFAULT '', -- free-text detail: composition, brand, prep...
  raw             TEXT NOT NULL,
  ingested_at     TEXT NOT NULL,
  schema_version  INTEGER NOT NULL,
  PRIMARY KEY (slug)                  -- dedupe key, enforced by the DB
);
"""

LOG_SCHEMA = """
CREATE TABLE IF NOT EXISTS food_log (
  date            TEXT NOT NULL,
  meal            TEXT NOT NULL,
  food_slug       TEXT NOT NULL,
  food_name       TEXT NOT NULL,
  amount          REAL NOT NULL,      -- amount consumed
  amount_unit     TEXT NOT NULL,      -- g | ml | each
  kcal            REAL NOT NULL,      -- resolved macros for `amount`
  protein_g       REAL NOT NULL,
  carb_g          REAL NOT NULL,
  fat_g           REAL NOT NULL,
  fibre_g         REAL NOT NULL,
  estimated       INTEGER NOT NULL,   -- 1 if Claude estimated the macros
  catalog_slug    TEXT,               -- catalog entry that resolved it, or NULL
  raw             TEXT NOT NULL,
  ingested_at     TEXT NOT NULL,
  schema_version  INTEGER NOT NULL,
  PRIMARY KEY (date, meal, food_slug, ingested_at) -- one row per eating event; a re-log
                                                   -- (new ingested_at) adds, never overwrites
);
"""

_UNIT_ALIASES = {
    "g": "g", "gram": "g", "grams": "g", "gm": "g",
    "ml": "ml", "milliliter": "ml", "milliliters": "ml", "millilitre": "ml",
    "each": "each", "ea": "each", "item": "each", "items": "each",
    "unit": "each", "units": "each", "serving": "each", "servings": "each",
    "pc": "each", "pcs": "each", "piece": "each", "pieces": "each",
}
_MACROS = ("kcal", "protein_g", "carb_g", "fat_g", "fibre_g")


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")


def _norm_unit(unit: str) -> str:
    """Map a unit onto g/ml/each, or refuse it.

    It used to pass anything through lowercased, so `tbsp` stayed `tbsp` — which misses
    every catalog lookup and, on the save path, freezes a unit nothing can ever scale.
    The tool schema has an enum, but a schema is a request, not an enforcement.
    """
    raw = (unit or "").strip().lower()
    known = _UNIT_ALIASES.get(raw)
    if known is None:
        raise ValueError(
            f"{unit!r} is not a unit this log understands. Use 'g' for mass, 'ml' for "
            "volume or 'each' for countable items, converting the amount yourself is NOT "
            "allowed — if the user gave tablespoons or cups, estimate the grams and say "
            "that is what you assumed. Nothing was logged."
        )
    return known


def _lookup_catalog(conn: sqlite3.Connection, slug: str) -> dict | None:
    row = conn.execute(
        "SELECT slug, name, ref_qty, ref_unit, kcal, protein_g, carb_g, fat_g, fibre_g "
        "FROM food_catalog WHERE slug = ?",
        (slug,),
    ).fetchone()
    return dict(row) if row is not None else None


# Skip a recent matching write when a confirmation repeats an earlier item.
_DUPLICATE_WINDOW = timedelta(seconds=120)


def _recent_duplicate(conn: sqlite3.Connection, date: str, slug: str, meal: str,
                      amount: float, unit: str, now: datetime) -> dict | None:
    """The identical row this one would duplicate, if it was written moments ago.

    `food_log` is append-per-event on purpose — its dedupe key carries `ingested_at` so a
    second helping ADDS rather than overwrites — which leaves nothing to stop the model
    writing the same food twice. A confirmation may restate the earlier message. The window keeps a genuine later helping loggable.
    """
    row = conn.execute(
        "SELECT ingested_at, kcal FROM food_log "
        "WHERE date = ? AND food_slug = ? AND meal = ? AND amount = ? AND amount_unit = ? "
        "ORDER BY ingested_at DESC LIMIT 1",
        (date, slug, meal, amount, unit),
    ).fetchone()
    if row is None:
        return None
    try:
        written = datetime.fromisoformat(row["ingested_at"])
    except (TypeError, ValueError):          # a hand-edited row must not break ingest
        return None
    if now - written > _DUPLICATE_WINDOW:
        return None
    return {"ingested_at": row["ingested_at"], "kcal": row["kcal"],
            "seconds_ago": round((now - written).total_seconds())}


def _day_totals(conn: sqlite3.Connection, date: str) -> dict:
    # Pure consumed sums for a day. Target/remaining framing is added by _status.
    row = conn.execute(
        "SELECT COALESCE(SUM(kcal),0)      AS kcal, "
        "       COALESCE(SUM(protein_g),0) AS protein_g, "
        "       COALESCE(SUM(carb_g),0)    AS carb_g, "
        "       COALESCE(SUM(fat_g),0)     AS fat_g, "
        "       COALESCE(SUM(fibre_g),0)   AS fibre_g "
        "FROM food_log WHERE date = ?",
        (date,),
    ).fetchone()
    return {
        "date": date,
        "kcal": round(row["kcal"], 1),
        "protein_g": round(row["protein_g"], 1),
        "carb_g": round(row["carb_g"], 1),
        "fat_g": round(row["fat_g"], 1),
        "fibre_g": round(row["fibre_g"], 1),
    }


def _catalog_rows(conn: sqlite3.Connection) -> list[dict]:
    # Full macros for every saved food, name-ordered — what targets.suggest sizes
    # portions from, and what the /catalog listing renders.
    rows = conn.execute(
        "SELECT name, ref_qty, ref_unit, kcal, protein_g, carb_g, fat_g, fibre_g, notes "
        "FROM food_catalog ORDER BY name"
    ).fetchall()
    return [dict(r) for r in rows]


def _status(consumed: dict, date: str, heading: str = targets.DAY_HEADING) -> dict:
    """Enrich consumed sums with targets + remaining, and — only in the evening, for
    today — deterministic catalog suggestions to close the day's gaps. The time gate
    reads the in-flight message timestamp so Claude never reasons about the clock.

    `progress_block` and `suggestions_block` are the finished, standard-shape texts the
    reply reproduces verbatim; the structured `remaining`/`suggestions` stay alongside them
    for the suggestion math and for anything the reply still words itself (the off-catalog
    licence).
    """
    out = {**consumed, "targets": targets.TARGETS, "remaining": targets.remaining(consumed)}
    out["progress_block"] = targets.render_progress(out["remaining"], heading)
    now = get_context().timestamp
    if date == now.date().isoformat() and now.hour >= targets.SUGGEST_AFTER_HOUR:
        suggestions = targets.suggest(out["remaining"], _catalog_rows(get_conn()))
        if suggestions:
            out["suggestions"] = suggestions
            out["suggestions_block"] = targets.render_suggestions(suggestions)
    return out


# The label a food row carries until the user says which meal it belonged to. A real
# label, not a NULL, so the row is a first-class part of the day's totals while it waits.
UNLABELLED_MEAL = "unlabelled"


def normalise_meal(meal: str | None) -> str:
    """Normalise the meal label. Whatever the user called it, that is the label.

    This used to number repeats — two `breakfast` messages became `breakfast-1` and
    `breakfast-2`. That was backwards: a meal is often logged across several messages
    (the oats, then the coffee ten minutes later), and numbering split one sitting into
    two while offering no way to merge them back. The user delimits their own meals now; a
    genuine second sitting is whatever he types ("second breakfast"), which passes
    through untouched. An absent label becomes `unlabelled` — a state the bot asks about
    and `set_meal` resolves — rather than being silently guessed at from the clock.
    """
    return (meal or "").strip().lower() or UNLABELLED_MEAL


# Foods that always carry their own meal label. Keyed by display name and slugged through
# the same `_slug` the log path uses, so this reads like the catalog listing the user sees and
# cannot drift from how names are keyed. A name that stops matching the catalog would just
# fall through to the old behaviour — silently — which is what the round-trip test guards.
DEFAULT_MEALS = {_slug(name): normalise_meal(meal) for name, meal in {
    "psyllium husk": "supplements",
    "creatine": "supplements",
    "vitamin D": "supplements",
    "fish oil": "supplements",
    "protein bar": "snack",
    "dates": "snack",
}.items()}


def _resolve_meal(slug: str, meal: str | None) -> str:
    """The meal one logged food lands under.

    A food in DEFAULT_MEALS takes its own label and **outranks a meal named in the message**,
    which is the surprising half and is deliberate. A message can contain both meal items and supplements, so one write may span two meals. Both `log_food`'s return and the MEAL prompt rule are shaped for it.

    Anything else keeps the old behaviour: the named meal, or `unlabelled` and a question.
    """
    return DEFAULT_MEALS.get(slug) or normalise_meal(meal)


def _accumulate(acc: dict, slug: str, name: str, amount: float, unit: str,
                macros: dict, catalog_slug: str | None) -> None:
    # Sum items that share a slug so two mentions of one food in a single message land as
    # one row (one occasion), instead of two rows a microsecond apart in the same meal.
    if slug in acc:
        e = acc[slug]
        e["amount"] += amount
        for k in _MACROS:
            e[k] += macros[k]
    else:
        acc[slug] = {
            "food_slug": slug, "food_name": name,
            "amount": amount, "amount_unit": unit,
            "catalog_slug": catalog_slug,
            **{k: macros[k] for k in _MACROS},
        }


def _save_catalog(name: str, ref_qty: float, ref_unit: str, macros: dict,
                  notes: str = "") -> dict:
    # Every log of this food divides by ref_qty. A zero makes the whole evening raise
    # (the suggestion pass divides by it too) AFTER rows have already committed, so the
    # model sees "nothing was logged" and retries into duplicates; a negative silently
    # inverts the macros. Refuse at the door — this is the one value in the catalog that
    # cannot be corrected later by logging over it.
    if not ref_qty > 0:
        raise ValueError(
            f"{name!r} cannot be saved with ref_qty={ref_qty!r}: the reference portion is "
            "what every future log is scaled by, so it must be a positive number "
            "(100 for a per-100g label, 1 for a counted item). Nothing was saved."
        )
    return writer.record("food_catalog", {
        "slug": _slug(name), "name": name,
        "ref_qty": float(ref_qty), "ref_unit": _norm_unit(ref_unit),
        "notes": notes,
        **{k: float(macros[k]) for k in _MACROS},
    })


# --- executors -------------------------------------------------------------

def save_food(name: str, ref_qty: float, ref_unit: str, kcal: float,
              protein_g: float, carb_g: float, fat_g: float, fibre_g: float = 0.0,
              notes: str = "") -> dict:
    saved = _save_catalog(name, ref_qty, ref_unit,
                          {"kcal": kcal, "protein_g": protein_g, "carb_g": carb_g,
                           "fat_g": fat_g, "fibre_g": fibre_g},
                          notes=notes)
    _clear_offer(_slug(name))            # the standing "want to save this?" is answered
    return {"saved": saved}


def _unresolved_reason(cat: sqlite3.Row | None, unit: str) -> str:
    """Why an item couldn't be logged from the catalog, phrased as the next action.
    Handed straight to Claude: a bare name left it to invent an explanation, and it
    surfaced the unit mismatch to the user as a question about grams vs 'each'."""
    if cat is None:
        return "not in the catalog — estimate its macros and propose them"
    return (
        f"the catalog stores this per {cat['ref_qty']:g} {cat['ref_unit']}, but you passed "
        f"{unit!r} — re-log it with an amount in {cat['ref_unit']}, estimating one from the "
        "portion the user described if they didn't give a number"
    )


# Atwater factors: kcal per gram of each macro (EU labelling also credits fibre at 2).
# Used only to sanity-check numbers, never to compute them.
_ATWATER = {"protein_g": 4, "carb_g": 4, "fat_g": 9, "fibre_g": 2}
# How far a stated kcal may sit from what its own macros imply before it is worth saying
# out loud. BOTH bounds must trip, so rounding on a small portion stays quiet.
_MACRO_CHECK_FRACTION = 0.25
_MACRO_CHECK_KCAL = 60


def _macro_check(name: str, macros: dict) -> dict | None:
    """Flag a set of macros whose calories don't add up, for the reply to query.

    Nothing else in the pipeline ever looks at whether numbers are *possible* — a label
    read wrong, or half-copied, is stored verbatim. A portion's calories and per-100g macros can otherwise appear together in one row. A warning, never a refusal — the user's own number wins if they
    confirm it, and blocking a log to argue about arithmetic is worse than logging it.
    """
    kcal = macros["kcal"]
    if kcal <= 0:
        return None                          # a zero-calorie food has nothing to reconcile
    implied = sum(macros[k] * f for k, f in _ATWATER.items())
    gap = kcal - implied
    if abs(gap) <= _MACRO_CHECK_KCAL or abs(gap) / kcal <= _MACRO_CHECK_FRACTION:
        return None
    return {"food_name": name, "stated_kcal": round(kcal, 1),
            "implied_by_macros_kcal": round(implied, 1), "difference_kcal": round(gap, 1),
            "note": ("the calories and the macros disagree — one of them is probably stated "
                     "per a different amount")}


def _given_macros(name: str, it: dict) -> dict | None:
    """The five macros off one item, or None if it carries none of them.

    All-or-nothing on purpose. `fibre_g` is not in the schema's `required` list, so an item
    giving four of five used to fall through to the catalog branch and be logged with STALE
    CATALOG MACROS, silently discarding the numbers the user had just read off a label —
    no error, no `unresolved`, nothing to notice. Partial input is a mistake to name, not
    a value to guess at.
    """
    present = [k for k in _MACROS if it.get(k) is not None]
    if not present:
        return None
    missing = [k for k in _MACROS if k not in present]
    if missing:
        raise ValueError(
            f"{name!r} gave {', '.join(present)} but not {', '.join(missing)}. Macros are "
            "all-or-nothing: pass all five (0 for any the user did not state), or pass none "
            "and let the catalog supply them. Nothing was logged."
        )
    return {k: float(it[k]) for k in _MACROS}


def _macros_reference(name: str, it: dict, amount: float, unit: str) -> float:
    """The amount the user's macros were stated per.

    Defaulting this to the eaten amount reads per-unit macros as a total, which halves the
    count when the stated value is per item.
    The reference is required whenever the quantity isn't 1, where "per one" and "per all of them"
    are different numbers and only the model knows which the user meant.
    """
    ref = it.get("macros_ref_qty")
    if ref is None:
        if amount == 1:
            return amount                    # per-one and per-all are the same number
        raise ValueError(
            f"{name!r} has user-given macros for {amount:g} {unit}, so macros_ref_qty is "
            f"required: pass {amount:g} if those numbers cover the whole amount, or the "
            "amount they were stated per (1 for per-item, 100 for a per-100g label). "
            "Nothing was logged."
        )
    ref = float(ref)
    if ref <= 0:
        raise ValueError(f"{name!r} has macros_ref_qty={ref:g}; it must be positive, and "
                         "0 is not the same as omitting it. Nothing was logged.")
    if unit == "each" and ref > _MAX_EACH_REFERENCE:
        # A per-100g label pinned to a counted food divides every future log by 100 — and
        # with `save`, freezes that into the catalog as "per 100 each".
        raise ValueError(
            f"{name!r} is counted in `each` but macros_ref_qty is {ref:g}. That reads as "
            f"'per {ref:g} items'. If those macros are per 100g, log this food in grams "
            "instead. Nothing was logged."
        )
    return ref


_MAX_EACH_REFERENCE = 12     # nobody states macros per a hundred slices


def log_food(date: str, meal: str | None = None, items: list[dict] | None = None) -> dict:
    # `meal` is optional (an unnamed meal logs as `unlabelled` and the bot asks), which
    # forces `items` to carry a default too — Python won't allow a required argument after
    # an optional one. The tool schema still requires `items`, so it is never actually absent.
    items = items or []
    conn = get_conn()
    resolved: dict[str, dict] = {}
    unresolved: list[dict] = []
    repeatable: set[str] = set()             # slugs the model explicitly marked as repeats
    checks: list[dict] = []                  # macros whose calories don't add up

    for it in items:
        name = it["name"]
        slug = _slug(name)
        amount = float(it["quantity"])
        unit = _norm_unit(it.get("unit", "each"))
        given = _given_macros(name, it)

        if given is not None:
            # Macros may be stated per a reference amount other than the one eaten
            # ("per 100g ... I ate 63g") — the scaling happens here, never in the model.
            ref_qty = _macros_reference(name, it, amount, unit)
            flag = _macro_check(name, given)
            if flag is not None:
                checks.append(flag)
            factor = amount / ref_qty
            macros = {k: v * factor for k, v in given.items()} if factor != 1 else given
            catalog_slug = None
            if it.get("save"):
                _save_catalog(name, ref_qty, unit, given)  # the stated reference portion, unscaled
                catalog_slug = slug
        else:
            cat = _lookup_catalog(conn, slug)
            if cat is None or _norm_unit(cat["ref_unit"]) != unit:
                unresolved.append({"name": name, "reason": _unresolved_reason(cat, unit)})
                continue
            factor = amount / cat["ref_qty"]
            macros = {k: cat[k] * factor for k in _MACROS}
            catalog_slug = cat["slug"]

        if it.get("allow_duplicate"):
            repeatable.add(slug)
        _accumulate(resolved, slug, name, amount, unit, macros, catalog_slug)

    # Resolved per item, not once for the call: a food with a fixed label takes it even
    # when the message names a meal, so one message can legitimately write two meals.
    now = writer.now()                        # the same clock writer.record stamps rows with
    logged, duplicates = [], []
    for slug, fields in resolved.items():
        row_meal = _resolve_meal(slug, meal)
        prior = (None if slug in repeatable else
                 _recent_duplicate(conn, date, slug, row_meal,
                                   fields["amount"], fields["amount_unit"], now))
        if prior is not None:
            # Refusing here rather than in the model is the point: the model cannot see
            # what it wrote last turn (history is text only), so it cannot know this is a
            # repeat. Reported, never silent — a genuine second helping comes back with
            # allow_duplicate.
            log.warning("skipped a duplicate %s logged %ss ago on %s",
                        slug, prior["seconds_ago"], date)
            duplicates.append({"food_name": fields["food_name"], "meal": row_meal,
                               "amount": fields["amount"], "amount_unit": fields["amount_unit"],
                               **prior})
            continue
        logged.append(writer.record(
            "food_log", {"date": date, "meal": row_meal, "estimated": 0, **fields}))
    # A committed log supersedes any pending estimate for the same food (e.g. the user
    # answered a proposal with real macros) — drop it so a later "yes" can't double-log.
    pend = _pending.get(get_context().reply_handle)
    if pend is not None:
        pend["rows"] = [r for r in pend["rows"] if r["food_slug"] not in resolved]
        if not pend["rows"]:
            _pending.pop(get_context().reply_handle, None)
    return {"logged": logged, "unresolved": unresolved,
            # Not an error: the food IS on the day already. The reply has to say so rather
            # than claim a second helping was logged, and re-call with the item's
            # `allow_duplicate` if the user really did eat it twice.
            "duplicate_skipped": duplicates,
            # Logged anyway — see _macro_check. The reply is what turns this into a
            # question the user can answer.
            "macro_check": checks,
            # Plural because the rows may not agree — a single value would be wrong on any
            # morning a breakfast list carries a supplement. Each row also states its own.
            "meals": sorted({r["meal"] for r in logged}),
            "needs_meal": any(r["meal"] == UNLABELLED_MEAL for r in logged),
            "day_totals": _status(_day_totals(conn, date), date)}


def propose_food_estimate(date: str, meal: str | None = None,
                          items: list[dict] | None = None) -> dict:
    # Does NOT write. Stash the estimate keyed by chat; confirm_pending commits it.
    items = items or []                      # see the note on log_food's signature
    ctx = get_context()
    rows = [{
        "food_slug": _slug(it["name"]), "food_name": it["name"],
        "amount": float(it["quantity"]), "amount_unit": _norm_unit(it.get("unit", "each")),
        "catalog_slug": None,
        **{k: float(it[k]) for k in _MACROS},
    } for it in items]
    # Two calls in ONE turn are a legitimate shape — the model splits a mixed dish, or
    # proposes a second food after logging a first — and this used to assign, so the
    # earlier food was destroyed while the reply named both and the user's "yes" committed
    # only the survivor. Merge instead; across turns the newer proposal still replaces the
    # old one, which is what "the chat has one slot" has always meant.
    prior = _pending.get(ctx.reply_handle)
    if prior is not None and prior["proposed_in"] is ctx:
        kept = [r for r in prior["rows"] if r["food_slug"] not in {n["food_slug"] for n in rows}]
        rows = kept + rows
        date, meal = prior["date"], meal if meal is not None else prior["meal"]
    _pending[ctx.reply_handle] = {
        "date": date, "meal": meal, "rows": rows, "original_raw": ctx.text or "",
        "proposed_in": ctx,  # identity of the proposing message; same turn ⇔ same object
        "proposed_at": ctx.timestamp,   # so an abandoned proposal goes stale, see _guard_fresh
    }
    checks = [c for c in (_macro_check(r["food_name"], r) for r in rows) if c]
    current = _day_totals(get_conn(), date)
    projected = {
        "date": date,
        **{k: round(current[k] + sum(r[k] for r in rows), 1) for k in _MACROS},
    }
    # A projection is not a log — the heading says so, so the block can't be mistaken for
    # what has actually been written.
    return {"pending": rows,
            "projected_day_totals": _status(projected, date,
                                            heading=targets.PROJECTION_HEADING),
            # An estimate whose own numbers don't add up is worth catching BEFORE the user
            # confirms it — that is the one moment they are looking at the figures.
            "macro_check": checks,
            "needs_confirmation": True}


def confirm_pending(meal: str | None = None, expect_food: str | None = None) -> dict:
    ctx = get_context()
    handle = ctx.reply_handle
    pend = _pending.get(handle)
    if pend is None:
        # A hard error, not a soft {"error": ...} result. Returned as data this read as
        # advisory and the model twice answered it with a confident "Logged:" plus an
        # invented progress block for a row that was never written. The harness turns a raise into is_error, which is the
        # same shape corrections.set_meal already uses for exactly this.
        raise ValueError(
            "Nothing is pending for this chat, so nothing was logged. An estimate is only "
            "staged by propose_food_estimate — if you showed the user numbers without "
            "calling it, call it now and let their NEXT message confirm."
        )
    # Freshness first: a proposal older than the conversation is dead whatever it was
    # aimed at, and saying so is more useful than complaining about the argument.
    _guard_fresh(handle, pend, ctx)
    _guard_expected_food(pend, expect_food)
    if pend["proposed_in"] is ctx and not _user_has_seen(pend, ctx):
        # A same-turn proposal must stay pending until the user sees and confirms it.
        raise ValueError(
            "This estimate was proposed by the CURRENT message and your previous reply did "
            "not show the user these numbers, so they have not seen them and nothing was "
            f"logged. Reply with the estimate ({_describe(pend)}) and ask them to confirm; "
            "call confirm_pending when their NEXT message does."
        )
    # Provenance. `raw` is normally the message that described the food, captured when the
    # estimate was staged. On the catch-up path the staging happened on the CONFIRMING
    # message, so that capture is "Yes please" — useless as a record of what was eaten.
    # The food was described in the turn before, which is what history hands back.
    raw = pend["original_raw"]
    if pend["proposed_in"] is ctx:
        raw = history.last_user_message(ctx) or raw
    _pending.pop(handle)
    _offer.pop(handle, None)             # a new commit supersedes any unanswered offer
    acc: dict[str, dict] = {}
    for r in pend["rows"]:
        _accumulate(acc, r["food_slug"], r["food_name"], r["amount"], r["amount_unit"],
                    r, r["catalog_slug"])
    # A meal named while confirming wins over the one the estimate was proposed under.
    # The user can correct the meal in the confirmation message.
    #
    # DEFAULT_MEALS deliberately does NOT apply here, though applying it in both paths for
    # symmetry is the obvious wrong move: this path exists for foods with no catalog entry,
    # so no default can match — and `meal` here is an explicit correction from the user,
    # which a default must never outrank.
    meal_label = normalise_meal(meal if meal is not None else pend["meal"])
    logged = [
        writer.record(
            "food_log",
            {"date": pend["date"], "meal": meal_label, "estimated": 1, **fields},
            raw=raw,                   # the food message, not the "yes"
        )
        for fields in acc.values()
    ]
    offer = _build_offer(handle, acc.values())
    return {"logged": logged,
            "meal": meal_label,
            "needs_meal": meal_label == UNLABELLED_MEAL and bool(logged),
            "catalog_offer": offer,
            "day_totals": _status(_day_totals(get_conn(), pend["date"]), pend["date"])}


def get_day_totals(date: str) -> dict:
    return _status(_day_totals(get_conn(), date), date)


def suggest_food(date: str | None = None) -> dict:
    """"What should I eat?" on demand, at any hour.

    `_status` only volunteers suggestions in the evening, because unprompted advice at
    9am is noise. Asking for them directly is a different thing, so this bypasses the
    clock gate — but runs the identical `targets.suggest`, so the ranking, the ceiling
    checks and the arithmetic stay in exactly one place.
    """
    conn = get_conn()
    date = date or get_context().timestamp.date().isoformat()
    consumed = _day_totals(conn, date)
    rem = targets.remaining(consumed)
    suggestions = targets.suggest(rem, _catalog_rows(conn))
    out = {
        "date": date,
        "consumed": consumed,
        "targets": targets.TARGETS,
        "remaining": rem,
        # Same block as every other reply: asking "what should I eat?" is a different
        # question from "what have I eaten?", but it is answered against the same day.
        "progress_block": targets.render_progress(rem),
        "suggestions": suggestions,
    }
    # Unlike `_status`, this bypasses the clock gate — so `suggest` may legitimately come
    # back with nothing to say (both floors met, no calorie room) and there is no block.
    if suggestions:
        out["suggestions_block"] = targets.render_suggestions(suggestions)
    return out


# --- catalog listing (backs the deterministic /catalog command; not a Claude tool) ---

def list_catalog() -> list[dict]:
    # Read-only, name-ordered dump of the whole catalog for the Telegram /catalog
    # command. Like get_day_totals it reads the projection directly and writes
    # nothing, so it needs no message context.
    return _catalog_rows(get_conn())


def _fmt_portion(qty: float, unit: str) -> str:
    return f"{qty:g}{'ea' if unit == 'each' else unit}"


def _fmt_g(value: float) -> str:
    """Grams, as short as stays honest: whole numbers bare, one decimal only below 10
    where a rounded gram would be a big relative error (0.4g protein is not 0g)."""
    if value == int(value):
        return str(int(value))
    return f"{value:.1f}" if value < 10 else str(round(value))


def render_catalog_table(rows: list[dict]) -> str:
    """Plain-text listing of the catalog, two lines per food: the name and its reference
    portion, then that portion's calories and macros. Channel-neutral (no markup) so it
    stays unit-testable; the adapter wraps it in a Telegram <pre> block.

    It was one fixed-width row per food, which needed ~49 characters and wrapped on an
    iPhone — the last column landed on the next line and the alignment fell apart. Names
    are the one thing that must survive, since logging a saved food means using its exact
    name, so the fix gives the name its own full-width line instead of truncating it into
    a column. Nothing is aligned across items now, so nothing can wrap out of alignment.
    """
    n = len(rows)
    lines = [f"Food catalog · {n} item{'' if n == 1 else 's'}", ""]
    for r in rows:
        lines.append(f"{r['name']} · {_fmt_portion(r['ref_qty'], r['ref_unit'])}")
        lines.append(
            f"  {round(r['kcal'])}kcal  "
            f"P{_fmt_g(r['protein_g'])} C{_fmt_g(r['carb_g'])} F{_fmt_g(r['fat_g'])} "
            f"fib{_fmt_g(r['fibre_g'])}"
        )
    return "\n".join(lines)


# --- projections (one idempotent upsert each; used by live ingest AND rebuild_db) ---

def project_catalog(conn: sqlite3.Connection, row: dict) -> None:
    # Default fields added after some rows were written, so replaying older JSONL
    # lines (which lack them) stays lossless. Real values in `row` win.
    row = {"notes": "", **row}
    conn.execute(
        "INSERT INTO food_catalog(slug, name, ref_qty, ref_unit, kcal, protein_g, carb_g, "
        "  fat_g, fibre_g, notes, raw, ingested_at, schema_version) "
        "VALUES (:slug, :name, :ref_qty, :ref_unit, :kcal, :protein_g, :carb_g, :fat_g, "
        "  :fibre_g, :notes, :raw, :ingested_at, :schema_version) "
        "ON CONFLICT(slug) DO UPDATE SET "
        "  name=excluded.name, ref_qty=excluded.ref_qty, ref_unit=excluded.ref_unit, "
        "  kcal=excluded.kcal, protein_g=excluded.protein_g, carb_g=excluded.carb_g, "
        "  fat_g=excluded.fat_g, fibre_g=excluded.fibre_g, notes=excluded.notes, "
        "  raw=excluded.raw, ingested_at=excluded.ingested_at, schema_version=excluded.schema_version",
        row,
    )


def project_log(conn: sqlite3.Connection, row: dict) -> None:
    conn.execute(
        "INSERT INTO food_log(date, meal, food_slug, food_name, amount, amount_unit, kcal, "
        "  protein_g, carb_g, fat_g, fibre_g, estimated, catalog_slug, raw, ingested_at, "
        "  schema_version) "
        "VALUES (:date, :meal, :food_slug, :food_name, :amount, :amount_unit, :kcal, "
        "  :protein_g, :carb_g, :fat_g, :fibre_g, :estimated, :catalog_slug, :raw, :ingested_at, "
        "  :schema_version) "
        "ON CONFLICT(date, meal, food_slug, ingested_at) DO UPDATE SET "
        "  food_name=excluded.food_name, amount=excluded.amount, amount_unit=excluded.amount_unit, "
        "  kcal=excluded.kcal, protein_g=excluded.protein_g, carb_g=excluded.carb_g, "
        "  fat_g=excluded.fat_g, fibre_g=excluded.fibre_g, estimated=excluded.estimated, "
        "  catalog_slug=excluded.catalog_slug, raw=excluded.raw, ingested_at=excluded.ingested_at, "
        "  schema_version=excluded.schema_version",
        row,
    )


# Ephemeral per-chat pending estimate (reply_handle -> proposal). In-process only;
# lost on restart, which is fine — the user just re-sends. Last proposal per chat wins.
_pending: dict[Any, dict] = {}

# Ephemeral per-chat "want to save that to the catalog?" offer, same lifetime rules.
# Losing it on restart costs nothing: the offer is a convenience, and the food is
# already logged either way.
_offer: dict[Any, dict] = {}


# A pending estimate goes stale on the same clock the conversation does. Before this it
# had no bound at all: an abandoned proposal sat in the slot indefinitely and the next
# unrelated "yes" committed it. Reusing history's constant rather than inventing a second
# one keeps "is this thread still live?" a single answer.
PENDING_EXPIRY = history.IDLE_EXPIRY


def _describe(pend: dict) -> str:
    """The pending rows as one short phrase, for an error the model has to act on."""
    return ", ".join(f"{r['amount']:g}{r['amount_unit']} {r['food_name']} "
                     f"({r['kcal']:g} kcal)" for r in pend["rows"])


# Words too generic to identify a food on their own: every other salad matches "salad".
# Overlap is measured on what's left, so "beetroot salad" and "pear salad" don't match.
_GENERIC_FOOD_WORDS = frozenset((
    "a", "and", "of", "the", "with", "in", "on", "salad", "bowl", "dish", "mix", "mixed",
    "plate", "portion", "serving", "style", "fresh", "home", "homemade", "small", "large",
))


def _food_tokens(name: str) -> frozenset[str]:
    return frozenset(_slug(name).split("_")) - _GENERIC_FOOD_WORDS


def _names_same_food(expect: str, pending_name: str) -> bool:
    """Is `expect` the same food as `pending_name`?

    Exact slug first. Failing that, a shared distinctive word is enough, so a shortened name can still identify a longer saved description.
    """
    if _slug(expect) == _slug(pending_name):
        return True
    wanted = _food_tokens(expect)
    return bool(wanted and wanted & _food_tokens(pending_name))


def _guard_expected_food(pend: dict, expect_food: str | None) -> None:
    """Refuse a confirmation aimed at a food that isn't the one pending.

    `confirm_pending` commits whatever occupies the chat's single slot, so a proposal the
    user walked away from is committed by the next "yes" meant for something else. Requiring the intended food turns a mismatched confirmation into a refusal.
    """
    if not expect_food:
        raise ValueError(
            f"expect_food is required and was not passed. The pending estimate is "
            f"{_describe(pend)} — nothing was logged. Call confirm_pending again naming the "
            "food you believe the user just confirmed, so a mismatch refuses instead of "
            "committing the wrong one."
        )
    if not any(_names_same_food(expect_food, r["food_name"]) for r in pend["rows"]):
        raise ValueError(
            f"The pending estimate is {_describe(pend)}, not {expect_food!r} — nothing was "
            "logged. If you meant a different food, propose_food_estimate it first; if you "
            "meant the pending one, confirm it by its own name."
        )


def _guard_fresh(handle: Any, pend: dict, ctx) -> None:
    """Drop and refuse a proposal older than the conversation that produced it."""
    if ctx.timestamp - pend["proposed_at"] > PENDING_EXPIRY:
        _pending.pop(handle, None)
        # The food is now unrecoverable from process state; the log is the only record
        # that it was ever offered, so an audit can tell "never eaten" from "lost".
        log.warning("dropped a stale pending estimate: %s", _describe(pend))
        raise ValueError(
            f"The pending estimate ({_describe(pend)}) is too old to confirm and has been "
            "dropped; nothing was logged. Propose it again if the user still wants it."
        )


def _user_has_seen(pend: dict, ctx) -> bool:
    """Did the previous reply actually put these numbers in front of the user?

    This is the property the same-turn guard is really defending — "the user has seen it" —
    rather than the proxy it used to test, "a different message proposed it". The proxy
    charges the user an extra round-trip whenever the model shows an estimate as prose and
    only calls propose_food_estimate after they have already agreed. Reading it off the recalled
    conversation keeps the answer runtime-authored: the model asserts nothing about itself.

    Every row must match, so one coincidental figure can't wave through a multi-item
    proposal, and a wholly zero-kcal estimate never matches — it has no distinctive number,
    and the ladder logs zeros directly rather than proposing them anyway.
    """
    reply = history.last_reply(ctx)
    if not reply:
        return False
    kcals = [round(r["kcal"]) for r in pend["rows"]]
    if not any(kcals):
        return False
    return all(re.search(rf"\b{k}\b", reply) for k in kcals)


# What a saved food should be stored per, by unit: weighed foods per 100 g/ml, counted
# foods per one. Anything else keeps the eaten amount.
_OFFER_REF_QTY = {"g": 100.0, "ml": 100.0, "each": 1.0}


def _offer_item(f: dict) -> dict:
    """Restate one committed food per the catalog's reference portion.

    The estimate is confirmed against what was eaten (150 g of mince, 2 slices of pizza),
    but that is a serving, not a reference: saved verbatim it fills the catalog with rows
    reading "per 150 g" and "per 2 each". Scaling downstream is proportional either way —
    this is so the catalog listing and the SAVE OFFER fragment read alike.
    """
    amount = f["amount"]
    ref_qty = _OFFER_REF_QTY.get(f["amount_unit"], amount)
    # A zero or negative amount can't be scaled; offer it as-is rather than divide by it.
    factor = ref_qty / amount if amount > 0 else 1.0
    if factor == 1.0:
        ref_qty = amount
    return {
        "name": f["food_name"],
        "ref_qty": ref_qty,
        "ref_unit": f["amount_unit"],
        **{k: round(f[k] * factor, 2) for k in _MACROS},
    }


def _build_offer(handle: Any, committed) -> list[dict] | None:
    """After an estimate commits, work out which of its foods aren't saved yet.

    An estimated food is exactly the food worth catalogueing — it cost a round-trip to
    guess, and guessing it again next week would cost another. The macros offered are the
    ones just confirmed, restated per the catalog's own reference portion, so accepting is
    one word. The user can also answer with better numbers off a label.
    """
    conn = get_conn()
    items = [
        _offer_item(f)
        for f in committed
        if _lookup_catalog(conn, f["food_slug"]) is None
    ]
    if not items:
        _offer.pop(handle, None)
        return None
    _offer[handle] = {"items": items}
    return items


def _clear_offer(slug: str) -> None:
    """Drop a saved food from every chat's standing offer. Keyed by slug rather than by
    chat because the executor that saves it doesn't know which offer prompted it."""
    for handle, offer in list(_offer.items()):
        offer["items"] = [i for i in offer["items"] if _slug(i["name"]) != slug]
        if not offer["items"]:
            _offer.pop(handle, None)


def _pending_section(msg) -> str | None:
    """Tell Claude this chat has an estimate awaiting confirmation, and exactly what it
    holds. The harness now recalls recent turns (core/history.py), but that transcript is
    lossy for this purpose: it expires, it's capped, and a "yes" may arrive after either
    bound. This states the pending rows outright so confirm_pending never depends on the
    proposal still being in the recalled window."""
    pend = _pending.get(msg.reply_handle)
    # An expired proposal is not actionable, so announcing it would only invite a
    # confirmation that _guard_fresh then refuses.
    if not pend or msg.timestamp - pend["proposed_at"] > PENDING_EXPIRY:
        return None
    items = ", ".join(
        f"{r['amount']:g}{r['amount_unit']} {r['food_name']} "
        f"({r['kcal']:g} kcal, {r['protein_g']:g}g protein)"
        for r in pend["rows"]
    )
    meal = pend["meal"] or "(no meal named)"
    return (
        f"PENDING: a {meal} food estimate is awaiting the user's confirmation — "
        f"{items}. If this message confirms it (e.g. 'yes', 'looks right'), call "
        "confirm_pending, passing expect_food so a mismatch is caught. If it confirms it "
        "but names a different meal ('log it for lunch'), pass that as confirm_pending's "
        "`meal` — that is the whole change, not a reason to re-propose. If it amends the "
        "NUMBERS or the amount, call propose_food_estimate again with the corrected values. "
        "If it does NONE of those — the user has moved on to something else — say in ONE "
        "short line at the end of your reply that this food is still NOT logged and you "
        "need a yes. An unanswered estimate is written nowhere and expires silently."
    )


_UNLABELLED_DAYS = 2        # how many recent unlabelled days to spell out
_UNLABELLED_ITEMS = 8       # per day, before summarising the rest as a count


def _unlabelled_section() -> str | None:
    """Name the food rows still waiting for a meal label, so the answer to "which meal?"
    lands on a turn that knows what was asked. Bounded: if a question goes unanswered for
    days this must not grow without limit, and only the recent days are actionable anyway.
    """
    rows = get_conn().execute(
        "SELECT date, food_name, amount, amount_unit FROM food_log "
        "WHERE meal = ? ORDER BY date DESC, ingested_at",
        (UNLABELLED_MEAL,),
    ).fetchall()
    if not rows:
        return None
    by_date: dict[str, list[str]] = {}
    for r in rows:
        by_date.setdefault(r["date"], []).append(
            f"{r['amount']:g}{r['amount_unit']} {r['food_name']}")
    shown = list(by_date)[:_UNLABELLED_DAYS]
    days = []
    for d in shown:
        items = by_date[d]
        listed = ", ".join(items[:_UNLABELLED_ITEMS])
        if len(items) > _UNLABELLED_ITEMS:
            listed += f", and {len(items) - _UNLABELLED_ITEMS} more"
        days.append(f"{d}: {listed}")
    more = len(by_date) - len(shown)
    tail = f" (plus {more} earlier day{'s' if more > 1 else ''})" if more else ""
    return (
        "UNLABELLED: these logged foods are still waiting for a meal — "
        + "; ".join(days) + tail + ". If this message answers which meal they were "
        "(even just 'breakfast'), call set_meal with that date and label. If it names a "
        "meal for only some of them, ask which day you should apply it to rather than "
        "guessing across days."
    )


def _offer_section(msg) -> str | None:
    """Carry the standing save-offer, with the exact numbers to save, so "yes" is
    actionable on a later turn without re-deriving anything."""
    offer = _offer.get(msg.reply_handle)
    if not offer:
        return None
    items = "; ".join(
        f"{o['name']} — per {o['ref_qty']:g}{o['ref_unit']}: {o['kcal']:g} kcal, "
        f"{o['protein_g']:g}g protein, {o['carb_g']:g}g carbs, {o['fat_g']:g}g fat, "
        f"{o['fibre_g']:g}g fibre"
        for o in offer["items"]
    )
    return (
        "SAVE OFFER: these were logged as estimates and are NOT in the catalog — "
        + items + ". If this message agrees to save (e.g. 'yes', 'save it'), call "
        "save_food with those numbers. If it supplies better ones (off a wrapper or a "
        "site), save THOSE instead, per whatever portion the user states. If it declines "
        "or moves on, drop it and don't ask again."
    )


def log_system_context(msg) -> str | None:
    """The food log's one system-prompt slot, carrying every piece of cross-turn state it
    needs: an estimate awaiting a "yes", rows awaiting a meal label, and a standing offer
    to save an estimated food. `Feature` allows a single fragment per feature, so these
    compose here rather than in the core."""
    sections = [s for s in (_pending_section(msg), _unlabelled_section(),
                            _offer_section(msg)) if s]
    return "\n".join(sections) or None


def catalog_system_context(msg) -> str | None:
    """List the user's saved foods so Claude logs them by their EXACT catalog name
    (and a compatible unit) instead of estimating. Without this, Claude can't know
    "cheddar" means the saved "cheddar cheese", and a near-miss name falls straight
    through to an estimate. An ambiguous mention is resolved by PROPOSING a reading the
    user can correct, never by asking: a question spends a round-trip and its answer
    lands on a turn that has no memory of what was asked.
    Rebuilt from the catalog every turn — cheap at personal-catalog scale."""
    rows = get_conn().execute(
        "SELECT name, ref_qty, ref_unit FROM food_catalog ORDER BY name"
    ).fetchall()
    if not rows:
        return None
    lines = "\n".join(f"- {r['name']} — per {r['ref_qty']:g} {r['ref_unit']}" for r in rows)
    return (
        "SAVED FOODS (the user's catalog — macros are stored for each). When the user "
        "mentions one, even loosely (e.g. 'cheddar' -> 'cheddar cheese', 'a slice of "
        "sourdough' -> 'sourdough', '1 can tuna' -> 'tuna'), log it with log_food using "
        "its EXACT name below and a unit compatible with how it's measured, so the stored "
        "macros are used instead of estimating. If a mention could match more than one of "
        "these (e.g. two salmon fillets), or you're unsure whether a saved food is meant, "
        "pick the most likely reading and log or propose it — say which one you assumed so "
        "the user can correct you. Ask only when the candidates differ enough to matter "
        "(a 90 kcal gap, not a 5 kcal one); otherwise log the likeliest.\n" + lines
    )


CATALOG_TOOLS = [{
    "name": "save_food",
    "description": (
        "Save or update a reusable food in the user's catalog, with macros stated PER a "
        "reference portion (e.g. per 100 g, per 1 each). Use only when the user explicitly "
        "wants to remember a food for reuse (e.g. 'save oats: 380 kcal, 13g protein, 67g carb, "
        "7g fat, 10g fibre per 100g'). This records nothing about what they ate. Pass the user's "
        "numbers as given — do not do arithmetic."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Display name, e.g. 'oats'."},
            "ref_qty": {"type": "number", "description": "Reference portion size, e.g. 100 for per-100g, 1 for per-item."},
            "ref_unit": {"type": "string", "enum": ["g", "ml", "each"], "description": "g for mass, ml for volume, each for countable items."},
            "kcal": {"type": "number"},
            "protein_g": {"type": "number"},
            "carb_g": {"type": "number"},
            "fat_g": {"type": "number"},
            "fibre_g": {"type": "number", "description": "Grams of dietary fibre per portion; pass 0 if the label doesn't list it."},
            "notes": {"type": "string", "description": "Optional free-text detail worth remembering for later analysis: composition (e.g. which nuts in 'mixed nuts'), brand, or prep (e.g. 'in brine, drained'). Omit if none."},
        },
        "required": ["name", "ref_qty", "ref_unit", "kcal", "protein_g", "carb_g", "fat_g", "fibre_g"],
    },
}]

_LOG_ITEM_PROPS = {
    "name": {"type": "string"},
    "quantity": {"type": "number", "description": "Amount consumed, in `unit`."},
    "unit": {"type": "string", "enum": ["g", "ml", "each"], "description": "g for mass, ml for volume, each for countable items."},
}

LOG_TOOLS = [
    {
        "name": "log_food",
        "description": (
            "Log food the user ate, for foods that are EITHER already in their saved catalog "
            "(pass just name + quantity + unit; the system fills in and scales the macros) OR "
            "for which the user gave macros explicitly (pass those macros on the item). Each call "
            "is a distinct eating event and ADDS to the day — logging the same food again (e.g. a "
            "second helping) creates a new entry, it does not overwrite the earlier one. "
            "Do NOT use this for a food you would have to estimate yourself with no catalog entry "
            "and no user-given macros — use propose_food_estimate instead. Any item that couldn't be "
            "resolved from the catalog comes back in `unresolved` as {name, reason}; do what the "
            "reason says (estimate and propose it, or re-log it with a compatible amount) rather "
            "than asking the user about it. An item identical to one logged moments ago is NOT "
            "written again and comes back in `duplicate_skipped` — say it was already on the day "
            "rather than claiming you logged it twice; only re-send it with `allow_duplicate` if "
            "the user says they really ate it again. "
            "Prefer the user's SAVED FOODS: if a mention matches one, pass its exact catalog "
            "name (and a compatible unit) so its stored macros are used rather than estimated. "
            "If a food's calories disagree with its own macros it is still logged, and comes "
            "back in `macro_check` — that is usually one figure stated per a different amount, "
            "and it is worth your one question. "
            "Never do arithmetic — pass quantities and any user-given macros verbatim. If the "
            "user stated the macros per a reference amount different from what they ate (e.g. "
            "'per 100g ... I ate 63g'), also pass that reference amount as `macros_ref_qty` — "
            "the system scales the macros to the eaten quantity. Set an "
            "item's `save` to true only if the user asked to remember that food."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "ISO date YYYY-MM-DD. Today if unspecified."},
                "meal": {"type": "string", "description": "The meal the user named: breakfast, lunch, dinner, snack — or anything else they call it ('second breakfast'). OMIT this entirely if they did not name one: do NOT guess it from the time of day. An omitted meal logs as 'unlabelled' and you then ask which meal it was. Pass the label exactly as given; repeats are not numbered, so two 'breakfast' messages are one breakfast."},
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            **_LOG_ITEM_PROPS,
                            "kcal": {"type": "number", "description": "Only if the user gave this food's macros."},
                            "protein_g": {"type": "number"},
                            "carb_g": {"type": "number"},
                            "fat_g": {"type": "number"},
                            "fibre_g": {"type": "number", "description": "Grams of dietary fibre. Macros are ALL-OR-NOTHING: if you pass any of kcal/protein_g/carb_g/fat_g you must pass all five, using 0 for any the user did not state. A partial set is refused, because it used to fall through to the catalog and silently discard the user's own numbers."},
                            "macros_ref_qty": {"type": "number", "description": "With user-given macros, the amount (in `unit`) those macros were stated per: 100 for a 'per 100g' label, 1 for per-item, or `quantity` itself when they cover the whole portion. REQUIRED whenever quantity is not 1 — 'two lattes, 83 kcal' means something different per-latte than in total, and guessing halved the count on real logs."},
                            "save": {"type": "boolean", "description": "True only if the user asked to remember this food; the macro reference portion (macros_ref_qty if given, else quantity) becomes its catalog reference portion."},
                            "allow_duplicate": {"type": "boolean", "description": "Only when the user genuinely ate the same food, same amount, same meal AGAIN within a couple of minutes and says so. Identical rows that close together are otherwise skipped and reported in `duplicate_skipped`, because they are almost always this tool being called twice for one message."},
                        },
                        "required": ["name", "quantity", "unit"],
                    },
                },
            },
            "required": ["date", "items"],
        },
    },
    {
        "name": "propose_food_estimate",
        "description": (
            "Use when the user ate something NOT in their catalog and did NOT give macros, so you "
            "must estimate. Do NOT estimate a food that likely matches one of the user's SAVED "
            "FOODS — log it by its catalog name via log_food, or ask to clarify if unsure which. "
            "This is the ONLY way to put an estimate in front of the user: it stages the "
            "numbers so their next message's 'yes' has something to commit. CALL IT BEFORE you "
            "write the estimate into your reply, every time — including when their own message "
            "already says 'estimate and log it'. If you write macro numbers into a reply without "
            "calling this first, nothing is staged: their 'yes' finds nothing to confirm and the "
            "food is never logged. It writes no row itself; confirm_pending does that once they "
            "confirm (e.g. 'yes', 'looks right'). Give your best per-item macro estimate. "
            "This is also the ONLY route for macros you looked up with web_search — a looked-up "
            "number is still an estimate the user has not seen; say where it came from when you "
            "show it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "ISO date YYYY-MM-DD. Today if unspecified."},
                "meal": {"type": "string", "description": "The meal the user named: breakfast, lunch, dinner, snack — or anything else they call it ('second breakfast'). OMIT this entirely if they did not name one: do NOT guess it from the time of day. An omitted meal logs as 'unlabelled' and you then ask which meal it was. Pass the label exactly as given; repeats are not numbered, so two 'breakfast' messages are one breakfast."},
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            **_LOG_ITEM_PROPS,
                            "kcal": {"type": "number"},
                            "protein_g": {"type": "number"},
                            "carb_g": {"type": "number"},
                            "fat_g": {"type": "number"},
                            "fibre_g": {"type": "number", "description": "Grams of dietary fibre; estimate it too (0 if negligible)."},
                        },
                        "required": ["name", "quantity", "unit", "kcal", "protein_g", "carb_g", "fat_g", "fibre_g"],
                    },
                },
            },
            "required": ["date", "items"],
        },
    },
    {
        "name": "confirm_pending",
        "description": (
            "Commit the food estimate currently pending for this chat. Call this ONLY when a "
            "LATER user message confirms it — never in the same turn that proposed it, unless "
            "your PREVIOUS reply already showed the user these exact numbers and this message "
            "agrees to them. A same-turn call the user has not seen is refused and logs nothing. "
            "You MUST pass expect_food naming what you are confirming, and pass `meal` if the "
            "user named one while confirming. The logged rows are marked as estimated."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "meal": {"type": "string", "description": "Only if the user names a meal when confirming, or corrects the one the estimate was proposed under ('log it for lunch'). Overrides the pending estimate's meal; omit to keep it. Pass the label exactly as given; repeats are not numbered, so two 'breakfast' messages are one breakfast."},
                "expect_food": {"type": "string", "description": "REQUIRED. The food you believe you are confirming, as you named it in propose_food_estimate. A bare confirmation commits whatever happens to be pending, which has repeatedly logged a food the user never approved; naming it means a mismatch is refused instead."},
            },
            "required": ["expect_food"],
        },
    },
    {
        "name": "get_day_totals",
        "description": (
            "Return total kcal and macros logged for a day, the user's daily targets (calorie "
            "and fat ceilings, protein and fibre floors, carbs tracked) with how much of each "
            "remains, and — in the evening — concrete catalog-food suggestions to close the "
            "day's gaps. Call this after logging food (or when the user asks where they're at). "
            "`progress_block` and `suggestions_block` come back already written out; your reply "
            "reproduces them verbatim rather than rephrasing the numbers."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"date": {"type": "string", "description": "ISO date YYYY-MM-DD."}},
            "required": ["date"],
        },
    },
    {
        "name": "suggest_food",
        "description": (
            "Answer 'what should I eat?' / 'what's left?' / 'give me something high-protein' at "
            "ANY time of day. Returns the same `suggestions_block` get_day_totals gives in the "
            "evening, but without waiting for the evening. Every option is already sized and "
            "checked against the room left on calories, fat and carbs — reproduce the block as "
            "given and never work out a portion or a macro total yourself."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"date": {"type": "string", "description": "ISO date YYYY-MM-DD. Defaults to today."}},
            "required": [],
        },
    },
]


CATALOG_FEATURE = Feature(
    name="food_catalog",
    tools=CATALOG_TOOLS,
    executors={"save_food": save_food},
    schema=CATALOG_SCHEMA,
    domain="food_catalog",
    dedupe_key=("slug",),
    project=project_catalog,
    system_context=catalog_system_context,  # surface saved foods so Claude logs by exact name
)

LOG_FEATURE = Feature(
    name="food_log",
    tools=LOG_TOOLS,
    executors={
        "log_food": log_food,
        "propose_food_estimate": propose_food_estimate,
        "confirm_pending": confirm_pending,
        "get_day_totals": get_day_totals,
        "suggest_food": suggest_food,
    },
    schema=LOG_SCHEMA,
    domain="food_log",
    dedupe_key=("date", "meal", "food_slug", "ingested_at"),
    project=project_log,
    # targets.py renders these; the reply reproduces them. Declaring them here is what
    # makes that enforceable in the harness instead of only asked for in the prompt.
    verbatim_blocks=("progress_block", "suggestions_block"),
    # What a line of those blocks looks like, so the harness can also catch a block the
    # model typed out when NO tool returned one — the case verbatim_blocks cannot see.
    block_signature=targets.BLOCK_LINE,
    system_context=log_system_context,
)
