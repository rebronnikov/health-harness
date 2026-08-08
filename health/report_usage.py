"""What the bot costs, read back out of the transcript.

`harness._record_usage` writes one `usage` line per API call into
`data/messages/<date>.jsonl`; this is the read side. It exists because a token count you
never total is not a cost signal — before it, the only way to notice spend was the credit
balance running out.

Two of its columns earn their place beyond the dollar figure:

  hit%   cache_read / (cache_read + cache_write). Prompt caching is a prefix match, so it
         fails SILENTLY: a stray per-request value in the cached prefix doesn't error, it
         just makes every call a fresh write. Expect 85-90%; a collapse toward 0 while
         `write` stays high is that failure, and nothing else reports it.

  c/msg  API calls per inbound message. Baseline 2.0-2.3 (one tool round plus the reply).
         This is the regression canary for a model or effort change: a drop toward 1.5
         means the model stopped making a round-trip it used to make — skipping a web
         lookup or a get_day_totals — which reads as a perfectly fine reply.

Prices are per model as recorded on each line, not as configured now, so a window spanning
a model switch reports both halves correctly.
"""
from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timedelta

from health.config import DATA_DIR
from health.store.msg_log import MESSAGES_DIR

# $ per million tokens, (input, output). Cache multipliers apply to the input price.
PRICES = {
    "claude-sonnet-5": (3.00, 15.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-haiku-4-5": (1.00, 5.00),
}

# A cache write costs more than plain input and a read costs a tenth of it — which is the
# whole reason caching pays here, and why these are load-bearing constants rather than a
# rounded guess.
CACHE_WRITE_5M = 1.25
CACHE_WRITE_1H = 2.00
CACHE_READ = 0.10

# web_search bills per request, not per token.
WEB_SEARCH_PER_REQUEST = 0.010

_MILLION = 1_000_000


class UnknownModel(Exception):
    """A usage line names a model with no price. Raised rather than defaulted: a model
    silently priced at zero turns the report into a reassuring lie."""


def cost(row: dict) -> float:
    """Dollars for one usage line. Pure — no filesystem, no clock."""
    model = row.get("model")
    if model not in PRICES:
        raise UnknownModel(f"no price for {model!r}; add it to PRICES")
    in_rate, out_rate = PRICES[model]

    # Older lines predate the per-TTL split; fall back to the total at the 5m rate, which
    # is what was configured when they were written.
    write_5m = row.get("cache_write_5m_tokens")
    write_1h = row.get("cache_write_1h_tokens") or 0
    if write_5m is None and not write_1h:
        write_5m = row.get("cache_write_tokens", 0)

    tokens = (
        row.get("input_tokens", 0)
        + (write_5m or 0) * CACHE_WRITE_5M
        + write_1h * CACHE_WRITE_1H
        + row.get("cache_read_tokens", 0) * CACHE_READ
    )
    return (tokens * in_rate + row.get("output_tokens", 0) * out_rate) / _MILLION + (
        row.get("web_search_requests", 0) * WEB_SEARCH_PER_REQUEST
    )


def summarise(rows: list[dict], inbound: int) -> dict:
    """Total a day's usage lines. Pure. `inbound` is that day's user-message count, which
    is what makes calls-per-message meaningful."""
    read = sum(r.get("cache_read_tokens", 0) for r in rows)
    write = sum(r.get("cache_write_tokens", 0) for r in rows)
    presented = read + write
    return {
        "messages": inbound,
        "calls": len(rows),
        "calls_per_message": len(rows) / inbound if inbound else 0.0,
        "input_tokens": sum(r.get("input_tokens", 0) for r in rows),
        "output_tokens": sum(r.get("output_tokens", 0) for r in rows),
        "cache_write_tokens": write,
        "cache_read_tokens": read,
        # Of the cacheable prefix presented across these calls, what fraction was served
        # from cache. Undefined (not 0) when nothing was presented at all.
        "hit_rate": read / presented if presented else None,
        "cost": sum(cost(r) for r in rows),
    }


def _read_day(day: date) -> list[dict]:
    path = DATA_DIR / MESSAGES_DIR / f"{day.isoformat()}.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def load_days(days: int, today: date):
    """Yield (day, usage_rows, inbound_count), oldest first, skipping days with no data."""
    for offset in range(days - 1, -1, -1):
        day = today - timedelta(days=offset)
        entries = _read_day(day)
        if not entries:
            continue
        yield (day,
               [e for e in entries if e.get("kind") == "usage"],
               sum(1 for e in entries if e.get("kind") == "in"))


def _k(n: float) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else f"{n:.0f}"


def _row(label: str, s: dict) -> str:
    hit = "—" if s["hit_rate"] is None else f"{s['hit_rate'] * 100:.1f}%"
    return (f"{label:<12}{s['messages']:>5}{s['calls']:>7}{s['calls_per_message']:>7.1f}"
            f"{_k(s['input_tokens']):>10}{_k(s['cache_write_tokens']):>8}"
            f"{_k(s['cache_read_tokens']):>8}{_k(s['output_tokens']):>9}"
            f"{hit:>8}{'$' + format(s['cost'], '.2f'):>8}")


HEADER = (f"{'date':<12}{'msgs':>5}{'calls':>7}{'c/msg':>7}{'input':>10}{'write':>8}"
          f"{'read':>8}{'output':>9}{'hit%':>8}{'$':>8}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Total the bot's API spend from the transcript.")
    ap.add_argument("--days", type=int, default=7, help="How many days back to report (default 7).")
    args = ap.parse_args()

    days = list(load_days(args.days, datetime.now().date()))
    if not days:
        print(f"No transcript data in the last {args.days} days.")
        return

    print(HEADER)
    all_rows, total_inbound = [], 0
    for day, rows, inbound in days:
        print(_row(day.isoformat(), summarise(rows, inbound)))
        all_rows += rows
        total_inbound += inbound

    total = summarise(all_rows, total_inbound)
    print("-" * len(HEADER))
    print(_row(f"{len(days)} days", total))
    if total["cost"]:
        print(f"{'':>{len(HEADER) - 20}}≈ ${total['cost'] / len(days) * 7:.2f} / week")


if __name__ == "__main__":
    main()
