# pyright: strict, reportPrivateUsage=false
"""Immutable public rendering configuration."""

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from carbotek.errors import ValidationError

DEFAULT_LANG = "ru"
DEFAULT_TIMEZONE = "Europe/Moscow"
DEFAULT_CURRENCY: Mapping[str, str] = MappingProxyType({"source": "RUB", "target": "RUB"})


@dataclass(frozen=True, slots=True)
class RenderOptions:
    """Configuration shared by a single render, never mutated by formatters or adapters."""

    complement: Mapping[str, Any] = field(default_factory=dict[str, Any])
    # Defaults of the Russian-locale document service.
    lang: str = DEFAULT_LANG
    timezone: str = DEFAULT_TIMEZONE
    use_high_precision_arithmetic: bool = False
    export_formatted_values_as_text: bool = False
    translations: Mapping[str, Mapping[str, str]] = field(default_factory=dict[str, Mapping[str, str]])
    enum: Mapping[str, Mapping[Any, Any]] = field(default_factory=dict[str, Mapping[Any, Any]])
    currency: Mapping[str, Any] = field(default_factory=lambda: dict(DEFAULT_CURRENCY))
    variable_str: str = ""

    def __post_init__(self) -> None:
        """Reject unknown IANA zones independently from the host operating system."""
        for name in ("lang", "timezone", "variable_str"):
            if type(getattr(self, name)) is not str:
                raise ValidationError(f"{name} must be a string")
        for name in ("use_high_precision_arithmetic", "export_formatted_values_as_text"):
            if type(getattr(self, name)) is not bool:
                raise ValidationError(f"{name} must be a boolean")
        for name in ("complement", "translations", "enum", "currency"):
            value: Any = getattr(self, name)
            if not isinstance(value, Mapping):
                raise ValidationError(f"{name} must be a mapping")
            copied: dict[Any, Any] = {key: _copy_value(item) for key, item in cast(Mapping[Any, Any], value).items()}
            if name in {"translations", "enum"}:
                if any(not isinstance(entries, Mapping) for entries in copied.values()):
                    raise ValidationError(f"{name} entries must be mappings")
                copied = {key: MappingProxyType(dict(entries)) for key, entries in copied.items()}
            if name == "currency":
                copied = _currency(copied)
            object.__setattr__(self, name, MappingProxyType(copied))
        object.__setattr__(self, "lang", self.lang.lower())
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValidationError(f"Unknown timezone: {self.timezone}") from error

    @classmethod
    def create(cls, **values: Any) -> RenderOptions:
        """Copy caller mappings at the public API boundary."""
        try:
            return cls._create(**values)
        except (TypeError, ValueError, AttributeError) as error:
            raise ValidationError(str(error)) from error

    @classmethod
    def _create(cls, **values: Any) -> RenderOptions:
        return cls(**values)


def _copy_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _copy_value(item) for key, item in cast(Mapping[Any, Any], value).items()}
    if isinstance(value, list):
        return [_copy_value(item) for item in cast(list[Any], value)]
    if isinstance(value, tuple):
        return tuple(_copy_value(item) for item in cast(tuple[Any, ...], value))
    return deepcopy(value)


def _currency(value: dict[Any, Any]) -> dict[Any, Any]:
    copied = {**DEFAULT_CURRENCY, **value}
    for key in ("source", "target"):
        if copied[key] is not None and not isinstance(copied[key], str):
            raise ValidationError(f"currency.{key} must be a string or null")
    if "rates" in copied:
        rates = copied["rates"]
        if not isinstance(rates, Mapping):
            raise ValidationError("currency.rates must be a mapping")
        entries = cast(Mapping[Any, Any], rates)
        if any(not isinstance(key, str) for key in entries):
            raise ValidationError("currency.rates keys must be strings")
        copied["rates"] = MappingProxyType(dict(entries))
    return copied
