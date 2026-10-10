# pyright: strict, reportPrivateUsage=false
"""Structural removal of hidden conditional blocks (``showBegin``/``hideBegin`` ... ``End``).

JS (``extracter.findAndSetValidPositionOfConditionalBlocks_v5`` with
``parser.findSafeConditionalBlockPosition_v5``) drops the XML between the two markers, split into
balanced pieces: elements partly inside keep their tags, elements wholly inside go. The tree
renderer records where hidden blocks start and end during its text pass and removes the elements
afterwards; the string renderer of loop sections cuts the same elements (``strings.py``).
"""

from lxml import etree  # pyright: ignore[reportAttributeAccessIssue] lxml has incomplete stubs.


class HiddenBlocks:
    """Hidden conditional blocks of a pass, removed like ``findSafeConditionalBlockPosition_v5``.

    Everything between the begin and the end marker goes; an element holding the begin marker at
    its very start (and closing before the end marker), or the end marker at its very end (and
    opening after the begin marker), goes whole, climbing while the element is the first (last)
    child of its parent. A block inside one text only loses its text.
    """

    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []
        self._open: tuple[etree._Element, bool] | None = None
        self._ranges: list[tuple[etree._Element, bool, etree._Element, bool]] = []

    def consume(self, node: etree._Element) -> None:
        for kind, text in self.events:
            if kind == "begin":
                if self._open is None:
                    self._open = (node, text == "")
            elif self._open is not None:
                begin, empty_before = self._open
                self._open = None
                if begin is not node:
                    self._ranges.append((begin, empty_before, node, text == ""))
        self.events.clear()

    def remove(self) -> None:
        removals: list[etree._Element] = []
        for begin, empty_before, end, empty_after in self._ranges:
            removals.extend(hidden_range(begin, empty_before, end, empty_after))
        for element in {id(item): item for item in removals}.values():
            parent = element.getparent()
            if parent is not None and not any(ancestor in removals for ancestor in element.iterancestors()):
                parent.remove(element)
        self._ranges.clear()


def hidden_range(
    begin: etree._Element, empty_before: bool, end: etree._Element, empty_after: bool
) -> list[etree._Element]:
    """Elements strictly inside a hidden block (see ``HiddenBlocks``)."""
    begin_path = [begin, *begin.iterancestors()]
    end_path = [end, *end.iterancestors()]
    end_set = set(end_path)
    common = next((node for node in begin_path if node in end_set), None)
    if common is None:
        return []
    begin_path = begin_path[: begin_path.index(common)]
    end_path = end_path[: end_path.index(common)]
    if not begin_path or not end_path:
        return []  # one holds the other: no element lies between
    removals: list[etree._Element] = []
    siblings = list(common)
    first, last = siblings.index(begin_path[-1]), siblings.index(end_path[-1])
    removals.extend(siblings[first + 1 : last])
    for node in begin_path[:-1]:
        removals.extend(node.itersiblings())
    for node in end_path[:-1]:
        removals.extend(node.itersiblings(preceding=True))
    if empty_before:
        removals.append(_climb(begin_path, preceding=True))
    if empty_after:
        removals.append(_climb(end_path, preceding=False))
    return removals


def _climb(path: list[etree._Element], *, preceding: bool) -> etree._Element:
    """The highest element of ``path`` the marker opens (closes) directly, with no content before (after)."""
    top = path[0]
    for node in path[1:]:
        child = top
        if preceding and (node.text or child.getprevious() is not None):
            break
        if not preceding and (child.tail or child.getnext() is not None):
            break
        top = node
    return top
