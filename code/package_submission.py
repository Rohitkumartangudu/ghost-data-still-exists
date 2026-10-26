"""Build code.zip at the repository root: README.md + code/, excluding caches, virtual environments, secrets,
the dataset and nested archives (README "Packaging code.zip" rules). Fails if anything looks like an API key.

Usage: python code/package_submission.py
"""
from __future__ import annotations

import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXCLUDE_DIRS = {".cache", "__pycache__", ".venv", "venv", "node_modules", ".git", "dataset"}
EXCLUDE_SUFFIXES = {".pyc", ".zip", ".tar", ".gz", ".tgz", ".7z", ".rar"}
EXCLUDE_NAMES = {".env", ".DS_Store"}
SECRET_RE = re.compile(rb"sk-ant-[A-Za-z0-9_-]{20,}")


def files() -> list[Path]:
    out = [ROOT / "README.md"]
    for p in sorted((ROOT / "code").rglob("*")):
        rel = p.relative_to(ROOT)
        if p.is_dir() or any(part in EXCLUDE_DIRS for part in rel.parts):
            continue
        if p.suffix.lower() in EXCLUDE_SUFFIXES or p.name in EXCLUDE_NAMES:
            continue
        out.append(p)
    return out


def main():
    target = ROOT / "code.zip"
    chosen = files()
    leaks = [str(p.relative_to(ROOT)) for p in chosen if SECRET_RE.search(p.read_bytes())]
    if leaks:
        sys.exit(f"refusing to package: possible API key in {leaks}")
    required = ["code/evaluation/evaluation_report.md", "code/evaluation/usage_report.md", "code/README.md", "README.md"]
    missing = [r for r in required if not (ROOT / r).exists()]
    if missing:
        sys.exit(f"missing required files: {missing}")
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in chosen:
            zf.write(p, p.relative_to(ROOT).as_posix())
    print(f"wrote {target} ({len(chosen)} files, {target.stat().st_size / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
