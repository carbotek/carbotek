"""PDF preprocessing shares a ZIP scan and decodes only profiled PNGs."""

from io import BytesIO
from types import SimpleNamespace
from zipfile import ZipFile

import pytest
from PIL import Image, ImageCms

from carbotek import Engine, _native_adapter, render_pdf
from carbotek.errors import ConvertError, ValidationError
from carbotek.pdf import convert as conversion


@pytest.fixture(autouse=True)
def fake_converter_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests fake the dxpdf package: the bundled engine must not take precedence."""
    monkeypatch.setattr(_native_adapter, "_pdf", None)


def package(image: bytes) -> bytes:
    stream = BytesIO()
    with ZipFile(stream, "w") as archive:
        archive.writestr("word/media/image.png", image)
        archive.writestr("word/media/unsupported.emf", b"EMF")
        archive.writestr("word/document.xml", b"<document/>")
    return stream.getvalue()


def converter(monkeypatch: pytest.MonkeyPatch) -> list[bytes]:
    documents: list[bytes] = []

    def convert(document: bytes, *, image_dpi: int) -> bytes:
        documents.append(document)
        assert image_dpi == 220
        return b"%PDF-test"

    monkeypatch.setattr(conversion, "import_module", lambda _: SimpleNamespace(convert=convert))
    return documents


def test_plain_png_is_not_decoded_and_source_zip_is_opened_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    png = BytesIO()
    Image.new("RGBA", (2, 2), (100, 50, 20, 80)).save(png, format="PNG")
    document = package(png.getvalue())
    documents = converter(monkeypatch)
    opens = 0

    def zip_file(*args: object, **kwargs: object) -> ZipFile:
        nonlocal opens
        opens += 1
        return ZipFile(*args, **kwargs)

    def unexpected_decode(*args: object, **kwargs: object) -> None:
        raise AssertionError("Unprofiled PNG must not be decoded")

    monkeypatch.setattr(conversion, "ZipFile", zip_file)
    monkeypatch.setattr(Image, "open", unexpected_decode)
    assert conversion.convert_docx_to_pdf(document) == b"%PDF-test"
    assert documents == [document]
    assert opens == 1
    assert "unsupported.emf" in caplog.text


def test_profile_normalization_preserves_alpha_and_other_zip_parts(monkeypatch: pytest.MonkeyPatch) -> None:
    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    png = BytesIO()
    Image.new("RGBA", (2, 2), (100, 50, 20, 80)).save(png, format="PNG", icc_profile=profile)
    document = package(png.getvalue())
    documents = converter(monkeypatch)
    assert conversion.convert_docx_to_pdf(document) == b"%PDF-test"
    with ZipFile(BytesIO(document)) as source, ZipFile(BytesIO(documents[0])) as output:
        assert source.namelist() == output.namelist()
        for name in ("word/document.xml", "word/media/unsupported.emf"):
            assert source.read(name) == output.read(name)
        with Image.open(BytesIO(output.read("word/media/image.png"))) as image:
            assert "icc_profile" not in image.info
            assert image.getpixel((0, 0)) == (100, 50, 20, 80)


def test_pdf_conversion_document_type_is_validated() -> None:
    with pytest.raises(ValidationError, match="document must be bytes"):
        conversion.convert_docx_to_pdf("document")  # type: ignore[arg-type]


@pytest.mark.parametrize("dpi", [0, -1, 2401, True, 1.5, "220", None])
@pytest.mark.parametrize("entry", ["engine", "module", "converter"])
def test_pdf_dpi_has_public_validation_error(dpi: object, entry: str) -> None:
    with pytest.raises(ValidationError, match="image_dpi must be an integer between 1 and 2400") as raised:
        if entry == "engine":
            Engine().render_pdf(b"invalid zip", {}, image_dpi=dpi)  # type: ignore[arg-type]
        elif entry == "module":
            render_pdf(b"invalid zip", {}, image_dpi=dpi)  # type: ignore[arg-type]
        else:
            conversion.convert_docx_to_pdf(b"invalid zip", image_dpi=dpi)  # type: ignore[arg-type]
    assert raised.value.code == "VALIDATION_ERROR"


@pytest.mark.parametrize("dpi", [1, 2400])
def test_pdf_dpi_boundaries_reach_converter(monkeypatch: pytest.MonkeyPatch, dpi: int) -> None:
    calls: list[tuple[bytes, int]] = []

    def convert(document: bytes, *, image_dpi: int) -> bytes:
        calls.append((document, image_dpi))
        return b"%PDF-test"

    monkeypatch.setattr(conversion, "import_module", lambda _: SimpleNamespace(convert=convert))
    assert conversion.convert_docx_to_pdf(b"invalid zip", image_dpi=dpi) == b"%PDF-test"
    assert calls == [(b"invalid zip", dpi)]


def test_converter_failure_retains_typed_cause(monkeypatch: pytest.MonkeyPatch) -> None:
    failure = RuntimeError("converter failed")

    def convert(document: bytes, *, image_dpi: int) -> bytes:
        raise failure

    monkeypatch.setattr(conversion, "import_module", lambda _: SimpleNamespace(convert=convert))
    with pytest.raises(ConvertError, match="converter failed") as raised:
        conversion.convert_docx_to_pdf(b"invalid zip")
    assert raised.value.code == "CONVERTER_ERROR"
    assert raised.value.__cause__ is failure


@pytest.mark.parametrize("result", [None, "%PDF-string"])
def test_converter_non_bytes_result_has_library_error(monkeypatch: pytest.MonkeyPatch, result: object) -> None:
    monkeypatch.setattr(conversion, "import_module", lambda _: SimpleNamespace(convert=lambda *args, **kwargs: result))
    with pytest.raises(ConvertError, match="dxpdf returned an invalid conversion result") as raised:
        conversion.convert_docx_to_pdf(b"invalid zip")
    assert raised.value.code == "CONVERTER_ERROR"
