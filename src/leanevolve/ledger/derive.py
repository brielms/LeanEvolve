"""Derived state: computed from events, never written.

Status is a projection.  There is no status column anywhere in the schema, so
no writer can set one, and every dimension is answered by replaying the events
that bear on it.  This is what keeps a timeout from reading as a refutation and
an agent's confidence from reading as a proof.

The dimensions are independent by construction.  ``state_of`` returns all of
them together precisely so that no caller is tempted to collapse them into one
word.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from leanevolve.ledger.events import Event
from leanevolve.ledger.store import Ledger
from leanevolve.ledger.vocabulary import dominant_verification_level

#: Events that record a completed rung of the verification ladder.
_VERIFICATION_EVENTS: Mapping[str, str] = {
    "scratch_kernel_checked": "scratch_checked",
    "axiom_policy_audited": "axiom_policy_audited",
    "authoritative_evaluation_recorded": "authoritatively_evaluated",
    "kernel_certified": "authoritatively_evaluated",
    "promotion_audited": "promotion_audited",
    "promotion_recorded": "promotion_audited",
    "elaboration_failed": "elaboration_failed",
}

#: Terminal operational outcomes, latest wins.
_OPERATIONAL_EVENTS: Mapping[str, str] = {
    "check_submitted": "queued",
    "check_started": "running",
    "check_completed": "completed",
    "check_failed": "failed",
    "check_timed_out": "timed_out",
    "check_interrupted": "interrupted",
    "check_cancelled": "interrupted",
    "check_superseded": "interrupted",
    "computation_started": "running",
    "computation_completed": "completed",
    "campaign_started": "running",
    "epoch_started": "running",
    "turn_started": "running",
}

_LIFECYCLE_EVENTS: Mapping[str, str] = {
    "object_superseded": "superseded",
    "object_retracted": "retracted",
    "artifact_archived": "archived",
}


@dataclass(frozen=True)
class DerivedState:
    """Every dimension of one object's current reading."""

    object_id: str
    truth: str = "open"
    verification: str = "untested"
    encoding: str = "not_encoded"
    lifecycle: str = "active"
    operational: str | None = None
    source_evidence: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        return {
            "truth": self.truth,
            "verification": self.verification,
            "encoding": self.encoding,
            "lifecycle": self.lifecycle,
            "operational": self.operational,
            "source_evidence": self.source_evidence,
        }


def _verification(events: Iterable[Event]) -> str:
    return dominant_verification_level(
        _VERIFICATION_EVENTS[event.action]
        for event in events
        if event.action in _VERIFICATION_EVENTS
    )


def _lifecycle(events: Iterable[Event]) -> str:
    state = "active"
    for event in events:
        if event.action in _LIFECYCLE_EVENTS:
            state = _LIFECYCLE_EVENTS[event.action]
    return state


def _operational(events: Iterable[Event]) -> str | None:
    state: str | None = None
    for event in events:
        if event.action in _OPERATIONAL_EVENTS:
            state = _OPERATIONAL_EVENTS[event.action]
        elif event.action in {"turn_completed", "epoch_completed",
                              "campaign_completed"}:
            recorded = event.payload.get("operational_state")
            if isinstance(recorded, str):
                state = recorded
    return state


def _is_proved(events: Iterable[Event]) -> bool:
    """A proposition is proved only while a kernel certification stands."""
    certified = False
    for event in events:
        if event.action == "kernel_certified":
            certified = True
        elif event.action in {"object_retracted"}:
            certified = False
    return certified


def truth_of(
    ledger: Ledger, object_id: str, *, until: int | None = None
) -> str:
    """Return ``open``, ``proved``, or ``refuted`` for one object.

    ``refuted`` is derived only from an active ``refutes`` edge whose source is
    itself proved and whose trust level is ``kernel``.  A witness, a
    computation, or an assertion cannot produce it.
    """
    events = ledger.events(subject_id=object_id, until=until)
    if _is_proved(events):
        return "proved"
    for edge in ledger.connections(to_id=object_id, relation="refutes"):
        if edge.properties.get("trust_level") != "kernel":
            continue
        if until is not None and edge.created_event_id > until:
            continue
        if edge.retracted_event_id is not None and (
            until is None or edge.retracted_event_id <= until
        ):
            continue
        source_events = ledger.events(subject_id=edge.from_id, until=until)
        if _is_proved(source_events):
            return "refuted"
    return "open"


def encoding_of(
    ledger: Ledger, object_id: str, *, until: int | None = None
) -> str:
    """Return how completely a source or research claim is formalized.

    Encoding is independent of truth: ``fully_encoded`` says the mapping covers
    the normalized claim, not that the claim holds.
    """
    edges = [
        edge
        for edge in ledger.connections(from_id=object_id, relation="formalized_as")
        if until is None or edge.created_event_id <= until
    ]
    if not edges:
        return "not_encoded"
    relationships = {
        edge.properties.get("formalization_relationship") for edge in edges
    }
    if relationships & {"literal_encoding", "equivalence"}:
        return "fully_encoded"
    if relationships & {"specialization", "strengthening", "weakening",
                        "consequence"}:
        return "partially_formalized"
    return "proposition_only"


def state_of(
    ledger: Ledger, object_id: str, *, until: int | None = None
) -> DerivedState:
    """Return every dimension for one object, computed from its events.

    ``until`` replays history to a past event, so the view as it stood then is
    reproducible alongside the current one.
    """
    record = ledger.object(object_id)
    events = ledger.events(subject_id=object_id, until=until)
    source_evidence: str | None = None
    if record is not None:
        recorded = record.properties.get("source_evidence_state")
        if isinstance(recorded, str):
            source_evidence = recorded
    return DerivedState(
        object_id=object_id,
        truth=truth_of(ledger, object_id, until=until),
        verification=_verification(events),
        encoding=encoding_of(ledger, object_id, until=until),
        lifecycle=_lifecycle(events),
        operational=_operational(events),
        source_evidence=source_evidence,
    )


__all__ = ["DerivedState", "encoding_of", "state_of", "truth_of"]
