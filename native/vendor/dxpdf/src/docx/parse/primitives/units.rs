//! `Deserialize` for `Dimension<U>`. Makes OOXML numeric attributes with a
//! unit marker (twips, EMU, half-points, etc.) usable directly in schema
//! structs without hand-written wrapper types.

use serde::{Deserialize, Deserializer};

use super::integer_measure::{IntegerMeasure, MeasureKind};
use crate::model::dimension::{Dimension, Unit};

/// Land a parsed measure in `U`: a native value is already in `U`, a
/// §22.9.2.15 universal measure arrives in EMU and divides by
/// [`Unit::EMU_PER_UNIT`], and a §22.9.2.9 percentage arrives in thousandths
/// of a percent and divides by [`Unit::THOUSANDTHS_PER_UNIT`] — each rounded
/// half away from zero, and each an error when `U` has no such scale, since
/// a spelling that names a kind of quantity the attribute does not measure
/// is a contradiction to report, not to guess through.
pub(crate) fn dimension_from_measure<U: Unit>(
    measure: IntegerMeasure,
) -> Result<Dimension<U>, &'static str> {
    let (numerator, denominator) = match measure.kind() {
        MeasureKind::Native => return Ok(Dimension::new(measure.value())),
        MeasureKind::Universal => {
            U::EMU_PER_UNIT.ok_or("a universal measure cannot target this unit")?
        }
        MeasureKind::Percent => {
            let scale =
                U::THOUSANDTHS_PER_UNIT.ok_or("a percentage is not valid for this measurement")?;
            (scale, 1)
        }
    };
    let scaled = i128::from(measure.value()) * i128::from(denominator);
    let numerator = i128::from(numerator);
    let rounded = (2 * scaled.abs() + numerator) / (2 * numerator);
    let signed = if scaled < 0 { -rounded } else { rounded };
    i64::try_from(signed)
        .map(Dimension::new)
        .map_err(|_| "measurement is outside the supported range")
}

pub(crate) fn deserialize_nonnegative_dimension<'de, D, U>(
    deserializer: D,
) -> Result<Dimension<U>, D::Error>
where
    D: Deserializer<'de>,
    U: Unit,
{
    let measure = IntegerMeasure::deserialize(deserializer)?;
    // Convert first: "-63%" on a length attribute is wrong for being a
    // percentage before it is wrong for being negative, and the more
    // diagnostic error should win.
    let dimension = dimension_from_measure(measure).map_err(serde::de::Error::custom)?;
    if measure.is_negative() {
        return Err(serde::de::Error::custom(
            "negative value is not valid for this OOXML measurement",
        ));
    }
    Ok(dimension)
}

pub(crate) fn deserialize_optional_nonnegative_dimension<'de, D, U>(
    deserializer: D,
) -> Result<Option<Dimension<U>>, D::Error>
where
    D: Deserializer<'de>,
    U: Unit,
{
    Option::<IntegerMeasure>::deserialize(deserializer)?.map_or(Ok(None), |measure| {
        let dimension = dimension_from_measure(measure).map_err(serde::de::Error::custom)?;
        if measure.is_negative() {
            return Err(serde::de::Error::custom(
                "negative value is not valid for this OOXML measurement",
            ));
        }
        Ok(Some(dimension))
    })
}

impl<'de, U: Unit> Deserialize<'de> for Dimension<U> {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        dimension_from_measure(IntegerMeasure::deserialize(d)?).map_err(serde::de::Error::custom)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::dimension::{Emu, HalfPoints, Twips};
    use serde::Deserialize;

    #[derive(Deserialize)]
    struct TwipsVal {
        #[serde(rename = "@val")]
        val: Dimension<Twips>,
    }

    #[derive(Deserialize)]
    struct Sample {
        #[serde(rename = "@w")]
        w: Dimension<Emu>,
        #[serde(rename = "@h")]
        h: Dimension<HalfPoints>,
    }

    #[derive(Deserialize)]
    struct NonnegativeTwips {
        #[serde(
            rename = "@val",
            deserialize_with = "deserialize_nonnegative_dimension"
        )]
        val: Dimension<Twips>,
    }

    #[derive(Deserialize)]
    struct OptionalNonnegativeTwips {
        #[serde(
            rename = "@val",
            default,
            deserialize_with = "deserialize_optional_nonnegative_dimension"
        )]
        val: Option<Dimension<Twips>>,
    }

    #[derive(Deserialize)]
    struct EighthVal {
        #[serde(rename = "@val")]
        val: Dimension<crate::model::dimension::EighthPoints>,
    }

    #[derive(Deserialize)]
    struct HalfVal {
        #[serde(rename = "@val")]
        val: Dimension<HalfPoints>,
    }

    /// §22.9.2.15 `ST_UniversalMeasure`: a length may carry an explicit unit
    /// (`mm|cm|in|pt|pc|pi`), and the value converts to the attribute's own
    /// native unit. Word writes this spelling when saving as Strict Open XML
    /// (`<w:pgSz w:w="595.30pt"/>`), and Transitional's measurement types
    /// admit it equally.
    #[test]
    fn universal_measures_convert_to_the_target_unit() {
        for (raw, twips) in [
            ("595.3pt", 11906), // A4 width: 595.3pt × 20 twips/pt
            ("1in", 1440),
            ("2.54cm", 1440), // exactly one inch
            ("10mm", 567),    // 36000 EMU/mm × 10 ÷ 635 EMU/twip = 566.93 → 567
            ("1pc", 240),     // 1 pica = 12pt
            ("1pi", 240),     // `pi` is the second spelling of pica
        ] {
            let v: TwipsVal = quick_xml::de::from_str(&format!(r#"<x val="{raw}"/>"#)).unwrap();
            assert_eq!(v.val.raw(), twips, "{raw} in twips");
        }

        let v: HalfVal = quick_xml::de::from_str(r#"<x val="12pt"/>"#).unwrap();
        assert_eq!(v.val.raw(), 24, "12pt in half-points");
        let v: EighthVal = quick_xml::de::from_str(r#"<x val="0.5pt"/>"#).unwrap();
        assert_eq!(v.val.raw(), 4, "0.5pt in eighth-points");
    }

    /// The sign travels with the measure: a signed target accepts `-12pt`,
    /// and the nonnegative deserializers reject it like any negative value.
    #[test]
    fn universal_measures_keep_their_sign() {
        let v: TwipsVal = quick_xml::de::from_str(r#"<x val="-12pt"/>"#).unwrap();
        assert_eq!(v.val.raw(), -240);
        let r: Result<NonnegativeTwips, _> = quick_xml::de::from_str(r#"<x val="-0.5pt"/>"#);
        assert!(r.is_err(), "nonnegative target must reject -0.5pt");
    }

    /// Every length unit's EMU ratio, exercised through a conversion that
    /// lands on it exactly — a wrong constant fails its own line. `Emu` is
    /// the DrawingML coordinate target (`cx="25.4mm"` in a Strict export),
    /// `Points` the §17.3.4 border `w:space`, `FractionPoints` the §17.6.5
    /// grid `w:charSpace`.
    #[test]
    fn every_length_unit_ratio_is_pinned() {
        #[derive(Deserialize)]
        struct EmuVal {
            #[serde(rename = "@val")]
            val: Dimension<Emu>,
        }
        #[derive(Deserialize)]
        struct PointsVal {
            #[serde(rename = "@val")]
            val: Dimension<crate::model::dimension::Points>,
        }
        #[derive(Deserialize)]
        struct FractionVal {
            #[serde(rename = "@val")]
            val: Dimension<crate::model::dimension::FractionPoints>,
        }
        let v: EmuVal = quick_xml::de::from_str(r#"<x val="25.4mm"/>"#).unwrap();
        assert_eq!(v.val.raw(), 914_400, "25.4mm is one inch of EMU");
        let v: PointsVal = quick_xml::de::from_str(r#"<x val="0.5in"/>"#).unwrap();
        assert_eq!(v.val.raw(), 36, "half an inch in whole points");
        let v: FractionVal = quick_xml::de::from_str(r#"<x val="1pt"/>"#).unwrap();
        assert_eq!(v.val.raw(), 4096, "one point on the 1/4096 grid");
    }

    /// An exact half lands away from zero at the EMU→unit division too:
    /// 0.25pt is 3175 EMU, precisely half of a half-point's 6350.
    #[test]
    fn emu_conversion_rounds_half_away_from_zero() {
        let v: HalfVal = quick_xml::de::from_str(r#"<x val="0.25pt"/>"#).unwrap();
        assert_eq!(v.val.raw(), 1);
        let v: HalfVal = quick_xml::de::from_str(r#"<x val="-0.25pt"/>"#).unwrap();
        assert_eq!(v.val.raw(), -1);
    }

    /// A negative percentage keeps its sign on the percent scale, and the
    /// `+` the two grammars do not allow stays rejected.
    #[test]
    fn percent_sign_travels_and_plus_is_rejected() {
        #[derive(Deserialize)]
        struct PctVal {
            #[serde(rename = "@val")]
            val: Dimension<crate::model::dimension::ThousandthPercent>,
        }
        let v: PctVal = quick_xml::de::from_str(r#"<x val="-50%"/>"#).unwrap();
        assert_eq!(v.val.raw(), -50_000);
        for raw in ["+595.3pt", "+50%"] {
            let r: Result<TwipsVal, _> = quick_xml::de::from_str(&format!(r#"<x val="{raw}"/>"#));
            let p: Result<PctVal, _> = quick_xml::de::from_str(&format!(r#"<x val="{raw}"/>"#));
            assert!(r.is_err() && p.is_err(), "{raw:?} must be rejected");
        }
    }

    /// §22.9.2.9: a percent spelling lands on the target's own percent scale
    /// — thousandths for DrawingML (`lumMod val="63%"` ≡ `val="63000"`) — and
    /// stays an error where the attribute measures a length.
    #[test]
    fn percent_spelling_lands_on_the_thousandth_scale() {
        #[derive(Deserialize)]
        struct PctVal {
            #[serde(rename = "@val")]
            val: Dimension<crate::model::dimension::ThousandthPercent>,
        }
        let v: PctVal = quick_xml::de::from_str(r#"<x val="63%"/>"#).unwrap();
        assert_eq!(v.val.raw(), 63_000);
        let v: PctVal = quick_xml::de::from_str(r#"<x val="63000"/>"#).unwrap();
        assert_eq!(v.val.raw(), 63_000, "the bare spelling is unchanged");
        let r: Result<TwipsVal, _> = quick_xml::de::from_str(r#"<x val="63%"/>"#);
        assert!(r.is_err(), "a percentage cannot target a length");
    }

    /// Only the six §22.9.2.15 unit spellings, lowercase, no space: anything
    /// else stays a parse error rather than a guess.
    #[test]
    fn malformed_universal_measures_are_rejected() {
        for raw in [
            "12px", "pt", "12 pt", "12PT", "12p", "1.pt", ".5pt", "12pt5",
        ] {
            let r: Result<TwipsVal, _> = quick_xml::de::from_str(&format!(r#"<x val="{raw}"/>"#));
            assert!(r.is_err(), "{raw:?} must be rejected");
        }
    }

    #[test]
    fn twips_attribute_deserializes() {
        let v: TwipsVal = quick_xml::de::from_str(r#"<x val="720"/>"#).unwrap();
        assert_eq!(v.val.raw(), 720);
    }

    #[test]
    fn mixed_unit_attributes() {
        let s: Sample = quick_xml::de::from_str(r#"<ext w="914400" h="400"/>"#).unwrap();
        assert_eq!(s.w.raw(), 914_400);
        assert_eq!(s.h.raw(), 400);
    }

    #[test]
    fn negative_values_preserved() {
        let v: TwipsVal = quick_xml::de::from_str(r#"<x val="-120"/>"#).unwrap();
        assert_eq!(v.val.raw(), -120);
    }

    #[test]
    fn non_integer_rejected() {
        let r: Result<TwipsVal, _> = quick_xml::de::from_str(r#"<x val="abc"/>"#);
        assert!(
            r.is_err(),
            "expected error, got {:?}",
            r.map(|v| v.val.raw())
        );
    }

    #[test]
    fn negative_fractions_are_rejected_for_required_nonnegative_dimensions() {
        for raw in ["-0.1", "-0.49"] {
            let result: Result<NonnegativeTwips, _> =
                quick_xml::de::from_str(&format!(r#"<x val="{raw}"/>"#));
            if let Ok(value) = result {
                panic!("{raw:?} must be rejected, got {}", value.val.raw());
            }
        }
    }

    #[test]
    fn negative_fractions_are_rejected_for_optional_nonnegative_dimensions() {
        for raw in ["-0.1", "-0.49"] {
            let result: Result<OptionalNonnegativeTwips, _> =
                quick_xml::de::from_str(&format!(r#"<x val="{raw}"/>"#));
            if let Ok(value) = result {
                panic!(
                    "{raw:?} must be rejected, got {:?}",
                    value.val.map(|dimension| dimension.raw())
                );
            }
        }
    }
}
