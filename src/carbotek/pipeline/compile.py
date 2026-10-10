# pyright: strict, reportPrivateUsage=false
"""Compile cache: the data-independent part of a render, done once per template.

A compiled template is the package after ``{o.*}`` extraction, normalization of every templated
part and the pre-phase of the features (hyperlink markers moved onto ``r:id``, ``bindColor``
declarations consumed). It is keyed by the SHA-256 of the template bytes. The work that depends on
data still runs on every render. Parts are immutable bytes, so
a cached package is shared between renders without copying.
"""

from collections import OrderedDict
from collections.abc import Callable, Mapping
from concurrent.futures import Future
from dataclasses import dataclass, field
from threading import RLock
from types import MappingProxyType
from typing import Any

from lxml import etree  # pyright: ignore[reportAttributeAccessIssue]

from carbotek.docx.dialect import SHEET
from carbotek.docx.normalize import normalize_document, prepare_malformed_html_calls
from carbotek.docx.plan import PartPlan, SetStatement
from carbotek.docx.renderer import substitute_variables
from carbotek.features import ColorBinding, HyperlinkSubstitution
from carbotek.features.native_chart import ChartTemplate, prepare_charts
from carbotek.features.template_options import extract_template_option_values
from carbotek.markers import VariableDeclaration, parse_declaration, scan
from carbotek.ooxml.xml import parse_xml, serialize_xml
from carbotek.package import TemplatePackage, TemplatePart
from carbotek.xlsx import prepare_workbook

from .ports import DocumentPartRenderer


@dataclass(frozen=True, slots=True)
class CompiledTemplate:
    """A template ready for the data-dependent phases."""

    package: TemplatePackage
    option_values: Mapping[str, object]
    color_bindings: Mapping[str, Any]
    # Post-phase features the template can need at all (decided once from its compiled bytes).
    features: frozenset[str] = frozenset({"color", "html", "hyperlinks", "images", "charts", "drawings"})

    plans: Mapping[str, PartPlan] = field(default_factory=dict[str, PartPlan])
    sets: tuple[SetStatement, ...] = ()
    charts: Mapping[str, ChartTemplate] = field(default_factory=dict[str, ChartTemplate])
    variables: Mapping[str, VariableDeclaration] = field(default_factory=dict[str, VariableDeclaration])

    @property
    def size(self) -> int:
        variable_bytes = sum(
            len(declaration.name) + len(declaration.expression) + sum(map(len, declaration.parameters))
            for declaration in self.variables.values()
        )
        return (
            variable_bytes
            + sum(len(part.content) for part in self.package.parts.values())
            + sum(
                len(chart.content)
                + chart.embedded_size
                + sum(len(key) + len(value) for key, value in chart.bindings.items())
                + sum(len(key) + len(value) for cells in chart.workbook.values() for key, value in cells.items())
                for chart in self.charts.values()
            )
        )


def compile_template(
    package: TemplatePackage,
    renderer: DocumentPartRenderer,
    *,
    export_formatted_values_as_text: bool = False,
    variable_str: str = "",
) -> CompiledTemplate:
    """Run every data-independent stage of the render pipeline."""
    if package.kind == "xlsx":
        package, declared = prepare_workbook(package, export_formatted_values_as_text=export_formatted_values_as_text)
        options = {key: value for key, value in declared.items() if key in _RENDER_OPTION_NAMES}
        return _analyze_template(package, renderer, options, {}, variable_str)
    package = _normalize_template_parts(package, renderer)
    package, option_values = extract_template_option_values(package)
    package = HyperlinkSubstitution().prepare(package)
    package, color_bindings = ColorBinding().prepare(package)
    return _analyze_template(package, renderer, option_values, color_bindings, variable_str)


def _analyze_template(
    package: TemplatePackage,
    renderer: DocumentPartRenderer,
    options: Mapping[str, object],
    bindings: Mapping[str, Any],
    variable_str: str,
) -> CompiledTemplate:
    package, variables = _extract_variables(package, renderer, variable_str)
    package, charts = prepare_charts(package) if package.kind == "docx" else (package, dict[str, ChartTemplate]())
    plans: dict[str, PartPlan] = {}
    statements: list[SetStatement] = []
    parts: dict[str, TemplatePart] = {}
    features: set[str] = {"charts"} if charts else set()
    for name, part in package.parts.items():
        content = part.content
        if part.is_xml and (package.kind == "xlsx" or renderer.is_templated(name)):
            if package.kind == "docx" and b":html" in content:
                root = parse_xml(content)
                before = etree.tostring(root)
                prepare_malformed_html_calls(root, name)
                after = etree.tostring(root)
                if after != before:
                    content = after
            keywords = {"dialect": SHEET} if package.kind == "xlsx" else {}
            content, plan, sets = renderer.compile_part(content, **keywords)
            plans[name] = plan
            statements.extend(sets)
            features.update(plan.features)
        elif name.startswith("word/charts/") and name.endswith(".xml") and scan(content.decode("utf-8")):
            features.add("charts")
        parts[name] = TemplatePart(name, content, part.is_xml)
    return CompiledTemplate(
        package.replace_parts(parts),
        MappingProxyType(dict(options)),
        MappingProxyType(dict(bindings)),
        frozenset(features),
        MappingProxyType(plans),
        tuple(statements),
        charts,
        variables,
    )


def _extract_variables(
    package: TemplatePackage, renderer: DocumentPartRenderer, variable_str: str
) -> tuple[TemplatePackage, Mapping[str, VariableDeclaration]]:
    declarations = {
        declaration.name: declaration
        for occurrence in scan(variable_str, include_aliases=True)
        if (declaration := parse_declaration(occurrence.raw)) is not None
    }
    replacements: dict[str, TemplatePart] = {}
    for part in package.parts.values():
        if not part.is_xml or not (
            package.kind == "xlsx" or renderer.is_templated(part.name) or part.name.startswith("word/charts/")
        ):
            continue
        if b"#" not in part.content:
            continue
        root = parse_xml(part.content)
        changed = False
        for node in root.iter():
            value = node.text or ""
            for occurrence in reversed(scan(value, include_aliases=True)):
                declaration = parse_declaration(occurrence.raw)
                if declaration is not None:
                    declarations[declaration.name] = declaration
                    value = value[: occurrence.start] + value[occurrence.end :]
                    changed = True
            node.text = value if node.text is not None else None
        if changed:
            replacements[part.name] = TemplatePart(part.name, serialize_xml(root), True)
    prepared = package.replace_parts(replacements)
    if declarations:
        for part in prepared.parts.values():
            if part.is_xml and b"$" in part.content:
                root = parse_xml(part.content)
                substitute_variables(root, declarations, all_text=True)
                replacements[part.name] = TemplatePart(part.name, serialize_xml(root), True)
    return prepared.replace_parts(replacements), MappingProxyType(declarations)


_RENDER_OPTION_NAMES = frozenset(
    {"lang", "timezone", "use_high_precision_arithmetic", "export_formatted_values_as_text"}
)


def _normalize_template_parts(package: TemplatePackage, renderer: DocumentPartRenderer) -> TemplatePackage:
    replacements: dict[str, TemplatePart] = {}
    for part in package.parts.values():
        if not part.is_xml or not renderer.is_templated(part.name):
            continue
        root = parse_xml(part.content)
        before = etree.tostring(root)
        normalize_document(root)
        after = etree.tostring(root)
        if after != before:
            replacements[part.name] = TemplatePart(part.name, after, True)
    return package.replace_parts(replacements) if replacements else package


@dataclass(slots=True)
class CompiledTemplateCache:
    """Thread-safe LRU of compiled templates, bounded by count and by part bytes."""

    capacity: int = 64
    max_bytes: int = 256 * 1024 * 1024
    _entries: OrderedDict[str, CompiledTemplate] = field(default_factory=OrderedDict[str, CompiledTemplate], init=False)
    _pending: dict[str, Future[CompiledTemplate]] = field(
        default_factory=dict[str, Future[CompiledTemplate]], init=False
    )
    _bytes: int = field(default=0, init=False)
    _lock: RLock = field(default_factory=RLock, init=False)
    _hits: int = field(default=0, init=False)
    _misses: int = field(default=0, init=False)

    def get(self, key: str, compile_: Callable[[], CompiledTemplate]) -> CompiledTemplate:
        with self._lock:
            cached = self._entries.get(key)
            if cached is not None:
                self._hits += 1
                self._entries.move_to_end(key)
                return cached
            pending = self._pending.get(key)
            owner = pending is None
            if pending is None:
                pending = Future[CompiledTemplate]()
                self._pending[key] = pending
                self._misses += 1
            else:
                self._hits += 1
        if not owner:
            return pending.result()
        try:
            compiled = compile_()
            size = compiled.size
            with self._lock:
                if self.capacity > 0 and size <= self.max_bytes and key not in self._entries:
                    self._entries[key] = compiled
                    self._bytes += size
                    while len(self._entries) > self.capacity or self._bytes > self.max_bytes:
                        _, evicted = self._entries.popitem(last=False)
                        self._bytes -= evicted.size
            pending.set_result(compiled)
            return compiled
        except BaseException as error:
            pending.set_exception(error)
            raise
        finally:
            with self._lock:
                self._pending.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._bytes = 0

    @property
    def metrics(self) -> dict[str, int | float]:
        with self._lock:
            total = self._hits + self._misses
            return {
                "size": len(self._entries),
                "bytes": self._bytes,
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": self._hits / total if total else 0.0,
            }
