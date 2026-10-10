# pyright: strict, reportPrivateUsage=false
"""Tree-level DOCX directives such as ``:drop(row)`` and ``:keep(p)``."""

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast
from uuid import uuid4

from lxml import etree  # pyright: ignore[reportAttributeAccessIssue] lxml has incomplete stubs.

from carbotek.errors import MarkerResolutionError
from carbotek.markers import Marker, parse, scan
from carbotek.markers.syntax import FormatterCall, source_with_formatters
from carbotek.ooxml.ns import qn
from carbotek.render_state import StateSlot
from carbotek.util import find_closest

from .dialect import DIALECT, SHEET, SHEET_NAMESPACE
from .loop_syntax import LOOP_SEGMENT

# Order of lib/preprocessor.js handleDropFormatter (docx): findClosest walks the keys in this order.
_TARGETS = {
    "col": qn("w:tc"),
    "colProperties": qn("w:gridCol"),
    "row": qn("w:tr"),
    "img": qn("w:drawing"),
    "p": qn("w:p"),
    "shape": qn("mc:AlternateContent"),
    "chart": qn("w:drawing"),
    "table": qn("w:tbl"),
}
# lib/preprocessor.js handleDropFormatter (xlsx): table = sheetData, row = row, col = c.
_SHEET_TARGETS = {
    "table": f"{{{SHEET_NAMESPACE}}}sheetData",
    "row": f"{{{SHEET_NAMESPACE}}}row",
    "col": f"{{{SHEET_NAMESPACE}}}c",
}
# The JS marker regex: drop/keep must be the last formatter call; a marker such as
# ``{d.x:keep(tr):ifGT(0)}`` is not a directive and reaches the builder as an unknown formatter.
_DIRECTIVE = re.compile(r"(\{[^{}]+?):(drop|keep)\(([^),]*),?([^)}]*)?\)[^)}]*}")


def is_directive(raw: str) -> bool:
    """Whether a marker is a structural ``:drop``/``:keep`` directive (handled before the builder)."""
    return _DIRECTIVE.fullmatch(raw) is not None


# Set when a loop copy leaves a directive (col, table, ...) for the pass over the whole part.
DEFERRED_DIRECTIVES: StateSlot[bool] = StateSlot("DEFERRED_DIRECTIVES", default=False)

_MARKER_ATTRIBUTE_ELEMENTS = etree.XPath("descendant-or-self::*[@*[contains(., '{')]]")
SPAN_BOUNDARY = "carbotek-directive-boundary"
type _SpanPatch = tuple[etree._Element, etree._Element, str, str, bool, tuple[int, int]]


def compile_directive_spans(
    root: etree._Element,
    *,
    can_promote: Callable[[Marker], bool],
    loop_ranges: Callable[[], tuple[tuple[etree._Element, etree._Element], ...]],
) -> bool:
    """Freeze multi-element ranges before a loop can expand or replace their members."""
    patches: list[_SpanPatch] = []
    targets = _SHEET_TARGETS if DIALECT.get() is SHEET else _TARGETS
    sources: list[tuple[etree._Element, str | None, str]] = [
        (text, None, text.text or "") for text in DIALECT.get().marker_texts(root)
    ]
    sources.extend(
        (node, name, value)
        for node in cast(list[etree._Element], _MARKER_ATTRIBUTE_ELEMENTS(root))
        for name, value in node.attrib.items()
        if "{" in value
    )
    candidates_by_tag: dict[str, list[etree._Element]] = {}
    ranges: tuple[tuple[etree._Element, etree._Element], ...] | None = None
    positions: dict[etree._Element, int] = {}
    for source_index, (text, attribute, content) in enumerate(sources):
        for occurrence in reversed(scan(content)):
            if not is_directive(occurrence.raw):
                continue
            marker = parse(occurrence.raw)
            directive = next((call for call in marker.formatters if call.name in {"drop", "keep"}), None)
            if directive is None or len(directive.arguments) < 2:
                continue
            name = _remove_quote(directive.arguments[0]).strip()
            if name not in {"p", "row"} or _count(directive.arguments[1:]) <= 1:
                continue
            target = _ancestor(text, targets[name])
            if target is None or target.getparent() is None:
                continue
            tag = targets[name]
            if tag not in candidates_by_tag:
                candidates_by_tag[tag] = list(root.iter(tag))
            candidates = candidates_by_tag[tag]
            start = candidates.index(target)
            end = candidates[min(start + _count(directive.arguments[1:]) - 1, len(candidates) - 1)]
            if ranges is None:
                ranges = loop_ranges()
                positions = {node: index for index, node in enumerate(root.iter())}
            end = _extend_over_loops(target, end, ranges, positions)
            condition = _without_directive(marker, directive.name)
            action = "hide" if directive.name == "drop" else "show"
            identifier = f"'ct-span-{uuid4().hex}'"
            begin = source_with_formatters(
                condition, (*condition.formatters, FormatterCall(action + "Begin", (identifier,)))
            )
            finish = f"{{d:{action}End({identifier})}}"  # the end needs no data path or formatter side effects
            crossing = any(first is target and last is end for first, last in ranges)
            patches.append((target, end, begin, finish, crossing, (source_index, occurrence.start)))
            content = content[: occurrence.start] + content[occurrence.end :]
        if attribute is None:
            text.text = content
        else:
            text.set(attribute, content)
    changed = bool(patches)
    if targets is _TARGETS and ranges:
        patches = _remove_internal_span_branches(patches, ranges)
    _apply_span_patches(patches, can_promote, targets is _TARGETS)
    return changed


def _remove_internal_span_branches(
    patches: list[_SpanPatch],
    ranges: tuple[tuple[etree._Element, etree._Element], ...],
) -> list[_SpanPatch]:
    """A span starting inside a loop deletes its nested XML branch before evaluation."""
    removed: set[etree._Element] = set()
    for first, last, *_ in patches:
        parent = first.getparent()
        if parent is None or last.getparent() is not parent:
            continue
        siblings = list(parent)
        closing = next(
            (
                end
                for start, end in ranges
                if start.getparent() is parent
                and end.getparent() is parent
                and siblings.index(start) < siblings.index(first) < siblings.index(end) <= siblings.index(last)
            ),
            None,
        )
        if closing is None:
            continue
        # Keep the loop's closing marker while removing the covered XML on either side.
        branch = [
            element for element in siblings[siblings.index(first) : siblings.index(last) + 1] if element is not closing
        ]
        # Nested iterator layouts retain their structural plan. Removed feature slots
        # are never evaluated, so their postphase ownership does not prevent pruning.
        if any(element.tag != qn("w:p") for element in branch) or any(
            sum(LOOP_SEGMENT.fullmatch(segment) is not None for segment in parse(occurrence.raw).path) > 1
            for element in branch
            for text in element.iter(qn("w:t"))
            for occurrence in scan(text.text or "")
        ):
            continue
        removed.update(branch)
    retained = _retarget_span_ends(patches, removed)
    for element in removed:
        parent = element.getparent()
        if parent is not None:
            parent.remove(element)
    return retained


def _retarget_span_ends(patches: list[_SpanPatch], removed: set[etree._Element]) -> list[_SpanPatch]:
    """Keep surviving controls closed when pruning removes another span's final sibling."""
    retained: list[_SpanPatch] = []
    for first, last, begin, finish, crossing, order in patches:
        if first in removed:
            continue
        while last in removed:
            # The retained first sibling precedes the removed endpoint.
            last = cast(etree._Element, last.getprevious())
        retained.append((first, last, begin, finish, crossing, order))
    return retained


def _apply_span_patches(
    patches: list[_SpanPatch],
    can_promote: Callable[[Marker], bool],
    word: bool,
) -> None:
    groups: dict[tuple[etree._Element, etree._Element], list[tuple[str, str, bool, tuple[int, int]]]] = {}
    for first, last, begin, end, crossing, order in patches:
        groups.setdefault((first, last), []).append((begin, end, crossing, order))
    for (first, last), spans in groups.items():
        values = _crossing_values(first, last, can_promote) if word and spans[0][2] else None
        if word and spans[0][2] and len(spans) > 1:
            # JS keeps the first source control and removes the nested XML branch
            # (extracter.deleteAndMoveNestedParts) before evaluation.
            begin, end, _, _ = min(spans, key=lambda span: span[3])
            parent = cast(etree._Element, first.getparent())
            covered = list(parent)[parent.index(first) : parent.index(last)]
            _insert_span_boundary(first, begin, True, None)
            _insert_span_boundary(last, end, False, None)
            for element in covered:
                parent.remove(element)
            continue
        for begin, end, _, _ in spans:
            _insert_span_boundary(first, begin, True, values)
            _insert_span_boundary(last, end, False, None)
            values = None


def _insert_span_boundary(target: etree._Element, marker: str, before: bool, values: tuple[str, bool] | None) -> None:
    boundary = etree.Element(SPAN_BOUNDARY)
    etree.SubElement(boundary, DIALECT.get().text).text = marker
    if values is not None:
        boundary.set("crossing-layout", "1")
        etree.SubElement(boundary, DIALECT.get().text).text = values[0]
        boundary.set("crossing-static-copies", "1" if values[1] else "0")
    if before:
        target.addprevious(boundary)
    else:
        target.addnext(boundary)


def _crossing_values(
    first: etree._Element, last: etree._Element, can_promote: Callable[[Marker], bool]
) -> tuple[str, bool] | None:
    """Separate ordinary loop slots from the once-only static XML of a crossing span."""
    parent = first.getparent()
    if parent is None or last.getparent() is not parent:
        return None
    elements = list(parent)[parent.index(first) : parent.index(last)]
    patches: list[tuple[etree._Element, str]] = []
    values: list[str] = []
    repeat_static = False
    for element in elements:
        if element.tag != qn("w:p"):
            return None
        for node in element.iter():
            for value in node.attrib.values():
                for occurrence in scan(value):
                    marker = parse(occurrence.raw)
                    if marker.formatters or any(LOOP_SEGMENT.fullmatch(segment) for segment in marker.path):
                        return None
                    repeat_static = True
        for node in element.iter(qn("w:t")):
            source = node.text or ""
            pieces: list[str] = []
            position = 0
            for occurrence in scan(source):
                marker = parse(occurrence.raw)
                # Feature XML and nested/block pipelines keep their existing tree plan.
                if not can_promote(marker):
                    return None
                iterator_count = sum(LOOP_SEGMENT.fullmatch(segment) is not None for segment in marker.path)
                if iterator_count > 1:
                    return None
                if iterator_count == 0:
                    continue
                pieces.append(source[position : occurrence.start])
                values.append(occurrence.raw)
                position = occurrence.end
            pieces.append(source[position:])
            patches.append((node, "".join(pieces)))
    if not values:
        return None
    for node, source in patches:
        node.text = source
    return "".join(values), repeat_static


def _extend_over_loops(
    first: etree._Element,
    last: etree._Element,
    ranges: tuple[tuple[etree._Element, etree._Element], ...],
    positions: dict[etree._Element, int],
) -> etree._Element:
    # An outside block covers every copy of an included template member. Its end stays outside.
    changed = True
    while changed:
        changed = False
        for begin, end in ranges:
            if positions[first] < positions[begin] <= positions[last] < positions[end]:
                last = end
                changed = True
    return last


def remove_span_boundaries(root: etree._Element) -> None:
    for boundary in tuple(root.iter(SPAN_BOUNDARY)):
        parent = boundary.getparent()
        if parent is not None:
            if boundary.get("crossing-layout") == "1":
                value = "".join(boundary.itertext())
                previous = boundary.getprevious()
                if previous is None:
                    parent.text = (parent.text or "") + value
                else:
                    previous.tail = (previous.tail or "") + value
            parent.remove(boundary)


def marker_nodes_outside(
    root: etree._Element, rendered: set[etree._Element]
) -> tuple[list[etree._Element], list[etree._Element]]:
    """``w:t`` nodes and elements with a ``{`` in their text / an attribute, in document order.

    Rendered loop copies (``rendered``) are skipped whole: only the rest of the part is walked.
    """
    texts: list[etree._Element] = []
    attributed: list[etree._Element] = []
    tags = DIALECT.get().texts
    stack = [root]
    while stack:
        node = stack.pop()
        if node.tag in tags and (content := node.text) and "{" in content:
            texts.append(node)
        if any("{" in value for value in node.attrib.values()):
            attributed.append(node)
        stack.extend(child for child in reversed(node) if isinstance(child.tag, str) and child not in rendered)
    return texts, attributed


@dataclass(frozen=True, slots=True)
class StructuralDirectiveExecutor:
    """Apply directives without using synthetic tags or string XML surgery."""

    def apply(
        self,
        root: etree._Element,
        resolve: Callable[[Marker], Any],
        *,
        loop_copy: bool = False,
        rendered: set[etree._Element] | None = None,
    ) -> None:
        """Remove directive markers and their selected elements when applicable.

        In a loop copy (``loop_copy``) only directives whose target lies inside the copy run; the
        others (a column, the enclosing table) keep their marker for the pass over the whole part.
        """
        removals: list[etree._Element] = []
        scope = root if loop_copy else None
        if rendered:
            texts, attributed = marker_nodes_outside(root, rendered)
        else:
            texts = DIALECT.get().marker_texts(root)
            attributed = cast(list[etree._Element], _MARKER_ATTRIBUTE_ELEMENTS(root))
        for text in texts:
            if text.text and "{" in text.text:
                text.text = _process_directives(text, text.text, resolve, removals, scope)
        for element in attributed:
            for attribute, content in tuple(element.attrib.items()):
                if "{" in content:
                    element.set(attribute, _process_directives(element, content, resolve, removals, scope))
        for element in _outermost(removals):
            parent = element.getparent()
            if parent is not None:
                parent.remove(element)


def _process_directives(
    element: etree._Element,
    content: str,
    resolve: Callable[[Marker], Any],
    removals: list[etree._Element],
    scope: etree._Element | None = None,
) -> str:
    for occurrence in reversed(scan(content)):
        if not is_directive(occurrence.raw):
            continue
        marker = parse(occurrence.raw)
        directive = next((item for item in marker.formatters if item.name in {"drop", "keep"}), None)
        if directive is None:
            continue
        target_name = _remove_quote(str(directive.arguments[0]) if directive.arguments else "").strip()
        targets = _SHEET_TARGETS if DIALECT.get() is SHEET else _TARGETS
        target_tag = targets.get(target_name)
        if scope is not None and target_tag is not None and not _inside(element, target_tag, target_name, scope):
            DEFERRED_DIRECTIVES.set(True)
            continue  # resolved later by the pass over the whole part
        if target_tag is None:
            action = directive.name
            closest = find_closest(target_name, targets)
            raise MarkerResolutionError(
                f'Unknown parameter in formatter {action}() of "{occurrence.raw}". Do you mean ":{action}({closest})"?'
            )
        content = f"{content[: occurrence.start]}{content[occurrence.end :]}"
        should_remove = bool(resolve(_without_directive(marker, directive.name)))
        if directive.name == "keep":
            should_remove = not should_remove
        if should_remove:
            target = _ancestor(element, target_tag)
            if target is None:
                continue  # JS finds no enclosing element and leaves the document unchanged
            if target_name == "col" and targets is _TARGETS:
                removals.extend(_column_targets(target))
            elif target_name == "col":
                removals.extend(_sheet_cells_from(target))
            elif target_name == "colProperties":
                removals.extend(_grid_column_targets(target))
            else:
                removals.extend(
                    _targets_to_remove(target, directive.arguments[1:] if target_name in {"p", "row"} else ())
                )
    return content


def _inside(element: etree._Element, target_tag: str, target_name: str, scope: etree._Element) -> bool:
    """Whether the directive's target belongs to the loop copy being rendered."""
    if target_name in {"col", "colProperties"}:
        return False  # a column spans rows outside the copy
    target = _ancestor(element, target_tag)
    return target is not None and (target is scope or scope in target.iterancestors())


def _remove_quote(value: str) -> str:
    """Port of ``helper.removeQuote``: strip one pair of surrounding quotes."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _without_directive(marker: Marker, name: str) -> Marker:
    position = next(index for index, formatter in enumerate(marker.formatters) if formatter.name == name)
    return Marker(
        marker.root, marker.path, marker.formatters[:position] + marker.formatters[position + 1 :], marker.raw
    )


def _ancestor(element: etree._Element, target: str) -> etree._Element | None:
    current: etree._Element | None = element
    while current is not None:
        if current.tag == target:
            return current
        current = current.getparent()
    return None


def _outermost(elements: list[etree._Element]) -> tuple[etree._Element, ...]:
    unique = {id(element): element for element in elements}
    return tuple(
        element
        for element in unique.values()
        if all(ancestor not in unique.values() for ancestor in element.iterancestors())
    )


def _targets_to_remove(element: etree._Element, arguments: tuple[str, ...]) -> tuple[etree._Element, ...]:
    """Return the requested element and, for rows/paragraphs, following siblings."""
    count = _count(arguments)
    parent = element.getparent()
    if parent is None or count == 1:
        return (element,)
    siblings = list(parent)
    start = siblings.index(element)
    return tuple(sibling for sibling in siblings[start:] if sibling.tag == element.tag)[:count]


def _count(arguments: tuple[str, ...]) -> int:
    """JS ``parseInt(count) || 1``: a missing, zero or non-numeric count hides one element."""
    text = _remove_quote(arguments[0]).strip() if arguments else ""
    digits = re.match(r"[+-]?\d+", text)
    count = int(digits.group(0)) if digits else 0
    return count if count > 0 else 1


def _sheet_cells_from(cell: etree._Element) -> tuple[etree._Element, ...]:
    """XLSX ``drop(col)``: the cell and every cell after it in its row.

    JS ``findPosXmlElement`` without a count keeps extending the element over directly adjacent
    ``<c>...</c>`` siblings, and the row it looks for other cells in extends over the adjacent rows
    too, so no other row is touched.
    """
    return (cell, *(sibling for sibling in cell.itersiblings() if sibling.tag == cell.tag))


def _column_targets(cell: etree._Element) -> tuple[etree._Element, ...]:
    """Remove a table column consistently, including its grid declaration."""
    row = _ancestor(cell, qn("w:tr"))
    table = _ancestor(cell, qn("w:tbl"))
    if row is None or table is None:
        return (cell,)
    column = 0
    for child in row:
        if child is cell:
            break
        if child.tag == qn("w:tc"):
            column += _span(child)
    return _column_targets_from_index(table, column)


def _span(cell: etree._Element) -> int:
    span = cell.find(f"{qn('w:tcPr')}/{qn('w:gridSpan')}")
    try:
        return max(1, int(span.get(qn("w:val"), "1"))) if span is not None else 1
    except ValueError:
        return 1


def _grid_column_targets(column: etree._Element) -> tuple[etree._Element, ...]:
    grid = _ancestor(column, qn("w:tblGrid"))
    table = _ancestor(column, qn("w:tbl"))
    if grid is None or table is None:
        return (column,)
    columns = [child for child in grid if child.tag == qn("w:gridCol")]
    return _column_targets_from_index(table, columns.index(column))


def _column_targets_from_index(table: etree._Element, index: int) -> tuple[etree._Element, ...]:
    """Cells covering grid column ``index`` in every row, counted in ``w:gridSpan`` units.

    A merged cell spanning the column is kept with its span reduced by one (C5 guard).
    """
    targets: list[etree._Element] = []
    for table_row in table.iterchildren(qn("w:tr")):
        position = 0
        for cell in (child for child in table_row if child.tag == qn("w:tc")):
            span = _span(cell)
            if position <= index < position + span:
                if span == 1:
                    targets.append(cell)
                else:
                    cell.find(f"{qn('w:tcPr')}/{qn('w:gridSpan')}").set(qn("w:val"), str(span - 1))  # type: ignore[union-attr]
                break
            position += span
    grid = table.find(qn("w:tblGrid"))
    if grid is not None:
        columns = [child for child in grid if child.tag == qn("w:gridCol")]
        if index < len(columns):
            targets.append(columns[index])
    return tuple(targets)
