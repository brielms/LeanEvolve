from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

from leanevolve.ledger.artifacts import ArtifactStore
from leanevolve.ledger.paper_intake import (
    FormalMappingInput,
    IntakeError,
    PaperIntakeRequest,
    SourceClaimInput,
    intake,
)
from leanevolve.ledger.store import Ledger


def _request(tmp_path: Path) -> PaperIntakeRequest:
    source = tmp_path / "paper.pdf"
    source.write_bytes(
        b"%PDF-1.7\nA Useful Theorem About Graphs\nTheorem 1 marker\n%%EOF"
    )
    return PaperIntakeRequest(
        id="useful-2026",
        title="A Useful Theorem About Graphs",
        authors=("A. Author",),
        permanent_identifier="doi:10.1234/useful",
        version_label="published-2026",
        publication="Journal 1 (2026), 1-10",
        source_path=source,
        expected_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        extracted_text="A Useful Theorem About Graphs\nTheorem 1 marker",
        extraction_tool="pdftotext",
        extraction_version="1",
        claims=(
            SourceClaimInput(
                id="useful-theorem-1",
                label="Theorem 1",
                locator="printed page 3, PDF page 4",
                marker="Theorem 1 marker",
                normalized_statement="Every useful graph is useful.",
                evidence_state="published_with_proof",
                formal_mappings=(
                    FormalMappingInput(
                        declaration="Project.Useful",
                        source_file="Project/Useful.lean",
                        relationship="literal_encoding",
                        claimed_formal_status="proposition_only",
                    ),
                ),
            ),
        ),
        cited_pages=(4,),
        visually_inspected_pages=(1, 4),
    )


def test_doi_and_local_pdf_happy_path(tmp_path: Path) -> None:
    with Ledger.open(tmp_path / "ledger.sqlite3") as ledger:
        report = intake(
            ledger,
            ArtifactStore(tmp_path / "artifacts"),
            _request(tmp_path),
            formal_declarations=frozenset({"Project.Useful"}),
        )
        assert report.source_artifact_id is not None
        assert report.machine_validation == (
            "source_hash",
            "pdf_signature",
            "title",
            "markers",
            "locators",
        )
        assert ledger.connections(
            from_id=report.publication_id, relation="has_version"
        )


def test_versioned_arxiv_divergence_requires_exact_version(tmp_path: Path) -> None:
    request = replace(
        _request(tmp_path),
        permanent_identifier="arXiv:2601.12345",
        claims=(
            replace(
                _request(tmp_path).claims[0],
                evidence_state="versioned_preprint_only",
            ),
        ),
    )
    with Ledger.open(tmp_path / "ledger.sqlite3") as ledger:
        with pytest.raises(IntakeError, match="versioned"):
            intake(ledger, ArtifactStore(tmp_path / "artifacts"), request)


def test_html_disguised_as_pdf_is_rejected(tmp_path: Path) -> None:
    request = _request(tmp_path)
    request.source_path.write_text("<html>not a pdf</html>")  # type: ignore[union-attr]
    request = replace(
        request,
        expected_sha256=hashlib.sha256(
            request.source_path.read_bytes()  # type: ignore[union-attr]
        ).hexdigest(),
        )
    with Ledger.open(tmp_path / "ledger.sqlite3") as ledger:
        with pytest.raises(IntakeError, match="non-PDF"):
            intake(
                ledger,
                ArtifactStore(tmp_path / "artifacts"),
                request,
                formal_declarations=frozenset({"Project.Useful"}),
            )


def test_inaccessible_source_returns_exact_acquisition_request(tmp_path: Path) -> None:
    request = replace(
        _request(tmp_path),
        source_path=None,
        expected_sha256=None,
        extracted_text=None,
    )
    with Ledger.open(tmp_path / "ledger.sqlite3") as ledger:
        report = intake(
            ledger,
            ArtifactStore(tmp_path / "artifacts"),
            request,
            formal_declarations=frozenset({"Project.Useful"}),
        )
        assert "A. Author" in report.acquisition_request
        assert request.permanent_identifier in report.acquisition_request


def test_specialization_requires_changed_assumptions(tmp_path: Path) -> None:
    original = _request(tmp_path)
    mapping = replace(
        original.claims[0].formal_mappings[0],
        relationship="specialization",
        changed_assumptions=(),
    )
    request = replace(
        original,
        claims=(replace(original.claims[0], formal_mappings=(mapping,)),),
    )
    with Ledger.open(tmp_path / "ledger.sqlite3") as ledger:
        with pytest.raises(IntakeError, match="changed assumptions"):
            intake(
                ledger,
                ArtifactStore(tmp_path / "artifacts"),
                request,
                formal_declarations=frozenset({"Project.Useful"}),
            )


def test_false_kernel_proved_assertion_is_rejected(tmp_path: Path) -> None:
    original = _request(tmp_path)
    mapping = replace(
        original.claims[0].formal_mappings[0],
        claimed_formal_status="kernel_proved",
    )
    request = replace(
        original,
        claims=(replace(original.claims[0], formal_mappings=(mapping,)),),
    )
    with Ledger.open(tmp_path / "ledger.sqlite3") as ledger:
        with pytest.raises(IntakeError, match="false kernel_proved"):
            intake(
                ledger,
                ArtifactStore(tmp_path / "artifacts"),
                request,
                formal_declarations=frozenset({"Project.Useful"}),
            )


def test_title_identifier_mismatch_is_rejected(tmp_path: Path) -> None:
    request = replace(_request(tmp_path), title="Completely Different Work")
    with Ledger.open(tmp_path / "ledger.sqlite3") as ledger:
        with pytest.raises(IntakeError, match="title"):
            intake(
                ledger,
                ArtifactStore(tmp_path / "artifacts"),
                request,
                formal_declarations=frozenset({"Project.Useful"}),
            )


def test_repeated_identical_intake_creates_no_records_or_events(tmp_path: Path) -> None:
    request = _request(tmp_path)
    with Ledger.open(tmp_path / "ledger.sqlite3") as ledger:
        store = ArtifactStore(tmp_path / "artifacts")
        intake(
            ledger,
            store,
            request,
            formal_declarations=frozenset({"Project.Useful"}),
        )
        count = ledger.event_count()
        second = intake(
            ledger,
            store,
            request,
            formal_declarations=frozenset({"Project.Useful"}),
        )
        assert second.event_range == (0, 0)
        assert ledger.event_count() == count


def test_mapping_reuses_existing_exact_declaration(tmp_path: Path) -> None:
    request = _request(tmp_path)
    with Ledger.open(tmp_path / "ledger.sqlite3") as ledger:
        with ledger.write("importer", "tool:test-seed") as session:
            session.create_object(
                "goal:useful",
                "formal_claim",
                "Useful goal",
                content_format="lean",
                content="Project.Useful",
                properties={
                    "declaration": "Project.Useful",
                    "formal_system": "lean4",
                    "proposition_sha256": hashlib.sha256(
                        b"Project.Useful"
                    ).hexdigest(),
                    "environment_identity": "test:existing-goal",
                },
            )
        report = intake(
            ledger,
            ArtifactStore(tmp_path / "artifacts"),
            request,
            formal_declarations=frozenset({"Project.Useful"}),
        )

        assert report.formal_claim_ids == ("goal:useful",)
        assert ledger.connections(
            from_id=report.claim_ids[0],
            relation="formalized_as",
            to_id="goal:useful",
        )
        assert len(
            [
                item
                for item in ledger.objects(kind="formal_claim")
                if item.properties.get("declaration") == "Project.Useful"
            ]
        ) == 1


def test_ambiguous_exact_declaration_rejects_intake_atomically(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    with Ledger.open(tmp_path / "ledger.sqlite3") as ledger:
        with ledger.write("importer", "tool:test-seed") as session:
            for object_id in ("goal:useful-one", "goal:useful-two"):
                session.create_object(
                    object_id,
                    "formal_claim",
                    object_id,
                    content_format="lean",
                    content="Project.Useful",
                    properties={
                        "declaration": "Project.Useful",
                        "formal_system": "lean4",
                        "proposition_sha256": hashlib.sha256(
                            b"Project.Useful"
                        ).hexdigest(),
                        "environment_identity": "test:ambiguous-goal",
                    },
                )
        before = ledger.event_count()

        with pytest.raises(IntakeError, match="ambiguous formal declaration"):
            intake(
                ledger,
                ArtifactStore(tmp_path / "artifacts"),
                request,
                formal_declarations=frozenset({"Project.Useful"}),
            )

        assert ledger.event_count() == before
        assert ledger.object("publication:useful-2026") is None
