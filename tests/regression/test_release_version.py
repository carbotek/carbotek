"""Release tags must produce valid Cargo and matching Python versions."""

import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "value,cargo,python",
    [
        ("v1.2.3", "1.2.3", "1.2.3"),
        ("v1.2.3rc1", "1.2.3-rc.1", "1.2.3rc1"),
        ("v1.2.3-rc.1", "1.2.3-rc.1", "1.2.3rc1"),
        ("1.2.3a0", "1.2.3-alpha.0", "1.2.3a0"),
        ("1.2.3-beta.2", "1.2.3-beta.2", "1.2.3b2"),
    ],
)
def test_release_version_translation(value: str, cargo: str, python: str) -> None:
    script = Path(__file__).resolve().parents[2] / "tools" / "release_version.py"
    for output_format, expected in (("cargo", cargo), ("python", python)):
        result = subprocess.run(  # noqa: S603 - fixed interpreter, script and fixture arguments; no shell
            [sys.executable, str(script), value, "--format", output_format], capture_output=True, text=True, check=True
        )
        assert result.stdout.strip() == expected


@pytest.mark.parametrize("value", ["1.2", "01.2.3", "1.2.3rc01", "1.2.3-preview.1", "1.2.3;echo bad"])
def test_invalid_release_tag_is_rejected(value: str) -> None:
    script = Path(__file__).resolve().parents[2] / "tools" / "release_version.py"
    result = subprocess.run(  # noqa: S603 - fixture arguments, no shell
        [sys.executable, str(script), value], capture_output=True, text=True, check=False
    )
    assert result.returncode != 0
    assert "Unsupported release version" in result.stderr
