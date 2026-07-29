"""Thin client used by the agent-facing Lean command."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import struct
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from leanevolve.ledger.artifacts import ArtifactStore
from leanevolve.ledger.derive import state_of
from leanevolve.ledger.queue import DurableCheckQueue
from leanevolve.ledger.store import Ledger


@dataclass(frozen=True)
class DurableLeanResult:
    returncode: int
    output: str
    elapsed_seconds: float
    job_id: str
    claim_ids: tuple[str, ...]
    event_range: tuple[int, int]
    deduplicated: bool


class LedgerLeanClient:
    def __init__(
        self,
        *,
        database: Path,
        artifacts: Path,
        lean: Path,
        snapshot_root: Path,
        checkpoint_key: str,
        environment_identity: str,
        lean_path: str | None = None,
        worker_socket: Path | None = None,
    ) -> None:
        self.database = database
        self.artifacts = artifacts
        self.lean = lean
        self.snapshot_root = snapshot_root
        self.checkpoint_key = checkpoint_key
        self.environment_identity = environment_identity
        self.lean_path = lean_path
        self.worker_socket = worker_socket

    def start_worker(self) -> None:
        environment = {
            "HOME": str(Path.home()),
            "PATH": os.environ.get("PATH", os.defpath),
        }
        if self.lean_path:
            environment["LEAN_PATH"] = self.lean_path
        log_directory = self.database.parent / "workers"
        log_directory.mkdir(parents=True, exist_ok=True)
        log_name = hashlib.sha256(self.checkpoint_key.encode()).hexdigest()[:16]
        log_handle = (log_directory / f"{log_name}.log").open("ab", buffering=0)
        subprocess.Popen(
            [
                sys.executable,
                "-m",
                "leanevolve.ledger.worker_service",
                "--database",
                str(self.database),
                "--artifacts",
                str(self.artifacts),
                "--checkpoint-key",
                self.checkpoint_key,
                "--environment-identity",
                self.environment_identity,
                "--lean",
                str(self.lean),
                "--snapshot-root",
                str(self.snapshot_root),
                "--idle-seconds",
                "7200",
            ],
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            env=environment,
            close_fds=True,
            start_new_session=True,
        )
        log_handle.close()

    def check(
        self,
        source: str,
        *,
        timeout_seconds: float,
        request_class: str = "proof",
        priority: int = 0,
        campaign_id: str | None = None,
        epoch_id: str | None = None,
        turn_id: str | None = None,
    ) -> DurableLeanResult:
        started = time.monotonic()
        if self.worker_socket is not None:
            request = {
                "source_base64": base64.b64encode(source.encode()).decode(),
                "timeout_seconds": timeout_seconds,
                "request_class": request_class,
                "priority": priority,
                "campaign_id": campaign_id,
                "epoch_id": epoch_id,
                "turn_id": turn_id,
            }
            try:
                return self._request_worker(request, started=started)
            except (FileNotFoundError, ConnectionRefusedError):
                # A detached worker can be killed or crash after the campaign
                # exported its stable socket path.  The path is discovery data,
                # not proof that a live listener still owns it.  Respawn under
                # the checkpoint lock and retry only failures that happened
                # before a request could be delivered.
                self.start_worker()
                deadline = time.monotonic() + 30.0
                while True:
                    try:
                        return self._request_worker(request, started=started)
                    except (FileNotFoundError, ConnectionRefusedError):
                        if time.monotonic() >= deadline:
                            raise RuntimeError(
                                "warm ledger worker did not recover"
                            )
                        time.sleep(0.05)
        with Ledger.open(self.database) as ledger:
            durable_queue = DurableCheckQueue(ledger, ArtifactStore(self.artifacts))
            before = ledger.event_count()
            job, deduplicated = durable_queue.submit(
                source.encode(),
                checkpoint_key=self.checkpoint_key,
                environment_identity=self.environment_identity,
                request_class=request_class,
                priority=priority,
                budget_seconds=timeout_seconds,
                campaign_id=campaign_id,
                epoch_id=epoch_id,
                turn_id=turn_id,
            )
            current = state_of(ledger, job.id).operational
            if current not in {"completed", "failed", "timed_out"}:
                self.start_worker()
            deadline = time.monotonic() + timeout_seconds + 30.0
            while current not in {"completed", "failed", "timed_out"}:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"durable check {job.id} did not finish")
                time.sleep(0.1)
                current = state_of(ledger, job.id).operational
            events = ledger.events(subject_id=job.id)
            terminal = next(
                event
                for event in reversed(events)
                if event.action
                in {"check_completed", "check_failed", "check_timed_out"}
            )
            result_artifact = terminal.evidence_object_id
            if result_artifact is None:
                result_edges = []
                for edge in ledger.connections(
                    to_id=job.id, relation="produced_by"
                ):
                    source = ledger.object(edge.from_id)
                    if source is not None and source.properties.get(
                        "artifact_type"
                    ) == "scratch_result":
                        result_edges.append(edge)
                result_artifact = result_edges[-1].from_id if result_edges else None
            payload: dict[str, object] = {}
            if result_artifact:
                digest = result_artifact.removeprefix("artifact:sha256:")
                payload = json.loads(ArtifactStore(self.artifacts).read(digest))
            claims = tuple(
                edge.from_id
                for edge in ledger.connections(to_id=job.id, relation="produced_by")
                if (ledger.object(edge.from_id) is not None)
                and ledger.object(edge.from_id).kind == "formal_claim"  # type: ignore[union-attr]
            )
            after = ledger.event_count()
            return DurableLeanResult(
                returncode=int(
                    payload.get(
                        "exit_code", terminal.payload.get("exit_code", 1)
                    )
                ),
                output=str(payload.get("diagnostics", "")),
                elapsed_seconds=time.monotonic() - started,
                job_id=job.id,
                claim_ids=claims,
                event_range=(before + 1, after) if after > before else (0, 0),
                deduplicated=deduplicated,
            )

    def _request_worker(
        self, request: dict[str, object], *, started: float
    ) -> DurableLeanResult:
        assert self.worker_socket is not None
        data = json.dumps(request, separators=(",", ":")).encode()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.connect(str(self.worker_socket))
            connection.sendall(struct.pack("!Q", len(data)) + data)
            header = bytearray()
            while len(header) < 8:
                chunk = connection.recv(8 - len(header))
                if not chunk:
                    raise RuntimeError("worker closed before response header")
                header.extend(chunk)
            length = struct.unpack("!Q", header)[0]
            body = bytearray()
            while len(body) < length:
                chunk = connection.recv(min(1024 * 1024, length - len(body)))
                if not chunk:
                    raise RuntimeError("worker closed before response body")
                body.extend(chunk)
        response = json.loads(body)
        if "error" in response:
            raise RuntimeError(str(response["error"]))
        return DurableLeanResult(
            returncode=int(response["returncode"]),
            output=str(response["output"]),
            elapsed_seconds=time.monotonic() - started,
            job_id=str(response["job_id"]),
            claim_ids=tuple(str(item) for item in response["claim_ids"]),
            event_range=tuple(int(item) for item in response["event_range"]),
            deduplicated=bool(response["deduplicated"]),
        )


__all__ = ["DurableLeanResult", "LedgerLeanClient"]
