"""Exports are deterministic and restore to the same ledger."""

from __future__ import annotations

import json

import pytest

from leanevolve.ledger import fixtures, integrity
from leanevolve.ledger.export import (
    EXPORT_FORMAT,
    ExportError,
    export_bytes,
    export_payload,
    read_export,
    restore,
    verify_restore,
    write_export,
)
from leanevolve.ledger.fixtures import replay_scenario as replay
from leanevolve.ledger.store import Ledger


@pytest.fixture()
def loaded(tmp_path) -> Ledger:
    store = Ledger.open(tmp_path / "source.sqlite3")
    replay(store, fixtures.TIMEOUT_PRESERVES_SCRATCH_SUCCESS)
    yield store
    store.close()


class TestDeterminism:
    def test_repeated_export_is_byte_identical(self, loaded: Ledger) -> None:
        assert export_bytes(loaded) == export_bytes(loaded)

    def test_two_ledgers_with_the_same_history_export_identically(
        self, tmp_path, loaded: Ledger
    ) -> None:
        with Ledger.open(tmp_path / "twin.sqlite3") as twin:
            replay(twin, fixtures.TIMEOUT_PRESERVES_SCRATCH_SUCCESS)
            twin_payload = export_payload(twin)
            original = export_payload(loaded)
        # recorded_at is a local fact about when rows were written, so compare
        # everything that describes what actually happened.
        for row in (*twin_payload["events"], *original["events"]):
            row.pop("recorded_at")
        assert twin_payload == original

    def test_the_head_hash_is_reproducible(self, tmp_path, loaded: Ledger) -> None:
        with Ledger.open(tmp_path / "twin2.sqlite3") as twin:
            replay(twin, fixtures.TIMEOUT_PRESERVES_SCRATCH_SUCCESS)
            assert twin.head().event_hash == loaded.head().event_hash

    def test_export_declares_its_format_and_head(self, loaded: Ledger) -> None:
        payload = export_payload(loaded)
        assert payload["format"] == EXPORT_FORMAT
        assert payload["head"]["event_hash"] == loaded.head().event_hash


class TestRoundTrip:
    def test_restore_reproduces_the_ledger(self, tmp_path, loaded: Ledger) -> None:
        digest = write_export(loaded, tmp_path / "export.json")
        assert len(digest) == 64
        payload = read_export(tmp_path / "export.json")
        with restore(payload, tmp_path / "restored.sqlite3") as restored:
            report = verify_restore(loaded, restored)
            assert report.ok
            assert restored.head().event_hash == loaded.head().event_hash
            assert restored.event_count() == loaded.event_count()

    def test_restored_objects_and_edges_match(self, tmp_path, loaded: Ledger) -> None:
        payload = export_payload(loaded)
        with restore(payload, tmp_path / "r2.sqlite3") as restored:
            assert [o.id for o in restored.objects()] == [
                o.id for o in loaded.objects()
            ]
            assert [c.id for c in restored.connections()] == [
                c.id for c in loaded.connections()
            ]

    def test_restore_refuses_to_overwrite(self, tmp_path, loaded: Ledger) -> None:
        payload = export_payload(loaded)
        target = tmp_path / "occupied.sqlite3"
        target.write_bytes(b"")
        with pytest.raises(ExportError, match="refusing to restore"):
            restore(payload, target)

    def test_restore_rejects_a_foreign_payload(self, tmp_path) -> None:
        with pytest.raises(ExportError, match="not a ledger export"):
            restore({"format": "something-else"}, tmp_path / "nope.sqlite3")

    def test_a_truncated_export_fails_rather_than_restoring_partly(
        self, tmp_path, loaded: Ledger
    ) -> None:
        payload = export_payload(loaded)
        payload["objects"] = payload["objects"][:1]
        target = tmp_path / "partial.sqlite3"
        with pytest.raises(ExportError, match="dangling references"):
            restore(payload, target)
        assert not target.exists()


class TestEveryScenarioRoundTrips:
    @pytest.mark.parametrize(
        "scenario", fixtures.all_scenarios(), ids=lambda s: s.name
    )
    def test_scenario_survives_export_and_restore(self, tmp_path, scenario) -> None:
        with Ledger.open(tmp_path / f"{scenario.name}.sqlite3") as ledger:
            replay(ledger, scenario)
            payload = export_payload(ledger)
            with restore(payload, tmp_path / f"{scenario.name}-r.sqlite3") as back:
                assert verify_restore(ledger, back).ok
                report = integrity.verify(back)
                assert report.ok, [i.as_dict() for i in report.errors()]


class TestSerialization:
    def test_export_is_valid_json(self, tmp_path, loaded: Ledger) -> None:
        write_export(loaded, tmp_path / "e.json")
        payload = json.loads((tmp_path / "e.json").read_text())
        assert payload["format"] == EXPORT_FORMAT

    def test_export_ends_with_a_newline(self, loaded: Ledger) -> None:
        assert export_bytes(loaded).endswith(b"\n")
