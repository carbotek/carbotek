use memchr::memchr_iter;

/// Bound inline text in normalized, well-formed XML by its escaped UTF-8 size.
/// Ambiguous markup and nested inline elements require the exact XML check.
pub(crate) fn inline_text_fits(xml: &[u8], limit: usize) -> bool {
    let xml = xml.strip_prefix(b"\xef\xbb\xbf").unwrap_or(xml);
    let mut open = None;
    for start in memchr_iter(b'<', xml) {
        let tag = &xml[start + 1..];
        if tag.starts_with(b"!")
            || (tag.starts_with(b"?") && !(start == 0 && tag.starts_with(b"?xml ")))
        {
            return false;
        }
        let closing = tag.starts_with(b"/");
        let name = if closing { &tag[1..] } else { tag };
        let Some(end) = name
            .iter()
            .position(|byte| matches!(byte, b' ' | b'\t' | b'\r' | b'\n' | b'/' | b'>'))
        else {
            return false;
        };
        let qualified_name = &name[..end];
        if qualified_name != b"is" && !qualified_name.ends_with(b":is") {
            continue;
        }
        if closing {
            let Some((opening_name, content_start)) = open.take() else {
                return false;
            };
            if qualified_name != opening_name || start - content_start > limit {
                return false;
            }
        } else if open.is_some() {
            return false;
        } else if !name[end..].starts_with(b"/>") {
            // Include start-tag attributes in the bound. The delimiter itself
            // contributes no text, but can account for the first byte after the name.
            open = Some((qualified_name, start + end + 2));
        }
    }
    open.is_none()
}

#[cfg(test)]
mod tests {
    use super::inline_text_fits;

    #[test]
    fn bounds_inline_spans_without_counting_unrelated_tags() {
        for xml in [
            b"<root><is><t>abc</t></is><is/></root>".as_slice(),
            b"<root><x:is><t>abc</t></x:is ><x:is/></root>",
            b"<root><island>ignore me</island><is><t>abc</t></is></root>",
        ] {
            assert!(inline_text_fits(xml, 10));
            assert!(!inline_text_fits(xml, 9));
        }
        assert!(!inline_text_fits(b"<is attr='>'><t>abc</t></is>", 10));
    }

    #[test]
    fn ambiguous_markup_requires_exact_validation() {
        for xml in [
            b"<is><![CDATA[</is>]]>abc</is>".as_slice(),
            b"<is><!-- </is> -->abc</is>",
            b"<is><?test </is> ?>abc</is>",
            b"<is><x:is>abc</x:is></is>",
            b"<x:is>abc</y:is>",
        ] {
            assert!(!inline_text_fits(xml, 100));
        }
    }

    #[test]
    fn accepts_a_utf8_bom_before_the_xml_declaration() {
        let xml = b"\xef\xbb\xbf<?xml version='1.0'?><is><t>abc</t></is>";
        assert!(inline_text_fits(xml, 10));
        assert!(!inline_text_fits(xml, 9));
    }
}
