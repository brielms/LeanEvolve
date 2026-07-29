"""Artifacts are identified by digest; locations are replaceable facts."""

from __future__ import annotations

import pytest

from leanevolve.ledger.artifacts import (
    ArtifactError,
    ArtifactStore,
    artifact_id,
    reverify,
    sha256_bytes,
    store_and_register,
    under_replicated,
)
from leanevolve.ledger.store import Ledger, LedgerError

PAYLOAD = b'{"format": "leanevolve-candidate-kernel-receipt-v1"}'
DIGEST = sha256_bytes(PAYLOAD)


@pytest.fixture()
def ledger(tmp_path) -> Ledger:
    with Ledger.open(tmp_path / "artifacts.sqlite3") as store:
        yield store


@pytest.fixture()
def store(tmp_path) -> ArtifactStore:
    return ArtifactStore(tmp_path / "cas")


class TestContentAddressing:
    def test_bytes_are_stored_under_their_digest(self, store: ArtifactStore) -> None:
        stored = store.put_bytes(PAYLOAD)
        assert stored.sha256 == DIGEST
        assert stored.object_id == artifact_id(DIGEST)
        assert stored.path.name == DIGEST
        assert stored.path.read_bytes() == PAYLOAD

    def test_writing_the_same_bytes_twice_is_the_same_artifact(
        self, store: ArtifactStore
    ) -> None:
        first = store.put_bytes(PAYLOAD)
        second = store.put_bytes(PAYLOAD)
        assert first.path == second.path
        assert first.sha256 == second.sha256

    def test_a_file_is_copied_under_its_digest(self, store, tmp_path) -> None:
        source = tmp_path / "receipt.json"
        source.write_bytes(PAYLOAD)
        stored = store.put_file(source)
        assert stored.sha256 == DIGEST
        assert store.contains(DIGEST)

    def test_corruption_is_detected_on_read(self, store: ArtifactStore) -> None:
        stored = store.put_bytes(PAYLOAD)
        stored.path.write_bytes(b"tampered")
        assert not store.verify(DIGEST)
        with pytest.raises(ArtifactError, match="corrupt"):
            store.read(DIGEST)

    def test_missing_bytes_are_not_silently_empty(self, store: ArtifactStore) -> None:
        with pytest.raises(ArtifactError, match="no local copy"):
            store.read("0" * 64)


class TestRegistration:
    def test_registering_records_identity_and_a_location(
        self, ledger: Ledger, store: ArtifactStore
    ) -> None:
        with ledger.write("authoritative_evaluator", "tool:evaluate") as session:
            stored = store_and_register(
                session,
                store,
                PAYLOAD,
                artifact_type="kernel_receipt",
                media_type="application/json",
            )
        record = ledger.object(stored.object_id)
        assert record is not None
        assert record.properties["sha256"] == DIGEST
        assert record.properties["byte_size"] == len(PAYLOAD)
        locations = ledger.locations(stored.object_id)
        assert len(locations) == 1
        assert locations[0].is_present

    def test_registration_is_idempotent(
        self, ledger: Ledger, store: ArtifactStore
    ) -> None:
        for _ in range(2):
            with ledger.write("authoritative_evaluator", "tool:evaluate") as session:
                store_and_register(
                    session,
                    store,
                    PAYLOAD,
                    artifact_type="kernel_receipt",
                    media_type="application/json",
                )
        assert len(ledger.objects(kind="artifact")) == 1
        assert len(ledger.locations(artifact_id(DIGEST))) == 1

    def test_same_bytes_reuse_identity_across_semantic_roles(
        self, ledger: Ledger, store: ArtifactStore
    ) -> None:
        with ledger.write("authoritative_evaluator", "tool:evaluate") as session:
            first = store_and_register(
                session,
                store,
                PAYLOAD,
                artifact_type="promotion_manifest",
                media_type="application/json",
            )
        with ledger.write("importer", "tool:import") as session:
            second = store_and_register(
                session,
                store,
                PAYLOAD,
                artifact_type="historical_source",
                media_type="application/json",
                canonical_name="Historical source",
            )
        assert first.object_id == second.object_id
        assert len(ledger.objects(kind="artifact")) == 1

    def test_a_bad_digest_is_refused(self, ledger: Ledger) -> None:
        with pytest.raises(LedgerError, match="sha256"):
            with ledger.write("ledger_service", "svc") as session:
                session.register_artifact(
                    "NOTAHASH",
                    artifact_type="kernel_receipt",
                    byte_size=1,
                    media_type="application/json",
                )

    def test_location_methods_reject_non_artifacts(self, ledger: Ledger) -> None:
        with pytest.raises(LedgerError, match="not an artifact"):
            with ledger.write("ledger_service", "svc") as session:
                session.create_object(
                    "turn:one", "turn", "A turn", content_format="json"
                )
                session.add_location("turn:one", "/tmp/somewhere")


class TestLocationLifecycle:
    def _register(self, ledger: Ledger, store: ArtifactStore) -> str:
        with ledger.write("authoritative_evaluator", "tool:evaluate") as session:
            stored = store_and_register(
                session,
                store,
                PAYLOAD,
                artifact_type="kernel_receipt",
                media_type="application/json",
                extra_locations=("replica/archive/receipt.json",),
            )
        return stored.object_id

    def test_losing_a_location_keeps_identity(
        self, ledger: Ledger, store: ArtifactStore
    ) -> None:
        object_id = self._register(ledger, store)
        with ledger.write("ledger_service", "svc") as session:
            session.lose_location(
                object_id,
                "replica/archive/receipt.json",
                reason="volume detached",
            )
        present = ledger.locations(object_id, present_only=True)
        assert len(present) == 1
        assert ledger.object(object_id) is not None

    def test_an_artifact_with_no_present_location_is_reported(
        self, ledger: Ledger, store: ArtifactStore
    ) -> None:
        object_id = self._register(ledger, store)
        with ledger.write("ledger_service", "svc") as session:
            for location in ledger.locations(object_id):
                session.lose_location(object_id, location.location, reason="gone")
        assert [r.id for r in ledger.artifacts_without_location()] == [object_id]

    def test_reverify_detects_tampering_and_records_it(
        self, ledger: Ledger, store: ArtifactStore
    ) -> None:
        object_id = self._register(ledger, store)
        store.path_for(DIGEST).write_bytes(b"tampered")
        with ledger.write("ledger_service", "svc") as session:
            failures = reverify(session, store, object_id)
        assert len(failures) == 2
        assert ledger.locations(object_id, present_only=True) == []

    def test_reverify_confirms_intact_bytes(
        self, ledger: Ledger, store: ArtifactStore
    ) -> None:
        object_id = self._register(ledger, store)
        with ledger.write("ledger_service", "svc") as session:
            failures = reverify(session, store, object_id)
        # The configured replica does not exist in this test; the local copy does.
        assert str(store.path_for(DIGEST)) not in failures


class TestRetention:
    def test_a_receipt_with_one_copy_is_under_replicated(
        self, ledger: Ledger, store: ArtifactStore
    ) -> None:
        with ledger.write("authoritative_evaluator", "tool:evaluate") as session:
            store_and_register(
                session,
                store,
                PAYLOAD,
                artifact_type="kernel_receipt",
                media_type="application/json",
            )
        assert [r.id for r in under_replicated(ledger)] == [artifact_id(DIGEST)]

    def test_two_copies_satisfy_the_policy(
        self, ledger: Ledger, store: ArtifactStore
    ) -> None:
        with ledger.write("authoritative_evaluator", "tool:evaluate") as session:
            store_and_register(
                session,
                store,
                PAYLOAD,
                artifact_type="kernel_receipt",
                media_type="application/json",
                extra_locations=("replica/archive/receipt.json",),
            )
        assert under_replicated(ledger) == []

    def test_rollouts_are_not_required_to_be_replicated(
        self, ledger: Ledger, store: ArtifactStore
    ) -> None:
        with ledger.write("ledger_service", "svc") as session:
            store_and_register(
                session,
                store,
                b"rollout bytes",
                artifact_type="rollout",
                media_type="application/jsonl",
            )
        assert under_replicated(ledger) == []
