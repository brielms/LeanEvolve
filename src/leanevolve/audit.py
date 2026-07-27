"""Tamper-evident manifests for discovery runs and evaluations."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import platform
import shutil
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from leanevolve import __version__

RUN_FORMAT = "leanevolve-run-v1"
EVALUATION_FORMAT = "leanevolve-evaluation-v1"
LINEAGE_FORMAT = "leanevolve-lineage-v1"
RUN_MANIFEST = "run_manifest.json"
EVENTS = "events.jsonl"


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def file_record(path: Path) -> dict[str, Any]:
    return {"sha256": sha256_file(path), "bytes": path.stat().st_size}


def record_set_sha256(records: dict[str, dict[str, Any]]) -> str:
    encoded = json.dumps(records, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sha256_bytes(encoded)


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def relative_records(base: Path, paths: Iterable[Path]) -> dict[str, dict[str, Any]]:
    root = base.resolve()
    records: dict[str, dict[str, Any]] = {}
    for path in sorted({item.resolve() for item in paths}):
        if not path.is_relative_to(root):
            raise ValueError(f"input escapes configuration directory: {path.name}")
        relative = path.relative_to(root).as_posix()
        records[relative] = file_record(path)
    return records


def append_event(
    run_dir: Path,
    event: str,
    details: dict[str, Any] | None = None,
) -> None:
    path = run_dir / EVENTS
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    core = {
        "sequence": len(lines),
        "timestamp_utc": utc_now(),
        "event": event,
        "details": details or {},
        "previous_line_sha256": sha256_bytes(lines[-1].encode("utf-8"))
        if lines
        else "",
    }
    canonical = json.dumps(core, sort_keys=True, separators=(",", ":"))
    record = {**core, "record_sha256": sha256_bytes(canonical.encode("utf-8"))}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def _shinka_version() -> dict[str, Any] | None:
    try:
        distribution = importlib.metadata.distribution("shinka-evolve")
    except importlib.metadata.PackageNotFoundError:
        return None
    direct_text = distribution.read_text("direct_url.json")
    return {
        "version": distribution.version,
        "direct_url": json.loads(direct_text) if direct_text else None,
    }


def create_run_manifest(
    run_dir: Path,
    config_root: Path,
    configuration: dict[str, Any],
    inputs: Iterable[Path],
    run_parameters: dict[str, Any],
    tool_inputs: Iterable[Path] = (),
) -> dict[str, Any]:
    """Create a run directory and immutable, path-portable input snapshot."""

    run_dir.mkdir(parents=True, exist_ok=False)
    records = relative_records(config_root, inputs)
    snapshot = run_dir / "input_snapshot"
    for relative, expected in records.items():
        source = config_root / relative
        destination = snapshot / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        if file_record(destination) != expected:
            raise RuntimeError(f"snapshot verification failed: {relative}")
    tool_paths = sorted({item.resolve() for item in tool_inputs})
    tool_records = {f"leanevolve/{path.name}": file_record(path) for path in tool_paths}
    tool_snapshot = run_dir / "tool_snapshot"
    for path in tool_paths:
        relative = f"leanevolve/{path.name}"
        destination = tool_snapshot / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)
        if file_record(destination) != tool_records[relative]:
            raise RuntimeError(f"tool snapshot verification failed: {relative}")
    payload = {
        "format": RUN_FORMAT,
        "status": "running",
        "started_at_utc": utc_now(),
        "finished_at_utc": None,
        "leanevolve_version": __version__,
        "shinka": _shinka_version(),
        "platform": {
            "python": platform.python_version(),
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
        },
        "configuration": configuration,
        "run_parameters": run_parameters,
        "inputs": records,
        "inputs_sha256": record_set_sha256(records),
        "input_snapshot": "input_snapshot",
        "tool_inputs": tool_records,
        "tool_inputs_sha256": record_set_sha256(tool_records),
        "tool_snapshot": "tool_snapshot",
        "result_files": {},
        "audit_limitations": [
            "Model generation is stochastic and need not replay byte-for-byte.",
            "Recorded candidates and kernel evaluations are deterministic inputs.",
            "Hashes are tamper-evident only after an external trusted anchor exists.",
            "The run manifest is audit evidence, not a mathematical proof object.",
        ],
    }
    atomic_json(run_dir / RUN_MANIFEST, payload)
    append_event(run_dir, "run_started")
    append_event(
        run_dir,
        "input_snapshot_completed",
        {
            "file_count": len(records),
            "inputs_sha256": payload["inputs_sha256"],
            "tool_file_count": len(tool_records),
            "tool_inputs_sha256": payload["tool_inputs_sha256"],
        },
    )
    return payload


def ignored_result(path: Path) -> bool:
    name = path.name
    return (
        name.startswith("._")
        or name.endswith((".sqlite-wal", ".sqlite-shm"))
        or (name.endswith(".sqlite") and not name.endswith("_snapshot.sqlite"))
        or name.endswith(".tmp")
    )


def result_inventory(run_dir: Path) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for path in sorted(run_dir.rglob("*")):
        if not path.is_file() or ignored_result(path):
            continue
        relative = path.relative_to(run_dir).as_posix()
        if relative == RUN_MANIFEST:
            continue
        records[relative] = file_record(path)
    return records


def finalize_run_manifest(
    run_dir: Path, status: str, failure_type: str | None = None
) -> dict[str, Any]:
    manifest_path = run_dir / RUN_MANIFEST
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    append_event(
        run_dir,
        "run_finished",
        {"status": status, "failure_type": failure_type},
    )
    payload["status"] = status
    payload["failure_type"] = failure_type
    payload["finished_at_utc"] = utc_now()
    payload["result_files"] = result_inventory(run_dir)
    payload["result_files_sha256"] = record_set_sha256(payload["result_files"])
    atomic_json(manifest_path, payload)
    return payload


def verify_event_chain(path: Path) -> list[str]:
    errors: list[str] = []
    previous_line = ""
    for index, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            errors.append(f"invalid JSON at event {index}")
            previous_line = line
            continue
        recorded_hash = record.pop("record_sha256", None)
        canonical = json.dumps(record, sort_keys=True, separators=(",", ":"))
        if recorded_hash != sha256_bytes(canonical.encode("utf-8")):
            errors.append(f"record hash mismatch at event {index}")
        if record.get("sequence") != index:
            errors.append(f"sequence mismatch at event {index}")
        expected_previous = (
            sha256_bytes(previous_line.encode("utf-8")) if previous_line else ""
        )
        if record.get("previous_line_sha256") != expected_previous:
            errors.append(f"previous hash mismatch at event {index}")
        previous_line = line
    return errors


def verify_inventory(run_dir: Path, expected: dict[str, dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    actual = result_inventory(run_dir)
    for relative, record in expected.items():
        if relative not in actual:
            errors.append(f"missing file: {relative}")
        elif actual[relative] != record:
            errors.append(f"hash/size mismatch: {relative}")
    for relative in actual.keys() - expected.keys():
        errors.append(f"unrecorded file: {relative}")
    return sorted(errors)
