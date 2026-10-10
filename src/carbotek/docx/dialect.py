# pyright: strict, reportPrivateUsage=false
"""The XML vocabulary a templated part is written in: WordprocessingML or SpreadsheetML.

The marker engine (fields, loops, directives, string sections) only needs to know which elements
hold template text and what a paragraph and a run look like; the rest is shared. The dialect of the
part being rendered is a context variable set by ``DocxTextRenderer.render_tree``.
"""

from dataclasses import dataclass
from typing import Any, cast

from lxml import etree  # pyright: ignore[reportAttributeAccessIssue] lxml has incomplete stubs.

from carbotek.ooxml.ns import NAMESPACES, qn
from carbotek.render_state import StateSlot

SHEET_NAMESPACE = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


class _AnyElement(tuple[Any, ...]):
    """``texts`` of a dialect where the text of any element can hold markers (SpreadsheetML).

    ``element.iter(*texts)`` visits every element (not comments), ``tag in texts`` holds for them.
    """

    def __contains__(self, tag: object) -> bool:
        return isinstance(tag, str)


@dataclass(frozen=True, slots=True)
class Dialect:
    name: str
    # Elements whose text can hold markers (``iter(*texts)``, ``tag in texts``).
    texts: tuple[Any, ...]
    # The element a new text run gets, its paragraph and run.
    text: str
    paragraph: str
    run: str
    run_properties: str
    namespace: str

    def marker_texts(self, element: etree._Element) -> list[etree._Element]:
        """Text elements of the subtree holding a ``{`` (XPath in C instead of a Python walk)."""
        return cast(list[etree._Element], _MARKER_TEXTS[self.name](element))

    def emptied_texts(self, element: etree._Element, marker: str) -> list[etree._Element]:
        """Text elements of the subtree whose whole text is ``marker``."""
        return cast(list[etree._Element], _EMPTIED_TEXTS[self.name](element, empty=marker))


WORD = Dialect("docx", (qn("w:t"),), qn("w:t"), qn("w:p"), qn("w:r"), qn("w:rPr"), NAMESPACES["w"])
# JS renders a marker in the text of any element of a workbook part: cells (<t>, <v>, <f>), header
# and footer, defined names, conditional formats, data validations, comments.
SHEET = Dialect(
    "xlsx",
    _AnyElement((etree.Element,)),
    f"{{{SHEET_NAMESPACE}}}t",
    f"{{{SHEET_NAMESPACE}}}is",
    f"{{{SHEET_NAMESPACE}}}r",
    f"{{{SHEET_NAMESPACE}}}rPr",
    SHEET_NAMESPACE,
)

_MARKER_TEXTS = {
    "docx": etree.XPath("descendant-or-self::w:t[contains(., '{')]", namespaces={"w": NAMESPACES["w"]}),
    "xlsx": etree.XPath("descendant-or-self::*[text()[contains(., '{')]]"),
}
_EMPTIED_TEXTS = {
    "docx": etree.XPath("descendant::w:t[. = $empty]", namespaces={"w": NAMESPACES["w"]}, smart_strings=False),
    "xlsx": etree.XPath("descendant::*[not(*) and . = $empty]", smart_strings=False),
}

DIALECT: StateSlot[Dialect] = StateSlot("DIALECT", default=WORD)
