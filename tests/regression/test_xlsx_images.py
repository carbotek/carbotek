"""Public XLSX image rendering covers shared MIME types and placeholder behavior."""

from base64 import b64encode
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import pytest
from lxml import etree
from PIL import Image

from carbotek import Engine
from carbotek.xlsx.images import _DEFAULT_EMF

_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_SHEET = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_DRAW = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
_ART = "http://schemas.openxmlformats.org/drawingml/2006/main"


def workbook(*, blip: bool = True, marker: str = "d.picture") -> bytes:
    parts = {
        "[Content_Types].xml": '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="xml" ContentType="application/xml"/><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="png" ContentType="image/png"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/></Types>',
        "_rels/.rels": f'<Relationships xmlns="{_PKG_REL}"><Relationship Id="rId1" Target="xl/workbook.xml" Type="{_REL}/officeDocument"/></Relationships>',
        "xl/workbook.xml": f'<workbook xmlns="{_SHEET}" xmlns:r="{_REL}"><sheets><sheet name="Sheet1" sheetId="1" r:id="rIdSheet"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels": f'<Relationships xmlns="{_PKG_REL}"><Relationship Id="rIdSheet" Target="worksheets/sheet1.xml" Type="{_REL}/worksheet"/></Relationships>',
        "xl/worksheets/sheet1.xml": f'<worksheet xmlns="{_SHEET}" xmlns:r="{_REL}"><sheetData/><drawing r:id="rIdDrawing"/></worksheet>',
        "xl/worksheets/_rels/sheet1.xml.rels": f'<Relationships xmlns="{_PKG_REL}"><Relationship Id="rIdDrawing" Target="../drawings/drawing1.xml" Type="{_REL}/drawing"/></Relationships>',
        "xl/drawings/drawing1.xml": f'<xdr:wsDr xmlns:xdr="{_DRAW}" xmlns:a="{_ART}" xmlns:r="{_REL}"><xdr:twoCellAnchor><xdr:pic><xdr:nvPicPr><xdr:cNvPr id="1" name="Picture" descr="{{{marker}}}"/></xdr:nvPicPr><xdr:blipFill>'
        + ('<a:blip r:embed="rIdTemplate"/>' if blip else "")
        + "</xdr:blipFill></xdr:pic></xdr:twoCellAnchor></xdr:wsDr>",
        "xl/drawings/_rels/drawing1.xml.rels": f'<Relationships xmlns="{_PKG_REL}"><Relationship Id="rIdTemplate" Target="../media/template.png" Type="{_REL}/image"/></Relationships>',
    }
    output = BytesIO()
    with ZipFile(output, "w") as archive:
        for name, content in parts.items():
            archive.writestr(name, content)
        archive.writestr("xl/media/template.png", b"placeholder")
    return output.getvalue()


@pytest.fixture(params=[False, True], ids=["native", "python"])
def engine(request: pytest.FixtureRequest) -> Engine:
    if request.param:
        request.getfixturevalue("python_only")
    return Engine()


@pytest.mark.parametrize(
    "mime,format_name,extension,content_type",
    [
        ("image/webp", "WEBP", "webp", "image/webp"),
        ("image/x-emf", "EMF", "emf", "image/x-emf"),
        ("image/x-png", "PNG", "png", "image/png"),
        ("image/pjpeg", "JPEG", "jpeg", "image/jpeg"),
    ],
)
def test_xlsx_shared_image_mime_and_formatter_are_preserved(
    engine: Engine, mime: str, format_name: str, extension: str, content_type: str
) -> None:
    if format_name == "EMF":
        raw = _DEFAULT_EMF.replace("Inkscape".encode("utf-16-le"), "Testscap".encode("utf-16-le"), 1)
        assert raw != _DEFAULT_EMF  # distinguish a preserved EMF from the fallback placeholder
    else:
        stream = BytesIO()
        Image.new("RGB", (12, 6), "red").save(stream, format=format_name)
        raw = stream.getvalue()
    source = f"data:{mime};base64," + b64encode(raw).decode("ascii")
    with ZipFile(
        BytesIO(engine.render(workbook(marker="d.picture:prepend('data:')"), {"picture": source.removeprefix("data:")}))
    ) as archive:
        media = f"xl/media/CarbotekImage0.{extension}"
        assert archive.read(media) == raw
        drawing = etree.fromstring(archive.read("xl/drawings/drawing1.xml"))
        blip = next(drawing.iter(f"{{{_ART}}}blip"))
        assert blip.get(f"{{{_REL}}}embed") == "rIdCarbotek0"
        assert "{d.picture" not in archive.read("xl/drawings/drawing1.xml").decode()
        relationships = etree.fromstring(archive.read("xl/drawings/_rels/drawing1.xml.rels"))
        relation = next(node for node in relationships if node.get("Id") == "rIdCarbotek0")
        assert relation.get("Target") == f"../media/CarbotekImage0.{extension}"
        types = etree.fromstring(archive.read("[Content_Types].xml"))
        assert any(node.get("Extension") == extension and node.get("ContentType") == content_type for node in types)


@pytest.mark.parametrize("source", ["data:image/png;base64,", "data:image/unknown;base64,QQ==", ""])
def test_xlsx_invalid_image_uses_placeholder(engine: Engine, source: str) -> None:
    with ZipFile(BytesIO(engine.render(workbook(), {"picture": source}))) as archive:
        assert archive.read("xl/media/CarbotekImage0.emf") == _DEFAULT_EMF


def test_xlsx_picture_without_blip_does_not_fetch_or_add_media(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    from carbotek.features import image

    def unexpected_fetch(*args: object, **kwargs: object) -> None:
        raise AssertionError("No image consumer means no fetch")

    monkeypatch.setattr(image, "fetch_image", unexpected_fetch)
    with ZipFile(BytesIO(engine.render(workbook(blip=False), {"picture": "https://example.com/image.png"}))) as archive:
        assert not any("CarbotekImage" in name for name in archive.namelist())
        assert b"rIdCarbotek" not in archive.read("xl/drawings/_rels/drawing1.xml.rels")


@pytest.mark.parametrize("extension,format_name", [("webp", "WEBP"), ("emf", "EMF")])
def test_xlsx_local_image_uses_shared_mime_table(
    engine: Engine, tmp_path: Path, extension: str, format_name: str
) -> None:
    from carbotek.features.fetch import ImageSourceConfig

    if format_name == "EMF":
        raw = _DEFAULT_EMF.replace("Inkscape".encode("utf-16-le"), "Testscap".encode("utf-16-le"), 1)
        assert raw != _DEFAULT_EMF  # distinguish a preserved EMF from the fallback placeholder
    else:
        stream = BytesIO()
        Image.new("RGB", (12, 6), "red").save(stream, format=format_name)
        raw = stream.getvalue()
    source = tmp_path / ("picture." + extension)
    source.write_bytes(raw)
    engine = Engine(image_sources=ImageSourceConfig(file_root=str(tmp_path)))
    with ZipFile(BytesIO(engine.render(workbook(), {"picture": source.as_uri()}))) as archive:
        assert archive.read(f"xl/media/CarbotekImage0.{extension}") == raw


@pytest.mark.parametrize(
    "mime,format_name,extension",
    [
        ("image/webp", "WEBP", "webp"),
        ("image/x-emf", "EMF", "emf"),
        ("image/x-png", "PNG", "png"),
        ("image/pjpeg", "JPEG", "jpeg"),
    ],
)
def test_xlsx_remote_image_normalizes_response_mime(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, mime: str, format_name: str, extension: str
) -> None:
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from threading import Thread

    from carbotek.features import fetch
    from carbotek.features.fetch import ImageSourceConfig

    if format_name == "EMF":
        raw = _DEFAULT_EMF.replace("Inkscape".encode("utf-16-le"), "Testscap".encode("utf-16-le"), 1)
        assert raw != _DEFAULT_EMF  # distinguish a preserved EMF from the fallback placeholder
    else:
        stream = BytesIO()
        Image.new("RGB", (12, 6), "red").save(stream, format=format_name)
        raw = stream.getvalue()
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            requests.append(self.path)
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    monkeypatch.setattr(fetch, "_resolve_public", lambda _host, _port: "127.0.0.1")
    engine = Engine(image_sources=ImageSourceConfig(allowlist=("localhost",)))
    try:
        url = f"http://localhost:{server.server_port}/picture"
        with ZipFile(BytesIO(engine.render(workbook(), {"picture": url}))) as archive:
            assert archive.read(f"xl/media/CarbotekImage0.{extension}") == raw
        assert requests == ["/picture"]
    finally:
        server.shutdown()
        thread.join()
        server.server_close()
