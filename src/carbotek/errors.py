# pyright: strict, reportPrivateUsage=false
"""Typed rendering failures with transport-independent error codes."""

from typing import ClassVar


class CarbotekError(Exception):
    """Base error for known rendering failures."""

    code: ClassVar[str] = "INTERNAL_ERROR"


class ValidationError(CarbotekError):
    """The request or the render options are invalid."""

    code = "VALIDATION_ERROR"


class TemplateError(CarbotekError):
    """The template cannot be read or rendered."""

    code = "TEMPLATE_ERROR"


class MarkerError(TemplateError):
    """A marker is malformed or cannot be placed (syntax, ``[i]``/``[i+1]`` pairing, blocks)."""


class FormatterError(TemplateError, ValueError):
    """A formatter does not exist or rejected its value or arguments.

    It also derives from ``ValueError`` because formatter implementations historically signalled
    invalid input with ``ValueError`` and some adapters still catch that base class.
    """


class FeatureUnavailable(CarbotekError):
    """A feature is not available in this package (for example ECharts charts or PDF from XLSX)."""

    code = "FEATURE_UNAVAILABLE"


class ConvertError(CarbotekError):
    """DOCX-to-PDF conversion failed."""

    code = "CONVERTER_ERROR"


class InvalidTemplateError(TemplateError):
    """The supplied bytes are not a valid DOCX template."""


class CorruptedTemplateError(InvalidTemplateError):
    """The OPC package contains unsafe or malformed content."""


class TemplateTooLargeError(InvalidTemplateError):
    """The declared or actual uncompressed package size exceeds the limit."""


class MarkerResolutionError(MarkerError):
    """A recognized marker cannot be resolved with the supplied rendering context."""
