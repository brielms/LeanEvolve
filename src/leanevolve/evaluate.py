"""Shinka-compatible command-line evaluator for Lean candidates."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from leanevolve.audit import EVALUATION_FORMAT, file_record, utc_now
from leanevolve.config import SearchConfig, load_config
from leanevolve.kernel import project_records, project_sha256, run_kernel


@dataclass(frozen=True)
class Evaluation:
    correct: bool
    accepted_goals: tuple[str, ...]
    metrics: dict[str, Any]
    feedback: str
    kernel_returncode: int | None
    goal_axioms: dict[str, tuple[str, ...]]
    command_display: tuple[str, ...]


def _rejected(message: str, source_bytes: int = 0) -> Evaluation:
    feedback = "REJECTED BEFORE KERNEL ACCEPTANCE: " + message
    return Evaluation(
        correct=False,
        accepted_goals=(),
        metrics={
            "combined_score": 0.0,
            "public": {
                "closed_goal_count": 0.0,
                "closed_goal_weight": 0.0,
                "kernel_accepted": 0.0,
            },
            "private": {"source_bytes": float(source_bytes), "elapsed_seconds": 0.0},
            "text_feedback": feedback,
        },
        feedback=feedback,
        kernel_returncode=None,
        goal_axioms={},
        command_display=(),
    )


def evaluate(
    candidate: Path,
    config: SearchConfig,
    results_dir: Path,
    expected_project_sha256: str | None = None,
) -> Evaluation:
    try:
        if not candidate.is_file() or candidate.is_symlink():
            raise ValueError("candidate must be a regular, non-symlink file")
        actual_project_sha256 = project_sha256(config)
        if (
            expected_project_sha256 is not None
            and actual_project_sha256 != expected_project_sha256
        ):
            raise ValueError(
                "formal project changed after run start: expected "
                f"{expected_project_sha256}, found {actual_project_sha256}"
            )
        source = candidate.read_text(encoding="utf-8")
        result = run_kernel(config, source, results_dir)
    except (OSError, UnicodeError, ValueError) as error:
        size = candidate.stat().st_size if candidate.exists() else 0
        return _rejected(str(error), size)

    accepted = set(result.accepted_goals)
    score = sum(goal.weight for goal in config.goals if goal.name in accepted)
    feedback = (
        "KERNEL-ACCEPTED GOALS: "
        + (", ".join(result.accepted_goals) if result.accepted_goals else "none")
        + ". Only listed declarations receive fitness credit."
        + "\nAXIOM POLICY: "
        + (
            ", ".join(sorted(config.kernel.allowed_axioms))
            if config.kernel.allowed_axioms
            else "no axioms allowed"
        )
    )
    if result.output:
        feedback += "\n" + result.output
    return Evaluation(
        correct=bool(result.accepted_goals),
        accepted_goals=result.accepted_goals,
        metrics={
            "combined_score": score,
            "public": {
                "closed_goal_count": float(len(result.accepted_goals)),
                "closed_goal_weight": score,
                "kernel_accepted": 1.0 if result.accepted_goals else 0.0,
            },
            "private": {
                "source_bytes": float(len(source.encode("utf-8"))),
                "source_limit_bytes": float(config.max_source_bytes),
                "elapsed_seconds": result.elapsed_seconds,
            },
            "text_feedback": feedback,
        },
        feedback=feedback,
        kernel_returncode=result.returncode,
        goal_axioms=result.goal_axioms,
        command_display=result.command_display,
    )


def write_results(
    results_dir: Path,
    evaluation: Evaluation,
    candidate: Path,
    config: SearchConfig,
) -> None:
    results_dir.mkdir(parents=True, exist_ok=True)
    (results_dir / "correct.json").write_text(
        json.dumps({"correct": evaluation.correct}, indent=2) + "\n"
    )
    (results_dir / "metrics.json").write_text(
        json.dumps(evaluation.metrics, indent=2, sort_keys=True) + "\n"
    )
    (results_dir / "feedback.txt").write_text(
        evaluation.feedback.rstrip() + "\n", encoding="utf-8"
    )
    manifest = {
        "format": EVALUATION_FORMAT,
        "recorded_at_utc": utc_now(),
        "candidate": file_record(candidate),
        "configuration": file_record(config.path),
        "formal_inputs": project_records(config),
        "formal_inputs_sha256": project_sha256(config),
        "lean_toolchain": (config.lean_project / "lean-toolchain")
        .read_text(encoding="utf-8")
        .strip(),
        "allowed_axioms": sorted(config.kernel.allowed_axioms),
        "sandbox_prefix": list(config.kernel.sandbox_prefix),
        "candidate_source_policy": {
            "max_source_bytes": config.max_source_bytes,
            "evolution_markers_required": True,
            "placeholder_scan": True,
        },
        "accepted_goals": list(evaluation.accepted_goals),
        "goal_axioms": {
            name: list(values) for name, values in evaluation.goal_axioms.items()
        },
        "correct": evaluation.correct,
        "kernel_returncode": evaluation.kernel_returncode,
        "kernel_command": list(evaluation.command_display),
        "metrics": evaluation.metrics,
    }
    (results_dir / "evaluation_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--program_path", type=Path, required=True)
    parser.add_argument("--results_dir", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--expected-project-sha256")
    args = parser.parse_args()
    config = load_config(args.config)
    candidate = args.program_path.resolve()
    results_dir = args.results_dir.resolve()
    evaluation = evaluate(
        candidate,
        config,
        results_dir,
        args.expected_project_sha256,
    )
    write_results(results_dir, evaluation, candidate, config)


if __name__ == "__main__":
    main()
