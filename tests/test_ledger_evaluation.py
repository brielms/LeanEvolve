from __future__ import annotations

import hashlib
import json
from pathlib import Path

from leanevolve.ledger.derive import state_of
from leanevolve.ledger.evaluation import (
    record_authoritative_evaluation,
    record_promotion,
    record_research_delta,
)
from leanevolve.ledger.store import Ledger, LedgerError
from leanevolve.ledger.worker import extract_declarations


def _goal(ledger: Ledger) -> None:
    with ledger.write("importer", "test:importer") as session:
        session.create_object(
            "goal:test",
            "formal_claim",
            "Test goal",
            content_format="lean",
            content="True",
            properties={
                "formal_system": "lean4",
                "declaration": "Example.TestGoal",
                "proposition_sha256": "a" * 64,
                "environment_identity": "lean:test",
                "role": "goal",
            },
        )


def test_evaluation_then_promotion_derive_truth_and_verification(
    tmp_path: Path,
) -> None:
    database = tmp_path / "ledger.sqlite3"
    artifacts = tmp_path / "artifacts"
    results = tmp_path / "results"
    results.mkdir()
    candidate = results / "candidate.lean"
    candidate.write_text("theorem test_helper : True := by trivial\n")
    declaration = extract_declarations(candidate.read_text())[0]
    proposition = str(declaration["proposition"])
    proposition_sha256 = hashlib.sha256(proposition.encode()).hexdigest()
    helper_id = f"claim:{proposition_sha256[:12]}"
    for name in ("kernel", "axiom", "board"):
        (results / f"{name}_receipt.json").write_text(
            json.dumps({"format": f"test-{name}", "status": "verified"})
        )
    manifest = {
        "candidate_path": str(candidate),
        "accepted_goals": ["test"],
        "obligation_statuses": {"test": "proved"},
        "verification_stage": "kernel_and_axiom",
        "correct": True,
        "evaluator": {"sha256": "b" * 64},
        "lean_toolchain": "lean:test",
        "allowed_standard_axioms": ["propext"],
        "authoritative_receipts": {
            name: {"path": f"{name}_receipt.json"}
            for name in ("kernel", "axiom", "board")
        },
    }
    evaluation_path = results / "evaluation_manifest.json"
    evaluation_path.write_text(json.dumps(manifest))
    promotion_path = results / "promotion_manifest.json"
    promotion_path.write_text(
        json.dumps(
            {
                "status": "verified",
                "accepted_goals": ["test"],
                "materialization_module": "Example.Generated.Frontier",
                "active_goal_catalog": {"catalog_sha256": "c" * 64},
                "cumulative_frontier": {
                    "path": str(candidate),
                    "sha256": hashlib.sha256(candidate.read_bytes()).hexdigest(),
                    "bytes": len(candidate.read_bytes()),
                },
            }
        )
    )
    with Ledger.open(database) as ledger:
        _goal(ledger)
        with ledger.write("importer", "test:importer") as session:
            session.create_object(
                helper_id,
                "formal_claim",
                "test_helper",
                content_format="lean",
                content=proposition,
                properties={
                    "formal_system": "lean4",
                    "declaration": "test_helper",
                    "proposition_sha256": proposition_sha256,
                    "environment_identity": "lean:test",
                },
            )
    first = record_authoritative_evaluation(
        database,
        artifacts,
        evaluation_path,
        campaign_id="campaign:test",
        epoch_id="epoch:test",
        turn_id="turn:test",
    )
    second = record_promotion(
        database,
        artifacts,
        promotion_path,
        campaign_id="campaign:test",
        epoch_id="epoch:test",
        turn_id="turn:test",
    )
    with Ledger.open(database) as ledger:
        state = state_of(ledger, "goal:test")
        assert state.truth == "proved"
        assert state.verification == "promotion_audited"
        helper_state = state_of(ledger, helper_id)
        assert helper_state.truth == "proved"
        assert helper_state.verification == "promotion_audited"
        assert [edge.relation for edge in ledger.connections(
            from_id=helper_id
        )] == ["certified_by", "included_in"]
        scoped = ledger.events(subject_id="goal:test")[-1]
        assert scoped.campaign_id == "campaign:test"
        assert scoped.epoch_id == "epoch:test"
        assert scoped.turn_id == "turn:test"
        count = ledger.event_count()
    assert first[0] > 0 and second[0] > first[1]
    assert record_authoritative_evaluation(
        database, artifacts, evaluation_path
    ) == (0, 0)
    assert record_promotion(database, artifacts, promotion_path) == (0, 0)
    with Ledger.open(database) as ledger:
        assert ledger.event_count() == count


def test_promotion_requires_an_explicit_project_materialization_module(
    tmp_path: Path,
) -> None:
    database = tmp_path / "ledger.sqlite3"
    artifacts = tmp_path / "artifacts"
    manifest = tmp_path / "promotion.json"
    manifest.write_text(
        json.dumps({"status": "verified", "accepted_goals": ["test"]})
    )
    with Ledger.open(database) as ledger:
        _goal(ledger)

    try:
        record_promotion(database, artifacts, manifest)
    except LedgerError as error:
        assert "materialization module" in str(error)
    else:
        raise AssertionError("promotion accepted a project-specific implicit module")


def test_research_annotations_write_through_as_open_scoped_findings(
    tmp_path: Path,
) -> None:
    database = tmp_path / "ledger.sqlite3"
    artifacts = tmp_path / "artifacts"
    delta = tmp_path / "research_ledger_delta.json"
    delta.write_text(json.dumps({
        "operations": [{
            "op": "add",
            "id": "two_cores",
            "title": "Two equality cores",
            "claim": "The radius-three case separates into two cores.",
            "board_goals": ["test"],
            "tags": ["theorem-search"],
        }],
    }))
    with Ledger.open(database) as ledger:
        _goal(ledger)
    first = record_research_delta(
        database,
        artifacts,
        delta,
        campaign_id="campaign:test",
        turn_id="turn:test",
    )
    assert first != (0, 0)
    with Ledger.open(database) as ledger:
        findings = ledger.objects(kind="research_claim")
        assert len(findings) == 1
        finding = findings[0]
        assert state_of(ledger, finding.id).truth == "open"
        assert [edge.to_id for edge in ledger.connections(
            from_id=finding.id, relation="advances"
        )] == ["goal:test"]
        assert ledger.events(subject_id=finding.id)[0].turn_id == "turn:test"
        count = ledger.event_count()
    assert record_research_delta(database, artifacts, delta) == (0, 0)
    with Ledger.open(database) as ledger:
        assert ledger.event_count() == count
