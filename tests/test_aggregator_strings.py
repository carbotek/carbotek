"""Distinct string aggregation follows the JS builder's traversal order."""

from carbotek.formatters.aggregator import agg_str_distinct
from carbotek.formatters.registry import MarkerContext


def test_distinct_collection_keeps_last_occurrences_outside_loop() -> None:
    values = ["x", 0, "x", False]
    outside = MarkerContext({}, {}, aggregate_forward=False)
    inside = MarkerContext({}, {}, aggregate_forward=True)
    assert agg_str_distinct(values, "|", marker_context=outside) == "0|x|false"
    assert agg_str_distinct(values, "|", marker_context=inside) == "x|0|false"
