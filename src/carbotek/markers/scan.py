# pyright: strict, reportPrivateUsage=false
"""Exact Carbotek marker candidate scanner."""

import re
from dataclasses import dataclass
from functools import lru_cache

_CANDIDATE = re.compile(r"\{([^\{]+?)\}")
_CARBOTEK = re.compile(r"^\{?\s*(?:[cdt]\s*[.\[:(])|^\{?\s*[#$]|^\{?\s*bindColor|^\{?\s*bindChart")


@dataclass(frozen=True, slots=True)
class MarkerOccurrence:
    """A candidate marker accepted by the original engine's marker predicate."""

    raw: str
    start: int
    end: int


@lru_cache(maxsize=8192)
def scan(text: str, *, include_aliases: bool = False) -> tuple[MarkerOccurrence, ...]:
    """Find valid marker candidates using JS-compatible non-nesting semantics."""
    result: list[MarkerOccurrence] = []
    for match in _CANDIDATE.finditer(text):
        raw = match.group(0)
        if not is_carbotek_marker(raw, include_aliases=include_aliases):
            continue
        result.append(MarkerOccurrence(raw, match.start(), match.end()))
    return tuple(result)


def is_carbotek_marker(value: str, *, include_aliases: bool = False) -> bool:
    """Apply the legacy predicate while optionally excluding variable aliases."""
    if not include_aliases and re.match(r"^\{?\s*#", value):
        return False
    return _CARBOTEK.search(value) is not None
