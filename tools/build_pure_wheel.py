"""Build a universal wheel containing the Python renderer without the native module."""

import argparse
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

from release_version import versions


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    version = next(
        line.split('"')[1]
        for line in (root / "native" / "Cargo.toml").read_text(encoding="utf-8").splitlines()
        if line.startswith('version = "')
    )
    version = versions(version)[1]
    output = args.out.resolve()
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="carbotek-pure-") as temp:
        stage = Path(temp)
        shutil.copytree(
            root / "src" / "carbotek",
            stage / "src" / "carbotek",
            ignore=shutil.ignore_patterns("*.pyd", "*.so", "*.pdb", "*.dll", "*.pyc", "__pycache__"),
        )
        for name in ("README.md", "LICENSE", "NOTICE", "THIRD_PARTY_LICENSES.md"):
            shutil.copy2(root / name, stage / name)
        shutil.copytree(root / "licenses", stage / "licenses")
        package_data: dict[str, list[str]] = {}
        package_root = stage / "src" / "carbotek"
        for directory in (package_root, *package_root.rglob("*")):
            if not directory.is_dir() or not (directory / "__init__.py").exists():
                continue
            package = ".".join(directory.relative_to(stage / "src").parts)
            package_data[package] = [
                item.name
                for item in directory.iterdir()
                if item.is_file() and item.suffix not in {".py", ".pyd", ".so", ".pdb", ".dll", ".pyc"}
            ]
        metadata = {
            "name": project["name"],
            "version": version,
            "description": project["description"],
            "long_description": (stage / "README.md").read_text(encoding="utf-8"),
            "long_description_content_type": "text/markdown",
            "python_requires": project["requires-python"],
            "install_requires": project["dependencies"],
            "classifiers": project["classifiers"],
            "keywords": project["keywords"],
            "url": project["urls"]["Homepage"],
            "project_urls": project["urls"],
            "license": project["license"],
            "author": project["authors"][0]["name"],
            "author_email": project["authors"][0]["email"],
            "packages": sorted(package_data),
            "package_dir": {"": "src"},
            "package_data": package_data,
            "license_files": ["LICENSE", "NOTICE", "THIRD_PARTY_LICENSES.md", "licenses/**/*.txt"],
            "entry_points": {"console_scripts": [f"{name}={target}" for name, target in project["scripts"].items()]},
            "zip_safe": False,
        }
        (stage / "setup.py").write_text(
            "from setuptools import setup\nsetup(**" + repr(metadata) + ")\n", encoding="utf-8"
        )
        (stage / "pyproject.toml").write_text(
            '[build-system]\nrequires = ["setuptools>=80", "wheel"]\nbuild-backend = "setuptools.build_meta"\n',
            encoding="utf-8",
        )
        subprocess.run(  # noqa: S603 - fixed interpreter and arguments; only the output path is user-supplied
            [sys.executable, "-m", "build", "--wheel", "--no-isolation", "--outdir", str(output)], cwd=stage, check=True
        )


if __name__ == "__main__":
    main()
