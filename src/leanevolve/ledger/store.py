"""Transactional object, connection, and event API.

Every canonical mutation goes through a write session, and a write session is
exactly one SQLite transaction and one contiguous run of the hash chain.  There
is no path that changes an object without appending the event that explains it,
which is what makes the audit history complete rather than best-effort.

Writes are idempotent.  Re-creating an identical object or edge is a no-op
rather than a duplicate, and an event carrying an idempotency key that has
already been committed returns the original event.  Replaying an interrupted
import is therefore safe, and a caller that cannot tell whether its last write
landed may simply repeat it.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from leanevolve.ledger import schema as schema_module
from leanevolve.ledger.events import (
    Envelope,
    Event,
    EventError,
    canonical_json,
    event_from_row,
    prepare,
)
from leanevolve.ledger.schema import GENESIS_HASH
from leanevolve.ledger.vocabulary import (
    CONTENT_FORMATS,
    RETENTION_STATES,
    VocabularyError,
    require_object_kind,
    validate_connection,
)

#: Content-addressed objects are named by their digest, never by a path.
ARTIFACT_ID_PREFIX = "artifact:sha256:"

_SHA256 = re.compile(r"[0-9a-f]{64}")


class LedgerError(RuntimeError):
    """Raised when a write cannot be applied to canonical state."""


class ConflictError(LedgerError):
    """Raised when a write contradicts a record that already exists."""


@dataclass(frozen=True)
class ObjectRecord:
    """A stable thing that can be named, connected, or acted upon."""

    id: str
    kind: str
    canonical_name: str
    content_format: str
    content: str
    properties: Mapping[str, object]
    created_event_id: int

    def identity(self) -> tuple[str, str, str, str, str]:
        """Return the fields that make two creations the same creation."""
        return (
            self.kind,
            self.canonical_name,
            self.content_format,
            self.content,
            canonical_json(dict(self.properties)),
        )


@dataclass(frozen=True)
class ConnectionRecord:
    """A typed, directed relationship between two objects."""

    id: int
    from_id: str
    relation: str
    to_id: str
    properties: Mapping[str, object]
    created_event_id: int
    retracted_event_id: int | None = None

    @property
    def is_active(self) -> bool:
        return self.retracted_event_id is None


@dataclass(frozen=True)
class LocationRecord:
    """Where an artifact's bytes are believed to be.  Replaceable, not identity."""

    object_id: str
    location: str
    state: str
    verified_at: str | None
    created_event_id: int

    @property
    def is_present(self) -> bool:
        return self.state == "present"


def _object_from_row(row: Mapping[str, Any]) -> ObjectRecord:
    return ObjectRecord(
        id=str(row["id"]),
        kind=str(row["kind"]),
        canonical_name=str(row["canonical_name"]),
        content_format=str(row["content_format"]),
        content=str(row["content"]),
        properties=json.loads(str(row["properties_json"])),
        created_event_id=int(row["created_event_id"]),
    )


def _connection_from_row(row: Mapping[str, Any]) -> ConnectionRecord:
    retracted = row["retracted_event_id"]
    return ConnectionRecord(
        id=int(row["id"]),
        from_id=str(row["from_id"]),
        relation=str(row["relation"]),
        to_id=str(row["to_id"]),
        properties=json.loads(str(row["properties_json"])),
        created_event_id=int(row["created_event_id"]),
        retracted_event_id=int(retracted) if retracted is not None else None,
    )


@dataclass
class WriteSession:
    """One transaction, one contiguous run of the chain.

    The session is handed out by :meth:`Ledger.write` and must not outlive its
    ``with`` block; every method appends an event before it touches state.
    """

    ledger: Ledger
    envelope: Envelope
    _next_id: int
    _previous_hash: str
    _written: list[Event] = field(default_factory=list)

    # -- events ------------------------------------------------------------

    def record(
        self,
        action: str,
        subject_id: str,
        payload: Mapping[str, object] | None = None,
        *,
        subject_type: str = "object",
        evidence_object_id: str | None = None,
        idempotency_key: str | None = None,
        occurred_at: str | None = None,
    ) -> Event:
        """Append one event, or return the existing one for a repeated key."""
        if idempotency_key is not None:
            existing = self.ledger._event_by_key(idempotency_key)
            if existing is not None:
                return existing
        if evidence_object_id is not None and not self.ledger._object_exists(
            evidence_object_id
        ):
            raise LedgerError(
                f"evidence {evidence_object_id!r} is not a known object"
            )
        try:
            event = prepare(
                event_id=self._next_id,
                envelope=self.envelope,
                action=action,
                subject_type=subject_type,
                subject_id=subject_id,
                payload=payload,
                evidence_object_id=evidence_object_id,
                idempotency_key=idempotency_key,
                previous_event_hash=self._previous_hash,
                occurred_at=occurred_at,
            )
        except EventError as error:
            raise LedgerError(str(error)) from error
        self.ledger._insert_event(event)
        self._next_id += 1
        self._previous_hash = event.event_hash
        self._written.append(event)
        return event

    # -- objects -----------------------------------------------------------

    def create_object(
        self,
        object_id: str,
        kind: str,
        canonical_name: str,
        *,
        content_format: str = "text",
        content: str = "",
        properties: Mapping[str, object] | None = None,
        occurred_at: str | None = None,
    ) -> ObjectRecord:
        """Create an object, or return the identical one that already exists."""
        try:
            declared = require_object_kind(kind)
        except VocabularyError as error:
            raise LedgerError(str(error)) from error
        if content_format not in CONTENT_FORMATS:
            raise LedgerError(f"unknown content format: {content_format!r}")
        if declared.content_formats and content_format not in declared.content_formats:
            raise LedgerError(
                f"kind {kind!r} does not accept content format "
                f"{content_format!r}; permitted: "
                f"{', '.join(sorted(declared.content_formats))}"
            )
        supplied = dict(properties or {})
        missing = [
            name for name in declared.required_properties if name not in supplied
        ]
        if missing:
            raise LedgerError(
                f"kind {kind!r} requires properties: {', '.join(missing)}"
            )
        candidate = ObjectRecord(
            id=object_id,
            kind=kind,
            canonical_name=canonical_name,
            content_format=content_format,
            content=content,
            properties=supplied,
            created_event_id=0,
        )
        existing = self.ledger.object(object_id)
        if existing is not None:
            if existing.identity() != candidate.identity():
                raise ConflictError(
                    f"object {object_id!r} already exists with different content"
                )
            return existing
        event = self.record(
            "object_created",
            object_id,
            {
                "kind": kind,
                "canonical_name": canonical_name,
                "content_format": content_format,
                "properties": supplied,
            },
            occurred_at=occurred_at,
        )
        self.ledger._connection.execute(
            "INSERT INTO objects (id, kind, canonical_name, content_format, "
            "content, properties_json, created_event_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                object_id,
                kind,
                canonical_name,
                content_format,
                content,
                canonical_json(supplied),
                event.id,
            ),
        )
        stored = self.ledger.object(object_id)
        assert stored is not None
        return stored

    def add_alias(self, alias: str, object_id: str) -> None:
        """Point a rename or traceability identifier at an existing object."""
        if not self.ledger._object_exists(object_id):
            raise LedgerError(f"unknown object: {object_id!r}")
        row = self.ledger._connection.execute(
            "SELECT object_id FROM aliases WHERE alias = ?", (alias,)
        ).fetchone()
        if row is not None:
            if str(row["object_id"]) != object_id:
                raise ConflictError(
                    f"alias {alias!r} already points at {row['object_id']!r}"
                )
            return
        event = self.record("alias_added", object_id, {"alias": alias})
        self.ledger._connection.execute(
            "INSERT INTO aliases (alias, object_id, created_event_id) VALUES (?, ?, ?)",
            (alias, object_id, event.id),
        )

    # -- connections -------------------------------------------------------

    def connect(
        self,
        from_id: str,
        relation: str,
        to_id: str,
        properties: Mapping[str, object] | None = None,
        *,
        occurred_at: str | None = None,
    ) -> ConnectionRecord:
        """Assert a typed edge, or return the identical active one."""
        source = self.ledger.object(from_id)
        target = self.ledger.object(to_id)
        if source is None:
            raise LedgerError(f"unknown source object: {from_id!r}")
        if target is None:
            raise LedgerError(f"unknown target object: {to_id!r}")
        supplied = dict(properties or {})
        try:
            validate_connection(relation, source.kind, target.kind, supplied)
        except VocabularyError as error:
            raise LedgerError(str(error)) from error
        existing = self.ledger.connection(from_id, relation, to_id)
        if existing is not None and existing.is_active:
            if canonical_json(dict(existing.properties)) != canonical_json(supplied):
                raise ConflictError(
                    f"connection {from_id!r} -{relation}-> {to_id!r} already "
                    "exists with different properties"
                )
            return existing
        event = self.record(
            "connection_created",
            from_id,
            {"relation": relation, "from_id": from_id, "to_id": to_id,
             "properties": supplied},
            subject_type="connection",
            occurred_at=occurred_at,
        )
        if existing is not None:
            # A retracted edge is re-asserted as a new fact rather than by
            # clearing the retraction, so both remain in the history.
            self.ledger._connection.execute(
                "UPDATE connections SET properties_json = ?, created_event_id = ?, "
                "retracted_event_id = NULL WHERE id = ?",
                (canonical_json(supplied), event.id, existing.id),
            )
        else:
            self.ledger._connection.execute(
                "INSERT INTO connections (from_id, relation, to_id, "
                "properties_json, created_event_id) VALUES (?, ?, ?, ?, ?)",
                (from_id, relation, to_id, canonical_json(supplied), event.id),
            )
        stored = self.ledger.connection(from_id, relation, to_id)
        assert stored is not None
        return stored

    def retract(
        self, from_id: str, relation: str, to_id: str, *, reason: str
    ) -> None:
        """Remove an edge from the current view while preserving its history."""
        existing = self.ledger.connection(from_id, relation, to_id)
        if existing is None:
            raise LedgerError(
                f"no connection {from_id!r} -{relation}-> {to_id!r} to retract"
            )
        if not existing.is_active:
            return
        event = self.record(
            "connection_retracted",
            from_id,
            {"reason": reason},
            subject_type="connection",
        )
        self.ledger._connection.execute(
            "UPDATE connections SET retracted_event_id = ? WHERE id = ?",
            (event.id, existing.id),
        )

    # -- artifacts ---------------------------------------------------------

    def register_artifact(
        self,
        sha256: str,
        *,
        artifact_type: str,
        byte_size: int,
        media_type: str,
        canonical_name: str | None = None,
    ) -> ObjectRecord:
        """Enter content-addressed bytes into the graph.

        The hash is the identity, so registering the same bytes twice is the
        same registration.  Where those bytes currently sit is a separate,
        replaceable fact recorded by :meth:`add_location`.
        """
        if not _SHA256.fullmatch(sha256):
            raise LedgerError(f"not a lowercase hex sha256: {sha256!r}")
        if byte_size < 0:
            raise LedgerError("byte_size must not be negative")
        object_id = f"{ARTIFACT_ID_PREFIX}{sha256}"
        record = self.create_object(
            object_id,
            "artifact",
            canonical_name or object_id,
            content_format="none",
            properties={
                "sha256": sha256,
                "artifact_type": artifact_type,
                "byte_size": byte_size,
                "media_type": media_type,
            },
        )
        self.record(
            "artifact_registered",
            object_id,
            {
                "sha256": sha256,
                "byte_size": byte_size,
                "artifact_type": artifact_type,
            },
            idempotency_key=f"artifact_registered:{sha256}",
        )
        return record

    def add_location(self, object_id: str, location: str) -> None:
        """Record that a replaceable location holds these bytes."""
        self._require_artifact(object_id)
        row = self.ledger._connection.execute(
            "SELECT state FROM artifact_locations WHERE object_id = ? "
            "AND location = ?",
            (object_id, location),
        ).fetchone()
        if row is not None and str(row["state"]) == "present":
            return
        event = self.record(
            "artifact_location_added", object_id, {"location": location}
        )
        if row is None:
            self.ledger._connection.execute(
                "INSERT INTO artifact_locations (object_id, location, state, "
                "created_event_id) VALUES (?, ?, 'present', ?)",
                (object_id, location, event.id),
            )
        else:
            self.ledger._connection.execute(
                "UPDATE artifact_locations SET state = 'present', "
                "created_event_id = ? WHERE object_id = ? AND location = ?",
                (event.id, object_id, location),
            )

    def verify_location(
        self, object_id: str, location: str, *, verified: bool
    ) -> None:
        """Record the outcome of re-reading a location and comparing its hash."""
        self._require_artifact(object_id)
        event = self.record(
            "artifact_location_verified",
            object_id,
            {"location": location, "verified": verified},
        )
        self.ledger._connection.execute(
            "UPDATE artifact_locations SET state = ?, verified_at = ? "
            "WHERE object_id = ? AND location = ?",
            (
                "present" if verified else "lost",
                event.occurred_at if verified else None,
                object_id,
                location,
            ),
        )

    def lose_location(self, object_id: str, location: str, *, reason: str) -> None:
        """Record that a location no longer holds these bytes."""
        self._require_artifact(object_id)
        self.record(
            "artifact_location_lost",
            object_id,
            {"location": location, "reason": reason},
        )
        self.ledger._connection.execute(
            "UPDATE artifact_locations SET state = 'lost' "
            "WHERE object_id = ? AND location = ?",
            (object_id, location),
        )

    def archive_artifact(self, object_id: str, retention_state: str) -> None:
        """Record a retention change.  Identity is unaffected."""
        self._require_artifact(object_id)
        if retention_state not in RETENTION_STATES:
            raise LedgerError(f"unknown retention state: {retention_state!r}")
        self.record(
            "artifact_archived", object_id, {"retention_state": retention_state}
        )

    def _require_artifact(self, object_id: str) -> ObjectRecord:
        record = self.ledger.object(object_id)
        if record is None:
            raise LedgerError(f"unknown object: {object_id!r}")
        if record.kind != "artifact":
            raise LedgerError(f"{object_id!r} is a {record.kind!r}, not an artifact")
        return record

    @property
    def events(self) -> Sequence[Event]:
        """Return the events written in this session, in order."""
        return tuple(self._written)


class Ledger:
    """A ledger database: the canonical graph plus its append-only history."""

    def __init__(self, connection: sqlite3.Connection, path: Path) -> None:
        self._connection = connection
        self._path = path

    # -- lifecycle ---------------------------------------------------------

    @classmethod
    def open(cls, path: Path | str, *, create: bool = True) -> Ledger:
        """Open or create a ledger at ``path``."""
        target = Path(path)
        return cls(schema_module.connect(target, create=create), target)

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> Ledger:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def schema_version(self) -> int:
        return schema_module.current_version(self._connection)

    # -- writing -----------------------------------------------------------

    @contextmanager
    def write(
        self,
        actor_class: str,
        actor_id: str,
        *,
        campaign_id: str | None = None,
        epoch_id: str | None = None,
        turn_id: str | None = None,
    ) -> Iterator[WriteSession]:
        """Open one transactional write session.

        The whole block commits or none of it does, so a partially written
        event group can never be mistaken for a complete one.
        """
        envelope = Envelope(
            actor_class=actor_class,
            actor_id=actor_id,
            campaign_id=campaign_id,
            epoch_id=epoch_id,
            turn_id=turn_id,
        )
        self._connection.execute("BEGIN IMMEDIATE")
        head = self.head()
        session = WriteSession(
            ledger=self,
            envelope=envelope,
            _next_id=(head.id + 1) if head else 1,
            _previous_hash=head.event_hash if head else GENESIS_HASH,
        )
        try:
            yield session
        except Exception:
            self._connection.execute("ROLLBACK")
            raise
        self._connection.execute("COMMIT")

    def _insert_event(self, event: Event) -> None:
        self._connection.execute(
            "INSERT INTO events (id, occurred_at, recorded_at, campaign_id, "
            "epoch_id, turn_id, actor_class, actor_id, action, subject_type, "
            "subject_id, payload_json, evidence_object_id, idempotency_key, "
            "previous_event_hash, event_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event.id,
                event.occurred_at,
                event.recorded_at,
                event.campaign_id,
                event.epoch_id,
                event.turn_id,
                event.actor_class,
                event.actor_id,
                event.action,
                event.subject_type,
                event.subject_id,
                canonical_json(dict(event.payload)),
                event.evidence_object_id,
                event.idempotency_key,
                event.previous_event_hash,
                event.event_hash,
            ),
        )

    # -- reading -----------------------------------------------------------

    def object(self, object_id: str) -> ObjectRecord | None:
        row = self._connection.execute(
            "SELECT * FROM objects WHERE id = ?", (object_id,)
        ).fetchone()
        return _object_from_row(row) if row else None

    def _object_exists(self, object_id: str) -> bool:
        row = self._connection.execute(
            "SELECT 1 FROM objects WHERE id = ?", (object_id,)
        ).fetchone()
        return row is not None

    def objects(self, *, kind: str | None = None) -> list[ObjectRecord]:
        if kind is None:
            rows = self._connection.execute("SELECT * FROM objects ORDER BY id")
        else:
            rows = self._connection.execute(
                "SELECT * FROM objects WHERE kind = ? ORDER BY id", (kind,)
            )
        return [_object_from_row(row) for row in rows]

    def resolve(self, identifier: str) -> str | None:
        """Return the object ID for an ID or a registered alias."""
        if self._object_exists(identifier):
            return identifier
        row = self._connection.execute(
            "SELECT object_id FROM aliases WHERE alias = ?", (identifier,)
        ).fetchone()
        return str(row["object_id"]) if row else None

    def connection(
        self, from_id: str, relation: str, to_id: str
    ) -> ConnectionRecord | None:
        row = self._connection.execute(
            "SELECT * FROM connections WHERE from_id = ? AND relation = ? "
            "AND to_id = ?",
            (from_id, relation, to_id),
        ).fetchone()
        return _connection_from_row(row) if row else None

    def connections(
        self,
        *,
        from_id: str | None = None,
        to_id: str | None = None,
        relation: str | None = None,
        include_retracted: bool = False,
    ) -> list[ConnectionRecord]:
        clauses: list[str] = []
        parameters: list[object] = []
        if from_id is not None:
            clauses.append("from_id = ?")
            parameters.append(from_id)
        if to_id is not None:
            clauses.append("to_id = ?")
            parameters.append(to_id)
        if relation is not None:
            clauses.append("relation = ?")
            parameters.append(relation)
        if not include_retracted:
            clauses.append("retracted_event_id IS NULL")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._connection.execute(
            f"SELECT * FROM connections{where} ORDER BY id", parameters
        )
        return [_connection_from_row(row) for row in rows]

    def events(
        self,
        *,
        subject_id: str | None = None,
        action: str | None = None,
        turn_id: str | None = None,
        since: int | None = None,
        until: int | None = None,
    ) -> list[Event]:
        """Return events in authoritative order, optionally filtered.

        ``until`` is inclusive, which is what makes replaying history to a
        past event straightforward.
        """
        clauses: list[str] = []
        parameters: list[object] = []
        if subject_id is not None:
            clauses.append("subject_id = ?")
            parameters.append(subject_id)
        if action is not None:
            clauses.append("action = ?")
            parameters.append(action)
        if turn_id is not None:
            clauses.append("turn_id = ?")
            parameters.append(turn_id)
        if since is not None:
            clauses.append("id > ?")
            parameters.append(since)
        if until is not None:
            clauses.append("id <= ?")
            parameters.append(until)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._connection.execute(
            f"SELECT * FROM events{where} ORDER BY id", parameters
        )
        return [event_from_row(row) for row in rows]

    def head(self) -> Event | None:
        """Return the most recent event, or None for an empty ledger."""
        row = self._connection.execute(
            "SELECT * FROM events ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return event_from_row(row) if row else None

    def _event_by_key(self, idempotency_key: str) -> Event | None:
        row = self._connection.execute(
            "SELECT * FROM events WHERE idempotency_key = ?", (idempotency_key,)
        ).fetchone()
        return event_from_row(row) if row else None

    def event_count(self) -> int:
        row = self._connection.execute("SELECT COUNT(*) AS n FROM events").fetchone()
        return int(row["n"])

    def locations(
        self, object_id: str, *, present_only: bool = False
    ) -> list[LocationRecord]:
        """Return the known locations of one artifact's bytes."""
        query = "SELECT * FROM artifact_locations WHERE object_id = ?"
        if present_only:
            query += " AND state = 'present'"
        rows = self._connection.execute(query + " ORDER BY location", (object_id,))
        return [
            LocationRecord(
                object_id=str(row["object_id"]),
                location=str(row["location"]),
                state=str(row["state"]),
                verified_at=(
                    str(row["verified_at"]) if row["verified_at"] is not None else None
                ),
                created_event_id=int(row["created_event_id"]),
            )
            for row in rows
        ]

    def artifacts_without_location(self) -> list[ObjectRecord]:
        """Return artifacts whose only known locations are unavailable.

        This is one of the questions the recovery queue must answer, and it is
        cheap enough to ask directly.
        """
        rows = self._connection.execute(
            "SELECT o.* FROM objects o WHERE o.kind = 'artifact' AND NOT EXISTS ("
            "  SELECT 1 FROM artifact_locations l "
            "  WHERE l.object_id = o.id AND l.state = 'present'"
            ") ORDER BY o.id"
        )
        return [_object_from_row(row) for row in rows]


__all__ = [
    "ARTIFACT_ID_PREFIX",
    "ConflictError",
    "ConnectionRecord",
    "Ledger",
    "LedgerError",
    "LocationRecord",
    "ObjectRecord",
    "WriteSession",
]
