# pyright: strict, reportPrivateUsage=false
"""Paragraph text projection with reversible offsets into OOXML text nodes."""

from dataclasses import dataclass

from lxml import etree  # pyright: ignore[reportAttributeAccessIssue] lxml has incomplete stubs.

from .dialect import DIALECT


@dataclass(frozen=True, slots=True)
class TextSpan:
    """A half-open offset interval belonging to one ``w:t`` element."""

    start: int
    end: int
    element: etree._Element


@dataclass(frozen=True, slots=True)
class ParagraphText:
    """Textual view of a paragraph plus a precise source-node map."""

    value: str
    spans: tuple[TextSpan, ...]

    @classmethod
    def from_paragraph(cls, paragraph: etree._Element) -> ParagraphText:
        """Build a view from Word text nodes in document order."""
        offset = 0
        spans: list[TextSpan] = []
        fragments: list[str] = []
        dialect = DIALECT.get()
        for element in paragraph.iter(*dialect.texts):
            nearest = next(element.iterancestors(dialect.paragraph), None)
            if nearest is not None and nearest is not paragraph:
                continue  # text boxes have their own paragraphs and independent offsets
            fragment = element.text or ""
            fragments.append(fragment)
            spans.append(TextSpan(offset, offset + len(fragment), element))
            offset += len(fragment)
        return cls("".join(fragments), tuple(spans))

    def locate(self, offset: int, *, prefer_next: bool = False) -> tuple[etree._Element, int]:
        """Return the text node and its local offset for a global paragraph offset.

        At a boundary between two nodes the earlier node is returned (an end offset), or the
        later non-empty one with ``prefer_next`` (a start offset).
        """
        if not 0 <= offset <= len(self.value):
            raise ValueError("Text offset is outside the paragraph")
        if prefer_next:
            for span in self.spans:
                if span.start <= offset < span.end:
                    return span.element, offset - span.start
        for span in self.spans:
            if span.start <= offset <= span.end:
                return span.element, offset - span.start
        if self.spans and offset == len(self.value):
            last = self.spans[-1]
            return last.element, last.end - last.start
        raise ValueError("Paragraph has no text node at the requested offset")
