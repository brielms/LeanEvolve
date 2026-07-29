"""Repository workflow settings and portable, machine-local overrides.

Scientific defaults live in the version-controlled ``leanevolve.toml``. Anything
that names a particular machine -- an external artifact volume, a cache
location, a resource limit -- belongs in the git-ignored
``leanevolve.local.toml`` written by ``mise run configure``. Supported workflows
therefore contain no absolute user paths, while a run receipt still records the
absolute paths that were actually resolved.
"""

from __future__ import annotations

import math
import shutil
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from leanevolve.workflow.errors import Exit, WorkflowError

SETTINGS_FORMAT = "leanevolve-workflows-v1"
SETTINGS_NAME = "leanevolve.toml"
LOCAL_SETTINGS_NAME = "leanevolve.local.toml"
SCHEDULE_STYLES = ("steps", "chunks", "spotlight")
WORKFLOW_KINDS = ("campaign", "verification", "intake", "utility")


def repository_root(start: Path | None = None) -> Path:
    """Return the directory holding ``leanevolve.toml``, searching upwards."""

    current = (start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        if (candidate / SETTINGS_NAME).is_file():
            return candidate
    raise WorkflowError(
        f"no {SETTINGS_NAME} found at or above {current}",
        exit_code=Exit.USAGE,
        remediation="run the task from inside the repository checkout",
    )


@dataclass(frozen=True)
class Storage:
    """Where artifacts go, and what the filesystem must guarantee."""

    artifact_root: Path
    cache_root: Path
    min_free_gb: float
    require_external: bool

    @property
    def receipts_dir(self) -> Path:
        return self.cache_root / "receipts"

    def free_gb(self) -> float | None:
        probe = self.artifact_root
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        if not probe.exists():
            return None
        return shutil.disk_usage(probe).free / 1_000_000_000


@dataclass(frozen=True)
class Limits:
    """Hard ceilings enforced below the friendly task layer."""

    max_api_costs: float
    max_parallel_jobs: int


@dataclass(frozen=True)
class LedgerConfig:
    """Canonical ledger locations and workflows that must use them."""

    database: Path | None
    artifacts: Path | None
    required_workflows: tuple[str, ...]


@dataclass(frozen=True)
class ScheduleSpec:
    """How a campaign workflow accepts an ordered solve/expansion schedule."""

    flag: str
    style: str


@dataclass(frozen=True)
class LeanProject:
    """A Lake project that ``check`` builds incrementally and ``audit`` rebuilds."""

    path: Path
    audit_command: tuple[str, ...]


@dataclass(frozen=True)
class DemoSpec:
    """The offline end-to-end demonstration, if this repository ships one."""

    config: Path
    candidate: Path
    expect_goals: tuple[str, ...]


@dataclass(frozen=True)
class Workflow:
    """One supported scientific workflow, described for humans and agents."""

    name: str
    summary: str
    kind: str
    command: tuple[str, ...]
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    cost: str
    runtime: str
    example: str
    requires: tuple[str, ...]
    plan_flag: str | None = None
    cost_flag: str | None = None
    results_flag: str | None = None
    model_flag: str | None = None
    defaults: tuple[tuple[str, str], ...] = ()
    schedule: ScheduleSpec | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "summary": self.summary,
            "kind": self.kind,
            "command": list(self.command),
            "inputs": list(self.inputs),
            "outputs": list(self.outputs),
            "cost": self.cost,
            "runtime": self.runtime,
            "example": self.example,
            "requires": list(self.requires),
            "plan_flag": self.plan_flag,
            "cost_flag": self.cost_flag,
            "results_flag": self.results_flag,
            "model_flag": self.model_flag,
            "defaults": [list(item) for item in self.defaults],
            "schedule": (
                None
                if self.schedule is None
                else {"flag": self.schedule.flag, "style": self.schedule.style}
            ),
        }


@dataclass(frozen=True)
class Settings:
    """Resolved repository settings with local overrides already applied."""

    root: Path
    claim: str
    storage: Storage
    limits: Limits
    ledger: LedgerConfig
    model_route: str | None
    model_auth_env: tuple[str, ...]
    required_tools: tuple[str, ...]
    optional_tools: tuple[str, ...]
    required_inputs: tuple[str, ...]
    lean_projects: tuple[LeanProject, ...]
    fast_checks: tuple[tuple[str, ...], ...]
    audit_checks: tuple[tuple[str, ...], ...]
    demo: DemoSpec | None
    workflows: dict[str, Workflow]
    local_overrides: tuple[str, ...] = field(default_factory=tuple)

    @property
    def local_path(self) -> Path:
        return self.root / LOCAL_SETTINGS_NAME

    def workflow(self, name: str) -> Workflow:
        try:
            return self.workflows[name]
        except KeyError:
            known = ", ".join(sorted(self.workflows)) or "none"
            raise WorkflowError(
                f"workflow {name!r} is not configured in this repository",
                exit_code=Exit.MISSING_TOOL,
                detail=f"configured workflows: {known}",
                remediation=(
                    f"declare [workflow.{name}] in {SETTINGS_NAME}; "
                    "see docs/workflows.md"
                ),
            ) from None


def _table(value: Any, field_name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise WorkflowError(
            f"{field_name} must be a TOML table",
            exit_code=Exit.VALIDATION,
            remediation=f"correct {field_name} in {SETTINGS_NAME}",
        )
    return value


def _string(value: Any, field_name: str, default: str | None = None) -> str:
    if value is None and default is not None:
        return default
    if not isinstance(value, str) or not value.strip():
        raise WorkflowError(
            f"{field_name} must be a nonempty string",
            exit_code=Exit.VALIDATION,
            remediation=f"correct {field_name} in {SETTINGS_NAME}",
        )
    return value


def _string_list(value: Any, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise WorkflowError(
            f"{field_name} must be an array of nonempty strings",
            exit_code=Exit.VALIDATION,
            remediation=f"correct {field_name} in {SETTINGS_NAME}",
        )
    return tuple(value)


def _number(value: Any, field_name: str, default: float, minimum: float) -> float:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise WorkflowError(
            f"{field_name} must be a number",
            exit_code=Exit.VALIDATION,
            remediation=f"correct {field_name} in {SETTINGS_NAME}",
        )
    if not math.isfinite(float(value)):
        raise WorkflowError(
            f"{field_name} must be finite",
            exit_code=Exit.VALIDATION,
            remediation=f"correct {field_name} in {SETTINGS_NAME}",
        )
    if float(value) < minimum:
        raise WorkflowError(
            f"{field_name} must be at least {minimum:g}",
            exit_code=Exit.VALIDATION,
            remediation=f"correct {field_name} in {SETTINGS_NAME}",
        )
    return float(value)


def _resolve(root: Path, value: str) -> Path:
    candidate = Path(value).expanduser()
    return candidate if candidate.is_absolute() else (root / candidate)


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except OSError as error:
        raise WorkflowError(
            f"cannot read {path.name}",
            exit_code=Exit.INFRASTRUCTURE,
            detail=str(error),
            remediation=f"restore {path}",
        ) from error
    except tomllib.TOMLDecodeError as error:
        raise WorkflowError(
            f"{path.name} is not valid TOML",
            exit_code=Exit.VALIDATION,
            detail=str(error),
            remediation=f"fix the syntax error in {path}",
        ) from error


def _workflow(name: str, raw: dict[str, Any]) -> Workflow:
    field_name = f"workflow.{name}"
    kind = _string(raw.get("kind"), f"{field_name}.kind", "utility")
    if kind not in WORKFLOW_KINDS:
        raise WorkflowError(
            f"{field_name}.kind must be one of {', '.join(WORKFLOW_KINDS)}",
            exit_code=Exit.VALIDATION,
            remediation=f"correct {field_name}.kind in {SETTINGS_NAME}",
        )
    command = _string_list(raw.get("command"), f"{field_name}.command")
    if not command:
        raise WorkflowError(
            f"{field_name}.command must name the program to execute",
            exit_code=Exit.VALIDATION,
            remediation=(
                f"set {field_name}.command in {SETTINGS_NAME}, for example "
                '["leanevolve-run"]'
            ),
        )
    schedule = raw.get("schedule")
    specification: ScheduleSpec | None = None
    if schedule is not None:
        table = _table(schedule, f"{field_name}.schedule")
        style = _string(table.get("style"), f"{field_name}.schedule.style")
        if style not in SCHEDULE_STYLES:
            raise WorkflowError(
                f"{field_name}.schedule.style must be one of "
                f"{', '.join(SCHEDULE_STYLES)}",
                exit_code=Exit.VALIDATION,
                remediation=f"correct {field_name}.schedule.style in {SETTINGS_NAME}",
            )
        specification = ScheduleSpec(
            flag=_string(table.get("flag"), f"{field_name}.schedule.flag"),
            style=style,
        )
    return Workflow(
        name=name,
        summary=_string(raw.get("summary"), f"{field_name}.summary"),
        kind=kind,
        command=command,
        inputs=_string_list(raw.get("inputs"), f"{field_name}.inputs"),
        outputs=_string_list(raw.get("outputs"), f"{field_name}.outputs"),
        cost=_string(raw.get("cost"), f"{field_name}.cost", "no model spend"),
        runtime=_string(raw.get("runtime"), f"{field_name}.runtime", "unspecified"),
        example=_string(raw.get("example"), f"{field_name}.example", ""),
        requires=_string_list(raw.get("requires"), f"{field_name}.requires"),
        plan_flag=raw.get("plan_flag"),
        cost_flag=raw.get("cost_flag"),
        results_flag=raw.get("results_flag"),
        model_flag=raw.get("model_flag"),
        defaults=tuple(
            (key, str(value))
            for key, value in sorted(
                _table(raw.get("defaults", {}), f"{field_name}.defaults").items()
            )
        ),
        schedule=specification,
    )


def _lean_projects(root: Path, raw: Any) -> tuple[LeanProject, ...]:
    if raw is None:
        return ()
    lean = _table(raw, "lean")
    entries = lean.get("project", [])
    if not isinstance(entries, list):
        raise WorkflowError(
            "lean.project must be an array of tables",
            exit_code=Exit.VALIDATION,
            remediation=f"use [[lean.project]] entries in {SETTINGS_NAME}",
        )
    projects: list[LeanProject] = []
    for index, entry in enumerate(entries):
        table = _table(entry, f"lean.project[{index}]")
        path = _resolve(root, _string(table.get("path"), f"lean.project[{index}].path"))
        projects.append(
            LeanProject(
                path=path,
                audit_command=_string_list(
                    table.get("audit_command"), f"lean.project[{index}].audit_command"
                ),
            )
        )
    return tuple(projects)


def _command_list(raw: Any, field_name: str) -> tuple[tuple[str, ...], ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise WorkflowError(
            f"{field_name} must be an array of command arrays",
            exit_code=Exit.VALIDATION,
            remediation=(
                f'use {field_name} = [["ruff", "check", "."]] in {SETTINGS_NAME}'
            ),
        )
    commands: list[tuple[str, ...]] = []
    for index, entry in enumerate(raw):
        command = _string_list(entry, f"{field_name}[{index}]")
        if not command:
            raise WorkflowError(
                f"{field_name}[{index}] must name a program",
                exit_code=Exit.VALIDATION,
                remediation=f"correct {field_name} in {SETTINGS_NAME}",
            )
        commands.append(command)
    return tuple(commands)


def _demo(root: Path, raw: Any) -> DemoSpec | None:
    if raw is None:
        return None
    table = _table(raw, "demo")
    return DemoSpec(
        config=_resolve(root, _string(table.get("config"), "demo.config")),
        candidate=_resolve(root, _string(table.get("candidate"), "demo.candidate")),
        expect_goals=_string_list(table.get("expect_goals"), "demo.expect_goals"),
    )


def load_settings(root: Path | None = None) -> Settings:
    """Load ``leanevolve.toml`` and overlay ``leanevolve.local.toml``."""

    base = repository_root(root)
    raw = _read_toml(base / SETTINGS_NAME)
    if raw.get("format") != SETTINGS_FORMAT:
        raise WorkflowError(
            f"{SETTINGS_NAME} must declare format = {SETTINGS_FORMAT!r}",
            exit_code=Exit.VALIDATION,
            remediation=f"add the format key to {base / SETTINGS_NAME}",
        )
    local_path = base / LOCAL_SETTINGS_NAME
    local = _read_toml(local_path) if local_path.is_file() else {}
    overrides: list[str] = []

    def merged(section: str, key: str, default: Any = None) -> Any:
        local_section = _table(local.get(section, {}), f"{section} (local)")
        if key in local_section:
            overrides.append(f"{section}.{key}")
            return local_section[key]
        return _table(raw.get(section, {}), section).get(key, default)

    storage_root = _string(merged("storage", "artifact_root"), "storage.artifact_root")
    cache_root = _string(
        merged("storage", "cache_root", ".cache/leanevolve"), "storage.cache_root"
    )
    require_external = merged("storage", "require_external", False)
    if not isinstance(require_external, bool):
        raise WorkflowError(
            "storage.require_external must be true or false",
            exit_code=Exit.VALIDATION,
            remediation=f"correct storage.require_external in {SETTINGS_NAME}",
        )
    workflows_raw = _table(raw.get("workflow", {}), "workflow")
    workflows = {
        name: _workflow(name, _table(entry, f"workflow.{name}"))
        for name, entry in sorted(workflows_raw.items())
    }
    model_route = merged("model", "route")
    ledger_database = merged("ledger", "database")
    ledger_artifacts = merged("ledger", "artifacts")
    ledger_required = _string_list(
        _table(raw.get("ledger", {}), "ledger").get("required_workflows"),
        "ledger.required_workflows",
    )
    if (ledger_database is None) != (ledger_artifacts is None):
        raise WorkflowError(
            "ledger.database and ledger.artifacts must be configured together",
            exit_code=Exit.VALIDATION,
            remediation=(
                f"set both values in {LOCAL_SETTINGS_NAME} with "
                "`leanevolve configure --ledger-database ... "
                "--ledger-artifacts ...`"
            ),
        )
    return Settings(
        root=base,
        claim=_string(
            _table(raw.get("project", {}), "project").get("claim"),
            "project.claim",
            "no mathematical claim is made by this repository",
        ),
        storage=Storage(
            artifact_root=_resolve(base, storage_root),
            cache_root=_resolve(base, cache_root),
            min_free_gb=_number(
                merged("storage", "min_free_gb", 1.0), "storage.min_free_gb", 1.0, 0.0
            ),
            require_external=require_external,
        ),
        limits=Limits(
            max_api_costs=_number(
                merged("limits", "max_api_costs", 5.0), "limits.max_api_costs", 5.0, 0.0
            ),
            max_parallel_jobs=int(
                _number(
                    merged("limits", "max_parallel_jobs", 1),
                    "limits.max_parallel_jobs",
                    1.0,
                    1.0,
                )
            ),
        ),
        ledger=LedgerConfig(
            database=(
                None
                if ledger_database is None
                else _resolve(base, _string(ledger_database, "ledger.database"))
            ),
            artifacts=(
                None
                if ledger_artifacts is None
                else _resolve(base, _string(ledger_artifacts, "ledger.artifacts"))
            ),
            required_workflows=ledger_required,
        ),
        model_route=None
        if model_route is None
        else _string(model_route, "model.route"),
        model_auth_env=_string_list(
            _table(raw.get("model", {}), "model").get("auth_env"), "model.auth_env"
        ),
        required_tools=_string_list(
            _table(raw.get("tools", {}), "tools").get("required"), "tools.required"
        ),
        optional_tools=_string_list(
            _table(raw.get("tools", {}), "tools").get("optional"), "tools.optional"
        ),
        required_inputs=_string_list(
            _table(raw.get("inputs", {}), "inputs").get("required"), "inputs.required"
        ),
        lean_projects=_lean_projects(base, raw.get("lean")),
        fast_checks=_command_list(
            _table(raw.get("checks", {}), "checks").get("fast"), "checks.fast"
        ),
        audit_checks=_command_list(
            _table(raw.get("checks", {}), "checks").get("audit"), "checks.audit"
        ),
        demo=_demo(base, raw.get("demo")),
        workflows=workflows,
        local_overrides=tuple(sorted(overrides)),
    )


def _toml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    text = str(value)
    if "\\" in text or '"' in text:
        raise WorkflowError(
            "local settings values must not contain quotes or backslashes",
            exit_code=Exit.USAGE,
            remediation=f"edit {LOCAL_SETTINGS_NAME} by hand for this value",
        )
    return f'"{text}"'


def write_local_settings(root: Path, sections: dict[str, dict[str, Any]]) -> Path:
    """Write machine-local overrides, preserving keys that are not being set."""

    path = root / LOCAL_SETTINGS_NAME
    existing = _read_toml(path) if path.is_file() else {}
    for section, values in sections.items():
        current = _table(existing.get(section, {}), f"{section} (local)")
        current.update(
            {key: value for key, value in values.items() if value is not None}
        )
        if current:
            existing[section] = current
    lines = [
        "# Machine-local LeanEvolve overrides. Not version controlled.",
        f"# Written by `leanevolve configure`; defaults live in {SETTINGS_NAME}.",
        "",
    ]
    for section in sorted(existing):
        lines.append(f"[{section}]")
        for key in sorted(existing[section]):
            lines.append(f"{key} = {_toml_scalar(existing[section][key])}")
        lines.append("")
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return path
