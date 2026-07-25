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
It is stashed in an ephemeral per-chat pending slot and only committed when the user
confirms — confirm_pending refuses a confirmation from the same message that proposed
(the model once chained propose → confirm in one turn and committed an estimate the user
never saw). Catalog hits and user-supplied macros auto-commit, as everything else does.

Known hole, and it bites harder now the bot is forbidden to ask before logging: the gate
keys on estimated *macros*, not estimated *amounts*. A catalog hit whose quantity Claude
guessed ("chickpeas" with no weight) auto-commits, and food_log has no correction path —
a later "make it 40g" adds a row rather than replacing one. See ROADMAP.md.
"""
from __future__ import annotations

import re
import sqlite3
from typing import Any

from health.core.contracts import Feature, get_context
from health.features import targets
from health.store import writer
from health.store.connection import get_conn

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
    return _UNIT_ALIASES.get((unit or "").strip().lower(), (unit or "").strip().lower())


def _lookup_catalog(conn: sqlite3.Connection, slug: str) -> dict | None:
    row = conn.execute(
        "SELECT slug, name, ref_qty, ref_unit, kcal, protein_g, carb_g, fat_g, fibre_g "
        "FROM food_catalog WHERE slug = ?",
        (slug,),
    ).fetchone()
    return dict(row) if row is not None else None


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


def _status(consumed: dict, date: str) -> dict:
    """Enrich consumed sums with targets + remaining, and — only in the evening, for
    today — deterministic catalog suggestions to close the day's gaps. The time gate
    reads the in-flight message timestamp so Claude never reasons about the clock."""
    out = {**consumed, "targets": targets.TARGETS, "remaining": targets.remaining(consumed)}
    now = get_context().timestamp
    if date == now.date().isoformat() and now.hour >= targets.SUGGEST_AFTER_HOUR:
        suggestions = targets.suggest(out["remaining"], _catalog_rows(get_conn()))
        if suggestions:
            out["suggestions"] = suggestions
    return out


_NUMBERED_MEALS = ("breakfast", "lunch", "dinner")


def _occasion_label(conn: sqlite3.Connection, date: str, meal: str) -> str:
    """Give each logging occasion a stable label. One log call = one occasion (all its
    items share the label). Small/standalone items land in a shared `snack` bucket;
    a main meal (breakfast/lunch/dinner) is numbered per day so a second breakfast reads
    as `breakfast-2` rather than silently merging into the first. Uniqueness of rows is
    guaranteed by `ingested_at` in the key regardless — the number is for readability."""
    base = (meal or "").strip().lower()
    if base not in _NUMBERED_MEALS:
        return base or "snack"
    n = conn.execute(
        "SELECT COUNT(DISTINCT meal) FROM food_log WHERE date = ? AND (meal = ? OR meal LIKE ?)",
        (date, base, base + "-%"),
    ).fetchone()[0]
    return f"{base}-{n + 1}"


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


def log_food(date: str, meal: str, items: list[dict]) -> dict:
    conn = get_conn()
    resolved: dict[str, dict] = {}
    unresolved: list[dict] = []

    for it in items:
        name = it["name"]
        slug = _slug(name)
        amount = float(it["quantity"])
        unit = _norm_unit(it.get("unit", "each"))
        has_macros = all(it.get(k) is not None for k in _MACROS)

        if has_macros:
            given = {k: float(it[k]) for k in _MACROS}
            # Macros may be stated per a reference amount other than the one eaten
            # ("per 100g ... I ate 63g") — the scaling happens here, never in the model.
            ref_qty = float(it.get("macros_ref_qty") or amount)
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

        _accumulate(resolved, slug, name, amount, unit, macros, catalog_slug)

    meal_label = _occasion_label(conn, date, meal) if resolved else meal
    logged = [
        writer.record("food_log", {"date": date, "meal": meal_label, "estimated": 0, **fields})
        for fields in resolved.values()
    ]
    # A committed log supersedes any pending estimate for the same food (e.g. the user
    # answered a proposal with real macros) — drop it so a later "yes" can't double-log.
    pend = _pending.get(get_context().reply_handle)
    if pend is not None:
        pend["rows"] = [r for r in pend["rows"] if r["food_slug"] not in resolved]
        if not pend["rows"]:
            _pending.pop(get_context().reply_handle, None)
    return {"logged": logged, "unresolved": unresolved,
            "day_totals": _status(_day_totals(conn, date), date)}


def propose_food_estimate(date: str, meal: str, items: list[dict]) -> dict:
    # Does NOT write. Stash the estimate keyed by chat; confirm_pending commits it.
    ctx = get_context()
    rows = [{
        "food_slug": _slug(it["name"]), "food_name": it["name"],
        "amount": float(it["quantity"]), "amount_unit": _norm_unit(it.get("unit", "each")),
        "catalog_slug": None,
        **{k: float(it[k]) for k in _MACROS},
    } for it in items]
    _pending[ctx.reply_handle] = {
        "date": date, "meal": meal, "rows": rows, "original_raw": ctx.text or "",
        "proposed_in": ctx,  # identity of the proposing message; same turn ⇔ same object
    }
    current = _day_totals(get_conn(), date)
    projected = {
        "date": date,
        **{k: round(current[k] + sum(r[k] for r in rows), 1) for k in _MACROS},
    }
    return {"pending": rows, "projected_day_totals": _status(projected, date),
            "needs_confirmation": True}


def confirm_pending() -> dict:
    handle = get_context().reply_handle
    pend = _pending.get(handle)
    if pend is None:
        return {"error": "Nothing pending to confirm."}
    if pend["proposed_in"] is get_context():
        # Confirmation must come from the USER, in a later message. On 2026-07-25 the
        # model chained propose → confirm inside one turn and committed an estimate the
        # user never saw (a duplicate 200g beef mince). Refuse — and keep the pending
        # alive so the user's real "yes" next turn still lands.
        return {
            "error": "This estimate was proposed by the CURRENT message — the user has "
                     "not seen it yet and nothing was logged. Reply with the estimate and "
                     "ask them to confirm; call confirm_pending only when a LATER message "
                     "from the user confirms it.",
            "pending": pend["rows"],
        }
    _pending.pop(handle)
    acc: dict[str, dict] = {}
    for r in pend["rows"]:
        _accumulate(acc, r["food_slug"], r["food_name"], r["amount"], r["amount_unit"],
                    r, r["catalog_slug"])
    meal_label = _occasion_label(get_conn(), pend["date"], pend["meal"])
    logged = [
        writer.record(
            "food_log",
            {"date": pend["date"], "meal": meal_label, "estimated": 1, **fields},
            raw=pend["original_raw"],  # the food message, not the "yes"
        )
        for fields in acc.values()
    ]
    return {"logged": logged,
            "day_totals": _status(_day_totals(get_conn(), pend["date"]), pend["date"])}


def get_day_totals(date: str) -> dict:
    return _status(_day_totals(get_conn(), date), date)


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


def pending_system_context(msg) -> str | None:
    """Tell Claude this chat has an estimate awaiting confirmation, and exactly what it
    holds. The harness now recalls recent turns (core/history.py), but that transcript is
    lossy for this purpose: it expires, it's capped, and a "yes" may arrive after either
    bound. This states the pending rows outright so confirm_pending never depends on the
    proposal still being in the recalled window."""
    pend = _pending.get(msg.reply_handle)
    if not pend:
        return None
    items = ", ".join(
        f"{r['amount']:g}{r['amount_unit']} {r['food_name']} "
        f"({r['kcal']:g} kcal, {r['protein_g']:g}g protein)"
        for r in pend["rows"]
    )
    return (
        f"PENDING: a {pend['meal']} food estimate is awaiting the user's confirmation — "
        f"{items}. If this message confirms it (e.g. 'yes', 'looks right'), call "
        "confirm_pending. If it amends the estimate, call propose_food_estimate again "
        "with the corrected values."
    )


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
            "than asking the user about it. "
            "Prefer the user's SAVED FOODS: if a mention matches one, pass its exact catalog "
            "name (and a compatible unit) so its stored macros are used rather than estimated. "
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
                "meal": {"type": "string", "description": "Base meal label: breakfast, lunch, dinner, or snack. If the user names a meal use it; otherwise infer from the current time of day, but use 'snack' for a small/standalone item (a latte, a coffee, a single piece of fruit). Pass the plain base label only — the system numbers repeats itself (breakfast-1, breakfast-2); never send a numbered label."},
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
                            "fibre_g": {"type": "number", "description": "Grams of dietary fibre; include with the other macros when the user gave them (0 if not stated)."},
                            "macros_ref_qty": {"type": "number", "description": "Only with user-given macros: the reference amount (in `unit`) the macros were stated per, when it differs from `quantity` (e.g. 100 for 'per 100g' when they ate 63g). Omit when the macros describe the eaten amount."},
                            "save": {"type": "boolean", "description": "True only if the user asked to remember this food; the macro reference portion (macros_ref_qty if given, else quantity) becomes its catalog reference portion."},
                        },
                        "required": ["name", "quantity", "unit"],
                    },
                },
            },
            "required": ["date", "meal", "items"],
        },
    },
    {
        "name": "propose_food_estimate",
        "description": (
            "Use when the user ate something NOT in their catalog and did NOT give macros, so you "
            "must estimate. Do NOT estimate a food that likely matches one of the user's SAVED "
            "FOODS — log it by its catalog name via log_food, or ask to clarify if unsure which. "
            "This SAVES NOTHING — it returns your estimate for you to show the user "
            "and ask them to confirm. Only after they confirm (e.g. 'yes', 'looks right') do you "
            "call confirm_pending to actually log it. Give your best per-item macro estimate. "
            "This is also the ONLY route for macros you looked up with web_search — a looked-up "
            "number is still an estimate the user has not seen; say where it came from when you "
            "show it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "ISO date YYYY-MM-DD. Today if unspecified."},
                "meal": {"type": "string", "description": "Base meal label: breakfast, lunch, dinner, or snack. If the user names a meal use it; otherwise infer from the current time of day, but use 'snack' for a small/standalone item (a latte, a coffee, a single piece of fruit). Pass the plain base label only — the system numbers repeats itself (breakfast-1, breakfast-2); never send a numbered label."},
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
            "required": ["date", "meal", "items"],
        },
    },
    {
        "name": "confirm_pending",
        "description": (
            "Commit the most recently proposed food estimate for this chat. Call this ONLY when a "
            "LATER user message confirms a pending estimate — never in the same turn that proposed "
            "it (the proposal must reach the user first; a same-turn call is refused and logs "
            "nothing). The logged rows are marked as estimated."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_day_totals",
        "description": (
            "Return total kcal and macros logged for a day, the user's daily targets (calorie "
            "ceiling, protein and fibre floors) with how much of each remains, and — in the "
            "evening — concrete catalog-food `suggestions` to close the day's gaps. Call this "
            "after logging food (or when the user asks where they're at) so your reply can report "
            "progress toward all three targets and weave in any suggestions."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"date": {"type": "string", "description": "ISO date YYYY-MM-DD."}},
            "required": ["date"],
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
    },
    schema=LOG_SCHEMA,
    domain="food_log",
    dedupe_key=("date", "meal", "food_slug", "ingested_at"),
    project=project_log,
    system_context=pending_system_context,
)
