#!/usr/bin/env python3
"""Content-free, tamper-evident observability for Shinka campaigns.

This module is deliberately independent from the live campaign runner.  It
defines a strict, append-only JSONL event stream that can later be wired into
campaign orchestration, model and tool bridges, the research ledger, the Lean
scratch checker, the evaluator, promotion, and audit code.

The schema is safe-by-construction: events may contain only bounded IDs,
SHA-256 digests, counters, durations, exit codes, and statuses.  There is no
field for prompts, model reasoning, proof text, diagnostics, command bodies,
tool output, secrets, or arbitrary metadata.  Callers must hash only already
redacted structural metadata; a digest is not a substitute for redaction of a
low-entropy secret.

The hash chain is tamper-evident, not self-authenticating.  Persist a
``ChainHead`` in an independently protected manifest if truncation detection
is required.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Literal

try:  # POSIX is the supported environment for the Shinka runner.
    import fcntl
except ImportError:  # pragma: no cover - exercised only off POSIX.
    fcntl = None  # type: ignore[assignment]


EVENT_FORMAT = "shinka-observability-event-v1"
HEAD_FORMAT = "shinka-observability-head-v1"
SCHEMA_VERSION = 1
GENESIS_SHA256 = "0" * 64
MAX_IDENTIFIER_CHARS = 160
MAX_RECORD_IDS = 512

SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
IDENTIFIER_PATTERN = re.compile(
    rf"[A-Za-z0-9][A-Za-z0-9_.:-]{{0,{MAX_IDENTIFIER_CHARS - 1}}}"
)


class ObservabilityError(ValueError):
    """Raised when an event or existing event log violates the schema."""


@dataclass(frozen=True)
class EventSpec:
    """Allowed payload fields and turn-envelope policy for one event."""

    required: frozenset[str]
    optional: frozenset[str] = frozenset()
    turn: Literal["required", "optional", "forbidden"] = "optional"


@dataclass(frozen=True)
class EventReceipt:
    """Small receipt returned after one durable append."""

    sequence: int
    event: str
    timestamp_utc: str
    record_sha256: str


@dataclass(frozen=True)
class ChainHead:
    """Anchorable identity of one complete event-log prefix."""

    event_count: int
    record_sha256: str

    def as_dict(self) -> dict[str, object]:
        return {
            "format": HEAD_FORMAT,
            "event_count": self.event_count,
            "record_sha256": self.record_sha256,
        }


@dataclass(frozen=True)
class VerificationResult:
    """Result of checking schema, sequence, hashes, and an optional anchor."""

    event_count: int
    head: ChainHead
    errors: tuple[str, ...]

    @property
    def valid(self) -> bool:
        return not self.errors


_START_FIELDS = frozenset({"operation_id", "phase"})
_FINISH_FIELDS = frozenset(
    {"operation_id", "phase", "status", "duration_ms"}
)
_FINISH_OPTIONAL = frozenset(
    {"manifest_sha256", "receipt_sha256", "error_class"}
)

_EVENT_SPECS: dict[str, EventSpec] = {
    "campaign.phase.started": EventSpec(
        _START_FIELDS,
        frozenset({"parent_operation_id", "config_sha256"}),
        turn="forbidden",
    ),
    "campaign.phase.finished": EventSpec(
        _FINISH_FIELDS,
        _FINISH_OPTIONAL,
        turn="forbidden",
    ),
    "turn.phase.started": EventSpec(
        _START_FIELDS,
        frozenset({"parent_operation_id", "input_sha256"}),
        turn="required",
    ),
    "turn.phase.finished": EventSpec(
        _FINISH_FIELDS,
        frozenset({"output_sha256", "receipt_sha256", "error_class"}),
        turn="required",
    ),
    "model.request.started": EventSpec(
        frozenset({"request_id", "model_id"}),
        frozenset({"request_sha256", "route_sha256"}),
    ),
    "model.request.finished": EventSpec(
        frozenset(
            {"request_id", "model_id", "status", "duration_ms"}
        ),
        frozenset(
            {
                "response_sha256",
                "input_tokens",
                "cached_input_tokens",
                "output_tokens",
                "reasoning_tokens",
                "total_tokens",
                "cost_microusd",
                "error_class",
            }
        ),
    ),
    "tool.invocation.started": EventSpec(
        frozenset({"invocation_id", "tool_id"}),
        frozenset({"metadata_sha256"}),
    ),
    "tool.invocation.finished": EventSpec(
        frozenset(
            {"invocation_id", "tool_id", "status", "duration_ms"}
        ),
        frozenset(
            {"result_sha256", "exit_code", "error_class"}
        ),
    ),
    "research_ledger.query.started": EventSpec(
        frozenset({"query_id", "query_sha256"}),
        frozenset(
            {"goal_id", "kind_filter", "status_filter", "tag_id"}
        ),
    ),
    "research_ledger.query.finished": EventSpec(
        frozenset(
            {
                "query_id",
                "query_sha256",
                "status",
                "duration_ms",
                "result_count",
            }
        ),
        frozenset({"error_class"}),
    ),
    "research_ledger.records_opened": EventSpec(
        frozenset({"open_id", "record_ids"}),
        frozenset({"query_id"}),
    ),
    "lean.scratch.started": EventSpec(
        frozenset({"invocation_id", "mode", "source_sha256"}),
        frozenset({"assembled_sha256"}),
    ),
    "lean.scratch.finished": EventSpec(
        frozenset(
            {
                "invocation_id",
                "mode",
                "source_sha256",
                "status",
                "duration_ms",
            }
        ),
        frozenset({"assembled_sha256", "exit_code", "error_class"}),
    ),
    "evaluator.stage.started": EventSpec(
        frozenset(
            {"evaluation_id", "stage", "mode", "candidate_sha256"}
        )
    ),
    "evaluator.stage.finished": EventSpec(
        frozenset(
            {
                "evaluation_id",
                "stage",
                "mode",
                "candidate_sha256",
                "status",
                "duration_ms",
            }
        ),
        frozenset(
            {
                "receipt_sha256",
                "accepted_goal_count",
                "certified_relation_count",
                "error_class",
            }
        ),
    ),
    "promotion.phase.started": EventSpec(
        frozenset({"promotion_id", "phase"}),
        frozenset({"candidate_sha256"}),
    ),
    "promotion.phase.finished": EventSpec(
        frozenset(
            {"promotion_id", "phase", "status", "duration_ms"}
        ),
        frozenset(
            {
                "manifest_sha256",
                "receipt_sha256",
                "accepted_goal_count",
                "certified_relation_count",
                "error_class",
            }
        ),
    ),
    "audit.phase.started": EventSpec(
        frozenset({"audit_id", "phase"}),
        frozenset({"input_sha256"}),
    ),
    "audit.phase.finished": EventSpec(
        frozenset({"audit_id", "phase", "status", "duration_ms"}),
        frozenset(
            {
                "receipt_sha256",
                "manifest_sha256",
                "audited_goal_count",
                "audited_relation_count",
                "error_class",
            }
        ),
    ),
}

EVENT_SPECS: Mapping[str, EventSpec] = MappingProxyType(_EVENT_SPECS)

_HASH_FIELDS = frozenset(
    {
        "assembled_sha256",
        "candidate_sha256",
        "config_sha256",
        "input_sha256",
        "manifest_sha256",
        "metadata_sha256",
        "output_sha256",
        "query_sha256",
        "receipt_sha256",
        "request_sha256",
        "response_sha256",
        "result_sha256",
        "route_sha256",
        "source_sha256",
    }
)
_NONNEGATIVE_INTEGER_FIELDS = frozenset(
    {
        "accepted_goal_count",
        "audited_goal_count",
        "audited_relation_count",
        "cached_input_tokens",
        "certified_relation_count",
        "cost_microusd",
        "duration_ms",
        "input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "result_count",
        "total_tokens",
    }
)
_IDENTIFIER_FIELDS = frozenset(
    {
        "audit_id",
        "error_class",
        "evaluation_id",
        "goal_id",
        "invocation_id",
        "kind_filter",
        "mode",
        "model_id",
        "open_id",
        "operation_id",
        "parent_operation_id",
        "phase",
        "promotion_id",
        "query_id",
        "request_id",
        "stage",
        "status",
        "status_filter",
        "tag_id",
        "tool_id",
    }
)
_ENVELOPE_FIELDS = frozenset(
    {
        "format",
        "schema_version",
        "sequence",
        "timestamp_utc",
        "campaign_id",
        "source_component",
        "turn",
        "event",
        "fields",
        "previous_record_sha256",
        "record_sha256",
    }
)


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _record_sha256(core: Mapping[str, object]) -> str:
    return hashlib.sha256(_canonical_json(core).encode("utf-8")).hexdigest()


def _validate_identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or IDENTIFIER_PATTERN.fullmatch(value) is None:
        raise ObservabilityError(f"{label} is not a bounded safe identifier")
    return value


def _validate_sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or SHA256_PATTERN.fullmatch(value) is None:
        raise ObservabilityError(f"{label} is not a lowercase SHA-256 digest")
    return value


def _validate_nonnegative_integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ObservabilityError(f"{label} must be a nonnegative integer")
    return value


def _validate_exit_code(value: object) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not -65535 <= value <= 65535
    ):
        raise ObservabilityError("exit_code must be an integer in -65535..65535")
    return value


def _validate_record_ids(value: object) -> list[str]:
    if not isinstance(value, (list, tuple)):
        raise ObservabilityError("record_ids must be a list or tuple")
    if not 1 <= len(value) <= MAX_RECORD_IDS:
        raise ObservabilityError(
            f"record_ids length must be in 1..{MAX_RECORD_IDS}"
        )
    identifiers = [
        _validate_identifier(item, "record_ids item") for item in value
    ]
    if len(identifiers) != len(set(identifiers)):
        raise ObservabilityError("record_ids may not contain duplicates")
    return sorted(identifiers)


def _normalize_fields(event: str, fields: Mapping[str, object]) -> dict[str, object]:
    spec = EVENT_SPECS.get(event)
    if spec is None:
        raise ObservabilityError(f"unsupported observability event: {event}")
    supplied = frozenset(fields)
    missing = spec.required - supplied
    unknown = supplied - spec.required - spec.optional
    if missing:
        raise ObservabilityError(
            f"{event} lacks required fields: {', '.join(sorted(missing))}"
        )
    if unknown:
        raise ObservabilityError(
            f"{event} rejects content-bearing or unknown fields: "
            + ", ".join(sorted(unknown))
        )

    normalized: dict[str, object] = {}
    for name, value in fields.items():
        if name in _HASH_FIELDS:
            normalized[name] = _validate_sha256(value, name)
        elif name in _NONNEGATIVE_INTEGER_FIELDS:
            normalized[name] = _validate_nonnegative_integer(value, name)
        elif name in _IDENTIFIER_FIELDS:
            normalized[name] = _validate_identifier(value, name)
        elif name == "exit_code":
            normalized[name] = _validate_exit_code(value)
        elif name == "record_ids":
            normalized[name] = _validate_record_ids(value)
        else:  # A schema field without a validator is a programming error.
            raise ObservabilityError(f"no safe validator registered for {name}")
    return normalized


def _validate_turn(value: object, policy: str) -> int | None:
    if value is None:
        if policy == "required":
            raise ObservabilityError("event requires a positive turn number")
        return None
    if policy == "forbidden":
        raise ObservabilityError("campaign-level event may not carry a turn")
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ObservabilityError("turn must be a positive integer")
    return value


def _utc_timestamp(clock: Callable[[], datetime]) -> str:
    value = clock()
    if value.tzinfo is None or value.utcoffset() is None:
        raise ObservabilityError("observability clock must return an aware datetime")
    value = value.astimezone(UTC)
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _validate_timestamp(value: object) -> None:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ObservabilityError("timestamp_utc must be an ISO-8601 UTC string")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise ObservabilityError("timestamp_utc is not valid ISO-8601") from error
    if parsed.utcoffset() != UTC.utcoffset(parsed):
        raise ObservabilityError("timestamp_utc is not UTC")


def new_operation_id(prefix: str) -> str:
    """Return a fresh, schema-safe correlation ID."""

    safe_prefix = _validate_identifier(prefix, "operation id prefix")
    value = f"{safe_prefix}:{uuid.uuid4().hex}"
    return _validate_identifier(value, "generated operation id")


def _verify_text(
    text: str,
    *,
    expected_campaign_id: str | None = None,
    expected_head: ChainHead | None = None,
) -> VerificationResult:
    errors: list[str] = []
    if text and not text.endswith("\n"):
        errors.append("event log ends with a partial JSONL record")
    raw_lines = text.splitlines()
    previous_hash = GENESIS_SHA256
    observed_campaign: str | None = None
    head_hash = GENESIS_SHA256

    for line_number, line in enumerate(raw_lines, start=1):
        if not line:
            errors.append(f"blank JSONL record at line {line_number}")
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            errors.append(
                f"invalid JSON at line {line_number}: {error.msg}"
            )
            continue
        if not isinstance(record, dict):
            errors.append(f"record at line {line_number} is not an object")
            continue
        unknown_envelope = set(record) - _ENVELOPE_FIELDS
        missing_envelope = _ENVELOPE_FIELDS - set(record)
        if unknown_envelope:
            errors.append(
                f"unknown envelope fields at line {line_number}: "
                + ", ".join(sorted(unknown_envelope))
            )
        if missing_envelope:
            errors.append(
                f"missing envelope fields at line {line_number}: "
                + ", ".join(sorted(missing_envelope))
            )
            continue
        try:
            if record["format"] != EVENT_FORMAT:
                raise ObservabilityError("unsupported event format")
            if record["schema_version"] != SCHEMA_VERSION:
                raise ObservabilityError("unsupported schema version")
            sequence = record["sequence"]
            if (
                isinstance(sequence, bool)
                or not isinstance(sequence, int)
                or sequence != line_number - 1
            ):
                raise ObservabilityError("event sequence mismatch")
            _validate_timestamp(record["timestamp_utc"])
            campaign_id = _validate_identifier(
                record["campaign_id"], "campaign_id"
            )
            _validate_identifier(
                record["source_component"], "source_component"
            )
            event = _validate_identifier(record["event"], "event")
            if event not in EVENT_SPECS:
                raise ObservabilityError(f"unsupported event: {event}")
            spec = EVENT_SPECS[event]
            _validate_turn(record["turn"], spec.turn)
            fields = record["fields"]
            if not isinstance(fields, dict):
                raise ObservabilityError("fields must be an object")
            normalized = _normalize_fields(event, fields)
            if normalized != fields:
                raise ObservabilityError("fields are not canonically normalized")
            claimed_previous = _validate_sha256(
                record["previous_record_sha256"],
                "previous_record_sha256",
            )
            if claimed_previous != previous_hash:
                raise ObservabilityError("previous record hash mismatch")
            claimed_hash = _validate_sha256(
                record["record_sha256"], "record_sha256"
            )
            core = {
                key: value
                for key, value in record.items()
                if key != "record_sha256"
            }
            if _record_sha256(core) != claimed_hash:
                raise ObservabilityError("record hash mismatch")
            if observed_campaign is None:
                observed_campaign = campaign_id
            elif campaign_id != observed_campaign:
                raise ObservabilityError("campaign_id changed within event log")
            if expected_campaign_id is not None and campaign_id != expected_campaign_id:
                raise ObservabilityError("campaign_id differs from expected value")
            previous_hash = claimed_hash
            head_hash = claimed_hash
        except ObservabilityError as error:
            errors.append(f"line {line_number}: {error}")
            claimed = record.get("record_sha256")
            if isinstance(claimed, str) and SHA256_PATTERN.fullmatch(claimed):
                previous_hash = claimed
                head_hash = claimed

    head = ChainHead(len(raw_lines), head_hash)
    if expected_head is not None:
        if head.event_count != expected_head.event_count:
            errors.append(
                "anchored event count mismatch: "
                f"expected {expected_head.event_count}, observed {head.event_count}"
            )
        if head.record_sha256 != expected_head.record_sha256:
            errors.append("anchored head hash mismatch")
    return VerificationResult(len(raw_lines), head, tuple(errors))


def verify_observability_log(
    path: Path,
    *,
    expected_campaign_id: str | None = None,
    expected_head: ChainHead | None = None,
) -> VerificationResult:
    """Validate a complete event log without modifying it."""

    if expected_campaign_id is not None:
        _validate_identifier(expected_campaign_id, "expected campaign_id")
    if path.is_symlink():
        return VerificationResult(
            0,
            ChainHead(0, GENESIS_SHA256),
            ("event log may not be a symlink",),
        )
    if not path.is_file():
        return VerificationResult(
            0,
            ChainHead(0, GENESIS_SHA256),
            (f"event log is unavailable: {path}",),
        )
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        return VerificationResult(
            0,
            ChainHead(0, GENESIS_SHA256),
            (f"event log is unreadable: {error}",),
        )
    return _verify_text(
        text,
        expected_campaign_id=expected_campaign_id,
        expected_head=expected_head,
    )


class ObservabilityLog:
    """Strict append-only writer for one campaign event stream."""

    def __init__(
        self,
        path: Path,
        *,
        campaign_id: str,
        source_component: str,
        clock: Callable[[], datetime] | None = None,
        durable: bool = True,
    ) -> None:
        self.path = path
        self.campaign_id = _validate_identifier(campaign_id, "campaign_id")
        self.source_component = _validate_identifier(
            source_component, "source_component"
        )
        self.clock = clock or (lambda: datetime.now(UTC))
        self.durable = durable

    def emit(
        self,
        event: str,
        *,
        turn: int | None = None,
        **fields: object,
    ) -> EventReceipt:
        """Validate and durably append one content-free event."""

        event = _validate_identifier(event, "event")
        spec = EVENT_SPECS.get(event)
        if spec is None:
            raise ObservabilityError(f"unsupported observability event: {event}")
        normalized_turn = _validate_turn(turn, spec.turn)
        normalized_fields = _normalize_fields(event, fields)
        timestamp = _utc_timestamp(self.clock)

        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise ObservabilityError("event log may not be a symlink")
        flags = os.O_RDWR | os.O_CREAT | os.O_APPEND
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(self.path, flags, 0o600)
        except OSError as error:
            raise ObservabilityError(f"cannot open event log: {error}") from error

        try:
            if fcntl is None:
                raise ObservabilityError(
                    "cross-process event locking requires a POSIX environment"
                )
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            with os.fdopen(descriptor, "r+", encoding="utf-8") as handle:
                descriptor = -1
                handle.seek(0)
                existing = handle.read()
                verification = _verify_text(
                    existing,
                    expected_campaign_id=self.campaign_id,
                )
                if not verification.valid:
                    raise ObservabilityError(
                        "refusing to append to an invalid event log: "
                        + "; ".join(verification.errors)
                    )
                sequence = verification.event_count
                core: dict[str, object] = {
                    "format": EVENT_FORMAT,
                    "schema_version": SCHEMA_VERSION,
                    "sequence": sequence,
                    "timestamp_utc": timestamp,
                    "campaign_id": self.campaign_id,
                    "source_component": self.source_component,
                    "turn": normalized_turn,
                    "event": event,
                    "fields": normalized_fields,
                    "previous_record_sha256": (
                        verification.head.record_sha256
                    ),
                }
                digest = _record_sha256(core)
                record = {**core, "record_sha256": digest}
                handle.seek(0, os.SEEK_END)
                handle.write(_canonical_json(record) + "\n")
                handle.flush()
                if self.durable:
                    os.fsync(handle.fileno())
        finally:
            if descriptor >= 0:
                os.close(descriptor)

        return EventReceipt(sequence, event, timestamp, digest)

    def head(self) -> ChainHead:
        """Return the verified, externally anchorable current chain head."""

        result = verify_observability_log(
            self.path,
            expected_campaign_id=self.campaign_id,
        )
        if not result.valid:
            raise ObservabilityError("; ".join(result.errors))
        return result.head


__all__ = [
    "EVENT_FORMAT",
    "EVENT_SPECS",
    "GENESIS_SHA256",
    "HEAD_FORMAT",
    "SCHEMA_VERSION",
    "ChainHead",
    "EventReceipt",
    "EventSpec",
    "ObservabilityError",
    "ObservabilityLog",
    "VerificationResult",
    "new_operation_id",
    "verify_observability_log",
]
