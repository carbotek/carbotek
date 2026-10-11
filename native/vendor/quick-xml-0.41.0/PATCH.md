# dxpdf patches

This is a vendored copy of [quick-xml 0.41.0](https://crates.io/crates/quick-xml/0.41.0)
(MIT-licensed; see `LICENSE-MIT.md`), patched for `dxpdf` and pinned in the
workspace root's `Cargo.toml` via:

```toml
[patch.crates-io]
quick-xml = { path = "vendor/quick-xml-0.41.0" }
```

Two independent patches live here, both in `src/de/map.rs` (plus one trait
addition in `src/de/mod.rs` for the first). Each is documented at its own
site with a `dxpdf patch (see PATCH.md)` comment; this file is the "why."

## What this patch does *not* fix (applies to both patches below)

`cargo publish` strips `[patch]` sections entirely — a published crate
always resolves its dependencies from the real registry, never from a local
or git-path patch. Concretely:

- **Covered**: every build rooted at *this* repository's own `Cargo.toml` —
  the `dxpdf` CLI binary, `cargo test`, CI, the Python wheels (built by CI
  running inside this repo), the `.deb` package, and the Go bindings'
  `staticlib` (`cargo build --features capi`).
- **Not covered**: `cargo install dxpdf` (installs the published,
  patch-stripped package) and, more importantly, anyone depending on
  `dxpdf = "…"` as a **Rust library** from their own `Cargo.toml` — their
  build resolves the real, unpatched quick-xml 0.41.0, and both bugs below
  reappear for them, with no indication anything here was ever patched.

This gap was weighed and accepted, not missed — see the project's own
review history (`git log -- vendor/, src/docx/`) for the alternatives
considered for the first patch (a general or narrow preprocessing elision
pass, a try-then-fallback hybrid) and why patching quick-xml was chosen as
the operative fix over them despite the gap. The second patch below
replaced a preprocessing workaround outright (`src/docx/whitespace_workaround.rs`,
now deleted) on the same reasoning, once the first patch had already proven
the approach out.

## Patch 1: namespace-qualified field matching

### Why

`quick_xml::de`'s serde support matches a struct field against an XML
element by **local name only** — the part after `:` — discarding the
namespace entirely (`de::key::QNameDeserializer::from_elem` /
`de::map::not_in`, both keyed off `BytesStart::local_name()`). That's fine
for the overwhelming majority of `dxpdf`'s `XxxXml` schemas, where every
child genuinely lives in one namespace (`w:`) and the producer's own choice
of prefix for it is irrelevant. It breaks the moment two elements from
**different** namespaces share a local name and a struct wants to
distinguish them.

That happened for real:
[`test-cases/joern.hendrich@vdwbayern.de.docx`](../../test-cases) carries a
numbering level's `<w:rPr>` with both the ordinary `<w:shadow w:val="0"/>`
boolean toggle (§17.3.2.37) and, non-adjacent to it, a Word-2010
`<w14:shadow>` — a WordArt-style text-effect extension `dxpdf` doesn't model
at the run level. Both matched `RPrXml`'s `shadow` field by local name, and
because they weren't adjacent, quick-xml reported the second as a duplicate
rather than folding it into the `Vec` — the whole document failed to parse.

We looked for a way to solve this **without** patching quick-xml first:

- A custom `Deserialize` for the colliding struct only ever sees a generic
  `D: serde::Deserializer<'de>` (it's reached through the parent's ordinary
  derive composition) — no raw XML text, no reader, nothing to build a
  namespace-aware view from. Confirmed for both quick-xml 0.41 and the
  latest 0.42 (which added `Deserializer::resolver()`/`resolver_mut()` —
  inherent methods on the *concrete* `Deserializer<'de, R, E>` struct, so
  unreachable from generic composed code either way).
- quick-xml's own `$value`/enum catch-all, empirically checked, *also*
  collapses to local name only — no escape hatch there either.
- quick-xml has no built-in way to capture a child element's raw XML text
  for a later, separate top-level parse that could hold the concrete
  `Deserializer` and use its namespace resolver.

So within `quick_xml::de`'s "one document-wide `from_str`/`from_reader`
call, composed via derive" architecture, there is no way to make any nested
type's parsing namespace-aware without changing something inside
`quick_xml::de` itself — this patch is not a workaround chosen for
convenience, it's the only point where a namespace check can happen at all.

### What changed

Both `SliceReader` (backing `Deserializer::from_str`) and `IoReader`
(backing `Deserializer::from_reader`) already wrap a real `NsReader`
internally — used today only for `xsi:nil` resolution (`has_nil_attr`). The
patch:

1. **`src/de/mod.rs`** — adds `resolve_element_namespace` to the `XmlRead`
   trait, implemented for both readers by delegating to the same
   `NsReader::resolver()` that `has_nil_attr` already uses, now via
   `resolve_element`.
2. **`src/de/map.rs`** — in `ElementMapAccess::next_key_seed`, before
   falling through to the ordinary local-name-only
   `QNameDeserializer::from_elem`, resolves the current element's
   namespace-qualified name in Clark notation (`{uri}local`) and checks
   whether it's one of the struct's own declared field names. If so, that
   qualified string is handed to serde as the key instead of the bare local
   name — so a field opts in to namespace-qualified matching purely by
   writing its `#[serde(rename = "...")]` in Clark notation.

Every other field, in every other `XxxXml` schema in this crate, is
completely unaffected: its rename is a plain local name, which the
qualified string is never equal to, so `next_key_seed` falls through to the
exact upstream behavior for it. `dxpdf`'s only use of this today is
`RPrXml::w14_shadow` (`src/docx/parse/properties/schema/run.rs`):

```rust
#[serde(
    rename = "{http://schemas.microsoft.com/office/word/2010/wordml}shadow",
    default
)]
w14_shadow: Vec<serde::de::IgnoredAny>,
```

Matching is by **resolved namespace URI**, not by literal prefix text — a
producer binding the Word-2010 namespace to some prefix other than the
conventional `w14` still routes correctly (pinned by
`w14_shadow_extension_matches_by_namespace_not_by_prefix_spelling` in
`run.rs`'s own test module). This is a real capability upgrade over prefix
string-matching (which is what `crate::docx::parse::body::mc_requires`
does, for comparison, for `mc:Choice`'s `Requires` list — a narrower,
already-accepted simplification elsewhere in this codebase that this patch
does not need to make).

## Patch 2: `xml:space="preserve"` is respected

### Why

`ElementMapAccess::skip_whitespaces` (`src/de/map.rs`) unconditionally
consumed and discarded any blank `Text` event standing between an element's
attributes and its next child/content — used every time the deserializer
looks for the next struct field, including a struct's own `$text` field. It
carried its own upstream `TODO: respect the xml:space attribute` right at
the call site, confirming this was a known, unaddressed gap, not something
we found by guessing.

Concretely, for `dxpdf`'s `TextXml` (`<w:t xml:space="preserve"> </w:t>`,
`src/docx/parse/body_schema.rs` — an `@xml:space` attribute field alongside
a `$text` content field), the *only* content is one space character. That
single character is itself the blank `Text` event `skip_whitespaces` eats
— so by the time `next_key_seed` gets to check for a `$text` key, the event
is already gone, and `content` deserializes to `""`. Reproduced directly
against this crate in isolation before touching anything (`TextXml { space:
Some("preserve"), content: "" }` — the attribute parses fine, the loss is
specifically the content). This is exactly the symptom the deleted
`src/docx/whitespace_workaround.rs` described: a `<w:r>` that is only a
preserved space between two other runs renders as if the space were never
there (`Label:Value` instead of `Label: Value`), and §22.1 Office Math hits
the same thing spacing `<m:t xml:space="preserve"> </m:t>` between operators
(`a+b` instead of `a + b`).

### What changed

`ElementMapAccess::skip_whitespaces` now checks whether `self.start` — the
element whose children/content are currently being scanned — carries a
direct `xml:space="preserve"` attribute, and if so, returns without
consuming anything, leaving the blank `Text` event for `next_key_seed` to
route to `$text`/`$value` normally. The check (`has_xml_space_preserve`) is
by literal attribute name (`xml:space`), not namespace resolution — `xml:`
is a reserved prefix that "cannot be redeclared or unbound" per this
crate's own comment in `key::QNameDeserializer::from_attr` just above,
which already relies on the same fact for the same prefix.

This checks only `self.start`'s own attribute, not inherited state from an
ancestor's `xml:space` (full XML §2.10 scoping, which is a proper stack —
the attribute inherits down the tree unless a descendant overrides it).
Every producer this was written against (Word, LibreOffice) declares
`xml:space="preserve"` directly on the element whose own text must be
preserved (confirmed by `dxpdf`'s own `TextXml` schema, which already reads
`@xml:space` off `<w:t>` itself) — never relies on inheriting it from an
ancestor. A container element with no `xml:space` of its own (the
overwhelmingly common case — a `<w:r>` with ordinary pretty-printed
indentation between its children, say) is completely unaffected: this still
eats that insignificant inter-tag whitespace exactly as upstream, verified
directly in the isolated reproduction alongside the fix itself.

Superseded `src/docx/whitespace_workaround.rs` entirely (deleted, along
with its call sites in `src/docx/parse/body.rs`, `src/docx/parse/math.rs`,
and the preprocessing pass in `src/docx/zip.rs`) — that module's own byte
substitution is no longer needed once the real content survives parsing
intact. The regression tests it existed to satisfy
(`whitespace_only_run_with_an_alternate_transitional_wml_prefix_roundtrips`
and its strict-namespace sibling, `tests/integration.rs`) are unchanged and
still pass — they test observable behavior end to end, not the mechanism.

## Maintaining this fork

Pinned to exactly 0.41.0; does **not** track upstream releases
automatically. Upgrading `quick-xml`'s version here means re-applying both
patches (each diff is small and self-contained — `git diff` against a fresh
copy of the target version's `src/de/mod.rs` and `src/de/map.rs` should make
the rebase mechanical) or upstreaming them as real PRs — patch 1 against
[quick-xml#218](https://github.com/tafia/quick-xml/issues/218), an already
open, unaddressed feature request for exactly that capability; patch 2
against the `TODO` it already carried upstream — after which this vendored
copy could be dropped in favor of a stock upstream release.

`tests` and `benches` were removed from this vendored copy (never part of
the crates.io package's `include` list anyway — irrelevant to building
`dxpdf`, which only uses this as a build dependency), so don't expect
`cargo test -p quick-xml` to do anything useful from inside this directory.
`cargo check --features serialize` (or `clippy`) does work and is how both
patches were verified in isolation before being wired into the main
project.
