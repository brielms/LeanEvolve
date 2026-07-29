"""Database schema, pragmas, and migrations.

SQLite holds structured metadata; large artifacts live in a content-addressed
store and are referenced here by hash.  The connection is configured for a
single writer with concurrent readers: WAL so readers never block an ordinary
write, foreign keys on so a dangling reference fails at insert time rather than
during an audit months later, and a bounded busy timeout so a contended write
retries instead of silently dropping an event.

Migrations are forward-only and numbered.  Opening a database whose schema is
newer than this code fails closed: a projection built by a future schema must
not be reinterpreted by an older reader.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

from leanevolve.ledger.vocabulary import (
    VOCABULARY_VERSION,
    vocabulary_sha256,
)

SCHEMA_VERSION = 1

#: Bounded wait before a contended write gives up.  A failure here is reported,
#: never swallowed.
BUSY_TIMEOUT_MS = 5_000

GENESIS_HASH = "0" * 64


class SchemaError(RuntimeError):
    """Raised when a database cannot be opened at this schema version."""


_MIGRATION_1 = """
CREATE TABLE schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
) STRICT;

-- Append-only history.  Nothing else may be written without an event, and no
-- event is ever updated or deleted.
CREATE TABLE events (
    id                  INTEGER PRIMARY KEY,
    occurred_at         TEXT    NOT NULL,
    recorded_at         TEXT    NOT NULL,
    campaign_id         TEXT,
    epoch_id            TEXT,
    turn_id             TEXT,
    actor_class         TEXT    NOT NULL,
    actor_id            TEXT    NOT NULL,
    action              TEXT    NOT NULL,
    subject_type        TEXT    NOT NULL,
    subject_id          TEXT    NOT NULL,
    payload_json        TEXT    NOT NULL DEFAULT '{}',
    evidence_object_id  TEXT,
    idempotency_key     TEXT    UNIQUE,
    previous_event_hash TEXT    NOT NULL,
    event_hash          TEXT    NOT NULL UNIQUE
) STRICT;

CREATE INDEX events_subject       ON events (subject_id, id);
CREATE INDEX events_action        ON events (action, id);
CREATE INDEX events_turn          ON events (turn_id, id);
CREATE INDEX events_evidence      ON events (evidence_object_id);

CREATE TABLE objects (
    id               TEXT PRIMARY KEY,
    kind             TEXT    NOT NULL,
    canonical_name   TEXT    NOT NULL,
    content_format   TEXT    NOT NULL,
    content          TEXT    NOT NULL DEFAULT '',
    properties_json  TEXT    NOT NULL DEFAULT '{}',
    created_event_id INTEGER NOT NULL REFERENCES events (id)
) STRICT;

CREATE INDEX objects_kind ON objects (kind);

-- Renames and traceability identifiers.  Many aliases may point at one object;
-- an alias is never reused for a different object.
CREATE TABLE aliases (
    alias            TEXT PRIMARY KEY,
    object_id        TEXT    NOT NULL REFERENCES objects (id),
    created_event_id INTEGER NOT NULL REFERENCES events (id)
) STRICT;

-- Connections are never deleted.  Retraction sets retracted_event_id, which
-- removes the edge from the current view and preserves it in history.
CREATE TABLE connections (
    id                 INTEGER PRIMARY KEY,
    from_id            TEXT    NOT NULL REFERENCES objects (id),
    relation           TEXT    NOT NULL,
    to_id              TEXT    NOT NULL REFERENCES objects (id),
    properties_json    TEXT    NOT NULL DEFAULT '{}',
    created_event_id   INTEGER NOT NULL REFERENCES events (id),
    retracted_event_id INTEGER REFERENCES events (id),
    UNIQUE (from_id, relation, to_id)
) STRICT;

CREATE INDEX connections_from ON connections (from_id, relation);
CREATE INDEX connections_to   ON connections (to_id, relation);

-- Where the bytes of a content-addressed artifact are believed to be.  The
-- hash is identity; every location change is an event.
CREATE TABLE artifact_locations (
    id               INTEGER PRIMARY KEY,
    object_id        TEXT    NOT NULL REFERENCES objects (id),
    location         TEXT    NOT NULL,
    state            TEXT    NOT NULL,
    verified_at      TEXT,
    created_event_id INTEGER NOT NULL REFERENCES events (id),
    UNIQUE (object_id, location)
) STRICT;
"""

#: Numbered forward-only migrations.  Index 0 produces schema version 1.
MIGRATIONS: tuple[str, ...] = (_MIGRATION_1,)


def _statements(script: str) -> tuple[str, ...]:
    """Split a migration into single statements.

    ``executescript`` would commit any open transaction before running, which
    would tear a migration in half if a later statement failed.  Executing the
    statements individually keeps the whole migration inside one transaction.
    """
    # Comments are removed first: prose in this file contains semicolons, and
    # splitting before stripping would cut a statement in half mid-comment.
    sql = "\n".join(
        line for line in script.splitlines() if not line.strip().startswith("--")
    )
    return tuple(
        statement.strip() for statement in sql.split(";") if statement.strip()
    )


def _configure(connection: sqlite3.Connection) -> None:
    connection.row_factory = sqlite3.Row
    connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = FULL")
    connection.execute("PRAGMA foreign_keys = ON")
    # Python's driver opens its own transactions; explicit control lets one
    # logical event group be exactly one transaction.
    connection.isolation_level = None


def current_version(connection: sqlite3.Connection) -> int:
    """Return the schema version, or 0 for an empty database."""
    row = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'schema_meta'"
    ).fetchone()
    if row is None:
        return 0
    stored = connection.execute(
        "SELECT value FROM schema_meta WHERE key = 'schema_version'"
    ).fetchone()
    return int(stored["value"]) if stored else 0


def migrate(connection: sqlite3.Connection) -> int:
    """Apply pending migrations and return the resulting version."""
    version = current_version(connection)
    if version > SCHEMA_VERSION:
        raise SchemaError(
            f"database schema version {version} is newer than this code "
            f"({SCHEMA_VERSION}); refusing to reinterpret it"
        )
    if version == SCHEMA_VERSION:
        return version
    connection.execute("BEGIN IMMEDIATE")
    try:
        for index in range(version, SCHEMA_VERSION):
            for statement in _statements(MIGRATIONS[index]):
                connection.execute(statement)
        connection.execute(
            "INSERT INTO schema_meta (key, value) VALUES ('schema_version', ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (str(SCHEMA_VERSION),),
        )
        connection.execute(
            "INSERT INTO schema_meta (key, value) VALUES ('vocabulary_version', ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (str(VOCABULARY_VERSION),),
        )
        connection.execute(
            "INSERT INTO schema_meta (key, value) VALUES ('vocabulary_sha256', ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (vocabulary_sha256(),),
        )
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise
    return SCHEMA_VERSION


def connect(
    path: Path | str,
    *,
    create: bool = True,
    on_migrate: Callable[[int, int], None] | None = None,
) -> sqlite3.Connection:
    """Open a configured connection, migrating it to the current schema."""
    target = Path(path)
    if not create and not target.exists():
        raise SchemaError(f"no ledger database at {target}")
    if create:
        target.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(target, timeout=BUSY_TIMEOUT_MS / 1000)
    _configure(connection)
    before = current_version(connection)
    after = migrate(connection)
    if on_migrate is not None and before != after:
        on_migrate(before, after)
    return connection


def vocabulary_binding(connection: sqlite3.Connection) -> tuple[int, str]:
    """Return the vocabulary version and digest this database was written with."""
    rows = {
        row["key"]: row["value"]
        for row in connection.execute(
            "SELECT key, value FROM schema_meta WHERE key IN "
            "('vocabulary_version', 'vocabulary_sha256')"
        )
    }
    return int(rows["vocabulary_version"]), rows["vocabulary_sha256"]


__all__ = [
    "BUSY_TIMEOUT_MS",
    "GENESIS_HASH",
    "MIGRATIONS",
    "SCHEMA_VERSION",
    "SchemaError",
    "connect",
    "current_version",
    "migrate",
    "vocabulary_binding",
]
