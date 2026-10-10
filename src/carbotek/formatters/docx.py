# pyright: strict, reportPrivateUsage=false
"""DOCX-specific formatter helpers used by feature adapters and public callers."""

import math
import re
from typing import Any
from urllib.parse import quote, unquote, urlparse

from .condition import JS_UNDEFINED, _js_string
from .registry import FormatterMetadata, FormatterRegistry, MarkerContext

# lib/hyperlinks.js URL_ON_ERROR: printed instead of an invalid URL.
URL_ON_ERROR = "https://carbone.io/hyperlink-validation"
_URL_SHAPE = re.compile(r"[-a-zA-Z0-9@:%._+~#=]{1,256}\.[a-zA-Z0-9()]{1,6}\b([-a-zA-Z0-9()@:%_+.~#?&/=]*)")


def register(registry: FormatterRegistry) -> None:
    registry.register("generateImageDocxId", image_docx_id)
    registry.register("generateImageDocxGlobalId", image_docx_global_id, FormatterMetadata(uses_marker_context=True))
    registry.register("generateImageDocxReference", image_docx_reference)
    registry.register("scaleImage", scale_image)
    registry.register("autoOrient", auto_orient)
    registry.register("autoOrientDocx", auto_orient)
    registry.register("svgUpdate", svg_update, FormatterMetadata(uses_marker_context=True))
    registry.register("svgSelectiveUpdate", svg_selective_update, FormatterMetadata(uses_marker_context=True))
    registry.register("getHTMLContentDocx", html_content)
    registry.register("generateHyperlinkReference", hyperlink_reference)
    context_metadata = FormatterMetadata(uses_marker_context=True)
    registry.register("validateURL", validate_url, context_metadata)
    registry.register("defaultURL", default_url, context_metadata)
    registry.register("html", html_content)


def image_docx_id(value: Any, index: Any = 0) -> str:
    del value
    return str(int(index) + 1000)


def image_docx_global_id(value: Any, *, marker_context: MarkerContext | None = None) -> Any:
    """A unique id greater than 999; the report counter is incremented (JS ``Number(uniqueId)``)."""
    del value
    context = marker_context or MarkerContext(data={}, complement={})
    previous = _js_number(context.unique_id)
    context.unique_id = previous + 1
    return 999 + previous


def image_docx_reference(value: Any, index: Any = 0) -> str:
    del value
    return f"rIdCarbotek{int(index)}"


def scale_image(value: Any, *_arguments: Any) -> Any:
    """Leave bytes/URI payload unchanged; dimensions are applied to the drawing adapter."""
    return value


def auto_orient(value: Any, *_arguments: Any) -> Any:
    """Orientation is read from image metadata by the image adapter when available."""
    return value


def svg_update(
    value: Any,
    attribute: Any,
    identifier: Any,
    selector: Any = "id > *",
    source: Any = "own",
    *,
    marker_context: MarkerContext | None = None,
) -> str:
    """Validate an update of the template's own SVG; a DOCX template has none (JS error)."""
    del source
    attribute = attribute.strip() if isinstance(attribute, str) else attribute
    selector = selector.strip() if isinstance(selector, str) else "id > *"
    if isinstance(identifier, bool):
        identifier = "true" if identifier else "false"
    elif isinstance(identifier, int | float):
        identifier = _js_string(identifier)
    if not isinstance(attribute, str) or attribute == "":
        raise ValueError(f'Attribute name "{_js_string(attribute)}" cannot be empty in updateSVG formatter')
    if isinstance(value, bool) or not isinstance(value, str | int | float):
        raise ValueError(f'Attribute value "{_js_string(value)}" must be a string or a number in updateSVG formatter')
    if not isinstance(identifier, str):
        raise ValueError(
            f'Identifier value "{_js_string(identifier)}" must be a string or a number in updateSVG formatter'
        )
    if marker_context is None or marker_context.svg_updates is None:
        raise ValueError("SVG cannot be found and cannot be updated. Please contact the support")
    identifiers = marker_context.svg_updates.setdefault(selector, {})
    updates = identifiers.get(identifier, [])
    if not updates and identifier != "":
        identifiers[identifier] = updates
    updates.append((attribute, value))
    return ""


def svg_selective_update(
    value: Any,
    attribute: Any,
    attribute_value: Any,
    selector: Any = "id > *",
    source: Any = "own",
    *,
    marker_context: MarkerContext | None = None,
) -> str:
    identifier = value if isinstance(value, str | int | float) and not isinstance(value, bool) else ""
    return svg_update(attribute_value, attribute, identifier, selector, source, marker_context=marker_context)


def _js_number(value: Any) -> float | int:
    """``Number(value)`` for the counter: numbers, numeric strings, otherwise NaN."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int | float):
        return value
    if isinstance(value, str):
        try:
            number = float(value.strip() or "0")
        except ValueError:
            return math.nan
        return int(number) if number.is_integer() else number
    return 0 if value is None else math.nan


def html_content(value: Any, *_arguments: Any) -> Any:
    return value


def default_url(value: Any, url: Any = "", *, marker_context: MarkerContext | None = None) -> Any:
    """Set the URL printed for invalid URLs in this report; print the data unchanged."""
    if marker_context is not None:
        marker_context.default_url_on_error = check_url("" if url is JS_UNDEFINED else url)
    return value


def validate_url(value: Any, *, marker_context: MarkerContext | None = None) -> Any:
    """Print a valid, encoded URL or the fallback URL (lib/hyperlinks.js ``validateURL``)."""
    fallback = marker_context.default_url_on_error if marker_context is not None else ""
    return check_url("" if value is JS_UNDEFINED else value, fallback)


def check_url(url: Any, fallback: str = "") -> str:
    """Port of ``hyperlinks.validateURL(url, fallback)``."""
    fallback = fallback or URL_ON_ERROR
    if not isinstance(url, str):
        return fallback
    try:
        encoded = unquote(url, errors="strict") != url
    except UnicodeDecodeError:
        encoded = False
    if not encoded:
        try:
            url = quote(url, safe=";,/?:@&=+$-_.!~*'()#", errors="strict")
        except UnicodeEncodeError:
            return fallback
    else:
        url = url.replace('"', "%22").replace("<", "%3C").replace(">", "%3E")
    if re.match(r"^(https?://)", url) is None:
        url = "https://" + url
    if _URL_SHAPE.search(url) is None:
        return fallback
    try:
        parsed = urlparse(str(url))
        host = parsed.hostname
        _ = parsed.port  # new URL() rejects malformed and out-of-range ports; port zero is valid.
    except ValueError:
        return fallback
    if not host or any(char.isspace() or char in "%/#?@\\" or ord(char) < 32 for char in unquote(host)):
        return fallback
    origin = f"{parsed.scheme}://{parsed.netloc.rsplit('@', 1)[-1]}"
    position = url.find("?")
    if position > 0:
        has_path = False
        while position > 0 and position > len(origin) - 1 and not has_path:
            if url[position] == "/":
                has_path = True
            position -= 1
        if not has_path and position > 0:
            url = url[: position + 1] + "/" + url[position + 1 :]
        elif not has_path:
            return fallback
    try:
        url.encode("utf-8")
    except UnicodeEncodeError:
        return fallback
    # An already percent-encoded URL may still contain literal XML control characters.
    return "".join(quote(char, safe="") if ord(char) < 32 or char in "\ufffe\uffff" else char for char in url)


def hyperlink_reference(value: Any, index: Any = 0) -> str:
    del value
    return f"CarbotekHyperlinkId{int(index)}"
