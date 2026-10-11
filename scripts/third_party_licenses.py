"""Write THIRD_PARTY_LICENSES.md for the Rust crates linked into carbotek._native.

    python scripts/third_party_licenses.py

Run it after changing native/Cargo.lock. Procedural macros and build scripts are not linked, so
they are left out. For a dual license the MIT (or Boost) text is used; the license text comes from
the crate sources in the local cargo registry, which `cargo fetch` populates.
"""

import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Preferred license of a multi-licensed crate, and the files that hold each license text.
_PREFERENCE = ("MIT", "BSD-3-Clause", "Zlib", "Unicode-3.0", "Apache-2.0", "BSL-1.0", "Unlicense")
_FILES = {
    "MIT": (
        "LICENSE-MIT",
        "LICENSE.MIT",
        "LICENSE-MIT.md",
        "LICENSE-MIT.txt",
        "LICENSE",
        "LICENSE.md",
        "LICENSE.txt",
        "COPYING",
    ),
    "Apache-2.0": ("LICENSE-APACHE", "LICENSE-APACHE.md", "LICENSE-APACHE.txt", "LICENSE", "LICENSE.md", "LICENSE.txt"),
    "BSD-3-Clause": ("LICENSE-BSD", "LICENSE.BSD-3-Clause", "LICENSE-BSD.md", "LICENSE", "LICENSE.md", "LICENSE.txt"),
    "Zlib": ("LICENSE-ZLIB", "LICENSE-ZLIB.md", "LICENSE", "LICENSE.md"),
    "Unicode-3.0": ("LICENSE", "LICENSE.txt", "LICENSE.md"),
    "BSL-1.0": ("LICENSE-BOOST", "LICENSE-BSL", "LICENSE"),
    "Unlicense": ("UNLICENSE", "LICENSE"),
}
# Crates published without a license file: the text of their sibling crate (same authors, same license).
_TEXT_FROM = {"alloc-stdlib": "alloc-no-stdlib"}
# Path dependencies have no registry source.
_PATH_CRATES = {"dxpdf": ROOT / "native" / "vendor" / "dxpdf"}
# C/C++ libraries compiled into Skia by skia-bindings (embed-freetype, textlayout) and linked statically.
_NATIVE_LIBRARIES = (
    ("Skia", "BSD-3-Clause", "Skia-BSD-3-Clause.txt"),
    ("FreeType", "FreeType License (FTL)", "FreeType-FTL.txt"),
    ("HarfBuzz", "MIT (Old MIT)", "HarfBuzz-MIT.txt"),
    ("ICU", "Unicode-3.0", "ICU-Unicode-3.0.txt"),
    ("libpng", "libpng-2.0", "libpng.txt"),
    ("zlib", "Zlib", "zlib.txt"),
    ("libjpeg-turbo", "IJG AND BSD-3-Clause AND Zlib", "libjpeg-turbo.txt"),
    ("libwebp", "BSD-3-Clause", "libwebp-BSD-3-Clause.txt"),
    ("Expat", "MIT", "expat-MIT.txt"),
    ("WOFF2", "MIT", "woff2-MIT.txt"),
)


def chosen_licenses(expression: str) -> list[str]:
    """Licenses whose texts must be shipped: all terms of an AND, the preferred term of an OR."""
    expression = expression.replace("(*)", "").replace("/", " OR ").strip()
    if " AND " in expression:
        return [term.strip("() ") for term in expression.split(" AND ")]
    terms = [term.strip("() ") for term in expression.split(" OR ")]
    return [next((name for name in _PREFERENCE if name in terms), terms[0])]


def linked_crates() -> list[tuple[str, str, str]]:
    cargo = shutil.which("cargo") or str(Path.home() / ".cargo" / "bin" / "cargo")
    output = subprocess.run(  # noqa: S603 - fixed arguments, cargo from PATH or ~/.cargo
        [
            cargo,
            "tree",
            "-e",
            "normal,no-proc-macro",
            "--target",
            "all",
            "--features",
            "pdf",
            "--prefix",
            "none",
            "--format",
            "{p}|{l}",
        ],
        cwd=ROOT / "native",
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    crates: set[tuple[str, str, str]] = set()
    for line in output.splitlines():
        if "|" not in line:
            continue
        package, _, license_expression = line.partition("|")
        name, version = package.split()[:2]
        if name != "carbotek-native":
            crates.add((name, version.removeprefix("v"), license_expression.strip()))
    return sorted(crates)


# Texts for crates whose package has no license file, taken from their repositories.
_OVERRIDES = ROOT / "licenses" / "crates"


def license_text(name: str, version: str, license_name: str) -> str | None:
    override = _OVERRIDES / f"{name}.txt"
    if override.is_file():
        return override.read_text(encoding="utf-8").strip()
    files = _FILES.get(license_name, ("LICENSE",))
    roots = [_PATH_CRATES[name]] if name in _PATH_CRATES else []
    registry = Path(os.environ.get("CARGO_HOME", Path.home() / ".cargo")) / "registry" / "src"
    roots += [index / f"{name}-{version}" for index in sorted(registry.iterdir())]
    if name in _TEXT_FROM:
        roots += sorted(
            path for index in sorted(registry.iterdir()) for path in index.glob(f"{_TEXT_FROM[name]}-[0-9]*")
        )
    for root in roots:
        for file_name in files:
            path = root / file_name
            if path.is_file():
                return path.read_text(encoding="utf-8").strip()
    return None


def main() -> None:
    sections = [
        "# Third-party licenses",
        "",
        "The native extension `carbotek._native` statically links these Rust crates (with the `pdf` feature).",
    ]
    missing: list[str] = []
    for name, version, expression in linked_crates():
        chosen = chosen_licenses(expression)
        sections += ["", f"## {name} {version}", "", f"License: {expression} (used here under {' AND '.join(chosen)})."]
        for license_name in dict.fromkeys(chosen):
            text = license_text(name, version, license_name)
            if text is None:
                missing.append(f"{name} {version} ({license_name})")
                continue
            sections += ["", "```text", text, "```"]
    sections += [
        "",
        "# Native libraries",
        "",
        "The PDF engine (dxpdf, `native/vendor/dxpdf`) links Skia, which compiles in these libraries:",
        "",
    ]
    sections += [f"- {name}: {spdx} (`licenses/{file}`)" for name, spdx, file in _NATIVE_LIBRARIES]
    sections += [
        "",
        "# Bundled data",
        "",
        "These data files in `src/carbotek/formatters` were generated from third-party sources:",
        "",
        "- `dayjs_date_locale.json.gz`, `dayjs_relative_time.json.gz`: locale data of Day.js 1.11.23,",
        "  Copyright (c) 2018-present, iamkun, MIT License (`licenses/dayjs-MIT.txt`).",
        "- `timezone_names.json`, `region_names.json`: Unicode CLDR data as exposed by ICU",
        "  through Node.js `Intl`, Copyright (c) Unicode, Inc., Unicode License v3 (`licenses/Unicode-3.0.txt`).",
        "- `currencies.json`, `locale_currency.json`, `locale_numbers.json`: currency and locale tables based on those of",
        "  Carbone v3.2.3, Apache License 2.0 (`licenses/Carbone-Apache-2.0.txt`, see NOTICE).",
    ]
    if missing:
        listed = "".join(f"\n  {item}" for item in missing)
        raise SystemExit(f"No license text (run cargo fetch in native/, or add licenses/crates/<name>.txt):{listed}")
    (ROOT / "THIRD_PARTY_LICENSES.md").write_text("\n".join(sections) + "\n", encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
