"""Event envelopes, canonical hashing, and the tamper-evident chain.

Ordering is the increasing event ID; ``occurred_at`` is wall-clock information
and is never used to decide precedence.  Each event commits to its predecessor,
so an accidental or malicious rewrite of history is detectable by replaying the
chain rather than by trusting the file's mtime.

The chain is tamper-*evident*, not self-authenticating: anyone who can rewrite
the database can also recompute the chain.  Detection comes from anchoring a
head elsewhere, which ``head_signed`` events and the exported head record exist
to support.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime

from leanevolve.ledger.schema import GENESIS_HASH
from leanevolve.ledger.vocabulary import (
    SUBJECT_TYPES,
    VocabularyError,
    authorize,
    validate_event_payload,
)

EVENT_FORMAT = "leanevolve-ledger-event-v1"
HEAD_FORMAT = "leanevolve-ledger-head-v1"

MAX_IDENTIFIER_CHARS = 200


class EventError(ValueError):
    """Raised when an event envelope is malformed or unauthorized."""


def utc_now() -> str:
    """Return an RFC 3339 timestamp in UTC."""
    return datetime.now(UTC).isoformat(timespec="microseconds")


def canonical_json(value: object) -> str:
    """Serialize deterministically so a digest depends only on content."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@dataclass(frozen=True)
class Envelope:
    """The scope an event inherits from its campaign, epoch, and turn.

    Agents never type these.  The runner allocates them once and every event
    written inside that scope carries them automatically, which is what makes a
    turn delta or a recovery queue answerable without a transcript.
    """

    actor_class: str
    actor_id: str
    campaign_id: str | None = None
    epoch_id: str | None = None
    turn_id: str | None = None

    def as_row(self) -> dict[str, object]:
        return {
            "campaign_id": self.campaign_id,
            "epoch_id": self.epoch_id,
            "turn_id": self.turn_id,
            "actor_class": self.actor_class,
            "actor_id": self.actor_id,
        }


@dataclass(frozen=True)
class Event:
    """One committed change to canonical state."""

    id: int
    occurred_at: str
    recorded_at: str
    actor_class: str
    actor_id: str
    action: str
    subject_type: str
    subject_id: str
    payload: Mapping[str, object] = field(default_factory=dict)
    evidence_object_id: str | None = None
    campaign_id: str | None = None
    epoch_id: str | None = None
    turn_id: str | None = None
    idempotency_key: str | None = None
    previous_event_hash: str = GENESIS_HASH
    event_hash: str = ""

    def hashed_core(self) -> dict[str, object]:
        """Return exactly the fields the chain commits to.

        ``recorded_at`` is excluded on purpose: re-importing the same history
        must produce the same chain, and the moment a row was written is not
        part of what happened.
        """
        return {
            "format": EVENT_FORMAT,
            "id": self.id,
            "occurred_at": self.occurred_at,
            "campaign_id": self.campaign_id,
            "epoch_id": self.epoch_id,
            "turn_id": self.turn_id,
            "actor_class": self.actor_class,
            "actor_id": self.actor_id,
            "action": self.action,
            "subject_type": self.subject_type,
            "subject_id": self.subject_id,
            "payload": dict(self.payload),
            "evidence_object_id": self.evidence_object_id,
            "previous_event_hash": self.previous_event_hash,
        }

    def compute_hash(self) -> str:
        """Return the digest this event must carry."""
        return hashlib.sha256(
            canonical_json(self.hashed_core()).encode("utf-8")
        ).hexdigest()

    def with_hash(self) -> Event:
        """Return a copy carrying its computed digest."""
        digest = self.compute_hash()
        return Event(
            id=self.id,
            occurred_at=self.occurred_at,
            recorded_at=self.recorded_at,
            actor_class=self.actor_class,
            actor_id=self.actor_id,
            action=self.action,
            subject_type=self.subject_type,
            subject_id=self.subject_id,
            payload=dict(self.payload),
            evidence_object_id=self.evidence_object_id,
            campaign_id=self.campaign_id,
            epoch_id=self.epoch_id,
            turn_id=self.turn_id,
            idempotency_key=self.idempotency_key,
            previous_event_hash=self.previous_event_hash,
            event_hash=digest,
        )


def _validate_identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise EventError(f"{label} must be a non-empty string")
    if len(value) > MAX_IDENTIFIER_CHARS:
        raise EventError(f"{label} exceeds {MAX_IDENTIFIER_CHARS} characters")
    if "\n" in value or "\r" in value:
        raise EventError(f"{label} must not contain newlines")
    return value


def _json_safe(payload: Mapping[str, object]) -> dict[str, object]:
    """Reject a payload that cannot round-trip through canonical JSON."""
    try:
        encoded = canonical_json(dict(payload))
    except (TypeError, ValueError) as error:
        raise EventError(f"payload is not JSON-serializable: {error}") from error
    decoded = json.loads(encoded)
    if decoded != json.loads(canonical_json(decoded)):
        raise EventError("payload does not round-trip through canonical JSON")
    return decoded


def prepare(
    *,
    event_id: int,
    envelope: Envelope,
    action: str,
    subject_type: str,
    subject_id: str,
    payload: Mapping[str, object] | None = None,
    evidence_object_id: str | None = None,
    idempotency_key: str | None = None,
    previous_event_hash: str,
    occurred_at: str | None = None,
) -> Event:
    """Validate and hash one event before it is written.

    Authority and payload contracts are checked here rather than at the call
    site, so no writer can reach the database with an event its actor class is
    not permitted to emit.
    """
    if subject_type not in SUBJECT_TYPES:
        raise EventError(f"unknown subject type: {subject_type!r}")
    _validate_identifier(subject_id, "subject_id")
    _validate_identifier(envelope.actor_id, "actor_id")
    try:
        authorize(envelope.actor_class, action)
        declared = validate_event_payload(
            action, payload, evidence_object_id=evidence_object_id
        )
    except VocabularyError as error:
        raise EventError(str(error)) from error
    if subject_type not in declared.subject_types:
        raise EventError(
            f"event {action!r} does not accept subject type {subject_type!r}; "
            f"permitted: {', '.join(sorted(declared.subject_types))}"
        )
    now = utc_now()
    event = Event(
        id=event_id,
        occurred_at=occurred_at or now,
        recorded_at=now,
        actor_class=envelope.actor_class,
        actor_id=envelope.actor_id,
        action=action,
        subject_type=subject_type,
        subject_id=subject_id,
        payload=_json_safe(payload or {}),
        evidence_object_id=evidence_object_id,
        campaign_id=envelope.campaign_id,
        epoch_id=envelope.epoch_id,
        turn_id=envelope.turn_id,
        idempotency_key=idempotency_key,
        previous_event_hash=previous_event_hash,
    )
    return event.with_hash()


def event_from_row(row: Mapping[str, object]) -> Event:
    """Rebuild an event from a database row."""
    payload = json.loads(str(row["payload_json"]))
    return Event(
        id=int(row["id"]),  # type: ignore[arg-type]
        occurred_at=str(row["occurred_at"]),
        recorded_at=str(row["recorded_at"]),
        actor_class=str(row["actor_class"]),
        actor_id=str(row["actor_id"]),
        action=str(row["action"]),
        subject_type=str(row["subject_type"]),
        subject_id=str(row["subject_id"]),
        payload=payload,
        evidence_object_id=(
            str(row["evidence_object_id"])
            if row["evidence_object_id"] is not None
            else None
        ),
        campaign_id=(
            str(row["campaign_id"]) if row["campaign_id"] is not None else None
        ),
        epoch_id=(str(row["epoch_id"]) if row["epoch_id"] is not None else None),
        turn_id=(str(row["turn_id"]) if row["turn_id"] is not None else None),
        idempotency_key=(
            str(row["idempotency_key"])
            if row["idempotency_key"] is not None
            else None
        ),
        previous_event_hash=str(row["previous_event_hash"]),
        event_hash=str(row["event_hash"]),
    )


def head_payload(event: Event | None, *, schema_version: int) -> dict[str, object]:
    """Return a signed-head record anchoring the chain at one event."""
    return {
        "format": HEAD_FORMAT,
        "schema_version": schema_version,
        "event_id": event.id if event else 0,
        "event_hash": event.event_hash if event else GENESIS_HASH,
        "occurred_at": event.occurred_at if event else None,
    }


__all__ = [
    "EVENT_FORMAT",
    "HEAD_FORMAT",
    "Envelope",
    "Event",
    "EventError",
    "canonical_json",
    "event_from_row",
    "head_payload",
    "prepare",
    "utc_now",
]
