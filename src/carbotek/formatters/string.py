# pyright: strict, reportPrivateUsage=false
"""Format-agnostic string formatters ported without document dependencies."""

import math
import re
import unicodedata
from decimal import Decimal
from hashlib import md5
from json import dumps
from math import floor, fmod, inf, isnan
from typing import Any

from carbotek.typing_helpers import is_mapping, is_sequence

from .condition import JS_UNDEFINED, _js_string
from .jsnumber import js_to_number
from .registry import FormatterMetadata, FormatterRegistry


def register(registry: FormatterRegistry) -> None:
    """Register string formatters in a supplied composition root."""
    registry.register("lowerCase", lower_case)
    registry.register("upperCase", upper_case)
    registry.register("ucFirst", uc_first)
    registry.register("firstUpper", uc_first)
    registry.register("capitalize", uc_first)
    registry.register("ucWords", uc_words)
    context_metadata = FormatterMetadata(uses_render_options=True)
    registry.register("convEnum", conv_enum, context_metadata)
    registry.register("prepend", prepend)
    registry.register("append", append)
    registry.register("replace", replace)
    registry.register("unaccent", unaccent)
    registry.register("substr", substr)
    registry.register("slice", substr)
    registry.register("padl", pad_left)
    registry.register("padr", pad_right)
    registry.register("split", split)
    registry.register("ifEmpty", if_empty, FormatterMetadata(is_conditional=True, updates_propagation=True))
    registry.register("print", printable)
    registry.register("hide", hide)
    registry.register("printJSON", print_json)
    registry.register("md5", md5_hash)
    registry.register("ellipsis", ellipsis)
    registry.register("neutralForArrayFilter", neutral_for_array_filter)
    registry.register("convCRLF", conv_crlf)
    registry.register("convCRLFH", conv_crlf_html)
    registry.register("preserveCharRef", preserve_char_ref)
    registry.register("escapeForXml", escape_for_xml)
    registry.register("t", translate, context_metadata)
    registry.register("len", length)


def lower_case(value: Any) -> Any:
    return value.lower() if isinstance(value, str) else value


def upper_case(value: Any) -> Any:
    return value.upper() if isinstance(value, str) else value


def uc_first(value: Any) -> Any:
    return value[:1].upper() + value[1:] if isinstance(value, str) else value


def uc_words(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    return re.sub(r"(?:^|\s)\S", lambda match: match.group().upper(), value)


def conv_enum(value: Any, enum_type: Any, *, render_options: Any = None) -> Any:
    """Map a value through the report's enum table, retaining unknown values."""
    values = getattr(render_options, "enum", {})
    mapping = values.get(_js_string(enum_type), {})
    result = mapping.get(_js_string(value), JS_UNDEFINED)
    return value if result is JS_UNDEFINED else result


def translate(value: Any, *, render_options: Any = None) -> Any:
    translations = getattr(render_options, "translations", {})
    language = getattr(render_options, "lang", "en")
    result = translations.get(language, {}).get(_js_string(value), JS_UNDEFINED)
    return value if result is JS_UNDEFINED or result is None else result


def length(value: Any) -> int:
    return len(value) if isinstance(value, str) or is_sequence(value) else 0


def _or_empty(value: Any) -> str:
    """JS ``value ?? ''`` followed by string concatenation."""
    return "" if value is None or value is JS_UNDEFINED else _js_string(value)


def prepend(value: Any, prefix: Any = "", *_unused: Any) -> str:
    return f"{_or_empty(prefix)}{_or_empty(value)}"


def append(value: Any, suffix: Any = "", *_unused: Any) -> str:
    return f"{_or_empty(value)}{_or_empty(suffix)}"


def replace(value: Any, search: Any, replacement: Any = "") -> Any:
    if search == "":
        return value
    return _or_empty(value).replace(_js_string(search), _or_empty(replacement))


def unaccent(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    return "".join(
        character for character in unicodedata.normalize("NFD", value) if not unicodedata.combining(character)
    )


def substr(value: Any, start: Any = 0, end: Any | None = None, preserve_words: Any = False) -> Any:
    """Slice a string, including whitespace-aware block slicing."""
    if not isinstance(value, str):
        return value
    if preserve_words not in (True, "true", "last"):
        return _js_slice(value, start, end)
    begin_number = _parse_js_int(start)
    end_number = _parse_js_int(end)
    end_number = max(end_number, 0)
    begin_number = max(begin_number, 0)
    width = end_number - begin_number
    if isnan(width) or isnan(begin_number):
        preceding_blocks = float("nan")
    elif width == 0:
        preceding_blocks = inf
    else:
        preceding_blocks = floor(begin_number / width)
    block_count = 0
    block_start: float = 0
    remainder = fmod(begin_number, width) if width and not isnan(width) and not isnan(begin_number) else float("nan")
    if remainder > 0:
        block_start = -(width - remainder)
        preceding_blocks += 1
    boundary = 0
    previous_whitespace = bool(value and value[0].isspace())
    position = 0
    for position, _character in enumerate(value):
        next_whitespace = value[position + 1].isspace() if position + 1 < len(value) else True
        if next_whitespace or previous_whitespace:
            boundary = position + 1
        if position - block_start + 1 >= width:
            block_count += 1
            if block_start == boundary:
                boundary = position + 1
            if block_count > preceding_blocks:
                break
            block_start = boundary
        previous_whitespace = next_whitespace
    else:
        position = len(value)
    if position == len(value) and block_count < preceding_blocks:
        return ""
    if preserve_words == "last":
        boundary = len(value)
    return _js_slice(value, block_start, boundary)


def _parse_js_int(value: Any) -> float:
    match = re.match(r"^\s*[+-]?\d+", str(value))
    return float(match.group()) if match else float("nan")


def _js_slice(value: str, start: Any, end: Any | None) -> str:
    def index(number: Any, default: int) -> int:
        if number is None:
            return default
        numeric = js_to_number(number)
        parsed = int(numeric) if math.isfinite(numeric) else (len(value) if numeric > 0 else 0)
        return max(0, min(len(value), len(value) + parsed if parsed < 0 else parsed))

    begin = index(start, 0)
    stop = index(end, len(value))
    return value[begin : max(begin, stop)]


def pad_left(value: Any, target_length: Any, padding: Any = " ") -> Any:
    padding = " " if padding is JS_UNDEFINED else padding
    return _pad(value, _pad_length(target_length), padding, left=True)


def pad_right(value: Any, target_length: Any, padding: Any = " ") -> Any:
    padding = " " if padding is JS_UNDEFINED else padding
    return _pad(value, _pad_length(target_length), padding, left=False)


def _pad_length(value: Any) -> int:
    number = js_to_number(value)
    if number == math.inf:
        return 10_001
    return int(number) if math.isfinite(number) else 0


def split(value: Any, separator: Any = None, *_unused: Any) -> Any:
    if value is None or value is JS_UNDEFINED:
        return value
    source = _js_string(value)
    if separator is JS_UNDEFINED:
        return [source]  # String.prototype.split(undefined)
    needle = _js_string(separator)
    return list(source) if needle == "" else source.split(needle)


def if_empty(value: Any, replacement: Any = "") -> Any:
    return replacement if value is None or value == "" or value == [] else value


def printable(_value: Any, message: Any = None, *_unused: Any) -> Any:
    return message


def print_json(value: Any) -> Any:
    """``JSON.stringify``: undefined stays undefined, non-finite numbers are ``null``."""
    if value is JS_UNDEFINED:
        return JS_UNDEFINED
    return dumps(_json_value(value), ensure_ascii=False, separators=(",", ":"))


def _json_value(value: Any) -> Any:
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        return int(value) if value.is_integer() and abs(value) < 1e21 else value
    if isinstance(value, Decimal):
        return _json_value(float(value))
    if is_mapping(value):
        return {str(key): _json_value(item) for key, item in value.items() if item is not JS_UNDEFINED}
    if is_sequence(value):
        return [None if item is JS_UNDEFINED else _json_value(item) for item in value]
    return value


def hide(_value: Any) -> str:
    """Suppress a field while retaining its containing OOXML element."""
    return ""


def md5_hash(value: Any) -> str:
    return md5(_js_string(value).encode("utf-8"), usedforsecurity=False).hexdigest()


def ellipsis(value: Any, maximum: Any, *_unused: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        limit = int(maximum)
    except TypeError, ValueError:
        return "..."
    return value if len(value) <= limit else f"{_js_slice(value, 0, limit)}..."


def neutral_for_array_filter(value: Any) -> Any:
    return value


# convCRLF injects XML in JS (``canInjectXML``): the renderer turns this marker into ``<w:br/>``.
LINE_BREAK = " br"


def conv_crlf(value: Any) -> Any:
    return re.sub(r"\r?\n", LINE_BREAK, value) if isinstance(value, str) else value


def conv_crlf_html(value: Any) -> Any:
    """Convert LF/CRLF to HTML breaks without coercing non-string values."""
    return value.replace("\r\n", "<br>").replace("\n", "<br>") if isinstance(value, str) else value


def preserve_char_ref(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    return re.sub(r"&amp;#([0-9]{1,7}|x[0-9a-fA-F]{1,6});", r"&#\1;", value)


def escape_for_xml(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    escaped = value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return strip_xml_controls(escaped)


def strip_xml_controls(value: str) -> str:
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", value)


def _pad(value: Any, target_length: int, padding: Any, *, left: bool) -> Any:
    if target_length > 10_000:
        raise ValueError("The target length cannot be greater than 10000")
    if not isinstance(value, str | int | float) or isinstance(value, bool):
        return value
    value = _js_string(value)
    if len(value) >= target_length:
        return value
    pad_text = "null" if padding is None else str(padding)
    if not pad_text:
        return value
    fill = pad_text * (target_length - len(value))
    fill = fill[: target_length - len(value)]
    return f"{fill}{value}" if left else f"{value}{fill}"
