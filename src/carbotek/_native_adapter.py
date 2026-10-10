# pyright: strict
"""Optional compiled section backend; the public rendering API stays in Python.

``CARBOTEK_NATIVE`` is read once, when carbotek is imported: ``0``/``false``/``off``/``no`` keeps
every render on the Python path, ``1``/``true``/``on``/``yes`` turns a missing extension into an
import error, and any other value (the default) uses the extension when it is installed.
"""

import os
import re
from collections.abc import Callable
from importlib.metadata import PackageNotFoundError, version
from typing import Any, Protocol

# Operation codes of a native section slot part, shared with native/src/lib.rs (OPERATIONS).
OP_LITERAL, OP_FIELD, OP_ISO_DATE, OP_RU_NUMBER, OP_IF_EQUAL, OP_CENTS = range(6)
OPERATIONS = {
    "literal": OP_LITERAL,
    "field": OP_FIELD,
    "iso_date": OP_ISO_DATE,
    "ru_number": OP_RU_NUMBER,
    "if_equal": OP_IF_EQUAL,
    "cents": OP_CENTS,
}

type SlotPart = tuple[int, str, str, str, str]


class PlainSection(Protocol):
    def render(self, rows: list[object]) -> str | None: ...
    def render_bytes(self, rows: list[object]) -> bytes | None: ...


class _Native(Protocol):
    """The functions of ``carbotek._native`` (see ``_native.pyi``)."""

    def PlainSection(self, literals: list[str], slots: list[tuple[int, list[SlotPart]]]) -> PlainSection: ...
    def digit_currency_cents(self, value: Any) -> str | None: ...
    def join_utf8(self, parts: list[str | bytes]) -> bytes: ...
    def decode_data_uri(self, value: Any) -> bytes | None: ...


def _load(setting: str) -> _Native | None:
    if setting in {"0", "false", "off", "no"}:
        return None
    try:
        from carbotek import _native  # pyright: ignore[reportMissingModuleSource] - optional extension
    except ImportError:
        if setting in {"1", "true", "on", "yes"}:
            raise
        return None
    try:
        installed_version = version("carbotek")
    except PackageNotFoundError:
        installed_version = None
    native_version = getattr(_native, "PACKAGE_VERSION", None)
    compatible = getattr(_native, "API_VERSION", None) == 2 and getattr(_native, "OPERATIONS", None) == OPERATIONS
    if installed_version is not None:
        normalized = re.sub(
            r"-(alpha|beta|rc)\.?(\d+)$",
            lambda match: {"alpha": "a", "beta": "b", "rc": "rc"}[match[1]] + match[2],
            native_version or "",
        )
        compatible = compatible and normalized == installed_version
    if not compatible:
        if setting in {"1", "true", "on", "yes"}:
            raise ImportError(
                "carbotek._native is incompatible with this Python adapter; reinstall or rebuild carbotek"
            )
        return None
    return _native


# Tests switch backends by replacing this attribute (monkeypatch), not the environment.
_native: _Native | None = _load(os.environ.get("CARBOTEK_NATIVE", "").strip().lower())


def native_available() -> bool:
    return _native is not None


def compile_plain_section(literals: list[str], slots: list[tuple[int, list[SlotPart]]]) -> PlainSection | None:
    native = _native
    return native.PlainSection(literals, slots) if native is not None else None


def native_digit_currency_cents(value: Any) -> str | None:
    native = _native
    return native.digit_currency_cents(value) if native is not None else None


def native_utf8_joiner() -> Callable[[list[str | bytes]], bytes] | None:
    """``join_utf8``, chosen before any section field is evaluated; None for the Python path."""
    native = _native
    return native.join_utf8 if native is not None else None


def native_decode_data_uri(value: Any) -> bytes | None:
    native = _native
    return native.decode_data_uri(value) if native is not None else None
