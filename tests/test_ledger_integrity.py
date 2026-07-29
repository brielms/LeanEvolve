"""Integrity validation detects tampering, forgery, and dangling references.

Each test damages a real database and asserts the specific finding, because a
validator that only ever runs against clean data is untested.
"""

from __future__ import annotations

import sqlite3

import pytest

from leanevolve.ledger import fixtures, integrity
from leanevolve.ledger.artifacts import ArtifactStore, sha256_bytes, store_and_register
from leanevolve.ledger.fixtures import replay_scenario as replay
from leanevolve.ledger.integrity import Severity
from leanevolve.ledger.store import Ledger

RECEIPT_BYTES = b'{"format": "kernel-receipt"}'


@pytest.fixture()
def loaded(tmp_path) -> Ledger:
    """A ledger holding the killed-turn scenario."""
    store = Ledger.open(tmp_path / "integrity.sqlite3")
    replay(store, fixtures.TIMEOUT_PRESERVES_SCRATCH_SUCCESS)
    yield store
    store.close()


def _codes(report: integrity.IntegrityReport) -> set[str]:
    return {item.code for item in report.findings}


def _raw(ledger: Ledger) -> sqlite3.Connection:
    """A second connection used to damage the database behind the API's back."""
    connection = sqlite3.connect(ledger.path)
    connection.row_factory = sqlite3.Row
    return connection


class TestCleanDatabase:
    def test_a_fresh_ledger_is_sound(self, tmp_path) -> None:
        with Ledger.open(tmp_path / "fresh.sqlite3") as ledger:
            report = integrity.verify(ledger)
        assert report.ok
        assert report.event_count == 0

    def test_a_loaded_scenario_is_sound(self, loaded: Ledger) -> None:
        report = integrity.verify(loaded)
        assert report.ok, [item.as_dict() for item in report.errors()]
        assert report.event_count > 0
        assert report.head_hash != "0" * 64

    def test_every_scenario_verifies(self, tmp_path) -> None:
        for index, scenario in enumerate(fixtures.all_scenarios()):
            with Ledger.open(tmp_path / f"s{index}.sqlite3") as ledger:
                replay(ledger, scenario)
                report = integrity.verify(ledger)
                assert report.ok, (
                    f"{scenario.name}: {[i.as_dict() for i in report.errors()]}"
                )


class TestTamperDetection:
    def test_a_rewritten_payload_breaks_its_digest(self, loaded: Ledger) -> None:
        connection = _raw(loaded)
        connection.execute(
            "UPDATE events SET payload_json = '{\"outcome\": \"forged\", "
            '"exit_code": 0}\' WHERE action = \'check_completed\''
        )
        connection.commit()
        connection.close()
        report = integrity.verify(loaded)
        assert not report.ok
        assert "event_hash_mismatch" in _codes(report)

    def test_a_deleted_event_breaks_the_chain(self, loaded: Ledger) -> None:
        connection = _raw(loaded)
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute(
            "DELETE FROM events WHERE id = (SELECT MAX(id) - 1 FROM events)"
        )
        connection.commit()
        connection.close()
        report = integrity.verify(loaded)
        assert not report.ok
        assert {"event_id_gap", "chain_break"} & _codes(report)

    def test_a_reordered_chain_is_detected(self, loaded: Ledger) -> None:
        connection = _raw(loaded)
        connection.execute(
            "UPDATE events SET previous_event_hash = ? WHERE id = 3",
            ("f" * 64,),
        )
        connection.commit()
        connection.close()
        report = integrity.verify(loaded)
        assert not report.ok
        assert "chain_break" in _codes(report)


class TestForgedAuthority:
    def test_a_certification_from_an_agent_is_reported(self, loaded: Ledger) -> None:
        # Written directly, bypassing the API that would have refused it.
        connection = _raw(loaded)
        connection.execute(
            "UPDATE events SET actor_class = 'research_agent' "
            "WHERE action = 'kernel_certified'"
        )
        connection.commit()
        connection.close()
        report = integrity.verify(loaded)
        assert not report.ok
        assert {"unauthorized_event", "truth_from_wrong_actor"} <= _codes(report)

    def test_a_refutation_without_kernel_trust_is_reported(self, tmp_path) -> None:
        with Ledger.open(tmp_path / "refute.sqlite3") as ledger:
            replay(ledger, fixtures.WITNESS_AND_BRIDGE_REFUTE)
            connection = _raw(ledger)
            connection.execute(
                "UPDATE connections SET properties_json = "
                "'{\"trust_level\": \"asserted\"}' WHERE relation = 'refutes'"
            )
            connection.commit()
            connection.close()
            report = integrity.verify(ledger)
        assert not report.ok
        assert "refutation_without_kernel_trust" in _codes(report)


class TestReferentialIntegrity:
    def test_a_dangling_evidence_reference_is_reported(self, loaded: Ledger) -> None:
        connection = _raw(loaded)
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute(
            "UPDATE events SET evidence_object_id = ? "
            "WHERE evidence_object_id IS NOT NULL",
            ("artifact:sha256:" + "9" * 64,),
        )
        connection.commit()
        connection.close()
        report = integrity.verify(loaded)
        assert not report.ok
        assert "missing_evidence" in _codes(report)

    def test_an_artifact_whose_id_disagrees_with_its_digest(
        self, loaded: Ledger
    ) -> None:
        connection = _raw(loaded)
        connection.execute(
            "UPDATE objects SET properties_json = "
            "json_set(properties_json, '$.sha256', ?) WHERE kind = 'artifact'",
            ("b" * 64,),
        )
        connection.commit()
        connection.close()
        report = integrity.verify(loaded)
        assert not report.ok
        assert "artifact_identity_mismatch" in _codes(report)


class TestArtifactFindings:
    def test_corrupt_local_bytes_are_found_by_a_deep_run(self, tmp_path) -> None:
        store = ArtifactStore(tmp_path / "cas")
        with Ledger.open(tmp_path / "deep.sqlite3") as ledger:
            with ledger.write("authoritative_evaluator", "tool:evaluate") as session:
                store_and_register(
                    session,
                    store,
                    RECEIPT_BYTES,
                    artifact_type="kernel_receipt",
                    media_type="application/json",
                )
            shallow = integrity.verify(ledger, store=store)
            assert "artifact_corrupt" not in _codes(shallow)
            store.path_for(sha256_bytes(RECEIPT_BYTES)).write_bytes(b"tampered")
            deep = integrity.verify(ledger, store=store, deep=True)
        assert not deep.ok
        assert "artifact_corrupt" in _codes(deep)

    def test_under_replication_warns_without_failing(self, tmp_path) -> None:
        store = ArtifactStore(tmp_path / "cas")
        with Ledger.open(tmp_path / "warn.sqlite3") as ledger:
            with ledger.write("authoritative_evaluator", "tool:evaluate") as session:
                store_and_register(
                    session,
                    store,
                    RECEIPT_BYTES,
                    artifact_type="kernel_receipt",
                    media_type="application/json",
                )
            report = integrity.verify(ledger, store=store)
        assert report.ok
        assert "artifact_under_replicated" in _codes(report)
        assert all(
            item.severity is Severity.WARNING
            for item in report.findings
            if item.code == "artifact_under_replicated"
        )


class TestRendering:
    def test_human_output_names_failures(self, loaded: Ledger) -> None:
        connection = _raw(loaded)
        connection.execute(
            "UPDATE events SET actor_class = 'research_agent' "
            "WHERE action = 'kernel_certified'"
        )
        connection.commit()
        connection.close()
        text = integrity.render(integrity.verify(loaded))
        assert "FAILED" in text
        assert "truth_from_wrong_actor" in text

    def test_json_output_is_stable(self, loaded: Ledger) -> None:
        import json

        payload = json.loads(integrity.render_json(integrity.verify(loaded)))
        assert payload["format"] == "leanevolve-ledger-integrity-v1"
        assert payload["ok"] is True
