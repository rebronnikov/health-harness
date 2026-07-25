"""The /catalog listing: the pure `render_catalog_table` (two-line-per-food layout,
number/portion formatting, phone-safe width) and `list_catalog`'s name ordering.
No Telegram, no markup — the adapter wraps this plain text in a <pre> block.
"""
from health.features import food

PHONE_SAFE = 34   # a <pre> line wider than this wraps on a narrow iPhone screen


def _row(name, ref_qty, ref_unit, kcal, protein_g, carb_g, fat_g, fibre_g, notes=""):
    return {
        "name": name, "ref_qty": ref_qty, "ref_unit": ref_unit, "kcal": kcal,
        "protein_g": protein_g, "carb_g": carb_g, "fat_g": fat_g, "fibre_g": fibre_g,
        "notes": notes,
    }


def test_render_gives_each_food_a_name_line_and_a_macro_line():
    table = food.render_catalog_table([
        _row("Banana", 1, "each", 108, 1.44, 24.4, 0.12, 0),
        _row("Skyr", 100, "g", 51, 9, 3.4, 0.1, 0),
    ])
    lines = table.splitlines()
    assert lines[0] == "Food catalog · 2 items"
    assert lines[1] == ""
    assert lines[2:] == [                                    # two lines per food, none dropped
        "Banana · 1ea",
        "  108kcal  P1.4 C24 F0.1 fib0",
        "Skyr · 100g",
        "  51kcal  P9 C3.4 F0.1 fib0",
    ]


def test_macro_lines_fit_a_phone_screen():
    """The reason for the layout: a wider line wraps in Telegram's <pre> block, and the
    old ~49-char fixed-width row dropped its last column onto the next line."""
    table = food.render_catalog_table([
        _row("chicken breast", 100, "g", 107, 22.9, 0, 1.6, 0),
        _row("jumbo oats", 100, "g", 379, 13.2, 58.7, 8.0, 10.1),
        _row("olive oil", 100, "ml", 884, 0, 0, 100, 0),
    ])
    macro_lines = [ln for ln in table.splitlines() if ln.startswith("  ")]
    assert len(macro_lines) == 3
    assert all(len(ln) <= PHONE_SAFE for ln in macro_lines)


def test_render_keeps_long_names_in_full():
    """Logging a saved food means naming it exactly, so a truncated name makes the
    listing useless. The name gets its own line precisely so it never has to be cut."""
    long = "bulgur wheat with red and white quinoa"
    table = food.render_catalog_table([_row(long, 100, "g", 130, 4.9, 24, 1.2, 2.1)])
    assert table.splitlines()[2] == f"{long} · 100g"


def test_macros_keep_a_decimal_only_where_rounding_would_mislead():
    table = food.render_catalog_table([_row("egg", 1, "each", 71, 6.3, 0.4, 4.8, 0)])
    macros = table.splitlines()[3]
    assert macros == "  71kcal  P6.3 C0.4 F4.8 fib0"       # 0.4g is not 0g
    big = food.render_catalog_table([_row("oats", 100, "g", 379, 13.2, 58.7, 8.0, 10.1)])
    assert big.splitlines()[3] == "  379kcal  P13 C59 F8 fib10"   # whole grams above 10


def test_render_formats_each_portion_unit():
    table = food.render_catalog_table([
        _row("egg", 1, "each", 71, 7, 0, 4.8, 0),
        _row("oats", 40, "g", 148, 4.5, 26, 2.3, 3.3),
        _row("milk", 100, "ml", 46, 3.5, 4.6, 1.5, 0),
    ])
    assert "1ea" in table and "40g" in table and "100ml" in table


def test_render_singular_and_empty_counts():
    one = food.render_catalog_table([_row("banana", 1, "each", 108, 1.4, 24.4, 0.1, 0)])
    assert one.splitlines()[0] == "Food catalog · 1 item"
    empty = food.render_catalog_table([])
    assert empty.splitlines() == ["Food catalog · 0 items"]   # title only, no food lines


def test_list_catalog_is_name_ordered(catalog):
    # `catalog` fixture saves skyr, banana, salmon (in that order).
    names = [r["name"] for r in food.list_catalog()]
    assert names == ["banana", "salmon", "skyr"]            # ORDER BY name, not insertion order
