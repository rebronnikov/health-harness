"""Pure-math tests for the targets compute lib. No DB, no contextvar, no clock —
every function under test is deterministic given its inputs.
"""
import pytest

from health.features import targets

# A tiny catalog to size suggestions from. `amount` fields are per the ref portion.
SKYR = {"name": "skyr", "ref_qty": 100, "ref_unit": "g", "kcal": 51,
        "protein_g": 9, "carb_g": 3.4, "fat_g": 0.1, "fibre_g": 0,
        "notes": "typical serving 210g"}                       # serving 210g: 107.1 kcal, 18.9g protein
LENTILS = {"name": "lentils", "ref_qty": 100, "ref_unit": "g", "kcal": 116,
           "protein_g": 9, "carb_g": 20, "fat_g": 0.4, "fibre_g": 8,
           "notes": "typical serving 200g"}                    # serving 200g: 232 kcal, 18g protein, 16g fibre
CHOCOLATE = {"name": "chocolate", "ref_qty": 100, "ref_unit": "g", "kcal": 535,
             "protein_g": 7.3, "carb_g": 59, "fat_g": 30, "fibre_g": 3,
             "notes": "typical serving 25g"}                   # serving 25g: 133.75 kcal (a "treat")
# High fibre, negligible protein -> shows up in fibre picks but NOT the top protein
# picks, so it survives name-dedup carrying closes="fibre".
BRAN = {"name": "bran", "ref_qty": 100, "ref_unit": "g", "kcal": 100,
        "protein_g": 0.5, "carb_g": 20, "fat_g": 1, "fibre_g": 10,
        "notes": "typical serving 30g"}                        # serving 30g: 30 kcal, 3g fibre
CATALOG = [SKYR, LENTILS, CHOCOLATE, BRAN]


def _remaining(kcal=0, protein_g=0, carb_g=0, fat_g=0, fibre_g=0):
    return targets.remaining(
        {"kcal": kcal, "protein_g": protein_g, "carb_g": carb_g, "fat_g": fat_g, "fibre_g": fibre_g}
    )


# --- remaining() ----------------------------------------------------------------

def test_remaining_kcal_ceiling_signed():
    under = _remaining(kcal=2000)["kcal"]
    assert under["direction"] == "ceiling"
    assert under["remaining"] == pytest.approx(200.0)          # 2200 - 2000
    over = _remaining(kcal=2500)["kcal"]
    assert over["remaining"] == pytest.approx(-300.0)          # ceilings go negative


def test_remaining_floors_clamp_at_zero():
    r = _remaining(protein_g=100, fibre_g=40)
    assert r["protein_g"]["direction"] == "floor"
    assert r["protein_g"]["still_needed"] == pytest.approx(50.0)   # 150 - 100
    assert r["fibre_g"]["still_needed"] == 0.0                     # 40 over the fibre floor -> clamped, not negative


def test_remaining_track_signed():
    r = _remaining(carb_g=300, fat_g=50)
    assert r["carb_g"]["direction"] == "track"
    assert r["carb_g"]["remaining"] == pytest.approx(-50.0)        # 250 - 300, tracked (may go negative)
    assert r["fat_g"]["remaining"] == pytest.approx(20.0)          # 70 - 50


# --- suggest() modes ------------------------------------------------------------

def test_suggest_protein_only_mode():
    rem = _remaining(kcal=1000, protein_g=100, fibre_g=26)        # protein short 50; fibre within tol
    out = targets.suggest(rem, CATALOG)
    assert out["mode"] == "protein"
    assert 0 < len(out["picks"]) <= 5
    assert all(p["closes"] == "protein" for p in out["picks"])


def test_suggest_fibre_only_mode():
    rem = _remaining(kcal=1000, protein_g=148, fibre_g=10)        # protein met (2 short, within 5); fibre short 18
    out = targets.suggest(rem, CATALOG)
    assert out["mode"] == "fibre"
    assert all(p["closes"] == "fibre" for p in out["picks"])


def test_suggest_protein_plus_fibre_mode():
    rem = _remaining(kcal=1000, protein_g=100, fibre_g=10)        # both floors unmet
    out = targets.suggest(rem, CATALOG)
    assert out["mode"] == "protein+fibre"
    closes = {p["closes"] for p in out["picks"]}
    assert "protein" in closes and "fibre" in closes


def test_suggest_treat_mode_when_floors_met():
    rem = _remaining(kcal=1000, protein_g=148, fibre_g=26)        # both floors met, 1200 kcal room
    out = targets.suggest(rem, CATALOG)
    assert out["mode"] == "treat"
    assert out["picks"] and out["picks"][0]["closes"] == "treat"


def test_suggest_none_when_floors_met_and_low_kcal():
    rem = _remaining(kcal=2150, protein_g=148, fibre_g=26)        # floors met, only 50 kcal left (< 150)
    assert targets.suggest(rem, CATALOG) is None


def test_suggest_over_budget_mode():
    rem = _remaining(kcal=2500, protein_g=100, fibre_g=26)        # over ceiling, protein still short
    out = targets.suggest(rem, CATALOG)
    assert out["mode"] == "over_budget"
    assert "note" in out


def test_suggest_over_budget_but_floors_met_is_none():
    rem = _remaining(kcal=2500, protein_g=148, fibre_g=26)        # over ceiling but nothing left to chase
    assert targets.suggest(rem, CATALOG) is None


# --- internals: serving/ranking/rounding ---------------------------------------

def test_serving_pick_filters_by_kcal_room():
    assert targets._serving_pick(SKYR, "protein_g", 50) is None   # a 210g serving is 107.1 kcal > 50
    pick = targets._serving_pick(SKYR, "protein_g", 200)
    assert pick is not None
    assert pick["covers_g"] == pick["protein_g"]                  # covers_g mirrors the closed nutrient


def test_rank_servings_orders_by_coverage_then_leanness():
    # Non-lean: most protein delivered per serving first. Lentils(18g) > skyr(18.9g)? skyr 18.9 > lentils 18.
    by_serving = targets._rank_servings(CATALOG, "protein_g", 3000, top=3)
    assert by_serving[0]["name"] == "skyr"
    # Lean: most protein per kcal first -> skyr (9/51) beats lentils (9/116) beats chocolate.
    lean = targets._rank_servings(CATALOG, "protein_g", None, top=3, lean=True)
    assert lean[0]["name"] == "skyr"


@pytest.mark.parametrize("amount,unit,expected", [
    (1.0, "each", 1.0),
    (0.2, "each", 0.5),      # min 0.5
    (1.3, "each", 1.5),      # nearest 0.5
    (12, "g", 10),           # nearest 5
    (3, "g", 5),             # min 5
    (23, "g", 25),
    (23, "ml", 25),
])
def test_round_amount(amount, unit, expected):
    assert targets._round_amount(amount, unit) == expected


def test_typical_serving_from_notes():
    assert targets._typical_serving(SKYR)["amount"] == 210.0             # parsed from the note
    each = {"ref_unit": "each", "ref_qty": 1, "notes": "typical serving 210g"}
    assert targets._typical_serving(each) == {"amount": 1, "unit": "each"}   # each -> ref portion, note ignored
    unit_mismatch = {"ref_unit": "ml", "ref_qty": 100, "notes": "typical serving 210g"}
    assert targets._typical_serving(unit_mismatch)["amount"] == 100         # note is in g, food is ml -> fallback


def test_dedup_preserves_order_and_drops_repeat_names():
    picks = [{"name": "a"}, {"name": "b"}, {"name": "a"}]
    assert [p["name"] for p in targets._dedup(picks)] == ["a", "b"]
