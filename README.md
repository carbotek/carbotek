# carbotek

<p align="center">
  <a href="https://github.com/carbotek/carbotek/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/carbotek/carbotek/actions/workflows/ci.yml/badge.svg"></a>
  <a href="https://pypi.org/project/carbotek/"><img alt="PyPI" src="https://img.shields.io/pypi/v/carbotek?color=blue"></a>
  <img alt="Python" src="https://img.shields.io/badge/python-3.15%2B-3776AB?logo=python&logoColor=white">
  <img alt="Rust" src="https://img.shields.io/badge/native-Rust%201.99-CE422B?logo=rust&logoColor=white">
  <a href="https://github.com/carbotek/carbotek/blob/main/LICENSE"><img alt="License" src="https://img.shields.io/badge/license-PolyForm%20Noncommercial%201.0.0-orange"></a>
  <br>
  <a href="https://github.com/astral-sh/ruff"><img alt="Ruff" src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json"></a>
  <a href="https://github.com/astral-sh/uv"><img alt="uv" src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/uv/main/assets/badge/v0.json"></a>
  <a href="https://microsoft.github.io/pyright/"><img alt="Checked with pyright" src="https://microsoft.github.io/pyright/img/pyright_badge.svg"></a>
  <a href="https://docs.gauge.sh"><img alt="Architecture: tach" src="https://img.shields.io/badge/architecture-tach-6C47FF"></a>
  <img alt="Typed" src="https://img.shields.io/badge/typing-typed-brightgreen">
  <img alt="Carbone syntax" src="https://img.shields.io/badge/Carbone%20syntax-5.14.4-2E7D32">
</p>

**English** | [Русский](https://github.com/carbotek/carbotek/blob/main/README.ru.md)

Render DOCX and XLSX templates with Carbone-style markers (`{d.name}`, loops, conditions,
formatters, images, barcodes, HTML, hyperlinks, colors) into DOCX, XLSX or PDF from Python.
There is no LibreOffice: PDF is produced by a bundled copy of [dxpdf](https://github.com/nerdy-pro/dxpdf)
with layout fixes for floating pictures (a signature over a stamp stays in place,
see [CARBOTEK.md](https://github.com/carbotek/carbotek/blob/main/native/vendor/dxpdf/CARBOTEK.md)).

carbotek is a Python port of the DOCX and XLSX engine of [Carbone](https://github.com/carboneio/carbone)
**v3.2.3**, the last Carbone release under the Apache License 2.0. The template syntax is compatible
with Carbone **5.14.4**. Behaviour was checked against the JavaScript engine with a differential
corpus and formatter oracles.

carbotek is an independent project and is not affiliated with or endorsed by CarboneIO SAS.
"Carbone" is used only to describe compatibility.

> Licensed under the **PolyForm Noncommercial 1.0.0** license. It is free for noncommercial
> use; commercial use requires a separate license (see [LICENSE](https://github.com/carbotek/carbotek/blob/main/LICENSE)).

## Installation

```bash
pip install carbotek        # or: uv add carbotek
```

Requires CPython 3.15+. The primary target is Linux in Docker (`python:3.15-slim-trixie`); CI runs
the test suite against the released wheel inside that image.

Linux wheels (manylinux, x86_64 and aarch64, one abi3 wheel for CPython 3.15+) contain an optional native accelerator (`carbotek._native`, Rust) that renders common
loop tables and money formatting faster; the output is identical to the pure-Python path. A
source-distribution install builds the accelerator and needs a Rust toolchain (1.87+). Set `CARBOTEK_NATIVE=0` to render without it, or `CARBOTEK_NATIVE=1` to make a
missing extension an import error; the variable is read once, when `carbotek` is imported.

A universal `py3-none-any` wheel is also published for environments without Rust. Pip prefers
an available platform wheel; select the universal wheel explicitly to install only the Python
renderer. From an unpacked source distribution, build it with
`uv run --no-project --with build --with setuptools --with wheel python tools/build_pure_wheel.py --out dist`.

The PDF engine is part of the Linux wheels and does not depend on `CARBOTEK_NATIVE`. A source or
universal-wheel install has no bundled engine: install `carbotek[pdf-fallback]` to convert with the
upstream `dxpdf` package instead, without the layout fixes.

## Library

```python
import json
import carbotek

template = open("invoice.docx", "rb").read()
data = json.load(open("data.json", encoding="utf-8"))

docx = carbotek.render(template, data, {"lang": "en", "timezone": "Europe/London"})
pdf = carbotek.render_pdf(template, data, image_dpi=220)   # DOCX templates only

engine = carbotek.Engine(cache_size=16)   # own compiled-template cache
engine.render(template, data)
print(engine.metrics)                     # size, bytes, hits, misses, hit_rate
```

`carbotek.render` detects the template type: a DOCX template returns DOCX, an XLSX template
returns XLSX. The module-level functions share one default `Engine`.

### Options

Options are the keys of `RenderOptions.create`:

| Key | Meaning |
|---|---|
| `lang` | Document language (default `ru`) |
| `timezone` | IANA time zone (default `Europe/Moscow`) |
| `complement` | `{c.*}` data |
| `translations` | `{t(...)}` dictionaries |
| `enum` | Enumerations for `convEnum` |
| `currency` | `{"source", "target", "rates"}` |
| `variable_str` | Template variables |
| `use_high_precision_arithmetic` | High-precision arithmetic |
| `export_formatted_values_as_text` | XLSX: keep formatted numbers as text |

### Russian formatters

carbotek adds formatters for Russian business documents (they are not part of Carbone):

| Formatter (aliases) | Example | Result |
|---|---|---|
| `digitCurrency` (`numCurrency`, `numberCurrency`, `digitalCurrency`) | `{d.a:digitCurrency}`, a = 1234567.5 | `1 234 567,50` |
| | `{d.a:digitCurrency(0)}` | `1 234 568` |
| `formatAmount` | `{d.a:formatAmount}`, a = 1234.5 | `1 234,50` |
| `wordNumber` (`words`, `numberToWords`) | `{d.a:wordNumber}`, a = 1234 | `одна тысяча двести тридцать четыре` |
| `wordCurrency` | `{d.a:wordCurrency}`, a = 1234.56 | `Одна тысяча двести тридцать четыре рубля 56 копеек` |
| `moneyFull` (`sumFull`) | `{d.a:moneyFull}`, a = 1234.56 | `1 234,56 (Одна тысяча двести тридцать четыре рубля 56 копеек)` |
| `procent` (`percent`) | `{d.a:procent}`, a = 20 | `20,00 %` |
| `dashIfZero` (`dashZero`) | `{d.a:dashIfZero}`, a = 0 | `—` |
| `plural` (`pluralize`) | `{d.n:plural('день','дня','дней')}`, n = 5 | `5 дней` |
| | `{d.n:plural('день','дня','дней', false)}`, n = 21 | `день` |
| `ruDate` | `{d.dt:ruDate}`, dt = `2026-10-11` | `«11» октября 2026` |
| `roundHalfUp` | `{d.a:roundHalfUp(1)}`, a = 2.25 | `2.3` |
| `parseAmount` | `{d.s:parseAmount}`, s = `1 234,50` | `1234.5` |
| `parseDate` | `{d.s:parseDate}`, s = `11.10.2026` | `2026-10-11` |
| `capitalize` (`firstUpper`) | `{d.s:capitalize}`, s = `иванов` | `Иванов` |

Amount formatters take an optional precision (default 2) and round half up. `ruDate` uses the
render time zone, or the one given as an argument.

### Errors

All template and data errors are subclasses of `carbotek.CarbotekError`. Each error has a stable
`code`:

| Code | Error |
|---|---|
| `VALIDATION_ERROR` | Invalid input |
| `TEMPLATE_ERROR` | Invalid template, marker or formatter |
| `FEATURE_UNAVAILABLE` | Unsupported feature |
| `CONVERTER_ERROR` | PDF conversion failed |

Messages follow the JS engine where applicable; input and infrastructure failures have library-specific messages.

### Images from URLs and files

By default, only data URIs are loaded. Remote and local images have to be enabled explicitly:

```python
from carbotek.features.fetch import ImageSourceConfig

engine = carbotek.Engine(image_sources=ImageSourceConfig(
    allowlist=("cdn.example.com",),   # hosts allowed for http(s) images; private addresses are refused
    file_root="/srv/images",          # file:// images are read only under this directory
    max_bytes=10_485_760,
    fetch_timeout_ms=5000,
    max_download_concurrency=4,
    total_timeout_ms=10000,
))
```

Without `image_sources`, the same settings are read from environment variables:

- `CARBOTEK__IMAGE_URL_ALLOWLIST` (comma-separated)
- `CARBOTEK__IMAGE_FILE_ROOT`
- `CARBOTEK__IMAGE_MAX_BYTES`
- `CARBOTEK__IMAGE_FETCH_TIMEOUT_MS`
- `CARBOTEK__IMAGE_MAX_DOWNLOAD_CONCURRENCY`
- `CARBOTEK__IMAGE_TOTAL_TIMEOUT_MS`

Environment settings are snapshotted once at the start of each render. Changes during a render affect the next call; an explicit `ImageSourceConfig` takes precedence. Image limits must be positive integers; invalid environment limits raise `ValidationError`. Explicit configuration also validates field types and copies/normalizes the allowlist.

## XLSX

XLSX templates render to XLSX the same way as in Carbone. Supported:

- markers;
- loops over rows and cells, including nested loops;
- conditions and conditional blocks;
- `drop`/`keep` (`row`, `col`, `table`);
- aggregates and variables.

The workbook is prepared as in the JS engine:

- `calcChain.xml` is removed;
- shared strings become inline strings;
- a cell whose only content is a `:formatN` marker becomes numeric. To keep it as text, use the `export_formatted_values_as_text` option or `{o.exportFormattedValuesAsText=true}`.

Markers work anywhere in the workbook: sheet names, headers and footers, defined names, comments.

- **Pictures:** put a marker in the alt text of a sheet picture, for example `{d.logo}` or `{d.code:barcode(ean13)}`.
- **Hyperlinks:** a hyperlink target `https://d.site` becomes `{d.site:validateURL}`.
- **Not recalculated:** formulas, `dimension` and merged cells.
- **No PDF output:** PDF from XLSX raises `FeatureUnavailable`.

## CLI

```bash
carbotek render template.docx data.json -o out.docx
carbotek render template.xlsx data.json                    # -> template.out.xlsx
carbotek render template.docx data.json --pdf -o out.pdf --image-dpi 300
carbotek bench  template.docx data.json -n 50              # warm cache: cold, mean, p50, p95, min, max, per second
carbotek bench  template.docx data.json --no-cache --json   # reset module caches and registry for each render
carbotek markers template.docx                             # markers per part after validated compilation
```

`python -m carbotek ...` works too.

Common flags:

- `--lang`, `--timezone`;
- `--options '{"complement": {"company": "X"}}'`;
- `-` instead of the data file reads data from stdin.

## Using it in a web service / task queue

The API is synchronous and CPU-bound. It takes bytes and returns bytes, and does no I/O except
the optional image downloads.

- **Create one `Engine` per process** and reuse it, so compiled templates are cached. `Engine` is thread-safe.
- **Do not call it on the event loop.**
  - In an async framework, use `await asyncio.to_thread(engine.render, template, data, options)`.
  - For a task queue (for example taskiq or Celery), a synchronous task with process-based workers scales best, because rendering holds the GIL.
- **Map `CarbotekError` to client errors (4xx)** by its `code`. Any other exception is a server error.
- **Install the template fonts in the container** (or metric-compatible fonts such as Carlito/Caladea). PDF uses system fonts.

```python
import asyncio
import carbotek

engine = carbotek.Engine(cache_size=64)

async def render_document(template: bytes, data: dict, pdf: bool) -> bytes:
    if pdf:
        return await asyncio.to_thread(engine.render_pdf, template, data)
    return await asyncio.to_thread(engine.render, template, data)
```

## Limitations

- **ECharts charts are not supported.** A `{d.x:chart}` marker raises `FeatureUnavailable`. Native Word/Excel charts are updated with the data.
- **PDF has no bookmark outline and no document information** (title, author).
- **Native OOXML charts are not drawn in PDF.** Their data in the DOCX is still updated.
- **WMF/EMF images are drawn only partially in PDF.** A warning is logged.
- **The table of contents is not recalculated.** Page numbers come from the template.
- **An even/odd-page section break acts like `nextPage`.**

## Differences from Carbone

Deliberate, documented differences from the JavaScript engine:

- A `{o.*}` option split across runs by Word is applied (Carbone leaves it in the text).
- An object or array printed by a marker uses JavaScript string conversion (`[object Object]`, `1,2`).
- A named sort combined with a repeater renders (Carbone fails to compile that template).
- An empty native chart series is written as `ptCount=0`, as OOXML requires.
- Items without a named sort key keep their input order.
- Each picture keeps its own frame size when several pictures use the same image marker (Carbone
  applies the size of the last of them to all).
- Native wheels are built only for Linux; other platforms install the pure-Python wheel, which is not tested there.

## Development

```bash
uv sync                      # builds the native extension; needs the Rust toolchain from rust-toolchain.toml
                             # (without the PDF engine: PDF tests then use the dxpdf package)
uv run ruff check . && uv run ruff format --check .
uv run pyright               # strict mode, per-file headers
uv run tach check            # layer boundaries, see tach.toml
uv run pytest -n auto
```

Layers, from top to bottom: `cli` → `api` → `pipeline`, `opc`, `pdf` → `features`, `xlsx` → `docx`
→ `markers`, `formatters` → `package`, `ooxml`, `options`. A module may import only lower layers;
`tach check` enforces it.

Security issues: see [SECURITY.md](https://github.com/carbotek/carbotek/blob/main/SECURITY.md).

## License

carbotek is licensed under the [PolyForm Noncommercial License 1.0.0](https://github.com/carbotek/carbotek/blob/main/LICENSE).
Copyright (c) 2026 Carbotek.

It is derived from Carbone v3.2.3, Copyright (c) CarboneIO SAS and contributors, licensed under
the [Apache License 2.0](https://github.com/carbotek/carbotek/blob/main/licenses/Carbone-Apache-2.0.txt). The original code was translated from
JavaScript to Python and Rust and substantially modified. See [NOTICE](https://github.com/carbotek/carbotek/blob/main/NOTICE) and
[THIRD_PARTY_LICENSES.md](https://github.com/carbotek/carbotek/blob/main/THIRD_PARTY_LICENSES.md).
