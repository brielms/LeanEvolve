from __future__ import annotations

from pathlib import Path

from leanevolve.ledger.client import DurableLeanResult, LedgerLeanClient


def test_socket_client_respawns_once_after_stale_worker() -> None:
    client = LedgerLeanClient(
        database=Path("ledger.sqlite3"),
        artifacts=Path("artifacts"),
        lean=Path("lean"),
        snapshot_root=Path("snapshot"),
        checkpoint_key="checkpoint:test",
        environment_identity="lean:test",
        worker_socket=Path("/tmp/stale-worker.sock"),
    )
    attempts = 0
    starts = 0

    def request_worker(
        _request: dict[str, object], *, started: float
    ) -> DurableLeanResult:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise FileNotFoundError("stale socket")
        return DurableLeanResult(0, "", 0.1, "check:1", (), (1, 2), False)

    def start_worker() -> None:
        nonlocal starts
        starts += 1

    client._request_worker = request_worker  # type: ignore[method-assign]
    client.start_worker = start_worker  # type: ignore[method-assign]

    result = client.check("theorem ok : True := by trivial", timeout_seconds=1)

    assert result.returncode == 0
    assert attempts == 2
    assert starts == 1
