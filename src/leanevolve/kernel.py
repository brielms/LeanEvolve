"""Run Lean and extract per-declaration axiom dependency receipts."""

from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from leanevolve.config import SearchConfig
from leanevolve.policy import validate_candidate

_DEPENDS = re.compile(r"^'(.+)' depends on axioms: \[(.*)]$")
_NO_AXIOMS = re.compile(r"^'(.+)' does not depend on any axioms$")
MAX_FEEDBACK_CHARS = 20_000


@dataclass(frozen=True)
class KernelResult:
    accepted_goals: tuple[str, ...]
    goal_axioms: dict[str, tuple[str, ...]]
    returncode: int
    elapsed_seconds: float
    output: str
    command_display: tuple[str, ...]


def project_records(config: SearchConfig) -> dict[str, dict[str, object]]:
    from leanevolve.audit import relative_records

    return relative_records(config.root, config.project_files())


def project_sha256(config: SearchConfig) -> str:
    from leanevolve.audit import record_set_sha256

    return record_set_sha256(project_records(config))


def _environment() -> dict[str, str]:
    names = (
        "ELAN_HOME",
        "HOME",
        "LANG",
        "LC_ALL",
        "PATH",
        "TMPDIR",
        "USER",
    )
    return {name: os.environ[name] for name in names if name in os.environ}


def _receipt_map(output: str) -> dict[str, tuple[str, ...]]:
    receipts: dict[str, tuple[str, ...]] = {}
    for line in output.splitlines():
        stripped = line.strip()
        no_axioms = _NO_AXIOMS.fullmatch(stripped)
        if no_axioms:
            receipts[no_axioms.group(1)] = ()
            continue
        depends = _DEPENDS.fullmatch(stripped)
        if depends:
            values = tuple(
                sorted(
                    item.strip() for item in depends.group(2).split(",") if item.strip()
                )
            )
            receipts[depends.group(1)] = values
    return receipts


def run_kernel(
    config: SearchConfig,
    source: str,
    results_dir: Path,
) -> KernelResult:
    """Check one complete candidate and audit every configured declaration."""

    validate_candidate(source, config.max_source_bytes)
    results_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    base_command = ["lake", "env", "lean"]
    if config.kernel.warning_as_error:
        base_command.append("-DwarningAsError=true")
    command_display = (
        *config.kernel.sandbox_prefix,
        *base_command,
        "<generated-candidate.lean>",
    )

    def invoke(path: Path) -> tuple[int, str]:
        remaining = config.kernel.timeout_seconds - (time.monotonic() - started)
        if remaining <= 0:
            return 124, "LEANEVOLVE TIMEOUT"
        command = [
            *config.kernel.sandbox_prefix,
            *base_command,
            str(path),
        ]
        try:
            completed = subprocess.run(
                command,
                cwd=config.lean_project,
                env=_environment(),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=remaining,
                check=False,
            )
            return completed.returncode, completed.stdout
        except subprocess.TimeoutExpired as error:
            raw = error.stdout or ""
            output = raw if isinstance(raw, str) else raw.decode("utf-8", "replace")
            return 124, output + "\nLEANEVOLVE TIMEOUT"

    check_path = results_dir / "candidate_check.lean"
    check_path.write_text(source.rstrip() + "\n", encoding="utf-8")
    returncode, check_output = invoke(check_path)
    log_sections = ["=== candidate ===\n" + check_output]
    if returncode != 0:
        elapsed = time.monotonic() - started
        output = "\n".join(log_sections)
        (results_dir / "lean.log").write_text(output, encoding="utf-8")
        return KernelResult(
            accepted_goals=(),
            goal_axioms={},
            returncode=returncode,
            elapsed_seconds=elapsed,
            output=output[-MAX_FEEDBACK_CHARS:],
            command_display=command_display,
        )

    receipts: dict[str, tuple[str, ...]] = {}
    for goal in config.goals:
        audit_path = results_dir / f"candidate_audit_{goal.name}.lean"
        audit_path.write_text(
            source.rstrip() + "\n\n" + f"#print axioms {goal.declaration}\n",
            encoding="utf-8",
        )
        audit_returncode, audit_output = invoke(audit_path)
        log_sections.append(f"=== {goal.name} ===\n" + audit_output)
        if audit_returncode == 0:
            receipt = _receipt_map(audit_output).get(goal.declaration)
            if receipt is not None:
                receipts[goal.declaration] = receipt
    elapsed = time.monotonic() - started
    output = "\n".join(log_sections)
    (results_dir / "lean.log").write_text(output, encoding="utf-8")
    accepted: list[str] = []
    goal_axioms: dict[str, tuple[str, ...]] = {}
    for goal in config.goals:
        dependencies = receipts.get(goal.declaration)
        if dependencies is None:
            continue
        goal_axioms[goal.name] = dependencies
        if not set(dependencies) <= config.kernel.allowed_axioms:
            continue
        if not set(goal.depends_on) <= set(accepted):
            continue
        accepted.append(goal.name)
    return KernelResult(
        accepted_goals=tuple(accepted),
        goal_axioms=goal_axioms,
        returncode=returncode,
        elapsed_seconds=elapsed,
        output=output[-MAX_FEEDBACK_CHARS:],
        command_display=command_display,
    )
