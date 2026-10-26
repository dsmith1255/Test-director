"""Create a portable source handoff without build or private user data."""
from __future__ import annotations

from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo


ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "dist" / "WIMNA-Test-Director-source.zip"
ROOT_FILES = (
    ".gitignore", "README.md", "START_HERE.txt", "campaign.template.json",
    "assumptions.example.json", "pyproject.toml", "requirements-build.txt",
    "requirements-test.txt", "overnight.queue.example.json",
    "Build-Release.ps1", "launch_gui.py", "package_source.py",
)
SOURCE_DIRS = ("wimna", "tests", "docs", "ac_app", ".github")
EXCLUDED_NAMES = {"__pycache__", ".pytest_cache", "wimna-director.exe"}
EXCLUDED_SUFFIXES = {".pyc", ".pyo", ".zip"}


def main() -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    files = [ROOT / name for name in ROOT_FILES]
    for directory in SOURCE_DIRS:
        files.extend(path for path in (ROOT / directory).rglob("*") if path.is_file())
    files = sorted(path for path in files if path.is_file()
                   and not any(part in EXCLUDED_NAMES for part in path.relative_to(ROOT).parts)
                   and path.suffix.lower() not in EXCLUDED_SUFFIXES)
    with ZipFile(OUTPUT, "w", compression=ZIP_DEFLATED, compresslevel=9) as archive:
        for path in files:
            name = path.relative_to(ROOT).as_posix()
            item = ZipInfo(name, (2026, 10, 20, 0, 0, 0))
            item.compress_type = ZIP_DEFLATED
            item.external_attr = 0o644 << 16
            archive.writestr(item, path.read_bytes(), compress_type=ZIP_DEFLATED, compresslevel=9)
    print(f"Source archive: {OUTPUT} ({len(files)} files)")


if __name__ == "__main__":
    main()
