"""Recover a completed Shinka proposal from a local Codex JSONL session.

This is untrusted audit tooling.  It never establishes theorem validity; the
recovered candidate must still pass the ordinary Lean evaluator and kernel
audit.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path


START_MARKER = "-- EVOLVE-BLOCK-START"
END_MARKER = "-- EVOLVE-BLOCK-END"
DIFF_PATTERN = re.compile(
    r"<<<<<<< SEARCH\n(?P<search>.*?)=======\n"
    r"(?P<replacement>.*?)\n>>>>>>> REPLACE",
    re.DOTALL,
)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def completed_response(session_path: Path) -> str:
    responses: list[str] = []
    for line in session_path.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        payload = record.get("payload", {})
        if (
            record.get("type") == "event_msg"
            and payload.get("type") == "task_complete"
            and isinstance(payload.get("last_agent_message"), str)
        ):
            responses.append(payload["last_agent_message"])
    if not responses:
        raise SystemExit("session contains no completed Codex response")
    return responses[-1]


def recover_diff(response: str, seed: str) -> str:
    try:
        diff = response.split("<DIFF>\n", 1)[1].split("\n</DIFF>", 1)[0]
    except (IndexError, ValueError) as error:
        raise SystemExit("completed response has no supported SEARCH/REPLACE diff") from error
    edits = list(DIFF_PATTERN.finditer(diff))
    if not edits:
        raise SystemExit("completed response has no supported SEARCH/REPLACE diff")

    candidate = seed
    for edit in edits:
        search = edit.group("search")
        if search.endswith("\n"):
            search = search[:-1]
        replacement = edit.group("replacement")
        start = candidate.find(START_MARKER)
        end = candidate.find(END_MARKER)
        if start < 0 or end < 0 or start >= end:
            raise SystemExit("seed has no unique ordered EVOLVE-BLOCK")
        if candidate.find(START_MARKER, start + 1) >= 0:
            raise SystemExit("seed has multiple EVOLVE-BLOCK starts")
        if candidate.find(END_MARKER, end + 1) >= 0:
            raise SystemExit("seed has multiple EVOLVE-BLOCK ends")
        editable_start = start + len(START_MARKER)
        editable_end = end

        if search == "":
            editable = candidate[editable_start:editable_end]
            if editable.strip():
                raise SystemExit(
                    "empty SEARCH recovery is allowed only for an empty "
                    "EVOLVE-BLOCK"
                )
            insertion = "\n" + replacement
            if replacement and not replacement.endswith("\n"):
                insertion += "\n"
            candidate = (
                candidate[:editable_start]
                + insertion
                + candidate[editable_end:]
            )
            continue

        occurrences = candidate.count(search)
        if occurrences != 1:
            raise SystemExit(
                f"SEARCH block occurs {occurrences} times in seed; "
                "expected exactly one"
            )
        location = candidate.index(search)
        if not (
            editable_start <= location
            and location + len(search) <= editable_end
        ):
            raise SystemExit("SEARCH block attempts to edit outside EVOLVE-BLOCK")
        candidate = (
            candidate[:location]
            + replacement
            + candidate[location + len(search):]
        )
    return candidate


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("session", type=Path)
    parser.add_argument("seed", type=Path)
    parser.add_argument("output_dir", type=Path)
    arguments = parser.parse_args()

    session = arguments.session.resolve()
    seed = arguments.seed.resolve()
    output_dir = arguments.output_dir.resolve()
    if output_dir.exists():
        raise SystemExit(f"refusing to overwrite existing output: {output_dir}")

    response = completed_response(session)
    seed_text = seed.read_text(encoding="utf-8")
    candidate = recover_diff(response, seed_text)

    output_dir.mkdir(parents=True)
    response_path = output_dir / "recovered_response.txt"
    candidate_path = output_dir / "recovered_candidate.lean"
    response_path.write_text(response, encoding="utf-8")
    candidate_path.write_text(candidate, encoding="utf-8")
    manifest = {
        "schema_version": 1,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "trust_status": "untrusted_recovery_requires_evaluation",
        "session": {
            "path": str(session),
            "sha256": sha256_bytes(session.read_bytes()),
        },
        "seed": {
            "path": str(seed),
            "sha256": sha256_bytes(seed_text.encode("utf-8")),
        },
        "response": {
            "path": response_path.name,
            "sha256": sha256_bytes(response.encode("utf-8")),
        },
        "candidate": {
            "path": candidate_path.name,
            "sha256": sha256_bytes(candidate.encode("utf-8")),
        },
    }
    (output_dir / "recovery_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(manifest, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
