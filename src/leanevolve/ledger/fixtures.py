"""Golden scenarios that fix the ledger's meaning.

Each scenario is the hardest version of one historical case: work preserved
across a killed turn, a refutation that needs two premises, a candidate that
both failed and succeeded, a claim replaced without being disproved, and a
computation that is valuable without being a theorem.

The scenarios are declarative on purpose.  They are written against the
vocabulary rather than against any storage API, so the same data drives the
core store tests, the historical importer, and the projection rebuild checks.
A scenario that cannot be expressed here is a scenario the vocabulary cannot
express, which is exactly the thing to discover before Phase 1 rather than
after the corpus is imported.

Expectations are stated per object and per dimension.  ``forbidden`` lists the
states that must *not* be derived, because most of the failures these fixtures
guard against are over-claims: a timeout read as a refutation, scratch success
read as promotion, a witness read as a proof.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import MappingProxyType

from leanevolve.ledger.vocabulary import (
    authorize,
    require_object_kind,
    require_state,
    validate_connection,
    validate_event_payload,
)

FIXTURE_FORMAT = "leanevolve-ledger-golden-scenario-v1"

_RECEIPT = "artifact:sha256:" + "1" * 64
_SOURCE = "artifact:sha256:" + "2" * 64
_WITNESS_BYTES = "artifact:sha256:" + "3" * 64
_MANIFEST = "artifact:sha256:" + "4" * 64
_DRAT = "artifact:sha256:" + "5" * 64
_CANDIDATE = "artifact:sha256:" + "7" * 64


@dataclass(frozen=True)
class FixtureObject:
    """One object a scenario needs before its events can refer to it."""

    id: str
    kind: str
    canonical_name: str
    content_format: str = "text"
    content: str = ""
    properties: Mapping[str, object] = field(default_factory=dict)


def _artifact(
    object_id: str,
    canonical_name: str,
    artifact_type: str,
    *,
    byte_size: int,
    media_type: str,
) -> FixtureObject:
    """Declare retained bytes a scenario cites as evidence."""
    return FixtureObject(
        id=object_id,
        kind="artifact",
        canonical_name=canonical_name,
        content_format="none",
        properties={
            "sha256": object_id.rsplit(":", 1)[-1],
            "artifact_type": artifact_type,
            "byte_size": byte_size,
            "media_type": media_type,
        },
    )


def _scope(object_id: str, kind: str, canonical_name: str) -> FixtureObject:
    """Declare a campaign, epoch, turn, or check scope."""
    return FixtureObject(
        id=object_id,
        kind=kind,
        canonical_name=canonical_name,
        content_format="json",
    )


@dataclass(frozen=True)
class FixtureConnection:
    """One typed edge, validated against the relation's declared contract."""

    from_id: str
    relation: str
    to_id: str
    properties: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class FixtureEvent:
    """One append-only change, in the order the scenario requires."""

    actor: str
    action: str
    subject_type: str
    subject_id: str
    payload: Mapping[str, object] = field(default_factory=dict)
    evidence_object_id: str | None = None
    #: Marks the point a scenario asks about mid-history, so that replaying to
    #: this event must reproduce the historical view rather than the current one.
    checkpoint_label: str | None = None


@dataclass(frozen=True)
class Expectation:
    """What the derived state must and must not say about one object."""

    object_id: str
    states: Mapping[str, str] = field(default_factory=dict)
    forbidden: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    note: str = ""


@dataclass(frozen=True)
class Scenario:
    """One golden case: objects, edges, ordered events, and expected reading."""

    name: str
    description: str
    objects: tuple[FixtureObject, ...]
    connections: tuple[FixtureConnection, ...]
    events: tuple[FixtureEvent, ...]
    expectations: tuple[Expectation, ...]
    #: Free-text invariants a projection must satisfy, checked by the stage
    #: that implements that projection.
    projection_invariants: tuple[str, ...] = ()

    def object_kinds(self) -> dict[str, str]:
        return {item.id: item.kind for item in self.objects}

    def validate(self) -> None:
        """Reject a scenario the controlled vocabulary cannot express."""
        kinds = self.object_kinds()
        for item in self.objects:
            require_object_kind(item.kind)
        for edge in self.connections:
            missing = {edge.from_id, edge.to_id} - set(kinds)
            if missing:
                raise ValueError(
                    f"{self.name}: connection names unknown object(s) {missing}"
                )
            validate_connection(
                edge.relation,
                kinds[edge.from_id],
                kinds[edge.to_id],
                edge.properties,
            )
        for event in self.events:
            authorize(event.actor, event.action)
            declared = validate_event_payload(
                event.action,
                event.payload,
                evidence_object_id=event.evidence_object_id,
            )
            if event.subject_type not in declared.subject_types:
                raise ValueError(
                    f"{self.name}: {event.action} does not accept subject type "
                    f"{event.subject_type!r}"
                )
            if event.subject_type == "object" and event.subject_id not in kinds:
                raise ValueError(
                    f"{self.name}: event names unknown object {event.subject_id!r}"
                )
            if event.evidence_object_id is not None:
                # Evidence that does not exist is precisely what the ledger
                # must refuse, so a scenario may not cite it either.
                if event.evidence_object_id not in kinds:
                    raise ValueError(
                        f"{self.name}: {event.action} cites undeclared evidence "
                        f"{event.evidence_object_id!r}"
                    )
                if kinds[event.evidence_object_id] != "artifact":
                    raise ValueError(
                        f"{self.name}: evidence {event.evidence_object_id!r} is "
                        f"a {kinds[event.evidence_object_id]!r}, not retained bytes"
                    )
        for expected in self.expectations:
            if expected.object_id not in kinds:
                raise ValueError(
                    f"{self.name}: expectation names unknown object "
                    f"{expected.object_id!r}"
                )
            for dimension, value in expected.states.items():
                require_state(dimension, value)
            for dimension, values in expected.forbidden.items():
                for value in values:
                    require_state(dimension, value)


# --------------------------------------------------------------------------
# 1. A killed turn must not erase Lean work that already checked.
# --------------------------------------------------------------------------

TIMEOUT_PRESERVES_SCRATCH_SUCCESS = Scenario(
    name="timeout_preserves_scratch_success",
    description=(
        "Three dependent declarations scratch-check.  The model is killed "
        "before it emits a candidate or a final response, so the turn has no "
        "terminal result.  Every checked declaration survives with its exact "
        "source and receipt, none is promoted, and one is later carried "
        "through the ordinary authoritative path."
    ),
    objects=(
        _scope("turn:solve_0001:g1", "turn", "Solve 0001 generation 1"),
        FixtureObject(
            "check:0001",
            "check",
            "Scratch check 0001",
            content_format="json",
            properties={"checkpoint_key": "ckpt-a", "request_class": "proof"},
        ),
        _artifact(
            _SOURCE,
            "Exact submitted scratch source",
            "scratch_source",
            byte_size=4096,
            media_type="text/x-lean",
        ),
        _artifact(
            _RECEIPT,
            "Kernel evaluation receipt",
            "kernel_receipt",
            byte_size=8421,
            media_type="application/json",
        ),
        FixtureObject(
            "claim:base_bound",
            "formal_claim",
            "Finite base bound",
            content_format="lean",
            content="Example.Generated.baseBound",
            properties={
                "formal_system": "lean4",
                "declaration": "Example.Generated.baseBound",
                "proposition_sha256": "a" * 64,
                "environment_identity": "ckpt-a",
            },
        ),
        FixtureObject(
            "claim:structural_profile",
            "formal_claim",
            "Structural profile lemma",
            content_format="lean",
            content="Example.Generated.structuralProfile",
            properties={
                "formal_system": "lean4",
                "declaration": "Example.Generated.structuralProfile",
                "proposition_sha256": "b" * 64,
                "environment_identity": "ckpt-a",
            },
        ),
        FixtureObject(
            "claim:assembly",
            "formal_claim",
            "Combined theorem",
            content_format="lean",
            content="Example.Generated.combinedResult",
            properties={
                "formal_system": "lean4",
                "declaration": "Example.Generated.combinedResult",
                "proposition_sha256": "c" * 64,
                "environment_identity": "ckpt-a",
            },
        ),
    ),
    connections=(
        FixtureConnection("claim:assembly", "depends_on", "claim:base_bound"),
        FixtureConnection("claim:assembly", "depends_on", "claim:structural_profile"),
        FixtureConnection("claim:base_bound", "produced_by", "check:0001"),
        FixtureConnection("claim:structural_profile", "produced_by", "check:0001"),
        FixtureConnection("claim:assembly", "produced_by", "check:0001"),
        FixtureConnection(
            "claim:assembly",
            "certified_by",
            _RECEIPT,
            {"verification_level": "promotion_audited"},
        ),
    ),
    events=(
        FixtureEvent("ledger_service", "turn_started", "object", "turn:solve_0001:g1"),
        FixtureEvent(
            "lean_scratch_gateway",
            "check_submitted",
            "object",
            "check:0001",
            {
                "checkpoint_key": "ckpt-a",
                "environment_identity": "lean4.32.1+mathlib-pinned",
                "input_sha256": "2" * 64,
                "request_class": "proof",
            },
            evidence_object_id=_SOURCE,
        ),
        FixtureEvent(
            "lean_scratch_gateway",
            "check_started",
            "object",
            "check:0001",
            {"worker_id": "warm-ckpt-a-0", "queue_delay_seconds": 0.4},
        ),
        FixtureEvent(
            "lean_scratch_gateway",
            "elaboration_succeeded",
            "object",
            "check:0001",
            {
                "declarations": [
                    "Example.Generated.baseBound",
                    "Example.Generated.structuralProfile",
                    "Example.Generated.combinedResult",
                ]
            },
        ),
        FixtureEvent(
            "lean_scratch_gateway",
            "scratch_kernel_checked",
            "object",
            "claim:base_bound",
            {
                "declaration": "Example.Generated.baseBound",
                "proposition_sha256": "a" * 64,
                "checkpoint_key": "ckpt-a",
                "direct_dependencies": [],
            },
            evidence_object_id=_SOURCE,
        ),
        FixtureEvent(
            "lean_scratch_gateway",
            "scratch_kernel_checked",
            "object",
            "claim:structural_profile",
            {
                "declaration": "Example.Generated.structuralProfile",
                "proposition_sha256": "b" * 64,
                "checkpoint_key": "ckpt-a",
                "direct_dependencies": [],
            },
            evidence_object_id=_SOURCE,
        ),
        FixtureEvent(
            "lean_scratch_gateway",
            "scratch_kernel_checked",
            "object",
            "claim:assembly",
            {
                "declaration": "Example.Generated.combinedResult",
                "proposition_sha256": "c" * 64,
                "checkpoint_key": "ckpt-a",
                "direct_dependencies": [
                    "Example.Generated.baseBound",
                    "Example.Generated.structuralProfile",
                ],
            },
            evidence_object_id=_SOURCE,
        ),
        FixtureEvent(
            "lean_scratch_gateway",
            "check_completed",
            "object",
            "check:0001",
            {"outcome": "elaborated", "exit_code": 0},
            evidence_object_id=_SOURCE,
            checkpoint_label="after_scratch_success",
        ),
        # The model dies here.  Nothing below was produced by the agent.
        FixtureEvent(
            "ledger_service",
            "turn_completed",
            "object",
            "turn:solve_0001:g1",
            {"operational_state": "interrupted"},
            checkpoint_label="model_killed",
        ),
        FixtureEvent(
            "ledger_service",
            "recovery_recorded",
            "object",
            "turn:solve_0001:g1",
            {"resolution": "recoverable: three scratch-checked declarations retained"},
        ),
        # Ordinary authoritative path, run later without the agent.
        FixtureEvent(
            "authoritative_evaluator",
            "authoritative_evaluation_recorded",
            "object",
            "claim:assembly",
            {
                "evaluator_version": "evaluate_candidate-2026.07",
                "stage": "kernel",
                "outcome": "accepted",
            },
            evidence_object_id=_RECEIPT,
        ),
        FixtureEvent(
            "axiom_auditor",
            "axiom_policy_audited",
            "object",
            "claim:assembly",
            {
                "declaration": "Example.Generated.combinedResult",
                "axioms": ["propext", "Classical.choice", "Quot.sound"],
                "policy": "standard",
                "within_policy": True,
            },
            evidence_object_id=_RECEIPT,
        ),
        FixtureEvent(
            "authoritative_evaluator",
            "kernel_certified",
            "object",
            "claim:assembly",
            {
                "declaration": "Example.Generated.combinedResult",
                "proposition_sha256": "c" * 64,
                "toolchain": "leanprover--lean4---v4.32.1",
                "evaluator_version": "evaluate_candidate-2026.07",
                "axiom_policy": "standard",
            },
            evidence_object_id=_RECEIPT,
        ),
        FixtureEvent(
            "authoritative_evaluator",
            "promotion_audited",
            "object",
            "claim:assembly",
            {"outcome": "accepted"},
            evidence_object_id=_RECEIPT,
        ),
    ),
    expectations=(
        Expectation(
            object_id="claim:base_bound",
            states={"truth": "open", "verification": "scratch_checked"},
            forbidden={"truth": ("proved",), "verification": ("promotion_audited",)},
            note="Scratch success is durable and advisory; it is never promotion.",
        ),
        Expectation(
            object_id="claim:structural_profile",
            states={"truth": "open", "verification": "scratch_checked"},
            forbidden={"truth": ("proved",)},
        ),
        Expectation(
            object_id="claim:assembly",
            states={"truth": "proved", "verification": "promotion_audited"},
            note="Proved only because the evaluator, not the agent, certified it.",
        ),
        Expectation(
            object_id="turn:solve_0001:g1",
            states={"operational": "interrupted"},
            forbidden={"operational": ("completed",)},
            note="An interrupted turn never becomes completed by inference.",
        ),
        Expectation(
            object_id="check:0001",
            states={"operational": "completed"},
            note="The check, not the turn, is the transaction boundary.",
        ),
    ),
    projection_invariants=(
        "recovery queue lists claim:base_bound and claim:structural_profile as "
        "scratch-checked but not authoritatively audited",
        "turn delta for turn:solve_0001:g1 includes all three declarations even "
        "though the turn produced no candidate",
        "recovering the work requires no Codex transcript",
    ),
)


# --------------------------------------------------------------------------
# 2. Two premises jointly refute; neither does so alone.
# --------------------------------------------------------------------------

WITNESS_AND_BRIDGE_REFUTE = Scenario(
    name="witness_and_bridge_refute",
    description=(
        "A finite witness and a certified bridge together refute a published "
        "claim.  The refuting edge belongs to the bridge, which depends on the "
        "witness, because one binary edge cannot carry two premises.  An "
        "unverified witness or a bare agent assertion must not move truth."
    ),
    objects=(
        FixtureObject(
            "claim:proposed_claim",
            "research_claim",
            "Proposed construction always succeeds",
            content_format="markdown",
        ),
        FixtureObject(
            "witness:counterexample",
            "counterexample",
            "Finite counterexample witness",
            content_format="json",
            properties={"witness_format": "structured_json"},
        ),
        _artifact(
            _WITNESS_BYTES,
            "Counterexample witness bytes",
            "computation_certificate",
            byte_size=15320,
            media_type="application/json",
        ),
        _artifact(
            _RECEIPT,
            "Kernel evaluation receipt",
            "kernel_receipt",
            byte_size=8421,
            media_type="application/json",
        ),
        FixtureObject(
            "claim:bridge",
            "formal_claim",
            "Certified bridge refutes proposed claim",
            content_format="lean",
            content="Example.PriorArt.proposedClaimFalse",
            properties={
                "formal_system": "lean4",
                "declaration": "Example.PriorArt.proposedClaimFalse",
                "proposition_sha256": "d" * 64,
                "environment_identity": "ckpt-a",
            },
        ),
    ),
    connections=(
        FixtureConnection("claim:bridge", "depends_on", "claim:proposed_claim"),
        FixtureConnection(
            "witness:counterexample",
            "supports",
            "claim:bridge",
            {"trust_level": "checked_certificate"},
        ),
        FixtureConnection(
            "claim:bridge",
            "refutes",
            "claim:proposed_claim",
            {"trust_level": "kernel"},
        ),
        FixtureConnection(
            "claim:bridge",
            "certified_by",
            _RECEIPT,
            {"verification_level": "authoritatively_evaluated"},
        ),
    ),
    events=(
        FixtureEvent(
            "computation_checker",
            "computation_certificate_verified",
            "object",
            "witness:counterexample",
            {
                "checker": "verifier.verify_candidate",
                "checker_version": "2026.07",
                "outcome": "verified",
            },
            evidence_object_id=_WITNESS_BYTES,
        ),
        FixtureEvent(
            "authoritative_evaluator",
            "kernel_certified",
            "object",
            "claim:bridge",
            {
                "declaration": "Example.PriorArt.proposedClaimFalse",
                "proposition_sha256": "d" * 64,
                "toolchain": "leanprover--lean4---v4.32.1",
                "evaluator_version": "evaluate_candidate-2026.07",
                "axiom_policy": "standard",
            },
            evidence_object_id=_RECEIPT,
        ),
    ),
    expectations=(
        Expectation(
            object_id="claim:proposed_claim",
            states={"truth": "refuted"},
            note="Derived from the certified bridge, never from the witness alone.",
        ),
        Expectation(
            object_id="claim:bridge",
            states={"truth": "proved", "verification": "authoritatively_evaluated"},
        ),
        Expectation(
            object_id="witness:counterexample",
            states={"truth": "open"},
            forbidden={"truth": ("proved",)},
            note="A verified certificate is evidence, not a kernel theorem.",
        ),
    ),
    projection_invariants=(
        "removing the kernel_certified event for claim:bridge returns "
        "claim:proposed_claim to open",
        "an agent-asserted refutes edge cannot produce refuted",
    ),
)


# --------------------------------------------------------------------------
# 3. One candidate, several evaluations, no overloaded status.
# --------------------------------------------------------------------------

SAME_CANDIDATE_DIVERGENT_EVALUATIONS = Scenario(
    name="same_candidate_divergent_evaluations",
    description=(
        "One candidate times out in its first evaluation, succeeds in a "
        "second, and is promoted later.  All three outcomes are preserved; "
        "neither the candidate nor either run carries a single status that "
        "has to be overwritten."
    ),
    objects=(
        _artifact(
            _CANDIDATE,
            "Candidate source",
            "candidate_source",
            byte_size=20481,
            media_type="text/x-lean",
        ),
        _artifact(
            _RECEIPT,
            "Kernel evaluation receipt",
            "kernel_receipt",
            byte_size=8421,
            media_type="application/json",
        ),
        _artifact(
            _MANIFEST,
            "Active frontier promotion manifest",
            "promotion_manifest",
            byte_size=12004,
            media_type="application/json",
        ),
        FixtureObject(
            "check:eval_a",
            "check",
            "Evaluation A",
            content_format="json",
            properties={"checkpoint_key": "ckpt-a", "request_class": "evaluation"},
        ),
        FixtureObject(
            "check:eval_b",
            "check",
            "Evaluation B",
            content_format="json",
            properties={"checkpoint_key": "ckpt-a", "request_class": "evaluation"},
        ),
        FixtureObject(
            "claim:evaluated_result",
            "formal_claim",
            "Evaluated intermediate result",
            content_format="lean",
            content="Example.Generated.evaluatedResult",
            properties={
                "formal_system": "lean4",
                "declaration": "Example.Generated.evaluatedResult",
                "proposition_sha256": "e" * 64,
                "environment_identity": "ckpt-a",
            },
        ),
    ),
    connections=(
        FixtureConnection("claim:evaluated_result", "included_in", _CANDIDATE),
        FixtureConnection("claim:evaluated_result", "produced_by", "check:eval_b"),
    ),
    events=(
        FixtureEvent(
            "authoritative_evaluator",
            "check_submitted",
            "object",
            "check:eval_a",
            {
                "checkpoint_key": "ckpt-a",
                "environment_identity": "lean4.32.1+mathlib-pinned",
                "input_sha256": "7" * 64,
                "request_class": "evaluation",
            },
            evidence_object_id=_CANDIDATE,
        ),
        FixtureEvent(
            "authoritative_evaluator",
            "check_timed_out",
            "object",
            "check:eval_a",
            {"budget_seconds": 900},
            checkpoint_label="after_first_timeout",
        ),
        FixtureEvent(
            "authoritative_evaluator",
            "check_submitted",
            "object",
            "check:eval_b",
            {
                "checkpoint_key": "ckpt-a",
                "environment_identity": "lean4.32.1+mathlib-pinned",
                "input_sha256": "7" * 64,
                "request_class": "evaluation",
            },
            evidence_object_id=_CANDIDATE,
        ),
        FixtureEvent(
            "authoritative_evaluator",
            "check_completed",
            "object",
            "check:eval_b",
            {"outcome": "elaborated", "exit_code": 0},
            evidence_object_id=_RECEIPT,
        ),
        FixtureEvent(
            "authoritative_evaluator",
            "kernel_certified",
            "object",
            "claim:evaluated_result",
            {
                "declaration": "Example.Generated.evaluatedResult",
                "proposition_sha256": "e" * 64,
                "toolchain": "leanprover--lean4---v4.32.1",
                "evaluator_version": "evaluate_candidate-2026.07",
                "axiom_policy": "standard",
            },
            evidence_object_id=_RECEIPT,
        ),
        FixtureEvent(
            "authoritative_evaluator",
            "promotion_recorded",
            "object",
            "claim:evaluated_result",
            {"manifest_sha256": "4" * 64, "catalog_sha256": "8" * 64},
            evidence_object_id=_MANIFEST,
        ),
    ),
    expectations=(
        Expectation(
            object_id="check:eval_a",
            states={"operational": "timed_out"},
            forbidden={"operational": ("completed", "failed")},
            note="A timeout is unresolved, never a refutation of the candidate.",
        ),
        Expectation(
            object_id="check:eval_b",
            states={"operational": "completed"},
        ),
        Expectation(
            object_id="claim:evaluated_result",
            states={"truth": "proved", "verification": "promotion_audited"},
            note="The earlier timeout does not weaken the later receipt.",
        ),
    ),
    projection_invariants=(
        "the chronology shows both evaluations of the candidate artifact",
        "no query returns one status for the candidate artifact",
    ),
)


# --------------------------------------------------------------------------
# 4. Replaced is not disproved, and history stays readable.
# --------------------------------------------------------------------------

SUPERSEDED_WITHOUT_REFUTATION = Scenario(
    name="superseded_without_refutation",
    description=(
        "A plausible obstruction is later resolved and replaced.  The old "
        "claim becomes superseded, not refuted, and replaying to the earlier "
        "event must still show it active."
    ),
    objects=(
        FixtureObject(
            "claim:route_obstruction",
            "research_claim",
            "Proposed route has an obstruction",
            content_format="markdown",
        ),
        FixtureObject(
            "claim:route_resolution",
            "research_claim",
            "Route obstruction dissolves under a stronger invariant",
            content_format="markdown",
        ),
    ),
    connections=(
        FixtureConnection(
            "claim:route_resolution",
            "supersedes",
            "claim:route_obstruction",
            {"reason": "obstruction dissolved once the stronger invariant was proved"},
        ),
    ),
    events=(
        FixtureEvent(
            "research_agent",
            "object_created",
            "object",
            "claim:route_obstruction",
            {"kind": "research_claim", "canonical_name": "Route obstruction"},
            checkpoint_label="obstruction_believed",
        ),
        FixtureEvent(
            "human_researcher",
            "object_superseded",
            "object",
            "claim:route_obstruction",
            {
                "superseded_by": "claim:route_resolution",
                "reason": "resolved by the stronger invariant",
            },
        ),
    ),
    expectations=(
        Expectation(
            object_id="claim:route_obstruction",
            states={"truth": "open", "lifecycle": "superseded"},
            forbidden={"truth": ("refuted",)},
            note="Superseded means replaced; it makes no claim about falsity.",
        ),
    ),
    projection_invariants=(
        "replay to obstruction_believed shows claim:route_obstruction active",
        "the current view excludes it from the search frontier",
    ),
)


# --------------------------------------------------------------------------
# 5. Exhaustive computation is valuable and is still not a theorem.
# --------------------------------------------------------------------------

COMPUTATION_IS_NOT_PROOF = Scenario(
    name="computation_is_not_proof",
    description=(
        "A complete SAT/DRAT computation is imported and independently "
        "checked.  Its evidence and significance stay visible while its "
        "formal status stays distinct from kernel proof."
    ),
    objects=(
        _artifact(
            _DRAT,
            "DRAT refutation certificate",
            "computation_certificate",
            byte_size=98_314_112,
            media_type="application/octet-stream",
        ),
        FixtureObject(
            "computation:bounded_sweep",
            "computation",
            "Bounded exhaustive sweep",
            content_format="json",
            properties={"method": "sat_drat"},
        ),
        FixtureObject(
            "claim:no_scoped_witness",
            "research_claim",
            "No witness exists in the scoped family",
            content_format="markdown",
        ),
    ),
    connections=(
        FixtureConnection(
            "computation:bounded_sweep",
            "supports",
            "claim:no_scoped_witness",
            {"trust_level": "checked_certificate"},
        ),
    ),
    events=(
        FixtureEvent(
            "computation_checker",
            "computation_started",
            "object",
            "computation:bounded_sweep",
            {"method": "sat_drat", "parameters": {"bound": 30}},
        ),
        FixtureEvent(
            "computation_checker",
            "computation_completed",
            "object",
            "computation:bounded_sweep",
            {"outcome": "unsat"},
            evidence_object_id=_DRAT,
        ),
        FixtureEvent(
            "computation_checker",
            "computation_certificate_verified",
            "object",
            "computation:bounded_sweep",
            {
                "checker": "drat-trim",
                "checker_version": "2026.02",
                "outcome": "verified",
            },
            evidence_object_id=_DRAT,
        ),
    ),
    expectations=(
        Expectation(
            object_id="claim:no_scoped_witness",
            states={"truth": "open", "encoding": "not_encoded"},
            forbidden={"truth": ("proved",)},
            note="Exhaustive search that finds nothing is not a proof.",
        ),
        Expectation(
            object_id="computation:bounded_sweep",
            states={"operational": "completed"},
        ),
    ),
    projection_invariants=(
        "the computation stays visible in the chronology and the goal board",
        "the formal proof graph excludes it",
    ),
)


SCENARIOS: Mapping[str, Scenario] = MappingProxyType(
    {
        scenario.name: scenario
        for scenario in (
            TIMEOUT_PRESERVES_SCRATCH_SUCCESS,
            WITNESS_AND_BRIDGE_REFUTE,
            SAME_CANDIDATE_DIVERGENT_EVALUATIONS,
            SUPERSEDED_WITHOUT_REFUTATION,
            COMPUTATION_IS_NOT_PROOF,
        )
    }
)


#: A representative writer identity for each actor class.  Scenarios name the
#: class; the concrete identity only has to be stable and recognizable.
SCENARIO_ACTOR_IDS: Mapping[str, str] = MappingProxyType(
    {
        "research_agent": "agent:fixture",
        "lean_scratch_gateway": "tool:lean-scratch-gateway",
        "authoritative_evaluator": "tool:evaluate_candidate",
        "axiom_auditor": "tool:axiom_audit",
        "computation_checker": "tool:verify_candidate",
        "human_researcher": "human:fixture",
        "importer": "tool:import",
        "ledger_service": "svc:ledger",
    }
)


#: Fixture histories are pinned to a fixed instant so that replaying a scenario
#: twice produces the same chain.  A golden database has to be reproducible to
#: be worth comparing against; real events keep their true wall-clock time.
FIXTURE_EPOCH = datetime(2026, 1, 1, tzinfo=UTC)


def _fixture_timestamp(offset: int) -> str:
    return (FIXTURE_EPOCH + timedelta(seconds=offset)).isoformat(
        timespec="microseconds"
    )


def replay_scenario(ledger: object, scenario: Scenario) -> dict[str, int]:
    """Load a scenario into a real ledger and return its labelled replay points.

    Objects are created first, then events in order, then connections: several
    scenarios assert an edge whose validity depends on events that must already
    have been recorded.  ``ledger`` is typed loosely to keep this module free
    of a hard dependency on the store.

    Every write is stamped from :data:`FIXTURE_EPOCH`, so two replays of the
    same scenario produce byte-identical exports and the same head hash.
    """
    offset = 0
    with ledger.write("importer", SCENARIO_ACTOR_IDS["importer"]) as session:
        for item in scenario.objects:
            session.create_object(
                item.id,
                item.kind,
                item.canonical_name,
                content_format=item.content_format,
                content=item.content,
                properties=dict(item.properties),
                occurred_at=_fixture_timestamp(offset),
            )
            offset += 1
    labels: dict[str, int] = {}
    for event in scenario.events:
        with ledger.write(event.actor, SCENARIO_ACTOR_IDS[event.actor]) as session:
            written = session.record(
                event.action,
                event.subject_id,
                dict(event.payload),
                subject_type=event.subject_type,
                evidence_object_id=event.evidence_object_id,
                occurred_at=_fixture_timestamp(offset),
            )
            offset += 1
        if event.checkpoint_label:
            labels[event.checkpoint_label] = written.id
    with ledger.write("importer", SCENARIO_ACTOR_IDS["importer"]) as session:
        for edge in scenario.connections:
            session.connect(
                edge.from_id,
                edge.relation,
                edge.to_id,
                dict(edge.properties),
                occurred_at=_fixture_timestamp(offset),
            )
            offset += 1
    return labels


def all_scenarios() -> Sequence[Scenario]:
    """Return every golden scenario in a stable order."""
    return tuple(SCENARIOS[name] for name in sorted(SCENARIOS))


def validate_all() -> None:
    """Reject any scenario the vocabulary cannot express."""
    for scenario in all_scenarios():
        scenario.validate()


__all__ = [
    "COMPUTATION_IS_NOT_PROOF",
    "FIXTURE_FORMAT",
    "SCENARIO_ACTOR_IDS",
    "SAME_CANDIDATE_DIVERGENT_EVALUATIONS",
    "SCENARIOS",
    "SUPERSEDED_WITHOUT_REFUTATION",
    "TIMEOUT_PRESERVES_SCRATCH_SUCCESS",
    "WITNESS_AND_BRIDGE_REFUTE",
    "Expectation",
    "FixtureConnection",
    "FixtureEvent",
    "FixtureObject",
    "Scenario",
    "all_scenarios",
    "replay_scenario",
    "validate_all",
]
