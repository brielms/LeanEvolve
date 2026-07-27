"""Defense-in-depth validation for model-authored Lean source."""

from __future__ import annotations

import re

START_MARKER = "-- EVOLVE-BLOCK-START"
END_MARKER = "-- EVOLVE-BLOCK-END"
_BANNED = {
    "admit": r"\badmit\b",
    "assumption declaration": r"\b(?:axiom|constant)\b",
    "compile-time command": r"#(?:eval|reduce|guard)\b",
    "custom elaborator": r"\b(?:elab|macro|syntax)\b",
    "foreign implementation": r"\b(?:extern|foreign|implemented_by)\b",
    "initialization": r"\binitialize\b",
    "placeholder": r"\bsorry\b",
    "run-time tactic": r"\brun_tac\b",
    "unsafe declaration": r"\b(?:unsafe|partial)\b",
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


def validate_candidate(source: str, max_bytes: int) -> None:
    size = len(source.encode("utf-8"))
    if size > max_bytes:
        raise ValueError(f"candidate is {size} bytes; limit is {max_bytes}")
    if source.count(START_MARKER) != 1 or source.count(END_MARKER) != 1:
        raise ValueError("candidate must contain exactly one evolution marker pair")
    if source.index(START_MARKER) >= source.index(END_MARKER):
        raise ValueError("evolution markers are out of order")
    scrubbed = scrub_comments_and_strings(source)
    violations = [
        label
        for label, pattern in _BANNED.items()
        if re.search(pattern, scrubbed, flags=re.IGNORECASE)
    ]
    if violations:
        raise ValueError("candidate violates source policy: " + ", ".join(violations))
