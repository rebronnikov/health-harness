"""Deferred (phase 2). Read-only analytics over the SQLite projection.

Plan: `get_summary` + a weekly-analysis prompt. Reads the projection (never JSONL
directly), computes deterministically in the executor, returns structured numbers
for Claude to narrate.
"""
FEATURE = None
