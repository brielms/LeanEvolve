from __future__ import annotations

from pathlib import Path

import pytest

from leanevolve.ledger.artifacts import ArtifactStore
from leanevolve.ledger.derive import state_of
from leanevolve.ledger.projections import recovery_queue
from leanevolve.ledger.queue import DurableCheckQueue
from leanevolve.ledger.store import Ledger, LedgerError


def _queue(tmp_path: Path, *, max_pending: int = 4) -> tuple[Ledger, DurableCheckQueue]:
    ledger = Ledger.open(tmp_path / "ledger.sqlite3")
    queue = DurableCheckQueue(
        ledger, ArtifactStore(tmp_path / "artifacts"), max_pending=max_pending
    )
    return ledger, queue


def test_source_commits_before_submission_and_identical_request_deduplicates(
    tmp_path: Path,
) -> None:
    ledger, queue = _queue(tmp_path)
    with ledger:
        first, deduplicated = queue.submit(
            b"theorem one : True := by trivial\n",
            checkpoint_key="checkpoint:a",
            environment_identity="lean:test",
            turn_id="turn:test",
        )
        count = ledger.event_count()
        second, second_deduplicated = queue.submit(
            b"theorem one : True := by trivial\n",
            checkpoint_key="checkpoint:a",
            environment_identity="lean:test",
            turn_id="turn:test",
        )

        assert deduplicated is False
        assert second_deduplicated is True
        assert first.id == second.id
        assert ledger.event_count() == count + 1
        reused = ledger.events(subject_id=first.id, action="check_reused")
        assert reused[0].turn_id == "turn:test"
        assert queue.source_bytes(first.id).startswith(b"theorem one")
        submitted = ledger.events(subject_id=first.id, action="check_submitted")
        assert submitted[0].evidence_object_id == first.source_artifact_id


def test_success_is_committed_with_claims_before_result_returns(tmp_path: Path) -> None:
    ledger, queue = _queue(tmp_path)
    with ledger:
        job, _ = queue.submit(
            b"theorem one : True := by trivial\n",
            checkpoint_key="checkpoint:a",
            environment_identity="lean:test",
        )
        queue.start(job.id, worker_id="worker:a", queue_delay=0.1)
        result = queue.complete(
            job.id,
            outcome="success",
            exit_code=0,
            diagnostics="",
            declarations=[
                {
                    "declaration": "one",
                    "proposition": "True",
                    "direct_dependencies": [],
                }
            ],
            phase_timings={
                "queue_delay": 0.1,
                "environment_load": 1.0,
                "elaboration": 0.2,
                "extraction": 0.1,
                "ledger_persistence": 0.1,
            },
        )

        assert state_of(ledger, job.id).operational == "completed"
        assert len(result.claim_ids) == 1
        assert state_of(ledger, result.claim_ids[0]).verification == "scratch_checked"
        assert state_of(ledger, result.claim_ids[0]).truth == "open"
        assert result.event_range[1] == ledger.head().id  # type: ignore[union-attr]


def test_reconciliation_and_backpressure_are_explicit(tmp_path: Path) -> None:
    ledger, queue = _queue(tmp_path, max_pending=1)
    with ledger:
        running, _ = queue.submit(
            b"#check Nat\n",
            checkpoint_key="checkpoint:a",
            environment_identity="lean:test",
            request_class="probe",
        )
        with pytest.raises(LedgerError, match="backpressure"):
            queue.submit(
                b"#check Int\n",
                checkpoint_key="checkpoint:a",
                environment_identity="lean:test",
                request_class="probe",
            )
        queue.start(running.id, worker_id="worker:a", queue_delay=0.0)
        assert queue.reconcile() == [running.id]
        assert state_of(ledger, running.id).operational == "interrupted"
        assert recovery_queue(ledger)["items"][0]["safe_action"] == "reconcile_check"
