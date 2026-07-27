"""Portable, validated configuration for a LeanEvolve search."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CONFIG_FORMAT = "leanevolve-config-v1"
_LEAN_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_']*(?:\.[A-Za-z_][A-Za-z0-9_']*)*$")
_GOAL_NAME = re.compile(r"^[a-z][a-z0-9_]{0,79}$")


@dataclass(frozen=True)
class Goal:
    """One independently audited declaration on the fitness ladder."""

    name: str
    declaration: str
    target_type: str
    weight: float
    depends_on: tuple[str, ...]
    description: str


@dataclass(frozen=True)
class KernelPolicy:
    """Lean execution and declaration-dependency policy."""

    allowed_axioms: frozenset[str]
    timeout_seconds: int
    warning_as_error: bool
    sandbox_prefix: tuple[str, ...]


@dataclass(frozen=True)
class SearchConfig:
    """A resolved configuration with paths anchored at its JSON file."""

    path: Path
    raw: dict[str, Any]
    lean_project: Path
    seed: Path
    prompt: Path
    max_source_bytes: int
    kernel: KernelPolicy
    goals: tuple[Goal, ...]

    @property
    def root(self) -> Path:
        return self.path.parent

    def project_files(self) -> tuple[Path, ...]:
        """Return formal inputs, excluding build products and editor metadata."""

        fixed = [
            self.lean_project / "lakefile.toml",
            self.lean_project / "lakefile.lean",
            self.lean_project / "lake-manifest.json",
            self.lean_project / "lean-toolchain",
        ]
        paths = {item.resolve() for item in fixed if item.is_file()}
        paths.update(
            item.resolve()
            for item in self.lean_project.rglob("*.lean")
            if ".lake" not in item.parts and not item.name.startswith("._")
        )
        return tuple(sorted(paths))

    def input_files(self) -> tuple[Path, ...]:
        return tuple(
            sorted(
                {
                    self.path.resolve(),
                    self.seed.resolve(),
                    self.prompt.resolve(),
                    *self.project_files(),
                }
            )
        )

    def portable_configuration(self) -> dict[str, Any]:
        """Return path-independent settings suitable for a run manifest."""

        return {
            "format": CONFIG_FORMAT,
            "lean_project": str(self.lean_project.relative_to(self.root)),
            "seed": str(self.seed.relative_to(self.root)),
            "prompt": str(self.prompt.relative_to(self.root)),
            "candidate": {"max_bytes": self.max_source_bytes},
            "kernel": {
                "allowed_axioms": sorted(self.kernel.allowed_axioms),
                "timeout_seconds": self.kernel.timeout_seconds,
                "warning_as_error": self.kernel.warning_as_error,
                "sandbox_prefix": list(self.kernel.sandbox_prefix),
            },
            "goals": [
                {
                    "name": goal.name,
                    "declaration": goal.declaration,
                    "target_type": goal.target_type,
                    "weight": goal.weight,
                    "depends_on": list(goal.depends_on),
                    "description": goal.description,
                }
                for goal in self.goals
            ],
        }


def _object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be a JSON object")
    return value


def _path(root: Path, value: Any, field: str, directory: bool) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a nonempty relative path")
    candidate = Path(value)
    if candidate.is_absolute():
        raise ValueError(f"{field} must be relative to the configuration")
    resolved = (root / candidate).resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError(f"{field} escapes the configuration directory")
    if directory and not resolved.is_dir():
        raise ValueError(f"{field} is not a directory: {value}")
    if not directory and not resolved.is_file():
        raise ValueError(f"{field} is not a file: {value}")
    return resolved


def _positive_int(value: Any, field: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer")
    if not 1 <= value <= maximum:
        raise ValueError(f"{field} must be in 1..{maximum}")
    return value


def _goals(value: Any) -> tuple[Goal, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError("goals must be a nonempty JSON array")
    goals: list[Goal] = []
    seen: set[str] = set()
    for index, raw_value in enumerate(value):
        raw = _object(raw_value, f"goals[{index}]")
        name = raw.get("name")
        declaration = raw.get("declaration")
        target_type = raw.get("target_type")
        description = raw.get("description", "")
        weight = raw.get("weight")
        dependencies = raw.get("depends_on", [])
        if not isinstance(name, str) or not _GOAL_NAME.fullmatch(name):
            raise ValueError(f"invalid goals[{index}].name")
        if name in seen:
            raise ValueError(f"duplicate goal name: {name}")
        if not isinstance(declaration, str) or not _LEAN_NAME.fullmatch(declaration):
            raise ValueError(f"invalid declaration for goal {name}")
        if not isinstance(target_type, str) or not target_type.strip():
            raise ValueError(f"target_type is required for goal {name}")
        if isinstance(weight, bool) or not isinstance(weight, (int, float)):
            raise ValueError(f"weight must be numeric for goal {name}")
        if not 0 < float(weight) <= 1_000_000:
            raise ValueError(f"weight is out of range for goal {name}")
        if not isinstance(dependencies, list) or not all(
            isinstance(item, str) for item in dependencies
        ):
            raise ValueError(f"depends_on must be a string array for {name}")
        if len(set(dependencies)) != len(dependencies):
            raise ValueError(f"duplicate dependency for goal {name}")
        unknown = set(dependencies) - seen
        if unknown:
            raise ValueError(
                f"goal {name} has unknown or forward dependencies: "
                + ", ".join(sorted(unknown))
            )
        if not isinstance(description, str):
            raise ValueError(f"description must be text for goal {name}")
        goals.append(
            Goal(
                name=name,
                declaration=declaration,
                target_type=target_type.strip(),
                weight=float(weight),
                depends_on=tuple(dependencies),
                description=description.strip(),
            )
        )
        seen.add(name)
    return tuple(goals)


def load_config(path: Path) -> SearchConfig:
    """Read and validate a portable LeanEvolve JSON configuration."""

    resolved = path.resolve()
    raw = _object(json.loads(resolved.read_text(encoding="utf-8")), "root")
    if raw.get("format") != CONFIG_FORMAT:
        raise ValueError(f"format must be {CONFIG_FORMAT!r}")
    root = resolved.parent
    candidate = _object(raw.get("candidate", {}), "candidate")
    kernel = _object(raw.get("kernel", {}), "kernel")
    axioms = kernel.get("allowed_axioms", [])
    if not isinstance(axioms, list) or not all(
        isinstance(item, str) and _LEAN_NAME.fullmatch(item) for item in axioms
    ):
        raise ValueError("kernel.allowed_axioms must contain Lean names")
    if len(axioms) != len(set(axioms)):
        raise ValueError("kernel.allowed_axioms contains duplicates")
    sandbox_prefix = kernel.get("sandbox_prefix", [])
    if not isinstance(sandbox_prefix, list) or not all(
        isinstance(item, str) and item for item in sandbox_prefix
    ):
        raise ValueError("kernel.sandbox_prefix must be a string array")
    warning_as_error = kernel.get("warning_as_error", True)
    if not isinstance(warning_as_error, bool):
        raise ValueError("kernel.warning_as_error must be boolean")
    config = SearchConfig(
        path=resolved,
        raw=raw,
        lean_project=_path(root, raw.get("lean_project"), "lean_project", True),
        seed=_path(root, raw.get("seed"), "seed", False),
        prompt=_path(root, raw.get("prompt"), "prompt", False),
        max_source_bytes=_positive_int(
            candidate.get("max_bytes", 8 * 1024 * 1024),
            "candidate.max_bytes",
            32 * 1024 * 1024,
        ),
        kernel=KernelPolicy(
            allowed_axioms=frozenset(axioms),
            timeout_seconds=_positive_int(
                kernel.get("timeout_seconds", 120),
                "kernel.timeout_seconds",
                3600,
            ),
            warning_as_error=warning_as_error,
            sandbox_prefix=tuple(sandbox_prefix),
        ),
        goals=_goals(raw.get("goals")),
    )
    if not (config.lean_project / "lean-toolchain").is_file():
        raise ValueError("lean_project must pin a lean-toolchain")
    if not any(
        (config.lean_project / name).is_file()
        for name in ("lakefile.toml", "lakefile.lean")
    ):
        raise ValueError("lean_project must contain a Lake project file")
    if not config.project_files():
        raise ValueError("lean_project contains no formal source inputs")
    return config
