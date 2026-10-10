# pyright: strict, reportPrivateUsage=false
"""Render-owned XML fragments that bypass a redundant trip through libxml2 CDATA."""

import re
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from uuid import uuid4

from lxml import etree  # pyright: ignore[reportAttributeAccessIssue] lxml has incomplete stubs.

from carbotek.errors import CorruptedTemplateError
from carbotek.render_state import StateSlot

RENDERED_TAG = "carbotek-rendered"
EMPTY_TEXT = "<!--ct-empty-->"  # XML structure, never a character from caller data
_EMPTY_BYTES = EMPTY_TEXT.encode("utf-8")
_EMPTY = EMPTY_TEXT
_REFERENCE = re.compile(r"cf-[0-9a-f]{32}-[0-9]+\Z")
_PLACEHOLDER = re.compile(
    rb'<!\[CDATA\[.*?\]\]>|<!--.*?-->|<\?.*?\?>|<carbotek-rendered ref="(cf-[0-9a-f]{32}-[0-9]+)"\s*/>',
    re.DOTALL,
)


@dataclass(slots=True)
class RenderedFragment:
    """The original XML remains available if a later pass needs to materialize it."""

    xml: str | bytes
    open_tag: str
    close_tag: str
    _encoded: bytes | None = field(default=None, init=False, repr=False)

    def encoded(self) -> bytes:
        if self._encoded is None:
            self._encoded = (
                self.xml.replace(_EMPTY.encode("utf-8"), b"")
                if isinstance(self.xml, bytes)
                else self.xml.replace(_EMPTY, "").encode("utf-8")
            )
        return self._encoded

    def text(self) -> str:
        return self.xml.decode("utf-8") if isinstance(self.xml, bytes) else self.xml


@dataclass(slots=True)
class _Fragments:
    prefix: str = field(default_factory=lambda: f"cf-{uuid4().hex}-")
    values: dict[str, RenderedFragment] = field(default_factory=dict[str, RenderedFragment])


_FRAGMENTS: StateSlot[_Fragments | None] = StateSlot("RENDERED_FRAGMENTS", default=None)


@contextmanager
def fragment_scope() -> Generator[None]:
    """Own fragments through rendering, post-processing and final package serialization.

    Standalone tree rendering has no scope and continues to use self-contained CDATA.
    A fresh registry for nested renders prevents references from colliding or escaping.
    """
    token = _FRAGMENTS.set(_Fragments())
    try:
        yield
    finally:
        _FRAGMENTS.reset(token)


def register_fragment(xml: str | bytes, open_tag: str, close_tag: str) -> str | None:
    """Return an opaque reference, or None when the caller needs self-contained CDATA."""
    registry = _FRAGMENTS.get()
    if registry is None:
        return None
    reference = f"{registry.prefix}{len(registry.values)}"
    registry.values[reference] = RenderedFragment(xml, open_tag, close_tag)
    return reference


def _lookup(reference: str) -> RenderedFragment:
    registry = _FRAGMENTS.get()
    fragment = registry.values.get(reference) if registry is not None else None
    if fragment is None:
        raise CorruptedTemplateError("Rendered XML fragment is unavailable outside its owning render")
    return fragment


def get_fragment(placeholder: etree._Element) -> RenderedFragment | None:
    """Resolve our placeholders; legacy CDATA and ordinary literal tags have no sidecar."""
    reference = placeholder.get("ref", "")
    if placeholder.tag != RENDERED_TAG or _REFERENCE.fullmatch(reference) is None:
        return None
    return _lookup(reference)


def iter_fragments(root: etree._Element) -> Iterator[tuple[etree._Element, RenderedFragment]]:
    for placeholder in root.iter(RENDERED_TAG):
        fragment = get_fragment(placeholder)
        if fragment is not None:
            yield placeholder, fragment


def splice_fragments(serialized: bytes) -> bytes:
    """Substitute generated references once, without interpreting markup inside a fragment."""
    if b'<carbotek-rendered ref="cf-' not in serialized:
        return serialized

    def replace(match: re.Match[bytes]) -> bytes:
        reference = match.group(1)
        # XML comments, CDATA and processing instructions can contain literal placeholder text.
        return _lookup(reference.decode("ascii")).encoded() if reference is not None else match.group()

    return _PLACEHOLDER.sub(replace, serialized)


_RENDERED_START = b'<carbotek-rendered wrap="'
_CDATA_START = b"<![CDATA["
_RENDERED_END = b"]]></carbotek-rendered>"


def splice_rendered(serialized: bytes) -> bytes:
    """Replace references and standalone CDATA placeholders with the rendered XML.

    Standalone copies are CDATA (serialized verbatim, no escaping to undo); they never contain
    ``]]>`` since text and attribute values escape ``>``.
    """
    serialized = splice_fragments(serialized)
    pieces: list[bytes] = []
    position = 0
    while (start := serialized.find(_RENDERED_START, position)) != -1:
        content = serialized.index(_CDATA_START, start) + len(_CDATA_START)
        end = serialized.index(_RENDERED_END, content)
        pieces.append(serialized[position:start])
        chunk = serialized[content:end]
        pieces.append(chunk.replace(_EMPTY_BYTES, b"") if _EMPTY_BYTES in chunk else chunk)
        position = end + len(_RENDERED_END)
    if not pieces:
        return serialized
    pieces.append(serialized[position:])
    return b"".join(pieces)
