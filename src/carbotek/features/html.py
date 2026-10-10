# pyright: strict, reportPrivateUsage=false
"""Safe, tree-based conversion of basic HTML markers into Word paragraphs."""

import re
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import Any

from lxml import etree, html  # pyright: ignore[reportAttributeAccessIssue]
lazy from PIL import ImageColor

from carbotek.docx.renderer import CONDITION_TESTS, DocxTextRenderer, formatter_argument, resolve_path
from carbotek.docx.session import CURRENT_RENDER, feature_parse, feature_scan, feature_source
from carbotek.errors import MarkerResolutionError
from carbotek.formatters import FormatterRegistry, MarkerContext, shared_registry
from carbotek.formatters.docx import check_url
from carbotek.formatters.number import number_string
from carbotek.markers import Marker
from carbotek.ooxml.ns import NAMESPACES, qn
from carbotek.ooxml.xml import parse_xml, serialize_xml
from carbotek.options import RenderOptions
from carbotek.package import TemplatePackage, TemplatePart
from carbotek.typing_helpers import is_mapping

from .hyperlinks import _anchor
from .image import HTML_IMAGE_PREFIX

_BLOCK_TAGS = {
    "address",
    "article",
    "blockquote",
    "div",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "li",
    "ol",
    "p",
    "pre",
    "table",
    "ul",
}
_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_HYPERLINK_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink"
_NUMBERING_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/numbering"
_NUMBERING_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml"
_STYLES_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles"
_STYLES_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"
_HEADING_STYLE = (("2F5496", 32), ("2F5496", 26), ("1F3763", 24), ("2F5496", 22), ("2F5496", 22), ("1F3763", 22))


class _HtmlNumbering:
    def __init__(self, package: TemplatePackage) -> None:
        existing = package.parts.get("word/numbering.xml")
        self.root = (
            parse_xml(existing.content)
            if existing is not None
            else etree.Element(qn("w:numbering"), nsmap={"w": NAMESPACES["w"]})
        )
        self.abstract_ids = {
            int(node.get(qn("w:abstractNumId"), ""))
            for node in self.root.findall(qn("w:abstractNum"))
            if node.get(qn("w:abstractNumId"), "").isdigit()
        }
        self.num_ids = {
            int(node.get(qn("w:numId"), ""))
            for node in self.root.findall(qn("w:num"))
            if node.get(qn("w:numId"), "").isdigit()
        }
        self.changed = False

    def new_list(self) -> tuple[int, etree._Element]:
        """One numbering per top-level HTML list (lib/html.js ``listId``); levels are added on use."""
        abstract_id = max({999, *self.abstract_ids}) + 1
        num_id = max({999, *self.num_ids}) + 1
        self.abstract_ids.add(abstract_id)
        self.num_ids.add(num_id)
        abstract = etree.Element(qn("w:abstractNum"))
        first_number = next(
            (index for index, child in enumerate(self.root) if child.tag == qn("w:num")), len(self.root)
        )
        self.root.insert(first_number, abstract)
        abstract.set(qn("w:abstractNumId"), str(abstract_id))
        etree.SubElement(abstract, qn("w:multiLevelType")).set(qn("w:val"), "hybridMultilevel")
        number = etree.SubElement(self.root, qn("w:num"))
        number.set(qn("w:numId"), str(num_id))
        etree.SubElement(number, qn("w:abstractNumId")).set(qn("w:val"), str(abstract_id))
        self.changed = True
        return num_id, abstract

    @staticmethod
    def add_level(abstract: etree._Element, ordered: bool, level: int, start: int = 1) -> None:
        """``getListStyleDocx(type, level)``: Symbol/Courier New/Wingdings bullets or ``%N.``."""
        variant = level % 3
        definition = etree.SubElement(abstract, qn("w:lvl"))
        definition.set(qn("w:ilvl"), str(level))
        etree.SubElement(definition, qn("w:start")).set(qn("w:val"), str(start))
        etree.SubElement(definition, qn("w:numFmt")).set(qn("w:val"), "decimal" if ordered else "bullet")
        etree.SubElement(definition, qn("w:lvlText")).set(qn("w:val"), f"%{level + 1}." if ordered else "o"[variant])
        etree.SubElement(definition, qn("w:lvlJc")).set(qn("w:val"), "left")
        indent = etree.SubElement(etree.SubElement(definition, qn("w:pPr")), qn("w:ind"))
        indent.set(qn("w:left"), str(720 + level * 720))
        indent.set(qn("w:hanging"), "360")
        if not ordered:
            font = ("Symbol", "Courier New", "Wingdings")[variant]
            fonts = etree.SubElement(etree.SubElement(definition, qn("w:rPr")), qn("w:rFonts"))
            fonts.set(qn("w:ascii"), font)
            fonts.set(qn("w:hAnsi"), font)
            fonts.set(qn("w:hint"), "default")

    def part(self) -> TemplatePart:
        return TemplatePart(
            "word/numbering.xml",
            serialize_xml(self.root),
            True,
        )


class _HtmlLinks:
    def __init__(self, package: TemplatePackage, part_name: str, numbering: _HtmlNumbering) -> None:
        parent, _, filename = part_name.rpartition("/")
        self.part_name = f"{parent}/_rels/{filename}.rels"
        existing = package.parts.get(self.part_name)
        self.root = parse_xml(existing.content) if existing is not None else etree.Element(f"{{{_REL}}}Relationships")
        self.used = {node.get("Id") for node in self.root}
        self.urls: dict[str, str] = {}
        self.changed = False
        self.has_links = False
        self.has_headings = False
        self.has_table = False
        self.image_counter = 0
        self.literal_texts: set[etree._Element] = set()
        self.numbering = numbering
        self.current_list: tuple[int, etree._Element, int] | None = None
        self.table_theme = ""
        self.heading_theme = ""
        self.quote_theme = ""
        self.no_space = False

    def target(self, href: str) -> tuple[str, str] | None:
        anchor = _anchor(href)
        if anchor is not None:
            self.has_links = True
            return "anchor", anchor
        url = check_url(href)
        self.has_links = True
        if url in self.urls:
            return "id", self.urls[url]
        index = 0
        while f"CarbotekHyperlinkId{index}" in self.used:
            index += 1
        relationship_id = f"CarbotekHyperlinkId{index}"
        self.used.add(relationship_id)
        self.urls[url] = relationship_id
        etree.SubElement(
            self.root,
            f"{{{_REL}}}Relationship",
            Id=relationship_id,
            Type=_HYPERLINK_REL,
            Target=url,
            TargetMode="External",
        )
        self.changed = True
        return "id", relationship_id

    def part(self) -> TemplatePart:
        return TemplatePart(self.part_name, serialize_xml(self.root), True)


def _write_numbering(
    package: TemplatePackage, replacements: dict[str, TemplatePart], numbering: _HtmlNumbering
) -> bool:
    if not numbering.changed:
        return False
    replacements["word/numbering.xml"] = numbering.part()
    content_types = package.parts.get("[Content_Types].xml")
    if content_types is not None:
        types_root = parse_xml(replacements.get(content_types.name, content_types).content)
        if all(node.get("PartName") != "/word/numbering.xml" for node in types_root):
            etree.SubElement(types_root, qn("ct:Override"), PartName="/word/numbering.xml", ContentType=_NUMBERING_MIME)
        replacements[content_types.name] = TemplatePart(
            content_types.name,
            serialize_xml(types_root),
            True,
        )
    rel_name = "word/_rels/document.xml.rels"
    existing_rels = replacements.get(rel_name) or package.parts.get(rel_name)
    rel_root = (
        parse_xml(existing_rels.content) if existing_rels is not None else etree.Element(f"{{{_REL}}}Relationships")
    )
    if all(node.get("Type") != _NUMBERING_REL for node in rel_root):
        # Appendix C: lib/html.js uses the fixed id rIdCarbotekNumbering; a suffix only on a clash.
        ids = {node.get("Id") for node in rel_root}
        index = 0
        candidate = "rIdCarbotekNumbering"
        while candidate in ids:
            index += 1
            candidate = f"rIdCarbotekNumbering{index}"
        etree.SubElement(
            rel_root,
            f"{{{_REL}}}Relationship",
            Id=candidate,
            Type=_NUMBERING_REL,
            Target="numbering.xml",
        )
        replacements[rel_name] = TemplatePart(rel_name, serialize_xml(rel_root), True)
    return True


def _write_styles(
    package: TemplatePackage,
    replacements: dict[str, TemplatePart],
    has_links: bool,
    has_headings: bool,
    has_table: bool,
) -> bool:
    if not (has_links or has_headings or has_table):
        return False
    name = "word/styles.xml"
    existing = package.parts.get(name)
    root = (
        parse_xml(existing.content)
        if existing is not None
        else etree.Element(qn("w:styles"), nsmap={"w": NAMESPACES["w"]})
    )
    used = {style.get(qn("w:styleId")) for style in root.findall(qn("w:style"))}
    if has_links and "Hyperlink" not in used:
        style = etree.SubElement(root, qn("w:style"))
        style.set(qn("w:type"), "character")
        style.set(qn("w:styleId"), "Hyperlink")
        etree.SubElement(style, qn("w:name")).set(qn("w:val"), "Hyperlink")
        etree.SubElement(style, qn("w:uiPriority")).set(qn("w:val"), "99")
        properties = etree.SubElement(style, qn("w:rPr"))
        link_color = etree.SubElement(properties, qn("w:color"))
        link_color.set(qn("w:val"), "0563C1")
        link_color.set(qn("w:themeColor"), "hyperlink")
        etree.SubElement(properties, qn("w:u")).set(qn("w:val"), "single")
    if has_headings:
        for level in range(1, 7):
            if f"Heading{level}" not in used:
                _add_heading_style(root, level)
    if has_table and "TableGrid" not in used:
        _add_table_style(root)
    replacements[name] = TemplatePart(name, serialize_xml(root), True)
    _ensure_part_link(package, replacements, name, _STYLES_REL, _STYLES_MIME)
    return True


def _add_heading_style(root: etree._Element, level: int) -> None:
    color, size = _HEADING_STYLE[level - 1]
    style = etree.SubElement(root, qn("w:style"))
    style.set(qn("w:type"), "paragraph")
    style.set(qn("w:styleId"), f"Heading{level}")
    etree.SubElement(style, qn("w:name")).set(qn("w:val"), f"heading {level}")
    etree.SubElement(style, qn("w:basedOn")).set(qn("w:val"), "Normal")
    etree.SubElement(style, qn("w:qFormat"))
    paragraph = etree.SubElement(style, qn("w:pPr"))
    etree.SubElement(paragraph, qn("w:outlineLvl")).set(qn("w:val"), str(level - 1))
    spacing = etree.SubElement(paragraph, qn("w:spacing"))
    spacing.set(qn("w:before"), "240" if level == 1 else "40")
    spacing.set(qn("w:after"), "0")
    properties = etree.SubElement(style, qn("w:rPr"))
    fonts = etree.SubElement(properties, qn("w:rFonts"))
    for attr in ("ascii", "hAnsi", "cs"):
        fonts.set(qn(f"w:{attr}"), "Aptos")
    etree.SubElement(properties, qn("w:color")).set(qn("w:val"), color)
    for tag in ("sz", "szCs"):
        etree.SubElement(properties, qn(f"w:{tag}")).set(qn("w:val"), str(size))


def _add_table_style(root: etree._Element) -> None:
    style = etree.SubElement(root, qn("w:style"))
    style.set(qn("w:type"), "table")
    style.set(qn("w:styleId"), "TableGrid")
    etree.SubElement(style, qn("w:name")).set(qn("w:val"), "Table Grid")
    properties = etree.SubElement(style, qn("w:tblPr"))
    indent = etree.SubElement(properties, qn("w:tblInd"))
    indent.set(qn("w:w"), "0")
    indent.set(qn("w:type"), "dxa")
    margins = etree.SubElement(properties, qn("w:tblCellMar"))
    for side in ("top", "left", "bottom", "right"):
        margin = etree.SubElement(margins, qn(f"w:{side}"))
        margin.set(qn("w:w"), "108" if side in {"left", "right"} else "0")
        margin.set(qn("w:type"), "dxa")
    borders = etree.SubElement(properties, qn("w:tblBorders"))
    for side in ("top", "left", "bottom", "right", "insideH", "insideV"):
        border = etree.SubElement(borders, qn(f"w:{side}"))
        for attr, value in (("val", "single"), ("sz", "4"), ("space", "0"), ("color", "auto")):
            border.set(qn(f"w:{attr}"), value)


def _ensure_part_link(
    package: TemplatePackage, replacements: dict[str, TemplatePart], name: str, relationship_type: str, mime: str
) -> None:
    content_types = package.parts.get("[Content_Types].xml")
    if content_types is not None:
        root = parse_xml(replacements.get(content_types.name, content_types).content)
        if all(node.get("PartName") != f"/{name}" for node in root):
            etree.SubElement(root, qn("ct:Override"), PartName=f"/{name}", ContentType=mime)
        replacements[content_types.name] = TemplatePart(content_types.name, serialize_xml(root), True)
    rel_name = "word/_rels/document.xml.rels"
    existing = replacements.get(rel_name) or package.parts.get(rel_name)
    rels = parse_xml(existing.content) if existing is not None else etree.Element(f"{{{_REL}}}Relationships")
    if all(node.get("Type") != relationship_type for node in rels):
        used = {node.get("Id") for node in rels}
        index = 0
        while f"rIdCarbotekStyles{index}" in used:
            index += 1
        etree.SubElement(
            rels, f"{{{_REL}}}Relationship", Id=f"rIdCarbotekStyles{index}", Type=relationship_type, Target="styles.xml"
        )
        replacements[rel_name] = TemplatePart(rel_name, serialize_xml(rels), True)


def _ensure_rels_content_type(package: TemplatePackage, replacements: dict[str, TemplatePart]) -> None:
    content_types = package.parts.get("[Content_Types].xml")
    if content_types is None:
        return
    root = parse_xml(replacements.get(content_types.name, content_types).content)
    if all(node.get("Extension") != "rels" for node in root):
        etree.SubElement(
            root,
            qn("ct:Default"),
            Extension="rels",
            ContentType="application/vnd.openxmlformats-package.relationships+xml",
        )
        replacements[content_types.name] = TemplatePart(content_types.name, serialize_xml(root), True)


@dataclass(frozen=True, slots=True)
class HtmlSubstitution:
    """Replace the paragraph containing a single ``:html`` marker with OOXML paragraphs."""

    def apply(
        self, package: TemplatePackage, data: Mapping[str, Any] | list[Any], options: RenderOptions
    ) -> TemplatePackage:
        replacements: dict[str, TemplatePart] = {}
        numbering = _HtmlNumbering(package)
        registry = shared_registry()
        context = MarkerContext(data=dict(data) if is_mapping(data) else {}, complement=dict(options.complement))
        new_relationships = False
        has_links = False
        has_headings = False
        has_table = False
        for part in package.parts.values():
            if not part.is_xml or not part.name.startswith(
                ("word/document", "word/header", "word/footer", "word/footnotes", "word/endnotes")
            ):
                continue
            if not part.contains(":html", attributes=False):
                continue  # no HTML marker left in this part
            root = part.tree()
            links = _HtmlLinks(package, part.name, numbering)
            if _replace_html_markers(root, data, links, options, registry, context):
                replacements[part.name] = TemplatePart.from_tree(part.name, root)
            if links.changed:
                replacements[links.part_name] = links.part()
                new_relationships = True
            has_links = has_links or links.has_links
            has_headings = has_headings or links.has_headings
            has_table = has_table or links.has_table
        if new_relationships := _write_numbering(package, replacements, numbering) or new_relationships:
            content_types = package.parts.get("[Content_Types].xml")
            if content_types is not None:
                types_root = parse_xml(replacements.get(content_types.name, content_types).content)
                if all(node.get("Extension") != "rels" for node in types_root):
                    etree.SubElement(
                        types_root,
                        qn("ct:Default"),
                        Extension="rels",
                        ContentType="application/vnd.openxmlformats-package.relationships+xml",
                    )
                    replacements[content_types.name] = TemplatePart(
                        content_types.name,
                        serialize_xml(types_root),
                        True,
                    )
        if _write_styles(package, replacements, has_links, has_headings, has_table):
            _ensure_rels_content_type(package, replacements)
        return package.replace_parts(replacements) if replacements else package


def _replace_html_markers(
    root: etree._Element,
    data: Mapping[str, Any] | list[Any],
    links: _HtmlLinks,
    options: RenderOptions,
    registry: FormatterRegistry,
    context: MarkerContext,
) -> bool:
    replacements_needed = [(text, _html_marker(text.text or "")) for text in root.iter(qn("w:t"))]
    changed = False
    for text, marker in replacements_needed:
        if _replace_inline_html(text, data, links, options, registry, context):
            changed = True
            continue
        if _replace_embedded_html(text, data, links, options, registry, context):
            changed = True
            continue
        if marker is None:
            continue
        paragraph = _ancestor(text, qn("w:p"))
        parent = paragraph.getparent() if paragraph is not None else None
        if paragraph is None or parent is None:
            continue
        links.table_theme, links.heading_theme, links.quote_theme = _html_themes(marker)
        links.no_space = _html_boolean_option(marker, "nospace")
        nodes = _paragraphs(_html_value(marker, data, options, registry, context), links)
        marker_run = text.getparent()
        _apply_template_style(
            nodes,
            *_template_style(
                paragraph.find(qn("w:pPr")), marker_run.find(qn("w:rPr")) if marker_run is not None else None
            ),
        )
        index = parent.index(paragraph)
        parent.remove(paragraph)
        for offset, node in enumerate(nodes):
            parent.insert(index + offset, node)
        changed = True
    if changed:
        for text in list(root.iter(qn("w:t"))):
            if text not in links.literal_texts:
                _replace_inline_html(text, data, links, options, registry, context)
    return changed


def _html_marker(source: str):
    occurrences = feature_scan(source)
    if len(occurrences) != 1 or source != occurrences[0].raw:
        return None
    marker = feature_parse(occurrences[0].raw)
    return marker if _is_html_marker(marker) else None


def _is_html_marker(marker: Marker) -> bool:
    return (
        marker.root in {"d", "c"}
        and any(call.name == "html" for call in marker.formatters)
        and re.fullmatch(r"\{[^{}]*?:html\(?[^{()}]*?\)?[^{<()>}]*?\}", feature_source(marker)) is not None
    )


def _html_themes(marker: Marker) -> tuple[str, str, str]:
    themes = {"tabletheme": "", "headingtheme": "", "quotetheme": ""}
    call = next((call for call in marker.formatters if call.name == "html"), None)
    if call is not None:
        for argument in call.arguments[:2]:
            name, separator, value = argument.partition(":")
            name, value = name.strip().lower(), value.strip()
            if separator and name in themes and re.fullmatch(r"[\w\s-]{1,100}", value):
                themes[name] = value
    return themes["tabletheme"], themes["headingtheme"], themes["quotetheme"]


def _html_boolean_option(marker: Marker, option: str) -> bool:
    call = next((call for call in marker.formatters if call.name == "html"), None)
    if call is None:
        return False
    for argument in call.arguments[:2]:
        name, separator, value = argument.partition(":")
        if name.strip().lower() == option:
            return not separator or value.strip().lower() == "true"
    return False


def _replace_inline_html(
    text: etree._Element,
    data: Mapping[str, Any] | list[Any],
    links: _HtmlLinks,
    options: RenderOptions,
    registry: FormatterRegistry,
    context: MarkerContext,
) -> bool:
    source = text.text or ""
    if any(
        _is_html_marker(marker) and not _html_boolean_option(marker, "inline")
        for marker in (feature_parse(occurrence.raw) for occurrence in feature_scan(source))
    ):
        return False
    occurrences = [
        (occurrence, marker)
        for occurrence in feature_scan(source)
        if _is_html_marker(marker := feature_parse(occurrence.raw)) and _html_boolean_option(marker, "inline")
    ]
    run = text.getparent()
    parent = run.getparent() if run is not None else None
    if not occurrences or run is None or run.tag != qn("w:r") or parent is None:
        return False
    if any(child is not text and child.tag != qn("w:rPr") for child in run):
        return False
    if parent.tag != qn("w:p"):
        return False
    nodes: list[etree._Element] = []
    start = 0
    properties = run.find(qn("w:rPr"))
    for occurrence, marker in occurrences:
        _append_original_run(nodes, source[start : occurrence.start], properties)
        links.table_theme, links.heading_theme, links.quote_theme = _html_themes(marker)
        links.no_space = _html_boolean_option(marker, "nospace")
        inline_nodes = _inline_html_nodes(_html_value(marker, data, options, registry, context), links)
        _apply_template_style(inline_nodes, _template_style(None, properties)[0], [])
        nodes.extend(inline_nodes)
        start = occurrence.end
    _append_original_run(nodes, source[start:], properties)
    index = parent.index(run)
    parent.remove(run)
    for offset, node in enumerate(nodes):
        parent.insert(index + offset, node)
    return True


def _replace_embedded_html(
    text: etree._Element,
    data: Mapping[str, Any] | list[Any],
    links: _HtmlLinks,
    options: RenderOptions,
    registry: FormatterRegistry,
    context: MarkerContext,
) -> bool:
    source = text.text or ""
    occurrences = [
        (occurrence, marker)
        for occurrence in feature_scan(source)
        if _is_html_marker(marker := feature_parse(occurrence.raw)) and not _html_boolean_option(marker, "inline")
    ]
    run = text.getparent()
    if not occurrences:
        return False
    if run is None or run.tag != qn("w:r") or any(child is not text and child.tag != qn("w:rPr") for child in run):
        return False
    paragraph = run.getparent()
    if paragraph is None or paragraph.tag != qn("w:p") or paragraph.getparent() is None:
        return False
    run_index = paragraph.index(run)
    before = [deepcopy(child) for child in paragraph[:run_index] if child.tag != qn("w:pPr")]
    after = [deepcopy(child) for child in paragraph[run_index + 1 :]]
    paragraph_properties = paragraph.find(qn("w:pPr"))
    run_properties = run.find(qn("w:rPr"))
    nodes: list[etree._Element] = []
    start = 0
    for index, (occurrence, marker) in enumerate(occurrences):
        prefix = source[start : occurrence.start]
        leading = before if index == 0 else []
        if leading or prefix:
            nodes.append(_split_paragraph(leading, prefix, paragraph_properties, run_properties))
        links.table_theme, links.heading_theme, links.quote_theme = _html_themes(marker)
        links.no_space = _html_boolean_option(marker, "nospace")
        value = _html_value(marker, data, options, registry, context)
        if _html_source(value):
            generated = _paragraphs(value, links)
            _apply_template_style(generated, *_template_style(paragraph_properties, run_properties))
            nodes.extend(generated)
        start = occurrence.end
    suffix = source[start:]
    if suffix or after:
        nodes.append(_split_paragraph(after, suffix, paragraph_properties, run_properties, trailing=True))
    parent = paragraph.getparent()
    if parent is None:
        return False
    position = parent.index(paragraph)
    parent.remove(paragraph)
    for offset, node in enumerate(nodes):
        parent.insert(position + offset, node)
    return True


_RPR_ORDER = [
    "rStyle",
    "rFonts",
    "b",
    "bCs",
    "i",
    "iCs",
    "caps",
    "smallCaps",
    "strike",
    "dstrike",
    "outline",
    "shadow",
    "emboss",
    "imprint",
    "noProof",
    "snapToGrid",
    "vanish",
    "webHidden",
    "color",
    "spacing",
    "w",
    "kern",
    "position",
    "sz",
    "szCs",
    "highlight",
    "u",
    "effect",
    "bdr",
    "shd",
    "fitText",
    "vertAlign",
    "rtl",
    "cs",
    "em",
    "lang",
    "eastAsianLayout",
    "specVanish",
    "oMath",
]
_PPR_ORDER = [
    "pStyle",
    "keepNext",
    "keepLines",
    "pageBreakBefore",
    "framePr",
    "widowControl",
    "numPr",
    "suppressLineNumbers",
    "pBdr",
    "shd",
    "tabs",
    "suppressAutoHyphens",
    "kinsoku",
    "wordWrap",
    "overflowPunct",
    "topLinePunct",
    "autoSpaceDE",
    "autoSpaceDN",
    "bidi",
    "adjustRightInd",
    "snapToGrid",
    "spacing",
    "ind",
    "contextualSpacing",
    "mirrorIndents",
    "suppressOverlap",
    "jc",
    "textDirection",
    "textAlignment",
    "textboxTightWrap",
    "outlineLvl",
    "divId",
    "cnfStyle",
    "rPr",
    "sectPr",
    "pPrChange",
]
_INHERITED_RUN = ("rFonts", "color", "sz", "szCs", "highlight", "shd")
_INHERITED_PARAGRAPH = ("bidi", "jc")


def _insert_ordered(container: etree._Element, element: etree._Element, order: list[str]) -> None:
    """Insert a property element at its schema position (CT_RPr / CT_PPr are sequences)."""
    rank = order.index(etree.QName(element).localname)
    for index, child in enumerate(container):
        name = etree.QName(child).localname
        if name in order and order.index(name) > rank:
            container.insert(index, element)
            return
    container.append(element)


def _template_style(
    paragraph_properties: etree._Element | None, run_properties: etree._Element | None
) -> tuple[list[etree._Element], list[etree._Element]]:
    """``getTemplateDefaultStyles``: run and paragraph formatting of the marker's paragraph."""
    runs = [] if run_properties is None else [c for c in run_properties if etree.QName(c).localname in _INHERITED_RUN]
    paragraphs = (
        []
        if paragraph_properties is None
        else [c for c in paragraph_properties if etree.QName(c).localname in _INHERITED_PARAGRAPH]
    )
    return runs, paragraphs


def _apply_template_style(
    nodes: list[etree._Element], run_style: list[etree._Element], paragraph_style: list[etree._Element]
) -> None:
    """Give generated paragraphs and runs the template formatting HTML did not set itself."""
    if not run_style and not paragraph_style:
        return
    for node in nodes:
        paragraphs = [node] if node.tag == qn("w:p") else []
        runs = [node] if node.tag == qn("w:r") else list(node.iter(qn("w:r"))) if node.tag == qn("w:p") else []
        for paragraph in paragraphs:
            properties = paragraph.find(qn("w:pPr"))
            for item in paragraph_style:
                if properties is None:
                    properties = etree.Element(qn("w:pPr"))
                    paragraph.insert(0, properties)
                if properties.find(str(item.tag)) is None:
                    _insert_ordered(properties, deepcopy(item), _PPR_ORDER)
        for run in runs:
            if run.find(qn("w:t")) is None:
                continue
            properties = run.find(qn("w:rPr"))
            for item in run_style:
                if properties is None:
                    properties = etree.Element(qn("w:rPr"))
                    run.insert(0, properties)
                if properties.find(str(item.tag)) is None:
                    _insert_ordered(properties, deepcopy(item), _RPR_ORDER)


def _split_paragraph(
    siblings: list[etree._Element],
    value: str,
    paragraph_properties: etree._Element | None,
    run_properties: etree._Element | None,
    *,
    trailing: bool = False,
) -> etree._Element:
    paragraph = etree.Element(qn("w:p"))
    if paragraph_properties is not None:
        paragraph.append(deepcopy(paragraph_properties))
    runs: list[etree._Element] = []
    _append_original_run(runs, value, run_properties)
    if trailing:
        paragraph.extend(runs)
        paragraph.extend(siblings)
    else:
        paragraph.extend(siblings)
        paragraph.extend(runs)
    return paragraph


def _append_original_run(nodes: list[etree._Element], value: str, properties: etree._Element | None) -> None:
    if not value:
        return
    run = etree.Element(qn("w:r"))
    if properties is not None:
        run.append(deepcopy(properties))
    text = etree.SubElement(run, qn("w:t"))
    if value[:1].isspace() or value[-1:].isspace():
        text.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    text.text = value
    nodes.append(run)


def _escape_invalid_numeric_entities(source: str) -> str:
    def replace(match: re.Match[str]) -> str:
        entity = match.group()
        digits = entity[2:-1]
        numeric = re.fullmatch(r"(?:[0-9]{1,7}|[xX][0-9A-Fa-f]{1,6})", digits)
        if numeric is not None:
            point = int(digits[1:], 16) if digits[:1] in {"x", "X"} else int(digits)
            if point in {9, 10, 13} or 32 <= point <= 55295 or 57344 <= point <= 65533 or 65536 <= point <= 1114111:
                return entity
        return "&amp;" + entity[1:]

    return re.sub(r"&#[\w]+;", replace, source)


def _inline_html_nodes(value: Any, links: _HtmlLinks) -> list[etree._Element]:
    source = _escape_invalid_numeric_entities(_html_source(value))
    for entity in ("&ensp;", "&emsp;"):
        source = source.replace(entity, "<tab/>")
    try:
        fragments = html.fragments_fromstring(source)
    except etree.ParserError, ValueError:
        fragments = [source]
    if not fragments and source:
        fragments = [source]  # whitespace-only HTML keeps its spaces in lib/html.js
    paragraph = etree.Element(qn("w:p"))
    for fragment in fragments:
        if isinstance(fragment, str):
            _append_run(paragraph, fragment, False, False, False)
        else:
            _append_html_runs(paragraph, fragment, False, False, False, links)
            _append_run(paragraph, fragment.tail or "", False, False, False)
    if CURRENT_RENDER.get() is None:
        links.literal_texts.update(paragraph.iter(qn("w:t")))
    return list(paragraph)


def _resolve(path: tuple[str, ...], data: Mapping[str, Any] | list[Any]) -> Any:
    """Resolve a marker path, including loop items frozen to ``field[N]`` by the renderer."""
    return resolve_path(path, data)


def _html_value(
    marker: Marker,
    data: Mapping[str, Any] | list[Any],
    options: RenderOptions,
    registry: FormatterRegistry,
    context: MarkerContext,
) -> Any:
    session = CURRENT_RENDER.get()
    if session is not None:
        html_index = next(index for index, formatter in enumerate(marker.formatters) if formatter.name == "html")
        prior = tuple(
            replace(call, name="convCRLFH") if call.name == "convCRLF" else call
            for call in marker.formatters[:html_index]
        )
        return session.evaluate(replace(marker, formatters=prior))
    source = data if marker.root == "d" else context.complement
    value = _resolve(marker.path, source)
    html_index = next(index for index, formatter in enumerate(marker.formatters) if formatter.name == "html")
    prior = tuple(
        replace(formatter, name="convCRLFH") if formatter.name == "convCRLF" else formatter
        for formatter in marker.formatters[:html_index]
    )
    conditional = {*CONDITION_TESTS, "and", "or", "ifEmpty", "ifEqual", "ifContain", "show", "elseShow"}
    if any(formatter.name in conditional for formatter in prior):
        return DocxTextRenderer(registry)._format(value, replace(marker, formatters=prior), options, context, data, {})
    for formatter in prior:
        arguments = tuple(formatter_argument(arg, data, context.complement, {}, marker) for arg in formatter.arguments)
        value = registry.apply(formatter.name, value, arguments, render_options=options, marker_context=context)
    return value


def _paragraphs(value: Any, links: _HtmlLinks) -> list[etree._Element]:
    source = _escape_invalid_numeric_entities(_html_source(value))
    for entity in ("&ensp;", "&emsp;"):
        source = source.replace(entity, "<tab/>")
    for entity in ("&#x9;", "&Tab;", "&#9;"):
        source = source.replace(entity, " ")
    try:
        fragments = html.fragments_fromstring(source)
    except etree.ParserError, ValueError:
        fragments = [source]
    if not fragments and source:
        fragments = [source]  # whitespace-only HTML keeps its spaces in lib/html.js
    paragraphs: list[etree._Element] = []
    inline = etree.Element(qn("w:p"))
    for fragment in fragments:
        if isinstance(fragment, str):
            _append_run(inline, fragment, False, False, False)
            continue
        if _html_tag(fragment) in _BLOCK_TAGS:
            if len(inline):
                paragraphs.append(inline)
                inline = etree.Element(qn("w:p"))
            paragraphs.extend(_block_paragraphs(fragment, links))
        elif _html_tag(fragment):
            _append_html_runs(inline, fragment, False, False, False, links)
        _append_run(inline, fragment.tail or "", False, False, False)
    if len(inline) or not paragraphs:
        paragraphs.append(inline)
    if CURRENT_RENDER.get() is None:
        links.literal_texts.update(text for paragraph in paragraphs for text in paragraph.iter(qn("w:t")))
    return paragraphs


def _html_source(value: Any) -> str:
    if value is None or isinstance(value, Mapping | list | tuple):
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return number_string(value)
    return str(value)


def _html_tag(node: etree._Element) -> str:
    return node.tag.lower() if isinstance(node.tag, str) else ""


def _block_paragraphs(node: etree._Element, links: _HtmlLinks, depth: int = 0) -> list[etree._Element]:
    tag = _html_tag(node)
    if tag == "table":
        return _with_block_spacing([_table_from_html(node, links)], tag, links)
    if tag in {"ul", "ol"}:
        paragraphs: list[etree._Element] = []
        ordered = tag == "ol"
        try:
            start = max(1, min(int(node.get("start", "1")), 1_000_000)) if ordered else 1
        except ValueError:
            start = 1
        if depth == 0 or links.current_list is None:
            links.current_list = (*links.numbering.new_list(), -1)
        number_id, abstract, list_depth = links.current_list
        level = min(depth, 8)
        if level > list_depth:
            links.numbering.add_level(abstract, ordered, level, start)
            links.current_list = (number_id, abstract, level)
        for item in node.findall("./li"):
            paragraph = etree.Element(qn("w:p"))
            properties = etree.SubElement(paragraph, qn("w:pPr"))
            numbering = etree.SubElement(properties, qn("w:numPr"))
            etree.SubElement(numbering, qn("w:ilvl")).set(qn("w:val"), str(min(depth, 8)))
            etree.SubElement(numbering, qn("w:numId")).set(qn("w:val"), str(number_id))
            _apply_paragraph_alignment(paragraph, item)
            _apply_paragraph_spacing(paragraph, item)
            _append_html_runs(paragraph, item, False, False, False, links)
            _apply_page_break(paragraph, item)
            paragraphs.append(paragraph)
            for child in item:
                if _html_tag(child) in {"ul", "ol"}:
                    paragraphs.extend(_block_paragraphs(child, links, depth + 1))
        if depth > 0:
            return paragraphs  # the spacing paragraph follows the top-level list only
        links.current_list = None
        return _with_block_spacing(paragraphs, tag, links)
    if tag in {"address", "article", "blockquote", "div"} and any(_html_tag(child) in _BLOCK_TAGS for child in node):
        return _nested_block_paragraphs(node, links, depth)
    return _with_block_spacing([_paragraph_from_html(node, links)], tag, links)


def _with_block_spacing(nodes: list[etree._Element], tag: str, links: _HtmlLinks) -> list[etree._Element]:
    # lib/html.js adds an empty paragraph after <p>, lists, tables and headings (checked against JS).
    if not links.no_space and tag in {"p", "ul", "ol", "table", "h1", "h2", "h3", "h4", "h5", "h6"}:
        nodes.append(etree.Element(qn("w:p")))
    return nodes


def _nested_block_paragraphs(node: etree._Element, links: _HtmlLinks, depth: int) -> list[etree._Element]:
    paragraphs: list[etree._Element] = []
    fragment = etree.Element(node.tag, attrib=node.attrib)
    fragment.text = node.text
    for child in node:
        if _html_tag(child) in _BLOCK_TAGS:
            if (fragment.text and fragment.text.strip()) or len(fragment):
                paragraphs.append(_paragraph_from_html(fragment, links))
            nested = deepcopy(child)
            inherited = _inherited_html_style(node.get("style", ""))
            if _html_tag(node) == "th":
                inherited = f"font-weight:bold;{inherited}"
            if inherited:
                nested.set("style", f"{inherited};{nested.get('style', '')}")
            paragraphs.extend(_block_paragraphs(nested, links, depth))
            fragment = etree.Element(node.tag, attrib=node.attrib)
            fragment.text = child.tail
        else:
            fragment.append(deepcopy(child))
    if (fragment.text and fragment.text.strip()) or len(fragment):
        paragraphs.append(_paragraph_from_html(fragment, links))
    return paragraphs


def _inherited_html_style(style: str) -> str:
    inheritable = {"color", "font-family", "font-size", "font-style", "font-weight", "text-align", "text-decoration"}
    return ";".join(
        declaration
        for declaration in style.split(";")
        if declaration.partition(":")[1] and declaration.partition(":")[0].strip().lower() in inheritable
    )


def _table_from_html(node: etree._Element, links: _HtmlLinks) -> etree._Element:
    """Build a minimal valid Word table from HTML ``tr``/``th``/``td`` nodes."""
    links.has_table = links.has_table or not links.table_theme  # a theme names an existing style
    rows = node.xpath("./tr | ./thead/tr | ./tbody/tr | ./tfoot/tr")
    layout = _table_layout(rows)
    column_count = max((start + span for row in layout for _, start, span, _ in row), default=1)
    table = etree.Element(qn("w:tbl"))
    properties = etree.SubElement(table, qn("w:tblPr"))
    etree.SubElement(properties, qn("w:tblStyle")).set(qn("w:val"), links.table_theme or "TableGrid")
    width = etree.SubElement(properties, qn("w:tblW"))
    width.set(qn("w:w"), "5000")
    width.set(qn("w:type"), "pct")
    etree.SubElement(properties, qn("w:tblLayout")).set(qn("w:type"), "fixed")
    look = etree.SubElement(properties, qn("w:tblLook"))
    look.set(qn("w:val"), "04A0")
    look.set(qn("w:firstRow"), "1")
    look.set(qn("w:lastRow"), "0")
    look.set(qn("w:firstColumn"), "1")
    look.set(qn("w:lastColumn"), "0")
    look.set(qn("w:noHBand"), "0")
    look.set(qn("w:noVBand"), "1")
    grid = etree.SubElement(table, qn("w:tblGrid"))
    for _ in range(column_count):
        etree.SubElement(grid, qn("w:gridCol")).set(qn("w:w"), str(5000 // column_count))
    table_fill = _cell_background_color(node)
    for source_row, cells in zip(rows, layout, strict=True):
        row = etree.SubElement(table, qn("w:tr"))
        row_fill = _cell_background_color(source_row) or table_fill
        for source_cell, _start, span, merge in cells:
            cell = etree.SubElement(row, qn("w:tc"))
            cell_properties = etree.SubElement(cell, qn("w:tcPr"))
            if merge != "continue":
                cell_width = etree.SubElement(cell_properties, qn("w:tcW"))  # lib/html.js: every new cell
                cell_width.set(qn("w:w"), "0")
                cell_width.set(qn("w:type"), "auto")
            if span > 1 or merge:
                if span > 1:
                    etree.SubElement(cell_properties, qn("w:gridSpan")).set(qn("w:val"), str(span))
                if merge:
                    etree.SubElement(cell_properties, qn("w:vMerge")).set(qn("w:val"), merge)
            _apply_cell_fill(
                cell, (_cell_background_color(source_cell) if source_cell is not None else None) or row_fill
            )
            if source_cell is None:
                cell.append(_paragraph(""))
                continue
            blocks = (
                _nested_block_paragraphs(source_cell, links, 0)
                if any(_html_tag(child) in _BLOCK_TAGS for child in source_cell)
                else [_paragraph_from_html(source_cell, links)]
            )
            for block in blocks or [_paragraph("")]:
                cell.append(block)
            if cell[-1].tag == qn("w:tbl"):
                cell.append(_paragraph(""))
    return table


def _cell_background_color(node: etree._Element) -> str | None:
    color = _css_color(node.get("bgcolor", ""))
    for declaration in node.get("style", "").split(";"):
        name, separator, value = declaration.partition(":")
        if separator and name.strip().lower() == "background-color":
            color = _css_color(value.strip()) or color
    return color


def _apply_cell_fill(cell: etree._Element, fill: str | None) -> None:
    if fill is None:
        return
    properties = cell.find(qn("w:tcPr"))
    if properties is None:
        properties = etree.Element(qn("w:tcPr"))
        cell.insert(0, properties)
    shade = etree.SubElement(properties, qn("w:shd"))
    shade.set(qn("w:val"), "clear")
    shade.set(qn("w:color"), "auto")
    shade.set(qn("w:fill"), fill)


def _table_layout(rows: list[etree._Element]) -> list[list[tuple[etree._Element | None, int, int, str | None]]]:
    """Place HTML cells on a rectangular grid and materialize vertical continuations."""
    layout: list[list[tuple[etree._Element | None, int, int, str | None]]] = []
    active: dict[int, tuple[int, int]] = {}
    for source_row in rows:
        cells: list[tuple[etree._Element | None, int, int, str | None]] = []
        occupied: set[int] = set()
        next_active: dict[int, tuple[int, int]] = {}
        for start, (span, remaining) in active.items():
            cells.append((None, start, span, "continue"))
            occupied.update(range(start, start + span))
            if remaining > 1:
                next_active[start] = (span, remaining - 1)
        position = 0
        for source_cell in source_row.xpath("./th | ./td"):
            span = _positive_span(source_cell.get("colspan"))
            rowspan = _positive_span(source_cell.get("rowspan"))
            while any(column in occupied for column in range(position, position + span)):
                position += 1
            cells.append((source_cell, position, span, "restart" if rowspan > 1 else None))
            occupied.update(range(position, position + span))
            if rowspan > 1:
                next_active[position] = (span, rowspan - 1)
            position += span
        cells.sort(key=lambda entry: entry[1])
        layout.append(cells)
        active = next_active
    return layout


def _positive_span(value: str | None) -> int:
    try:
        return max(1, min(1000, int(value))) if value is not None else 1
    except ValueError:
        return 1


def _paragraph(value: str) -> etree._Element:
    paragraph = etree.Element(qn("w:p"))
    run = etree.SubElement(paragraph, qn("w:r"))
    text = etree.SubElement(run, qn("w:t"))
    if value[:1].isspace() or value[-1:].isspace():
        text.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    text.text = value
    return paragraph


def _paragraph_from_html(node: etree._Element, links: _HtmlLinks) -> etree._Element:
    """Map the portable inline HTML emphasis subset to Word runs."""
    paragraph = etree.Element(qn("w:p"))
    tag = _html_tag(node)
    if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
        links.has_headings = links.has_headings or not links.heading_theme
        properties = etree.SubElement(paragraph, qn("w:pPr"))
        etree.SubElement(properties, qn("w:pStyle")).set(qn("w:val"), f"{links.heading_theme or 'Heading'}{tag[1:]}")
    elif tag == "blockquote" and links.quote_theme:
        properties = etree.SubElement(paragraph, qn("w:pPr"))
        etree.SubElement(properties, qn("w:pStyle")).set(qn("w:val"), links.quote_theme)
    _apply_paragraph_alignment(paragraph, node)
    _apply_paragraph_spacing(paragraph, node)
    _append_html_runs(paragraph, node, tag == "th", False, False, links)
    _apply_page_break(paragraph, node)
    return paragraph


def _apply_page_break(paragraph: etree._Element, node: etree._Element) -> None:
    before = after = False
    for declaration in node.get("style", "").split(";"):
        name, separator, value = declaration.partition(":")
        if not separator:
            continue
        property_name, setting = name.strip().lower(), value.strip().lower()
        if property_name in {"break-before", "page-break-before"}:
            before = setting in {"page", "always"}
        elif property_name in {"break-after", "page-break-after"}:
            after = setting in {"page", "always"}
    if before:
        properties = paragraph.find(qn("w:pPr"))
        if properties is None:
            properties = etree.Element(qn("w:pPr"))
            paragraph.insert(0, properties)
        etree.SubElement(properties, qn("w:pageBreakBefore"))
    if after:
        etree.SubElement(etree.SubElement(paragraph, qn("w:r")), qn("w:br")).set(qn("w:type"), "page")


def _apply_paragraph_alignment(paragraph: etree._Element, node: etree._Element) -> None:
    alignment = node.get("align", "").strip().lower()
    for declaration in node.get("style", "").split(";"):
        name, separator, value = declaration.partition(":")
        if separator and name.strip().lower() == "text-align":
            alignment = value.strip().lower()
    word_alignment = {
        "left": "left",
        "start": "left",
        "center": "center",
        "right": "right",
        "end": "right",
        "justify": "both",
    }.get(alignment)
    if word_alignment is None:
        return
    properties = paragraph.find(qn("w:pPr"))
    if properties is None:
        properties = etree.Element(qn("w:pPr"))
        paragraph.insert(0, properties)
    etree.SubElement(properties, qn("w:jc")).set(qn("w:val"), word_alignment)


def _apply_paragraph_spacing(paragraph: etree._Element, node: etree._Element) -> None:
    spacing_values: dict[str, str] = {}
    for declaration in node.get("style", "").split(";"):
        name, separator, value = declaration.partition(":")
        if not separator:
            continue
        property_name = name.strip().lower()
        if property_name == "margin":
            values = value.split()
            if 1 <= len(values) <= 4:
                top = _css_twips(values[0])
                bottom = _css_twips(values[2] if len(values) >= 3 else values[0])
                if top is not None and bottom is not None:
                    spacing_values.update(before=str(top), after=str(bottom))
            continue
        if property_name in {"margin-top", "margin-bottom"}:
            twips = _css_twips(value.strip())
            if twips is not None:
                spacing_values["before" if property_name == "margin-top" else "after"] = str(twips)
        elif property_name == "line-height":
            line_spacing = _css_line_spacing(value.strip())
            if line_spacing is not None:
                spacing_values.update(line=str(line_spacing[0]), lineRule=line_spacing[1])
    if not spacing_values:
        return
    properties = paragraph.find(qn("w:pPr"))
    if properties is None:
        properties = etree.Element(qn("w:pPr"))
        paragraph.insert(0, properties)
    spacing = etree.SubElement(properties, qn("w:spacing"))
    for attribute, value in spacing_values.items():
        spacing.set(qn(f"w:{attribute}"), value)


def _css_twips(value: str) -> int | None:
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(px|pt|in|cm|mm)?", value, re.IGNORECASE)
    if match is None or (match.group(2) is None and float(match.group(1)) != 0):
        return None
    unit = (match.group(2) or "px").lower()
    twips = float(match.group(1)) * {"px": 15.0, "pt": 20.0, "in": 1440.0, "cm": 1440 / 2.54, "mm": 1440 / 25.4}[unit]
    return min(round(twips), 31680)


def _css_line_spacing(value: str) -> tuple[int, str] | None:
    if re.fullmatch(r"\d+(?:\.\d+)?", value):
        return round(float(value) * 240), "auto"
    if re.fullmatch(r"\d+(?:\.\d+)?%", value):
        return round(float(value[:-1]) * 2.4), "auto"
    twips = _css_twips(value)
    return (twips, "exact") if twips is not None else None


def _append_html_runs(
    paragraph: etree._Element,
    node: etree._Element,
    bold: bool,
    italic: bool,
    underline: bool,
    links: _HtmlLinks,
    color: str | None = None,
    fill: str | None = None,
    font_size: int | None = None,
    font_family: str | None = None,
) -> None:
    tag = _html_tag(node)
    if not tag:
        return
    if tag == "br":
        etree.SubElement(etree.SubElement(paragraph, qn("w:r")), qn("w:br"))
        return
    if tag == "tab":
        etree.SubElement(etree.SubElement(paragraph, qn("w:r")), qn("w:tab"))
        return
    if tag == "img":
        _append_image_run(paragraph, node, links)
        return
    if tag == "input":
        if node.get("type", "").strip().lower() == "checkbox":
            _append_checkbox(paragraph, "checked" in node.attrib)
        return
    bold = bold or tag in {"b", "strong"}
    italic = italic or tag in {"i", "em"}
    underline = underline or tag == "u"
    bold, italic, underline, color, fill, font_size, font_family = _inline_css(
        node.get("style", ""), bold, italic, underline, color, fill, font_size, font_family
    )
    container = paragraph
    if tag == "a":
        target = links.target(node.get("href", ""))
        if target is not None:
            container = etree.SubElement(paragraph, qn("w:hyperlink"))
            container.set(qn("r:id") if target[0] == "id" else qn("w:anchor"), target[1])
    first_run = len(container)
    _append_run(container, node.text or "", bold, italic, underline, color, fill, font_size, font_family)
    for child in node:
        if _html_tag(child) not in {"ul", "ol"}:
            _append_html_runs(container, child, bold, italic, underline, links, color, fill, font_size, font_family)
        _append_run(container, child.tail or "", bold, italic, underline, color, fill, font_size, font_family)
    if tag in {"s", "del"}:
        _strike(container[first_run:])


def _strike(nodes: list[etree._Element]) -> None:
    """``<s>``/``<del>``: ``<w:strike/>`` on every run of the element (lib/html.js xmlStyles)."""
    for run in (node for element in nodes for node in element.iter(qn("w:r"))):
        properties = run.find(qn("w:rPr"))
        if properties is None:
            properties = etree.Element(qn("w:rPr"))
            run.insert(0, properties)
        if properties.find(qn("w:strike")) is None:
            _insert_ordered(properties, etree.Element(qn("w:strike")), _RPR_ORDER)


def _inline_css(
    style: str,
    bold: bool,
    italic: bool,
    underline: bool,
    color: str | None,
    fill: str | None,
    font_size: int | None,
    font_family: str | None,
) -> tuple[bool, bool, bool, str | None, str | None, int | None, str | None]:
    for declaration in style.split(";"):
        name, separator, value = declaration.partition(":")
        if not separator:
            continue
        property_name = name.strip().lower()
        property_value = value.strip().lower()
        if property_name == "font-size":
            font_size = _css_font_size(value.strip(), font_size) or font_size
            continue
        if property_name == "font-family":
            font_family = _css_font_family(value.strip()) or font_family
            continue
        if property_name == "font-weight":
            bold = _css_bold(property_value, bold)
            continue
        if property_name == "font-style":
            if property_value in {"italic", "oblique"}:
                italic = True
            elif property_value == "normal":
                italic = False
            continue
        if property_name == "text-decoration":
            if "underline" in property_value.split():
                underline = True
            elif property_value == "none":
                underline = False
            continue
        parsed = _css_color(value.strip())
        if property_name == "color" and parsed is not None:
            color = parsed
        elif property_name == "background-color" and parsed is not None:
            fill = parsed
    return bold, italic, underline, color, fill, font_size, font_family


def _css_color(value: str) -> str | None:
    try:
        red, green, blue = ImageColor.getrgb(value)[:3]
    except ValueError:
        return None
    return f"{red:02X}{green:02X}{blue:02X}"


def _css_bold(value: str, inherited: bool) -> bool:
    if value in {"bold", "bolder"}:
        return True
    if value in {"normal", "lighter"}:
        return False
    return int(value) >= 600 if re.fullmatch(r"[0-9]+", value) else inherited


def _css_font_family(value: str) -> str | None:
    name = value.split(",", 1)[0].strip().strip("\"'").strip()
    if not name or any(ord(char) < 32 for char in name):
        return None
    return {"serif": "Times New Roman", "sans-serif": "Arial", "monospace": "Courier New"}.get(name.lower(), name)


def _css_font_size(value: str, inherited: int | None) -> int | None:
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(px|pt|em|rem|%)", value, re.IGNORECASE)
    if match is None:
        return None
    amount = float(match.group(1))
    unit = match.group(2).lower()
    base = inherited or 24
    half_points = amount * {"px": 1.5, "pt": 2.0, "em": base, "rem": 24.0, "%": base / 100}[unit]
    return max(1, min(round(half_points), 3276))


def _append_checkbox(container: etree._Element, checked: bool) -> None:
    control = etree.SubElement(container, qn("w:sdt"))
    properties = etree.SubElement(control, qn("w:sdtPr"))
    checkbox = etree.SubElement(properties, qn("w14:checkbox"))
    etree.SubElement(checkbox, qn("w14:checked")).set(qn("w14:val"), "1" if checked else "0")
    for tag, code in (("checkedState", "2612"), ("uncheckedState", "2610")):
        state = etree.SubElement(checkbox, qn(f"w14:{tag}"))
        state.set(qn("w14:val"), code)
        state.set(qn("w14:font"), "Segoe UI Symbol")
    content = etree.SubElement(control, qn("w:sdtContent"))
    run = etree.SubElement(content, qn("w:r"))
    etree.SubElement(run, qn("w:t")).text = "☒" if checked else "☐"


def _append_image_run(container: etree._Element, node: etree._Element, links: _HtmlLinks) -> None:
    source = node.get("src", "").strip()
    if not source:
        return
    declarations: dict[str, str] = {}
    for declaration in node.get("style", "").split(";"):
        name, separator, value = declaration.partition(":")
        if separator:
            declarations[name.strip().lower()] = value.strip()
    width = _image_extent(declarations.get("width"), node.get("width")) or 0
    height = _image_extent(declarations.get("height"), node.get("height")) or 0
    links.image_counter += 1
    identity = 10000 + links.image_counter
    run = etree.SubElement(container, qn("w:r"))
    drawing = etree.SubElement(run, qn("w:drawing"))
    inline = etree.SubElement(drawing, qn("wp:inline"), distT="0", distB="0", distL="0", distR="0")
    etree.SubElement(inline, qn("wp:extent"), cx=str(width), cy=str(height))
    etree.SubElement(inline, qn("wp:effectExtent"), l="0", t="0", r="0", b="0")
    try:
        etree.SubElement(inline, qn("wp:docPr"), id=str(identity), name="", descr=HTML_IMAGE_PREFIX + source)
    except ValueError as error:
        raise MarkerResolutionError("Invalid HTML image source") from error
    frame = etree.SubElement(inline, qn("wp:cNvGraphicFramePr"))
    etree.SubElement(frame, qn("a:graphicFrameLocks"), noChangeAspect="1")
    graphic = etree.SubElement(inline, qn("a:graphic"))
    graphic_data = etree.SubElement(
        graphic, qn("a:graphicData"), uri="http://schemas.openxmlformats.org/drawingml/2006/picture"
    )
    picture = etree.SubElement(graphic_data, qn("pic:pic"))
    nonvisual = etree.SubElement(picture, qn("pic:nvPicPr"))
    etree.SubElement(nonvisual, qn("pic:cNvPr"), id=str(identity), name="", descr="")
    etree.SubElement(nonvisual, qn("pic:cNvPicPr"))
    fill = etree.SubElement(picture, qn("pic:blipFill"))
    etree.SubElement(fill, qn("a:blip")).set(qn("r:embed"), "rIdCarbotekPending")
    etree.SubElement(etree.SubElement(fill, qn("a:stretch")), qn("a:fillRect"))
    properties = etree.SubElement(picture, qn("pic:spPr"))
    transform = etree.SubElement(properties, qn("a:xfrm"))
    etree.SubElement(transform, qn("a:off"), x="0", y="0")
    etree.SubElement(transform, qn("a:ext"), cx=str(width), cy=str(height))
    etree.SubElement(etree.SubElement(properties, qn("a:prstGeom"), prst="rect"), qn("a:avLst"))


def _image_extent(value: str | None, fallback: str | None = None, *, attribute: bool = False) -> int | None:
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(px|pt|in|cm|mm)?\s*", value, re.IGNORECASE) if value else None
    if match is None:
        if fallback is not None:
            return _image_extent(fallback, attribute=True)
        if attribute and value is not None:
            percentage = re.fullmatch(r"\s*(\d+(?:\.\d+)?)%\s*", value)
            if percentage is not None:
                return round(min(max(int(float(percentage.group(1))), 1), 4096) * 9525)
        return None
    unit = (match.group(2) or "px").lower()
    pixels = float(match.group(1)) * {"px": 1.0, "pt": 96 / 72, "in": 96.0, "cm": 96 / 2.54, "mm": 96 / 25.4}[unit]
    return round(min(max(pixels, 1), 4096) * 9525)


def _append_run(
    paragraph: etree._Element,
    value: str,
    bold: bool,
    italic: bool,
    underline: bool,
    color: str | None = None,
    fill: str | None = None,
    font_size: int | None = None,
    font_family: str | None = None,
) -> None:
    for index, line in enumerate(value.replace("\r\n", "\n").replace("\r", "\n").split("\n")):
        if index:
            etree.SubElement(etree.SubElement(paragraph, qn("w:r")), qn("w:br"))
        if not line:
            continue
        run = etree.SubElement(paragraph, qn("w:r"))
        if (
            bold
            or italic
            or underline
            or color
            or fill
            or font_size
            or font_family
            or paragraph.tag == qn("w:hyperlink")
        ):
            # CT_RPr sequence order: rStyle, rFonts, b, bCs, i, iCs, color, sz, szCs, u, shd.
            properties = etree.SubElement(run, qn("w:rPr"))
            if paragraph.tag == qn("w:hyperlink"):
                etree.SubElement(properties, qn("w:rStyle")).set(qn("w:val"), "Hyperlink")
            if font_family is not None:
                _set_font_properties(properties, font_family, None)
            if bold:  # lib/html.js xmlStyles.docx: <w:b/><w:bCs/>
                etree.SubElement(properties, qn("w:b"))
                etree.SubElement(properties, qn("w:bCs"))
            if italic:
                etree.SubElement(properties, qn("w:i"))
                etree.SubElement(properties, qn("w:iCs"))
            if color:
                etree.SubElement(properties, qn("w:color")).set(qn("w:val"), color)
            if font_size is not None:
                _set_font_properties(properties, None, font_size)
            if underline:
                etree.SubElement(properties, qn("w:u")).set(qn("w:val"), "single")
            if fill:
                shade = etree.SubElement(properties, qn("w:shd"))
                shade.set(qn("w:val"), "clear")
                shade.set(qn("w:color"), "auto")
                shade.set(qn("w:fill"), fill)
        text = etree.SubElement(run, qn("w:t"))
        if line[:1].isspace() or line[-1:].isspace():
            text.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
        text.text = line


def _set_font_properties(properties: etree._Element, family: str | None, size: int | None) -> None:
    if family is not None:
        fonts = etree.SubElement(properties, qn("w:rFonts"))
        for attribute in ("ascii", "hAnsi", "cs"):
            fonts.set(qn(f"w:{attribute}"), family)
    if size is not None:
        for tag in ("sz", "szCs"):
            etree.SubElement(properties, qn(f"w:{tag}")).set(qn("w:val"), str(size))


def _ancestor(node: etree._Element, tag: str) -> etree._Element | None:
    current = node
    while current is not None:
        if current.tag == tag:
            return current
        current = current.getparent()
    return None
