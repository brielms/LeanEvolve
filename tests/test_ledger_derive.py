"""Derived state is computed, independent per dimension, and replayable.

These tests drive the golden scenarios from ``fixtures.py`` through the real
store and assert the expectations those scenarios declare.
"""

from __future__ import annotations

import pytest

from leanevolve.ledger import fixtures
from leanevolve.ledger.derive import state_of, truth_of
from leanevolve.ledger.fixtures import replay_scenario as replay
from leanevolve.ledger.store import Ledger


@pytest.fixture()
def ledger(tmp_path) -> Ledger:
    with Ledger.open(tmp_path / "derive.sqlite3") as store:
        yield store


@pytest.mark.parametrize("scenario", fixtures.all_scenarios(), ids=lambda s: s.name)
def test_scenario_expectations_hold(
    ledger: Ledger, scenario: fixtures.Scenario
) -> None:
    replay(ledger, scenario)
    for expected in scenario.expectations:
        derived = state_of(ledger, expected.object_id).as_dict()
        for dimension, value in expected.states.items():
            assert derived[dimension] == value, (
                f"{scenario.name}: {expected.object_id} {dimension} "
                f"is {derived[dimension]!r}, expected {value!r} — {expected.note}"
            )
        for dimension, forbidden in expected.forbidden.items():
            assert derived[dimension] not in forbidden, (
                f"{scenario.name}: {expected.object_id} {dimension} must not be "
                f"{derived[dimension]!r} — {expected.note}"
            )


class TestTruthHasOneWayIn:
    def test_an_agent_assertion_cannot_produce_proved(self, ledger: Ledger) -> None:
        with ledger.write("research_agent", "agent:test") as session:
            session.create_object(
                "claim:hopeful", "research_claim", "Surely true",
                content_format="markdown",
            )
        assert truth_of(ledger, "claim:hopeful") == "open"

    def test_scratch_success_alone_is_not_proved(self, ledger: Ledger) -> None:
        scenario = fixtures.TIMEOUT_PRESERVES_SCRATCH_SUCCESS
        replay(ledger, scenario)
        derived = state_of(ledger, "claim:base_bound")
        assert derived.verification == "scratch_checked"
        assert derived.truth == "open"

    def test_refutation_needs_a_proved_bridge(self, ledger: Ledger) -> None:
        replay(ledger, fixtures.WITNESS_AND_BRIDGE_REFUTE)
        assert truth_of(ledger, "claim:proposed_claim") == "refuted"

    def test_retracting_the_bridge_reopens_the_target(self, ledger: Ledger) -> None:
        replay(ledger, fixtures.WITNESS_AND_BRIDGE_REFUTE)
        with ledger.write("human_researcher", "human:researcher") as session:
            session.retract(
                "claim:bridge",
                "refutes",
                "claim:proposed_claim",
                reason="receipt withdrawn",
            )
        assert truth_of(ledger, "claim:proposed_claim") == "open"


class TestReplay:
    def test_history_reproduces_the_earlier_view(self, ledger: Ledger) -> None:
        labels = replay(ledger, fixtures.SUPERSEDED_WITHOUT_REFUTATION)
        at_belief = labels["obstruction_believed"]
        assert (
            state_of(ledger, "claim:route_obstruction", until=at_belief).lifecycle
            == "active"
        )
        assert state_of(ledger, "claim:route_obstruction").lifecycle == "superseded"

    def test_superseded_never_becomes_refuted(self, ledger: Ledger) -> None:
        replay(ledger, fixtures.SUPERSEDED_WITHOUT_REFUTATION)
        assert truth_of(ledger, "claim:route_obstruction") == "open"

    def test_the_view_before_certification_is_open(self, ledger: Ledger) -> None:
        labels = replay(ledger, fixtures.TIMEOUT_PRESERVES_SCRATCH_SUCCESS)
        at_kill = labels["model_killed"]
        assert state_of(ledger, "claim:assembly", until=at_kill).truth == "open"
        assert state_of(ledger, "claim:assembly").truth == "proved"


class TestOperationalIndependence:
    def test_a_timeout_is_not_a_failure_or_a_refutation(self, ledger: Ledger) -> None:
        replay(ledger, fixtures.SAME_CANDIDATE_DIVERGENT_EVALUATIONS)
        assert state_of(ledger, "check:eval_a").operational == "timed_out"
        assert state_of(ledger, "check:eval_a").truth == "open"

    def test_an_interrupted_turn_does_not_taint_its_completed_check(
        self, ledger: Ledger
    ) -> None:
        replay(ledger, fixtures.TIMEOUT_PRESERVES_SCRATCH_SUCCESS)
        assert state_of(ledger, "turn:solve_0001:g1").operational == "interrupted"
        assert state_of(ledger, "check:0001").operational == "completed"

    def test_a_verified_certificate_is_not_a_theorem(self, ledger: Ledger) -> None:
        replay(ledger, fixtures.COMPUTATION_IS_NOT_PROOF)
        assert state_of(ledger, "computation:bounded_sweep").truth == "open"
        assert state_of(ledger, "claim:no_scoped_witness").truth == "open"


class TestEncodingIndependence:
    def test_encoding_says_nothing_about_truth(self, ledger: Ledger) -> None:
        with ledger.write("human_researcher", "human:researcher") as session:
            session.create_object(
                "source_claim:published_result",
                "source_claim",
                "Published result",
                content_format="markdown",
                properties={
                    "locator": "p. 271, Theorem 1",
                    "source_evidence_state": "published_with_proof",
                },
            )
            session.create_object(
                "claim:published_result_formal",
                "formal_claim",
                "Published result, formal",
                content_format="lean",
                content="Example.PriorArt.publishedResult",
                properties={
                    "formal_system": "lean4",
                    "declaration": "Example.PriorArt.publishedResult",
                    "proposition_sha256": "f" * 64,
                    "environment_identity": "ckpt-a",
                },
            )
            session.connect(
                "source_claim:published_result",
                "formalized_as",
                "claim:published_result_formal",
                {
                    "formalization_relationship": "literal_encoding",
                    "changed_assumptions": [],
                },
            )
        derived = state_of(ledger, "source_claim:published_result")
        assert derived.encoding == "fully_encoded"
        assert derived.truth == "open"
        assert derived.source_evidence == "published_with_proof"

    def test_a_specialization_is_only_partially_formalized(
        self, ledger: Ledger
    ) -> None:
        with ledger.write("human_researcher", "human:researcher") as session:
            session.create_object(
                "source_claim:general",
                "source_claim",
                "General statement",
                content_format="markdown",
                properties={
                    "locator": "p. 3, Theorem 2",
                    "source_evidence_state": "versioned_preprint_only",
                },
            )
            session.create_object(
                "claim:loopless_case",
                "formal_claim",
                "Loopless case",
                content_format="lean",
                content="Example.PriorArt.looplessCase",
                properties={
                    "formal_system": "lean4",
                    "declaration": "Example.PriorArt.looplessCase",
                    "proposition_sha256": "e" * 64,
                    "environment_identity": "ckpt-a",
                },
            )
            session.connect(
                "source_claim:general",
                "formalized_as",
                "claim:loopless_case",
                {
                    "formalization_relationship": "specialization",
                    "changed_assumptions": ["loopless"],
                },
            )
        assert state_of(ledger, "source_claim:general").encoding == (
            "partially_formalized"
        )
