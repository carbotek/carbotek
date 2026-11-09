"""Pictures positioned relative to their paragraph (`relativeFrom="paragraph"`/`"line"`) in PDF.

Word measures the offset from the paragraph's top edge: where the preceding block ends, including its
space after, or the top of the table cell. The paragraph's space before therefore moves its text, not
its pictures, and a larger space after above it absorbs the space before entirely. The checks compare
synthetic documents that differ only in spacing, so they do not depend on the installed fonts.
"""

import os
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from PIL import Image

from carbotek import convert_docx_to_pdf
from carbotek._native_adapter import native_pdf_available

pypdfium2 = pytest.importorskip("pypdfium2")
pdfium_raw = pytest.importorskip("pypdfium2.raw")

pytestmark = pytest.mark.skipif(
    not native_pdf_available() and os.environ.get("CARBOTEK_TEST_PDF_FALLBACK") != "1",
    reason="needs the bundled PDF engine",
)

_EMU_PER_PT = 12700
_TOLERANCE = 0.1  # pt; both documents go through the same fonts and layout
_NAMESPACES = (
    'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
    'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
    'xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture"'
)


def _anchor(*, relative_from: str = "paragraph", wrap: str = "wrapNone", height: float = 20.0) -> str:
    width, height_emu = 40 * _EMU_PER_PT, int(height * _EMU_PER_PT)
    return (
        '<w:r><w:drawing><wp:anchor distT="0" distB="0" distL="0" distR="0" simplePos="0" '
        'relativeHeight="251658240" behindDoc="0" locked="0" layoutInCell="1" allowOverlap="1">'
        '<wp:simplePos x="0" y="0"/>'
        f'<wp:positionH relativeFrom="column"><wp:posOffset>{300 * _EMU_PER_PT}</wp:posOffset></wp:positionH>'
        f'<wp:positionV relativeFrom="{relative_from}"><wp:posOffset>0</wp:posOffset></wp:positionV>'
        f'<wp:extent cx="{width}" cy="{height_emu}"/><wp:effectExtent l="0" t="0" r="0" b="0"/><wp:{wrap}/>'
        '<wp:docPr id="1" name="Probe"/>'
        '<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">'
        '<pic:pic><pic:nvPicPr><pic:cNvPr id="1" name="Probe"/><pic:cNvPicPr/></pic:nvPicPr>'
        '<pic:blipFill><a:blip r:embed="rIdProbe"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill>'
        f'<pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{width}" cy="{height_emu}"/></a:xfrm>'
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr></pic:pic>'
        "</a:graphicData></a:graphic></wp:anchor></w:drawing></w:r>"
    )


def _paragraph(text: str, *, before: int = 0, after: int = 0, style: str = "", runs: str = "") -> str:
    """`before`/`after` in twentieths of a point, as in `<w:spacing>`."""
    properties = f'<w:pStyle w:val="{style}"/>' if style else ""
    properties += f'<w:spacing w:before="{before}" w:after="{after}"/>'
    return f"<w:p><w:pPr>{properties}</w:pPr>{runs}<w:r><w:t>{text}</w:t></w:r></w:p>"


def _cell(content: str) -> str:
    return (
        '<w:tbl><w:tblPr><w:tblW w:w="6000" w:type="dxa"/><w:tblLayout w:type="fixed"/></w:tblPr>'
        '<w:tblGrid><w:gridCol w:w="6000"/></w:tblGrid>'
        f'<w:tr><w:tc><w:tcPr><w:tcW w:w="6000" w:type="dxa"/></w:tcPr>{content}</w:tc></w:tr></w:tbl>'
    )


def _docx(body: str) -> bytes:
    """A4 page; Normal is 12 pt without spacing; `Listed` adds contextual spacing."""
    image = BytesIO()
    Image.new("RGB", (40, 20), (220, 30, 30)).save(image, format="PNG")
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
            '<Relationship Id="rIdProbe" Target="media/probe.png" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"/>'
            "</Relationships>"
        ),
        "word/document.xml": (
            f"<w:document {_NAMESPACES}><w:body>{body}"
            '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
            '<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/></w:sectPr>'
            "</w:body></w:document>"
        ),
        # Word 2013+ layout; without it Word measures `tblInd` to the cell text, as Word 2010 did.
        "word/settings.xml": (
            f'<w:settings {_NAMESPACES}><w:compat><w:compatSetting w:name="compatibilityMode" '
            'w:uri="http://schemas.microsoft.com/office/word" w:val="15"/></w:compat></w:settings>'
        ),
        "word/styles.xml": (
            f"<w:styles {_NAMESPACES}><w:docDefaults>"
            '<w:rPrDefault><w:rPr><w:sz w:val="24"/></w:rPr></w:rPrDefault>'
            '<w:pPrDefault><w:pPr><w:spacing w:after="0" w:line="240" w:lineRule="auto"/></w:pPr></w:pPrDefault>'
            "</w:docDefaults>"
            '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>'
            '<w:style w:type="paragraph" w:styleId="Listed"><w:name w:val="Listed"/><w:basedOn w:val="Normal"/>'
            "<w:pPr><w:contextualSpacing/></w:pPr></w:style>"
            "</w:styles>"
        ),
    }
    stream = BytesIO()
    with ZipFile(stream, "w", ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
        archive.writestr("word/media/probe.png", image.getvalue())
    return stream.getvalue()


def _geometry(body: str) -> tuple[float, float, float]:
    """(picture top, picture bottom, top of the "ANCHOR" text) in points from the page top."""
    document = pypdfium2.PdfDocument(convert_docx_to_pdf(_docx(body)))
    assert len(document) == 1
    page = document[0]
    height = page.get_height()
    pictures = [obj.get_bounds() for obj in page.get_objects(filter=[pdfium_raw.FPDF_PAGEOBJ_IMAGE], max_depth=5)]
    assert len(pictures) == 1
    _left, bottom, _right, top = pictures[0]
    text = page.get_textpage()
    index = text.get_text_range().find("ANCHOR")
    assert index >= 0
    return height - top, height - bottom, height - text.get_charbox(index)[3]


def _anchored(
    *,
    before: int = 0,
    after_above: int = 0,
    cell: bool = False,
    relative_from: str = "paragraph",
    wrap: str = "wrapNone",
    height: float = 20.0,
) -> str:
    """The paragraph "TOP", then "ANCHOR" carrying the picture: in the body, or first in a table cell."""
    picture = _anchor(relative_from=relative_from, wrap=wrap, height=height)
    anchored = _paragraph("ANCHOR", before=before, runs=picture)
    return _paragraph("TOP", after=after_above) + (_cell(anchored) if cell else anchored)


@pytest.mark.parametrize("relative_from", ["paragraph", "line"])
@pytest.mark.parametrize("cell", [False, True], ids=["body", "cell"])
def test_space_before_moves_the_text_not_the_picture(cell: bool, relative_from: str) -> None:
    # Word: with 30 pt before, the picture stays at the paragraph's top edge and the text is 30 pt lower.
    # A `line` offset on the first line is measured from the same edge.
    picture, _, text = _geometry(_anchored(cell=cell, relative_from=relative_from))
    spaced_picture, _, spaced_text = _geometry(_anchored(before=600, cell=cell, relative_from=relative_from))
    assert spaced_picture == pytest.approx(picture, abs=_TOLERANCE)
    assert spaced_text - text == pytest.approx(30, abs=_TOLERANCE)


@pytest.mark.parametrize("cell", [False, True], ids=["body", "cell"])
def test_top_and_bottom_picture_keeps_the_space_before_below_it(cell: bool) -> None:
    # Word: the 60 pt band starts at the top edge; the text follows it after its own 30 pt space before.
    top, bottom, text = _geometry(_anchored(cell=cell, wrap="wrapTopAndBottom", height=60))
    spaced_top, spaced_bottom, spaced_text = _geometry(
        _anchored(before=600, cell=cell, wrap="wrapTopAndBottom", height=60)
    )
    assert bottom - top == pytest.approx(60, abs=_TOLERANCE)
    assert spaced_top == pytest.approx(top, abs=_TOLERANCE)
    assert spaced_bottom == pytest.approx(bottom, abs=_TOLERANCE)
    assert spaced_text - text == pytest.approx(30, abs=_TOLERANCE)


@pytest.mark.parametrize("wrap", ["wrapNone", "wrapTopAndBottom"])
def test_larger_space_after_above_absorbs_the_space_before(wrap: str) -> None:
    # Word: 24 pt after the previous paragraph and 12 pt before this one place the text 24 pt lower,
    # and the paragraph's top edge — the picture's origin — is the top of that text.
    without = _geometry(_anchored(after_above=480, wrap=wrap))
    absorbed = _geometry(_anchored(after_above=480, before=240, wrap=wrap))
    assert absorbed == pytest.approx(without, abs=_TOLERANCE)


@pytest.mark.parametrize("cell", [False, True], ids=["body", "cell"])
def test_contextual_spacing_keeps_the_picture_on_the_text(cell: bool) -> None:
    # Word drops the 18 pt before between paragraphs of one style with contextual spacing,
    # so the paragraph's top edge is the top of its text, as without any spacing.
    def body(before: int, style: str) -> str:
        content = _paragraph("FIRST", before=before, style=style) + _paragraph(
            "ANCHOR", before=before, style=style, runs=_anchor()
        )
        return _paragraph("TOP") + (_cell(content) if cell else content)

    plain_top, _, plain_text = _geometry(body(0, ""))
    listed_top, _, listed_text = _geometry(body(360, "Listed"))
    assert listed_top - listed_text == pytest.approx(plain_top - plain_text, abs=_TOLERANCE)
