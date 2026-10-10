# pyright: strict, reportPrivateUsage=false
"""Non-destructive DOCX run normalization before marker scanning."""

import re

from lxml import etree  # pyright: ignore[reportAttributeAccessIssue] lxml has incomplete stubs.

from carbotek.markers import parse
from carbotek.markers.syntax import MarkerSyntaxError
from carbotek.ooxml.ns import NAMESPACES, qn

from .text import ParagraphText

_NOISE_TAGS = frozenset({qn("w:proofErr"), qn("w:noProof"), qn("w:lastRenderedPageBreak")})
_MARKER = re.compile(r"\{[^{}]+?\}")


def normalize_document(root: etree._Element) -> None:
    """Normalize all paragraphs in a WordprocessingML part in place."""
    for paragraph in root.iter(qn("w:p")):
        normalize_paragraph(paragraph)


def repair_empty_table_cells(root: etree._Element) -> None:
    """Ensure structural directives cannot leave WordprocessingML cells invalid."""
    for cell in _CELLS_WITHOUT_PARAGRAPH(root):
        etree.SubElement(cell, qn("w:p"))


_CELLS_WITHOUT_PARAGRAPH = etree.XPath(
    "descendant-or-self::w:tc[not(w:p) and not(carbotek-rendered)]", namespaces={"w": NAMESPACES["w"]}
)


_FIELD_REFERENCE = re.compile(r'\b(PAGEREF|REF)(\s+)(?:"([^"]+)"|([^\s\\]+))')


def rename_bookmarks_in_copy(copy: etree._Element, seen: dict[str, int], reserved: set[str]) -> None:
    """Make the bookmarks of one loop copy unique and keep its own references pointing at them.

    The first copy keeps the template name (external references such as a TOC still find it);
    later copies get a suffix. References inside the copy (``w:hyperlink/@w:anchor``,
    ``PAGEREF``/``REF`` field codes) follow the renamed bookmark.
    """
    renamed: dict[str, str] = {}
    for start in copy.iter(qn("w:bookmarkStart")):
        name = start.get(qn("w:name"))
        if not name or name.startswith("_GoBack"):
            continue
        if name not in seen:
            seen[name] = 0
            continue
        renamed[name] = _next_bookmark_name(name, seen, reserved)
        start.set(qn("w:name"), renamed[name])
    if not renamed:
        return
    for link in copy.iter(qn("w:hyperlink")):
        if (anchor := link.get(qn("w:anchor"))) in renamed:
            link.set(qn("w:anchor"), renamed[anchor])
    for field in copy.iter(qn("w:fldSimple")):
        field.set(qn("w:instr"), _rename_field_reference(field.get(qn("w:instr"), ""), renamed))
    groups: list[list[etree._Element]] = []
    for node in copy.iter():
        if node.tag == qn("w:fldChar"):
            kind = node.get(qn("w:fldCharType"))
            if kind == "begin":
                groups.append([])
            elif kind == "end" and groups:
                _rename_instruction_group(groups.pop(), renamed)
        elif node.tag == qn("w:instrText"):
            if groups:
                groups[-1].append(node)
            else:
                node.text = _rename_field_reference(node.text or "", renamed)
    for group in groups:
        _rename_instruction_group(group, renamed)


def _next_bookmark_name(name: str, seen: dict[str, int], reserved: set[str]) -> str:
    while True:
        seen[name] += 1
        suffix = f"_{seen[name]}"
        candidate = name[: 40 - len(suffix)] + suffix
        if candidate not in reserved:
            reserved.add(candidate)
            return candidate


def _rename_field_reference(source: str, renamed: dict[str, str]) -> str:
    def replacement(match: re.Match[str]) -> str:
        name = match[3] or match[4]
        target = renamed.get(name, name)
        return match[1] + match[2] + ('"' + target + '"' if match[3] is not None else target)

    return _FIELD_REFERENCE.sub(replacement, source)


def _rename_instruction_group(nodes: list[etree._Element], renamed: dict[str, str]) -> None:
    if not nodes:
        return
    lengths = [len(node.text or "") for node in nodes]
    source = "".join(node.text or "" for node in nodes)
    rewritten = _rename_field_reference(source, renamed)
    if rewritten == source:
        return
    offset = 0
    for node, length in zip(nodes[:-1], lengths, strict=False):
        node.text = rewritten[offset : offset + length]
        offset += length
    nodes[-1].text = rewritten[offset:]


def unique_drawing_ids(roots: list[etree._Element]) -> bool:
    """Give every ``wp:docPr`` of the document (body, headers, footers) a unique ``id``.

    Loop copies clone drawings with their ids; Word treats duplicate ``docPr`` ids as corruption.
    The first occurrence keeps its id, later duplicates get the next free number.
    """
    properties = [node for root in roots for node in root.iter(qn("wp:docPr"))]
    used: set[int] = set()
    numeric = [int(node.get("id", "0")) for node in properties if node.get("id", "").isdigit()]
    next_free = max(numeric, default=0) + 1
    changed = False
    for node in properties:
        value = node.get("id", "")
        if value.isdigit() and int(value) not in used:
            used.add(int(value))
            continue
        node.set("id", str(next_free))
        used.add(next_free)
        next_free += 1
        changed = True
    return changed


def repair_bookmarks(root: etree._Element) -> None:
    """Give cloned Word bookmarks unique paired identifiers and names."""
    next_id = 0
    open_ids: dict[str, list[str]] = {}
    names: set[str] = set()
    last_suffix: dict[str, int] = {}  # first suffix worth trying per name (taken ones stay taken)
    start_tag = qn("w:bookmarkStart")
    for element in root.iter(start_tag, qn("w:bookmarkEnd")):
        if element.tag == start_tag:
            original = element.get(qn("w:id"), "")
            identifier = str(next_id)
            next_id += 1
            open_ids.setdefault(original, []).append(identifier)
            element.set(qn("w:id"), identifier)
            if name := element.get(qn("w:name")):
                unique_name = name
                suffix = last_suffix.get(name, 1)
                while unique_name in names:
                    unique_name = f"{name}_{suffix}"
                    suffix += 1
                last_suffix[name] = suffix
                names.add(unique_name)
                element.set(qn("w:name"), unique_name)
        else:
            original = element.get(qn("w:id"), "")
            if candidates := open_ids.get(original, []):
                element.set(qn("w:id"), candidates.pop(0))


def normalize_paragraph(paragraph: etree._Element) -> None:
    """Remove non-text marker noise and merge semantically identical plain runs."""
    _remove_noise(paragraph)
    _merge_runs(paragraph)
    _isolate_cross_run_markers(paragraph)


def _remove_noise(paragraph: etree._Element) -> None:
    for element in tuple(paragraph.iter()):
        if element.tag not in _NOISE_TAGS:
            continue
        parent = element.getparent()
        if parent is not None:
            parent.remove(element)


def _merge_runs(paragraph: etree._Element) -> None:
    """Merge every chain of adjacent plain text runs with equal properties in one pass.

    A merged run keeps absorbing its right neighbours, so the result equals repeated pairwise
    merging; the canonical properties of each run are serialized once.
    """
    run_tag = qn("w:r")
    current: etree._Element | None = None
    current_key: bytes | None = None
    for child in list(paragraph):
        if child.tag != run_tag or not _is_plain_text_run(child):
            current, current_key = None, None
            continue
        key = _run_properties(child)
        if current is not None and key == current_key:
            _merge(current, child)
            paragraph.remove(child)
            continue
        current, current_key = child, key


def _is_plain_text_run(run: etree._Element) -> bool:
    return all(child.tag in {qn("w:rPr"), qn("w:t")} for child in run) and bool(run.findall(qn("w:t")))


def _run_properties(run: etree._Element) -> bytes:
    properties = run.find(qn("w:rPr"))
    # Canonical XML: attribute order and namespace prefixes do not make two formats different.
    return b"" if properties is None else etree.tostring(properties, method="c14n")


def _merge(left: etree._Element, right: etree._Element) -> None:
    target = left.findall(qn("w:t"))[-1]
    target.text = (target.text or "") + "".join(text.text or "" for text in right.findall(qn("w:t")))
    target.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")


def _isolate_cross_run_markers(paragraph: etree._Element) -> None:
    projection = ParagraphText.from_paragraph(paragraph)
    for match in reversed(tuple(_MARKER.finditer(projection.value))):
        start_node, start_offset = projection.locate(match.start(), prefer_next=True)
        end_node, end_offset = projection.locate(match.end())
        if start_node is end_node:
            continue
        start_text = start_node.text or ""
        end_text = end_node.text or ""
        # The marker moves into the run where it starts; text after it keeps its own run (JS).
        start_node.text = f"{start_text[:start_offset]}{match.group(0)}"
        start_node.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
        tail = end_text[end_offset:]
        reached_end = False
        for span in projection.spans:
            if span.element is start_node:
                continue
            if span.element is end_node:
                span.element.text = tail
                if tail:
                    span.element.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
                reached_end = True
                break
            if not reached_end and span.start >= match.start() and span.end <= match.end():
                span.element.text = ""


_HTML_CALL = re.compile(r"\{(?P<prefix>[^{}]*?):html\(?(?P<options>[^{()}]*?)\)?[^{<()>}]*?\}")


def prepare_malformed_html_calls(root: etree._Element, filename: str) -> None:
    """Match JS HTML preprocessing when an unclosed quote consumes the HTML call."""
    for node in root.iter(qn("w:t")):
        source = node.text or ""
        if ":html" not in source:
            continue

        def prepare(match: re.Match[str], text_node: etree._Element = node) -> str:
            try:
                marker = parse(match[0])
            except MarkerSyntaxError:
                return match[0]  # declarations and invalid markers retain their own validation path
            if any(call.name == "html" for call in marker.formatters):
                return match[0]
            options: dict[str, str] = {}
            for item in match["options"].split(",")[:2]:
                name, _, value = item.strip().partition(":")
                if name in {"inline", "nospace"} and value in {"", "true"}:
                    options[name] = name
                elif name in {"tabletheme", "headingtheme", "quotetheme"} and value:
                    options[name] = re.sub(r"\s", "", value)
            arguments = [filename.rsplit("/", 1)[-1], _malformed_html_style(text_node)]
            arguments.extend(
                options.get(name, "") for name in ("inline", "nospace", "tabletheme", "headingtheme", "quotetheme")
            )
            while len(arguments) > 1 and not arguments[-1]:
                arguments.pop()
            helper = "getHTMLContentDocx(" + ",".join("'" + argument + "'" for argument in arguments) + ")"
            return "{" + match["prefix"] + ":" + helper + "}"

        node.text = _HTML_CALL.sub(prepare, source)


def _malformed_html_style(text: etree._Element) -> str:
    paragraph = next((parent for parent in text.iterancestors() if parent.tag == qn("w:p")), None)
    if paragraph is None:
        return ""
    preceding: list[etree._Element] = []
    for node in paragraph.iter():
        if node is text:
            break
        preceding.append(node)
    fragments: list[str] = []
    fonts = next((node for node in preceding if node.tag == qn("w:rFonts")), None)
    if fonts is not None:
        font = next(
            (
                value
                for key, value in fonts.attrib.items()
                if key in {qn("w:ascii"), qn("w:hAnsi"), qn("w:cs"), qn("w:eastAsia")}
            ),
            "",
        )
        if font:
            fragments.append("-ff" + font)
    for tag, attribute, suffix in (
        ("sz", "val", "fs"),
        ("bidi", "", "rtl"),
        ("color", "val", "tcolor"),
        ("highlight", "val", "bgcolor"),
        ("shd", "fill", "shadowcolor"),
        ("jc", "val", "textalign"),
    ):
        node = next((node for node in preceding if node.tag == qn("w:" + tag)), None)
        value = node.get(qn("w:" + attribute), "") if node is not None and attribute else ""
        if value or (node is not None and not attribute):
            fragments.append("-" + suffix + value)
    return "style" + "".join(fragments) if fragments else ""
