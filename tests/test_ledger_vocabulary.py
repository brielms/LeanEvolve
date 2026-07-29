"""The controlled vocabulary rejects unknown terms and unauthorized actors."""

from __future__ import annotations

import pytest

from leanevolve.ledger import vocabulary as vocab


def test_every_relation_endpoint_names_a_declared_kind() -> None:
    for name, relation in vocab.RELATIONS.items():
        unknown_from = relation.from_kinds - set(vocab.OBJECT_KINDS)
        unknown_to = relation.to_kinds - set(vocab.OBJECT_KINDS)
        assert not unknown_from, f"{name} accepts undeclared source {unknown_from}"
        assert not unknown_to, f"{name} accepts undeclared target {unknown_to}"
        assert relation.status_effect in vocab.STATUS_EFFECTS


def test_every_event_action_names_declared_actors_and_subjects() -> None:
    for name, action in vocab.EVENT_ACTIONS.items():
        unknown = action.actors - set(vocab.ACTORS)
        assert not unknown, f"{name} permits undeclared actor {unknown}"
        assert action.subject_types <= vocab.SUBJECT_TYPES
        overlap = set(action.required_payload) & set(action.optional_payload)
        assert not overlap, f"{name} lists {overlap} as both required and optional"


def test_unknown_terms_are_rejected() -> None:
    with pytest.raises(vocab.VocabularyError):
        vocab.require_object_kind("theorem")
    with pytest.raises(vocab.VocabularyError):
        vocab.require_relation("implies")
    with pytest.raises(vocab.VocabularyError):
        vocab.require_event_action("proved_it")
    with pytest.raises(vocab.VocabularyError):
        vocab.require_actor("someone")
    with pytest.raises(vocab.VocabularyError):
        vocab.require_state("truth", "probably")
    with pytest.raises(vocab.VocabularyError):
        vocab.require_state("plausibility", "high")


class TestAuthority:
    """A research agent cannot certify, and no actor is universally trusted."""

    def test_research_agent_may_not_certify(self) -> None:
        with pytest.raises(vocab.VocabularyError, match="may not emit"):
            vocab.authorize("research_agent", "kernel_certified")

    def test_research_agent_may_not_promote(self) -> None:
        with pytest.raises(vocab.VocabularyError):
            vocab.authorize("research_agent", "promotion_recorded")

    def test_scratch_gateway_may_not_certify(self) -> None:
        with pytest.raises(vocab.VocabularyError):
            vocab.authorize("lean_scratch_gateway", "kernel_certified")

    def test_computation_checker_may_not_certify(self) -> None:
        with pytest.raises(vocab.VocabularyError):
            vocab.authorize("computation_checker", "kernel_certified")

    def test_human_assertion_is_not_a_kernel_proof(self) -> None:
        with pytest.raises(vocab.VocabularyError):
            vocab.authorize("human_researcher", "kernel_certified")

    def test_evaluator_may_certify(self) -> None:
        vocab.authorize("authoritative_evaluator", "kernel_certified")

    def test_agent_may_propose(self) -> None:
        vocab.authorize("research_agent", "object_created")
        vocab.authorize("research_agent", "connection_created")

    def test_only_the_evaluator_bears_truth(self) -> None:
        for action in vocab.TRUTH_BEARING_ACTIONS:
            assert vocab.EVENT_ACTIONS[action].actors == {"authoritative_evaluator"}


class TestConnections:
    def test_endpoint_kinds_are_enforced(self) -> None:
        vocab.validate_connection("depends_on", "formal_claim", "formal_claim")
        with pytest.raises(vocab.VocabularyError, match="source"):
            vocab.validate_connection("depends_on", "artifact", "formal_claim")
        with pytest.raises(vocab.VocabularyError, match="target"):
            vocab.validate_connection("depends_on", "formal_claim", "artifact")

    def test_required_properties_are_enforced(self) -> None:
        with pytest.raises(vocab.VocabularyError, match="requires properties"):
            vocab.validate_connection("supersedes", "research_claim", "research_claim")
        vocab.validate_connection(
            "supersedes",
            "research_claim",
            "research_claim",
            {"reason": "replaced by a sharper bound"},
        )

    def test_refutation_requires_kernel_trust(self) -> None:
        with pytest.raises(vocab.VocabularyError, match="kernel trust"):
            vocab.validate_connection(
                "refutes",
                "formal_claim",
                "research_claim",
                {"trust_level": "computational"},
            )
        vocab.validate_connection(
            "refutes",
            "formal_claim",
            "research_claim",
            {"trust_level": "kernel"},
        )

    def test_an_unverified_witness_cannot_refute(self) -> None:
        # A counterexample witness is evidence; the refuting edge belongs to a
        # proved bridge claim that depends on it.
        with pytest.raises(vocab.VocabularyError, match="source"):
            vocab.validate_connection(
                "refutes",
                "counterexample",
                "research_claim",
                {"trust_level": "kernel"},
            )
        vocab.validate_connection(
            "supports",
            "counterexample",
            "research_claim",
            {"trust_level": "computational"},
        )

    def test_formalization_states_its_mapping_and_changed_assumptions(self) -> None:
        with pytest.raises(vocab.VocabularyError, match="requires properties"):
            vocab.validate_connection(
                "formalized_as", "source_claim", "formal_claim", {}
            )
        with pytest.raises(vocab.VocabularyError, match="formalization relationship"):
            vocab.validate_connection(
                "formalized_as",
                "source_claim",
                "formal_claim",
                {
                    "formalization_relationship": "roughly_the_same",
                    "changed_assumptions": [],
                },
            )
        vocab.validate_connection(
            "formalized_as",
            "source_claim",
            "formal_claim",
            {
                "formalization_relationship": "specialization",
                "changed_assumptions": ["loopless"],
            },
        )

    def test_annotations_may_attach_to_any_kind(self) -> None:
        for kind in vocab.OBJECT_KINDS:
            vocab.validate_connection("annotates", "annotation", kind)


class TestEventPayloads:
    def test_missing_required_field_is_rejected(self) -> None:
        with pytest.raises(vocab.VocabularyError, match="requires payload fields"):
            vocab.validate_event_payload("object_created", {})

    def test_unexpected_field_is_rejected(self) -> None:
        with pytest.raises(vocab.VocabularyError, match="does not accept"):
            vocab.validate_event_payload(
                "object_created", {"kind": "research_claim", "confidence": 0.9}
            )

    def test_evidence_requirement_is_enforced(self) -> None:
        payload = {
            "declaration": "Example.Generated.trial",
            "proposition_sha256": "0" * 64,
            "checkpoint_key": "abc",
        }
        with pytest.raises(vocab.VocabularyError, match="requires an evidence"):
            vocab.validate_event_payload("scratch_kernel_checked", payload)
        vocab.validate_event_payload(
            "scratch_kernel_checked",
            payload,
            evidence_object_id="artifact:sha256:" + "0" * 64,
        )

    def test_interruption_records_who_detected_it(self) -> None:
        # Never inferred from an incomplete log, so the detector is required.
        with pytest.raises(vocab.VocabularyError):
            vocab.validate_event_payload("check_interrupted", {})
        vocab.validate_event_payload(
            "check_interrupted", {"detected_by": "startup_reconciliation"}
        )

    def test_imported_events_declare_their_ordering_fidelity(self) -> None:
        with pytest.raises(vocab.VocabularyError):
            vocab.validate_event_payload(
                "historical_import_recorded",
                {"import_source": "research_ledger.json"},
                evidence_object_id="artifact:sha256:" + "0" * 64,
            )
        vocab.validate_event_payload(
            "historical_import_recorded",
            {"import_source": "research_ledger.json", "ordering_fidelity": "inferred"},
            evidence_object_id="artifact:sha256:" + "0" * 64,
        )


class TestVerificationLadder:
    def test_strongest_level_wins(self) -> None:
        assert (
            vocab.dominant_verification_level(
                ["scratch_checked", "authoritatively_evaluated", "untested"]
            )
            == "authoritatively_evaluated"
        )

    def test_elaboration_failure_never_dominates(self) -> None:
        assert (
            vocab.dominant_verification_level(
                ["elaboration_failed", "scratch_checked"]
            )
            == "scratch_checked"
        )
        assert vocab.dominant_verification_level(["elaboration_failed"]) == "untested"

    def test_empty_history_is_untested(self) -> None:
        assert vocab.dominant_verification_level([]) == "untested"

    def test_unknown_level_is_rejected(self) -> None:
        with pytest.raises(vocab.VocabularyError):
            vocab.dominant_verification_level(["looks_fine"])


class TestStateIndependence:
    """No dimension may imply another."""

    def test_dimensions_do_not_share_values(self) -> None:
        # Overlapping vocabulary between dimensions is how an overloaded
        # status field creeps back in.
        seen: dict[str, str] = {}
        for dimension, values in vocab.STATE_DIMENSIONS.items():
            for value in values:
                assert value not in seen, (
                    f"{value!r} appears in both {seen.get(value)} and {dimension}"
                )
                seen[value] = dimension

    def test_timeout_is_operational_not_truth(self) -> None:
        assert "timed_out" in vocab.OPERATIONAL_STATES
        assert "timed_out" not in vocab.TRUTH_STATES

    def test_fully_encoded_is_not_proved(self) -> None:
        assert "fully_encoded" in vocab.ENCODING_STATES
        assert "fully_encoded" not in vocab.TRUTH_STATES

    def test_superseded_is_lifecycle_not_refuted(self) -> None:
        assert "superseded" in vocab.LIFECYCLE_STATES
        assert "superseded" not in vocab.TRUTH_STATES


class TestExport:
    def test_payload_is_deterministic(self) -> None:
        assert vocab.vocabulary_payload() == vocab.vocabulary_payload()
        assert vocab.vocabulary_sha256() == vocab.vocabulary_sha256()

    def test_payload_covers_every_declared_term(self) -> None:
        payload = vocab.vocabulary_payload()
        assert set(payload["object_kinds"]) == set(vocab.OBJECT_KINDS)
        assert set(payload["relations"]) == set(vocab.RELATIONS)
        assert set(payload["event_actions"]) == set(vocab.EVENT_ACTIONS)
        assert set(payload["actors"]) == set(vocab.ACTORS)
        assert set(payload["states"]) == set(vocab.STATE_DIMENSIONS)
        assert payload["version"] == vocab.VOCABULARY_VERSION
