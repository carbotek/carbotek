"""Space before the first paragraph of the document or of a section in PDF.

Word keeps it at the top of the page: an 18 pt space before moves the first line of the document, and of a
section that starts on a new page, 18 pt down, as it does in the middle of a page. The checks compare synthetic
documents that differ only in that space, so they do not depend on the installed fonts.
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
_W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
_PAGE = (
    '<w:pgSz w:w="11906" w:h="16838"/>'
    '<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440" w:header="708" w:footer="708"/>'
)


def _paragraph(text: str, *, before: int = 0, section_end: bool = False) -> str:
    """`before` in twentieths of a point; `section_end` closes a section that the next one follows on a new page."""
    section = f'<w:sectPr><w:type w:val="nextPage"/>{_PAGE}</w:sectPr>' if section_end else ""
    return (
        f'<w:p><w:pPr><w:spacing w:before="{before}" w:after="0"/>{section}</w:pPr><w:r><w:t>{text}</w:t></w:r></w:p>'
    )


def _docx(body: str) -> bytes:
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
        "word/document.xml": f"<w:document {_W}><w:body>{body}<w:sectPr>{_PAGE}</w:sectPr></w:body></w:document>",
        # Word 2013+ layout, as in the documents carbotek renders.
        "word/settings.xml": (
            f'<w:settings {_W}><w:compat><w:compatSetting w:name="compatibilityMode" '
            'w:uri="http://schemas.microsoft.com/office/word" w:val="15"/></w:compat></w:settings>'
        ),
    }
    stream = BytesIO()
    with ZipFile(stream, "w", ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return stream.getvalue()


def _top(body: str) -> tuple[int, float]:
    """(page index, top in points) of the "PROBE" text."""
    document = pypdfium2.PdfDocument(convert_docx_to_pdf(_docx(body)))
    for index, page in enumerate(document):
        text = page.get_textpage()
        found = text.get_text_range().find("PROBE")
        if found >= 0:
            return index, page.get_height() - text.get_charbox(found)[3]
    raise AssertionError("PROBE not found")


def _first_of_document(before: int) -> str:
    return _paragraph("PROBE", before=before) + _paragraph("NEXT")


def _first_of_section(before: int) -> str:
    return _paragraph("PREVIOUS SECTION", section_end=True) + _paragraph("PROBE", before=before)


def _mid_page(before: int) -> str:
    return _paragraph("ABOVE") + _paragraph("PROBE", before=before)


@pytest.mark.parametrize(
    ("build", "page"),
    [(_first_of_document, 0), (_first_of_section, 1), (_mid_page, 0)],
    ids=["document", "section", "mid-page"],
)
def test_space_before_the_first_paragraph_is_kept(build: Callable[[int], str], page: int) -> None:
    plain_page, plain_top = _top(build(0))
    spaced_page, spaced_top = _top(build(360))
    assert plain_page == spaced_page == page
    assert spaced_top - plain_top == pytest.approx(18, abs=_TOLERANCE)
