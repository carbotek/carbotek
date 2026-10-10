"""Translate supported release tags to Cargo SemVer and Python distribution versions."""

import argparse
import re
import sys
import tomllib
from pathlib import Path


def versions(value: str) -> tuple[str, str]:
    match = re.fullmatch(
        r"v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:(a|b|rc)(0|[1-9]\d*)|-(alpha|beta|rc)\.(0|[1-9]\d*))?", value
    )
    if match is None:
        raise ValueError(f"Unsupported release version: {value!r}")
    base = ".".join(match.group(index) for index in (1, 2, 3))
    stage = match[4] or {"alpha": "a", "beta": "b", "rc": "rc", None: ""}[match[6]]
    number = match[5] or match[7] or ""
    cargo_stage = {"a": "alpha", "b": "beta", "rc": "rc", "": ""}[stage]
    return (base + (f"-{cargo_stage}.{number}" if stage else ""), base + stage + number)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", nargs="?")
    parser.add_argument("--format", choices=("cargo", "python"), default="cargo")
    parser.add_argument("--write-cargo", action="store_true")
    args = parser.parse_args()
    manifest = Path(__file__).resolve().parents[1] / "native" / "Cargo.toml"
    text = manifest.read_text(encoding="utf-8")
    value = args.version or tomllib.loads(text)["package"]["version"]
    try:
        cargo, python = versions(value)
    except ValueError as error:
        parser.error(str(error))
    if args.write_cargo:
        text, count = re.subn(r'^version = "[^"\n]+"$', f'version = "{cargo}"', text, count=1, flags=re.MULTILINE)
        if count != 1:
            parser.error("Cargo package version not found")
        manifest.write_text(text, encoding="utf-8")
    sys.stdout.write((cargo if args.format == "cargo" else python) + "\n")


if __name__ == "__main__":
    main()
