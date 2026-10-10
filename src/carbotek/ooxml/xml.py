# pyright: strict, reportPrivateUsage=false
"""The only way the engine parses and serializes OOXML.

Entity resolution and network access are disabled, and a document type declaration is rejected
outright: WordprocessingML, SpreadsheetML and OPC parts never carry one, so a DTD in a template is
either an attack (XXE, entity expansion) or a corrupted part.
"""

import re

from lxml import etree  # pyright: ignore[reportAttributeAccessIssue] lxml has incomplete stubs.

from carbotek.errors import CorruptedTemplateError


def parse_xml(content: bytes | str, name: str = "XML part") -> etree._Element:
    """Parse untrusted XML with the safe settings; raise ``CorruptedTemplateError`` on any problem."""
    # A parser per call: lxml parser objects must not be shared between threads.
    parser = etree.XMLParser(
        resolve_entities=False, no_network=True, huge_tree=False, remove_blank_text=False, strip_cdata=False
    )
    payload = content.encode("utf-8") if isinstance(content, str) else content
    try:
        root = etree.fromstring(payload, parser=parser)
    except etree.XMLSyntaxError as error:
        raise CorruptedTemplateError(f"Invalid XML in {name}") from error
    docinfo = root.getroottree().docinfo
    if docinfo.doctype or docinfo.internalDTD is not None or docinfo.externalDTD is not None:
        raise CorruptedTemplateError(f"Document type declarations are not allowed in {name}")
    return root


# Characters XML 1.0 forbids. JS removes the C0 controls when it injects a value
# (lib/builder.js); lone surrogates and U+FFFE/U+FFFF are removed too, since Word rejects them.
_INVALID_XML_CHARACTERS = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff￾￿]")


def sanitize_xml_text(value: str) -> str:
    """Remove characters that cannot appear in an XML 1.0 document (Appendix C)."""
    if value.isascii() and value.isprintable():
        return value  # the common case, checked in C without the regular expression
    return _INVALID_XML_CHARACTERS.sub("", value)


def serialize_xml(root: etree._Element) -> bytes:
    """Serialize a part the way Word expects it: UTF-8 declaration with ``standalone="yes"``."""
    serialized = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
    if b"<carbotek-rendered" in serialized:
        from carbotek.ooxml.fragments import splice_rendered

        return splice_rendered(serialized)
    return serialized
