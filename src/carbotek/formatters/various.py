# pyright: strict, reportPrivateUsage=false
"""Portable miscellaneous formatters retained for DOCX rendering."""

import json
import math
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from .condition import JS_UNDEFINED
from .number import number_string
from .registry import FormatterMetadata, FormatterRegistry


def register(registry: FormatterRegistry) -> None:
    registry.register("formatR", format_region, FormatterMetadata(uses_render_options=True))


def format_region(value: Any, *, render_options: Any = None) -> Any:
    """``formatR``: ``countries.getName(regions[value] ?? value, lang)`` of i18n-iso-countries."""
    if value is None or value is JS_UNDEFINED:
        return value
    language = getattr(render_options, "lang", "en")
    if not isinstance(language, str):
        return value  # JS: this.lang.split throws, the formatter returns the value
    data = _region_data()
    alias = data["aliases"].get(number_string(value))
    code = _to_alpha2(alias if alias is not None else value, data)
    if code is None:
        return value
    return data["names"].get(language.split("-", 1)[0].lower(), {}).get(code, value)


def _to_alpha2(code: Any, data: dict[str, dict[str, Any]]) -> str | None:
    """``toAlpha2`` of i18n-iso-countries: numeric, alpha-2 or alpha-3 code to alpha-2."""
    if isinstance(code, str):
        if _DIGITS.fullmatch(code):
            return data["numeric"].get(_numeric_code(code))
        if len(code) == 2:
            return code.upper()
        if len(code) == 3:
            return data["alpha3"].get(code.upper())
        return None
    if isinstance(code, int | float) and not isinstance(code, bool):
        return data["numeric"].get(_numeric_code(code))
    return None


def _numeric_code(code: str | float) -> str:
    """``formatNumericCode``: ``String("000" + (code ? code : "")).slice(-3)``."""
    falsy = code in (0, "") or (isinstance(code, float) and math.isnan(code))
    return ("000" + ("" if falsy else number_string(code)))[-3:]


_DIGITS = re.compile(r"[0-9]*")


@lru_cache(maxsize=1)
def _region_data() -> dict[str, dict[str, Any]]:
    return json.loads(Path(__file__).with_name("region_names.json").read_text(encoding="utf-8"))
