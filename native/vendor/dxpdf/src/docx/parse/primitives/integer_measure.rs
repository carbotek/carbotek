use serde::{Deserialize, Deserializer};

/// Which of §22.9.2's spellings the attribute used — and therefore what unit
/// the stored value is in, since only a bare number is in the attribute's own
/// native unit.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum MeasureKind {
    /// A bare number: the value is in the attribute's native unit.
    Native,
    /// A §22.9.2.15 universal measure (`595.3pt`, `2.54cm`): the value is in
    /// EMU, the one grid every listed unit lands on exactly (914400/inch).
    /// The consumer divides by its own unit's `Unit::EMU_PER_UNIT`.
    Universal,
    /// A §22.9.2.9 percentage (`50%`): the value is in **thousandths** of a
    /// percent — the finest grid an OOXML percent attribute uses, and the one
    /// both targets divide out of exactly (`Unit::THOUSANDTHS_PER_UNIT` is 1
    /// for DrawingML's thousandth scale, 20 for §17.18.90's fiftieths).
    Percent,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) struct IntegerMeasure {
    value: i64,
    negative: bool,
    kind: MeasureKind,
}

impl IntegerMeasure {
    pub(crate) fn value(self) -> i64 {
        self.value
    }

    pub(crate) fn kind(self) -> MeasureKind {
        self.kind
    }

    pub(crate) fn is_negative(&self) -> bool {
        self.negative
    }
}

impl<'de> Deserialize<'de> for IntegerMeasure {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        let raw = String::deserialize(deserializer)?;
        parse_integer_measure(&raw).map_err(serde::de::Error::custom)
    }
}

/// §22.9.2.15 unit spellings and their EMU-per-unit ratio. `pc` and `pi` are
/// two spellings of the pica. One table drives both the suffix match below
/// and the ratio lookup, so the unit set cannot drift out of sync with
/// itself.
const UNIVERSAL_UNITS: &[(&str, i64)] = &[
    ("mm", 36_000),
    ("cm", 360_000),
    ("in", 914_400),
    ("pt", 12_700),
    ("pc", 152_400),
    ("pi", 152_400),
];

fn parse_integer_measure(raw: &str) -> Result<IntegerMeasure, &'static str> {
    if let Ok(value) = raw.parse::<i64>() {
        return Ok(IntegerMeasure {
            value,
            negative: raw.starts_with('-'),
            kind: MeasureKind::Native,
        });
    }

    if let Some(head) = raw.strip_suffix('%') {
        let (negative, value) = parse_decimal_scaled(head, 1000, false)?;
        return Ok(IntegerMeasure {
            value,
            negative,
            kind: MeasureKind::Percent,
        });
    }

    for &(suffix, emu_per_unit) in UNIVERSAL_UNITS {
        let Some(head) = raw.strip_suffix(suffix) else {
            continue;
        };
        let (negative, value) = parse_decimal_scaled(head, emu_per_unit, false)?;
        return Ok(IntegerMeasure {
            value,
            negative,
            kind: MeasureKind::Universal,
        });
    }

    // A bare decimal that the i64 fast path could not take: either it has a
    // fraction, or it is an out-of-range integer (which the parse below
    // rejects at the i64 conversion).
    let (negative, value) = parse_decimal_scaled(raw, 1, true)?;
    if !raw.contains('.') {
        return Err("expected an integer or decimal measurement");
    }
    Ok(IntegerMeasure {
        value,
        negative,
        kind: MeasureKind::Native,
    })
}

/// Parse `-?[0-9]+(\.[0-9]+)?`, multiply by `scale`, and round half away
/// from zero. Returns the sign separately so `-0.4` still reads as negative
/// after rounding to zero.
///
/// `allow_plus` grandfathers the pre-existing `+` tolerance for **bare**
/// numbers only: §22.9.2.15 and §22.9.2.9 both spell their sign as an
/// optional minus, so `+595.3pt` and `+50%` stay parse errors.
///
/// The fraction is truncated to its first 12 digits before scaling: with
/// every scale ≤ 914400 the discarded tail is below 1e-6 of a unit, and the
/// pre-universal-measure code read only the first digit.
fn parse_decimal_scaled(
    raw: &str,
    scale: i64,
    allow_plus: bool,
) -> Result<(bool, i64), &'static str> {
    let (negative, unsigned) = match raw.strip_prefix('-') {
        Some(rest) => (true, rest),
        None if allow_plus => (false, raw.strip_prefix('+').unwrap_or(raw)),
        None => (false, raw),
    };
    let (whole, fraction) = match unsigned.split_once('.') {
        Some(pair) => pair,
        None => (unsigned, "0"),
    };
    if whole.is_empty()
        || fraction.is_empty()
        || !whole.bytes().all(|b| b.is_ascii_digit())
        || !fraction.bytes().all(|b| b.is_ascii_digit())
    {
        return Err("invalid decimal measurement");
    }

    let whole = whole
        .parse::<i128>()
        .map_err(|_| "measurement is outside the supported range")?;
    let mut fraction_scaled: i128 = 0;
    for digit in fraction.bytes().take(12) {
        fraction_scaled = fraction_scaled * 10 + i128::from(digit - b'0');
    }
    for _ in fraction.len()..12 {
        fraction_scaled *= 10;
    }
    const FRACTION_ONE: i128 = 1_000_000_000_000;
    let magnitude = whole
        .checked_mul(i128::from(scale))
        .and_then(|scaled_whole| {
            let extra = (fraction_scaled * i128::from(scale) + FRACTION_ONE / 2) / FRACTION_ONE;
            scaled_whole.checked_add(extra)
        })
        .ok_or("measurement is outside the supported range")?;
    let signed = if negative { -magnitude } else { magnitude };
    let value = i64::try_from(signed).map_err(|_| "measurement is outside the supported range")?;
    Ok((negative, value))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[derive(Deserialize)]
    struct Value {
        #[serde(rename = "@val")]
        val: IntegerMeasure,
    }

    fn parse(raw: &str) -> Result<i64, quick_xml::DeError> {
        quick_xml::de::from_str::<Value>(&format!(r#"<x val="{raw}"/>"#))
            .map(|value| value.val.value())
    }

    #[test]
    fn accepts_integer_and_decimal_measurements() {
        assert_eq!(parse("0").unwrap(), 0);
        assert_eq!(parse("-120").unwrap(), -120);
        assert_eq!(parse("100.0").unwrap(), 100);
        assert_eq!(parse("252.00000000000003").unwrap(), 252);
        assert_eq!(parse("283.46456692913375").unwrap(), 283);
    }

    #[test]
    fn rounds_half_away_from_zero() {
        assert_eq!(parse("1.5").unwrap(), 2);
        assert_eq!(parse("-1.5").unwrap(), -2);
        assert_eq!(parse("1.499").unwrap(), 1);
        assert_eq!(parse("-1.499").unwrap(), -1);
    }

    #[test]
    fn rejects_non_decimal_and_out_of_range_values() {
        for raw in [
            "",
            ".5",
            "1.",
            "1e2",
            "NaN",
            "inf",
            "abc",
            "9223372036854775808",
            "-9223372036854775809",
        ] {
            assert!(parse(raw).is_err(), "{raw:?} must be rejected");
        }
    }

    /// The three spellings carry their kind, and the stored value is on that
    /// kind's own grid: native units, EMU, thousandths of a percent.
    #[test]
    fn kinds_and_grids() {
        let m = |raw: &str| {
            quick_xml::de::from_str::<Value>(&format!(r#"<x val="{raw}"/>"#))
                .unwrap()
                .val
        };
        assert_eq!(m("720").kind(), MeasureKind::Native);
        let universal = m("595.3pt");
        assert_eq!(universal.kind(), MeasureKind::Universal);
        assert_eq!(universal.value(), 7_560_310, "595.3 × 12700 EMU");
        let percent = m("63%");
        assert_eq!(percent.kind(), MeasureKind::Percent);
        assert_eq!(percent.value(), 63_000, "63 × 1000 thousandths");
    }
}
