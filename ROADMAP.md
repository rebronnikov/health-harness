# Roadmap

Tracked follow-ups for the health bot. Ordered roughly by priority; not commitments.

## Done

- **Corrections (edit / delete)** — `query_log`, `edit_entry`, `delete_entry` in
  `health/features/corrections.py`. Append-only events; `project()` applies UPDATE/DELETE
  to the target table; every event pins the target's `ingested_at` so a superseded
  correction replays as a no-op. Must stay last in `FEATURES`.
- **Weekly review / analytics** — `get_summary` in `health/features/review.py`, the first
  read-only feature (no schema/domain/project).
- **Web nutrition lookup** — `web_search` declared in the harness; looked-up macros route
  through the existing propose → confirm gate, never a new write path.
- **Bot may ask one question** — the flat "never ask" rule is now bias-to-act with a
  one-question cap. See CLAUDE.md for the three guardrails that make it safe.
- **`/catalog` too wide for iPhone** — two lines per food; names are no longer truncated.

## In progress

### Name matching for the catalog
Surface the saved-food catalog to the bot so it logs staples by their exact name +
a compatible unit (e.g. "cheddar" → `cheddar cheese`, "a slice of sourdough" →
the per-slice entry) instead of estimating. When the bot isn't confident which
saved food is meant (e.g. two salmon fillets), it picks the likeliest reading and
says what it assumed — it does **not** ask.  Implemented as
a `system_context` on `CATALOG_FEATURE` in `health/features/food.py`.

## Next

### Meals layer (repeatable meals)
Two-layer food model. Ingredients already exist (the `food_catalog`). A **meal** is
a named bundle that *references* catalog ingredients with default amounts — e.g.
`breakfast = jumbo oats 80g + milk 350ml + blueberries 50g`. Logging "ate breakfast"
expands the meal into `food_log` rows, pulling each ingredient's macros from the
catalog, scaling, and freezing the resolved numbers into the log (so history stays
self-contained). Behaviour agreed with the owner:
- **Default amounts** per component, with **per-day override** ("breakfast but 150g
  blueberries") and **adding extras** ("breakfast + a banana").
- **Strictly two levels** — meals contain ingredients, not other meals.
- A meal item not yet in the catalog goes through the existing estimate→confirm flow
  and can be saved as an ingredient.
Likely shape: a new `meal_catalog` (slug + component list) mirroring how
`food_catalog` + `log_food` already compose; a `log_meal(meal_slug, date, overrides)`
executor that expands to `food_log` rows. Two things it will want that don't exist yet:
`log_food`'s body refactored into a shared `_resolve_and_log(date, meal, items)` so meal
expansion reuses catalog scaling, the estimate path, occasion numbering and accumulation
for free; and `"meal_catalog"` added to `query_log`'s domain enum. Corrections already
work on the expanded rows untouched, since those are ordinary `food_log` entries.

## Later

- **Targets: bot-settable + weight-derived** — the daily targets (calories / protein /
  fibre) now live as code constants in `health/features/targets.py`
  (`TARGETS = {kcal, protein_g, fibre_g}`; calories a ceiling, protein/fibre floors),
  replacing the old `PROTEIN_TARGET_G`/`FIBRE_TARGET_G` stopgaps in `food.py`.
  `remaining()` frames consumed-vs-target and `suggest()` proposes catalog foods to
  close the gaps; `food._status` surfaces both after every log and adds evening
  suggestions past `SUGGEST_AFTER_HOUR`. Still to do: set targets from a bot message
  (needs a persisted store instead of constants), derive them from body weight + goal
  once `measurements` lands, and swap the placeholder numbers for the owner's aligned values.
- **Richer summaries** — `get_summary` returns food/steps/weight/measurements over a
  range. Not yet in it: week-over-week comparison, day-of-week patterns, and the catalog
  `notes` for micronutrient / composition context.
- **Exercise** — `exercise.py` stub, the last one left.
- **Catalog search tool** — if the catalog grows to hundreds of items, replace the
  always-on catalog list in the bot's context with an on-demand `find_food(query)`
  search tool.
- **Correcting a catalog entry by message** — `corrections` deliberately covers the dated
  logs only. `save_food` overwrites an entry by name, but there's no way to *delete* a
  saved food (e.g. a duplicate) short of editing `data/food_catalog.jsonl` + rebuilding.

## Pending data / quirks

- Add **`a second salmon fillet`** (a second salmon distinct from the home fillet).
- **Buckwheat** is stored as dry/uncooked values — log the dry weight.
