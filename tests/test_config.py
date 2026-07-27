from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from leanevolve.config import load_config

ROOT = Path(__file__).resolve().parents[1]
DEMO_CONFIG = ROOT / "examples" / "demo" / "evolve.json"


def test_demo_configuration_is_portable_and_dependency_ordered() -> None:
    config = load_config(DEMO_CONFIG)
    assert [goal.name for goal in config.goals] == [
        "zero_right",
        "addition_commutes",
    ]
    assert config.goals[1].depends_on == ("zero_right",)
    assert not Path(config.portable_configuration()["lean_project"]).is_absolute()
    assert config.project_files()


def test_forward_dependency_is_rejected(tmp_path: Path) -> None:
    payload = json.loads(DEMO_CONFIG.read_text(encoding="utf-8"))
    payload["goals"][0]["depends_on"] = ["addition_commutes"]
    copied = tmp_path / "demo"
    shutil.copytree(DEMO_CONFIG.parent, copied, ignore=shutil.ignore_patterns(".lake"))
    config_path = copied / "evolve.json"
    config_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="forward dependencies"):
        load_config(config_path)
