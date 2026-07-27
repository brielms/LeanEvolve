"""Small compatibility adaptations around the pinned ShinkaEvolve runtime."""

from __future__ import annotations

import asyncio
import re
import shutil
import types
import uuid
from pathlib import Path


def enable_lean_language() -> None:
    """Register Lean source conventions in Shinka's language tables."""

    from shinka.utils import languages

    languages._LANGUAGE_ALIASES["lean4"] = "lean"
    languages._LANGUAGE_EXTENSIONS["lean"] = "lean"
    languages._EVOLVE_COMMENT_PREFIXES["lean"] = "--"
    languages._LANGUAGE_FENCE_TAGS["lean"] = ("lean", "lean4")
    languages._EVOLVE_MARKER_PATTERNS["lean"] = (
        re.compile(r"^\s*--\s*EVOLVE-BLOCK-START\s*$"),
        re.compile(r"^\s*--\s*EVOLVE-BLOCK-END\s*$"),
    )
    languages._EVOLVE_MARKER_EXAMPLES["lean"] = (
        "-- EVOLVE-BLOCK-START",
        "-- EVOLVE-BLOCK-END",
    )


def atomic_refresh_best_snapshot(source: Path, destination: Path) -> None:
    """Replace Shinka's convenience `best` directory with atomic staging."""

    staging = destination.parent / f".best.next-{uuid.uuid4().hex}"
    previous = destination.parent / f".best.previous-{uuid.uuid4().hex}"
    shutil.copytree(source, staging)
    try:
        if destination.exists():
            destination.rename(previous)
        staging.rename(destination)
        if previous.exists():
            shutil.rmtree(previous)
    except BaseException:
        if not destination.exists() and previous.exists():
            previous.rename(destination)
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def install_atomic_best_snapshot(runner: object, results_dir: Path) -> None:
    """Patch the pinned runtime's race-prone best-directory refresh."""

    async def update_best(instance: object) -> None:
        async_db = getattr(instance, "async_db", None)
        if async_db is None:
            return
        lock = getattr(instance, "_leanevolve_best_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            setattr(instance, "_leanevolve_best_lock", lock)
        async with lock:
            programs = await async_db.get_top_programs_async(n=1, correct_only=True)
            if not programs:
                return
            best = programs[0]
            if best.id == getattr(instance, "best_program_id", None):
                return
            source = results_dir / f"gen_{best.generation}"
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None,
                atomic_refresh_best_snapshot,
                source,
                results_dir / "best",
            )
            setattr(instance, "best_program_id", best.id)

    setattr(
        runner,
        "_update_best_solution_async",
        types.MethodType(update_best, runner),
    )
