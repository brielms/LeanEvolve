"""Controlled vocabulary for the unified research ledger.

Object kinds, connection relations, event actions, actor authority, and the
independent derived-state dimensions are frozen here.  Unknown terms are
rejected by default: a write that names a kind, relation, action, actor, or
state outside this module fails before it reaches the database.

The vocabulary is versioned.  Adding a term bumps ``VOCABULARY_VERSION``;
renaming or removing one additionally requires a supersession event, because
historical events keep referring to the old term forever.

Nothing here describes a particular research project.  Kinds say what an object
*is*; roles such as goal, spotlight target, or frontier member are validated
properties or projection concerns, so a goal does not become a second object
when it is proved.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

VOCABULARY_FORMAT = "leanevolve-ledger-vocabulary-v1"
VOCABULARY_VERSION = 1


class VocabularyError(ValueError):
    """Raised when a term is unknown or used against its declared contract."""


# --------------------------------------------------------------------------
# Derived-state dimensions
#
# These dimensions are independent.  None of them implies another: a timeout
# means unresolved and never refuted, an operational failure does not
# invalidate a completed check, computational evidence is not a kernel
# theorem, a fully encoded proposition is not a proved one, and superseded
# means replaced rather than false.
# --------------------------------------------------------------------------

TRUTH_STATES = frozenset({"open", "proved", "refuted"})

VERIFICATION_LEVELS = (
    "untested",
    "elaboration_failed",
    "scratch_checked",
    "axiom_policy_audited",
    "authoritatively_evaluated",
    "promotion_audited",
)

#: Verification is a ladder; ``elaboration_failed`` is a terminal outcome for
#: one attempt rather than a rung, so it never dominates another level.
VERIFICATION_ORDER = MappingProxyType(
    {
        "untested": 0,
        "elaboration_failed": 0,
        "scratch_checked": 1,
        "axiom_policy_audited": 2,
        "authoritatively_evaluated": 3,
        "promotion_audited": 4,
    }
)

ENCODING_STATES = frozenset(
    {
        "not_encoded",
        "proposition_only",
        "partially_formalized",
        "fully_encoded",
    }
)

LIFECYCLE_STATES = frozenset({"active", "superseded", "retracted", "archived"})

SOURCE_EVIDENCE_STATES = frozenset(
    {
        "published_with_proof",
        "published_claim_with_omitted_argument",
        "versioned_preprint_only",
        "published_open_conjecture",
        "secondary_attribution_only",
        "claimed_proof_unaccepted",
        "source_not_obtained",
    }
)

OPERATIONAL_STATES = frozenset(
    {
        "queued",
        "running",
        "completed",
        "failed",
        "timed_out",
        "interrupted",
        "recoverable",
    }
)

STATE_DIMENSIONS = MappingProxyType(
    {
        "truth": frozenset(TRUTH_STATES),
        "verification": frozenset(VERIFICATION_LEVELS),
        "encoding": frozenset(ENCODING_STATES),
        "lifecycle": frozenset(LIFECYCLE_STATES),
        "source_evidence": frozenset(SOURCE_EVIDENCE_STATES),
        "operational": frozenset(OPERATIONAL_STATES),
    }
)


# --------------------------------------------------------------------------
# Object kinds
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ObjectKind:
    """One thing that can be named, connected, or made the subject of an event."""

    name: str
    description: str
    required_properties: tuple[str, ...] = ()
    content_formats: frozenset[str] = frozenset()


_OBJECT_KINDS: tuple[ObjectKind, ...] = (
    ObjectKind(
        name="formal_claim",
        description=(
            "An exact proposition in a formal system.  Identity is the "
            "normalized proposition, not the declaration name: changing the "
            "proposition creates a new object plus an explicit relationship, "
            "while renaming a declaration only adds an alias.  A proposition "
            "encoded as `def ... : Prop` is a formal claim even with no proof."
        ),
        required_properties=(
            "formal_system",
            "declaration",
            "proposition_sha256",
            "environment_identity",
        ),
        content_formats=frozenset({"lean", "text"}),
    ),
    ObjectKind(
        name="research_claim",
        description=(
            "An informal lemma, reduction, insight, conjecture, or obstruction."
        ),
        content_formats=frozenset({"markdown", "text"}),
    ),
    ObjectKind(
        name="counterexample",
        description="A finite or formal witness intended to refute a claim.",
        required_properties=("witness_format",),
        content_formats=frozenset({"json", "lean", "text"}),
    ),
    ObjectKind(
        name="computation",
        description=(
            "A reproducible search, SAT result, enumeration, or other "
            "computational finding.  Never a kernel theorem."
        ),
        required_properties=("method",),
        content_formats=frozenset({"json", "text"}),
    ),
    ObjectKind(
        name="publication",
        description=(
            "Immutable bibliographic identity.  A DOI identifies a "
            "publication, not exact source bytes."
        ),
        required_properties=("permanent_identifier",),
        content_formats=frozenset({"json"}),
    ),
    ObjectKind(
        name="source_version",
        description="One exact version or edition of a publication.",
        required_properties=("version_label",),
        content_formats=frozenset({"json"}),
    ),
    ObjectKind(
        name="source_claim",
        description=(
            "A theorem, definition, conjecture, or assertion extracted from "
            "one source version, with locators and a source-evidence state."
        ),
        required_properties=("locator", "source_evidence_state"),
        content_formats=frozenset({"markdown", "text"}),
    ),
    ObjectKind(
        name="artifact",
        description=(
            "Content-addressed retained bytes.  The hash is identity; "
            "locations are replaceable and their changes are events."
        ),
        required_properties=("sha256", "artifact_type", "byte_size", "media_type"),
        content_formats=frozenset({"none"}),
    ),
    ObjectKind(
        name="campaign",
        description="An operational scope grouping epochs and turns.",
        content_formats=frozenset({"json"}),
    ),
    ObjectKind(
        name="epoch",
        description=(
            "An autonomous solve chunk, field exploration, human review, "
            "paper-intake boundary, or spotlight sprint.  Changing cadence or "
            "inputs starts a new epoch linked to its predecessor."
        ),
        content_formats=frozenset({"json"}),
    ),
    ObjectKind(
        name="turn",
        description="One attributable unit of agent or human work.",
        content_formats=frozenset({"json"}),
    ),
    ObjectKind(
        name="check",
        description=(
            "One submitted formal-system check with its own durable identity, "
            "so that a check rather than a turn is the transaction boundary."
        ),
        required_properties=("checkpoint_key", "request_class"),
        content_formats=frozenset({"json"}),
    ),
    ObjectKind(
        name="annotation",
        description=(
            "A correction, explanation, caveat, or human review note attached "
            "to another object."
        ),
        content_formats=frozenset({"markdown", "text"}),
    ),
)

OBJECT_KINDS = MappingProxyType({item.name: item for item in _OBJECT_KINDS})

CONTENT_FORMATS = frozenset(
    {"lean", "text", "markdown", "json", "none"},
)

#: Kinds whose identity is anchored by content hash rather than by name.
CONTENT_ADDRESSED_KINDS = frozenset({"artifact"})

#: Operational scopes that may appear in an event envelope.
SCOPE_KINDS = frozenset({"campaign", "epoch", "turn"})


# --------------------------------------------------------------------------
# Connections
# --------------------------------------------------------------------------

#: What a relation is allowed to do to the target's derived state.  Only
#: ``refutes`` and ``supersedes`` may move another object's state, and only
#: when the connection itself carries trusted evidence.
STATUS_EFFECTS = frozenset(
    {
        "none",
        "refutes_target",
        "supersedes_target",
        "encodes_target",
    }
)

#: How a formal encoding relates to the claim it encodes.  Every changed
#: hypothesis or convention must be explicit, so the mapping kind is required
#: on every ``formalized_as`` connection.
FORMALIZATION_RELATIONSHIPS = frozenset(
    {
        "literal_encoding",
        "specialization",
        "strengthening",
        "weakening",
        "consequence",
        "equivalence",
        "helper",
    }
)

_CLAIM_KINDS = frozenset({"formal_claim", "research_claim"})
_EVIDENCE_KINDS = frozenset(
    {"formal_claim", "counterexample", "computation", "artifact"}
)
_ANY_CLAIMLIKE = _CLAIM_KINDS | {"counterexample", "computation", "source_claim"}


@dataclass(frozen=True)
class Relation:
    """A typed, directed relationship between two objects."""

    name: str
    description: str
    from_kinds: frozenset[str]
    to_kinds: frozenset[str]
    status_effect: str = "none"
    required_properties: tuple[str, ...] = ()
    #: True when several premises jointly justify the edge, in which case the
    #: source must be an assembly claim rather than one of the premises.
    requires_assembly_source: bool = False


_MATHEMATICAL_RELATIONS: tuple[Relation, ...] = (
    Relation(
        name="depends_on",
        description="The source uses the target as a proof or assembly dependency.",
        from_kinds=_CLAIM_KINDS,
        to_kinds=_CLAIM_KINDS,
    ),
    Relation(
        name="decomposes_into",
        description="The source goal is reduced to the target subgoal.",
        from_kinds=_CLAIM_KINDS,
        to_kinds=_CLAIM_KINDS,
    ),
    Relation(
        name="supports",
        description=(
            "Evidence or a weaker result raises support for a claim without "
            "proving it."
        ),
        from_kinds=_EVIDENCE_KINDS | {"source_claim"},
        to_kinds=_CLAIM_KINDS,
        required_properties=("trust_level",),
    ),
    Relation(
        name="refutes",
        description=(
            "The source is evidence that refutes the target.  A witness alone "
            "cannot carry this edge: an n-ary refutation goes through a proved "
            "bridge claim with its own explicit dependencies."
        ),
        from_kinds=frozenset({"formal_claim"}),
        to_kinds=_ANY_CLAIMLIKE,
        status_effect="refutes_target",
        required_properties=("trust_level",),
        requires_assembly_source=True,
    ),
    Relation(
        name="supersedes",
        description=(
            "The source replaces an older formulation, route, or record.  "
            "Superseded is not refuted."
        ),
        from_kinds=_ANY_CLAIMLIKE,
        to_kinds=_ANY_CLAIMLIKE,
        status_effect="supersedes_target",
        required_properties=("reason",),
    ),
    Relation(
        name="advances",
        description=(
            "The source materially advances the target without being a literal "
            "formal dependency."
        ),
        from_kinds=_ANY_CLAIMLIKE,
        to_kinds=_CLAIM_KINDS,
    ),
    Relation(
        name="equivalent_to",
        description="The two statements are equivalent under stated assumptions.",
        from_kinds=_CLAIM_KINDS,
        to_kinds=_CLAIM_KINDS,
        required_properties=("changed_assumptions",),
    ),
    Relation(
        name="specializes",
        description="The source adds hypotheses to the target.",
        from_kinds=_CLAIM_KINDS,
        to_kinds=_CLAIM_KINDS,
        required_properties=("changed_assumptions",),
    ),
    Relation(
        name="strengthens",
        description="The source concludes more than the target.",
        from_kinds=_CLAIM_KINDS,
        to_kinds=_CLAIM_KINDS,
        required_properties=("changed_assumptions",),
    ),
    Relation(
        name="weakens",
        description="The source concludes less than the target.",
        from_kinds=_CLAIM_KINDS,
        to_kinds=_CLAIM_KINDS,
        required_properties=("changed_assumptions",),
    ),
)

_PROVENANCE_RELATIONS: tuple[Relation, ...] = (
    Relation(
        name="formalized_as",
        description=(
            "A source or research claim is encoded by a formal claim.  The "
            "mapping kind and any changed assumptions are explicit."
        ),
        from_kinds=frozenset({"source_claim", "research_claim"}),
        to_kinds=frozenset({"formal_claim"}),
        status_effect="encodes_target",
        required_properties=("formalization_relationship", "changed_assumptions"),
    ),
    Relation(
        name="produced_by",
        description=(
            "A claim or artifact was produced by a turn, check, or computation."
        ),
        from_kinds=_ANY_CLAIMLIKE | {"artifact"},
        to_kinds=frozenset({"turn", "check", "computation"}),
    ),
    Relation(
        name="certified_by",
        description=(
            "A formal claim is associated with a certification receipt.  The "
            "receipt is an artifact; the guarantee it carries is recorded by "
            "the event that created this edge."
        ),
        from_kinds=frozenset({"formal_claim"}),
        to_kinds=frozenset({"artifact"}),
        required_properties=("verification_level",),
    ),
    Relation(
        name="included_in",
        description=(
            "A declaration or artifact occurs in a candidate, frontier, or "
            "source snapshot."
        ),
        from_kinds=frozenset({"formal_claim", "artifact"}),
        to_kinds=frozenset({"artifact"}),
    ),
    Relation(
        name="targets",
        description="A turn, epoch, or spotlight attempt targets a claim.",
        from_kinds=SCOPE_KINDS,
        to_kinds=_CLAIM_KINDS,
    ),
    Relation(
        name="annotates",
        description="A note, correction, or caveat applies to another object.",
        from_kinds=frozenset({"annotation"}),
        to_kinds=frozenset(OBJECT_KINDS),
    ),
    Relation(
        name="has_version",
        description="A publication has one exact version or edition.",
        from_kinds=frozenset({"publication"}),
        to_kinds=frozenset({"source_version"}),
    ),
    Relation(
        name="has_source",
        description="A source version has exact retained bytes.",
        from_kinds=frozenset({"source_version"}),
        to_kinds=frozenset({"artifact"}),
    ),
    Relation(
        name="contains",
        description="A source version contains an extracted claim.",
        from_kinds=frozenset({"source_version"}),
        to_kinds=frozenset({"source_claim"}),
    ),
)

RELATIONS = MappingProxyType(
    {item.name: item for item in (*_MATHEMATICAL_RELATIONS, *_PROVENANCE_RELATIONS)}
)

MATHEMATICAL_RELATIONS = frozenset(item.name for item in _MATHEMATICAL_RELATIONS)
PROVENANCE_RELATIONS = frozenset(item.name for item in _PROVENANCE_RELATIONS)

#: Trust levels a supporting or refuting edge may claim.  Only ``kernel`` may
#: drive a derived truth state.
TRUST_LEVELS = frozenset(
    {"kernel", "checked_certificate", "computational", "asserted"}
)


# --------------------------------------------------------------------------
# Actors and authority
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Actor:
    """One class of writer, with the events it is permitted to emit."""

    name: str
    description: str


_ACTORS: tuple[Actor, ...] = (
    Actor(
        name="research_agent",
        description=(
            "Proposes claims and relationships and requests checks.  It may "
            "not certify, promote, or silently revise an exact proposition."
        ),
    ),
    Actor(
        name="lean_scratch_gateway",
        description=(
            "Stores submitted source and records elaboration and scratch "
            "kernel outcomes.  Scratch success is never promotion."
        ),
    ),
    Actor(
        name="authoritative_evaluator",
        description=(
            "Emits the evaluation and certification events from which proved, "
            "refuted, and promotion state are derived."
        ),
    ),
    Actor(
        name="axiom_auditor",
        description="Emits axiom-policy audit outcomes.",
    ),
    Actor(
        name="computation_checker",
        description=(
            "Records reproducible computational findings and independently "
            "checked certificates.  Never masquerades as kernel certification."
        ),
    ),
    Actor(
        name="human_researcher",
        description=(
            "Creates and annotates claims, chooses spotlight targets, corrects "
            "metadata, and authorizes campaigns.  Assertion is not proof."
        ),
    ),
    Actor(
        name="importer",
        description=(
            "Historical migration.  Its events identify their import source "
            "and whether original ordering is exact, inferred, or unknown."
        ),
    ),
    Actor(
        name="ledger_service",
        description=(
            "The ledger itself: reconciliation, retention, artifact location, "
            "and lifecycle bookkeeping."
        ),
    ),
)

ACTORS = MappingProxyType({item.name: item for item in _ACTORS})


# --------------------------------------------------------------------------
# Events
# --------------------------------------------------------------------------

SUBJECT_TYPES = frozenset({"object", "connection", "ledger"})


@dataclass(frozen=True)
class EventAction:
    """One append-only change, with its payload contract and allowed actors."""

    name: str
    description: str
    subject_types: frozenset[str]
    actors: frozenset[str]
    required_payload: tuple[str, ...] = ()
    optional_payload: tuple[str, ...] = ()
    #: True when the event is meaningless without an evidence artifact.
    requires_evidence: bool = False


_ALL_ACTORS = frozenset(ACTORS)
_OBJECT = frozenset({"object"})
_CONNECTION = frozenset({"connection"})
_OBJECT_OR_CONNECTION = frozenset({"object", "connection"})
_LEDGER = frozenset({"ledger"})

_EVENT_ACTIONS: tuple[EventAction, ...] = (
    # -- creation and correction -------------------------------------------
    EventAction(
        name="object_created",
        description="An object entered the graph.",
        subject_types=_OBJECT,
        actors=_ALL_ACTORS,
        required_payload=("kind",),
        optional_payload=("canonical_name", "content_format", "properties"),
    ),
    EventAction(
        name="connection_created",
        description="A typed relationship was asserted.",
        subject_types=_CONNECTION,
        actors=_ALL_ACTORS,
        required_payload=("relation", "from_id", "to_id"),
        optional_payload=("properties",),
    ),
    EventAction(
        name="connection_retracted",
        description=(
            "A relationship no longer holds.  The edge is excluded from the "
            "current projection and preserved in history."
        ),
        subject_types=_CONNECTION,
        actors=frozenset(
            {"human_researcher", "authoritative_evaluator", "ledger_service"}
        ),
        required_payload=("reason",),
    ),
    EventAction(
        name="annotation_added",
        description="A note, caveat, or explanation was attached.",
        subject_types=_OBJECT_OR_CONNECTION,
        actors=_ALL_ACTORS,
        required_payload=("annotation_id",),
    ),
    EventAction(
        name="correction_recorded",
        description=(
            "A historical record was corrected by appending a new fact rather "
            "than rewriting the old one."
        ),
        subject_types=_OBJECT_OR_CONNECTION,
        actors=frozenset({"human_researcher", "ledger_service", "importer"}),
        required_payload=("reason", "corrects_event_id"),
    ),
    EventAction(
        name="alias_added",
        description=(
            "A declaration was renamed or given a traceability alias without "
            "changing mathematical identity."
        ),
        subject_types=_OBJECT,
        actors=frozenset(
            {"human_researcher", "authoritative_evaluator", "ledger_service"}
        ),
        required_payload=("alias",),
    ),
    # -- check lifecycle ----------------------------------------------------
    EventAction(
        name="check_submitted",
        description="A check was allocated an identity and committed with its input.",
        subject_types=_OBJECT,
        actors=frozenset({"lean_scratch_gateway", "authoritative_evaluator"}),
        required_payload=(
            "checkpoint_key",
            "environment_identity",
            "input_sha256",
            "request_class",
        ),
        optional_payload=(
            "priority",
            "budget_seconds",
            "idempotency_key",
            "checker_identity",
        ),
        requires_evidence=True,
    ),
    EventAction(
        name="check_reused",
        description=(
            "A scoped request reused an identical durable check without "
            "re-running its kernel work."
        ),
        subject_types=_OBJECT,
        actors=frozenset({"lean_scratch_gateway"}),
        required_payload=("original_submission_scope",),
    ),
    EventAction(
        name="check_started",
        description="A worker began executing the check.",
        subject_types=_OBJECT,
        actors=frozenset({"lean_scratch_gateway", "authoritative_evaluator"}),
        required_payload=("worker_id",),
        optional_payload=("queue_delay_seconds",),
    ),
    EventAction(
        name="check_completed",
        description="The check produced a terminal result.",
        subject_types=_OBJECT,
        actors=frozenset({"lean_scratch_gateway", "authoritative_evaluator"}),
        required_payload=("outcome", "exit_code"),
        optional_payload=("phase_timings", "diagnostics_sha256"),
        requires_evidence=True,
    ),
    EventAction(
        name="check_failed",
        description="The formal system rejected the input.",
        subject_types=_OBJECT,
        actors=frozenset({"lean_scratch_gateway", "authoritative_evaluator"}),
        required_payload=("exit_code",),
        optional_payload=("phase_timings", "diagnostics_sha256"),
    ),
    EventAction(
        name="check_timed_out",
        description="The check exceeded its budget.  Unresolved, never refuted.",
        subject_types=_OBJECT,
        actors=frozenset({"lean_scratch_gateway", "authoritative_evaluator"}),
        required_payload=("budget_seconds",),
    ),
    EventAction(
        name="check_interrupted",
        description=(
            "The check has no terminal result and its outcome is unknown.  "
            "Success is never inferred from an incomplete log."
        ),
        subject_types=_OBJECT,
        actors=frozenset({"ledger_service"}),
        required_payload=("detected_by",),
    ),
    EventAction(
        name="check_cancelled",
        description=(
            "Queued or running work was cancelled.  Not a mathematical failure."
        ),
        subject_types=_OBJECT,
        actors=frozenset({"ledger_service", "human_researcher"}),
        required_payload=("reason",),
    ),
    EventAction(
        name="check_superseded",
        description=(
            "A newer request made this one irrelevant.  Not a mathematical "
            "failure."
        ),
        subject_types=_OBJECT,
        actors=frozenset({"ledger_service"}),
        required_payload=("superseded_by",),
    ),
    EventAction(
        name="check_deduplicated",
        description="An identical request was answered from an existing check.",
        subject_types=_OBJECT,
        actors=frozenset({"ledger_service"}),
        required_payload=("duplicate_of",),
    ),
    # -- formal outcomes ----------------------------------------------------
    EventAction(
        name="elaboration_succeeded",
        description="The submitted stream elaborated.",
        subject_types=_OBJECT,
        actors=frozenset({"lean_scratch_gateway", "authoritative_evaluator"}),
        required_payload=("declarations",),
    ),
    EventAction(
        name="elaboration_failed",
        description="The submitted stream did not elaborate.",
        subject_types=_OBJECT,
        actors=frozenset({"lean_scratch_gateway", "authoritative_evaluator"}),
        optional_payload=("diagnostics_sha256",),
    ),
    EventAction(
        name="scratch_kernel_checked",
        description=(
            "A declaration passed an in-memory scratch check.  Durable, "
            "advisory, and not promotion."
        ),
        subject_types=_OBJECT,
        actors=frozenset({"lean_scratch_gateway"}),
        required_payload=("declaration", "proposition_sha256", "checkpoint_key"),
        optional_payload=("direct_dependencies",),
        requires_evidence=True,
    ),
    EventAction(
        name="axiom_policy_audited",
        description="Axiom dependencies were compared against the policy.",
        subject_types=_OBJECT,
        actors=frozenset({"axiom_auditor", "authoritative_evaluator"}),
        required_payload=("declaration", "axioms", "policy", "within_policy"),
        requires_evidence=True,
    ),
    EventAction(
        name="authoritative_evaluation_recorded",
        description=(
            "An independent evaluation ran with its own source policy and "
            "produced verified receipts."
        ),
        subject_types=_OBJECT,
        actors=frozenset({"authoritative_evaluator"}),
        required_payload=("evaluator_version", "stage", "outcome"),
        optional_payload=("goal_statuses",),
        requires_evidence=True,
    ),
    EventAction(
        name="kernel_certified",
        description=(
            "The strongest formal guarantee: an exact proposition has a valid "
            "receipt binding source, dependencies, toolchain, evaluator, "
            "catalog, checkpoint, and axiom policy."
        ),
        subject_types=_OBJECT,
        actors=frozenset({"authoritative_evaluator"}),
        required_payload=(
            "declaration",
            "proposition_sha256",
            "toolchain",
            "evaluator_version",
            "axiom_policy",
        ),
        requires_evidence=True,
    ),
    EventAction(
        name="materialization_recorded",
        description="Accepted declarations were materialized into maintained source.",
        subject_types=_OBJECT,
        actors=frozenset({"authoritative_evaluator"}),
        required_payload=("module",),
        requires_evidence=True,
    ),
    EventAction(
        name="promotion_audited",
        description="A clean standalone audit accepted a prospective promotion.",
        subject_types=_OBJECT,
        actors=frozenset({"authoritative_evaluator"}),
        required_payload=("outcome",),
        requires_evidence=True,
    ),
    EventAction(
        name="promotion_recorded",
        description="The audited frontier became the active promotion.",
        subject_types=_OBJECT,
        actors=frozenset({"authoritative_evaluator"}),
        required_payload=("manifest_sha256", "catalog_sha256"),
        requires_evidence=True,
    ),
    # -- computation --------------------------------------------------------
    EventAction(
        name="computation_started",
        description="A reproducible computation began.",
        subject_types=_OBJECT,
        actors=frozenset({"computation_checker", "research_agent"}),
        required_payload=("method",),
        optional_payload=("parameters",),
    ),
    EventAction(
        name="computation_completed",
        description="A computation produced a result.  Evidence, not a theorem.",
        subject_types=_OBJECT,
        actors=frozenset({"computation_checker", "research_agent"}),
        required_payload=("outcome",),
        requires_evidence=True,
    ),
    EventAction(
        name="computation_certificate_verified",
        description="An independent checker validated a certificate.",
        subject_types=_OBJECT,
        actors=frozenset({"computation_checker"}),
        required_payload=("checker", "checker_version", "outcome"),
        requires_evidence=True,
    ),
    # -- sources ------------------------------------------------------------
    EventAction(
        name="source_acquired",
        description="Exact source bytes were retained and hashed.",
        subject_types=_OBJECT,
        actors=frozenset({"human_researcher", "importer", "ledger_service"}),
        required_payload=("sha256", "media_type"),
        requires_evidence=True,
    ),
    EventAction(
        name="source_acquisition_failed",
        description=(
            "The source could not be obtained.  A secondary summary is never "
            "substituted silently."
        ),
        subject_types=_OBJECT,
        actors=frozenset({"human_researcher", "importer", "ledger_service"}),
        required_payload=("reason", "acquisition_request"),
    ),
    EventAction(
        name="source_extraction_recorded",
        description="Text or structure was extracted from retained bytes.",
        subject_types=_OBJECT,
        actors=frozenset({"human_researcher", "importer", "ledger_service"}),
        required_payload=("tool", "tool_version"),
        requires_evidence=True,
    ),
    EventAction(
        name="source_marker_validated",
        description="A quoted marker was found at the recorded locator.",
        subject_types=_OBJECT,
        actors=frozenset({"human_researcher", "importer", "ledger_service"}),
        required_payload=("marker", "locator", "found"),
    ),
    EventAction(
        name="human_review_recorded",
        description=(
            "A human reviewed the normalized statement or source-to-formal "
            "mapping.  OCR and parsing never substitute for this."
        ),
        subject_types=_OBJECT_OR_CONNECTION,
        actors=frozenset({"human_researcher"}),
        required_payload=("outcome",),
        optional_payload=("caveats",),
    ),
    # -- operational scopes -------------------------------------------------
    EventAction(
        name="campaign_started",
        description="A campaign scope opened.",
        subject_types=_OBJECT,
        actors=frozenset({"human_researcher", "ledger_service"}),
        required_payload=("configuration_sha256",),
    ),
    EventAction(
        name="campaign_completed",
        description="A campaign scope closed.",
        subject_types=_OBJECT,
        actors=frozenset({"human_researcher", "ledger_service"}),
        required_payload=("operational_state",),
    ),
    EventAction(
        name="epoch_started",
        description="An epoch opened, linked to its predecessor when one exists.",
        subject_types=_OBJECT,
        actors=frozenset({"human_researcher", "ledger_service"}),
        required_payload=("epoch_kind",),
        optional_payload=("predecessor_id",),
    ),
    EventAction(
        name="epoch_completed",
        description="An epoch closed.",
        subject_types=_OBJECT,
        actors=frozenset({"human_researcher", "ledger_service"}),
        required_payload=("operational_state",),
    ),
    EventAction(
        name="turn_started",
        description="A turn opened.",
        subject_types=_OBJECT,
        actors=frozenset({"ledger_service", "human_researcher"}),
        optional_payload=("actor_identity",),
    ),
    EventAction(
        name="turn_completed",
        description="A turn reached a terminal operational state.",
        subject_types=_OBJECT,
        actors=frozenset({"ledger_service", "human_researcher"}),
        required_payload=("operational_state",),
    ),
    EventAction(
        name="spotlight_selected",
        description=(
            "A spotlight target was chosen, with hunch, relevance path, and "
            "budget recorded up front."
        ),
        subject_types=_OBJECT,
        actors=frozenset({"human_researcher", "research_agent"}),
        required_payload=("goal_id", "proposition_sha256", "selector", "hunch"),
        optional_payload=("relevance_path", "turn_budget", "cost_budget"),
    ),
    EventAction(
        name="spotlight_concluded",
        description="A spotlight reached proved, refuted, or unresolved.",
        subject_types=_OBJECT,
        actors=frozenset({"human_researcher", "ledger_service"}),
        required_payload=("terminal_outcome", "reason"),
    ),
    # -- artifacts ----------------------------------------------------------
    EventAction(
        name="artifact_registered",
        description="Bytes were content-addressed and entered the graph.",
        subject_types=_OBJECT,
        actors=_ALL_ACTORS,
        required_payload=("sha256", "byte_size", "artifact_type"),
    ),
    EventAction(
        name="artifact_location_added",
        description=(
            "A replaceable location now holds these bytes.  Any actor that can "
            "register an artifact may say where it wrote it; saying where bytes "
            "are cannot make a claim true, and forcing a second actor to record "
            "it would only add a way to lose the location."
        ),
        subject_types=_OBJECT,
        actors=_ALL_ACTORS,
        required_payload=("location",),
    ),
    EventAction(
        name="artifact_location_verified",
        description="A location was re-read and its hash matched.",
        subject_types=_OBJECT,
        actors=frozenset({"ledger_service", "human_researcher"}),
        required_payload=("location", "verified"),
    ),
    EventAction(
        name="artifact_location_lost",
        description="A location no longer holds these bytes.",
        subject_types=_OBJECT,
        actors=frozenset({"ledger_service", "human_researcher"}),
        required_payload=("location", "reason"),
    ),
    EventAction(
        name="artifact_archived",
        description="Retention state changed; identity did not.",
        subject_types=_OBJECT,
        actors=frozenset({"ledger_service", "human_researcher"}),
        required_payload=("retention_state",),
    ),
    # -- lifecycle and recovery ---------------------------------------------
    EventAction(
        name="object_superseded",
        description="An object was replaced.  Replaced is not false.",
        subject_types=_OBJECT,
        actors=frozenset(
            {"human_researcher", "authoritative_evaluator", "research_agent"}
        ),
        required_payload=("superseded_by", "reason"),
    ),
    EventAction(
        name="object_retracted",
        description="An object should no longer drive the search.",
        subject_types=_OBJECT,
        actors=frozenset(
            {"human_researcher", "authoritative_evaluator", "ledger_service"}
        ),
        required_payload=("reason",),
    ),
    EventAction(
        name="recovery_recorded",
        description=(
            "Startup reconciliation resolved, resumed, or explained an "
            "incomplete record."
        ),
        subject_types=_OBJECT_OR_CONNECTION,
        actors=frozenset({"ledger_service"}),
        required_payload=("resolution",),
    ),
    EventAction(
        name="historical_import_recorded",
        description=(
            "A legacy record was imported, naming its source and whether the "
            "original time or ordering is exact, inferred, or unknown."
        ),
        subject_types=_OBJECT_OR_CONNECTION,
        actors=frozenset({"importer"}),
        required_payload=("import_source", "ordering_fidelity"),
        requires_evidence=True,
    ),
    EventAction(
        name="schema_migrated",
        description="The database moved to a new schema or vocabulary version.",
        subject_types=_LEDGER,
        actors=frozenset({"ledger_service"}),
        required_payload=("from_version", "to_version"),
    ),
    EventAction(
        name="head_signed",
        description="A periodic root was written over the event hash chain.",
        subject_types=_LEDGER,
        actors=frozenset({"ledger_service"}),
        required_payload=("event_id", "event_hash"),
    ),
)

EVENT_ACTIONS = MappingProxyType({item.name: item for item in _EVENT_ACTIONS})

#: Actions whose presence can move an object's derived truth state.  No other
#: action may, which is what keeps an agent assertion from creating a proof.
TRUTH_BEARING_ACTIONS = frozenset({"kernel_certified"})

#: Ordering fidelity an imported event may claim about its original position.
ORDERING_FIDELITY = frozenset({"exact", "inferred", "unknown"})

#: Retention states an artifact may hold.
RETENTION_STATES = frozenset({"retained", "archived", "external_only", "lost"})


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def require_object_kind(kind: object) -> ObjectKind:
    """Return the declared kind or reject an unknown one."""
    if not isinstance(kind, str) or kind not in OBJECT_KINDS:
        raise VocabularyError(f"unknown object kind: {kind!r}")
    return OBJECT_KINDS[kind]


def require_relation(relation: object) -> Relation:
    """Return the declared relation or reject an unknown one."""
    if not isinstance(relation, str) or relation not in RELATIONS:
        raise VocabularyError(f"unknown relation: {relation!r}")
    return RELATIONS[relation]


def require_event_action(action: object) -> EventAction:
    """Return the declared action or reject an unknown one."""
    if not isinstance(action, str) or action not in EVENT_ACTIONS:
        raise VocabularyError(f"unknown event action: {action!r}")
    return EVENT_ACTIONS[action]


def require_actor(actor: object) -> Actor:
    """Return the declared actor class or reject an unknown one."""
    if not isinstance(actor, str) or actor not in ACTORS:
        raise VocabularyError(f"unknown actor: {actor!r}")
    return ACTORS[actor]


def require_state(dimension: str, value: object) -> str:
    """Return a state value that belongs to the named dimension."""
    if dimension not in STATE_DIMENSIONS:
        raise VocabularyError(f"unknown state dimension: {dimension!r}")
    allowed = STATE_DIMENSIONS[dimension]
    if not isinstance(value, str) or value not in allowed:
        raise VocabularyError(f"unknown {dimension} state: {value!r}")
    return value


def authorize(actor: str, action: str) -> None:
    """Reject an event that its actor class is not permitted to emit."""
    require_actor(actor)
    declared = require_event_action(action)
    if actor not in declared.actors:
        raise VocabularyError(
            f"actor {actor!r} may not emit {action!r}; "
            f"permitted actors: {', '.join(sorted(declared.actors))}"
        )


def validate_connection(
    relation: str,
    from_kind: str,
    to_kind: str,
    properties: Mapping[str, object] | None = None,
) -> Relation:
    """Check endpoint kinds and required properties for one connection."""
    declared = require_relation(relation)
    require_object_kind(from_kind)
    require_object_kind(to_kind)
    if from_kind not in declared.from_kinds:
        raise VocabularyError(
            f"relation {relation!r} does not accept a {from_kind!r} source; "
            f"permitted: {', '.join(sorted(declared.from_kinds))}"
        )
    if to_kind not in declared.to_kinds:
        raise VocabularyError(
            f"relation {relation!r} does not accept a {to_kind!r} target; "
            f"permitted: {', '.join(sorted(declared.to_kinds))}"
        )
    supplied = dict(properties or {})
    missing = [name for name in declared.required_properties if name not in supplied]
    if missing:
        raise VocabularyError(
            f"relation {relation!r} requires properties: {', '.join(missing)}"
        )
    if "trust_level" in supplied:
        level = supplied["trust_level"]
        if level not in TRUST_LEVELS:
            raise VocabularyError(f"unknown trust level: {level!r}")
        if declared.status_effect == "refutes_target" and level != "kernel":
            raise VocabularyError(
                "a refutation edge requires kernel trust; "
                f"{level!r} evidence may only support"
            )
    if "formalization_relationship" in supplied:
        mapping = supplied["formalization_relationship"]
        if mapping not in FORMALIZATION_RELATIONSHIPS:
            raise VocabularyError(f"unknown formalization relationship: {mapping!r}")
    if "verification_level" in supplied:
        level = supplied["verification_level"]
        if level not in VERIFICATION_ORDER:
            raise VocabularyError(f"unknown verification level: {level!r}")
    return declared


def validate_event_payload(
    action: str,
    payload: Mapping[str, object] | None,
    *,
    evidence_object_id: str | None = None,
) -> EventAction:
    """Check one event's payload contract, including its evidence requirement."""
    declared = require_event_action(action)
    supplied = dict(payload or {})
    missing = [name for name in declared.required_payload if name not in supplied]
    if missing:
        raise VocabularyError(
            f"event {action!r} requires payload fields: {', '.join(missing)}"
        )
    allowed = set(declared.required_payload) | set(declared.optional_payload)
    unexpected = sorted(set(supplied) - allowed)
    if unexpected:
        raise VocabularyError(
            f"event {action!r} does not accept payload fields: "
            f"{', '.join(unexpected)}"
        )
    if declared.requires_evidence and not evidence_object_id:
        raise VocabularyError(f"event {action!r} requires an evidence artifact")
    return declared


def dominant_verification_level(levels: Iterable[str]) -> str:
    """Return the strongest completed level among those recorded."""
    best = "untested"
    for level in levels:
        if level not in VERIFICATION_ORDER:
            raise VocabularyError(f"unknown verification level: {level!r}")
        if VERIFICATION_ORDER[level] > VERIFICATION_ORDER[best]:
            best = level
    return best


# --------------------------------------------------------------------------
# Export
# --------------------------------------------------------------------------


def vocabulary_payload() -> dict[str, object]:
    """Return the whole vocabulary as deterministic, publishable JSON."""
    return {
        "format": VOCABULARY_FORMAT,
        "version": VOCABULARY_VERSION,
        "object_kinds": {
            name: {
                "description": kind.description,
                "required_properties": list(kind.required_properties),
                "content_formats": sorted(kind.content_formats),
            }
            for name, kind in sorted(OBJECT_KINDS.items())
        },
        "relations": {
            name: {
                "description": relation.description,
                "from_kinds": sorted(relation.from_kinds),
                "to_kinds": sorted(relation.to_kinds),
                "status_effect": relation.status_effect,
                "required_properties": list(relation.required_properties),
                "requires_assembly_source": relation.requires_assembly_source,
            }
            for name, relation in sorted(RELATIONS.items())
        },
        "event_actions": {
            name: {
                "description": action.description,
                "subject_types": sorted(action.subject_types),
                "actors": sorted(action.actors),
                "required_payload": list(action.required_payload),
                "optional_payload": list(action.optional_payload),
                "requires_evidence": action.requires_evidence,
            }
            for name, action in sorted(EVENT_ACTIONS.items())
        },
        "actors": {
            name: {"description": actor.description}
            for name, actor in sorted(ACTORS.items())
        },
        "states": {
            dimension: sorted(values)
            for dimension, values in sorted(STATE_DIMENSIONS.items())
        },
        "verification_order": dict(sorted(VERIFICATION_ORDER.items())),
        "trust_levels": sorted(TRUST_LEVELS),
        "formalization_relationships": sorted(FORMALIZATION_RELATIONSHIPS),
        "truth_bearing_actions": sorted(TRUTH_BEARING_ACTIONS),
        "ordering_fidelity": sorted(ORDERING_FIDELITY),
        "retention_states": sorted(RETENTION_STATES),
    }


def vocabulary_sha256() -> str:
    """Return the digest bound into every database that uses this vocabulary."""
    canonical = json.dumps(
        vocabulary_payload(), sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


__all__ = [
    "ACTORS",
    "CONTENT_ADDRESSED_KINDS",
    "CONTENT_FORMATS",
    "ENCODING_STATES",
    "EVENT_ACTIONS",
    "FORMALIZATION_RELATIONSHIPS",
    "LIFECYCLE_STATES",
    "MATHEMATICAL_RELATIONS",
    "OBJECT_KINDS",
    "OPERATIONAL_STATES",
    "ORDERING_FIDELITY",
    "PROVENANCE_RELATIONS",
    "RELATIONS",
    "RETENTION_STATES",
    "SCOPE_KINDS",
    "SOURCE_EVIDENCE_STATES",
    "STATE_DIMENSIONS",
    "SUBJECT_TYPES",
    "TRUST_LEVELS",
    "TRUTH_BEARING_ACTIONS",
    "TRUTH_STATES",
    "VERIFICATION_LEVELS",
    "VERIFICATION_ORDER",
    "VOCABULARY_FORMAT",
    "VOCABULARY_VERSION",
    "Actor",
    "EventAction",
    "ObjectKind",
    "Relation",
    "VocabularyError",
    "authorize",
    "dominant_verification_level",
    "require_actor",
    "require_event_action",
    "require_object_kind",
    "require_relation",
    "require_state",
    "validate_connection",
    "validate_event_payload",
    "vocabulary_payload",
    "vocabulary_sha256",
]
