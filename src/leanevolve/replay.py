"""Verify a finalized run and reevaluate every recorded candidate."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from leanevolve.audit import (
    EVENTS,
    RUN_MANIFEST,
    file_record,
    verify_event_chain,
    verify_inventory,
)
from leanevolve.config import load_config
from leanevolve.evaluate import evaluate
from leanevolve.lineage import verify_lineage


def replay(run_dir: Path) -> list[str]:
    errors: list[str] = []
    manifest_path = run_dir / RUN_MANIFEST
    if not manifest_path.is_file():
        return ["missing run manifest"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    errors.extend(verify_event_chain(run_dir / EVENTS))
    if manifest.get("result_files"):
        errors.extend(verify_inventory(run_dir, manifest["result_files"]))
    errors.extend(verify_lineage(run_dir))
    expected_tools = manifest.get("tool_inputs", {})
    if expected_tools:
        package_root = Path(__file__).resolve().parent
        actual_tools = {
            f"leanevolve/{path.name}": file_record(path)
            for path in sorted(package_root.glob("*.py"))
        }
        if actual_tools != expected_tools:
            errors.append(
                "installed LeanEvolve sources differ from the recorded tool snapshot"
            )
    if errors:
        return errors
    config_relative = manifest.get("run_parameters", {}).get("config_relative_path")
    if not isinstance(config_relative, str):
        return ["manifest lacks config_relative_path"]
    with tempfile.TemporaryDirectory(prefix="leanevolve-replay-") as temporary:
        replay_root = Path(temporary) / "input_snapshot"
        shutil.copytree(run_dir / "input_snapshot", replay_root)
        config = load_config(replay_root / config_relative)
        build = subprocess.run(
            ["lake", "build"],
            cwd=config.lean_project,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        if build.returncode:
            return [
                "snapshotted Lean project failed to build:\n" + build.stdout[-4000:]
            ]
        generation_dirs = sorted(
            (
                path
                for path in run_dir.glob("gen_*")
                if path.is_dir() and path.name[4:].isdigit()
            ),
            key=lambda path: int(path.name[4:]),
        )
        for generation_dir in generation_dirs:
            candidate = generation_dir / "main.lean"
            receipt_path = generation_dir / "results" / "evaluation_manifest.json"
            if not candidate.is_file() or not receipt_path.is_file():
                continue
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            if file_record(candidate) != receipt.get("candidate"):
                errors.append(f"candidate receipt mismatch: {generation_dir.name}")
                continue
            evaluation_dir = Path(temporary) / (generation_dir.name + "_results")
            fresh = evaluate(candidate, config, evaluation_dir)
            expected = tuple(receipt.get("accepted_goals", []))
            if fresh.accepted_goals != expected:
                errors.append(
                    f"accepted goals differ for {generation_dir.name}: "
                    f"expected {list(expected)}, found {list(fresh.accepted_goals)}"
                )
    return errors


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args()
    errors = replay(args.run_dir.resolve())
    if errors:
        for error in errors:
            print("ERROR:", error)
        raise SystemExit(1)
    print("Replay accepted: inventory, lineage, and kernel results agree.")


if __name__ == "__main__":
    main()
