"""Build anonymous signature-table probes: normal flow and page-boundary flow."""

import struct
import zlib
from pathlib import Path
from zipfile import ZipFile, ZipInfo

ROOT = Path(__file__).resolve().parents[1]


def write(z, name, data):
    info = ZipInfo(name, (2026, 1, 1, 0, 0, 0))
    info.external_attr = 0o600 << 16
    z.writestr(info, data)


def png():
    def chunk(kind, payload):
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload))

    rows = bytearray()
    for y in range(100):
        rows.append(0)
        for x in range(100):
            ink = abs(((x - 50) ** 2 + (y - 50) ** 2) ** 0.5 - 35) < 2 or (15 < x < 85 and abs(y - (35 + x // 8)) < 2)
            rows.extend((20, 50, 180, 255 if ink else 0))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 100, 100, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


DRAWING = """<w:r><w:drawing><wp:anchor distT="0" distB="0" distL="0" distR="0" simplePos="0" relativeHeight="1" behindDoc="1" locked="0" layoutInCell="1" allowOverlap="1">
<wp:simplePos x="0" y="0"/><wp:positionH relativeFrom="column"><wp:posOffset>0</wp:posOffset></wp:positionH><wp:positionV relativeFrom="paragraph"><wp:posOffset>0</wp:posOffset></wp:positionV>
<wp:extent cx="1270000" cy="1270000"/><wp:effectExtent l="0" t="0" r="0" b="0"/><wp:wrapNone/><wp:docPr id="1" name="Anonymous seal"/>
<wp:cNvGraphicFramePr/><a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture"><pic:pic>
<pic:nvPicPr><pic:cNvPr id="1" name="seal.png"/><pic:cNvPicPr/></pic:nvPicPr><pic:blipFill><a:blip r:embed="rIdImage"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill>
<pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="1270000" cy="1270000"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr>
</pic:pic></a:graphicData></a:graphic></wp:anchor></w:drawing></w:r>"""


def paragraph(text, drawing=""):
    return f'<w:p><w:pPr><w:spacing w:before="0" w:after="0" w:line="280" w:lineRule="exact"/></w:pPr>{drawing}<w:r><w:t>{text}</w:t></w:r></w:p>'


for boundary, body_anchor in ((False, False), (True, False), (False, True), (True, True), (True, "empty")):
    padding = "".join(paragraph(f"Padding {i}") for i in range(18)) if boundary else ""
    table = (
        '<w:tbl><w:tblPr><w:tblW w:w="5000" w:type="dxa"/><w:tblLayout w:type="fixed"/></w:tblPr><w:tblGrid><w:gridCol w:w="5000"/></w:tblGrid><w:tr><w:tc><w:tcPr><w:tcW w:w="5000" w:type="dxa"/></w:tcPr>'
        + paragraph("Director")
        + paragraph("ANCHOR", DRAWING)
        + paragraph("SIGNATURE LINE")
        + paragraph("MP")
        + "</w:tc></w:tr></w:tbl>"
    )
    if body_anchor:
        padding += paragraph("" if body_anchor == "empty" else "BODY ANCHOR", DRAWING)
        table = table.replace('id="1"', 'id="2"').replace("Anonymous seal", "Anonymous second seal")
    doc = (
        """<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture"><w:body>"""
        + padding
        + table
        + paragraph("AFTER TABLE")
        + '<w:sectPr><w:pgSz w:w="8000" w:h="8000"/><w:pgMar w:top="720" w:bottom="720" w:left="720" w:right="720" w:header="0" w:footer="0"/></w:sectPr></w:body></w:document>'
    )
    name = "wrap-none-signature-boundary.docx" if boundary else "wrap-none-signature.docx"
    if body_anchor:
        name = name.replace("signature", "body-and-cell-signature")
    if body_anchor == "empty":
        name = name.replace(".docx", "-empty.docx")
    with ZipFile(ROOT / "test-files" / name, "w") as z:
        write(
            z,
            "[Content_Types].xml",
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="png" ContentType="image/png"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>',
        )
        write(
            z,
            "_rels/.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rIdDoc" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>',
        )
        write(
            z,
            "word/_rels/document.xml.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rIdImage" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/seal.png"/></Relationships>',
        )
        write(z, "word/document.xml", doc)
        write(z, "word/media/seal.png", png())
    print(name)
