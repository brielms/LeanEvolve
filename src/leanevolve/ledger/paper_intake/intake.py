"""Validated prior-art intake into the shared research graph."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from leanevolve.ledger.artifacts import ArtifactStore, store_and_register
from leanevolve.ledger.store import Ledger, WriteSession
from leanevolve.ledger.vocabulary import (
    FORMALIZATION_RELATIONSHIPS,
    SOURCE_EVIDENCE_STATES,
)


class IntakeError(ValueError):
    """A hard paper-intake gate rejected the request."""


@dataclass(frozen=True)
class FormalMappingInput:
    declaration: str
    source_file: str
    relationship: str
    changed_assumptions: tuple[str, ...] = ()
    claimed_formal_status: str = "proposition_only"


@dataclass(frozen=True)
class SourceClaimInput:
    id: str
    label: str
    locator: str
    marker: str
    normalized_statement: str
    evidence_state: str
    caveats: tuple[str, ...] = ()
    formal_mappings: tuple[FormalMappingInput, ...] = ()


@dataclass(frozen=True)
class PaperIntakeRequest:
    id: str
    title: str
    authors: tuple[str, ...]
    permanent_identifier: str
    version_label: str
    publication: str
    source_path: Path | None
    expected_sha256: str | None
    extracted_text: str | None
    extraction_tool: str
    extraction_version: str
    claims: tuple[SourceClaimInput, ...]
    cited_pages: tuple[int, ...] = ()
    visually_inspected_pages: tuple[int, ...] = ()
    human_reviewed: bool = False


@dataclass(frozen=True)
class IntakeReport:
    publication_id: str
    version_id: str
    source_artifact_id: str | None
    claim_ids: tuple[str, ...]
    formal_claim_ids: tuple[str, ...]
    machine_validation: tuple[str, ...]
    outstanding_human_review: tuple[str, ...]
    acquisition_request: str | None
    event_range: tuple[int, int]


def _slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "-", value).strip("-").lower()


def _validate_request(
    request: PaperIntakeRequest,
    *,
    formal_declarations: frozenset[str],
    certified_declarations: frozenset[str],
) -> tuple[bytes | None, list[str]]:
    if not request.title.strip() or not request.authors:
        raise IntakeError("publication title and authors are required")
    identifier = request.permanent_identifier
    if identifier.lower().startswith("arxiv:") and not re.search(
        r"v\d+$", identifier, re.IGNORECASE
    ):
        raise IntakeError("an arXiv identifier must be versioned")
    if not request.version_label.strip():
        raise IntakeError("an exact source version label is required")
    claim_ids = {claim.id for claim in request.claims}
    if len(claim_ids) != len(request.claims):
        raise IntakeError("source claim IDs must be unique")
    for claim in request.claims:
        if claim.evidence_state not in SOURCE_EVIDENCE_STATES:
            raise IntakeError(f"unknown source evidence state: {claim.evidence_state}")
        if (
            claim.evidence_state == "published_with_proof"
            and identifier.lower().startswith("arxiv:")
        ):
            raise IntakeError("an arXiv-only source may not be labelled published")
        if not claim.locator.strip():
            raise IntakeError(f"source claim {claim.id} lacks a theorem/page locator")
        for mapping in claim.formal_mappings:
            if mapping.relationship not in FORMALIZATION_RELATIONSHIPS:
                raise IntakeError(
                    f"unknown formalization relationship: {mapping.relationship}"
                )
            if mapping.declaration not in formal_declarations:
                raise IntakeError(
                    f"formal declaration does not exist: {mapping.declaration}"
                )
            if (
                mapping.relationship != "literal_encoding"
                and not mapping.changed_assumptions
            ):
                raise IntakeError(
                    f"mapping {mapping.declaration} must state changed assumptions"
                )
            if (
                mapping.claimed_formal_status == "kernel_proved"
                and mapping.declaration not in certified_declarations
            ):
                raise IntakeError(
                    f"false kernel_proved assertion for {mapping.declaration}"
                )
    missing_pages = set(request.cited_pages) - set(request.visually_inspected_pages)
    if missing_pages:
        raise IntakeError(
            "cited pages were not visually inspected: "
            + ", ".join(str(page) for page in sorted(missing_pages))
        )
    if request.source_path is None:
        return None, []
    if not request.source_path.is_file():
        raise IntakeError(f"source file does not exist: {request.source_path}")
    data = request.source_path.read_bytes()
    if request.source_path.suffix.lower() == ".pdf" and not data.startswith(b"%PDF-"):
        raise IntakeError("a .pdf source contains HTML or non-PDF bytes")
    digest = hashlib.sha256(data).hexdigest()
    if request.expected_sha256 and digest != request.expected_sha256:
        raise IntakeError(
            f"source hash mismatch: expected {request.expected_sha256}, got {digest}"
        )
    if request.extracted_text is None:
        raise IntakeError("retained source lacks a validated text extraction")
    normalized_text = " ".join(request.extracted_text.lower().split())
    title_words = [word for word in re.findall(r"[a-z0-9]+", request.title.lower())]
    title_prefix = " ".join(title_words[: min(5, len(title_words))])
    if title_words and title_prefix not in normalized_text:
        raise IntakeError("source title does not match the requested publication")
    for claim in request.claims:
        if claim.marker not in request.extracted_text:
            raise IntakeError(f"source marker not found for claim {claim.id}")
    return data, ["source_hash", "pdf_signature", "title", "markers", "locators"]


def _formal_claim_for_mapping(
    session: WriteSession,
    mapping: FormalMappingInput,
) -> str:
    """Reuse the unique formal claim for a declaration, or create it once."""
    matches = [
        record
        for record in session.ledger.objects(kind="formal_claim")
        if record.properties.get("declaration") == mapping.declaration
    ]
    if len(matches) > 1:
        ids = ", ".join(record.id for record in matches)
        raise IntakeError(
            f"ambiguous formal declaration {mapping.declaration}: {ids}"
        )
    if matches:
        return matches[0].id
    proposition_hash = hashlib.sha256(mapping.declaration.encode()).hexdigest()
    formal_id = f"claim:{proposition_hash[:12]}"
    session.create_object(
        formal_id,
        "formal_claim",
        mapping.declaration,
        content_format="lean",
        content=mapping.declaration,
        properties={
            "formal_system": "lean4",
            "declaration": mapping.declaration,
            "proposition_sha256": proposition_hash,
            "environment_identity": "paper-intake:declaration-only",
            "source_file": mapping.source_file,
        },
    )
    return formal_id


def intake(
    ledger: Ledger,
    artifact_store: ArtifactStore,
    request: PaperIntakeRequest,
    *,
    formal_declarations: frozenset[str] = frozenset(),
    certified_declarations: frozenset[str] = frozenset(),
) -> IntakeReport:
    """Validate fully, then apply one duplicate-safe intake transaction."""
    source_bytes, validations = _validate_request(
        request,
        formal_declarations=formal_declarations,
        certified_declarations=certified_declarations,
    )
    publication_id = f"publication:{_slug(request.id)}"
    version_id = f"source-version:{_slug(request.id)}:{_slug(request.version_label)}"
    before = ledger.event_count()
    source_artifact_id: str | None = None
    formal_ids: list[str] = []
    claim_ids: list[str] = []
    acquisition_request: str | None = None
    with ledger.write("importer", "tool:paper-intake-v1") as session:
        session.create_object(
            publication_id,
            "publication",
            request.title,
            content_format="json",
            content="",
            properties={
                "permanent_identifier": request.permanent_identifier,
                "authors": list(request.authors),
                "publication": request.publication,
            },
        )
        session.create_object(
            version_id,
            "source_version",
            f"{request.title} ({request.version_label})",
            content_format="json",
            content="",
            properties={"version_label": request.version_label},
        )
        session.connect(publication_id, "has_version", version_id)
        if source_bytes is not None:
            stored = store_and_register(
                session,
                artifact_store,
                source_bytes,
                artifact_type="publication_source",
                media_type=(
                    "application/pdf"
                    if request.source_path.suffix.lower() == ".pdf"  # type: ignore[union-attr]
                    else "application/octet-stream"
                ),
                canonical_name=f"Source bytes for {request.title}",
                extra_locations=(str(request.source_path.resolve()),),  # type: ignore[union-attr]
            )
            source_artifact_id = stored.object_id
            session.connect(version_id, "has_source", stored.object_id)
            session.record(
                "source_acquired",
                version_id,
                {
                    "sha256": stored.sha256,
                    "media_type": (
                        "application/pdf"
                        if request.source_path.suffix.lower() == ".pdf"  # type: ignore[union-attr]
                        else "application/octet-stream"
                    ),
                },
                evidence_object_id=stored.object_id,
                idempotency_key=f"source-acquired:{version_id}:{stored.sha256}",
            )
            session.record(
                "source_extraction_recorded",
                version_id,
                {
                    "tool": request.extraction_tool,
                    "tool_version": request.extraction_version,
                },
                evidence_object_id=stored.object_id,
                idempotency_key=f"source-extracted:{version_id}:{stored.sha256}",
            )
        else:
            acquisition_request = (
                f"Please provide the exact source for {', '.join(request.authors)}, "
                f"{request.title}, {request.publication}, "
                f"{request.permanent_identifier}."
            )
            session.record(
                "source_acquisition_failed",
                version_id,
                {
                    "reason": "source_not_obtained",
                    "acquisition_request": acquisition_request,
                },
                idempotency_key=f"source-unavailable:{version_id}",
            )
        for claim in request.claims:
            claim_id = f"source-claim:{_slug(claim.id)}"
            claim_ids.append(claim_id)
            session.create_object(
                claim_id,
                "source_claim",
                claim.label,
                content_format="text",
                content=claim.normalized_statement,
                properties={
                    "locator": claim.locator,
                    "source_evidence_state": (
                        claim.evidence_state
                        if source_bytes is not None
                        else "source_not_obtained"
                    ),
                    "marker": claim.marker,
                    "caveats": list(claim.caveats),
                },
            )
            session.connect(version_id, "contains", claim_id)
            session.record(
                "source_marker_validated",
                claim_id,
                {
                    "marker": claim.marker,
                    "locator": claim.locator,
                    "found": source_bytes is not None,
                },
                idempotency_key=f"marker:{claim_id}:{hashlib.sha256(claim.marker.encode()).hexdigest()}",
            )
            for mapping in claim.formal_mappings:
                formal_id = _formal_claim_for_mapping(session, mapping)
                formal_ids.append(formal_id)
                session.connect(
                    claim_id,
                    "formalized_as",
                    formal_id,
                    {
                        "formalization_relationship": mapping.relationship,
                        "changed_assumptions": list(mapping.changed_assumptions),
                    },
                )
    if request.human_reviewed:
        with ledger.write("human_researcher", "human:paper-review") as session:
            for claim_id in claim_ids:
                session.record(
                    "human_review_recorded",
                    claim_id,
                    {"outcome": "accepted", "caveats": []},
                    idempotency_key=f"human-review:{claim_id}:accepted",
                )
    after = ledger.event_count()
    outstanding = () if request.human_reviewed else (
        "normalized statements require human mathematical review",
        "source-to-formal mappings require human mathematical review",
    )
    return IntakeReport(
        publication_id=publication_id,
        version_id=version_id,
        source_artifact_id=source_artifact_id,
        claim_ids=tuple(claim_ids),
        formal_claim_ids=tuple(formal_ids),
        machine_validation=tuple(validations),
        outstanding_human_review=outstanding,
        acquisition_request=acquisition_request,
        event_range=(before + 1, after) if after > before else (0, 0),
    )


__all__ = [
    "FormalMappingInput",
    "IntakeError",
    "IntakeReport",
    "PaperIntakeRequest",
    "SourceClaimInput",
    "intake",
]
