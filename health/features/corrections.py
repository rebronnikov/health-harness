"""Deferred (phase 1). The misparse fixer that the auto-commit design relies on.

Plan: `query_log`, `edit_entry`, `delete_entry`. Edits/deletes must themselves be
recorded as new append-only JSONL events (never mutate prior lines) so the system
of record stays immutable and rebuild stays faithful; the projection reflects the
latest correction via the same dedupe-keyed upsert.
"""
FEATURE = None
