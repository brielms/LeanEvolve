"""Crash-independent warm Lean worker service."""

from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import json
import os
import signal
import socket
import struct
import sys
import threading
import time
from pathlib import Path

from leanevolve.ledger.artifacts import ArtifactStore
from leanevolve.ledger.derive import state_of
from leanevolve.ledger.queue import DurableCheckQueue
from leanevolve.ledger.store import Ledger
from leanevolve.ledger.worker import LeanServerBackend, WarmWorker


def _lock_path(database: Path, checkpoint_key: str) -> Path:
    digest = hashlib.sha256(checkpoint_key.encode()).hexdigest()[:16]
    return database.parent / "workers" / f"{digest}.lock"


def socket_path(database: Path, checkpoint_key: str) -> Path:
    digest = hashlib.sha256(
        f"{database.resolve()}\0{checkpoint_key}".encode()
    ).hexdigest()[:20]
    # Detached workers deliberately receive a minimal environment, while the
    # parent process may inherit macOS's per-user TMPDIR.  Use the stable local
    # temporary root so both processes always derive the same socket path.
    return Path("/tmp") / "leanevolve-ledger-workers" / (
        f"{digest}.sock"
    )


def _receive(connection: socket.socket) -> dict[str, object]:
    header = bytearray()
    while len(header) < 8:
        chunk = connection.recv(8 - len(header))
        if not chunk:
            raise EOFError("client closed before request header")
        header.extend(chunk)
    length = struct.unpack("!Q", header)[0]
    if length > 256 * 1024 * 1024:
        raise ValueError("worker request exceeds 256 MiB")
    body = bytearray()
    while len(body) < length:
        chunk = connection.recv(min(1024 * 1024, length - len(body)))
        if not chunk:
            raise EOFError("client closed before request body")
        body.extend(chunk)
    payload = json.loads(body)
    if not isinstance(payload, dict):
        raise ValueError("worker request must be an object")
    return payload


def _send(connection: socket.socket, payload: dict[str, object]) -> None:
    data = json.dumps(payload, separators=(",", ":")).encode()
    connection.sendall(struct.pack("!Q", len(data)) + data)


def _serve_request(
    connection: socket.socket,
    *,
    database: Path,
    artifacts: Path,
    checkpoint_key: str,
    environment_identity: str,
) -> None:
    try:
        request = _receive(connection)
        source_value = request.get("source_base64")
        if not isinstance(source_value, str):
            raise ValueError("request lacks source_base64")
        source = base64.b64decode(source_value, validate=True)
        with Ledger.open(database) as ledger:
            durable_queue = DurableCheckQueue(ledger, ArtifactStore(artifacts))
            before = ledger.event_count()
            job, deduplicated = durable_queue.submit(
                source,
                checkpoint_key=checkpoint_key,
                environment_identity=environment_identity,
                request_class=str(request.get("request_class", "proof")),
                priority=int(request.get("priority", 0)),
                budget_seconds=float(request.get("timeout_seconds", 60.0)),
                campaign_id=(
                    str(request["campaign_id"])
                    if request.get("campaign_id") else None
                ),
                epoch_id=(
                    str(request["epoch_id"]) if request.get("epoch_id") else None
                ),
                turn_id=(
                    str(request["turn_id"]) if request.get("turn_id") else None
                ),
            )
            deadline = time.monotonic() + float(
                request.get("timeout_seconds", 60.0)
            ) + 30.0
            state = state_of(ledger, job.id).operational
            while state not in {"completed", "failed", "timed_out"}:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"durable check {job.id} did not finish")
                time.sleep(0.05)
                state = state_of(ledger, job.id).operational
            terminal = next(
                event
                for event in reversed(ledger.events(subject_id=job.id))
                if event.action
                in {"check_completed", "check_failed", "check_timed_out"}
            )
            result_artifact = terminal.evidence_object_id
            if result_artifact is None:
                for edge in reversed(
                    ledger.connections(to_id=job.id, relation="produced_by")
                ):
                    record = ledger.object(edge.from_id)
                    if record is not None and record.properties.get(
                        "artifact_type"
                    ) == "scratch_result":
                        result_artifact = record.id
                        break
            result_payload: dict[str, object] = {}
            if result_artifact:
                digest = result_artifact.removeprefix("artifact:sha256:")
                loaded = json.loads(ArtifactStore(artifacts).read(digest))
                if isinstance(loaded, dict):
                    result_payload = loaded
            claims = []
            for edge in ledger.connections(to_id=job.id, relation="produced_by"):
                record = ledger.object(edge.from_id)
                if record is not None and record.kind == "formal_claim":
                    claims.append(record.id)
            after = ledger.event_count()
            _send(
                connection,
                {
                    "returncode": int(
                        result_payload.get(
                            "exit_code", terminal.payload.get("exit_code", 1)
                        )
                    ),
                    "output": str(result_payload.get("diagnostics", "")),
                    "job_id": job.id,
                    "claim_ids": claims,
                    "event_range": (
                        [before + 1, after] if after > before else [0, 0]
                    ),
                    "deduplicated": deduplicated,
                },
            )
    except BaseException as error:
        _send(
            connection,
            {"error": f"{type(error).__name__}: {error}"},
        )
    finally:
        connection.close()


def _accept_loop(
    listener: socket.socket,
    stopping: threading.Event,
    **handler_arguments: object,
) -> None:
    listener.settimeout(0.2)
    while not stopping.is_set():
        try:
            connection, _ = listener.accept()
        except TimeoutError:
            continue
        threading.Thread(
            target=_serve_request,
            kwargs={"connection": connection, **handler_arguments},
            daemon=True,
        ).start()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--checkpoint-key", required=True)
    parser.add_argument("--environment-identity", required=True)
    parser.add_argument("--lean", type=Path, required=True)
    parser.add_argument("--snapshot-root", type=Path, required=True)
    parser.add_argument("--idle-seconds", type=float, default=600.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    lock_path = _lock_path(args.database, args.checkpoint_key)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_handle = lock_path.open("a+")
    try:
        fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return 0
    lock_handle.seek(0)
    lock_handle.truncate()
    lock_handle.write(str(os.getpid()))
    lock_handle.flush()
    stopping = False
    stopping_event = threading.Event()

    def stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True
        stopping_event.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    lean_path = os.environ.get("LEAN_PATH")
    service_socket_path = socket_path(args.database, args.checkpoint_key)
    service_socket_path.parent.mkdir(parents=True, exist_ok=True)
    service_socket_path.unlink(missing_ok=True)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(service_socket_path))
    listener.listen(16)
    acceptor = threading.Thread(
        target=_accept_loop,
        args=(listener, stopping_event),
        kwargs={
            "database": args.database,
            "artifacts": args.artifacts,
            "checkpoint_key": args.checkpoint_key,
            "environment_identity": args.environment_identity,
        },
        daemon=True,
    )
    acceptor.start()
    with Ledger.open(args.database) as ledger:
        durable_queue = DurableCheckQueue(ledger, ArtifactStore(args.artifacts))
        durable_queue.reconcile(checkpoint_key=args.checkpoint_key)
        backend = LeanServerBackend(
            args.lean,
            args.snapshot_root,
            lean_path=lean_path,
        )
        worker = WarmWorker(
            durable_queue,
            checkpoint_key=args.checkpoint_key,
            environment_identity=args.environment_identity,
            backend=backend,
            worker_id=f"lean-server:{os.getpid()}",
        )
        idle_since = time.monotonic()
        try:
            while not stopping:
                pending = durable_queue.pending(checkpoint_key=args.checkpoint_key)
                if not pending:
                    if time.monotonic() - idle_since >= args.idle_seconds:
                        break
                    time.sleep(0.1)
                    continue
                idle_since = time.monotonic()
                active_id = pending[0].id
                try:
                    worker.process_next()
                except TimeoutError:
                    continue
                except BaseException:
                    durable_queue.interrupt(
                        active_id, detected_by="warm_worker_exception"
                    )
                    raise
        finally:
            worker.close()
            stopping_event.set()
            listener.close()
            service_socket_path.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["main", "socket_path"]
