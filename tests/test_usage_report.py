"""The cost report's arithmetic.

`cost()` and `summarise()` are pure functions over parsed usage lines, so the multipliers
that make caching worth doing are testable without a filesystem or an API. They are the
whole point of the module: if they drift, the report lies about the one number it exists
to give.
"""
import pytest

from health import report_usage


def _line(model="claude-opus-5", **counts):
    base = {"model": model, "input_tokens": 0, "output_tokens": 0,
            "cache_write_tokens": 0, "cache_read_tokens": 0,
            "cache_write_5m_tokens": 0, "cache_write_1h_tokens": 0,
            "web_search_requests": 0, "web_fetch_requests": 0}
    return {**base, **counts}


M = 1_000_000


def test_plain_input_and_output_price_at_the_model_rate():
    assert report_usage.cost(_line(input_tokens=M)) == pytest.approx(5.00)
    assert report_usage.cost(_line(output_tokens=M)) == pytest.approx(25.00)


def test_a_cache_read_costs_a_tenth_and_a_write_a_quarter_more():
    """This ratio is the entire reason caching pays: two calls sharing a prefix cost
    1.25 + 0.1 = 1.35x instead of 2x. If these constants drift the report stops being able
    to show whether the change was worth making."""
    assert report_usage.cost(
        _line(cache_read_tokens=M)) == pytest.approx(0.50)
    assert report_usage.cost(
        _line(cache_write_tokens=M, cache_write_5m_tokens=M)) == pytest.approx(6.25)
    assert report_usage.cost(
        _line(cache_write_tokens=M, cache_write_1h_tokens=M)) == pytest.approx(10.00)


def test_an_old_line_without_the_ttl_split_prices_at_the_five_minute_rate():
    """Lines written before the per-TTL fields existed carry only the total. They were
    written under the 5m TTL, so that is what they are priced at — rather than silently
    dropping out of the total."""
    row = _line(cache_write_tokens=M)
    del row["cache_write_5m_tokens"], row["cache_write_1h_tokens"]
    assert report_usage.cost(row) == pytest.approx(6.25)


def test_each_line_prices_at_its_own_recorded_model():
    """A window spanning a model switch has to report both halves correctly, so the price
    comes from the line, never from whatever MODEL happens to be configured now."""
    assert report_usage.cost(_line("claude-sonnet-5", input_tokens=M)) == pytest.approx(3.00)
    assert report_usage.cost(_line("claude-opus-5", input_tokens=M)) == pytest.approx(5.00)


def test_a_web_search_bills_per_request_not_per_token():
    assert report_usage.cost(_line(web_search_requests=3)) == pytest.approx(0.03)


def test_an_unknown_model_is_reported_not_silently_priced_at_zero():
    """A model quietly costing nothing would turn the report into a reassuring lie."""
    with pytest.raises(report_usage.UnknownModel):
        report_usage.cost(_line("claude-something-new", input_tokens=M))


def test_the_hit_rate_is_reads_over_the_prefix_presented():
    """Prompt caching fails silently — a stray per-request value in the cached prefix makes
    every call a fresh write with no error anywhere. This ratio is the only thing that
    reports it."""
    rows = [_line(cache_write_tokens=7700), *[_line(cache_read_tokens=7700)] * 9]
    assert report_usage.summarise(rows, inbound=5)["hit_rate"] == pytest.approx(0.9)


def test_a_run_with_no_caching_at_all_has_an_undefined_hit_rate():
    """Not 0.0 — nothing was presented, which is a different fact from nothing hitting."""
    assert report_usage.summarise([_line(input_tokens=100)], inbound=1)["hit_rate"] is None


def test_calls_per_message_counts_round_trips_not_turns():
    """The canary for a model or effort change: a drop toward 1.5 from the 2.0-2.3 baseline
    means the model stopped making a round-trip it used to make."""
    s = report_usage.summarise([_line(), _line(), _line()], inbound=2)
    assert s["calls"] == 3 and s["messages"] == 2
    assert s["calls_per_message"] == pytest.approx(1.5)


def test_a_day_with_no_messages_does_not_divide_by_zero():
    assert report_usage.summarise([], inbound=0)["calls_per_message"] == 0.0
