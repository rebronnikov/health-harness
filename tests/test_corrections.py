"""Corrections — edit/delete as append-only events, and the guards that keep them honest.

Two things carry the design and are tested hardest here: an amount edit rescales macros in
the EXECUTOR (Claude never does arithmetic), and every event pins the target row's
`ingested_at` so a correction that has been superseded matches nothing. The replay half of
that second guarantee lives in test_rebuild_roundtrip.py.
"""
import json

import pytest

from health.core import harness
from health.core.contracts import NormalizedMessage
from health.features import corrections, food, steps
from health.store.connection import get_conn

from conftest import DEFAULT_TS, rows, text_block, tool_block

DAY = "2026-07-14"
FOOD_KEY = ("date", "meal", "food_slug", "ingested_at")


def _log_skyr(grams=100, meal="breakfast"):
    """One food_log row, plus the (key, ingested_at) pair query_log would hand back."""
    (row,) = food.log_food(DAY, meal, [{"name": "skyr", "quantity": grams, "unit": "g"}])["logged"]
    return row, {k: row[k] for k in FOOD_KEY}, row["ingested_at"]


def _jsonl(tmp_path, name):
    path = tmp_path / f"{name}.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# --- edit -------------------------------------------------------------------

def test_amount_edit_rescales_macros_from_the_rows_own_numbers(ctx, catalog):
    """'the skyr was 50g, not 100'. The executor scales the row's frozen macros — the
    model passes the amount alone and never computes a calorie."""
    ctx()
    _, key, at = _log_skyr(100)                                 # skyr: 51 kcal / 100 g
    ctx(text="the skyr was 50g")
    out = corrections.edit_entry("food_log", key, at, {"amount": 50})

    (stored,) = rows(get_conn(), "SELECT * FROM food_log")
    assert stored["amount"] == 50
    assert stored["kcal"] == pytest.approx(25.5)                # 51 * 0.5
    assert stored["protein_g"] == pytest.approx(4.5)            # 9 * 0.5
    assert out["applied"]["kcal"] == pytest.approx(25.5)


def test_edit_preserves_the_key_and_ingested_at(ctx, catalog):
    """An edit that moved `ingested_at` would slip out from under its own guard on
    replay, and out from under any later correction."""
    ctx()
    row, key, at = _log_skyr(100)
    ctx(text="make it 50g")
    corrections.edit_entry("food_log", key, at, {"amount": 50})

    (stored,) = rows(get_conn(), "SELECT * FROM food_log")
    assert {k: stored[k] for k in FOOD_KEY} == key
    assert stored["ingested_at"] == at


def test_explicit_values_win_over_the_rescale(ctx, catalog):
    ctx()
    _, key, at = _log_skyr(100)
    ctx(text="50g, and the label said 30 kcal")
    corrections.edit_entry("food_log", key, at, {"amount": 50, "kcal": 30})

    (stored,) = rows(get_conn(), "SELECT * FROM food_log")
    assert stored["kcal"] == 30                                 # stated verbatim
    assert stored["protein_g"] == pytest.approx(4.5)            # still rescaled


def test_edit_a_scalar_domain(ctx):
    ctx(text="9000 steps")
    row = steps.log_steps(DAY, 9000)
    ctx(text="that was 9500")
    corrections.edit_entry("steps", {"date": DAY, "source": row["source"]},
                           row["ingested_at"], {"count": 9500})
    assert rows(get_conn(), "SELECT count FROM steps")[0]["count"] == 9500


def test_chained_edits_converge_on_the_last(ctx, catalog):
    ctx()
    _, key, at = _log_skyr(100)
    ctx(text="50g")
    corrections.edit_entry("food_log", key, at, {"amount": 50})
    ctx(text="no, 40g")
    corrections.edit_entry("food_log", key, at, {"amount": 40})

    (stored,) = rows(get_conn(), "SELECT * FROM food_log")
    assert stored["amount"] == 40
    assert stored["kcal"] == pytest.approx(20.4)                # 51 * 0.4, off the ORIGINAL row


# --- delete -----------------------------------------------------------------

def test_delete_removes_the_row_and_day_totals_follow(ctx, catalog):
    ctx()
    _log_skyr(100, meal="breakfast")
    _, key, at = _log_skyr(210, meal="snack")
    assert food.get_day_totals(DAY)["kcal"] == pytest.approx(158.1)

    ctx(text="drop that snack")
    corrections.delete_entry("food_log", key, at)

    assert len(rows(get_conn(), "SELECT * FROM food_log")) == 1
    assert food.get_day_totals(DAY)["kcal"] == pytest.approx(51.0)


# --- guards: nothing is written when the call is bad ------------------------

def test_unknown_row_raises_and_writes_nothing(ctx, catalog, tmp_path):
    ctx()
    _, key, at = _log_skyr(100)
    with pytest.raises(ValueError, match="No food_log row matches"):
        corrections.delete_entry("food_log", {**key, "food_slug": "nope",
                                              "ingested_at": at}, at)
    assert _jsonl(tmp_path, "corrections") == []
    assert len(rows(get_conn(), "SELECT * FROM food_log")) == 1


def test_stale_ingested_at_raises(ctx):
    """The row was overwritten after query_log returned it, so the correction the user is
    describing no longer applies to what's stored."""
    ctx(text="9000 steps")
    row = steps.log_steps(DAY, 9000)
    steps.log_steps(DAY, 9200)                                  # re-log -> new ingested_at
    with pytest.raises(ValueError, match="overwritten"):
        corrections.edit_entry("steps", {"date": DAY, "source": row["source"]},
                               row["ingested_at"], {"count": 9500})


def test_editing_a_key_field_raises_and_writes_nothing(ctx, catalog, tmp_path):
    ctx()
    _, key, at = _log_skyr(100)
    with pytest.raises(ValueError, match="can't be edited"):
        corrections.edit_entry("food_log", key, at, {"meal": "lunch"})
    assert _jsonl(tmp_path, "corrections") == []


def test_mismatched_key_raises(ctx, catalog):
    ctx()
    _, key, at = _log_skyr(100)
    with pytest.raises(ValueError, match="must have exactly the fields"):
        corrections.edit_entry("food_log", {"date": DAY}, at, {"amount": 50})


def test_food_catalog_is_not_correctable(ctx, catalog):
    """It's a lookup table, not a log — save_food overwrites an entry by name."""
    with pytest.raises(ValueError, match="save_food"):
        corrections.query_log("food_catalog", DAY, DAY)


# --- query_log --------------------------------------------------------------

def test_query_log_hands_back_a_ready_made_key(ctx, catalog):
    ctx()
    row, key, at = _log_skyr(100)
    out = corrections.query_log("food_log", DAY, DAY)

    assert len(out["rows"]) == 1
    assert out["rows"][0]["key"] == key
    assert out["rows"][0]["ingested_at"] == at
    assert out["truncated"] is False


def test_query_log_lists_editable_fields_without_key_or_provenance(ctx, catalog):
    ctx()
    _log_skyr(100)
    editable = corrections.query_log("food_log", DAY, DAY)["editable_fields"]
    assert "amount" in editable and "kcal" in editable
    for excluded in (*FOOD_KEY, "raw", "schema_version"):
        assert excluded not in editable


def test_query_log_filters_by_text_and_date(ctx, catalog):
    ctx()
    _log_skyr(100, meal="breakfast")
    food.log_food(DAY, "lunch", [{"name": "banana", "quantity": 1, "unit": "each"}])

    assert len(corrections.query_log("food_log", DAY, DAY)["rows"]) == 2
    only = corrections.query_log("food_log", DAY, DAY, filter_text="banana")["rows"]
    assert [r["food_slug"] for r in only] == ["banana"]
    assert corrections.query_log("food_log", "2026-07-01", "2026-07-02")["rows"] == []


# --- the event itself -------------------------------------------------------

def test_the_correction_is_an_append_only_event_with_live_provenance(ctx, catalog, tmp_path):
    ctx()
    _, key, at = _log_skyr(100)
    ctx(text="actually the skyr was 50g", source="telegram")
    corrections.edit_entry("food_log", key, at, {"amount": 50})

    (event,) = _jsonl(tmp_path, "corrections")
    assert event["action"] == "edit"
    assert event["target_domain"] == "food_log"
    assert event["target_ingested_at"] == at
    assert event["changes"]["amount"] == 50
    assert event["changes"]["kcal"] == pytest.approx(25.5)      # resolved, not recomputed later
    assert event["raw"] == "actually the skyr was 50g"          # runtime-stamped, not model-authored
    assert event["source"] == "telegram"

    # The food_log line it corrects is untouched — JSONL is never mutated.
    assert [r["amount"] for r in _jsonl(tmp_path, "food_log")] == [100]


# --- through the harness ----------------------------------------------------

def test_harness_query_then_delete(ctx, catalog, fake_client):
    """The real shape: query_log, copy the key back, delete. Also exercises the error
    path — a stale key comes back as an is_error tool_result the model can recover from."""
    ctx()
    _, key, at = _log_skyr(100)

    fake_client.script(
        [tool_block("query_log", {"domain": "food_log", "start_date": DAY, "end_date": DAY},
                    id="tu-1")],
        [tool_block("delete_entry", {"domain": "food_log", "key": {**key, "food_slug": "gone"},
                                     "ingested_at": at}, id="tu-2")],      # stale -> is_error
        [tool_block("delete_entry", {"domain": "food_log", "key": key, "ingested_at": at},
                    id="tu-3")],
        [text_block("Deleted the skyr.")],
    )
    reply = harness.handle(NormalizedMessage(source="telegram", reply_handle="chat-1",
                                             timestamp=DEFAULT_TS, text="delete that skyr"))

    assert reply == "Deleted the skyr."
    assert rows(get_conn(), "SELECT * FROM food_log") == []

    failed = fake_client.calls[2]["messages"][-1]["content"][0]
    assert failed["is_error"] is True and "No food_log row matches" in failed["content"]
