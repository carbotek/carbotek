//! Parser for `word/settings.xml`.

use crate::model::Dup;
use serde::Deserialize;

use crate::docx::dimension::{Dimension, Twips};
use crate::docx::error::Result;
use crate::docx::model::{DocumentSettings, RevisionSaveId};
use crate::docx::parse::primitives::units::deserialize_nonnegative_dimension;
use crate::docx::parse::primitives::OnOff;
use crate::docx::parse::serde_xml::from_xml;

/// Parse `word/settings.xml`. Entry point: deserializes into an intermediate
/// schema, then maps to the model type.
pub fn parse_settings(data: &[u8]) -> Result<DocumentSettings> {
    from_xml::<SettingsXml>(data).map(Into::into)
}

#[derive(Deserialize, Default)]
struct SettingsXml {
    #[serde(rename = "defaultTabStop", default)]
    default_tab_stop: Vec<DimensionVal<Twips>>,
    #[serde(rename = "evenAndOddHeaders", default)]
    even_and_odd_headers: Vec<OnOff>,
    #[serde(default)]
    rsids: Vec<RsidsXml>,
    #[serde(default)]
    compat: Vec<CompatXml>,
}

#[derive(Deserialize, Default)]
struct CompatXml {
    #[serde(rename = "compatSetting", default)]
    settings: Vec<CompatSettingXml>,
}

/// `<w:compatSetting w:name="…" w:uri="…" w:val="…"/>`: a name/value pair in a namespace given by
/// `@uri`. Only Word's own namespace defines `compatibilityMode`.
#[derive(Deserialize)]
struct CompatSettingXml {
    #[serde(rename = "@name", default)]
    name: String,
    #[serde(rename = "@uri", default)]
    uri: String,
    #[serde(rename = "@val", default)]
    val: String,
}

const WORD_COMPAT_URI: &str = "http://schemas.microsoft.com/office/word";

#[derive(Deserialize, Default)]
struct RsidsXml {
    #[serde(rename = "rsidRoot", default)]
    rsid_root: Vec<StringVal>,
    #[serde(rename = "rsid", default)]
    rsids: Vec<StringVal>,
}

#[derive(Deserialize)]
#[serde(bound(deserialize = "U: crate::docx::dimension::Unit"))]
struct DimensionVal<U: crate::docx::dimension::Unit> {
    #[serde(
        rename = "@val",
        deserialize_with = "deserialize_nonnegative_dimension"
    )]
    val: Dimension<U>,
}

#[derive(Deserialize)]
struct StringVal {
    #[serde(rename = "@val")]
    val: String,
}

impl From<SettingsXml> for DocumentSettings {
    fn from(x: SettingsXml) -> Self {
        let mut s = DocumentSettings::default();
        if let Some(t) = Dup::from(x.default_tab_stop).into_value() {
            s.default_tab_stop = t.val;
        }
        if let Some(OnOff(on)) = Dup::from(x.even_and_odd_headers).into_value() {
            s.even_and_odd_headers = on;
        }
        if let Some(r) = Dup::from(x.rsids).into_value() {
            if let Some(root) = Dup::from(r.rsid_root).into_value() {
                s.rsid_root = RevisionSaveId::from_hex(&root.val);
            }
            s.rsids = r
                .rsids
                .into_iter()
                .filter_map(|v| RevisionSaveId::from_hex(&v.val))
                .collect();
        }
        if let Some(compat) = Dup::from(x.compat).into_value() {
            s.compatibility_mode = compat
                .settings
                .iter()
                .rev()
                .find(|c| c.name == "compatibilityMode" && c.uri == WORD_COMPAT_URI)
                .and_then(|c| c.val.trim().parse().ok());
        }
        s
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn settings(compat: &str) -> DocumentSettings {
        parse_settings(
            format!(
                r#"<w:settings xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">{compat}</w:settings>"#
            )
            .as_bytes(),
        )
        .unwrap()
    }

    #[test]
    fn compatibility_mode_is_read_from_words_namespace() {
        let s = settings(
            r#"<w:compat><w:useFELayout/><w:compatSetting w:name="compatibilityMode" w:uri="http://schemas.microsoft.com/office/word" w:val="15"/></w:compat>"#,
        );
        assert_eq!(s.compatibility_mode, Some(15));
    }

    #[test]
    fn compatibility_mode_is_absent_without_words_setting() {
        assert_eq!(settings("").compatibility_mode, None);
        let foreign = settings(
            r#"<w:compat><w:compatSetting w:name="compatibilityMode" w:uri="urn:other" w:val="15"/></w:compat>"#,
        );
        assert_eq!(
            foreign.compatibility_mode, None,
            "another namespace's setting"
        );
    }
}
