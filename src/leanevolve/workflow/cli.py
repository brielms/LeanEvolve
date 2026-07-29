"""The single command family behind every mise task.

Each subcommand produces the same versioned receipt, writes it under the
configured cache root, and exits with a stable exit-code class. Agents can use
``--json`` everywhere and never have to scrape terminal output.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from leanevolve.audit import utc_now
from leanevolve.workflow.doctor import run_doctor, run_status
from leanevolve.workflow.errors import EXIT_DESCRIPTIONS, Exit, WorkflowError
from leanevolve.workflow.gates import (
    campaign_report,
    collect_artifact_usage,
    run_audit,
    run_check,
    run_demo,
)
from leanevolve.workflow.launch import run_plan, run_workflow
from leanevolve.workflow.receipt import Receipt, error_receipt, write_receipt
from leanevolve.workflow.settings import (
    LOCAL_SETTINGS_NAME,
    Settings,
    load_settings,
    write_local_settings,
)

BUILTIN_TASKS: tuple[dict[str, str], ...] = (
    {
        "name": "setup",
        "summary": "Install pinned tools and synchronize the locked environment.",
        "inputs": "none",
        "outputs": ".venv, resolved toolchain",
        "cost": "no model spend",
        "runtime": "seconds after the first run",
        "example": "mise run setup",
    },
    {
        "name": "doctor",
        "summary": "Diagnose tools, lockfile, Lean toolchain, storage, and inputs.",
        "inputs": "none",
        "outputs": "READY or a list of failures with recovery commands",
        "cost": "no model spend",
        "runtime": "a few seconds",
        "example": "mise run doctor",
    },
    {
        "name": "check",
        "summary": "Fast edit-time gate: lint, tests, incremental Lean build.",
        "inputs": "the working tree",
        "outputs": "pass/fail plus a receipt",
        "cost": "no model spend",
        "runtime": "seconds to a few minutes",
        "example": "mise run check",
    },
    {
        "name": "audit",
        "summary": (
            "Release gate: publication scan, lockfile, clean Lean rebuild, demo."
        ),
        "inputs": "the working tree, optionally recorded campaigns",
        "outputs": "pass/fail plus a receipt naming what was and was not checked",
        "cost": "no model spend",
        "runtime": "minutes; rebuilds Lean from clean",
        "example": "mise run audit -- --replay latest",
    },
    {
        "name": "demo",
        "summary": "Offline end-to-end kernel demonstration; no model call.",
        "inputs": "the bundled example configuration and candidate",
        "outputs": "an evaluation directory and a miniature verified receipt",
        "cost": "no model spend",
        "runtime": "under a minute once Lean is built",
        "example": "mise run demo",
    },
    {
        "name": "status",
        "summary": "What this repository currently claims, derived from receipts.",
        "inputs": "recorded campaigns and task receipts",
        "outputs": "inventory plus the safest next action",
        "cost": "no model spend",
        "runtime": "a few seconds",
        "example": "mise run status",
    },
    {
        "name": "configure",
        "summary": (
            f"Write machine-local storage and limit overrides to {LOCAL_SETTINGS_NAME}."
        ),
        "inputs": "paths and limits given as flags",
        "outputs": f"{LOCAL_SETTINGS_NAME} (never version controlled)",
        "cost": "no model spend",
        "runtime": "instant",
        "example": "mise run configure -- --artifact-root /path/to/volume/runs",
    },
    {
        "name": "plan",
        "summary": (
            "Preview an expensive workflow without creating or spending anything."
        ),
        "inputs": "a workflow name and its arguments",
        "outputs": "schedule, hard cost ceiling, storage reserve, inherited frontier",
        "cost": "no model spend",
        "runtime": "seconds",
        "example": "mise run plan -- shinka --proposal-steps 3",
    },
    {
        "name": "campaigns",
        "summary": "List recorded campaigns and how each one can be recovered.",
        "inputs": "the configured artifact root",
        "outputs": "per-campaign status, schedule, goals, and recovery command",
        "cost": "no model spend",
        "runtime": "seconds",
        "example": "mise run campaigns",
    },
    {
        "name": "artifacts",
        "summary": "Report disk usage split into evidence and disposable cache.",
        "inputs": "the configured artifact and cache roots",
        "outputs": "byte counts per category",
        "cost": "no model spend",
        "runtime": "seconds to minutes on large volumes",
        "example": "mise run artifacts",
    },
)


def _menu(settings: Settings, receipt: Receipt) -> Receipt:
    """Print the workflow menu humans read and agents parse."""

    receipt.say("LeanEvolve workflows")
    receipt.say()
    receipt.say("Every task below runs through `uv run --locked`, so none of them")
    receipt.say("require activating an environment or naming an interpreter path.")
    receipt.say()
    receipt.say("built-in tasks")
    for task in BUILTIN_TASKS:
        receipt.say(f"  {task['name']}")
        receipt.say(f"    {task['summary']}")
        receipt.say(f"    inputs:  {task['inputs']}")
        receipt.say(f"    outputs: {task['outputs']}")
        receipt.say(f"    cost:    {task['cost']}; runtime: {task['runtime']}")
        receipt.say(f"    example: {task['example']}")
    receipt.say()
    receipt.say("configured workflows")
    if not settings.workflows:
        receipt.say("  none declared in leanevolve.toml")
    for name, workflow in sorted(settings.workflows.items()):
        receipt.say(f"  {name} ({workflow.kind})")
        receipt.say(f"    {workflow.summary}")
        if workflow.inputs:
            receipt.say(f"    inputs:  {'; '.join(workflow.inputs)}")
        if workflow.outputs:
            receipt.say(f"    outputs: {'; '.join(workflow.outputs)}")
        receipt.say(f"    cost:    {workflow.cost}; runtime: {workflow.runtime}")
        if workflow.schedule is not None:
            receipt.say(
                f"    schedule: {workflow.schedule.flag} "
                f"({workflow.schedule.style} style)"
            )
        if workflow.example:
            receipt.say(f"    example: {workflow.example}")
    receipt.say()
    receipt.say("exit codes")
    for code, meaning in EXIT_DESCRIPTIONS.items():
        receipt.say(f"  {int(code):<4} {meaning}")
    receipt.inputs = {
        "builtin_tasks": [dict(task) for task in BUILTIN_TASKS],
        "workflows": [
            workflow.as_dict() for _, workflow in sorted(settings.workflows.items())
        ],
        "exit_codes": {
            int(code): {"class": code.name.lower(), "meaning": meaning}
            for code, meaning in EXIT_DESCRIPTIONS.items()
        },
    }
    receipt.scientific_status = "documentation only"
    receipt.next_action = "mise run doctor"
    return receipt


def _configure(
    settings: Settings, receipt: Receipt, args: argparse.Namespace
) -> Receipt:
    if (args.ledger_database is None) != (args.ledger_artifacts is None):
        raise WorkflowError(
            "--ledger-database and --ledger-artifacts must be set together",
            exit_code=Exit.USAGE,
            remediation="pass both ledger paths in the same configure command",
        )
    sections: dict[str, dict[str, Any]] = {
        "storage": {
            "artifact_root": args.artifact_root,
            "cache_root": args.cache_root,
            "min_free_gb": args.min_free_gb,
        },
        "limits": {
            "max_api_costs": args.max_api_costs,
            "max_parallel_jobs": args.max_parallel_jobs,
        },
        "model": {"route": args.model_route},
        "ledger": {
            "database": args.ledger_database,
            "artifacts": args.ledger_artifacts,
        },
    }
    requested = {
        key: value
        for section in sections.values()
        for key, value in section.items()
        if value is not None
    }
    if not requested:
        receipt.say("current effective settings")
        receipt.say()
        receipt.say(f"  storage.artifact_root  {settings.storage.artifact_root}")
        receipt.say(f"  storage.cache_root     {settings.storage.cache_root}")
        receipt.say(f"  storage.min_free_gb    {settings.storage.min_free_gb:g}")
        receipt.say(f"  limits.max_api_costs   {settings.limits.max_api_costs:g}")
        receipt.say(f"  limits.max_parallel_jobs {settings.limits.max_parallel_jobs}")
        receipt.say(f"  model.route            {settings.model_route or 'unset'}")
        receipt.say(
            f"  ledger.database        {settings.ledger.database or 'unset'}"
        )
        receipt.say(
            f"  ledger.artifacts       {settings.ledger.artifacts or 'unset'}"
        )
        receipt.say()
        receipt.say(
            "  overridden locally: "
            + (", ".join(settings.local_overrides) or "nothing")
        )
        receipt.say(f"  local file: {settings.local_path}")
        receipt.next_action = (
            "pass a flag to change a value, for example "
            "`mise run configure -- --artifact-root <path>`"
        )
        receipt.inputs = {
            "artifact_root": str(settings.storage.artifact_root),
            "cache_root": str(settings.storage.cache_root),
            "min_free_gb": settings.storage.min_free_gb,
            "max_api_costs": settings.limits.max_api_costs,
            "max_parallel_jobs": settings.limits.max_parallel_jobs,
            "model_route": settings.model_route,
            "ledger_database": (
                None
                if settings.ledger.database is None
                else str(settings.ledger.database)
            ),
            "ledger_artifacts": (
                None
                if settings.ledger.artifacts is None
                else str(settings.ledger.artifacts)
            ),
            "local_overrides": list(settings.local_overrides),
        }
        return receipt
    path = write_local_settings(settings.root, sections)
    receipt.outputs = [str(path)]
    receipt.inputs = {"written": requested, "path": str(path)}
    receipt.say(f"wrote machine-local overrides to {path}")
    for key, value in sorted(requested.items()):
        receipt.say(f"  {key} = {value}")
    receipt.say()
    receipt.say(f"{LOCAL_SETTINGS_NAME} is git-ignored; scientific defaults stay in")
    receipt.say("leanevolve.toml so supported workflows contain no machine paths.")
    receipt.next_action = "mise run doctor"
    return receipt


def _artifacts(settings: Settings, receipt: Receipt) -> Receipt:
    usage = collect_artifact_usage(settings)
    receipt.say("artifact usage")
    receipt.say()
    receipt.say(
        f"  immutable evidence   {usage['artifact_bytes'] / 1_000_000:.1f} MB"
        f"  {usage['artifact_root']}"
    )
    receipt.say(
        f"  disposable cache     {usage['cache_bytes'] / 1_000_000:.1f} MB"
        f"  {usage['cache_root']}"
    )
    receipt.say()
    receipt.say("  the cache root holds logs and demo output and is safe to delete;")
    receipt.say("  campaign directories are evidence and are never deleted by a task.")
    receipt.inputs = usage
    receipt.scientific_status = "inventory only"
    receipt.next_action = "mise run campaigns"
    return receipt


def _dispatch(args: argparse.Namespace, receipt: Receipt) -> Receipt:
    settings = load_settings(Path.cwd())
    if args.command == "menu":
        return _menu(settings, receipt)
    if args.command == "doctor":
        return run_doctor(settings, receipt)
    if args.command == "status":
        return run_status(settings, receipt)
    if args.command == "check":
        return run_check(settings, receipt)
    if args.command == "audit":
        return run_audit(settings, receipt, replay=args.replay)
    if args.command == "demo":
        return run_demo(settings, receipt)
    if args.command == "campaigns":
        return campaign_report(settings, receipt)
    if args.command == "artifacts":
        return _artifacts(settings, receipt)
    if args.command == "configure":
        return _configure(settings, receipt, args)
    if args.command == "plan":
        return run_plan(settings, receipt, args.workflow, _forwarded(args.arguments))
    if args.command == "run":
        assume_yes = args.yes
        raw_arguments = list(args.arguments)
        if "--yes" in raw_arguments:
            assume_yes = True
            raw_arguments.remove("--yes")
        arguments = _forwarded(raw_arguments)
        return run_workflow(
            settings,
            receipt,
            args.workflow,
            arguments,
            assume_yes=assume_yes,
        )
    raise WorkflowError(
        f"unknown command {args.command!r}",
        exit_code=Exit.USAGE,
        remediation="run `leanevolve menu`",
    )


def _forwarded(arguments: list[str]) -> list[str]:
    return arguments[1:] if arguments and arguments[0] == "--" else list(arguments)


def _receipt_directory() -> Path | None:
    """Locate the receipt directory, tolerating settings that failed to load."""

    try:
        return load_settings(Path.cwd()).storage.receipts_dir
    except WorkflowError:
        return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="leanevolve",
        description="Supported LeanEvolve workflows. Every task also accepts --json.",
    )
    parser.add_argument(
        "--json", action="store_true", help="emit the versioned receipt"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("menu", "list every supported task and workflow"),
        ("doctor", "diagnose the environment"),
        ("status", "report repository state from receipts"),
        ("check", "fast edit-time gate"),
        ("demo", "offline end-to-end kernel demonstration"),
        ("campaigns", "list recorded campaigns and recovery commands"),
        ("artifacts", "report artifact and cache disk usage"),
    ):
        subparsers.add_parser(name, help=help_text)
    audit = subparsers.add_parser("audit", help="release gate")
    audit.add_argument(
        "--replay",
        choices=("none", "latest", "all"),
        default="none",
        help="replay recorded campaigns as part of the gate",
    )
    configure = subparsers.add_parser(
        "configure", help=f"write machine-local overrides to {LOCAL_SETTINGS_NAME}"
    )
    configure.add_argument("--artifact-root", help="where campaign evidence is written")
    configure.add_argument(
        "--cache-root", help="where logs and demo output are written"
    )
    configure.add_argument("--min-free-gb", type=float, help="free-space reserve")
    configure.add_argument(
        "--max-api-costs", type=float, help="hard per-model-chunk spend ceiling"
    )
    configure.add_argument("--max-parallel-jobs", type=int, help="local resource limit")
    configure.add_argument("--model-route", help="default model route")
    configure.add_argument(
        "--ledger-database", help="canonical research-ledger SQLite database"
    )
    configure.add_argument(
        "--ledger-artifacts", help="canonical content-addressed ledger artifacts"
    )
    plan = subparsers.add_parser("plan", help="preview an expensive workflow")
    plan.add_argument("workflow")
    plan.add_argument("arguments", nargs=argparse.REMAINDER)
    run = subparsers.add_parser("run", help="run a declared workflow")
    run.add_argument("workflow")
    run.add_argument(
        "--yes",
        action="store_true",
        help="non-interactive authorization; recorded in the receipt",
    )
    run.add_argument("arguments", nargs=argparse.REMAINDER)
    return parser


def main(argv: list[str] | None = None) -> int:
    raw_arguments = list(sys.argv[1:] if argv is None else argv)
    # Mise appends task arguments after the version-controlled command, so a
    # user naturally writes `mise run status -- --json`. Accept `--json`
    # anywhere in the friendly command rather than forcing agents to know the
    # internal argparse ordering.
    json_output = "--json" in raw_arguments
    raw_arguments = [item for item in raw_arguments if item != "--json"]
    parser = build_parser()
    args = parser.parse_args(raw_arguments)
    args.json = args.json or json_output
    receipt = Receipt(task=args.command, started_at_utc=utc_now())
    try:
        receipt = _dispatch(args, receipt)
    except WorkflowError as error:
        receipt = error_receipt(receipt, error)
    except KeyboardInterrupt:
        receipt.status = "interrupted"
        receipt.exit_code = Exit.INTERRUPTED
        receipt.next_action = f"re-run `leanevolve {args.command}` when ready"
    except Exception as error:  # noqa: BLE001 - reported, never a bare traceback
        receipt = error_receipt(
            receipt,
            WorkflowError(
                f"unexpected failure in `{args.command}`",
                exit_code=Exit.INFRASTRUCTURE,
                detail=f"{type(error).__name__}: {error}",
                remediation=(
                    "run `leanevolve doctor`; if it reports READY, this is a bug"
                ),
            ),
        )
    receipt.finished_at_utc = utc_now()
    directory = _receipt_directory()
    if directory is not None:
        write_receipt(directory, receipt)
    if args.json:
        print(json.dumps(receipt.as_dict(), indent=2, sort_keys=True))
    else:
        sys.stdout.write(receipt.render())
    return int(receipt.exit_code)


def entrypoint() -> None:
    raise SystemExit(main())


if __name__ == "__main__":
    entrypoint()
