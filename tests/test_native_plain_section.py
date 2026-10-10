"""The native section contract used before the Python renderer selects it."""

import random
from decimal import Decimal

import pytest

pytest.importorskip("carbotek._native")

from carbotek import _native, _native_adapter
from carbotek._native import PlainSection, digit_currency_cents, join_utf8
from carbotek._native_adapter import OP_CENTS, OP_FIELD, OP_IF_EQUAL, OP_ISO_DATE, OP_LITERAL, OP_RU_NUMBER
from carbotek.formatters.ru import _format_amount


def _cell(operation: int, field: str, *arguments: str) -> PlainSection:
    expected, shown, otherwise = (*arguments, "", "", "")[:3]
    return PlainSection(["<w:t>", "", "</w:t>"], [(1, [(operation, field, expected, shown, otherwise)])])


def test_operation_codes_are_shared_with_the_extension() -> None:
    assert _native.OPERATIONS == _native_adapter.OPERATIONS


def test_mixed_text_and_xml_escaping() -> None:
    section = PlainSection(
        ["<w:t>", "", "</w:t>"],
        [(1, [(OP_FIELD, "a", "", "", ""), (OP_LITERAL, " & ", "", "", ""), (OP_FIELD, "b", "", "", "")])],
    )
    assert section.render([{"a": "<&\r", "b": 2}, {"a": "x", "b": None}]) == (
        "<w:t>&lt;&amp;&#13; &amp; 2</w:t><w:t>x &amp; </w:t>"
    )
    assert join_utf8([b"<t>", "Привет", b"</t>"]) == "<t>Привет</t>".encode()
    assert join_utf8([]) == b""


def test_empty_text_marker_after_sanitizing_controls() -> None:
    section = _cell(OP_FIELD, "a")
    assert section.render([{"a": ""}, {"a": "\x01"}, {}]) == "<w:t><!--ct-empty--></w:t>" * 3
    assert section.render_bytes([{"a": ""}, {"a": "\x01"}, {}]) == ("<w:t><!--ct-empty--></w:t>" * 3).encode()


def test_unsupported_values_request_python_fallback() -> None:
    section = _cell(OP_FIELD, "a")
    assert section.render([{"a": Decimal("1.25")}]) is None
    assert section.render([{"a": "\ud800"}]) is None
    assert section.render([{"a": object()}]) is None
    assert section.render([{"a": "valid"}, object()]) is None
    assert section.render_bytes([{"a": Decimal("1.25")}]) is None


def test_only_exact_lists_and_dicts_are_rendered() -> None:
    class Rows(list[dict[str, str]]):
        pass

    class Row(dict[str, str]):
        pass

    section = _cell(OP_FIELD, "a")
    assert section.render(Rows([{"a": "x"}])) is None
    assert section.render([Row(a="x")]) is None
    assert section.render(({"a": "x"},)) is None
    assert section.render([{"a": "x"}]) == "<w:t>x</w:t>"


def test_invalid_slot_layout_and_operation_are_rejected() -> None:
    with pytest.raises(ValueError, match="Invalid section slot layout"):
        PlainSection(["<w:t>", "placeholder", "</w:t>"], [(1, [(OP_FIELD, "a", "", "", "")])])
    with pytest.raises(ValueError, match="Invalid section slot operation"):
        _cell(len(_native_adapter.OPERATIONS), "a")


def test_fixed_date_number_and_condition() -> None:
    section = PlainSection(
        ["<w:t>", "", " / ", "", " / ", "", "</w:t>"],
        [
            (1, [(OP_ISO_DATE, "date", "", "", "")]),
            (3, [(OP_RU_NUMBER, "amount", "", "", "")]),
            (5, [(OP_IF_EQUAL, "status", "done", "Готово", "В работе")]),
        ],
    )
    assert section.render([{"date": "2024-02-29", "amount": 1234.5, "status": "done"}]) == (
        "<w:t>29.02.2024 / 1 234,50 / Готово</w:t>"
    )
    assert section.render([{"date": "2023-02-29", "amount": 1, "status": "done"}]) is None
    assert section.render([{"date": "2024-02-29", "amount": "1.0", "status": "done"}]) is None


def test_condition_distinguishes_missing_and_null() -> None:
    """JavaScript compares missing as undefined and explicit null as null."""
    rows = [{"s": None}, {}, {"s": "null"}, {"s": "x"}]
    assert _cell(OP_IF_EQUAL, "s", "null", "yes", "no").render(rows) == "".join(
        f"<w:t>{text}</w:t>" for text in ("yes", "no", "yes", "no")
    )
    assert _cell(OP_IF_EQUAL, "s", "x", "yes", "no").render(rows) == "".join(
        f"<w:t>{text}</w:t>" for text in ("no", "no", "no", "yes")
    )


def test_cents_operation_matches_digit_currency() -> None:
    section = _cell(OP_CENTS, "m")
    assert section.render([{"m": 1234.5}, {"m": -0.0}, {"m": None}, {}, {"m": 7}]) == "".join(
        f"<w:t>{text}</w:t>" for text in ("1 234,50", "0,00", "<!--ct-empty-->", "<!--ct-empty-->", "7,00")
    )
    for value in (1.234, "1,23", True, 1e12, float("nan")):
        assert section.render([{"m": value}]) is None


def test_plain_floats_use_js_number_spelling() -> None:
    """``String(x)`` of JS; Python integers keep all their digits (``number_string``)."""
    section = _cell(OP_FIELD, "value")
    values = [1.0, -0.0, 1e-5, 1.5e-7, 1e16, 1.5e17, 1e21, float("nan"), float("-inf")]
    assert section.render([{"value": value} for value in values]) == "".join(
        f"<w:t>{text}</w:t>"
        for text in (
            "1",
            "0",
            "0.00001",
            "1.5e-7",
            "10000000000000000",
            "150000000000000000",
            "1e+21",
            "NaN",
            "-Infinity",
        )
    )
    assert section.render([{"value": 10**30}, {"value": -(2**63)}, {"value": True}]) == (
        f"<w:t>{10**30}</w:t><w:t>{-(2**63)}</w:t><w:t>true</w:t>"
    )


def test_native_number_spelling_matches_python_on_random_doubles() -> None:
    from carbotek.formatters.number import number_string

    rng = random.Random(5)  # noqa: S311 - reproducible test values
    values = [rng.choice((-1, 1)) * rng.random() * 10 ** rng.uniform(-330, 308) for _ in range(20_000)]
    values += [float.fromhex(f"0x1.{rng.getrandbits(52):013x}p{rng.randrange(-1074, 1024)}") for _ in range(5_000)]
    rendered = _cell(OP_FIELD, "value").render([{"value": value} for value in values])
    assert rendered == "".join(f"<w:t>{number_string(value)}</w:t>" for value in values)


def test_native_ru_number_matches_python_format_number() -> None:
    from types import SimpleNamespace

    from carbotek.formatters.number import format_number

    rng = random.Random(9)  # noqa: S311 - reproducible test values
    values: list[float] = [(rng.randrange(-(10**12), 10**12) * 10 + 5) / 1000 for _ in range(5_000)]
    values += [rng.choice((-1, 1)) * rng.random() * 10 ** rng.uniform(-9, 12) for _ in range(5_000)]
    values += [0.0, -0.0, 1e-6, -1e-6, 9.99e-7, 1.005, 0.285, 2.675, 999_999_999_999.99]
    options = SimpleNamespace(lang="ru")
    rendered = _cell(OP_RU_NUMBER, "value").render([{"value": value} for value in values])
    assert rendered == "".join(f"<w:t>{format_number(value, '2', render_options=options)}</w:t>" for value in values)


def test_digit_currency_cents_matches_python_for_unique_amounts() -> None:
    random_values = random.Random(17)  # noqa: S311 - deterministic numeric test values
    cents = [random_values.randrange(-100_000_000, 100_000_000) for _ in range(2000)]
    cents.extend([0, 1, -1, 100, -100, 999_999_999])
    for amount in cents:
        value = amount / 100
        assert digit_currency_cents(value) == _format_amount(value, 2, False, None, True)
    for value in (0, -0.0, 1, -1, 30_000, -30_000, 9_999_999):
        assert digit_currency_cents(value) == _format_amount(value, 2, False, None, True)
    assert digit_currency_cents(1.234) is None
    assert digit_currency_cents("1,23") is None
    assert digit_currency_cents(True) is None
