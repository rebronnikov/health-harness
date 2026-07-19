"""The /catalog listing: the pure `render_catalog_table` (fixed-width alignment,
number/portion formatting, name truncation) and `list_catalog`'s name ordering.
No Telegram, no markup — the adapter wraps this plain text in a <pre> block.
"""
from health.features import food


def _row(name, ref_qty, ref_unit, kcal, protein_g, carb_g, fat_g, fibre_g, notes=""):
    return {
        "name": name, "ref_qty": ref_qty, "ref_unit": ref_unit, "kcal": kcal,
        "protein_g": protein_g, "carb_g": carb_g, "fat_g": fat_g, "fibre_g": fibre_g,
        "notes": notes,
    }


def test_render_lists_every_row_with_a_header():
    table = food.render_catalog_table([
        _row("Banana", 1, "each", 108, 1.44, 24.4, 0.12, 0),
        _row("Skyr", 100, "g", 51, 9, 3.4, 0.1, 0),
    ])
    lines = table.splitlines()
    assert lines[0] == "Food catalog · 2 items"
    assert lines[1] == ""
    assert lines[2].split() == ["name", "port", "kcal", "P", "C", "F", "fib"]
    body = lines[3:]
    assert len(body) == 2                                    # one line per food, none dropped
    # kcal rounded to a whole number; macros to one decimal; `each` -> `ea`.
    assert body[0].split() == ["Banana", "1ea", "108", "1.4", "24.4", "0.1", "0.0"]
    assert body[1].split() == ["Skyr", "100g", "51", "9.0", "3.4", "0.1", "0.0"]


def test_render_columns_are_fixed_width():
    table = food.render_catalog_table([
        _row("Banana", 1, "each", 108, 1.44, 24.4, 0.12, 0),
        _row("Chicken breast", 100, "g", 107, 22.9, 0, 1.6, 0),
        _row("Milk", 100, "ml", 46, 3.5, 4.6, 1.5, 0),
    ])
    header, *body = table.splitlines()[2:]
    assert all(len(line) == len(header) for line in body)   # everything lines up under the header


def test_render_truncates_long_names_keeping_alignment():
    long = "bulgur wheat with red and white quinoa"
    table = food.render_catalog_table([_row(long, 100, "g", 130, 4.9, 24, 1.2, 2.1)])
    header, row = table.splitlines()[2], table.splitlines()[3]
    assert row.startswith(long[:14] + "…")                  # trimmed to the column, ellipsis appended
    assert len(row) == len(header)                          # truncation preserves the fixed width


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
    assert empty.splitlines()[0] == "Food catalog · 0 items"
    assert len(empty.splitlines()) == 3                     # title, blank, header — no data rows


def test_list_catalog_is_name_ordered(catalog):
    # `catalog` fixture saves skyr, banana, salmon (in that order).
    names = [r["name"] for r in food.list_catalog()]
    assert names == ["banana", "salmon", "skyr"]            # ORDER BY name, not insertion order
