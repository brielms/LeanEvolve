"""Export a hash-bound parent path from Shinka's run database."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from leanevolve.audit import (
    LINEAGE_FORMAT,
    atomic_json,
    file_record,
    sha256_bytes,
)

LINEAGE_NAME = "proof_lineage.json"
DATABASE_NAME = "programs.sqlite"
DATABASE_SNAPSHOT = "programs_snapshot.sqlite"


def _canonical_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sha256_bytes(encoded)


def snapshot_database(run_dir: Path) -> Path | None:
    source_path = run_dir / DATABASE_NAME
    if not source_path.is_file():
        return None
    destination_path = run_dir / DATABASE_SNAPSHOT
    temporary_path = run_dir / (DATABASE_SNAPSHOT + ".tmp")
    source = sqlite3.connect(source_path.resolve().as_uri() + "?mode=ro", uri=True)
    destination = sqlite3.connect(temporary_path)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    temporary_path.replace(destination_path)
    return destination_path


def _rows(database: Path) -> list[dict[str, Any]]:
    connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(programs)")}
        required = {"id", "code", "parent_id", "generation", "timestamp"}
        if not required <= columns:
            raise ValueError("Shinka programs table is missing required columns")
        optional = [
            name
            for name in ("combined_score", "correct", "metadata")
            if name in columns
        ]
        selected = [*sorted(required), *optional]
        rows = connection.execute(
            "SELECT " + ", ".join(selected) + " FROM programs "
            "ORDER BY generation ASC, timestamp ASC, id ASC"
        ).fetchall()
    finally:
        connection.close()
    return [dict(row) for row in rows]


def _proposal_artifacts(generation_dir: Path, run_dir: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if not generation_dir.is_dir():
        return records
    for path in sorted(generation_dir.rglob("*")):
        if not path.is_file() or path.name == "main.lean" or "results" in path.parts:
            continue
        records.append(
            {"path": path.relative_to(run_dir).as_posix(), **file_record(path)}
        )
    return records


def write_lineage(run_dir: Path) -> dict[str, Any] | None:
    database = snapshot_database(run_dir)
    if database is None:
        return None
    nodes: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    for row in _rows(database):
        program_id = str(row["id"])
        parent_id = str(row["parent_id"]) if row.get("parent_id") else None
        generation = int(row["generation"])
        generation_dir = run_dir / f"gen_{generation}"
        candidate_path = generation_dir / "main.lean"
        receipt_path = generation_dir / "results" / "evaluation_manifest.json"
        candidate = file_record(candidate_path) if candidate_path.is_file() else None
        receipt = file_record(receipt_path) if receipt_path.is_file() else None
        accepted: list[str] = []
        if receipt_path.is_file():
            accepted = list(
                json.loads(receipt_path.read_text(encoding="utf-8")).get(
                    "accepted_goals", []
                )
            )
        parent = by_id.get(parent_id) if parent_id else None
        if parent_id and parent is None:
            errors.append(f"unresolved parent {parent_id} for {program_id}")
        parent_goals = set(parent["accepted_goals"]) if parent else set()
        metadata = str(row.get("metadata") or "")
        core = {
            "program_id": program_id,
            "generation": generation,
            "parent_id": parent_id,
            "parent_node_sha256": parent.get("node_sha256") if parent else None,
            "candidate": candidate,
            "evaluation_receipt": receipt,
            "code_sha256": sha256_bytes(str(row["code"]).encode("utf-8")),
            "metadata_sha256": sha256_bytes(metadata.encode("utf-8")),
            "accepted_goals": accepted,
            "newly_accepted_goals": sorted(set(accepted) - parent_goals),
            "combined_score": float(row.get("combined_score") or 0.0),
            "correct": bool(row.get("correct")),
            "proposal_artifacts": _proposal_artifacts(generation_dir, run_dir),
        }
        node = {**core, "node_sha256": _canonical_hash(core)}
        nodes.append(node)
        by_id[program_id] = node
    frontier = max(
        nodes,
        key=lambda item: (
            item["combined_score"],
            len(item["accepted_goals"]),
            item["generation"],
        ),
        default=None,
    )
    frontier_path: list[dict[str, Any]] = []
    cursor = frontier
    seen: set[str] = set()
    while cursor is not None:
        if cursor["program_id"] in seen:
            errors.append("cycle in parent lineage")
            break
        seen.add(cursor["program_id"])
        frontier_path.append(cursor)
        cursor = by_id.get(cursor["parent_id"]) if cursor["parent_id"] else None
    frontier_path.reverse()
    core_payload = {
        "format": LINEAGE_FORMAT,
        "database_snapshot": file_record(database),
        "nodes": nodes,
        "frontier_program_id": frontier["program_id"] if frontier else None,
        "frontier_path_program_ids": [item["program_id"] for item in frontier_path],
        "frontier_accepted_goals": frontier["accepted_goals"] if frontier else [],
        "lineage_complete": not errors,
        "errors": errors,
    }
    payload = {**core_payload, "lineage_sha256": _canonical_hash(core_payload)}
    atomic_json(run_dir / LINEAGE_NAME, payload)
    return payload


def verify_lineage(run_dir: Path) -> list[str]:
    path = run_dir / LINEAGE_NAME
    if not path.is_file():
        return ["missing proof lineage"]
    payload = json.loads(path.read_text(encoding="utf-8"))
    recorded = payload.pop("lineage_sha256", None)
    errors: list[str] = []
    if recorded != _canonical_hash(payload):
        errors.append("proof lineage hash mismatch")
    for node in payload.get("nodes", []):
        node_copy = dict(node)
        node_hash = node_copy.pop("node_sha256", None)
        if node_hash != _canonical_hash(node_copy):
            errors.append(f"node hash mismatch: {node.get('program_id')}")
        generation = node.get("generation")
        for key, relative in (
            ("candidate", f"gen_{generation}/main.lean"),
            (
                "evaluation_receipt",
                f"gen_{generation}/results/evaluation_manifest.json",
            ),
        ):
            expected = node.get(key)
            file_path = run_dir / relative
            if expected is None:
                continue
            # Older lineages stored the relative path alongside the hash and
            # size.  The path is already fixed by `relative` above; compare it
            # separately so this schema extension does not look like changed
            # candidate bytes.
            expected_path = expected.get("path") if isinstance(expected, dict) else None
            expected_record = (
                {name: value for name, value in expected.items() if name != "path"}
                if isinstance(expected, dict)
                else expected
            )
            if (
                not file_path.is_file()
                or (expected_path is not None and expected_path != relative)
                or file_record(file_path) != expected_record
            ):
                errors.append(f"hash/size mismatch for {key}: {node.get('program_id')}")
    return errors
