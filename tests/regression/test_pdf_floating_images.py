"""Floating pictures without text wrapping (wrapNone) in PDF: a red circle drawn over a green square.

The pictures overlap instead of being stacked, the text after them keeps its place, and a pair that
does not fit at the bottom of a page moves to the next page whole. Documents are synthetic.
"""

import os
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from PIL import Image, ImageChops, ImageDraw

from carbotek import convert_docx_to_pdf
from carbotek._native_adapter import native_pdf_available

pypdfium2 = pytest.importorskip("pypdfium2")

# The bundled engine carries the layout fix; upstream dxpdf 0.8.1 fails these checks.
# CARBOTEK_TEST_PDF_FALLBACK=1 runs them against an installed dxpdf package instead.
pytestmark = pytest.mark.skipif(
    not native_pdf_available() and os.environ.get("CARBOTEK_TEST_PDF_FALLBACK") != "1",
    reason="needs the bundled PDF engine",
)

_SIDE = 1440000  # EMU, 113.4 pt
_NAMESPACES = (
    'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
    'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
    'xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture"'
)


def _png(draw_circle: bool) -> bytes:
    image = Image.new("RGBA", (200, 200), (0, 0, 0, 0) if draw_circle else (0, 160, 0, 255))
    if draw_circle:
        ImageDraw.Draw(image).ellipse((0, 0, 199, 199), fill=(220, 0, 0, 255))
    stream = BytesIO()
    image.save(stream, format="PNG")
    return stream.getvalue()


def _anchor(index: int, relationship: str, layer: int) -> str:
    return (
        f'<w:r><w:drawing><wp:anchor distT="0" distB="0" distL="0" distR="0" simplePos="0" '
        f'relativeHeight="{251657216 + layer * 1024}" behindDoc="0" locked="0" layoutInCell="1" allowOverlap="1">'
        '<wp:simplePos x="0" y="0"/>'
        '<wp:positionH relativeFrom="column"><wp:posOffset>720000</wp:posOffset></wp:positionH>'
        '<wp:positionV relativeFrom="paragraph"><wp:posOffset>0</wp:posOffset></wp:positionV>'
        f'<wp:extent cx="{_SIDE}" cy="{_SIDE}"/><wp:effectExtent l="0" t="0" r="0" b="0"/><wp:wrapNone/>'
        f'<wp:docPr id="{index}" name="Picture {index}"/>'
        '<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">'
        f'<pic:pic><pic:nvPicPr><pic:cNvPr id="{index}" name="Picture {index}"/><pic:cNvPicPr/></pic:nvPicPr>'
        f'<pic:blipFill><a:blip r:embed="{relationship}"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill>'
        f'<pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{_SIDE}" cy="{_SIDE}"/></a:xfrm>'
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr></pic:pic>'
        "</a:graphicData></a:graphic></wp:anchor></w:drawing></w:r>"
    )


def _paragraph(text: str, runs: str = "") -> str:
    return f"<w:p>{runs}<w:r><w:t>{text}</w:t></w:r></w:p>"


def _docx(
    *, pictures: bool, padding: int = 0, cell: bool = False, styled: bool = False, mixed_cell: bool = False
) -> bytes:
    """Anchor paragraph with the pictures, then two text paragraphs; in the body or in a table cell."""
    anchors = _anchor(1, "rIdSquare", 1) + _anchor(2, "rIdCircle", 2) if pictures else ""
    content = _paragraph("ANCHOR", anchors) + _paragraph("LINE AFTER") + _paragraph("NEXT")
    if cell:
        grid = '<w:gridCol w:w="4677"/><w:gridCol w:w="4678"/>' if mixed_cell else '<w:gridCol w:w="9355"/>'
        other = ("<w:tc>" + "".join(_paragraph(f"OTHER {i}") for i in range(4)) + "</w:tc>") if mixed_cell else ""
        content = (
            "<w:tbl><w:tblPr>"
            + ('<w:tblStyle w:val="ProbeTable"/>' if styled else "")
            + f'<w:tblW w:w="5000" w:type="pct"/></w:tblPr><w:tblGrid>{grid}</w:tblGrid>'
            f'<w:tr><w:tc><w:tcPr><w:tcW w:w="{4677 if mixed_cell else 9355}" w:type="dxa"/></w:tcPr>{content}</w:tc>{other}</w:tr></w:tbl>'
            + _paragraph("AFTER TABLE")
        )
    body = "".join(_paragraph(f"PADDING {index}") for index in range(padding)) + content
    files = {
        "[Content_Types].xml": (
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Default Extension="png" ContentType="image/png"/>'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Override PartName="/word/document.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            '<Override PartName="/word/styles.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
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
            '<Relationship Id="rIdSquare" Target="media/square.png" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"/>'
            '<Relationship Id="rIdCircle" Target="media/circle.png" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"/>'
            "</Relationships>"
        ),
        "word/document.xml": (
            f"<w:document {_NAMESPACES}><w:body>{body}"
            '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
            '<w:pgMar w:top="1134" w:right="850" w:bottom="1134" w:left="1701"/></w:sectPr>'
            "</w:body></w:document>"
        ),
        # The original minimal fixture omitted styles: Word supplied Calibri
        # 11 / 1.08 / 8pt after, while dxpdf supplied Times New Roman 12 / no
        # spacing. Record the Word defaults so the comparison is portable.
        "word/styles.xml": (
            f"<w:styles {_NAMESPACES}><w:docDefaults>"
            '<w:rPrDefault><w:rPr><w:rFonts w:ascii="Calibri" w:hAnsi="Calibri"/>'
            '<w:sz w:val="22"/></w:rPr></w:rPrDefault>'
            '<w:pPrDefault><w:pPr><w:spacing w:after="160" w:line="259" w:lineRule="auto"/>'
            "</w:pPr></w:pPrDefault></w:docDefaults>"
            '<w:style w:type="table" w:styleId="ProbeTable"><w:name w:val="Probe Table"/></w:style>'
            "</w:styles>"
        ),
    }
    stream = BytesIO()
    with ZipFile(stream, "w", ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
        if pictures:
            archive.writestr("word/media/square.png", _png(draw_circle=False))
            archive.writestr("word/media/circle.png", _png(draw_circle=True))
    return stream.getvalue()


def _pages(pdf: bytes) -> list[Image.Image]:
    document = pypdfium2.PdfDocument(pdf)
    return [page.render(scale=1).to_pil().convert("RGB") for page in document]


def _box(image: Image.Image, color: str) -> tuple[int, int, int, int] | None:
    """Bounding box of the clearly green or clearly red pixels."""
    red, green, blue = image.split()
    high, low = (green, red) if color == "green" else (red, green)
    mask = ImageChops.multiply(
        ImageChops.multiply(
            high.point(lambda value: 255 if value > 120 else 0), low.point(lambda value: 255 if value < 80 else 0)
        ),
        blue.point(lambda value: 255 if value < 80 else 0),
    )
    return mask.getbbox()


def _green(pixel: object) -> bool:
    red, green, blue = pixel  # type: ignore[misc]
    return green > 120 and red < 80 and blue < 80


def _red(pixel: object) -> bool:
    red, green, blue = pixel  # type: ignore[misc]
    return red > 180 and green < 60 and blue < 60


def _text_top(pdf: bytes, needle: str) -> tuple[int, float]:
    document = pypdfium2.PdfDocument(pdf)
    for index, page in enumerate(document):
        text = page.get_textpage()
        searcher = text.search(needle)
        found = searcher.get_next()
        if found is not None:
            start, _count = found
            _left, _bottom, _right, top = text.get_charbox(start)
            return index, round(top, 1)
    raise AssertionError(f"{needle!r} not found")


@pytest.mark.parametrize("cell", [False, True], ids=["body", "cell"])
def test_circle_is_drawn_over_the_square(cell: bool) -> None:
    (page, *rest) = _pages(convert_docx_to_pdf(_docx(pictures=True, cell=cell)))
    assert not rest
    square = _box(page, "green")
    circle = _box(page, "red")
    assert square is not None and circle is not None
    left, top, right, bottom = square
    # One square, not two pictures one below the other: the bounding box stays square.
    assert abs((right - left) - (bottom - top)) <= 3
    center = ((left + right) // 2, (top + bottom) // 2)
    assert _red(page.getpixel(center)), "the circle covers the middle of the square"
    assert _green(page.getpixel((left + 3, top + 3))), "the square shows in the corners"
    circle_left, circle_top, circle_right, circle_bottom = circle
    assert left - 3 <= circle_left and circle_right <= right + 3
    assert top - 3 <= circle_top and circle_bottom <= bottom + 3


@pytest.mark.parametrize("cell", [False, True], ids=["body", "cell"])
def test_text_after_the_pictures_keeps_its_place(cell: bool) -> None:
    with_pictures = convert_docx_to_pdf(_docx(pictures=True, cell=cell))
    without_pictures = convert_docx_to_pdf(_docx(pictures=False, cell=cell))
    for needle in ("ANCHOR", "LINE AFTER", "NEXT"):
        assert _text_top(with_pictures, needle) == _text_top(without_pictures, needle)
    if cell:
        # Word's wrapNone drawings overflow the row: following body text
        # must not move either. Painted bounds matter only for page-fit.
        assert _text_top(with_pictures, "AFTER TABLE") == _text_top(without_pictures, "AFTER TABLE")


@pytest.mark.parametrize("styled", [False, True], ids=["unstyled", "styled"])
def test_cell_column_anchor_is_not_shifted_by_page_margin_twice(styled: bool) -> None:
    (page, *rest) = _pages(convert_docx_to_pdf(_docx(pictures=True, cell=True, styled=styled)))
    assert not rest
    square = _box(page, "green")
    assert square is not None
    # Word export: left margin 1701/20 pt + column-relative 720000/12700 pt.
    expected_left = 1701 / 20 + 720000 / 12700
    assert abs(square[0] - expected_left) <= 2
    assert abs(square[1] - 1134 / 20) <= 2


@pytest.mark.skipif(
    os.environ.get("CARBOTEK_WORD_REFERENCE_FONTS") != "1",
    reason="Word reference requires the same installed Calibri font",
)
@pytest.mark.parametrize("cell", [False, True], ids=["body", "cell"])
def test_text_matches_word_export(cell: bool) -> None:
    import json
    from pathlib import Path

    reference = json.loads(Path(__file__).with_name("pdf_floating_images_word.json").read_text())
    pdf = convert_docx_to_pdf(_docx(pictures=True, cell=cell))
    for needle, (expected_page, expected_top) in reference["cell" if cell else "body"].items():
        page, top = _text_top(pdf, needle)
        assert page == expected_page
        assert abs(top - expected_top) <= 0.75, f"{needle}: PDF {top}, Word {expected_top}"


def _padding_near_page_bottom(cell: bool) -> int:
    """Most padding paragraphs that still keep the whole text on the first page."""
    padding = 0
    while _text_top(convert_docx_to_pdf(_docx(pictures=False, padding=padding + 1, cell=cell)), "NEXT")[0] == 0:
        padding += 1
    return padding


@pytest.mark.parametrize("cell", [False, True], ids=["body", "cell"])
def test_pictures_that_do_not_fit_move_to_the_next_page_whole(cell: bool) -> None:
    # The anchor paragraph is a few lines above the bottom margin; the 113 pt pictures are not.
    padding = _padding_near_page_bottom(cell)
    pages = _pages(convert_docx_to_pdf(_docx(pictures=True, padding=padding, cell=cell)))
    assert _box(pages[0], "green") is None, "no part of the square is left on the first page"
    assert _box(pages[0], "red") is None
    square = _box(pages[1], "green")
    assert square is not None
    left, top, right, bottom = square
    assert _red(pages[1].getpixel(((left + right) // 2, (top + bottom) // 2)))


def test_other_cell_line_cuts_cannot_strand_a_floating_picture() -> None:
    padding = _padding_near_page_bottom(False)
    pdf = convert_docx_to_pdf(_docx(pictures=True, padding=padding, cell=True, mixed_cell=True))
    pages = _pages(pdf)
    assert len(pages) == 2
    assert _box(pages[0], "green") is None
    assert _box(pages[0], "red") is None
    assert _box(pages[1], "green") is not None
    assert _text_top(pdf, "ANCHOR")[0] == _text_top(pdf, "OTHER 3")[0] == 1
