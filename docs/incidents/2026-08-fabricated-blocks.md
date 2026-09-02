# The model said "Logged" and printed totals for food that was never written

**Where it is pinned:** `tests/test_verbatim_blocks.py` (the two tests named at the end).

## What the user saw

Every food reply ends with a rendered progress block — the day's totals against the
targets, one line per macro:

```
Today so far
🔥 Calories: 794 / 2200 (1406 left)
🥩 Protein: 71g / 150g (79g left)
🍞 Carbs: 92g / 250g (158g left)
🥑 Fat: 13g / 70g (57g left)
🌾 Fibre: 19g / 28g (9g left)
```

The block is produced by `targets.render_progress` in Python and handed back from every
food executor as `progress_block`; the prompt's only instruction about it is to reproduce
it verbatim. A reply can show a plausible block and claim a write even when no tool ran or the executor refused the operation.

The user had no way to tell those replies from real ones. That is the whole problem: a
person running a calorie deficit off these numbers cannot audit them by eye, and a
plausible wrong total is worse than no total.

## Why the first defence could not see it

The first pass at protecting the block (`verbatim_blocks`) had been built for a different,
milder failure: the model could alter a block it was asked to reproduce character for character — numbers intact, icons swapped
(`🍞 Carbs` became `🌾 Carbs`, which displaced Fibre to `🌱`, `🌿` or `🌰`). Harmless-looking,
except that the block exists so days can be compared at a glance, and the drift compounded:
`history.save` stored the mutated reply and fed it back next turn as the example to follow,
so one swap held for a whole session.

So a feature declares which result keys hold finished text, and the harness collects those
strings from every tool result in the turn and **repairs** any reply line whose ASCII
skeleton matches a returned line but whose bytes differ. That fixed the icon drift on the
spot, and doing it *before* `history.save` broke the feedback loop.

It is structurally blind to fabrication. The repair pass compares the reply against blocks
a tool *returned this turn*. When no tool ran, the comparison set is empty and the reply
passes untouched — which is not an edge case but the worst case.

## The fix: two layers, both in the chassis

1. **Repair** (`verbatim_blocks`, per feature): a line that matches a returned block line
   on its ASCII skeleton is replaced with the returned bytes. Prose is never touched.
2. **Strip** (`block_signature`, per feature): a regex matching *one line* of that feature's
   rendered blocks — food derives it from `targets.PROGRESS_ROWS` and the block headings,
   so the labels stay editable in one place. Any reply line that matches the signature but
   whose skeleton is absent from this turn's tool results was not reproduced; it was typed.
   It is removed, the reply gets a note appended disowning the numbers and saying the food
   was **not** logged, and the harness logs an error.

A line whose *numbers* differ is stripped too, rather than left alone as the first version
did: it is unrepairable — nothing says what was meant — and shipping it would ship the
wrong figure.

Both live in `harness._enforce_blocks`, and neither knows what a progress block is. The
core reads key names and patterns off the feature declarations (`Feature.verbatim_blocks`,
`Feature.block_signature`) and stays feature-agnostic, which is one of the five invariants
the repo is built on.

## Why not fix the prompt

The prompt already said "character for character". The harness can enforce that instruction on each reply. The same reasoning put the duplicate-log guard
and the estimate gate in the executors rather than the prompt: history is text only, so the
model genuinely cannot see what it wrote last turn, and a guard has to live where the
information is.

## What it cost to get here

- The diagnosis came from the message transcript (`data/messages/<date>.jsonl`), which
  records every inbound message, tool call and reply *including turns that write
  nothing*. It captures fabricated replies even when no domain row was written: `raw` only exists
  on rows that were written, and there were no rows.

## The tests that pin it

- `test_a_block_no_tool_returned_is_stripped` — a synthetic example: "Logged"
  plus a full block, no tool called; the block must be gone, the prose kept, the note
  appended.
- `test_the_repaired_reply_is_what_history_and_the_transcript_keep` — a swapped icon is
  repaired *before* the reply reaches history, so the next turn is shown the canonical
  block, not the drift.
- `test_a_line_whose_numbers_differ_is_stripped_and_disowned` and
  `test_the_steer_sentence_is_never_mistaken_for_a_block_line` bound the signature from
  both sides: a wrong number goes, ordinary prose naming a macro stays.
