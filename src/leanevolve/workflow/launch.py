"""`plan` and `run`: everything expensive goes through one gate.

Planning is free and creates nothing. It resolves the locked environment, the
ordered schedule, the hard cost ceiling, the storage reserve, and the
predecessor a new campaign would inherit, then stops. ``run`` repeats every one
of those validations before it spends a model turn, records the authorization
that let it proceed, and finalizes a receipt even when interrupted.
"""

from __future__ import annotations

import importlib.metadata
import math
import os
import secrets
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from leanevolve.audit import utc_now
from leanevolve.workflow import campaign as campaigns_module
from leanevolve.workflow.environment import (
    Environment,
    describe_environment,
    require_current_lock,
    require_managed_interpreter,
    resolve_lake,
    resolve_tool,
)
from leanevolve.workflow.errors import Exit, WorkflowError
from leanevolve.workflow.receipt import Receipt
from leanevolve.workflow.schedule import (
    SOLVE,
    SPOTLIGHT,
    Schedule,
    extract_schedule,
    parse_schedule,
)
from leanevolve.workflow.settings import LedgerConfig, Settings, Workflow


def _flag_value(arguments: list[str], flag: str) -> str | None:
    for index, argument in enumerate(arguments):
        if argument == flag and index + 1 < len(arguments):
            return arguments[index + 1]
        if argument.startswith(f"{flag}="):
            return argument.split("=", 1)[1]
    return None


def _has_flag(arguments: list[str], flag: str) -> bool:
    return any(
        argument == flag or argument.startswith(f"{flag}=") for argument in arguments
    )


def require_workflow_dependencies(workflow: Workflow) -> None:
    """Reject a missing declared dependency before allocating or spending."""

    missing: list[str] = []
    for requirement in workflow.requires:
        if requirement == "shinka":
            try:
                importlib.metadata.version("shinka-evolve")
            except importlib.metadata.PackageNotFoundError:
                missing.append("ShinkaEvolve")
        elif requirement == "lake":
            # Lean is commonly installed through elan, whose bin directory
            # need not be inherited by a bare `uv run` invocation.
            if not resolve_lake().available:
                missing.append(requirement)
        elif not resolve_tool(requirement).available:
            missing.append(requirement)
    if missing:
        names = ", ".join(missing)
        raise WorkflowError(
            f"workflow {workflow.name!r} is missing required tooling: {names}",
            exit_code=Exit.MISSING_TOOL,
            remediation="run `mise run setup`, then re-run `mise run doctor`",
        )


def campaign_directory(settings: Settings, workflow: Workflow) -> Path:
    """Build a collision-resistant, human-readable campaign path."""

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return (
        settings.storage.artifact_root
        / f"{workflow.name}-{stamp}-{secrets.token_hex(3)}"
    )


def require_storage(settings: Settings) -> dict[str, Any]:
    """Refuse to start work the filesystem cannot hold."""

    storage = settings.storage
    root = storage.artifact_root
    probe = root if root.exists() else root.parent
    if not probe.exists():
        raise WorkflowError(
            f"the artifact root does not exist: {root}",
            exit_code=Exit.INFRASTRUCTURE,
            remediation=(
                f"create {root}, or run `mise run configure -- --artifact-root <path>`"
            ),
        )
    if not os.access(probe, os.W_OK):
        raise WorkflowError(
            f"the artifact root is not writable: {root}",
            exit_code=Exit.INFRASTRUCTURE,
            remediation="run `mise run configure -- --artifact-root <writable path>`",
        )
    free_gb = storage.free_gb()
    if free_gb is not None and free_gb < storage.min_free_gb:
        raise WorkflowError(
            f"only {free_gb:.2f} GB free below {root}, reserve is "
            f"{storage.min_free_gb:g} GB",
            exit_code=Exit.INFRASTRUCTURE,
            remediation=(
                "free space, or run `mise run configure -- --artifact-root <path>`"
            ),
        )
    external = not root.resolve().is_relative_to(settings.root.resolve())
    if storage.require_external and not external:
        raise WorkflowError(
            "this repository requires an external artifact volume",
            exit_code=Exit.INFRASTRUCTURE,
            remediation=(
                "attach the volume, then `mise run configure -- --artifact-root <path>`"
            ),
        )
    return {
        "artifact_root": str(root),
        "free_gb": None if free_gb is None else round(free_gb, 2),
        "min_free_gb": storage.min_free_gb,
        "external": external,
    }


def require_ledger(settings: Settings, workflow: Workflow) -> dict[str, Any] | None:
    """Resolve the canonical ledger and fail closed for required workflows."""

    ledger: LedgerConfig | None = getattr(settings, "ledger", None)
    required = ledger is not None and workflow.name in ledger.required_workflows
    if ledger is None or ledger.database is None or ledger.artifacts is None:
        if required:
            raise WorkflowError(
                f"workflow {workflow.name!r} requires the canonical ledger",
                exit_code=Exit.INFRASTRUCTURE,
                remediation=(
                    "run `leanevolve configure --ledger-database <database> "
                    "--ledger-artifacts <directory>`"
                ),
            )
        return None
    if not ledger.database.is_file():
        if required:
            raise WorkflowError(
                f"canonical ledger database is unavailable: {ledger.database}",
                exit_code=Exit.INFRASTRUCTURE,
                remediation=(
                    "attach the ledger volume or correct the local configuration"
                ),
            )
        return None
    if not ledger.artifacts.is_dir() or not os.access(ledger.artifacts, os.W_OK):
        if required:
            raise WorkflowError(
                "canonical ledger artifact store is unavailable: "
                f"{ledger.artifacts}",
                exit_code=Exit.INFRASTRUCTURE,
                remediation=(
                    "attach the ledger volume or correct the local configuration"
                ),
            )
        return None
    return {
        "database": str(ledger.database.resolve()),
        "artifacts": str(ledger.artifacts.resolve()),
        "required": required,
    }


def resolve_cost_ceiling(
    settings: Settings, workflow: Workflow, arguments: list[str]
) -> float | None:
    """Return the per-chunk ceiling, rejecting any attempt to raise it."""

    if workflow.cost_flag is None:
        return None
    limit = settings.limits.max_api_costs
    requested = _flag_value(arguments, workflow.cost_flag)
    if requested is None:
        return limit
    try:
        value = float(requested)
    except ValueError:
        raise WorkflowError(
            f"{workflow.cost_flag} must be a number, got {requested!r}",
            exit_code=Exit.USAGE,
            remediation=f"pass {workflow.cost_flag} <amount>",
        ) from None
    if not math.isfinite(value):
        raise WorkflowError(
            f"{workflow.cost_flag} must be finite",
            exit_code=Exit.USAGE,
            remediation=f"pass {workflow.cost_flag} a finite amount",
        )
    if value < 0:
        raise WorkflowError(
            f"{workflow.cost_flag} must not be negative",
            exit_code=Exit.USAGE,
            remediation=f"pass {workflow.cost_flag} <amount>",
        )
    if value > limit:
        raise WorkflowError(
            f"{workflow.cost_flag} {value:g} exceeds the configured ceiling {limit:g}",
            exit_code=Exit.USAGE,
            remediation=(
                "lower the request, or raise the per-chunk "
                "limits.max_api_costs in leanevolve.toml and commit that decision"
            ),
        )
    return value


def _model_spend_chunk_count(schedule: Schedule | None) -> int:
    """Return how many independently budgeted model chunks will run."""

    if schedule is None:
        return 1
    if schedule.style == SPOTLIGHT:
        return schedule.solve_turns
    return max(1, sum(epoch.kind in (SOLVE, SPOTLIGHT) for epoch in schedule.epochs))


def estimate_runtime(
    settings: Settings, schedule: Schedule | None
) -> tuple[str, str | None]:
    """Estimate wall time from comparable finished campaigns, or say why not."""

    if schedule is None:
        return "unavailable", "this workflow does not declare a schedule"
    durations: list[float] = []
    for item in campaigns_module.discover(settings.storage.artifact_root):
        if (
            item.status != "completed"
            or not item.started_at_utc
            or not item.finished_at_utc
        ):
            continue
        if (item.schedule or {}).get("description") != schedule.describe():
            continue
        try:
            start = datetime.fromisoformat(item.started_at_utc)
            end = datetime.fromisoformat(item.finished_at_utc)
        except ValueError:
            continue
        durations.append((end - start).total_seconds())
    if not durations:
        return (
            "unavailable",
            "no completed campaign with this schedule has been recorded yet",
        )
    low, high = min(durations) / 60, max(durations) / 60
    if len(durations) == 1:
        return f"about {low:.0f} min (one comparable campaign)", None
    return f"{low:.0f}-{high:.0f} min ({len(durations)} comparable campaigns)", None


@dataclass(frozen=True)
class Plan:
    """A validated, no-spend description of what a run would do."""

    workflow: Workflow
    command: tuple[str, ...]
    schedule: Schedule | None
    cost_ceiling: float | None
    cost_ceiling_per_chunk: float | None
    model_spend_chunk_count: int
    storage: dict[str, Any]
    ledger: dict[str, Any] | None
    runtime_estimate: str
    runtime_reason: str | None
    inherited: dict[str, Any]
    campaign_dir: Path | None
    environment: Environment

    def as_dict(self) -> dict[str, Any]:
        return {
            "workflow": self.workflow.name,
            "command": list(self.command),
            "schedule": None if self.schedule is None else self.schedule.as_dict(),
            "maximum_model_spend": self.cost_ceiling,
            "maximum_model_spend_per_chunk": self.cost_ceiling_per_chunk,
            "model_spend_chunk_count": self.model_spend_chunk_count,
            "estimated_wall_time": self.runtime_estimate,
            "estimated_wall_time_reason": self.runtime_reason,
            "storage": self.storage,
            "ledger": self.ledger,
            "inherited_frontier": self.inherited,
            "campaign_directory": None
            if self.campaign_dir is None
            else str(self.campaign_dir),
            "environment": self.environment.as_dict(),
        }

    def render(self) -> list[str]:
        lines = [f"workflow:             {self.workflow.name}"]
        if self.schedule is not None:
            lines.append(f"schedule:             {self.schedule.describe()}")
        if self.cost_ceiling is None:
            lines.append(
                "maximum model spend:  none (this workflow does not contact a model)"
            )
        else:
            lines.append(
                "maximum model spend:  "
                f"{self.cost_ceiling:g} aggregate authorized ceiling"
            )
            lines.append(
                "per model chunk:      "
                f"{self.cost_ceiling_per_chunk:g} x "
                f"{self.model_spend_chunk_count} chunk(s)"
            )
        lines.append(
            "estimated wall time:  "
            + self.runtime_estimate
            + (f" -- {self.runtime_reason}" if self.runtime_reason else "")
        )
        if self.ledger is not None:
            lines.append(f"canonical ledger:      {self.ledger['database']}")
        free = self.storage["free_gb"]
        lines.append(
            "storage reserve:      "
            f"{self.storage['min_free_gb']:g} GB required, "
            + ("unknown free" if free is None else f"{free:.2f} GB free")
            + f" at {self.storage['artifact_root']}"
        )
        if self.inherited["available"]:
            goals = ", ".join(self.inherited["accepted_goals"]) or "no accepted goals"
            lines.append(
                f"inherited frontier:   {self.inherited['campaign']} "
                f"({goals}, inputs {str(self.inherited['inputs_sha256'])[:16]}...)"
            )
        else:
            lines.append(f"inherited frontier:   none -- {self.inherited['reason']}")
        if self.campaign_dir is not None:
            lines.append(f"campaign directory:   {self.campaign_dir} (not yet created)")
        lines.append("command:              " + " ".join(self.command))
        return lines


def build_plan(
    settings: Settings,
    workflow: Workflow,
    arguments: list[str],
    *,
    allocate_campaign: bool,
) -> Plan:
    """Validate every input a run depends on, without creating anything."""

    require_managed_interpreter(settings.root)
    require_current_lock(settings.root, resolve_tool("uv"))
    require_workflow_dependencies(workflow)
    storage = require_storage(settings)
    ledger = require_ledger(settings, workflow)
    per_chunk_ceiling = resolve_cost_ceiling(settings, workflow, arguments)
    campaign_dir = (
        campaign_directory(settings, workflow)
        if allocate_campaign and workflow.results_flag is not None
        else None
    )
    command = list(workflow.command) + list(arguments)
    for flag, value in workflow.defaults:
        if not _has_flag(command, flag):
            command.extend([flag, value])
    if workflow.model_flag and not _has_flag(command, workflow.model_flag):
        if settings.model_route is None:
            raise WorkflowError(
                f"{workflow.model_flag} was not supplied and no model.route "
                "is configured",
                exit_code=Exit.USAGE,
                remediation=(
                    f"pass {workflow.model_flag} <route>, or set model.route in "
                    "leanevolve.toml"
                ),
            )
        command.extend([workflow.model_flag, settings.model_route])
    schedule: Schedule | None = None
    spotlight_value = _flag_value(command, "--spotlight")
    if spotlight_value is not None:
        schedule = parse_schedule(SPOTLIGHT, spotlight_value)
    elif workflow.schedule is not None:
        schedule = extract_schedule(
            workflow.schedule.flag, workflow.schedule.style, command
        )
    model_spend_chunk_count = _model_spend_chunk_count(schedule)
    ceiling = (
        None
        if per_chunk_ceiling is None
        else per_chunk_ceiling * model_spend_chunk_count
    )
    if ceiling is not None and not math.isfinite(ceiling):
        raise WorkflowError(
            "aggregate model-spend ceiling must be finite",
            exit_code=Exit.USAGE,
            remediation="lower the per-chunk cost ceiling or chunk count",
        )
    if (
        workflow.cost_flag
        and per_chunk_ceiling is not None
        and not _has_flag(command, workflow.cost_flag)
    ):
        command.extend([workflow.cost_flag, f"{per_chunk_ceiling:g}"])
    if (
        workflow.results_flag
        and campaign_dir is not None
        and not _has_flag(command, workflow.results_flag)
    ):
        command.extend([workflow.results_flag, str(campaign_dir)])
    estimate, reason = estimate_runtime(settings, schedule)
    return Plan(
        workflow=workflow,
        command=tuple(command),
        schedule=schedule,
        cost_ceiling=ceiling,
        cost_ceiling_per_chunk=per_chunk_ceiling,
        model_spend_chunk_count=model_spend_chunk_count,
        storage=storage,
        ledger=ledger,
        runtime_estimate=estimate,
        runtime_reason=reason,
        inherited=campaigns_module.inherited_frontier(
            campaigns_module.discover(settings.storage.artifact_root)
        ),
        campaign_dir=campaign_dir,
        environment=describe_environment(settings),
    )


def _execute(command: list[str], settings: Settings, workflow: Workflow) -> int:
    environment = os.environ.copy()
    ledger = require_ledger(settings, workflow)
    if ledger is not None:
        environment["LEANEVOLVE_LEDGER_DB"] = str(ledger["database"])
        environment["LEANEVOLVE_LEDGER_ARTIFACTS"] = str(ledger["artifacts"])
    try:
        completed = subprocess.run(
            command,
            cwd=str(settings.root),
            env=environment,
            check=False,
        )
    except FileNotFoundError as error:
        raise WorkflowError(
            f"the workflow program is not installed: {command[0]}",
            exit_code=Exit.MISSING_TOOL,
            detail=str(error),
            remediation="run `mise run setup`, then retry",
        ) from error
    return completed.returncode


def run_plan(
    settings: Settings, receipt: Receipt, name: str, arguments: list[str]
) -> Receipt:
    """Preview an expensive workflow. Creates nothing and spends nothing."""

    workflow = settings.workflow(name)
    plan = build_plan(settings, workflow, arguments, allocate_campaign=True)
    receipt.say(f"plan for `{name}` (no campaign created, no model turn spent)")
    receipt.say()
    for line in plan.render():
        receipt.say("  " + line)
    receipt.inputs = plan.as_dict()
    receipt.step("validation", "ok", "inputs, schedule, ceiling, and storage accepted")

    if workflow.plan_flag:
        command = [*plan.command, workflow.plan_flag]
        receipt.say()
        receipt.say(f"  runner self-check:    {' '.join(command)}")
        status = _execute(command, settings, workflow)
        receipt.step(
            "runner self-check", "ok" if status == 0 else "failed", f"exit {status}"
        )
        if status:
            receipt.status = "failed"
            receipt.exit_code = Exit.VALIDATION
            receipt.next_action = (
                "correct the configuration the runner rejected, then re-run this plan"
            )
            receipt.say("  PLAN REJECTED by the workflow's own configuration check")
            return receipt

    receipt.guarantees = [
        "the locked environment and lockfile were verified before planning",
        "the schedule and cost ceiling were parsed and are enforceable",
        "no campaign directory was created and no model turn was consumed",
    ]
    receipt.not_checked = [
        "whether the model route currently accepts requests",
        "how long this particular schedule will actually take",
    ]
    receipt.scientific_status = "planning only; nothing was computed or proved"
    receipt.next_action = f"mise run {name} -- {' '.join(arguments)}".rstrip()
    receipt.say()
    receipt.say("PLAN ACCEPTED")
    return receipt


def run_workflow(
    settings: Settings,
    receipt: Receipt,
    name: str,
    arguments: list[str],
    *,
    assume_yes: bool,
) -> Receipt:
    """Run a declared workflow after the same validation the plan performs."""

    workflow = settings.workflow(name)
    plan = build_plan(settings, workflow, arguments, allocate_campaign=True)
    receipt.inputs = plan.as_dict()
    receipt.say(f"run `{name}`")
    receipt.say()
    for line in plan.render():
        receipt.say("  " + line)
    receipt.say()

    spends = plan.cost_ceiling is not None and plan.cost_ceiling > 0
    interactive = sys.stdin.isatty() and sys.stdout.isatty()
    authorization = "not required"
    if spends:
        if assume_yes:
            authorization = "non-interactive (--yes)"
        elif interactive:
            answer = input(f"Proceed and spend up to {plan.cost_ceiling:g}? [y/N] ")
            if answer.strip().lower() not in ("y", "yes"):
                receipt.status = "cancelled"
                receipt.exit_code = Exit.OK
                receipt.say("cancelled before any spend")
                receipt.next_action = f"mise run plan -- {name}"
                return receipt
            authorization = "interactive confirmation"
        else:
            raise WorkflowError(
                "this workflow spends model credits and stdin is not a terminal",
                exit_code=Exit.USAGE,
                remediation=(
                    "re-run with --yes, for example "
                    f"`leanevolve run {name} --yes -- ...`"
                ),
            )
    receipt.inputs["authorization"] = authorization
    receipt.step("authorization", "ok", authorization)

    if plan.campaign_dir is not None:
        receipt.outputs.append(str(plan.campaign_dir))
    started = utc_now()
    try:
        status = _execute(list(plan.command), settings, workflow)
    except KeyboardInterrupt:
        receipt.status = "interrupted"
        receipt.exit_code = Exit.INTERRUPTED
        receipt.step("workflow", "interrupted", "stopped before the runner finished")
        receipt.next_action = (
            f"inspect {plan.campaign_dir} with `leanevolve campaigns`; "
            "evidence already recorded is reused, never silently rerun"
        )
        receipt.say("INTERRUPTED")
        return receipt
    receipt.inputs["started_at_utc"] = started
    receipt.step("workflow", "ok" if status == 0 else "failed", f"exit {status}")

    recorded = (
        campaigns_module.read_campaign(plan.campaign_dir)
        if plan.campaign_dir is not None
        and campaigns_module.is_campaign_dir(plan.campaign_dir)
        else None
    )
    if recorded is not None:
        receipt.inputs["campaign"] = recorded.as_dict()
        receipt.say(f"  campaign status:      {recorded.status}")
        receipt.say(
            f"  accepted goals:       {', '.join(recorded.accepted_goals) or 'none'}"
        )
        receipt.scientific_status = "kernel-accepted goals: " + (
            ", ".join(recorded.accepted_goals)
            or "none; the campaign produced no result"
        )
        if not recorded.accepted_goals and status == 0:
            receipt.exit_code = Exit.NO_RESULT
            receipt.status = "no_result"
    receipt.guarantees = [
        "the per-chunk ceiling passed to the runner cannot exceed configuration",
        "aggregate authorization is derived from the recorded schedule",
        "the ordered schedule was recorded before the run began",
    ]
    receipt.not_checked = [
        "mathematical correctness of anything the run accepted",
        "replay of the produced campaign (`mise run audit -- --replay latest`)",
    ]
    if status and receipt.status == "ok":
        receipt.status = "failed"
        receipt.exit_code = Exit.INFRASTRUCTURE
        receipt.next_action = (
            f"inspect the runner output above; `leanevolve campaigns` explains whether "
            f"{plan.campaign_dir} can be reused"
        )
    elif receipt.next_action is None:
        receipt.next_action = "mise run audit -- --replay latest"
    return receipt
