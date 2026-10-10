# pyright: strict, reportPrivateUsage=false
"""Syntax tree for Carbotek markers, parsed without runtime code generation."""

import re
from dataclasses import dataclass
from functools import lru_cache
from html import unescape

from carbotek.errors import MarkerError


class MarkerSyntaxError(MarkerError, ValueError):
    """Raised when a recognized marker cannot be structurally parsed.

    It keeps ``ValueError`` as a base because adapters that treat unparsable text as plain text
    catch that class.
    """


@dataclass(frozen=True, slots=True)
class FormatterCall:
    """One formatter in a left-to-right formatter pipeline."""

    name: str
    arguments: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Marker:
    """Parsed marker source path and formatter pipeline."""

    root: str
    path: tuple[str, ...]
    formatters: tuple[FormatterCall, ...]
    raw: str


_BLOCK_END = re.compile(r":(?:showEnd|hideEnd)")
_BLOCK_BEGIN = re.compile(r":(?:showBegin|hideBegin)")


def is_conditional_block_end_marker(marker: str) -> bool:
    """Port of ``condition._isConditionalBlockEndMarker``: a marker closing a show/hide block."""
    return _BLOCK_END.search(re.sub(r"\s", "", marker)) is not None


def is_conditional_block_begin_marker(marker: str) -> bool:
    """Port of ``condition._isConditionalBlockBeginMarker``: a marker opening a show/hide block."""
    return _BLOCK_BEGIN.search(re.sub(r"\s", "", marker)) is not None


def printed(raw: str) -> str:
    """The marker as JS prints it in error messages (``helper.printMarker``): cleaned, in braces."""
    body = raw[1:-1] if raw.startswith("{") and raw.endswith("}") else raw
    return "{" + _remove_unquoted_whitespace(unescape(body).replace("\n", "").replace("\t", "")) + "}"


def source_with_formatters(marker: Marker, formatters: tuple[FormatterCall, ...]) -> str:
    """Keep the original path syntax, including quoted filter values, when rewriting calls."""
    path = _split_marker(printed(marker.raw)[1:-1])[0]
    calls = "".join(
        f":{call.name}({','.join(call.arguments)})" if call.arguments else f":{call.name}" for call in formatters
    )
    return "{" + path + calls + "}"


@lru_cache(maxsize=8192)
def parse(raw: str) -> Marker:
    """Parse a marker while preserving brackets, arguments, and quoted text."""
    if not raw.startswith("{") or not raw.endswith("}"):
        raise MarkerSyntaxError("Marker must be enclosed in braces")
    cleaned = _remove_unquoted_whitespace(unescape(raw[1:-1]).replace("\n", "").replace("\t", ""))
    segments = _split_marker(cleaned)
    if not segments or not segments[0]:
        raise MarkerSyntaxError("Marker has no path")
    root, path = _parse_path(segments[0])
    formatters = _count_as_cum_count(tuple(_parse_formatter(segment) for segment in segments[1:]))
    return Marker(root=root, path=path, formatters=formatters, raw=raw)


def _split_marker(value: str) -> list[str]:
    """Separate the strict source path from JS's tolerant formatter scanner."""
    depth = 0
    quote: str | None = None
    for index, character in enumerate(value):
        if quote is not None:
            if character == quote:
                quote = None
        elif character in {"'", '"'}:
            quote = character
        elif character in "([":
            depth += 1
        elif character in ")]":
            depth -= 1
        elif character == ":" and depth == 0:
            path = value[:index]
            _split_top_level(path, ":")
            return [path, *_split_formatter_calls(value[index + 1 :])]
    return _split_top_level(value, ":")


def _split_formatter_calls(value: str) -> list[str]:
    """A quote toggles string mode; an unclosed call consumes the remaining pipeline."""
    result: list[str] = []
    start = 0
    in_string = False
    in_parenthesis = False
    for index, character in enumerate(value):
        if character in {"'", '"'}:
            in_string = not in_string
        elif character == "(":
            in_parenthesis = True
        elif character == ")" and not in_string:
            in_parenthesis = False
        elif character == ":" and not in_parenthesis and not in_string:
            result.append(value[start:index])
            start = index + 1
    result.append(value[start:])
    return result


def _formatter_arguments(payload: str) -> tuple[str, ...]:
    """Only complete single-quote pairs protect commas in JS parseFormatter."""
    protected = re.sub(r"'[^']*?'", lambda match: match[0].replace(",", "\uffff"), payload)
    result: list[str] = []
    for argument in re.split(r" *, *", protected):
        argument = re.sub(r"'? *$", "", argument.lstrip(" ")).replace("\uffff", ",")
        # Canonical quotes preserve the renderer's distinction between literals and paths.
        if argument.startswith("'"):
            argument += "'"
        result.append(argument)
    return tuple(result)


def _remove_unquoted_whitespace(value: str) -> str:
    """Preserve only complete quoted spans, matching parser.removeWhitespace."""
    return re.sub(
        r"""([^"^']+)|(["|'](?:[^"'\\]|\\.)*["|'])""",
        lambda match: re.sub(r"\s", "", match[1]) if match[1] is not None else match[0],
        value,
    )


def _split_top_level(value: str, separator: str) -> list[str]:
    result: list[str] = []
    start = 0
    depth = 0
    quote: str | None = None
    escaped = False
    for index, character in enumerate(value):
        if quote is not None:
            if character == quote and not escaped:
                quote = None
            escaped = character == "\\" and not escaped
            continue
        if character in {"'", '"'}:
            quote = character
        elif character in "([":
            depth += 1
        elif character in ")]":
            depth -= 1
            if depth < 0:
                raise MarkerSyntaxError("Unexpected closing delimiter")
        elif character == separator and depth == 0:
            result.append(value[start:index])
            start = index + 1
    if quote is not None or depth != 0:
        raise MarkerSyntaxError("Unbalanced marker delimiters")
    result.append(value[start:])
    return result


def _parse_path(value: str) -> tuple[str, tuple[str, ...]]:
    if value.startswith("t("):
        if not value.endswith(")"):
            raise MarkerSyntaxError("Translation marker has an unclosed argument")
        return "t", (_unquote(value[2:-1]),)
    for root in ("d", "c"):
        if value == root:
            return root, ()
        if value.startswith((f"{root}.", f"{root}[")):
            return root, tuple(_split_path(value[1:]))
    if value.startswith("$"):
        variable = value[1:]
        match = re.fullmatch(r"([A-Za-z_]\w*)(?:\((.*)\))?", variable)
        if match is None:
            raise MarkerSyntaxError(f"Malformed template variable: {value!r}")
        argument_text = match.group(2)
        arguments = () if argument_text is None or not argument_text else tuple(_split_top_level(argument_text, ","))
        return "$", (match.group(1), *arguments)
    raise MarkerSyntaxError(f"Unsupported marker root: {value!r}")


def _split_path(value: str) -> list[str]:
    result: list[str] = []
    token = ""
    depth = 0
    index = 0
    while index < len(value):
        character = value[index]
        if character == "[":
            depth += 1
        elif character == "]":
            depth -= 1
        if character == "." and depth == 0:
            if token:
                result.append(token)
            token = ""
            end = index + 1
            while end < len(value) and value[end] == ".":
                end += 1
            result.extend(".." for _ in range(end - index - 1))
            index = end
            continue
        token += character
        index += 1
    if depth != 0:
        raise MarkerSyntaxError("Unbalanced path brackets")
    if token:
        result.append(token)
    return result


def _parse_formatter(value: str) -> FormatterCall:
    if not value:
        raise MarkerSyntaxError("Formatter name is empty")
    if "(" not in value:
        return FormatterCall(value, ())
    name, arguments = value.split("(", maxsplit=1)
    if not name:
        raise MarkerSyntaxError(f"Malformed formatter: {value!r}")
    closing = arguments.rfind(")")
    payload = (arguments if closing == -1 else arguments[:closing]).strip()
    return FormatterCall(name, _formatter_arguments(payload) if payload else ())


def _count_as_cum_count(formatters: tuple[FormatterCall, ...]) -> tuple[FormatterCall, ...]:
    """parser.js: ``:count`` is ``:cumCount`` and ``:count(n)`` is ``:cumCount:add(n-1)``."""
    result: list[FormatterCall] = []
    for formatter in formatters:
        if formatter.name != "count":
            result.append(formatter)
            continue
        result.append(FormatterCall("cumCount", ()))
        start = formatter.arguments[0].strip() if formatter.arguments else ""
        if re.fullmatch(r"\d+", start):
            result.append(FormatterCall("add", (str(int(start) - 1),)))
    return tuple(result)


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        quote = value[0]
        return value[1:-1].replace(f"\\{quote}", quote).replace("\\\\", "\\")
    return value
