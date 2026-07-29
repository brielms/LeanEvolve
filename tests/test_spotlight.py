from __future__ import annotations

import pytest

from leanevolve.workflow.errors import Exit, WorkflowError
from leanevolve.workflow.schedule import SPOTLIGHT, parse_schedule
from leanevolve.workflow.spotlight import (
    MAX_SPOTLIGHT_TURNS,
    RelevancePath,
    RelevanceStep,
    SpotlightFocus,
    Verdict,
    build_relevance_path,
    decide_verdict,
    ensure_target_unmutated,
    require_relevance,
    statement_hash,
    validate_turn_budget,
)

STATEMENT = "∀ n, 0 < n → P n"


def proved_step(source: str, target: str, kind: str = "reduction") -> RelevanceStep:
    return RelevanceStep(source, target, kind, "proved")


def focus(**overrides: object) -> SpotlightFocus:
    defaults: dict[str, object] = {
        "goal": "sub_lemma",
        "statement": STATEMENT,
        "statement_sha256": statement_hash(STATEMENT),
        "turn_budget": 3,
        "selector": "researcher",
        "hunch": "the contraction should close it",
        "relevance": RelevancePath(
            goal="sub_lemma",
            final_goal="final",
            steps=(proved_step("sub_lemma", "final"),),
        ),
        "field_goals": ("sub_lemma", "other", "final"),
        "final_goal": "final",
    }
    defaults.update(overrides)
    return SpotlightFocus(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Schedule syntax
# ---------------------------------------------------------------------------


def test_spotlight_schedule_parses_goal_and_budget() -> None:
    schedule = parse_schedule(SPOTLIGHT, "sub_lemma for 3 turns")
    assert schedule.spotlight_goals == ("sub_lemma",)
    assert schedule.epochs[0].kind == SPOTLIGHT
    assert schedule.epochs[0].turns == 3
    assert schedule.describe() == "spotlight sub_lemma for 3 turns"


def test_a_bare_goal_is_a_single_turn() -> None:
    assert parse_schedule(SPOTLIGHT, "sub_lemma").epochs[0].turns == 1


def test_spotlight_turns_are_counted_as_solve_turns() -> None:
    # A spotlight turn costs a model call and is gated like any other, so a
    # cost ceiling must not under-count it.
    assert parse_schedule(SPOTLIGHT, "sub_lemma for 4 turns").solve_turns == 4


def test_spotlight_epoch_is_recorded_with_its_focus() -> None:
    recorded = parse_schedule(SPOTLIGHT, "sub_lemma for 2 turns").as_dict()
    assert recorded["epochs"] == [
        {"index": 0, "kind": "spotlight", "turns": 2, "focus": "sub_lemma"}
    ]


@pytest.mark.parametrize(
    "value",
    ["", "   ", "sub_lemma for 3", "sub_lemma for zero turns", "9lemma for 2 turns"],
)
def test_malformed_spotlight_schedules_are_rejected(value: str) -> None:
    with pytest.raises(WorkflowError) as error:
        parse_schedule(SPOTLIGHT, value)
    assert error.value.exit_code == Exit.USAGE


def test_a_spotlight_may_not_be_a_whole_campaign() -> None:
    with pytest.raises(WorkflowError):
        parse_schedule(SPOTLIGHT, f"sub_lemma for {MAX_SPOTLIGHT_TURNS + 1} turns")


def test_ordinary_schedules_carry_no_focus() -> None:
    assert parse_schedule("chunks", "2,3").spotlight_goals == ()
    assert parse_schedule("steps", "3").spotlight_goals == ()


# ---------------------------------------------------------------------------
# The gate that runs before any model spend
# ---------------------------------------------------------------------------


def test_budget_must_be_a_short_sprint() -> None:
    assert validate_turn_budget(1) == 1
    assert validate_turn_budget(MAX_SPOTLIGHT_TURNS) == MAX_SPOTLIGHT_TURNS
    for bad in (0, -1, MAX_SPOTLIGHT_TURNS + 1):
        with pytest.raises(WorkflowError) as error:
            validate_turn_budget(bad)
        assert error.value.exit_code == Exit.USAGE


def test_a_relevance_free_target_is_rejected() -> None:
    empty = RelevancePath(goal="orphan", final_goal="final", steps=())
    with pytest.raises(WorkflowError) as error:
        require_relevance(empty)
    assert error.value.exit_code == Exit.VALIDATION
    assert "no recorded relevance path" in error.value.message


def test_a_relevance_path_the_kernel_has_not_accepted_is_rejected() -> None:
    path = RelevancePath(
        goal="sub_lemma",
        final_goal="final",
        steps=(
            proved_step("sub_lemma", "bridge"),
            RelevanceStep("bridge", "final", "reduction", "open"),
        ),
    )
    with pytest.raises(WorkflowError) as error:
        require_relevance(path)
    assert error.value.exit_code == Exit.VALIDATION
    assert "bridge -> final" in (error.value.detail or "")


def test_a_fully_proved_relevance_path_is_accepted() -> None:
    path = RelevancePath(
        goal="sub_lemma",
        final_goal="final",
        steps=(proved_step("sub_lemma", "bridge"), proved_step("bridge", "final")),
    )
    assert require_relevance(path) is path


# ---------------------------------------------------------------------------
# The frozen target
# ---------------------------------------------------------------------------


def test_reformatting_a_statement_is_not_mutating_it() -> None:
    ensure_target_unmutated(focus(), "∀ n,   0 < n  →   P n")


@pytest.mark.parametrize(
    "weakened",
    [
        "∀ n, 0 < n → Q n",
        "∀ n, 0 < n → P n ∨ True",
        "∃ n, 0 < n → P n",
        "",
    ],
)
def test_a_mutated_target_is_rejected(weakened: str) -> None:
    with pytest.raises(WorkflowError) as error:
        ensure_target_unmutated(focus(), weakened)
    assert error.value.exit_code == Exit.VALIDATION
    assert "may not be weakened" in (error.value.detail or "")


# ---------------------------------------------------------------------------
# Verdicts
# ---------------------------------------------------------------------------


def test_closing_the_focus_is_proved() -> None:
    outcome = decide_verdict(
        focus(), accepted_goals=["sub_lemma"], refuted_goals=[], turns_used=2
    )
    assert outcome.verdict is Verdict.PROVED


def test_refuting_the_focus_is_refuted() -> None:
    outcome = decide_verdict(
        focus(), accepted_goals=[], refuted_goals=["sub_lemma"], turns_used=1
    )
    assert outcome.verdict is Verdict.REFUTED


def test_exhausting_the_budget_is_unresolved_never_refuted() -> None:
    outcome = decide_verdict(
        focus(turn_budget=3), accepted_goals=[], refuted_goals=[], turns_used=3
    )
    assert outcome.verdict is Verdict.UNRESOLVED
    assert outcome.budget_exhausted
    assert "budget was exhausted" in outcome.reason


def test_an_unrelated_theorem_does_not_resolve_the_focus_but_is_kept() -> None:
    # Focus is a priority, not a filter: the incidental result survives, and it
    # neither rescues nor condemns the frozen objective.
    outcome = decide_verdict(
        focus(turn_budget=3),
        accepted_goals=["unrelated_theorem"],
        refuted_goals=[],
        turns_used=3,
    )
    assert outcome.verdict is Verdict.UNRESOLVED
    assert outcome.incidental_goals == ("unrelated_theorem",)
    assert outcome.reason.endswith("for the focus")


def test_an_unexpected_final_proof_is_accepted_during_a_spotlight() -> None:
    outcome = decide_verdict(
        focus(),
        accepted_goals=["final", "sub_lemma"],
        refuted_goals=[],
        turns_used=1,
    )
    assert outcome.verdict is Verdict.PROVED
    assert "final" in outcome.accepted_goals


def test_refutation_of_the_focus_outranks_an_incidental_acceptance() -> None:
    outcome = decide_verdict(
        focus(),
        accepted_goals=["something_else"],
        refuted_goals=["sub_lemma"],
        turns_used=1,
    )
    assert outcome.verdict is Verdict.REFUTED


def test_every_outcome_is_exactly_one_of_three_verdicts() -> None:
    assert {verdict.value for verdict in Verdict} == {
        "proved",
        "refuted",
        "unresolved",
    }


def test_outcome_receipt_records_budget_and_frozen_hash() -> None:
    outcome = decide_verdict(
        focus(), accepted_goals=[], refuted_goals=[], turns_used=3
    )
    recorded = outcome.as_dict()
    assert recorded["verdict"] == "unresolved"
    assert recorded["turn_budget"] == 3
    assert recorded["focus"]["statement_sha256"] == statement_hash(STATEMENT)
    assert recorded["focus"]["selector"] == "researcher"
    assert recorded["focus"]["hunch"] == "the contraction should close it"


# ---------------------------------------------------------------------------
# Path finding
# ---------------------------------------------------------------------------


def test_the_shortest_recorded_path_is_reported() -> None:
    edges = {
        "start": [("detour", "dependency"), ("bridge", "reduction")],
        "detour": [("bridge", "dependency")],
        "bridge": [("final", "reduction")],
    }
    path = build_relevance_path(
        "start",
        "final",
        edges=edges,
        kernel_statuses={"bridge": "proved", "final": "proved", "detour": "proved"},
    )
    assert [step.target for step in path.steps] == ["bridge", "final"]


def test_a_goal_with_no_route_out_has_no_path() -> None:
    path = build_relevance_path(
        "orphan", "final", edges={"other": [("final", "reduction")]}, kernel_statuses={}
    )
    assert path.steps == ()


def test_a_cycle_in_the_edges_terminates() -> None:
    edges = {"a": [("b", "dependency")], "b": [("a", "dependency")]}
    path = build_relevance_path("a", "final", edges=edges, kernel_statuses={})
    assert path.steps == ()


def test_an_unrecorded_step_status_is_not_treated_as_proved() -> None:
    path = build_relevance_path(
        "start",
        "final",
        edges={"start": [("final", "reduction")]},
        kernel_statuses={},
    )
    assert path.unchecked_steps()
    with pytest.raises(WorkflowError):
        require_relevance(path)
