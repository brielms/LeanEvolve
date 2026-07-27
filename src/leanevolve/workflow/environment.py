"""Resolve the toolchain a task is actually running under.

Nothing here trusts a name on ``PATH``: every tool is resolved to an absolute
path and a version string, and the resolved set is recorded in run provenance
so a campaign can be tied to the environment that produced it. Discovery is
portable -- no user or volume path is hard-coded -- but the *resolved* absolute
paths are reported, because a receipt that hides them is not auditable.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from leanevolve import __version__
from leanevolve.workflow.errors import Exit, WorkflowError
from leanevolve.workflow.settings import Settings

UNMANAGED_ENVIRONMENT_ESCAPE = "LEANEVOLVE_ALLOW_UNMANAGED_ENV"
_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class Tool:
    """One resolved external program."""

    name: str
    path: str | None
    version: str | None
    error: str | None = None

    @property
    def available(self) -> bool:
        return self.path is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": self.path,
            "version": self.version,
            "error": self.error,
        }


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
        timeout=_TIMEOUT_SECONDS,
    )


def _first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ""


def resolve_tool(name: str, version_args: tuple[str, ...] = ("--version",)) -> Tool:
    """Locate a program and read its version, without raising on absence."""

    path = shutil.which(name)
    if path is None:
        return Tool(name=name, path=None, version=None, error="not found on PATH")
    try:
        completed = _run([path, *version_args])
    except (OSError, subprocess.SubprocessError) as error:
        return Tool(name=name, path=path, version=None, error=str(error))
    if completed.returncode:
        return Tool(
            name=name,
            path=path,
            version=None,
            error=_first_line(completed.stdout)
            or f"exit status {completed.returncode}",
        )
    return Tool(name=name, path=path, version=_first_line(completed.stdout))


def elan_bin() -> Path:
    """Return the conventional elan binary directory for this account."""

    home = os.environ.get("ELAN_HOME")
    if home:
        return Path(home) / "bin"
    return Path.home() / ".elan" / "bin"


def resolve_lake() -> Tool:
    """Find ``lake`` on PATH, then in the account's elan installation."""

    tool = resolve_tool("lake")
    if tool.available:
        return tool
    fallback = elan_bin() / "lake"
    if not fallback.is_file() or not os.access(fallback, os.X_OK):
        return Tool(
            name="lake",
            path=None,
            version=None,
            error="not found on PATH or in the elan installation",
        )
    try:
        completed = _run([str(fallback), "--version"])
    except (OSError, subprocess.SubprocessError) as error:
        return Tool(name="lake", path=str(fallback), version=None, error=str(error))
    return Tool(
        name="lake",
        path=str(fallback),
        version=_first_line(completed.stdout) if not completed.returncode else None,
        error=None if not completed.returncode else _first_line(completed.stdout),
    )


def lake_environment(lake: Tool) -> dict[str, str]:
    """Return an environment in which ``lake`` and ``lean`` are on PATH."""

    environment = dict(os.environ)
    if lake.path is not None:
        directory = str(Path(lake.path).parent)
        if directory not in environment.get("PATH", "").split(os.pathsep):
            environment["PATH"] = directory + os.pathsep + environment.get("PATH", "")
    return environment


def require_lake() -> Tool:
    """Return a usable ``lake`` or explain exactly how to install one."""

    lake = resolve_lake()
    if lake.available and lake.version:
        return lake
    raise WorkflowError(
        "the Lean build tool 'lake' is unavailable",
        exit_code=Exit.MISSING_TOOL,
        detail=lake.error,
        remediation=(
            "install elan from https://github.com/leanprover/elan and re-run "
            "`mise run doctor`"
        ),
    )


@dataclass(frozen=True)
class LockState:
    """Whether ``uv.lock`` still agrees with ``pyproject.toml``."""

    present: bool
    current: bool
    detail: str | None

    def as_dict(self) -> dict[str, Any]:
        return {"present": self.present, "current": self.current, "detail": self.detail}


def lock_state(root: Path, uv: Tool) -> LockState:
    """Check the lockfile without mutating it."""

    if not (root / "uv.lock").is_file():
        return LockState(present=False, current=False, detail="uv.lock is missing")
    if not uv.available:
        return LockState(
            present=True, current=False, detail="uv is unavailable, cannot verify"
        )
    try:
        completed = _run([uv.path or "uv", "lock", "--check", "--project", str(root)])
    except (OSError, subprocess.SubprocessError) as error:
        return LockState(present=True, current=False, detail=str(error))
    if completed.returncode:
        return LockState(
            present=True,
            current=False,
            detail=_first_line(completed.stdout) or "uv.lock is out of date",
        )
    return LockState(present=True, current=True, detail=None)


def require_current_lock(root: Path, uv: Tool) -> LockState:
    """Refuse to continue on a stale lockfile, before anything expensive runs."""

    state = lock_state(root, uv)
    if state.current:
        return state
    raise WorkflowError(
        "uv.lock does not match pyproject.toml",
        exit_code=Exit.VALIDATION,
        detail=state.detail,
        remediation="run `mise run lock` and commit the updated uv.lock",
    )


def managed_interpreter(root: Path) -> tuple[bool, str]:
    """Report whether this interpreter is the project's locked environment."""

    if os.environ.get(UNMANAGED_ENVIRONMENT_ESCAPE) == "1":
        return True, f"{UNMANAGED_ENVIRONMENT_ESCAPE}=1 overrides the check"
    prefix = Path(sys.prefix).resolve()
    expected = (root / ".venv").resolve()
    if prefix == expected:
        return True, f"running from {prefix}"
    return False, f"running from {prefix}, expected {expected}"


def require_managed_interpreter(root: Path) -> str:
    """Stop a system interpreter from starting expensive or auditable work."""

    managed, detail = managed_interpreter(root)
    if managed:
        return detail
    raise WorkflowError(
        "this task must run inside the locked project environment",
        exit_code=Exit.MISSING_TOOL,
        detail=detail,
        remediation=(
            "run it through the task interface, for example "
            "`mise run check`, or prefix it with `uv run --locked`"
        ),
    )


@dataclass(frozen=True)
class Environment:
    """The full resolved toolchain, suitable for a receipt or provenance block."""

    root: Path
    tools: tuple[Tool, ...]
    lock: LockState
    interpreter: str
    interpreter_managed: bool
    python_version: str
    leanevolve_version: str
    git_commit: str | None
    git_dirty: bool | None

    def tool(self, name: str) -> Tool:
        for candidate in self.tools:
            if candidate.name == name:
                return candidate
        return Tool(name=name, path=None, version=None, error="not resolved")

    def as_dict(self) -> dict[str, Any]:
        return {
            "repository_root": str(self.root),
            "tools": [tool.as_dict() for tool in self.tools],
            "lock": self.lock.as_dict(),
            "interpreter": self.interpreter,
            "interpreter_managed": self.interpreter_managed,
            "python_version": self.python_version,
            "leanevolve_version": self.leanevolve_version,
            "git_commit": self.git_commit,
            "git_dirty": self.git_dirty,
        }


def _git_state(root: Path) -> tuple[str | None, bool | None]:
    git = shutil.which("git")
    if git is None:
        return None, None
    try:
        head = _run([git, "-C", str(root), "rev-parse", "HEAD"])
        status = _run([git, "-C", str(root), "status", "--porcelain"])
    except (OSError, subprocess.SubprocessError):
        return None, None
    if head.returncode or status.returncode:
        return None, None
    return head.stdout.strip() or None, bool(status.stdout.strip())


def describe_environment(settings: Settings) -> Environment:
    """Resolve every tool a supported workflow can depend on."""

    uv = resolve_tool("uv")
    resolved: dict[str, Tool] = {
        "mise": resolve_tool("mise"),
        "uv": uv,
        "git": resolve_tool("git"),
        "elan": resolve_tool("elan"),
        "lake": resolve_lake(),
    }
    for name in (*settings.required_tools, *settings.optional_tools):
        if name not in resolved:
            resolved[name] = resolve_tool(name)
    tools = [resolved[name] for name in sorted(resolved)]
    managed, detail = managed_interpreter(settings.root)
    commit, dirty = _git_state(settings.root)
    return Environment(
        root=settings.root,
        tools=tuple(tools),
        lock=lock_state(settings.root, uv),
        interpreter=detail,
        interpreter_managed=managed,
        python_version=(
            f"{sys.version_info.major}.{sys.version_info.minor}."
            f"{sys.version_info.micro}"
        ),
        leanevolve_version=__version__,
        git_commit=commit,
        git_dirty=dirty,
    )
