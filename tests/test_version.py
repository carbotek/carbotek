"""carbotek.__version__ comes from the installed distribution, whose version maturin takes from Cargo.toml."""

import re
import tomllib
from importlib.metadata import version
from pathlib import Path

import carbotek


def test_version_is_the_distribution_version() -> None:
    assert carbotek.__version__ == version("carbotek")


def test_distribution_version_is_the_crate_version() -> None:
    cargo = tomllib.loads((Path(__file__).resolve().parents[1] / "native" / "Cargo.toml").read_text(encoding="utf-8"))
    normalized = re.sub(
        r"-(alpha|beta|rc)\.(\d+)$",
        lambda match: {"alpha": "a", "beta": "b", "rc": "rc"}[match[1]] + match[2],
        cargo["package"]["version"],
    )
    assert version("carbotek") == normalized
