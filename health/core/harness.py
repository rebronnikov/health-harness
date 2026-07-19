"""The chassis tool-use loop. Feature-agnostic — it knows only the registry.

It is the only "smart" part: it asks Claude to route + extract, runs the matching
executor, feeds the result back, and loops until Claude returns plain text. Before
running any executor it binds the current message to a contextvar so executors can
read runtime-authored provenance (raw text, source, time) instead of trusting the
model to echo it.
"""
from __future__ import annotations

import json

from anthropic import Anthropic

from health.config import MODEL
from health.core import history
from health.core.contracts import NormalizedMessage, current_message
from health.core.prompts import build_system_prompt
from health.core.registry import all_executors, all_tools, system_context_fragments

client = Anthropic()                 # reads ANTHROPIC_API_KEY from env

MAX_TOKENS = 1024


def handle(msg: NormalizedMessage) -> str:
    token = current_message.set(msg)         # runtime provenance for every executor this turn
    try:
        system = build_system_prompt(now=msg.timestamp)
        for fragment in system_context_fragments(msg):   # e.g. a pending estimate awaiting "yes"
            system += "\n" + fragment
        # Prior turns let a follow-up ("make it 90g", "1. log 40g") resolve against what
        # was actually said. Bounded and in-process — see health/core/history.py.
        messages = history.load(msg) + [{"role": "user", "content": msg.text}]
        tools, execs = all_tools(), all_executors()

        while True:
            resp = client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=system,
                tools=tools,
                messages=messages,
            )
            messages.append({"role": "assistant", "content": resp.content})

            tool_uses = [b for b in resp.content if b.type == "tool_use"]
            if not tool_uses:
                reply = "".join(b.text for b in resp.content if b.type == "text")
                history.save(msg, reply)     # text only — never the tool blocks above
                return reply

            results = []
            for tu in tool_uses:
                try:
                    out = execs[tu.name](**tu.input)
                    results.append({
                        "type": "tool_result",
                        "tool_use_id": tu.id,
                        "content": json.dumps(out),
                    })
                except Exception as e:       # surface failures back to the model, don't crash
                    results.append({
                        "type": "tool_result",
                        "tool_use_id": tu.id,
                        "content": f"ERROR: {e}",
                        "is_error": True,
                    })
            messages.append({"role": "user", "content": results})
    finally:
        current_message.reset(token)         # never leak context to the next message
