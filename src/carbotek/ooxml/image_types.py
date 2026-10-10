# pyright: strict
"""Shared image types for DOCX, XLSX, local files and remote responses."""

from types import MappingProxyType

MIME_EXTENSIONS = MappingProxyType(
    {
        "image/png": "png",
        "image/jpeg": "jpeg",
        "image/gif": "gif",
        "image/bmp": "bmp",
        "image/svg+xml": "svg",
        "image/webp": "webp",
        "image/x-emf": "emf",
        "image/xbm": "xbm",
        "image/x-portable-bitmap": "pbm",
    }
)
CONTENT_TYPES = MappingProxyType(
    {
        **{extension: mime for mime, extension in MIME_EXTENSIONS.items()},
        "jpg": "image/jpeg",
        "xmb": "image/xbm",
    }
)
_ALIASES = {
    "image/x-png": "image/png",
    "image/pjpeg": "image/jpeg",
    "image/x-windows-bmp": "image/bmp",
    "image/x-ms-bmp": "image/bmp",
    "image/x-xbitmap": "image/xbm",
    "image/x-xbm": "image/xbm",
    "image/emf": "image/x-emf",
}


def image_mime(value: str) -> str:
    value = value.split(";", 1)[0].strip().lower()
    return _ALIASES.get(value, value)
