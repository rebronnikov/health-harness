"""The registry assembles features into what the harness and store use.

This FEATURES list is the ONLY place adding a feature touches shared code.
Everything else a feature needs lives inside its own module under features/.
"""
from __future__ import annotations

import sqlite3

from health.core.contracts import Feature
from health.features import corrections, exercise, food, measurements, review, steps, weight

# NOTE: targets is a compute lib (constants + remaining/suggest math) imported by
# food, not a routed feature — it declares no tools, so it isn't listed here.

FEATURES: list[Feature] = [
    steps.FEATURE,
    weight.FEATURE,
    food.CATALOG_FEATURE,
    food.LOG_FEATURE,
    measurements.FEATURE,
    exercise.FEATURE,
    review.FEATURE,          # read-only: no domain, so rebuild never sees it
    # corrections MUST STAY LAST. rebuild_db replays domains in this order, and a
    # correction has to be applied after the rows it corrects — see corrections.py.
    corrections.FEATURE,
]


def all_tools() -> list[dict]:
    return [t for f in FEATURES for t in f.tools]


def all_executors() -> dict[str, callable]:
    return {name: fn for f in FEATURES for name, fn in f.executors.items()}


def system_context_fragments(msg) -> list[str]:
    """Per-message system-prompt additions contributed by features (e.g. a pending
    food estimate awaiting confirmation). Empty for stateless turns."""
    out = []
    for f in FEATURES:
        if f.system_context:
            frag = f.system_context(msg)
            if frag:
                out.append(frag)
    return out


def init_schema(conn: sqlite3.Connection) -> None:
    for f in FEATURES:
        if f.schema:
            conn.executescript(f.schema)        # all DDL uses IF NOT EXISTS


def ingesting_features() -> list[Feature]:
    """Features that own a JSONL domain + projector — what rebuild_db replays."""
    return [f for f in FEATURES if f.domain and f.project]


def feature_by_domain(domain: str) -> Feature:
    for f in FEATURES:
        if f.domain == domain:
            return f
    raise KeyError(f"no feature owns domain {domain!r}")
