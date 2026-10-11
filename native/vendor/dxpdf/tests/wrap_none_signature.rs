//! Non-wrapping table drawings must not displace subsequent signature text.
use dxpdf::render::layout::draw_command::DrawCommand;

fn check(name: &str, expected_pages: usize) {
    let path = format!("{}/test-files/{name}", env!("CARGO_MANIFEST_DIR"));
    let doc = dxpdf::docx::parse(&std::fs::read(path).unwrap()).unwrap();
    let (_, pages) = dxpdf::render::resolve_and_layout(doc);
    assert_eq!(pages.len(), expected_pages);
    let mut checked = false;
    for page in pages {
        for image in page.commands.iter().filter_map(|c| match c {
            DrawCommand::Image { rect, .. } => Some(rect),
            _ => None,
        }) {
            let signature_y = page
                .commands
                .iter()
                .find_map(|c| match c {
                    DrawCommand::Text { text, position, .. } if text.contains("SIGNATURE") => {
                        Some(position.y.raw())
                    }
                    _ => None,
                })
                .expect("signature and drawing remain on the same page");
            assert!(signature_y > image.origin.y.raw());
            assert!(
                signature_y < image.origin.y.raw() + 60.0,
                "signature follows the anchor line, not the 100pt picture: {signature_y}"
            );
            assert!(
                image.origin.y.raw() + image.size.height.raw() <= 364.0,
                "picture is contained inside page margins"
            );
            checked = true;
        }
    }
    assert!(checked);
}

#[test]
fn wrap_none_signature_line_stays_over_drawing() {
    check("wrap-none-signature.docx", 1);
}

#[test]
fn wrap_none_signature_table_moves_whole_at_page_boundary() {
    check("wrap-none-signature-boundary.docx", 2);
}

#[test]
fn wrap_none_body_and_cell_anchors_stay_together_at_page_boundary() {
    check("wrap-none-body-and-cell-signature-boundary.docx", 2);
}

#[test]
fn wrap_none_body_and_cell_anchors_do_not_add_flow_height() {
    check("wrap-none-body-and-cell-signature.docx", 1);
}

#[test]
fn wrap_none_empty_body_anchor_moves_before_following_table() {
    check("wrap-none-body-and-cell-signature-boundary-empty.docx", 2);
}
