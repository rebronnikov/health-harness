"""food_catalog: slug/unit normalization on save, slug-keyed upsert, and the
notes backfill that keeps replaying pre-notes JSONL lines lossless.
"""
from health.features import food
from health.store.connection import get_conn

from conftest import rows


def test_save_food_normalizes_slug_and_unit(ctx):
    ctx(source="seed")
    saved = food.save_food("Cheddar Cheese", 30, "grams", 125, 7.6, 0, 10.5, 0)["saved"]
    assert saved["slug"] == "cheddar_cheese"                     # lowercased, non-alnum -> _
    assert saved["ref_unit"] == "g"                             # "grams" alias normalized


def test_save_food_upserts_by_slug(ctx, tmp_path):
    ctx(source="seed")
    food.save_food("oats", 100, "g", 380, 13, 67, 7, 10)
    food.save_food("oats", 100, "g", 400, 13, 67, 7, 10)        # same slug -> overwrite
    catalog = rows(get_conn(), "SELECT slug, kcal FROM food_catalog WHERE slug='oats'")
    assert catalog == [{"slug": "oats", "kcal": 400}]          # one row, last wins
    assert len((tmp_path / "food_catalog.jsonl").read_text().splitlines()) == 2  # both appended


def test_project_catalog_backfills_missing_notes(ctx):
    # A row shaped like an OLD JSONL line (written before `notes` existed).
    legacy = {
        "slug": "legacy", "name": "legacy food", "ref_qty": 100, "ref_unit": "g",
        "kcal": 100, "protein_g": 5, "carb_g": 10, "fat_g": 2, "fibre_g": 1,
        "raw": "seed", "ingested_at": "2026-01-01T00:00:00+00:00", "schema_version": 1,
    }
    conn = get_conn()
    food.project_catalog(conn, legacy)                          # must not KeyError on missing notes
    conn.commit()
    (stored,) = rows(conn, "SELECT notes FROM food_catalog WHERE slug='legacy'")
    assert stored["notes"] == ""                               # defaulted, replay stays lossless
