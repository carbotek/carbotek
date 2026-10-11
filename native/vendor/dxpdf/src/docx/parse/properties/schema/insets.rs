//! Edge insets (§17.4.68 tcMar, §17.4.42 tblCellMar) — four-sided twips
//! padding shared by table-cell margins and table default cell margins.
//!
//! Each side is `<w:top w:w="N" w:type="dxa"/>` etc. Only `dxa` (twips) is
//! meaningful for cell padding; other `@type` values are ignored here.

use crate::model::Dup;
use serde::{Deserialize, Deserializer};

use crate::docx::model::dimension::{Dimension, Twips};
use crate::docx::model::geometry::{EdgeInsets, PartialEdgeInsets};
use crate::docx::parse::primitives::integer_measure::{IntegerMeasure, MeasureKind};
use crate::docx::parse::primitives::units::dimension_from_measure;

#[derive(Clone, Debug, Default, Deserialize)]
pub(crate) struct EdgeInsetsTwipsXml {
    #[serde(default)]
    top: Vec<SideXml>,
    #[serde(default)]
    bottom: Vec<SideXml>,
    #[serde(default, alias = "start")]
    left: Vec<SideXml>,
    #[serde(default, alias = "end")]
    right: Vec<SideXml>,
}

#[derive(Clone, Copy, Debug)]
struct SideXml {
    w: Option<Dimension<Twips>>,
}

/// The raw shadow of `<w:top w:w="N" w:type="dxa"/>` etc.: both attributes
/// have to be read together (`SideXml`'s own `Deserialize` below), since
/// which spelling of "percent, not a length" `@w` used depends on `@type`.
#[derive(Deserialize)]
struct SideAttrsXml {
    #[serde(rename = "@w", default)]
    w: Option<IntegerMeasure>,
    #[serde(rename = "@type", default)]
    ty: Option<String>,
}

/// A side's `@w`/`@type` are CT_TblWidth's, but only a `dxa`-typed length is
/// meaningful for padding (the module doc owns why). A percent spelling is
/// legal for the type but meaningless here, and §17.18.91 admits two ways to
/// write one: a literal `%` suffix on `@w` (§22.9.2.9, `MeasureKind::Percent`)
/// or the older Transitional `w:type="pct"` alongside a bare number — a bare
/// `w="2500"` under `type="pct"` is not 2500 twips. `auto`/`nil` name no
/// width at all and drop the same way. Every one of those drops the side
/// with a warning instead of failing the document, mirroring
/// `TableMeasureXml`'s policy for a spelling that contradicts its `@type`.
impl<'de> Deserialize<'de> for SideXml {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        let raw = SideAttrsXml::deserialize(deserializer)?;
        let Some(measure) = raw.w else {
            return Ok(SideXml { w: None });
        };
        let type_is_dxa_or_absent = matches!(raw.ty.as_deref(), None | Some("dxa"));
        if measure.kind() == MeasureKind::Percent || !type_is_dxa_or_absent {
            log::warn!("[table] dropping a cell-margin width that isn't a dxa length");
            return Ok(SideXml { w: None });
        }
        let dimension = dimension_from_measure(measure).map_err(serde::de::Error::custom)?;
        if measure.is_negative() {
            return Err(serde::de::Error::custom(
                "negative value is not valid for this OOXML measurement",
            ));
        }
        Ok(SideXml { w: Some(dimension) })
    }
}

/// Conversion used for the table-level default (`<w:tblCellMar>`). Per
/// OOXML §17.4.42 a table default has no further inheritance, so missing
/// sides collapse to zero.
impl From<EdgeInsetsTwipsXml> for EdgeInsets<Twips> {
    fn from(x: EdgeInsetsTwipsXml) -> Self {
        Self::new(
            Dup::from(x.top)
                .into_value()
                .and_then(|s| s.w)
                .unwrap_or_default(),
            Dup::from(x.right)
                .into_value()
                .and_then(|s| s.w)
                .unwrap_or_default(),
            Dup::from(x.bottom)
                .into_value()
                .and_then(|s| s.w)
                .unwrap_or_default(),
            Dup::from(x.left)
                .into_value()
                .and_then(|s| s.w)
                .unwrap_or_default(),
        )
    }
}

/// Conversion used for per-cell overrides (`<w:tcMar>`, §17.4.68). Each
/// child element (`<w:top>` §17.4.75, `<w:start>` §17.4.34, `<w:bottom>`
/// §17.4.5, `<w:end>` §17.4.11) is an *exception* that overrides the
/// corresponding side of the parent `<w:tblCellMar>` (§17.4.42).
///
/// Per the spec, cascade is per-side: a side whose element is structurally
/// absent inherits from the parent; a side whose element is present is an
/// explicit override, including `<w:top w:w="0" w:type="dxa"/>` which is
/// `CT_TblWidth` (§17.4.87) for "0 twips" — an explicit zero, not a
/// placeholder for "no override". The layout layer resolves the partial
/// override against the table-level default via
/// [`PartialEdgeInsets::resolve_against`].
impl From<EdgeInsetsTwipsXml> for PartialEdgeInsets<Twips> {
    fn from(x: EdgeInsetsTwipsXml) -> Self {
        Self::new(
            Dup::from(x.top).into_value().and_then(|s| s.w),
            Dup::from(x.right).into_value().and_then(|s| s.w),
            Dup::from(x.bottom).into_value().and_then(|s| s.w),
            Dup::from(x.left).into_value().and_then(|s| s.w),
        )
    }
}

#[cfg(test)]
mod tests {
    /// §22.9.2.15 / §22.9.2.9 through a margin side: a length spelling
    /// converts, a percent one drops the side, a negative one is fatal.
    #[test]
    fn side_widths_take_lengths_and_drop_percents() {
        let x: EdgeInsetsTwipsXml =
            quick_xml::de::from_str(r#"<m><top w="0.1in" type="dxa"/></m>"#).unwrap();
        let insets = EdgeInsets::from(x);
        assert_eq!(insets.top.raw(), 144, "0.1in in twips");

        let x: EdgeInsetsTwipsXml =
            quick_xml::de::from_str(r#"<m><top w="5%" type="pct"/></m>"#).unwrap();
        let insets = EdgeInsets::from(x);
        assert_eq!(insets.top.raw(), 0, "percent side is dropped, not fatal");

        let r: Result<EdgeInsetsTwipsXml, _> =
            quick_xml::de::from_str(r#"<m><top w="-1pt" type="dxa"/></m>"#);
        assert!(r.is_err(), "a negative length side is still rejected");
    }

    /// A percent side is never "honoured" here — only `dxa` is meaningful
    /// for cell padding — so unlike a length side, its sign must not be
    /// fatal either: the value is discarded regardless, the same rule
    /// `measure.rs` applies to a spelling its own `@type` contradicts.
    #[test]
    fn negative_percent_side_drops_without_failing() {
        let x: EdgeInsetsTwipsXml =
            quick_xml::de::from_str(r#"<m><top w="-5%" type="pct"/></m>"#).unwrap();
        let insets = EdgeInsets::from(x);
        assert_eq!(
            insets.top.raw(),
            0,
            "a negative percent side is dropped, not fatal"
        );
    }

    /// §17.18.91 `ST_TblWidthType` has two ways to spell "percent, not a
    /// length": a literal `%` suffix on `@w` (§22.9.2.9) or the older
    /// Transitional `w:type="pct"` alongside a bare number. Both must drop
    /// the side the same way — a bare `w="2500"` under `type="pct"` is not
    /// 2500 twips of padding.
    #[test]
    fn side_widths_drop_the_type_pct_spelling_of_percent_too() {
        let x: EdgeInsetsTwipsXml =
            quick_xml::de::from_str(r#"<m><top w="2500" type="pct"/></m>"#).unwrap();
        let insets = EdgeInsets::from(x);
        assert_eq!(
            insets.top.raw(),
            0,
            "type=\"pct\" with a bare number must drop, not read as 2500 twips"
        );
    }

    /// `auto`/`nil` name no width at all — the same "only dxa is meaningful
    /// here" rule that drops a percent spelling.
    #[test]
    fn side_widths_drop_auto_and_nil_types() {
        let x: EdgeInsetsTwipsXml =
            quick_xml::de::from_str(r#"<m><top w="100" type="auto"/></m>"#).unwrap();
        let insets = EdgeInsets::from(x);
        assert_eq!(
            insets.top.raw(),
            0,
            "type=\"auto\" must drop, not read as twips"
        );
    }

    use super::*;

    fn parse(xml: &str) -> EdgeInsets<Twips> {
        let x: EdgeInsetsTwipsXml = quick_xml::de::from_str(xml).unwrap();
        x.into()
    }

    #[test]
    fn all_four_sides_captured() {
        let e = parse(
            r#"<tcMar>
                <top w="100"/>
                <bottom w="200"/>
                <left w="50"/>
                <right w="75"/>
            </tcMar>"#,
        );
        assert_eq!(e.top.raw(), 100);
        assert_eq!(e.bottom.raw(), 200);
        assert_eq!(e.left.raw(), 50);
        assert_eq!(e.right.raw(), 75);
    }

    #[test]
    fn start_and_end_alias_left_right() {
        let e = parse(
            r#"<tcMar>
                <start w="80"/>
                <end w="120"/>
            </tcMar>"#,
        );
        assert_eq!(e.left.raw(), 80);
        assert_eq!(e.right.raw(), 120);
    }

    #[test]
    fn missing_sides_default_to_zero() {
        let e = parse(r#"<tcMar><top w="100"/></tcMar>"#);
        assert_eq!(e.top.raw(), 100);
        assert_eq!(e.right.raw(), 0);
        assert_eq!(e.bottom.raw(), 0);
        assert_eq!(e.left.raw(), 0);
    }

    /// Per OOXML §17.4.68, an absent child element in `<w:tcMar>` means
    /// "inherit from `<w:tblCellMar>` for that side". Only the sides
    /// structurally present in the XML are an override; the rest stay `None`
    /// so the layout cascade can fall back to the table-level default.
    #[test]
    fn partial_tcmar_preserves_absent_sides_as_none() {
        let xml = r#"<tcMar>
            <top w="40"/>
        </tcMar>"#;
        let parsed: EdgeInsetsTwipsXml = quick_xml::de::from_str(xml).unwrap();
        let p: PartialEdgeInsets<Twips> = parsed.into();
        assert_eq!(p.top.map(|d| d.raw()), Some(40));
        assert!(p.bottom.is_none(), "absent <w:bottom> must stay None");
        assert!(p.left.is_none(), "absent <w:start>/<w:left> must stay None");
        assert!(p.right.is_none(), "absent <w:end>/<w:right> must stay None");

        let default = EdgeInsets::<Twips>::new(
            Dimension::new(57),
            Dimension::new(108),
            Dimension::new(57),
            Dimension::new(103),
        );
        let resolved = p.resolve_against(default);
        assert_eq!(resolved.top.raw(), 40, "explicit override wins");
        assert_eq!(resolved.bottom.raw(), 57, "absent side inherits");
        assert_eq!(resolved.left.raw(), 103, "absent side inherits");
        assert_eq!(resolved.right.raw(), 108, "absent side inherits");
    }

    /// Per OOXML §17.4.87 (`CT_TblWidth`), `@type="dxa" @w="0"` is
    /// "0 twentieths of a point", i.e. an explicit zero. Combined with
    /// §17.4.68's "exception" semantics, a `<w:top w:w="0" w:type="dxa"/>`
    /// inside `<w:tcMar>` is an *explicit override* to zero, structurally
    /// distinct from an absent child element. This test pins the spec-
    /// faithful interpretation: the parser must report `Some(0)`, not `None`.
    ///
    /// (Word/LibreOffice may render such cells with the table-default
    /// padding regardless — that is a rendering quirk in those products
    /// and not something the parser may silently impose; emulating it
    /// belongs in the layout layer behind an explicit, named code path.)
    #[test]
    fn partial_tcmar_w_zero_is_explicit_zero_override() {
        let xml = r#"<tcMar>
            <top w="0"/>
            <bottom w="0"/>
        </tcMar>"#;
        let parsed: EdgeInsetsTwipsXml = quick_xml::de::from_str(xml).unwrap();
        let p: PartialEdgeInsets<Twips> = parsed.into();
        assert_eq!(
            p.top.map(|d| d.raw()),
            Some(0),
            "w=0 must be preserved as explicit Some(0), not silently dropped"
        );
        assert_eq!(p.bottom.map(|d| d.raw()), Some(0));
        assert!(p.left.is_none());
        assert!(p.right.is_none());

        let default = EdgeInsets::<Twips>::new(
            Dimension::new(57),
            Dimension::new(108),
            Dimension::new(57),
            Dimension::new(103),
        );
        let resolved = p.resolve_against(default);
        assert_eq!(resolved.top.raw(), 0, "explicit zero overrides default");
        assert_eq!(resolved.bottom.raw(), 0, "explicit zero overrides default");
        assert_eq!(resolved.left.raw(), 103, "absent side inherits default");
        assert_eq!(resolved.right.raw(), 108, "absent side inherits default");
    }
}
