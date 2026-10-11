"""Choice of the PDF engine: the bundled one first, then the dxpdf package, else a clear error."""

import pytest
from test_render_session import document, paragraph

from carbotek import _native_adapter
from carbotek.errors import ConvertError
from carbotek.pdf import convert


def test_without_any_engine_the_error_says_what_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_native_adapter, "_pdf", None)

    def missing(name: str) -> object:
        raise ImportError(name)

    monkeypatch.setattr(convert, "import_module", missing)
    with pytest.raises(ConvertError, match="bundled PDF engine or the dxpdf package"):
        convert.convert_docx_to_pdf(document(paragraph("x")))


def test_bundled_engine_is_preferred(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[float] = []

    class Engine:
        def convert_pdf(self, docx: bytes, image_dpi: float) -> bytes:
            calls.append(image_dpi)
            return b"%PDF-bundled"

    monkeypatch.setattr(_native_adapter, "_pdf", Engine())
    monkeypatch.setattr(convert, "import_module", lambda name: pytest.fail(f"imported {name}"))
    assert convert.convert_docx_to_pdf(document(paragraph("x")), image_dpi=150) == b"%PDF-bundled"
    assert calls == [150.0]
