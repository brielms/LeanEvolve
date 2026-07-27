from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from leanevolve.audit import file_record
from leanevolve.lineage import verify_lineage, write_lineage


def _add_program(
    database: sqlite3.Connection,
    run_dir: Path,
    program_id: str,
    generation: int,
    parent_id: str | None,
    goals: list[str],
) -> None:
    generation_dir = run_dir / f"gen_{generation}"
    results = generation_dir / "results"
    results.mkdir(parents=True)
    candidate = generation_dir / "main.lean"
    candidate.write_text(f"-- generation {generation}\n", encoding="utf-8")
    receipt = {
        "candidate": file_record(candidate),
        "accepted_goals": goals,
    }
    (results / "evaluation_manifest.json").write_text(
        json.dumps(receipt), encoding="utf-8"
    )
    database.execute(
        "INSERT INTO programs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            program_id,
            candidate.read_text(encoding="utf-8"),
            parent_id,
            generation,
            float(generation),
            float(len(goals)),
            True,
            json.dumps({"model": "test"}),
        ),
    )
    database.commit()


def test_parent_path_and_receipts_are_hash_bound(tmp_path: Path) -> None:
    database = sqlite3.connect(tmp_path / "programs.sqlite")
    database.execute(
        """
        CREATE TABLE programs (
            id TEXT PRIMARY KEY,
            code TEXT NOT NULL,
            parent_id TEXT,
            generation INTEGER NOT NULL,
            timestamp REAL NOT NULL,
            combined_score REAL,
            correct BOOLEAN,
            metadata TEXT
        )
        """
    )
    _add_program(database, tmp_path, "seed", 0, None, ["base"])
    _add_program(database, tmp_path, "advance", 1, "seed", ["base", "step"])
    database.close()
    lineage = write_lineage(tmp_path)
    assert lineage is not None
    assert lineage["frontier_path_program_ids"] == ["seed", "advance"]
    assert lineage["frontier_accepted_goals"] == ["base", "step"]
    assert not verify_lineage(tmp_path)
    (tmp_path / "gen_1" / "main.lean").write_text("tampered\n")
    assert any("candidate" in error for error in verify_lineage(tmp_path))
