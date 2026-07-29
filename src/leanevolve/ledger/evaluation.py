"""Write-through authoritative evaluation and promotion receipts."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from leanevolve.ledger.artifacts import ArtifactStore, store_and_register
from leanevolve.ledger.events import canonical_json
from leanevolve.ledger.store import Ledger, LedgerError
from leanevolve.ledger.worker import extract_declarations


def _load(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise LedgerError(f"receipt is not an object: {path}")
    return payload


def _formal_claims_declared_in(
    ledger: Ledger, source_text: str
) -> tuple[tuple[str, str, str], ...]:
    """Resolve exact proposition identities introduced by candidate source."""

    resolved: list[tuple[str, str, str]] = []
    for item in extract_declarations(source_text):
        declaration = str(item["declaration"])
        proposition = str(item["proposition"])
        proposition_sha256 = hashlib.sha256(proposition.encode()).hexdigest()
        claim_id = f"claim:{proposition_sha256[:12]}"
        claim = ledger.object(claim_id)
        if (
            claim is not None
            and claim.kind == "formal_claim"
            and claim.properties.get("proposition_sha256")
            == proposition_sha256
        ):
            resolved.append((claim_id, declaration, proposition_sha256))
    return tuple(resolved)


def record_authoritative_evaluation(
    database: Path,
    artifacts: Path,
    manifest_path: Path,
    *,
    campaign_id: str | None = None,
    epoch_id: str | None = None,
    turn_id: str | None = None,
) -> tuple[int, int]:
    """Commit a previously verified evaluator manifest and its goal results."""
    manifest = _load(manifest_path)
    candidate_path = Path(str(manifest["candidate_path"]))
    if not candidate_path.is_file():
        raise LedgerError(f"evaluation candidate is missing: {candidate_path}")
    receipt_records = manifest.get("authoritative_receipts", {})
    if not isinstance(receipt_records, dict):
        raise LedgerError("evaluation manifest lacks authoritative receipts")
    with Ledger.open(database) as ledger:
        before = ledger.event_count()
        with ledger.write(
            "authoritative_evaluator",
            "tool:shinka-authoritative-evaluator-v1",
            campaign_id=campaign_id,
            epoch_id=epoch_id,
            turn_id=turn_id,
        ) as session:
            store = ArtifactStore(artifacts)
            manifest_artifact = store_and_register(
                session,
                store,
                manifest_path.read_bytes(),
                artifact_type="evaluation_manifest",
                media_type="application/json",
                canonical_name=f"Evaluation manifest {manifest_path.parent.name}",
                extra_locations=(str(manifest_path.resolve()),),
            )
            candidate_artifact = store_and_register(
                session,
                store,
                candidate_path.read_bytes(),
                artifact_type="candidate_source",
                media_type="text/x-lean",
                canonical_name=f"Evaluated candidate {candidate_path.name}",
                extra_locations=(str(candidate_path.resolve()),),
            )
            session.connect(
                candidate_artifact.object_id,
                "included_in",
                manifest_artifact.object_id,
            )
            registered_receipts: dict[str, str] = {}
            for name, record in receipt_records.items():
                if not isinstance(record, dict) or not isinstance(
                    record.get("path"), str
                ):
                    continue
                receipt_path = manifest_path.parent / str(record["path"])
                if not receipt_path.is_file():
                    raise LedgerError(f"evaluation receipt is missing: {receipt_path}")
                stored = store_and_register(
                    session,
                    store,
                    receipt_path.read_bytes(),
                    artifact_type=(
                        "axiom_receipt" if name == "axiom" else "kernel_receipt"
                    ),
                    media_type="application/json",
                    canonical_name=f"{name} receipt for {manifest_path.parent.name}",
                    extra_locations=(str(receipt_path.resolve()),),
                )
                registered_receipts[str(name)] = stored.object_id
            evaluator = manifest.get("evaluator", {})
            evaluator_version = (
                str(evaluator.get("sha256", "unknown"))
                if isinstance(evaluator, dict)
                else "unknown"
            )
            session.record(
                "authoritative_evaluation_recorded",
                candidate_artifact.object_id,
                {
                    "evaluator_version": evaluator_version,
                    "stage": str(manifest.get("verification_stage", "unknown")),
                    "outcome": "accepted" if manifest.get("correct") else "rejected",
                    "goal_statuses": manifest.get("obligation_statuses", {}),
                },
                evidence_object_id=manifest_artifact.object_id,
                idempotency_key=(
                    f"authoritative-evaluation:{manifest_artifact.sha256}"
                ),
            )
            kernel_evidence = registered_receipts.get("kernel") or (
                manifest_artifact.object_id
            )
            for goal_name in manifest.get("accepted_goals", []):
                goal_id = f"goal:{goal_name}"
                goal = ledger.object(goal_id)
                if goal is None:
                    raise LedgerError(f"evaluation accepted unknown goal {goal_name}")
                session.connect(
                    goal_id,
                    "certified_by",
                    kernel_evidence,
                    {"verification_level": "authoritatively_evaluated"},
                )
                session.record(
                    "kernel_certified",
                    goal_id,
                    {
                        "declaration": str(goal.properties["declaration"]),
                        "proposition_sha256": str(
                            goal.properties["proposition_sha256"]
                        ),
                        "toolchain": str(manifest.get("lean_toolchain", "unknown")),
                        "evaluator_version": evaluator_version,
                        "axiom_policy": manifest.get(
                            "allowed_standard_axioms", []
                        ),
                    },
                    evidence_object_id=kernel_evidence,
                    idempotency_key=f"kernel-certified:{goal_id}:{kernel_evidence}",
                )
            if manifest.get("correct"):
                for claim_id, declaration, proposition_sha256 in (
                    _formal_claims_declared_in(
                        ledger, candidate_path.read_text(encoding="utf-8")
                    )
                ):
                    session.connect(
                        claim_id,
                        "certified_by",
                        kernel_evidence,
                        {"verification_level": "authoritatively_evaluated"},
                    )
                    session.record(
                        "kernel_certified",
                        claim_id,
                        {
                            "declaration": declaration,
                            "proposition_sha256": proposition_sha256,
                            "toolchain": str(
                                manifest.get("lean_toolchain", "unknown")
                            ),
                            "evaluator_version": evaluator_version,
                            "axiom_policy": manifest.get(
                                "allowed_standard_axioms", []
                            ),
                        },
                        evidence_object_id=kernel_evidence,
                        idempotency_key=(
                            f"kernel-certified:{claim_id}:{kernel_evidence}"
                        ),
                    )
        after = ledger.event_count()
    return ((before + 1, after) if after > before else (0, 0))


def record_promotion(
    database: Path,
    artifacts: Path,
    manifest_path: Path,
    *,
    campaign_id: str | None = None,
    epoch_id: str | None = None,
    turn_id: str | None = None,
) -> tuple[int, int]:
    """Commit a clean, already-verified frontier promotion."""
    manifest = _load(manifest_path)
    if manifest.get("status") != "verified":
        raise LedgerError("promotion manifest is not verified")
    accepted = manifest.get("accepted_goals", [])
    if not isinstance(accepted, list):
        raise LedgerError("promotion accepted_goals is malformed")
    materialization_module = manifest.get("materialization_module")
    if accepted and (
        not isinstance(materialization_module, str)
        or not materialization_module.strip()
    ):
        raise LedgerError("promotion manifest lacks a materialization module")
    catalog = manifest.get("active_goal_catalog", {})
    catalog_sha256 = (
        str(catalog.get("catalog_sha256", "unknown"))
        if isinstance(catalog, dict)
        else "unknown"
    )
    with Ledger.open(database) as ledger:
        before = ledger.event_count()
        with ledger.write(
            "authoritative_evaluator",
            "tool:shinka-promotion-v1",
            campaign_id=campaign_id,
            epoch_id=epoch_id,
            turn_id=turn_id,
        ) as session:
            store = ArtifactStore(artifacts)
            evidence = store_and_register(
                session,
                store,
                manifest_path.read_bytes(),
                artifact_type="promotion_manifest",
                media_type="application/json",
                canonical_name=f"Promotion {manifest_path.parent.name}",
                extra_locations=(str(manifest_path.resolve()),),
            )
            cumulative = manifest.get("cumulative_frontier", {})
            promoted_source = None
            if isinstance(cumulative, dict) and isinstance(
                cumulative.get("path"), str
            ):
                source_path = Path(str(cumulative["path"]))
                if not source_path.is_file():
                    raise LedgerError("promotion cumulative frontier is missing")
                source_bytes = source_path.read_bytes()
                if (
                    hashlib.sha256(source_bytes).hexdigest()
                    != cumulative.get("sha256")
                    or len(source_bytes) != cumulative.get("bytes")
                ):
                    raise LedgerError("promotion cumulative frontier bytes changed")
                promoted_source = store_and_register(
                    session,
                    store,
                    source_bytes,
                    artifact_type="promoted_frontier_source",
                    media_type="text/x-lean",
                    canonical_name=f"Promoted frontier {manifest_path.parent.name}",
                    extra_locations=(str(source_path.resolve()),),
                )
                session.connect(
                    promoted_source.object_id,
                    "included_in",
                    evidence.object_id,
                )
            for goal_name in accepted:
                goal_id = f"goal:{goal_name}"
                if ledger.object(goal_id) is None:
                    raise LedgerError(f"promotion contains unknown goal {goal_name}")
                session.record(
                    "promotion_audited",
                    goal_id,
                    {"outcome": "accepted"},
                    evidence_object_id=evidence.object_id,
                    idempotency_key=f"promotion-audited:{goal_id}:{evidence.sha256}",
                )
                session.record(
                    "materialization_recorded",
                    goal_id,
                    {"module": materialization_module},
                    evidence_object_id=evidence.object_id,
                    idempotency_key=f"materialized:{goal_id}:{evidence.sha256}",
                )
                session.record(
                    "promotion_recorded",
                    goal_id,
                    {
                        "manifest_sha256": evidence.sha256,
                        "catalog_sha256": catalog_sha256,
                    },
                    evidence_object_id=evidence.object_id,
                    idempotency_key=f"promoted:{goal_id}:{evidence.sha256}",
                )
            if promoted_source is not None and turn_id is not None:
                source_text = source_bytes.decode("utf-8")
                claim_ids = {
                    event.subject_id
                    for event in ledger.events(action="kernel_certified")
                    if event.turn_id == turn_id
                }
                for claim_id in sorted(claim_ids):
                    claim = ledger.object(claim_id)
                    if claim is None or claim.kind != "formal_claim":
                        continue
                    declaration = str(claim.properties.get("declaration", ""))
                    short_name = declaration.rsplit(".", 1)[-1]
                    if not short_name or re.search(
                        rf"(?m)^\s*(?:theorem|lemma|def)\s+{re.escape(short_name)}\b",
                        source_text,
                    ) is None:
                        continue
                    session.connect(
                        claim_id,
                        "included_in",
                        promoted_source.object_id,
                    )
                    session.record(
                        "promotion_audited",
                        claim_id,
                        {"outcome": "accepted"},
                        evidence_object_id=evidence.object_id,
                        idempotency_key=(
                            f"promotion-audited:{claim_id}:{evidence.sha256}"
                        ),
                    )
                    session.record(
                        "materialization_recorded",
                        claim_id,
                        {"module": materialization_module},
                        evidence_object_id=evidence.object_id,
                        idempotency_key=(
                            f"materialized:{claim_id}:{evidence.sha256}"
                        ),
                    )
                    session.record(
                        "promotion_recorded",
                        claim_id,
                        {
                            "manifest_sha256": evidence.sha256,
                            "catalog_sha256": catalog_sha256,
                        },
                        evidence_object_id=evidence.object_id,
                        idempotency_key=(
                            f"promoted:{claim_id}:{evidence.sha256}"
                        ),
                    )
        after = ledger.event_count()
    return ((before + 1, after) if after > before else (0, 0))


def record_research_delta(
    database: Path,
    artifacts: Path,
    delta_path: Path,
    *,
    campaign_id: str | None = None,
    epoch_id: str | None = None,
    turn_id: str | None = None,
) -> tuple[int, int]:
    """Commit model annotations as open canonical findings, never as proof."""

    delta = _load(delta_path)
    operations = delta.get("operations", [])
    if not isinstance(operations, list):
        raise LedgerError("research delta operations must be a list")
    with Ledger.open(database) as ledger:
        before = ledger.event_count()
        with ledger.write(
            "research_agent",
            "tool:shinka-research-annotation-v1",
            campaign_id=campaign_id,
            epoch_id=epoch_id,
            turn_id=turn_id,
        ) as session:
            store = ArtifactStore(artifacts)
            evidence = store_and_register(
                session,
                store,
                delta_path.read_bytes(),
                artifact_type="research_annotation_delta",
                media_type="application/json",
                canonical_name=f"Research annotations {delta_path.parent.name}",
                extra_locations=(str(delta_path.resolve()),),
            )
            for raw in operations:
                if not isinstance(raw, dict):
                    continue
                encoded = canonical_json(raw)
                digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
                object_id = f"research:agent-note:{digest[:24]}"
                claim = str(raw.get("claim") or raw.get("note") or encoded)
                title = str(raw.get("title") or raw.get("id") or "Agent finding")
                session.create_object(
                    object_id,
                    "research_claim",
                    title,
                    content_format="text",
                    content=claim,
                    properties={
                        "role": "research_finding",
                        "source": "shinka_annotation",
                        "operation": str(raw.get("op", "add")),
                        "tags": list(raw.get("tags", [])),
                        "evidence_artifact": evidence.object_id,
                    },
                )
                for goal_name in raw.get("board_goals", []):
                    goal_id = f"goal:{goal_name}"
                    if ledger.object(goal_id) is not None:
                        session.connect(object_id, "advances", goal_id)
        after = ledger.event_count()
    return ((before + 1, after) if after > before else (0, 0))


__all__ = [
    "record_authoritative_evaluation",
    "record_promotion",
    "record_research_delta",
]
