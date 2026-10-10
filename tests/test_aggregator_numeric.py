"""Keep the numeric shortcut aligned with the original string parser."""

import math
import re

from carbotek.formatters.aggregator import agg_avg, agg_sum, parse_float_js


def _original_parse(value: object) -> float:
    match = re.match(r"^\s*([+-]?(?:Infinity|(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?))", str(value))
    return float(match.group(1)) if match else math.nan


def test_numeric_parse_matches_string_path() -> None:
    values = [0, -1, 2**53 + 1, 10**300, 0.0, -0.0, 1e-300, 1e300, math.inf, -math.inf, math.nan, True, "1.2tail"]
    for value in values:
        actual = parse_float_js(value)
        expected = _original_parse(value)
        assert actual == expected or (math.isnan(actual) and math.isnan(expected))
        if isinstance(value, float) and value == 0.0:
            assert math.copysign(1.0, actual) == math.copysign(1.0, expected)


def test_collection_sum_adds_like_the_js_loop() -> None:
    """JS adds ``[]`` items from the last one, an in-loop aggregate in document order, never compensated."""
    from carbotek.formatters.registry import MarkerContext

    values = [0.1] * 10
    assert agg_sum(values) == 0.9999999999999999  # sum() would give 1.0
    assert agg_sum([0.3, 0.2, 0.1]) == 0.1 + 0.2 + 0.3 == 0.6000000000000001
    in_loop = MarkerContext({}, {}, aggregate_forward=True)
    assert agg_sum([0.3, 0.2, 0.1], marker_context=in_loop) == 0.3 + 0.2 + 0.1 == 0.6
    assert agg_sum([1e16, 1.0, -1e16, None, 3, -0.0]) == 4.0
    assert agg_sum([1e16, 1.0, -1e16, None, 3, -0.0], marker_context=in_loop) == 3.0
    assert agg_avg([0.3, 0.2, 0.1]) == (0.1 + 0.2 + 0.3) / 3
    assert agg_sum([]) == 0
