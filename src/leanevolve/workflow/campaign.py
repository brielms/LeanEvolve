"""Discover and summarize campaigns from their own receipts.

Nothing here re-derives scientific status from prose. A campaign's state comes
from its run manifest, its hash-linked event stream, and its proof lineage; if
those are absent or disagree, the campaign is reported as unverified rather
than quietly summarized.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from leanevolve.audit import EVENTS, RUN_MANIFEST
from leanevolve.lineage import LINEAGE_NAME

RESUMABLE_STATES = ("interrupted", "failed")


@dataclass(frozen=True)
class Campaign:
    """One campaign directory, summarized from its recorded evidence."""

    path: Path
    status: str
    started_at_utc: str | None
    finished_at_utc: str | None
    model: str | None
    schedule: dict[str, Any] | None
    accepted_goals: tuple[str, ...]
    lineage_complete: bool | None
    inputs_sha256: str | None
    problems: tuple[str, ...]

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def replayable(self) -> bool:
        return (
            self.status == "completed"
            and self.lineage_complete is True
            and not self.problems
        )

    def recovery(self) -> str:
        if self.status == "running":
            return (
                "the campaign never recorded an end state; inspect "
                f"{self.path / EVENTS} before reusing it"
            )
        if self.status in RESUMABLE_STATES:
            return (
                "start a new campaign; evidence already recorded here is reusable "
                "as a predecessor and is never silently rerun"
            )
        if self.replayable:
            return f"leanevolve replay --run-dir {self.path}"
        return "this campaign is not replayable; see the recorded problems"

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": str(self.path),
            "status": self.status,
            "started_at_utc": self.started_at_utc,
            "finished_at_utc": self.finished_at_utc,
            "model": self.model,
            "schedule": self.schedule,
            "accepted_goals": list(self.accepted_goals),
            "lineage_complete": self.lineage_complete,
            "inputs_sha256": self.inputs_sha256,
            "replayable": self.replayable,
            "problems": list(self.problems),
            "recovery": self.recovery(),
        }


def _load_json(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    if not path.is_file():
        return None, f"missing {path.name}"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return None, f"unreadable {path.name}: {error}"
    if not isinstance(payload, dict):
        return None, f"malformed {path.name}"
    return payload, None


def read_campaign(path: Path) -> Campaign:
    """Summarize one campaign directory without trusting anything but receipts."""

    problems: list[str] = []
    manifest, error = _load_json(path / RUN_MANIFEST)
    if error:
        problems.append(error)
    manifest = manifest or {}
    parameters = manifest.get("run_parameters")
    parameters = parameters if isinstance(parameters, dict) else {}
    lineage: dict[str, Any] = {}
    if (path / LINEAGE_NAME).is_file():
        loaded, lineage_error = _load_json(path / LINEAGE_NAME)
        if lineage_error:
            problems.append(lineage_error)
        lineage = loaded or {}
    if not (path / EVENTS).is_file():
        problems.append(f"missing {EVENTS}")
    status = manifest.get("status")
    schedule = parameters.get("schedule")
    return Campaign(
        path=path,
        status=status if isinstance(status, str) else "unknown",
        started_at_utc=manifest.get("started_at_utc"),
        finished_at_utc=manifest.get("finished_at_utc"),
        model=parameters.get("model"),
        schedule=schedule if isinstance(schedule, dict) else None,
        accepted_goals=tuple(lineage.get("frontier_accepted_goals", []) or ()),
        lineage_complete=lineage.get("lineage_complete"),
        inputs_sha256=manifest.get("inputs_sha256"),
        problems=tuple(problems),
    )


def is_campaign_dir(path: Path) -> bool:
    return path.is_dir() and (path / RUN_MANIFEST).is_file()


def discover(artifact_root: Path, limit: int | None = None) -> list[Campaign]:
    """Return campaigns under the artifact root, newest recorded start first."""

    if not artifact_root.is_dir():
        return []
    found: list[Campaign] = []
    for candidate in sorted(artifact_root.iterdir()):
        if is_campaign_dir(candidate):
            found.append(read_campaign(candidate))
    found.sort(key=lambda item: (item.started_at_utc or "", item.name), reverse=True)
    return found if limit is None else found[:limit]


def latest_replayable(campaigns: list[Campaign]) -> Campaign | None:
    """Return the newest campaign whose own receipts verify, if any."""

    for campaign in campaigns:
        if campaign.replayable:
            return campaign
    return None


def inherited_frontier(campaigns: list[Campaign]) -> dict[str, Any]:
    """Describe the predecessor a new campaign would build on."""

    ambiguous = [
        campaign
        for campaign in campaigns
        if campaign.status == "completed" and not campaign.replayable
    ]
    latest = latest_replayable(campaigns)
    if latest is None:
        return {
            "available": False,
            "reason": (
                "no completed campaign with a verified lineage was found"
                if not ambiguous
                else "the newest completed campaigns have unverified lineage"
            ),
            "partially_verified_candidates": [item.name for item in ambiguous],
        }
    return {
        "available": True,
        "campaign": latest.name,
        "path": str(latest.path),
        "finished_at_utc": latest.finished_at_utc,
        "accepted_goals": list(latest.accepted_goals),
        "inputs_sha256": latest.inputs_sha256,
    }
