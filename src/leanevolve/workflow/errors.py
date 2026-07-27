"""Stable exit-code classes and failures that always carry a recovery command.

Agents and scripts branch on the exit code; humans read the message. A task
must never fail with a bare traceback, so every failure raised inside this
package carries both a plain-language explanation and one concrete command
that makes progress.
"""

from __future__ import annotations

from enum import IntEnum


class Exit(IntEnum):
    """Exit-code classes shared by every LeanEvolve task."""

    OK = 0
    USAGE = 2
    MISSING_TOOL = 3
    VALIDATION = 4
    NO_RESULT = 5
    INFRASTRUCTURE = 6
    INTERRUPTED = 130


EXIT_DESCRIPTIONS: dict[Exit, str] = {
    Exit.OK: "task succeeded",
    Exit.USAGE: "the request was malformed or referenced missing inputs",
    Exit.MISSING_TOOL: "a required tool or environment is unavailable",
    Exit.VALIDATION: "a gate rejected the repository or an artifact",
    Exit.NO_RESULT: "the workflow ran but produced no scientific result",
    Exit.INFRASTRUCTURE: "storage, network, or a subprocess failed unexpectedly",
    Exit.INTERRUPTED: "the task was interrupted before it finished",
}


class WorkflowError(Exception):
    """A task failure with a plain-language cause and a recovery command."""

    def __init__(
        self,
        message: str,
        *,
        exit_code: Exit = Exit.VALIDATION,
        remediation: str | None = None,
        detail: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.exit_code = exit_code
        self.remediation = remediation
        self.detail = detail

    def render(self) -> str:
        lines = [f"ERROR: {self.message}"]
        if self.detail:
            lines.append(self.detail.rstrip())
        if self.remediation:
            lines.append(f"TRY: {self.remediation}")
        return "\n".join(lines)

    def as_dict(self) -> dict[str, object]:
        return {
            "message": self.message,
            "exit_code": int(self.exit_code),
            "exit_class": self.exit_code.name.lower(),
            "remediation": self.remediation,
            "detail": self.detail,
        }
