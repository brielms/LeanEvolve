"""`doctor` and `status`: the two questions a task interface must answer.

``doctor`` answers "can this machine run the supported workflows", and is the
first command to reach for when anything misbehaves. ``status`` answers "what
does this repository currently claim, and what is the safest next step" -- and
derives that only from kernel receipts, campaign manifests, and configuration,
never from celebratory prose checked into the tree.
"""

from __future__ import annotations

import importlib.metadata
import json
import os
from pathlib import Path
from typing import Any

from leanevolve.audit import sha256_file
from leanevolve.run import PINNED_SHINKA_COMMIT
from leanevolve.workflow import campaign as campaigns_module
from leanevolve.workflow.environment import Environment, describe_environment
from leanevolve.workflow.errors import Exit
from leanevolve.workflow.receipt import Receipt
from leanevolve.workflow.settings import Settings


def _shinka_distribution() -> dict[str, Any] | None:
    try:
        distribution = importlib.metadata.distribution("shinka-evolve")
    except importlib.metadata.PackageNotFoundError:
        return None
    direct = distribution.read_text("direct_url.json")
    revision = None
    if direct:
        try:
            payload = json.loads(direct)
        except json.JSONDecodeError:
            payload = {}
        revision = (payload.get("vcs_info") or {}).get("commit_id")
    return {"version": distribution.version, "commit": revision}


def _lean_toolchain_pins(settings: Settings) -> dict[str, str | None]:
    pins: dict[str, str | None] = {}
    for project in settings.lean_projects:
        relative = _relative(project.path, settings.root)
        pin_path = project.path / "lean-toolchain"
        pins[relative] = (
            pin_path.read_text(encoding="utf-8").strip() if pin_path.is_file() else None
        )
    return pins


def _relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path)


def _storage_report(settings: Settings) -> dict[str, Any]:
    storage = settings.storage
    root = storage.artifact_root
    exists = root.is_dir()
    writable = (
        os.access(root if exists else root.parent, os.W_OK) if root.parent else False
    )
    free_gb = storage.free_gb()
    return {
        "artifact_root": str(root),
        "cache_root": str(storage.cache_root),
        "exists": exists,
        "writable": bool(writable),
        "free_gb": None if free_gb is None else round(free_gb, 2),
        "min_free_gb": storage.min_free_gb,
        "require_external": storage.require_external,
        "external": not root.resolve().is_relative_to(settings.root.resolve())
        if root.exists() or root.parent.exists()
        else None,
    }


def _input_report(settings: Settings) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for relative in settings.required_inputs:
        path = settings.root / relative
        present = path.is_file()
        records.append(
            {
                "path": relative,
                "present": present,
                "sha256": sha256_file(path) if present else None,
            }
        )
    return records


def run_doctor(settings: Settings, receipt: Receipt) -> Receipt:
    """Diagnose the environment and finish with READY or actionable failures."""

    environment = describe_environment(settings)
    storage = _storage_report(settings)
    inputs = _input_report(settings)
    toolchains = _lean_toolchain_pins(settings)
    shinka = _shinka_distribution()
    failures: list[tuple[str, str]] = []

    receipt.say("LeanEvolve environment report")
    receipt.say()
    receipt.say(f"repository       {settings.root}")
    receipt.say(f"interpreter      {environment.interpreter}")
    receipt.say(
        f"python           {environment.python_version} "
        f"(leanevolve {environment.leanevolve_version})"
    )
    if shinka and shinka["commit"] != PINNED_SHINKA_COMMIT:
        failures.append(
            (
                "the installed ShinkaEvolve revision does not match the repository pin",
                "run `mise run setup` to synchronize the locked environment",
            )
        )
    if not environment.interpreter_managed:
        failures.append(
            (
                "the active interpreter is not the locked project environment",
                "run `mise run setup`, then invoke tasks through mise",
            )
        )

    receipt.say()
    receipt.say("tools")
    for tool in environment.tools:
        required = tool.name in settings.required_tools or tool.name in (
            "uv",
            "mise",
            "git",
        )
        if tool.available:
            receipt.say(f"  {tool.name:<10} {tool.version or 'version unknown'}")
            receipt.say(f"  {'':<10} {tool.path}")
        else:
            marker = "MISSING" if required else "absent (optional)"
            receipt.say(f"  {tool.name:<10} {marker}")
            if required:
                failures.append(
                    (
                        f"required tool {tool.name!r} is unavailable",
                        f"install {tool.name} and re-run `mise run doctor`",
                    )
                )
        receipt.step(
            f"tool:{tool.name}",
            "ok" if tool.available else "missing",
            tool.version or tool.error,
        )

    receipt.say()
    receipt.say("locked environment")
    receipt.say(
        f"  uv.lock        {'current' if environment.lock.current else 'STALE'}"
        + (f" ({environment.lock.detail})" if environment.lock.detail else "")
    )
    if not environment.lock.current:
        failures.append(
            (
                "uv.lock does not match pyproject.toml",
                "run `mise run lock` and commit the updated uv.lock",
            )
        )
    receipt.say(
        "  shinka         "
        + (
            f"{shinka['version']} @ {shinka['commit'] or 'unknown revision'}"
            if shinka
            else "not installed (`mise run setup`)"
        )
    )

    receipt.say()
    receipt.say("lean")
    for relative, pin in toolchains.items():
        receipt.say(f"  {relative}: {pin or 'NO lean-toolchain PIN'}")
        if pin is None:
            failures.append(
                (
                    f"{relative} does not pin a Lean toolchain",
                    f"add a lean-toolchain file to {relative}",
                )
            )
    lake = environment.tool("lake")
    if toolchains and not lake.available:
        failures.append(
            (
                "lake is unavailable, so no Lean gate can run",
                "install elan from https://github.com/leanprover/elan",
            )
        )

    receipt.say()
    receipt.say("storage")
    receipt.say(f"  artifacts      {storage['artifact_root']}")
    receipt.say(f"  cache          {storage['cache_root']}")
    free = storage["free_gb"]
    receipt.say(
        f"  free space     {'unknown' if free is None else f'{free:.2f} GB'}"
        f" (reserve {storage['min_free_gb']:g} GB)"
    )
    if not storage["writable"]:
        failures.append(
            (
                f"the artifact root is not writable: {storage['artifact_root']}",
                "run `mise run configure -- --artifact-root <writable path>`",
            )
        )
    elif free is not None and free < storage["min_free_gb"]:
        failures.append(
            (
                f"only {free:.2f} GB free below {storage['artifact_root']}",
                "free space, or point elsewhere with "
                "`mise run configure -- --artifact-root <path>`",
            )
        )
    if storage["require_external"] and storage["external"] is False:
        failures.append(
            (
                "this repository requires an external artifact volume",
                "attach the volume and run "
                "`mise run configure -- --artifact-root <mounted path>`",
            )
        )

    if settings.model_auth_env:
        receipt.say()
        receipt.say("model authentication")
        for name in settings.model_auth_env:
            present = bool(os.environ.get(name))
            receipt.say(f"  {name:<24} {'set' if present else 'not set'}")

    if inputs:
        receipt.say()
        receipt.say("required inputs")
        for record in inputs:
            digest = record["sha256"]
            receipt.say(
                f"  {record['path']}: " + (f"{digest[:16]}..." if digest else "MISSING")
            )
            if not record["present"]:
                failures.append(
                    (
                        f"required input is missing: {record['path']}",
                        f"restore {record['path']} from version control",
                    )
                )

    receipt.say()
    receipt.say("git")
    receipt.say(f"  commit         {environment.git_commit or 'unknown'}")
    worktree = "has uncommitted changes" if environment.git_dirty else "clean"
    receipt.say(f"  worktree       {worktree}")
    if environment.git_dirty:
        receipt.say(
            "  note           uncommitted changes would be included in a run's"
            " input snapshot"
        )

    receipt.inputs = {
        "settings": str(settings.root / "leanevolve.toml"),
        "local_overrides": list(settings.local_overrides),
    }
    receipt.outputs = []
    receipt.guarantees = [
        "every reported tool was resolved to an absolute path and queried",
        "the lockfile check compares uv.lock against pyproject.toml",
        "no credential value is read or printed, only whether a variable is set",
    ]
    receipt.not_checked = [
        "whether a model route actually accepts requests",
        "mathematical correctness of anything in this repository",
    ]
    receipt.scientific_status = "environment diagnosis only; no mathematical claim"

    receipt.say()
    if failures:
        receipt.status = "failed"
        receipt.exit_code = Exit.MISSING_TOOL
        receipt.say(f"NOT READY: {len(failures)} problem(s)")
        for index, (problem, fix) in enumerate(failures, start=1):
            receipt.say(f"  {index}. {problem}")
            receipt.say(f"     try: {fix}")
        receipt.next_action = failures[0][1]
    else:
        receipt.say("READY")
        receipt.next_action = "mise run check"
    receipt.inputs["environment"] = environment.as_dict()
    receipt.inputs["storage"] = storage
    receipt.inputs["lean_toolchains"] = toolchains
    receipt.inputs["shinka"] = shinka
    receipt.inputs["required_inputs"] = inputs
    return receipt


def _receipt_summary(settings: Settings) -> list[dict[str, Any]]:
    directory = settings.storage.receipts_dir
    if not directory.is_dir():
        return []
    records: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            records.append(
                {"task": path.stem, "status": "unreadable", "path": str(path)}
            )
            continue
        records.append(
            {
                "task": payload.get("task", path.stem),
                "status": payload.get("status"),
                "exit_class": payload.get("exit_class"),
                "finished_at_utc": payload.get("finished_at_utc"),
                "path": str(path),
            }
        )
    return records


def run_status(settings: Settings, receipt: Receipt) -> Receipt:
    """Report repository state from receipts, not from narrative documents."""

    environment: Environment = describe_environment(settings)
    shinka = _shinka_distribution()
    found = campaigns_module.discover(settings.storage.artifact_root)
    receipts = _receipt_summary(settings)
    inherited = campaigns_module.inherited_frontier(found)

    receipt.say("LeanEvolve status")
    receipt.say()
    receipt.say(f"claim            {settings.claim}")
    receipt.say(
        f"git              {(environment.git_commit or 'unknown')[:12]}"
        f" ({'dirty' if environment.git_dirty else 'clean'})"
    )
    lock_status = "locked" if environment.lock.current else "STALE uv.lock"
    interpreter_status = (
        "managed interpreter"
        if environment.interpreter_managed
        else "UNMANAGED interpreter"
    )
    receipt.say(f"environment      {lock_status}, {interpreter_status}")

    receipt.say()
    receipt.say("workflows")
    for name, workflow in sorted(settings.workflows.items()):
        missing = [
            requirement
            for requirement in workflow.requires
            if (
                (requirement == "shinka" and shinka is None)
                or (
                    requirement != "shinka"
                    and not environment.tool(requirement).available
                )
            )
        ]
        state = "ready" if not missing else "blocked: " + ", ".join(missing)
        receipt.say(f"  {name:<14} {state}")

    receipt.say()
    receipt.say("campaigns")
    if not found:
        receipt.say(f"  none recorded under {settings.storage.artifact_root}")
    else:
        for item in found[:10]:
            goals = ", ".join(item.accepted_goals) or "no accepted goals"
            receipt.say(f"  {item.name:<28} {item.status:<12} {goals}")
        if len(found) > 10:
            receipt.say(f"  ... {len(found) - 10} older campaign(s) not shown")
    frontier_goals = (
        ", ".join(inherited["accepted_goals"]) or "no goals"
        if inherited["available"]
        else None
    )
    receipt.say(
        "  inherited frontier: "
        + (
            f"{inherited['campaign']} ({frontier_goals})"
            if inherited["available"]
            else str(inherited["reason"])
        )
    )

    receipt.say()
    receipt.say("task receipts")
    if not receipts:
        receipt.say(f"  none yet under {settings.storage.receipts_dir}")
    for record in receipts:
        receipt.say(
            f"  {str(record['task']):<14} {str(record.get('status')):<10}"
            f" {record.get('finished_at_utc') or ''}"
        )

    unverified = [item for item in found if item.problems or item.status == "running"]
    if unverified:
        receipt.say()
        receipt.say("attention")
        for item in unverified[:5]:
            receipt.say(f"  {item.name}: {item.recovery()}")

    if not environment.lock.current:
        receipt.next_action = "mise run lock"
    elif not environment.interpreter_managed:
        receipt.next_action = "mise run setup"
    elif unverified:
        receipt.next_action = (
            f"leanevolve campaigns --json  # inspect {unverified[0].name}"
        )
    else:
        receipt.next_action = "mise run check"

    receipt.inputs = {
        "environment": environment.as_dict(),
        "campaigns": [item.as_dict() for item in found],
        "inherited_frontier": inherited,
        "task_receipts": receipts,
        "claim": settings.claim,
    }
    receipt.guarantees = [
        "campaign status is read from run manifests and proof lineage only",
        "workflow readiness reflects resolved tools, not documentation",
    ]
    receipt.not_checked = [
        "whether recorded campaigns still replay (run `mise run audit`)",
        "mathematical correctness of any accepted goal",
    ]
    receipt.scientific_status = (
        "inventory of recorded evidence; acceptance of a goal is a Lean kernel "
        "result, not a claim made by this report"
    )
    return receipt
