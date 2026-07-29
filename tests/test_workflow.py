from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest

from leanevolve.workflow import cli
from leanevolve.workflow.doctor import _ledger_report
from leanevolve.workflow.errors import Exit, WorkflowError
from leanevolve.workflow.launch import (
    _execute,
    _model_spend_chunk_count,
    build_plan,
    require_ledger,
    resolve_cost_ceiling,
)
from leanevolve.workflow.receipt import Receipt
from leanevolve.workflow.schedule import parse_schedule
from leanevolve.workflow.settings import (
    LedgerConfig,
    Limits,
    ScheduleSpec,
    Workflow,
    load_settings,
)


def workflow(**overrides: object) -> Workflow:
    values: dict[str, object] = {
        "name": "test",
        "summary": "test workflow",
        "kind": "campaign",
        "command": ("runner",),
        "inputs": (),
        "outputs": (),
        "cost": "no spend",
        "runtime": "instant",
        "example": "mise run test",
        "requires": (),
    }
    values.update(overrides)
    return Workflow(**values)


def test_chunk_schedule_preserves_sequential_semantics() -> None:
    schedule = parse_schedule("chunks", "2,3")

    assert schedule.describe() == (
        "2 solve turns -> expansion -> 3 solve turns -> expansion"
    )
    assert schedule.solve_turns == 5
    assert schedule.expansion_count == 2
    assert [epoch["kind"] for epoch in schedule.as_dict()["epochs"]] == [
        "solve",
        "expand",
        "solve",
        "expand",
    ]


@pytest.mark.parametrize("value", ["", "0", "2,,3", "2,-1", "100"])
def test_malformed_chunk_schedule_is_actionable(value: str) -> None:
    with pytest.raises(WorkflowError) as captured:
        parse_schedule("chunks", value)

    assert captured.value.exit_code == Exit.USAGE
    assert (
        captured.value.remediation == "pass a valid schedule, for example --chunks 2,3"
    )


def test_cost_ceiling_cannot_be_raised() -> None:
    settings = SimpleNamespace(limits=Limits(max_api_costs=5.0, max_parallel_jobs=1))
    configured = workflow(cost_flag="--max-api-costs")

    assert resolve_cost_ceiling(settings, configured, []) == 5.0
    assert resolve_cost_ceiling(settings, configured, ["--max-api-costs", "2.5"]) == 2.5
    with pytest.raises(WorkflowError) as captured:
        resolve_cost_ceiling(settings, configured, ["--max-api-costs", "6"])
    assert captured.value.exit_code == Exit.USAGE


def test_required_workflow_fails_closed_without_canonical_ledger() -> None:
    settings = SimpleNamespace(
        ledger=LedgerConfig(
            database=None,
            artifacts=None,
            required_workflows=("proof-search",),
        )
    )

    with pytest.raises(WorkflowError) as captured:
        require_ledger(settings, workflow(name="proof-search"))

    assert captured.value.exit_code == Exit.INFRASTRUCTURE
    assert "requires the canonical ledger" in captured.value.message


def test_optional_workflow_can_run_when_configured_ledger_is_detached(
    tmp_path: Path,
) -> None:
    settings = SimpleNamespace(
        ledger=LedgerConfig(
            database=tmp_path / "detached" / "research.sqlite3",
            artifacts=tmp_path / "detached" / "artifacts",
            required_workflows=(),
        )
    )

    assert require_ledger(settings, workflow(name="proof-search")) is None


def test_doctor_reports_ledger_availability(tmp_path: Path) -> None:
    database = tmp_path / "research.sqlite3"
    database.touch()
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    settings = SimpleNamespace(
        ledger=LedgerConfig(
            database=database,
            artifacts=artifacts,
            required_workflows=("proof-search",),
        )
    )

    report = _ledger_report(settings)
    assert report["available"] is True
    assert report["required_workflows"] == ["proof-search"]


def test_configure_refuses_a_partial_ledger_pair() -> None:
    args = Namespace(
        artifact_root=None,
        cache_root=None,
        min_free_gb=None,
        max_api_costs=None,
        max_parallel_jobs=None,
        model_route=None,
        ledger_database="research.sqlite3",
        ledger_artifacts=None,
    )

    with pytest.raises(WorkflowError) as captured:
        cli._configure(object(), Receipt(task="configure"), args)

    assert captured.value.exit_code == Exit.USAGE
    assert "must be set together" in captured.value.message


def test_workflow_execution_inherits_canonical_ledger(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    database = tmp_path / "research.sqlite3"
    database.touch()
    artifacts = tmp_path / "ledger-artifacts"
    artifacts.mkdir()
    settings = SimpleNamespace(
        root=tmp_path,
        ledger=LedgerConfig(
            database=database,
            artifacts=artifacts,
            required_workflows=("proof-search",),
        ),
    )
    captured: dict[str, object] = {}

    def run(command: list[str], **kwargs: object) -> SimpleNamespace:
        captured.update(command=command, **kwargs)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("leanevolve.workflow.launch.subprocess.run", run)

    assert _execute(["runner"], settings, workflow(name="proof-search")) == 0
    environment = captured["env"]
    assert isinstance(environment, dict)
    assert environment["LEANEVOLVE_LEDGER_DB"] == str(database)
    assert environment["LEANEVOLVE_LEDGER_ARTIFACTS"] == str(artifacts)


@pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
def test_cost_ceiling_must_be_finite(value: str) -> None:
    settings = SimpleNamespace(
        limits=Limits(max_api_costs=5.0, max_parallel_jobs=1)
    )
    configured = workflow(cost_flag="--max-api-costs")

    with pytest.raises(WorkflowError) as captured:
        resolve_cost_ceiling(settings, configured, ["--max-api-costs", value])

    assert captured.value.exit_code == Exit.USAGE
    assert "finite" in captured.value.message


@pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
def test_toml_api_cost_limit_must_be_finite(value: str, tmp_path: Path) -> None:
    (tmp_path / "leanevolve.toml").write_text(
        "\n".join(
            [
                'format = "leanevolve-workflows-v1"',
                "",
                "[storage]",
                'artifact_root = "artifacts"',
                'cache_root = ".cache"',
                "",
                "[limits]",
                f"max_api_costs = {value}",
                "max_parallel_jobs = 1",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(WorkflowError) as captured:
        load_settings(tmp_path)

    assert captured.value.exit_code == Exit.VALIDATION
    assert "limits.max_api_costs must be finite" in captured.value.message


def test_settings_refuse_a_partial_ledger_pair(tmp_path: Path) -> None:
    (tmp_path / "leanevolve.toml").write_text(
        "\n".join(
            [
                'format = "leanevolve-workflows-v1"',
                "",
                "[storage]",
                'artifact_root = "artifacts"',
                "",
                "[ledger]",
                'database = "research.sqlite3"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(WorkflowError) as captured:
        load_settings(tmp_path)

    assert captured.value.exit_code == Exit.VALIDATION
    assert "must be configured together" in captured.value.message


def test_aggregate_authorization_counts_independent_model_chunks() -> None:
    ordinary = parse_schedule("chunks", "2,3")
    spotlight = parse_schedule("spotlight", "intermediate_goal for 4 turns")

    assert _model_spend_chunk_count(ordinary) == 2
    assert _model_spend_chunk_count(spotlight) == 4


def test_stale_lock_fails_before_campaign_path_allocation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = SimpleNamespace(root=tmp_path)
    configured = workflow(
        results_flag="--results-dir",
        schedule=ScheduleSpec(flag="--proposal-steps", style="steps"),
    )
    allocated = False

    monkeypatch.setattr(
        "leanevolve.workflow.launch.require_managed_interpreter", lambda root: "ok"
    )

    def stale(root: Path, uv: object) -> None:
        raise WorkflowError("stale", exit_code=Exit.VALIDATION)

    def allocate(settings: object, selected: object) -> Path:
        nonlocal allocated
        allocated = True
        return tmp_path / "must-not-exist"

    monkeypatch.setattr("leanevolve.workflow.launch.require_current_lock", stale)
    monkeypatch.setattr("leanevolve.workflow.launch.campaign_directory", allocate)

    with pytest.raises(WorkflowError):
        build_plan(
            settings, configured, ["--proposal-steps", "3"], allocate_campaign=True
        )
    assert not allocated


def test_json_is_accepted_after_mise_task_name(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "_dispatch", lambda args, receipt: receipt)
    monkeypatch.setattr(cli, "_receipt_directory", lambda: None)

    assert cli.main(["menu", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["task"] == "menu"


def test_yes_is_consumed_by_wrapper_not_forwarded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    monkeypatch.setattr(cli, "load_settings", lambda path: object())

    def run(
        settings: object,
        receipt: Receipt,
        name: str,
        arguments: list[str],
        *,
        assume_yes: bool,
    ) -> Receipt:
        captured.update(name=name, arguments=arguments, assume_yes=assume_yes)
        return receipt

    monkeypatch.setattr(cli, "run_workflow", run)
    args = Namespace(
        command="run",
        workflow="shinka",
        arguments=["--yes", "--", "--proposal-steps", "3"],
        yes=False,
    )

    cli._dispatch(args, Receipt(task="run"))
    assert captured == {
        "name": "shinka",
        "arguments": ["--proposal-steps", "3"],
        "assume_yes": True,
    }
