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
from health.core.prompts import STATIC_PROMPT, clock_line
from health.core.registry import all_executors, all_tools, system_context_fragments
from health.store import msg_log

log = logging.getLogger(__name__)

client = Anthropic()                 # reads ANTHROPIC_API_KEY from env

# On the Claude 5 family thinking is on by default and shares this cap with the reply
# text, so it needs headroom beyond the reply itself. The adapter splits anything over
# Telegram's 4096-CHAR limit, so the reply is bounded well below this and the slack goes
# to thinking — a weekly summary shouldn't be cut off mid-sentence by its own reasoning.
MAX_TOKENS = 8192
EFFORT = "medium"                    # thinking depth; routing + extraction needs no more

# Prompt caching. The rendered prompt is tools -> system -> messages, so ONE breakpoint on
# the static system block covers the tool schemas and static instructions.
# Everything per-message, including the clock and feature fragments,
# goes in a second, uncached block after it: content past a breakpoint cannot
# invalidate it. This is also why the clock left the head of the prompt — a value that
# changes every minute in a cached prefix makes the whole prefix uncacheable.
#
# Five-minute TTL, not an hour: a write costs 1.25x base at 5m and 2x at 1h, a read 0.1x.
# A short TTL covers bursts of repeated calls.
#
# NOT the top-level `cache_control=` parameter: that auto-places on the LAST cacheable
# block, which here is the volatile one. Every request would write a fresh entry and read
# none — and `cache_creation_input_tokens` would stay non-zero, so it would still LOOK like
# caching worked. `report_usage`'s hit% column is what catches that.
SYSTEM_CACHE_CONTROL = {"type": "ephemeral"}

# One message can legitimately need several rounds (propose -> show, or query -> edit ->
# totals), but never a dozen. The cap turns a model that loops on a failing tool into a
# bounded, honest failure instead of an unbounded spend.
MAX_LOOP_ITERATIONS = 12

# Server-side tools run on Anthropic's infrastructure: they are declared here and never
# dispatched locally, so there is no executor and no feature owns them. That keeps them
# chassis infrastructure (like MODEL), not a feature, and the invariant intact.
# web_search_20260209 does its own result filtering internally — declaring a separate
# code_execution tool alongside it would give the model a second execution environment.
#
# `user_location` biases results to UK products, which is the whole point here: a US
# label for the same branded food carries different numbers. It is a bias, not a filter —
# `allowed_domains` was considered and rejected, because a hard allowlist silently returns
# nothing for a food that isn't on a UK grocer's site, and the retailers most likely to
# have it are also the most likely to block automated access (measured 2026-07-25:
# Waitrose, Tesco and Sainsbury's all 403 a direct fetch; M&S serves its nutrition table).
# Search still reaches the blocked retailers through the index, so the ordering lives in
# the prompt, where it can degrade gracefully instead of failing shut.
#
# web_fetch can only retrieve a URL already in the conversation, so the pair works as
# search -> fetch the promising result -> read the real per-100g table, rather than
# settling for whatever a snippet happened to include.
SERVER_TOOLS = [
    {
        "type": "web_search_20260209",
        "name": "web_search",
        "max_uses": 4,
        "user_location": {"type": "approximate", "country": "GB"},
    },
    {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 3},
]

FAILSAFE_REPLY = (
    "Something went wrong on my side \N{EM DASH} nothing was logged. "
    "Please re-send that."
)

LOOP_LIMIT_REPLY = (
    "I hit my internal tool-call limit on that one \N{EM DASH} nothing further was "
    "executed. Check what landed with a quick question, then re-send the rest."
)

# How many search hits to keep per result block. A search returns ten-ish, and the point
# of recording them is "which pages did it read?", which the first few answer.
MAX_SERVER_RESULTS = 10


def _field(obj, name):
    """Read one field off a block that may be an SDK object or a plain dict — server-tool
    errors arrive as objects in production and as dicts from the test double."""
    return obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)


def _summarise_server_result(block):
    """Boil a server-tool result down to what a human would want a week later: which pages
    were found, which one was read, or why it failed.

    Summarising is load-bearing, not tidiness. A `web_search_result` carries
    `encrypted_content` beside its title, and a `web_fetch_result` carries the entire
    retrieved document — writing either verbatim would put multi-KB blobs into a file
    appended on every single turn, to answer a question nobody asks.
    """
    content = _field(block, "content")
    if content is None:
        return None
    if isinstance(content, list):                       # a successful search
        return [{"title": _field(r, "title"), "url": _field(r, "url")}
                for r in content[:MAX_SERVER_RESULTS]]
    # A successful fetch, or either tool's in-band error object.
    kept = {k: _field(content, k) for k in ("url", "retrieved_at", "error_code")}
    return {k: v for k, v in kept.items() if v is not None} or None


def _record_server_tools(msg: NormalizedMessage, content) -> None:
    """Transcribe the web lookups in one response.

    Server tools have no executor, so the loop's dispatcher never sees them and the
    transcript would show a turn that searched the web as a turn that did nothing — which
    is exactly how a `container_id` bug hid until it 400'd on 2026-07-26. Client
    `tool_use` blocks match neither branch and stay with the dispatcher that runs them.
    """
    for b in content:
        type_ = getattr(b, "type", "")
        if type_ == "server_tool_use":
            msg_log.server_tool(msg, b.name, id=getattr(b, "id", None),
                                input=getattr(b, "input", None))
        elif type_.endswith("_tool_result"):
            msg_log.server_tool(msg, type_, id=getattr(b, "tool_use_id", None),
                                result=_summarise_server_result(b))


def _record_usage(msg: NormalizedMessage, resp) -> None:
    """Transcribe one model call's token counts.

    `response.usage` is the only cost signal the bot has, and it used to be discarded —
    which made prompt caching unverifiable (a cache hit is only visible as
    `cache_read_input_tokens`) and "is this getting cheaper?" unanswerable. Reading it is
    pure getattr, so this can raise nothing; msg_log swallows the rest.
    """
    u = getattr(resp, "usage", None)
    if u is None:
        return

    def n(obj, name):                    # the SDK types these Optional[int]
        return getattr(obj, name, None) or 0

    creation, server = getattr(u, "cache_creation", None), getattr(u, "server_tool_use", None)
    msg_log.usage(
        msg, model=MODEL, effort=EFFORT,
        input_tokens=n(u, "input_tokens"),
        output_tokens=n(u, "output_tokens"),
        cache_write_tokens=n(u, "cache_creation_input_tokens"),
        cache_read_tokens=n(u, "cache_read_input_tokens"),
        # The per-TTL split, so the report can price a historical line correctly even after
        # SYSTEM_CACHE_CONTROL changes — it never has to guess which multiplier applied.
        cache_write_5m_tokens=n(creation, "ephemeral_5m_input_tokens"),
        cache_write_1h_tokens=n(creation, "ephemeral_1h_input_tokens"),
        # web_search bills per request, not per token — a separate SKU that is otherwise
        # invisible in a token count.
        web_search_requests=n(server, "web_search_requests"),
        web_fetch_requests=n(server, "web_fetch_requests"),
    )


def handle(msg: NormalizedMessage) -> str:
    token = current_message.set(msg)         # runtime provenance for every executor this turn
    msg_log.inbound(msg)                     # transcript: what was said (see store/msg_log.py)
    try:
        # Two blocks, and the split is the whole saving: the first is byte-identical on
        # every request and carries the cache breakpoint; the second holds everything that
        # varies per turn — the clock, and e.g. a pending estimate awaiting "yes". The
        # volatile block is always non-empty (the clock is always there), which matters
        # because an empty text block is a 400.
        volatile = "\n".join([clock_line(msg.timestamp), *system_context_fragments(msg)])
        system = [
            {"type": "text", "text": STATIC_PROMPT, "cache_control": SYSTEM_CACHE_CONTROL},
            {"type": "text", "text": volatile},
        ]
        # Prior turns let a follow-up ("make it 90g", "1. log 40g") resolve against what
        # was actually said. Bounded and in-process — see health/core/history.py.
        messages = history.load(msg) + [{"role": "user", "content": msg.text}]
        # Registry tools first, server tools last: a stable prefix keeps the request
        # cacheable as features come and go.
        tools, execs = all_tools() + SERVER_TOOLS, all_executors()

        # web_search_20260209 filters its results inside a server-side code-execution
        # container, so a turn that searched carries a container id that EVERY later
        # request in the same turn must echo back — otherwise the API rejects it with
        # "container_id is required when there are pending tool uses generated by code
        # execution with tools" (measured 2026-07-26: a search plus a propose_food_estimate
        # in one assistant turn 400'd on the follow-up, after the executor had already
        # committed). It is server-side plumbing, not an optimisation: both continuation
        # paths below (resuming a pause_turn, and returning tool results) need it.
        container = None

        for _ in range(MAX_LOOP_ITERATIONS):
            resp = client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=system,
                tools=tools,
                output_config={"effort": EFFORT},
                messages=messages,
                **({"container": container} if container else {}),
            )
            # Both records go in before any branch below: this is the only point reached on
            # every iteration regardless of how it ends (a paused turn's `continue`, the
            # failsafe return, the normal return, the loop-limit fall-through). Lower down,
            # a paused or refused turn would cost money and show nothing in the cost report
            # — exactly where an anomaly would hide.
            _record_usage(msg, resp)
            messages.append({"role": "assistant", "content": resp.content})
            _record_server_tools(msg, resp.content)
            stop = getattr(resp, "stop_reason", None)
            # Latest wins; never cleared mid-turn. `container` is a local, so the next
            # message starts a fresh one.
            container = getattr(getattr(resp, "container", None), "id", None) or container

            if stop == "pause_turn":
                # A server-side tool (web search) paused mid-answer. Re-sending the
                # paused turn resumes it where it left off; no user turn is injected.
                # Read as a final response it would ship a truncated reply.
                continue

            # Only client tools are dispatched here: `server_tool_use` and
            # `web_search_tool_result` blocks are a different type, so they fall through
            # to the model untouched (server-tool errors are in-band and model-facing).
            tool_uses = [b for b in resp.content if b.type == "tool_use"]
            if not tool_uses:
                if stop == "max_tokens":
                    log.warning("reply truncated at MAX_TOKENS=%s", MAX_TOKENS)
                reply = "".join(b.text for b in resp.content if b.type == "text")
                if stop == "refusal" or not reply:
                    # Safety classifiers can end a turn with no text (HTTP 200,
                    # stop_reason "refusal"). Never leave the user hanging on an
                    # empty reply, and keep the empty turn out of history.
                    log.warning("no reply text (stop_reason=%r); sending failsafe", stop)
                    msg_log.outbound(msg, FAILSAFE_REPLY)
                    return FAILSAFE_REPLY
                history.save(msg, reply)     # text only — never the tool blocks above
                msg_log.outbound(msg, reply)
                return reply

            results = []
            for tu in tool_uses:
                try:
                    out = execs[tu.name](**tu.input)
                    msg_log.tool_call(msg, tu.name, tu.input, result=out)
                    results.append({
                        "type": "tool_result",
                        "tool_use_id": tu.id,
                        "content": json.dumps(out),
                    })
                except Exception as e:       # surface failures back to the model, don't crash
                    msg_log.tool_call(msg, tu.name, tu.input, error=f"{type(e).__name__}: {e}")
                    results.append({
                        "type": "tool_result",
                        "tool_use_id": tu.id,
                        "content": f"ERROR: {e}",
                        "is_error": True,
                    })
            messages.append({"role": "user", "content": results})

        # Fell out of the loop still calling tools. Say so rather than inventing a
        # summary of work we can't account for; executors already committed whatever
        # they committed, and the JSONL log is the record of it.
        log.warning("tool loop hit MAX_LOOP_ITERATIONS=%s; giving up", MAX_LOOP_ITERATIONS)
        msg_log.outbound(msg, LOOP_LIMIT_REPLY)
        return LOOP_LIMIT_REPLY
    finally:
        current_message.reset(token)         # never leak context to the next message
