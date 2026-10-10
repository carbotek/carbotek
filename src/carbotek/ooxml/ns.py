# pyright: strict, reportPrivateUsage=false
"""Namespace-safe OOXML helpers. Never compare source prefixes directly."""

from functools import lru_cache

from lxml.etree import QName

NAMESPACES: dict[str, str] = {
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "ct": "http://schemas.openxmlformats.org/package/2006/content-types",
    "mc": "http://schemas.openxmlformats.org/markup-compatibility/2006",
    "pic": "http://schemas.openxmlformats.org/drawingml/2006/picture",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "w14": "http://schemas.microsoft.com/office/word/2010/wordml",
    "wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
    "wps": "http://schemas.microsoft.com/office/word/2010/wordprocessingShape",
}


@lru_cache(maxsize=512)
def qn(name: str) -> str:
    """Translate a stable ``prefix:local`` name to Clark notation."""
    prefix, separator, local_name = name.partition(":")
    if not separator or not prefix or not local_name or prefix not in NAMESPACES:
        raise ValueError(f"Unknown OOXML qualified name: {name!r}")
    return str(QName(NAMESPACES[prefix], local_name))
