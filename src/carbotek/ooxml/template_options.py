# pyright: strict
"""Common JS-compatible template-option grammar for DOCX and XLSX."""

import re

_OPTION = re.compile(r"\{o\.([^{}]{1,90})\}")
_OPTION_PAIR = re.compile(r"^([A-Za-z0-9_]+)=(.+)$", re.DOTALL)


def extract_template_options(xml: str, options: dict[str, object]) -> str:
    """Remove ``{o.key=value}`` markers; collect the options carbotek supports."""

    def consume(match: re.Match[str]) -> str:
        text = re.sub(r"\s+", "", match.group(1).strip())
        parsed = _OPTION_PAIR.match(text)
        if parsed is None:
            return match.group(0)
        key, value = parsed.group(1), parsed.group(2)
        if key == "preReleaseFeatureIn":
            digits = re.match(r"[+-]?\d+", value)
            options["pre_release_feature_in"] = int(digits.group(0)) if digits else None
        elif key == "useHighPrecisionArithmetic":
            options["use_high_precision_arithmetic"] = value == "true"
        elif key == "timezone":
            options["timezone"] = value
        elif key == "lang":
            options["lang"] = value.lower()
        elif key == "exportFormattedValuesAsText":
            options["export_formatted_values_as_text"] = value == "true"
        elif key == "styleSource":
            return "{d:neutralForArrayFilter:print('')}"
        return ""

    return _OPTION.sub(consume, xml)
