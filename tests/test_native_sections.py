"""Whole renders: a native section must produce exactly the Python renderer's document."""

import random
from collections.abc import Callable
from datetime import date, timedelta
from io import BytesIO
from typing import Any
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from carbotek import Engine, RenderOptions, _native_adapter
from carbotek.docx import strings

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_PACKAGE = {
    "[Content_Types].xml": (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        "</Types>"
    ),
    "_rels/.rels": (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Target="word/document.xml" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"/>'
        "</Relationships>"
    ),
    "word/_rels/document.xml.rels": (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>'
    ),
}
pytestmark = pytest.mark.skipif(
    not _native_adapter.native_available(), reason="the native extension is disabled or missing"
)

_OPTIONS = RenderOptions.create(lang="ru", timezone="Europe/Moscow")


def _paragraph(text: str) -> str:
    return f'<w:p><w:r><w:t xml:space="preserve">{escape(text)}</w:t></w:r></w:p>'


def _cell(*paragraphs: str) -> str:
    return "<w:tc>" + "".join(_paragraph(text) for text in paragraphs) + "</w:tc>"


def _docx(*rows: str, before: str = "") -> bytes:
    body = f"{before}<w:tbl><w:tblGrid/>{''.join(rows)}</w:tbl>"
    document = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document xmlns:w="{_W}">'
        f"<w:body>{body}<w:sectPr/></w:body></w:document>"
    )
    payload = BytesIO()
    with ZipFile(payload, "w", ZIP_DEFLATED) as archive:
        for name, content in (*_PACKAGE.items(), ("word/document.xml", document)):
            archive.writestr(name, content)
    return payload.getvalue()


def _table(*cells: str, before: str = "") -> bytes:
    """One repeated row holding ``cells`` (one marker text per cell) over ``d.rows``."""
    return _docx(
        "<w:tr>" + "".join(_cell(text) for text in cells) + "</w:tr>",
        "<w:tr>" + _cell("{d.rows[i+1].s}") + "</w:tr>",
        before=before,
    )


class _Spy:
    """Counts the native sections that rendered instead of requesting the Python path."""

    def __init__(self) -> None:
        self.rendered = 0
        self.fallbacks = 0

    def compile(self, literals: list[str], slots: list[tuple[int, list[_native_adapter.SlotPart]]]) -> Any:
        section = _native_adapter.compile_plain_section(literals, slots)
        if section is None:
            return None
        spy = self

        class Section:
            def _count(self, result: Any) -> Any:
                if result is None:
                    spy.fallbacks += 1
                else:
                    spy.rendered += 1
                return result

            def render(self, rows: list[object]) -> str | None:
                return self._count(section.render(rows))

            def render_bytes(self, rows: list[object]) -> bytes | None:
                return self._count(section.render_bytes(rows))

        return Section()


def _render_both(template: bytes, data: Any, monkeypatch: pytest.MonkeyPatch) -> tuple[bytes, bytes, _Spy]:
    """document.xml rendered with the native sections and on the Python path."""
    spy = _Spy()
    with monkeypatch.context() as patch:
        patch.setattr(strings, "compile_plain_section", spy.compile)
        native = Engine().render(template, data, _OPTIONS)
    with monkeypatch.context() as patch:
        patch.setattr(_native_adapter, "_native", None)
        python = Engine().render(template, data, _OPTIONS)
    return (
        ZipFile(BytesIO(native)).read("word/document.xml"),
        ZipFile(BytesIO(python)).read("word/document.xml"),
        spy,
    )


def _texts(document: bytes) -> list[str]:
    from lxml import etree

    root = etree.fromstring(document)
    return ["".join(paragraph.itertext()) for paragraph in root.iter(f"{{{_W}}}p")]


def test_quoted_and_null_condition_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    """Single quotes delimit an argument (doubled inside); double quotes are ordinary text in JS."""
    template = _table(
        "{d.rows[i].s:ifEQ('done'):show('Готово'):elseShow('В работе')}",
        "{d.rows[i].s:ifEQ(null):show(yes):elseShow(no)}",
        "{d.rows[i].s:ifEQ('a, b'):show('it''s'):elseShow(\"x\")}",
    )
    data = {"rows": [{"s": "done"}, {"s": "new"}, {"s": None}, {}, {"s": "a, b"}]}
    native, python, spy = _render_both(template, data, monkeypatch)
    assert spy.rendered == 1
    assert native == python
    assert _texts(native) == [
        "Готово", "no", '"x"',
        "В работе", "no", '"x"',
        "В работе", "yes", '"x"',
        "В работе", "no", '"x"',
        "В работе", "no", "it's",
    ]  # fmt: skip


def test_numeric_looking_arguments_keep_their_spelling(monkeypatch: pytest.MonkeyPatch) -> None:
    """JS passes ``007`` and ``+5`` as strings: ifEQ compares text and show prints it as written."""
    template = _table("{d.rows[i].s:ifEQ(1):show(007):elseShow(+5)}")
    native, python, spy = _render_both(template, {"rows": [{"s": "1"}, {"s": "01"}, {"s": "x"}]}, monkeypatch)
    assert spy.rendered == 1
    assert native == python
    assert _texts(native) == ["007", "+5", "+5"]


def test_unsupported_value_falls_back_for_the_whole_section(monkeypatch: pytest.MonkeyPatch) -> None:
    from decimal import Decimal

    template = _table("{d.rows[i].s}", "{d.rows[i].m:digitCurrency}")
    data = {"rows": [{"s": "a", "m": 1.5}, {"s": "b", "m": Decimal("2.25")}, {"s": "c", "m": 1.005}]}
    native, python, spy = _render_both(template, data, monkeypatch)
    assert spy.fallbacks == 1
    assert native == python


def test_tiny_negative_amounts_keep_their_sign(monkeypatch: pytest.MonkeyPatch) -> None:
    """roundHalfUp keeps a value whose String() has an exponent: a negative one prints "-0,00"."""
    template = _table("{d.rows[i].m:digitCurrency}")
    data = {"rows": [{"m": value} for value in (-1e-10, -5e-324, -1e-9, 1234.5, 1e-10, -0.0)]}
    native, python, _ = _render_both(template, data, monkeypatch)
    assert native == python
    assert _texts(native) == ["-0,00", "-0,00", "-0,00", "1 234,50", "0,00", "0,00"]


def test_high_precision_format_number_stays_on_python(monkeypatch: pytest.MonkeyPatch) -> None:
    """With useHighPrecisionArithmetic formatN skips round(): 1.005 stays "1,00" as in JS."""
    template = _table("{d.rows[i].n:formatN(2)}", before=_paragraph("x{o.useHighPrecisionArithmetic=true}"))
    data = {"rows": [{"n": value} for value in (1.005, 2.675, -0.0001)]}
    native, python, spy = _render_both(template, data, monkeypatch)
    assert spy.rendered == 0
    assert native == python
    assert _texts(native) == ["x", "1,00", "2,67", "-0,00"]


def _random_text(rng: random.Random) -> str:
    alphabet = "ab xyz09&<>\"'\r\n\t\x01\x0b￾Жжё€😀 "
    return "".join(rng.choice(alphabet) for _ in range(rng.randrange(0, 8)))


def _maybe(rng: random.Random, value: Callable[[], Any], row: dict[str, Any], key: str) -> None:
    roll = rng.random()
    if roll < 0.06:
        return  # missing field
    row[key] = None if roll < 0.12 else value()


def _row(rng: random.Random) -> dict[str, Any]:
    row: dict[str, Any] = {}
    texts = ["done", "null", "x y", "", "Done"]
    _maybe(rng, lambda: rng.choice(texts) if rng.random() < 0.4 else _random_text(rng), row, "s")
    numbers: list[Callable[[], Any]] = [
        lambda: rng.randrange(-(10**12), 10**12),
        lambda: rng.choice((-1, 1)) * rng.random() * 10 ** rng.uniform(-9, 23),
        lambda: rng.choice((0.0, -0.0, float("nan"), float("inf"), float("-inf"), 10**25, -(2**70))),
        lambda: rng.choice((True, False)),
    ]
    _maybe(rng, lambda: rng.choice(numbers)(), row, "v")
    _maybe(rng, lambda: (date(1900, 1, 1) + timedelta(days=rng.randrange(0, 73_000))).isoformat(), row, "d")
    half_cents: list[Callable[[], Any]] = [
        lambda: rng.randrange(-(10**13), 10**13) / 100,
        lambda: (rng.randrange(-(10**9), 10**9) * 10 + 5) / 1000,
        lambda: rng.randrange(-(10**11), 10**11),
        lambda: rng.choice((0, 0.0, -0.0, 0.125, -0.125, 1.005, 2.675)),
    ]
    _maybe(rng, lambda: rng.choice(half_cents)(), row, "n")
    _maybe(rng, lambda: rng.choice((rng.randrange(-(10**13), 10**13) / 100, rng.randrange(-(10**9), 10**9))), row, "m")
    return row


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_random_rows_render_like_python(seed: int, monkeypatch: pytest.MonkeyPatch) -> None:
    template = _table(
        "{d.rows[i].s}",
        "{d.rows[i].v}",
        "Итого: {d.rows[i].s} / {d.rows[i].v}.",
        "{d.rows[i].d:formatD(DD.MM.YYYY)}",
        "{d.rows[i].d:formatD('DD.MM.YYYY')}",
        "{d.rows[i].n:formatN(2)}",
        "{d.rows[i].m:digitCurrency}",
        "{d.rows[i].s:ifEQ('x y'):show('да, «ок»'):elseShow(нет)}",
        "{d.rows[i].s:ifEQ(null):show(empty):elseShow(set)}",
    )
    rng = random.Random(seed)  # noqa: S311 - reproducible test data
    data = {"rows": [_row(rng) for _ in range(400)]}
    native, python, spy = _render_both(template, data, monkeypatch)
    assert spy.rendered == 1, "the generated values must stay inside the native subset"
    assert native == python


def test_nested_loop_sections_render_like_python(monkeypatch: pytest.MonkeyPatch) -> None:
    template = _docx(
        "<w:tr>"
        + _cell("{d.rows[i].s}")
        + _cell("{d.rows[i].items[j].v:formatN(2)} {d.rows[i].items[j].s}", "{d.rows[i].items[j+1].v}")
        + "</w:tr>",
        "<w:tr>" + _cell("{d.rows[i+1].s}") + "</w:tr>",
    )
    rng = random.Random(7)  # noqa: S311 - reproducible test data
    data = {
        "rows": [
            {
                "s": f"r{index}",
                "items": [{"v": rng.randrange(-(10**8), 10**8) / 100, "s": _random_text(rng)} for _ in range(5)],
            }
            for index in range(30)
        ]
    }
    native, python, spy = _render_both(template, data, monkeypatch)
    assert spy.rendered >= 30
    assert native == python
