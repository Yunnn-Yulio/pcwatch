"""Build the source-only release ZIP using an explicit file allowlist."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import re
import zipfile


ROOT = Path(__file__).resolve().parents[1]
LAUNCHERS = (
    "Run.cmd",
    "Stop.cmd",
    "Enable-startup.cmd",
    "Disable-startup.cmd",
    "Open-logs.cmd",
)
MODULES = (
    "__init__.py",
    "__main__.py",
    "app.py",
    "windows.py",
    "startup.py",
    "metrics.py",
    "events.py",
    "storage.py",
)


def package_files(root: Path) -> list[Path]:
    """Select application source and public documentation, never runtime data."""
    paths = [
        root / "README.md",
        root / "LICENSE",
        root / "CHANGELOG.md",
        root / "PCWatch.pyw",
        root / "docs" / "architecture.md",
    ]
    paths.extend(root / "pcwatch" / name for name in MODULES)
    paths.extend(root / "scripts" / name for name in LAUNCHERS)
    if not (root / "pcwatch" / "__init__.py").is_file():
        raise FileNotFoundError("The pcwatch package is missing")
    for path in paths:
        if not path.is_file() or path.is_symlink():
            raise FileNotFoundError(f"Expected regular package file: {path.name}")
        if not path.resolve().is_relative_to(root.resolve()):
            raise ValueError(f"Package file points outside the source folder: {path.name}")
    return sorted(paths, key=lambda path: path.relative_to(root).as_posix())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "artifacts")
    args = parser.parse_args()
    source = (ROOT / "pcwatch" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*[\"\']([^\"\']+)[\"\']', source, re.MULTILINE)
    if not match or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", match.group(1)):
        raise ValueError("The package must declare a semantic __version__")
    paths = package_files(ROOT)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    archive = args.output_dir / f"PCWatch-{match.group(1)}-python.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as bundle:
        for path in paths:
            info = zipfile.ZipInfo(path.relative_to(ROOT).as_posix(), (1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            bundle.writestr(info, path.read_bytes(), compresslevel=9)
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix(archive.suffix + ".sha256").write_text(
        f"{checksum}  {archive.name}\n", encoding="utf-8", newline="\n"
    )
    print(f"Created {archive.name} ({len(paths)} files)")
    print(f"SHA-256: {checksum}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
