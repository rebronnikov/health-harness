"""One-off: give already-logged banana rows the fibre the catalog never had.

`food_catalog.banana` was seeded with `fibre_g = 0` because the retailer's label omits
fibre. Every row logged from it copied that zero forward, so a food that carries ~1.4g
fibre per 100g contributed nothing to a target that is a FLOOR. Fixing the catalog only
helps future logs; this repairs the history.

It writes through `corrections.edit_entry`, not by editing `data/food_log.jsonl`: the
tools can express this, so the JSONL surgery route (CLAUDE.md, "Repairing / editing
logged data safely") isn't warranted. Each row becomes its own append-only event pinned
to that row's `ingested_at`, which is what makes it replay faithfully under `rebuild_db`.

Context setup mirrors `health/seed_catalog.py:load()` — a synthetic NormalizedMessage so
`writer.record` can stamp runtime-authored provenance, since executors read the clock and
the source from the in-flight message and never from their caller.

Re-runnable: the `fibre_g = 0` filter means a second run selects nothing.

    .venv/bin/python scripts/backfill_banana_fibre.py [--dry-run]

Disposable — the permanent record is the events in data/corrections.jsonl.
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from health.core.contracts import NormalizedMessage, current_message   # noqa: E402
from health.features import corrections                                 # noqa: E402
from health.store.connection import get_conn, init_db                   # noqa: E402

SLUG = "banana"
FIBRE_PER_UNIT = 1.68        # 1.4g/100g * 120g peeled — same derivation as the other macros

REASON = (
    "backfill: banana fibre was seeded as 0 because the retailer's label omits it; "
    "1.4g/100g (another UK retailer's panel + McCance & Widdowson 2015) = "
    f"{FIBRE_PER_UNIT}g per banana"
)


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    init_db()
    conn = get_conn()

    rows = [dict(r) for r in conn.execute(
        "SELECT date, meal, food_slug, amount, ingested_at FROM food_log "
        "WHERE catalog_slug = ? AND fibre_g = 0 ORDER BY ingested_at",
        (SLUG,),
    )]
    if not rows:
        print("Nothing to backfill — no banana rows are sitting at fibre 0.")
        return

    total = sum(r["amount"] for r in rows) * FIBRE_PER_UNIT
    print(f"{len(rows)} row(s), {sum(r['amount'] for r in rows):g} banana-units, "
          f"+{total:.2f}g fibre{' (dry run)' if dry_run else ''}")
    if dry_run:
        for r in rows:
            print(f"  {r['date']} {r['meal']:<16} x{r['amount']:g} -> "
                  f"{round(FIBRE_PER_UNIT * r['amount'], 4):g}g")
        return

    msg = NormalizedMessage(
        source="backfill", reply_handle=None, timestamp=datetime.now(), text=REASON,
    )
    token = current_message.set(msg)
    try:
        for r in rows:
            # Fibre scales with the amount logged.
            corrections.edit_entry(
                "food_log",
                key={k: r[k] for k in ("date", "meal", "food_slug", "ingested_at")},
                ingested_at=r["ingested_at"],
                changes={"fibre_g": round(FIBRE_PER_UNIT * r["amount"], 4)},
            )
    finally:
        current_message.reset(token)

    print(f"Wrote {len(rows)} correction(s) to data/corrections.jsonl.")


if __name__ == "__main__":
    main()
