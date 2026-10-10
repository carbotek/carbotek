# pyright: strict, reportPrivateUsage=false
"""Text barcode encodings and image barcode descriptors."""

import json
import re
from typing import Any

from .jsnumber import js_float_string
from .registry import FormatterMetadata, FormatterRegistry, MarkerContext

_HEX_COLOR = re.compile(r"^#?[0-9A-Fa-f]{6}$")


def register(registry: FormatterRegistry) -> None:
    metadata = FormatterMetadata(uses_marker_context=True)
    registry.register("isImage", is_image, metadata)
    registry.register("barcode", barcode, metadata)
    registry.register("initBarcodeValuesBasedOnType", initial_values)


def is_image(value: Any, *, marker_context: MarkerContext | None = None) -> Any:
    if marker_context is not None:
        marker_context.is_barcode_image = True
    return value


def barcode(value: Any, kind: Any = None, *arguments: Any, marker_context: MarkerContext | None = None) -> str:
    if marker_context is not None and marker_context.is_barcode_image:
        marker_context.is_barcode_image = False
        return _image_descriptor(value, kind, arguments, marker_context.extension)
    encoders = {"ean8": _ean8, "ean13": _ean13, "ean128": _ean128, "code128": _ean128, "code39": _code39}
    encoder = encoders.get(kind)
    return encoder(value) if encoder is not None else ""


def initial_values(kind: Any) -> dict[str, Any]:
    values: dict[str, Any] = {"scale": 3, "rotate": "N", "includetext": True, "textxalign": "center"}
    if kind == "mailmark":
        values["type"] = "9"
    elif kind == "rectangularmicroqrcode":
        values["version"] = "R17x139"
    elif kind == "gs1-cc":
        values.update(ccversion="b", cccolumns=4)
    elif kind == "maxicode":
        values.update(mode=2, parse=True)
    return values


def _ean8(value: Any) -> str:
    if value is None:
        raise TypeError("Cannot read properties of null")
    if not isinstance(value, str) or len(value) != 8 or not value.isascii() or not value.isdigit():
        return ""
    return (
        ":"
        + "".join(chr(17 + ord(char)) for char in value[:4])
        + "*"
        + "".join(chr(49 + ord(char)) for char in value[4:])
        + "+"
    )


def _ean13(value: Any) -> str:
    if value is None:
        raise TypeError("Cannot read properties of null")
    if not isinstance(value, str) or len(value) != 13 or not value.isascii() or not value.isdigit():
        return ""
    leading = int(value[0])
    output = value[0] + chr(17 + ord(value[1]))
    parity = (
        leading <= 3,
        leading in {0, 4, 7, 8},
        leading in {0, 1, 4, 5, 9},
        leading in {0, 2, 5, 6, 7},
        leading in {0, 3, 6, 8, 9},
    )
    for index, left in enumerate(parity, start=2):
        output += chr((17 if left else 27) + ord(value[index]))
    return output + "*" + "".join(chr(49 + ord(char)) for char in value[7:]) + "+"


def _code39(value: Any) -> str:
    if not isinstance(value, str) or not value or not re.fullmatch(r"[ A-Z$%+./0-9-]+", value):
        return ""
    return f"*{value}*"


def _ean128(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or any(not (32 <= ord(char) <= 126 or ord(char) == 203) for char in value)
    ):
        return ""
    output = ""
    index = 0
    text_mode = True
    while index < len(value):
        if text_mode:
            run = 4 if index == 0 or index + 4 == len(value) else 6
            if (
                value[index : index + run].isascii()
                and len(value[index : index + run]) == run
                and value[index : index + run].isdigit()
            ):
                output += chr(210 if index == 0 else 204)
                text_mode = False
            elif index == 0:
                output = chr(209)
        if not text_mode:
            pair = value[index : index + 2]
            if len(pair) == 2 and pair.isascii() and pair.isdigit():
                number = int(pair)
                output += chr(number + (32 if number < 95 else 105))
                index += 2
            else:
                output += chr(205)
                text_mode = True
        if text_mode:
            output += value[index]
            index += 1
    checksum = 0
    for position, char in enumerate(output):
        code = ord(char)
        code -= 32 if code < 127 else 105
        if position == 0:
            checksum = code
        checksum = (checksum + position * code) % 103
    checksum += 32 if checksum < 95 else 100
    return output + chr(checksum) + chr(211)


def _image_descriptor(value: Any, kind: Any, arguments: tuple[Any, ...], extension: str) -> str:
    options: dict[str, Any] = {}
    for argument in arguments:
        if not isinstance(argument, str):
            continue
        parts = argument.split(":")
        if len(parts) != 2:
            continue
        key, raw = parts
        parsed = _validated_option(key, raw)
        if parsed is not None:
            options[key] = parsed
    if extension == "html" and options.get("svg") is not False:
        options["svg"] = True
    return json.dumps({"bcid": kind, "text": _js_string(value), **options}, ensure_ascii=False, separators=(",", ":"))


def _validated_option(key: str, raw: str) -> Any | None:
    if key in {"svg", "includetext"} and raw in {"true", "false"}:
        return raw == "true"
    if key in {"width", "height", "scale", "textsize"}:
        try:
            number = int(float(raw))
        except ValueError:
            return None
        limit = 10 if key == "scale" else 100 if key == "textsize" else 1000
        return raw if 0 < number <= limit else None
    if key in {"textxalign", "textyalign"}:
        choices = {"left", "center", "right", "justify"} if key == "textxalign" else {"below", "center", "above"}
        return raw if raw in choices else None
    if key == "rotate" and raw.upper() in {"N", "R", "L", "I"}:
        return raw.upper()
    if key == "eclevel" and raw.upper() in {"L", "M", "Q", "H"}:
        return raw.upper()
    if key in {"barcolor", "textcolor", "backgroundcolor"} and _HEX_COLOR.fullmatch(raw):
        return raw.removeprefix("#")
    return None


def _js_string(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, float):
        return js_float_string(value)
    return str(value)
