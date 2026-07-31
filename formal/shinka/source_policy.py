"""Shared metaprogramming denylist for untrusted Lean source.

Lean issue #14576 (fixed by PR #14577) let a malformed kernel declaration slip
past the pre-patch kernel.  Reaching it from an untrusted candidate requires
compile-time metaprogramming: running a metaprogram during elaboration, or
building a `Lean.Declaration` / raw projection by hand and handing it to
`addDecl`.  This module rejects those source-level mechanisms.

This is defence in depth, not a substitute for the patched kernel.  The kernel
remains the only thing that decides whether a proof term is valid; these
patterns only remove the route by which an untrusted candidate could hand the
kernel a declaration it never type-checked.

The definitions live here, outside any single entry point, so the authoritative
evaluator and the scratch checker cannot drift apart.  `src/leanevolve/policy.py`
carries a mirror because it ships in the wheel without `formal/`; a test asserts
the two stay identical.

Every pattern is matched against source with comments and string literals
blanked out, so a candidate may still discuss `addDecl` in a docstring.
"""

from __future__ import annotations

import re

# Ordered by the escape route each one closes.  Matching is case-sensitive:
# Lean identifiers are, and `Meta` is an ordinary namespace component.
METAPROGRAMMING_PATTERNS: dict[str, str] = {
    # `run_tac` was already rejected; `run_meta` is the same escape for a
    # `MetaM` action and was not.
    "compile-time metaprogram execution": r"\brun_(?:meta|tac)\b",
    # Lean 3 style `meta def`, and the Lean 4 `meta` declaration modifier.
    "meta declaration": (
        r"\bmeta\s+(?:def|abbrev|instance|inductive|structure|class"
        r"|theorem|lemma|constant|opaque|partial|unsafe)\b"
    ),
    # The environment-mutating entry points.  `addDeclWithoutChecking` skips
    # the kernel outright; `addDecl`/`addDeclCore` accept a declaration the
    # candidate built rather than one the elaborator derived from source.
    "kernel declaration injection": r"\baddDecl(?:Core|WithoutChecking)?\b",
    # `Lean.Declaration` constructors.  A candidate that names one is building
    # a declaration by hand instead of writing a `theorem`.
    "raw declaration constructor": (
        r"\b(?:axiomDecl|defnDecl|thmDecl|opaqueDecl|quotDecl"
        r"|mutualDefnDecl|inductDecl)\b"
    ),
    # Raw projections are the specific malformed-term shape behind #14576.
    "raw projection construction": r"\bmkProj\b|\bExpr\s*\.\s*proj\b",
}


def scrub_comments_and_strings(source: str) -> str:
    """Replace comments and string contents while preserving line structure."""

    result: list[str] = []
    index = 0
    block_depth = 0
    in_string = False
    while index < len(source):
        pair = source[index : index + 2]
        character = source[index]
        if block_depth:
            if pair == "/-":
                block_depth += 1
                result.extend("  ")
                index += 2
            elif pair == "-/":
                block_depth -= 1
                result.extend("  ")
                index += 2
            else:
                result.append("\n" if character == "\n" else " ")
                index += 1
            continue
        if in_string:
            if character == "\\" and index + 1 < len(source):
                result.extend("  ")
                index += 2
            elif character == '"':
                in_string = False
                result.append(" ")
                index += 1
            else:
                result.append("\n" if character == "\n" else " ")
                index += 1
            continue
        if pair == "--":
            newline = source.find("\n", index)
            if newline < 0:
                result.extend(" " * (len(source) - index))
                break
            result.extend(" " * (newline - index))
            result.append("\n")
            index = newline + 1
        elif pair == "/-":
            block_depth = 1
            result.extend("  ")
            index += 2
        elif character == '"':
            in_string = True
            result.append(" ")
            index += 1
        else:
            result.append(character)
            index += 1
    if block_depth:
        raise ValueError("unterminated block comment")
    if in_string:
        raise ValueError("unterminated string literal")
    return "".join(result)


def _scrubbed_or_raw(source: str) -> str:
    """Scrub comments and strings, falling back to raw source.

    An unterminated comment or string is rejected by Lean anyway.  Scanning the
    raw text in that case fails closed rather than letting a candidate hide a
    metaprogram behind a comment it never closes.
    """

    try:
        return scrub_comments_and_strings(source)
    except ValueError:
        return source


def metaprogramming_violations(source: str) -> list[str]:
    """Return the labels of every metaprogramming pattern the source matches."""

    scrubbed = _scrubbed_or_raw(source)
    return [
        label
        for label, pattern in METAPROGRAMMING_PATTERNS.items()
        if re.search(pattern, scrubbed)
    ]
