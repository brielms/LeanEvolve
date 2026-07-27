"""Launch a sequential ShinkaEvolve campaign scored by Lean."""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
from pathlib import Path

from leanevolve.audit import (
    append_event,
    create_run_manifest,
    finalize_run_manifest,
)
from leanevolve.config import SearchConfig, load_config
from leanevolve.evaluate import __file__ as evaluator_file
from leanevolve.kernel import project_sha256
from leanevolve.lineage import LINEAGE_NAME, write_lineage
from leanevolve.shinka_runtime import (
    enable_lean_language,
    install_atomic_best_snapshot,
)

PINNED_SHINKA_COMMIT = "b67a07328ab7e21e999d9e20a44f4f0054a4b83c"
MAX_MODEL = "headless/codex@gpt-5.6-sol?effort=max"


def _duration(seconds: int) -> str:
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def _build_project(config: SearchConfig) -> None:
    completed = subprocess.run(["lake", "build"], cwd=config.lean_project, check=False)
    if completed.returncode:
        raise SystemExit("Lean project build failed")


def _render_prompt(config: SearchConfig) -> str:
    lines = [
        config.prompt.read_text(encoding="utf-8").rstrip(),
        "",
        "# Active kernel-scored goals",
        "",
    ]
    for goal in config.goals:
        dependencies = ", ".join(goal.depends_on) or "none"
        lines.extend(
            [
                f"- `{goal.name}` ({goal.weight:g} points)",
                f"  declaration: `{goal.declaration}`",
                f"  target type: `{goal.target_type}`",
                f"  prerequisites: {dependencies}",
                f"  note: {goal.description or 'no additional note'}",
            ]
        )
    lines.extend(
        [
            "",
            "The parent candidate is the current verified frontier. Preserve its",
            "accepted declarations and make one attainable formal advance at a time.",
            "Only Lean kernel acceptance changes fitness.",
        ]
    )
    return "\n".join(lines) + "\n"


def _configure_max_bridge(model: str) -> str | None:
    if model != MAX_MODEL:
        return None
    from shinka.llm.providers import headless

    headless._VALID_EFFORTS.add("max")
    bridge = Path(__file__).with_name("codex_bridge.py")
    command = shlex.join([sys.executable, str(bridge)])
    existing = os.environ.get("SHINKA_HEADLESS_COMMAND")
    if existing is not None and existing != command:
        raise SystemExit("SHINKA_HEADLESS_COMMAND conflicts with the max bridge")
    os.environ["SHINKA_HEADLESS_COMMAND"] = command
    return command


def _build_runtime(
    config: SearchConfig,
    results_dir: Path,
    model: str,
    proposal_steps: int,
    max_api_costs: float,
    headless_timeout: int,
):
    try:
        from shinka.core import EvolutionConfig
        from shinka.database import DatabaseConfig
        from shinka.launch import LocalJobConfig
    except ImportError as error:
        raise SystemExit("install the optional 'shinka' dependency") from error
    enable_lean_language()
    headless_command = _configure_max_bridge(model)
    os.environ["SHINKA_HEADLESS_TIMEOUT"] = str(headless_timeout)
    evolution = EvolutionConfig(
        task_sys_msg=_render_prompt(config),
        patch_types=["diff", "full"],
        patch_type_probs=[0.8, 0.2],
        num_generations=proposal_steps + 1,
        max_patch_resamples=1,
        max_patch_attempts=1,
        job_type="local",
        language="lean",
        llm_models=[model],
        llm_dynamic_selection=None,
        meta_rec_interval=None,
        embedding_model=None,
        init_program_path=str(config.seed),
        results_dir=str(results_dir),
        max_novelty_attempts=1,
        use_text_feedback=True,
        inspiration_sort_order="chronological",
        max_api_costs=max_api_costs,
        evolve_prompts=False,
        enable_controlled_oversubscription=False,
        proposal_buffer_max=1,
    )
    database = DatabaseConfig(
        num_islands=1,
        archive_size=100,
        max_stdout_log_chars=20_000,
        num_archive_inspirations=1,
        num_top_k_inspirations=1,
        migration_interval=10_000,
        migration_rate=0.0,
        enable_dynamic_islands=False,
        parent_selection_strategy="sequential",
    )
    job = LocalJobConfig(
        eval_program_path=str(Path(evaluator_file).resolve()),
        extra_cmd_args={
            "config": str(config.path),
            "expected-project-sha256": project_sha256(config),
        },
        eval_verbose=False,
        numeric_threads_per_job=1,
        time=_duration(headless_timeout + config.kernel.timeout_seconds + 60),
        python_executable=sys.executable,
    )
    return evolution, database, job, headless_command


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--results-dir", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--proposal-steps", type=int, default=3)
    parser.add_argument("--max-api-costs", type=float, default=5.0)
    parser.add_argument("--headless-timeout", type=int, default=1200)
    parser.add_argument("--check-config", action="store_true")
    args = parser.parse_args()
    if not 0 <= args.proposal_steps <= 99:
        raise SystemExit("--proposal-steps must be in 0..99")
    if args.max_api_costs < 0 or (args.max_api_costs == 0 and not args.check_config):
        raise SystemExit(
            "--max-api-costs must be positive for a run; zero is reserved "
            "for --check-config"
        )
    if not 1 <= args.headless_timeout <= 7200:
        raise SystemExit("--headless-timeout must be in 1..7200")
    config = load_config(args.config)
    results_dir = args.results_dir.resolve()
    _build_project(config)
    evolution, database, job, headless_command = _build_runtime(
        config,
        results_dir,
        args.model,
        args.proposal_steps,
        args.max_api_costs,
        args.headless_timeout,
    )
    if args.check_config:
        print("configuration valid")
        print(f"goals={len(config.goals)}")
        print(f"proposal_steps={args.proposal_steps}")
        print(f"formal_inputs_sha256={project_sha256(config)}")
        return
    create_run_manifest(
        results_dir,
        config.root,
        config.portable_configuration(),
        config.input_files(),
        {
            "config_relative_path": config.path.relative_to(config.root).as_posix(),
            "model": args.model,
            "proposal_steps": args.proposal_steps,
            "max_api_costs": args.max_api_costs,
            "headless_timeout_seconds": args.headless_timeout,
            "headless_command": headless_command,
            "formal_inputs_sha256": project_sha256(config),
            "parent_selection_strategy": "sequential",
            "inspiration_sort_order": "chronological",
            "shinka_upstream_commit": PINNED_SHINKA_COMMIT,
        },
        tool_inputs=sorted(Path(__file__).resolve().parent.glob("*.py")),
    )
    status = "completed"
    failure_type: str | None = None
    try:
        from shinka.core import ShinkaEvolveRunner

        runner = ShinkaEvolveRunner(
            evo_config=evolution,
            job_config=job,
            db_config=database,
            max_evaluation_jobs=1,
            max_proposal_jobs=1,
            max_db_workers=1,
            verbose=True,
        )
        install_atomic_best_snapshot(runner, results_dir)
        runner.run()
    except BaseException as error:
        status = "interrupted" if isinstance(error, KeyboardInterrupt) else "failed"
        failure_type = type(error).__name__
        raise
    finally:
        try:
            lineage = write_lineage(results_dir)
            if lineage is not None:
                append_event(
                    results_dir,
                    "proof_lineage_recorded",
                    {
                        "path": LINEAGE_NAME,
                        "lineage_sha256": lineage["lineage_sha256"],
                        "lineage_complete": lineage["lineage_complete"],
                        "frontier_accepted_goals": lineage["frontier_accepted_goals"],
                    },
                )
        finally:
            finalize_run_manifest(results_dir, status, failure_type)


if __name__ == "__main__":
    main()
