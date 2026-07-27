"""Completion receipts shared by every task.

A task's default output is a short human summary; ``--json`` prints the same
receipt as a versioned document. Either way the receipt is written to disk
before the process exits -- including when the task is interrupted -- so that
"where is the evidence for this run" always has an answer.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from leanevolve.workflow.errors import EXIT_DESCRIPTIONS, Exit, WorkflowError

RECEIPT_FORMAT = "leanevolve-task-receipt-v1"


@dataclass
class Step:
    """One reported unit of work inside a task."""

    name: str
    status: str
    detail: str | None = None
    log_path: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "log_path": self.log_path,
        }


@dataclass
class Receipt:
    """The versioned result document a task produces."""

    task: str
    task_version: str = "1"
    started_at_utc: str = ""
    finished_at_utc: str | None = None
    status: str = "ok"
    exit_code: Exit = Exit.OK
    steps: list[Step] = field(default_factory=list)
    inputs: dict[str, Any] = field(default_factory=dict)
    outputs: list[str] = field(default_factory=list)
    guarantees: list[str] = field(default_factory=list)
    not_checked: list[str] = field(default_factory=list)
    scientific_status: str = "tooling result only; no mathematical claim"
    next_action: str | None = None
    error: dict[str, Any] | None = None
    summary_lines: list[str] = field(default_factory=list)
    path: Path | None = None

    def step(
        self,
        name: str,
        status: str,
        detail: str | None = None,
        log_path: Path | None = None,
    ) -> Step:
        record = Step(
            name=name,
            status=status,
            detail=detail,
            log_path=None if log_path is None else str(log_path),
        )
        self.steps.append(record)
        return record

    def say(self, line: str = "") -> None:
        self.summary_lines.append(line)

    def failed_steps(self) -> list[Step]:
        return [item for item in self.steps if item.status == "failed"]

    def as_dict(self) -> dict[str, Any]:
        return {
            "format": RECEIPT_FORMAT,
            "task": self.task,
            "task_version": self.task_version,
            "status": self.status,
            "exit_code": int(self.exit_code),
            "exit_class": self.exit_code.name.lower(),
            "exit_meaning": EXIT_DESCRIPTIONS[self.exit_code],
            "started_at_utc": self.started_at_utc,
            "finished_at_utc": self.finished_at_utc,
            "steps": [item.as_dict() for item in self.steps],
            "inputs": self.inputs,
            "outputs": self.outputs,
            "guarantees": self.guarantees,
            "not_checked": self.not_checked,
            "scientific_status": self.scientific_status,
            "next_action": self.next_action,
            "error": self.error,
            "receipt_path": None if self.path is None else str(self.path),
        }

    def render(self) -> str:
        lines = list(self.summary_lines)
        if self.error:
            lines.append("")
            lines.append(f"ERROR: {self.error['message']}")
            if self.error.get("detail"):
                lines.append(str(self.error["detail"]).rstrip())
            if self.error.get("remediation"):
                lines.append(f"TRY: {self.error['remediation']}")
        if self.guarantees:
            lines.append("")
            lines.append("guarantees:")
            lines.extend(f"  - {item}" for item in self.guarantees)
        if self.not_checked:
            lines.append("not checked:")
            lines.extend(f"  - {item}" for item in self.not_checked)
        if self.next_action:
            lines.append("")
            lines.append(f"next: {self.next_action}")
        if self.path is not None:
            lines.append(f"receipt: {self.path}")
        return "\n".join(lines).strip() + "\n"


def write_receipt(directory: Path, receipt: Receipt) -> Path | None:
    """Persist a receipt, tolerating an unwritable cache rather than failing."""

    try:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{receipt.task}.json"
        receipt.path = path
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(receipt.as_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
        return path
    except OSError:
        receipt.path = None
        return None


def error_receipt(receipt: Receipt, error: WorkflowError) -> Receipt:
    """Record a failure on a receipt without discarding the work already done."""

    receipt.status = "failed"
    receipt.exit_code = error.exit_code
    receipt.error = error.as_dict()
    if receipt.next_action is None and error.remediation:
        receipt.next_action = error.remediation
    return receipt
