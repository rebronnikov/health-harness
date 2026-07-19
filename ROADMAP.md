# Roadmap

Tracked follow-ups for the health bot. Ordered roughly by priority; not commitments.

## In progress

### Name matching for the catalog
Surface the saved-food catalog to the bot so it logs staples by their exact name +
a compatible unit (e.g. "cheddar" → `cheddar cheese`, "a slice of sourdough" →
the per-slice entry) instead of estimating. When the bot isn't confident which
saved food is meant (e.g. two salmon fillets), it picks the likeliest reading and
says what it assumed — it does **not** ask.  Implemented as
a `system_context` on `CATALOG_FEATURE` in `health/features/food.py`.

## Next

### Gate guessed *amounts*, not just guessed macros
The estimate gate keys on the wrong thing. `log_food` defers to
`propose_food_estimate` → `confirm_pending` only when **macros** must be estimated; a
catalog hit whose **quantity** Claude guessed auto-commits on the spot. Now that the bot
is forbidden to ask before logging, it guesses amounts routinely — and a guessed amount
is as much a fabrication as a guessed macro.

When a mixed dish omits an ingredient weight, an inferred quantity can be wrong. Corrections let the user fix that row.

Likely shape: Claude flags a per-item `amount_assumed: true` (it knows when it guessed);
`log_food` routes those items into `_pending` instead of writing them, still resolving
the macros from the catalog itself — so "Claude never does arithmetic" holds and the
existing confirm path does the rest. Alternative: activate the stubbed `corrections`
feature so a wrong row can be amended. Not yet decided.

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
- **`/catalog` table too wide for iPhone** — the `/catalog` listing renders as a
  fixed-width `<pre>` table (~49 chars/row: name + port + kcal + P/C/F/fib). On a
  narrow iPhone screen the last column (`fib`) wraps onto the next line instead of
  scrolling, breaking the alignment. Fix by fitting a row into a phone-safe width
  (~30–34 chars). Options: narrow the name column and tighten macro widths; drop or
  fold the fibre column (e.g. show it only in a per-item detail view); or switch to a
  two-line-per-item layout (name+portion on line 1, macros on line 2). Lives in
  `render_catalog_table` in `health/features/food.py` (the pure renderer) — the
  Telegram `<pre>` wrapping in `telegram_adapter.py` doesn't need to change.

## Pending data / quirks

- Add **`a second salmon fillet`** (a second salmon distinct from the home fillet).
- **Buckwheat** is stored as dry/uncooked values — log the dry weight.
