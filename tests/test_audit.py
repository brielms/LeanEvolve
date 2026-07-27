from __future__ import annotations

from pathlib import Path

from leanevolve.audit import (
    RUN_MANIFEST,
    create_run_manifest,
    finalize_run_manifest,
    verify_event_chain,
    verify_inventory,
)


def test_manifest_detects_event_and_result_tampering(tmp_path: Path) -> None:
    root = tmp_path / "inputs"
    root.mkdir()
    source = root / "input.txt"
    source.write_text("pinned input\n", encoding="utf-8")
    run_dir = tmp_path / "run"
    create_run_manifest(
        run_dir,
        root,
        {"format": "test"},
        [source],
        {"model": "test"},
    )
    output = run_dir / "result.txt"
    output.write_text("accepted\n", encoding="utf-8")
    manifest = finalize_run_manifest(run_dir, "completed")
    assert not verify_event_chain(run_dir / "events.jsonl")
    assert not verify_inventory(run_dir, manifest["result_files"])
    output.write_text("rewritten\n", encoding="utf-8")
    errors = verify_inventory(run_dir, manifest["result_files"])
    assert "hash/size mismatch: result.txt" in errors
    assert (run_dir / RUN_MANIFEST).is_file()
