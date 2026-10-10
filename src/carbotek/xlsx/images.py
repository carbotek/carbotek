# pyright: strict, reportPrivateUsage=false
"""XLSX pictures and hyperlinks (``lib/image.js`` ``preProcessXLSX``/``postProcessXlsx``, ``lib/hyperlinks.js``).

Preparation (on the part text, cached with the compiled template):

* a picture of ``xl/drawings/drawingN.xml`` with a marker (usually in the alt text) loses its markers
  and its ``a:blip`` points to ``{marker:generateImageXlsxReference(N)}``; ``[i]`` is refused;
* a hyperlink target ``https://d.path`` of ``xl/worksheets/_rels/sheetN.xml.rels`` becomes
  ``{d.path:validateURL}`` (rendered like any marker).

After rendering, :class:`WorkbookImages` loads every picture value once (keyed by the value, as the
JS image database), points the blips at ``rIdCarbotekN``, adds ``xl/media/CarbotekImageN.ext`` next to
the template's own media, the relationships of each drawing and the content types. A value that
gives no image becomes the grey cross EMF of JS.
"""

import json
import re
from base64 import b64decode
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from lxml import etree  # pyright: ignore[reportAttributeAccessIssue] lxml has incomplete stubs.

from carbotek.docx.session import CURRENT_RENDER, feature_parse
from carbotek.errors import MarkerResolutionError
from carbotek.features.barcode import render_barcode
from carbotek.features.fetch import ImageSourceConfig
from carbotek.features.image import fetch_remote_images, image_payload, render_chart, resolve_image_path
from carbotek.ooxml.image_types import CONTENT_TYPES, MIME_EXTENSIONS
from carbotek.ooxml.xml import parse_xml
from carbotek.package import TemplatePackage, TemplatePart

from .prepare import is_carbotek_marker

_DRAWING = re.compile(r"xl/drawings/drawing(\d+)\.xml")
_DRAWING_RELS = re.compile(r"xl/drawings/_rels/drawing(\d+)\.xml\.rels")
_SHEET_RELS = re.compile(r"_rels/sheet+\d*.xml.rels")
_PICTURE = re.compile(r"<xdr:pic>.*?\{(.+?)\}[\s\S]*?</xdr:pic>")
_ANY_MARKER = re.compile(r"\{([^{]+?)\}")
_BLIP = re.compile(r'<a:blip[^>]*?r:embed="[^>]*?"/?>')
_EMBED = re.compile(r'r:embed=".*?"')
_HYPERLINK_TARGET = re.compile(r'Target="((http[s]?://)?[dc]\.[^"]*)"')
_IMAGE_RELATIONSHIP_TARGET = re.compile(
    r'<Relationship[^<>]*?Target="([^<>]*?)\.(jpg|png|jpeg|gif|bmp|xmb|svg|webp|pbm){1}"[^<>]*?/>', re.IGNORECASE
)
REFERENCE_FORMATTER = "generateImageXlsxReference"

# lib/image.js DEFAULT_EMF_IMAGE: printed when a value gives no image.
_DEFAULT_EMF = b64decode(
    "AQAAAPQAAAAAAAAAAAAAAEUGAABFBgAAAAAAAAAAAABHDQAARw0AACBFTUYAAAEAYAMAACIAAAACAAAARAAAAGwAAAAAAAAA3ScAAH0zAADYAAAAFwEA"
    "AAAAAAAAAAAAAAAAAMBLAwDYQQQASQBuAGsAcwBjAGEAcABlACAAMQAuADAALgAyACAAKABlADgANgBjADgANwAwADgANwA5ACwAIAAyADAAMgAxAC0A"
    "MAAxAC0AMQA1ACkAIAAAAFMAYQBuAHMAIAB0AGkAdAByAGUAQwByAG8AcwBzAEcAcgBlAHkANAAuAGUAbQBmAAAAAAAAABEAAAAMAAAAAQAAACQAAAAk"
    "AAAAAACAPwAAAAAAAAAAAACAPwAAAAAAAAAAAgAAAEYAAAAsAAAAIAAAAFNjcmVlbj0xMDIwNXgxMzE4MXB4LCAyMTZ4Mjc5bW0ARgAAADAAAAAjAAAA"
    "RHJhd2luZz0xMjguMHgxMjguMHB4LCAzMy45eDMzLjltbQAAEgAAAAwAAAABAAAAEwAAAAwAAAACAAAAFgAAAAwAAAAYAAAAGAAAAAwAAAAAAAAAFAAA"
    "AAwAAAANAAAAXwAAADgAAAABAAAAAAAAAAAAAAAAAAAAAAAAAAAAAQAFAAAAAAAAALOzswAGAAAAAAAAAAAAAAAlAAAADAAAAAEAAAA7AAAACAAAABsA"
    "AAAQAAAAAgAAAAIAAAA2AAAAEAAAAEUGAAACAAAANgAAABAAAABFBgAAQwYAADYAAAAQAAAAAgAAAEMGAAA9AAAACAAAADwAAAAIAAAAQAAAABgAAAAA"
    "AAAAAAAAAP//////////JQAAAAwAAAAIAACAKAAAAAwAAAABAAAAXwAAADgAAAABAAAAAAAAAAAAAAAAAAAAAAAAAAAAAQAFAAAAAAAAALOzswAGAAAA"
    "AAAAAAAAAAAlAAAADAAAAAEAAAA7AAAACAAAABsAAAAQAAAAAAAAAAIAAAA2AAAAEAAAAEUGAABDBgAANgAAABAAAAAAAAAAQwYAADYAAAAQAAAARQYA"
    "AAIAAAA8AAAACAAAAEAAAAAYAAAAAAAAAAAAAAD//////////yUAAAAMAAAACAAAgCgAAAAMAAAAAQAAAA4AAAAUAAAAAAAAAAAAAABgAwAA"
)
# lib/image.js mimetypeList / extensionList.
_EXTENSIONS = MIME_EXTENSIONS
_CONTENT_TYPES = CONTENT_TYPES
_R_EMBED = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed"
_A_BLIP = "{http://schemas.openxmlformats.org/drawingml/2006/main}blip"


def prepare_pictures(name: str, text: str) -> str:
    """``image.preProcessXLSX`` for one part (only ``xl/drawings/drawingN.xml`` changes)."""
    found = _DRAWING.search(name)
    if found is None:
        return text
    drawing = found.group(1)

    def picture(match: re.Match[str]) -> str:
        source, marker = match.group(0), match.group(1)
        if not is_carbotek_marker(marker):
            return source
        if "[i]" in marker:
            raise MarkerResolutionError("Carbotek does not support a list of images on XLSX template.")
        marker = re.sub(r":barcode", ":isImage:barcode", marker, flags=re.IGNORECASE)
        source = _ANY_MARKER.sub("", source)
        reference = f'r:embed="{{{marker}:{REFERENCE_FORMATTER}({drawing})}}"'
        return _BLIP.sub(lambda blip: _EMBED.sub(lambda _: reference, blip.group(0)), source)

    return _PICTURE.sub(picture, text)


def prepare_hyperlinks(name: str, text: str) -> str:
    """``hyperlinks.insertHyperlinksXLSX`` for one part (worksheet relationships)."""
    if _SHEET_RELS.search(name) is None:
        return text

    def target(match: re.Match[str]) -> str:
        decoded = _decode_marker(match.group(0))
        decoded = re.sub(r'Target="(http[s]?://)?', 'Target="{', decoded)
        return re.sub(r'/?"$', ':validateURL}"', decoded)

    return _HYPERLINK_TARGET.sub(target, text)


def _decode_marker(text: str) -> str:
    """``hyperlinks.decodeMarker`` (``%7B``/``%7D`` and, as in JS, any ``%D``/``%B`` escape)."""
    text = re.sub(r"%[25]*7B", "{", text, flags=re.IGNORECASE)
    text = re.sub(r"%[25]*7D", "}", text, flags=re.IGNORECASE)
    text = re.sub(r"%[25]*D", "]", text, flags=re.IGNORECASE)
    return re.sub(r"%[25]*B", "[", text, flags=re.IGNORECASE)


@dataclass(slots=True)
class _Image:
    id: int
    raw: bytes
    extension: str
    drawings: list[int]


@dataclass(frozen=True, slots=True)
class WorkbookImages:
    """Resolve the picture references of the rendered drawings (``image.postProcessXlsx``)."""

    config: ImageSourceConfig | None = None

    def apply(
        self,
        package: TemplatePackage,
        data: Mapping[str, Any] | list[Any],
        complement: Mapping[str, Any],
        language: str = "en",
    ) -> TemplatePackage:
        database: dict[str, _Image] = {}
        replacements: dict[str, TemplatePart] = {}
        pending: list[tuple[str, int, etree._Element]] = []
        for name in package.order:
            found = _DRAWING.fullmatch(name)
            part = package.parts.get(name)
            if found is None or part is None or not part.contains(REFERENCE_FORMATTER):
                continue
            root = part.tree()
            pending.append((name, int(found.group(1)), root))
        urls = {
            value
            for _, _, root in pending
            for blip in root.iter(_A_BLIP)
            if isinstance(value := _value(blip, data, complement), str) and value.startswith(("https:", "http:"))
        }
        remote = fetch_remote_images(urls, self.config)
        for name, drawing, root in pending:
            for blip in root.iter(_A_BLIP):
                reference = blip.get(_R_EMBED, "")
                if REFERENCE_FORMATTER not in reference:
                    continue
                marker = feature_parse(reference)
                value = _image_value(marker, data, complement)
                key = value if isinstance(value, str) else json.dumps(value, sort_keys=True, default=str)
                image = database.get(key)
                if image is None:
                    raw, extension = self._load(value, marker.formatters, remote, language)
                    image = database[key] = _Image(len(database), raw, extension, [])
                image.drawings.append(drawing)
                blip.set(_R_EMBED, f"rIdCarbotek{image.id}")
            replacements[name] = TemplatePart.from_tree(name, root)
        if not database:
            return package.replace_parts(replacements)
        extensions = self._relationships(package, database, replacements)
        replacements.update(_content_types(package, extensions))
        return package.replace_parts(replacements)

    def _load(
        self, value: Any, formatters: tuple[Any, ...], remote: Mapping[str, tuple[bytes, str] | None], language: str
    ) -> tuple[bytes, str]:
        barcode = next((call for call in formatters if call.name == "barcode"), None)
        chart = next((call for call in formatters if call.name == "chart"), None)
        payload = (
            render_chart(value, language)
            if chart is not None
            else render_barcode(value, barcode.arguments)
            if barcode is not None
            else remote.get(value)
            if isinstance(value, str) and value.startswith(("https:", "http:"))
            else image_payload(value, self.config)
        )
        if payload is None or payload[1] not in _EXTENSIONS:
            return _DEFAULT_EMF, "emf"
        return payload[0], _EXTENSIONS[payload[1]]

    @staticmethod
    def _relationships(
        package: TemplatePackage, database: Mapping[str, _Image], replacements: dict[str, TemplatePart]
    ) -> list[str]:
        """Media parts and drawing relationships; the extensions in use, in order."""
        extensions: list[str] = []
        for name in package.order:
            found = _DRAWING_RELS.fullmatch(name)
            part = package.parts.get(name)
            if found is None or part is None:
                continue
            drawing = int(found.group(1))
            text = part.content.decode("utf-8")
            targets = _IMAGE_RELATIONSHIP_TARGET.findall(text)
            if not targets:
                continue  # JS: the media folder is not found, the drawing keeps its pictures
            folder = targets[-1][0].rpartition("/")[0] + "/"
            relationships: list[str] = []
            for image in database.values():
                if drawing not in image.drawings:
                    continue
                path = f"{folder}CarbotekImage{image.id}.{image.extension}"
                media = "xl" + path[2:]
                if media not in package.parts and media not in replacements:
                    replacements[media] = TemplatePart(media, image.raw, False)
                relationships.append(
                    '<Relationship Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" '
                    f'Target="{path}" Id="rIdCarbotek{image.id}"/>'
                )
                if image.extension not in extensions:
                    extensions.append(image.extension)
            text = text.replace("</Relationships>", "".join(relationships) + "</Relationships>", 1)
            replacements[name] = TemplatePart(name, text.encode("utf-8"), True)
        return extensions


def _value(blip: etree._Element, data: Mapping[str, Any] | list[Any], complement: Mapping[str, Any]) -> Any:
    reference = blip.get(_R_EMBED, "")
    if REFERENCE_FORMATTER not in reference:
        return None
    marker = feature_parse(reference)
    return _image_value(marker, data, complement)


def _content_types(package: TemplatePackage, extensions: list[str]) -> dict[str, TemplatePart]:
    """``image.defineImageContentTypeDocx``: a ``Default`` for each new image extension."""
    part = package.parts.get("[Content_Types].xml")
    if part is None:
        return {}
    text = part.content.decode("utf-8")
    additions = ""
    for extension in (item.lower() for item in extensions):
        pattern = re.compile(f'Extension="{re.escape(extension)}"', re.IGNORECASE)
        if pattern.search(text) or pattern.search(additions) or extension not in _CONTENT_TYPES:
            continue
        additions += f'<Default Extension="{extension}" ContentType="{_CONTENT_TYPES[extension]}"/>'
    if not additions:
        return {}
    parse_xml(text.replace("</Types>", additions + "</Types>", 1).encode("utf-8"))  # stays well-formed
    return {
        part.name: TemplatePart(part.name, text.replace("</Types>", additions + "</Types>", 1).encode("utf-8"), True)
    }


def _image_value(marker: Any, data: Mapping[str, Any] | list[Any], complement: Mapping[str, Any]) -> Any:
    session = CURRENT_RENDER.get()
    if session is not None:
        skip = {REFERENCE_FORMATTER, "barcode", "chart", "isImage", "imageFit", "autoOrient"}
        return session.evaluate(
            replace(marker, formatters=tuple(call for call in marker.formatters if call.name not in skip))
        )
    return resolve_image_path(marker.path, data if marker.root == "d" else complement)
