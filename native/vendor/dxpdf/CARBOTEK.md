# dxpdf in carbotek

This directory is a patched copy of [dxpdf](https://github.com/nerdy-pro/dxpdf) (MIT, see
`LICENSE`), the DOCX-to-PDF converter that carbotek uses for `render_pdf`. It is linked into
`carbotek._native` when the extension is built with the `pdf` feature (all published Linux wheels).

## Base

- dxpdf **0.8.1**, upstream commit `f1c43f135065decb505d06df2684f2d41dfc7805`.
- `src/`, `Cargo.toml`, `Cargo.lock`, `README.md` and `LICENSE` are the crates.io source of 0.8.1,
  apart from the changes below. Upstream tests, benchmarks, packaging files and the Go bindings are
  not copied.

## Changes

Floating pictures without text wrapping (`wrapNone`), for example a signature drawn over a stamp:

1. `src/render/layout/section/stacker.rs`, `stack_blocks`: a `wrapNone` picture no longer moves the
   text cursor of the block. Its bottom edge is kept in a separate `overlay_bottom`, and the block
   height is `max(cursor_y, overlay_bottom)`. Text after the picture stays in place, while the
   cell or row still grows to hold the picture.
2. `src/render/layout/section/layout.rs`, `layout_section_with_clearance`: before the pictures of a
   paragraph are placed, the paragraph moves to the next column or page when its paragraph-relative
   `wrapNone` pictures do not fit above the bottom margin. This also applies to an empty anchor
   paragraph. It does not apply when the paragraph has floating shapes, other wrap modes or an
   explicit break, and never at the top of a full-height column (no endless page creation).

Tests: unit tests `wrap_none_*` in `stacker.rs`, integration tests `tests/wrap_none_signature.rs`
with synthetic documents in `test-files/wrap-none-*.docx` (generator: `tests/make_wrap_none_fixture.py`).
carbotek checks the result end to end in `tests/regression/test_pdf_floating_images.py`.

```sh
cargo test --manifest-path native/vendor/dxpdf/Cargo.toml --lib wrap_none
cargo test --manifest-path native/vendor/dxpdf/Cargo.toml --test wrap_none_signature
```

## Updating or dropping the copy

1. Take the new upstream release (crates.io source), copy the tests and fixtures listed above and run
   them on the unmodified source. If they pass, upstream has fixed the problem: remove this
   directory and the `pdf` feature, and depend on the `dxpdf` package again.
2. Otherwise re-apply both changes by hand, keeping the conditions described above, and run the
   tests, `cargo clippy` and the carbotek test suite.
3. Update the base version and commit in this file.
