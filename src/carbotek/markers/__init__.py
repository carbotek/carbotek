# pyright: strict, reportPrivateUsage=false
"""Carbotek marker scanning and syntax parsing, independent of document formats."""

from .scan import MarkerOccurrence, scan
from .syntax import FormatterCall, Marker, MarkerSyntaxError, parse
from .variables import VariableDeclaration, VariableEnvironment, VariableExpressionError, parse_declaration, substitute

__all__ = [
    "FormatterCall",
    "Marker",
    "MarkerOccurrence",
    "MarkerSyntaxError",
    "VariableDeclaration",
    "VariableEnvironment",
    "VariableExpressionError",
    "parse",
    "parse_declaration",
    "scan",
    "substitute",
]
