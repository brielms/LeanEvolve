"""Content-addressed artifact storage.

Large bytes do not belong in SQLite.  They are written to a local store named
by digest, verified, and only then referenced from the graph; replication to
slower or removable storage happens afterwards and never changes identity.

The invariant that makes this safe: bytes are hashed *before* they are trusted
and re-hashed whenever a location is verified.  A path is a hint about where
some bytes were last seen, never a claim about what they are.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from leanevolve.ledger.store import (
    ARTIFACT_ID_PREFIX,
    Ledger,
    LedgerError,
    ObjectRecord,
    WriteSession,
)

#: Read size for hashing large certificates without loading them into memory.
_CHUNK = 1024 * 1024

#: Artifact types that must survive the loss of any single location.  These are
#: the records a proof claim depends on; a rollout or a diagnostic is not.
REPLICATED_TYPES = frozenset(
    {
        "kernel_receipt",
        "axiom_receipt",
        "promotion_manifest",
        "computation_certificate",
        "candidate_source",
        "scratch_source",
    }
)

MINIMUM_COPIES = 2


class ArtifactError(RuntimeError):
    """Raised when bytes do not match the digest they are stored under."""


def artifact_id(sha256: str) -> str:
    """Return the object ID for content with this digest."""
    return f"{ARTIFACT_ID_PREFIX}{sha256}"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    """Hash a file without reading it entirely into memory."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class StoredArtifact:
    """Bytes that are now in the local store, with their verified digest."""

    object_id: str
    sha256: str
    byte_size: int
    path: Path


class ArtifactStore:
    """A local content-addressed directory: ``<root>/<aa>/<sha256>``.

    Sharding by the first byte of the digest keeps directory sizes reasonable
    without inventing a second naming scheme to get wrong.
    """

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)

    @property
    def root(self) -> Path:
        return self._root

    def path_for(self, sha256: str) -> Path:
        return self._root / sha256[:2] / sha256

    def contains(self, sha256: str) -> bool:
        return self.path_for(sha256).is_file()

    def put_bytes(self, data: bytes) -> StoredArtifact:
        """Write bytes locally, verify the digest, then return the identity."""
        digest = sha256_bytes(data)
        destination = self.path_for(digest)
        if not destination.is_file():
            self._write_atomic(destination, data)
        stored = sha256_file(destination)
        if stored != digest:
            raise ArtifactError(
                f"stored bytes hash to {stored}, expected {digest}"
            )
        return StoredArtifact(
            object_id=artifact_id(digest),
            sha256=digest,
            byte_size=len(data),
            path=destination,
        )

    def put_file(self, source: Path) -> StoredArtifact:
        """Copy a file into the store under its digest."""
        source = Path(source)
        if not source.is_file():
            raise ArtifactError(f"not a regular file: {source}")
        digest = sha256_file(source)
        destination = self.path_for(digest)
        if not destination.is_file():
            destination.parent.mkdir(parents=True, exist_ok=True)
            handle, temporary = tempfile.mkstemp(dir=destination.parent)
            os.close(handle)
            temporary_path = Path(temporary)
            try:
                shutil.copyfile(source, temporary_path)
                if sha256_file(temporary_path) != digest:
                    raise ArtifactError(
                        f"{source} changed while it was being copied"
                    )
                os.replace(temporary_path, destination)
            finally:
                temporary_path.unlink(missing_ok=True)
        return StoredArtifact(
            object_id=artifact_id(digest),
            sha256=digest,
            byte_size=destination.stat().st_size,
            path=destination,
        )

    def read(self, sha256: str) -> bytes:
        """Return the stored bytes, refusing anything that no longer matches."""
        path = self.path_for(sha256)
        if not path.is_file():
            raise ArtifactError(f"no local copy of {sha256}")
        data = path.read_bytes()
        if sha256_bytes(data) != sha256:
            raise ArtifactError(f"local copy of {sha256} is corrupt")
        return data

    def verify(self, sha256: str) -> bool:
        """Return whether the local copy still hashes to its name."""
        path = self.path_for(sha256)
        if not path.is_file():
            return False
        return sha256_file(path) == sha256

    def _write_atomic(self, destination: Path, data: bytes) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(dir=destination.parent)
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise


def store_and_register(
    session: WriteSession,
    store: ArtifactStore,
    data: bytes,
    *,
    artifact_type: str,
    media_type: str,
    canonical_name: str | None = None,
    extra_locations: Iterable[str] = (),
) -> StoredArtifact:
    """Persist bytes locally, then enter them into the graph with a location.

    Local first, verified, then referenced.  A caller that crashes between the
    write and the commit leaves an unreferenced blob, which is recoverable; the
    reverse order would leave a reference to bytes that may not exist.
    """
    stored = store.put_bytes(data)
    existing = session.ledger.object(stored.object_id)
    if existing is None:
        session.register_artifact(
            stored.sha256,
            artifact_type=artifact_type,
            byte_size=stored.byte_size,
            media_type=media_type,
            canonical_name=canonical_name,
        )
    elif (
        existing.kind != "artifact"
        or existing.properties.get("sha256") != stored.sha256
        or existing.properties.get("byte_size") != stored.byte_size
    ):
        raise ArtifactError(
            f"content-addressed object conflict for {stored.object_id}"
        )
    session.add_location(stored.object_id, str(stored.path))
    for location in extra_locations:
        session.add_location(stored.object_id, location)
    return stored


def under_replicated(ledger: Ledger) -> list[ObjectRecord]:
    """Return artifacts that must survive a lost location but currently cannot."""
    at_risk: list[ObjectRecord] = []
    for record in ledger.objects(kind="artifact"):
        artifact_type = record.properties.get("artifact_type")
        if artifact_type not in REPLICATED_TYPES:
            continue
        present = ledger.locations(record.id, present_only=True)
        if len(present) < MINIMUM_COPIES:
            at_risk.append(record)
    return at_risk


def reverify(
    session: WriteSession, store: ArtifactStore, object_id: str
) -> list[str]:
    """Re-hash every known location of one artifact and record the outcome.

    Returns the locations that no longer hold the expected bytes.
    """
    record = session.ledger.object(object_id)
    if record is None:
        raise LedgerError(f"unknown object: {object_id!r}")
    digest = str(record.properties["sha256"])
    failures: list[str] = []
    for location in session.ledger.locations(object_id):
        path = Path(location.location)
        ok = path.is_file() and sha256_file(path) == digest
        if not ok and store.path_for(digest) == path:
            ok = store.verify(digest)
        session.verify_location(object_id, location.location, verified=ok)
        if not ok:
            failures.append(location.location)
    return failures


__all__ = [
    "MINIMUM_COPIES",
    "REPLICATED_TYPES",
    "ArtifactError",
    "ArtifactStore",
    "StoredArtifact",
    "artifact_id",
    "reverify",
    "sha256_bytes",
    "sha256_file",
    "store_and_register",
    "under_replicated",
]
