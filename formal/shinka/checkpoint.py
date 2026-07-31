"""Hash-pinned cumulative Lean checkpoints and independently materialized deltas."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

from formal.shinka.audit import file_record, utc_now


START_MARKER = "-- EVOLVE-BLOCK-START"
END_MARKER = "-- EVOLVE-BLOCK-END"
APPEND_SENTINEL = "-- SHINKA-APPEND-HERE"
CHECKPOINT_NAMESPACE = "Demo.Generated"
CHECKPOINT_MODULE = f"{CHECKPOINT_NAMESPACE}.Checkpoint"


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _evolve_block_bounds(source: str) -> tuple[list[str], int, int]:
    """Return source lines and the unique exact-line marker positions."""
    lines = source.splitlines(keepends=True)
    starts = [
        index for index, line in enumerate(lines)
        if line.strip() == START_MARKER
    ]
    ends = [
        index for index, line in enumerate(lines)
        if line.strip() == END_MARKER
    ]
    if len(starts) != 1 or len(ends) != 1 or starts[0] >= ends[0]:
        raise ValueError("candidate needs exactly one ordered evolve block")
    return lines, starts[0], ends[0]


def evolve_block(source: str) -> str:
    """Extract the exact editable region from a complete Lean candidate."""

    lines, start, end = _evolve_block_bounds(source)
    return "".join(lines[start + 1:end])


def delta_seed_source() -> str:
    return f"""\
import {CHECKPOINT_MODULE}

namespace {CHECKPOINT_NAMESPACE}

{START_MARKER}
{APPEND_SENTINEL}
{END_MARKER}

end {CHECKPOINT_NAMESPACE}
"""


def materialize_sources(checkpoint_source: str, delta_source: str) -> str:
    """Create one deterministic standalone file with one final sentinel."""

    checkpoint_lines, checkpoint_start, checkpoint_end = _evolve_block_bounds(
        checkpoint_source
    )
    delta_lines = evolve_block(delta_source).splitlines(keepends=True)
    prior_lines = checkpoint_lines[checkpoint_start + 1:checkpoint_end]

    def content(lines: list[str]) -> str:
        value = "".join(
            line for line in lines if line.strip() != APPEND_SENTINEL
        )
        if value and not value.endswith("\n"):
            value += "\n"
        return value

    prefix_end = sum(
        len(line) for line in checkpoint_lines[:checkpoint_start + 1]
    )
    suffix_start = sum(
        len(line) for line in checkpoint_lines[:checkpoint_end]
    )
    return (
        checkpoint_source[:prefix_end]
        + content(prior_lines)
        + content(delta_lines)
        + APPEND_SENTINEL
        + "\n"
        + checkpoint_source[suffix_start:]
    )


def prepare_checkpoint(
    cumulative_candidate: Path,
    output_dir: Path,
) -> dict[str, object]:
    """Freeze one cumulative candidate and emit a tiny editable delta seed."""

    source_path = cumulative_candidate.resolve()
    destination = output_dir.resolve()
    if destination.exists():
        raise ValueError(f"refusing to overwrite checkpoint: {destination}")
    source = source_path.read_text(encoding="utf-8")
    evolve_block(source)
    destination.mkdir(parents=True)
    checkpoint_path = destination / "checkpoint.lean"
    seed_path = destination / "delta_seed.lean"
    checkpoint_path.write_text(source, encoding="utf-8")
    seed_path.write_text(delta_seed_source(), encoding="utf-8")
    manifest = {
        "format": "shinka-lean-checkpoint-v1",
        "created_at_utc": utc_now(),
        "source_candidate": {
            "path": str(source_path),
            **file_record(source_path),
        },
        "checkpoint": {
            "path": checkpoint_path.name,
            **file_record(checkpoint_path),
        },
        "delta_seed": {
            "path": seed_path.name,
            **file_record(seed_path),
        },
        "materialization_rule": (
            "insert the delta evolve block immediately before the checkpoint "
            "end marker"
        ),
        "trust_status": (
            "checkpoint and delta remain untrusted until independently "
            "kernel-audited after materialization"
        ),
    }
    (destination / "checkpoint_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def materialize_files(
    checkpoint: Path,
    delta: Path,
    destination: Path,
) -> dict[str, object]:
    checkpoint_path = checkpoint.resolve()
    delta_path = delta.resolve()
    output_path = destination.resolve()
    if output_path.exists():
        raise ValueError(f"refusing to overwrite materialization: {output_path}")
    source = materialize_sources(
        checkpoint_path.read_text(encoding="utf-8"),
        delta_path.read_text(encoding="utf-8"),
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output_path.name}.",
        suffix=".tmp",
        dir=output_path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(source)
        os.replace(temporary, output_path)
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "checkpoint": {
            "path": str(checkpoint_path),
            **file_record(checkpoint_path),
        },
        "delta": {"path": str(delta_path), **file_record(delta_path)},
        "materialized": {
            "path": str(output_path),
            **file_record(output_path),
        },
        "materialized_source_sha256": sha256_text(source),
    }
