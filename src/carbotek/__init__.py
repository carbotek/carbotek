# pyright: strict, reportPrivateUsage=false
"""carbotek: DOCX and XLSX templates with Carbone-style markers rendered to DOCX, XLSX or PDF (dxpdf).

import carbotek
docx = carbotek.render(template_bytes, {"name": "Ann"}, {"lang": "ru"})
pdf = carbotek.render_pdf(template_bytes, data)
engine = carbotek.Engine(cache_size=16)   # own compile cache, e.g. for benchmarks
"""

from importlib.metadata import PackageNotFoundError, version

from .api import Engine, default_engine, render, render_pdf
from .errors import CarbotekError
from .options import RenderOptions
from .pdf import convert_docx_to_pdf

# The single source is native/Cargo.toml; the release workflow sets it from the git tag.
try:
    __version__ = version("carbotek")
except PackageNotFoundError:
    __version__ = "0+unknown"

__all__ = [
    "CarbotekError",
    "Engine",
    "RenderOptions",
    "__version__",
    "convert_docx_to_pdf",
    "default_engine",
    "render",
    "render_pdf",
]
