# pyright: strict, reportPrivateUsage=false
"""OPC part names and their declarations in the content-types manifest."""

import posixpath
from collections.abc import Collection, Iterable
from string import ascii_lowercase, ascii_uppercase
from urllib.parse import quote

from lxml import etree  # pyright: ignore[reportAttributeAccessIssue]

from carbotek.ooxml.ns import qn

_ASCII_LOWER = str.maketrans(ascii_uppercase, ascii_lowercase)


def part_uri(name: str) -> str:
    return quote(name, safe="/%:!$&'()*+,;=@")


def part_key(name: str) -> str:
    """Compare URI and IRI spellings with OPC's case-insensitive ASCII rules."""
    return part_uri(name).translate(_ASCII_LOWER)


def member_name(names: Collection[str], name: str) -> str:
    """Keep the spelling used in the ZIP package."""
    if name in names:
        return name
    key = part_key(name)
    return next((member for member in names if part_key(member) == key), name)


def relationship_part_name(names: Collection[str], source: str) -> str:
    """Find an existing relationship part without changing its ZIP spelling."""
    directory, filename = posixpath.split(source)
    return member_name(names, posixpath.join(directory, "_rels", f"{filename}.rels"))


def target_name(source: str, target: str) -> str:
    """Resolve an internal relationship target relative to its source part."""
    return posixpath.normpath(posixpath.join(posixpath.dirname(source), target)).lstrip("/")


def remove_overrides(manifest: etree._Element, names: Iterable[str]) -> bool:
    """Remove overrides for the given ZIP member names and report whether the manifest changed."""
    part_names = {f"/{name}".lower() for name in names}
    matches = [node for node in manifest.findall(qn("ct:Override")) if node.get("PartName", "").lower() in part_names]
    for node in matches:
        manifest.remove(node)
    return bool(matches)
