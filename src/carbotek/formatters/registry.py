# pyright: strict, reportPrivateUsage=false
"""Explicit formatter registry; behavior metadata lives next to each formatter."""

import inspect
import json
from collections.abc import Callable, MutableMapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from carbotek.errors import CarbotekError, FormatterError
from carbotek.util import find_closest

from .jsnumber import JS_UNDEFINED

type Formatter = Callable[..., Any]
# Object.keys(carbotek.formatters) after lib/unified.js init() (exported from the JS engine).
JS_REGISTRATION_ORDER: tuple[str, ...] = tuple(
    json.loads(Path(__file__).with_name("registration_order.json").read_text(encoding="utf-8"))
)


class UnknownFormatterError(FormatterError):
    """A marker names a formatter that is not registered."""


@dataclass(slots=True)
class MarkerContext:
    """Mutable state scoped to a single document render and one marker pipeline."""

    data: MutableMapping[str, Any]
    complement: MutableMapping[str, Any]
    parent: Any = None
    set_database: dict[int, dict[Any, Any]] = field(default_factory=dict[int, dict[Any, Any]])
    default_url_on_error: str = ""
    unique_id: Any = 0
    modified_currency_target: Any = None
    # Updates of the template's own SVG (svgUpdate); None: the template is not an SVG (DOCX).
    svg_updates: dict[str, dict[str, list[tuple[str, Any]]]] | None = None
    aggregation_database: dict[str, Any] = field(default_factory=dict[str, Any])
    # JS adds the items of a ``[]`` aggregate from the last one (the builder loop runs backwards);
    # an aggregate inside the loop over its own array adds them in document order.
    aggregate_forward: bool = False
    marker_key: str = ""
    conditional_blocks: list[bool] = field(default_factory=list[bool])
    conditional_spans: dict[str, int] = field(default_factory=dict[str, int])
    is_barcode_image: bool = False
    bookmark_names: dict[str, int] = field(default_factory=dict[str, int])
    reserved_bookmark_names: set[str] = field(default_factory=set[str])
    extension: str = "docx"
    # JS ``this.isConditionTrue`` / ``stopPropagation`` / ``isHidden`` of the formatter protocol.
    is_condition_true: Any = None
    stop_propagation: bool = False
    hidden_flag: int | None = None

    @property
    def is_hidden(self) -> bool:
        """Whether the current document position is inside a hidden conditional block."""
        return any(self.conditional_blocks)


@dataclass(frozen=True, slots=True)
class FormatterMetadata:
    """Capabilities used by compilation/execution rather than hidden function attributes."""

    mutates_input: bool = False
    accepts_math: bool = False
    can_inject_xml: bool = False
    is_conditional: bool = False
    updates_propagation: bool = False  # JS isUpdatingPropagation: stops the formatter chain
    uses_render_options: bool = False
    uses_marker_context: bool = False


@dataclass(frozen=True, slots=True)
class RegisteredFormatter:
    """A formatter implementation and its declared execution behavior."""

    function: Formatter
    metadata: FormatterMetadata
    max_arguments: int | None = None  # positional arguments after the value; None = variadic
    required_arguments: int = 0


class FormatterRegistry:
    """Dependency injected registry, preventing global mutable formatter state."""

    def __init__(self) -> None:
        self._formatters: dict[str, RegisteredFormatter] = {}

    def register(self, name: str, function: Formatter, metadata: FormatterMetadata | None = None) -> None:
        """Register exactly one formatter name."""
        if name in self._formatters:
            raise ValueError(f"Formatter already registered: {name}")
        self._formatters[name] = RegisteredFormatter(
            function, metadata or FormatterMetadata(), _max_arguments(function), _required_arguments(function)
        )

    def apply(
        self,
        name: str,
        value: Any,
        arguments: tuple[Any, ...],
        *,
        render_options: Any | None = None,
        marker_context: MarkerContext | None = None,
    ) -> Any:
        """Invoke a formatter or raise a typed rendering error."""
        formatter = self._formatters.get(name)
        if formatter is None:
            raise self.unknown(name)
        keywords = {}
        if formatter.metadata.uses_render_options:
            keywords["render_options"] = render_options
        if formatter.metadata.uses_marker_context:
            keywords["marker_context"] = marker_context
        arguments = self.prepare_arguments(name, arguments)
        try:
            return formatter.function(value, *arguments, **keywords)
        except CarbotekError:
            raise
        except (ValueError, TypeError, ArithmeticError, LookupError) as error:
            # Formatter input failures belong to the typed rendering-error contract.
            raise FormatterError(str(error)) from error

    def prepare_arguments(self, name: str, arguments: tuple[Any, ...]) -> tuple[Any, ...]:
        """JS ignores extra arguments and supplies undefined for missing required parameters."""
        registered = self._formatters[name]
        if registered.max_arguments is not None:
            arguments = arguments[: registered.max_arguments]
        return arguments + (JS_UNDEFINED,) * max(registered.required_arguments - len(arguments), 0)

    def unknown(self, name: str) -> UnknownFormatterError:
        """The error of ``builder.getFormatterString`` for a formatter name that does not exist."""
        suggestion = find_closest(name, [item for item in JS_REGISTRATION_ORDER if item in self._formatters])
        return UnknownFormatterError(f'Formatter "{name}" does not exist. Do you mean "{suggestion}"?')

    def __contains__(self, name: object) -> bool:
        return name in self._formatters

    @property
    def names(self) -> frozenset[str]:
        """Expose registered names for validation and test tooling."""
        return frozenset(self._formatters)

    def registration(self, name: str) -> RegisteredFormatter | None:
        """Read immutable execution metadata without exposing the mutable registry."""
        return self._formatters.get(name)

    def metadata(self, name: str) -> FormatterMetadata:
        """Declared behavior of a formatter; an unknown name has the default (no flags)."""
        registered = self._formatters.get(name)
        return registered.metadata if registered is not None else FormatterMetadata()

    def function(self, name: str) -> Formatter:
        """The implementation registered under ``name`` (aliases share one)."""
        return self._formatters[name].function


def _max_arguments(function: Formatter) -> int | None:
    try:
        parameters = list(inspect.signature(function).parameters.values())
    except TypeError, ValueError:
        return None
    if any(parameter.kind is inspect.Parameter.VAR_POSITIONAL for parameter in parameters):
        return None
    positional = [
        parameter
        for parameter in parameters
        if parameter.kind in {inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD}
    ]
    return max(len(positional) - 1, 0)


def _required_arguments(function: Formatter) -> int:
    try:
        parameters = list(inspect.signature(function).parameters.values())
    except TypeError, ValueError:
        return 0
    required = [
        parameter
        for parameter in parameters
        if parameter.kind in {inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD}
        and parameter.default is inspect.Parameter.empty
    ]
    return max(len(required) - 1, 0)
