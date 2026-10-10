# pyright: strict, reportPrivateUsage=false
"""Russian formatters.

JS numbers are IEEE doubles, so amounts are handled as ``float``, including
its string-shift half-up rounding (``roundHalfUp``) and the quirks of ``number-to-words-ru`` that
the conformance corpus (``tests/conformance/ru.json``) pins down.
"""

import math
import re
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Context, Decimal
from functools import lru_cache
from typing import Any
from zoneinfo import ZoneInfo

from carbotek._native_adapter import native_digit_currency_cents
from carbotek.typing_helpers import is_dict

from .condition import JS_UNDEFINED
from .jsnumber import js_math_round, js_to_number, js_to_string
from .number import number_string
from .registry import FormatterMetadata, FormatterRegistry
from .string import uc_first


def register(registry: FormatterRegistry) -> None:
    registry.register("plural", plural)
    registry.register("pluralize", plural)
    registry.register("procent", procent)
    registry.register("percent", procent)
    registry.register("dashIfZero", dash_if_zero)
    registry.register("dashZero", dash_if_zero)
    registry.register("digitCurrency", digit_currency)
    registry.register("numCurrency", digit_currency)
    registry.register("numberCurrency", digit_currency)
    registry.register("digitalCurrency", digit_currency)
    registry.register("ruDate", ru_date, FormatterMetadata(uses_render_options=True))
    registry.register("wordNumber", word_number)
    registry.register("words", word_number)
    registry.register("numberToWords", word_number)
    registry.register("wordCurrency", word_currency)
    registry.register("moneyFull", money_full)
    registry.register("sumFull", money_full)
    registry.register("roundHalfUp", round_half_up)
    registry.register("parseAmount", parse_amount)
    registry.register("formatAmount", format_amount)
    registry.register("parseDate", parse_date)


def _missing(value: Any) -> bool:
    """``value === undefined || value === null || value === ''``."""
    return value is None or value is JS_UNDEFINED or (isinstance(value, str) and value == "")


def _blank(value: Any) -> bool:
    return _missing(value) or (isinstance(value, str) and value.strip() == "")


_JS_DECIMAL = re.compile(r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?")
_JS_RADIX = re.compile(r"0([xXoObB])([0-9a-fA-F]+)")


def _js_number(text: str) -> float:
    """``Number(string)`` for an already trimmed string without spaces."""
    if text in {"Infinity", "+Infinity"}:
        return math.inf
    if text == "-Infinity":
        return -math.inf
    if _JS_DECIMAL.fullmatch(text):
        return float(text)
    radix = _JS_RADIX.fullmatch(text)
    if radix:
        base = {"x": 16, "o": 8, "b": 2}[radix.group(1).lower()]
        try:
            return float(int(radix.group(2), base))
        except ValueError:
            return math.nan
    return math.nan


def parse_amount(value: Any) -> float | None:
    """Port of ``parseAmount``: a finite number from a number or a formatted string, else ``None``."""
    if value is None or value is JS_UNDEFINED or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, Decimal):
        return float(value) if value.is_finite() else None
    if not isinstance(value, str):
        return None
    trimmed = value.strip()
    if not trimmed:
        return None
    text = re.sub(r"[\s ]+", "", trimmed)
    if "," in text and "." in text:
        text = (
            text.replace(",", "") if text.index(",") < text.index(".") else text.replace(".", "").replace(",", ".", 1)
        )
    elif "," in text:
        text = text.replace(",", ".", 1)
    number = _js_number(text)
    return number if math.isfinite(number) else None


def round_half_up(num: Any, precision: Any = 2) -> Any:
    """Port of ``roundHalfUp``: half-up through an exponential string shift, symmetric for negatives.

    ``precision`` is a JS value: a template passes the string ``"2"``, which ``>=`` compares as a
    number and ``+ 'e'`` concatenates as text.
    """
    if precision is JS_UNDEFINED:
        precision = 2
    if not isinstance(num, int | float) or isinstance(num, bool):
        return num
    if js_to_number(precision) >= 0 and math.isfinite(num) and float(num).is_integer():
        return num
    if not math.isfinite(num):
        return num
    text = number_string(float(num))
    if "e" in text.lower():
        return num
    sign = -1 if num < 0 else 1
    if type(precision) is int and 0 <= precision <= 15 and abs(num) < 1e15:
        # Same steps without the string round trips: String(shifted) of an integral double below
        # 1e15 is its integer digits, and Number(digits + "e-" + p) is float() of that text.
        shifted = math.floor(float(f"{text.lstrip('-')}e{precision}") + 0.5)
        if shifted < 10**15:
            return sign * float(f"{shifted}e-{precision}")
    places = js_to_string(precision)
    shifted = js_math_round(js_to_number(f"{number_string(abs(float(num)))}e{places}"))
    # Number(shifted + 'e-' + p) is NaN when String(shifted) is already exponential ("1e+100e-100").
    return sign * js_to_number(f"{number_string(shifted)}e-{places}")


def _js_parse_int(value: Any) -> float:
    match = re.match(r"\s*([+-]?\d+)", str(value))
    return float(int(match.group(1))) if match else math.nan


def safe_precision(precision: Any, fallback: int) -> int:
    """Port of ``safePrecision``: clamp a template argument to an integer in [0, 100]."""
    if precision is None or precision is JS_UNDEFINED or precision == "":
        return fallback
    number = (
        float(precision)
        if isinstance(precision, int | float) and not isinstance(precision, bool)
        else _js_parse_int(precision)
    )
    if not math.isfinite(number) or number < 0:
        return fallback
    return 100 if number > 100 else int(number)


_FIXED_CONTEXT = Context(prec=200)


@lru_cache(maxsize=128)
def _quantum(places: int) -> Decimal:
    return Decimal(1).scaleb(-places)


def _to_fixed(value: float, places: int) -> str:
    """``Number.prototype.toFixed`` below 1e21: nearest decimal of the exact double, ties up."""
    if not math.isfinite(value) or abs(value) >= 1e21:
        return number_string(value)
    if type(value) is float and type(places) is int and 0 <= places <= 100:
        # Scale the exact binary double as integers: decimal ties round away from zero,
        # without constructing a Decimal or depending on the process decimal context.
        numerator, denominator = abs(value).as_integer_ratio()
        rounded_integer, remainder = divmod(numerator * 10**places, denominator)
        rounded_integer += remainder * 2 >= denominator
        digits = str(rounded_integer)
        if places:
            digits = digits.zfill(places + 1)
            text = f"{digits[:-places]}.{digits[-places:]}"
        else:
            text = digits
        return f"-{text}" if value < 0 and rounded_integer != 0 else text
    rounded = Decimal(abs(value)).quantize(_quantum(places), rounding=ROUND_HALF_UP, context=_FIXED_CONTEXT)
    text = f"{rounded:.{places}f}"
    return f"-{text}" if value < 0 and rounded != 0 else text


def _group(digits: str, separator: str) -> str:
    """Thousands groups, like the JS port's lookahead regular expression (slicing for plain digits)."""
    if not digits.isdigit():
        return re.sub(r"\B(?=(\d{3})+(?!\d))", separator, digits)
    if len(digits) <= 3:
        return digits
    first = len(digits) % 3 or 3
    return separator.join([digits[:first], *(digits[index : index + 3] for index in range(first, len(digits), 3))])


def format_amount(num: Any, options: Any = None) -> Any:
    """Port of ``formatAmount`` (options: ``precision``, ``nbsp``, ``zero``, ``sign``)."""
    options = options if is_dict(options) else {}
    places = options.get("precision", 2)
    use_nbsp = options.get("nbsp") is True
    zero = options.get("zero")
    show_sign = options.get("sign", True)
    return _format_amount(num, places, use_nbsp, zero, show_sign)


def _format_amount(num: Any, places: Any, use_nbsp: bool, zero: Any, show_sign: Any) -> Any:
    """The amount operation with options already unpacked (also used by digitCurrency)."""
    if num == 0 and zero is not None:
        return zero
    rounded = round_half_up(num, places)
    negative = num < 0 or (num == 0 and math.copysign(1.0, float(num)) < 0)
    prefix = "-" if show_sign and negative and rounded != 0 else ""
    absolute = abs(float(rounded))
    space = " " if use_nbsp else " "
    if absolute >= 1e21 and absolute.is_integer():
        integer = _group(str(int(absolute)), space)
        return f"{prefix}{integer},{'0' * places}" if places > 0 else f"{prefix}{integer}"
    integer, _, fraction = _to_fixed(absolute, places).partition(".")
    integer = _group(integer, space)
    return f"{prefix}{integer},{fraction}" if places > 0 and fraction else f"{prefix}{integer}"


def digit_currency(value: Any, precision: Any = None) -> Any:
    """``{d.x:digitCurrency}``: 130000 -> "130 000,00"."""
    if _blank(value):
        return ""
    if precision is None and (native := native_digit_currency_cents(value)) is not None:
        return native
    places = safe_precision(precision, 2)
    parsed = parse_amount(value)
    if parsed is None:
        return value
    return _format_amount(parsed, places, False, None, True)


def money_full(value: Any, precision: Any = None) -> str:
    """``{d.x:moneyFull}``: digits and words computed from the same rounded amount."""
    if _blank(value):
        return ""
    number = parse_amount(value)
    if number is None:
        return ""
    places = safe_precision(precision, 2)
    digits = digit_currency(number, places)
    words = word_currency(round_half_up(number, places))
    return f"{digits} ({words})" if digits and words else ""


def _integer_and_fraction(number: float) -> tuple[str, str]:
    """Digit strings of ``String(number)`` as number-to-words-ru splits them (sign dropped)."""
    text = number_string(abs(number))
    integer, _, fraction = text.partition(".")
    # For "1e+21" the library keeps only the digits of the integer part ("121").
    return re.sub(r"\D", "", integer) or "0", re.sub(r"\D", "", fraction)


def word_currency(value: Any) -> str:
    """``{d.x:wordCurrency}``: the amount in words with rubles and kopecks."""
    number = parse_amount(value)
    if number is None:
        return ""
    integer, fraction = _integer_and_fraction(number)
    kopecks = Decimal(f"0.{fraction or '0'}").quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    rubles = int(integer) + int(kopecks)
    cents = int((kopecks % 1) * 100)
    text = (
        f"{'минус ' if number < 0 else ''}{_words(rubles)} {_form(rubles, 'рубль', 'рубля', 'рублей')} "
        f"{cents:02d} {_form(cents, 'копейка', 'копейки', 'копеек')}"
    )
    return text[:1].upper() + text[1:]


def word_number(value: Any) -> Any:
    """``{d.x:wordNumber}``: the integer part in words, lower case."""
    if _missing(value):
        return ""
    number = parse_amount(value)
    if number is None:
        return value
    integer, fraction = _integer_and_fraction(number)
    # number-to-words-ru rounds the fraction to kopecks first: 0.999 reads "один", like wordCurrency.
    kopecks = Decimal(f"0.{fraction or '0'}").quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"{'минус ' if number < 0 else ''}{_words(int(integer) + int(kopecks))}"


def capitalize(value: Any) -> Any:
    return uc_first(value) if isinstance(value, str) else value


def dash_if_zero(value: Any, dash: Any = "—") -> Any:
    """Replace a zero or empty amount with a financial dash."""
    if dash is JS_UNDEFINED:
        dash = "—"
    if _missing(value):
        return dash
    return dash if parse_amount(value) == 0 else value


def procent(value: Any, precision: Any = None) -> str:
    """``{d.rate:procent}``: 20 -> "20,00 %"."""
    if _missing(value):
        return ""
    number = parse_amount(value)
    if number is None:
        return value if isinstance(value, str) else _js_string(value)
    places = safe_precision(precision, 2)
    return _to_fixed(float(round_half_up(number, places)), places).replace(".", ",", 1) + " %"


def _js_string(value: Any) -> str:
    from .condition import _js_string as js_string

    return js_string(value)


def plural(count: Any, one: Any, few: Any, many: Any, show_number: Any = True) -> Any:
    """Russian plural form: ``plural(5, 'день', 'дня', 'дней')`` -> "5 дней"."""
    if show_number is JS_UNDEFINED:
        show_number = True
    if _blank(count):
        return ""
    number = parse_amount(count)
    if number is None:
        return ""
    absolute = abs(number)
    form = few if not absolute.is_integer() else _form(int(absolute), one, few, many)
    if show_number not in (True, "true", 1, "1"):
        return form
    printed = number_string(number).replace(".", ",", 1)
    return f"{printed} {form}"


def _form(number: int, one: Any, few: Any, many: Any) -> Any:
    if 11 <= number % 100 <= 19:
        return many
    if number % 10 == 1:
        return one
    return few if 2 <= number % 10 <= 4 else many


_STRICT_ISO = re.compile(r"\d{4}-\d{2}-\d{2}")
_STRICT_DOTTED = re.compile(r"\d{2}\.\d{2}\.\d{4}")
_EXPLICIT_OFFSET = re.compile(r"(Z|[+-]\d{2}:?\d{2})$")


def parse_date(value: Any, timezone: Any = "Europe/Moscow") -> date | None:
    """Port of ``parseDate``: strict date parsing, the calendar date in ``timezone``."""
    if timezone is None or timezone is JS_UNDEFINED:
        timezone = "Europe/Moscow"
    zone = ZoneInfo(str(timezone))
    if _missing(value) or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        return (value.replace(tzinfo=zone) if value.tzinfo is None else value.astimezone(zone)).date()
    if isinstance(value, date):
        return value
    if isinstance(value, int | float):
        if not math.isfinite(value):
            return None
        return datetime.fromtimestamp(value / 1000, tz=zone).date()
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        if _STRICT_ISO.fullmatch(text):
            return date.fromisoformat(text)
        if _STRICT_DOTTED.fullmatch(text):
            day, month, year = text.split(".")
            return date(int(year), int(month), int(day))
        if (prefix := re.match(r"(\d{4})-(\d{2})-(\d{2})", text)) is not None:
            date(int(prefix.group(1)), int(prefix.group(2)), int(prefix.group(3)))
        if (prefix := re.match(r"(\d{2})\.(\d{2})\.(\d{4})", text)) is not None:
            date(int(prefix.group(3)), int(prefix.group(2)), int(prefix.group(1)))
            return None  # dayjs does not parse "DD.MM.YYYY HH:mm" without a format
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00") if text.endswith("Z") else text)
    except ValueError:
        return None
    if _EXPLICIT_OFFSET.search(text):
        return parsed.astimezone(zone).date()
    return parsed.replace(tzinfo=zone).date()


def ru_date(value: Any, custom_timezone: Any = None, *, render_options: Any = None) -> Any:
    """``{d.x:ruDate}``: «DD» <month> YYYY in the render timezone and language."""
    if _blank(value):
        return ""
    timezone = custom_timezone if custom_timezone not in (None, "", JS_UNDEFINED) else None
    timezone = timezone or getattr(render_options, "timezone", None) or "Europe/Moscow"
    language = str(getattr(render_options, "lang", None) or "ru").lower()
    try:
        parsed = parse_date(value, timezone)
    except ValueError, KeyError:
        return value
    if parsed is None:
        return value
    from .date import _date_locales

    locales = _date_locales()
    # dayjs keeps the global locale (ru) for a language it does not know.
    month = locales.get(language, locales["ru"])["monthsAfterDay"][parsed.month - 1]
    return f"«{parsed.day:02d}» {month} {parsed.year:04d}"


_ONES = ("ноль", "один", "два", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять")
_TEENS = (
    "десять",
    "одиннадцать",
    "двенадцать",
    "тринадцать",
    "четырнадцать",
    "пятнадцать",
    "шестнадцать",
    "семнадцать",
    "восемнадцать",
    "девятнадцать",
)
_TENS = ("", "", "двадцать", "тридцать", "сорок", "пятьдесят", "шестьдесят", "семьдесят", "восемьдесят", "девяносто")
_HUNDREDS = ("", "сто", "двести", "триста", "четыреста", "пятьсот", "шестьсот", "семьсот", "восемьсот", "девятьсот")
_SCALES = (
    (10**18, ("квинтиллион", "квинтиллиона", "квинтиллионов"), False),
    (10**15, ("квадриллион", "квадриллиона", "квадриллионов"), False),
    (10**12, ("триллион", "триллиона", "триллионов"), False),
    (10**9, ("миллиард", "миллиарда", "миллиардов"), False),
    (10**6, ("миллион", "миллиона", "миллионов"), False),
    (10**3, ("тысяча", "тысячи", "тысяч"), True),
)


def _words(value: int) -> str:
    if value == 0:
        return _ONES[0]
    result: list[str] = []
    remainder = value
    for divisor, forms, feminine in _SCALES:
        group, remainder = divmod(remainder, divisor)
        if group:
            result.append(_words(group) if group >= 1000 else _under_thousand(group, feminine=feminine))
            result.append(_form(group, *forms))
    if remainder:
        result.append(_under_thousand(remainder))
    return " ".join(result)


def _under_thousand(value: int, *, feminine: bool = False) -> str:
    parts = [_HUNDREDS[value // 100]] if value >= 100 else []
    rest = value % 100
    if 10 <= rest < 20:
        parts.append(_TEENS[rest - 10])
    else:
        if rest >= 20:
            parts.append(_TENS[rest // 10])
        if rest % 10:
            one = rest % 10
            parts.append("одна" if feminine and one == 1 else "две" if feminine and one == 2 else _ONES[one])
    return " ".join(part for part in parts if part)
