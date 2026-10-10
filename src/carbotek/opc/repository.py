# pyright: strict, reportPrivateUsage=false
"""OPC repository that validates untrusted DOCX and XLSX input before exposing it to the engine."""

import zlib
from dataclasses import dataclass
from io import BytesIO
from pathlib import PurePosixPath
from zipfile import ZIP_DEFLATED, ZIP_STORED, BadZipFile, ZipFile, ZipInfo

from carbotek.errors import CorruptedTemplateError, InvalidTemplateError, TemplateTooLargeError
from carbotek.ooxml.xml import parse_xml
from carbotek.package import TemplatePackage, TemplatePart

_CONTENT_TYPES = "[Content_Types].xml"
_MAIN_DOCUMENT = "word/document.xml"
# lib/file.js: the JS engine rejects zip bombs with this text.
_EXPANDED_TOO_LARGE = "ZIP rejected: total expanded size too large"
_COMPRESSED_MEDIA = (".png", ".jpg", ".jpeg", ".gif", ".webp")


@dataclass(frozen=True, slots=True)
class OpcTemplateRepository:
    """Read and write DOCX OPC packages while preserving their member order."""

    max_compressed_size: int = 20 * 1024 * 1024
    max_uncompressed_size: int = 200 * 1024 * 1024
    max_members: int = 10_000

    def load(self, payload: bytes) -> TemplatePackage:
        """Decode a valid DOCX package and reject zip-slip, zip-bomb, and XML attacks."""
        if len(payload) > self.max_compressed_size:
            raise TemplateTooLargeError("Template exceeds the compressed size limit")
        try:
            with ZipFile(BytesIO(payload)) as archive:
                infos = archive.infolist()
                self._validate_directory(infos)
                parts = self._read_parts(archive, infos)
        except (BadZipFile, zlib.error, EOFError, NotImplementedError, RuntimeError) as error:
            # Same text as the JS engine (fflate) for an unreadable archive.
            raise InvalidTemplateError("invalid zip data") from error

        kind = self._detect_kind(parts)
        return TemplatePackage.create(parts, tuple(part.filename for part in infos), kind)

    def dump(self, template: TemplatePackage) -> bytes:
        """Encode validated parts in original order, appending parts introduced by features."""
        if not template.order or not all(template.order):
            raise CorruptedTemplateError("Template package contains an empty member name")

        buffer = BytesIO()
        written: set[str] = set()
        with ZipFile(buffer, mode="w", compression=ZIP_DEFLATED) as archive:
            for name in (*template.order, *template.parts):
                if name in written:
                    continue
                part = template.parts.get(name)
                if part is None:
                    continue
                # Already compressed media gains nothing from deflate, which costs most of a picture-heavy dump.
                stored = name.lower().endswith(_COMPRESSED_MEDIA)
                archive.writestr(name, part.content, compress_type=ZIP_STORED if stored else ZIP_DEFLATED)
                written.add(name)
        return buffer.getvalue()

    def _validate_directory(self, infos: list[ZipInfo]) -> None:
        if len(infos) > self.max_members:
            raise TemplateTooLargeError("Template exceeds the ZIP member limit")
        declared_size = 0
        names: set[str] = set()
        for info in infos:
            self._validate_member_name(info.filename)
            if info.filename in names:
                raise CorruptedTemplateError(f"Duplicate ZIP member: {info.filename}")
            names.add(info.filename)
            declared_size += info.file_size
            if declared_size > self.max_uncompressed_size:
                raise TemplateTooLargeError(_EXPANDED_TOO_LARGE)

    def _read_parts(self, archive: ZipFile, infos: list[ZipInfo]) -> dict[str, TemplatePart]:
        parts: dict[str, TemplatePart] = {}
        total_size = 0
        for info in infos:
            content = archive.read(info)
            total_size += len(content)
            if total_size > self.max_uncompressed_size:
                raise TemplateTooLargeError(_EXPANDED_TOO_LARGE)
            is_xml = self._is_xml_part(info.filename)
            if is_xml:
                self._validate_xml(content, info.filename)
            parts[info.filename] = TemplatePart(info.filename, content, is_xml)
        return parts

    @staticmethod
    def _validate_member_name(name: str) -> None:
        path = PurePosixPath(name)
        if not name or "\\" in name or path.is_absolute() or ".." in path.parts:
            raise CorruptedTemplateError(f"Unsafe ZIP member name: {name!r}")

    @staticmethod
    def _is_xml_part(name: str) -> bool:
        return name.endswith((".xml", ".rels")) or name == _CONTENT_TYPES

    @staticmethod
    def _validate_xml(content: bytes, name: str) -> None:
        parse_xml(content, name)

    @staticmethod
    def _detect_kind(parts: dict[str, TemplatePart]) -> str:
        """``file.detectType``: a ``word/`` part makes a DOCX, an ``xl/`` part an XLSX.

        xlsm stays xlsx, as with ``params.xlsmEnabled = false`` in JS.
        """
        if _CONTENT_TYPES in parts and _MAIN_DOCUMENT in parts:
            return "docx"
        if _CONTENT_TYPES in parts and any(name.startswith("xl/") for name in parts):
            return "xlsx"
        raise InvalidTemplateError("Template is not a DOCX or XLSX OPC package")
