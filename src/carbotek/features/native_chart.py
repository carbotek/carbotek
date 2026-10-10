# pyright: strict, reportPrivateUsage=false
"""Bind Word chart caches to template markers and clone chart parts per drawing."""

import posixpath
import re
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from io import BytesIO
from types import MappingProxyType
from typing import Any
from zipfile import BadZipFile, ZipFile, ZipInfo

from lxml import etree  # pyright: ignore[reportAttributeAccessIssue]

from carbotek.docx.loops import COMPLEMENT_ROOT
from carbotek.docx.renderer import DocxTextRenderer, printable, resolve_path
from carbotek.docx.session import CURRENT_RENDER
from carbotek.errors import MarkerResolutionError
from carbotek.formatters import MarkerContext
from carbotek.markers import Marker, VariableEnvironment, parse, scan, substitute
from carbotek.ooxml.xml import parse_xml, serialize_xml
from carbotek.options import RenderOptions
from carbotek.package import TemplatePackage, TemplatePart
from carbotek.typing_helpers import is_list, is_mapping

_CHART = "http://schemas.openxmlformats.org/drawingml/2006/chart"
_SHEET = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_CT = "http://schemas.openxmlformats.org/package/2006/content-types"
_CHART_REL = f"{_R}/chart"
_CHART_MIME = "application/vnd.openxmlformats-officedocument.drawingml.chart+xml"
_WORKBOOK_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_BIND = re.compile(r"\{bindChart\s*\(([^)]+)\)\s*=\s*([^}]+)\}")
_ITERATOR_SEGMENT = re.compile(r"([^[]*)\[i(?:\+1)?\]")
_RANGE = re.compile(r"(?:'((?:[^']|'')+)'|([^!]+))!\$?([A-Z]+)\$?(\d+)(?::\$?([A-Z]+)\$?(\d+))?")
_CELL_RANGE = re.compile(r"([A-Z]+)(\d+):([A-Z]+)(\d+)")
_CELL_ADDRESS = re.compile(r"([A-Z]+)(\d+)")


@dataclass(frozen=True, slots=True)
class _WorkbookUpdate:
    formula: str
    values: tuple[str, ...]
    numeric: bool


@dataclass(frozen=True, slots=True)
class _ChartRender:
    content: bytes
    updates: tuple[_WorkbookUpdate, ...]


@dataclass(frozen=True, slots=True)
class _WorkbookTemplate:
    files: Mapping[str, bytes]
    infos: tuple[ZipInfo, ...]
    trees: Mapping[str, etree._Element]
    byte_size: int

    def read(self, name: str | ZipInfo) -> bytes:
        return self.files[name.filename if isinstance(name, ZipInfo) else name]

    def namelist(self) -> Mapping[str, bytes]:
        return self.files

    def infolist(self) -> tuple[ZipInfo, ...]:
        return self.infos

    def tree(self, name: str) -> etree._Element:
        return deepcopy(self.trees[name])


def _prepare_workbook(payload: bytes) -> _WorkbookTemplate:
    with ZipFile(BytesIO(payload)) as archive:
        files = {info.filename: archive.read(info) for info in archive.infolist()}
        infos = tuple(archive.infolist())
    trees = {name: parse_xml(content) for name, content in files.items() if name.endswith((".xml", ".rels"))}
    return _WorkbookTemplate(
        MappingProxyType(files),
        infos,
        MappingProxyType(trees),
        sum(len(content) for content in files.values()) + sum(len(_xml(tree)) for tree in trees.values()),
    )


@dataclass(frozen=True, slots=True)
class ChartTemplate:
    content: bytes
    bindings: Mapping[str, str]
    workbook: Mapping[str, Mapping[str, str]]
    embedded: Mapping[str, _WorkbookTemplate]

    @property
    def embedded_size(self) -> int:
        """Account for decompressed bytes and retained XML representations."""
        return sum(book.byte_size for book in self.embedded.values())


def prepare_charts(package: TemplatePackage) -> tuple[TemplatePackage, Mapping[str, ChartTemplate]]:
    """Read bindings and embedded cells once; annotate references to preserve copy scopes."""
    plans: dict[str, ChartTemplate] = {}
    workbook_cache: dict[str, dict[str, dict[str, str]]] = {}
    embedded_cache: dict[str, _WorkbookTemplate] = {}
    replacements: dict[str, TemplatePart] = {}
    for name, part in package.parts.items():
        if not name.startswith("word/charts/") or not name.endswith(".xml"):
            continue
        source = serialize_xml(parse_xml(part.content)).decode("utf-8")
        bindings: dict[str, str] = {}

        def binding(match: re.Match[str], bindings: dict[str, str] = bindings) -> str:
            key = match.group(1).replace(",", ".").strip()
            marker = "{" + match.group(2).strip() + "}"
            if key in bindings and bindings[key] != marker:
                raise MarkerResolutionError(f"Chart value {key} has conflicting bindings")
            bindings[key] = marker
            return ""

        content = _BIND.sub(binding, source).encode("utf-8")
        workbook = _workbook_cells(name, package, workbook_cache, embedded_cache)
        if (
            not bindings
            and not scan(source)
            and not any(scan(value) for cells in workbook.values() for value in cells.values())
        ):
            continue
        plans[name] = ChartTemplate(
            content,
            MappingProxyType(bindings),
            MappingProxyType({key: MappingProxyType(cells) for key, cells in workbook.items()}),
            MappingProxyType(
                {
                    target: embedded_cache[target]
                    for relation in parse_xml(package.parts[_rels_name(name)].content)
                    if (target := _target_name(name, relation.get("Target", ""))) in embedded_cache
                }
            )
            if _rels_name(name) in package.parts
            else MappingProxyType({}),
        )
    for part in package.parts.values():
        if not part.is_xml or not DocxTextRenderer.is_templated(part.name):
            continue
        relation_part = package.parts.get(_rels_name(part.name))
        if relation_part is None:
            continue
        ids = {
            relation.get("Id", "")
            for relation in parse_xml(relation_part.content)
            if relation.get("Type") == _CHART_REL and _target_name(part.name, relation.get("Target", "")) in plans
        }
        if not ids:
            continue
        root = part.tree()
        changed = False
        for chart in root.iter(f"{{{_CHART}}}chart"):
            identifier = chart.get(f"{{{_R}}}id", "")
            if identifier in ids:
                chart.set(f"{{{_R}}}id", f"{{c.__chartReference:ifEQ('{identifier}')}}")
                changed = True
        if changed:
            replacements[part.name] = TemplatePart.from_tree(part.name, root)
    return package.replace_parts(replacements), MappingProxyType(plans)


@dataclass(frozen=True, slots=True)
class NativeChartSubstitution:
    """Expand chart point caches and link cloned chart parts to Word drawings."""

    def apply(
        self,
        package: TemplatePackage,
        data: Mapping[str, Any] | list[Any],
        options: RenderOptions,
        plans: Mapping[str, ChartTemplate] | None = None,
    ) -> TemplatePackage:
        replacements: dict[str, TemplatePart] = {}
        content_types = parse_xml(package.parts["[Content_Types].xml"].content)
        sources = set(plans or {})
        chart_index = 0
        for part in package.parts.values():
            if not part.is_xml or not part.name.startswith(
                ("word/document", "word/header", "word/footer", "word/footnotes", "word/endnotes")
            ):
                continue
            relationships_name = _rels_name(part.name)
            original_relationships = package.parts.get(relationships_name)
            if original_relationships is None:
                continue
            relationships = parse_xml(original_relationships.content)
            chart_targets = {
                node.get("Id"): _target_name(part.name, node.get("Target", ""))
                for node in relationships
                if node.get("Type") == _CHART_REL
            }
            if not chart_targets:
                continue  # no chart in this part: do not touch its tree
            root = part.tree()
            changed = False
            for chart in root.iter(f"{{{_CHART}}}chart"):
                identifier = chart.get(f"{{{_R}}}id", "")
                session = CURRENT_RENDER.get()
                scope: Mapping[str, Any] = {}
                if session is not None and identifier in session.deferred:
                    marker, scope = session.deferred[identifier]
                    identifier = marker.formatters[0].arguments[0].strip("'\"")
                source_name = chart_targets.get(identifier)
                source = package.parts.get(source_name) if source_name is not None else None
                if source is None or source_name is None:
                    continue
                result = _render_chart(
                    source_name,
                    source.content,
                    package,
                    data,
                    options,
                    scope,
                    plans.get(source_name) if plans is not None else None,
                )
                if result is None:
                    chart.set(f"{{{_R}}}id", identifier)
                    continue
                sources.add(source_name)
                new_name = f"word/charts/CarbotekChart{chart_index}.xml"
                while new_name in package.parts or new_name in replacements:
                    chart_index += 1
                    new_name = f"word/charts/CarbotekChart{chart_index}.xml"
                new_id = f"rIdCarbotekChart{chart_index}"
                while any(node.get("Id") == new_id for node in relationships):
                    chart_index += 1
                    new_id = f"rIdCarbotekChart{chart_index}"
                chart_index += 1
                chart.set(f"{{{_R}}}id", new_id)
                etree.SubElement(
                    relationships,
                    f"{{{_REL}}}Relationship",
                    Id=new_id,
                    Type=_CHART_REL,
                    Target=f"charts/{posixpath.basename(new_name)}",
                )
                etree.SubElement(content_types, f"{{{_CT}}}Override", PartName=f"/{new_name}", ContentType=_CHART_MIME)
                chart_content = _clone_chart_dependencies(
                    source_name,
                    new_name,
                    chart_index - 1,
                    package,
                    result,
                    replacements,
                    content_types,
                    plans.get(source_name) if plans is not None else None,
                )
                replacements[new_name] = TemplatePart(new_name, chart_content, True)
                changed = True
            removed_relations = _prune_chart_relations(root, relationships, part.name, sources)
            if changed:
                replacements[part.name] = TemplatePart.from_tree(part.name, root)
            if changed or removed_relations:
                replacements[relationships_name] = TemplatePart(relationships_name, _xml(relationships), True)
        if not replacements:
            return _remove_chart_templates(package, sources)
        replacements["[Content_Types].xml"] = TemplatePart("[Content_Types].xml", _xml(content_types), True)
        return _remove_chart_templates(package.replace_parts(replacements), sources)


def _prune_chart_relations(
    root: etree._Element, relationships: etree._Element, part_name: str, sources: set[str]
) -> bool:
    used = {chart.get(f"{{{_R}}}id") for chart in root.iter(f"{{{_CHART}}}chart")}
    changed = False
    for relation in tuple(relationships):
        if (
            relation.get("Type") == _CHART_REL
            and _target_name(part_name, relation.get("Target", "")) in sources
            and relation.get("Id") not in used
        ):
            relationships.remove(relation)
            changed = True
    return changed


def _remove_chart_templates(package: TemplatePackage, sources: set[str]) -> TemplatePackage:
    """Remove unused template graphs, retaining dependencies referenced by other parts."""
    candidates = set(sources)
    for name in sources:
        relations = package.parts.get(_rels_name(name))
        if relations is not None:
            candidates.update(
                _target_name(name, relation.get("Target", ""))
                for relation in parse_xml(relations.content)
                if relation.get("TargetMode") != "External" and relation.get("Target", "").endswith(".xlsx")
            )
    removed: set[str] = set()
    while candidates:
        incoming: set[str] = set()
        for name, part in package.parts.items():
            if not name.endswith(".rels") or "_rels/" not in name or name in removed:
                continue
            directory, filename = name.rsplit("_rels/", 1)
            source = directory + filename.removesuffix(".rels")
            for relation in parse_xml(part.content):
                if relation.get("TargetMode") != "External":
                    incoming.add(_target_name(source, relation.get("Target", "")))
        orphaned = candidates - incoming
        if not orphaned:
            break
        removed.update(orphaned)
        removed.update(_rels_name(name) for name in orphaned)
        candidates.difference_update(orphaned)
    if not removed:
        return package
    types = parse_xml(package.parts["[Content_Types].xml"].content)
    for node in tuple(types):
        if node.get("PartName", "").lstrip("/") in removed:
            types.remove(node)
    return package.without_parts(removed).replace_part(TemplatePart("[Content_Types].xml", _xml(types), True))


def _clone_chart_dependencies(
    source_name: str,
    new_name: str,
    chart_number: int,
    package: TemplatePackage,
    result: _ChartRender,
    replacements: dict[str, TemplatePart],
    content_types: etree._Element,
    plan: ChartTemplate | None,
) -> bytes:
    source_rels = package.parts.get(_rels_name(source_name))
    if source_rels is None:
        return _without_external_data(result.content)
    new_rels = _rels_name(new_name)
    rel_root = parse_xml(source_rels.content)
    for relation in list(rel_root):
        if not relation.get("Target", "").endswith(".xlsx"):
            continue
        source_workbook_name = _target_name(source_name, relation.get("Target", ""))
        source_workbook = package.parts.get(source_workbook_name)
        updated_workbook = (
            _sync_workbook(
                plan.embedded.get(source_workbook_name) if plan is not None else source_workbook.content,
                result.updates,
            )
            if source_workbook is not None and result.updates
            else None
        )
        if updated_workbook is None:
            rel_root.remove(relation)
            continue
        workbook_name = f"word/embeddings/CarbotekWorkbook{chart_number}.xlsx"
        replacements[workbook_name] = TemplatePart(workbook_name, updated_workbook, False)
        relation.set("Target", posixpath.relpath(workbook_name, posixpath.dirname(new_name)))
        if all(node.get("Extension") != "xlsx" for node in content_types.findall(f"{{{_CT}}}Default")):
            etree.SubElement(content_types, f"{{{_CT}}}Default", Extension="xlsx", ContentType=_WORKBOOK_MIME)
    replacements[new_rels] = TemplatePart(new_rels, _xml(rel_root), True)
    if not any(node.get("Target", "").endswith(".xlsx") for node in rel_root):
        return _without_external_data(result.content)
    return result.content


def _render_chart(
    chart_name: str,
    content: bytes,
    package: TemplatePackage,
    data: Mapping[str, Any] | list[Any],
    options: RenderOptions,
    scope: Mapping[str, Any],
    plan: ChartTemplate | None,
) -> _ChartRender | None:
    bindings: dict[str, str] = {}

    def remove_binding(match: re.Match[str]) -> str:
        key = match.group(1).replace(",", ".").strip()
        marker = "{" + match.group(2).strip() + "}"
        if key in bindings and bindings[key] != marker:
            raise MarkerResolutionError(f"Chart value {key} has conflicting bindings")
        bindings[key] = marker
        return ""

    if plan is None:
        text = serialize_xml(parse_xml(content)).decode("utf-8")
        text = _BIND.sub(remove_binding, text)
        root = parse_xml(text.encode("utf-8"))
        workbook = _workbook_cells(chart_name, package)
    else:
        root = parse_xml(plan.content)
        bindings.update(plan.bindings)
        workbook = plan.workbook
    renderer = DocxTextRenderer()
    changed = bool(bindings)
    updates: list[_WorkbookUpdate] = []
    for reference in root.iter():
        if reference.tag not in {f"{{{_CHART}}}strRef", f"{{{_CHART}}}numRef"}:
            continue
        formula = reference.find(f"{{{_CHART}}}f")
        cache = reference.find(f"{{{_CHART}}}strCache")
        if cache is None:
            cache = reference.find(f"{{{_CHART}}}numCache")
        if cache is None:
            continue
        points = list(cache.findall(f"{{{_CHART}}}pt"))
        candidates = [_point_text(point) for point in points]
        if formula is not None and formula.text:
            candidates = [bindings.get(value, value) for value in candidates]
            candidates_from_sheet = _range_cells(formula.text, workbook, len(points))
            candidates = [
                value if "{" in value else (bindings.get(sheet_value, sheet_value) or value)
                for value, sheet_value in zip(candidates, candidates_from_sheet, strict=False)
            ] + candidates[len(candidates_from_sheet) :]
        else:
            candidates = [bindings.get(value, value) for value in candidates]
        marker = _iterator_marker(candidates)
        if marker is not None:
            contexts = _chart_iterator_contexts(parse(marker), data, options, scope)
            values = [_resolve(marker, data, options, renderer, context) for context in contexts or []]
            _replace_points(cache, points, values)
            if formula is not None and formula.text:
                updates.append(_WorkbookUpdate(formula.text, tuple(values), reference.tag == f"{{{_CHART}}}numRef"))
                formula.text = _formula_for_length(formula.text, len(values))
            changed = True
            continue
        reference_changed = False
        for point, candidate in zip(points, candidates, strict=False):
            if candidate.startswith("{") and candidate.endswith("}"):
                value = point.find(f"{{{_CHART}}}v")
                if value is not None:
                    value.text = _resolve(candidate, data, options, renderer, dict(scope))
                    changed = True
                    reference_changed = True
        if reference_changed and formula is not None and formula.text:
            updates.append(
                _WorkbookUpdate(
                    formula.text, tuple(_point_text(point) for point in points), reference.tag == f"{{{_CHART}}}numRef"
                )
            )
    changed = _render_chart_labels(root, data, options, renderer, scope) or changed
    return _ChartRender(_xml(root), tuple(updates)) if changed else None


def _render_chart_labels(
    root: etree._Element,
    data: Mapping[str, Any] | list[Any],
    options: RenderOptions,
    renderer: DocxTextRenderer,
    scope: Mapping[str, Any],
) -> bool:
    """Render template text outside caches once, without rescanning generated values."""
    changed = False
    for node in root.iter():
        if any(parent.tag in {f"{{{_CHART}}}strRef", f"{{{_CHART}}}numRef"} for parent in node.iterancestors()):
            continue
        source = node.text or ""
        occurrences = scan(source)
        if not occurrences:
            continue
        pieces: list[str] = []
        position = 0
        for occurrence in occurrences:
            pieces.extend(
                (source[position : occurrence.start], _resolve(occurrence.raw, data, options, renderer, dict(scope)))
            )
            position = occurrence.end
        pieces.append(source[position:])
        node.text = "".join(pieces)
        changed = True
    return changed


def _resolve(
    marker_text: str,
    data: Mapping[str, Any] | list[Any],
    options: RenderOptions,
    renderer: DocxTextRenderer,
    iterators: dict[str, Any],
) -> str:
    marker = parse(marker_text)
    session = CURRENT_RENDER.get()
    if session is not None:
        if marker.root == "$":
            expanded = substitute(marker_text[1:-1], session.variables.declarations)
            if re.match(r"\s*(?:[dc](?:[.\[:]|\s*$)|\$|t\()", expanded):
                marker = parse("{" + expanded + "}")
        value = printable(session.evaluate(marker, iterators))
        return session.restore(value.encode("utf-8")).decode("utf-8")
    context = MarkerContext(data=dict(data) if is_mapping(data) else {}, complement=dict(options.complement))
    variables = VariableEnvironment(context.data, context.complement, {})
    return renderer._resolve(marker, data, options, iterators, variables, context)


def _iterator_marker(candidates: list[str]) -> str | None:
    for candidate in candidates:
        if not candidate.startswith(("{d.", "{c.", "{d[", "{c[")) or not candidate.endswith("}"):
            continue
        if any(_ITERATOR_SEGMENT.fullmatch(segment) for segment in parse(candidate).path):
            return candidate
    return None


def _chart_iterator_contexts(
    marker: Marker, data: Mapping[str, Any] | list[Any], options: RenderOptions, scope: Mapping[str, Any]
) -> list[dict[str, Any]] | None:
    if marker.root not in {"d", "c"}:
        return None

    def walk(index: int, value: Any, prefix: tuple[str, ...], context: dict[str, Any]) -> list[dict[str, Any]] | None:
        if index == len(marker.path):
            return [context]
        segment = marker.path[index]
        match = _ITERATOR_SEGMENT.fullmatch(segment)
        if match is None:
            if not is_mapping(value) and not is_list(value):
                return None
            return walk(index + 1, resolve_path((segment,), value), (*prefix, segment), context)
        field = match.group(1)
        sequence = value if not field else value.get(field) if is_mapping(value) else None
        if not is_list(sequence):
            return None
        key = ".".join((*prefix, field))
        contexts: list[dict[str, Any]] = []
        for item in [scope[key]] if key in scope else sequence:
            nested = walk(index + 1, item, (*prefix, segment), {**context, key: item})
            if nested is not None:
                contexts.extend(nested)
        return contexts

    session = CURRENT_RENDER.get()
    complement = session.context.complement if session is not None else options.complement
    source = data if marker.root == "d" else complement
    return walk(0, source, (COMPLEMENT_ROOT,) if marker.root == "c" else (), dict(scope))


def _replace_points(cache: etree._Element, points: list[etree._Element], values: list[str]) -> None:
    for point in points:
        cache.remove(point)
    count = cache.find(f"{{{_CHART}}}ptCount")
    if count is None:
        count = etree.SubElement(cache, f"{{{_CHART}}}ptCount")
    count.set("val", str(len(values)))
    for index, value in enumerate(values):
        point = etree.SubElement(cache, f"{{{_CHART}}}pt", idx=str(index))
        etree.SubElement(point, f"{{{_CHART}}}v").text = value


def _formula_for_length(formula: str, count: int) -> str:
    match = _RANGE.fullmatch(formula)
    if match is None:
        return formula
    start = int(match.group(4))
    width = _column_number(match.group(5) or match.group(3)) - _column_number(match.group(3)) + 1
    if width < 1:
        return formula
    if count == 1 and match.group(5) is None:
        return formula
    prefix = formula.split(":", 1)[0]
    rows = max(1, (count + width - 1) // width)
    return f"{prefix}:${match.group(5) or match.group(3)}${start + rows - 1}"


def _without_external_data(content: bytes) -> bytes:
    root = parse_xml(content)
    for external in list(root.iter(f"{{{_CHART}}}externalData")):
        parent = external.getparent()
        if parent is not None:
            parent.remove(external)
    return _xml(root)


def _sync_workbook(payload: bytes | _WorkbookTemplate | None, updates: tuple[_WorkbookUpdate, ...]) -> bytes | None:
    if payload is None:
        return None
    try:
        source = payload if isinstance(payload, _WorkbookTemplate) else _prepare_workbook(payload)
        workbook = source.tree("xl/workbook.xml")
        relations = source.tree("xl/_rels/workbook.xml.rels")
        targets = {relation.get("Id"): relation.get("Target", "") for relation in relations}
        sheets = {
            sheet.get("name", ""): (
                target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join("xl", target))
            )
            for sheet in workbook.iter(f"{{{_SHEET}}}sheet")
            if (target := targets.get(sheet.get(f"{{{_R}}}id"), ""))
        }
        modified: dict[str, etree._Element] = {}
        range_ends: dict[str, dict[int, int]] = {}
        for update in updates:
            match = _RANGE.fullmatch(update.formula)
            if match is None:
                return None
            first_column = _column_number(match.group(3))
            last_column = _column_number(match.group(5) or match.group(3))
            width = last_column - first_column + 1
            if width < 1:
                return None
            sheet_name = _sheet_name(match)
            path = sheets.get(sheet_name)
            if path is None or path not in source.namelist():
                return None
            if path not in modified:
                modified[path] = source.tree(path)
            root = modified[path]
            first_row = int(match.group(4))
            last_row = int(match.group(6) or match.group(4))
            new_last_row = first_row + (len(update.values) + width - 1) // width - 1
            changes = range_ends.setdefault(path, {})
            changes[last_row] = max(changes.get(last_row, 0), new_last_row)
            for row_number in range(first_row, max(last_row, new_last_row) + 1):
                for column in range(first_column, last_column + 1):
                    index = (row_number - first_row) * width + column - first_column
                    value = update.values[index] if index < len(update.values) else None
                    _write_sheet_cell(root, f"{_column_name(column)}{row_number}", row_number, value, update.numeric)
        for path, ends in range_ends.items():
            _sync_sheet_ranges(source, path, modified, ends)
        output = BytesIO()
        with ZipFile(output, "w") as destination:
            for info in source.infolist():
                destination.writestr(
                    deepcopy(info), _xml(modified[info.filename]) if info.filename in modified else source.read(info)
                )
        return output.getvalue()
    except BadZipFile, KeyError, etree.XMLSyntaxError, ValueError:
        return None


def _sync_sheet_ranges(
    source: _WorkbookTemplate, path: str, modified: dict[str, etree._Element], range_ends: dict[int, int]
) -> None:
    sheet = modified[path]
    addresses = [
        match
        for cell in sheet.iter(f"{{{_SHEET}}}c")
        if any(child.tag in {f"{{{_SHEET}}}v", f"{{{_SHEET}}}is"} for child in cell)
        if (match := _CELL_ADDRESS.fullmatch(cell.get("r", ""))) is not None
    ]
    if not addresses:
        return
    columns = [_column_number(match.group(1)) for match in addresses]
    rows = [int(match.group(2)) for match in addresses]
    dimension = sheet.find(f"{{{_SHEET}}}dimension")
    if dimension is not None:
        dimension.set(
            "ref",
            f"{_column_name(min(columns))}{min(rows)}:{_column_name(max(columns))}{max(rows)}",
        )
    parent, filename = posixpath.split(path)
    rels_path = f"{parent}/_rels/{filename}.rels"
    if rels_path not in source.namelist():
        return
    for relation in source.tree(rels_path):
        if not relation.get("Type", "").endswith("/table"):
            continue
        target = relation.get("Target", "")
        table_path = (
            target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join(parent, target))
        )
        if table_path not in source.namelist():
            continue
        table = modified.get(table_path)
        if table is None:
            table = source.tree(table_path)
        match = _CELL_RANGE.fullmatch(table.get("ref", ""))
        if match is None or int(match.group(4)) not in range_ends:
            continue
        left, right = _column_number(match.group(1)), _column_number(match.group(3))
        first_row = int(match.group(2))
        old_last_row = int(match.group(4))
        allowed_last_row = max(old_last_row, range_ends[old_last_row])
        last_row = max(
            (
                row
                for column, row in zip(columns, rows, strict=True)
                if left <= column <= right and first_row <= row <= allowed_last_row
            ),
            default=first_row,
        )
        updated_ref = f"{match.group(1)}{first_row}:{match.group(3)}{last_row}"
        table.set("ref", updated_ref)
        for filter_node in table.iter(f"{{{_SHEET}}}autoFilter"):
            filter_node.set("ref", updated_ref)
        modified[table_path] = table


def _write_sheet_cell(root: etree._Element, address: str, row_number: int, value: str | None, numeric: bool) -> None:
    data = root.find(f"{{{_SHEET}}}sheetData")
    if data is None:
        return
    row = next((item for item in data.findall(f"{{{_SHEET}}}row") if item.get("r") == str(row_number)), None)
    if row is None:
        if value is None:
            return
        row = etree.Element(f"{{{_SHEET}}}row", r=str(row_number))
        index = next((index for index, item in enumerate(data) if int(item.get("r", "0")) > row_number), len(data))
        data.insert(index, row)
    cell = next((item for item in row.findall(f"{{{_SHEET}}}c") if item.get("r") == address), None)
    if value is None:
        if cell is not None:
            row.remove(cell)
        return
    if cell is None:
        cell = etree.Element(f"{{{_SHEET}}}c", r=address)
        column = _column_number(_CELL_ADDRESS.fullmatch(address).group(1))  # type: ignore[union-attr]
        position = next(
            (
                index
                for index, item in enumerate(row)
                if item.tag == f"{{{_SHEET}}}c"
                and (match := _CELL_ADDRESS.fullmatch(item.get("r", ""))) is not None
                and _column_number(match.group(1)) > column
            ),
            len(row),
        )
        row.insert(position, cell)
    for child in list(cell):
        if child.tag in {f"{{{_SHEET}}}f", f"{{{_SHEET}}}v", f"{{{_SHEET}}}is"}:
            cell.remove(child)
    if numeric:
        cell.attrib.pop("t", None)
        etree.SubElement(cell, f"{{{_SHEET}}}v").text = value
    else:
        cell.set("t", "inlineStr")
        inline = etree.SubElement(cell, f"{{{_SHEET}}}is")
        etree.SubElement(inline, f"{{{_SHEET}}}t").text = value


def _point_text(point: etree._Element) -> str:
    value = point.find(f"{{{_CHART}}}v")
    return value.text or "" if value is not None else ""


def _workbook_cells(
    chart_name: str,
    package: TemplatePackage,
    cache: dict[str, dict[str, dict[str, str]]] | None = None,
    embedded_cache: dict[str, _WorkbookTemplate] | None = None,
) -> dict[str, dict[str, str]]:
    relationships = package.parts.get(_rels_name(chart_name))
    if relationships is None:
        return {}
    for relation in parse_xml(relationships.content):
        if not relation.get("Target", "").endswith(".xlsx"):
            continue
        name = _target_name(chart_name, relation.get("Target", ""))
        embedded = package.parts.get(name)
        if embedded is None:
            continue
        try:
            if cache is not None and name in cache:
                return cache[name]
            prepared = _prepare_workbook(embedded.content)
            if embedded_cache is not None:
                embedded_cache[name] = prepared
            result = _read_workbook(prepared)
            if cache is not None:
                cache[name] = result
            return result
        except BadZipFile, KeyError, etree.XMLSyntaxError, ValueError:
            return {}
    return {}


def _read_workbook(archive: _WorkbookTemplate) -> dict[str, dict[str, str]]:
    shared = []
    if "xl/sharedStrings.xml" in archive.namelist():
        strings = archive.tree("xl/sharedStrings.xml")
        shared = [_spreadsheet_string(si) for si in strings.findall(f"{{{_SHEET}}}si")]
    workbook = archive.tree("xl/workbook.xml")
    relations = archive.tree("xl/_rels/workbook.xml.rels")
    targets = {node.get("Id"): node.get("Target", "") for node in relations}
    sheets: dict[str, dict[str, str]] = {}
    for sheet in workbook.iter(f"{{{_SHEET}}}sheet"):
        target = targets.get(sheet.get(f"{{{_R}}}id"), "")
        path = target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join("xl", target))
        if path not in archive.namelist():
            continue
        cells: dict[str, str] = {}
        for cell in archive.tree(path).iter(f"{{{_SHEET}}}c"):
            value = cell.find(f"{{{_SHEET}}}v")
            if value is not None and value.text is not None:
                cells[cell.get("r", "")] = shared[int(value.text)] if cell.get("t") == "s" else value.text
            elif cell.get("t") == "inlineStr":
                inline = cell.find(f"{{{_SHEET}}}is")
                cells[cell.get("r", "")] = _spreadsheet_string(inline) if inline is not None else ""
        sheets[sheet.get("name", "")] = cells
    return sheets


def _spreadsheet_string(node: etree._Element) -> str:
    return "".join(
        text.text or ""
        for part in node
        if part.tag in {f"{{{_SHEET}}}t", f"{{{_SHEET}}}r"}
        for text in ([part] if part.tag == f"{{{_SHEET}}}t" else part.findall(f"{{{_SHEET}}}t"))
    )


def _range_cells(formula: str, sheets: Mapping[str, Mapping[str, str]], count: int) -> list[str]:
    match = _RANGE.fullmatch(formula)
    if match is None:
        return []
    name = _sheet_name(match)
    first_column = _column_number(match.group(3))
    first_row = int(match.group(4))
    last_column = _column_number(match.group(5) or match.group(3))
    last_row = int(match.group(6) or match.group(4))
    if last_column < first_column or last_row < first_row:
        return []
    cells = sheets.get(name, {})
    return [
        cells.get(f"{_column_name(column)}{row}", "")
        for row in range(first_row, last_row + 1)
        for column in range(first_column, last_column + 1)
    ][:count]


def _sheet_name(match: re.Match[str]) -> str:
    return match.group(1).replace("''", "'") if match.group(1) is not None else match.group(2)


def _column_number(name: str) -> int:
    number = 0
    for letter in name:
        number = number * 26 + ord(letter) - ord("A") + 1
    return number


def _column_name(number: int) -> str:
    name = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        name = chr(ord("A") + remainder) + name
    return name


def _target_name(source: str, target: str) -> str:
    return posixpath.normpath(posixpath.join(posixpath.dirname(source), target)).lstrip("/")


def _rels_name(part_name: str) -> str:
    parent, filename = posixpath.split(part_name)
    return f"{parent}/_rels/{filename}.rels"


def _xml(root: etree._Element) -> bytes:
    return serialize_xml(root)
