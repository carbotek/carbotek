# pyright: strict, reportPrivateUsage=false
"""Optional SVG-based formatters with explicit errors until their backend is enabled."""

from typing import Any

from carbotek.errors import MarkerResolutionError

from .registry import FormatterRegistry


def register(registry: FormatterRegistry) -> None:
    for name in {
        "generateChartImage",
        "generateBarcodeImage",
    }:
        registry.register(name, unavailable(name))


def unavailable(name: str):
    def formatter(_value: Any, *_arguments: Any) -> Any:
        raise MarkerResolutionError(f'Formatter "{name}" requires the optional SVG rendering backend')

    return formatter
