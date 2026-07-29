"""Whole-database validation.

Checks schema and vocabulary binding, foreign keys, the event hash chain,
artifact digests, actor authority, and the invariants that keep derived truth
honest.  Every finding names its subject so a report can be acted on rather
than merely read.

A clean run means the database is internally consistent and that no recorded
event exceeded its actor's authority.  It does not mean the mathematics is
right; that is what the Lean audit is for.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import IntEnum

from leanevolve.ledger import schema as schema_module
from leanevolve.ledger.artifacts import ArtifactStore, under_replicated
from leanevolve.ledger.store import Ledger
from leanevolve.ledger.vocabulary import (
    RELATIONS,
    TRUTH_BEARING_ACTIONS,
    VocabularyError,
    authorize,
    require_object_kind,
    validate_connection,
    validate_event_payload,
    vocabulary_sha256,
)


class Severity(IntEnum):
    """How much a finding matters.  Only ``ERROR`` fails an integrity run."""

    INFO = 0
    WARNING = 1
    ERROR = 2


@dataclass(frozen=True)
class Finding:
    """One problem, with enough identity to fix it."""

    severity: Severity
    code: str
    subject: str
    detail: str

    def as_dict(self) -> dict[str, object]:
        return {
            "severity": self.severity.name.lower(),
            "code": self.code,
            "subject": self.subject,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class IntegrityReport:
    """The outcome of one validation run."""

    findings: tuple[Finding, ...]
    event_count: int
    object_count: int
    connection_count: int
    head_hash: str

    @property
    def ok(self) -> bool:
        return not any(item.severity is Severity.ERROR for item in self.findings)

    def errors(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity is Severity.ERROR)

    def as_dict(self) -> dict[str, object]:
        return {
            "format": "leanevolve-ledger-integrity-v1",
            "ok": self.ok,
            "event_count": self.event_count,
            "object_count": self.object_count,
            "connection_count": self.connection_count,
            "head_hash": self.head_hash,
            "findings": [item.as_dict() for item in self.findings],
        }


def _check_schema(ledger: Ledger) -> list[Finding]:
    findings: list[Finding] = []
    version = ledger.schema_version
    if version != schema_module.SCHEMA_VERSION:
        findings.append(
            Finding(
                Severity.ERROR,
                "schema_version_mismatch",
                "database",
                f"database is at schema {version}, code expects "
                f"{schema_module.SCHEMA_VERSION}",
            )
        )
    try:
        _, digest = schema_module.vocabulary_binding(ledger._connection)
    except KeyError:
        findings.append(
            Finding(
                Severity.ERROR,
                "vocabulary_unbound",
                "database",
                "no vocabulary digest is recorded",
            )
        )
        return findings
    if digest != vocabulary_sha256():
        # Not an error: history written under an older vocabulary stays valid,
        # but the difference must be visible rather than silent.
        findings.append(
            Finding(
                Severity.WARNING,
                "vocabulary_drift",
                "database",
                f"database was written with vocabulary {digest[:12]}, "
                f"code has {vocabulary_sha256()[:12]}",
            )
        )
    return findings


def _check_foreign_keys(ledger: Ledger) -> list[Finding]:
    rows = ledger._connection.execute("PRAGMA foreign_key_check").fetchall()
    return [
        Finding(
            Severity.ERROR,
            "foreign_key_violation",
            str(row[0]),
            f"row {row[1]} references missing {row[2]}",
        )
        for row in rows
    ]


def _check_chain(ledger: Ledger) -> list[Finding]:
    findings: list[Finding] = []
    previous_hash = schema_module.GENESIS_HASH
    expected_id = 1
    for event in ledger.events():
        subject = f"event:{event.id}"
        if event.id != expected_id:
            findings.append(
                Finding(
                    Severity.ERROR,
                    "event_id_gap",
                    subject,
                    f"expected id {expected_id}; a missing event breaks ordering",
                )
            )
            expected_id = event.id
        if event.previous_event_hash != previous_hash:
            findings.append(
                Finding(
                    Severity.ERROR,
                    "chain_break",
                    subject,
                    "previous_event_hash does not match the preceding event",
                )
            )
        recomputed = event.compute_hash()
        if event.event_hash != recomputed:
            findings.append(
                Finding(
                    Severity.ERROR,
                    "event_hash_mismatch",
                    subject,
                    "stored digest does not match the event's content",
                )
            )
        previous_hash = event.event_hash
        expected_id += 1
    return findings


def _check_authority(ledger: Ledger) -> list[Finding]:
    findings: list[Finding] = []
    for event in ledger.events():
        subject = f"event:{event.id}"
        try:
            authorize(event.actor_class, event.action)
        except VocabularyError as error:
            findings.append(
                Finding(Severity.ERROR, "unauthorized_event", subject, str(error))
            )
        try:
            validate_event_payload(
                event.action,
                event.payload,
                evidence_object_id=event.evidence_object_id,
            )
        except VocabularyError as error:
            findings.append(
                Finding(Severity.ERROR, "invalid_payload", subject, str(error))
            )
        if event.evidence_object_id is not None:
            record = ledger.object(event.evidence_object_id)
            if record is None:
                findings.append(
                    Finding(
                        Severity.ERROR,
                        "missing_evidence",
                        subject,
                        f"evidence {event.evidence_object_id} does not exist",
                    )
                )
    return findings


def _check_objects(ledger: Ledger) -> list[Finding]:
    findings: list[Finding] = []
    for record in ledger.objects():
        try:
            declared = require_object_kind(record.kind)
        except VocabularyError as error:
            findings.append(
                Finding(Severity.ERROR, "unknown_kind", record.id, str(error))
            )
            continue
        missing = [
            name
            for name in declared.required_properties
            if name not in record.properties
        ]
        if missing:
            findings.append(
                Finding(
                    Severity.ERROR,
                    "missing_properties",
                    record.id,
                    f"kind {record.kind} requires {', '.join(missing)}",
                )
            )
        if record.kind == "artifact":
            digest = record.properties.get("sha256")
            if not isinstance(digest, str) or not record.id.endswith(digest):
                findings.append(
                    Finding(
                        Severity.ERROR,
                        "artifact_identity_mismatch",
                        record.id,
                        "object ID does not match its recorded digest",
                    )
                )
    return findings


def _check_connections(ledger: Ledger) -> list[Finding]:
    findings: list[Finding] = []
    kinds = {record.id: record.kind for record in ledger.objects()}
    for edge in ledger.connections(include_retracted=True):
        subject = f"{edge.from_id} -{edge.relation}-> {edge.to_id}"
        if edge.relation not in RELATIONS:
            findings.append(
                Finding(
                    Severity.ERROR,
                    "unknown_relation",
                    subject,
                    "relation is not declared",
                )
            )
            continue
        if edge.from_id not in kinds or edge.to_id not in kinds:
            findings.append(
                Finding(
                    Severity.ERROR,
                    "dangling_connection",
                    subject,
                    "endpoint is missing",
                )
            )
            continue
        try:
            validate_connection(
                edge.relation,
                kinds[edge.from_id],
                kinds[edge.to_id],
                edge.properties,
            )
        except VocabularyError as error:
            findings.append(
                Finding(Severity.ERROR, "invalid_connection", subject, str(error))
            )
    return findings


def _check_truth_invariants(ledger: Ledger) -> list[Finding]:
    """Nothing may be proved or refuted except through the evaluator."""
    findings: list[Finding] = []
    for event in ledger.events():
        if event.action in TRUTH_BEARING_ACTIONS and (
            event.actor_class != "authoritative_evaluator"
        ):
            findings.append(
                Finding(
                    Severity.ERROR,
                    "truth_from_wrong_actor",
                    f"event:{event.id}",
                    f"{event.action} emitted by {event.actor_class}",
                )
            )
    for edge in ledger.connections(relation="refutes"):
        if edge.properties.get("trust_level") != "kernel":
            findings.append(
                Finding(
                    Severity.ERROR,
                    "refutation_without_kernel_trust",
                    f"{edge.from_id} -refutes-> {edge.to_id}",
                    "an active refutation must carry kernel trust",
                )
            )
    return findings


def _check_artifacts(
    ledger: Ledger, store: ArtifactStore | None, *, deep: bool
) -> list[Finding]:
    findings: list[Finding] = []
    for record in ledger.artifacts_without_location():
        findings.append(
            Finding(
                Severity.WARNING,
                "artifact_unavailable",
                record.id,
                "no known location currently holds these bytes",
            )
        )
    for record in under_replicated(ledger):
        findings.append(
            Finding(
                Severity.WARNING,
                "artifact_under_replicated",
                record.id,
                f"{record.properties.get('artifact_type')} should have at least "
                "two available copies",
            )
        )
    if store is not None and deep:
        for record in ledger.objects(kind="artifact"):
            digest = record.properties.get("sha256")
            if not isinstance(digest, str):
                continue
            if not store.contains(digest):
                continue
            if not store.verify(digest):
                findings.append(
                    Finding(
                        Severity.ERROR,
                        "artifact_corrupt",
                        record.id,
                        "local bytes no longer hash to their name",
                    )
                )
    return findings


def verify(
    ledger: Ledger,
    *,
    store: ArtifactStore | None = None,
    deep: bool = False,
) -> IntegrityReport:
    """Validate the database and return every finding.

    ``deep`` additionally re-hashes local artifact bytes, which is worth doing
    before a promotion or a cutover and too slow to do on every read.
    """
    findings: list[Finding] = []
    findings.extend(_check_schema(ledger))
    findings.extend(_check_foreign_keys(ledger))
    findings.extend(_check_chain(ledger))
    findings.extend(_check_authority(ledger))
    findings.extend(_check_objects(ledger))
    findings.extend(_check_connections(ledger))
    findings.extend(_check_truth_invariants(ledger))
    findings.extend(_check_artifacts(ledger, store, deep=deep))
    head = ledger.head()
    return IntegrityReport(
        findings=tuple(findings),
        event_count=ledger.event_count(),
        object_count=len(ledger.objects()),
        connection_count=len(ledger.connections(include_retracted=True)),
        head_hash=head.event_hash if head else schema_module.GENESIS_HASH,
    )


def render(report: IntegrityReport) -> str:
    """Render a report for a human reader."""
    lines = [
        f"ledger integrity: {'ok' if report.ok else 'FAILED'}",
        f"  events      {report.event_count}",
        f"  objects     {report.object_count}",
        f"  connections {report.connection_count}",
        f"  head        {report.head_hash[:16]}",
    ]
    if report.findings:
        lines.append("")
        for item in report.findings:
            lines.append(
                f"  [{item.severity.name.lower():7}] {item.code}: "
                f"{item.subject} — {item.detail}"
            )
    return "\n".join(lines)


def render_json(report: IntegrityReport) -> str:
    return json.dumps(report.as_dict(), indent=2, sort_keys=True)


__all__ = [
    "Finding",
    "IntegrityReport",
    "Severity",
    "render",
    "render_json",
    "verify",
]
