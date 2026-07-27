#!/usr/bin/env python3
"""Fail on common publication hazards in version-controlled files."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAX_FILE_BYTES = 2 * 1024 * 1024
PATTERNS = {
    "cloud access key": re.compile(r"AKIA[0-9A-Z]{16}"),
    "GitHub token": re.compile(r"gh[opusr]_[A-Za-z0-9]{20,}"),
    "model-provider token": re.compile(r"sk-[A-Za-z0-9_-]{24,}"),
    "private key header": re.compile("BEGIN [A-Z ]+" + "PRIVATE KEY"),
    "machine-specific path": re.compile(r"/(?:Users|Volumes)/[^\s'\"<>]+"),
    "Unix home path": re.compile(r"/home/[a-z][a-z0-9_-]*/"),
}


def candidate_paths() -> list[Path]:
    completed = subprocess.run(
        [
            "git",
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
        ],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        check=True,
    )
    return [
        ROOT / item.decode("utf-8") for item in completed.stdout.split(b"\0") if item
    ]


def main() -> None:
    errors: list[str] = []
    for path in candidate_paths():
        relative = path.relative_to(ROOT).as_posix()
        if path.is_symlink():
            errors.append(f"symlink is not release-safe: {relative}")
            continue
        if not path.is_file():
            continue
        data = path.read_bytes()
        if len(data) > MAX_FILE_BYTES:
            errors.append(f"oversized file: {relative} ({len(data)} bytes)")
        if b"\0" in data:
            errors.append(f"binary file requires explicit review: {relative}")
            continue
        text = data.decode("utf-8", "replace")
        for label, pattern in PATTERNS.items():
            if pattern.search(text):
                errors.append(f"{label}: {relative}")
    if errors:
        for error in sorted(errors):
            print("ERROR:", error)
        raise SystemExit(1)
    print(f"Release audit passed for {len(candidate_paths())} files.")


if __name__ == "__main__":
    main()
