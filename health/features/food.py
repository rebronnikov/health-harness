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

Estimates are gated: a food we have to estimate is NOT written on the spot. It is
stashed in an ephemeral per-chat pending slot and only committed when the user
confirms (their "yes" lands on a later, context-free turn — the harness stays
stateless; the tiny bit of state lives here). Catalog hits and user-supplied macros
auto-commit, as everything else in the system does.
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
    # Full macros for every saved food — what targets.suggest sizes portions from.
    rows = conn.execute(
        "SELECT name, ref_qty, ref_unit, kcal, protein_g, carb_g, fat_g, fibre_g, notes "
        "FROM food_catalog"
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


def log_food(date: str, meal: str, items: list[dict]) -> dict:
    conn = get_conn()
    resolved: dict[str, dict] = {}
    unresolved: list[str] = []

    for it in items:
        name = it["name"]
        slug = _slug(name)
        amount = float(it["quantity"])
        unit = _norm_unit(it.get("unit", "each"))
        has_macros = all(it.get(k) is not None for k in _MACROS)

        if has_macros:
            macros = {k: float(it[k]) for k in _MACROS}
            catalog_slug = None
            if it.get("save"):
                _save_catalog(name, amount, unit, macros)  # this portion becomes the reference
                catalog_slug = slug
        else:
            cat = _lookup_catalog(conn, slug)
            if cat is None or _norm_unit(cat["ref_unit"]) != unit:
                unresolved.append(name)  # unknown food, or unit family mismatch -> let Claude estimate
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
    }
    current = _day_totals(get_conn(), date)
    projected = {
        "date": date,
        **{k: round(current[k] + sum(r[k] for r in rows), 1) for k in _MACROS},
    }
    return {"pending": rows, "projected_day_totals": _status(projected, date),
            "needs_confirmation": True}


def confirm_pending() -> dict:
    pend = _pending.pop(get_context().reply_handle, None)
    if pend is None:
        return {"error": "Nothing pending to confirm."}
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
    """Tell Claude this chat has an estimate awaiting confirmation. Without this, a
    bare "yes" lands on a context-free turn and Claude has no reason to call
    confirm_pending — the harness keeps no history. This surfaces just that one bit."""
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
    through to an estimate. Ambiguous mentions should be clarified, not guessed.
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
        "ASK a short clarifying question instead of calling log_food or "
        "propose_food_estimate.\n" + lines
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
            "and no user-given macros — use propose_food_estimate instead. Any item that isn't in "
            "the catalog (and had no macros) comes back in `unresolved`; estimate those and propose "
            "them. Prefer the user's SAVED FOODS: if a mention matches one, pass its exact catalog "
            "name (and a compatible unit) so its stored macros are used rather than estimated. "
            "Never do arithmetic — pass quantities and any user-given macros verbatim. Set an "
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
                            "save": {"type": "boolean", "description": "True only if the user asked to remember this food; quantity+unit then become its catalog reference portion."},
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
            "call confirm_pending to actually log it. Give your best per-item macro estimate."
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
            "Commit the most recently proposed food estimate for this chat. Call this when the user "
            "confirms a pending estimate. The logged rows are marked as estimated."
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
