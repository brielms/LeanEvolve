"""The canonical, disposable ledger projections registered in ``PROJECTIONS``.

Every view carries the exact chain head that produced it.  Callers may cache or
render these dictionaries, but deleting them loses no authoritative state.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from leanevolve.ledger.derive import state_of
from leanevolve.ledger.schema import GENESIS_HASH
from leanevolve.ledger.store import Ledger, ObjectRecord

PROJECTION_SCHEMA_VERSION = 1


def _stamp(ledger: Ledger, name: str, payload: dict[str, Any]) -> dict[str, Any]:
    head = ledger.head()
    return {
        "format": f"leanevolve-ledger-{name}-v1",
        "schema_version": PROJECTION_SCHEMA_VERSION,
        "source_event_range": [1, head.id] if head else [0, 0],
        "ledger_head_hash": head.event_hash if head else GENESIS_HASH,
        **payload,
    }


def _object(record: ObjectRecord, ledger: Ledger) -> dict[str, Any]:
    return {
        "id": record.id,
        "kind": record.kind,
        "canonical_name": record.canonical_name,
        "content": record.content,
        "properties": dict(record.properties),
        "state": state_of(ledger, record.id).as_dict(),
    }


def goal_board(ledger: Ledger) -> dict[str, Any]:
    goals = [
        record
        for record in ledger.objects()
        if record.properties.get("role") == "goal"
    ]
    goal_ids = {record.id for record in goals}
    edges = [
        edge
        for edge in ledger.connections()
        if edge.from_id in goal_ids and edge.to_id in goal_ids
        and edge.relation in {"depends_on", "decomposes_into"}
    ]
    return _stamp(
        ledger,
        "goal-board",
        {
            "goals": [_object(record, ledger) for record in goals],
            "connections": [
                {
                    "from": edge.from_id,
                    "relation": edge.relation,
                    "to": edge.to_id,
                    "properties": dict(edge.properties),
                }
                for edge in edges
            ],
        },
    )


def active_goal_catalog(ledger: Ledger) -> dict[str, Any]:
    """Project the exact Shinka catalog interface from canonical goal objects.

    The v2 JSON shape is retained only as an interface for older Shinka code.
    Goal content, metadata, and graph edges are read from the live ledger; no
    retained historical artifact participates in this projection.
    """

    goals = sorted(
        (
            record
            for record in ledger.objects(kind="formal_claim")
            if record.properties.get("role") == "goal"
        ),
        key=lambda record: record.created_event_id,
    )
    if not goals:
        raise ValueError("ledger contains no canonical goal objects")
    goal_ids = {record.id for record in goals}
    names = {
        record.id: (
            record.id.removeprefix("goal:")
            if record.id.startswith("goal:")
            else str(record.properties.get("goal_name", ""))
        )
        for record in goals
    }
    if any(not name for name in names.values()):
        raise ValueError("canonical goal lacks a stable goal name")

    dependencies: dict[str, list[str]] = {record.id: [] for record in goals}
    parents: dict[str, str] = {}
    for edge in ledger.connections():
        if edge.from_id not in goal_ids or edge.to_id not in goal_ids:
            continue
        if edge.relation == "depends_on":
            dependencies[edge.from_id].append(names[edge.to_id])
        elif edge.relation == "decomposes_into":
            prior = parents.setdefault(edge.to_id, names[edge.from_id])
            if prior != names[edge.from_id]:
                raise ValueError(
                    f"goal {names[edge.to_id]} has multiple canonical parents"
                )

    projected = []
    for record in goals:
        properties = dict(record.properties)
        required = ("route", "tier", "weight")
        missing = [key for key in required if properties.get(key) is None]
        if missing:
            raise ValueError(
                f"goal {names[record.id]} lacks canonical metadata: "
                + ", ".join(missing)
            )
        item: dict[str, Any] = {
            "name": names[record.id],
            "target_type": record.content,
            "weight": float(properties["weight"]),
            "tier": int(properties["tier"]),
            "route": str(properties["route"]),
            "depends_on": dependencies[record.id],
            "description": (
                ""
                if record.canonical_name == names[record.id].replace("_", " ")
                else record.canonical_name
            ),
            "parent": parents.get(record.id),
            "kind": str(properties.get("legacy_kind") or "theorem"),
        }
        diagnostic_role = properties.get("diagnostic_role")
        if diagnostic_role is None and item["kind"] == "diagnostic":
            diagnostic_role = (
                "refutation"
                if "counterexample" in names[record.id]
                else (
                    "bounded"
                    if "_up_to_" in names[record.id]
                    or names[record.id].endswith("_up_to_five")
                    else "positive"
                )
            )
        if diagnostic_role is not None:
            item["diagnostic_role"] = str(diagnostic_role)
        projected.append(item)

    # The one tier-zero goal is the campaign's final objective and remains last
    # for compatibility with goal-catalog consumers.
    final = [item for item in projected if item["tier"] == 0]
    if len(final) != 1:
        raise ValueError("ledger must contain exactly one tier-zero final goal")
    projected = [item for item in projected if item["tier"] != 0] + final
    head = ledger.head()
    return {
        "format": "leanevolve-goal-catalog-v2",
        "goals": projected,
        "ledger_projection": {
            "format": "leanevolve-ledger-projection-source-v1",
            "source_event_range": [1, head.id] if head else [0, 0],
            "ledger_head_hash": head.event_hash if head else GENESIS_HASH,
        },
    }


def active_goal_statuses(ledger: Ledger) -> dict[str, Any]:
    """Project kernel-backed Shinka obligation states from canonical events."""

    goal_records = [
        record
        for record in ledger.objects(kind="formal_claim")
        if record.properties.get("role") == "goal"
    ]
    statuses = {
        str(
            record.properties.get("goal_name")
            or record.id.removeprefix("goal:")
        ): state_of(ledger, record.id).truth
        for record in goal_records
    }
    return _stamp(ledger, "active-goal-statuses", {"obligation_statuses": statuses})


def spotlight_relevance(ledger: Ledger) -> dict[str, Any]:
    """Project route-to-terminal relevance solely from canonical graph edges."""

    routes: dict[str, str] = {}
    terminals: dict[str, dict[str, str]] = {}
    final_goals = [
        record
        for record in ledger.objects(kind="formal_claim")
        if record.properties.get("role") == "goal"
        and record.properties.get("tier") == 0
    ]
    if len(final_goals) != 1:
        raise ValueError("ledger must contain exactly one tier-zero final goal")
    final_goal_id = final_goals[0].id
    final_goal = str(
        final_goals[0].properties.get("goal_name")
        or final_goal_id.removeprefix("goal:")
    )
    for edge in ledger.connections(relation="advances"):
        role = edge.properties.get("role")
        if role == "spotlight_relevance":
            route = str(edge.properties.get("route", ""))
            terminal = edge.to_id.removeprefix("goal:")
            if not route:
                raise ValueError("spotlight relevance edge lacks a route")
            prior = routes.setdefault(route, terminal)
            if prior != terminal:
                raise ValueError(f"route {route} has conflicting terminals")
        elif role == "terminal_reduction":
            terminal = edge.from_id.removeprefix("goal:")
            if edge.to_id != final_goal_id:
                raise ValueError(
                    f"terminal reduction {terminal} does not advance full"
                )
            record = ledger.object(edge.from_id)
            assert record is not None
            terminals[terminal] = {
                "statement": record.content,
                "declaration": str(record.properties.get("declaration", "")),
            }
    if not routes or not terminals:
        raise ValueError("ledger contains no canonical spotlight relevance graph")
    return _stamp(
        ledger,
        "spotlight-relevance",
        {
            "final_goal": final_goal,
            "routes": routes,
            "terminal_reductions": terminals,
        },
    )


def research_findings(ledger: Ledger) -> dict[str, Any]:
    """Project current informal/computational research without legacy boards."""

    records = []
    allowed = {"research_claim", "computation", "source_claim"}
    for record in sorted(ledger.objects(), key=lambda item: item.created_event_id):
        if record.kind not in allowed:
            continue
        state = state_of(ledger, record.id)
        goals = [
            edge.to_id.removeprefix("goal:")
            for edge in ledger.connections(from_id=record.id, relation="advances")
            if edge.to_id.startswith("goal:")
        ]
        dependencies = [
            edge.to_id
            for edge in ledger.connections(from_id=record.id, relation="depends_on")
        ]
        properties = dict(record.properties)
        records.append({
            "id": record.id,
            "kind": record.kind,
            "title": record.canonical_name,
            "claim": record.content,
            "tags": list(properties.get("tags", [])),
            "board_goals": goals,
            "record_dependencies": dependencies,
            "canonical_state": state.as_dict(),
            "historical_classification": {
                "kind": properties.get("legacy_kind"),
                "status": properties.get("legacy_status"),
            },
        })
    return _stamp(ledger, "research-findings", {"records": records})


def research_ledger_compatibility(ledger: Ledger) -> dict[str, Any]:
    """Render a safe v1 search interface from live canonical findings.

    It deliberately does not repeat historical ``proved``/``refuted`` labels
    for informal claims. Canonical truth is carried separately and only formal
    goal objects may become proved through evaluator events.
    """

    findings = research_findings(ledger)
    records = []
    for item in findings["records"]:
        kind = item["historical_classification"].get("kind")
        if kind not in {
            "insight", "conjecture", "lemma", "obstruction", "computation",
            "source_claim",
        }:
            kind = "computation" if item["kind"] == "computation" else "insight"
        records.append({
            "id": item["id"].removeprefix("research:"),
            "title": item["title"],
            "claim": item["claim"],
            "kind": kind,
            "status": "open",
            "board_goals": item["board_goals"],
            "board_dependencies": [],
            "record_dependencies": [
                value.removeprefix("research:")
                for value in item["record_dependencies"]
                if value.startswith("research:")
            ],
            "prior_art_claims": [],
            "references": [],
            "tags": item["tags"],
            "notes": [
                "Canonical ledger state: "
                + json_safe_state(item["canonical_state"])
            ],
            "kernel_evidence": [],
            "computational_evidence": [],
            "created_at_utc": "",
            "updated_at_utc": "",
            "history": [],
        })
    return {
        "format": "leanevolve-research-ledger-v1",
        "records": records,
        "ledger_projection": {
            "source_event_range": findings["source_event_range"],
            "ledger_head_hash": findings["ledger_head_hash"],
        },
    }


def json_safe_state(state: dict[str, Any]) -> str:
    return ", ".join(
        f"{key}={value}" for key, value in sorted(state.items())
        if value is not None
    )


def chronology(ledger: Ledger) -> dict[str, Any]:
    return _stamp(
        ledger,
        "chronology",
        {
            "events": [
                {
                    "id": event.id,
                    "occurred_at": event.occurred_at,
                    "campaign_id": event.campaign_id,
                    "epoch_id": event.epoch_id,
                    "turn_id": event.turn_id,
                    "actor_class": event.actor_class,
                    "actor_id": event.actor_id,
                    "action": event.action,
                    "subject_type": event.subject_type,
                    "subject_id": event.subject_id,
                    "evidence_object_id": event.evidence_object_id,
                    "payload": dict(event.payload),
                }
                for event in ledger.events()
            ]
        },
    )


def formal_proof_graph(ledger: Ledger) -> dict[str, Any]:
    claims = [
        record
        for record in ledger.objects(kind="formal_claim")
        if state_of(ledger, record.id).truth == "proved"
    ]
    claim_ids = {record.id for record in claims}
    edges = [
        edge
        for edge in ledger.connections(relation="depends_on")
        if edge.from_id in claim_ids and edge.to_id in claim_ids
    ]
    return _stamp(
        ledger,
        "formal-proof-graph",
        {
            "claims": [_object(record, ledger) for record in claims],
            "dependencies": [
                {"from": edge.from_id, "to": edge.to_id} for edge in edges
            ],
        },
    )


def turn_delta(ledger: Ledger, turn_id: str) -> dict[str, Any]:
    events = ledger.events(turn_id=turn_id)
    return _stamp(
        ledger,
        "turn-delta",
        {
            "turn_id": turn_id,
            "events": [
                {
                    "id": event.id,
                    "action": event.action,
                    "subject_id": event.subject_id,
                    "evidence_object_id": event.evidence_object_id,
                    "payload": dict(event.payload),
                }
                for event in events
            ],
            "objects": [
                _object(record, ledger)
                for record in ledger.objects()
                if any(event.subject_id == record.id for event in events)
            ],
        },
    )


def recovery_queue(ledger: Ledger) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for record in ledger.objects(kind="check"):
        state = state_of(ledger, record.id)
        if state.operational in {"queued", "running", "interrupted"}:
            items.append(
                {
                    "kind": "incomplete_check",
                    "object_id": record.id,
                    "state": state.operational,
                    "safe_action": "reconcile_check",
                }
            )
    for record in ledger.objects(kind="formal_claim"):
        state = state_of(ledger, record.id)
        if state.verification in {"scratch_checked", "axiom_policy_audited"}:
            items.append(
                {
                    "kind": "checked_unpromoted_claim",
                    "object_id": record.id,
                    "state": state.verification,
                    "safe_action": "authoritative_audit",
                }
            )
    for record in ledger.artifacts_without_location():
        items.append(
            {
                "kind": "missing_artifact_location",
                "object_id": record.id,
                "safe_action": "restore_or_replicate_artifact",
            }
        )
    return _stamp(ledger, "recovery-queue", {"items": items})


def prior_art_crosswalk(ledger: Ledger) -> dict[str, Any]:
    publications = []
    for publication in ledger.objects(kind="publication"):
        versions = ledger.connections(from_id=publication.id, relation="has_version")
        publications.append(
            {
                **_object(publication, ledger),
                "versions": [
                    {
                        "id": edge.to_id,
                        "sources": [
                            source.to_id
                            for source in ledger.connections(
                                from_id=edge.to_id, relation="has_source"
                            )
                        ],
                        "claims": [
                            {
                                "id": claim.to_id,
                                "formal_mappings": [
                                    {
                                        "id": mapping.to_id,
                                        "properties": dict(mapping.properties),
                                        "state": state_of(
                                            ledger, mapping.to_id
                                        ).as_dict(),
                                    }
                                    for mapping in ledger.connections(
                                        from_id=claim.to_id,
                                        relation="formalized_as",
                                    )
                                ],
                            }
                            for claim in ledger.connections(
                                from_id=edge.to_id, relation="contains"
                            )
                        ],
                    }
                    for edge in versions
                ],
            }
        )
    return _stamp(ledger, "prior-art-crosswalk", {"publications": publications})


def unified_status(ledger: Ledger) -> dict[str, Any]:
    board = goal_board(ledger)
    recovery = recovery_queue(ledger)
    goals = board["goals"]
    open_goals = [goal["id"] for goal in goals if goal["state"]["truth"] == "open"]
    proved_goals = [
        goal["id"] for goal in goals if goal["state"]["truth"] == "proved"
    ]
    final_goals = [goal for goal in goals if goal["properties"].get("tier") == 0]
    return _stamp(
        ledger,
        "unified-status",
        {
            "general_theorem_open": any(
                goal["state"]["truth"] == "open" for goal in final_goals
            ),
            "goal_counts": {
                "total": len(goals),
                "open": len(open_goals),
                "proved": len(proved_goals),
            },
            "open_goals": open_goals,
            "certified_frontier": proved_goals,
            "recovery_count": len(recovery["items"]),
            "safest_next_action": (
                recovery["items"][0]["safe_action"]
                if recovery["items"]
                else ("work_open_goal" if open_goals else "audit_completion")
            ),
        },
    )


PROJECTIONS: dict[str, Callable[..., dict[str, Any]]] = {
    "active_goal_statuses": active_goal_statuses,
    "research_findings": research_findings,
    "goal_board": goal_board,
    "chronology": chronology,
    "formal_proof_graph": formal_proof_graph,
    "turn_delta": turn_delta,
    "recovery_queue": recovery_queue,
    "prior_art_crosswalk": prior_art_crosswalk,
    "unified_status": unified_status,
}

__all__ = [*PROJECTIONS, "PROJECTION_SCHEMA_VERSION", "PROJECTIONS"]
