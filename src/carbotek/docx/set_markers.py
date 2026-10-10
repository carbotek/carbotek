# pyright: strict, reportPrivateUsage=false
"""Compile-time handling of ``:set`` markers (lib/preprocessor.js ``preParseSetFormatters``).

JS removes every ``{…:set(expression)}`` marker from the template, rewrites ``expression`` into a
chain of internal ``_set*`` formatters (``decomposeSetFormatter``) and executes the markers before
the report is built. A marker over an array (``d.list[]``, ``d.list[v>1]``) runs once per item.
"""

import re
from dataclasses import dataclass

from carbotek.errors import MarkerResolutionError

SET_MARKER = re.compile(r"(\{[^{}]+?):set\(([^)]*)\)\}")
_ITERATOR_FILTER = re.compile(r"[\[,][^=><!]+[,\]]")


@dataclass(frozen=True, slots=True)
class SetCall:
    """One internal formatter of the decomposed chain; ``key`` is a literal, ``value`` a path or literal."""

    name: str
    key: str | None = None
    value: str | None = None


@dataclass(frozen=True, slots=True)
class SetTarget:
    """Decomposed target expression: root (``d``, ``c`` or relative ``.``) and the call chain."""

    root: str
    calls: tuple[SetCall, ...]


def decompose(expression: str, full_marker: str) -> SetTarget:
    """Port of ``decomposeSetFormatter`` with its three ``Forbidden … in "[…]"`` errors."""
    cleaned = re.sub(r"\s", "", expression)
    root_match = re.match(r"^([dc])?(\.+)", cleaned)
    root = root_match.group(1) if root_match and root_match.group(1) else "."
    rest = cleaned[root_match.end() :] if root_match else cleaned
    segments = re.split(r"[\[\]]", rest)
    calls: list[SetCall] = []
    last: SetCall | None = None
    for index, segment in enumerate(segments):
        if index % 2 == 0:
            for attribute in segment.split("."):
                if attribute:
                    last = SetCall("_setObj", attribute)
                    calls.append(last)
            continue
        if last is not None and calls and calls[-1] is last:
            calls[-1] = last = SetCall("_setArr", last.key)
        operands = segment.split("=")
        if len(operands) == 2:
            left, right = operands
            if "." in left or not left:
                raise MarkerResolutionError(f'Forbidden left operand in "[{segment}]" of "{full_marker}"')
            if re.search(r"[\[\]]", right) or not right:
                raise MarkerResolutionError(f'Forbidden right operand in "[{segment}]" of "{full_marker}"')
            last = SetCall("_setObjInArr", left, right)
        elif index < len(segments) - 2 or segment != "":
            raise MarkerResolutionError(f'Forbidden join condition in "[{segment}]" of "{full_marker}"')
        else:
            last = SetCall("_setObjInArr")
        calls.append(last)
    if calls:
        final = calls[-1]
        calls[-1] = SetCall("_setValInArr" if final.name == "_setObjInArr" else "_setVal", final.key, final.value)
    return SetTarget(root, tuple(calls))


def with_iterators(marker: str, full_marker: str) -> str:
    """Port of ``addIteratorForSetFormatter``: ``[]`` → ``[i]``, ``[filter]`` → ``[i, filter]``."""
    colon = marker.find(":")
    limit = len(marker) if colon == -1 else colon

    def rewrite(match: re.Match[str]) -> str:
        text = match.group(0)
        if match.start() >= limit or re.search(r"\[\d+\]", text):
            return text
        if re.fullmatch(r"\[\s*\]", text):
            return "[i]"
        decoded = text.replace("&lt;", "<").replace("&gt;", ">").replace("&apos;", "'")
        if _ITERATOR_FILTER.search(decoded):
            without_quotes = re.sub(r"'[^']*?'", lambda quoted: quoted.group(0).replace(",", "￿"), decoded)
            if _ITERATOR_FILTER.search(without_quotes):
                raise MarkerResolutionError(
                    f'Using an iterator in "[]" is forbidden when using ":set" formatter in "{full_marker}"'
                )
        return "[i, " + text[1:]

    return re.sub(r"\[[^\]]*\]", rewrite, marker)
