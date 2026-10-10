# pyright: strict, reportPrivateUsage=false
"""Formatter composition root."""

from functools import lru_cache

from .aggregator import register as register_aggregator
from .array import register as register_array
from .barcode import register as register_barcode
from .chart import register as register_chart
from .color import register as register_color
from .condition import register as register_condition
from .date import register as register_date
from .docx import register as register_docx
from .number import register as register_number
from .optional import register as register_optional
from .registry import FormatterRegistry, MarkerContext
from .ru import register as register_ru
from .set import register as register_set
from .string import register as register_string
from .various import register as register_various


def default_registry() -> FormatterRegistry:
    """Build the standard formatter set without global side effects."""
    registry = FormatterRegistry()
    register_array(registry)
    register_barcode(registry)
    register_chart(registry)
    register_color(registry)
    register_condition(registry)
    register_aggregator(registry)
    register_string(registry)
    register_number(registry)
    register_optional(registry)
    register_date(registry)
    register_docx(registry)
    register_ru(registry)
    register_set(registry)
    register_various(registry)
    return registry


@lru_cache(maxsize=1)
def shared_registry() -> FormatterRegistry:
    """The standard formatter set built once per process for the render hot path (read-only use)."""
    return default_registry()


__all__ = ["FormatterRegistry", "MarkerContext", "default_registry", "shared_registry"]
