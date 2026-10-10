"""The narrow native decoder must keep the Python data-URI path available."""

import base64

import pytest

pytest.importorskip("carbotek._native")

from carbotek import _native_adapter
from carbotek._native import decode_data_uri
from carbotek.features.image import image_payload


@pytest.mark.parametrize("mime", ["png", "jpeg", "gif", "bmp", "svg+xml"])
def test_canonical_data_uri_matches_python(mime: str, monkeypatch: pytest.MonkeyPatch) -> None:
    raw = bytes(range(256)) * 32
    uri = f"data:image/{mime};base64," + base64.b64encode(raw).decode("ascii")
    assert decode_data_uri(uri) == raw
    native = image_payload(uri)
    monkeypatch.setattr(_native_adapter, "_native", None)
    assert native == image_payload(uri) == (raw, f"image/{mime}")


def test_noncanonical_and_malformed_data_uri_uses_python_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    values = [
        "data:IMAGE/PNG;base64,YQ==",
        "data:image/png;base64,YQ=",
        "data:image/png;base64,YQ==!",
        "data:image/png;base64,YR==",
        "data:image/png;base64,",
    ]
    native = [image_payload(uri) for uri in values]
    for uri in values[:-1]:
        assert decode_data_uri(uri) is None
    assert decode_data_uri(values[-1]) == b""
    monkeypatch.setattr(_native_adapter, "_native", None)
    assert native == [image_payload(uri) for uri in values]
