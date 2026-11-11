# health-harness

**A Telegram health tracker where the LLM routes and extracts, and never computes.**

[![tests](https://github.com/rebronnikov/health-harness/actions/workflows/tests.yml/badge.svg)](https://github.com/rebronnikov/health-harness/actions/workflows/tests.yml)
![python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue)
![license](https://img.shields.io/badge/license-MIT-green)

You text it what you ate, walked or lifted. Claude picks a tool and fills in the arguments. Deterministic Python does every calculation and every write. An append-only JSONL log is the system of record; SQLite is a projection you can delete.

`CLAUDE.md` at the root is the agent-facing working notes — invariants, incidents and the reasons behind them, kept whole because that is what the coding agent reads; the human version is [`docs/`](docs/). The code was written with Claude Code. The human owned the invariants, the incident analysis and the test design.

## What it looks like

All numbers in the examples, demo output, and sample targets are illustrative.

```
> lunch: 159g chicken breast, 160g buckwheat, 80g chickpeas
```

The model's entire contribution is one tool call, arguments read straight off the message:

```
log_food(date="2026-09-02", meal="lunch", items=[
    {"name": "chicken breast", "quantity": 159, "unit": "g"},
    {"name": "buckwheat",      "quantity": 160, "unit": "g"},
    {"name": "chickpeas",      "quantity": 80,  "unit": "g"}])
```

The reply carries the executor's rendered block and one steering sentence of the model's own:

```
Logged lunch: 159g chicken breast, 160g buckwheat, 80g chickpeas.

Today so far
🔥 Calories: 794 / 2200 (1406 left)
🥩 Protein: 71g / 150g (79g left)
🍞 Carbs: 92g / 250g (158g left)
🥑 Fat: 13g / 70g (57g left)
🌾 Fibre: 19g / 28g (9g left)

Protein and fibre are the ones to chase, and there is room for both.
```

Every number in that block was computed in Python (`seed_demo` below prints exactly this one). Had the model retyped a single row, the chassis would have replaced it; had it printed a block no tool produced, the chassis would have stripped it and said so.

## Try it in 60 seconds (no API key)

```bash
git clone https://github.com/rebronnikov/health-harness && cd health-harness
python3 -m venv .venv && .venv/bin/pip install -e .
.venv/bin/python -m health.seed_demo --days 7          # a week of demo data, written through the real executors
printf '/today\n' | .venv/bin/python -m health.cli     # the numbered listing plus the block above
.venv/bin/python -m health.rebuild_db                  # delete the SQLite projection and rebuild it from JSONL
printf '/today\n' | .venv/bin/python -m health.cli     # byte-identical — CI diffs these two
```

With `ANTHROPIC_API_KEY` in `.env` (copy `.env.example`), `.venv/bin/python -m health.cli` takes free text: `40g oats and a banana`, `delete 2`, `what should I eat?`, `weight 72.6`. Slash commands never call the model.

## How it works

```
Telegram update → adapter → NormalizedMessage → harness.handle → Claude tool-use loop
    → executor → writer.record → JSONL (+ SQLite projection) → reply
```

Five invariants, and breaking one means the feature is wrong, not the rule:

1. **Claude dispatches; it never computes.** All arithmetic lives in executors.
2. **JSONL is the system of record; SQLite is a disposable projection.** `writer.record()` appends the canonical line first, then upserts the projection, then logs provenance.
3. **One idempotent `project()` per feature serves both live ingest and `rebuild_db`**, which is what makes a rebuild reproduce live state.
4. **Provenance is runtime-authored, never model-authored.** `raw`, `source` and `ingested_at` come from the in-flight message via a contextvar; an executor cannot fabricate them.
5. **The core never references a specific feature.** It knows the registry and nothing else.

A feature is one module declaring its tool schemas, executors, DDL, JSONL domain, dedupe key and projector ([`health/features/steps.py`](health/features/steps.py) is the template). Adding one touches exactly one line of shared code: the registry list. Seven are wired: steps, weight, food catalog, food log, measurements, exercise, a read-only review, and corrections — append-only events that edit or delete rows by pinning their `ingested_at`, so a superseded correction replays as a no-op.

## Three decisions worth arguing about

**1. The model is forbidden from doing arithmetic.** It may *estimate* a food's macros — gated behind the user's "yes", and it must name its source — but it never sums, scales or subtracts. A stated "per 100 g" label eaten as 63 g is scaled by the executor; the day's totals and the room left are rendered by the executor; a correction of "that was 40 g, not 50" rescales the row in the executor. The cost is more tool calls. The return is that no number in a reply can be quietly wrong, and every one of them is auditable from the log. Daily targets are env-with-defaults in `config.py` rather than a `targets.toml`: five integers that never change at runtime do not justify a parser, a missing-file branch and a second config surface.

**2. Rendered text is repaired by the chassis, not requested in the prompt.** The prompt asks for the progress block "character for character", yet a reply could preserve the numbers while swapping icons.

```
🍞 Carbs: 92g / 250g (158g left)      ← what the tool returned
🌾 Carbs: 92g / 250g (158g left)      ← illustrative model output
```

Conversation history then fed the drift back into later turns. So a feature declares which result keys are finished text, and the harness restores any line the reply altered *before* the reply reaches history. A reply could also print a plausible block without any tool result. A repair pass has nothing to compare against when no tool ran, so a feature also declares a one-line *signature* of its blocks, and a matching line absent from this turn's tool results is stripped and the reply disowns it. The write-up is [`docs/incidents/2026-08-fabricated-blocks.md`](docs/incidents/2026-08-fabricated-blocks.md).

**3. JSONL is the system of record; SQLite is disposable.** Live ingest and `rebuild_db` call the same `project()`, so "rebuild reproduces live" is a property of the code, not a discipline, and it is tested three ways: a snapshot-equality round-trip over a representative mix, the same on demo data, and the CI job that diffs `/today` across a rebuild. Corrections are events, never mutations: the domain file is never rewritten, and a wrong correction is itself a one-message fix.

## Failure modes and fixes

| Symptom | Fix | Regression test |
|---|---|---|
| Repeated clarification delays logging | Limit follow-up questions and retain recent conversation | `test_prompts.py` |
| An estimate is confirmed before the user sees it | Check the previous reply for the staged values | `test_estimate_confirmation.py` |
| A server-tool continuation fails after a write | Carry the server container ID across continuation paths | `test_harness_loop.py` |
| A reply prints progress for a write that never happened | Strip rendered lines the tools did not return | `test_verbatim_blocks.py` |
| A confirmation names a different item from the pending estimate | Require the intended food and match its name | `test_estimate_confirmation.py` |
| A confirmation re-sends an earlier item | Check for a recent duplicate before writing | `test_food_log.py` |
| A serving's calories and macros use different reference amounts | Compare calories with the macros and surface a warning | `test_food_log.py` |

## Removing saved foods

Send a request such as “remove Orchard Oat Bites from my catalog.” If several names match, the bot asks you to choose. The removal survives a database rebuild, and past meals stay in your food log.

## Running it for real

```bash
cp .env.example .env         # ANTHROPIC_API_KEY, TELEGRAM_BOT_TOKEN, ALLOWED_CHAT_ID
./scripts/bot.sh install     # macOS launchd; renders the plist for this checkout
```

[`docs/deploy.md`](docs/deploy.md) has the rest: the chat-id allowlist that is the whole security model, logs, the transcript, cost reporting, and why the checkout must not live under `~/Documents`.

## Tests

The hermetic tests require neither network access nor an API key. `tests/conftest.py` redirects every data path to a tempdir *before the first import* and a guardrail test fails loudly if that ever breaks; the tool-use loop is driven by a scripted `FakeAnthropic` whose recorded requests make prompt assembly and cache placement assertable. CI runs the suite on Python 3.11, 3.12 and 3.13, lints with ruff, and runs the keyless demo round-trip above.

The harness records per-call usage. Run `health.report_usage` to inspect costs and prompt-cache performance for your deployment.

## What's not here

Single user by construction. Auth is a chat-id allowlist and nothing more. No migrations beyond rebuild-from-JSONL. macOS-only deployment files. Pending estimates, conversation history and the numbered listing behind "delete 2" are in-process and lost on restart. No type checker — ruff lints, nothing types.

## License

MIT.
