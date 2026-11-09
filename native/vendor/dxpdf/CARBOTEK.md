# dxpdf in carbotek

This directory is a patched copy of [dxpdf](https://github.com/nerdy-pro/dxpdf) (MIT, see
`LICENSE`), the DOCX-to-PDF converter that carbotek uses for `render_pdf`. It is linked into
`carbotek._native` when the extension is built with the `pdf` feature (all published Linux wheels).

## Base

- dxpdf **0.8.1**, upstream commit `f1c43f135065decb505d06df2684f2d41dfc7805`.
- `src/`, `Cargo.toml`, `Cargo.lock`, `README.md` and `LICENSE` are the crates.io source of 0.8.1,
  apart from the changes below. Upstream tests, benchmarks, packaging files and the Go bindings are
  not copied.

## quick-xml

dxpdf builds with a patched quick-xml 0.41.0 (`native/vendor/quick-xml-0.41.0`, MIT, see its
`PATCH.md`), as upstream's own wheels do. `native/Cargo.toml` applies it with `[patch.crates-io]`,
because a `[patch]` in a dependency's manifest is ignored. Upstream's benchmark targets are removed
from the copied `Cargo.toml`.

## Changes

Floating pictures without text wrapping (`wrapNone`), for example a signature drawn over a stamp:

1. `src/render/layout/section/stacker.rs`, `stack_blocks`: a `wrapNone` picture no longer moves the
   text cursor or the cell/row flow height. Word allows the drawing to overflow
   the row: text after the table stays in place too. `table/grid.rs` and
   `table/mod.rs` separately measure the drawing's actual emitted bottom for
   page-fit, while pagination subtracts only the group's flow height. This
   replaces the earlier `max(cursor_y, overlay_bottom)` rule, which displaced
   text after the table and was contradicted by a Microsoft Word export.
   Rows containing floating images or shapes are atomic: legal text cuts in
   another cell must not strand an overflowing drawing on the previous page.
2. `src/render/layout/section/layout.rs`, `layout_section_with_clearance`: before the pictures of a
   paragraph are placed, the paragraph moves to the next column or page when its paragraph-relative
   `wrapNone` pictures do not fit above the bottom margin. This also applies to an empty anchor
   paragraph. It does not apply when the paragraph has floating shapes, other wrap modes or an
   explicit break, and never at the top of a full-height column (no endless page creation).
3. `src/render/layout/build/block.rs`: a paragraph's cell context is identified
   by conditional cell formatting, not the presence of a resolved table style.
   An unstyled table still uses the stack coordinate frame; otherwise its
   column-relative drawings receive the page's left margin twice.
4. `src/render/layout/measurer.rs`: when the OS/2 Windows and Skia typographic
   line boxes have equal total height, redistribute ascent/descent/leading to
   the Windows baseline. This fixes Calibri's ~2.2pt baseline discrepancy
   against Word at 11pt without changing its natural line height. Missing,
   truncated or different-height boxes retain the prior metrics; emoji
   measurement is unchanged.

5. Picture transforms now travel from `pic:spPr/a:xfrm` through inline fragments and floating
   images into the paint command. `painter.rs` flips and rotates the destination frame about its
   center, after the source crop is applied. Shared media stays shared: transforms belong to the
   placement and do not modify or re-encode the source. Page-fit continues to use the unrotated
   layout frame: rotating an overlay must not move its paragraph or table row to another page.
6. Non-wrapping pictures in table cells retain page/margin references on each axis separately.
   Their absolute coordinates are resolved after pagination and header/footer placement, against
   the final logical page parity. Table/cell translations and repeated header rows no longer add
   local offsets to these references. Page-relative vertical positions do not inflate the row's
   local page-fit height. Paragraph/column references keep their existing local behavior. Wrapping
   cell drawings and VML are outside this change.
7. Paragraph-relative floats (`relativeFrom="paragraph"`, and `line` on the first line) are measured
   from the paragraph's top edge, as Word does: the bottom of the preceding block including its space
   after, or the top of the page, column or cell (`section/types.rs`, `paragraph_top_edge`). Upstream
   used the top of the text, which put pictures one `space_before` too low. The text keeps only the
   space before that survives collapse: all of it below a block without space after, the excess over
   a smaller space after, none below a larger space after or under contextual spacing. One origin
   serves registration, page-fit, drawing and re-registration after a page or column move, in the
   body (`layout.rs`) and in cells and headers/footers (`stacker.rs`). A `wrapTopAndBottom` band
   starts at that edge and pushes the whole paragraph below it, so the text keeps its space before
   under the band (`top_and_bottom_push`); this replaces the stacker's subtraction of `space_before`
   after the band. All floats of one paragraph share the origin, so a band no longer moves its
   paragraph's other floats.
8. `<w:tblCellMar>` keeps unstated sides apart from a stated zero: `TableProperties::cell_margins` is a
   `PartialEdgeInsets`, like `<w:tcMar>`. Sides cascade one by one through `basedOn`, the
   `wholeTable` overlay (`resolve/properties.rs`) and the table's own `tblPr` (`build/table.rs`); a
   side stated nowhere is zero. Upstream parsed missing sides as zero and then read every zero as
   unstated, so a table's `<w:left w:w="0"/>` kept its style's 108-twip margin (text 5.4 pt right of
   Word), and a style stating one side dropped the other three of its parent.

9. The first paragraph of the document, and of a section that starts on a new page, keeps its space
   before at the page top (`section/layout.rs`). Upstream zeroed it through a `first_on_section_page`
   flag, which is removed; Word 2013+ and Word's compatibility modes both apply that space.
10. At the top of a page or column that the text flowed to or that a page break led to, a paragraph
   loses its space before, as in Word: natural page and column flow, keep-with-next moves, a
   paragraph moved whole by the splitter, a page break ending the previous paragraph or forming a
   paragraph of its own. `PageLayoutState::top_drops_space_before` is set by `push_new_page` and
   `advance_to_next_column` and cleared by an explicit column break, which keeps the space. Text after
   a page break inside a paragraph continues it without space before. A paragraph's own
   `pageBreakBefore`, or a page break at its start, drops the space from `compatibilityMode` 15 on
   and keeps it in earlier modes (`OwnPageBreakSpacing`, from `w:compat/w:compatSetting` parsed in
   `docx/parse/settings.rs`). The keep-with-next fit on a fresh page subtracts the whole space before
   of the group's first paragraph (upstream subtracted only a contextual one).

Tests: unit tests in `section/stacker.rs` and `section/mod.rs` (§20.4.3.5 sections,
`space_before_applies_to_the_first_paragraph_of_a_section` and the "space_before at a page Word
breaks to" section), `docx/parse/settings.rs`,
`build/table.rs` (`an_explicit_zero_table_cell_margin_overrides_the_style`), `resolve/properties.rs`
(`table_cell_margins_*`) and `model/geometry.rs`; end to end in
`tests/regression/test_pdf_paragraph_anchors.py`, `tests/regression/test_pdf_table_cell_margins.py` and
`tests/regression/test_pdf_page_top_spacing.py`.
The Python checks compare synthetic documents that differ only in spacing or margins, so they hold
with any installed font; each was also evaluated on Word's own PDF export of the same documents.

Tests: `tests/regression/test_picture_geometry.py` checks rotation, both flips, crop-aware template
image fitting, independent transforms for shared media, unchanged page-fit on rotation, mixed local/page axes,
displaced second cells and repeated headers (including mirrored logical page numbering). All
fixtures are generated from synthetic colored images; no customer documents are committed.

Tests: unit tests `wrap_none_*` in `stacker.rs`, integration tests `tests/wrap_none_signature.rs`
with synthetic documents in `test-files/wrap-none-*.docx` (generator: `tests/make_wrap_none_fixture.py`).
carbotek checks the result end to end in `tests/regression/test_pdf_floating_images.py`.
The Python fixture specifies Calibri 11, 1.08 line spacing and 8pt after:
otherwise Word supplies host defaults that are absent from the DOCX. The
foreground `relativeHeight` values are in Word's normal range so the circle
also appears in front of the square when opened in Word. Recorded Word text
positions are in `tests/regression/pdf_floating_images_word.json` (same named
directory outside this vendor tree). Set `CARBOTEK_WORD_REFERENCE_FONTS=1`
only with the matching Calibri installed to run the 0.75pt reference checks.

```sh
cargo test --manifest-path native/vendor/dxpdf/Cargo.toml --lib wrap_none
cargo test --manifest-path native/vendor/dxpdf/Cargo.toml --test wrap_none_signature
```

## Updating or dropping the copy

1. Take the new upstream release (crates.io source), copy the tests and fixtures listed above and run
   them on the unmodified source. If they pass, upstream has fixed the problem: remove this
   directory and the `pdf` feature, and depend on the `dxpdf` package again.
2. Otherwise re-apply the changes above by hand, keeping their conditions, and run the
   tests, `cargo clippy` and the carbotek test suite.
3. Update the base version and commit in this file.
