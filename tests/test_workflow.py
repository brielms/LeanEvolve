from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest

from leanevolve.workflow import cli
from leanevolve.workflow.errors import Exit, WorkflowError
from leanevolve.workflow.launch import build_plan, resolve_cost_ceiling
from leanevolve.workflow.receipt import Receipt
from leanevolve.workflow.schedule import parse_schedule
from leanevolve.workflow.settings import Limits, ScheduleSpec, Workflow


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
