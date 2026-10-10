# pyright: strict, reportPrivateUsage=false
"""DOCX hyperlinks whose URL is a marker, in two phases around the loop expansion.

Word stores the URL in the part's ``.rels`` (with ``{``/``}`` percent-encoded). ``prepare`` moves the
marker from the relationship onto every ``r:id`` that referenced it (``w:hyperlink``,
``a:hlinkClick``) and deletes the relationship, like ``lib/hyperlinks.js`` preprocessing. The
renderer then freezes loop iterators in those attributes, and ``apply`` resolves each marker to a
new ``CarbotekHyperlinkIdN`` relationship (or a ``w:anchor`` for ``#bookmark`` values).
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote

from lxml import etree  # pyright: ignore[reportAttributeAccessIssue]

from carbotek.docx.renderer import resolve_path
from carbotek.docx.session import CURRENT_RENDER, feature_parse, feature_scan
from carbotek.formatters.docx import URL_ON_ERROR, check_url
from carbotek.markers import Marker, scan
from carbotek.ooxml.ns import qn
from carbotek.ooxml.xml import parse_xml, serialize_xml
from carbotek.package import TemplatePackage, TemplatePart

_HYPERLINK = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink"
_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_ANCHOR = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,39}$")
_LINK_TAGS = (qn("w:hyperlink"), qn("a:hlinkClick"))


class _RelationshipIds:
    def __init__(self, root: etree._Element) -> None:
        self.used = {node.get("Id") for node in root}
        self.urls: dict[str, str] = {}

    def for_url(self, url: str) -> str:
        if url not in self.urls:
            number = 0
            while f"CarbotekHyperlinkId{number}" in self.used:
                number += 1
            value = f"CarbotekHyperlinkId{number}"
            self.used.add(value)
            self.urls[url] = value
        return self.urls[url]


def _pairs(package: TemplatePackage) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    for rel_name in package.parts:
        if rel_name.startswith("word/_rels/") and rel_name.endswith(".xml.rels"):
            document_name = rel_name.replace("word/_rels/", "word/").removesuffix(".rels")
            if document_name in package.parts:
                result.append((rel_name, document_name))
    return result


@dataclass(frozen=True, slots=True)
class HyperlinkSubstitution:
    """Resolve links without ever embedding untrusted URLs into document XML."""

    def prepare(self, package: TemplatePackage) -> TemplatePackage:
        """Move marker-valued relationships onto the ``r:id`` attributes that use them."""
        replacements: dict[str, TemplatePart] = {}
        for rel_name, document_name in _pairs(package):
            rel_root = parse_xml(package.parts[rel_name].content)
            markers: dict[str, str] = {}
            for relationship in list(rel_root):
                raw = _decode(relationship.get("Target", ""))
                occurrences = scan(raw)
                if len(occurrences) != 1 or raw != occurrences[0].raw or relationship.get("Id") is None:
                    continue
                markers[relationship.get("Id", "")] = raw
                rel_root.remove(relationship)
            if not markers:
                continue
            document_root = package.parts[document_name].tree()
            for tag in _LINK_TAGS:
                for link in document_root.iter(tag):
                    if (marker := markers.get(link.get(qn("r:id"), ""))) is not None:
                        link.set(qn("r:id"), marker)
            replacements[rel_name] = TemplatePart(rel_name, serialize_xml(rel_root), True)
            replacements[document_name] = TemplatePart.from_tree(document_name, document_root)
        return package.replace_parts(replacements) if replacements else package

    def apply(
        self, package: TemplatePackage, data: Mapping[str, Any] | list[Any], complement: Mapping[str, Any] | None = None
    ) -> TemplatePackage:
        """Resolve the (frozen) markers left in ``r:id`` attributes to real relationships."""
        complement = complement or {}
        replacements: dict[str, TemplatePart] = {}
        for rel_name, document_name in _pairs(package):
            document_part = package.parts[document_name]
            if document_part.has_tree:
                document_root = document_part.tree()
                if not _MARKER_LINK_ID(document_root):
                    continue  # no marker left in an r:id of this part
            elif b':id="{' not in document_part.content:
                continue
            else:
                document_root = document_part.tree()
            links = [
                link for tag in _LINK_TAGS for link in document_root.iter(tag) if feature_scan(link.get(qn("r:id"), ""))
            ]
            if not links:
                continue
            rel_root = parse_xml(package.parts[rel_name].content)
            ids = _RelationshipIds(rel_root)
            for link in links:
                marker = feature_parse(feature_scan(link.get(qn("r:id"), ""))[0].raw)
                session = CURRENT_RENDER.get()
                value = (
                    session.evaluate(marker)
                    if session is not None
                    else _formatted(marker, resolve_path(marker.path, data if marker.root == "d" else complement))
                )
                url, anchor = _url(value), _anchor(value)
                if anchor is not None and link.tag == qn("w:hyperlink"):
                    link.attrib.pop(qn("r:id"), None)
                    link.set(qn("w:anchor"), anchor)
                elif url is None:
                    link.attrib.pop(qn("r:id"), None)
                else:
                    new_id = ids.for_url(url)
                    link.set(qn("r:id"), new_id)
                    if all(node.get("Id") != new_id for node in rel_root):
                        etree.SubElement(
                            rel_root,
                            f"{{{_REL}}}Relationship",
                            Id=new_id,
                            Type=_HYPERLINK,
                            Target=url,
                            TargetMode="External",
                        )
            replacements[rel_name] = TemplatePart(rel_name, serialize_xml(rel_root), True)
            replacements[document_name] = TemplatePart.from_tree(document_name, document_root)
        return package.replace_parts(replacements) if replacements else package


_MARKER_LINK_ID = etree.XPath(
    "boolean(//@r:id[starts-with(., '{') or starts-with(., '[[ct-')])",
    namespaces={"r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"},
)


def _decode(target: str) -> str:
    """``decodeMarker``: Word percent-encodes braces and brackets, sometimes twice (%257B)."""
    return unquote(unquote(target)) if "%25" in target else unquote(target)


def _formatted(marker: Marker, value: Any) -> Any:
    if not marker.formatters:
        return value
    from carbotek.formatters import shared_registry

    registry = shared_registry()
    for call in marker.formatters:
        value = registry.apply(call.name, value, tuple(argument.strip("'\"") for argument in call.arguments))
    return value


def _url(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    url = value
    if url.startswith("#"):
        return None
    # lib/hyperlinks.js validateURL; an invalid URL removes the link instead of pointing to the
    # carbone.io placeholder (JS parity).
    checked = check_url(url)
    return None if checked == URL_ON_ERROR else checked


def _anchor(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    name = value.strip().removeprefix("#")
    return name if value.strip().startswith("#") and _ANCHOR.fullmatch(name) else None
