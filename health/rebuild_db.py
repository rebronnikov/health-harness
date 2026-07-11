"""Rebuild the SQLite projection from the JSONL system of record.

    python -m health.rebuild_db [--skip-bad-tail]

Deletes the projection, recreates every feature's table, then replays each
data/<domain>.jsonl line through that feature's own `project()` — the SAME upsert
used by live ingest. Because `project()` is a dedupe-keyed ON CONFLICT DO UPDATE,
replay converges regardless of duplicates, and since JSONL append order equals
arrival order, the result is logically identical to what live ingest produced.

Provenance (ingest_log.jsonl) is an audit sidecar and is NOT replayed into the
projection — the projection is built only from canonical domain rows.
"""
from __future__ import annotations

import argparse
import json
import os

from health.config import DATA_DIR, DB_PATH
from health.core.registry import ingesting_features, init_schema
from health.store.connection import open_conn


def rebuild(skip_bad_tail: bool = False) -> str:
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)                       # the projection is disposable by definition
    conn = open_conn(DB_PATH)
    try:
        init_schema(conn)
        for feature in ingesting_features():
            path = DATA_DIR / f"{feature.domain}.jsonl"
            if not path.exists():
                continue
            lines = path.read_text(encoding="utf-8").splitlines()
            for i, line in enumerate(lines):
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    is_last = i == len(lines) - 1
                    if skip_bad_tail and is_last:
                        break                    # tolerate a truncated final line (crash mid-append)
                    raise RuntimeError(
                        f"Corrupt JSONL in {path} at line {i + 1}. "
                        f"Re-run with --skip-bad-tail only if this is a truncated final line."
                    )
                feature.project(conn, row)
        conn.commit()
    finally:
        conn.close()
    return str(DB_PATH)


def main() -> None:
    ap = argparse.ArgumentParser(description="Rebuild the SQLite projection from JSONL.")
    ap.add_argument(
        "--skip-bad-tail",
        action="store_true",
        help="Tolerate a truncated final JSONL line (e.g. a crash mid-append).",
    )
    args = ap.parse_args()
    path = rebuild(skip_bad_tail=args.skip_bad_tail)
    print(f"Rebuilt projection at {path}")


if __name__ == "__main__":
    main()
