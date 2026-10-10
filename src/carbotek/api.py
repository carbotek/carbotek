# pyright: strict, reportPrivateUsage=false
"""Public synchronous API: module-level helpers over a shared :class:`Engine`."""

from collections.abc import Mapping
from functools import lru_cache
from io import BytesIO
from typing import Any
from zipfile import BadZipFile, ZipFile

from .docx.renderer import DocxTextRenderer
from .errors import FeatureUnavailable, ValidationError
from .features.fetch import ImageSourceConfig
from .opc import OpcTemplateRepository
from .options import RenderOptions
from .pdf import convert_docx_to_pdf
from .pipeline.compile import CompiledTemplate, CompiledTemplateCache
from .pipeline.document import RenderDocument
from .typing_helpers import is_list, is_mapping

type Data = Mapping[str, Any] | list[Any]
type Options = RenderOptions | Mapping[str, Any] | None


class Engine:
    """A renderer with its own template caches; reuse one instance to benefit from the compile cache.

    ``cache_size``/``cache_bytes`` bound the compiled-template LRU (0 disables caching);
    ``image_sources`` configures remote/local image loading (default: data URIs only).
    """

    def __init__(
        self,
        *,
        cache_size: int = 64,
        cache_bytes: int = 256 * 1024 * 1024,
        image_sources: ImageSourceConfig | None = None,
    ) -> None:
        if image_sources is not None and not isinstance(image_sources, ImageSourceConfig):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise ValidationError("image_sources must be ImageSourceConfig or None")
        self._document = RenderDocument(
            OpcTemplateRepository(),
            DocxTextRenderer(),
            image_sources,
            CompiledTemplateCache(capacity=cache_size, max_bytes=cache_bytes),
        )

    def compile(self, template: bytes, options: Options = None) -> CompiledTemplate:
        """Validate and prepare a template using the same compile cache as render()."""
        if not isinstance(template, bytes):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise ValidationError("template must be bytes")
        return self._document.compile(template, _options(options))

    def render(self, template: bytes, data: Data, options: Options = None) -> bytes:
        """Render a DOCX template into a DOCX document, an XLSX template into an XLSX workbook."""
        return self._execute(template, data, options, rasterize_svg=False, image_dpi=220)

    def render_pdf(self, template: bytes, data: Data, options: Options = None, *, image_dpi: int = 220) -> bytes:
        """Render a DOCX template and convert the result to PDF with dxpdf (XLSX has no PDF output)."""
        if type(image_dpi) is not int or not 1 <= image_dpi <= 2400:
            raise ValidationError("image_dpi must be an integer between 1 and 2400")
        if not isinstance(template, bytes):  # pyright: ignore[reportUnnecessaryIsInstance] - public API guard
            raise ValidationError("template must be bytes")
        if _is_workbook(template):
            raise FeatureUnavailable("PDF output is only available for DOCX templates (XLSX needs LibreOffice)")
        document = self._execute(template, data, options, rasterize_svg=True, image_dpi=image_dpi)
        return convert_docx_to_pdf(document, image_dpi=image_dpi)

    @property
    def metrics(self) -> dict[str, int | float]:
        """Compile cache metrics: size, bytes, hits, misses, hit_rate."""
        return self._document.metrics

    def _execute(self, template: bytes, data: Data, options: Options, *, rasterize_svg: bool, image_dpi: int) -> bytes:
        if not isinstance(template, bytes):  # pyright: ignore[reportUnnecessaryIsInstance] - public API guard
            raise ValidationError("template must be bytes")
        if not isinstance(data, Mapping | list) or (  # pyright: ignore[reportUnnecessaryIsInstance] - public API guard
            is_list(data) and any(not is_mapping(item) for item in data)
        ):
            raise ValidationError("data must be a mapping or list of mappings")
        return self._document.execute(
            template, data, _options(options), rasterize_svg=rasterize_svg, image_dpi=image_dpi
        )


@lru_cache(maxsize=1)
def default_engine() -> Engine:
    """The process-wide engine behind :func:`render` and :func:`render_pdf`."""
    return Engine()


def render(template: bytes, data: Data, options: Options = None) -> bytes:
    """Render a DOCX template into a DOCX document, an XLSX template into an XLSX workbook."""
    return default_engine().render(template, data, options)


def render_pdf(template: bytes, data: Data, options: Options = None, *, image_dpi: int = 220) -> bytes:
    """Render a DOCX template and convert the resulting package to PDF."""
    return default_engine().render_pdf(template, data, options, image_dpi=image_dpi)


def _is_workbook(template: bytes) -> bool:
    """Whether the template is an XLSX package (checked before any rendering work)."""
    try:
        with ZipFile(BytesIO(template)) as archive:
            names = archive.namelist()
    except BadZipFile, ValueError:
        return False  # rejected with the engine's own error by the render
    return "word/document.xml" not in names and any(name.startswith("xl/") for name in names)


def _options(options: Options) -> RenderOptions:
    if options is None:
        return RenderOptions.create()
    if isinstance(options, RenderOptions):
        return options
    if not is_mapping(options):
        raise ValidationError("options must be a mapping or RenderOptions")
    if any(not isinstance(key, str) for key in options):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise ValidationError("option names must be strings")
    return RenderOptions.create(**options)
