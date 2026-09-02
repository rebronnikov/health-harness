"""Corrections — the misparse fixer, and the bot's read tool over what it logged.

Everything else here is append-only and dedupe-keyed, so writing a row again overwrites
it. That has no answer for "delete that" or "the chickpeas were 40g, not 80" on
`food_log`, whose key includes `ingested_at` precisely so a second helping ADDS a row.
This feature is that answer, and it stays inside the same discipline: an edit or a delete
is itself an append-only JSONL event, never a mutation of a prior line. It owns no table
— `data/corrections.jsonl` IS the audit trail.

The projection is where it differs, and this is the one sanctioned exception to
"`project()` is an upsert": it applies an UPDATE or DELETE to the *target* feature's
table. Still idempotent — applying the same correction twice converges.

Replay ordering is the subtle part, and it needs two things.

1. This feature MUST be LAST in `registry.FEATURES`. `rebuild_db` replays domains in list
   order, so a correction has to land after the rows it corrects.
2. That alone isn't enough for overwrite domains. Live, "log 9000 steps -> correct to
   9500 -> re-log 9200" ends at 9200; a naive replay would apply the correction after
   both writes and end at 9500. So every event records the target row's `ingested_at`,
   and every statement matches on it. Once the target has been overwritten by a later
   write it carries a new `ingested_at`, the correction matches nothing, and it replays
   as the no-op it effectively was. For the same reason an edit never touches
   `ingested_at` or any dedupe-key column, so chained edits converge in JSONL order.

Arithmetic boundary, as everywhere: Claude names the field and the new value; scaling a
food row's macros to a corrected amount happens here, and the resolved numbers are frozen
into the event so a rebuild replays them rather than recomputing.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date as _date
from datetime import timedelta
from typing import Any

from health.core.contracts import Feature, get_context
from health.features.food import UNLABELLED_MEAL, get_day_totals, normalise_meal
from health.store import writer
from health.store.connection import get_conn

# Stamped by writer.record() from the runtime — never editable through a correction.
_PROVENANCE = ("raw", "ingested_at", "schema_version", "source")

QUERY_LIMIT = 50
RECENT_LIMIT = 10                 # default size of a "what have I logged?" listing
LISTING_EXPIRY = timedelta(minutes=30)   # how long "delete 2" can refer back to a listing

# The few columns worth showing in a cross-domain listing. Everything else stays in the
# row for the model to read; this is just what makes a line readable at a glance.
_SUMMARY_COLUMNS = {
    "food_log": ("meal", "food_name", "amount", "amount_unit", "kcal"),
    "steps": ("count",),
    "weight": ("weight_kg",),
    "measurements": ("metric", "value_cm"),
    "exercise": ("exercise", "sets", "reps", "weight_kg", "duration_min", "rpe"),
}

# Columns that scale with a driver column, so correcting an amount doesn't require the
# model to do arithmetic (it never does). domain -> (driver column, dependent columns).
_PROPORTIONAL = {
    "food_log": ("amount", ("kcal", "protein_g", "carb_g", "fat_g", "fibre_g")),
}

# Columns that define what the driver MEANS, so rescaling across them is meaningless.
# `amount` 1 -> 30 is thirty times as much bread only while the unit stays `each`; as
# "1 each -> 30 g" it is less bread, and the automatic rescale returned 4077 kcal for a
# slice. Changing the basis is a delete plus a fresh log, exactly like moving a date.
_FIXED_ALONGSIDE_DRIVER = {"food_log": ("amount_unit",)}


# --- what can be corrected --------------------------------------------------

def _targetable() -> dict[str, Feature]:
    """Domains a correction can address, keyed by domain name (which is also the table
    name — every feature names its table after its domain).

    The test is a `date` in the dedupe key, i.e. "this is a dated log". That admits
    steps/weight/measurements/food_log and any dated feature added later, and excludes
    `food_catalog` (a lookup table, not a log: nothing to query by date, and the way to
    change an entry is to save_food over it, which upserts on slug and keeps provenance
    consistent) and this feature itself.
    """
    from health.core.registry import ingesting_features   # lazy: avoids the import cycle

    return {f.domain: f for f in ingesting_features() if "date" in f.dedupe_key}


def _feature(domain: str) -> Feature:
    targetable = _targetable()
    if domain not in targetable:
        raise ValueError(
            f"{domain!r} can't be corrected. Correctable domains: {sorted(targetable)}. "
            "To change a saved food use save_food, which overwrites it by name."
        )
    return targetable[domain]


def _columns(conn: sqlite3.Connection, table: str) -> dict[str, str]:
    # `table` is always a domain that passed _feature(), never model-supplied text.
    return {r["name"]: (r["type"] or "").upper()
            for r in conn.execute(f"PRAGMA table_info({table})")}


def _editable(conn: sqlite3.Connection, feature: Feature) -> list[str]:
    """Table columns minus the dedupe key minus provenance. Editing a key column would
    move the row out from under its own correction on replay, so those are out; a date or
    meal move is a delete plus a fresh log."""
    return [c for c in _columns(conn, feature.domain)
            if c not in feature.dedupe_key and c not in _PROVENANCE]


def _match(feature: Feature, key: dict, ingested_at: str) -> tuple[str, dict]:
    """A WHERE clause pinning exactly one row: its dedupe key, plus the `ingested_at` it
    carried when query_log returned it. That guard is what makes replay faithful."""
    if set(key) != set(feature.dedupe_key):
        raise ValueError(
            f"key for {feature.domain} must have exactly the fields "
            f"{list(feature.dedupe_key)}; got {sorted(key)}. Call query_log and copy the "
            "row's `key` back verbatim."
        )
    conds = [f"{c} = :k_{c}" for c in feature.dedupe_key]
    params = {f"k_{c}": key[c] for c in feature.dedupe_key}
    if "ingested_at" in feature.dedupe_key:
        if key["ingested_at"] != ingested_at:            # food_log pins it via the key
            raise ValueError(
                f"key['ingested_at']={key['ingested_at']!r} contradicts "
                f"ingested_at={ingested_at!r}; both must come from the same query_log row."
            )
    else:
        conds.append("ingested_at = :target_ingested_at")
        params["target_ingested_at"] = ingested_at
    return " AND ".join(conds), params


def _find(conn: sqlite3.Connection, feature: Feature, key: dict, ingested_at: str):
    where, params = _match(feature, key, ingested_at)
    return conn.execute(f"SELECT * FROM {feature.domain} WHERE {where}", params).fetchone()


def _require(conn: sqlite3.Connection, feature: Feature, key: dict, ingested_at: str) -> dict:
    row = _find(conn, feature, key, ingested_at)
    if row is None:
        raise ValueError(
            f"No {feature.domain} row matches {key} with ingested_at {ingested_at}. It may "
            "already have been corrected, deleted, or overwritten by a later entry — call "
            "query_log again and use a row from the fresh result."
        )
    return dict(row)


def _rescale(domain: str, row: dict, changes: dict) -> dict:
    """Correcting a food row's amount also moves its calories and macros. Scale them from
    the row's OWN frozen numbers (not the catalog, which may have changed since), and
    freeze the result into the event so a rebuild replays it rather than recomputing.
    Anything the caller stated explicitly wins."""
    spec = _PROPORTIONAL.get(domain)
    if spec is None:
        return changes
    driver, dependent = spec
    if driver not in changes:
        return changes
    old = float(row[driver])
    if old == 0:
        return changes                       # nothing to scale from; take changes as given
    factor = float(changes[driver]) / old
    scaled = {c: round(float(row[c]) * factor, 4) for c in dependent if c not in changes}
    return {**scaled, **changes}


# --- executors --------------------------------------------------------------

def _today() -> str:
    # The clock comes from the in-flight message, never from the model — same discipline
    # as food's evening-suggestion gate.
    return get_context().timestamp.date().isoformat()


def query_log(domain: str, start_date: str | None = None, end_date: str | None = None,
              filter_text: str | None = None, limit: int | None = None) -> dict:
    conn = get_conn()
    feature = _feature(domain)
    cols = _columns(conn, feature.domain)
    # Dates default to today, so "did I log my weight?" doesn't force the model to work
    # out a range first. `limit` lets "the last 3" be exactly that rather than 50 rows
    # the reply has to trim by hand.
    end_date = end_date or _today()
    start_date = start_date or end_date
    cap = max(1, min(int(limit or QUERY_LIMIT), QUERY_LIMIT))

    sql = f"SELECT * FROM {feature.domain} WHERE date BETWEEN :start AND :end"
    params: dict = {"start": start_date, "end": end_date}
    if filter_text:
        # Match any text column: food_name/meal on a log row, raw everywhere.
        text_cols = [c for c, t in cols.items() if t.startswith("TEXT")]
        if text_cols:
            sql += " AND (" + " OR ".join(f"{c} LIKE :q" for c in text_cols) + ")"
            params["q"] = f"%{filter_text}%"
    sql += f" ORDER BY ingested_at DESC LIMIT {cap}"

    rows = [dict(r) for r in conn.execute(sql, params)]
    return {
        "domain": domain,
        "start_date": start_date,
        "end_date": end_date,
        "rows": [{**r, "key": {k: r[k] for k in feature.dedupe_key}} for r in rows],
        "editable_fields": _editable(conn, feature),
        "truncated": len(rows) == cap,
    }


def _summarise(domain: str, row: dict) -> str:
    """One readable line for a row, for a numbered listing. Values only — the column names
    are obvious in context and would double the width on a phone."""
    cols = _SUMMARY_COLUMNS.get(domain) or [
        c for c in row if c not in _PROVENANCE and c != "date"]
    parts = []
    for c in cols:
        v = row.get(c)
        if v is None or v == "":
            continue
        parts.append(f"{v:g}" if isinstance(v, float) else str(v))
    return f"{domain}: " + " ".join(parts)


def recent_entries(days: int = 1, limit: int = RECENT_LIMIT,
                   end_date: str | None = None) -> dict:
    """Everything logged recently, across every domain, newest first and numbered.

    `query_log` answers "what did I eat on Tuesday" — one domain, filtered. This answers
    "what have I logged?", which spans food, steps, weight, measurements and training at
    once, and is what a person means when they ask. Each row carries the `key` and
    `ingested_at` that edit_entry/delete_entry need, plus an `n` the user can point at
    ("delete 3") on the next turn.
    """
    conn = get_conn()
    end = end_date or _today()
    span = max(1, int(days))
    start = (_date.fromisoformat(end) - timedelta(days=span - 1)).isoformat()
    cap = max(1, min(int(limit or RECENT_LIMIT), QUERY_LIMIT))

    collected: list[dict] = []
    for domain, feature in sorted(_targetable().items()):
        for r in conn.execute(
            f"SELECT * FROM {domain} WHERE date BETWEEN ? AND ?", (start, end)
        ):
            row = dict(r)
            collected.append({
                "domain": domain,
                "date": row["date"],
                "ingested_at": row["ingested_at"],
                "key": {k: row[k] for k in feature.dedupe_key},
                "summary": _summarise(domain, row),
                "row": row,
            })

    collected.sort(key=lambda e: e["ingested_at"], reverse=True)
    shown = collected[:cap]
    for n, entry in enumerate(shown, start=1):
        entry["n"] = n
    _remember_listing(shown)
    return {
        "start_date": start,
        "end_date": end,
        "entries": shown,
        "total_in_range": len(collected),
        "truncated": len(collected) > len(shown),
    }


def _day_view(domain: str, row: dict) -> dict:
    """The corrected day, for domains that have one.

    A correction is the one write that changes a total the user has already been shown, so
    the reply has to be able to state the new one — and it can only do that from a tool
    result. Keyed off the row rather than hardcoded, so a future dated domain that grows a
    day view is one line here and nothing in the core.
    """
    if domain != "food_log" or "date" not in row:
        return {}
    return {"day_totals": get_day_totals(row["date"])}


def edit_entry(domain: str, key: dict, ingested_at: str, changes: dict) -> dict:
    conn = get_conn()
    feature = _feature(domain)
    row = _require(conn, feature, key, ingested_at)     # raises before anything is written

    if not changes:
        raise ValueError("`changes` is empty — nothing to edit.")
    editable = _editable(conn, feature)
    rejected = [c for c in changes if c not in editable]
    if rejected:
        raise ValueError(
            f"{rejected} can't be edited on {domain} (editable: {editable}). Identity "
            "fields like date and meal are fixed: to move a row, delete_entry it and log "
            "it again."
        )

    fixed = [c for c in _FIXED_ALONGSIDE_DRIVER.get(domain, ()) if c in changes]
    if fixed:
        raise ValueError(
            f"{fixed} can't be edited on {domain}: it is the basis the amount is measured "
            "in, and the macros are rescaled from the amount, so changing both at once "
            "produces nonsense (1 slice -> 30 g rescaled a sourdough row to 4077 kcal). "
            "Delete the row and log it again with the right unit."
        )

    resolved = _rescale(domain, row, dict(changes))
    stored = writer.record("corrections", {
        "action": "edit",
        "target_domain": domain,
        "target_key": dict(key),
        "target_ingested_at": ingested_at,
        "changes": resolved,
    })
    return {"edited": {**row, **resolved}, "applied": resolved,
            "correction_at": stored["ingested_at"],
            # An amount edit moves the day's totals, and nothing else in the turn will
            # fetch them — without this the reply reports the row and leaves the user to
            # guess what it did to the day.
            **_day_view(domain, row)}


def set_meal(date: str, meal: str) -> dict:
    """Give a day's unlabelled food rows the meal the user just named.

    The bot logs food the moment it arrives, even when no meal was named, so nothing is
    ever held hostage to a question — the rows land as `unlabelled` and this resolves
    them. That makes it the one correction that writes a DEDUPE-KEY column (`meal` is
    part of food_log's key), which the ordinary edit path rightly refuses.

    It is safe because the event pins the exact rows by `ingested_at`, the same guard the
    other actions use. A blanket "relabel whatever is unlabelled on this date" would
    diverge on rebuild: corrections replay after every food row, so it would sweep up
    later rows that were never unlabelled at the time. Pinning makes replay reproduce
    live exactly, and re-applying is a no-op because the rows already hold the label.
    """
    conn = get_conn()
    label = normalise_meal(meal)
    if label == UNLABELLED_MEAL:
        raise ValueError("`meal` is empty — pass the meal the user named (e.g. 'breakfast').")

    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM food_log WHERE date = ? AND meal = ? ORDER BY ingested_at",
        (date, UNLABELLED_MEAL),
    )]
    if not rows:
        raise ValueError(
            f"Nothing on {date} is waiting for a meal label. Call query_log to see what "
            "was logged that day; to move an already-labelled row, delete_entry it and "
            "log it again."
        )

    stored = writer.record("corrections", {
        "action": "relabel",
        "target_domain": "food_log",
        "target_key": {"date": date},
        # A LIST here, unlike edit/delete: one relabel pins the whole set of rows it
        # resolved. project() branches on the action before any of this is interpreted.
        "target_ingested_at": [r["ingested_at"] for r in rows],
        "changes": {"meal": label},
    })
    return {
        "relabelled": [{**r, "meal": label} for r in rows],
        "date": date,
        "meal": label,
        "count": len(rows),
        "correction_at": stored["ingested_at"],
    }


def delete_entry(domain: str, key: dict, ingested_at: str) -> dict:
    conn = get_conn()
    feature = _feature(domain)
    row = _require(conn, feature, key, ingested_at)     # raises before anything is written

    stored = writer.record("corrections", {
        "action": "delete",
        "target_domain": domain,
        "target_key": dict(key),
        "target_ingested_at": ingested_at,
        "changes": None,
    })
    return {"deleted": row, "correction_at": stored["ingested_at"],
            **_day_view(domain, row)}


# --- projection (the sanctioned non-upsert; used by live ingest AND rebuild_db) ---

def project(conn: sqlite3.Connection, row: dict) -> None:
    """Apply the correction to the TARGET table.

    Idempotent: re-applying converges. Deliberately SILENT when it matches nothing —
    unlike the executors, which raise so the user hears about it. On replay, matching
    nothing means the target was overwritten by a later write, which is precisely when
    the correction should not apply.
    """
    action = row["action"]
    if action not in ("edit", "delete", "relabel"):
        raise ValueError(f"unknown correction action {action!r}")

    if action == "relabel":
        _project_relabel(conn, row)
        return

    feature = _feature(row["target_domain"])
    where, params = _match(feature, dict(row["target_key"]), row["target_ingested_at"])

    if action == "delete":
        conn.execute(f"DELETE FROM {feature.domain} WHERE {where}", params)
        return

    changes = row["changes"] or {}
    if not changes:
        return                                          # an empty edit is a no-op
    rejected = [c for c in changes if c not in _editable(conn, feature)]
    if rejected:
        raise ValueError(
            f"correction targets non-editable column(s) {rejected} on {feature.domain}"
        )
    sets = ", ".join(f"{c} = :set_{c}" for c in changes)
    conn.execute(
        f"UPDATE {feature.domain} SET {sets} WHERE {where}",
        {**params, **{f"set_{c}": v for c, v in changes.items()}},
    )


# --- the numbered listing, so "delete 3" means something next turn ---------------

# Ephemeral per-chat memory of the last listing (reply_handle -> {at, entries}).
# In-process only, like food's pending estimate: losing it on restart just means the
# user re-lists. It exists because a number is only a reference if both sides agree
# what it points at, and the chat transcript alone can't carry a row's dedupe key.
_listing: dict[Any, dict] = {}


def _remember_listing(entries: list[dict]) -> None:
    ctx = get_context()
    if not entries:
        _listing.pop(ctx.reply_handle, None)
        return
    _listing[ctx.reply_handle] = {
        "at": ctx.timestamp,
        "entries": [{k: e[k] for k in ("n", "domain", "key", "ingested_at", "summary")}
                    for e in entries],
    }


def listing_system_context(msg) -> str | None:
    """Surface the last numbered listing so a bare "delete 3" resolves to an exact row.

    Expires: an hour later "3" almost certainly refers to something else, and acting on a
    stale number would delete the wrong entry — the one mistake corrections can't make
    cheap, since the user wouldn't know it happened.
    """
    listed = _listing.get(msg.reply_handle)
    if not listed:
        return None
    if msg.timestamp - listed["at"] > LISTING_EXPIRY:
        _listing.pop(msg.reply_handle, None)
        return None
    lines = "\n".join(
        f"{e['n']}. {e['summary']} | domain={e['domain']} key={json.dumps(e['key'], sort_keys=True)} "
        f"ingested_at={e['ingested_at']}"
        for e in listed["entries"]
    )
    return (
        "LAST LISTING (what the user just saw, numbered). If this message refers to an "
        "entry by number ('delete 2', 'the third one was 40g'), that is the row it means: "
        "pass that line's domain, key and ingested_at straight to edit_entry or "
        "delete_entry — you do not need to look it up again.\n" + lines
    )


def _project_relabel(conn: sqlite3.Connection, row: dict) -> None:
    """Apply a set_meal event: stamp one label onto the exact rows it pinned.

    This is the only place a dedupe-key column is written, so it is deliberately narrow —
    food_log's `meal` and nothing else. Matching on the pinned `ingested_at` list makes it
    both idempotent (a second application writes the same value) and replay-faithful (a
    row deleted or never written matches nothing). No PK collision is possible: two rows
    can't share an `ingested_at`, so moving one into another meal can't land on top of an
    existing row.
    """
    if row["target_domain"] != "food_log":
        raise ValueError(f"relabel only applies to food_log, not {row['target_domain']!r}")
    pinned = row["target_ingested_at"] or []
    if not pinned:
        return
    holes = ", ".join(f":ia{i}" for i in range(len(pinned)))
    conn.execute(
        f"UPDATE food_log SET meal = :meal WHERE date = :date AND ingested_at IN ({holes})",
        {
            "meal": row["changes"]["meal"],
            "date": row["target_key"]["date"],
            **{f"ia{i}": v for i, v in enumerate(pinned)},
        },
    )


_DOMAIN_ENUM = {
    "type": "string",
    "enum": ["food_log", "steps", "weight", "measurements", "exercise"],
}
_KEY_PROP = {
    "type": "object",
    "description": "The `key` object from the query_log row, copied exactly.",
    "additionalProperties": True,
}
_INGESTED_AT_PROP = {
    "type": "string",
    "description": "The `ingested_at` of that same query_log row, copied exactly.",
}

TOOLS = [
    {
        "name": "query_log",
        "description": (
            "Look up rows the user actually logged. Use it to answer questions about "
            "specific entries ('what did I eat on Tuesday?', 'did I log my weight "
            "yesterday?'), and ALWAYS before edit_entry or delete_entry — those need a "
            "row's exact `key` and `ingested_at`, and this is the only thing that returns "
            "them. Each row comes back with a ready-made `key` object to copy back "
            "verbatim, plus `editable_fields` listing what edit_entry will accept. "
            "Returns at most 50 rows, newest first."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "domain": {
                    **_DOMAIN_ENUM,
                    "description": "Which log to read. Saved foods aren't here — the /catalog command lists those.",
                },
                "start_date": {"type": "string", "description": "ISO date YYYY-MM-DD, inclusive. Defaults to end_date (i.e. a single day)."},
                "end_date": {"type": "string", "description": "ISO date YYYY-MM-DD, inclusive. Defaults to today."},
                "filter_text": {"type": "string", "description": "Optional substring to narrow the rows, matched against the text fields (food name, meal, the original message). E.g. 'chickpea'."},
                "limit": {"type": "integer", "description": f"Maximum rows, newest first. Defaults to {QUERY_LIMIT}, which is also the cap."},
            },
            "required": ["domain"],
        },
    },
    {
        "name": "recent_entries",
        "description": (
            "What the user has logged recently, across EVERY log at once — food, steps, "
            "weight, measurements and training — newest first and numbered. This is the "
            "tool for 'what have I logged today?', 'what did I do yesterday?', 'show me "
            "the last 5 things'. Each entry carries the `key` and `ingested_at` that "
            "edit_entry and delete_entry need, and an `n` the user can refer to next turn "
            "('delete 3'). Use query_log instead when they ask about ONE log or want a "
            "text filter, and get_summary for totals and averages over a period."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": "How many days back to include, counting the end date as day 1. 1 = just that day, 3 = the last three days."},
                "limit": {"type": "integer", "description": f"Maximum entries to return, newest first. Default {RECENT_LIMIT}."},
                "end_date": {"type": "string", "description": "ISO date YYYY-MM-DD the window ends on. Defaults to today."},
            },
            "required": [],
        },
    },
    {
        "name": "edit_entry",
        "description": (
            "Correct a value on a row the user says is wrong ('the chickpeas were 40g', "
            "'that was 9500 steps'). Call query_log first and pass that row's `key` and "
            "`ingested_at` back verbatim. Put ONLY the fields that change in `changes`. "
            "For a food row, changing `amount` rescales its calories and macros "
            "automatically — pass the amount alone and never work the macros out "
            "yourself. Identity fields (date, meal, which food) can NOT be edited: to "
            "move an entry to another day or meal, delete_entry it and log it again."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "domain": _DOMAIN_ENUM,
                "key": _KEY_PROP,
                "ingested_at": _INGESTED_AT_PROP,
                "changes": {
                    "type": "object",
                    "description": "Field -> new value, for the fields that change only. Each must be one of the row's `editable_fields`.",
                    "additionalProperties": True,
                },
            },
            "required": ["domain", "key", "ingested_at", "changes"],
        },
    },
    {
        "name": "set_meal",
        "description": (
            "Say which meal a day's UNLABELLED food rows belonged to. When the user logs "
            "food without naming a meal it is logged straight away as 'unlabelled' and you "
            "ask which meal it was; when they answer — even with a bare 'breakfast' — call "
            "this with that date and label. It relabels every unlabelled row on that date "
            "at once, so if two different meals are waiting, ask which day/meal pair they "
            "mean rather than guessing. This only touches unlabelled rows: to move an "
            "already-labelled entry to a different meal, delete_entry it and log it again."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "ISO date YYYY-MM-DD of the unlabelled rows."},
                "meal": {"type": "string", "description": "The meal the user named: breakfast, lunch, dinner, snack, or whatever they called it."},
            },
            "required": ["date", "meal"],
        },
    },
    {
        "name": "delete_entry",
        "description": (
            "Remove a row that shouldn't be there — a double-log, or something the user "
            "says they didn't have. Call query_log first and pass that row's `key` and "
            "`ingested_at` back verbatim. This is recorded as an append-only correction "
            "and is undone by simply logging the entry again, so when the user's "
            "instruction clearly points at one row, just do it. If more than one row "
            "could be meant, name them and ask which."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "domain": _DOMAIN_ENUM,
                "key": _KEY_PROP,
                "ingested_at": _INGESTED_AT_PROP,
            },
            "required": ["domain", "key", "ingested_at"],
        },
    },
]


FEATURE = Feature(
    name="corrections",
    tools=TOOLS,
    executors={
        "query_log": query_log,
        "recent_entries": recent_entries,
        "edit_entry": edit_entry,
        "delete_entry": delete_entry,
        "set_meal": set_meal,
    },
    schema=None,                      # owns no table — the JSONL is the audit trail
    domain="corrections",
    dedupe_key=("target_domain", "ingested_at"),
    project=project,
    system_context=listing_system_context,
)
