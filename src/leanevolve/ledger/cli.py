"""Command line access to a ledger database.

Canonical mutations belong to the tools that produce them, not ad-hoc SQL.
The migration command is the one deliberate write path here: it is idempotent,
retains every source as evidence, and never modifies a legacy campaign file.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from leanevolve.ledger import integrity
from leanevolve.ledger.artifacts import ArtifactStore
from leanevolve.ledger.derive import state_of
from leanevolve.ledger.export import export_bytes, write_export
from leanevolve.ledger.projections import (
    PROJECTIONS,
    recovery_queue,
    unified_status,
)
from leanevolve.ledger.schema import SchemaError
from leanevolve.ledger.store import Ledger

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_FAILED = 1


def _open(path: str) -> Ledger:
    return Ledger.open(Path(path), create=False)


def _command_verify(args: argparse.Namespace) -> int:
    store = ArtifactStore(args.artifacts) if args.artifacts else None
    with _open(args.database) as ledger:
        report = integrity.verify(ledger, store=store, deep=args.deep)
    print(integrity.render_json(report) if args.json else integrity.render(report))
    return EXIT_OK if report.ok else EXIT_FAILED


def _command_head(args: argparse.Namespace) -> int:
    with _open(args.database) as ledger:
        head = ledger.head()
        payload = {
            "event_id": head.id if head else 0,
            "event_hash": head.event_hash if head else "0" * 64,
            "event_count": ledger.event_count(),
            "schema_version": ledger.schema_version,
        }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"head    {payload['event_hash'][:16]} (event {payload['event_id']})")
        print(f"events  {payload['event_count']}")
        print(f"schema  {payload['schema_version']}")
    return EXIT_OK


def _command_show(args: argparse.Namespace) -> int:
    with _open(args.database) as ledger:
        object_id = ledger.resolve(args.object_id)
        if object_id is None:
            print(f"unknown object: {args.object_id}", file=sys.stderr)
            return EXIT_FAILED
        record = ledger.object(object_id)
        assert record is not None
        derived = state_of(ledger, object_id)
        payload = {
            "id": record.id,
            "kind": record.kind,
            "canonical_name": record.canonical_name,
            "content_format": record.content_format,
            "properties": dict(record.properties),
            "state": derived.as_dict(),
            "connections": [
                {"relation": edge.relation, "to": edge.to_id}
                for edge in ledger.connections(from_id=object_id)
            ],
            "events": [
                {"id": event.id, "action": event.action, "actor": event.actor_class}
                for event in ledger.events(subject_id=object_id)
            ],
        }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return EXIT_OK
    print(f"{payload['id']}  ({payload['kind']})")
    print(f"  {payload['canonical_name']}")
    for dimension, value in sorted(derived.as_dict().items()):
        if value is not None:
            print(f"  {dimension:16} {value}")
    if payload["connections"]:
        print("  connections")
        for edge in payload["connections"]:
            print(f"    -{edge['relation']}-> {edge['to']}")
    return EXIT_OK


def _command_events(args: argparse.Namespace) -> int:
    with _open(args.database) as ledger:
        events = ledger.events(
            subject_id=args.subject,
            action=args.action,
            turn_id=args.turn,
            since=args.since,
            until=args.until,
        )
        payload = [
            {
                "id": event.id,
                "occurred_at": event.occurred_at,
                "actor_class": event.actor_class,
                "action": event.action,
                "subject_id": event.subject_id,
                "turn_id": event.turn_id,
            }
            for event in events
        ]
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return EXIT_OK
    for event in payload:
        print(
            f"{event['id']:>6}  {event['occurred_at']}  "
            f"{event['actor_class']:<24} {event['action']:<32} "
            f"{event['subject_id']}"
        )
    return EXIT_OK


def _command_export(args: argparse.Namespace) -> int:
    with _open(args.database) as ledger:
        if args.output:
            digest = write_export(ledger, args.output)
            print(f"{digest}  {args.output}")
        else:
            sys.stdout.write(export_bytes(ledger).decode("utf-8"))
    return EXIT_OK


def _command_project(args: argparse.Namespace) -> int:
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    with _open(args.database) as ledger:
        for name, project in PROJECTIONS.items():
            if name == "turn_delta":
                continue
            payload = project(ledger)
            destination = output / f"{name.replace('_', '-')}.json"
            destination.write_text(
                json.dumps(payload, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
    print(output)
    return EXIT_OK


def _command_status(args: argparse.Namespace) -> int:
    with _open(args.database) as ledger:
        payload = unified_status(ledger)
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"head    {payload['ledger_head_hash'][:16]}")
        print(f"goals   {payload['goal_counts']}")
        print(f"next    {payload['safest_next_action']}")
    return EXIT_OK


def _command_recover(args: argparse.Namespace) -> int:
    with _open(args.database) as ledger:
        payload = recovery_queue(ledger)
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        for item in payload["items"]:
            print(f"{item['object_id']}  {item['safe_action']}")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="leanevolve-ledger",
        description="Inspect and validate a research ledger database.",
    )
    parser.add_argument("--database", required=True, help="path to the ledger")
    parser.add_argument("--json", action="store_true", help="emit stable JSON")
    subparsers = parser.add_subparsers(dest="command", required=True)

    check = subparsers.add_parser("verify", help="validate the whole database")
    check.add_argument("--artifacts", help="artifact store root, to re-hash bytes")
    check.add_argument(
        "--deep", action="store_true", help="re-hash local artifact bytes"
    )
    check.set_defaults(handler=_command_verify)

    head = subparsers.add_parser("head", help="show the chain head")
    head.set_defaults(handler=_command_head)

    show = subparsers.add_parser("show", help="show one object and its derived state")
    show.add_argument("object_id", help="object ID or alias")
    show.set_defaults(handler=_command_show)

    events = subparsers.add_parser("events", help="list events in order")
    events.add_argument("--subject")
    events.add_argument("--action")
    events.add_argument("--turn")
    events.add_argument("--since", type=int)
    events.add_argument("--until", type=int)
    events.set_defaults(handler=_command_events)

    export = subparsers.add_parser("export", help="write a deterministic export")
    export.add_argument("--output", help="destination file; stdout when omitted")
    export.set_defaults(handler=_command_export)

    project = subparsers.add_parser(
        "project", help="rebuild all disposable projections"
    )
    project.add_argument("--output", required=True, help="projection directory")
    project.set_defaults(handler=_command_project)

    status = subparsers.add_parser(
        "status", help="show canonical current research status"
    )
    status.set_defaults(handler=_command_status)

    recover = subparsers.add_parser(
        "recover", help="show canonical interrupted/unpromoted work"
    )
    recover.set_defaults(handler=_command_recover)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        return int(args.handler(args))
    except SchemaError as error:
        print(f"error: {error}", file=sys.stderr)
        return EXIT_USAGE
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return EXIT_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
