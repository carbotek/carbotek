# pyright: strict, reportPrivateUsage=false
"""Portable color formatter primitives shared by DOCX color bindings."""

import re
from colorsys import hls_to_rgb
from typing import Any

from carbotek.typing_helpers import is_list, is_mapping

from .condition import js_truthy
from .registry import FormatterRegistry

_HEX = re.compile(r"^#[0-9a-f]{6}$")
_BIND_HEX = re.compile(r"^#?[0-9a-fA-F]{3,6}$")
_NAMED_COLORS = {
    "red": "ff0000",
    "green": "00ff00",
    "blue": "0000ff",
    "magenta": "ff00ff",
    "yellow": "ffff00",
    "cyan": "00ffff",
    "darkBlue": "000080",
    "darkCyan": "008080",
    "darkGreen": "008000",
    "darkMagenta": "800080",
    "darkRed": "800000",
    "darkYellow": "808000",
    "darkGray": "808080",
    "lightGray": "c0c0c0",
    "black": "000000",
    "transparent": "",
    "white": "ffffff",
}


def register(registry: FormatterRegistry) -> None:
    registry.register("color", color)
    registry.register("shadeColor", shade_color)
    registry.register("colorToDocx", color_to_docx)
    registry.register("getAndConvertColorDocx", get_and_convert_color_docx)


def color(value: Any, _scope: Any = "p", _kind: Any = "text") -> str:
    if isinstance(value, str):
        candidate = value.lower()
        candidate = candidate if candidate.startswith("#") else f"#{candidate}"
        if _HEX.fullmatch(candidate):
            return candidate
    return "#888888"


def shade_color(value: Any, amount: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        offset = int(amount)
        packed = int(value.removeprefix("#") or "0", 16)
    except ValueError:
        return value
    channels = [max(0, min(255, (packed >> shift & 255) + offset)) for shift in (16, 8, 0)]
    return f"#{channels[0]:02x}{channels[1]:02x}{channels[2]:02x}"


def color_to_docx(value: Any) -> Any:
    if isinstance(value, str):
        return value[1:]
    if is_list(value):
        return value[1:]
    raise TypeError("colorToDocx requires a string or array")


def get_and_convert_color_docx(value: Any, color_format: Any = "hexa", _target: Any = "color") -> str:
    if not (js_truthy(value) and js_truthy(color_format) and js_truthy(_target)):
        return ""
    kind = str(color_format)
    if kind in {"#hexa", "hexa"}:
        if not isinstance(value, str):
            return ""
        candidate = value.removeprefix("#") if kind == "#hexa" else value
        return candidate if _BIND_HEX.fullmatch(candidate) else ""
    if kind == "color":
        if not isinstance(value, str) or value not in _NAMED_COLORS:
            raise ValueError(f"Unknown named color: {value}")
        if _target == "textBackgroundColor":
            if value == "transparent":
                raise ValueError("DOCX highlight cannot be transparent")
            return value
        return _NAMED_COLORS[value]
    if kind in {"rgb", "hsl"}:
        if not is_mapping(value):
            return ""
        try:
            if kind == "rgb":
                channels = (int(value[name]) & 255 for name in ("r", "g", "b"))
            else:
                hue, saturation, lightness = (float(value[name]) for name in ("h", "s", "l"))
                if hue > 1 or saturation > 1 or lightness > 1:
                    hue, saturation, lightness = hue / 360, saturation / 100, lightness / 100
                channels = (int(component * 255 + 0.5) for component in hls_to_rgb(hue, lightness, saturation))
            return "".join(f"{channel:02x}" for channel in channels)
        except KeyError, TypeError, ValueError:
            return ""
    return ""
