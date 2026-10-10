# pyright: strict, reportPrivateUsage=false
"""Extract supported render options declared inside DOCX text nodes."""

from dataclasses import replace

from carbotek.ooxml.template_options import extract_template_options as extract_options
from carbotek.ooxml.xml import parse_xml, serialize_xml
from carbotek.options import RenderOptions
from carbotek.package import TemplatePackage, TemplatePart


def extract_template_options(package: TemplatePackage, options: RenderOptions) -> tuple[TemplatePackage, RenderOptions]:
    package, values = extract_template_option_values(package)
    return package, replace(options, **values)


def extract_template_option_values(package: TemplatePackage) -> tuple[TemplatePackage, dict[str, object]]:
    replacements: dict[str, TemplatePart] = {}
    values: dict[str, object] = {}
    for part in package.parts.values():
        if not part.is_xml:
            continue
        source = serialize_xml(parse_xml(part.content, part.name)).decode("utf-8")
        changed = extract_options(source, values)
        if changed != source:
            replacements[part.name] = TemplatePart(part.name, changed.encode("utf-8"), True)
    supported = {
        key: value
        for key, value in values.items()
        if key in {"lang", "timezone", "use_high_precision_arithmetic", "export_formatted_values_as_text"}
    }
    return (package.replace_parts(replacements) if replacements else package), supported
