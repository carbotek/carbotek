# pyright: strict, reportPrivateUsage=false
"""Data-independent marker analysis shared by compilation and standalone rendering."""

from dataclasses import dataclass

from lxml import etree  # pyright: ignore[reportAttributeAccessIssue]

from carbotek.formatters import FormatterRegistry
from carbotek.markers import Marker, parse, scan
from carbotek.markers.syntax import printed
from carbotek.ooxml.ns import qn

from .dialect import Dialect
from .directives import is_directive
from .set_markers import SetTarget, decompose, with_iterators


@dataclass(frozen=True, slots=True)
class SetStatement:
    marker: Marker
    target: SetTarget


@dataclass(frozen=True, slots=True)
class PartPlan:
    markers: tuple[Marker, ...]
    directives: bool
    columns: bool
    blocks: bool
    attribute_markers: bool
    aliases: bool
    mutates_input: bool
    features: frozenset[str]


def analyze_part(root: etree._Element, dialect: Dialect, registry: FormatterRegistry) -> PartPlan:
    markers: list[Marker] = []
    attributes = False
    features: set[str] = set()
    directives = columns = blocks = aliases = mutates_input = False
    for node in root.iter():
        sources = [node.text or ""] if node.tag in dialect.texts else []
        for name, value in node.attrib.items():
            if scan(value):
                sources.append(value)
                attributes = True
                if name == qn("r:id"):
                    features.add("hyperlinks")
                if node.tag == qn("wp:docPr") and name == "descr":
                    features.update({"images", "drawings"})
        if node.tag == qn("wp:docPr"):
            features.add("drawings")
        for source in sources:
            for occurrence in scan(source, include_aliases=True):
                if occurrence.raw.lstrip("{ ").startswith(("#", "bindColor", "bindChart")):
                    aliases |= occurrence.raw.lstrip("{ ").startswith("#")
                    continue
                marker = parse(occurrence.raw)
                markers.append(marker)
                aliases |= marker.root == "$"
                directive = is_directive(occurrence.raw)
                directives |= directive
                for call in marker.formatters:
                    columns |= (
                        directive
                        and call.name in {"drop", "keep"}
                        and bool(call.arguments)
                        and call.arguments[0].strip(" '\"") in {"col", "tc", "colProperties"}
                    )
                    blocks |= call.name in {"showBegin", "showEnd", "hideBegin", "hideEnd"}
                    mutates_input |= call.name == "set" or registry.metadata(call.name).mutates_input
                    if call.name == "html":
                        features.update({"html", "images", "drawings", "hyperlinks"})
                    if call.name in {"color", "colorToDocx", "getAndConvertColorDocx", "shadeColor"}:
                        features.add("color")
    return PartPlan(
        tuple(markers), directives, columns, blocks, attributes, aliases, mutates_input, frozenset(features)
    )


def extract_sets(root: etree._Element, dialect: Dialect) -> tuple[SetStatement, ...]:
    statements: list[SetStatement] = []
    for text in root.iter(*dialect.texts):
        source = text.text or ""
        replacements: list[tuple[int, int]] = []
        for occurrence in scan(source):
            if ":" not in occurrence.raw:
                continue
            marker = parse(occurrence.raw)
            if not marker.formatters or marker.formatters[-1].name != "set":
                continue
            call = marker.formatters[-1]
            expression = ",".join(call.arguments)
            raw = printed(occurrence.raw)
            prior = raw[: raw.rfind(":set(")]
            statement = parse(with_iterators(prior, raw) + "}")
            statements.append(SetStatement(statement, decompose(expression, raw)))
            replacements.append((occurrence.start, occurrence.end))
        for start, end in reversed(replacements):
            source = source[:start] + source[end:]
        text.text = source
    return tuple(statements)
