"""Spotlight focus: one frozen objective for a short, time-boxed sprint.

A spotlight narrows what a campaign *prioritizes*; it never narrows what the
campaign may prove, and it never changes what counts as evidence. The final
theorem and the complete goal field stay visible to the model, an unexpected
kernel-valid advance is still accepted, and the same candidate receives the
same verdict whatever the cadence.

Three rules carry most of the weight here, and each exists because the obvious
implementation would be wrong:

* The focus target is **frozen by hash**. A model that could restate its own
  objective could earn credit by weakening it, so the proposition recorded at
  selection is the proposition scored at the end.
* Exhausting the budget is **`unresolved`, never `refuted`**. Running out of
  turns is a fact about the search, not about the mathematics.
* Relevance is **checked before any spend**. A focus with no recorded,
  kernel-checked path toward the final theorem is rejected while it is still
  free to reject it.

This module is deliberately free of any particular project's mathematics. It
knows what a focus, a relevance path, and a verdict *are*; a caller supplies
the goals, the relevance records, and the kernel statuses.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from leanevolve.workflow.errors import Exit, WorkflowError

# A spotlight is a sprint, not a campaign. A long "focus" is a schedule.
MAX_SPOTLIGHT_TURNS = 25

# Kernel statuses a relevance link may carry. Only PROVED lets a focus run.
STATUS_PROVED = "proved"


class Verdict(StrEnum):
    """The only three outcomes a spotlight may report."""

    PROVED = "proved"
    REFUTED = "refuted"
    UNRESOLVED = "unresolved"


def statement_hash(statement: str) -> str:
    """Hash a proposition as the frozen identity of a focus target.

    Whitespace is normalized so that reformatting a statement is not mistaken
    for restating it, while any change to a token changes the hash.
    """
    normalized = " ".join(statement.split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RelevanceStep:
    """One recorded link on the path from a focus target to the final theorem."""

    source: str
    target: str
    kind: str
    # The kernel's word on this link. A step that is merely *stated* is not a
    # step that has been *checked*.
    kernel_status: str

    @property
    def is_kernel_checked(self) -> bool:
        return self.kernel_status == STATUS_PROVED

    def describe(self) -> str:
        mark = "kernel-proved" if self.is_kernel_checked else self.kernel_status
        return f"{self.source} -> {self.target} ({self.kind}, {mark})"

    def as_dict(self) -> dict[str, object]:
        return {
            "source": self.source,
            "target": self.target,
            "kind": self.kind,
            "kernel_status": self.kernel_status,
        }


@dataclass(frozen=True)
class RelevancePath:
    """A recorded route from a focus target to the project's final theorem."""

    goal: str
    final_goal: str
    steps: tuple[RelevanceStep, ...]

    @property
    def terminal_step(self) -> RelevanceStep | None:
        return self.steps[-1] if self.steps else None

    def unchecked_steps(self) -> tuple[RelevanceStep, ...]:
        return tuple(step for step in self.steps if not step.is_kernel_checked)

    def describe(self) -> str:
        if not self.steps:
            return f"{self.goal}: no recorded path to {self.final_goal}"
        return " | ".join(step.describe() for step in self.steps)

    def as_dict(self) -> dict[str, object]:
        return {
            "goal": self.goal,
            "final_goal": self.final_goal,
            "steps": [step.as_dict() for step in self.steps],
            "description": self.describe(),
        }


@dataclass(frozen=True)
class SpotlightFocus:
    """A validated, frozen focus target and the budget it may spend."""

    goal: str
    statement: str
    statement_sha256: str
    turn_budget: int
    selector: str
    hunch: str
    relevance: RelevancePath
    # Everything the field may still prove. A spotlight reprioritizes this
    # list; it never truncates it.
    field_goals: tuple[str, ...] = field(default=())
    final_goal: str = ""
    # Frozen starting states make spotlight fitness a replayable delta rather
    # than a function of whatever frontier happens to be active at replay
    # time.  Tuple pairs keep this frozen dataclass transitively immutable.
    baseline_statuses: tuple[tuple[str, str], ...] = field(default=())

    def as_dict(self) -> dict[str, object]:
        return {
            "goal": self.goal,
            "statement": self.statement,
            "statement_sha256": self.statement_sha256,
            "turn_budget": self.turn_budget,
            "selector": self.selector,
            "hunch": self.hunch,
            "relevance": self.relevance.as_dict(),
            "final_goal": self.final_goal,
            "field_goal_count": len(self.field_goals),
            "baseline_statuses": dict(self.baseline_statuses),
        }


@dataclass(frozen=True)
class SpotlightOutcome:
    """What a finished spotlight sprint is allowed to claim."""

    focus: SpotlightFocus
    verdict: Verdict
    turns_used: int
    accepted_goals: tuple[str, ...]
    refuted_goals: tuple[str, ...]
    budget_exhausted: bool
    reason: str

    @property
    def incidental_goals(self) -> tuple[str, ...]:
        """Accepted goals other than the focus: kept, never discarded."""
        return tuple(name for name in self.accepted_goals if name != self.focus.goal)

    def as_dict(self) -> dict[str, object]:
        return {
            "format": "leanevolve-spotlight-outcome-v1",
            "focus": self.focus.as_dict(),
            "verdict": self.verdict.value,
            "turns_used": self.turns_used,
            "turn_budget": self.focus.turn_budget,
            "budget_exhausted": self.budget_exhausted,
            "accepted_goals": list(self.accepted_goals),
            "incidental_goals": list(self.incidental_goals),
            "refuted_goals": list(self.refuted_goals),
            "reason": self.reason,
        }

    def render(self) -> str:
        lines = [
            f"spotlight {self.focus.goal}: {self.verdict.value.upper()}",
            f"  reason        {self.reason}",
            f"  turns         {self.turns_used}/{self.focus.turn_budget}"
            + (" (budget exhausted)" if self.budget_exhausted else ""),
            f"  target sha256 {self.focus.statement_sha256[:16]}",
            f"  relevance     {self.focus.relevance.describe()}",
        ]
        if self.incidental_goals:
            lines.append(
                "  also accepted " + ", ".join(self.incidental_goals)
            )
        if self.refuted_goals:
            lines.append("  refuted       " + ", ".join(self.refuted_goals))
        return "\n".join(lines)


def validate_turn_budget(turns: int) -> int:
    """Reject a budget that is not a short sprint."""
    if turns < 1 or turns > MAX_SPOTLIGHT_TURNS:
        raise WorkflowError(
            f"spotlight budget {turns} is not in 1..{MAX_SPOTLIGHT_TURNS}",
            exit_code=Exit.USAGE,
            remediation=(
                "a spotlight is a short sprint; pass a budget in "
                f"1..{MAX_SPOTLIGHT_TURNS}, or run an ordinary campaign instead"
            ),
        )
    return turns


def require_relevance(path: RelevancePath) -> RelevancePath:
    """Reject a focus with no recorded, kernel-checked path to the final theorem.

    This runs before any campaign directory is created or any model turn is
    spent: a relevance-free focus costs nothing to refuse.
    """
    if not path.steps:
        raise WorkflowError(
            f"spotlight target {path.goal!r} has no recorded relevance path to "
            f"{path.final_goal!r}",
            exit_code=Exit.VALIDATION,
            remediation=(
                "record a relevance or decomposition path for this goal, or "
                "choose a target that already has one"
            ),
            detail=(
                "A spotlight may only focus a goal whose advance is recorded "
                "as advancing the final theorem. Focus is a priority, not a "
                "licence to work on something unconnected."
            ),
        )
    unchecked = path.unchecked_steps()
    if unchecked:
        listed = "\n".join(f"  {step.describe()}" for step in unchecked)
        raise WorkflowError(
            f"spotlight target {path.goal!r} has a relevance path that the "
            "kernel has not accepted",
            exit_code=Exit.VALIDATION,
            remediation=(
                "prove the unaccepted relevance step first, then spotlight "
                "this goal"
            ),
            detail=f"unaccepted steps:\n{listed}",
        )
    return path


def ensure_target_unmutated(focus: SpotlightFocus, statement: str) -> None:
    """Reject a focus whose proposition changed after it was frozen.

    Weakening the objective is the cheapest way to fake a spotlight success,
    so the proposition is compared by hash rather than trusted by name.
    """
    observed = statement_hash(statement)
    if observed != focus.statement_sha256:
        raise WorkflowError(
            f"spotlight target {focus.goal!r} no longer matches the frozen "
            "proposition",
            exit_code=Exit.VALIDATION,
            remediation=(
                "restore the recorded proposition, or start a new spotlight "
                "epoch for the changed statement"
            ),
            detail=(
                f"frozen   {focus.statement_sha256}\n"
                f"observed {observed}\n"
                "A spotlight goal may not be weakened or restated to earn credit."
            ),
        )


def decide_verdict(
    focus: SpotlightFocus,
    *,
    accepted_goals: Iterable[str],
    refuted_goals: Iterable[str],
    turns_used: int,
) -> SpotlightOutcome:
    """Reduce a finished sprint to exactly one of proved, refuted, unresolved.

    Only the focus target decides the verdict. Incidental results are recorded
    and kept -- an unrelated theorem proved during a spotlight is still a real
    theorem -- but they neither rescue nor condemn the focus.
    """
    accepted = tuple(dict.fromkeys(accepted_goals))
    refuted = tuple(dict.fromkeys(refuted_goals))
    exhausted = turns_used >= focus.turn_budget

    if focus.goal in refuted:
        verdict = Verdict.REFUTED
        reason = "the focus proposition was formally refuted"
    elif focus.goal in accepted:
        verdict = Verdict.PROVED
        reason = "the focus proposition was accepted by the kernel"
    elif exhausted:
        # The budget ran out. That is a fact about the search, not the
        # mathematics, and must never be reported as a refutation.
        verdict = Verdict.UNRESOLVED
        reason = (
            "the spotlight budget was exhausted without a kernel result "
            "for the focus"
        )
    else:
        verdict = Verdict.UNRESOLVED
        reason = "the spotlight ended without a kernel result for the focus"

    return SpotlightOutcome(
        focus=focus,
        verdict=verdict,
        turns_used=turns_used,
        accepted_goals=accepted,
        refuted_goals=refuted,
        budget_exhausted=exhausted,
        reason=reason,
    )


def build_relevance_path(
    goal: str,
    final_goal: str,
    *,
    edges: Mapping[str, Sequence[tuple[str, str]]],
    kernel_statuses: Mapping[str, str],
    max_depth: int = 32,
) -> RelevancePath:
    """Find a recorded path from `goal` toward `final_goal`.

    `edges` maps a goal to `(next_goal, kind)` pairs that are recorded as
    advancing it. The shortest path is preferred so that a receipt records the
    most direct justification rather than an incidental detour. A path is
    returned even when a step is unproved; judging it is `require_relevance`'s
    job, so that the caller can report precisely which link is missing.
    """
    if goal == final_goal:
        return RelevancePath(goal=goal, final_goal=final_goal, steps=())

    # Breadth-first: the first path found is a shortest one.
    queue: list[tuple[str, tuple[RelevanceStep, ...]]] = [(goal, ())]
    seen = {goal}
    while queue:
        current, steps = queue.pop(0)
        if len(steps) >= max_depth:
            continue
        for following, kind in edges.get(current, ()):
            step = RelevanceStep(
                source=current,
                target=following,
                kind=kind,
                kernel_status=kernel_statuses.get(following, "unrecorded"),
            )
            extended = (*steps, step)
            if following == final_goal:
                return RelevancePath(
                    goal=goal, final_goal=final_goal, steps=extended
                )
            if following not in seen:
                seen.add(following)
                queue.append((following, extended))

    return RelevancePath(goal=goal, final_goal=final_goal, steps=())


def focus_receipt(outcome: SpotlightOutcome) -> str:
    """Serialize an outcome as a stable, machine-readable receipt."""
    return json.dumps(outcome.as_dict(), indent=2, sort_keys=True) + "\n"
