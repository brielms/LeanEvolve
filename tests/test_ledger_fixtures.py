"""The golden scenarios must be expressible in the frozen vocabulary.

These tests do not exercise storage; they assert that the hardest historical
cases can be said at all.  A failure here means the vocabulary is wrong, which
is far cheaper to learn now than after the corpus is imported.
"""

from __future__ import annotations

import pytest

from leanevolve.ledger import fixtures
from leanevolve.ledger import vocabulary as vocab


def test_every_scenario_validates() -> None:
    fixtures.validate_all()


@pytest.mark.parametrize("scenario", fixtures.all_scenarios(), ids=lambda s: s.name)
class TestScenarioShape:
    def test_has_events_and_expectations(self, scenario: fixtures.Scenario) -> None:
        assert scenario.events, f"{scenario.name} records no history"
        assert scenario.expectations, f"{scenario.name} asserts nothing"

    def test_object_ids_are_unique(self, scenario: fixtures.Scenario) -> None:
        ids = [item.id for item in scenario.objects]
        assert len(ids) == len(set(ids))

    def test_content_formats_are_declared(self, scenario: fixtures.Scenario) -> None:
        for item in scenario.objects:
            assert item.content_format in vocab.CONTENT_FORMATS
            kind = vocab.OBJECT_KINDS[item.kind]
            if kind.content_formats:
                assert item.content_format in kind.content_formats

    def test_declared_required_properties_are_present(
        self, scenario: fixtures.Scenario
    ) -> None:
        for item in scenario.objects:
            kind = vocab.OBJECT_KINDS[item.kind]
            missing = [
                name for name in kind.required_properties if name not in item.properties
            ]
            assert not missing, f"{item.id} is missing {missing}"


class TestPreservationOfKilledWork:
    scenario = fixtures.TIMEOUT_PRESERVES_SCRATCH_SUCCESS

    def test_scratch_success_precedes_the_interruption(self) -> None:
        actions = [event.action for event in self.scenario.events]
        assert actions.index("scratch_kernel_checked") < actions.index("turn_completed")

    def test_every_scratch_result_is_committed_with_evidence(self) -> None:
        checked = [
            event
            for event in self.scenario.events
            if event.action == "scratch_kernel_checked"
        ]
        assert len(checked) == 3
        assert all(event.evidence_object_id for event in checked)

    def test_the_agent_never_certifies(self) -> None:
        for event in self.scenario.events:
            if event.action in vocab.TRUTH_BEARING_ACTIONS:
                assert event.actor == "authoritative_evaluator"

    def test_unpromoted_work_is_not_claimed_as_proved(self) -> None:
        by_id = {item.object_id: item for item in self.scenario.expectations}
        assert by_id["claim:base_bound"].states["truth"] == "open"
        assert "proved" in by_id["claim:base_bound"].forbidden["truth"]

    def test_the_interrupted_turn_is_not_completed(self) -> None:
        by_id = {item.object_id: item for item in self.scenario.expectations}
        turn = by_id["turn:solve_0001:g1"]
        assert turn.states["operational"] == "interrupted"
        assert "completed" in turn.forbidden["operational"]


class TestRefutationNeedsABridge:
    scenario = fixtures.WITNESS_AND_BRIDGE_REFUTE

    def test_the_refuting_edge_belongs_to_the_formal_bridge(self) -> None:
        refutes = [
            edge for edge in self.scenario.connections if edge.relation == "refutes"
        ]
        assert len(refutes) == 1
        kinds = self.scenario.object_kinds()
        assert kinds[refutes[0].from_id] == "formal_claim"
        assert refutes[0].properties["trust_level"] == "kernel"

    def test_the_witness_only_supports(self) -> None:
        for edge in self.scenario.connections:
            if self.scenario.object_kinds()[edge.from_id] == "counterexample":
                assert edge.relation == "supports"

    def test_a_witness_cannot_be_rewritten_into_a_refutation(self) -> None:
        with pytest.raises(vocab.VocabularyError):
            vocab.validate_connection(
                "refutes", "counterexample", "research_claim", {"trust_level": "kernel"}
            )


class TestNoOverloadedStatus:
    scenario = fixtures.SAME_CANDIDATE_DIVERGENT_EVALUATIONS

    def test_both_evaluations_are_retained(self) -> None:
        submitted = [
            event
            for event in self.scenario.events
            if event.action == "check_submitted"
        ]
        assert len(submitted) == 2

    def test_timeout_and_success_coexist(self) -> None:
        by_id = {item.object_id: item for item in self.scenario.expectations}
        assert by_id["check:eval_a"].states["operational"] == "timed_out"
        assert by_id["check:eval_b"].states["operational"] == "completed"

    def test_timeout_is_never_read_as_failure_or_refutation(self) -> None:
        by_id = {item.object_id: item for item in self.scenario.expectations}
        assert "failed" in by_id["check:eval_a"].forbidden["operational"]


class TestSupersessionIsNotRefutation:
    scenario = fixtures.SUPERSEDED_WITHOUT_REFUTATION

    def test_lifecycle_moves_but_truth_does_not(self) -> None:
        expectation = self.scenario.expectations[0]
        assert expectation.states["lifecycle"] == "superseded"
        assert expectation.states["truth"] == "open"
        assert "refuted" in expectation.forbidden["truth"]

    def test_history_has_a_replay_point(self) -> None:
        labels = [
            event.checkpoint_label
            for event in self.scenario.events
            if event.checkpoint_label
        ]
        assert "obstruction_believed" in labels


class TestComputationIsNotProof:
    scenario = fixtures.COMPUTATION_IS_NOT_PROOF

    def test_the_certificate_supports_rather_than_proves(self) -> None:
        edge = self.scenario.connections[0]
        assert edge.relation == "supports"
        assert edge.properties["trust_level"] == "checked_certificate"

    def test_no_truth_bearing_event_appears(self) -> None:
        actions = {event.action for event in self.scenario.events}
        assert not actions & vocab.TRUTH_BEARING_ACTIONS

    def test_the_supported_claim_stays_open(self) -> None:
        by_id = {item.object_id: item for item in self.scenario.expectations}
        assert by_id["claim:no_scoped_witness"].states["truth"] == "open"
