# pyright: strict, reportPrivateUsage=false
"""DOCX-to-PDF adapter backed by the native dxpdf binding."""

import logging
import re
from importlib import import_module
from io import BytesIO
from typing import Any, cast
from zipfile import BadZipFile, ZipFile

lazy from PIL import Image, ImageCms

from carbotek.errors import ConvertError, ValidationError

logger = logging.getLogger(__name__)


def convert_docx_to_pdf(document: bytes, *, image_dpi: int = 220) -> bytes:
    """Convert an already-rendered DOCX package to PDF without Office automation."""
    if not isinstance(document, bytes):  # pyright: ignore[reportUnnecessaryIsInstance] - public API guard for untyped callers
        raise ValidationError("document must be bytes")
    if type(image_dpi) is not int or not 1 <= image_dpi <= 2400:
        raise ValidationError("image_dpi must be an integer between 1 and 2400")
    try:
        converter = getattr(import_module("dxpdf"), "convert", None)
        if not callable(converter):
            raise ConvertError("dxpdf does not expose convert()")
        result: Any = converter(_normalize_profiled_pngs(document), image_dpi=image_dpi)
    except ImportError as error:  # pragma: no cover - only possible in an incomplete install.
        raise ConvertError("dxpdf is not installed") from error
    except Exception as error:
        raise ConvertError(str(error)) from error
    if not isinstance(result, bytes):
        raise ConvertError("dxpdf returned an invalid conversion result")
    return result


def warn_unsupported_media(document: bytes) -> None:
    """dxpdf does not draw WMF and draws EMF only partially: say so in the log."""
    try:
        with ZipFile(BytesIO(document)) as archive:
            _warn_media(archive.namelist())
    except BadZipFile:
        return


def _warn_media(parts: list[str]) -> None:
    names = [name for name in parts if re.search(r"\.(wmf|emf)$", name, re.IGNORECASE)]
    if names:
        logger.warning("PDF conversion: WMF/EMF images are not fully supported by dxpdf: %s", ", ".join(names))


def _normalize_profiled_pngs(document: bytes) -> bytes:
    """Remove PNG color metadata that makes dxpdf drop some images on Linux."""
    replacements: dict[str, bytes] = {}
    try:
        with ZipFile(BytesIO(document)) as source:
            _warn_media(source.namelist())
            for info in source.infolist():
                if not info.filename.startswith("word/media/") or not info.filename.lower().endswith(".png"):
                    continue
                raw = source.read(info)
                if b"iCCP" not in raw:
                    continue  # a PNG without an ICC chunk needs no pixel decoding
                with Image.open(BytesIO(raw)) as image:
                    profile = image.info.get("icc_profile")
                    if not profile:
                        continue
                    rgba = image.convert("RGBA")
                    alpha = rgba.getchannel("A")
                    try:
                        original_profile = ImageCms.ImageCmsProfile(BytesIO(profile))
                        transformed = ImageCms.profileToProfile(
                            rgba.convert("RGB"),
                            original_profile,
                            cast(ImageCms.ImageCmsProfile, ImageCms.createProfile("sRGB")),  # pyright: ignore[reportUnknownMemberType]
                            outputMode="RGB",
                        )
                        rgb = transformed if transformed is not None else rgba.convert("RGB")
                    except OSError, ValueError, ImageCms.PyCMSError:
                        rgb = rgba.convert("RGB")
                    rgb.putalpha(alpha)
                    rgb.info.clear()
                    output = BytesIO()
                    rgb.save(output, format="PNG")
                    replacements[info.filename] = output.getvalue()
            if not replacements:
                return document
            output = BytesIO()
            with ZipFile(output, "w") as target:
                for info in source.infolist():
                    target.writestr(
                        info, replacements[info.filename] if info.filename in replacements else source.read(info)
                    )
            return output.getvalue()
    except BadZipFile:
        return document
