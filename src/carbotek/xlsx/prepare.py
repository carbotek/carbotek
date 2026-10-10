# pyright: strict, reportPrivateUsage=false
"""Data-independent preparation of an XLSX template, done on strings.

Order of ``lib/preprocessor.js`` ``execute`` for an xlsx template:

1. every XML part: ``removeXMLInsideMarkers`` (a marker split by rich-text runs becomes whole, the
   tags move after it) and ``extractTemplateOptions`` (``{o.key=value}``);
2. ``removeCalcChain``: ``xl/calcChain.xml`` and its relationship go (Excel rebuilds it);
3. ``convertSharedStringToInlineString``: each ``t="s"`` cell gets its shared string inline
   (``t="inlineStr"``) or, for a lone ``:formatN`` marker, becomes numeric (``t="n"``); the shared
   string part and its relationship go;
4. ``lib/xlsx.js`` ``preprocess`` (``preReleaseFeatureIn >= 4022008``, the default 5000000): the
   ``r=``/``spans=`` references go, rows and cells skipped by a reference gap become empty ones;
   below that version only the references go (``removeRowCounterInWorksheet``).

Everything here is a direct port working on the part text; the result is cached with the
compiled template.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import cmp_to_key

from carbotek.ooxml.template_options import extract_template_options
from carbotek.ooxml.xml import parse_xml, serialize_xml
from carbotek.package import TemplatePackage, TemplatePart

# lib/params.js: preReleaseFeatureIn of a production engine (5004002 only under a test runner).
DEFAULT_PRE_RELEASE = 5000000
_XLSX_PREPROCESS_FROM = 4022008


def prepare_workbook(
    package: TemplatePackage, *, export_formatted_values_as_text: bool = False
) -> tuple[TemplatePackage, dict[str, object]]:
    """Prepared package and the render option overrides declared by ``{o.*}`` markers.

    ``export_formatted_values_as_text`` (render option, overridden by the template) keeps
    ``:formatN`` cells as text instead of numbers.
    """
    texts = {name: _xml_text(part.content, name) for name, part in package.parts.items() if part.is_xml}
    options: dict[str, object] = {}
    for name, text in texts.items():
        texts[name] = extract_template_options(remove_xml_inside_markers(text), options)
    removed: list[str] = []
    _remove_one_file(texts, removed, next((name for name in texts if name.endswith("xl/calcChain.xml")), None))
    shared_name = next((name for name in package.order if name in texts and name.endswith("sharedStrings.xml")), None)
    shared = read_shared_strings(texts[shared_name]) if shared_name is not None else []
    _remove_one_file(texts, removed, shared_name)
    as_text = options.get("export_formatted_values_as_text", export_formatted_values_as_text) is True
    pre_release = options.get("pre_release_feature_in", DEFAULT_PRE_RELEASE)
    for name in texts:
        if not name.endswith(".xml") or "sheet" not in name:
            continue
        inline = convert_to_inline_string(texts[name], shared, export_as_text=as_text)
        if isinstance(pre_release, int) and pre_release >= _XLSX_PREPROCESS_FROM:
            texts[name] = fill_references(inline)
        else:
            texts[name] = remove_row_counter(inline)
    from .images import prepare_hyperlinks, prepare_pictures

    for name, text in texts.items():
        texts[name] = prepare_pictures(name, prepare_hyperlinks(name, text))
    replacements = {
        name: TemplatePart(name, text.encode("utf-8"), True)
        for name, text in texts.items()
        if text.encode("utf-8") != package.parts[name].content
    }
    return package.replace_parts(replacements).without_parts(removed), options


def _xml_text(content: bytes, name: str) -> str:
    """Validate all parts, preserving unchanged UTF-8 markup for byte-stable output."""
    root = parse_xml(content, name)
    encoding = root.getroottree().docinfo.encoding.upper().replace("-", "")
    if encoding in {"UTF8", "ASCII", "USASCII"}:
        return content.decode("utf-8")
    return serialize_xml(root).decode("utf-8")


def _remove_one_file(texts: dict[str, str], removed: list[str], name: str | None) -> None:
    """``removeOneFile``: drop the part and its relationship in ``<dir>/_rels/workbook.xml.rels``."""
    if name is None:
        return
    removed.append(name)
    del texts[name]
    directory, _, basename = name.rpartition("/")
    relationships = f"{directory}/_rels/workbook.xml.rels" if directory else "/_rels/workbook.xml.rels"
    if relationships in texts:
        texts[relationships] = re.sub(
            r'<Relationship [^>]*Target="' + re.escape(basename) + r'"[^>]*/>', "", texts[relationships], count=1
        )


# --- parser.removeXMLInsideMarkers --------------------------------------------------------------

_TAG_RUN = re.compile(r"((?:<[^>]+>)+)")
_MARKER_WITH_XML = re.compile(r"(\{[^{]+?\})")
_ANY_TAG = re.compile(r"<(?:[\s\S]+?)>")
_LINE_BREAKS = re.compile(r"(\r\n|\n|\r)")
_TAG_WITH_SPACES = re.compile(r"\s*(<[^>]*>)\s*")
_CARBOTEK_MARKER = re.compile(r"^\{?\s*(?:[cdt]\s*[.\[:(])|^\{?\s*[#$]|^\{?\s*bindColor|^\{?\s*bindChart")
_NOT_PRINTABLE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f]")
_BLOCK_END = re.compile(r":(?:showEnd|hideEnd)")


def is_carbotek_marker(marker: str) -> bool:
    """``parser.isCarbotekMarker``."""
    return _CARBOTEK_MARKER.search(marker) is not None and _NOT_PRINTABLE.search(marker) is None


def remove_xml_inside_markers(xml: str) -> str:
    """``parser.removeXMLInsideMarkers``: ``{d.<tags>x}`` becomes ``{d.x}<tags>``."""
    cleaned = _TAG_RUN.sub(lambda match: match.group(1).replace("{", "\x00").replace("}", "￿"), xml)

    def join(match: re.Match[str]) -> str:
        marker_with_xml = match.group(1)
        if "<" not in marker_with_xml:
            return match.group(0)
        marker_only = _LINE_BREAKS.sub("", _ANY_TAG.sub("", marker_with_xml))
        xml_only = "".join(tag.group(1) for tag in _TAG_WITH_SPACES.finditer(marker_with_xml))
        if not is_carbotek_marker(marker_only):
            return match.group(0)
        if _BLOCK_END.search(re.sub(r"\s", "", marker_only)):
            return xml_only + marker_only
        return marker_only + xml_only

    cleaned = _MARKER_WITH_XML.sub(join, cleaned)
    return cleaned.replace("\x00", "{").replace("￿", "}")


# --- preprocessor.extractTemplateOptions --------------------------------------------------------

# --- preprocessor.readSharedString / convertToInlineString ---------------------------------------

_SHARED_ITEM = re.compile(r"<si>(.*?)</si>", re.DOTALL)
_SHARED_CELL = re.compile(r'(<([A-Za-z0-9_])[^>]*t="s"[^>]*>)(.*?)(</\2>)', re.DOTALL)
_SHARED_INDEX = re.compile(r"<v>([\n0-9]+?)</v>")
_NUMBER_MARKER = re.compile(r"\{[^{]+?:formatN(?:\(\s*[0-9]*\s*\))?\}")
_FORMAT_N = re.compile(r":formatN(?:\(\s*[0-9]*\s*\))?")


def read_shared_strings(xml: str) -> list[str]:
    """``readSharedString``: the inner XML of every ``<si>``.

    JS matches ``<si>(.+?)</si>``: an empty ``<si></si>`` is skipped, the later indexes shift and the
    last cells read past the end (a TypeError). Every item counts here ([xlsx-empty-shared-string]).
    """
    return _SHARED_ITEM.findall(xml)


def convert_to_inline_string(xml: str, shared: list[str], *, export_as_text: bool = False) -> str:
    """``convertToInlineString``: shared-string cells become inline strings or numeric cells."""

    def inline(match: re.Match[str]) -> str:
        open_tag, content, close_tag = match.group(1), match.group(3), match.group(4)
        index = _SHARED_INDEX.search(content)
        if index is None:
            return match.group(0)
        position = int(re.match(r"\s*([0-9]+)", index.group(1)).group(1))  # type: ignore[union-attr] - parseInt
        selected = shared[position] if position < len(shared) else "undefined"
        number = _NUMBER_MARKER.search(shared[position]) if position < len(shared) else None
        if number is not None and not export_as_text:
            value = _FORMAT_N.sub("", number.group(0), count=1)
            return open_tag.replace('t="s"', 't="n"', 1) + f"<v>{value}</v>" + close_tag
        return open_tag.replace('t="s"', 't="inlineStr"', 1) + f"<is>{selected}</is>" + close_tag

    return _SHARED_CELL.sub(inline, xml)


def remove_row_counter(xml: str) -> str:
    """``removeRowCounterInWorksheet``: drop ``r=`` and ``spans=`` of rows and cells."""
    xml = re.sub(r'<(?:c|row)[^>]*\s(r="\S+")[^>]*>', lambda match: match.group(0).replace(match.group(1), "", 1), xml)
    return re.sub(
        r'<(?:c|row)[^>]*(spans="\S+")[^>]*>', lambda match: match.group(0).replace(match.group(1), "", 1), xml
    )


# --- lib/xlsx.js preprocess, lib/chart.js executeStateMachine, helper.applyPatches ------------------

_EMPTY_CELL = '<c t="inlineStr"><is><t></t></is></c>'


@dataclass(slots=True)
class _Patch:
    start: int
    stop: int
    add: str


@dataclass(slots=True)
class _Scan:
    xml: str
    patches: list[_Patch] = field(default_factory=list[_Patch])
    row: int = 0
    col: int = 0
    last_cell_start: int = 0
    last_row_start: int = 0


def column_number(cell: str) -> int:
    """``cellToColumnNumber``: ``AB12`` -> 28."""
    letters = re.sub(r"\d+$", "", cell)
    column = 0
    for letter in letters:
        column = column * 26 + (ord(letter) - ord("A") + 1)
    return column


def _row_start(scan: _Scan, tag_end: int, start: int, end: int) -> str:
    scan.col = 0
    scan.last_row_start = start
    return "inRowProps"


def _row_reference(scan: _Scan, tag_end: int, start: int, end: int) -> str:
    closing = scan.xml.find('"', end)
    row = _parse_int(scan.xml[end:closing])
    gap = row - (scan.row + 1)
    if gap > 0:
        scan.patches.append(_Patch(scan.last_row_start, scan.last_row_start, f"<row>{_EMPTY_CELL}</row>" * gap))
    scan.patches.append(_Patch(start, closing + 1, ""))
    scan.row = row
    return "inRowProps"


def _row_spans(scan: _Scan, tag_end: int, start: int, end: int) -> str:
    scan.patches.append(_Patch(start, scan.xml.find('"', end) + 1, ""))
    return "inRowProps"


def _cell_start(scan: _Scan, tag_end: int, start: int, end: int) -> str:
    scan.last_cell_start = start
    return "inCellProps"


def _cell_reference(scan: _Scan, tag_end: int, start: int, end: int) -> str:
    closing = scan.xml.find('"', end)
    column = column_number(scan.xml[end:closing])
    gap = column - (scan.col + 1)
    if gap > 0:
        scan.patches.append(_Patch(scan.last_cell_start, scan.last_cell_start, _EMPTY_CELL * gap))
    scan.patches.append(_Patch(start, closing + 1, ""))
    scan.col = column
    return "inCell"


def _goto(state: str) -> Callable[[_Scan, int, int, int], str]:
    return lambda scan, tag_end, start, end: state


type _Machine = dict[str, dict[str, Callable[[_Scan, int, int, int], str]]]

_STATES: _Machine = {
    "main": {"<row": _row_start},
    "inRowProps": {' r="': _row_reference, ' spans="': _row_spans, "/>": _goto("main"), ">": _goto("inCell")},
    "inCell": {"<c": _cell_start, "</row": _goto("main")},
    "inCellProps": {' r="': _cell_reference},
}


def _parse_int(text: str) -> int:
    digits = re.match(r"\s*([+-]?\d+)", text)
    return int(digits.group(1)) if digits else 0  # NaN in JS: no gap and no row number


def _execute(scan: _Scan, machine: _Machine) -> None:
    """``executeStateMachine``: visit every token in document order, following the states."""
    xml = scan.xml
    matches: list[tuple[int, int, Callable[[_Scan, int, int, int], str], str]] = []
    for name, state in machine.items():
        for token, handler in state.items():
            position = xml.find(token)
            while position != -1:
                matches.append((position, position + len(token), handler, name))
                position = xml.find(token, position + 1)
    matches.sort(key=lambda match: match[0])  # stable, like V8's sort
    current = "main"
    tag_end = 0
    for start, end, handler, name in matches:
        if name != current:
            continue
        tag = xml[start] == "<"
        boundary = end < len(xml) and xml[end] in {" ", ">"}
        if not ((tag and boundary) or (not tag and start < tag_end)):
            continue
        current = handler(scan, tag_end, start, end)
        if tag:
            tag_end = xml.find(">", end) + 1


def _apply_patches(xml: str, patches: list[_Patch]) -> str:
    """``helper.applyPatches`` for patches without priority: a second patch at the same offset is skipped."""
    ordered = sorted(patches, key=cmp_to_key(lambda left, right: left.start - right.start))
    result: list[str] = []
    last = 0
    previous: _Patch | None = None
    for patch in ordered:
        if previous is not None and patch.start == previous.start:
            continue
        result.append(xml[last : patch.start])
        result.append(patch.add)
        last = patch.stop
        previous = patch
    return "".join(result) + xml[last:]


def fill_references(xml: str) -> str:
    """``lib/xlsx.js`` ``preprocess``: references removed, skipped rows and cells made explicit."""
    scan = _Scan(xml)
    _execute(scan, _STATES)
    return _apply_patches(xml, scan.patches)
