"""JavaScript number conversion in string formatter arguments."""

from carbotek.formatters.string import pad_left, pad_right, substr
from carbotek.markers import parse


def test_numeric_argument_spelling() -> None:
    assert pad_left("ABCDEF", "5.5", "0") == "ABCDEF"
    assert pad_right("A", "5.5", "0") == "A0000"
    assert substr("ABCDEF", "0x1") == "BCDEF"
    assert substr("ABCDEF", "01") == "BCDEF"


def test_unclosed_single_quote_is_tolerated_like_js() -> None:
    assert parse("{d.s:append('abc)}").formatters[0].arguments == ("'abc'",)
