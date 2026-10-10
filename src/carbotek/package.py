# pyright: strict, reportPrivateUsage=false
"""Immutable domain representation of an OPC template package."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from lxml import etree  # pyright: ignore[reportAttributeAccessIssue] lxml has incomplete stubs.

_TEXT = etree.XPath("boolean(//text()[contains(., $n)])")
_TEXT_OR_ATTRIBUTE = etree.XPath("boolean(//text()[contains(., $n)] | //@*[contains(., $n)])")
_TEXT_OR_REAL_ATTRIBUTE = etree.XPath(
    "boolean(//text()[contains(., $n)] | //@*[contains(., $n) and "
    "not(name() = 'ref' and count(.. | $fragments) = count($fragments))])"
)


class TemplatePart:
    """One OPC member: its bytes, or a parsed XML tree owned by the current render.

    A render parses each templated part once and hands the tree from stage to stage
    (:meth:`from_tree` / :meth:`tree`); the bytes are produced only when they are asked for
    (for example when the package is written). Parts built from bytes are never parsed in
    place: :meth:`tree` returns a fresh tree for them, so cached templates stay intact.
    """

    __slots__ = ("_content", "_tree", "is_xml", "name")

    def __init__(self, name: str, content: bytes, is_xml: bool) -> None:
        self.name = name
        self.is_xml = is_xml
        self._content: bytes | None = content
        self._tree: Any = None

    @classmethod
    def from_tree(cls, name: str, root: Any) -> TemplatePart:
        """A part whose content is ``root`` (serialized lazily); the tree belongs to this render."""
        part = cls(name, b"", True)
        part._content = None
        part._tree = root
        return part

    @property
    def content(self) -> bytes:
        if self._content is None:
            from carbotek.ooxml.xml import serialize_xml

            self._content = serialize_xml(self._tree)
        return self._content

    @property
    def has_tree(self) -> bool:
        return self._tree is not None

    def tree(self) -> Any:
        """The owned tree of a tree part (mutations are this render's), else a fresh parse of the bytes."""
        if self._tree is not None:
            self._content = None  # the caller may change the tree: serialize again when needed
            return self._tree
        from carbotek.ooxml.xml import parse_xml

        return parse_xml(self.content, self.name)

    def contains(self, needle: str, *, attributes: bool = True) -> bool:
        """Whether the part can hold ``needle``: bytes search, or a text (and attribute) search in the tree."""
        if self._tree is None:
            return needle.encode("utf-8") in (self._content or b"")
        from carbotek.ooxml.fragments import iter_fragments

        fragments = list(iter_fragments(self._tree))
        encoded_needle = needle.encode("utf-8")
        if any(
            (encoded_needle in fragment.xml if isinstance(fragment.xml, bytes) else needle in fragment.xml)
            or (attributes and needle in fragment.open_tag)
            for _, fragment in fragments
        ):
            return True
        if attributes:
            # Internal references replace CDATA and its wrap attribute; they must not become
            # searchable document content, while the original wrapper remains searchable above.
            if any(needle in placeholder.get("ref", "") for placeholder, _ in fragments):
                return bool(_TEXT_OR_REAL_ATTRIBUTE(self._tree, n=needle, fragments=[node for node, _ in fragments]))
            return bool(_TEXT_OR_ATTRIBUTE(self._tree, n=needle))
        return bool(_TEXT(self._tree, n=needle))

    def __repr__(self) -> str:
        return f"TemplatePart({self.name!r}, tree={self._tree is not None})"


@dataclass(frozen=True, slots=True)
class TemplatePackage:
    """Validated package data, independent of its ZIP-based persistence mechanism."""

    parts: Mapping[str, TemplatePart]
    order: tuple[str, ...]
    main_document: str = "word/document.xml"
    kind: str = "docx"  # "docx" or "xlsx"

    @classmethod
    def create(cls, parts: Mapping[str, TemplatePart], order: tuple[str, ...], kind: str = "docx") -> TemplatePackage:
        """Freeze mappings at the infrastructure-to-domain boundary."""
        return cls(parts=MappingProxyType(dict(parts)), order=order, kind=kind)

    def replace_part(self, part: TemplatePart) -> TemplatePackage:
        """Create a package with one changed part while preserving domain immutability."""
        parts = dict(self.parts)
        parts[part.name] = part
        return type(self).create(parts, self.order, self.kind)

    def replace_parts(self, replacements: Mapping[str, TemplatePart]) -> TemplatePackage:
        """Replace and append OPC parts atomically while preserving existing member order."""
        parts = dict(self.parts)
        parts |= replacements
        order = (*self.order, *(name for name in replacements if name not in self.parts))
        return type(self).create(parts, order, self.kind)

    def without_parts(self, names: Iterable[str]) -> TemplatePackage:
        """Drop parts from the package (and from the member order)."""
        removed = set(names)
        parts = {name: part for name, part in self.parts.items() if name not in removed}
        return type(self).create(parts, tuple(name for name in self.order if name not in removed), self.kind)
