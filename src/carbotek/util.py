# pyright: strict, reportPrivateUsage=false
"""Small string helpers."""

from collections.abc import Container, Iterable


def distance(first: str, second: str) -> float:
    """Port of ``helper.distance``: the approximate string distance used for suggestions.

    It is not Levenshtein: common characters are counted with a look-ahead of five characters
    (https://github.com/awnist/distance), and the result is ``(len1 + len2) / 2 - common``.
    """
    if not first:
        return len(second)
    if not second:
        return len(first)
    cursor = common = offset1 = offset2 = 0
    max_offset = 5
    while cursor + offset1 < len(first) and cursor + offset2 < len(second):
        if first[cursor + offset1] == second[cursor + offset2]:
            common += 1
        else:
            offset1 = offset2 = 0
            for index in range(max_offset):
                if cursor + index < len(first) and first[cursor + index] == second[cursor]:
                    offset1 = index
                    break
                if cursor + index < len(second) and first[cursor] == second[cursor + index]:
                    offset2 = index
                    break
        cursor += 1
    return (len(first) + len(second)) / 2 - common


def find_closest(value: str, choices: Iterable[str]) -> str:
    """Port of ``helper.findClosest``: the first choice with the smallest distance, or ``""``."""
    candidates = list(choices)
    if not candidates or not value:
        return ""
    best, best_distance = 0, float(len(value))
    for index, candidate in enumerate(candidates):
        current = distance(value, candidate)
        if current < best_distance:
            best, best_distance = index, current
    return candidates[best]


def locale_key(language: str, available: Container[str]) -> str:
    """Prefer an exact locale, then its language, then English."""
    normalized = language.lower().replace("_", "-")
    base = normalized.split("-", 1)[0]
    return normalized if normalized in available else base if base in available else "en"
