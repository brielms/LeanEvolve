from __future__ import annotations

import queue
import shutil
from pathlib import Path

import pytest

from leanevolve.ledger.artifacts import ArtifactStore
from leanevolve.ledger.derive import state_of
from leanevolve.ledger.queue import DurableCheckQueue
from leanevolve.ledger.store import Ledger
from leanevolve.ledger.worker import (
    BackendResult,
    LeanServerBackend,
    WarmWorker,
    extract_declarations,
)


class FakeWarmBackend:
    environment_load_seconds = 2.5

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.closed = False

    def check(self, source: str, *, timeout_seconds: float) -> BackendResult:
        self.calls.append(source)
        return BackendResult(0, "", 0.2)

    def close(self) -> None:
        self.closed = True


def test_lean_server_waits_for_authoritative_diagnostics_barrier() -> None:
    backend = object.__new__(LeanServerBackend)
    backend._messages = queue.Queue()
    backend._request_id = 40
    sent: list[dict[str, object]] = []
    backend._send = sent.append
    uri = "file:///tmp/Candidate.lean"

    # The empty notification is the premature message that used to be treated
    # as success.  The real error arrives before Lean's completion response.
    backend._messages.put(
        {
            "method": "textDocument/publishDiagnostics",
            "params": {"uri": uri, "version": 1, "diagnostics": []},
        }
    )
    backend._messages.put(
        {
            "method": "textDocument/publishDiagnostics",
            "params": {
                "uri": uri,
                "version": 1,
                "isIncremental": True,
                "diagnostics": [{"severity": 1, "message": "unsolved goals"}],
            },
        }
    )
    backend._messages.put({"id": "ledger:41", "result": {}})

    diagnostics = backend._wait_for_diagnostics(uri, version=1, timeout=1.0)

    assert diagnostics == [{"severity": 1, "message": "unsolved goals"}]
    assert sent == [
        {
            "jsonrpc": "2.0",
            "id": "ledger:41",
            "method": "textDocument/waitForDiagnostics",
            "params": {"uri": uri, "version": 1},
        }
    ]


def test_lean_server_does_not_confuse_reverse_direction_request_with_response(
) -> None:
    backend = object.__new__(LeanServerBackend)
    backend._messages = queue.Queue()
    backend._request_id = 40
    sent: list[dict[str, object]] = []
    backend._send = sent.append
    uri = "file:///tmp/Candidate.lean"

    # Lean issues this server-to-client request during elaboration.  The old
    # worker compared only the id and returned success before the real error.
    backend._messages.put(
        {
            "jsonrpc": "2.0",
            "id": "ledger:41",
            "method": "workspace/inlayHint/refresh",
        }
    )
    backend._messages.put(
        {
            "method": "textDocument/publishDiagnostics",
            "params": {
                "uri": uri,
                "version": 1,
                "diagnostics": [{"severity": 1, "message": "unsolved goals"}],
            },
        }
    )
    backend._messages.put({"id": "ledger:41", "result": {}})

    diagnostics = backend._wait_for_diagnostics(uri, version=1, timeout=1.0)

    assert diagnostics == [{"severity": 1, "message": "unsolved goals"}]
    assert sent[1] == {
        "jsonrpc": "2.0",
        "id": "ledger:41",
        "result": None,
    }


def test_real_lean_server_rejects_an_unsolved_theorem(tmp_path: Path) -> None:
    lean_path = shutil.which("lean")
    if lean_path is None:
        elan_lean = Path.home() / ".elan" / "bin" / "lean"
        lean_path = str(elan_lean) if elan_lean.is_file() else None
    if lean_path is None:
        pytest.skip("Lean is not installed")
    backend = LeanServerBackend(Path(lean_path), tmp_path)
    try:
        result = backend.check(
            "theorem unfinished : False := by\n",
            timeout_seconds=15.0,
        )
    finally:
        backend.close()

    assert result.returncode == 1
    assert "unsolved goals" in result.diagnostics


def test_real_lean_server_ignores_broken_snapshot_lake_workspace(
    tmp_path: Path,
) -> None:
    lean_path = shutil.which("lean")
    if lean_path is None:
        elan_lean = Path.home() / ".elan" / "bin" / "lean"
        lean_path = str(elan_lean) if elan_lean.is_file() else None
    if lean_path is None:
        pytest.skip("Lean is not installed")
    (tmp_path / "lakefile.toml").write_text(
        'name = "deliberately-broken"\n'
        '[[require]]\nname = "missing"\ngit = "./does-not-exist"\n'
    )
    backend = LeanServerBackend(Path(lean_path), tmp_path)
    try:
        result = backend.check(
            "theorem complete : True := by trivial\n",
            timeout_seconds=15.0,
        )
    finally:
        backend.close()

    assert result.returncode == 0
    assert result.diagnostics == ""


def test_lean_server_diagnostics_barrier_fails_closed_on_timeout() -> None:
    backend = object.__new__(LeanServerBackend)
    backend._messages = queue.Queue()
    backend._request_id = 0
    backend._send = lambda _payload: None

    with pytest.raises(TimeoutError, match="elaboration"):
        backend._wait_for_diagnostics(
            "file:///tmp/Candidate.lean", version=1, timeout=0.001
        )


def test_extraction_attributes_only_submitted_candidate_declarations() -> None:
    source = """\
-- BEGIN FROZEN SCRATCH MODULE Example.Generated.Checkpoint
theorem inherited : True := by trivial
-- END FROZEN SCRATCH MODULE Example.Generated.Checkpoint
-- BEGIN SCRATCH CANDIDATE
theorem introduced : True := by trivial
theorem dependent : True := by exact introduced
-- END SCRATCH CANDIDATE
"""

    assert extract_declarations(source) == [
        {
            "declaration": "introduced",
            "proposition": "True",
            "direct_dependencies": [],
        },
        {
            "declaration": "dependent",
            "proposition": "True",
            "direct_dependencies": ["introduced"],
        },
    ]


def test_extraction_rejects_ambiguous_candidate_markers() -> None:
    with pytest.raises(ValueError, match="markers"):
        extract_declarations(
            "-- BEGIN SCRATCH CANDIDATE\ntheorem x : True := by trivial"
        )


def test_extraction_preserves_complete_binder_telescope() -> None:
    source = """\
theorem narrowed
    {n r s : Nat} {G : Nat → Nat → Bool}
    (degreeLe : s ≤ r) :
    s ≤ r ∧ n = n := by
  exact ⟨degreeLe, rfl⟩
"""

    assert extract_declarations(source) == [
        {
            "declaration": "narrowed",
            "proposition": (
                "{n r s : Nat} {G : Nat → Nat → Bool} "
                "(degreeLe : s ≤ r) : s ≤ r ∧ n = n"
            ),
            "direct_dependencies": [],
        }
    ]


def test_two_checks_share_one_loaded_backend_and_report_phase_timings(
    tmp_path: Path,
) -> None:
    with Ledger.open(tmp_path / "ledger.sqlite3") as ledger:
        queue = DurableCheckQueue(ledger, ArtifactStore(tmp_path / "artifacts"))
        first, _ = queue.submit(
            b"theorem first : True := by trivial\n",
            checkpoint_key="checkpoint:a",
            environment_identity="lean:test",
            campaign_id="campaign:test",
            epoch_id="epoch:test",
            turn_id="turn:test",
        )
        second, _ = queue.submit(
            b"theorem second : True := by trivial\n",
            checkpoint_key="checkpoint:a",
            environment_identity="lean:test",
        )
        backend = FakeWarmBackend()
        worker = WarmWorker(
            queue,
            checkpoint_key="checkpoint:a",
            environment_identity="lean:test",
            backend=backend,
            worker_id="worker:a",
        )

        assert worker.process_next().job_id == first.id  # type: ignore[union-attr]
        assert worker.process_next().job_id == second.id  # type: ignore[union-attr]
        assert len(backend.calls) == 2
        completed = ledger.events(action="check_completed")
        started = ledger.events(action="check_started")[0]
        assert started.campaign_id == "campaign:test"
        assert started.epoch_id == "epoch:test"
        assert started.turn_id == "turn:test"
        assert completed[0].campaign_id == "campaign:test"
        assert completed[0].epoch_id == "epoch:test"
        assert completed[0].turn_id == "turn:test"
        assert completed[0].payload["phase_timings"]["environment_load"] == 2.5
        assert completed[1].payload["phase_timings"]["environment_load"] == 0.0
        assert {
            "queue_delay",
            "environment_load",
            "elaboration",
            "extraction",
            "ledger_persistence",
        } <= set(completed[1].payload["phase_timings"])


def test_priority_and_checkpoint_mismatch_fail_closed(tmp_path: Path) -> None:
    with Ledger.open(tmp_path / "ledger.sqlite3") as ledger:
        queue = DurableCheckQueue(ledger, ArtifactStore(tmp_path / "artifacts"))
        low, _ = queue.submit(
            b"#check Nat\n",
            checkpoint_key="checkpoint:a",
            environment_identity="lean:test",
            request_class="probe",
            priority=0,
        )
        high, _ = queue.submit(
            b"theorem high : True := by trivial\n",
            checkpoint_key="checkpoint:a",
            environment_identity="lean:test",
            priority=100,
        )
        assert queue.pending()[0].id == high.id
        worker = WarmWorker(
            queue,
            checkpoint_key="checkpoint:a",
            environment_identity="lean:wrong",
            backend=FakeWarmBackend(),
            worker_id="worker:a",
        )
        with pytest.raises(RuntimeError, match="mismatch"):
            worker.process_next()
        assert queue.pending()[1].id == low.id


def test_invalidating_false_check_retracts_current_claim_edges(
    tmp_path: Path,
) -> None:
    with Ledger.open(tmp_path / "ledger.sqlite3") as ledger:
        queue = DurableCheckQueue(ledger, ArtifactStore(tmp_path / "artifacts"))
        job, _ = queue.submit(
            b"theorem false_positive : True := by trivial\n",
            checkpoint_key="checkpoint:a",
            environment_identity="lean:test",
        )
        worker = WarmWorker(
            queue,
            checkpoint_key="checkpoint:a",
            environment_identity="lean:test",
            backend=FakeWarmBackend(),
            worker_id="worker:a",
        )
        result = worker.process_next()
        assert result is not None
        claim_id = result.claim_ids[0]
        assert ledger.connection(claim_id, "produced_by", job.id) is not None

        queue.invalidate(job.id, reason="regression exposed false success")

        assert state_of(ledger, job.id).lifecycle == "retracted"
        produced = ledger.connection(claim_id, "produced_by", job.id)
        assert produced is not None and not produced.is_active
        assert ledger.connections(from_id=claim_id, relation="advances") == []
        assert ledger.events(subject_id=job.id, action="correction_recorded")
