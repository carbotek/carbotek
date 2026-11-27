# pyright: strict, reportPrivateUsage=false
"""OPC part names and their declarations in the content-types manifest."""

import posixpath
from collections.abc import Iterable

from lxml import etree  # pyright: ignore[reportAttributeAccessIssue]

from carbotek.ooxml.ns import qn

_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


def part_key(name: str) -> str:
    """Return the ASCII case-insensitive lookup key of an OPC part name."""
    return name.translate(_ASCII_LOWER)


def target_name(source: str, target: str) -> str:
    """Resolve an internal relationship target relative to its source part."""
    return posixpath.normpath(posixpath.join(posixpath.dirname(source), target)).lstrip("/")


def relationship_part_name(source: str) -> str:
    """Return the relationships entry for the source part or package root."""
    directory, filename = posixpath.split(source)
    return posixpath.join(directory, "_rels", f"{filename}.rels")


def remove_overrides(manifest: etree._Element, names: Iterable[str]) -> bool:
    """Remove overrides for the given ZIP member names and report whether the manifest changed."""
    part_names = {f"/{name}".lower() for name in names}
    matches = [node for node in manifest.findall(qn("ct:Override")) if node.get("PartName", "").lower() in part_names]
    for node in matches:
        manifest.remove(node)
    return bool(matches)
