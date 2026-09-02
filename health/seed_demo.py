"""Seed a week of plausible demo data — the no-API-key tour of what a day looks like.

    python -m health.seed_demo --days 7 [--reset] [--today YYYY-MM-DD]

Everything is written through the feature executors inside the `current_message`
contextvar, exactly as `seed_catalog.load()` does, and nothing is written any other way.
That is not a convenience; three things depend on it:

1. Provenance stays runtime-authored (invariant 4): `writer.record()` stamps `raw`,
   `source` and `ingested_at` from the bound message, and raises `LookupError` outside one.
2. `rebuild_db` reproduces the demo, so the "JSONL is the system of record" round-trip is
   checkable on demo data — CI diffs `/today` before and after a rebuild.
3. It exercises `food._resolve_meal` (one message writing two meals), the duplicate guard
   and every feature's `project()`: a smoke test that ships as a feature.

Dates are relative to `--today` (default: `HEALTH_TODAY`, else the real date), so the demo
never goes stale and CI can pin it. Amounts are jittered by a `random.Random` seeded per
date — reproducible, but the days differ. Today gets breakfast and lunch only, so it is
left with unmet protein and fibre floors and calorie room: `suggest_food` has something to
say, and the progress block printed at the end is the one the README shows.

Running it twice without `--reset` is refused rather than doubled: `food_log` keys on
`ingested_at`, so a second pass would add every row again.
"""
from __future__ import annotations

import argparse
import os
import random
import sys
from datetime import date, datetime, time, timedelta

from health import seed_catalog
from health.core.contracts import NormalizedMessage, current_message
from health.core.registry import ingesting_features
from health.features import exercise, food, measurements, steps, weight
from health.store import writer
from health.store.connection import get_conn, init_db

SOURCE = "demo"

# (food, unit, base amount, jitter) per meal. Never the same food twice in one meal at
# the same amount — the duplicate guard is wall-clock (120 s) and this writes a week in
# under a second. The supplements in breakfast carry their own meal label, so one
# breakfast message lands under two meals.
MEALS = {
    "breakfast": [("jumbo oats", "g", 40, 10), ("milk", "ml", 200, 50), ("banana", "each", 1, 0),
                  ("creatine", "each", 1, 0), ("psyllium husk", "g", 8, 0),
                  ("fish oil", "each", 1, 0), ("vitamin D", "each", 1, 0)],
    "lunch": [("chicken breast", "g", 150, 30), ("buckwheat", "g", 150, 40),
              ("chickpeas", "g", 100, 40)],
    "dinner": [("salmon fillet", "g", 130, 20), ("blueberries", "g", 80, 30),
               ("skyr", "g", 210, 0)],
    "snack": [("mixed nuts", "g", 25, 0), ("protein bar", "each", 1, 0)],
}
TODAY_MEALS = ("breakfast", "lunch")          # leave room, so there is something to suggest

SESSIONS = (
    [{"exercise": "bench press", "sets": 4, "reps": 8, "weight_kg": 60, "rpe": 8},
     {"exercise": "pull-ups", "sets": 4, "reps": 8},
     {"exercise": "plank", "sets": 3, "duration_min": 1}],
    [{"exercise": "squat", "sets": 4, "reps": 6, "weight_kg": 80, "rpe": 8},
     {"exercise": "deadlift", "sets": 3, "reps": 10, "weight_kg": 60}],
)


def _bound(day: date, text: str):
    """The demo's stand-in for a message: noon on `day`, so the evening suggestion gate
    stays shut and the executors' clock reads are deterministic."""
    return NormalizedMessage(source=SOURCE, reply_handle=None,
                             timestamp=datetime.combine(day, time(12, 0)), text=text)


def _write(day: date, text: str, fn, *args, **kwargs):
    token = current_message.set(_bound(day, text))
    try:
        return fn(*args, **kwargs)
    finally:
        current_message.reset(token)


def _items(rng: random.Random, meal: str) -> list[dict]:
    out = []
    for name, unit, base, jitter in MEALS[meal]:
        amount = base + (rng.randint(-jitter, jitter) if jitter else 0)
        out.append({"name": name, "quantity": amount, "unit": unit})
    return out


def _seed_day(day: date, age: int, is_today: bool) -> dict:
    """One day's worth of messages, oldest-style first. `age` is days before today."""
    rng = random.Random(day.isoformat())
    iso = day.isoformat()
    counts = {"food": 0, "meals": set()}

    count = rng.randint(60, 125) * 100
    _write(day, f"{count} steps", steps.log_steps, [{"date": iso, "count": count}])
    kg = round(78.6 - 0.04 * age + rng.uniform(-0.3, 0.3), 1)
    _write(day, f"weight {kg}", weight.log_weight, iso, kg)
    if age % 2 == 0:
        session = SESSIONS[(age // 2) % len(SESSIONS)]
        _write(day, "training: " + ", ".join(e["exercise"] for e in session),
               exercise.log_exercise, iso, session)

    for meal in (TODAY_MEALS if is_today else MEALS):
        items = _items(rng, meal)
        text = f"{meal}: " + ", ".join(f"{i['quantity']:g}{i['unit']} {i['name']}" for i in items)
        out = _write(day, text, food.log_food, iso, meal, items)
        if out["unresolved"] or out["duplicate_skipped"]:
            raise RuntimeError(f"demo data did not log cleanly on {iso}: {out}")
        counts["food"] += len(out["logged"])
        counts["meals"].update(out["meals"])
    return counts


def reset() -> list[str]:
    """Drop the system of record and empty the projection, leaving the schema. The
    projection is cleared table by table rather than by deleting the file, so a caller
    that already holds the connection (tests) keeps a valid one."""
    removed = []
    # The writer's copy of DATA_DIR, because these are the files the writer appended.
    names = [f"{f.domain}.jsonl" for f in ingesting_features()] + ["ingest_log.jsonl"]
    for path in (writer.DATA_DIR / n for n in names):
        if path.exists():
            path.unlink()
            removed.append(path.name)
    conn = get_conn()
    for f in ingesting_features():
        if f.schema:                       # corrections owns a domain but no table of its own
            conn.execute(f"DELETE FROM {f.domain}")
    conn.commit()
    return removed


def seed(days: int = 7, today: date | None = None, reset_first: bool = False) -> dict:
    today = today or _default_today()
    days = max(1, int(days))
    first = today - timedelta(days=days - 1)
    init_db()
    if reset_first:
        reset()
    already = get_conn().execute(
        "SELECT COUNT(*) FROM food_log WHERE date BETWEEN ? AND ?",
        (first.isoformat(), today.isoformat())).fetchone()[0]
    if already:
        raise SystemExit(
            f"{already} food row(s) already logged between {first} and {today}; "
            "re-run with --reset to start from an empty data directory, or pick a "
            "different DATA_DIR.")

    seed_catalog.load()
    _write(first, "waist 84", measurements.log_measurement, first.isoformat(), "waist", 84.0)
    food_rows, meals = 0, set()
    for i in range(days):
        day = first + timedelta(days=i)
        counts = _seed_day(day, age=days - 1 - i, is_today=(day == today))
        food_rows += counts["food"]
        meals |= counts["meals"]
    progress = _write(today, "/today", food.get_day_totals, today.isoformat())["progress_block"]
    return {"first": first, "today": today, "days": days, "food_rows": food_rows,
            "meals": sorted(meals), "progress_block": progress}


def _default_today() -> date:
    pinned = os.environ.get("HEALTH_TODAY")
    return date.fromisoformat(pinned) if pinned else date.today()


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Seed DATA_DIR with a week of demo data.")
    ap.add_argument("--days", type=int, default=7, help="how many days, ending today (default 7)")
    ap.add_argument("--reset", action="store_true",
                    help="empty the data directory first (JSONL and projection)")
    ap.add_argument("--today", type=date.fromisoformat, default=None,
                    help="the last day to seed (default: $HEALTH_TODAY, else the real date)")
    args = ap.parse_args(argv)
    try:
        out = seed(days=args.days, today=args.today, reset_first=args.reset)
    except SystemExit as e:
        if e.code and not isinstance(e.code, int):
            print(e.code, file=sys.stderr)
            raise SystemExit(1)
        raise
    print(f"Seeded {out['days']} day(s), {out['first']} to {out['today']}: "
          f"{out['food_rows']} food rows across {', '.join(out['meals'])}, plus steps, "
          f"weight and training. Data in {writer.DATA_DIR}.\n")
    print(out["progress_block"])


if __name__ == "__main__":
    main()
