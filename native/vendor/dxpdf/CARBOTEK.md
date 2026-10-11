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
