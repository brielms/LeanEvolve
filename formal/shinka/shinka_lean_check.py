#!/usr/bin/env python3
"""Read-only Lean scratch checker for one Shinka proposal.

The Headless Codex route runs with a read-only filesystem.  This command reads
the proposed source from stdin, assembles the frozen project source snapshot,
checkpoint, and selected parent entirely in memory, and asks the pinned Lean
binary to elaborate that single stream with ``--stdin``.

This is fast proposal feedback, not the authoritative evaluator.  It computes
no goal fitness, writes no candidate, and creates no proof receipt.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path

# The campaign runner snapshots this checker to
# `input_snapshot/formal/shinka/`, without the package `__init__.py` files, and
# runs it as a script.  `source_policy.py` is snapshotted beside it, so the
# sibling import is the one that resolves there; the package import is the one
# that resolves in the repository and under the test suite.
try:
    from formal.shinka.source_policy import metaprogramming_violations
except ImportError:  # pragma: no cover - exercised only in the run snapshot
    from source_policy import metaprogramming_violations

FORMAT = "shinka-lean-scratch-v1"
START_MARKER = "-- EVOLVE-BLOCK-START"
END_MARKER = "-- EVOLVE-BLOCK-END"
APPEND_SENTINEL = "-- SHINKA-APPEND-HERE"
#: Name of the Lean library a project builds.  It is the import allowlist
#: prefix, the frozen snapshot root, and the namespace root, so it is a
#: security-relevant setting rather than cosmetics: too broad and the
#: allowlist stops constraining imports, too narrow and valid ones fail.
DEFAULT_LIBRARY = "Demo"
#: Sub-namespace holding machine-generated declarations within the library.
GENERATED = "Generated"
CHECKPOINT_MODULE_SUFFIX = "Checkpoint"


def evolve_namespace(library: str = DEFAULT_LIBRARY) -> str:
    return f"{library}.{GENERATED}"


def checkpoint_module(library: str = DEFAULT_LIBRARY) -> str:
    return f"{evolve_namespace(library)}.{CHECKPOINT_MODULE_SUFFIX}"


CHECKPOINT_MODULE = checkpoint_module()
DEFAULT_SNAPSHOT = Path("input_snapshot/formal/lean")
DEFAULT_CHECKPOINT = Path("checkpoint_input.lean")
DEFAULT_SCRATCH_ENVIRONMENT = Path("scratch_environment.json")
SCRATCH_ENVIRONMENT_FORMAT = "shinka-scratch-environment-v1"
OBSERVABILITY_MARKER = "SHINKA_SCRATCH_OBSERVABILITY "
DEFAULT_MAX_SOURCE_BYTES = 8 * 1024 * 1024
DEFAULT_MAX_ASSEMBLED_BYTES = 64 * 1024 * 1024
ABSOLUTE_MAX_SOURCE_BYTES = 32 * 1024 * 1024
ABSOLUTE_MAX_ASSEMBLED_BYTES = 128 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 240
MAX_TIMEOUT_SECONDS = 300
MAX_DIAGNOSTIC_CHARS = 12_000
MAX_DIAGNOSTIC_LINES = 120
MAX_CAPTURE_HEAD_BYTES = 64 * 1024
MAX_CAPTURE_TAIL_BYTES = 64 * 1024
ALLOWED_STANDARD_AXIOMS = frozenset({"propext", "Classical.choice", "Quot.sound"})

IMPORT_LINE = re.compile(r"^[ \t]*import[ \t]+([^\r\n]+)$")
HUNK_HEADER = re.compile(
    r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?: .*)?(?:\r?\n)?$"
)
DECLARATION_NAME = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_']*(?:\.[A-Za-z_][A-Za-z0-9_']*)*$"
)
FORBIDDEN_PATTERNS = {
    "admitted proof": r"\b(?:sorry|admit)\b",
    "new assertion": r"\baxiom\b",
    "unsafe declaration": r"\bunsafe\b",
    "opaque declaration": r"\bopaque\b",
    "command elaborator": r"\b(?:elab|macro|syntax|initialize|extern)\b",
    "compile-time tactic escape": r"\brun_tac\b",
    "evaluation command": r"#(?:eval|reduce|run|print)\b",
    "file inclusion": r"\binclude_(?:str|bytes)\b",
    "system IO": r"\b(?:System|IO)\s*\.",
    "implementation substitution": r"implemented_by",
    "early-exit command": r"#\s*exit\b",
    # Flattening source into one stdin stream cannot reproduce Lean's private
    # name isolation between separately compiled modules. Rejecting private
    # declarations prevents that scratch-only visibility from yielding a
    # false positive.
    "module-private declaration": r"\bprivate\b",
}


class ExitCode(IntEnum):
    """Stable process exit codes for callers and tests."""

    OK = 0
    USAGE = 2
    POLICY = 3
    ASSEMBLY = 4
    LEAN_REJECTED = 5
    TIMEOUT = 6
    INFRASTRUCTURE = 7


class ScratchFailure(Exception):
    """A classified failure that is safe to render to the proposal agent."""

    def __init__(self, message: str, *, status: str, exit_code: ExitCode):
        super().__init__(message)
        self.status = status
        self.exit_code = exit_code


class PolicyFailure(ScratchFailure):
    def __init__(self, message: str):
        super().__init__(
            message,
            status="policy_rejected",
            exit_code=ExitCode.POLICY,
        )


class UsageFailure(ScratchFailure):
    def __init__(self, message: str):
        super().__init__(
            message,
            status="usage_error",
            exit_code=ExitCode.USAGE,
        )


class AssemblyFailure(ScratchFailure):
    def __init__(self, message: str):
        super().__init__(
            message,
            status="assembly_rejected",
            exit_code=ExitCode.ASSEMBLY,
        )


class InfrastructureFailure(ScratchFailure):
    def __init__(self, message: str):
        super().__init__(
            message,
            status="infrastructure_error",
            exit_code=ExitCode.INFRASTRUCTURE,
        )


@dataclass(frozen=True)
class ModuleSource:
    name: str
    path: Path | None
    source: str
    imports: tuple[str, ...]


@dataclass(frozen=True)
class Assembly:
    source: str
    modules: tuple[str, ...]
    external_imports: tuple[str, ...]


@dataclass(frozen=True)
class LeanResult:
    returncode: int
    output: str
    elapsed_seconds: float


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _file_record(path: Path, source: str) -> dict[str, object]:
    return {
        "path": str(path),
        "bytes": len(source.encode("utf-8")),
        "sha256": _sha256_text(source),
    }


def _read_utf8_bytes(data: bytes, *, label: str, maximum: int) -> str:
    if len(data) > maximum:
        raise PolicyFailure(f"{label} exceeds {maximum} UTF-8 bytes")
    if b"\x00" in data:
        raise PolicyFailure(f"{label} contains a NUL byte")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise PolicyFailure(f"{label} is not valid UTF-8: {error}") from error


def read_stdin(maximum: int) -> str:
    """Read one bounded proposal without using a temporary file."""

    data = sys.stdin.buffer.read(maximum + 1)
    return _read_utf8_bytes(data, label="stdin proposal", maximum=maximum)


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _safe_root(value: Path) -> Path:
    try:
        root = value.resolve(strict=True)
    except OSError as error:
        raise PolicyFailure(f"solve root is unavailable: {value}: {error}") from error
    if not root.is_dir():
        raise PolicyFailure(f"solve root is not a directory: {root}")
    return root


def _safe_path_within(
    root: Path,
    value: Path,
    *,
    label: str,
    directory: bool = False,
) -> Path:
    raw = value if value.is_absolute() else root / value
    if raw.is_symlink():
        raise PolicyFailure(f"{label} may not be a symlink: {raw}")
    try:
        resolved = raw.resolve(strict=True)
    except OSError as error:
        raise PolicyFailure(f"{label} is unavailable: {raw}: {error}") from error
    if not _inside(resolved, root):
        raise PolicyFailure(f"{label} escapes the solve root: {resolved}")
    expected = resolved.is_dir() if directory else resolved.is_file()
    if not expected:
        kind = "directory" if directory else "regular file"
        raise PolicyFailure(f"{label} is not a {kind}: {resolved}")
    return resolved


def _read_file(path: Path, *, label: str, maximum: int) -> str:
    if path.is_symlink():
        raise PolicyFailure(f"{label} may not be a symlink: {path}")
    try:
        if path.stat().st_size > maximum:
            raise PolicyFailure(f"{label} exceeds {maximum} UTF-8 bytes")
        with path.open("rb") as stream:
            data = stream.read(maximum + 1)
    except OSError as error:
        raise PolicyFailure(f"cannot read {label}: {path}: {error}") from error
    return _read_utf8_bytes(data, label=label, maximum=maximum)


def _evolve_bounds(source: str) -> tuple[list[str], int, int]:
    lines = source.splitlines(keepends=True)
    starts = [index for index, line in enumerate(lines) if line.strip() == START_MARKER]
    ends = [index for index, line in enumerate(lines) if line.strip() == END_MARKER]
    if len(starts) != 1 or len(ends) != 1 or starts[0] >= ends[0]:
        raise PolicyFailure("candidate needs exactly one ordered evolve block")
    return lines, starts[0], ends[0]


def _outside_evolve_block(source: str) -> tuple[str, str]:
    lines, start, end = _evolve_bounds(source)
    return "".join(lines[: start + 1]), "".join(lines[end:])


def _append_anchor(source: str) -> tuple[str, str]:
    """Split source at its sole legal append point without normalizing bytes."""

    lines, start, end = _evolve_bounds(source)
    sentinels = [
        index for index, line in enumerate(lines) if line.strip() == APPEND_SENTINEL
    ]
    if len(sentinels) > 1:
        raise PolicyFailure("candidate contains more than one append sentinel")
    if sentinels and not start < sentinels[0] < end:
        raise PolicyFailure("append sentinel is outside the evolve block")
    insertion = sentinels[0] if sentinels else end
    return "".join(lines[:insertion]), "".join(lines[insertion:])


def validate_append_only_extension(parent: str, source: str) -> None:
    """Require an exact insertion at the parent's append point.

    This deliberately compares the original bytes, including whitespace and
    line endings.  An accepted declaration may only be followed by new text;
    it cannot be edited, deleted, reordered, or shadowed by moving the append
    sentinel.
    """

    parent_prefix, parent_suffix = _append_anchor(parent)
    source_prefix, source_suffix = _append_anchor(source)
    if source_suffix != parent_suffix or not source_prefix.startswith(parent_prefix):
        raise PolicyFailure(
            "proposal is not an exact append-only extension of the parent"
        )


def _imports(source: str) -> tuple[str, ...]:
    imported: list[str] = []
    for line in source.splitlines():
        match = IMPORT_LINE.match(line)
        if match is None:
            continue
        # Match the authoritative evaluator exactly: trailing tokens, including
        # comment markers, are not silently accepted as part of an import.
        imported.extend(match.group(1).split())
    return tuple(imported)


def _header_import_line_indices(source: str) -> tuple[int, ...]:
    """Return imports in Lean's leading module header, ignoring comments.

    The evaluator deliberately scans all exact import lines for policy. For
    assembly, however, only syntactically leading imports are dependencies;
    erasing a later invalid import could otherwise turn rejected Lean into a
    false scratch success.
    """

    indices: list[int] = []
    block_depth = 0
    for index, kept_line in enumerate(source.splitlines(keepends=True)):
        line = kept_line.rstrip("\r\n")
        cursor = 0
        comment_only = True
        while cursor < len(line):
            if block_depth:
                if line.startswith("/-", cursor):
                    block_depth += 1
                    cursor += 2
                elif line.startswith("-/", cursor):
                    block_depth -= 1
                    cursor += 2
                else:
                    cursor += 1
                continue
            if line[cursor].isspace():
                cursor += 1
                continue
            if line.startswith("--", cursor):
                cursor = len(line)
                continue
            if line.startswith("/-", cursor):
                block_depth += 1
                cursor += 2
                continue
            comment_only = False
            break
        if comment_only:
            continue
        if block_depth == 0 and IMPORT_LINE.match(line) is not None:
            indices.append(index)
            continue
        break
    return tuple(indices)


def _header_imports(source: str) -> tuple[str, ...]:
    lines = source.splitlines()
    imported: list[str] = []
    for index in _header_import_line_indices(source):
        match = IMPORT_LINE.match(lines[index])
        assert match is not None
        imported.extend(match.group(1).split())
    return tuple(imported)


def validate_candidate_policy(
    source: str,
    *,
    maximum: int,
    parent: str | None = None,
    library: str = DEFAULT_LIBRARY,
) -> None:
    """Apply the evaluator's source restrictions, except goal-name scoring."""

    source_bytes = len(source.encode("utf-8"))
    if source_bytes > maximum:
        raise PolicyFailure(f"candidate exceeds {maximum} UTF-8 bytes")
    if "\x00" in source:
        raise PolicyFailure("candidate contains a NUL byte")
    imported = _imports(source)
    if not imported:
        raise PolicyFailure(
            f"candidate must import an existing {library} module"
        )
    invalid = [
        name
        for name in imported
        if name != library and not name.startswith(f"{library}.")
    ]
    if invalid:
        raise PolicyFailure(
            f"candidate imports modules outside {library}: " + ", ".join(invalid)
        )
    namespace = evolve_namespace(library)
    if re.search(rf"\bnamespace\s+{re.escape(namespace)}\b", source) is None:
        raise PolicyFailure(f"candidate must use namespace {namespace}")
    _evolve_bounds(source)
    if parent is not None:
        if _outside_evolve_block(source) != _outside_evolve_block(parent):
            raise PolicyFailure("proposal changes text outside the evolve block")
        validate_append_only_extension(parent, source)
    for label, pattern in FORBIDDEN_PATTERNS.items():
        if re.search(pattern, source):
            raise PolicyFailure(f"candidate contains forbidden {label}")
    # Matched against comment- and string-scrubbed source, so prose may still
    # name these mechanisms.  Kept identical to the authoritative evaluator.
    violations = metaprogramming_violations(source)
    if violations:
        raise PolicyFailure(
            "candidate contains forbidden " + ", ".join(violations)
        )


def append_to_parent(parent: str, snippet: str) -> str:
    """Insert a snippet at the unique sentinel, or just before the end marker."""

    if not snippet.strip():
        raise PolicyFailure("append proposal is empty")
    stray = [
        (number, line.strip())
        for number, line in enumerate(snippet.splitlines(), start=1)
        if START_MARKER in line or END_MARKER in line
    ]
    if stray:
        # Copying a slice out of an existing candidate drags its markers along,
        # so name the exact lines and both remedies instead of failing flatly.
        locations = "; ".join(
            f"line {number}: {text}" for number, text in stray[:4]
        )
        raise PolicyFailure(
            "append proposal may not contain evolve markers "
            f"({locations}). An append snippet is inserted inside the parent's "
            "existing evolve block, so send only the new declarations without "
            "any EVOLVE-BLOCK marker lines. If you meant to submit a complete "
            "file rather than an addition, use --mode candidate instead, which "
            "requires the markers."
        )
    lines, start, end = _evolve_bounds(parent)
    sentinels = [
        index for index, line in enumerate(lines) if line.strip() == APPEND_SENTINEL
    ]
    if len(sentinels) > 1:
        raise PolicyFailure("parent contains more than one append sentinel")
    insertion = sentinels[0] if sentinels else end
    if not start < insertion <= end:
        raise PolicyFailure("append sentinel is outside the evolve block")
    payload = snippet
    if insertion > 0 and not lines[insertion - 1].endswith(("\n", "\r")):
        payload = "\n" + payload
    if not payload.endswith("\n"):
        payload += "\n"
    lines.insert(insertion, payload)
    return "".join(lines)


def apply_unified_diff(parent: str, patch: str) -> str:
    """Apply one strict, single-file unified diff without touching the disk."""

    if not patch.strip():
        raise PolicyFailure("unified diff is empty")
    lines = patch.splitlines(keepends=True)
    index = 0
    header_pairs = 0
    while index < len(lines) and not lines[index].startswith("@@ "):
        line = lines[index]
        if line.startswith("--- "):
            if index + 1 >= len(lines) or not lines[index + 1].startswith("+++ "):
                raise PolicyFailure("unified diff has an unmatched file header")
            header_pairs += 1
            index += 2
            continue
        if line.startswith(("diff --git ", "index ")) or not line.strip():
            index += 1
            continue
        raise PolicyFailure(f"unsupported unified-diff metadata: {line.rstrip()}")
    if header_pairs != 1:
        raise PolicyFailure("unified diff must describe exactly one file")

    original = parent.splitlines(keepends=True)
    output: list[str] = []
    original_cursor = 0
    hunk_count = 0
    while index < len(lines):
        header = HUNK_HEADER.match(lines[index])
        if header is None:
            raise PolicyFailure(
                f"expected unified-diff hunk header, got: {lines[index].rstrip()}"
            )
        hunk_count += 1
        old_start = int(header.group(1))
        old_count = int(header.group(2) or "1")
        new_start = int(header.group(3))
        new_count = int(header.group(4) or "1")
        new_target = new_start - 1 if new_count else new_start
        prior_delta = len(output) - original_cursor
        target = new_target - prior_delta
        expected_old_target = old_start - 1 if old_count else old_start
        start_insertion = old_count == 0 and old_start == 1 and target == 0
        if target != expected_old_target and not start_insertion:
            raise PolicyFailure(
                "unified-diff old/new hunk coordinates are inconsistent"
            )
        if target < original_cursor or target > len(original):
            raise PolicyFailure("unified-diff hunks are overlapping or out of range")
        output.extend(original[original_cursor:target])
        original_cursor = target
        index += 1
        seen_old = 0
        seen_new = 0
        while index < len(lines) and not lines[index].startswith("@@ "):
            line = lines[index]
            if line.startswith("\\ No newline at end of file"):
                raise PolicyFailure(
                    "unified diffs with no-newline markers are unsupported"
                )
            if not line or line[0] not in " +-":
                raise PolicyFailure(f"malformed unified-diff line: {line.rstrip()}")
            prefix = line[0]
            payload = line[1:]
            if prefix in " -":
                if original_cursor >= len(original):
                    raise PolicyFailure("unified diff reads beyond the parent")
                if original[original_cursor] != payload:
                    raise PolicyFailure(
                        "unified-diff context does not exactly match the parent "
                        f"at line {original_cursor + 1}"
                    )
                original_cursor += 1
                seen_old += 1
            if prefix in " +":
                output.append(payload)
                seen_new += 1
            index += 1
        if seen_old != old_count or seen_new != new_count:
            raise PolicyFailure("unified-diff hunk counts disagree with the hunk body")
    if hunk_count == 0:
        raise PolicyFailure("unified diff contains no hunks")
    output.extend(original[original_cursor:])
    return "".join(output)


def _module_name(snapshot_root: Path, path: Path) -> str:
    relative = path.relative_to(snapshot_root)
    return ".".join(relative.with_suffix("").parts)


def _without_imports(source: str) -> str:
    lines: list[str] = []
    header_indices = set(_header_import_line_indices(source))
    for index, line in enumerate(source.splitlines(keepends=True)):
        if index not in header_indices:
            lines.append(line)
        elif line.endswith("\r\n"):
            lines.append("\r\n")
        elif line.endswith("\n"):
            lines.append("\n")
    return "".join(lines)


def load_snapshot_modules(
    snapshot_root: Path,
    *,
    maximum: int,
    library: str = DEFAULT_LIBRARY,
) -> dict[str, ModuleSource]:
    try:
        snapshot_root = snapshot_root.resolve(strict=True)
    except OSError as error:
        raise AssemblyFailure(
            f"formal snapshot root is unavailable: {error}"
        ) from error
    if not snapshot_root.is_dir():
        raise AssemblyFailure(
            f"formal snapshot root is not a directory: {snapshot_root}"
        )
    modules: dict[str, ModuleSource] = {}
    total_source_bytes = 0
    try:
        paths = sorted(snapshot_root.rglob("*.lean"))
    except OSError as error:
        raise AssemblyFailure(f"cannot enumerate formal snapshot: {error}") from error
    for path in paths:
        if path.name.startswith("._"):
            continue
        if path.is_symlink():
            raise AssemblyFailure(f"snapshot module may not be a symlink: {path}")
        try:
            resolved = path.resolve(strict=True)
        except OSError as error:
            raise AssemblyFailure(f"snapshot module is unavailable: {path}") from error
        if not _inside(resolved, snapshot_root):
            raise AssemblyFailure(f"snapshot module escapes its root: {path}")
        if not resolved.is_file():
            raise AssemblyFailure(f"snapshot module is not a regular file: {path}")
        name = _module_name(snapshot_root, resolved)
        relative = resolved.relative_to(snapshot_root)
        if relative != Path(f"{library}.lean"):
            if not relative.parts or relative.parts[0] != library:
                continue
            within_library = relative.relative_to(library)
            if (
                within_library.parts[0] == GENERATED
                and within_library != Path(f"{GENERATED}/PromotedFrontier.lean")
            ) or within_library.name == "Audit.lean":
                continue
        try:
            source = _read_file(
                resolved,
                label=f"snapshot module {resolved.name}",
                maximum=maximum,
            )
        except PolicyFailure as error:
            raise AssemblyFailure(str(error)) from error
        total_source_bytes += len(source.encode("utf-8"))
        if total_source_bytes > maximum:
            raise AssemblyFailure(f"frozen snapshot exceeds {maximum} UTF-8 bytes")
        for label in ("early-exit command", "module-private declaration"):
            if re.search(FORBIDDEN_PATTERNS[label], source):
                raise AssemblyFailure(
                    f"snapshot module {name} contains unsupported {label}"
                )
        if name in modules:
            raise AssemblyFailure(f"duplicate snapshot module: {name}")
        modules[name] = ModuleSource(
            name,
            resolved,
            source,
            _header_imports(source),
        )
    if library not in modules:
        raise AssemblyFailure(
            f"frozen formal snapshot does not contain {library}.lean"
        )
    return modules


def assemble_frozen_source(
    snapshot_root: Path,
    candidate: str,
    *,
    checkpoint: str | None,
    maximum: int,
    axiom_declarations: Sequence[str] = (),
    library: str = DEFAULT_LIBRARY,
) -> Assembly:
    """Topologically flatten frozen modules into one read-only Lean stream."""

    modules = load_snapshot_modules(
        snapshot_root, maximum=maximum, library=library
    )
    if checkpoint is not None:
        checkpoint_name = checkpoint_module(library)
        modules[checkpoint_name] = ModuleSource(
            checkpoint_name,
            None,
            checkpoint,
            _header_imports(checkpoint),
        )
    candidate_module = ModuleSource(
        "<candidate>",
        None,
        candidate,
        _header_imports(candidate),
    )
    ordered: list[ModuleSource] = []
    visiting: set[str] = set()
    visited: set[str] = set()
    external: list[str] = []

    def visit(name: str, importer: str) -> None:
        if name in visited:
            return
        module = modules.get(name)
        if module is None:
            if name == library or name.startswith(f"{library}."):
                raise AssemblyFailure(
                    f"{importer} imports missing frozen {library} module {name}"
                )
            if name not in external:
                external.append(name)
            return
        if name in visiting:
            raise AssemblyFailure(f"cyclic frozen module import at {name}")
        visiting.add(name)
        for dependency in module.imports:
            visit(dependency, name)
        visiting.remove(name)
        visited.add(name)
        ordered.append(module)

    for imported in candidate_module.imports:
        visit(imported, candidate_module.name)

    chunks = ["".join(f"import {name}\n" for name in external)]
    for module in ordered:
        chunks.extend(
            [
                f"\n-- BEGIN FROZEN SCRATCH MODULE {module.name}\n",
                "section\n",
                _without_imports(module.source),
                "\nend\n",
                f"\n-- END FROZEN SCRATCH MODULE {module.name}\n",
            ]
        )
    chunks.extend(
        [
            "\n-- BEGIN SCRATCH CANDIDATE\n",
            _without_imports(candidate_module.source),
            "\n-- END SCRATCH CANDIDATE\n",
        ]
    )
    if axiom_declarations:
        chunks.append("\n-- SCRATCH-ONLY AXIOM QUERIES\n")
        chunks.extend(
            f"#print axioms {declaration}\n" for declaration in axiom_declarations
        )
    assembled = "".join(chunks)
    assembled_bytes = len(assembled.encode("utf-8"))
    if assembled_bytes > maximum:
        raise AssemblyFailure(f"assembled scratch source exceeds {maximum} UTF-8 bytes")
    return Assembly(
        source=assembled,
        modules=tuple(module.name for module in ordered),
        external_imports=tuple(external),
    )


def _toolchain_directory_name(specification: str) -> str:
    return specification.replace("/", "--").replace(":", "---")


def _account_home() -> Path:
    """Return the OS account home without trusting shell-controlled HOME."""

    try:
        import pwd

        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except (ImportError, KeyError, OSError) as error:
        raise InfrastructureFailure(
            f"cannot resolve the operating-system account home: {error}"
        ) from error


def resolve_pinned_lean(snapshot_root: Path) -> tuple[Path, str, str]:
    toolchain_file = snapshot_root / "lean-toolchain"
    if toolchain_file.is_symlink() or not toolchain_file.is_file():
        raise InfrastructureFailure(
            f"frozen snapshot lacks a regular lean-toolchain file: {toolchain_file}"
        )
    try:
        if toolchain_file.stat().st_size > 4096:
            raise InfrastructureFailure("lean-toolchain is unexpectedly large")
        with toolchain_file.open("rb") as stream:
            data = stream.read(4097)
        if len(data) > 4096:
            raise InfrastructureFailure("lean-toolchain is unexpectedly large")
        specification = data.decode("utf-8").strip()
    except (OSError, UnicodeError) as error:
        raise InfrastructureFailure(f"cannot read lean-toolchain: {error}") from error
    if (
        not specification
        or "\x00" in specification
        or any(character.isspace() for character in specification)
    ):
        raise InfrastructureFailure("lean-toolchain is empty or malformed")
    elan_root = _account_home() / ".elan"
    lean = (
        elan_root
        / "toolchains"
        / _toolchain_directory_name(specification)
        / "bin"
        / "lean"
    )
    try:
        lean = lean.resolve(strict=True)
    except (OSError, ValueError) as error:
        raise InfrastructureFailure(
            f"pinned Lean binary is unavailable for {specification}: {error}"
        ) from error
    resolution = "frozen-toolchain"
    if not lean.is_file() or not os.access(lean, os.X_OK):
        raise InfrastructureFailure(f"Lean binary is not executable: {lean}")
    return lean, specification, resolution


def build_scratch_environment_record(source_root: Path) -> dict[str, object]:
    """Bind scratch module paths to the evaluator-verified Lake cache.

    The authoritative evaluator already verifies every package checkout
    against ``lake-manifest.json`` and binds the compiled-artifact sidecars.
    Reuse that exact resolver once, before the read-only proposal agent starts,
    and persist only the resulting immutable search-path receipt.
    """

    from formal.shinka.evaluate_candidate import (
        _resolve_pinned_lake_dependencies,
    )

    source_root = source_root.resolve(strict=True)
    dependencies = _resolve_pinned_lake_dependencies(source_root)
    _lean, specification, _resolution = resolve_pinned_lean(source_root)
    manifest_path = source_root / "lake-manifest.json"
    toolchain_path = source_root / "lean-toolchain"
    entries: list[dict[str, str]] = []
    artifact_sha256 = hashlib.sha256(b"{}").hexdigest()
    artifact_count = 0
    if dependencies is not None:
        artifact_sha256 = dependencies.artifact_digest_sha256
        artifact_count = dependencies.artifact_record_count
        for name, revision in reversed(dependencies.revisions):
            configured_module_path = (
                dependencies.packages_dir
                / name
                / ".lake/build/lib/lean"
            )
            # A Lake manifest may include optional tooling packages that have
            # no compiled library in the local cache. Lake itself places such
            # nonexistent entries on LEAN_PATH; omitting them is equivalent.
            if not configured_module_path.is_dir():
                continue
            module_path = configured_module_path.resolve(strict=True)
            entries.append(
                {
                    "package": name,
                    "revision": revision,
                    "module_path": str(module_path),
                }
            )
    core: dict[str, object] = {
        "format": SCRATCH_ENVIRONMENT_FORMAT,
        "toolchain": specification,
        "lean_toolchain_sha256": _sha256_file(toolchain_path),
        "lake_manifest_sha256": _sha256_file(manifest_path),
        "dependency_artifacts_sha256": artifact_sha256,
        "dependency_artifact_count": artifact_count,
        "module_paths": entries,
    }
    return {
        **core,
        "record_sha256": hashlib.sha256(
            _canonical_json(core).encode("utf-8")
        ).hexdigest(),
    }


def write_scratch_environment_record(
    path: Path,
    record: dict[str, object],
) -> None:
    """Atomically persist one prevalidated scratch environment receipt."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _load_scratch_lean_path(
    solve_root: Path,
    snapshot_root: Path,
    external_imports: tuple[str, ...],
) -> tuple[Path, ...]:
    """Load and revalidate the runner-issued package-path receipt."""

    if not external_imports:
        return ()
    record_path = _safe_path_within(
        solve_root,
        DEFAULT_SCRATCH_ENVIRONMENT,
        label="scratch environment receipt",
    )
    try:
        raw = record_path.read_bytes()
        if len(raw) > 128 * 1024:
            raise InfrastructureFailure("scratch environment receipt is too large")
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise InfrastructureFailure(
            f"cannot read scratch environment receipt: {error}"
        ) from error
    if not isinstance(payload, dict):
        raise InfrastructureFailure("scratch environment receipt is not an object")
    claimed = payload.get("record_sha256")
    core = dict(payload)
    core.pop("record_sha256", None)
    actual = hashlib.sha256(_canonical_json(core).encode("utf-8")).hexdigest()
    if claimed != actual:
        raise InfrastructureFailure("scratch environment receipt hash mismatch")
    if payload.get("format") != SCRATCH_ENVIRONMENT_FORMAT:
        raise InfrastructureFailure("unsupported scratch environment receipt")
    if payload.get("lake_manifest_sha256") != _sha256_file(
        snapshot_root / "lake-manifest.json"
    ):
        raise InfrastructureFailure("scratch Lake manifest differs from snapshot")
    if payload.get("lean_toolchain_sha256") != _sha256_file(
        snapshot_root / "lean-toolchain"
    ):
        raise InfrastructureFailure("scratch Lean toolchain differs from snapshot")
    modules = payload.get("module_paths")
    if not isinstance(modules, list) or not modules:
        raise InfrastructureFailure(
            "external imports require a nonempty pinned Lean module path"
        )
    paths: list[Path] = []
    for entry in modules:
        if not isinstance(entry, dict):
            raise InfrastructureFailure("malformed scratch module-path entry")
        name = entry.get("package")
        revision = entry.get("revision")
        value = entry.get("module_path")
        if (
            not isinstance(name, str)
            or not re.fullmatch(r"[A-Za-z0-9_.-]+", name)
            or not isinstance(revision, str)
            or not re.fullmatch(r"[0-9a-f]{40}", revision)
            or not isinstance(value, str)
        ):
            raise InfrastructureFailure("malformed scratch module-path entry")
        path = Path(value)
        if not path.is_absolute() or path.is_symlink():
            raise InfrastructureFailure(
                f"unsafe scratch module path for package {name}"
            )
        try:
            path = path.resolve(strict=True)
        except OSError as error:
            raise InfrastructureFailure(
                f"scratch module path is unavailable for package {name}: {error}"
            ) from error
        if not path.is_dir() or path.name != "lean":
            raise InfrastructureFailure(
                f"invalid scratch module path for package {name}"
            )
        paths.append(path)
    return tuple(paths)


class _BoundedOutput:
    def __init__(self) -> None:
        self.total = 0
        self.head = bytearray()
        self.tail = bytearray()

    def add(self, chunk: bytes) -> None:
        self.total += len(chunk)
        head_room = MAX_CAPTURE_HEAD_BYTES - len(self.head)
        if head_room > 0:
            self.head.extend(chunk[:head_room])
            chunk = chunk[head_room:]
        if chunk:
            self.tail.extend(chunk)
            if len(self.tail) > MAX_CAPTURE_TAIL_BYTES:
                del self.tail[:-MAX_CAPTURE_TAIL_BYTES]

    def render(self) -> str:
        retained = len(self.head) + len(self.tail)
        if self.total <= retained:
            data = bytes(self.head + self.tail)
        else:
            omitted = self.total - retained
            marker = f"\n... {omitted} output bytes omitted ...\n".encode()
            data = bytes(self.head) + marker + bytes(self.tail)
        return data.decode("utf-8", errors="replace")


def _lean_environment(module_paths: tuple[Path, ...] = ()) -> dict[str, str]:
    """Minimal fixed environment with only runner-validated module paths."""

    environment = {
        "HOME": str(_account_home()),
        "PATH": os.defpath,
    }
    if module_paths:
        environment["LEAN_PATH"] = os.pathsep.join(map(str, module_paths))
    return environment


def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        try:
            process.kill()
        except OSError:
            pass


def _close_process_pipes(process: subprocess.Popen[bytes]) -> None:
    for stream in (process.stdin, process.stdout):
        if stream is not None and not stream.closed:
            try:
                stream.close()
            except OSError:
                pass


def invoke_lean(
    lean: Path,
    snapshot_root: Path,
    source: str,
    timeout_seconds: int,
    module_paths: tuple[Path, ...] = (),
) -> LeanResult:
    """Run Lean with stdin and no output path or scratch directory."""

    started = time.monotonic()
    try:
        process = subprocess.Popen(
            [str(lean), "--stdin"],
            cwd=snapshot_root,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            close_fds=True,
            env=_lean_environment(module_paths),
            start_new_session=True,
        )
    except OSError as error:
        raise InfrastructureFailure(
            f"could not execute pinned Lean: {error}"
        ) from error

    assert process.stdin is not None and process.stdout is not None
    captured = _BoundedOutput()
    reader_errors: list[BaseException] = []
    writer_errors: list[BaseException] = []

    def drain_output() -> None:
        try:
            while chunk := process.stdout.read(64 * 1024):
                captured.add(chunk)
        except (OSError, ValueError) as error:
            reader_errors.append(error)

    def feed_source() -> None:
        try:
            process.stdin.write(source.encode("utf-8"))
            process.stdin.close()
        except BrokenPipeError:
            pass
        except (OSError, ValueError) as error:
            writer_errors.append(error)

    reader = threading.Thread(target=drain_output, daemon=True)
    writer = threading.Thread(target=feed_source, daemon=True)
    reader.start()
    writer.start()
    try:
        returncode = process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as error:
        _kill_process_group(process)
        process.wait()
        writer.join(timeout=2)
        reader.join(timeout=2)
        _close_process_pipes(process)
        elapsed = time.monotonic() - started
        failure = ScratchFailure(
            f"Lean scratch check timed out after {timeout_seconds}s",
            status="timeout",
            exit_code=ExitCode.TIMEOUT,
        )
        setattr(failure, "diagnostics", captured.render())
        setattr(failure, "elapsed_seconds", elapsed)
        raise failure from error
    writer.join(timeout=2)
    reader.join(timeout=2)
    _close_process_pipes(process)
    if writer.is_alive() or reader.is_alive():
        _kill_process_group(process)
        raise InfrastructureFailure(
            "Lean I/O worker did not terminate after the subprocess"
        )
    if reader_errors:
        raise InfrastructureFailure(f"could not read Lean output: {reader_errors[0]}")
    if writer_errors and returncode == 0:
        raise InfrastructureFailure(f"could not stream Lean source: {writer_errors[0]}")
    return LeanResult(
        returncode=returncode,
        output=captured.render(),
        elapsed_seconds=time.monotonic() - started,
    )


def compact_diagnostics(output: str) -> str:
    lines = output.splitlines()
    if len(lines) > MAX_DIAGNOSTIC_LINES:
        head_count = MAX_DIAGNOSTIC_LINES - 21
        omitted = len(lines) - MAX_DIAGNOSTIC_LINES + 1
        lines = [
            *lines[:head_count],
            f"... {omitted} diagnostic lines omitted ...",
            *lines[-20:],
        ]
    text = "\n".join(lines)
    if len(text) > MAX_DIAGNOSTIC_CHARS:
        retained = MAX_DIAGNOSTIC_CHARS - 80
        text = text[:retained] + "\n... diagnostics truncated ..."
    return text


def parse_axiom_receipts(
    output: str,
    declarations: Sequence[str],
) -> dict[str, object]:
    lines = output.splitlines()
    receipts: dict[str, object] = {}
    for declaration in declarations:
        no_axioms = f"'{declaration}' does not depend on any axioms"
        with_axioms = f"'{declaration}' depends on axioms: ["
        matches = [
            index
            for index, line in enumerate(lines)
            if line == no_axioms or line.startswith(with_axioms)
        ]
        dependencies: list[str] | None = None
        if len(matches) == 1:
            index = matches[0]
            line = lines[index]
            if line == no_axioms:
                dependencies = []
            else:
                while not line.endswith("]") and index + 1 < len(lines):
                    index += 1
                    line += " " + lines[index].strip()
                if line.endswith("]"):
                    payload = line[len(with_axioms) : -1]
                    dependencies = [
                        name.strip() for name in payload.split(",") if name.strip()
                    ]
        receipts[declaration] = {
            "dependencies": dependencies,
            "within_project_allowlist": (
                dependencies is not None
                and set(dependencies) <= ALLOWED_STANDARD_AXIOMS
            ),
        }
    return receipts


def _empty_receipt(mode: str) -> dict[str, object]:
    return {
        "format": FORMAT,
        "status": "not_run",
        "exit_code": int(ExitCode.INFRASTRUCTURE),
        "mode": mode,
        "checks": {
            "source_policy": "not_run",
            "assembly": "not_run",
            "lean": "not_run",
        },
        "inputs": {
            "solve_root": None,
            "snapshot_root": None,
            "parent": None,
            "checkpoint": None,
            "stdin_bytes": None,
            "candidate_bytes": None,
            "candidate_sha256": None,
        },
        "assembly": {
            "bytes": None,
            "sha256": None,
            "module_count": None,
            "modules": [],
            "external_imports": [],
        },
        "lean": {
            "toolchain": None,
            "path": None,
            "resolution": None,
            "module_path_count": None,
            "process_returncode": None,
            "elapsed_seconds": None,
        },
        "axioms": {},
        "diagnostics": "",
        "trust": ("scratch elaboration only; no fitness, promotion, or proof receipt"),
    }


def _validate_arguments(args: argparse.Namespace) -> None:
    if not 1 <= args.timeout_seconds <= MAX_TIMEOUT_SECONDS:
        raise PolicyFailure(f"timeout must be in 1..{MAX_TIMEOUT_SECONDS} seconds")
    if not 1 <= args.max_source_bytes <= ABSOLUTE_MAX_SOURCE_BYTES:
        raise PolicyFailure(
            f"max source bytes must be in 1..{ABSOLUTE_MAX_SOURCE_BYTES}"
        )
    if not (
        args.max_source_bytes
        <= args.max_assembled_bytes
        <= ABSOLUTE_MAX_ASSEMBLED_BYTES
    ):
        raise PolicyFailure(
            "max assembled bytes must be at least max source bytes and at most "
            f"{ABSOLUTE_MAX_ASSEMBLED_BYTES}"
        )
    if args.mode == "candidate" and args.parent is not None:
        raise PolicyFailure("candidate mode does not accept --parent")
    if args.mode in {"append", "diff"} and args.parent is None:
        raise PolicyFailure(f"{args.mode} mode requires --parent")
    invalid_axioms = [
        name for name in args.axiom if DECLARATION_NAME.fullmatch(name) is None
    ]
    if invalid_axioms:
        raise PolicyFailure(
            "invalid axiom declaration name(s): " + ", ".join(invalid_axioms)
        )


def run_check(
    args: argparse.Namespace,
    proposal: str,
    *,
    lean_runner: Callable[
        [Path, Path, str, int, tuple[Path, ...]], LeanResult
    ] = invoke_lean,
    lean_resolver: Callable[[Path], tuple[Path, str, str]] = resolve_pinned_lean,
    event_sink: Callable[[str, dict[str, object]], None] | None = None,
) -> tuple[dict[str, object], ExitCode]:
    receipt = _empty_receipt(args.mode)
    started = time.monotonic()
    scratch_invocation_id: str | None = None
    scratch_started = 0.0
    try:
        _validate_arguments(args)
        root = _safe_root(args.solve_root)
        snapshot = _safe_path_within(
            root,
            args.snapshot_root,
            label="formal input snapshot",
            directory=True,
        )
        receipt["inputs"]["solve_root"] = str(root)
        receipt["inputs"]["snapshot_root"] = str(snapshot.relative_to(root))
        receipt["inputs"]["stdin_bytes"] = len(proposal.encode("utf-8"))

        parent_source: str | None = None
        if args.parent is not None:
            parent_path = _safe_path_within(
                root,
                args.parent,
                label="selected parent",
            )
            parent_source = _read_file(
                parent_path,
                label="selected parent",
                maximum=args.max_source_bytes,
            )
            receipt["inputs"]["parent"] = _file_record(
                parent_path.relative_to(root), parent_source
            )

        checkpoint_source: str | None = None
        if not args.no_checkpoint:
            automatic = root / DEFAULT_CHECKPOINT
            if automatic.exists() or automatic.is_symlink():
                checkpoint_path = _safe_path_within(
                    root,
                    DEFAULT_CHECKPOINT,
                    label="frozen checkpoint",
                )
                checkpoint_source = _read_file(
                    checkpoint_path,
                    label="frozen checkpoint",
                    maximum=args.max_source_bytes,
                )
                receipt["inputs"]["checkpoint"] = _file_record(
                    checkpoint_path.relative_to(root), checkpoint_source
                )
                validate_candidate_policy(
                    checkpoint_source,
                    maximum=args.max_source_bytes,
                    library=args.library,
                )
                generated_imports = [
                    name
                    for name in _imports(checkpoint_source)
                    if name.startswith(f"{evolve_namespace(args.library)}.")
                ]
                if generated_imports:
                    raise PolicyFailure(
                        "checkpoint imports generated modules: "
                        + ", ".join(generated_imports)
                    )

        if args.mode == "candidate":
            candidate = proposal
        elif args.mode == "append":
            assert parent_source is not None
            candidate = append_to_parent(parent_source, proposal)
        else:
            assert args.mode == "diff" and parent_source is not None
            candidate = apply_unified_diff(parent_source, proposal)

        validate_candidate_policy(
            candidate,
            maximum=args.max_source_bytes,
            parent=parent_source,
            library=args.library,
        )
        if checkpoint_source is not None:
            imports_checkpoint_exactly = (
                re.search(
                    rf"(?m)^\s*import\s+{re.escape(checkpoint_module(args.library))}\s*$",
                    candidate,
                )
                is not None
            )
            imports_checkpoint_in_header = checkpoint_module(
                args.library
            ) in _header_imports(candidate)
            if not (imports_checkpoint_exactly and imports_checkpoint_in_header):
                raise PolicyFailure(
                    "delta candidate must import "
                    f"{checkpoint_module(args.library)}"
                )
        receipt["checks"]["source_policy"] = "passed"
        receipt["inputs"]["candidate_bytes"] = len(candidate.encode("utf-8"))
        receipt["inputs"]["candidate_sha256"] = _sha256_text(candidate)
        if checkpoint_source is not None:
            total_candidate_bytes = len(candidate.encode("utf-8")) + len(
                checkpoint_source.encode("utf-8")
            )
            if total_candidate_bytes > args.max_source_bytes:
                raise PolicyFailure(
                    "checkpoint plus candidate exceeds "
                    f"{args.max_source_bytes} UTF-8 bytes"
                )

        assembly = assemble_frozen_source(
            snapshot,
            candidate,
            checkpoint=checkpoint_source,
            maximum=args.max_assembled_bytes,
            axiom_declarations=args.axiom,
            library=args.library,
        )
        receipt["checks"]["assembly"] = "passed"
        receipt["assembly"].update(
            {
                "bytes": len(assembly.source.encode("utf-8")),
                "sha256": _sha256_text(assembly.source),
                "module_count": len(assembly.modules),
                "modules": list(assembly.modules),
                "external_imports": list(assembly.external_imports),
            }
        )
        lean, specification, resolution = lean_resolver(snapshot)
        module_paths = _load_scratch_lean_path(
            root,
            snapshot,
            assembly.external_imports,
        )
        receipt["lean"].update(
            {
                "toolchain": specification,
                "path": str(lean),
                "resolution": resolution,
                "module_path_count": len(module_paths),
            }
        )
        scratch_invocation_id = f"scratch:{uuid.uuid4().hex}"
        scratch_started = time.monotonic()
        if event_sink is not None:
            event_sink(
                "lean.scratch.started",
                {
                    "invocation_id": scratch_invocation_id,
                    "mode": args.mode,
                    "source_sha256": receipt["inputs"]["candidate_sha256"],
                    "assembled_sha256": receipt["assembly"]["sha256"],
                },
            )
        result = lean_runner(
            lean,
            snapshot,
            assembly.source,
            args.timeout_seconds,
            module_paths,
        )
        receipt["lean"].update(
            {
                "process_returncode": result.returncode,
                "elapsed_seconds": round(result.elapsed_seconds, 6),
            }
        )
        receipt["diagnostics"] = compact_diagnostics(result.output)
        receipt["axioms"] = parse_axiom_receipts(result.output, args.axiom)
        if result.returncode != 0:
            receipt["status"] = "lean_rejected"
            receipt["exit_code"] = int(ExitCode.LEAN_REJECTED)
            receipt["checks"]["lean"] = "failed"
            return receipt, ExitCode.LEAN_REJECTED
        receipt["status"] = "lean_ok"
        receipt["exit_code"] = int(ExitCode.OK)
        receipt["checks"]["lean"] = "passed"
        return receipt, ExitCode.OK
    except ScratchFailure as error:
        receipt["status"] = error.status
        receipt["exit_code"] = int(error.exit_code)
        diagnostics = getattr(error, "diagnostics", "")
        message = str(error)
        receipt["diagnostics"] = compact_diagnostics(
            message + (("\n" + diagnostics) if diagnostics else "")
        )
        if error.exit_code == ExitCode.POLICY:
            receipt["checks"]["source_policy"] = "failed"
        elif error.exit_code == ExitCode.ASSEMBLY:
            receipt["checks"]["assembly"] = "failed"
        elif error.exit_code == ExitCode.TIMEOUT:
            receipt["checks"]["lean"] = "timeout"
            receipt["lean"]["elapsed_seconds"] = round(
                getattr(error, "elapsed_seconds", time.monotonic() - started),
                6,
            )
        return receipt, error.exit_code
    finally:
        if scratch_invocation_id is not None and event_sink is not None:
            status = str(receipt["status"])
            observed_status = {
                "lean_ok": "lean_accepted",
                "lean_rejected": "lean_rejected",
            }.get(status, status)
            fields: dict[str, object] = {
                "invocation_id": scratch_invocation_id,
                "mode": args.mode,
                "source_sha256": receipt["inputs"]["candidate_sha256"],
                "assembled_sha256": receipt["assembly"]["sha256"],
                "status": observed_status,
                "duration_ms": max(
                    0,
                    round((time.monotonic() - scratch_started) * 1000),
                ),
                "exit_code": int(receipt["exit_code"]),
            }
            if observed_status not in {"lean_accepted", "lean_rejected"}:
                fields["error_class"] = observed_status
            event_sink("lean.scratch.finished", fields)


def render_human(receipt: dict[str, object]) -> str:
    status = str(receipt["status"]).upper()
    lines = [f"SHINKA LEAN SCRATCH: {status}"]
    lines.append(f"  mode       {receipt['mode']}")
    candidate_hash = receipt["inputs"].get("candidate_sha256")
    if candidate_hash:
        lines.append(f"  candidate  {str(candidate_hash)[:16]}")
    if receipt["assembly"].get("module_count") is not None:
        lines.append(
            "  assembly   "
            f"{receipt['assembly']['module_count']} modules, "
            f"{receipt['assembly']['bytes']} bytes"
        )
    if receipt["lean"].get("toolchain"):
        lines.append(f"  toolchain  {receipt['lean']['toolchain']}")
    if receipt["lean"].get("elapsed_seconds") is not None:
        lines.append(f"  elapsed    {receipt['lean']['elapsed_seconds']}s")
    diagnostics = str(receipt.get("diagnostics") or "")
    if diagnostics:
        lines.extend(["", diagnostics])
    lines.extend(
        [
            "",
            "Scratch feedback only: no fitness, promotion, or proof receipt.",
        ]
    )
    return "\n".join(lines)


class ReceiptArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise UsageFailure(message)


def build_parser() -> argparse.ArgumentParser:
    parser = ReceiptArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=("candidate", "append", "diff"),
        required=True,
        help=(
            "stdin is a complete candidate, an appended Lean snippet, or a unified diff"
        ),
    )
    parser.add_argument(
        "--solve-root",
        type=Path,
        default=Path.cwd(),
        help="frozen solve directory (default: current directory)",
    )
    parser.add_argument(
        "--snapshot-root",
        type=Path,
        default=DEFAULT_SNAPSHOT,
        help="formal snapshot path relative to the solve root",
    )
    parser.add_argument(
        "--parent",
        type=Path,
        help="selected parent path inside the solve root; required for append/diff",
    )
    parser.add_argument(
        "--no-checkpoint",
        action="store_true",
        help="do not auto-load the exact checkpoint_input.lean input",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=DEFAULT_TIMEOUT_SECONDS,
    )
    parser.add_argument(
        "--max-source-bytes",
        type=int,
        default=DEFAULT_MAX_SOURCE_BYTES,
    )
    parser.add_argument(
        "--max-assembled-bytes",
        type=int,
        default=DEFAULT_MAX_ASSEMBLED_BYTES,
    )
    parser.add_argument(
        "--axiom",
        action="append",
        default=[],
        metavar="DECLARATION",
        help="append a scratch-only #print axioms query (repeatable)",
    )
    parser.add_argument(
        "--library",
        default=DEFAULT_LIBRARY,
        help=(
            "Lean library name: the import allowlist prefix, frozen snapshot "
            f"root, and namespace root (default: {DEFAULT_LIBRARY})"
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit the stable machine-readable receipt instead of human output",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    raw_arguments = list(sys.argv[1:] if argv is None else argv)
    json_requested = "--json" in raw_arguments
    mode_hint = "unknown"
    if "--mode" in raw_arguments:
        mode_index = raw_arguments.index("--mode")
        if mode_index + 1 < len(raw_arguments):
            mode_hint = raw_arguments[mode_index + 1]
    try:
        args = parser.parse_args(raw_arguments)
        # Validate bounds before using them to size the stdin read.
        _validate_arguments(args)
        proposal = read_stdin(args.max_source_bytes)

        def emit_observability(
            event: str,
            fields: dict[str, object],
        ) -> None:
            payload = {"event": event, **fields}
            print(
                OBSERVABILITY_MARKER + _canonical_json(payload),
                file=sys.stderr,
                flush=True,
            )

        receipt, exit_code = run_check(
            args,
            proposal,
            event_sink=emit_observability,
        )
    except ScratchFailure as error:
        receipt = _empty_receipt(args.mode if "args" in locals() else mode_hint)
        receipt["status"] = error.status
        receipt["exit_code"] = int(error.exit_code)
        receipt["diagnostics"] = str(error)
        exit_code = error.exit_code
    if json_requested:
        print(json.dumps(receipt, indent=2, sort_keys=True))
    else:
        print(render_human(receipt))
    return int(exit_code)


if __name__ == "__main__":
    raise SystemExit(main())
