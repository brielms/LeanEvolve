from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from leanevolve.config import load_config
from leanevolve.evaluate import evaluate

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "examples" / "demo"


@pytest.fixture(scope="module")
def lean_config():
    if shutil.which("lake") is None:
        pytest.skip("lake is unavailable")
    completed = subprocess.run(
        ["lake", "build"],
        cwd=DEMO / "lean",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    if completed.returncode:
        pytest.fail(completed.stdout)
    return load_config(DEMO / "evolve.json")


def test_seed_closes_exactly_one_kernel_goal(tmp_path: Path, lean_config) -> None:
    result = evaluate(DEMO / "seed.lean", lean_config, tmp_path / "results")
    assert result.correct
    assert result.accepted_goals == ("zero_right",)
    assert set(result.goal_axioms["zero_right"]) <= lean_config.kernel.allowed_axioms


def test_formal_extension_closes_both_goals(tmp_path: Path, lean_config) -> None:
    candidate = tmp_path / "candidate.lean"
    candidate.write_text(
        (DEMO / "seed.lean")
        .read_text(encoding="utf-8")
        .replace(
            "-- The next configured declaration is intentionally absent.",
            "theorem addition_commutes : Demo.AdditionCommutesTarget := by\n"
            "  intro a b\n"
            "  exact Nat.add_comm a b\n\n"
            "-- The next configured declaration is intentionally absent.",
        ),
        encoding="utf-8",
    )
    result = evaluate(candidate, lean_config, tmp_path / "results")
    assert result.accepted_goals == ("zero_right", "addition_commutes")


def test_placeholder_never_reaches_kernel_credit(tmp_path: Path, lean_config) -> None:
    candidate = tmp_path / "candidate.lean"
    candidate.write_text(
        (DEMO / "seed.lean").read_text(encoding="utf-8").replace("simp", "sorry"),
        encoding="utf-8",
    )
    result = evaluate(candidate, lean_config, tmp_path / "results")
    assert not result.correct
    assert result.accepted_goals == ()
    assert "source policy" in result.feedback
