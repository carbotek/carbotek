# pyright: strict, reportPrivateUsage=false
"""Resolve DOCX color markers and bindings in text and XML attributes."""

import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from lxml import etree  # pyright: ignore[reportAttributeAccessIssue]

from carbotek.docx.renderer import formatter_argument, resolve_path
from carbotek.docx.session import CURRENT_RENDER, feature_parse, feature_scan, feature_source
from carbotek.errors import MarkerResolutionError
from carbotek.formatters import FormatterRegistry, MarkerContext, shared_registry
from carbotek.markers import Marker, MarkerOccurrence, parse
from carbotek.ooxml.ns import qn
from carbotek.ooxml.xml import sanitize_xml_text
from carbotek.options import RenderOptions
from carbotek.package import TemplatePackage, TemplatePart
from carbotek.typing_helpers import is_mapping

# lib/color.js getBindColorMarkers: the comma between reference and format is optional.
_BIND_COLOR = re.compile(r"\{\s*(bindColor)\s*\(\s*([^,]*?)\s*,?\s*([^,]*?)\s*\)\s*=\s*([^}]*?)\s*}")
_BIND_FORMATS = ("#hexa", "hexa", "color", "hsl", "rgb")
_BIND_WARNING = "Carbotek bindColor warning:"
# Declaration order matters: it is the order JS lists them in its error messages.
_SCOPE_PRIORITY = {"row": 16, "cell": 32, "p": 64, "shape": 48, "page": 80}
_TYPE_PRIORITY = {"text": 1, "background": 2, "border": 4}
_FORBIDDEN_TYPES = {
    ("row", "border"),
    ("cell", "border"),
    ("p", "background"),
    ("p", "border"),
    ("page", "text"),
    ("page", "border"),
}


@dataclass(frozen=True, slots=True)
class _Binding:
    reference: str
    format: str
    marker: Marker


@dataclass(frozen=True, slots=True)
class ColorBinding:
    """Render only color-related marker pipelines in XML attribute values."""

    def prepare(self, package: TemplatePackage) -> tuple[TemplatePackage, dict[str, _Binding]]:
        """Pre-phase: consume the ``{bindColor(...) = marker}`` declarations (data-independent)."""
        roots = _roots(package)
        bindings, changed_parts = _collect_bindings(roots)
        # Bind colors on the template, so each repeated attribute carries its own scope.
        for name, root in roots.items():
            for element in root.iter():
                for attribute, source in tuple(element.attrib.items()):
                    binding = _matching_binding(element, attribute, source, bindings)
                    if binding is None:
                        continue
                    target = "textBackgroundColor" if element.tag == qn("w:highlight") else "textColor"
                    if target == "textBackgroundColor" and binding.format != "color":
                        raise MarkerResolutionError("DOCX highlight bindColor requires the color format")
                    raw = binding.marker.raw[:-1] + f":getAndConvertColorDocx('{binding.format}','{target}')}}"
                    element.set(attribute, raw)
                    if attribute == qn("w:fill"):
                        element.attrib.pop(qn("w:themeFill"), None)
                    changed_parts.add(name)
        replacements = {name: TemplatePart.from_tree(name, roots[name]) for name in changed_parts}
        return (package.replace_parts(replacements) if replacements else package), {}

    def apply(
        self,
        package: TemplatePackage,
        data: Mapping[str, Any] | list[Any],
        options: RenderOptions,
        bindings: dict[str, _Binding] | None = None,
    ) -> TemplatePackage:
        replacements: dict[str, TemplatePart] = {}
        registry = shared_registry()
        context = MarkerContext(data=dict(data) if is_mapping(data) else {}, complement=dict(options.complement))
        changed_parts: set[str] = set()
        if bindings is None:
            roots = _roots(package)
            bindings, changed_parts = _collect_bindings(roots)
        else:
            # Without bindColor only markers ("{") can change a part: the others are not even parsed.
            roots = _roots(package, needs_marker=not bindings)
        for part_name, root in roots.items():
            changed = part_name in changed_parts
            changed |= _apply_text_colors(root, data, options, registry, context)
            # Without bindColor only attributes holding a marker can change.
            for element in root.iter() if bindings else _COLOR_ATTRIBUTE_ELEMENTS(root):
                for attribute, source in element.items():
                    binding = _matching_binding(element, attribute, source, bindings)
                    if binding is not None:
                        target = "textBackgroundColor" if element.tag == qn("w:highlight") else "textColor"
                        if target == "textBackgroundColor" and binding.format != "color":
                            raise MarkerResolutionError("DOCX highlight bindColor requires the color format")
                        value = _evaluate_marker(binding.marker, data, options, registry, context)
                        resolved = str(
                            registry.apply(
                                "getAndConvertColorDocx",
                                value,
                                (binding.format, target),
                                render_options=options,
                                marker_context=context,
                            )
                        )
                        if attribute == qn("w:fill"):
                            element.attrib.pop(qn("w:themeFill"), None)
                    else:
                        resolved = _resolve_marker(source, data, options, registry, context)
                    if resolved is not None:
                        element.set(attribute, resolved)
                        changed = True
            if changed:
                replacements[part_name] = TemplatePart.from_tree(part_name, root)
        return package.replace_parts(replacements) if replacements else package


_COLOR_TEXTS = etree.XPath(
    "//w:t[contains(., ':color')]", namespaces={"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
)
# Color markers in attributes live in the color, shading and highlight properties.
_COLOR_ATTRIBUTE_ELEMENTS = etree.XPath(
    "//*[@w:val[contains(., '{') or contains(., '[[ct-')] or @w:fill[contains(., '{') or contains(., '[[ct-')] or @w:color[contains(., '{') or contains(., '[[ct-')] or @val[contains(., '[[ct-')]]",
    namespaces={"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"},
)


def _roots(package: TemplatePackage, *, needs_marker: bool = False) -> dict[str, etree._Element]:
    return {
        part.name: part.tree()
        for part in package.parts.values()
        if part.is_xml
        and part.name.startswith(("word/document", "word/header", "word/footer", "word/footnotes", "word/endnotes"))
        and (not needs_marker or part.has_tree or part.contains("{") or part.contains("[[ct-"))
    }


def _collect_bindings(roots: Mapping[str, etree._Element]) -> tuple[dict[str, _Binding], set[str]]:
    bindings: dict[str, _Binding] = {}
    used: list[str] = []
    changed: set[str] = set()
    for part_name, root in roots.items():
        for text in root.iter(qn("w:t")):
            source = text.text or ""

            def remove(match: re.Match[str]) -> str:
                reference, kind, expression = match.group(2), match.group(3), match.group(4)
                if not reference or not kind or not expression:
                    raise MarkerResolutionError(f'{_BIND_WARNING} the marker is not valid "{match.group(0)}".')
                if reference in used:
                    raise MarkerResolutionError(
                        f'{_BIND_WARNING} 2 bindColor markers try to edit the same color "{reference}".'
                    )
                if kind not in _BIND_FORMATS:
                    formats = ", ".join(f'"{item}"' for item in _BIND_FORMATS)
                    raise MarkerResolutionError(
                        f"{_BIND_WARNING} the color format does not exist. Use one of the following: {formats}."
                    )
                marker = parse("{" + expression + "}")
                if marker.root not in {"d", "c"}:
                    raise MarkerResolutionError(f'{_BIND_WARNING} the marker is not valid "{expression}".')
                key = reference.removeprefix("#").lower()
                bindings[key] = _Binding(key, kind, marker)
                used.append(reference)
                return ""

            text.text = _BIND_COLOR.sub(remove, source)
            if text.text != source:
                changed.add(part_name)
    return bindings, changed


def _apply_text_colors(
    root: etree._Element,
    data: Mapping[str, Any] | list[Any],
    options: RenderOptions,
    registry: FormatterRegistry,
    context: MarkerContext,
) -> bool:
    changed = False
    applied: dict[etree._Element, int] = {}
    for text in _COLOR_TEXTS(root):  # only text nodes that can hold a ``:color`` marker
        source = text.text or ""
        removed: list[MarkerOccurrence] = []
        for occurrence in feature_scan(source):
            marker = feature_parse(occurrence.raw)
            if marker.root not in {"d", "c", "$"} or not marker.formatters or marker.formatters[-1].name != "color":
                continue
            call = marker.formatters[-1]
            scope = call.arguments[0] if call.arguments else "p"
            kind = call.arguments[1] if len(call.arguments) > 1 else "text"
            _validate_color_scope(scope, kind, feature_source(marker))
            value = _evaluate_marker(marker, data, options, registry, context)
            color_value = str(registry.apply("colorToDocx", value, (), render_options=options, marker_context=context))
            priority = _SCOPE_PRIORITY[scope] + _TYPE_PRIORITY[kind]
            _set_color(root, text, scope, kind, color_value, feature_source(marker), priority, applied)
            removed.append(occurrence)
            changed = True
        for occurrence in reversed(removed):
            source = source[: occurrence.start] + source[occurrence.end :]
        text.text = source
    return changed


def _validate_color_scope(scope: str, kind: str, raw: str) -> None:
    """Reject unknown scopes and types with the texts of lib/color.js preProcessColor."""
    if scope not in _SCOPE_PRIORITY:
        raise MarkerResolutionError(
            f'The color scope "{scope}" is unknown in "{raw}". The formatter accepts only '
            f"{', '.join(_SCOPE_PRIORITY)} for the first argument."
        )
    if kind not in _TYPE_PRIORITY:
        raise MarkerResolutionError(
            f'The color type "{kind}" is unknown in "{raw}". The formatter accepts only '
            f"{', '.join(_TYPE_PRIORITY)} for the second argument."
        )
    if (scope, kind) in _FORBIDDEN_TYPES:
        raise MarkerResolutionError(f'The color type "{kind}" is invalid for the scope "{scope}" in "{raw}".')


def _outside_scope(raw: str, scope: str) -> MarkerResolutionError:
    return MarkerResolutionError(f'The Carbotek tag "{raw}" is outside of its defined scope "({scope})"')


def _set_color(
    root: etree._Element,
    text: etree._Element,
    scope: str,
    kind: str,
    value: str,
    raw: str,
    priority: int,
    applied: dict[etree._Element, int],
) -> None:
    if kind == "text":
        tag = {"p": "w:p", "cell": "w:tc", "row": "w:tr", "shape": "w:drawing"}[scope]
        container = next(iter(text.iterancestors(qn(tag))), None)
        if container is None:
            raise _outside_scope(raw, scope)
        for run in container.iter(qn("w:r")):
            properties = run.find(qn("w:rPr"))
            if properties is None:
                properties = etree.Element(qn("w:rPr"))
                run.insert(0, properties)
            color = properties.find(qn("w:color"))
            if color is None:
                color = etree.SubElement(properties, qn("w:color"))
            if _should_apply(color, priority, applied):
                color.set(qn("w:val"), value)
    elif scope in {"cell", "row"}:
        _set_cell_background(text, scope, value, raw, priority, applied)
    elif scope == "page":
        background = root.find(qn("w:background"))
        if background is None:
            background = etree.Element(qn("w:background"))
            root.insert(0, background)
        if _should_apply(background, priority, applied):
            background.set(qn("w:color"), value)
    else:
        _set_shape_color(text, kind, value, raw, priority, applied)


def _should_apply(element: etree._Element, priority: int, applied: dict[etree._Element, int]) -> bool:
    previous = applied.get(element)
    if previous is not None and previous >= priority:
        return False
    applied[element] = priority
    return True


def _set_cell_background(
    text: etree._Element,
    scope: str,
    value: str,
    raw: str,
    priority: int,
    applied: dict[etree._Element, int],
) -> None:
    if scope == "cell":
        cell = next(iter(text.iterancestors(qn("w:tc"))), None)
        cells = [cell] if cell is not None else []
    else:
        row = next(iter(text.iterancestors(qn("w:tr"))), None)
        cells = row.findall(qn("w:tc")) if row is not None else []
    if not cells:
        raise _outside_scope(raw, scope)
    for cell in cells:
        _shade_cell(cell, value, priority, applied)


def _shade_cell(cell: etree._Element, value: str, priority: int, applied: dict[etree._Element, int]) -> None:
    properties = cell.find(qn("w:tcPr"))
    if properties is None:
        properties = etree.Element(qn("w:tcPr"))
        cell.insert(0, properties)
    shading = properties.find(qn("w:shd"))
    if shading is None:
        shading = etree.SubElement(properties, qn("w:shd"))
        shading.set(qn("w:val"), "clear")
    if _should_apply(shading, priority, applied):
        shading.set(qn("w:fill"), value)
        shading.attrib.pop(qn("w:themeFill"), None)


def _set_shape_color(
    text: etree._Element,
    kind: str,
    value: str,
    raw: str,
    priority: int,
    applied: dict[etree._Element, int],
) -> None:
    drawing = next(iter(text.iterancestors(qn("w:drawing"))), None)
    properties = drawing.find(f".//{qn('wps:spPr')}") if drawing is not None else None
    if properties is None:
        raise _outside_scope(raw, "shape")
    target = properties
    if kind == "border":
        target = properties.find(qn("a:ln"))
        if target is None:
            target = etree.SubElement(properties, qn("a:ln"))
    solid_fill = target.find(qn("a:solidFill"))
    if solid_fill is None:
        solid_fill = etree.SubElement(target, qn("a:solidFill"))
    rgb = solid_fill.find(qn("a:srgbClr"))
    if rgb is None:
        rgb = etree.SubElement(solid_fill, qn("a:srgbClr"))
    if _should_apply(rgb, priority, applied):
        rgb.set("val", value)


def _matching_binding(
    element: etree._Element, attribute: str, source: str, bindings: Mapping[str, _Binding]
) -> _Binding | None:
    if not bindings:
        return None
    eligible = (
        (element.tag in {qn("w:color"), qn("w:highlight")} and attribute == qn("w:val"))
        or attribute in {qn("w:fill"), qn("w:color")}
        or (element.tag == qn("a:srgbClr") and attribute == "val")
    )
    return None if not eligible else bindings.get(source.removeprefix("#").lower())


def _resolve_marker(
    source: str,
    data: Mapping[str, Any] | list[Any],
    options: RenderOptions,
    registry: FormatterRegistry,
    context: MarkerContext,
) -> str | None:
    occurrences = feature_scan(source)
    if len(occurrences) != 1 or source != occurrences[0].raw:
        return None
    marker = feature_parse(occurrences[0].raw)
    if (
        marker.root not in {"d", "c", "$"}
        or not marker.formatters
        or all(
            call.name
            not in {
                "color",
                "colorToDocx",
                "getAndConvertColorDocx",
                "shadeColor",
            }
            for call in marker.formatters
        )
    ):
        return None
    return sanitize_xml_text(str(_evaluate_marker(marker, data, options, registry, context)))


def _evaluate_marker(
    marker: Marker,
    data: Mapping[str, Any] | list[Any],
    options: RenderOptions,
    registry: FormatterRegistry,
    context: MarkerContext,
) -> Any:
    session = CURRENT_RENDER.get()
    if session is not None:
        final = marker.formatters[-1] if marker.formatters else None
        if final is not None and final.name in {"color", "getAndConvertColorDocx"}:
            value = session.evaluate(replace(marker, formatters=marker.formatters[:-1]))
            arguments = tuple(argument.strip("'\"") for argument in final.arguments)
            return registry.apply(final.name, value, arguments, render_options=options, marker_context=session.context)
        return session.evaluate(marker)
    value: Any = resolve_path(marker.path, data if marker.root == "d" else context.complement)
    for call in marker.formatters:
        arguments = tuple(formatter_argument(argument, data, context.complement, {}) for argument in call.arguments)
        value = registry.apply(call.name, value, arguments, render_options=options, marker_context=context)
    return value
