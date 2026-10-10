# pyright: strict, reportPrivateUsage=false
"""Render use case: orchestration only, with all format details delegated to adapters."""

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any

from carbotek.docx.dialect import SHEET
from carbotek.docx.normalize import unique_drawing_ids
from carbotek.docx.plan import PartPlan
from carbotek.docx.session import CURRENT_RENDER, RenderSession, isolated_render, render_session
from carbotek.features import (
    ColorBinding,
    HtmlSubstitution,
    HyperlinkSubstitution,
    ImageSubstitution,
    NativeChartSubstitution,
)
from carbotek.features.fetch import ImageSourceConfig
from carbotek.ooxml.fragments import fragment_scope
from carbotek.options import RenderOptions
from carbotek.package import TemplatePackage, TemplatePart
from carbotek.xlsx.images import WorkbookImages

from .compile import CompiledTemplate, CompiledTemplateCache, compile_template
from .ports import DocumentPartRenderer, TemplateRepository


@dataclass(frozen=True, slots=True)
class RenderDocument:
    """Application use case for DOCX-to-DOCX rendering."""

    repository: TemplateRepository
    renderer: DocumentPartRenderer
    image_source_config: ImageSourceConfig | None = None
    compiled: CompiledTemplateCache = field(default_factory=CompiledTemplateCache)

    @isolated_render
    @fragment_scope()
    def execute(
        self,
        template: bytes,
        data: Mapping[str, Any] | list[Any],
        options: RenderOptions,
        *,
        rasterize_svg: bool = False,
        image_dpi: int = 220,
    ) -> bytes:
        """Render the supported DOCX parts and return a newly assembled package."""
        # Freeze legacy configuration before formatters or feature workers can change the environment.
        execution = replace(self, image_source_config=self.image_source_config or ImageSourceConfig.from_environment())
        # Compile once per template (cached): {o.*}, normalization, feature pre-phase.
        compiled = self.compile(template, options)
        complement = dict(options.complement)
        complement.setdefault("now", datetime.now(UTC).isoformat())
        options = replace(options, **compiled.option_values, complement=complement)
        mutates = (
            bool(compiled.sets)
            or bool(compiled.variables)
            or bool(options.variable_str)
            or any(plan.mutates_input or plan.aliases for plan in compiled.plans.values())
        )
        working_data = (
            (deepcopy(dict(data)) if mutates else dict(data))
            if isinstance(data, Mapping)
            else deepcopy(data)
            if mutates
            else list(data)
        )
        session = self.renderer.create_session(working_data, options)
        session.variables.declarations = {**session.variables.declarations, **compiled.variables}
        options = replace(options, complement=session.context.complement)
        session.options = options
        with render_session(session):
            return execution._render_compiled(compiled, working_data, options, session, rasterize_svg, image_dpi)

    @isolated_render
    def compile(self, template: bytes, options: RenderOptions) -> CompiledTemplate:
        """Validated preparation shared by rendering and template diagnostics."""
        as_text = options.export_formatted_values_as_text
        return self.compiled.get(
            sha256(template).hexdigest()
            + (":text" if as_text else "")
            + (
                ":vars:" + sha256(options.variable_str.encode("utf-8", "surrogatepass")).hexdigest()
                if options.variable_str
                else ""
            ),
            lambda: compile_template(
                self.repository.load(template),
                self.renderer,
                export_formatted_values_as_text=as_text,
                variable_str=options.variable_str,
            ),
        )

    def _render_compiled(
        self,
        compiled: CompiledTemplate,
        data: Mapping[str, Any] | list[Any],
        options: RenderOptions,
        session: RenderSession,
        rasterize_svg: bool,
        image_dpi: int,
    ) -> bytes:
        package = compiled.package
        self.renderer.execute_sets(compiled.sets, session)
        if package.kind == "xlsx":
            return self.repository.dump(
                _restore_literals(self._render_workbook(package, data, options, compiled.plans), session)
            )
        # Main phase: loops, fields and directives; feature markers are frozen to item paths.
        for part in package.parts.values():
            if part.is_xml and self.renderer.is_templated(part.name):
                # One parse per part and render: the tree travels through the features and is
                # serialized once, when the package is written.
                session.part_plan = compiled.plans.get(part.name)
                tree = self.renderer.render_tree(part.content, data, options, normalized=True)
                package = package.replace_part(TemplatePart.from_tree(part.name, tree))
        # Post-phase: features resolve the frozen markers of every loop copy.
        # Features whose markers the template does not contain are skipped (CompiledTemplate.features).
        features = compiled.features | session.features
        if "color" in features:
            package = ColorBinding().apply(package, data, options, dict(compiled.color_bindings))
        if "html" in features:
            package = HtmlSubstitution().apply(package, data, options)
        if "hyperlinks" in features:
            package = HyperlinkSubstitution().apply(package, data, options.complement)
        if "charts" in features:
            package = NativeChartSubstitution().apply(package, data, options, compiled.charts)
        if "images" in features:
            package = ImageSubstitution(self.image_source_config).apply(
                package,
                data,
                complement=options.complement,
                rasterize_svg=rasterize_svg,
                image_dpi=image_dpi,
                language=options.lang,
            )
        if "drawings" in features:
            package = _unique_drawing_ids(package, self.renderer)
        return self.repository.dump(_restore_literals(package, session))

    def _render_workbook(
        self,
        package: TemplatePackage,
        data: Mapping[str, Any] | list[Any],
        options: RenderOptions,
        plans: Mapping[str, PartPlan],
    ) -> TemplatePackage:
        """XLSX: markers run in every XML part of the workbook (all parts are marked in JS)."""
        for part in package.parts.values():
            if part.is_xml and b"{" in part.content:
                if (session := CURRENT_RENDER.get()) is not None:
                    session.part_plan = plans.get(part.name)
                tree = self.renderer.render_tree(part.content, data, options, normalized=True, dialect=SHEET)
                package = package.replace_part(TemplatePart.from_tree(part.name, tree))
        return WorkbookImages(self.image_source_config).apply(package, data, options.complement, options.lang)

    @property
    def metrics(self) -> dict[str, int | float]:
        """Compile cache metrics of this process."""
        return self.compiled.metrics


def _unique_drawing_ids(package: TemplatePackage, renderer: DocumentPartRenderer) -> TemplatePackage:
    names = [part.name for part in package.parts.values() if part.is_xml and renderer.is_templated(part.name)]
    roots = {name: package.parts[name].tree() for name in names}
    if not unique_drawing_ids(list(roots.values())):
        return package
    return package.replace_parts({name: TemplatePart.from_tree(name, root) for name, root in roots.items()})


def _restore_literals(package: TemplatePackage, session: RenderSession) -> TemplatePackage:
    if not session.protected_literals:
        return package
    replacements: dict[str, TemplatePart] = {}
    for name, part in package.parts.items():
        if not part.is_xml:
            continue
        content = part.content
        restored = session.restore(content)
        if content != restored:
            replacements[name] = TemplatePart(name, restored, True)
    return package.replace_parts(replacements) if replacements else package
