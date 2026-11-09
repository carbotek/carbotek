"""Table cell margins (`<w:tblCellMar>`) in PDF cascade per side, and a stated zero is a value.

Word reads each side separately: a table's `<w:left w:w="0"/>` removes its style's left margin and keeps the
style's right one, and a style that states only one side inherits the others from its `basedOn` parent.
The checks compare the same text in synthetic tables, so they do not depend on the installed fonts.
"""

import os
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

_STYLE_MARGIN = 108 / 20  # pt, Word's usual 108-twip left/right cell margin
_TOLERANCE = 0.1  # pt
_W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def _margins(**sides: int) -> str:
    return (
        "<w:tblCellMar>"
        + "".join(f'<w:{side} w:w="{w}" w:type="dxa"/>' for side, w in sides.items())
        + "</w:tblCellMar>"
    )


def _paragraph(text: str, alignment: str) -> str:
    return f'<w:p><w:pPr><w:jc w:val="{alignment}"/></w:pPr><w:r><w:t>{text}</w:t></w:r></w:p>'


def _docx(*, style: str = "Grid", direct: str = "") -> bytes:
    """A body line, then a 300 pt table whose cell holds a left- and a right-aligned line."""
    table = (
        f'<w:tbl><w:tblPr><w:tblStyle w:val="{style}"/><w:tblW w:w="6000" w:type="dxa"/>'
        f'<w:tblInd w:w="0" w:type="dxa"/><w:tblLayout w:type="fixed"/>{direct}</w:tblPr>'
        '<w:tblGrid><w:gridCol w:w="6000"/></w:tblGrid><w:tr><w:tc><w:tcPr><w:tcW w:w="6000" w:type="dxa"/></w:tcPr>'
        + _paragraph("LEFTCELL", "left")
        + _paragraph("RIGHTCELL", "right")
        + "</w:tc></w:tr></w:tbl>"
    )
    files = {
        "[Content_Types].xml": (
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Override PartName="/word/document.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            '<Override PartName="/word/styles.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
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
            '<Relationship Id="rIdStyles" Target="styles.xml" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles"/>'
            '<Relationship Id="rIdSettings" Target="settings.xml" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/settings"/>'
            "</Relationships>"
        ),
        "word/document.xml": (
            f"<w:document {_W}><w:body>{_paragraph('LEFTBODY', 'left')}{table}"
            '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
            '<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/></w:sectPr>'
            "</w:body></w:document>"
        ),
        # "Grid" carries Word's usual 108-twip side margins; "ChildLeft0" states only a zero left.
        # Word 2013+ layout; without it Word measures `tblInd` to the cell text, as Word 2010 did.
        "word/settings.xml": (
            f'<w:settings {_W}><w:compat><w:compatSetting w:name="compatibilityMode" '
            'w:uri="http://schemas.microsoft.com/office/word" w:val="15"/></w:compat></w:settings>'
        ),
        "word/styles.xml": (
            f"<w:styles {_W}>"
            '<w:style w:type="table" w:styleId="Grid"><w:name w:val="Grid"/>'
            f"<w:tblPr>{_margins(top=0, left=108, bottom=0, right=108)}</w:tblPr></w:style>"
            '<w:style w:type="table" w:styleId="ChildLeft0"><w:name w:val="ChildLeft0"/><w:basedOn w:val="Grid"/>'
            f"<w:tblPr>{_margins(left=0)}</w:tblPr></w:style>"
            "</w:styles>"
        ),
    }
    stream = BytesIO()
    with ZipFile(stream, "w", ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return stream.getvalue()


def _cell_edges(docx: bytes) -> tuple[float, float]:
    """(left margin, right edge): where "LEFTCELL" starts relative to "LEFTBODY", and where "RIGHTCELL" ends."""
    document = pypdfium2.PdfDocument(convert_docx_to_pdf(docx))
    text = document[0].get_textpage()
    content = text.get_text_range()

    def left(word: str) -> float:
        return text.get_charbox(content.index(word))[0]

    def right(word: str) -> float:
        return text.get_charbox(content.index(word) + len(word) - 1)[2]

    return left("LEFTCELL") - left("LEFTBODY"), right("RIGHTCELL")


def test_style_margins_indent_the_cell_text() -> None:
    left, _right = _cell_edges(_docx())
    assert left == pytest.approx(_STYLE_MARGIN, abs=_TOLERANCE)


@pytest.mark.parametrize(
    "docx",
    [_docx(direct=_margins(left=0)), _docx(style="ChildLeft0")],
    ids=["table-over-style", "style-over-parent"],
)
def test_stated_zero_left_margin_overrides_the_inherited_one(docx: bytes) -> None:
    # Word: the text starts at the cell's left edge, and the right margin is still the style's.
    _style_left, style_right = _cell_edges(_docx())
    left, right = _cell_edges(docx)
    assert left == pytest.approx(0, abs=_TOLERANCE)
    assert right == pytest.approx(style_right, abs=_TOLERANCE)


def test_unstated_sides_of_a_table_margin_come_from_the_style() -> None:
    # Word: a zero right margin on the table keeps the style's left one.
    _style_left, style_right = _cell_edges(_docx())
    left, right = _cell_edges(_docx(direct=_margins(right=0)))
    assert left == pytest.approx(_STYLE_MARGIN, abs=_TOLERANCE)
    assert right - style_right == pytest.approx(_STYLE_MARGIN, abs=_TOLERANCE)
