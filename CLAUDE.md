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
- **Tests** live in `tests/` (pytest; dev-only dep in `requirements-dev.txt`). Run `.venv/bin/python -m pytest`. They cover the core invariants — food scaling/`each`-vs-mass units, the estimate gating, runtime provenance + JSONL-before-projection write order, and the `rebuild_db` round-trip — plus the pure `targets.py` math. **The suite is hermetic and MUST stay that way:** `tests/conftest.py` redirects `DATA_DIR`/`DB_PATH` to a tempdir *before the first `health` import* so tests never touch the owner's real `data/`; `tests/test_isolation_guardrail.py` fails loudly if that ever breaks. There is no linter configured. For runtime behaviour a test can't reach (Telegram wiring), still verify by driving the flow and inspecting `data/*.jsonl` + the projection. The harness tool-use loop is covered via the `fake_client` fixture in `conftest.py` (a scripted Anthropic stand-in; `calls[i]["messages"]` is what makes context assembly assertable) — see `tests/test_harness_history.py`.
- Logs: `data/logs/bot.err.log` holds stderr *and* all app logging (`bot.out.log` is usually empty). Note: the httpx logger is silenced to WARNING because getUpdates URLs embed the bot token.

## Architecture — the chassis

Every message follows one feature-agnostic path (`fitness-harness-architecture.md` is the fuller spec):

```
Telegram update → adapter → NormalizedMessage → harness.handle → Claude tool-use loop → executor → writer.record → JSONL (+ SQLite projection) → reply
```

- **Adapter** (`health/adapters/telegram_adapter.py`) — the only channel-specific code. Translates updates to/from `NormalizedMessage`, enforces the allowlist, runs the blocking `harness.handle` in a worker thread, and always replies (on failure it sends a "nothing was logged" message — a handler exception never leaves the user hanging).
- **Harness** (`health/core/harness.py`) — the only "smart" part. Builds a thin system prompt, calls the Anthropic API with *all* registered tools, and loops running executors until Claude returns plain text. Claude does **routing + extraction only**.
- **Executors** own the *work* — deterministic, dumb. They build SI-unit fields and call `writer.record()`; they never touch raw text, time, source, or SQL directly.

**Core invariants (breaking one means the feature is wrong, not the rule):**
1. **Claude dispatches; it never computes.** All arithmetic lives in executors (see the "never do arithmetic" rule in `health/core/prompts.py` and the sizing math in `targets.py`).
2. **JSONL is the system of record; SQLite is a disposable projection.** `writer.record()` appends the canonical JSONL line *first*, then upserts the projection, then appends provenance to `ingest_log.jsonl`.
3. **`project()` is one idempotent upsert used by BOTH live ingest and `rebuild_db`** — that's what guarantees a rebuild reproduces live-ingest state. It's an `ON CONFLICT(dedupe_key) DO UPDATE`.
4. **Provenance is runtime-authored, never model-authored.** `raw`/`source`/`ingested_at`/`schema_version` are stamped by `writer.record()` from the `current_message` contextvar (`health/core/contracts.py`), set for the duration of each turn.
5. **The core never references a specific feature** — it only knows the registry.

The system prompt is deliberately thin (just today's date/time + routing rules). Conversation history is **bounded and ephemeral** (`health/core/history.py`): the last 10 turns per chat, dropped after 30 minutes idle, in-process only (lost on restart), and **plain text only** — never the `tool_use`/`tool_result` blocks, which would risk an orphaned `tool_use` and re-feed stale tool results. It feeds the model's context and nothing else; provenance is still stamped from the live message, never from history. A feature that needs *structured* cross-turn state still surfaces it via a `system_context` fragment (e.g. food's pending-estimate awaiting "yes") — history is the conversational fallback, not a substitute.

**The bot must never ask a clarifying question before logging** (`prompts.py`, and the SAVED FOODS fragment in `food.py`). It estimates, proposes, and says what it assumed; the user corrects. This is a hard rule learned the hard way: on 2026-07-15 an "ASK a short clarifying question" instruction in the catalog fragment sent the owner round eight turns without logging a salad, because the questions landed on turns that couldn't remember asking them. Don't reintroduce it.

## Adding or changing a feature

A feature is one module under `health/features/` that declares a `Feature(...)` — `tools` (Anthropic schemas), `executors` (name→fn), `schema` (DDL, uses `IF NOT EXISTS`), `domain` (its `data/<domain>.jsonl` + table), `dedupe_key`, `project` (the upsert), and an optional `system_context`. **`health/features/steps.py` is the reference template**; copy its shape. The **only** shared-code edit is adding the feature to `FEATURES` in `health/core/registry.py`. Everything else lives in the module.

Active features: `steps`, `weight`, `food` (two features — catalog + log). `exercise`/`measurements`/`corrections`/`review` are stubbed and commented out in the registry (see `ROADMAP.md`). `targets.py` is a **compute lib, not a routed feature** (no tools) — imported by `food`.

## Data model specifics (the non-obvious parts)

- **Food is two features in one module** (`health/features/food.py`): `food_catalog` (reusable foods, macros stored per a reference portion `ref_qty`/`ref_unit` ∈ `g|ml|each`, dedupe on `slug`) and `food_log` (one row per eating event). Logging scales catalog macros by `amount / ref_qty`.
- **`each`-unit catalog foods scale by count, not weight.** A food stored as `ref_unit="each"` (e.g. a banana, an egg) counts whole units — a stated gram weight is ignored. Foods you weigh per portion (e.g. salmon) should be stored per `100 g` so `"2 fillets 90g each"` scales to 180 g. Gram-weights hidden in `notes` are human-readable only; the executor doesn't parse them for scaling (except `targets._typical_serving`, which reads a "typical serving Ng" note for suggestion sizing).
- **User-given macros can be stated per a different reference amount.** A message like "per 100g: 184 kcal … I ate 63g" is logged via `log_food` with the macros verbatim plus `macros_ref_qty: 100`; the executor scales by `quantity / macros_ref_qty` (and `save: true` stores the *stated reference* as the catalog portion, not the eaten amount). Before this field existed (2026-07-25), that message shape logged per-100g macros unscaled against 63g.
- **Food rows dedupe by ingestion time** so a genuine second helping adds a row. A confirmation may repeat the earlier food. The executor checks the same date, meal, slug, amount, and unit within its duplicate window. An explicit override permits an intentional repeat.
- **Estimated foods are gated.** Catalog hits and user-given macros auto-commit; a food Claude must *estimate* goes `propose_food_estimate` → shown to user → `confirm_pending` on their "yes". `confirm_pending` refuses a call from the same message that proposed (guards against the model self-confirming in one turn, which on 2026-07-25 double-logged a beef mince it re-extracted from history); the pending survives the refusal so the user's real "yes" still commits. Pending state is in-process only (`_pending`), lost on restart.
- **Daily targets** live as constants in `targets.py` (`TARGETS`: kcal is a ceiling, protein/fibre floors, carb/fat tracked). Change them there + restart. Evening catalog suggestions kick in past `SUGGEST_AFTER_HOUR` (16:00 local).

## Repairing / editing logged data safely

Because SQLite is just a projection, fix data at the JSONL layer, then rebuild:

1. **Stop the bot cleanly:** `launchctl bootout gui/$(id -u)/local.health-bot` (KeepAlive only relaunches on a *crash*/unclean exit, so a clean bootout stays stopped). This is required — the bot holds an open SQLite connection, and `rebuild_db` deletes and recreates the DB file.
2. Edit the domain JSONL (`data/<domain>.jsonl`) — and the matching `ingest_log.jsonl` line for consistency. Back up first.
3. `.venv/bin/python -m health.rebuild_db`.
4. Restart with `./scripts/bot.sh install` (not `restart` — after a bootout the service is unloaded, and `restart`'s `kickstart -k` needs it loaded).

To *change a catalog entry*, prefer re-running the seed loader for just that item so provenance stays consistent, e.g.
`.venv/bin/python -c "from health.seed_catalog import load, INGREDIENTS; load([i for i in INGREDIENTS if i['name']=='<name>'])"`.

## Config & security

`.env` (loaded by `health/config.py`, gitignored) must define `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`, and `ALLOWED_CHAT_ID`. `MODEL` (default `claude-sonnet-4-6`) selects the routing model. The bot **refuses to start without `ALLOWED_CHAT_ID`** — it's the security boundary: any message from another chat is dropped silently (no reply, so the bot's existence isn't confirmed to strangers). `data/` (JSONL, SQLite, logs) is gitignored — it's the owner's health data.
