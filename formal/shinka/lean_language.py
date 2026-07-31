"""Pinned ShinkaEvolve language-table extension for Lean 4."""

from __future__ import annotations

import re


def enable_lean_language() -> None:
    """Register Lean source conventions in the pinned Shinka process.

    ShinkaEvolve commit b67a073 does not yet list Lean as a language. Its
    language helpers consult module-level tables dynamically, so extending
    those tables before runner construction is sufficient and keeps the
    upstream installation outside this repository.
    """

    from shinka.utils import languages

    languages._LANGUAGE_ALIASES["lean4"] = "lean"
    languages._LANGUAGE_EXTENSIONS["lean"] = "lean"
    languages._EVOLVE_COMMENT_PREFIXES["lean"] = "--"
    languages._LANGUAGE_FENCE_TAGS["lean"] = ("lean", "lean4")
    languages._EVOLVE_MARKER_PATTERNS["lean"] = (
        re.compile(r"^\s*--\s*EVOLVE-BLOCK-START\s*$"),
        re.compile(r"^\s*--\s*EVOLVE-BLOCK-END\s*$"),
    )
    languages._EVOLVE_MARKER_EXAMPLES["lean"] = (
        "-- EVOLVE-BLOCK-START",
        "-- EVOLVE-BLOCK-END",
    )
