"""SQLite connection management. The DB is a disposable projection — never the
system of record — so this module only opens it and applies feature DDL; it never
decides what a row means."""
from __future__ import annotations

import sqlite3

from health.config import DATA_DIR, DB_PATH

_conn: sqlite3.Connection | None = None


def open_conn(path) -> sqlite3.Connection:
    """Open a fresh connection to `path` (used by rebuild_db for a clean DB).

    check_same_thread=False because harness.handle runs in asyncio.to_thread
    worker threads; writer.record serializes access with a lock, so a single
    shared connection across those threads is safe at single-user volume.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def get_conn() -> sqlite3.Connection:
    """Process-wide singleton connection to the projection DB."""
    global _conn
    if _conn is None:
        _conn = open_conn(DB_PATH)
    return _conn


def init_db() -> None:
    """Ensure data/ exists and every feature's table is created. Run once at startup."""
    from health.core.registry import init_schema  # lazy: avoid import cycle

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = get_conn()
    init_schema(conn)
    conn.commit()
