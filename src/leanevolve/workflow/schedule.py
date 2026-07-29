"""Ordered, auditable campaign schedules.

A campaign is a sequence of epochs. Changing how often a human reviews the
campaign must never change what counts as evidence, so the schedule is parsed
and recorded before any model turn is spent, and the recorded form is what a
receipt and a replay compare against.

Two styles are supported:

``steps``
    One integer: that many sequential solve turns, no field expansion.

``chunks``
    Comma-separated integers: ``2,3`` means two solve turns, one field
    expansion, three solve turns, and one final field expansion. The ordering
    is part of the scientific meaning and is never parallelized.

``spotlight``
    ``<goal> for <n> turns``: a short sprint that reprioritizes the field
    around one frozen objective. It is a solve epoch with a named focus, not a
    different kind of evidence -- see :mod:`leanevolve.workflow.spotlight`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from leanevolve.workflow.errors import Exit, WorkflowError
from leanevolve.workflow.spotlight import MAX_SPOTLIGHT_TURNS

MAX_TURNS_PER_CHUNK = 99
MAX_CHUNKS = 32
SOLVE = "solve"
EXPAND = "expand"
SPOTLIGHT = "spotlight"

_SPOTLIGHT_PATTERN = re.compile(
    r"^(?P<goal>[A-Za-z_][A-Za-z0-9_.]*)"
    r"(?:\s+for\s+(?P<turns>\d+)\s*turns?)?$"
)


@dataclass(frozen=True)
class Epoch:
    """One auditable stage of a campaign."""

    kind: str
    turns: int
    focus: str | None = None

    def describe(self) -> str:
        if self.kind == SOLVE:
            unit = "solve turn" if self.turns == 1 else "solve turns"
            return f"{self.turns} {unit}"
        if self.kind == SPOTLIGHT:
            unit = "turn" if self.turns == 1 else "turns"
            return f"spotlight {self.focus} for {self.turns} {unit}"
        return "expansion"


@dataclass(frozen=True)
class Schedule:
    """A campaign's ordered epochs, derived from one user-facing argument."""

    style: str
    raw: str
    epochs: tuple[Epoch, ...]

    @property
    def solve_turns(self) -> int:
        return sum(
            epoch.turns
            for epoch in self.epochs
            if epoch.kind in (SOLVE, SPOTLIGHT)
        )

    @property
    def expansion_count(self) -> int:
        return sum(1 for epoch in self.epochs if epoch.kind == EXPAND)

    @property
    def spotlight_goals(self) -> tuple[str, ...]:
        return tuple(
            epoch.focus
            for epoch in self.epochs
            if epoch.kind == SPOTLIGHT and epoch.focus
        )

    def describe(self) -> str:
        return " -> ".join(epoch.describe() for epoch in self.epochs)

    def as_dict(self) -> dict[str, object]:
        return {
            "style": self.style,
            "argument": self.raw,
            "description": self.describe(),
            "solve_turns": self.solve_turns,
            "expansion_count": self.expansion_count,
            "epochs": [
                {
                    "index": index,
                    "kind": epoch.kind,
                    "turns": epoch.turns,
                    **({"focus": epoch.focus} if epoch.focus else {}),
                }
                for index, epoch in enumerate(self.epochs)
            ],
        }


def _malformed(value: str, reason: str, style: str) -> WorkflowError:
    example = {
        "chunks": "--chunks 2,3",
        "spotlight": "--spotlight 'intermediate_goal for 3 turns'",
    }.get(style, "--proposal-steps 3")
    return WorkflowError(
        f"malformed campaign schedule {value!r}: {reason}",
        exit_code=Exit.USAGE,
        remediation=f"pass a valid schedule, for example {example}",
    )


def _turns(text: str, value: str, style: str) -> int:
    stripped = text.strip()
    if not stripped.isdigit():
        raise _malformed(value, "each chunk must be a positive integer", style)
    turns = int(stripped)
    if not 1 <= turns <= MAX_TURNS_PER_CHUNK:
        raise _malformed(
            value, f"each chunk must be in 1..{MAX_TURNS_PER_CHUNK}", style
        )
    return turns


def _parse_spotlight(value: str) -> Schedule:
    """Parse ``<goal> for <n> turns`` into one focused solve epoch."""

    text = value.strip()
    if not text:
        raise _malformed(value, "the schedule is empty", SPOTLIGHT)
    match = _SPOTLIGHT_PATTERN.match(text)
    if match is None:
        raise _malformed(value, "expected '<goal> for <n> turns'", SPOTLIGHT)
    turns = _turns(match.group("turns") or "1", value, SPOTLIGHT)
    if turns > MAX_SPOTLIGHT_TURNS:
        raise _malformed(
            value,
            f"a spotlight is a short sprint of at most {MAX_SPOTLIGHT_TURNS} turns",
            SPOTLIGHT,
        )
    return Schedule(
        style=SPOTLIGHT,
        raw=value,
        epochs=(Epoch(SPOTLIGHT, turns, focus=match.group("goal")),),
    )


def parse_schedule(style: str, value: str) -> Schedule:
    """Parse a schedule argument into ordered epochs, or explain why it cannot."""

    if style == "steps":
        return Schedule(
            style=style,
            raw=value,
            epochs=(Epoch(SOLVE, _turns(value, value, style)),),
        )
    if style == SPOTLIGHT:
        return _parse_spotlight(value)
    if style != "chunks":
        raise WorkflowError(
            f"unknown schedule style {style!r}",
            exit_code=Exit.VALIDATION,
            remediation=(
                "set schedule.style to 'steps', 'chunks', or 'spotlight' in "
                "leanevolve.toml"
            ),
        )
    parts = value.split(",")
    if not value.strip():
        raise _malformed(value, "the schedule is empty", style)
    if len(parts) > MAX_CHUNKS:
        raise _malformed(value, f"at most {MAX_CHUNKS} chunks are supported", style)
    epochs: list[Epoch] = []
    for part in parts:
        epochs.append(Epoch(SOLVE, _turns(part, value, style)))
        epochs.append(Epoch(EXPAND, 0))
    return Schedule(style=style, raw=value, epochs=tuple(epochs))


def extract_schedule(flag: str, style: str, arguments: list[str]) -> Schedule | None:
    """Read the schedule argument out of the arguments forwarded to a runner."""

    for index, argument in enumerate(arguments):
        value: str | None = None
        if argument == flag:
            if index + 1 >= len(arguments):
                raise WorkflowError(
                    f"{flag} requires a value",
                    exit_code=Exit.USAGE,
                    remediation=f"pass {flag} followed by the schedule",
                )
            value = arguments[index + 1]
        elif argument.startswith(f"{flag}="):
            value = argument.split("=", 1)[1]
        if value is not None:
            return parse_schedule(style, value)
    return None
