"""Small write-through adapter for campaign, epoch, and turn scopes."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from leanevolve.ledger.store import Ledger


def _slug(value: str) -> str:
    clean = re.sub(r"[^a-zA-Z0-9_.-]+", "-", value).strip("-").lower()
    return clean[:80] or hashlib.sha256(value.encode()).hexdigest()[:16]


def campaign_id(path: Path) -> str:
    return f"campaign:{_slug(path.name)}"


def epoch_id(campaign: str, index: int) -> str:
    return f"epoch:{campaign.removeprefix('campaign:')}:{index:04d}"


def turn_id(campaign: str, index: int) -> str:
    return f"turn:{campaign.removeprefix('campaign:')}:{index:04d}"


def start_campaign(
    database: Path,
    path: Path,
    configuration: dict[str, object],
    *,
    focus_goal: str | None = None,
) -> str:
    identifier = campaign_id(path)
    configuration_json = json.dumps(
        configuration, sort_keys=True, separators=(",", ":")
    )
    configuration_hash = hashlib.sha256(configuration_json.encode()).hexdigest()
    with Ledger.open(database) as ledger:
        with ledger.write("ledger_service", "tool:shinka-campaign-v1") as session:
            session.create_object(
                identifier,
                "campaign",
                path.name,
                content_format="json",
                content=configuration_json,
                properties={
                    "campaign_path": str(path),
                    "configuration_sha256": configuration_hash,
                },
            )
            session.record(
                "campaign_started",
                identifier,
                {"configuration_sha256": configuration_hash},
                idempotency_key=f"campaign-started:{identifier}:{configuration_hash}",
            )
            if focus_goal is not None:
                goal_id = f"goal:{focus_goal}"
                if ledger.object(goal_id) is None:
                    raise ValueError(f"unknown campaign focus goal: {focus_goal}")
                session.connect(identifier, "targets", goal_id)
    return identifier


def finish_campaign(database: Path, identifier: str, state: str) -> None:
    with Ledger.open(database) as ledger:
        with ledger.write("ledger_service", "tool:shinka-campaign-v1") as session:
            session.record(
                "campaign_completed",
                identifier,
                {"operational_state": state},
                idempotency_key=f"campaign-completed:{identifier}:{state}",
            )


def start_epoch(
    database: Path,
    campaign: str,
    index: int,
    *,
    kind: str = "general_search",
    focus_goal: str | None = None,
) -> str:
    identifier = epoch_id(campaign, index)
    with Ledger.open(database) as ledger:
        with ledger.write(
            "ledger_service", "tool:shinka-campaign-v1", campaign_id=campaign
        ) as session:
            session.create_object(
                identifier,
                "epoch",
                f"{kind} epoch {index}",
                content_format="json",
                content=json.dumps({"index": index, "kind": kind}, sort_keys=True),
                properties={"epoch_kind": kind, "index": index},
            )
            session.record(
                "epoch_started",
                identifier,
                {"epoch_kind": kind},
                idempotency_key=f"epoch-started:{identifier}",
            )
            if focus_goal is not None:
                goal_id = f"goal:{focus_goal}"
                if ledger.object(goal_id) is None:
                    raise ValueError(f"unknown epoch focus goal: {focus_goal}")
                session.connect(identifier, "targets", goal_id)
    return identifier


def finish_epoch(database: Path, campaign: str, identifier: str, state: str) -> None:
    with Ledger.open(database) as ledger:
        with ledger.write(
            "ledger_service", "tool:shinka-campaign-v1", campaign_id=campaign
        ) as session:
            session.record(
                "epoch_completed",
                identifier,
                {"operational_state": state},
                idempotency_key=f"epoch-completed:{identifier}:{state}",
            )


def start_turn(
    database: Path,
    campaign: str,
    epoch: str,
    index: int,
    *,
    focus_goal: str | None = None,
    actor_identity: str = "research-agent",
) -> str:
    identifier = turn_id(campaign, index)
    with Ledger.open(database) as ledger:
        with ledger.write(
            "ledger_service",
            "tool:shinka-campaign-v1",
            campaign_id=campaign,
            epoch_id=epoch,
            turn_id=identifier,
        ) as session:
            session.create_object(
                identifier,
                "turn",
                f"General-search turn {index}",
                content_format="json",
                content=json.dumps({"index": index}, sort_keys=True),
                properties={"index": index},
            )
            session.record(
                "turn_started",
                identifier,
                {"actor_identity": actor_identity},
                idempotency_key=f"turn-started:{identifier}",
            )
            if focus_goal is not None:
                goal_id = f"goal:{focus_goal}"
                if ledger.object(goal_id) is None:
                    raise ValueError(f"unknown turn focus goal: {focus_goal}")
                session.connect(identifier, "targets", goal_id)
    return identifier


def finish_turn(
    database: Path,
    campaign: str,
    epoch: str,
    identifier: str,
    state: str,
) -> None:
    with Ledger.open(database) as ledger:
        with ledger.write(
            "ledger_service",
            "tool:shinka-campaign-v1",
            campaign_id=campaign,
            epoch_id=epoch,
            turn_id=identifier,
        ) as session:
            session.record(
                "turn_completed",
                identifier,
                {"operational_state": state},
                idempotency_key=f"turn-completed:{identifier}:{state}",
            )


__all__ = [
    "campaign_id",
    "epoch_id",
    "finish_campaign",
    "finish_epoch",
    "finish_turn",
    "start_campaign",
    "start_epoch",
    "start_turn",
    "turn_id",
]
