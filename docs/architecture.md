# Fitness Harness — Architecture & Scaffolding

> **The original chassis spec, written before any code existed.** The shape is what
> shipped — one feature-agnostic path, the feature contract, the registry as the only
> shared edit — and it has held through seven features. The specifics have moved on:
> `operations/` became `health/features/`, `db/` became `health/store/`, the sketch's
> `while True` became a bounded loop, and two of the rules below were superseded by
> stronger ones (noted inline). Read `CLAUDE.md` for what is true today; read this for
> why it is shaped the way it is.

This is the **chassis** spec: the overall logic, the layering, and the skeleton to
build first. Features are deliberately *not* detailed here — each one gets its own
instruction + script later. What this doc fixes is the **contract every feature
plugs into**, so those later features are self-contained and never touch the core.

> A companion per-feature plan (food resolution, weekly analyses, schema) existed
> alongside this and was consumed as the features were built; it is not published.
> This doc is sufficient on its own to stand up the scaffolding.

---

## 1. Overall logic — the request lifecycle

Every input, regardless of feature, follows the same path. Nothing in this path
knows what a "step" or a "meal" is.

```
1. Adapter receives raw input            (e.g. a Telegram update)
2. Adapter → NormalizedMessage           (channel-specific → internal format)
3. Harness.handle(msg):
     a. build system prompt (today's date + settings summary)
     b. call Anthropic API with ALL registered tools
     c. loop:  while Claude returns tool_use →
                  run the matching executor → feed result back
     d. when Claude returns plain text → that's the reply
4. Adapter sends the reply back          (via the message's reply_handle)
```

Three responsibilities, cleanly separated:
- **Adapter** owns I/O and format translation.
- **Claude (runtime API)** owns *routing + extraction* — read intent, pick a tool,
  fill its arguments. It is the only "smart" part.
- **Executors** own the *work* — deterministic writes/reads. Dumb on purpose.

---

## 2. Invariants (the rules every feature must obey)

1. **Claude dispatches; it never computes.** All arithmetic lives in executors.
2. **Executors are deterministic & idempotent.** Same input → same write. No
   model-authored SQL.
3. **Raw text is preserved**, so anything can be corrected later.
4. **Computed values are snapshotted** into the row at write time.
5. **The core never references a specific feature.** It only knows the registry.

> *As shipped*, 3 and 4 were replaced by two stronger invariants: **the JSONL log is the
> system of record and SQLite is a disposable projection** rebuilt from it by one
> idempotent `project()` shared with live ingest, and **provenance (`raw`, `source`,
> `ingested_at`) is stamped by the runtime, never authored by the model**. Corrections
> became append-only events rather than edits.

If a future feature would break one of these, the design is wrong — fix the
feature, not the rule.

---

## 3. Layered architecture

```
┌──────────────────────────────────────────────┐
│ ADAPTERS        telegram (now) · cli/file (later)
│ → emit NormalizedMessage, know how to reply    │
└───────────────────┬────────────────────────────┘
                    │  NormalizedMessage
                    ▼
┌──────────────────────────────────────────────┐
│ CORE / CHASSIS   (feature-agnostic, stable)    │
│   harness.handle()      the tool-use loop      │
│   registry              collects all features  │
│   prompts               builds system prompt   │
│   contracts             the interfaces         │
└───────────────────┬────────────────────────────┘
        ┌───────────┴────────────┐
        ▼                        ▼
┌──────────────┐        ┌──────────────────────┐
│ Anthropic API│        │ FEATURES (plugins)    │
│ (router)     │◄──────►│ steps · exercise ·    │
│              │ tools/ │ measurements · food · │
│              │ results│ targets · review …    │
└──────────────┘        └──────────┬────────────┘
                                   ▼
                           ┌───────────────┐
                           │ SQLite (file) │
                           └───────────────┘
```

The **two decoupling boundaries** that make this extensible:
- **Adapter boundary** — swap/add input channels without touching core.
- **Feature boundary** — add capabilities without touching core.

---

## 4. The feature contract (the centrepiece)

A **feature** is one module that declares everything it needs. Nothing else in the
system has to be edited to support it.

```python
# core/contracts.py  — imported by both the core and every feature (no cycles)
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Iterator, Protocol

@dataclass
class NormalizedMessage:
    source: str                 # "telegram" | "cli" | ...
    reply_handle: Any           # opaque token the adapter uses to reply
    timestamp: datetime
    text: str | None = None
    attachment: bytes | None = None        # future: HR/CSV exports, images
    attachment_type: str | None = None

class InputAdapter(Protocol):
    def listen(self) -> Iterator[NormalizedMessage]: ...
    def reply(self, msg: NormalizedMessage, text: str) -> None: ...

@dataclass
class Feature:
    name: str
    tools: list[dict]                       # Anthropic tool schemas
    executors: dict[str, Callable]          # tool_name -> function
    schema: str | None = None               # DDL fragment, applied idempotently
```

Each feature module ends by exposing a `FEATURE = Feature(...)`. That's the whole
contract: **tools + executors + (optional) schema.**

### The registry assembles features into what the harness uses
```python
# core/registry.py
from core.contracts import Feature
from operations import steps           # add modules here as features are built
# from operations import exercise, measurements, targets, food, review

FEATURES: list[Feature] = [
    steps.FEATURE,
    # exercise.FEATURE,
    # measurements.FEATURE,
    # targets.FEATURE,
    # food.FEATURE,        # phase 2
    # review.FEATURE,      # phase 2
]

def all_tools() -> list[dict]:
    return [t for f in FEATURES for t in f.tools]

def all_executors() -> dict[str, callable]:
    return {name: fn for f in FEATURES for name, fn in f.executors.items()}

def init_schema(conn) -> None:
    for f in FEATURES:
        if f.schema:
            conn.executescript(f.schema)   # all DDL uses IF NOT EXISTS
```

**This list is the only place adding a feature touches shared code.** Everything
else lives inside the feature's own module.

---

## 5. Scaffolding — what to build first

The skeleton below is runnable end-to-end with exactly **one** feature wired
(`steps`). It proves the whole loop and serves as the template every later feature
copies. Build this, send "I did 10k steps today," confirm it logs — then stop.

```
fitness-harness/
├── .env                  # ANTHROPIC_API_KEY, TELEGRAM_BOT_TOKEN, DB_PATH
├── main.py               # wire adapter -> harness; run the listen loop
├── core/
│   ├── contracts.py      # §4 — NormalizedMessage, InputAdapter, Feature
│   ├── registry.py       # §4 — FEATURES + aggregators
│   ├── harness.py        # §5.1 — the tool-use loop
│   └── prompts.py        # §5.2 — system prompt builder
├── adapters/
│   └── telegram_adapter.py   # §5.3 — polling adapter
├── operations/
│   ├── steps.py          # §5.4 — the ONE reference feature (fully built)
│   ├── exercise.py       # stub — built later
│   ├── measurements.py   # stub — built later
│   ├── targets.py        # stub — built later
│   ├── food.py           # stub — phase 2
│   └── review.py         # stub — phase 2
└── db/
    └── connection.py     # get_conn(); runs registry.init_schema() once at startup
```

### 5.1 Harness (chassis — references the registry only)
```python
# core/harness.py
import json
from anthropic import Anthropic
from core.registry import all_tools, all_executors
from core.prompts import build_system_prompt
from core.contracts import NormalizedMessage

client = Anthropic()                 # reads ANTHROPIC_API_KEY from env
MODEL = "claude-sonnet-..."          # builder: confirm current model string (config.MODEL today)

def handle(msg: NormalizedMessage) -> str:
    system   = build_system_prompt(today=msg.timestamp.date())
    messages = [{"role": "user", "content": msg.text}]
    tools, execs = all_tools(), all_executors()

    while True:
        resp = client.messages.create(
            model=MODEL, max_tokens=1024,
            system=system, tools=tools, messages=messages,
        )
        messages.append({"role": "assistant", "content": resp.content})

        tool_uses = [b for b in resp.content if b.type == "tool_use"]
        if not tool_uses:
            return "".join(b.text for b in resp.content if b.type == "text")

        results = []
        for tu in tool_uses:
            try:
                out = execs[tu.name](**tu.input)
                results.append({"type": "tool_result", "tool_use_id": tu.id,
                                "content": json.dumps(out)})
            except Exception as e:
                results.append({"type": "tool_result", "tool_use_id": tu.id,
                                "content": f"ERROR: {e}", "is_error": True})
        messages.append({"role": "user", "content": results})
```

### 5.2 System prompt (thin context, by design)
```python
# core/prompts.py
def build_system_prompt(today) -> str:
    # Keep context cheap: inject today's date + a one-line settings summary.
    # Do NOT stuff history here — features expose query tools for that.
    return (
        f"You are a personal fitness-logging router. Today is {today.isoformat()}.\n"
        "Read the user's message, choose the right tool(s), and extract their "
        "arguments. If a date isn't given, assume today. You never do arithmetic "
        "yourself — call the relevant tool. After tools run, reply with a short, "
        "natural confirmation."
    )
```

### 5.3 Telegram adapter (the only channel-specific code)
```python
# adapters/telegram_adapter.py  — sketch
from datetime import datetime
from core.contracts import NormalizedMessage
# uses python-telegram-bot in polling mode

class TelegramAdapter:
    def __init__(self, token): ...
    def listen(self):
        for update in self._poll():                  # long-polling loop
            yield NormalizedMessage(
                source="telegram",
                reply_handle=update.chat_id,
                timestamp=datetime.now(),
                text=update.text,
            )
    def reply(self, msg, text):
        self._send(chat_id=msg.reply_handle, text=text)
```

### 5.4 Reference feature — the template to copy

> *As shipped*, an executor never runs SQL itself: it builds the SI-unit fields and calls
> `writer.record()`, which appends the JSONL line first, then upserts the projection via
> the feature's declared `project()`, then logs provenance. `health/features/steps.py`
> is the current template; the sketch below is the pre-JSONL shape.

```python
# operations/steps.py
from datetime import datetime
from core.contracts import Feature
from db.connection import get_conn

SCHEMA = """
CREATE TABLE IF NOT EXISTS steps (
  date      TEXT PRIMARY KEY,
  count     INTEGER NOT NULL,
  logged_at TEXT NOT NULL
);
"""

TOOLS = [{
    "name": "log_steps",
    "description": "Log the user's step count for a day. Upserts: one row per date.",
    "input_schema": {
        "type": "object",
        "properties": {
            "date":  {"type": "string", "description": "ISO date YYYY-MM-DD; today if unspecified."},
            "count": {"type": "integer", "description": "Step count."},
        },
        "required": ["date", "count"],
    },
}]

def log_steps(date: str, count: int) -> dict:
    conn = get_conn()
    conn.execute(
        "INSERT INTO steps(date, count, logged_at) VALUES (?,?,?) "
        "ON CONFLICT(date) DO UPDATE SET count=excluded.count, "
        "logged_at=excluded.logged_at",
        (date, count, datetime.now().isoformat()),
    )
    conn.commit()
    return {"ok": True, "date": date, "count": count}

EXECUTORS = {"log_steps": log_steps}

FEATURE = Feature(name="steps", tools=TOOLS, executors=EXECUTORS, schema=SCHEMA)
```

### 5.5 Entrypoint
```python
# main.py
from dotenv import load_dotenv; load_dotenv()
import os
from adapters.telegram_adapter import TelegramAdapter
from core.harness import handle
from db.connection import init_db

def main():
    init_db()                                   # runs registry.init_schema()
    adapter = TelegramAdapter(os.environ["TELEGRAM_BOT_TOKEN"])
    for msg in adapter.listen():                # blocks, polling
        reply = handle(msg)
        adapter.reply(msg, reply)

if __name__ == "__main__":
    main()
```

---

## 6. The per-feature workflow (the deferred work)

Adding any feature later — exercise, measurements, targets, food, review — is the
same four-step recipe. This is what gets its own instruction + script each time.

1. **Spec** the feature: what it captures, its tool(s), its table(s), edge cases.
   (Food and review were the two that needed a written spec first.)
2. **Implement** `operations/<feature>.py`: `SCHEMA`, `TOOLS`, executor fn(s),
   `EXECUTORS`, and the `FEATURE = Feature(...)` line. Copy `steps.py` as the shape.
3. **Register**: uncomment/add `<feature>.FEATURE` in `core/registry.py`.
4. **Verify** by talking to the bot.

No edits to `harness.py`, `prompts.py`, or the adapter. Ever. That property is the
point of the whole design.

---

## 7. Feature catalogue (names only — detail deferred)

| Feature        | Tools (provisional)                                   | Phase |
|----------------|-------------------------------------------------------|-------|
| steps          | `log_steps`                                           | scaffold (built) |
| exercise       | `log_exercise`                                        | 1 |
| measurements   | `log_measurement`                                     | 1 |
| targets        | `remaining_calories` (+ deterministic TDEE math)      | 1 |
| corrections    | `query_log`, `edit_entry`, `delete_entry`             | 1 |
| food           | `add_food`, `lookup_food`, `log_food`, `log_food_estimate` | 2 |
| review         | `get_summary` (+ weekly analysis prompt)              | 2 |
| heart-rate     | file-import via a future `file_adapter`               | 3 |

Each row is a future work item. The chassis treats them identically.
