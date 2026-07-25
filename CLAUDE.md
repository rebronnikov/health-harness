# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Single-user personal health tracker. the owner sends natural-language Telegram messages ("40g jumbo oats", "12000 steps", "weight 72.6", "1 latte"); Claude routes+extracts them into tool calls; deterministic executors write the data. The bot runs locally on the owner's Mac as a launchd agent that long-polls Telegram.

## Commands

```bash
# Run the bot in the foreground (needs .env; see Config)
.venv/bin/python -m health.main

# Manage the resident launchd agent (the "live" bot)
./scripts/bot.sh install     # copy plist, (re)load, start — use this to (re)start after a clean stop
./scripts/bot.sh restart     # kickstart -k; only works if already loaded
./scripts/bot.sh status      # state / pid / last exit
./scripts/bot.sh logs        # tail data/logs/bot.err.log

# Rebuild the SQLite projection from the JSONL system of record
.venv/bin/python -m health.rebuild_db [--skip-bad-tail]

# Bulk-load / update catalog staples (idempotent; dedupes on slug)
.venv/bin/python -m health.seed_catalog

# Run the test suite (install dev deps once: pip install -r requirements-dev.txt)
.venv/bin/python -m pytest
```

- **After editing code, the live bot needs `./scripts/bot.sh restart`** — launchd runs a resident copy; edits don't take effect until restart.
- **Tests** live in `tests/` (pytest; dev-only dep in `requirements-dev.txt`). Run `.venv/bin/python -m pytest`. They cover the core invariants — food scaling/`each`-vs-mass units, the estimate gating, runtime provenance + JSONL-before-projection write order, and the `rebuild_db` round-trip (which now includes corrections *and* `set_meal`: deleting either `ingested_at` guard fails several tests, including snapshot equality) — plus the pure `targets.py` math. **The suite is hermetic and MUST stay that way:** `tests/conftest.py` redirects `DATA_DIR`/`DB_PATH` to a tempdir *before the first `health` import* so tests never touch the owner's real `data/`; `tests/test_isolation_guardrail.py` fails loudly if that ever breaks. There is no linter configured. For runtime behaviour a test can't reach (Telegram wiring), still verify by driving the flow and inspecting `data/*.jsonl` + the projection. The harness tool-use loop is covered via the `fake_client` fixture in `conftest.py` (a scripted Anthropic stand-in; `calls[i]["messages"]` is what makes context assembly assertable) — see `tests/test_harness_history.py`.
- Logs: `data/logs/bot.err.log` holds stderr *and* all app logging (`bot.out.log` is usually empty). Note: the httpx logger is silenced to WARNING because getUpdates URLs embed the bot token. **For "what actually happened in that conversation?" read `data/messages/<date>.jsonl` instead** — `bot.err.log` deliberately carries no message text.

## Architecture — the chassis

Every message follows one feature-agnostic path (`fitness-harness-architecture.md` is the fuller spec):

```
Telegram update → adapter → NormalizedMessage → harness.handle → Claude tool-use loop → executor → writer.record → JSONL (+ SQLite projection) → reply
```

- **Adapter** (`health/adapters/telegram_adapter.py`) — the only channel-specific code. Translates updates to/from `NormalizedMessage`, enforces the allowlist, runs the blocking `harness.handle` in a worker thread, and always replies (on failure it sends a "nothing was logged" message — a handler exception never leaves the user hanging).
- **Harness** (`health/core/harness.py`) — the only "smart" part. Builds a thin system prompt, calls the Anthropic API with *all* registered tools, and loops running executors until Claude returns plain text. Claude does **routing + extraction only**. The loop is bounded at `MAX_LOOP_ITERATIONS` (12) and returns an honest "hit my internal tool-call limit" reply on exhaustion. It also declares `SERVER_TOOLS` — `web_search` (localised to GB) and `web_fetch` — which run on Anthropic's side and have no executor; that's chassis infrastructure like `MODEL`, not a feature, so invariant 5 holds. Server tools make `stop_reason: "pause_turn"` reachable: the loop re-sends the paused turn unchanged to resume it, *before* the `tool_use` check, because read as a final response it would ship a truncated reply. It also writes the conversation transcript (`health/store/msg_log.py`): inbound message, every tool call with its result or error, and the final reply.
- **Executors** own the *work* — deterministic, dumb. They build SI-unit fields and call `writer.record()`; they never touch raw text, time, source, or SQL directly.

**Core invariants (breaking one means the feature is wrong, not the rule):**
1. **Claude dispatches; it never computes.** All arithmetic lives in executors (see the "never do arithmetic" rule in `health/core/prompts.py` and the sizing math in `targets.py`).
2. **JSONL is the system of record; SQLite is a disposable projection.** `writer.record()` appends the canonical JSONL line *first*, then upserts the projection, then appends provenance to `ingest_log.jsonl`. (`data/messages/` is *not* a system of record — see the transcript note below.)
3. **`project()` is one idempotent upsert used by BOTH live ingest and `rebuild_db`** — that's what guarantees a rebuild reproduces live-ingest state. It's an `ON CONFLICT(dedupe_key) DO UPDATE`. **The one sanctioned exception is `corrections`**, which owns no table and instead applies an `UPDATE`/`DELETE`/`relabel` to the *target* feature's table — still idempotent, and still the same function for both callers.
4. **Provenance is runtime-authored, never model-authored.** `raw`/`source`/`ingested_at`/`schema_version` are stamped by `writer.record()` from the `current_message` contextvar (`health/core/contracts.py`), set for the duration of each turn.
5. **The core never references a specific feature** — it only knows the registry.

The system prompt is deliberately thin (just today's date/time + routing rules). Conversation history is **bounded and ephemeral** (`health/core/history.py`): the last 10 turns per chat, dropped after 90 minutes idle, in-process only (lost on restart), and **plain text only** — never the `tool_use`/`tool_result` blocks, which would risk an orphaned `tool_use` and re-feed stale tool results. It feeds the model's context and nothing else; provenance is still stamped from the live message, never from history. A feature that needs *structured* cross-turn state still surfaces it via a `system_context` fragment (e.g. food's pending-estimate awaiting "yes") — history is the conversational fallback, not a substitute.

**The bot is biased to act, and may ask at most one question** (`prompts.py`, and the SAVED FOODS fragment in `food.py`). Default behaviour is unchanged: estimate, log, and say what you assumed; the user corrects. Asking is allowed only when it genuinely can't act — a destructive instruction with several plausible targets, a self-contradicting message, an unboundable amount — and the question must restate the item so the answer stands alone.

This used to be a flat "never ask", after the catalog prompt caused repeated clarification. It was relaxed deliberately, and the three things that made that failure possible are gone: there was **no conversation history at all** then, so the answers landed on turns that couldn't remember asking (history exists now, and `IDLE_EXPIRY` is 90 minutes); nothing capped the number of questions (one per reply now, asserted in `tests/test_prompts.py`); and acting wrongly was unfixable, which made asking feel safer than guessing (corrections make a wrong guess a one-message fix). If you find yourself re-tightening this, check those three still hold rather than just reverting the wording.

## Adding or changing a feature

A feature is one module under `health/features/` that declares a `Feature(...)` — `tools` (Anthropic schemas), `executors` (name→fn), `schema` (DDL, uses `IF NOT EXISTS`), `domain` (its `data/<domain>.jsonl` + table), `dedupe_key`, `project` (the upsert), and an optional `system_context`. **`health/features/steps.py` is the reference template**; copy its shape. The **only** shared-code edit is adding the feature to `FEATURES` in `health/core/registry.py`. Everything else lives in the module.

A **read-only** feature just omits `schema`/`domain`/`project` — the defaults allow it and `ingesting_features()` filters it out, so it adds a tool without adding a system of record. `review.py` is the template for that shape.

Active features: `steps`, `weight`, `food` (two features — catalog + log), `measurements`, `exercise`, `review` (read-only), `corrections`. No stubs remain; the meals layer is queued in `ROADMAP.md`. `targets.py` is a **compute lib, not a routed feature** (no tools) — imported by `food` and `review`.

**`corrections` MUST STAY LAST in `FEATURES`.** `rebuild_db` replays domains in list order, and a correction has to land after the rows it corrects. Registry order is load-bearing here, not cosmetic — `tests/test_registry.py` asserts it.

## Data model specifics (the non-obvious parts)

- **Food is two features in one module** (`health/features/food.py`): `food_catalog` (reusable foods, macros stored per a reference portion `ref_qty`/`ref_unit` ∈ `g|ml|each`, dedupe on `slug`) and `food_log` (one row per eating event). Logging scales catalog macros by `amount / ref_qty`.
- **`each`-unit catalog foods scale by count, not weight.** A food stored as `ref_unit="each"` (e.g. a banana, an egg) counts whole units — a stated gram weight is ignored. Foods you weigh per portion (e.g. salmon) should be stored per `100 g` so `"2 fillets 90g each"` scales to 180 g. Gram-weights hidden in `notes` are human-readable only; the executor doesn't parse them for scaling (except `targets._typical_serving`, which reads a "typical serving Ng" note for suggestion sizing).
- **User-given macros can be stated per a different reference amount.** A message like "per 100g: 184 kcal … I ate 63g" is logged via `log_food` with the macros verbatim plus `macros_ref_qty: 100`; the executor scales by `quantity / macros_ref_qty` (and `save: true` stores the *stated reference* as the catalog portion, not the eaten amount). Before this field existed (2026-07-25), that message shape logged per-100g macros unscaled against 63g.
- **Food rows dedupe by ingestion time** so a genuine second helping adds a row. A confirmation may repeat the earlier food. The executor checks the same date, meal, slug, amount, and unit within its duplicate window. An explicit override permits an intentional repeat.
- **Estimated foods are gated.** Catalog hits and user-given macros auto-commit; a food Claude must *estimate* goes `propose_food_estimate` → shown to user → `confirm_pending` on their "yes". `confirm_pending` refuses a call from the same message that proposed (guards against the model self-confirming in one turn, which on 2026-07-25 double-logged a beef mince it re-extracted from history); the pending survives the refusal so the user's real "yes" still commits. Pending state is in-process only (`_pending`), lost on restart.
- **Daily targets** live as constants in `targets.py` (`TARGETS`: kcal and fat are ceilings, protein/fibre floors, carbs tracked). Change them there + restart. Evening catalog suggestions kick in past `SUGGEST_AFTER_HOUR` (16:00 local); `suggest_food` answers the same question on demand at any hour.
- **Suggestions are split by what fits.** `targets.suggest` sizes one typical serving per food and checks it against the room left on kcal, fat and carbs, returning `fits`, `over` (each pick carrying the breach in grams) and `off_catalog_ok` — set only when fewer than `MIN_FITTING_PICKS` catalog foods fit, which is the reply's licence to name a food the owner hasn't saved. Off-catalog suggestions carry **no macro numbers**: eating one goes through the estimate gate like any unknown food.
- **Meal labels are whatever the owner calls them, never numbered.** `normalise_meal` lowercases and trims; that's all. Several messages labelled `breakfast` are one breakfast (numbering used to split them into `breakfast-1`/`breakfast-2` with no way to merge back), and a real second sitting is whatever he types. An omitted label stores `UNLABELLED_MEAL` and the bot asks — **it must not infer the meal from the clock**, because he often logs a whole day retrospectively.
- **Guessed *amounts* auto-commit; only guessed *macros* are gated.** That's deliberate, not an oversight — corrections are what make it safe, so a wrong guess is a one-message fix rather than a hand-edit. (The old ROADMAP question about gating amounts is resolved this way.)
- **Corrections append events.** Each event pins the targeted ingestion timestamp. Replay applies it only to that version. Amount edits rescale macros, while a date or reference-unit change needs a delete and a new entry. Food edits and deletes return the recomputed day totals.
- **`set_meal` is the one correction that writes a dedupe-key column** (`meal`), because food is logged before its label is known. It is safe only because the event pins the exact rows it resolved by `ingested_at`: a blanket "relabel whatever is unlabelled on this date" would, on rebuild, sweep up later rows that were never unlabelled at the time (corrections replay after every food row). `tests/test_rebuild_roundtrip.py::test_a_relabel_does_not_sweep_up_later_unlabelled_rows` is that proof — don't relax it to a `WHERE meal='unlabelled'` sweep.
- **Web-sourced macros are still estimates.** `web_search` (+ `web_fetch` for the real per-100g table) may look up an unknown packaged food, but the numbers go through `propose_food_estimate` → user's "yes" → `confirm_pending` like any other estimate, with the source named in the reply. No new write path exists for them. Search is biased to the UK via `user_location`, **not** restricted by `allowed_domains`: a hard allowlist fails shut on a food no UK grocer lists, and the retailers most likely to have it block automated fetches anyway (measured 2026-07-25: Waitrose/Tesco/Sainsbury's 403; M&S serves its table). Retailer priority lives in the prompt so it can degrade.
- **A confirmed estimate offers itself to the catalog.** `confirm_pending` returns `catalog_offer` for any committed food not already saved, and the standing offer rides `_offer` + the `log_system_context` fragment so a later "yes" (or better numbers off a label) resolves to `save_food`. The food is logged either way — the offer is a convenience, not a gate.
- **Exercise is append-per-entry** (`date, exercise, ingested_at`), like `food_log` and unlike `steps`. Logging bench twice in a day is two pieces of work, not a correction of the first. Every measure but the name is nullable so one tool covers weights, bodyweight and timed work; `duration_min` is **per set**, so review multiplies by `sets` for total time.
- **The message transcript is not a system of record** (`health/store/msg_log.py` → `data/messages/<date>.jsonl`). It records every inbound message, tool call (args + result or error) and outbound reply, including turns that write nothing — which is the gap it closes, since `raw` only exists on rows that got written and `ingest_log.jsonl` stores a hash, not the text. `rebuild_db` never reads it, nothing projects from it, and it is the one writer that swallows its own errors: an observability side-channel must never turn a logged meal into a user-visible failure.

## Repairing / editing logged data safely

**First resort: just tell the bot.** "delete that latte", "the chickpeas were 40g" — `recent_entries`/`query_log` + `edit_entry`/`delete_entry` handle it, an amount edit rescales the macros, and the change is itself an append-only event. `/today`, `/yesterday`, `/week` and `/last N` print the same rows as a numbered list you can point at ("delete 2"); the numbering is remembered per chat for `LISTING_EXPIRY` (30 min) and then deliberately forgotten, because acting on a stale number deletes the wrong row silently. Reach for the JSONL surgery below only for something the tools can't express (moving a row's date, fixing a catalog entry, repairing corruption).

Because SQLite is just a projection, fix data at the JSONL layer, then rebuild:

1. **Stop the bot cleanly:** `launchctl bootout gui/$(id -u)/local.health-bot` (KeepAlive only relaunches on a *crash*/unclean exit, so a clean bootout stays stopped). This is required — the bot holds an open SQLite connection, and `rebuild_db` deletes and recreates the DB file.
2. Edit the domain JSONL (`data/<domain>.jsonl`) — and the matching `ingest_log.jsonl` line for consistency. Back up first. If a `corrections.jsonl` event points at the row you're changing, fix or drop it too, or it will match nothing (or the wrong thing) on replay.
3. `.venv/bin/python -m health.rebuild_db`.
4. Restart with `./scripts/bot.sh install` (not `restart` — after a bootout the service is unloaded, and `restart`'s `kickstart -k` needs it loaded).

To *change a catalog entry*, prefer re-running the seed loader for just that item so provenance stays consistent, e.g.
`.venv/bin/python -c "from health.seed_catalog import load, INGREDIENTS; load([i for i in INGREDIENTS if i['name']=='<name>'])"`.

## Config & security

`.env` (loaded by `health/config.py`, gitignored) must define `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`, and `ALLOWED_CHAT_ID`. `MODEL` (default `claude-sonnet-4-6`, currently set to `claude-opus-5`) selects the routing model. `MAX_TOKENS` is 8192 because on the Claude 5 family thinking is on by default and shares that cap with the reply; the adapter splits any reply over Telegram's 4096-**char** limit, so the slack goes to reasoning. The bot **refuses to start without `ALLOWED_CHAT_ID`** — it's the security boundary: any message from another chat is dropped silently (no reply, so the bot's existence isn't confirmed to strangers). `data/` (JSONL, SQLite, logs) is gitignored — it's the owner's health data.
