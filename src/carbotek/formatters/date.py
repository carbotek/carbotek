# pyright: strict, reportPrivateUsage=false
"""Portable date formatters for the common Day.js-compatible token subset."""

import gzip
import json
import math
import re
from calendar import monthrange
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta, tzinfo
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from carbotek.util import locale_key

from .condition import JS_UNDEFINED
from .registry import FormatterMetadata, FormatterRegistry


class _InvalidDate:
    def __str__(self) -> str:
        return "Invalid Date"


INVALID_DATE = _InvalidDate()
_ISO_DATE_ONLY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def register(registry: FormatterRegistry) -> None:
    """Register date operations that have no document-format dependency."""
    registry.register("formatD", format_date, FormatterMetadata(uses_render_options=True))
    registry.register("formatI", format_interval, FormatterMetadata(uses_render_options=True))
    registry.register("convDate", convert_date, FormatterMetadata(uses_render_options=True))
    registry.register("convert", convert_date, FormatterMetadata(uses_render_options=True))
    registry.register("addD", add_date)
    registry.register("subD", sub_date)
    registry.register("startOfD", start_of_date)
    registry.register("endOfD", end_of_date)
    registry.register("diffD", diff_date)


def format_date(
    value: Any, pattern_out: Any, pattern_in: Any | None = None, *, render_options: Any = None
) -> str | None:
    """Format ISO or explicitly patterned values with Carbotek's common tokens."""
    if value is None or value is JS_UNDEFINED:
        return value
    date_only = type(value) is str and pattern_in is None and _ISO_DATE_ONLY.fullmatch(value) is not None
    if date_only:
        # Calendar-only output does not need the host timezone. Validate before inspecting the
        # output pattern: an invalid input still returns "Invalid Date", even for a bad pattern.
        try:
            moment = datetime.fromisoformat(value)
        except ValueError:
            return "Invalid Date"
    else:
        moment = _parse(value, pattern_in)
    if isinstance(moment, _InvalidDate):
        return "Invalid Date"
    if date_only and type(pattern_out) is str and pattern_out == "DD.MM.YYYY":
        return f"{moment.day:02d}.{moment.month:02d}.{moment.year:04d}"
    if not isinstance(value, datetime) and (
        len(str(value)) >= 13
        or (pattern_in is not None and any(token in str(pattern_in) for token in ("H", "h", "m", "s")))
    ):
        moment = moment.astimezone(ZoneInfo(str(getattr(render_options, "timezone", "UTC"))))
    language = locale_key(str(getattr(render_options, "lang", "en") or "en"), _date_locales())
    locale = _date_locales().get(language, _date_locales()["en"])
    pattern, month_form, short_month_form, unbracketed = _pattern_plan(str(pattern_out), language)
    # The weekOfYear/weekYear plugins are not loaded: these tokens throw.
    if "gggg" in unbracketed:
        raise TypeError("t.weekYear is not a function")
    if "w" in unbracketed:
        raise TypeError("t.week is not a function")
    if date_only and ("X" in unbracketed or "x" in unbracketed or "Z" in unbracketed or "z" in unbracketed):
        moment = moment.replace(tzinfo=_local_timezone())
    zones = _zone_names(moment) if "z" in unbracketed else ("", "")
    forms = (locale, month_form, short_month_form, zones)
    return "".join(piece if token is None else token(moment, forms) for piece, token in _pattern_pieces(pattern))


def _offset_text(moment: datetime) -> str:
    offset = moment.utcoffset() or timedelta()
    minutes = int(offset.total_seconds() / 60)
    return f"{'+' if minutes >= 0 else '-'}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d}"


type _Forms = tuple[Mapping[str, Any], str, str, tuple[str, str]]
type _TokenFunction = Callable[[datetime, _Forms], str]

# Day.js format tokens; each is computed only when the pattern uses it.
_TOKENS: dict[str, _TokenFunction] = {
    "YYYY": lambda m, f: f"{m.year:04d}",
    "YY": lambda m, f: f"{m.year % 100:02d}",
    "MMMM": lambda m, f: f[0][f[1]][m.month - 1],
    "MMM": lambda m, f: f[0][f[2]][m.month - 1],
    "MM": lambda m, f: f"{m.month:02d}",
    "M": lambda m, f: str(m.month),
    "DD": lambda m, f: f"{m.day:02d}",
    "D": lambda m, f: str(m.day),
    "Do": lambda m, f: f[0]["ordinal"][m.day - 1],
    "dddd": lambda m, f: f[0]["weekdays"][(m.weekday() + 1) % 7],
    "ddd": lambda m, f: f[0]["weekdaysShort"][(m.weekday() + 1) % 7],
    "dd": lambda m, f: f[0]["weekdaysMin"][(m.weekday() + 1) % 7],
    "d": lambda m, f: str((m.weekday() + 1) % 7),
    "HH": lambda m, f: f"{m.hour:02d}",
    "H": lambda m, f: str(m.hour),
    "hh": lambda m, f: f"{m.hour % 12 or 12:02d}",
    "h": lambda m, f: str(m.hour % 12 or 12),
    "mm": lambda m, f: f"{m.minute:02d}",
    "m": lambda m, f: str(m.minute),
    "ss": lambda m, f: f"{m.second:02d}",
    "s": lambda m, f: str(m.second),
    "SSS": lambda m, f: f"{m.microsecond // 1000:03d}",
    "A": lambda m, f: f[0]["meridiem"][m.hour][0],
    "a": lambda m, f: f[0]["meridiem"][m.hour][1],
    "Z": lambda m, f: _offset_text(m),
    "ZZ": lambda m, f: _offset_text(m).replace(":", ""),
    "Q": lambda m, f: str((m.month + 2) // 3),
    "k": lambda m, f: str(m.hour or 24),
    "kk": lambda m, f: f"{m.hour or 24:02d}",
    "X": lambda m, f: str(int(_dayjs_epoch(m))),
    "x": lambda m, f: str(int(_dayjs_epoch(m) * 1000)),
    # isoWeek plugin (W, WW, GGGG); weekOfYear/weekYear are not loaded.
    "W": lambda m, f: str(m.isocalendar().week),
    "WW": lambda m, f: f"{m.isocalendar().week:02d}",
    "GGGG": lambda m, f: f"{m.isocalendar().year:04d}",
    "zzz": lambda m, f: f[3][0],
    "z": lambda m, f: f[3][1],
}


@lru_cache(maxsize=512)
def _pattern_pieces(pattern: str) -> tuple[tuple[str, _TokenFunction | None], ...]:
    """The pattern split once into literal text and token functions."""
    pieces: list[tuple[str, _TokenFunction | None]] = []
    position = 0
    for match in _DATE_TOKEN.finditer(pattern):
        if match.start() > position:
            pieces.append((pattern[position : match.start()], None))
        token = match.group()
        pieces.append((token[1:-1], None) if token.startswith("[") else ("", _TOKENS[token]))
        position = match.end()
    if position < len(pattern):
        pieces.append((pattern[position:], None))
    return tuple(pieces)


_DATE_TOKEN = re.compile(
    r"\[[^\]]*\]|YYYY|GGGG|MMMM|dddd|SSS|MMM|ddd|zzz|YY|MM|DD|dd|HH|hh|mm|ss|ZZ|WW|Do|kk|[MDdHhmsAZQkXxaWz]"
)


@lru_cache(maxsize=512)
def _pattern_plan(pattern: str, language: str) -> tuple[str, str, str, str]:
    """Localized pattern, month name forms and the text outside ``[...]`` (once per pattern)."""
    locale = _date_locales().get(language, _date_locales()["en"])
    pattern = _expand_localized_pattern(pattern, locale["formats"])
    after_day = re.search(r"D[oD]?(?:\[[^\]]*\]|\s)+MMMM?", pattern) is not None
    before_day = re.search(r"MMMM?(?:\[[^\]]*\]|\s)+D[oD]?", pattern) is not None
    month_form = "monthsAfterDay" if after_day else "monthsBeforeDay" if before_day else "months"
    short_month_form = "monthsShortAfterDay" if after_day else "monthsShortBeforeDay" if before_day else "monthsShort"
    return pattern, month_form, short_month_form, re.sub(r"\[[^\]]*\]", "", pattern)


def _dayjs_epoch(moment: datetime) -> float:
    """Epoch printed by X/x after ``dayjs.tz``.

    For a zone with a non-zero offset the plugin keeps the wall-clock time and re-reads it in the
    process zone, so X is shifted by the difference; a zero offset takes the real UTC path.
    """
    if not (moment.utcoffset() or timedelta()):
        return moment.timestamp()
    return moment.replace(tzinfo=_local_timezone(), fold=0).timestamp()


def _zone_names(moment: datetime) -> tuple[str, str]:
    """``zzz``/``z`` of the Day.js timezone plugin: Intl ``en-US`` long and short zone names.

    Names come from ``timezone_names.json`` (dumped from Node.js ``Intl``, ICU/CLDR data): per zone and UTC
    offset, the names of each period. A zone ICU does not name prints the GMT offset like Intl.
    """
    offset = int((moment.utcoffset() or timedelta()).total_seconds())
    periods = _timezone_names().get(str(getattr(moment.tzinfo, "key", "")), {}).get(str(offset))
    if periods:
        epoch = moment.timestamp()
        long_name, short_name = next(
            ((long, short) for start, long, short in reversed(periods) if start is None or start <= epoch),
            (periods[0][1], periods[0][2]),
        )
        return long_name, short_name
    sign = "+" if offset >= 0 else "-"
    hours, rest = divmod(abs(offset), 3600)
    minutes = rest // 60
    long_name = "GMT" if offset == 0 else f"GMT{sign}{hours:02d}:{minutes:02d}"
    short_name = "GMT" if offset == 0 else f"GMT{sign}{hours}" + (f":{minutes:02d}" if minutes else "")
    return long_name, short_name


@lru_cache(maxsize=1)
def _timezone_names() -> dict[str, dict[str, list[tuple[float | None, str, str]]]]:
    return json.loads(Path(__file__).with_name("timezone_names.json").read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def _date_locales() -> dict[str, dict[str, Any]]:
    asset = Path(__file__).with_name("dayjs_date_locale.json.gz")
    with gzip.open(asset, "rt", encoding="utf-8") as stream:
        return json.load(stream)


def _expand_localized_pattern(pattern: str, formats: dict[str, str]) -> str:
    defaults = {
        "LTS": "h:mm:ss A",
        "LT": "h:mm A",
        "L": "MM/DD/YYYY",
        "LL": "MMMM D, YYYY",
        "LLL": "MMMM D, YYYY h:mm A",
        "LLLL": "dddd, MMMM D, YYYY h:mm A",
    }
    token = re.compile(r"\[[^\]]+\]|LTS|LT|LLLL|LLL|LL|L|llll|lll|ll|l")

    def expand(match: re.Match[str]) -> str:
        name = match.group()
        if name.startswith("["):
            return name
        if name in formats:
            return formats[name]
        if name in defaults:
            return defaults[name]
        expanded = formats.get(name.upper(), defaults[name.upper()])
        return re.sub(
            r"\[[^\]]+\]|MMMM|MM|DD|dddd",
            lambda part: part.group() if part.group().startswith("[") else part.group()[1:],
            expanded,
        )

    return token.sub(expand, pattern)


def convert_date(value: Any, pattern_in: Any, pattern_out: Any, *, render_options: Any = None) -> str | None:
    return format_date(value, pattern_out, pattern_in, render_options=render_options)


def add_date(
    value: Any, amount: Any, unit: Any = "day", pattern_in: Any | None = None
) -> datetime | _InvalidDate | None:
    if value is None or value is JS_UNDEFINED:
        return value
    moment = _parse(value, pattern_in)
    if isinstance(moment, _InvalidDate):
        return moment
    parsed_amount = _js_parse_int(amount)
    return INVALID_DATE if parsed_amount is None else _shift(moment, parsed_amount, unit or "day")


def sub_date(
    value: Any, amount: Any, unit: Any = "day", pattern_in: Any | None = None
) -> datetime | _InvalidDate | None:
    if value is None or value is JS_UNDEFINED:
        return value
    parsed_amount = _js_parse_int(amount)
    return add_date(value, -parsed_amount if parsed_amount is not None else "NaN", unit, pattern_in)


def start_of_date(value: Any, unit: Any = "year", pattern_in: Any | None = None) -> datetime | _InvalidDate | None:
    if value is None or value is JS_UNDEFINED:
        return value
    moment = _parse(value, pattern_in)
    if isinstance(moment, _InvalidDate):
        return INVALID_DATE
    unit_name = _date_unit(unit)
    if unit_name == "year":
        return moment.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    if unit_name == "month":
        return moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if unit_name in {"week", "isoweek"}:
        day_index = (moment.weekday() + 1) % 7 if unit_name == "week" else moment.weekday()
        return (moment - timedelta(days=day_index)).replace(hour=0, minute=0, second=0, microsecond=0)
    if unit_name in {"day", "date"}:
        return moment.replace(hour=0, minute=0, second=0, microsecond=0)
    if unit_name == "hour":
        return moment.replace(minute=0, second=0, microsecond=0)
    if unit_name == "minute":
        return moment.replace(second=0, microsecond=0)
    if unit_name == "second":
        return moment.replace(microsecond=0)
    return moment


def end_of_date(value: Any, unit: Any = "year", pattern_in: Any | None = None) -> datetime | _InvalidDate | None:
    start = start_of_date(value, unit, pattern_in)
    if start is None or start is JS_UNDEFINED or isinstance(start, _InvalidDate):
        return start
    unit_name = _date_unit(unit)
    if unit_name in {"date", "isoweek"}:
        unit_name = {"date": "day", "isoweek": "week"}[unit_name]
    if unit_name not in {"year", "month", "week", "day", "hour", "minute", "second"}:
        return start
    return _shift(start, 1, unit_name) - timedelta(milliseconds=1)


def diff_date(
    value: Any, other: Any, unit: Any = "ms", pattern_in: Any | None = None, pattern_other: Any | None = None
) -> int | float | None:
    if value is None or value is JS_UNDEFINED:
        return value
    first = _parse(value, pattern_in)
    second = _parse(other, pattern_other)
    if isinstance(first, _InvalidDate) or isinstance(second, _InvalidDate):
        return float("nan")
    unit_name = _date_unit(unit or "ms")
    if unit_name in {"month", "quarter", "year"}:
        return int(_month_difference(second, first) / {"month": 1, "quarter": 3, "year": 12}[unit_name])
    delta = second - first
    scale = {"second": 1, "minute": 60, "hour": 3600, "day": 86400, "week": 604800}.get(unit_name, 0.001)
    return int(delta.total_seconds() / scale)


def _month_difference(first: datetime, second: datetime) -> float:
    if first.day < second.day:
        return -_month_difference(second, first)
    whole = (second.year - first.year) * 12 + second.month - first.month
    anchor = _shift(first, whole, "month")
    before = second < anchor
    adjacent = _shift(first, whole + (-1 if before else 1), "month")
    adjustment = (second - anchor).total_seconds() / (
        (anchor - adjacent).total_seconds() if before else (adjacent - anchor).total_seconds()
    )
    return -(whole + adjustment)


_DURATION_SECONDS = {
    "ms": 0.001,
    "second": 1,
    "minute": 60,
    "hour": 3600,
    "day": 86400,
    "week": 604800,
    "month": 2628000,
    "year": 31536000,
}


def format_interval(value: Any, unit: Any, input_unit: Any = "ms", *, render_options: Any = None) -> float | str | None:
    if value is None or value is JS_UNDEFINED:
        return value
    try:
        seconds = float(value) * _DURATION_SECONDS[_unit(input_unit)]
    except TypeError, ValueError, KeyError:
        return math.nan
    if unit in {"human", "human+"}:
        language = locale_key(str(getattr(render_options, "lang", "en") or "en"), _date_locales())
        return _human_duration(seconds, language, with_suffix=unit == "human+")
    scale = _DURATION_SECONDS.get(_unit(unit))
    return seconds / scale if scale is not None else math.nan


def _human_duration(seconds: float, language: str, *, with_suffix: bool) -> str:
    absolute = abs(seconds)

    def rounded(number: float) -> int:
        return math.floor(number + 0.5)

    if rounded(absolute) <= 44:
        key, count = "s", 0
    elif rounded(absolute) <= 89:
        key, count = "m", 1
    elif rounded(absolute / 60) <= 44:
        key, count = "mm", rounded(absolute / 60)
    elif rounded(absolute / 60) <= 89:
        key, count = "h", 1
    elif rounded(absolute / 3600) <= 21:
        key, count = "hh", rounded(absolute / 3600)
    elif rounded(absolute / 3600) <= 35:
        key, count = "d", 1
    elif rounded(absolute / 86400) <= 25:
        key, count = "dd", rounded(absolute / 86400)
    elif rounded(absolute / 86400) <= 45:
        key, count = "M", 1
    elif rounded(absolute / 2628000) <= 10:
        key, count = "MM", rounded(absolute / 2628000)
    elif rounded(absolute / 2628000) <= 17:
        key, count = "y", 1
    else:
        key, count = "yy", rounded(absolute / 31536000)
    locales = _relative_time_locales()
    rules = locales.get(language, locales["en"])
    phrase = _duration_phrase(key, count, rules, with_suffix=with_suffix, future=seconds > 0)
    if not with_suffix:
        return phrase
    wrapper = rules["future" if seconds > 0 else "past"]
    return str(wrapper).replace("%s", phrase)


@lru_cache(maxsize=1)
def _relative_time_locales() -> dict[str, dict[str, Any]]:
    asset = Path(__file__).with_name("dayjs_relative_time.json.gz")
    with gzip.open(asset, "rt", encoding="utf-8") as stream:
        return json.load(stream)


def _duration_phrase(key: str, count: int, rules: dict[str, Any], *, with_suffix: bool, future: bool) -> str:
    rule = rules[key]
    if isinstance(rule, str):
        return rule.replace("%d", str(count))
    samples = rule[(2 if with_suffix else 0) + (1 if future else 0)]
    if count < len(samples):
        return str(samples[count])
    exemplar = 100 + count % 100
    return str(samples[exemplar]).replace(str(exemplar), str(count), 1)


def _parse(value: Any, pattern: Any | None) -> datetime | _InvalidDate:
    if isinstance(value, datetime):
        return value.astimezone() if value.tzinfo is not None else value.replace(tzinfo=_local_timezone())
    source = str(value)
    try:
        if pattern == "X":
            return datetime.fromtimestamp(float(source), _local_timezone())
        if pattern == "x":
            return datetime.fromtimestamp(float(source) / 1000, _local_timezone())
        if pattern:
            parsed = datetime.strptime(source, _python_pattern(str(pattern)))  # noqa: DTZ007 - the zone comes from %z or the process zone below
            # A Z/ZZ token parses the offset; otherwise the date is in the process time zone.
            return parsed.astimezone(_local_timezone()) if parsed.tzinfo else parsed.replace(tzinfo=_local_timezone())
        if len(source) == 8 and source.isdigit():
            return datetime.strptime(source, "%Y%m%d").replace(tzinfo=_local_timezone())
        parsed = datetime.fromisoformat(source)
        return (
            parsed.astimezone(_local_timezone())
            if parsed.tzinfo is not None
            else parsed.replace(tzinfo=_local_timezone())
        )
    except ValueError, OverflowError, TypeError:
        return INVALID_DATE


def _local_timezone() -> tzinfo:
    return datetime.now().astimezone().tzinfo or UTC


def _python_pattern(pattern: str) -> str:
    tokens = {
        "YYYY": "%Y",
        "YY": "%y",
        "MM": "%m",
        "DD": "%d",
        "HH": "%H",
        "mm": "%M",
        "ss": "%S",
        "ZZ": "%z",
        "Z": "%z",
    }
    for token in sorted(tokens, key=len, reverse=True):
        pattern = pattern.replace(token, tokens[token])
    return pattern


def _shift(moment: datetime, amount: int, unit: Any) -> datetime:
    unit_name = _date_unit(unit)
    if unit_name in {"month", "year"}:
        months = amount * {"month": 1, "year": 12}[unit_name]
        ordinal = moment.year * 12 + moment.month - 1 + months
        year, month_index = divmod(ordinal, 12)
        return moment.replace(
            year=year, month=month_index + 1, day=min(moment.day, monthrange(year, month_index + 1)[1])
        )
    delta_name = f"{unit_name}s" if unit_name in {"week", "day", "hour", "minute", "second"} else "milliseconds"
    return moment + timedelta(**{delta_name: amount})


def _date_unit(value: Any) -> str:
    if value is None or value == "":
        return "year"
    aliases = {
        "M": "month",
        "m": "minute",
        "D": "date",
        "d": "day",
        "w": "week",
        "y": "year",
        "h": "hour",
        "Q": "quarter",
        "s": "second",
        "ms": "ms",
    }
    source = str(value)
    if source in aliases:
        return aliases[source]
    normalized = source.lower().removesuffix("s")
    return "ms" if normalized == "millisecond" else normalized


def _js_parse_int(value: Any) -> int | None:
    match = re.match(r"^\s*([+-]?\d+)", str(value))
    return int(match.group(1)) if match else None


def _unit(value: Any) -> str:
    name = str(value).lower()
    if name in {"ms", "millisecond", "milliseconds"}:
        return "ms"
    singular = name.rstrip("s")
    return {"d": "day", "w": "week", "h": "hour", "m": "minute", "s": "second"}.get(singular, singular)
