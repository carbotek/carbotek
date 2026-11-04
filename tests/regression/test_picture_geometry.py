"""Synthetic pictures: fitting uses the crop, PDF preserves per-occurrence transforms."""

import math
from base64 import b64encode
from io import BytesIO
from zipfile import ZipFile

import pytest
from lxml import etree
from PIL import Image, ImageChops

from carbotek import Engine, convert_docx_to_pdf
from carbotek._native_adapter import native_pdf_available

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
WP = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
PIC = "http://schemas.openxmlformats.org/drawingml/2006/picture"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
REL = "http://schemas.openxmlformats.org/package/2006/relationships"


def _edit_document(document: bytes, edit) -> bytes:
    output = BytesIO()
    with ZipFile(BytesIO(document)) as source, ZipFile(output, "w") as target:
        for item in source.infolist():
            content = source.read(item)
            if item.filename == "word/document.xml":
                root = etree.fromstring(content)
                edit(root)
                content = etree.tostring(root)
            target.writestr(item, content)
    return output.getvalue()


def _color_box(image: Image.Image) -> tuple[int, int, int, int] | None:
    red, green, blue = image.convert("RGB").split()
    mask = ImageChops.multiply(
        green.point(lambda v: 255 if v < 80 else 0),
        ImageChops.lighter(red, blue).point(lambda v: 255 if v > 180 else 0),
    )
    return mask.getbbox()


def _pages(pdf: bytes) -> list[Image.Image]:
    import pypdfium2

    with pypdfium2.PdfDocument(pdf) as document:
        return [page.render(scale=2).to_pil().convert("RGB") for page in document]


def _png() -> bytes:
    image = Image.new("RGBA", (200, 100), "red")
    image.paste("blue", (100, 0, 200, 100))
    image.paste("blue", (0, 50, 100, 100))
    image.paste("red", (100, 50, 200, 100))
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _document(
    *, transform: str = "", crop: str = "", inline: bool = False, fit: str = "fill", cell: bool = False
) -> bytes:
    marker = "{d.image" + (f":imageFit('{fit}')" if fit else "") + "}"
    picture = (
        f'<wp:extent cx="914400" cy="457200"/><wp:docPr id="1" name="Probe" descr="{marker}"/>'
        f'<a:graphic><a:graphicData uri="{PIC}"><pic:pic><pic:nvPicPr><pic:cNvPr id="1" name="Probe"/>'
        '<pic:cNvPicPr/></pic:nvPicPr><pic:blipFill><a:blip r:embed="rIdImage"/>'
        f"{crop}<a:stretch><a:fillRect/></a:stretch></pic:blipFill><pic:spPr><a:xfrm {transform}>"
        '<a:off x="0" y="0"/><a:ext cx="914400" cy="457200"/></a:xfrm>'
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr></pic:pic></a:graphicData></a:graphic>'
    )
    if inline:
        picture = f"<wp:inline>{picture}</wp:inline>"
    else:
        picture = (
            '<wp:anchor simplePos="0" relativeHeight="251658240" behindDoc="0" locked="0" layoutInCell="1" allowOverlap="1">'
            '<wp:simplePos x="0" y="0"/><wp:positionH relativeFrom="column"><wp:posOffset>914400</wp:posOffset></wp:positionH>'
            '<wp:positionV relativeFrom="paragraph"><wp:posOffset>914400</wp:posOffset></wp:positionV>'
            + picture.replace("<wp:docPr", "<wp:wrapNone/><wp:docPr")
            + "</wp:anchor>"
        )
    content = f"<w:p><w:r><w:drawing>{picture}</w:drawing></w:r></w:p><w:p><w:r><w:t>AFTER</w:t></w:r></w:p>"
    if cell:
        content = (
            '<w:tbl><w:tblPr><w:tblW w:w="5000" w:type="pct"/></w:tblPr><w:tblGrid>'
            '<w:gridCol w:w="9360"/></w:tblGrid><w:tr><w:tc><w:tcPr><w:tcW w:w="9360" w:type="dxa"/></w:tcPr>'
            + content
            + "</w:tc></w:tr></w:tbl>"
        )
    output = BytesIO()
    with ZipFile(output, "w") as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="png" ContentType="image/png"/>'
            '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>',
        )
        archive.writestr(
            "_rels/.rels",
            f'<Relationships xmlns="{REL}"><Relationship Id="rIdDoc" Target="word/document.xml" Type="{R}/officeDocument"/></Relationships>',
        )
        archive.writestr(
            "word/_rels/document.xml.rels",
            f'<Relationships xmlns="{REL}"><Relationship Id="rIdImage" Target="media/probe.png" Type="{R}/image"/></Relationships>',
        )
        archive.writestr(
            "word/document.xml",
            f'<w:document xmlns:w="{W}" xmlns:wp="{WP}" xmlns:a="{A}" xmlns:pic="{PIC}" xmlns:r="{R}"><w:body>{content}<w:sectPr><w:pgSz w:w="12240" w:h="15840"/><w:pgMar w:top="1440" w:left="1440" w:right="1440" w:bottom="1440"/></w:sectPr></w:body></w:document>',
        )
        archive.writestr("word/media/probe.png", _png())
    return output.getvalue()


@pytest.mark.parametrize("fit", ["", "fillWidth", "contain", "fill"])
@pytest.mark.parametrize(
    "crop,expected",
    [('<a:srcRect l="25000" r="25000"/>', (914400, 914400)), ('<a:srcRect l="-50%" r="-50%"/>', (914400, 228600))],
)
def test_fit_uses_visible_source_aspect(crop: str, expected: tuple[int, int], fit: str) -> None:
    rendered = Engine().render(
        _document(crop=crop, fit=fit), {"image": "data:image/png;base64," + b64encode(_png()).decode()}
    )
    if fit == "contain" and expected[1] > 457200:
        expected = (457200, 457200)
    elif fit == "fill":
        expected = (914400, 457200)
    with ZipFile(BytesIO(rendered)) as archive:
        root = etree.fromstring(archive.read("word/document.xml"))
    for extent in root.iter(f"{{{WP}}}extent", f"{{{A}}}ext"):
        assert (int(extent.get("cx")), int(extent.get("cy"))) == expected
    assert root.find(f".//{{{A}}}srcRect").attrib == etree.fromstring(crop.replace("a:", "")).attrib


def test_missing_picture_is_a_valid_transparent_png() -> None:
    rendered = Engine().render(_document(inline=True), {"image": ""})
    with ZipFile(BytesIO(rendered)) as archive:
        raw = archive.read("word/media/CarbotekImage0.png")
    with Image.open(BytesIO(raw)) as image:
        image.load()
        assert image.convert("RGBA").getpixel((0, 0)) == (0, 0, 0, 0)


@pytest.mark.parametrize("data", [{"image": ""}, {"image": None}, {}])
def test_missing_nonwrapping_picture_has_no_invisible_overlay(data: dict) -> None:
    rendered = Engine().render(_document(), data)
    with ZipFile(BytesIO(rendered)) as archive:
        root = etree.fromstring(archive.read("word/document.xml"))
    assert root.find(f".//{{{WP}}}anchor") is None
    assert root.find(f".//{{{W}}}drawing") is None
    assert root.find(f".//{{{W}}}t").text == "AFTER"


@pytest.mark.skipif(not native_pdf_available(), reason="needs the bundled PDF engine")
@pytest.mark.parametrize("inline,cell", [(False, False), (True, False), (False, True)])
@pytest.mark.parametrize(
    "angle,flip_h,flip_v",
    [
        (90, False, False),
        (-90, False, False),
        (30, False, False),
        (0, True, False),
        (0, False, True),
        (90, True, False),
        (30, True, True),
    ],
)
def test_pdf_picture_transform(angle: int, flip_h: bool, flip_v: bool, inline: bool, cell: bool) -> None:
    original = convert_docx_to_pdf(_document(inline=inline, cell=cell))
    document = convert_docx_to_pdf(
        _document(
            transform=f'rot="{angle * 60000}" flipH="{int(flip_h)}" flipV="{int(flip_v)}"', inline=inline, cell=cell
        )
    )

    def picture(pdf: bytes) -> tuple[Image.Image, tuple[int, int, int, int]]:
        (image,) = _pages(pdf)
        box = _color_box(image)
        assert box is not None
        return image, box

    _, base = picture(original)
    image, box = picture(document)
    cx, cy = (base[0] + base[2]) / 2, (base[1] + base[3]) / 2
    assert abs((box[0] + box[2]) / 2 - cx) <= 2
    assert abs((box[1] + box[3]) / 2 - cy) <= 2
    radians = math.radians(angle)
    expected_width = abs(144 * math.cos(radians)) + abs(72 * math.sin(radians))
    expected_height = abs(144 * math.sin(radians)) + abs(72 * math.cos(radians))
    assert abs(box[2] - box[0] - expected_width) <= 3
    assert abs(box[3] - box[1] - expected_height) <= 3
    # A point in the source's left half follows the clockwise rotation and flip.
    local_x = 36 if flip_h else -36
    local_y = 18 if flip_v else -18
    px = round(cx + local_x * math.cos(radians) - local_y * math.sin(radians))
    py = round(cy + local_x * math.sin(radians) + local_y * math.cos(radians))
    red, green, blue = image.getpixel((px, py))
    assert red > 180 and green < 80 and blue < 80


@pytest.mark.skipif(not native_pdf_available(), reason="needs the bundled PDF engine")
@pytest.mark.parametrize(
    "horizontal,vertical", [("page", "page"), ("margin", "margin"), ("page", "paragraph"), ("column", "page")]
)
def test_page_axes_in_a_displaced_second_table_cell(horizontal: str, vertical: str) -> None:
    def displace(root) -> None:
        body = root.find(f"{{{W}}}body")
        padding = etree.fromstring(
            f'<w:p xmlns:w="{W}"><w:pPr><w:spacing w:line="2400" w:lineRule="exact"/></w:pPr><w:r><w:t>PADDING</w:t></w:r></w:p>'
        )
        body.insert(0, padding)
        table = body.find(f"{{{W}}}tbl")
        row = table.find(f"{{{W}}}tr")
        row.insert(
            0, etree.fromstring(f'<w:tc xmlns:w="{W}"><w:tcPr><w:tcW w:w="4680" w:type="dxa"/></w:tcPr><w:p/></w:tc>')
        )
        grid = table.find(f"{{{W}}}tblGrid")
        grid[0].set(f"{{{W}}}w", "4680")
        etree.SubElement(grid, f"{{{W}}}gridCol", {f"{{{W}}}w": "4680"})
        row[1].find(f"{{{W}}}tcPr/{{{W}}}tcW").set(f"{{{W}}}w", "4680")

    original = _edit_document(_document(cell=True), displace)
    baseline = _color_box(_pages(convert_docx_to_pdf(original))[0])
    assert baseline is not None

    def change(root) -> None:
        root.find(f".//{{{WP}}}positionH").set("relativeFrom", horizontal)
        root.find(f".//{{{WP}}}positionV").set("relativeFrom", vertical)

    (page,) = _pages(convert_docx_to_pdf(_edit_document(original, change)))
    box = _color_box(page)
    assert box is not None
    expected_x = {"page": 144, "margin": 288, "column": baseline[0]}[horizontal]
    expected_y = {"page": 144, "margin": 288, "paragraph": baseline[1]}[vertical]
    assert abs(box[0] - expected_x) <= 2
    assert abs(box[1] - expected_y) <= 2


@pytest.mark.skipif(not native_pdf_available(), reason="needs the bundled PDF engine")
@pytest.mark.parametrize("mirrored", [False, True])
def test_page_anchors_in_repeated_table_headers(mirrored: bool) -> None:
    def repeat(root) -> None:
        table = root.find(f".//{{{W}}}tbl")
        header = table.find(f"{{{W}}}tr")
        props = etree.Element(f"{{{W}}}trPr")
        etree.SubElement(props, f"{{{W}}}tblHeader")
        header.insert(0, props)
        for _ in range(45):
            table.append(
                etree.fromstring(
                    f'<w:tr xmlns:w="{W}"><w:trPr><w:trHeight w:val="600" w:hRule="exact"/></w:trPr><w:tc><w:p><w:r><w:t>ROW</w:t></w:r></w:p></w:tc></w:tr>'
                )
            )
        for axis in ("H", "V"):
            position = root.find(f".//{{{WP}}}position{axis}")
            position.set("relativeFrom", "insideMargin" if mirrored else "page")
            position[0].text = "0" if mirrored else "1828800"
        if mirrored:
            etree.SubElement(root.find(f".//{{{W}}}sectPr"), f"{{{W}}}pgNumType", {f"{{{W}}}start": "2"})

    pages = _pages(convert_docx_to_pdf(_edit_document(_document(cell=True), repeat)))
    assert len(pages) >= 3
    for index, page in enumerate(pages):
        box = _color_box(page)
        assert box is not None, f"header picture missing on page {index + 1}"
        expected = ((1080, 1440) if index % 2 == 0 else (0, 0)) if mirrored else (288, 288)
        assert abs(box[0] - expected[0]) <= 2
        assert abs(box[1] - expected[1]) <= 2


@pytest.mark.skipif(not native_pdf_available(), reason="needs the bundled PDF engine")
@pytest.mark.parametrize("cell", [False, True])
def test_rotation_keeps_page_fit_based_on_the_layout_frame(cell: bool) -> None:
    def near_bottom(root) -> None:
        body = root.find(f"{{{W}}}body")
        body.insert(
            0,
            etree.fromstring(
                f'<w:p xmlns:w="{W}"><w:pPr><w:spacing w:line="12000" w:lineRule="exact"/></w:pPr><w:r><w:t>PADDING</w:t></w:r></w:p>'
            ),
        )
        root.find(f".//{{{WP}}}positionV/{{{WP}}}posOffset").text = "0"

    original = _edit_document(_document(cell=cell), near_bottom)
    original_pages = _pages(convert_docx_to_pdf(original))
    assert _color_box(original_pages[0]) is not None
    rotated = _edit_document(_document(cell=cell, transform='rot="5400000"'), near_bottom)
    pages = _pages(convert_docx_to_pdf(rotated))
    assert len(pages) == len(original_pages)
    assert _color_box(pages[0]) is not None


@pytest.mark.skipif(not native_pdf_available(), reason="needs the bundled PDF engine")
def test_shared_bitmap_keeps_per_picture_transforms() -> None:
    from copy import deepcopy

    def duplicate(root) -> None:
        paragraph = root.find(f".//{{{W}}}body/{{{W}}}p")
        second = deepcopy(paragraph[0])
        second.find(f".//{{{A}}}xfrm").attrib.clear()
        second.find(f".//{{{WP}}}positionH/{{{WP}}}posOffset").text = "2743200"
        paragraph.append(second)

    document = _edit_document(_document(transform='rot="5400000"'), duplicate)
    (page,) = _pages(convert_docx_to_pdf(document))
    first = _color_box(page.crop((0, 0, 500, page.height)))
    second = _color_box(page.crop((500, 0, page.width, page.height)))
    assert first is not None and second is not None
    assert (first[2] - first[0], first[3] - first[1]) == (72, 144)
    assert (second[2] - second[0], second[3] - second[1]) == (144, 72)


@pytest.mark.skipif(not native_pdf_available(), reason="needs the bundled PDF engine")
def test_crop_is_applied_before_rotation() -> None:
    (page,) = _pages(convert_docx_to_pdf(_document(crop='<a:srcRect l="50000"/>', transform='rot="5400000"')))
    box = _color_box(page)
    assert box is not None
    cx, cy = (box[0] + box[2]) // 2, (box[1] + box[3]) // 2
    red, green, blue = page.getpixel((cx + 18, cy - 36))
    assert blue > 180 and red < 80 and green < 80
    red, green, blue = page.getpixel((cx - 18, cy + 36))
    assert red > 180 and green < 80 and blue < 80
