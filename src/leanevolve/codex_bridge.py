"""Translate Headless's `xhigh` compatibility value to Codex `max`."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HEADLESS_PACKAGE = "@roberttlange/headless@0.4.0"
MODEL = "gpt-5.6-sol"


def _argument_value(arguments: list[str], flag: str) -> str | None:
    try:
        index = arguments.index(flag)
    except ValueError:
        return None
    return arguments[index + 1] if index + 1 < len(arguments) else None


def _shim_source(real_codex: str) -> str:
    return f"""#!/usr/bin/env python3
import json
import os
import sys
from datetime import datetime, timezone

before = list(sys.argv[1:])
after = [
    'model_reasoning_effort="max"'
    if item == 'model_reasoning_effort="xhigh"'
    else item
    for item in before
]
receipt_path = os.environ.get("LEANEVOLVE_BRIDGE_RECEIPT")
if receipt_path:
    receipt = {{
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "requested_effort": "max",
        "compatibility_effort": "xhigh",
        "effective_effort": "max",
    }}
    with open(receipt_path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(receipt, sort_keys=True) + "\\n")
os.execv({real_codex!r}, [{real_codex!r}, *after])
"""


def main() -> None:
    arguments = sys.argv[1:]
    requested = _argument_value(arguments, "--reasoning-effort")
    model = _argument_value(arguments, "--model")
    transformed = list(arguments)
    if requested == "max":
        if model != MODEL:
            raise SystemExit(f"max bridge requires {MODEL}; received {model!r}")
        transformed[transformed.index("--reasoning-effort") + 1] = "xhigh"
    real_codex = shutil.which("codex")
    if real_codex is None:
        raise SystemExit("Codex CLI is unavailable on PATH")
    with tempfile.TemporaryDirectory(prefix="leanevolve-headless-") as temporary:
        shim = Path(temporary) / "codex"
        shim.write_text(_shim_source(real_codex), encoding="utf-8")
        shim.chmod(0o700)
        environment = os.environ.copy()
        environment["PATH"] = (
            str(shim.parent) + os.pathsep + environment.get("PATH", "")
        )
        work_dir = _argument_value(arguments, "--work-dir")
        if requested == "max" and work_dir:
            environment["LEANEVOLVE_BRIDGE_RECEIPT"] = str(
                Path(work_dir) / "headless_bridge_invocations.jsonl"
            )
        completed = subprocess.run(
            ["npx", "-y", HEADLESS_PACKAGE, *transformed],
            env=environment,
            check=False,
        )
    raise SystemExit(completed.returncode)


if __name__ == "__main__":
    main()
