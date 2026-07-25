"""The chassis tool-use loop. Feature-agnostic — it knows only the registry.

It is the only "smart" part: it asks Claude to route + extract, runs the matching
executor, feeds the result back, and loops until Claude returns plain text. Before
running any executor it binds the current message to a contextvar so executors can
read runtime-authored provenance (raw text, source, time) instead of trusting the
model to echo it.
"""
from __future__ import annotations

import json
import logging

from anthropic import Anthropic

from health.config import MODEL
from health.core import history
from health.core.contracts import NormalizedMessage, current_message
from health.core.prompts import build_system_prompt
from health.core.registry import all_executors, all_tools, system_context_fragments

log = logging.getLogger(__name__)

client = Anthropic()                 # reads ANTHROPIC_API_KEY from env

# On the Claude 5 family thinking is on by default and shares this cap with the
# reply text, so it needs headroom beyond the reply itself (Telegram caps a
# message at 4096 chars anyway).
MAX_TOKENS = 4096
EFFORT = "medium"                    # thinking depth; routing + extraction needs no more

FAILSAFE_REPLY = (
    "Something went wrong on my side \N{EM DASH} nothing was logged. "
    "Please re-send that."
)


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
                output_config={"effort": EFFORT},
                messages=messages,
            )
            messages.append({"role": "assistant", "content": resp.content})

            tool_uses = [b for b in resp.content if b.type == "tool_use"]
            if not tool_uses:
                stop = getattr(resp, "stop_reason", None)
                if stop == "max_tokens":
                    log.warning("reply truncated at MAX_TOKENS=%s", MAX_TOKENS)
                reply = "".join(b.text for b in resp.content if b.type == "text")
                if stop == "refusal" or not reply:
                    # Safety classifiers can end a turn with no text (HTTP 200,
                    # stop_reason "refusal"). Never leave the user hanging on an
                    # empty reply, and keep the empty turn out of history.
                    log.warning("no reply text (stop_reason=%r); sending failsafe", stop)
                    return FAILSAFE_REPLY
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
