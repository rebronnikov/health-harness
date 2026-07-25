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

import sqlite3

from health.core.contracts import Feature
from health.store import writer
from health.store.connection import get_conn

# Stamped by writer.record() from the runtime — never editable through a correction.
_PROVENANCE = ("raw", "ingested_at", "schema_version", "source")

QUERY_LIMIT = 50

# Columns that scale with a driver column, so correcting an amount doesn't require the
# model to do arithmetic (it never does). domain -> (driver column, dependent columns).
_PROPORTIONAL = {
    "food_log": ("amount", ("kcal", "protein_g", "carb_g", "fat_g", "fibre_g")),
}


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

def query_log(domain: str, start_date: str, end_date: str,
              filter_text: str | None = None) -> dict:
    conn = get_conn()
    feature = _feature(domain)
    cols = _columns(conn, feature.domain)

    sql = f"SELECT * FROM {feature.domain} WHERE date BETWEEN :start AND :end"
    params: dict = {"start": start_date, "end": end_date}
    if filter_text:
        # Match any text column: food_name/meal on a log row, raw everywhere.
        text_cols = [c for c, t in cols.items() if t.startswith("TEXT")]
        if text_cols:
            sql += " AND (" + " OR ".join(f"{c} LIKE :q" for c in text_cols) + ")"
            params["q"] = f"%{filter_text}%"
    sql += f" ORDER BY ingested_at DESC LIMIT {QUERY_LIMIT}"

    rows = [dict(r) for r in conn.execute(sql, params)]
    return {
        "domain": domain,
        "rows": [{**r, "key": {k: r[k] for k in feature.dedupe_key}} for r in rows],
        "editable_fields": _editable(conn, feature),
        "truncated": len(rows) == QUERY_LIMIT,
    }


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

    resolved = _rescale(domain, row, dict(changes))
    stored = writer.record("corrections", {
        "action": "edit",
        "target_domain": domain,
        "target_key": dict(key),
        "target_ingested_at": ingested_at,
        "changes": resolved,
    })
    return {"edited": {**row, **resolved}, "applied": resolved,
            "correction_at": stored["ingested_at"]}


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
    return {"deleted": row, "correction_at": stored["ingested_at"]}


# --- projection (the sanctioned non-upsert; used by live ingest AND rebuild_db) ---

def project(conn: sqlite3.Connection, row: dict) -> None:
    """Apply the correction to the TARGET table.

    Idempotent: re-applying converges. Deliberately SILENT when it matches nothing —
    unlike the executors, which raise so the user hears about it. On replay, matching
    nothing means the target was overwritten by a later write, which is precisely when
    the correction should not apply.
    """
    action = row["action"]
    if action not in ("edit", "delete"):
        raise ValueError(f"unknown correction action {action!r}")

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


_DOMAIN_ENUM = {
    "type": "string",
    "enum": ["food_log", "steps", "weight", "measurements"],
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
                "start_date": {"type": "string", "description": "ISO date YYYY-MM-DD, inclusive."},
                "end_date": {"type": "string", "description": "ISO date YYYY-MM-DD, inclusive. Same as start_date for a single day."},
                "filter_text": {"type": "string", "description": "Optional substring to narrow the rows, matched against the text fields (food name, meal, the original message). E.g. 'chickpea'."},
            },
            "required": ["domain", "start_date", "end_date"],
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
        "edit_entry": edit_entry,
        "delete_entry": delete_entry,
    },
    schema=None,                      # owns no table — the JSONL is the audit trail
    domain="corrections",
    dedupe_key=("target_domain", "ingested_at"),
    project=project,
)
