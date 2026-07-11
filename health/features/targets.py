"""Deferred (phase 1). Copy steps.py as the shape.

Plan: deterministic TDEE / remaining-calories math exposed as a query tool
(`remaining_calories`). All arithmetic lives in the executor — Claude only dispatches.
This feature is read/compute-oriented and may not own a JSONL domain.
"""
FEATURE = None
