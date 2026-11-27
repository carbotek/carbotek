# pyright: strict, reportPrivateUsage=false
"""Validate text stored in rendered worksheet cells."""

import re
from collections.abc import Callable, Iterator

from lxml import etree  # pyright: ignore[reportAttributeAccessIssue] lxml has incomplete stubs.

from carbotek._native_adapter import native_inline_text_fits
from carbotek.docx.dialect import SHEET_NAMESPACE
from carbotek.errors import CorruptedTemplateError, TemplateError
from carbotek.ooxml.fragments import RENDERED_TAG
from carbotek.ooxml.ns import NAMESPACES, qn
from carbotek.ooxml.parts import part_key, relationship_part_name, target_name
from carbotek.ooxml.xml import parse_xml
from carbotek.package import TemplatePackage, TemplatePart

from .prepare import column_number

_STRICT_SHEET_NAMESPACE = "http://purl.oclc.org/ooxml/spreadsheetml/main"
_SHEET_NAMESPACES = (SHEET_NAMESPACE, _STRICT_SHEET_NAMESPACE)
_REL_NAMESPACES = (NAMESPACES["r"], "http://purl.oclc.org/ooxml/officeDocument/relationships")
_CELL_REFERENCE = re.compile(r"[A-Z]{1,3}[1-9][0-9]{0,6}")
_TEXT_PATHS = ("s:t", "s:r/s:t")
_CELL_TEXT_LIMIT = 32_767
_XSTRING_ESCAPE = re.compile(r"_x[0-9a-fA-F]{4}_")
_INLINE_START = re.compile(rb"<(?:[^\s<>/:]+:)?is(?=[\x20\t\r\n/>])")
_INLINE_END = re.compile(rb"</(?:[^\s<>/:]+:)?is[\x20\t\r\n]*>")

# XPath counts code points. A code point needs at most two UTF-16 units.
_CANDIDATES = {
    namespace: etree.XPath(
        "s:sheetData/s:row/s:c[(@t = 'inlineStr' and s:is[string-length(.) > $limit])"
        " or ($has_large_shared and @t = 's')]",
        namespaces={"s": namespace},
    )
    for namespace in _SHEET_NAMESPACES
}


def validate_cell_text(package: TemplatePackage, *, infer_reference: bool) -> None:
    """Reject strings above Excel's cell limit before writing the workbook."""
    for sheet_name, part, large_shared in _worksheets(package):
        # Serialization expands any render fragments stored as tree placeholders.
        content = part.content
        # XML parts have already been validated and normalized to UTF-8 by preparation.
        if not large_shared and part.is_xml and _inline_text_fits(content):
            continue
        root = _worksheet_tree(part, content)
        name = etree.QName(root)
        if name.localname != "worksheet" or name.namespace not in _CANDIDATES:
            continue
        for cell in _CANDIDATES[name.namespace](root, limit=_CELL_TEXT_LIMIT // 2, has_large_shared=bool(large_shared)):
            if cell.get("t") == "s":
                value = cell.findtext(f"{{{name.namespace}}}v", "")
                try:
                    index = int(value)
                except ValueError:
                    continue
                if index not in large_shared:
                    continue
            elif _text_length(cell.find(f"{{{name.namespace}}}is"), name.namespace) <= _CELL_TEXT_LIMIT:
                continue
            reference = _cell_reference(cell, infer=infer_reference)
            location = "'" + sheet_name.replace("'", "''") + "'" if sheet_name else part.name
            if reference:
                location += f"!{reference}"
            raise TemplateError(
                f"XLSX cell text exceeds Excel's {_CELL_TEXT_LIMIT} UTF-16 code unit limit in {location}"
            )


def _inline_text_fits(content: bytes) -> bool:
    """Skip the XML pass when each inline string occupies at most the limit in bytes."""
    native_result = native_inline_text_fits(content, _CELL_TEXT_LIMIT)
    return _python_inline_text_fits(content) if native_result is None else native_result


def _python_inline_text_fits(content: bytes) -> bool:
    if b"<!" in content:
        return False  # Comments and CDATA can contain apparent tag delimiters.
    instruction = content.find(b"<?")
    if instruction != -1 and (
        not content.startswith(b"<?xml ", instruction) or content.find(b"<?", instruction + 2) != -1
    ):
        return False
    if b":is" not in content:
        segments = content.split(b"<is>")
        if content.count(b"<is") == len(segments) - 1:
            # A segment with no close may be the start of a nested element.
            return all(segment.find(b"</is>", 0, _CELL_TEXT_LIMIT + len(b"</is>")) >= 0 for segment in segments[1:])
    segments = _INLINE_START.split(content)
    return all(
        (end := _INLINE_END.search(segment)) is not None and end.start() <= _CELL_TEXT_LIMIT for segment in segments[1:]
    )


def _worksheet_tree(part: TemplatePart, content: bytes) -> etree._Element:
    if part.has_tree:
        root: etree._Element = part.tree(readonly=True)
        # Fragment placeholders hide cells until the final worksheet is serialized.
        if next(root.iter(RENDERED_TAG), None) is None:
            return root
    try:
        return parse_xml(content, part.name)
    except CorruptedTemplateError as error:
        cause = error.__cause__
        if not isinstance(cause, etree.XMLSyntaxError) or "Text node too long" not in cause.msg:
            raise

    # A target-built tree has no document type metadata. This target rejects DTDs during parsing.
    parser = etree.XMLParser(
        target=_TextCheckTree(part.name),
        resolve_entities=False,
        no_network=True,
        huge_tree=False,
        remove_blank_text=False,
        strip_cdata=False,
    )
    try:
        return etree.fromstring(content, parser=parser)
    except etree.XMLSyntaxError as error:
        raise CorruptedTemplateError(f"Invalid XML in {part.name}") from error


class _TextCheckTree(etree.TreeBuilder):
    """Build a validation-only tree without storing text in cached formula values."""

    def __init__(self, part_name: str) -> None:
        super().__init__()
        self._part_name = part_name
        self._elements: list[tuple[str, bool]] = []

    def start(self, tag: str, attrib: dict[str, str]) -> None:
        self._elements.append((tag, attrib.get("t") == "str"))
        super().start(tag, attrib)

    def end(self, tag: str) -> None:
        self._elements.pop()
        super().end(tag)

    def data(self, data: str) -> None:
        if len(self._elements) >= 2 and self._elements[-2][1]:
            parent_tag = self._elements[-2][0]
            if any(
                parent_tag == f"{{{namespace}}}c" and self._elements[-1][0] == f"{{{namespace}}}v"
                for namespace in _SHEET_NAMESPACES
            ):
                return
        super().data(data)

    def doctype(self, name: str | None, public_id: str | None, system_id: str | None) -> None:
        raise CorruptedTemplateError(f"Document type declarations are not allowed in {self._part_name}")


def _related_parts(root: etree._Element, source: str, relation: str) -> Iterator[tuple[str, str]]:
    types = {f"{namespace}/{relation}" for namespace in _REL_NAMESPACES}
    for node in root.iterfind(qn("rel:Relationship")):
        target = node.get("Target")
        if target and node.get("TargetMode") != "External" and node.get("Type") in types:
            yield node.get("Id", ""), target_name(source, target)


def _worksheets(package: TemplatePackage) -> Iterator[tuple[str, TemplatePart, frozenset[int]]]:
    # OPC part URI equivalence ignores ASCII case; preserve actual names for diagnostics.
    parts = {part_key(name): part for name, part in package.parts.items()}
    package_rels = parts.get(relationship_part_name(""))
    if package_rels is None:
        return
    root = parse_xml(package_rels.content, package_rels.name)
    workbook = next((name for _, name in _related_parts(root, "", "officeDocument")), None)
    workbook_part = parts.get(part_key(workbook)) if workbook is not None else None
    if workbook_part is None:
        return
    workbook = workbook_part.name
    workbook_rels = parts.get(part_key(relationship_part_name(workbook)))
    if workbook_rels is None:
        return
    root = parse_xml(workbook_part.content, workbook)
    name = etree.QName(root)
    if name.localname != "workbook" or name.namespace not in _SHEET_NAMESPACES:
        return
    sheet_names = {
        sheet_id: sheet.get("name", "")
        for sheet in root.iterfind("s:sheets/s:sheet", {"s": name.namespace})
        for rel_namespace in _REL_NAMESPACES
        if (sheet_id := sheet.get(f"{{{rel_namespace}}}id"))
    }
    relations = parse_xml(workbook_rels.content, workbook_rels.name)
    shared = next(
        (
            part
            for _, target in _related_parts(relations, workbook, "sharedStrings")
            if (part := parts.get(part_key(target))) is not None
        ),
        None,
    )
    large_shared: frozenset[int] = _oversized_shared_strings(shared) if shared is not None else frozenset()
    for id_, target in _related_parts(relations, workbook, "worksheet"):
        if id_ in sheet_names and (part := parts.get(part_key(target))) is not None:
            yield sheet_names[id_], part, large_shared


def _oversized_shared_strings(part: TemplatePart) -> frozenset[int]:
    root = _worksheet_tree(part, part.content)
    name = etree.QName(root)
    if name.localname != "sst" or name.namespace not in _SHEET_NAMESPACES:
        return frozenset()
    return frozenset(
        index
        for index, item in enumerate(root.iterfind(f"{{{name.namespace}}}si"))
        if _text_length(item, name.namespace) > _CELL_TEXT_LIMIT
    )


def _position(element: etree._Element, parse_reference: Callable[[str], int]) -> int | None:
    """An implicit coordinate follows the nearest preceding explicit one."""
    reference = element.get("r")
    if reference is not None:
        return parse_reference(reference) or None
    distance = 0
    for sibling in element.itersiblings(tag=etree.QName(element), preceding=True):
        distance += 1
        if (reference := sibling.get("r")) is not None:
            position = parse_reference(reference)
            return position + distance if position else None
    return distance + 1


def _row_number(reference: str) -> int:
    return int(reference) if len(reference) <= 7 and reference.isascii() and reference.isdecimal() else 0


def _column_number(reference: str) -> int:
    return column_number(reference) if _CELL_REFERENCE.fullmatch(reference) else 0


def _cell_reference(cell: etree._Element, *, infer: bool) -> str:
    if (reference := cell.get("r")) is not None:
        return reference if _CELL_REFERENCE.fullmatch(reference) else ""
    parent = cell.getparent()
    if not infer or parent is None:
        return ""
    row = _position(parent, _row_number)
    column = _position(cell, _column_number)
    if not row or row > 1_048_576 or not column or column > 16_384:
        return ""
    letters = ""
    while column:
        column, index = divmod(column - 1, 26)
        letters = chr(ord("A") + index) + letters
    return f"{letters}{row}"


def _text_length(element: etree._Element | None, namespace: str) -> int:
    if element is None:
        return 0
    namespaces = {"s": namespace}
    return sum(
        _xstring_length("".join(node.itertext())) for path in _TEXT_PATHS for node in element.iterfind(path, namespaces)
    )


def _xstring_length(text: str) -> int:
    # A single pass keeps _x005F_xHHHH_ literal. The XML parser has already resolved entities.
    units = len(text) + (0 if text.isascii() else sum(ord(char) > 0xFFFF for char in text))
    return units - 6 * sum(1 for _ in _XSTRING_ESCAPE.finditer(text))
