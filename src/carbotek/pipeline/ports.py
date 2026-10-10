# pyright: strict, reportPrivateUsage=false
"""Ports implemented by infrastructure adapters."""

from collections.abc import Mapping
from typing import Any, Protocol

from carbotek.docx.plan import PartPlan, SetStatement
from carbotek.docx.session import RenderSession
from carbotek.options import RenderOptions
from carbotek.package import TemplatePackage


class TemplateRepository(Protocol):
    """Loads and stores templates without exposing ZIP implementation details."""

    def load(self, payload: bytes) -> TemplatePackage:
        """Validate and decode a template package."""
        ...

    def dump(self, template: TemplatePackage) -> bytes:
        """Encode a template package."""
        ...


class DocumentPartRenderer(Protocol):
    """Port for rendering one format-specific template part."""

    def create_session(self, data: Mapping[str, Any] | list[Any], options: RenderOptions) -> RenderSession: ...

    def compile_part(
        self, content: bytes, *, dialect: Any = ...
    ) -> tuple[bytes, PartPlan, tuple[SetStatement, ...]]: ...

    def execute_sets(self, statements: tuple[SetStatement, ...], session: RenderSession) -> None: ...

    def is_templated(self, name: str) -> bool:
        """Return whether this part belongs to the renderer's templating surface."""
        ...

    def render_tree(
        self,
        content: bytes,
        data: Mapping[str, Any] | list[Any],
        options: RenderOptions,
        *,
        normalized: bool = False,
        dialect: Any = ...,
    ) -> Any:
        """Render a supported part (in the given XML dialect) into an XML tree owned by the caller."""
        ...
