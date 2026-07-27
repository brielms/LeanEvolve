"""`check`, `audit`, and `demo`: the three gates, kept honestly distinct.

``check`` is the fast edit-time signal. It uses Lake's incremental build and
never claims to be a forensic audit. ``audit`` is the release-grade gate: a
clean Lean rebuild, the publication scan, the lockfile check, and the offline
kernel demonstration. Each task reports both what it guaranteed and what it
did not look at, because a fast check presented as a complete audit is the
failure mode this split exists to prevent.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from leanevolve.audit import file_record, sha256_file, utc_now
from leanevolve.config import load_config
from leanevolve.evaluate import evaluate, write_results
from leanevolve.workflow import campaign as campaigns_module
from leanevolve.workflow.environment import (
    lake_environment,
    lock_state,
    require_lake,
    resolve_tool,
)
from leanevolve.workflow.errors import Exit, WorkflowError
from leanevolve.workflow.receipt import Receipt
from leanevolve.workflow.settings import Settings

DEMO_FORMAT = "leanevolve-demo-receipt-v1"
_LOG_TAIL_CHARS = 4000


def _log_dir(settings: Settings, task: str) -> Path:
    directory = settings.storage.cache_root / "logs" / task
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _write_log(directory: Path, name: str, text: str) -> Path | None:
    try:
        path = directory / f"{name}.log"
        path.write_text(text, encoding="utf-8")
        return path
    except OSError:
        return None


def _run_step(
    receipt: Receipt,
    settings: Settings,
    task: str,
    name: str,
    command: list[str],
    cwd: Path | None = None,
    environment: dict[str, str] | None = None,
) -> bool:
    """Run one gate step, keeping the full log on disk and a short line on screen."""

    program = shutil.which(command[0], path=(environment or {}).get("PATH"))
    if program is None and not Path(command[0]).is_file():
        receipt.step(name, "failed", f"{command[0]} is not available")
        receipt.say(f"  {name:<28} MISSING  ({command[0]} not found)")
        return False
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd) if cwd else str(settings.root),
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
    except OSError as error:
        receipt.step(name, "failed", str(error))
        receipt.say(f"  {name:<28} FAILED   ({error})")
        return False
    elapsed = time.monotonic() - started
    log_path = _write_log(
        _log_dir(settings, task), name.replace("/", "_"), completed.stdout
    )
    if completed.returncode:
        receipt.step(
            name,
            "failed",
            f"exit status {completed.returncode}",
            log_path=log_path,
        )
        receipt.say(f"  {name:<28} FAILED   ({elapsed:.1f}s)")
        tail = completed.stdout[-_LOG_TAIL_CHARS:].rstrip()
        if tail:
            receipt.say("    " + "\n    ".join(tail.splitlines()[-20:]))
        if log_path is not None:
            receipt.say(f"    full log: {log_path}")
        return False
    receipt.step(name, "ok", f"{elapsed:.1f}s", log_path=log_path)
    receipt.say(f"  {name:<28} ok       ({elapsed:.1f}s)")
    return True


def _lean_steps(
    receipt: Receipt,
    settings: Settings,
    task: str,
    clean: bool,
) -> bool:
    if not settings.lean_projects:
        return True
    lake = require_lake()
    environment = lake_environment(lake)
    ok = True
    for project in settings.lean_projects:
        label = project.path.name
        if clean:
            ok &= _run_step(
                receipt,
                settings,
                task,
                f"lake clean [{label}]",
                [lake.path or "lake", "clean"],
                cwd=project.path,
                environment=environment,
            )
        ok &= _run_step(
            receipt,
            settings,
            task,
            f"lake build [{label}]",
            [lake.path or "lake", "build"],
            cwd=project.path,
            environment=environment,
        )
        if clean and project.audit_command:
            ok &= _run_step(
                receipt,
                settings,
                task,
                f"axiom gate [{label}]",
                list(project.audit_command),
                cwd=project.path,
                environment=environment,
            )
    return ok


def run_check(settings: Settings, receipt: Receipt) -> Receipt:
    """Fast edit-time validation. Deliberately not a forensic audit."""

    receipt.say("check (fast edit-time gate)")
    receipt.say()
    ok = True
    for command in settings.fast_checks:
        ok &= _run_step(receipt, settings, "check", command[0], list(command))
    ok &= _lean_steps(receipt, settings, "check", clean=False)

    receipt.guarantees = [
        "configured lint and test commands passed",
        "every configured Lake project builds incrementally from current sources",
    ]
    receipt.not_checked = [
        "a clean Lean rebuild (incremental build products were reused)",
        "the publication scan and lockfile gate (`mise run audit`)",
        "replay of any recorded campaign (`mise run audit -- --replay latest`)",
        "mathematical correctness of any declaration",
    ]
    receipt.scientific_status = (
        "fast repository gate; this is not evidence that any theorem is proved"
    )
    receipt.say()
    if ok:
        receipt.say("CHECK PASSED (fast gate only)")
        receipt.next_action = "mise run audit"
    else:
        receipt.status = "failed"
        receipt.exit_code = Exit.VALIDATION
        receipt.say(f"CHECK FAILED ({len(receipt.failed_steps())} step(s))")
        receipt.next_action = "fix the failing step above, then re-run `mise run check`"
    return receipt


def _demo_receipt_path(settings: Settings) -> Path:
    return settings.storage.cache_root / "demo" / "demo_receipt.json"


def run_demo(settings: Settings, receipt: Receipt) -> Receipt:
    """Deterministic, offline end-to-end evidence that the trust boundary works."""

    if settings.demo is None:
        raise WorkflowError(
            "this repository does not configure an offline demonstration",
            exit_code=Exit.MISSING_TOOL,
            remediation="add a [demo] table to leanevolve.toml; see docs/workflows.md",
        )
    specification = settings.demo
    for path, label in (
        (specification.config, "config"),
        (specification.candidate, "candidate"),
    ):
        if not path.is_file():
            raise WorkflowError(
                f"the demo {label} is missing: {path}",
                exit_code=Exit.USAGE,
                remediation=(
                    f"restore {path} or correct demo.{label} in leanevolve.toml"
                ),
            )
    lake = require_lake()
    receipt.say("demo (offline, deterministic, no model call)")
    receipt.say()
    config = load_config(specification.config)
    if not _run_step(
        receipt,
        settings,
        "demo",
        f"lake build [{config.lean_project.name}]",
        [lake.path or "lake", "build"],
        cwd=config.lean_project,
        environment=lake_environment(lake),
    ):
        receipt.status = "failed"
        receipt.exit_code = Exit.VALIDATION
        receipt.next_action = "run `mise run doctor` to check the Lean toolchain"
        return receipt

    results_dir = settings.storage.cache_root / "demo" / "evaluation"
    if results_dir.exists():
        shutil.rmtree(results_dir)
    started = time.monotonic()
    evaluation = evaluate(specification.candidate, config, results_dir)
    write_results(results_dir, evaluation, specification.candidate, config)
    elapsed = time.monotonic() - started
    accepted = evaluation.accepted_goals
    receipt.step(
        "kernel evaluation",
        "ok" if accepted else "failed",
        f"accepted goals: {', '.join(accepted) or 'none'}",
    )
    receipt.say(
        f"  {'kernel evaluation':<28} {'ok' if accepted else 'FAILED'}"
        f"       ({elapsed:.1f}s)"
    )
    receipt.say(f"  accepted goals: {', '.join(accepted) or 'none'}")

    missing = [goal for goal in specification.expect_goals if goal not in accepted]
    manifest_path = results_dir / "evaluation_manifest.json"
    payload = {
        "format": DEMO_FORMAT,
        "recorded_at_utc": utc_now(),
        "config": {
            "path": str(specification.config.relative_to(settings.root)),
            **file_record(specification.config),
        },
        "candidate": {
            "path": str(specification.candidate.relative_to(settings.root)),
            **file_record(specification.candidate),
        },
        "lean_toolchain": (config.lean_project / "lean-toolchain")
        .read_text(encoding="utf-8")
        .strip(),
        "accepted_goals": list(accepted),
        "expected_goals": list(specification.expect_goals),
        "evaluation_manifest_sha256": sha256_file(manifest_path),
        "lake_version": lake.version,
    }
    demo_receipt = _demo_receipt_path(settings)
    demo_receipt.parent.mkdir(parents=True, exist_ok=True)
    demo_receipt.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    reread = json.loads(demo_receipt.read_text(encoding="utf-8"))
    verified = (
        reread == payload
        and sha256_file(manifest_path) == payload["evaluation_manifest_sha256"]
    )
    receipt.step("miniature receipt", "ok" if verified else "failed", str(demo_receipt))
    receipt.say(f"  {'miniature receipt':<28} {'ok' if verified else 'FAILED'}")
    receipt.outputs = [str(results_dir), str(demo_receipt)]
    receipt.inputs = {
        "config_sha256": payload["config"]["sha256"],
        "candidate_sha256": payload["candidate"]["sha256"],
        "lean_toolchain": payload["lean_toolchain"],
    }
    receipt.guarantees = [
        "a real Lean kernel evaluated the bundled candidate under the axiom policy",
        "the emitted receipt was re-read and its recorded hashes re-verified",
        "no model was contacted and no credits were spent",
    ]
    receipt.not_checked = [
        "any candidate other than the bundled one",
        "mathematical significance of the accepted goals",
    ]
    receipt.scientific_status = (
        f"kernel accepted: {', '.join(accepted) or 'nothing'}; "
        "the demonstration exercises the gate, it does not advance mathematics"
    )
    receipt.say()
    receipt.say(f"  inspect: {results_dir / 'feedback.txt'}")
    if missing or not verified:
        receipt.status = "failed"
        receipt.exit_code = Exit.VALIDATION
        receipt.say(
            "DEMO FAILED: expected goals not accepted: " + ", ".join(missing)
            if missing
            else "DEMO FAILED: the miniature receipt did not verify"
        )
        receipt.next_action = "run `mise run doctor`, then re-run `mise run demo`"
    else:
        receipt.say("DEMO PASSED")
        receipt.next_action = "mise run check"
    return receipt


def run_audit(settings: Settings, receipt: Receipt, replay: str = "none") -> Receipt:
    """Release-grade gate: clean rebuild, publication scan, lock, and demo."""

    receipt.say("audit (release gate)")
    receipt.say()
    ok = True
    for command in settings.audit_checks:
        ok &= _run_step(receipt, settings, "audit", command[0], list(command))

    state = lock_state(settings.root, resolve_tool("uv"))
    receipt.step(
        "uv.lock", "ok" if state.current else "failed", state.detail or "current"
    )
    receipt.say(
        f"  {'uv.lock':<28} {'ok' if state.current else 'FAILED'}"
        + (f"   ({state.detail})" if state.detail else "")
    )
    ok &= state.current

    ok &= _lean_steps(receipt, settings, "audit", clean=True)

    replayed: list[str] = []
    if replay != "none":
        found = campaigns_module.discover(settings.storage.artifact_root)
        targets = found[:1] if replay == "latest" else found
        if not targets:
            receipt.say(f"  {'campaign replay':<28} none recorded")
        for item in targets:
            ok &= _run_step(
                receipt,
                settings,
                "audit",
                f"replay [{item.name}]",
                ["leanevolve-replay", "--run-dir", str(item.path)],
            )
            replayed.append(item.name)

    if settings.demo is not None:
        demo = run_demo(settings, Receipt(task="audit-demo"))
        receipt.steps.extend(demo.steps)
        status = "ok" if demo.status == "ok" else "failed"
        receipt.say(f"  {'offline demo':<28} {status}")
        ok &= demo.status == "ok"

    receipt.guarantees = [
        "the publication scan found no machine-specific path, secret, or symlink",
        "uv.lock matches pyproject.toml",
        "every configured Lake project rebuilt from clean and passed its axiom gate",
        "the offline demonstration re-verified a real kernel receipt",
    ]
    receipt.not_checked = [
        "campaigns not selected by --replay"
        if replay != "all"
        else "campaigns recorded outside the configured artifact root",
        "mathematical correctness of any declaration",
        "that a model route is reachable",
    ]
    if replay == "none":
        receipt.not_checked.insert(
            0, "replay of recorded campaigns (pass --replay latest)"
        )
    receipt.inputs = {"replay_scope": replay, "replayed_campaigns": replayed}
    receipt.scientific_status = (
        "release gate over repository artifacts; kernel acceptance is evidence "
        "about declarations, not a claim that any open problem is solved"
    )
    receipt.say()
    if ok:
        receipt.say("AUDIT PASSED")
        receipt.next_action = "the working tree is release-consistent"
    else:
        receipt.status = "failed"
        receipt.exit_code = Exit.VALIDATION
        receipt.say(f"AUDIT FAILED ({len(receipt.failed_steps())} step(s))")
        receipt.next_action = "fix the failing step above, then re-run `mise run audit`"
    return receipt


def campaign_report(settings: Settings, receipt: Receipt) -> Receipt:
    """List recorded campaigns and say exactly how each one can be recovered."""

    found = campaigns_module.discover(settings.storage.artifact_root)
    receipt.say(f"campaigns under {settings.storage.artifact_root}")
    receipt.say()
    if not found:
        receipt.say("  none recorded")
    for item in found:
        receipt.say(f"  {item.name}")
        receipt.say(f"    status      {item.status}")
        receipt.say(f"    started     {item.started_at_utc or 'unknown'}")
        receipt.say(
            f"    schedule    {(item.schedule or {}).get('description', 'unrecorded')}"
        )
        receipt.say(f"    goals       {', '.join(item.accepted_goals) or 'none'}")
        receipt.say(f"    replayable  {'yes' if item.replayable else 'no'}")
        if item.problems:
            receipt.say(f"    problems    {'; '.join(item.problems)}")
        receipt.say(f"    recovery    {item.recovery()}")
    receipt.inputs = {"campaigns": [item.as_dict() for item in found]}
    receipt.guarantees = [
        "each summary is derived from that campaign's own manifest and lineage"
    ]
    receipt.not_checked = ["whether the recorded candidates still replay"]
    receipt.scientific_status = "inventory only"
    receipt.next_action = (
        "mise run audit -- --replay latest" if found else "mise run demo"
    )
    return receipt


def collect_artifact_usage(settings: Settings) -> dict[str, Any]:
    """Report disk usage split into evidence, derived artifacts, and caches."""

    def usage(path: Path) -> int:
        if not path.exists():
            return 0
        return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())

    return {
        "artifact_root": str(settings.storage.artifact_root),
        "artifact_bytes": usage(settings.storage.artifact_root),
        "cache_root": str(settings.storage.cache_root),
        "cache_bytes": usage(settings.storage.cache_root),
    }
