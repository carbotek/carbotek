# pyright: strict, reportPrivateUsage=false
"""String rendering of loop sections.

A section without directives or bookmarks is serialized once with a slot per marker text,
attribute and nested loop. Each item then renders by joining strings; all copies are parsed back in
one libxml2 call instead of deep-copying and walking the template tree per item. With a conditional
block in the section every text is a slot rendered by the tree renderer's ``_replace`` in document
order, and the elements a hidden block removes (``blocks.hidden_range``) are wrapped in cut marks:
the XML between the marks of a hidden block is dropped from the copy.

Outermost copies that the passes after rendering do not need remain XML text in a render-owned
registry. Small ``carbotek-rendered`` placeholders refer to them until ``serialize_xml`` splices
them into the part. Standalone tree rendering uses self-contained CDATA instead.
"""

import re
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from lxml import etree  # pyright: ignore[reportAttributeAccessIssue] lxml has incomplete stubs.

from carbotek._native_adapter import OP_FIELD, OP_LITERAL, PlainSection, compile_plain_section, native_utf8_joiner
from carbotek.formatters.string import LINE_BREAK
from carbotek.markers import parse, scan
from carbotek.ooxml.fragments import EMPTY_TEXT, RENDERED_TAG, get_fragment, register_fragment
from carbotek.ooxml.ns import qn
from carbotek.ooxml.xml import parse_xml
from carbotek.render_state import StateSlot

from .blocks import hidden_range
from .dialect import DIALECT

type TextPiece = str | Callable[[Mapping[str, Any]], str]
# Compiles the text of one ``w:t`` into literal pieces and per-item marker functions; None when a
# marker needs the tree renderer (deferred features, conditions, aggregates, ...).
# Arguments: the text, the element tag, the attribute name (None for the text of an element) and
# whether the section has conditional blocks (texts are then rendered in order by ``_replace``).
type TextCompiler = Callable[[str, str, str | None, bool], list[TextPiece] | None]

TEXT_COMPILER: StateSlot[TextCompiler | None] = StateSlot("TEXT_COMPILER", default=None)
# Whether the render is inside a hidden conditional block right now (MarkerContext.is_hidden).
IS_HIDDEN: StateSlot[Callable[[], bool] | None] = StateSlot("IS_HIDDEN", default=None)


class PlainField:
    """A compiled one-field marker with enough metadata for a whole-section native pass.

    ``operation`` is an ``OP_*`` code of ``carbotek._native_adapter``; ``arg1``–``arg3`` are the
    decoded literal arguments of ``OP_IF_EQUAL`` (compared value, shown, shown otherwise).
    """

    __slots__ = ("_fallback", "arg1", "arg2", "arg3", "field", "key", "operation")

    def __init__(
        self,
        key: str,
        field: str,
        fallback: Callable[[Mapping[str, Any]], str],
        operation: int = OP_FIELD,
        arg1: str = "",
        arg2: str = "",
        arg3: str = "",
    ) -> None:
        self.key = key
        self.field = field
        self._fallback = fallback
        self.operation = operation
        self.arg1 = arg1
        self.arg2 = arg2
        self.arg3 = arg3

    def __call__(self, iterators: Mapping[str, Any]) -> str:
        return self._fallback(iterators)

    @property
    def fallback(self) -> Callable[[Mapping[str, Any]], str]:
        return self._fallback


class _PlainTextSlot:
    __slots__ = ("_compiled", "key", "parts")

    def __init__(self, compiled: list[TextPiece], key: str) -> None:
        self.key = key
        self._compiled = compiled
        self.parts = [
            (OP_LITERAL, piece, "", "", "")
            if isinstance(piece, str)
            else (piece.operation, piece.field, piece.arg1, piece.arg2, piece.arg3)
            for piece in compiled
            if isinstance(piece, str | PlainField)
        ]

    def __call__(self, iterators: Mapping[str, Any]) -> str:
        value = "".join(piece if isinstance(piece, str) else piece(iterators) for piece in self._compiled)
        return _escape_text(value) if value else _EMPTY

    def python_evaluator(self) -> Callable[[Mapping[str, Any]], str]:
        return _text_slot_general(
            [piece.fallback if isinstance(piece, PlainField) else piece for piece in self._compiled]
        )


_SLOT = "\ue000"
_NAMESPACE_DECLARATION = re.compile(r'\sxmlns(?::(?P<prefix>[\w.-]+))?="(?P<uri>[^"]*)"')
_WRAPPER = "carbotek-copies"
_EMPTY = EMPTY_TEXT
_MAX_BYTES_PER_SLOT = 600  # above this, a section is rendered by deep-copying the template tree
_BLOCK_NAMES = ("showBegin", "hideBegin", "showEnd", "hideEnd")
_EMPTY_BYTES = _EMPTY.encode("utf-8")
_W_P = qn("w:p")
_W_TC = qn("w:tc")
LOOP_PLACEHOLDER = "carbotek-loop"
_LOOP_PLACEHOLDER = re.compile(r"<carbotek-loop\b[^>]*/>")
_CUT = "carbotek-cut"
_CUT_MARK = re.compile(r"<carbotek-cut\b[^>]*/>")


class _Cut:
    """Start or end of the elements a conditional block removes when it is hidden."""

    __slots__ = ("block", "hidden", "opening")

    def __init__(self, block: int, opening: bool, hidden: list[bool]) -> None:
        self.block = block
        self.opening = opening
        self.hidden = hidden  # per block: hidden for the copy being rendered (set by its begin text)


def _emit(pieces: list[Any], iterators: Mapping[str, Any]) -> str:
    """One copy of a section with cut marks; every piece is evaluated, hidden cuts are dropped."""
    out: list[str] = []
    starts: list[int] = []
    for piece in pieces:
        if isinstance(piece, str):
            out.append(piece)
        elif isinstance(piece, _Cut):
            if piece.opening:
                starts.append(len(out))
            elif piece.hidden[piece.block]:
                del out[starts.pop() :]
            else:
                starts.pop()
        else:
            out.append(piece(iterators))
    return "".join(out)


class StringSection:
    """A loop section compiled into literal XML and marker slots."""

    __slots__ = (
        "_byte_literals",
        "_close",
        "_cuts",
        "_literals",
        "_native",
        "_native_key",
        "_open",
        "_pieces",
        "_slots",
        "heavy",
    )
    # A section is a TextPiece of the enclosing section when it is a nested loop (see ``text``).

    def __init__(
        self, pieces: list[Any], open_tag: str, close_tag: str, *, heavy: bool = False, cuts: bool = False
    ) -> None:
        self.heavy = heavy  # parsing copies back would cost more than deep-copying the template
        self._pieces = pieces
        self._open = open_tag
        self._close = close_tag
        self._cuts = cuts
        self._literals, self._slots = _slot_layout(pieces) if not cuts else ([], [])
        self._byte_literals: list[bytes] | None = None
        self._native: PlainSection | None = None
        self._native_key: str | None = None
        if self._slots and all(isinstance(evaluate, _PlainTextSlot) for _, evaluate in self._slots):
            fields = [(index, evaluate) for index, evaluate in self._slots if isinstance(evaluate, _PlainTextSlot)]
            key = fields[0][1].key
            if all(evaluate.key == key for _, evaluate in fields):
                self._native = compile_plain_section(
                    self._literals, [(index, evaluate.parts) for index, evaluate in fields]
                )
                self._native_key = key
        self._slots = [
            (index, evaluate.python_evaluator() if isinstance(evaluate, _PlainTextSlot) else evaluate)
            for index, evaluate in self._slots
        ]

    def render_plain_values(self, values: list[Any], key: str) -> str | None:
        """Render a supported top-level section in one native call, or request Python fallback."""
        if self._native is None or self._native_key != key:
            return None
        return self._native.render(values)

    def render_plain_bytes(self, values: list[Any], key: str) -> bytes | None:
        """Render a registry-bound top-level section directly as UTF-8."""
        if self._native is None or self._native_key != key:
            return None
        return self._native.render_bytes(values)

    def text(self, iterators: Mapping[str, Any]) -> str:
        """The XML of one copy."""
        if self._cuts:
            return _emit(self._pieces, iterators)
        parts = self._literals.copy()
        for index, evaluate in self._slots:
            parts[index] = evaluate(iterators)
        return "".join(parts)

    def render_xml(self, items: Iterable[Mapping[str, Any]]) -> str:
        """The XML of all copies, one per iterator mapping (emptied texts still marked)."""
        if self._cuts:
            return "".join(_emit(self._pieces, iterators) for iterators in items)
        out: list[str] = []
        literals = self._literals
        slots = self._slots
        for iterators in items:
            offset = len(out)
            out.extend(literals)
            for index, evaluate in slots:
                out[offset + index] = evaluate(iterators)
        return "".join(out)

    def render_xml_bytes(self, items: Iterable[Mapping[str, Any]]) -> bytes | None:
        """Evaluate every slot once, then join directly to UTF-8 for the fragment registry."""
        if self._cuts or (join := native_utf8_joiner()) is None:
            return None
        if self._byte_literals is None:
            self._byte_literals = [literal.encode("utf-8") for literal in self._literals]
        out: list[str | bytes] = []
        literals = self._byte_literals
        slots = self._slots
        for iterators in items:
            offset = len(out)
            out.extend(literals)
            for index, evaluate in slots:
                out[offset + index] = evaluate(iterators)
        return join(out)

    def parse(self, xml: str | bytes) -> list[etree._Element]:
        """Copies rendered by ``render_xml`` as elements, parsed in one go."""
        return _parse_copies(xml, self._open, self._close)

    def render(self, items: Iterable[Mapping[str, Any]]) -> list[etree._Element]:
        """All copies, one per iterator mapping, parsed in one go."""
        return self.parse(self.render_xml(items))

    @property
    def wrapper(self) -> tuple[str, str]:
        return self._open, self._close


def _parse_copies(xml: str | bytes, open_tag: str, close_tag: str) -> list[etree._Element]:
    payload = (
        open_tag + xml + close_tag
        if isinstance(xml, str)
        else open_tag.encode("utf-8") + xml + close_tag.encode("utf-8")
    )
    wrapper = parse_xml(payload, "loop copies")
    if _EMPTY in xml if isinstance(xml, str) else _EMPTY_BYTES in xml:
        # The tree renderer leaves an emptied text as "" (<w:t></w:t>), not as no text (<w:t/>).
        for node in list(wrapper.iter(etree.Comment)):
            parent = node.getparent()
            if node.text == "ct-empty" and parent is not None and len(parent) == 1 and not parent.text:
                parent.remove(node)
                parent.text = ""
    return list(wrapper)


# Markup the post-render features act on: copies holding it are parsed into the tree instead.
_FEATURE_MARKUP = ("docPr", "w:hyperlink", "r:id=", "r:embed", "fldChar", "instrText", "w:bookmark", "w:fldSimple")


def placeholder_safe(elements: list[etree._Element], parent: etree._Element) -> bool:
    """Whether copies of this template section need nothing from the passes after rendering.

    Those passes (pictures, drawing ids, hyperlinks, bookmarks, fields, empty cells) work on the
    tree; a section without their markup can stay text until the part is serialized.
    """
    if parent.tag == _W_TC and any(element.tag != _W_P for element in elements):
        return False  # a cell must keep a paragraph: repair_empty_table_cells needs to see them
    for element in elements:
        xml = etree.tostring(element, encoding="unicode")
        if any(markup in xml for markup in _FEATURE_MARKUP):
            return False
        if any(cell.find(_W_P) is None for cell in element.iter(_W_TC)):
            return False
    return True


def rendered_placeholder(xml: str | bytes, open_tag: str, close_tag: str) -> etree._Element | None:
    """One element referring to rendered copies, or None when they must be parsed now.

    Copies with a ``{`` (a frozen marker for a later feature, a marker of another pass, or data
    that later passes might read) or a convCRLF line break (turned into ``w:br`` on the tree) are
    parsed as before.
    """
    if (
        ("{" in xml or "[[ct-" in xml or LINE_BREAK in xml)
        if isinstance(xml, str)
        else (b"{" in xml or b"[[ct-" in xml or LINE_BREAK.encode("utf-8") in xml)
    ):
        return None
    placeholder = etree.Element(RENDERED_TAG)
    reference = register_fragment(xml, open_tag, close_tag)
    if reference is not None:
        placeholder.set("ref", reference)
        return placeholder
    if isinstance(xml, bytes):
        xml = xml.decode("utf-8")
    placeholder.text = etree.CDATA(xml)
    placeholder.set("wrap", open_tag)
    return placeholder


def materialize(placeholder: etree._Element) -> list[etree._Element]:
    """Replace a rendered placeholder by its parsed copies (for passes that must see them)."""
    fragment = get_fragment(placeholder)
    copies = (
        _parse_copies(fragment.xml, fragment.open_tag, fragment.close_tag)
        if fragment is not None
        else _parse_copies(placeholder.text or "", placeholder.get("wrap", ""), f"</{_WRAPPER}>")
    )
    parent = placeholder.getparent()
    if parent is not None:
        position = parent.index(placeholder)
        parent[position : position + 1] = copies
    return copies


def materialize_all(root: etree._Element) -> None:
    for placeholder in list(root.iter(RENDERED_TAG)):
        materialize(placeholder)


def has_blocks(elements: list[etree._Element]) -> bool:
    """Whether a conditional block marker occurs in the section's texts."""
    return any(
        (content := text.text) and any(name in content for name in _BLOCK_NAMES)
        for element in elements
        for text in element.iter(*DIALECT.get().texts)
    )


def compile_section(
    elements: list[etree._Element],
    loops: list[TextPiece] | None = None,
    *,
    scope: list[etree._Element] | None = None,
) -> StringSection | None:
    """The string form of a loop section, or None when a marker text cannot be compiled.

    ``loops`` renders the nested loops, whose sections were replaced by ``<carbotek-loop n="k"/>``
    placeholders (see ``LoopExpander``).
    """
    compiled = _compiled_slots(elements, loops or [])
    if compiled is None:
        return None
    slots, hidden, elements = compiled
    namespaces: dict[str | None, str] = {}
    for element in scope or elements:  # the template elements declare what the copies use
        namespaces.update(element.nsmap)
    for node, name, _, _ in slots:
        if name is None:
            node.text = _SLOT
        elif name != "":
            node.set(name, _SLOT)
    try:
        source_parent = (scope or elements)[0].getparent() if (scope or elements) else None
        inherited = source_parent.nsmap if source_parent is not None else {}
        serialized = "".join(
            _without_namespaces(etree.tostring(element, encoding="unicode"), inherited) for element in elements
        )
    finally:
        for node, name, original, _ in slots:
            if name is None:
                node.text = original or None
            elif name != "":
                node.set(name, original)
    serialized = _CUT_MARK.sub(_SLOT, _LOOP_PLACEHOLDER.sub(_SLOT, serialized))
    if serialized.count(_SLOT) != len(slots):
        return None  # the slot character occurs in the template itself
    # Mostly static XML: parsing it back and moving it into the part costs more than libxml2's
    # deep copy of the template (measured: akt-3k rows, ~8 KB with 7 markers) unless it stays text.
    heavy = len(serialized) > _MAX_BYTES_PER_SLOT * max(len(slots), 1)
    pieces: list[TextPiece] = []
    compiled_slots: list[list[TextPiece] | None] = [compiled for *_, compiled in slots]
    kinds: list[str | None] = [name for _, name, _, _ in slots]
    for literal, compiled, kind in zip(serialized.split(_SLOT), [*compiled_slots, None], [*kinds, ""], strict=True):
        pieces.append(literal)
        if compiled is None:
            continue
        if kind is None:
            pieces.append(_text_slot(compiled))
        elif kind == "":
            pieces.extend(compiled)  # a nested loop or a cut mark: no escaping
        else:
            pieces.extend(_escaped(piece) for piece in compiled)
    declarations = "".join(
        f' xmlns="{uri}"' if prefix is None else f' xmlns:{prefix}="{uri}"' for prefix, uri in namespaces.items()
    )
    return StringSection(
        _merged(pieces), f"<{_WRAPPER}{declarations}>", f"</{_WRAPPER}>", heavy=heavy, cuts=bool(hidden)
    )


# (element, attribute / None for the text / "" for a nested loop, original value, compiled pieces)
type _Slot = tuple[etree._Element, str | None, str, list[Any]]


def _compiled_slots(
    elements: list[etree._Element], loops: list[TextPiece]
) -> tuple[list[_Slot], list[bool], list[etree._Element]] | None:
    """Slots of a section, per-block hidden flags, and the section elements with its cut marks."""
    compiler = TEXT_COMPILER.get()
    if compiler is None:
        return None
    blocks = has_blocks(elements)
    hidden: list[bool] = []
    begins: dict[etree._Element, list[int]] = {}
    if blocks:
        marked = _mark_cuts(elements, hidden, begins)
        if marked is None:
            return None
        elements = marked
    slots = _slots(elements, compiler, loops, blocks, (hidden, begins))
    return None if slots is None else (slots, hidden, elements)


def _slots(
    elements: list[etree._Element],
    compiler: TextCompiler,
    loops: list[TextPiece],
    blocks: bool,
    cuts: tuple[list[bool], dict[etree._Element, list[int]]],
) -> list[_Slot] | None:
    """Every slot of the section in serialization order; None when one cannot be compiled.

    A start tag serializes its attributes before its text, so attributes come first.
    """
    slots: list[_Slot] = []
    texts = DIALECT.get().texts
    hidden, begins = cuts
    for element in elements:
        for node in element.iter():
            if not isinstance(node.tag, str):
                continue
            if node.tag == LOOP_PLACEHOLDER:
                slots.append((node, "", "", [loops[int(node.get("n", "0"))]]))
                continue
            if node.tag == _CUT:
                slots.append((node, "", "", [_Cut(int(node.get("n", "0")), node.get("s") == "1", hidden)]))
                continue
            for name, value in node.attrib.items():
                if "{" in value:
                    if (compiled := compiler(value, node.tag, name, blocks)) is None:
                        return None
                    slots.append((node, name, value, compiled))
            if node.tag in texts and (blocks or ((content := node.text) and "{" in content)):
                content = node.text or ""
                if not blocks and any(name in content for name in _BLOCK_NAMES):
                    return None
                if (compiled := compiler(content, node.tag, None, blocks)) is None:
                    return None
                if node in begins:
                    compiled = [_recording_begin(compiled, begins[node], hidden)]
                slots.append((node, None, content, compiled))
    return slots


def _recording_begin(compiled: list[TextPiece], blocks: list[int], hidden: list[bool]) -> TextPiece:
    """The text holding a block's begin marker records whether the block hides this copy."""
    state = IS_HIDDEN.get()

    def piece(iterators: Mapping[str, Any]) -> str:
        before = state() if state is not None else False
        value = "".join(item if isinstance(item, str) else item(iterators) for item in compiled)
        now = state() if state is not None else False
        for block in blocks:
            hidden[block] = not before and now
        return value

    return piece


def _mark_cuts(
    elements: list[etree._Element], hidden: list[bool], begins: dict[etree._Element, list[int]]
) -> list[etree._Element] | None:
    """Wrap what each block removes in cut marks: the section elements with them, None for the tree.

    The begin (end) text holds nothing else before (after) its marker (the tree decides
    ``empty_before`` on the rendered text), blocks are paired in the section and cut disjoint
    elements; otherwise the section is rendered on the tree.
    """
    parent = elements[0].getparent()
    pairs = _block_pairs(elements)
    if pairs is None or parent is None:
        return None
    taken: list[etree._Element] = []
    plans: list[tuple[etree._Element, list[etree._Element]]] = []
    for begin, empty_before, end, empty_after in pairs:
        removals = list({id(item): item for item in hidden_range(begin, empty_before, end, empty_after)}.values())
        outermost = [item for item in removals if not any(ancestor in removals for ancestor in item.iterancestors())]
        for item in outermost:
            if any(item is other or item in other.iterancestors() or other in item.iterancestors() for other in taken):
                return None  # overlapping blocks (nested or chained): the tree renderer
        taken.extend(outermost)
        plans.append((begin, outermost))
    # If any combination of cuts can remove every direct paragraph of a retained
    # cell, the tree renderer must repair it. Blocks elsewhere do not require
    # materializing unrelated tables in a large section.
    for element in elements:
        for cell in element.iter(_W_TC):
            paragraphs = cell.findall(_W_P)
            if (
                paragraphs
                and all(
                    any(paragraph is item or item in paragraph.iterancestors() for item in taken)
                    for paragraph in paragraphs
                )
                and not any(cell is item or item in cell.iterancestors() for item in taken)
            ):
                return None
    first, last = elements[0], elements[-1]
    for number, (begin, outermost) in enumerate(plans):
        hidden.append(False)
        begins.setdefault(begin, []).append(number)
        for item in outermost:
            item.addprevious(etree.Element(_CUT, n=str(number), s="1"))
            item.addnext(etree.Element(_CUT, n=str(number), s="0"))
    start, stop = parent.index(first), parent.index(last)
    while start > 0 and parent[start - 1].tag == _CUT:
        start -= 1
    while stop + 1 < len(parent) and parent[stop + 1].tag == _CUT:
        stop += 1
    return list(parent)[start : stop + 1]


def _block_pairs(elements: list[etree._Element]) -> list[tuple[etree._Element, bool, etree._Element, bool]] | None:
    """(begin text, empty before, end text, empty after) of every block across texts; None to fall back."""
    texts = DIALECT.get().texts
    stack: list[tuple[etree._Element, bool]] = []
    pairs: list[tuple[etree._Element, bool, etree._Element, bool]] = []
    for element in elements:
        for node in element.iter(*texts):
            content = node.text or ""
            for occurrence in scan(content):
                kind = _block_kind(occurrence.raw)
                if kind is None:
                    continue
                if kind == "begin":
                    before = content[: occurrence.start]
                    if "{" in before:
                        return None  # other markers before: emptiness depends on the data
                    stack.append((node, before == ""))
                    continue
                if not stack:
                    return None
                begin, empty_before = stack.pop()
                after = content[occurrence.end :]
                if "{" in after:
                    return None
                if begin is not node:
                    pairs.append((begin, empty_before, node, after == ""))
    return None if stack else pairs


def _block_kind(raw: str) -> str | None:
    if not any(name in raw for name in _BLOCK_NAMES):
        return None
    formatters = parse(raw).formatters
    last = formatters[-1].name if formatters else ""
    return "begin" if last in {"showBegin", "hideBegin"} else "end" if last in {"showEnd", "hideEnd"} else None


def _escaped(piece: TextPiece) -> TextPiece:
    if isinstance(piece, str):
        return _escape_attribute(piece)
    return lambda iterators: _escape_attribute(piece(iterators))


def _text_slot(compiled: list[TextPiece]) -> TextPiece:
    """The whole text of one ``w:t``: escaped, or the empty marker when nothing is left."""
    compiled = [piece for piece in compiled if piece != ""]
    fields = [piece for piece in compiled if isinstance(piece, PlainField)]
    if (
        fields
        and all(isinstance(piece, str | PlainField) for piece in compiled)
        and all(piece.key == fields[0].key for piece in fields)
    ):
        return _PlainTextSlot(compiled, fields[0].key)
    return _text_slot_general([piece.fallback if isinstance(piece, PlainField) else piece for piece in compiled])


def _text_slot_general(compiled: list[TextPiece]) -> Callable[[Mapping[str, Any]], str]:
    """Original Python slot evaluator for sections or values outside the native subset."""
    if len(compiled) == 1 and not isinstance(only := compiled[0], str):
        # The usual cell: the text is exactly one marker.
        def single(iterators: Mapping[str, Any]) -> str:
            value = only(iterators)
            return _escape_text(value) if value else _EMPTY

        return single

    literals, slots = _slot_layout(compiled)

    def slot(iterators: Mapping[str, Any]) -> str:
        parts = literals.copy()
        for index, evaluate in slots:
            parts[index] = evaluate(iterators)
        value = "".join(parts)
        return _escape_text(value) if value else _EMPTY

    return slot


def _slot_layout(pieces: list[TextPiece]) -> tuple[list[str], list[tuple[int, Callable[[Mapping[str, Any]], str]]]]:
    """Separate constant strings from ordered evaluations once, while compiling the section."""
    literals: list[str] = []
    slots: list[tuple[int, Callable[[Mapping[str, Any]], str]]] = []
    for index, piece in enumerate(pieces):
        if isinstance(piece, str):
            literals.append(piece)
        else:
            literals.append("")
            slots.append((index, piece))
    return literals, slots


def _without_namespaces(serialized: str, inherited: Mapping[str | None, str]) -> str:
    """Strip only bindings which the insertion parent already provides."""
    end = serialized.index(">")

    def declaration(match: re.Match[str]) -> str:
        return "" if inherited.get(match.group("prefix")) == match.group("uri") else match.group(0)

    return _NAMESPACE_DECLARATION.sub(declaration, serialized[:end]) + serialized[end:]


def _merged(pieces: list[TextPiece]) -> list[TextPiece]:
    merged: list[TextPiece] = []
    for piece in pieces:
        if isinstance(piece, str) and merged and isinstance(merged[-1], str):
            merged[-1] += piece
        elif piece != "":
            merged.append(piece)
    return merged


def _escape_text(value: str) -> str:
    """Escaped exactly as lxml serializes element text (so kept copies match the tree output)."""
    if "&" in value:
        value = value.replace("&", "&amp;")
    if "<" in value:
        value = value.replace("<", "&lt;")
    if ">" in value:
        value = value.replace(">", "&gt;")
    if "\r" in value:
        value = value.replace("\r", "&#13;")
    return value


def _escape_attribute(value: str) -> str:
    """Escaped for both element text and a double-quoted attribute value."""
    if "&" in value:
        value = value.replace("&", "&amp;")
    if "<" in value:
        value = value.replace("<", "&lt;")
    if ">" in value:
        value = value.replace(">", "&gt;")
    if '"' in value:
        value = value.replace('"', "&quot;")
    # The parser normalizes raw line ends (and whitespace in attributes): keep them as references.
    if "\r" in value or "\n" in value or "\t" in value:
        value = value.replace("\r", "&#13;").replace("\n", "&#10;").replace("\t", "&#9;")
    return value
