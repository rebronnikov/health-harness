"""The system prompt is pure and deliberately thin: it injects only today's date
and the current local time, which the food/meal routing rules depend on.

These assert against `STATIC_PROMPT` — the constant the harness actually sends — rather
than `build_system_prompt`, which the bot no longer calls. Testing the unsent assembly
would let the shipped prompt drift out from under its own tests.
"""
from datetime import datetime

from health.core.prompts import BODY, ROLE, STATIC_PROMPT, build_system_prompt, clock_line


def test_the_clock_carries_the_date_and_time():
    out = clock_line(datetime(2026, 7, 14, 9, 5))
    assert "2026-07-14" in out
    assert "09:05" in out


def test_the_clock_is_pure():
    now = datetime(2026, 7, 14, 9, 5)
    assert clock_line(now) == clock_line(now)


def test_the_cached_half_of_the_prompt_holds_no_clock():
    """Prompt caching is a prefix match on exact bytes, so a value that changes every
    minute anywhere in the cached block makes the whole ~7,700-token prefix uncacheable —
    silently, with no error and no failing request. This is what stops the clock drifting
    back into it."""
    assert "2026-07-14" not in STATIC_PROMPT
    assert "09:05" not in STATIC_PROMPT
    assert ":" not in STATIC_PROMPT.split("\n")[0]          # the role line stays clock-free


def test_the_whole_prompt_still_reassembles_from_the_parts():
    """build_system_prompt is no longer what the harness sends; this keeps it honest, so
    "what does the prompt say, end to end" stays answerable from one call."""
    now = datetime(2026, 7, 14, 9, 5)
    whole = build_system_prompt(now=now)
    assert whole == f"{ROLE} {clock_line(now)}\n{BODY}"
    assert whole.startswith(ROLE) and BODY in whole


def test_the_arithmetic_boundary_is_stated():
    """The rule the whole chassis rests on — Claude routes and extracts, executors
    compute. Losing it to a prompt edit would be silent and expensive."""
    assert "never do arithmetic" in STATIC_PROMPT


def test_the_progress_block_must_be_echoed_not_narrated():
    out = STATIC_PROMPT
    assert "progress_block" in out
    assert "EXACTLY as given" in out


def test_the_reply_still_steers_after_the_block():
    out = STATIC_PROMPT
    assert "THE STEER" in out
    assert "ONE short sentence of your own" in out
    assert "Never end a food reply on the bare block" in out


def test_the_steer_may_not_carry_the_numbers():
    """The split that lets prose and a rendered block coexist: the block owns every figure,
    the sentence owns the direction. Without this the steer just re-narrates the day and
    the drift is back one line lower."""
    out = STATIC_PROMPT
    assert "must NOT repeat a number from the block" in out
    assert "restate a figure from it in your own prose" in out      # the verbatim rule holds


def test_suggestions_are_echoed_not_formatted():
    """The list is rendered by targets.render_suggestions now. Left to the model it
    invented a heading per reply ("Ideas to close the gaps:") off identical data — the same
    drift the progress block was built to stop, one section further down."""
    out = STATIC_PROMPT
    assert "suggestions_block" in out
    assert "reproduce that string " in out
    assert "off_catalog_ok" in out                                  # still the model's to word
    assert "bullet points" not in out                               # ...but the bullets aren't


def test_questions_are_capped_at_one():
    out = STATIC_PROMPT
    assert "BIAS TO ACT" in out
    assert "at most ONE short question" in out


def test_the_lookup_ladder_is_ordered_and_ends_at_the_estimate_gate():
    """Where macros come from is a four-step ladder, not a source list: a zero-calorie
    drink is logged outright, a saved food wins over any search, a named producer sends
    the lookup to its own site, and everything looked up is still an estimate.
    """
    out = STATIC_PROMPT
    assert "explicit zeros for all five macros" in out          # (1) no gate on a zero
    assert "never re-check them against the web" in out         # (2) the catalog wins
    assert "that producer's OWN site first" in out              # (3) brand -> the brand
    assert "waitrose.com first" in out                          # (4) generic -> a grocer
    assert "never log it directly" in out                       # both -> propose/confirm


def test_a_stated_assumption_closes_the_question():
    out = STATIC_PROMPT
    assert "never ask about a detail you already settled with a stated assumption" in out


def test_the_estimate_must_be_staged_before_it_is_shown():
    """The 2026-08 confirmation loop: the model wrote estimates as prose and only called
    propose_food_estimate after the owner had already said yes, so his "yes" landed on an empty
    slot and cost him a second (sometimes third) confirmation. The rule used to be one
    sentence buried between BIAS TO ACT and the macros ladder; it is now its own section,
    and it must keep saying which comes first."""
    body = BODY.lower()
    assert "estimates:" in body
    staged = body.index("estimates:")
    section = body[staged:staged + 900]
    assert "propose_food_estimate first" in section
    assert "estimate and log it" in section          # the explicit-instruction case


def test_no_macro_number_may_be_invented():
    assert "never put a macro number in a reply that no tool returned this turn" in BODY.lower()


def test_the_meal_question_is_conditional_on_the_result():
    """Six foods now carry a fixed label, so "the user didn't name a meal" is no longer the
    same thing as "a question is owed". The trigger has to be what the tool actually wrote,
    or the bot goes back to asking about rows that are already settled."""
    body = BODY.lower()
    assert "needs_meal" in body
    meal = body[body.index("meal:"):body.index("meal:") + 1400]
    assert "only when `needs_meal` comes back true" in meal
    assert "one message can write two meals" in meal
    assert "never infer it from the clock" in meal          # the old rule still stands
