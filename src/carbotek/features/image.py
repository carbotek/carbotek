# pyright: strict, reportPrivateUsage=false
"""Data-URI image substitution for Word drawing alt-text markers."""

import os
import re
import struct
import time
from base64 import b64decode
from collections.abc import Mapping
from dataclasses import dataclass
from mimetypes import guess_type
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
lazy from concurrent.futures import ThreadPoolExecutor, as_completed

from lxml import etree  # pyright: ignore[reportAttributeAccessIssue]
lazy from resvg_py import svg_to_bytes

from carbotek._native_adapter import native_decode_data_uri
from carbotek.docx.renderer import resolve_path
from carbotek.docx.session import CURRENT_RENDER, feature_parse, feature_scan
from carbotek.errors import FeatureUnavailable, MarkerResolutionError
from carbotek.markers import FormatterCall, Marker
from carbotek.ooxml.image_types import MIME_EXTENSIONS, image_mime
from carbotek.ooxml.ns import qn
from carbotek.ooxml.xml import parse_xml, serialize_xml
from carbotek.package import TemplatePackage, TemplatePart

from .barcode import render_barcode
from .fetch import ImageSourceConfig, fetch_image


def render_chart(value: Any, language: str = "en") -> tuple[bytes, str]:
    """ECharts charts (``{d.x:chart}``) need a JavaScript runtime and are not part of this package."""
    raise FeatureUnavailable('Formatter "chart" (ECharts) is not supported')


_IMAGE_RELATIONSHIP = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"
_RELATIONSHIPS = "http://schemas.openxmlformats.org/package/2006/relationships"
_CONTENT_TYPES = "http://schemas.openxmlformats.org/package/2006/content-types"
_MIME_EXTENSIONS = MIME_EXTENSIONS
HTML_IMAGE_PREFIX = "__CARBOTEK_HTML_IMAGE__:"
_DEFAULT_PNG = b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScL5XQAAAABJRU5ErkJggg=="
)


@dataclass(frozen=True, slots=True)
class _ImageSource:
    doc_pr: etree._Element
    marker: Marker
    value: Any


@dataclass(frozen=True, slots=True)
class ImageSubstitution:
    """Populate existing ``w:drawing`` placeholders from data URI values."""

    config: ImageSourceConfig | None = None

    def apply(
        self,
        package: TemplatePackage,
        data: Mapping[str, Any] | list[Any],
        *,
        complement: Mapping[str, Any] | None = None,
        rasterize_svg: bool = False,
        image_dpi: int = 220,
        language: str = "en",
    ) -> TemplatePackage:
        database: dict[bytes, tuple[int, str, str]] = {}
        replacements: dict[str, TemplatePart] = {}
        consumers: dict[int, set[str]] = {}
        drawing_id = 1
        complement = complement or {}
        roots, remote_urls = _collect_image_roots(package, data, complement)
        remote_images = fetch_remote_images(remote_urls, self.config)
        for part_name, (root, sources) in roots.items():
            changed = False
            for source in sources:
                doc_pr, marker, value = source.doc_pr, source.marker, source.value
                barcode_call = next((call for call in marker.formatters if call.name == "barcode"), None)
                chart_call = next((call for call in marker.formatters if call.name == "chart"), None)
                payload = (
                    render_chart(value, language)
                    if chart_call is not None
                    else render_barcode(value, barcode_call.arguments)
                    if barcode_call is not None
                    else remote_images.get(value)
                    if isinstance(value, str) and value.startswith(("https:", "http:"))
                    else image_payload(value, self.config)
                )
                if payload is None:
                    payload = (_DEFAULT_PNG, "image/png")
                    marker = Marker(marker.root, marker.path, (FormatterCall("imageFit", ("fill",)),), marker.raw)
                raw, mime = payload
                if mime == "image/svg+xml" and rasterize_svg:
                    try:
                        raw = rasterize_safe_svg(raw, image_dpi)
                    except (UnicodeDecodeError, ValueError) as error:
                        raise MarkerResolutionError("Invalid SVG image") from error
                    mime = "image/png"
                image_id, extension, _ = database.setdefault(raw, (len(database), _MIME_EXTENSIONS[mime], mime))
                consumers.setdefault(image_id, set()).add(part_name)
                if not _replace_drawing(doc_pr, marker, raw, mime, image_id, drawing_id):
                    continue
                drawing_id += 1
                changed = True
            if changed:
                replacements[part_name] = TemplatePart.from_tree(part_name, root)
        if not database:
            return package
        relationship_roots: dict[str, etree._Element] = {}
        for raw, (image_id, extension, _mime) in database.items():
            replacements[f"word/media/CarbotekImage{image_id}.{extension}"] = TemplatePart(
                f"word/media/CarbotekImage{image_id}.{extension}", raw, False
            )
            for consumer in consumers[image_id]:
                _append_relationship(package, replacements, relationship_roots, consumer, image_id, extension)
        for rel_name, rel_root in relationship_roots.items():
            replacements[rel_name] = TemplatePart(rel_name, serialize_xml(rel_root), True)
        content_types = parse_xml(package.parts["[Content_Types].xml"].content)
        known_extensions = {node.get("Extension") for node in content_types}
        for _, extension, mime in database.values():
            if extension not in known_extensions:
                etree.SubElement(content_types, f"{{{_CONTENT_TYPES}}}Default", Extension=extension, ContentType=mime)
        if "rels" not in known_extensions:
            etree.SubElement(
                content_types,
                f"{{{_CONTENT_TYPES}}}Default",
                Extension="rels",
                ContentType="application/vnd.openxmlformats-package.relationships+xml",
            )
        replacements["[Content_Types].xml"] = TemplatePart(
            "[Content_Types].xml",
            serialize_xml(content_types),
            True,
        )
        return package.replace_parts(replacements)


_IMAGE_MARKER = re.compile(rb'descr="(?:[^"]*{|__CARBOTEK_HTML_IMAGE__:)')


_IMAGE_MARKER_XPATH = etree.XPath(
    "boolean(//*[local-name()='docPr'][contains(@descr, '{') or starts-with(@descr, '__CARBOTEK_HTML_IMAGE__:')])"
)


def _collect_image_roots(
    package: TemplatePackage, data: Mapping[str, Any] | list[Any], complement: Mapping[str, Any]
) -> tuple[dict[str, tuple[etree._Element, list[_ImageSource]]], set[str]]:
    roots: dict[str, tuple[etree._Element, list[_ImageSource]]] = {}
    remote_urls: set[str] = set()
    for part in package.parts.values():
        if not part.is_xml or not part.name.startswith(
            ("word/document", "word/header", "word/footer", "word/footnotes", "word/endnotes")
        ):
            continue
        if part.has_tree:
            root = part.tree()
            if not any(
                "{" in (descr := doc_pr.get("descr", "")) or "[[ct-" in descr or descr.startswith(HTML_IMAGE_PREFIX)
                for doc_pr in root.iter(qn("wp:docPr"))
            ):
                continue  # no picture with a marker in its alt text
        elif _IMAGE_MARKER.search(part.content) is None:
            continue
        else:
            root = part.tree()
        sources: list[_ImageSource] = []
        for doc_pr in root.iter(qn("wp:docPr")):
            marker = _marker(doc_pr.get("descr", ""))
            drawing = doc_pr.getparent()
            if marker is not None and drawing is not None and drawing.find(f".//{qn('a:blip')}") is not None:
                value = _source_value(marker, data, complement)
                sources.append(_ImageSource(doc_pr, marker, value))
                if (
                    all(call.name not in {"barcode", "chart"} for call in marker.formatters)
                    and isinstance(value, str)
                    and value.startswith(("https:", "http:"))
                ):
                    remote_urls.add(value)
        if sources:
            roots[part.name] = root, sources
    return roots, remote_urls


def fetch_remote_images(urls: set[str], config: ImageSourceConfig | None = None) -> dict[str, tuple[bytes, str] | None]:
    if not urls:
        return {}
    try:
        concurrency = (
            config.max_download_concurrency
            if config is not None
            else int(os.environ.get("CARBOTEK__IMAGE_MAX_DOWNLOAD_CONCURRENCY", "4"))
        )
        total_timeout = (
            config.total_timeout_ms
            if config is not None
            else int(os.environ.get("CARBOTEK__IMAGE_TOTAL_TIMEOUT_MS", "10000"))
        ) / 1000
    except ValueError:
        return {}
    if concurrency < 1 or total_timeout <= 0:
        return {}
    deadline = time.monotonic() + total_timeout
    results: dict[str, tuple[bytes, str] | None] = {}
    pool = ThreadPoolExecutor(max_workers=concurrency)
    try:
        futures = {
            pool.submit(
                fetch_image, url, overall_deadline=deadline, **({"config": config} if config is not None else {})
            ): url
            for url in sorted(urls)
        }
        try:
            for future in as_completed(futures, timeout=total_timeout):
                results[futures[future]] = future.result()
        except TimeoutError:
            pass
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return results


def _marker(description: str):
    if description.startswith(HTML_IMAGE_PREFIX):
        return Marker("d", (), (FormatterCall("imageFit", ("fill",)),), description)
    session = CURRENT_RENDER.get()
    if session is not None and description in session.deferred:
        marker = feature_parse(description)
        return marker if marker.root in {"d", "c"} else None
    occurrences = feature_scan(description)
    if len(occurrences) != 1:
        return None
    marker = feature_parse(occurrences[0].raw)
    return marker if marker.root in {"d", "c"} else None


def _source_value(marker: Marker, data: Mapping[str, Any] | list[Any], complement: Mapping[str, Any]) -> Any:
    session = CURRENT_RENDER.get()
    if session is not None and marker.raw in session.deferred:
        skipped = {"imageFit", "autoOrient", "isImage", "barcode", "chart"}
        formatters = tuple(call for call in marker.formatters if call.name not in skipped)
        source = marker if formatters == marker.formatters else Marker(marker.root, marker.path, formatters, marker.raw)
        return session.evaluate(source)
    return (
        marker.raw[len(HTML_IMAGE_PREFIX) :]
        if marker.raw.startswith(HTML_IMAGE_PREFIX)
        else resolve_image_path(marker.path, data if marker.root == "d" else complement)
    )


def _image_fit(marker: Any) -> str:
    for call in marker.formatters:
        if call.name == "imageFit" and call.arguments:
            value = call.arguments[0].strip("\"'")
            if value in {"contain", "fill", "fillWidth"}:
                return value
    return "fillWidth"


def _replace_drawing(
    doc_pr: etree._Element, marker: Any, raw: bytes, mime: str, image_id: int, drawing_id: int
) -> bool:
    drawing = doc_pr.getparent()
    if drawing is None:
        return False
    blip = drawing.find(f".//{qn('a:blip')}")
    if blip is None:
        return False
    fit = _image_fit(marker)
    if marker.raw.startswith(HTML_IMAGE_PREFIX):
        _fit_html_extents(drawing, raw, mime)
    auto_orient = any(call.name == "autoOrient" for call in marker.formatters)
    if auto_orient and fit == "fill":
        raise MarkerResolutionError(':autoOrient is not supported with imageFit("fill")')
    orientation = _jpeg_orientation(raw) if auto_orient and mime == "image/jpeg" else 1
    if fit != "fill":
        _fit_extents(drawing, raw, mime, fit, orientation)
    if auto_orient:
        _apply_orientation(drawing, orientation)
    blip.set(qn("r:embed"), f"rIdCarbotek{image_id}")
    # lib/image.js: only the marker leaves wp:docPr (its id is made unique for the whole document
    # afterwards); pic:cNvPr gets id N+1000 and an empty name (Appendix C).
    doc_pr.set("descr", "" if marker.raw.startswith(HTML_IMAGE_PREFIX) else _without_markers(doc_pr.get("descr", "")))
    for properties in drawing.iter(qn("pic:cNvPr")):
        properties.set("id", str(1000 + image_id))
        properties.set("name", "")
        properties.set("descr", "")
    # A raster replaces the image, so an SVG extension of the template picture must go.
    for extension_list in list(blip.iter(qn("a:extLst"))):
        if (
            extension_list.find(f".//{{{_SVG_NAMESPACE}}}svgBlip") is not None
            and (extension_parent := extension_list.getparent()) is not None
        ):
            extension_parent.remove(extension_list)
    return True


_SVG_NAMESPACE = "http://schemas.microsoft.com/office/drawing/2016/SVG/main"


def _without_markers(description: str) -> str:
    """The description without its image marker and ``:imageFit(...)``, as JS leaves it."""
    session = CURRENT_RENDER.get()
    if session is not None and description in session.deferred:
        return ""
    description = re.sub(r":imageFit\(['\"]?(.*?)['\"]?\)", "", description)
    for occurrence in reversed(feature_scan(description)):
        description = description[: occurrence.start] + description[occurrence.end :]
    return description


def _fit_html_extents(drawing: etree._Element, raw: bytes, mime: str) -> None:
    extent = drawing.find(f".//{qn('wp:extent')}")
    if extent is None:
        return
    width, height = int(extent.get("cx", "0")), int(extent.get("cy", "0"))
    if width > 0 and height > 0:
        return
    image_width, image_height = _image_size(raw, mime) or (96, 96)
    if min(image_width, image_height) <= 0:
        image_width, image_height = 96, 96
    if width <= 0 and height <= 0:
        width, height = image_width * 9525, image_height * 9525
    elif width <= 0:
        width = round(height * image_width / image_height)
    else:
        height = round(width * image_height / image_width)
    for current in (extent, *drawing.iter(qn("a:ext"))):
        current.set("cx", str(max(width, 1)))
        current.set("cy", str(max(height, 1)))


def _fit_extents(drawing: etree._Element, raw: bytes, mime: str, fit: str, orientation: int = 1) -> None:
    dimensions = _image_size(raw, mime)
    extent = drawing.find(f".//{qn('wp:extent')}")
    if dimensions is None or extent is None:
        return
    try:
        width, height = int(extent.get("cx", "0")), int(extent.get("cy", "0"))
    except ValueError:
        return
    image_width, image_height = dimensions
    if min(width, height, image_width, image_height) <= 0:
        return
    if orientation > 4:
        width, height = height, width
    width_ratio = width / image_width
    height_ratio = height / image_height
    if orientation > 4:
        width_ratio, height_ratio = height_ratio, width_ratio
    ratio = min(width_ratio, height_ratio) if fit == "contain" else width_ratio
    new_width, new_height = int(image_width * ratio), int(image_height * ratio)
    for current in (extent, *drawing.iter(qn("a:ext"))):
        current.set("cx", str(new_width))
        current.set("cy", str(new_height))


def _apply_orientation(drawing: etree._Element, orientation: int) -> None:
    transform = drawing.find(f".//{qn('a:xfrm')}")
    if transform is None:
        return
    for name in ("rot", "flipH", "flipV"):
        transform.attrib.pop(name, None)
    attributes = {
        2: {"flipH": "1"},
        3: {"rot": "10800000"},
        4: {"flipV": "1"},
        5: {"rot": "5400000", "flipV": "1"},
        6: {"rot": "5400000"},
        7: {"rot": "16200000", "flipV": "1"},
        8: {"rot": "16200000"},
    }
    transform.attrib.update(attributes.get(orientation, {}))


def _jpeg_orientation(raw: bytes) -> int:
    if not raw.startswith(b"\xff\xd8"):
        return 1
    position = 2
    while position + 4 <= len(raw) and raw[position] == 0xFF:
        marker = raw[position + 1]
        length = int.from_bytes(raw[position + 2 : position + 4], "big")
        end = position + 2 + length
        if length < 2 or end > len(raw):
            break
        if marker == 0xE1 and raw[position + 4 : position + 10] == b"Exif\0\0":
            tiff = raw[position + 10 : end]
            if len(tiff) < 8 or tiff[:2] not in {b"II", b"MM"}:
                return 1
            order = "little" if tiff[:2] == b"II" else "big"
            if int.from_bytes(tiff[2:4], order) != 42:
                return 1
            offset = int.from_bytes(tiff[4:8], order)
            if offset + 2 > len(tiff):
                return 1
            count = int.from_bytes(tiff[offset : offset + 2], order)
            for index in range(count):
                entry = offset + 2 + index * 12
                if entry + 12 > len(tiff):
                    break
                if int.from_bytes(tiff[entry : entry + 2], order) == 274:
                    value = int.from_bytes(tiff[entry + 8 : entry + 10], order)
                    return value if 1 <= value <= 8 else 1
            return 1
        position = end
    return 1


def _image_size(raw: bytes, mime: str) -> tuple[int, int] | None:
    if mime == "image/png" and raw.startswith(b"\x89PNG\r\n\x1a\n") and len(raw) >= 24:
        return struct.unpack(">II", raw[16:24])
    if mime == "image/gif" and raw[:6] in {b"GIF87a", b"GIF89a"} and len(raw) >= 10:
        return struct.unpack("<HH", raw[6:10])
    if mime == "image/bmp" and raw.startswith(b"BM") and len(raw) >= 26:
        width, height = struct.unpack("<ii", raw[18:26])
        return abs(width), abs(height)
    if mime == "image/jpeg" and raw.startswith(b"\xff\xd8"):
        position = 2
        while position + 4 <= len(raw):
            if raw[position] != 0xFF:
                break
            marker = raw[position + 1]
            length = int.from_bytes(raw[position + 2 : position + 4], "big")
            if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB} and position + 9 <= len(raw):
                return int.from_bytes(raw[position + 7 : position + 9], "big"), int.from_bytes(
                    raw[position + 5 : position + 7], "big"
                )
            if length < 2:
                break
            position += 2 + length
    if mime == "image/svg+xml":
        return _svg_size(raw)
    if mime in {"image/webp", "image/x-emf", "image/xbm", "image/x-portable-bitmap"}:
        from io import BytesIO

        from PIL import Image

        try:
            with Image.open(BytesIO(raw)) as image:
                return image.size
        except OSError, ValueError, Image.DecompressionBombError:
            return None
    return None


def _svg_size(raw: bytes) -> tuple[int, int] | None:
    """Width/height of the root ``<svg>`` (``px`` or unitless), else its ``viewBox`` (npm image-size)."""
    match = re.search(rb"<svg\b[^>]*>", raw[:4096])
    if match is None:
        return None
    tag = match.group().decode("utf-8", "replace")

    def length(name: str) -> float | None:
        found = re.search(rf"\s{name}\s*=\s*[\"']\s*([0-9.]+)\s*(px)?\s*[\"']", tag)
        return float(found.group(1)) if found else None

    width, height = length("width"), length("height")
    view_box = re.search(r"viewBox\s*=\s*[\"']\s*[-0-9.]+[\s,]+[-0-9.]+[\s,]+([0-9.]+)[\s,]+([0-9.]+)", tag)
    if view_box is not None and (width is None or height is None):
        box_width, box_height = float(view_box.group(1)), float(view_box.group(2))
        if width is None and height is None:
            width, height = box_width, box_height
        elif width is None and box_height and height is not None:
            width = height * box_width / box_height
        elif height is None and box_width and width is not None:
            height = width * box_height / box_width
    if not width or not height:
        return None
    return round(width), round(height)


def resolve_image_path(path: tuple[str, ...], data: Mapping[str, Any] | list[Any]) -> Any:
    """Resolve a marker path, including loop items frozen to ``field[N]`` by the renderer."""
    return resolve_path(path, data)


def image_payload(value: Any, config: ImageSourceConfig | None = None) -> tuple[bytes, str] | None:
    if not isinstance(value, str):
        return None
    if value.startswith("file:"):
        return _file_payload(value, config.file_root if config is not None else None)
    if value.startswith(("https:", "http:")):
        return fetch_image(value, config=config) if config is not None else fetch_image(value)
    if not value.startswith("data:") or ";base64," not in value:
        return None
    header, encoded = value.split(",", maxsplit=1)
    mime = image_mime(header[5:].removesuffix(";base64"))
    if mime not in _MIME_EXTENSIONS:
        return None
    if (decoded := native_decode_data_uri(value)) is not None:
        return (decoded, mime) if decoded else None
    try:
        decoded = b64decode(encoded, validate=True)
        return (decoded, mime) if decoded else None
    except ValueError:
        return None


def _file_payload(value: str, configured_root: str | None = None) -> tuple[bytes, str] | None:
    """Read a local image only from the explicitly configured root directory."""
    root_value = configured_root if configured_root is not None else os.environ.get("CARBOTEK__IMAGE_FILE_ROOT")
    try:
        parsed = urlparse(value)
    except ValueError:
        return None
    if not root_value or parsed.scheme != "file" or parsed.netloc:
        return None
    path = unquote(parsed.path)
    # Reject network/device paths lexically, before any filesystem operation.
    if path.replace("\\", "/").startswith("//"):
        return None
    try:
        if os.name == "nt" and len(path) >= 3 and path[0] == "/" and path[2] == ":":
            path = path[1:]
        # Normalize dot segments without opening files or following links.
        root = Path(os.path.abspath(root_value))  # noqa: PTH100 - lexical check must not follow links
        candidate = Path(os.path.abspath(path))  # noqa: PTH100 - reject before filesystem resolution
        candidate.relative_to(root)
        # Links inside the allowed tree must also stay within the resolved root.
        root = Path(root_value).resolve(strict=True)
        candidate = Path(path).resolve(strict=True)
        candidate.relative_to(root)
    except OSError, ValueError:
        return None
    mime, _ = guess_type(candidate.name)
    mime = image_mime(mime or "")
    if mime not in _MIME_EXTENSIONS:
        return None
    try:
        return candidate.read_bytes(), mime
    except OSError:
        return None


def _rels_name(part_name: str) -> str:
    parent, _, filename = part_name.rpartition("/")
    return f"{parent}/_rels/{filename}.rels"


def _relationships_root(
    package: TemplatePackage, replacements: Mapping[str, TemplatePart], name: str
) -> etree._Element:
    existing = replacements.get(name) or package.parts.get(name)
    return parse_xml(existing.content) if existing is not None else etree.Element(f"{{{_RELATIONSHIPS}}}Relationships")


def _append_relationship(
    package: TemplatePackage,
    replacements: Mapping[str, TemplatePart],
    roots: dict[str, etree._Element],
    consumer: str,
    image_id: int,
    extension: str,
) -> None:
    name = _rels_name(consumer)
    root = roots.get(name)
    if root is None:
        root = _relationships_root(package, replacements, name)
        roots[name] = root
    etree.SubElement(
        root,
        f"{{{_RELATIONSHIPS}}}Relationship",
        Id=f"rIdCarbotek{image_id}",
        Type=_IMAGE_RELATIONSHIP,
        Target=f"media/CarbotekImage{image_id}.{extension}",
    )


def rasterize_safe_svg(raw: bytes, image_dpi: int) -> bytes:
    session = CURRENT_RENDER.get()
    key = ("svg", raw, image_dpi)
    if session is not None and key in session.assets:
        return session.assets[key][0]
    root = _validate_svg(raw, image_dpi)
    result = svg_to_bytes(serialize_xml(root).decode("utf-8"), dpi=float(image_dpi))
    if session is not None:
        session.assets[key] = result, "image/png"
    return result


def _validate_svg(raw: bytes, image_dpi: int, depth: int = 0) -> etree._Element:
    """Bound raster allocation and reject references outside the SVG payload."""
    root = parse_xml(raw)
    if etree.QName(root).localname != "svg":
        raise MarkerResolutionError("Invalid SVG image")
    if depth > 8:
        raise MarkerResolutionError("SVG image nesting exceeds the limit")
    for node in root.iter():
        if not isinstance(node.tag, str):
            continue
        for name, value in node.attrib.items():
            if etree.QName(name).localname == "href":
                _validate_svg_reference(value, image_dpi, depth)
            if "url(" in value.lower() or "@import" in value.lower() or "\\" in value:
                _validate_svg_css(value, image_dpi, depth)
        if etree.QName(node).localname == "style" and node.text:
            _validate_svg_css(node.text, image_dpi, depth)
    _bound_svg_dimensions(root, image_dpi)
    return root


def _bound_svg_dimensions(root: etree._Element, image_dpi: int) -> None:
    import math

    dimensions: list[float] = []
    units = {
        "": 1.0,
        "px": 1.0,
        "in": image_dpi,
        "cm": image_dpi / 2.54,
        "mm": image_dpi / 25.4,
        "pt": image_dpi / 72,
        "pc": image_dpi / 6,
    }
    viewbox = root.get("viewBox", "").replace(",", " ").split()
    fallback = [300.0, 150.0]
    if viewbox:
        try:
            numbers = [float(value) for value in viewbox]
        except ValueError as error:
            raise MarkerResolutionError("Invalid SVG viewBox") from error
        if len(numbers) != 4 or not all(math.isfinite(value) for value in numbers):
            raise MarkerResolutionError("Invalid SVG viewBox")
        fallback = numbers[2:]
    for index, name in enumerate(("width", "height")):
        source = root.get(name, "")
        if not source or source.strip().endswith("%"):
            source = str(fallback[index])
        match = re.fullmatch(r"\s*([+\-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+\-]?\d+)?)\s*([a-z]*)\s*", source)
        if match is None or match[2] not in units:
            raise MarkerResolutionError("SVG needs explicit bounded width and height")
        dimension = float(match[1]) * units[match[2]]
        if not math.isfinite(dimension) or not 0 < dimension <= 16384:
            raise MarkerResolutionError("SVG dimensions exceed the raster limit")
        dimensions.append(dimension)
        root.set(name, f"{dimension}px")
    if dimensions[0] * dimensions[1] > 16_000_000:
        raise MarkerResolutionError("SVG area exceeds the raster limit")


def _validate_svg_css(value: str, image_dpi: int, depth: int) -> None:
    if "@import" in value.lower() or "\\" in value:
        raise MarkerResolutionError("External SVG styles are not allowed")
    for reference in re.findall(r"url\(\s*([^)]*)\)", value, re.IGNORECASE):
        _validate_svg_reference(reference.strip().strip("'\""), image_dpi, depth)


def _validate_svg_reference(value: str, image_dpi: int, depth: int) -> None:
    if value.startswith("#"):
        return
    payload = image_payload(value) if value.startswith("data:") else None
    if payload is None:
        raise MarkerResolutionError("External SVG resources are not allowed")
    raw, mime = payload
    if len(raw) > 10 * 1024 * 1024:
        raise MarkerResolutionError("SVG embedded resource exceeds the limit")
    if mime == "image/svg+xml":
        _validate_svg(raw, image_dpi, depth + 1)
    else:
        from io import BytesIO

        from PIL import Image

        try:
            with Image.open(BytesIO(raw)) as image:
                if image.width * image.height > 16_000_000:
                    raise MarkerResolutionError("SVG embedded image area exceeds the limit")
        except (OSError, Image.DecompressionBombError) as error:
            raise MarkerResolutionError("Invalid SVG embedded image") from error
