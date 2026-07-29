"""Long-lived Lean language-server workers keyed by validated checkpoints.

This module keeps Lean itself alive.  It does not merely keep a Python wrapper
around repeated ``lean --stdin`` calls: one ``lean --server`` process owns a
checkpoint/environment key and elaborates sequential virtual documents.  The
durable queue remains the source of job truth, so the service can reconcile a
crash without consulting a model transcript.
"""

from __future__ import annotations

import json
import os
import queue as thread_queue
import re
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from leanevolve.ledger.queue import CheckResult, DurableCheckQueue


@dataclass(frozen=True)
class BackendResult:
    returncode: int
    diagnostics: str
    elapsed_seconds: float


class WarmBackend(Protocol):
    environment_load_seconds: float

    def check(self, source: str, *, timeout_seconds: float) -> BackendResult: ...

    def close(self) -> None: ...


class LeanServerBackend:
    """A genuine persistent Lean LSP process for one environment key."""

    def __init__(
        self,
        lean: Path,
        snapshot_root: Path,
        *,
        lean_path: str | None = None,
    ) -> None:
        self.lean = lean
        self.snapshot_root = snapshot_root.resolve()
        # Scratch inputs are complete flattened streams.  Do not let Lean's
        # server discover and execute Lake setup from the copied snapshot: a
        # frozen source tree is not necessarily a functional Git/Lake
        # workspace, and ambient project state must not affect validation.
        self._workspace = tempfile.TemporaryDirectory(
            prefix="leanevolve-lean-server-"
        )
        self._server_root = Path(self._workspace.name).resolve()
        started = time.monotonic()
        environment = {"HOME": str(Path.home()), "PATH": os.defpath}
        if lean_path:
            environment["LEAN_PATH"] = lean_path
        self._process = subprocess.Popen(
            [str(lean), "--server"],
            cwd=self._server_root,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            start_new_session=True,
        )
        self._messages: thread_queue.Queue[dict[str, Any] | BaseException] = (
            thread_queue.Queue()
        )
        self._request_id = 0
        self._document_id = 0
        self._reader = threading.Thread(target=self._read_messages, daemon=True)
        self._stderr = threading.Thread(target=self._drain_stderr, daemon=True)
        self._stderr_lines: list[str] = []
        self._reader.start()
        self._stderr.start()
        response = self._request(
            "initialize",
            {
                "processId": os.getpid(),
                "rootUri": self._server_root.as_uri(),
                "capabilities": {},
                "workspaceFolders": None,
            },
            timeout=30.0,
        )
        if "error" in response:
            self.close()
            raise RuntimeError(
                f"Lean server initialization failed: {response['error']}"
            )
        self._notify("initialized", {})
        self.environment_load_seconds = time.monotonic() - started

    @property
    def pid(self) -> int:
        return self._process.pid

    def _drain_stderr(self) -> None:
        assert self._process.stderr is not None
        for raw in self._process.stderr:
            self._stderr_lines.append(raw.decode(errors="replace").rstrip())
            if len(self._stderr_lines) > 200:
                del self._stderr_lines[:100]

    def _read_messages(self) -> None:
        assert self._process.stdout is not None
        stream = self._process.stdout
        try:
            while True:
                headers: dict[str, str] = {}
                while True:
                    line = stream.readline()
                    if not line:
                        return
                    if line in {b"\r\n", b"\n"}:
                        break
                    name, value = line.decode("ascii").split(":", 1)
                    headers[name.lower()] = value.strip()
                length = int(headers["content-length"])
                body = stream.read(length)
                if len(body) != length:
                    raise EOFError("Lean server closed mid-message")
                payload = json.loads(body)
                if isinstance(payload, dict):
                    self._messages.put(payload)
        except BaseException as error:
            self._messages.put(error)

    def _send(self, payload: dict[str, Any]) -> None:
        if self._process.poll() is not None:
            raise RuntimeError("Lean server is not running")
        data = json.dumps(payload, separators=(",", ":")).encode()
        frame = f"Content-Length: {len(data)}\r\n\r\n".encode() + data
        assert self._process.stdin is not None
        self._process.stdin.write(frame)
        self._process.stdin.flush()

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _request(
        self, method: str, params: dict[str, Any], *, timeout: float
    ) -> dict[str, Any]:
        self._request_id += 1
        # JSON-RPC request identifiers are scoped by direction, so Lean may
        # legitimately send a server-to-client request with the same numeric
        # identifier as one of our client-to-server requests.  A string prefix
        # makes accidental equality impossible in practice, while the explicit
        # response check below remains the actual protocol boundary.
        request_id = f"ledger:{self._request_id}"
        self._send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            }
        )
        deadline = time.monotonic() + timeout
        deferred: list[dict[str, Any]] = []
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                for message in deferred:
                    self._messages.put(message)
                raise TimeoutError(f"Lean server request {method} timed out")
            message = self._messages.get(timeout=remaining)
            if isinstance(message, BaseException):
                raise RuntimeError("Lean server reader failed") from message
            if self._is_server_request(message):
                self._answer_server_request(message)
                continue
            if "method" not in message and message.get("id") == request_id:
                for item in deferred:
                    self._messages.put(item)
                return message
            deferred.append(message)

    @staticmethod
    def _is_server_request(message: dict[str, Any]) -> bool:
        return "method" in message and "id" in message

    def _answer_server_request(self, message: dict[str, Any]) -> None:
        """Acknowledge optional client-capability and refresh requests.

        These messages travel in the opposite direction from our outstanding
        requests.  They are not responses, even when their JSON-RPC ids happen
        to be equal.
        """
        self._send(
            {
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": None,
            }
        )

    def check(self, source: str, *, timeout_seconds: float) -> BackendResult:
        started = time.monotonic()
        self._document_id += 1
        uri = (
            self._server_root / f".ledger-check-{self._document_id}.lean"
        ).as_uri()
        version = 1
        self._notify(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": uri,
                    "languageId": "lean4",
                    "version": version,
                    "text": source,
                }
            },
        )
        try:
            diagnostics = self._wait_for_diagnostics(
                uri, version=version, timeout=timeout_seconds
            )
        finally:
            self._notify("textDocument/didClose", {"textDocument": {"uri": uri}})
        errors = [item for item in diagnostics if int(item.get("severity", 1)) == 1]
        rendered = "\n".join(
            str(item.get("message", "")) for item in diagnostics
        )
        return BackendResult(
            returncode=1 if errors else 0,
            diagnostics=rendered,
            elapsed_seconds=time.monotonic() - started,
        )

    def _wait_for_diagnostics(
        self, uri: str, *, version: int, timeout: float
    ) -> list[dict[str, Any]]:
        """Wait for Lean's authoritative end-of-elaboration barrier.

        Lean diagnostics are incremental and an empty notification can arrive long
        before elaboration finishes.  ``textDocument/waitForDiagnostics`` responds
        only after the reporter and every command snapshot for the requested
        document version have completed.  This is the same synchronization request
        used by Lean's own language-server test runner.
        """

        self._request_id += 1
        request_id = f"ledger:{self._request_id}"
        self._send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "textDocument/waitForDiagnostics",
                "params": {"uri": uri, "version": version},
            }
        )
        deadline = time.monotonic() + timeout
        diagnostics: list[dict[str, Any]] = []
        saw_matching_diagnostics = False
        deferred: list[dict[str, Any]] = []
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Lean elaboration timed out")
                try:
                    message = self._messages.get(timeout=remaining)
                except thread_queue.Empty as error:
                    raise TimeoutError("Lean elaboration timed out") from error
                if isinstance(message, BaseException):
                    raise RuntimeError("Lean server reader failed") from message
                if self._is_server_request(message):
                    self._answer_server_request(message)
                    continue
                if "method" not in message and message.get("id") == request_id:
                    if "error" in message:
                        raise RuntimeError(
                            "Lean waitForDiagnostics failed: "
                            f"{message['error']}"
                        )
                    if not saw_matching_diagnostics:
                        raise RuntimeError(
                            "Lean waitForDiagnostics returned without a matching "
                            "diagnostics notification"
                        )
                    return diagnostics
                params = message.get("params", {})
                if (
                    message.get("method") == "textDocument/publishDiagnostics"
                    and params.get("uri") == uri
                    and params.get("version", version) == version
                ):
                    saw_matching_diagnostics = True
                    incoming = list(params.get("diagnostics", []))
                    if params.get("isIncremental", False):
                        diagnostics.extend(incoming)
                    else:
                        diagnostics = incoming
                else:
                    deferred.append(message)
        finally:
            for message in deferred:
                self._messages.put(message)

    def close(self) -> None:
        if self._process.poll() is not None:
            self._workspace.cleanup()
            return
        try:
            self._request("shutdown", {}, timeout=3.0)
            self._notify("exit", {})
            self._process.wait(timeout=3.0)
        except (OSError, RuntimeError, TimeoutError, subprocess.TimeoutExpired):
            self._process.kill()
            self._process.wait()
        finally:
            self._workspace.cleanup()


_DECLARATION = re.compile(
    r"(?m)^\s*(?:theorem|lemma)\s+([A-Za-z_][A-Za-z0-9_'.]*)\b"
)
_CANDIDATE_BEGIN = "-- BEGIN SCRATCH CANDIDATE"
_CANDIDATE_END = "-- END SCRATCH CANDIDATE"


def submitted_candidate_source(source: str) -> str:
    """Exclude frozen imports from an assembled scratch-check extraction.

    The Lean server must elaborate the complete flattened stream, but only the
    candidate section was submitted by this turn.  Treating declarations from
    frozen checkpoint modules as new scratch results both bloats the ledger and
    misattributes inherited mathematics to the current turn.
    """
    begin = source.find(_CANDIDATE_BEGIN)
    end = source.find(_CANDIDATE_END)
    if begin == -1 and end == -1:
        # Direct queue clients submit an unassembled stream.
        return source
    if begin == -1 or end == -1 or end <= begin:
        # Assembly is trusted service output. Fail closed rather than silently
        # assigning an ambiguous declaration set to a successful check.
        raise ValueError("malformed scratch candidate extraction markers")
    begin += len(_CANDIDATE_BEGIN)
    return source[begin:end]


_submitted_declaration_source = submitted_candidate_source


def extract_declarations(source: str) -> list[dict[str, Any]]:
    """Extract checked declarations introduced by the submitted candidate."""
    submitted = submitted_candidate_source(source)
    matches = list(_DECLARATION.finditer(submitted))
    names = [match.group(1) for match in matches]
    declarations: list[dict[str, Any]] = []
    for index, match in enumerate(matches):
        block_end = (
            matches[index + 1].start() if index + 1 < len(matches) else len(submitted)
        )
        block = submitted[match.start():block_end]
        signature = _declaration_signature(block, match.end() - match.start())
        dependencies = [
            name
            for name in names
            if name != match.group(1)
            and re.search(
                rf"(?<![A-Za-z0-9_']){re.escape(name)}(?![A-Za-z0-9_'])",
                block,
            )
        ]
        declarations.append(
            {
                "declaration": match.group(1),
                "proposition": " ".join(signature.split()),
                "direct_dependencies": dependencies,
            }
        )
    return declarations


def _declaration_signature(block: str, name_end: int) -> str:
    """Return the complete telescope and result type before the proof body.

    A regular expression cannot distinguish binder annotations such as
    ``(n : Nat)`` from the declaration's result separator.  Scan Lean's basic
    delimiters and comments so the first top-level colon and subsequent
    assignment are selected instead.
    """
    depth = 0
    block_comment_depth = 0
    line_comment = False
    in_string = False
    escaped = False
    separator: int | None = None
    assignment: int | None = None
    index = name_end
    while index < len(block):
        current = block[index]
        following = block[index + 1] if index + 1 < len(block) else ""
        if line_comment:
            if current == "\n":
                line_comment = False
            index += 1
            continue
        if block_comment_depth:
            if current == "/" and following == "-":
                block_comment_depth += 1
                index += 2
                continue
            if current == "-" and following == "/":
                block_comment_depth -= 1
                index += 2
                continue
            index += 1
            continue
        if in_string:
            if escaped:
                escaped = False
            elif current == "\\":
                escaped = True
            elif current == '"':
                in_string = False
            index += 1
            continue
        if current == "-" and following == "-":
            line_comment = True
            index += 2
            continue
        if current == "/" and following == "-":
            block_comment_depth = 1
            index += 2
            continue
        if current == '"':
            in_string = True
            index += 1
            continue
        if current in "([{":
            depth += 1
        elif current in ")]}" and depth:
            depth -= 1
        elif depth == 0 and current == ":" and following == "=":
            if separator is not None:
                assignment = index
                break
        elif depth == 0 and current == ":" and separator is None:
            separator = index
        index += 1
    if separator is None or assignment is None:
        raise ValueError("could not extract complete Lean declaration signature")
    telescope = block[name_end:separator].strip()
    result = block[separator + 1:assignment].strip()
    return f"{telescope} : {result}" if telescope else result


class WarmWorker:
    """Serialize one checkpoint key through one already-loaded Lean process."""

    def __init__(
        self,
        durable_queue: DurableCheckQueue,
        *,
        checkpoint_key: str,
        environment_identity: str,
        backend: WarmBackend,
        worker_id: str,
    ) -> None:
        self.queue = durable_queue
        self.checkpoint_key = checkpoint_key
        self.environment_identity = environment_identity
        self.backend = backend
        self.worker_id = worker_id
        self._used = False

    def process_next(self) -> CheckResult | None:
        pending = self.queue.pending(checkpoint_key=self.checkpoint_key)
        if not pending:
            return None
        job = pending[0]
        if job.environment_identity != self.environment_identity:
            raise RuntimeError(
                "checkpoint-key environment mismatch; warm process rejected"
            )
        started = time.monotonic()
        self.queue.start(job.id, worker_id=self.worker_id, queue_delay=0.0)
        source = self.queue.source_bytes(job.id).decode("utf-8")
        environment_load = 0.0 if self._used else self.backend.environment_load_seconds
        self._used = True
        try:
            result = self.backend.check(source, timeout_seconds=job.budget_seconds)
        except TimeoutError:
            self.queue.time_out(job.id, budget_seconds=job.budget_seconds)
            raise
        extraction_started = time.monotonic()
        declarations = extract_declarations(source) if result.returncode == 0 else []
        extraction = time.monotonic() - extraction_started
        timings = {
            "queue_delay": 0.0,
            "environment_load": environment_load,
            "elaboration": result.elapsed_seconds,
            "extraction": extraction,
            "ledger_persistence": 0.0,
            "total_worker": time.monotonic() - started,
        }
        return self.queue.complete(
            job.id,
            outcome="success" if result.returncode == 0 else "rejected",
            exit_code=result.returncode,
            diagnostics=result.diagnostics,
            declarations=declarations,
            phase_timings=timings,
        )

    def close(self) -> None:
        self.backend.close()


__all__ = [
    "BackendResult",
    "LeanServerBackend",
    "WarmBackend",
    "WarmWorker",
    "extract_declarations",
    "submitted_candidate_source",
]
