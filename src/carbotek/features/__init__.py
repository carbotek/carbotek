# pyright: strict, reportPrivateUsage=false
"""DOCX features that require coordinated OPC package post-processing."""

from .color import ColorBinding
from .html import HtmlSubstitution
from .hyperlinks import HyperlinkSubstitution
from .image import ImageSubstitution
from .native_chart import NativeChartSubstitution
from .template_options import extract_template_options

__all__ = [
    "ColorBinding",
    "HtmlSubstitution",
    "HyperlinkSubstitution",
    "ImageSubstitution",
    "NativeChartSubstitution",
    "extract_template_options",
]
