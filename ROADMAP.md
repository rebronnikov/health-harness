# Roadmap

Tracked follow-ups for the health bot. Ordered roughly by priority; not commitments.

## In progress

### Name matching for the catalog
Surface the saved-food catalog to the bot so it logs staples by their exact name +
a compatible unit (e.g. "cheddar" → `cheddar cheese`, "a slice of sourdough" →
the per-slice entry) instead of estimating. When the bot isn't confident which
saved food is meant (e.g. two salmon fillets), it asks to clarify rather than
guessing. Implemented as a `system_context` on `CATALOG_FEATURE` in
`health/features/food.py`.

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
executor that expands to `food_log` rows.

### Targets feature
Persisted per-user targets (calories / protein / fibre) + deterministic
"remaining today" math exposed as a query tool. Replaces the hardcoded stopgap
constants `PROTEIN_TARGET_G = 150` and `FIBRE_TARGET_G = 28` in
`health/features/food.py`. Read/compute-oriented (`targets.py` stub reserved for it);
may derive from body weight + goal once measurements exist.

## Later

- **Weight & body measurements** — `measurements.py` stub. Enables weight-loss
  tracking and target derivation.
- **Corrections (edit / delete)** — `corrections.py` stub. Edits/deletes recorded as
  new append-only JSONL events (never mutate prior lines); projection reflects the
  latest via the same dedupe upsert. Today the only way to fix an entry is to
  re-log over the same dedupe key.
- **Weekly review / analytics** — `review.py` stub. Read-only summaries over the
  SQLite projection (uses the catalog `notes` for micronutrient / composition
  context).
- **Catalog search tool** — if the catalog grows to hundreds of items, replace the
  always-on catalog list in the bot's context with an on-demand `find_food(query)`
  search tool.

## Pending data / quirks

- Add **`a second salmon fillet`** (a second salmon distinct from the home fillet).
- **Buckwheat** is stored as dry/uncooked values — log the dry weight.
