"""Tamper-evident manifests for untrusted Shinka discovery runs."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import platform
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


RUN_MANIFEST_FORMAT = "shinka-lean-run-v1"
EVALUATION_MANIFEST_FORMAT = "shinka-lean-evaluation-v1"
RUN_MANIFEST_NAME = "run_manifest.json"
EVENT_LOG_NAME = "events.jsonl"


def ignored_result_path(path: Path) -> bool:
    """Files that are transient filesystem/database implementation details."""

    name = path.name
    if name.startswith("._"):
        return True
    # Importing an immutable snapshotted Python module may create interpreter
    # bytecode beside it.  Bytecode is a derived cache, not a research result;
    # inventorying it would make a read-only inspection invalidate the run it
    # is inspecting.  Source bytes remain fully recorded and verified.
    if "__pycache__" in path.parts or name.endswith((".pyc", ".pyo")):
        return True
    if name.endswith((".sqlite-wal", ".sqlite-shm")):
        return True
    if name.endswith(".sqlite") and not name.endswith(
        "_snapshot.sqlite"
    ):
        return True
    return False


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def file_record(path: Path) -> dict[str, object]:
    return {
        "sha256": sha256_file(path),
        "bytes": path.stat().st_size,
    }


def record_set_sha256(
    records: dict[str, dict[str, object]],
) -> str:
    canonical = json.dumps(
        records,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def source_inputs(
    repository_root: Path,
    paths: Iterable[Path],
) -> dict[str, dict[str, object]]:
    records: dict[str, dict[str, object]] = {}
    for path in sorted({item.resolve() for item in paths}):
        relative = str(path.relative_to(repository_root.resolve()))
        records[relative] = file_record(path)
    return records


#: Name of the Lean library a project builds, used for the root module file
#: and the directory holding its modules.  Projects with a different library
#: name pass it explicitly rather than relying on this default.
DEFAULT_LIBRARY = "Generated"


def formal_source_paths(
    formal_root: Path,
    library: str = DEFAULT_LIBRARY,
) -> list[Path]:
    paths = [
        formal_root / "lakefile.toml",
        formal_root / "lake-manifest.json",
        formal_root / "lean-toolchain",
        formal_root / f"{library}.lean",
    ]
    paths.extend(
        sorted(
            path
            for path in (formal_root / library).rglob("*.lean")
            if not path.name.startswith("._")
        )
    )
    return paths


def shinka_provenance() -> dict[str, object]:
    distribution = importlib.metadata.distribution("shinka-evolve")
    direct_url_text = distribution.read_text("direct_url.json")
    direct_url = (
        json.loads(direct_url_text) if direct_url_text else None
    )
    return {
        "version": distribution.version,
        "direct_url": direct_url,
    }


def _atomic_json(path: Path, payload: dict[str, object]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n"
    )
    os.replace(temporary, path)


def append_event(
    results_dir: Path,
    event: str,
    details: dict[str, object] | None = None,
) -> None:
    path = results_dir / EVENT_LOG_NAME
    previous_hash = ""
    sequence = 0
    if path.exists():
        lines = path.read_text().splitlines()
        sequence = len(lines)
        if lines:
            previous_hash = hashlib.sha256(
                lines[-1].encode("utf-8")
            ).hexdigest()
    core = {
        "sequence": sequence,
        "timestamp_utc": utc_now(),
        "event": event,
        "details": details or {},
        "previous_line_sha256": previous_hash,
    }
    canonical = json.dumps(core, sort_keys=True, separators=(",", ":"))
    record = {
        **core,
        "record_sha256": hashlib.sha256(
            canonical.encode("utf-8")
        ).hexdigest(),
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def create_run_manifest(
    results_dir: Path,
    repository_root: Path,
    input_paths: Iterable[Path],
    configuration: dict[str, object],
) -> None:
    results_dir.mkdir(parents=True, exist_ok=False)
    resolved_inputs = sorted({item.resolve() for item in input_paths})
    input_records = source_inputs(repository_root, resolved_inputs)
    payload: dict[str, object] = {
        "format": RUN_MANIFEST_FORMAT,
        "status": "running",
        "started_at_utc": utc_now(),
        "finished_at_utc": None,
        "configuration": configuration,
        "platform": {
            "python": platform.python_version(),
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
        },
        "shinka": shinka_provenance(),
        "inputs": input_records,
        "input_snapshot": "input_snapshot",
        "result_files": {},
        "audit_limitations": [
            "Model generation is stochastic and is not expected to "
            "reproduce byte-for-byte.",
            "Recorded candidates and kernel-derived fitness are replayable.",
            "Hashes are tamper-evident only while this manifest is anchored "
            "in trusted versioned or immutable storage.",
            "This run manifest is not a mathematical proof object.",
        ],
    }
    _atomic_json(results_dir / RUN_MANIFEST_NAME, payload)
    snapshot_root = results_dir / "input_snapshot"
    for source in resolved_inputs:
        relative = source.relative_to(repository_root.resolve())
        destination = snapshot_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        if file_record(destination) != input_records[str(relative)]:
            raise RuntimeError(
                f"input snapshot verification failed: {relative}"
            )
    append_event(results_dir, "run_started")
    append_event(
        results_dir,
        "input_snapshot_completed",
        {
            "path": "input_snapshot",
            "file_count": len(input_records),
        },
    )


def _result_inventory(results_dir: Path) -> dict[str, dict[str, object]]:
    records: dict[str, dict[str, object]] = {}
    for path in sorted(item for item in results_dir.rglob("*")
                       if item.is_file()
                       and not ignored_result_path(item)):
        relative = str(path.relative_to(results_dir))
        if relative == RUN_MANIFEST_NAME:
            continue
        records[relative] = file_record(path)
    return records


def finalize_run_manifest(
    results_dir: Path,
    status: str,
    failure_type: str | None = None,
) -> None:
    append_event(
        results_dir,
        "run_finished",
        {
            "status": status,
            "failure_type": failure_type,
        },
    )
    path = results_dir / RUN_MANIFEST_NAME
    payload = json.loads(path.read_text())
    payload["status"] = status
    payload["finished_at_utc"] = utc_now()
    payload["failure_type"] = failure_type
    payload["result_files"] = _result_inventory(results_dir)
    _atomic_json(path, payload)


def verify_records(
    base: Path,
    records: dict[str, dict[str, object]],
    exclude: set[str] | None = None,
) -> list[str]:
    errors: list[str] = []
    excluded = exclude or set()
    for relative, expected in sorted(records.items()):
        if relative in excluded:
            continue
        path = base / relative
        if not path.is_file():
            errors.append(f"missing: {relative}")
            continue
        actual = file_record(path)
        if actual != expected:
            errors.append(
                f"hash/size mismatch: {relative}; "
                f"expected={expected}, actual={actual}"
            )
    return errors


def verify_inventory(
    base: Path,
    records: dict[str, dict[str, object]],
    exclude: set[str] | None = None,
) -> list[str]:
    """Verify both recorded content and absence of unrecorded files."""

    excluded = exclude or set()
    errors = verify_records(base, records, excluded)
    expected = {
        relative for relative in records if relative not in excluded
    }
    actual = {
        str(path.relative_to(base))
        for path in base.rglob("*")
        if path.is_file()
        and not ignored_result_path(path)
        and str(path.relative_to(base)) not in excluded
    }
    for relative in sorted(actual - expected):
        errors.append(f"unrecorded file: {relative}")
    return errors


def verify_event_chain(path: Path) -> list[str]:
    """Check sequence numbers and both links in the JSONL hash chain."""

    if not path.is_file():
        return [f"missing: {path.name}"]
    errors: list[str] = []
    previous_line_hash = ""
    for expected_sequence, line in enumerate(path.read_text().splitlines()):
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            errors.append(
                f"invalid JSON at event {expected_sequence}: {error.msg}"
            )
            previous_line_hash = hashlib.sha256(
                line.encode("utf-8")
            ).hexdigest()
            continue
        if not isinstance(record, dict):
            errors.append(
                f"event {expected_sequence} is not a JSON object"
            )
            continue
        if record.get("sequence") != expected_sequence:
            errors.append(
                f"event sequence mismatch at line {expected_sequence}"
            )
        if record.get("previous_line_sha256") != previous_line_hash:
            errors.append(
                f"previous hash mismatch at event {expected_sequence}"
            )
        claimed_record_hash = record.get("record_sha256")
        core = {
            key: value
            for key, value in record.items()
            if key != "record_sha256"
        }
        canonical = json.dumps(
            core, sort_keys=True, separators=(",", ":")
        )
        actual_record_hash = hashlib.sha256(
            canonical.encode("utf-8")
        ).hexdigest()
        if claimed_record_hash != actual_record_hash:
            errors.append(
                f"record hash mismatch at event {expected_sequence}"
            )
        previous_line_hash = hashlib.sha256(
            line.encode("utf-8")
        ).hexdigest()
    return errors
