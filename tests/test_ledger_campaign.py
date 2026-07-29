from __future__ import annotations

from pathlib import Path

from leanevolve.ledger.campaign import (
    finish_campaign,
    finish_epoch,
    finish_turn,
    start_campaign,
    start_epoch,
    start_turn,
)
from leanevolve.ledger.store import Ledger


def test_campaign_scopes_and_actor_identity_are_project_supplied(
    tmp_path: Path,
) -> None:
    database = tmp_path / "ledger.sqlite3"
    campaign = start_campaign(
        database,
        tmp_path / "runs" / "example-run",
        {"workflow": "proof-search"},
    )
    epoch = start_epoch(database, campaign, 1, kind="focused_search")
    turn = start_turn(
        database,
        campaign,
        epoch,
        1,
        actor_identity="model:example-route",
    )
    finish_turn(database, campaign, epoch, turn, "completed")
    finish_epoch(database, campaign, epoch, "completed")
    finish_campaign(database, campaign, "completed")

    with Ledger.open(database) as ledger:
        started = ledger.events(subject_id=turn, action="turn_started")
        assert started[0].payload["actor_identity"] == "model:example-route"
        assert started[0].campaign_id == campaign
        assert started[0].epoch_id == epoch
        assert started[0].turn_id == turn
