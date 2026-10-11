//! Table measure (§17.18.87 ST_TblWidth) — a discriminated width value used
//! by `<w:tblW>`, `<w:tcW>`, `<w:tblInd>`, `<w:tblCellSpacing>`, `<w:wAfter>`.
//!
//! `@type` picks the interpretation of `@w`:
//! - `dxa` → twips
//! - `pct` → percentage (§17.18.90, stored in fiftieths of a percent: 5000 = 100%)
//! - `auto` / `nil` → no explicit value

use serde::Deserialize;

use crate::docx::model::dimension::{Dimension, FiftiethPercent};
use crate::docx::model::TableMeasure;
use crate::docx::parse::primitives::integer_measure::{IntegerMeasure, MeasureKind};

#[derive(Clone, Copy, Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
enum StTblWidthType {
    Auto,
    Dxa,
    Nil,
    Pct,
}

#[derive(Clone, Copy, Debug, Deserialize)]
pub(crate) struct TableMeasureXml {
    #[serde(rename = "@w", default)]
    w: Option<IntegerMeasure>,
    #[serde(rename = "@type", default = "default_type")]
    ty: StTblWidthType,
}

fn default_type() -> StTblWidthType {
    StTblWidthType::Auto
}

impl From<TableMeasureXml> for TableMeasure {
    fn from(x: TableMeasureXml) -> Self {
        use crate::docx::parse::primitives::units::dimension_from_measure;

        // `@w` is ST_MeasurementOrPercent: a §22.9.2.15 universal measure
        // (`"297.65pt"`) or a §22.9.2.9 percentage (`"50%"`) outright; `dimension_from_measure` accepts whichever of
        // the two the arm's own unit can hold. A spelling that contradicts
        // `@type` — a percentage under `dxa`, a length under `pct` — names no
        // width this engine can honour, so it degrades to `Auto` rather than
        // guessing which of the two declarations to believe.
        match x.ty {
            StTblWidthType::Auto => Self::Auto,
            StTblWidthType::Nil => Self::Nil,
            StTblWidthType::Dxa => match x.w.map(dimension_from_measure) {
                None => Self::Twips(Dimension::new(0)),
                Some(Ok(twips)) => Self::Twips(twips),
                Some(Err(reason)) => {
                    log::warn!("[table] dropping dxa width: {reason}");
                    Self::Auto
                }
            },
            StTblWidthType::Pct => match x.w.map(dimension_from_measure::<FiftiethPercent>) {
                None => Self::Pct(Dimension::new(0)),
                Some(Ok(pct)) => Self::Pct(pct),
                Some(Err(reason)) => {
                    log::warn!("[table] dropping pct width: {reason}");
                    Self::Auto
                }
            },
        }
    }
}

impl TableMeasureXml {
    /// Whether `From<TableMeasureXml> for TableMeasure` (above) discards
    /// `@w` for this element: `@type` never reads it at all (`auto`/`nil`),
    /// or `@w`'s spelling contradicts `@type` (a percentage under `dxa`, a
    /// length under `pct`). Shared with the negative-value guard below so
    /// the two "which widths does this engine ignore" answers can't drift
    /// apart — see that guard's doc comment for why a discarded width's sign
    /// must not be allowed to fail the document either.
    fn discards_w(&self, w: &IntegerMeasure) -> bool {
        matches!(
            (self.ty, w.kind()),
            (StTblWidthType::Auto, _)
                | (StTblWidthType::Nil, _)
                | (StTblWidthType::Dxa, MeasureKind::Percent)
                | (StTblWidthType::Pct, MeasureKind::Universal)
        )
    }
}

/// Deserialize a possibly-repeated table measurement, rejecting a negative
/// value on the occurrence that survives.
///
/// Validation runs **after** last-wins collapsing, not across the whole list.
/// The two rules have to agree: if a discarded occurrence could fail the
/// document, then `<w:tcW w="-100"/><w:tcW w="500"/>` would be fatal even
/// though the effective width is a perfectly legal 500 — the parser would be
/// ignoring an element for its value while honouring it for its validity.
/// See `crate::docx::parse::primitives::duplicates` for the collapsing policy.
///
/// The same rule extends to a width `discards_w` reports as discarded: the
/// conversion above ignores it (degrading to `Auto`/`Nil`), so its sign must
/// not fail the document either — rejecting `w="-50%" type="dxa"` while
/// accepting `w="50%" type="dxa"` would again honour an ignored value for
/// its validity, and the same goes for `auto`/`nil`, which ignore `@w`
/// unconditionally.
pub(crate) fn deserialize_vec_nonnegative_table_measure<'de, D>(
    deserializer: D,
) -> Result<Vec<TableMeasureXml>, D::Error>
where
    D: serde::Deserializer<'de>,
{
    let measures = Vec::<TableMeasureXml>::deserialize(deserializer)?;
    if let Some(last) = measures.last() {
        if let Some(w) = last.w.as_ref() {
            if w.is_negative() && !last.discards_w(w) {
                return Err(serde::de::Error::custom(
                    "negative value is not valid for this OOXML table measurement",
                ));
            }
        }
    }
    Ok(measures)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[derive(Deserialize)]
    struct NonnegativeTableMeasure {
        #[serde(
            rename = "tblW",
            default,
            deserialize_with = "deserialize_vec_nonnegative_table_measure"
        )]
        value: Vec<TableMeasureXml>,
    }

    fn parse(xml: &str) -> TableMeasure {
        let x: TableMeasureXml = quick_xml::de::from_str(xml).unwrap();
        x.into()
    }

    #[test]
    fn dxa_twips() {
        match parse(r#"<tblW w="5000" type="dxa"/>"#) {
            TableMeasure::Twips(d) => assert_eq!(d.raw(), 5000),
            other => panic!("expected Twips, got {other:?}"),
        }
    }

    #[test]
    fn pct_fiftieth_percent() {
        match parse(r#"<tblW w="2500" type="pct"/>"#) {
            TableMeasure::Pct(d) => assert_eq!(d.raw(), 2500),
            other => panic!("expected Pct, got {other:?}"),
        }
    }

    #[test]
    fn auto_and_nil() {
        assert!(matches!(
            parse(r#"<tblW w="0" type="auto"/>"#),
            TableMeasure::Auto
        ));
        assert!(matches!(
            parse(r#"<tblW w="0" type="nil"/>"#),
            TableMeasure::Nil
        ));
    }

    #[test]
    fn missing_type_defaults_to_auto() {
        assert!(matches!(parse(r#"<tblW/>"#), TableMeasure::Auto));
    }

    #[test]
    fn decimal_widths_round_to_nearest_integer() {
        match parse(r#"<tblW w="2500.4" type="dxa"/>"#) {
            TableMeasure::Twips(d) => assert_eq!(d.raw(), 2500),
            other => panic!("expected Twips, got {other:?}"),
        }
        match parse(r#"<tblW w="2500.5" type="dxa"/>"#) {
            TableMeasure::Twips(d) => assert_eq!(d.raw(), 2501),
            other => panic!("expected Twips, got {other:?}"),
        }
    }

    /// §17.18.87's `@w` is `ST_MeasurementOrPercent`: a `dxa` width may be
    /// spelled as a §22.9.2.15 universal measure, and a `pct` one as a
    /// §22.9.2.9 percentage. Word's Strict output uses both spellings.
    #[test]
    fn universal_measure_converts_to_twips_for_dxa() {
        match parse(r#"<tblW w="297.65pt" type="dxa"/>"#) {
            TableMeasure::Twips(d) => assert_eq!(d.raw(), 5953),
            other => panic!("expected Twips, got {other:?}"),
        }
    }

    #[test]
    fn percent_spelling_lands_in_fiftieths_for_pct() {
        match parse(r#"<tblW w="50%" type="pct"/>"#) {
            TableMeasure::Pct(d) => assert_eq!(d.raw(), 2500),
            other => panic!("expected Pct, got {other:?}"),
        }
        match parse(r#"<tblW w="33.3%" type="pct"/>"#) {
            TableMeasure::Pct(d) => assert_eq!(d.raw(), 1665),
            other => panic!("expected Pct, got {other:?}"),
        }
    }

    /// A spelling that contradicts `@type` names no width this engine can
    /// honour; it degrades to `Auto` (with a warning) rather than guessing
    /// which of the two declarations to believe.
    #[test]
    fn contradictory_spelling_and_type_degrade_to_auto() {
        assert!(matches!(
            parse(r#"<tblW w="50%" type="dxa"/>"#),
            TableMeasure::Auto
        ));
        assert!(matches!(
            parse(r#"<tblW w="297.65pt" type="pct"/>"#),
            TableMeasure::Auto
        ));
    }

    /// A spelling the conversion discards must not fail the document over
    /// its sign either — while the same sign on an honoured spelling still
    /// does.
    #[test]
    fn contradictory_negative_degrades_while_honoured_negative_rejects() {
        let ok: Result<NonnegativeTableMeasure, _> =
            quick_xml::de::from_str(r#"<x><tblW w="-50%" type="dxa"/></x>"#);
        let value = ok.expect("a discarded spelling's sign must not be fatal");
        assert!(matches!(
            crate::model::Dup::from(value.value)
                .into_value()
                .map(TableMeasure::from),
            Some(TableMeasure::Auto)
        ));
        let err: Result<NonnegativeTableMeasure, _> =
            quick_xml::de::from_str(r#"<x><tblW w="-50%" type="pct"/></x>"#);
        assert!(err.is_err(), "an honoured negative percent is still fatal");
    }

    /// The half-tie on the thousandths→fiftieths division rounds away from
    /// zero: 0.01% is 10 thousandths, exactly half of one fiftieth.
    #[test]
    fn pct_half_tie_rounds_away_from_zero() {
        match parse(r#"<tblW w="0.01%" type="pct"/>"#) {
            TableMeasure::Pct(d) => assert_eq!(d.raw(), 1),
            other => panic!("expected Pct, got {other:?}"),
        }
    }

    /// `auto`/`nil` discard `@w` unconditionally — `TableMeasureXml::from`
    /// never reads it for either type — so a negative spelling under either
    /// must degrade rather than fail the document, the same rule that
    /// already applies to a spelling that contradicts `@type`.
    #[test]
    fn auto_and_nil_negative_widths_degrade_without_failing() {
        let ok: Result<NonnegativeTableMeasure, _> =
            quick_xml::de::from_str(r#"<x><tblW w="-50%"/></x>"#);
        let value = ok.expect("an auto width's sign must not be fatal");
        assert!(matches!(
            crate::model::Dup::from(value.value)
                .into_value()
                .map(TableMeasure::from),
            Some(TableMeasure::Auto)
        ));

        let ok: Result<NonnegativeTableMeasure, _> =
            quick_xml::de::from_str(r#"<x><tblW w="-1in" type="nil"/></x>"#);
        let value = ok.expect("a nil width's sign must not be fatal");
        assert!(matches!(
            crate::model::Dup::from(value.value)
                .into_value()
                .map(TableMeasure::from),
            Some(TableMeasure::Nil)
        ));
    }

    #[test]
    fn negative_fractions_are_rejected_for_nonnegative_table_measurements() {
        for raw in ["-0.1", "-0.49"] {
            let result: Result<NonnegativeTableMeasure, _> =
                quick_xml::de::from_str(&format!(r#"<x><tblW w="{raw}" type="dxa"/></x>"#));
            if let Ok(value) = result {
                panic!(
                    "{raw:?} must be rejected, got {:?}",
                    crate::model::Dup::from(value.value)
                        .into_value()
                        .map(TableMeasure::from)
                );
            }
        }
    }
}
