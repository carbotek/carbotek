# pyright: strict, reportPrivateUsage=false
"""ECharts marker metadata and SVG font compatibility helpers."""

import re
from typing import Any

from carbotek.typing_helpers import is_dict

from .registry import FormatterMetadata, FormatterRegistry


def register(registry: FormatterRegistry) -> None:
    registry.register("chart", chart, FormatterMetadata(mutates_input=True))
    registry.register("_convertSingleLineFontStyleSVG", convert_font)


def chart(value: Any) -> Any:
    if is_dict(value):
        value["type"] = "echarts@v5a" if value.get("type") == "echarts@v5a" else "echarts@v5"
    return value


def convert_font(value: Any) -> str:
    return convert_font_shorthand(value) if isinstance(value, str) else ""


def convert_font_shorthand(value: str) -> str:
    """Expand ECharts SVG font shorthand as the v5a formatter does."""

    def replace(match: re.Match[str]) -> str:
        parts = match.group(1).split(" ")
        if len(parts) > 4:
            parts[3] = " ".join(parts[3:])
        return (
            'style="font-style:'
            + parts[0]
            + ";font-weight:"
            + parts[1]
            + ";font-size:"
            + parts[2]
            + ";font-family:"
            + parts[3]
        )

    return re.sub(r'style="font: ([^;"]+)', replace, value)
