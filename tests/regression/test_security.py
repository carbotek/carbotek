"""Regression probes for the security and render-state review findings."""

from pathlib import Path

import pytest
from lxml import etree

from carbotek.docx.dialect import SHEET
from carbotek.docx.renderer import SKIPPED_COPIES, DocxTextRenderer, _texts_and_copies
from carbotek.errors import CorruptedTemplateError, MarkerResolutionError
from carbotek.features.image import _file_payload, image_payload, rasterize_safe_svg
from carbotek.options import RenderOptions


@pytest.mark.parametrize("size", ["100000", "1e100", "-1"])
def test_svg_allocation_is_rejected(size: str) -> None:
    with pytest.raises(MarkerResolutionError):
        rasterize_safe_svg(f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}"/>'.encode(), 220)


@pytest.mark.parametrize(
    "target", ["C:/secret/scan.png", "file:///tmp/scan.png", "//host/share/scan.png", "https://example.com/a.png"]
)
def test_svg_external_image_is_rejected(target: str) -> None:
    with pytest.raises(MarkerResolutionError):
        rasterize_safe_svg(
            f'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><image href="{target}"/></svg>'.encode(),
            220,
        )


@pytest.mark.parametrize(
    "url", ["file:////host/share/a.png", "file:///%5Chost%5Cshare/a.png", "file:///%2Fhost/share/a.png"]
)
def test_network_file_uri_never_touches_filesystem(url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> Path:
        pytest.fail("network file URI reached filesystem resolution")

    monkeypatch.setattr(Path, "resolve", forbidden)
    assert _file_payload(url, "C:/images") is None


@pytest.mark.parametrize("kind", ["absolute", "traversal", "encoded-traversal", "sibling-prefix", "relative"])
def test_file_outside_root_never_reaches_resolution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    allowed = tmp_path / "images"
    allowed.mkdir()
    if kind == "absolute":
        url = (tmp_path / "outside.svg").as_uri()
    elif kind in {"traversal", "encoded-traversal"}:
        url = (allowed / ".." / "outside.svg").as_uri()
        if kind == "encoded-traversal":
            url = url.replace("../", "%2e%2e%2f")
    elif kind == "sibling-prefix":
        url = (tmp_path / "images-extra" / "outside.svg").as_uri()
    else:
        monkeypatch.chdir(allowed)
        url = "file:../outside.svg"

    def forbidden(*args: object, **kwargs: object) -> Path:
        pytest.fail("outside-root file URI reached filesystem resolution")

    monkeypatch.setattr(Path, "resolve", forbidden)
    assert _file_payload(url, str(allowed)) is None


def test_file_inside_root_remains_supported(tmp_path: Path) -> None:
    allowed = tmp_path / "images"
    allowed.mkdir()
    image = allowed / "picture.svg"
    payload = b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"/>'
    image.write_bytes(payload)
    assert _file_payload(image.as_uri(), str(allowed)) == (payload, "image/svg+xml")


def test_empty_data_uri_is_not_media() -> None:
    assert image_payload("data:image/png;base64,") is None


def test_xlsx_walk_reaches_leaf_cells() -> None:
    root = etree.fromstring(b"<worksheet><sheetData><row><c><v>{d.title}</v></c></row></sheetData></worksheet>")
    assert [node.text for node in _texts_and_copies(root, set(), SHEET.texts)] == ["{d.title}"]


def test_render_restores_context_after_failure() -> None:
    sentinel = set()
    token = SKIPPED_COPIES.set(sentinel)
    try:
        with pytest.raises(CorruptedTemplateError):
            DocxTextRenderer().render_tree(b"<broken", {}, RenderOptions.create())
        assert SKIPPED_COPIES.get() is sentinel
    finally:
        SKIPPED_COPIES.reset(token)


def test_viewbox_svg_and_local_gradient_are_supported() -> None:
    raw = (
        b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">'
        b'<!--valid comment--><defs><linearGradient id="g"><stop stop-color="red"/></linearGradient></defs>'
        b'<rect width="10" height="10" fill="url(\'#g\')"/></svg>'
    )
    assert rasterize_safe_svg(raw, 220).startswith(b"\x89PNG")


@pytest.mark.parametrize(
    "attribute",
    [
        'style="fill:u\\72l(file:///secret/image.svg)"',
        'fill="u\\72l(file:///secret/image.svg)"',
        'style="@import &quot;file:///secret/style.css&quot;;"',
    ],
)
def test_svg_escaped_attribute_resources_are_rejected_before_rasterization(
    attribute: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from carbotek.features import image

    def forbidden(*args, **kwargs):
        pytest.fail("unsafe SVG reached native rasterization")

    monkeypatch.setattr(image, "svg_to_bytes", forbidden)
    raw = ('<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><rect ' + attribute + "/></svg>").encode()
    with pytest.raises(MarkerResolutionError):
        rasterize_safe_svg(raw, 220)


@pytest.mark.parametrize(
    "inner",
    [
        '<image href="file:///secret/image.png"/>',
        "<style>rect {fill: url(https://example.com/image.svg)}</style>",
        '<rect style="fill:u\\72l(file:///secret/image.svg)"/>',
    ],
)
def test_embedded_svg_resources_are_validated_recursively(inner: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from base64 import b64encode

    from carbotek.features import image

    def forbidden(*args, **kwargs):
        pytest.fail("unsafe nested SVG reached native rasterization")

    monkeypatch.setattr(image, "svg_to_bytes", forbidden)
    embedded = ('<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">' + inner + "</svg>").encode()
    for _ in range(2):
        source = "data:image/svg+xml;base64," + b64encode(embedded).decode()
        embedded = (
            '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><image href="' + source + '"/></svg>'
        ).encode()
    with pytest.raises(MarkerResolutionError):
        rasterize_safe_svg(embedded, 220)


@pytest.mark.parametrize(
    "attributes,accepted",
    [
        ('width="16384" height="1"', True),
        ('width="16385" height="1"', False),
        ('width="4000" height="4000"', True),
        ('width="4001" height="4000"', False),
        ('width="1in" height="1in"', True),
        ('width="100in" height="1in"', False),
        ('width="100%" height="100%" viewBox="0 0 100000 10"', False),
        ('viewBox="0 0 NaN 10"', False),
    ],
)
def test_svg_raster_allocation_boundaries_are_checked_before_native(
    attributes: str, accepted: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    from carbotek.features import image

    calls = []

    def rasterize(source, **kwargs):
        calls.append(source)
        return b"PNG_TEST_RESULT"

    monkeypatch.setattr(image, "svg_to_bytes", rasterize)
    raw = ('<svg xmlns="http://www.w3.org/2000/svg" ' + attributes + "/>").encode()
    if accepted:
        assert rasterize_safe_svg(raw, 220) == b"PNG_TEST_RESULT"
        assert len(calls) == 1
    else:
        with pytest.raises(MarkerResolutionError):
            rasterize_safe_svg(raw, 220)
        assert calls == []


@pytest.mark.parametrize(
    "url",
    [
        "file://host/share/image.png",
        "file://localhost/C:/images/image.png",
        "file:////host/share/image.png",
        "file://///host/share/image.png",
        "file:///%5C%5Chost%5Cshare%5Cimage.png",
        "file:///%2F%2Fhost/share/image.png",
        "file:///%5Chost%5Cshare%5Cimage.png",
        "file:///%2Fhost/share/image.png",
        "file:%5C%5Chost%5Cshare%5Cimage.png",
        "file:%2F%2Fhost/share/image.png",
        "file:///%5C%5C%3F%5CC:%5Cimages%5Cimage.png",
        "file:///%5C%5C.%5CC:%5Cimages%5Cimage.png",
    ],
)
def test_network_and_device_file_uri_variants_never_reach_filesystem(url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args, **kwargs):
        pytest.fail("network/device URI reached the filesystem")

    monkeypatch.setattr(Path, "resolve", forbidden)
    monkeypatch.setattr(Path, "read_bytes", forbidden)
    assert _file_payload(url, "C:/images") is None


@pytest.mark.parametrize("uppercase_root", [False, True])
def test_local_file_uri_with_encoded_spaces_remains_supported(tmp_path: Path, uppercase_root: bool) -> None:
    import os

    if uppercase_root and os.name != "nt":
        pytest.skip("Windows case-insensitive path behavior")
    allowed = tmp_path / "Allowed Images"
    allowed.mkdir()
    image = allowed / "picture space.svg"
    payload = b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"/>'
    image.write_bytes(payload)
    configured = str(allowed).upper() if uppercase_root else str(allowed)
    assert "%20" in image.as_uri()
    assert _file_payload(image.as_uri(), configured) == (payload, "image/svg+xml")
