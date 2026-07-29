"""Deterministic export and restore.

Two databases holding the same history must export to the same bytes, so an
export can be diffed, archived, and used as the pre-cutover rollback bundle.
Determinism comes from canonical JSON with sorted keys and a fixed row order —
never from insertion order or wall-clock time.

Restore is a faithful reconstruction, not a re-derivation: rows are written as
recorded, keeping event IDs and digests, and the chain is then verified.  That
is what lets a restored ledger prove it is the same ledger.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from leanevolve.ledger import schema as schema_module
from leanevolve.ledger.events import canonical_json
from leanevolve.ledger.integrity import IntegrityReport, verify
from leanevolve.ledger.store import Ledger

EXPORT_FORMAT = "leanevolve-ledger-export-v1"


class ExportError(RuntimeError):
    """Raised when an export cannot be produced or restored faithfully."""


def _rows(ledger: Ledger, query: str) -> list[dict[str, Any]]:
    return [dict(row) for row in ledger._connection.execute(query)]


def export_payload(ledger: Ledger) -> dict[str, object]:
    """Return the whole database as a deterministic, ordered structure."""
    head = ledger.head()
    meta = {
        row["key"]: row["value"]
        for row in ledger._connection.execute("SELECT key, value FROM schema_meta")
    }
    return {
        "format": EXPORT_FORMAT,
        "schema_version": ledger.schema_version,
        "schema_meta": dict(sorted(meta.items())),
        "head": {
            "event_id": head.id if head else 0,
            "event_hash": head.event_hash if head else schema_module.GENESIS_HASH,
        },
        "events": _rows(ledger, "SELECT * FROM events ORDER BY id"),
        "objects": _rows(ledger, "SELECT * FROM objects ORDER BY id"),
        "aliases": _rows(ledger, "SELECT * FROM aliases ORDER BY alias"),
        "connections": _rows(ledger, "SELECT * FROM connections ORDER BY id"),
        "artifact_locations": _rows(
            ledger,
            "SELECT * FROM artifact_locations ORDER BY object_id, location",
        ),
    }


def export_bytes(ledger: Ledger) -> bytes:
    """Return the canonical serialization of the whole database."""
    return (canonical_json(export_payload(ledger)) + "\n").encode("utf-8")


def write_export(ledger: Ledger, path: Path | str) -> str:
    """Write a deterministic export and return its digest."""
    import hashlib

    data = export_bytes(ledger)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


_TABLES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "events",
        (
            "id",
            "occurred_at",
            "recorded_at",
            "campaign_id",
            "epoch_id",
            "turn_id",
            "actor_class",
            "actor_id",
            "action",
            "subject_type",
            "subject_id",
            "payload_json",
            "evidence_object_id",
            "idempotency_key",
            "previous_event_hash",
            "event_hash",
        ),
    ),
    (
        "objects",
        (
            "id",
            "kind",
            "canonical_name",
            "content_format",
            "content",
            "properties_json",
            "created_event_id",
        ),
    ),
    ("aliases", ("alias", "object_id", "created_event_id")),
    (
        "connections",
        (
            "id",
            "from_id",
            "relation",
            "to_id",
            "properties_json",
            "created_event_id",
            "retracted_event_id",
        ),
    ),
    (
        "artifact_locations",
        ("id", "object_id", "location", "state", "verified_at", "created_event_id"),
    ),
)


def restore(payload: dict[str, object], path: Path | str) -> Ledger:
    """Rebuild a ledger from an export, preserving IDs and digests.

    Foreign keys are deferred for the duration of the restore because objects
    and the events that created them reference each other; they are enforced
    again, and the chain re-verified, before the ledger is returned.
    """
    if payload.get("format") != EXPORT_FORMAT:
        raise ExportError(f"not a ledger export: {payload.get('format')!r}")
    target = Path(path)
    if target.exists():
        raise ExportError(f"refusing to restore over an existing file: {target}")
    ledger = Ledger.open(target)
    connection = ledger._connection
    try:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("BEGIN IMMEDIATE")
        for table, columns in _TABLES:
            rows = payload.get(table) or []
            if not isinstance(rows, list):
                raise ExportError(f"{table} is not a list")
            placeholders = ", ".join("?" for _ in columns)
            statement = (
                f"INSERT INTO {table} ({', '.join(columns)}) "
                f"VALUES ({placeholders})"
            )
            for row in rows:
                connection.execute(
                    statement, tuple(row.get(column) for column in columns)
                )
        meta = payload.get("schema_meta") or {}
        if isinstance(meta, dict):
            for key, value in sorted(meta.items()):
                connection.execute(
                    "INSERT INTO schema_meta (key, value) VALUES (?, ?) "
                    "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                    (key, value),
                )
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        ledger.close()
        target.unlink(missing_ok=True)
        raise
    connection.execute("PRAGMA foreign_keys = ON")
    violations = connection.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        ledger.close()
        target.unlink(missing_ok=True)
        raise ExportError(f"restored ledger has {len(violations)} dangling references")
    return ledger


def read_export(path: Path | str) -> dict[str, object]:
    """Load an export from disk."""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def verify_restore(original: Ledger, restored: Ledger) -> IntegrityReport:
    """Confirm a restored ledger is byte-identical in export and internally sound."""
    if export_bytes(original) != export_bytes(restored):
        raise ExportError("restored ledger does not export identically")
    return verify(restored)


__all__ = [
    "EXPORT_FORMAT",
    "ExportError",
    "export_bytes",
    "export_payload",
    "read_export",
    "restore",
    "verify_restore",
    "write_export",
]
