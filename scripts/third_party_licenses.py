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
# license expression -> (chosen license, license file names to try)
_CHOICES = {
    "MIT OR Apache-2.0": ("MIT", ("LICENSE-MIT", "LICENSE")),
    "Apache-2.0 OR MIT": ("MIT", ("LICENSE-MIT", "LICENSE")),
    "Apache-2.0 OR BSL-1.0": ("BSL-1.0", ("LICENSE-BOOST", "LICENSE-BSL")),
}


def linked_crates() -> list[tuple[str, str, str]]:
    cargo = shutil.which("cargo") or str(Path.home() / ".cargo" / "bin" / "cargo")
    output = subprocess.run(  # noqa: S603 - fixed arguments, cargo from PATH or ~/.cargo
        [cargo, "tree", "-e", "normal,no-proc-macro", "--target", "all", "--prefix", "none", "--format", "{p}|{l}"],
        cwd=ROOT / "native",
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    crates: set[tuple[str, str, str]] = set()
    for line in output.splitlines():
        package, _, license_expression = line.partition("|")
        name, version = package.split()[:2]
        if name != "carbotek-native":
            crates.add((name, version.removeprefix("v"), license_expression.strip()))
    return sorted(crates)


def license_text(name: str, version: str, files: tuple[str, ...]) -> str:
    registry = Path(os.environ.get("CARGO_HOME", Path.home() / ".cargo")) / "registry" / "src"
    for index in sorted(registry.iterdir()):
        for file_name in files:
            path = index / f"{name}-{version}" / file_name
            if path.is_file():
                return path.read_text(encoding="utf-8").strip()
    raise SystemExit(f"No {files} for {name} {version}: run cargo fetch in native/")


def main() -> None:
    sections = [
        "# Third-party licenses",
        "",
        "The native extension `carbotek._native` statically links these Rust crates.",
    ]
    for name, version, expression in linked_crates():
        chosen, files = _CHOICES[expression]
        sections += [
            "",
            f"## {name} {version}",
            "",
            f"License: {expression} (used here under {chosen}).",
            "",
            "```text",
        ]
        sections += [license_text(name, version, files), "```"]
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
    (ROOT / "THIRD_PARTY_LICENSES.md").write_text("\n".join(sections) + "\n", encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
