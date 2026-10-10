# pyright: strict, reportPrivateUsage=false
"""Tree-first expansion of paragraph-level Carbotek array loops."""

import re
from collections import deque
from collections.abc import Callable, Iterable, Iterator, Mapping
from copy import deepcopy
from dataclasses import dataclass, replace
from functools import lru_cache
from typing import Any

from lxml import etree  # pyright: ignore[reportAttributeAccessIssue] lxml has incomplete stubs.

from carbotek.errors import MarkerResolutionError
from carbotek.markers import Marker, parse, scan
from carbotek.markers.syntax import printed
from carbotek.ooxml.ns import qn
from carbotek.render_state import StateSlot
from carbotek.typing_helpers import is_list, is_mapping

from .dialect import DIALECT, SHEET
from .directives import SPAN_BOUNDARY, is_directive
from .loop_syntax import LOOP_SEGMENT
from .session import CURRENT_RENDER
from .strings import (
    LOOP_PLACEHOLDER,
    StringSection,
    TextPiece,
    compile_section,
    has_blocks,
    placeholder_safe,
    rendered_placeholder,
)
from .text import ParagraphText

type ElementRenderer = Callable[[etree._Element, Mapping[str, Any]], None]
_UNDEFINED = object()
# A loop segment of a marker path: ``rows[i]``, ``rows[i+1]``, ``rows[i++]``, a sorting iterator
# ``rows[ord]`` (any word, extracter.js: "a word before ',' or ']' means a loop"), with optional
# filters ``[i, qty>0]`` and repeater ``[i+1*qty]``.


# First path segment of complement (``c.``) loops: the expander sees ``c`` as a key of the data.
COMPLEMENT_ROOT = "\x00c"


@dataclass(frozen=True, slots=True)
class _Boundary:
    position: int
    key: str
    source_path: tuple[str, ...]
    end: bool
    filters: tuple[str, ...] = ()
    repeater: str | None = None
    iterator: str = "i"
    raw: str = ""
    read_text: str = ""


class LoopExpander:
    """Expands paired loop markers using direct XML sibling boundaries.

    ``i++`` is Carbotek's special loop-start iterator.  The JavaScript engine
    needs a prepass for its string-position builder; in this tree renderer the
    same block boundaries are already explicit, so it has the same expansion
    behaviour as ``i`` and still closes with ``i+1``.
    """

    @staticmethod
    def template_ranges(root: etree._Element) -> tuple[tuple[etree._Element, etree._Element], ...]:
        """Original sibling ranges, available to preprocessors before any copy is created."""
        ranges: list[tuple[etree._Element, etree._Element]] = []
        for parent in _loop_parents(root):
            boundaries = LoopExpander._boundaries(parent, {})
            closed_until: dict[str, int] = {}
            for start in boundaries:
                if start.end or start.position <= closed_until.get(start.key, -1):
                    continue
                end = next(
                    (
                        item
                        for item in boundaries
                        if item.end and item.key == start.key and item.position >= start.position
                    ),
                    None,
                )
                if end is not None:
                    # Fields inside this pair are members, not new overlapping loops.
                    closed_until[start.key] = end.position
                    ranges.append((parent[start.position], parent[end.position]))
        return tuple(ranges)

    def expand(
        self,
        root: etree._Element,
        data: Mapping[str, Any] | list[Any],
        render: ElementRenderer,
        iterators: Mapping[str, Any] | None = None,
    ) -> None:
        """Expand every currently discoverable paragraph loop in document order."""
        self._validate_boundaries(root, iterators or {})
        registry = RENDERED_COPIES.get() if not iterators else None
        while self._expand_one(root, data, render, iterators or {}, _rendered(registry)):
            pass

    def _validate_boundaries(self, root: etree._Element, iterators: Mapping[str, Any]) -> None:
        """Reject malformed loop pairs before modifying the OOXML tree."""
        cache: dict[etree._Element, tuple[_Boundary, ...]] = {}
        for parent in _loop_parents(root):
            boundaries = _validation_boundaries(parent, iterators, cache)
            for boundary in boundaries:
                if boundary.end:
                    paired = any(
                        not item.end and item.key == boundary.key and item.position <= boundary.position
                        for item in boundaries
                    )
                else:
                    paired = any(
                        item.end and item.key == boundary.key and item.position >= boundary.position
                        for item in boundaries
                    )
                if not paired and not self._paired_in_ancestor(parent, boundary, iterators, cache):
                    raise MarkerResolutionError(_unpaired_message(boundary))

    @staticmethod
    def _paired_in_ancestor(
        parent: etree._Element,
        boundary: _Boundary,
        iterators: Mapping[str, Any],
        cache: dict[etree._Element, tuple[_Boundary, ...]],
    ) -> bool:
        """Table row boundaries are paired by their table ancestor, not the row.

        The pair must lie on the right side in the ancestor too: a start needs an end in a later
        child, an end a start in an earlier one (JS rejects ``[i]`` after the loop's ``[i+1]``).
        """
        child, ancestor = parent, parent.getparent()
        while ancestor is not None:
            position = ancestor.index(child)
            candidates = _validation_boundaries(ancestor, iterators, cache)
            if boundary.end:
                paired = any(
                    not item.end and item.key == boundary.key and item.position < position for item in candidates
                )
            else:
                paired = any(item.end and item.key == boundary.key and item.position > position for item in candidates)
            if paired:
                return True
            child, ancestor = ancestor, ancestor.getparent()
        return False

    def _expand_one(
        self,
        root: etree._Element,
        data: Mapping[str, Any] | list[Any],
        render: ElementRenderer,
        iterators: Mapping[str, Any],
        rendered: frozenset[etree._Element] = frozenset(),
    ) -> bool:
        # Rendered loop copies hold no template markers: their (large) subtrees are not scanned again.
        for parent in _loop_parents(root, rendered):
            boundaries = self._boundaries(parent, iterators, rendered)
            for start in boundaries:
                if start.end:
                    continue
                end = next(
                    (
                        item
                        for item in boundaries
                        if item.end and item.key == start.key and item.position >= start.position
                    ),
                    None,
                )
                if end is None:
                    continue
                if start.position == end.position and not self._repeats_text(parent[start.position], start, iterators):
                    continue  # both markers in one child: the nearest common ancestor is deeper
                self._replace(parent, start, end, data, render, iterators)
                return True
        return False

    @staticmethod
    def _boundaries(
        parent: etree._Element,
        iterators: Mapping[str, Any] | None = None,
        rendered: frozenset[etree._Element] = frozenset(),
    ) -> tuple[_Boundary, ...]:
        boundaries: list[_Boundary] = []
        for position, child in enumerate(parent):
            if not isinstance(child.tag, str) or child.tag in _NO_LOOP_CHILDREN or child in rendered:
                continue
            boundaries.extend(
                boundary
                for occurrence in scan(_text_content(child))
                if (boundary := _raw_boundary(occurrence.raw, position, iterators or {})) is not None
            )
        return tuple(boundaries)

    @staticmethod
    def _repeats_text(child: etree._Element, start: _Boundary, iterators: Mapping[str, Any]) -> bool:
        """Both markers in one child: repeat text only when no deeper child separates them.

        A loop repeats the part between the markers extended to their nearest common
        ancestor: cells of one row repeat the cell, runs of one paragraph repeat the runs, and
        two markers in one text node repeat the text between them.
        """
        if child.tag != DIALECT.get().paragraph:
            return False
        inner = LoopExpander._boundaries(child, iterators)
        starts = {item.position for item in inner if not item.end and item.key == start.key}
        ends = {item.position for item in inner if item.end and item.key == start.key}
        return not any(first < last for first in starts for last in ends)

    @staticmethod
    def _replace(
        parent: etree._Element,
        start: _Boundary,
        end: _Boundary,
        data: Mapping[str, Any] | list[Any],
        render: ElementRenderer,
        iterators: Mapping[str, Any],
    ) -> None:
        if parent[start.position].get("crossing-layout") == "1":
            _replace_crossing(parent, start, end, data, render, iterators)
        else:
            LoopExpander._replace_regular(parent, start, end, data, render, iterators)

    @staticmethod
    def _replace_regular(
        parent: etree._Element,
        start: _Boundary,
        end: _Boundary,
        data: Mapping[str, Any] | list[Any],
        render: ElementRenderer,
        iterators: Mapping[str, Any],
    ) -> None:
        array = _array_at(data, start.source_path, iterators)
        values = _sorted(_filter_values(array, start.filters), start.iterator)
        start = replace(start, repeater=start.repeater or end.repeater)
        if start.position == end.position and parent[start.position].tag == DIALECT.get().paragraph:
            LoopExpander._replace_inline(parent[start.position], start, end, (array, values), data, render, iterators)
            return
        removed = list(parent)[start.position : end.position + 1]
        original = removed[:-1]
        insertion = start.position
        expander = LoopExpander()
        nested = _has_nested_loop(original, {**iterators, start.key: None})
        produced: list[etree._Element] = []
        # What the copies can contain is known from the template section: tell the renderer once.
        copy_features = _copy_features(original)
        features = COPY_FEATURES.set(copy_features)
        try:
            # Without nested loops and directives a copy keeps its shape: no holder block is needed.
            blocks = has_blocks(original)
            # A conditional block can span the elements of a copy: the copy is rendered as one piece.
            flat = not nested and not copy_features.directives and not blocks and not copy_features.bookmarks
            section = (
                _string_section(original, data, (*iterators, start.key))
                if not copy_features.directives and not copy_features.bookmarks
                else None
            )
            keepable = (
                not iterators
                and KEEP_RENDERED.get()
                and placeholder_safe(original, parent)
                and not any(node.tag == SPAN_BOUNDARY for element in original for node in element.iter())
            )
            if section is not None and section.heavy and not keepable:
                section = None
            if section is not None:
                # Copies rendered as strings, parsed in one call; outermost copies that the passes after
                # rendering do not need stay text until the part is serialized (no tree at all).
                # Validate repeat counts before evaluating any fields, as the eager iterator list did.
                counts = [_repeat_count(value, start.repeater) for value in values] if start.repeater else None
                items = _section_items(array, values, iterators, start.key, counts)
                xml = _section_xml(section, values, start.key, counts is None and not iterators, keepable, items)
                kept = rendered_placeholder(xml, *section.wrapper) if xml and keepable else None
                produced = [kept] if kept is not None else section.parse(xml)
                values = []
            positions = _positions(array) if values else {}
            for value in values:
                iterator = _iteration(iterators, start.key, positions, value)
                for _ in range(_repeat_count(value, start.repeater)):
                    if flat:
                        for element in original:
                            clone = deepcopy(element)
                            render(clone, iterator)
                            produced.append(clone)
                        continue
                    block = etree.Element(parent.tag)
                    for element in original:
                        block.append(deepcopy(element))
                    if nested:
                        expander.expand(block, data, render, iterator)
                    if blocks or copy_features.bookmarks:
                        render(block, iterator)
                        produced.extend(block)
                        continue
                    for clone in list(block):
                        render(clone, iterator)
                        if clone.getparent() is not block:
                            continue  # a :drop/:keep directive removed this copy
                        produced.append(clone)
        finally:
            COPY_FEATURES.reset(features)
        for element in removed:
            parent.remove(element)
        # One slice assignment: inserting copy by copy is quadratic in the number of siblings.
        parent[insertion:insertion] = produced
        if not iterators and (registry := RENDERED_COPIES.get()) is not None:
            registry.append(produced)

    @staticmethod
    def first_loop(
        root: etree._Element, iterators: Mapping[str, Any]
    ) -> tuple[etree._Element, _Boundary, _Boundary] | None:
        """The loop ``_expand_one`` would expand next: (parent, start, end)."""
        for parent in _loop_parents(root):
            boundaries = LoopExpander._boundaries(parent, iterators)
            for start in boundaries:
                if start.end:
                    continue
                end = next(
                    (
                        item
                        for item in boundaries
                        if item.end and item.key == start.key and item.position >= start.position
                    ),
                    None,
                )
                if end is None:
                    continue
                if start.position == end.position and not LoopExpander._repeats_text(
                    parent[start.position], start, iterators
                ):
                    continue
                return parent, start, end
        return None

    @staticmethod
    def _replace_inline(
        paragraph: etree._Element,
        start: _Boundary,
        end: _Boundary,
        items: tuple[list[Any], list[Any]],
        data: Mapping[str, Any] | list[Any],
        render: ElementRenderer,
        iterators: Mapping[str, Any],
    ) -> None:
        array, values = items
        dialect = DIALECT.get()
        projection = ParagraphText.from_paragraph(paragraph)
        occurrences = scan(projection.value)
        opening = next(
            occurrence
            for occurrence in occurrences
            if (boundary := _boundary(parse(occurrence.raw), start.position, iterators)) is not None
            and boundary.key == start.key
            and not boundary.end
        )
        closing = next(
            occurrence
            for occurrence in occurrences
            if occurrence.start >= opening.end
            if (boundary := _boundary(parse(occurrence.raw), end.position, iterators)) is not None
            and boundary.key == end.key
            and boundary.end
        )
        template = projection.value[opening.start : closing.start]
        pieces: list[str] = []
        positions = _positions(array)
        for value in values:
            iterator = _iteration(iterators, start.key, positions, value)
            for _ in range(_repeat_count(value, start.repeater)):
                fragment = etree.Element(dialect.paragraph)
                etree.SubElement(etree.SubElement(fragment, dialect.run), dialect.text).text = template
                holder = etree.Element("carbotek-holder")
                holder.append(fragment)
                LoopExpander().expand(holder, data, render, iterator)  # nested loops in the same paragraph
                render(fragment, iterator)
                pieces.append(_text_content(fragment))
        opening_text, opening_offset = projection.locate(opening.start)
        closing_text, closing_offset = projection.locate(closing.end)
        repeated = "".join(pieces)
        if opening_text is closing_text:
            content = opening_text.text or ""
            opening_text.text = content[:opening_offset] + repeated + content[closing_offset:]
            return
        opening_text.text = (opening_text.text or "")[:opening_offset] + repeated
        clearing = False
        for text in paragraph.iter(*dialect.texts):
            if text is opening_text:
                clearing = True
            elif text is closing_text:
                text.text = (text.text or "")[closing_offset:]
                break
            elif clearing:
                text.text = ""


INDEX_SUFFIX = "#index"


@dataclass(frozen=True, slots=True)
class CopyFeatures:
    """Markers a loop copy can hold, derived once from the repeated template section."""

    attribute_markers: bool
    directives: bool
    # showBegin/hideBegin blocks hide plain text too: then every text node must be visited.
    blocks: bool = True
    bookmarks: bool = True


# Whether outermost loop copies may stay XML text until serialization (decided per part).
KEEP_RENDERED: StateSlot[bool] = StateSlot("KEEP_RENDERED", default=False)

# Set while the copies of one loop are rendered; None outside loops (render everything).
COPY_FEATURES: StateSlot[CopyFeatures | None] = StateSlot("COPY_FEATURES", default=None)
# Outermost loop copies of the part being rendered (already fully rendered), one run of siblings per loop.
RENDERED_COPIES: StateSlot[list[list[etree._Element]] | None] = StateSlot("RENDERED_COPIES", default=None)

# Whether the part being rendered has conditional blocks anywhere (they can span loop copies).
PART_HAS_BLOCKS: StateSlot[bool] = StateSlot("PART_HAS_BLOCKS", default=True)


def _validation_boundaries(
    parent: etree._Element,
    iterators: Mapping[str, Any],
    cache: dict[etree._Element, tuple[_Boundary, ...]],
) -> tuple[_Boundary, ...]:
    """Reuse scans while validation observes one immutable template tree/scope."""
    if parent not in cache:
        cache[parent] = LoopExpander._boundaries(parent, iterators)
    return cache[parent]


def _replace_crossing(
    parent: etree._Element,
    start: _Boundary,
    end: _Boundary,
    data: Mapping[str, Any] | list[Any],
    render: ElementRenderer,
    iterators: Mapping[str, Any],
) -> None:
    """Evaluate input-order controls/slots before arranging the slots by iterator order."""
    array = _array_at(data, start.source_path, iterators)
    values = _filter_values(array, start.filters)
    start = replace(start, repeater=start.repeater or end.repeater)
    removed = list(parent)[start.position : end.position + 1]
    boundary = removed[0]
    controls: list[etree._Element] = []
    groups: dict[int, deque[list[etree._Element]]] = {}
    static_groups: dict[int, deque[list[etree._Element]]] = {}
    repeat_static = boundary.get("crossing-static-copies") == "1"
    token = COPY_FEATURES.set(_copy_features(removed[:-1]))
    try:
        positions = _positions(array)
        counts = [_repeat_count(value, start.repeater) for value in values]
        for value, count in zip(values, counts, strict=True):
            iterator = _iteration(iterators, start.key, positions, value)
            group: list[etree._Element] = []
            static_group: list[etree._Element] = []
            for _ in range(count):
                control = deepcopy(boundary)
                control.remove(control[1])
                render(control, iterator)
                controls.append(control)
                slot = deepcopy(boundary)
                slot.remove(slot[0])
                render(slot, iterator)
                group.append(slot)
                if repeat_static:
                    # Attribute slots make this XML independent of the hidden value span.
                    static_group.extend(deepcopy(element) for element in removed[1:-1])
            groups.setdefault(id(value), deque()).append(group)
            static_groups.setdefault(id(value), deque()).append(static_group)
        sorted_values = _sorted(values, start.iterator)
        produced = controls + [node for value in sorted_values for node in groups[id(value)].popleft()]
        static_copies = (
            [node for value in sorted_values for node in static_groups[id(value)].popleft()] if repeat_static else []
        )
        if not repeat_static and produced:
            for element in removed[1:-1]:
                clone = deepcopy(element)
                render(clone, iterators)
                produced.append(clone)
    finally:
        COPY_FEATURES.reset(token)
    for element in removed:
        parent.remove(element)
    parent[start.position : start.position] = produced
    if static_copies:
        # The generated end closes the value span before the attribute-bearing XML.
        # Leave these copies for the enclosing pass, which evaluates their static slots.
        insertion = start.position + len(produced) + 1
        parent[insertion:insertion] = static_copies
    if not iterators and (registry := RENDERED_COPIES.get()) is not None:
        registry.append(produced)


def _string_section(
    original: list[etree._Element], data: Mapping[str, Any] | list[Any], keys: tuple[str, ...]
) -> StringSection | None:
    """``original`` compiled for string rendering, nested loops included; None for the tree path.

    Nested loops are found on a copy of the template exactly as ``LoopExpander.expand`` finds them
    in each rendered copy, and replaced by placeholders rendering their own compiled section.
    """
    holder = etree.Element("carbotek-holder")
    for element in original:
        holder.append(deepcopy(element))
    iterators = dict.fromkeys(keys)
    loops: list[TextPiece] = []
    try:
        LoopExpander()._validate_boundaries(holder, iterators)
        while (found := LoopExpander.first_loop(holder, iterators)) is not None:
            parent, start, end = found
            if start.position == end.position:
                return None  # a loop inside one paragraph repeats text: tree renderer
            start = replace(start, repeater=start.repeater or end.repeater)
            removed = list(parent)[start.position : end.position + 1]
            inner = _string_section(removed[:-1], data, (*keys, start.key))
            if inner is None:
                return None
            placeholder = etree.Element(LOOP_PLACEHOLDER, n=str(len(loops)))
            loops.append(_nested_loop(start, inner, data))
            for element in removed:
                parent.remove(element)
            parent.insert(start.position, placeholder)
    except MarkerResolutionError:
        return None  # the tree renderer reports it (or not, for an empty array) as before
    return compile_section(list(holder), loops, scope=original)


def _section_xml(
    section: StringSection,
    values: list[Any],
    key: str,
    native_allowed: bool,
    keepable: bool,
    items: Iterable[Mapping[str, Any]],
) -> str | bytes:
    native = (
        (section.render_plain_bytes(values, key) if keepable else section.render_plain_values(values, key))
        if native_allowed
        else None
    )
    if native is not None:
        session = CURRENT_RENDER.get()
        if session is not None:
            if isinstance(native, bytes):
                return session.protect(native.decode("utf-8")).encode("utf-8") if b"{" in native else native
            return session.protect(native)
        return native
    if keepable and (byte_xml := section.render_xml_bytes(items)) is not None:
        return byte_xml
    return section.render_xml(items)


def _nested_loop(start: _Boundary, section: StringSection, data: Mapping[str, Any] | list[Any]) -> TextPiece:
    def piece(iterators: Mapping[str, Any]) -> str:
        array = _array_at(data, start.source_path, iterators)
        values = _sorted(_filter_values(array, start.filters), start.iterator)
        if start.repeater is None:
            native = section.render_plain_values(values, start.key)
            if native is not None:
                session = CURRENT_RENDER.get()
                return session.protect(native) if session is not None else native
        positions = _positions(array)
        return "".join(
            section.text(iterator)
            for value in values
            for iterator in (_iteration(iterators, start.key, positions, value),)
            for _ in range(_repeat_count(value, start.repeater))
        )

    return piece


def _copy_features(elements: list[etree._Element]) -> CopyFeatures:
    attribute_markers = False
    directives = False
    bookmarks = False
    for element in elements:
        for node in element.iter():
            bookmarks = bookmarks or node.tag == _BOOKMARK_START
            for value in node.attrib.values():
                if "{" in value:
                    attribute_markers = True
                    directives = directives or any(is_directive(item.raw) for item in scan(value))
            if node.text and any(is_directive(item.raw) for item in scan(node.text)):
                directives = True
    return CopyFeatures(attribute_markers, directives, PART_HAS_BLOCKS.get(), bookmarks)


_BOOKMARK_START = qn("w:bookmarkStart")


def _has_nested_loop(elements: list[etree._Element], iterators: Mapping[str, Any]) -> bool:
    """Whether the repeated section holds another loop (checked once, not for every copy)."""
    texts = DIALECT.get().texts
    for element in elements:
        for text in element.iter(*texts):
            for occurrence in scan(text.text or ""):
                if "[" in occurrence.raw and _raw_boundary(occurrence.raw, 0, iterators) is not None:
                    return True
    return False


def _rendered(registry: list[list[etree._Element]] | None) -> frozenset[etree._Element]:
    return frozenset(copy for run in registry for copy in run) if registry else frozenset()


def _iter_outside(root: etree._Element, rendered: frozenset[etree._Element]) -> Iterator[etree._Element]:
    """Elements of ``root`` in document order, skipping the subtrees of ``rendered`` elements."""
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(child for child in reversed(node) if isinstance(child.tag, str) and child not in rendered)


def _loop_parents(root: etree._Element, rendered: frozenset[etree._Element] = frozenset()) -> list[etree._Element]:
    """Elements, in document order, that contain a text node with ``[`` (every loop marker has one).

    Markers are whole inside one ``w:t`` after normalization, so only the ancestors of such text
    nodes can hold loop boundaries; the rest of the tree is skipped.
    """
    elements = list(_iter_outside(root, rendered)) if rendered else None
    tags = DIALECT.get().texts
    texts = (node for node in elements if node.tag in tags) if elements is not None else root.iter(*tags)
    candidates: set[etree._Element] = set()
    for text in texts:
        if "[" not in (text.text or ""):
            continue
        node = text.getparent()
        while node is not None and node not in candidates:
            candidates.add(node)
            if node is root:
                break
            node = node.getparent()
    return [
        element
        for element in (elements if elements is not None else root.iter())
        if element in candidates and isinstance(element.tag, str) and element.tag not in _NO_LOOP_PARENTS
    ]


# Elements whose children are never repeated sections (run content, properties, drawings).
_NO_LOOP_PARENTS = frozenset(
    {qn("w:r"), qn("w:t"), qn("w:rPr"), qn("w:pPr"), qn("w:tblPr"), qn("w:trPr"), qn("w:tcPr"), qn("w:drawing")}
    | {qn("w:sectPr"), qn("w:tblGrid"), qn("w:instrText"), qn("w:fldChar"), qn("w:bookmarkStart")}
    | {SHEET.run, SHEET.run_properties, *(f"{{{SHEET.namespace}}}{name}" for name in ("t", "v", "f"))}
)

# Children that never carry a repeated section (properties); markers there are not loop bounds.
_NO_LOOP_CHILDREN = frozenset({qn("w:pPr"), qn("w:rPr"), qn("w:tblPr"), qn("w:trPr"), qn("w:tcPr"), qn("w:sdtPr")})


def _section_items(
    array: list[Any],
    values: list[Any],
    iterators: Mapping[str, Any],
    key: str,
    counts: list[int] | None,
) -> Iterator[Mapping[str, Any]]:
    """Build the index map only if rendering consumes Python iterator scopes."""
    positions = _positions(array)
    for index, value in enumerate(values):
        iterator = _iteration(iterators, key, positions, value)
        for _ in range(counts[index] if counts is not None else 1):
            yield iterator


def _positions(array: list[Any]) -> dict[int, int]:
    """Index of each item in the unfiltered array (first occurrence), computed once per loop."""
    positions: dict[int, int] = {}
    for position, item in enumerate(array):
        positions.setdefault(id(item), position)
    return positions


def _iteration(iterators: Mapping[str, Any], key: str, positions: dict[int, int], value: Any) -> dict[str, Any]:
    """Iterator scope of one item: the item under ``key`` and its position in the unfiltered array.

    The position (``key + INDEX_SUFFIX``) lets deferred markers be frozen to ``field[N]`` so that
    features resolved after the loop expansion (images, links, HTML, colors) see the right item.
    It is inserted before the item because relative arguments use the last iterator value.
    """
    return {**iterators, key + INDEX_SUFFIX: positions.get(id(value), 0), key: value}


def _unpaired_message(boundary: _Boundary) -> str:
    """JS extracter texts for a loop start without ``[i+1]`` and an end without ``[i]``."""
    marker = printed(boundary.raw)
    if boundary.end:
        bracket = boundary.read_text.rfind("[")
        missing = boundary.read_text[:bracket] + boundary.read_text[bracket:].replace("+1", "")
        return (
            f"The marker {marker} has no corresponding {{{missing}...}} marker before or is not placed correctly. "
            "Please add markers to describe the i-th section of the repetition."
        )
    name = boundary.source_path[-1] if boundary.source_path else boundary.key
    return (
        f'The marker {marker} has no corresponding [i+1] for array "{name}". '
        f"Add at least one tag with {name}[i+1] to describe where is the i+1-th item."
    )


def _raw_boundary(raw: str, position: int, iterators: Mapping[str, Any]) -> _Boundary | None:
    """``_boundary`` of a marker text, decided once per (marker, open loops) and moved to ``position``."""
    if "[" not in raw:
        return None
    boundary = _cached_boundary(raw, tuple(iterators))
    if boundary is None or boundary.position == position:
        return boundary
    return _Boundary(
        position,
        boundary.key,
        boundary.source_path,
        boundary.end,
        boundary.filters,
        boundary.repeater,
        boundary.iterator,
        boundary.raw,
        boundary.read_text,
    )


@lru_cache(maxsize=2048)
def _cached_boundary(raw: str, keys: tuple[str, ...]) -> _Boundary | None:
    return _boundary(parse(raw), 0, dict.fromkeys(keys))


def _boundary(marker: Marker, position: int, iterators: Mapping[str, Any] | None = None) -> _Boundary | None:
    if marker.root not in {"d", "c"}:
        return None
    source_path: list[str] = [COMPLEMENT_ROOT] if marker.root == "c" else []
    candidates: list[_Boundary] = []
    for segment in marker.path:
        match = LOOP_SEGMENT.fullmatch(segment)
        if match is not None:
            field = match.group("field")
            key = ".".join((*source_path, field))
            path = (*source_path, field) if field else tuple(source_path)
            entries = tuple(part.strip() for part in (match.group("filters") or "").split(",") if part.strip())
            repeater = (match.group("repeat") or "")[1:] or next(
                (entry[1:] for entry in entries if entry.startswith("*")), None
            )
            filters = tuple(entry for entry in entries if not entry.startswith("*"))
            name = match.group("name")
            read_text = ".".join((marker.root, *source_path[marker.root == "c" :], segment))
            candidates.append(
                _Boundary(
                    position, key, path, match.group("suffix") == "+1", filters, repeater, name, marker.raw, read_text
                )
            )
        source_path.append(segment)
    return next((candidate for candidate in candidates if candidate.key not in (iterators or {})), None)


def _array_at(
    data: Mapping[str, Any] | list[Any], path: tuple[str, ...], iterators: Mapping[str, Any] | None = None
) -> list[Any]:
    value: Any = data
    context = iterators or {}
    prefix: list[str] = []
    for segment in path:
        match = LOOP_SEGMENT.fullmatch(segment)
        if match is not None and match.group("suffix") != "+1":
            field = match.group("field")
            key = ".".join((*prefix, field))
            value = context.get(key)
            if value is None:
                raise MarkerResolutionError(f"Loop iterator is not available: {key}")
            prefix.append(segment)
            continue
        if not is_mapping(value):
            raise MarkerResolutionError(f"Loop path is not an object: {'.'.join(path)}")
        value = value.get(segment)
        prefix.append(segment)
    if not is_list(value):
        raise MarkerResolutionError(f"Loop path is not an array: {'.'.join(path)}")
    return value


def _sorted(values: list[Any], iterator: str) -> list[Any]:
    """A sorting iterator ``[ord]`` orders the items by that attribute (builder.js positions).

    ``i`` keeps the array order. The sort is stable; numbers compare numerically, then strings;
    items without the attribute keep their relative order after the others.
    """
    if iterator == "i":
        return values

    def key(item: Any) -> tuple[int, float, str]:
        value = _field(item, iterator)
        if isinstance(value, bool) or value is None or value is _UNDEFINED:
            return (2, 0.0, "")
        if isinstance(value, int | float):
            return (0, float(value), "")
        return (1, 0.0, str(value))

    return sorted(values, key=key)


def _filter_values(values: list[Any], filters: tuple[str, ...]) -> list[Any]:
    if not filters:
        return values
    return [value for value in values if all(_matches_filter(value, condition) for condition in filters)]


def _repeat_count(value: Any, repeater: str | None) -> int:
    if repeater is None:
        return 1
    count = _field(value, repeater)
    try:
        integer = int(count)
    except (TypeError, ValueError) as error:
        raise MarkerResolutionError(f"Repeater {repeater!r} is not an integer") from error
    if integer > _MAX_REPETITION_FACTOR:
        raise MarkerResolutionError(f"The repeater cannot be above {_MAX_REPETITION_FACTOR}")
    return max(integer, 0)


# params.maxRepetitionFactor of lib/params.js.
_MAX_REPETITION_FACTOR = 400


def _matches_filter(value: Any, condition: str) -> bool:
    match = re.fullmatch(r"(?P<left>[^!=<>]+?)(?P<operator>!=|==|=|>=|<=|>|<)(?P<right>.+)", condition)
    if match is None:
        raise MarkerResolutionError(f"Invalid array filter: {condition}")
    left = _filter_field(value, match.group("left").strip().removeprefix("."))
    right_source = match.group("right").strip()
    right = _filter_operand(value, right_source)
    operator = match.group("operator")
    if operator in {"=", "=="}:
        return _filter_string(left) == _filter_string(right)
    if operator == "!=":
        return _filter_string(left) != _filter_string(right)
    if re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", right_source):
        right = right_source
    if isinstance(left, str) and isinstance(right, str):
        return {">": left > right, ">=": left >= right, "<": left < right, "<=": left <= right}[operator]
    first, second = _filter_number(left), _filter_number(right)
    return {">": first > second, ">=": first >= second, "<": first < second, "<=": first <= second}[operator]


def _filter_operand(item: Any, source: str) -> Any:
    if source.startswith("."):
        return _filter_field(item, source[1:])
    if len(source) >= 2 and source[0] == source[-1] and source[0] in {"'", '"'}:
        return source[1:-1]
    if source.lower() in {"true", "false"}:
        return source.lower() == "true"
    if source.lower() == "null":
        return None
    if source.lower() == "undefined":
        return _UNDEFINED
    try:
        return float(source) if "." in source else int(source)
    except ValueError:
        return source


def _filter_field(item: Any, path: str) -> Any:
    value = item
    for name in path.split("."):
        if not is_mapping(value) or name not in value:
            return _UNDEFINED
        value = value[name]
    return value


def _filter_string(value: Any) -> str:
    if value is _UNDEFINED:
        return "undefined"
    if value is None:
        return "null"
    return str(value).lower() if isinstance(value, bool) else str(value)


def _filter_number(value: Any) -> float:
    if value is _UNDEFINED:
        return float("nan")
    if value is None:
        return 0.0
    if isinstance(value, str) and not value.strip():
        return 0.0
    try:
        return float(value)
    except TypeError, ValueError:
        return float("nan")


def _field(item: Any, path: str) -> Any:
    value = item
    for name in path.split("."):
        if not is_mapping(value):
            return None
        value = value.get(name)
    return value


def _text_content(element: etree._Element) -> str:
    """Read marker text from either a paragraph or an entire table row."""
    dialect = DIALECT.get()
    if element.tag == dialect.paragraph:
        return ParagraphText.from_paragraph(element).value
    return "".join(text.text or "" for text in element.iter(*dialect.texts))
