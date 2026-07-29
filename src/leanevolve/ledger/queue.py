"""Durable, content-deduplicated scratch-check queue.

The queue is represented by ordinary check objects and lifecycle events, so a
worker crash cannot strand state in an unrelated broker.  Source bytes are in
the artifact store before ``check_submitted`` commits, and result bytes commit
before a successful response may be rendered to an agent.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from leanevolve.ledger.artifacts import ArtifactStore, store_and_register
from leanevolve.ledger.derive import state_of
from leanevolve.ledger.events import canonical_json
from leanevolve.ledger.store import Ledger, LedgerError

CHECKER_IDENTITY = "warm-lean-server-v2-direction-safe-isolated"


@dataclass(frozen=True)
class CheckJob:
    id: str
    checkpoint_key: str
    environment_identity: str
    input_sha256: str
    source_artifact_id: str
    request_class: str
    priority: int
    budget_seconds: float
    state: str
    created_event_id: int


@dataclass(frozen=True)
class CheckResult:
    job_id: str
    outcome: str
    exit_code: int
    result_artifact_id: str
    claim_ids: tuple[str, ...]
    event_range: tuple[int, int]


def _request_digest(
    source: bytes,
    checkpoint_key: str,
    environment_identity: str,
    request_class: str,
) -> str:
    payload = b"\0".join(
        [
            CHECKER_IDENTITY.encode(),
            source,
            checkpoint_key.encode(),
            environment_identity.encode(),
            request_class.encode(),
        ]
    )
    return hashlib.sha256(payload).hexdigest()


class DurableCheckQueue:
    """Ledger-backed queue with bounded pending work and stable job IDs."""

    def __init__(
        self,
        ledger: Ledger,
        artifact_store: ArtifactStore,
        *,
        max_pending: int = 64,
    ) -> None:
        if max_pending < 1:
            raise ValueError("max_pending must be positive")
        self.ledger = ledger
        self.artifact_store = artifact_store
        self.max_pending = max_pending

    def _scope(self, job_id: str) -> dict[str, str | None]:
        submitted = self.ledger.events(
            subject_id=job_id, action="check_submitted"
        )
        if not submitted:
            return {"campaign_id": None, "epoch_id": None, "turn_id": None}
        event = submitted[0]
        return {
            "campaign_id": event.campaign_id,
            "epoch_id": event.epoch_id,
            "turn_id": event.turn_id,
        }

    def _focus_goals(self, job_id: str) -> tuple[str, ...]:
        """Resolve canonical spotlight targets from the job's durable scope."""

        scope = self._scope(job_id)
        goals: list[str] = []
        for scope_id in (
            scope.get("turn_id"),
            scope.get("epoch_id"),
            scope.get("campaign_id"),
        ):
            if not scope_id:
                continue
            for edge in self.ledger.connections(
                from_id=scope_id, relation="targets"
            ):
                target = self.ledger.object(edge.to_id)
                if target is not None and target.kind == "formal_claim":
                    goals.append(target.id)
        return tuple(dict.fromkeys(goals))

    def _from_record(self, object_id: str) -> CheckJob:
        record = self.ledger.object(object_id)
        if record is None or record.kind != "check":
            raise LedgerError(f"unknown check: {object_id}")
        properties = record.properties
        return CheckJob(
            id=record.id,
            checkpoint_key=str(properties["checkpoint_key"]),
            environment_identity=str(properties["environment_identity"]),
            input_sha256=str(properties["input_sha256"]),
            source_artifact_id=str(properties["source_artifact_id"]),
            request_class=str(properties["request_class"]),
            priority=int(properties.get("priority", 0)),
            budget_seconds=float(properties.get("budget_seconds", 30.0)),
            state=state_of(self.ledger, record.id).operational or "queued",
            created_event_id=record.created_event_id,
        )

    def submit(
        self,
        source: bytes,
        *,
        checkpoint_key: str,
        environment_identity: str,
        request_class: str = "proof",
        priority: int = 0,
        budget_seconds: float = 30.0,
        campaign_id: str | None = None,
        epoch_id: str | None = None,
        turn_id: str | None = None,
    ) -> tuple[CheckJob, bool]:
        digest = _request_digest(
            source, checkpoint_key, environment_identity, request_class
        )
        job_id = f"check:{digest[:24]}"
        if self.ledger.object(job_id) is not None:
            if campaign_id or epoch_id or turn_id:
                reuse_scope = turn_id or epoch_id or campaign_id or "unscoped"
                with self.ledger.write(
                    "lean_scratch_gateway",
                    "tool:durable-check-queue-v1",
                    campaign_id=campaign_id,
                    epoch_id=epoch_id,
                    turn_id=turn_id,
                ) as session:
                    session.record(
                        "check_reused",
                        job_id,
                        {"original_submission_scope": self._scope(job_id)},
                        idempotency_key=f"check-reused:{job_id}:{reuse_scope}",
                    )
            return self._from_record(job_id), True
        if len(self.pending()) >= self.max_pending:
            raise LedgerError(
                f"check queue is full ({self.max_pending}); backpressure applied"
            )
        input_sha256 = hashlib.sha256(source).hexdigest()
        with self.ledger.write(
            "lean_scratch_gateway",
            "tool:durable-check-queue-v1",
            campaign_id=campaign_id,
            epoch_id=epoch_id,
            turn_id=turn_id,
        ) as session:
            stored = store_and_register(
                session,
                self.artifact_store,
                source,
                artifact_type="scratch_source",
                media_type="text/x-lean",
                canonical_name=f"Scratch source for {job_id}",
            )
            session.create_object(
                job_id,
                "check",
                f"Scratch check {digest[:12]}",
                content_format="json",
                content=canonical_json(
                    {
                        "input_sha256": input_sha256,
                        "checkpoint_key": checkpoint_key,
                    }
                ),
                properties={
                    "checker_identity": CHECKER_IDENTITY,
                    "checkpoint_key": checkpoint_key,
                    "request_class": request_class,
                    "environment_identity": environment_identity,
                    "input_sha256": input_sha256,
                    "source_artifact_id": stored.object_id,
                    "priority": priority,
                    "budget_seconds": budget_seconds,
                },
            )
            session.connect(stored.object_id, "produced_by", job_id)
            session.record(
                "check_submitted",
                job_id,
                {
                    "checkpoint_key": checkpoint_key,
                    "environment_identity": environment_identity,
                    "input_sha256": input_sha256,
                    "request_class": request_class,
                    "priority": priority,
                    "budget_seconds": budget_seconds,
                    "idempotency_key": digest,
                    "checker_identity": CHECKER_IDENTITY,
                },
                evidence_object_id=stored.object_id,
                idempotency_key=f"check-submitted:{digest}",
            )
        return self._from_record(job_id), False

    def pending(self, *, checkpoint_key: str | None = None) -> list[CheckJob]:
        jobs = []
        for record in self.ledger.objects(kind="check"):
            job = self._from_record(record.id)
            if job.state != "queued":
                continue
            if checkpoint_key is not None and job.checkpoint_key != checkpoint_key:
                continue
            jobs.append(job)
        return sorted(jobs, key=lambda job: (-job.priority, job.created_event_id))

    def start(self, job_id: str, *, worker_id: str, queue_delay: float) -> CheckJob:
        job = self._from_record(job_id)
        if job.state != "queued":
            raise LedgerError(f"check {job_id} is {job.state}, not queued")
        with self.ledger.write(
            "lean_scratch_gateway",
            "tool:warm-lean-worker-v1",
            **self._scope(job_id),
        ) as session:
            session.record(
                "check_started",
                job_id,
                {"worker_id": worker_id, "queue_delay_seconds": queue_delay},
            )
        return self._from_record(job_id)

    def complete(
        self,
        job_id: str,
        *,
        outcome: str,
        exit_code: int,
        diagnostics: str,
        declarations: list[dict[str, Any]],
        phase_timings: dict[str, float],
    ) -> CheckResult:
        job = self._from_record(job_id)
        if job.state != "running":
            raise LedgerError(f"check {job_id} is {job.state}, not running")
        before = self.ledger.event_count()
        result_payload = {
            "format": "leanevolve-scratch-result-v1",
            "job_id": job_id,
            "outcome": outcome,
            "exit_code": exit_code,
            "diagnostics": diagnostics,
            "declarations": declarations,
            "phase_timings": phase_timings,
        }
        claim_ids: list[str] = []
        focus_goals = self._focus_goals(job_id)
        with self.ledger.write(
            "lean_scratch_gateway",
            "tool:warm-lean-worker-v1",
            **self._scope(job_id),
        ) as session:
            stored = store_and_register(
                session,
                self.artifact_store,
                (canonical_json(result_payload) + "\n").encode(),
                artifact_type="scratch_result",
                media_type="application/json",
                canonical_name=f"Scratch result for {job_id}",
            )
            session.connect(stored.object_id, "produced_by", job_id)
            if outcome == "success":
                session.record(
                    "elaboration_succeeded",
                    job_id,
                    {
                        "declarations": [
                            str(item.get("declaration", ""))
                            for item in declarations
                        ]
                    },
                )
                by_declaration: dict[str, str] = {}
                for item in declarations:
                    declaration = str(item["declaration"])
                    proposition = str(item["proposition"])
                    proposition_hash = hashlib.sha256(proposition.encode()).hexdigest()
                    claim_id = f"claim:{proposition_hash[:12]}"
                    if self.ledger.object(claim_id) is None:
                        session.create_object(
                            claim_id,
                            "formal_claim",
                            declaration,
                            content_format="lean",
                            content=proposition,
                            properties={
                                "formal_system": "lean4",
                                "declaration": declaration,
                                "proposition_sha256": proposition_hash,
                                "environment_identity": job.environment_identity,
                            },
                        )
                    session.connect(claim_id, "produced_by", job_id)
                    for focus_goal in focus_goals:
                        if claim_id != focus_goal:
                            session.connect(claim_id, "advances", focus_goal)
                    session.record(
                        "scratch_kernel_checked",
                        claim_id,
                        {
                            "declaration": declaration,
                            "proposition_sha256": proposition_hash,
                            "checkpoint_key": job.checkpoint_key,
                            "direct_dependencies": item.get("direct_dependencies", []),
                        },
                        evidence_object_id=stored.object_id,
                        idempotency_key=(
                            f"scratch-checked:{job_id}:{proposition_hash}"
                        ),
                    )
                    claim_ids.append(claim_id)
                    by_declaration[declaration] = claim_id
                for item in declarations:
                    source_id = by_declaration.get(str(item["declaration"]))
                    for dependency in item.get("direct_dependencies", []):
                        target_id = by_declaration.get(str(dependency))
                        if source_id and target_id:
                            session.connect(source_id, "depends_on", target_id)
                session.record(
                    "check_completed",
                    job_id,
                    {
                        "outcome": outcome,
                        "exit_code": exit_code,
                        "phase_timings": phase_timings,
                        "diagnostics_sha256": hashlib.sha256(
                            diagnostics.encode()
                        ).hexdigest(),
                    },
                    evidence_object_id=stored.object_id,
                )
            else:
                session.record(
                    "elaboration_failed",
                    job_id,
                    {
                        "diagnostics_sha256": hashlib.sha256(
                            diagnostics.encode()
                        ).hexdigest()
                    },
                )
                session.record(
                    "check_failed",
                    job_id,
                    {
                        "exit_code": exit_code,
                        "phase_timings": phase_timings,
                        "diagnostics_sha256": hashlib.sha256(
                            diagnostics.encode()
                        ).hexdigest(),
                    },
                )
        after = self.ledger.event_count()
        return CheckResult(
            job_id=job_id,
            outcome=outcome,
            exit_code=exit_code,
            result_artifact_id=stored.object_id,
            claim_ids=tuple(claim_ids),
            event_range=(before + 1, after),
        )

    def cancel(self, job_id: str, *, reason: str) -> None:
        self._from_record(job_id)
        with self.ledger.write(
            "ledger_service", "tool:check-queue-v1", **self._scope(job_id)
        ) as session:
            session.record("check_cancelled", job_id, {"reason": reason})

    def time_out(self, job_id: str, *, budget_seconds: float) -> None:
        self._from_record(job_id)
        with self.ledger.write(
            "lean_scratch_gateway",
            "tool:warm-lean-worker-v1",
            **self._scope(job_id),
        ) as session:
            session.record(
                "check_timed_out", job_id, {"budget_seconds": budget_seconds}
            )

    def interrupt(self, job_id: str, *, detected_by: str) -> None:
        self._from_record(job_id)
        with self.ledger.write(
            "ledger_service",
            "tool:check-reconciliation-v1",
            **self._scope(job_id),
        ) as session:
            session.record(
                "check_interrupted", job_id, {"detected_by": detected_by}
            )

    def supersede(self, job_id: str, *, newer_job_id: str) -> None:
        self._from_record(job_id)
        self._from_record(newer_job_id)
        with self.ledger.write(
            "ledger_service", "tool:check-queue-v1", **self._scope(job_id)
        ) as session:
            session.record(
                "check_superseded", job_id, {"superseded_by": newer_job_id}
            )

    def invalidate(self, job_id: str, *, reason: str) -> None:
        """Withdraw a false check result and every claim edge it created.

        The original events and artifacts remain immutable audit history.  A
        future checker version receives a distinct content identity and may
        re-establish the same proposition through fresh evidence.
        """
        self._from_record(job_id)
        check_events = self.ledger.events(subject_id=job_id)
        corrected = [
            event
            for event in check_events
            if event.action in {"elaboration_succeeded", "check_completed"}
        ]
        claim_ids = [
            edge.from_id
            for edge in self.ledger.connections(
                relation="produced_by", to_id=job_id
            )
            if (record := self.ledger.object(edge.from_id)) is not None
            and record.kind == "formal_claim"
        ]
        with self.ledger.write(
            "ledger_service",
            "tool:scratch-check-invalidation-v1",
            **self._scope(job_id),
        ) as session:
            for event in corrected:
                session.record(
                    "correction_recorded",
                    job_id,
                    {"reason": reason, "corrects_event_id": event.id},
                    idempotency_key=f"invalidate-check-event:{event.id}",
                )
            session.record(
                "object_retracted",
                job_id,
                {"reason": reason},
                idempotency_key=f"invalidate-check:{job_id}",
            )
            for claim_id in claim_ids:
                session.retract(
                    claim_id,
                    "produced_by",
                    job_id,
                    reason=reason,
                )

            for claim_id in claim_ids:
                active_checks = []
                for edge in self.ledger.connections(
                    from_id=claim_id, relation="produced_by"
                ):
                    target = self.ledger.object(edge.to_id)
                    if target is None or target.kind != "check":
                        continue
                    if state_of(self.ledger, target.id).lifecycle == "active":
                        active_checks.append(target.id)
                if active_checks:
                    continue
                # A later authoritative promotion independently establishes
                # the declaration even if its earlier scratch receipt is
                # withdrawn.  Preserve mathematical relevance edges in that
                # case; only the bad check provenance is retracted.
                if self.ledger.connections(
                    from_id=claim_id, relation="included_in"
                ):
                    continue
                for relation in ("advances", "depends_on"):
                    for edge in list(
                        self.ledger.connections(
                            from_id=claim_id, relation=relation
                        )
                    ):
                        session.retract(
                            claim_id,
                            relation,
                            edge.to_id,
                            reason=reason,
                        )

    def reconcile(self, *, checkpoint_key: str | None = None) -> list[str]:
        """Mark running checks interrupted after worker startup."""
        interrupted: list[str] = []
        for record in self.ledger.objects(kind="check"):
            if checkpoint_key is not None and str(
                record.properties.get("checkpoint_key")
            ) != checkpoint_key:
                continue
            if state_of(self.ledger, record.id).operational != "running":
                continue
            with self.ledger.write(
                "ledger_service",
                "tool:check-reconciliation-v1",
                **self._scope(record.id),
            ) as session:
                session.record(
                    "check_interrupted",
                    record.id,
                    {"detected_by": "worker_startup"},
                )
                session.record(
                    "recovery_recorded",
                    record.id,
                    {"resolution": "safe_to_resubmit_from_source_artifact"},
                )
            interrupted.append(record.id)
        return interrupted

    def source_bytes(self, job_id: str) -> bytes:
        job = self._from_record(job_id)
        digest = job.source_artifact_id.removeprefix("artifact:sha256:")
        return self.artifact_store.read(digest)


__all__ = ["CheckJob", "CheckResult", "DurableCheckQueue"]
