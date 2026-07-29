"""The ledger CLI reports state and fails loudly on a damaged database."""

from __future__ import annotations

import json
import sqlite3

import pytest

from leanevolve.ledger import fixtures
from leanevolve.ledger.cli import EXIT_FAILED, EXIT_OK, main
from leanevolve.ledger.fixtures import replay_scenario as replay
from leanevolve.ledger.store import Ledger


@pytest.fixture()
def database(tmp_path):
    path = tmp_path / "cli.sqlite3"
    with Ledger.open(path) as ledger:
        replay(ledger, fixtures.TIMEOUT_PRESERVES_SCRATCH_SUCCESS)
    return path


def run(database, *args: str) -> int:
    return main(["--database", str(database), *args])


class TestVerify:
    def test_a_sound_database_exits_zero(self, database, capsys) -> None:
        assert run(database, "verify") == EXIT_OK
        assert "ok" in capsys.readouterr().out

    def test_a_damaged_database_exits_nonzero(self, database, capsys) -> None:
        connection = sqlite3.connect(database)
        connection.execute(
            "UPDATE events SET actor_class = 'research_agent' "
            "WHERE action = 'kernel_certified'"
        )
        connection.commit()
        connection.close()
        assert run(database, "verify") == EXIT_FAILED
        assert "FAILED" in capsys.readouterr().out

    def test_json_output_is_machine_readable(self, database, capsys) -> None:
        assert run(database, "--json", "verify") == EXIT_OK
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is True
        assert payload["event_count"] > 0


class TestHead:
    def test_head_reports_the_chain_tip(self, database, capsys) -> None:
        assert run(database, "--json", "head") == EXIT_OK
        payload = json.loads(capsys.readouterr().out)
        assert payload["event_id"] == payload["event_count"]
        assert len(payload["event_hash"]) == 64


class TestCanonicalViews:
    def test_status_is_a_head_stamped_ledger_projection(
        self, database, capsys
    ) -> None:
        assert run(database, "--json", "status") == EXIT_OK
        payload = json.loads(capsys.readouterr().out)
        assert len(payload["ledger_head_hash"]) == 64
        assert payload["format"] == "leanevolve-ledger-unified-status-v1"

    def test_recovery_reads_incomplete_work_from_the_ledger(
        self, database, capsys
    ) -> None:
        assert run(database, "--json", "recover") == EXIT_OK
        payload = json.loads(capsys.readouterr().out)
        assert payload["format"] == "leanevolve-ledger-recovery-queue-v1"
        assert payload["items"]

    def test_project_writes_only_domain_neutral_views(
        self, database, tmp_path, capsys
    ) -> None:
        output = tmp_path / "projections"
        assert run(database, "project", "--output", str(output)) == EXIT_OK
        capsys.readouterr()
        names = {path.name for path in output.glob("*.json")}
        assert "goal-board.json" in names
        assert "chronology.json" in names
        assert "unified-status.json" in names
        assert "active-goal-catalog.json" not in names


class TestShow:
    def test_show_reports_every_dimension(self, database, capsys) -> None:
        assert run(database, "--json", "show", "claim:base_bound") == EXIT_OK
        payload = json.loads(capsys.readouterr().out)
        assert payload["state"]["truth"] == "open"
        assert payload["state"]["verification"] == "scratch_checked"

    def test_show_resolves_an_alias(self, database, capsys) -> None:
        with Ledger.open(database) as ledger:
            with ledger.write("human_researcher", "human:researcher") as session:
                session.add_alias("Example.Legacy.baseBound", "claim:base_bound")
        assert run(database, "--json", "show", "Example.Legacy.baseBound") == EXIT_OK
        assert json.loads(capsys.readouterr().out)["id"] == "claim:base_bound"

    def test_an_unknown_object_fails(self, database, capsys) -> None:
        assert run(database, "show", "claim:nope") == EXIT_FAILED
        assert "unknown object" in capsys.readouterr().err


class TestEvents:
    def test_events_list_in_order(self, database, capsys) -> None:
        assert run(database, "--json", "events") == EXIT_OK
        payload = json.loads(capsys.readouterr().out)
        assert [item["id"] for item in payload] == sorted(
            item["id"] for item in payload
        )

    def test_events_filter_by_action(self, database, capsys) -> None:
        assert (
            run(database, "--json", "events", "--action", "scratch_kernel_checked")
            == EXIT_OK
        )
        payload = json.loads(capsys.readouterr().out)
        assert len(payload) == 3

    def test_events_replay_to_a_past_point(self, database, capsys) -> None:
        assert run(database, "--json", "events", "--until", "3") == EXIT_OK
        assert len(json.loads(capsys.readouterr().out)) == 3


class TestExport:
    def test_export_to_stdout_is_json(self, database, capsys) -> None:
        assert run(database, "export") == EXIT_OK
        payload = json.loads(capsys.readouterr().out)
        assert payload["format"] == "leanevolve-ledger-export-v1"

    def test_export_to_a_file_reports_its_digest(
        self, database, tmp_path, capsys
    ) -> None:
        destination = tmp_path / "export.json"
        assert run(database, "export", "--output", str(destination)) == EXIT_OK
        digest = capsys.readouterr().out.split()[0]
        assert len(digest) == 64
        assert destination.is_file()


class TestUsage:
    def test_a_missing_database_is_reported(self, tmp_path, capsys) -> None:
        assert main(["--database", str(tmp_path / "absent.sqlite3"), "head"]) != EXIT_OK
        assert "no ledger database" in capsys.readouterr().err
