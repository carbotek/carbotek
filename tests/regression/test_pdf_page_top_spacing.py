"""Space before a paragraph at the top of a page or column in PDF, as Word lays it out.

Word keeps the space before the first paragraph of the document and of a section that starts on a new page.
At the top of a page or column that the text flowed to, or that a page break led to, it drops the space
before; a paragraph's own `pageBreakBefore`, or a page break at its start, drops it from Word 2013
(`compatibilityMode` 15) on and keeps it in earlier compatibility modes. Text after a page break inside a
paragraph continues that paragraph and has no space before.

Each check compares two synthetic documents that differ only in an 18 pt space before the "PROBE"
paragraph, so it does not depend on the installed fonts. Lines that fill a page have an exact height.
"""

import os
from collections.abc import Callable
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from carbotek import convert_docx_to_pdf
from carbotek._native_adapter import native_pdf_available

pypdfium2 = pytest.importorskip("pypdfium2")

pytestmark = pytest.mark.skipif(
    not native_pdf_available() and os.environ.get("CARBOTEK_TEST_PDF_FALLBACK") != "1",
    reason="needs the bundled PDF engine",
)

_TOLERANCE = 0.1  # pt
_SPACE = 360  # twentieths of a point: 18 pt
_W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
_PAGE = (
    '<w:pgSz w:w="11906" w:h="16838"/>'
    '<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440" w:header="708" w:footer="708"/>'
)
_TWO_COLUMNS = '<w:cols w:num="2" w:space="708"/>'
_PAGE_BREAK = '<w:r><w:br w:type="page"/></w:r>'
_FULL_PAGE = 49  # exact 14 pt lines that fill the 697.9 pt text height of the page


def _paragraph(text: str = "", *, before: int = 0, properties: str = "", runs: str = "", tail: str = "") -> str:
    """A paragraph of exact 14 pt lines; `runs` come before the text, `tail` after it."""
    spacing = f'<w:spacing w:before="{before}" w:after="0" w:line="280" w:lineRule="exact"/>'
    text_run = f"<w:r><w:t>{text}</w:t></w:r>" if text else ""
    return f"<w:p><w:pPr>{spacing}{properties}</w:pPr>{runs}{text_run}{tail}</w:p>"


def _fill(lines: int) -> str:
    return "".join(_paragraph(f"LINE {index}") for index in range(lines))


def _docx(body: str, *, columns: str = "", word_2013: bool = True) -> bytes:
    compat = (
        '<w:compat><w:compatSetting w:name="compatibilityMode" '
        'w:uri="http://schemas.microsoft.com/office/word" w:val="15"/></w:compat>'
        if word_2013
        else ""
    )
    files = {
        "[Content_Types].xml": (
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Override PartName="/word/document.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            '<Override PartName="/word/settings.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml"/>'
            "</Types>"
        ),
        "_rels/.rels": (
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Target="word/document.xml" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"/>'
            "</Relationships>"
        ),
        "word/_rels/document.xml.rels": (
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rIdSettings" Target="settings.xml" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/settings"/>'
            "</Relationships>"
        ),
        "word/document.xml": (
            f"<w:document {_W}><w:body>{body}<w:sectPr>{_PAGE}{columns}</w:sectPr></w:body></w:document>"
        ),
        "word/settings.xml": f"<w:settings {_W}>{compat}</w:settings>",
    }
    stream = BytesIO()
    with ZipFile(stream, "w", ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return stream.getvalue()


def _probe(docx: bytes) -> tuple[int, float, float]:
    """(page index, left, top) in points of the "PROBE" text."""
    document = pypdfium2.PdfDocument(convert_docx_to_pdf(docx))
    for index, page in enumerate(document):
        text = page.get_textpage()
        found = text.get_text_range().find("PROBE")
        if found >= 0:
            left, _bottom, _right, top = text.get_charbox(found)
            return index, left, page.get_height() - top
    raise AssertionError("PROBE not found")


def _applied_space(build: Callable[[int], str], *, columns: str = "", word_2013: bool = True) -> float:
    """How much an 18 pt space before moves "PROBE", which stays in the same page and column."""
    plain_page, plain_left, plain_top = _probe(_docx(build(0), columns=columns, word_2013=word_2013))
    page, left, top = _probe(_docx(build(_SPACE), columns=columns, word_2013=word_2013))
    assert (page, round(left)) == (plain_page, round(plain_left))
    return top - plain_top


def _first_of_document(before: int) -> str:
    return _paragraph("PROBE", before=before) + _paragraph("NEXT")


def _first_of_section(before: int) -> str:
    section_end = f'<w:sectPr><w:type w:val="nextPage"/>{_PAGE}</w:sectPr>'
    return _paragraph("PREVIOUS SECTION", properties=section_end) + _paragraph("PROBE", before=before)


def _mid_page(before: int) -> str:
    return _paragraph("ABOVE") + _paragraph("PROBE", before=before)


def _natural_page_break(before: int) -> str:
    return _fill(_FULL_PAGE) + _paragraph("PROBE", before=before)


def _kept_with_next(before: int) -> str:
    # The probe would fit, but not with the three lines it is kept with.
    three_lines = _paragraph("X", tail="<w:r><w:br/><w:t>Y</w:t><w:br/><w:t>Z</w:t></w:r>")
    return _fill(_FULL_PAGE - 2) + _paragraph("PROBE", before=before, properties="<w:keepNext/>") + three_lines


def _break_ending_previous_paragraph(before: int) -> str:
    return _paragraph("ABOVE", tail=_PAGE_BREAK) + _paragraph("PROBE", before=before)


def _break_only_paragraph(before: int) -> str:
    return _paragraph("ABOVE") + _paragraph(runs=_PAGE_BREAK) + _paragraph("PROBE", before=before)


def _break_inside_paragraph(before: int) -> str:
    return _paragraph("ABOVE") + _paragraph(
        "FIRST LINE", before=before, tail=_PAGE_BREAK + "<w:r><w:t>PROBE</w:t></w:r>"
    )


def _page_break_before(before: int) -> str:
    return _paragraph("ABOVE") + _paragraph("PROBE", before=before, properties="<w:pageBreakBefore/>")


def _break_starting_paragraph(before: int) -> str:
    return _paragraph("ABOVE") + _paragraph("PROBE", before=before, runs=_PAGE_BREAK)


@pytest.mark.parametrize(
    "build",
    [_first_of_document, _first_of_section, _mid_page],
    ids=["document", "section", "mid-page"],
)
def test_space_before_is_kept_at_the_start_of_the_document_or_a_section(build: Callable[[int], str]) -> None:
    assert _applied_space(build) == pytest.approx(18, abs=_TOLERANCE)


@pytest.mark.parametrize("word_2013", [True, False], ids=["word-2013", "compatibility-mode"])
@pytest.mark.parametrize(
    "build",
    [
        _natural_page_break,
        _kept_with_next,
        _break_ending_previous_paragraph,
        _break_only_paragraph,
        _break_inside_paragraph,
    ],
    ids=["natural", "keep-next", "break-ending-previous", "break-only-paragraph", "break-inside-paragraph"],
)
def test_space_before_is_dropped_at_a_page_break(build: Callable[[int], str], word_2013: bool) -> None:
    assert _applied_space(build, word_2013=word_2013) == pytest.approx(0, abs=_TOLERANCE)


def test_space_before_is_dropped_when_text_flows_to_the_next_column() -> None:
    assert _applied_space(_natural_page_break, columns=_TWO_COLUMNS) == pytest.approx(0, abs=_TOLERANCE)


@pytest.mark.parametrize(
    "build", [_page_break_before, _break_starting_paragraph], ids=["page-break-before", "break-starting-paragraph"]
)
@pytest.mark.parametrize(("word_2013", "expected"), [(True, 0), (False, 18)], ids=["word-2013", "compatibility-mode"])
def test_own_page_break_drops_the_space_before_from_word_2013(
    build: Callable[[int], str], word_2013: bool, expected: float
) -> None:
    assert _applied_space(build, word_2013=word_2013) == pytest.approx(expected, abs=_TOLERANCE)
