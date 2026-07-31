#!/usr/bin/env python3
"""Compact, semantic prompt projections over the canonical research ledger.

The SQLite ledger remains the source of exact truth.  This module deliberately
keeps semantic prose in a separate projection: prose can explain a formal
claim, but only ledger events and kernel-backed ``refutes`` edges determine its
truth and verification states.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CANONICAL_LEDGER = (
    REPOSITORY_ROOT / ".cache/leanevolve/ledger/research.sqlite3"
)
PACKET_FORMAT = "shinka-semantic-spotlight-packet-v1"
CARD_FORMAT = "shinka-semantic-theorem-card-v1"
MANIFEST_FORMAT = "shinka-semantic-spotlight-manifest-v1"
#: Encoded research annotations are single comment lines thousands of
#: characters long; excerpts drop them so they cannot fill a page.
RESEARCH_ANNOTATION_PREFIX = "-- RESEARCH:"
NORMAL_PROMPT_HARD_CAP_BYTES = 350_000
NORMAL_PACKET_SOFT_TARGET_BYTES = 180_000
FIELD_PACKET_HARD_CAP_BYTES = 180_000
VERIFICATION_RANK = {
    "untested": 0,
    "elaboration_failed": 0,
    "scratch_checked": 1,
    "axiom_policy_audited": 2,
    "authoritatively_evaluated": 3,
    "promotion_audited": 4,
}
VERIFICATION_EVENTS = {
    "elaboration_failed": "elaboration_failed",
    "scratch_kernel_checked": "scratch_checked",
    "axiom_policy_audited": "axiom_policy_audited",
    "authoritative_evaluation_recorded": "authoritatively_evaluated",
    "kernel_certified": "authoritatively_evaluated",
    "promotion_audited": "promotion_audited",
    "promotion_recorded": "promotion_audited",
}
NEIGHBOR_RELATIONS = frozenset(
    {
        "depends_on",
        "decomposes_into",
        "specializes",
        "advances",
        "supports",
        "refutes",
        "annotates",
    }
)
CARD_LIST_FIELDS = (
    "hypotheses",
    "useful_consequences",
    "limitations",
    "known_counterexamples",
)


class PacketError(ValueError):
    """A semantic packet cannot be produced without violating its contract."""


class PromptBudgetError(PacketError):
    """The exact prompt is too large and must not be silently truncated."""


@dataclass(frozen=True)
class LedgerObject:
    id: str
    kind: str
    canonical_name: str
    content_format: str
    content: str
    properties: dict[str, Any]
    created_event_id: int


@dataclass(frozen=True)
class ExactState:
    truth: str
    verification: str
    lifecycle: str


@dataclass(frozen=True)
class SemanticTheoremCard:
    format: str
    object_id: str
    mathematical_title: str
    ordinary_statement: str
    hypotheses: tuple[str, ...]
    conclusion: str
    proof_mechanism: str
    useful_consequences: tuple[str, ...]
    research_significance: str
    limitations: tuple[str, ...]
    known_counterexamples: tuple[str, ...]
    dependencies: tuple[str, ...]
    truth_state: str
    verification_state: str
    lifecycle_state: str
    lean_declaration: str
    proposition_sha256: str
    exact_contract: str
    source_pointers: tuple[str, ...]
    receipt_pointers: tuple[str, ...]
    semantic_provenance: tuple[str, ...]


@dataclass(frozen=True)
class SpotlightPacket:
    text: str
    manifest: dict[str, Any]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _open_read_only(resolved: Path) -> sqlite3.Connection:
    """Open a WAL ledger read-only even with no writer present.

    SQLite deletes ``-wal`` and ``-shm`` when the last writer closes cleanly,
    and a ``mode=ro`` connection cannot recreate the ``-shm`` it needs, so an
    ordinary read fails with "unable to open database file" in exactly the
    window between campaigns.  Falling back to ``immutable=1`` is safe only
    when no write-ahead log holds committed data the main file lacks, so the
    fallback is refused whenever a non-empty ``-wal`` exists.
    """

    uri = resolved.as_uri()

    def _opened(parameters: str) -> sqlite3.Connection:
        # sqlite3.connect is lazy, so the file is only really opened by a
        # statement; force that here to catch the failure at the right place.
        connection = sqlite3.connect(f"{uri}?{parameters}", uri=True, timeout=5.0)
        try:
            connection.execute("SELECT 1 FROM sqlite_master LIMIT 1")
        except sqlite3.Error:
            connection.close()
            raise
        return connection

    try:
        return _opened("mode=ro")
    except sqlite3.OperationalError as error:
        log = Path(f"{resolved}-wal")
        if log.exists() and log.stat().st_size > 0:
            raise PacketError(
                f"canonical ledger is busy or unreadable: {resolved}: {error}"
            ) from error
        try:
            return _opened("mode=ro&immutable=1")
        except sqlite3.Error:
            raise PacketError(
                f"canonical ledger cannot be opened read-only: {resolved}: "
                f"{error}"
            ) from error


def _connect_read_only(database: Path) -> sqlite3.Connection:
    resolved = database.resolve()
    if not resolved.is_file():
        raise PacketError(f"canonical ledger is missing: {resolved}")
    connection = _open_read_only(resolved)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    connection.execute("BEGIN")
    required = {"objects", "events", "connections", "artifact_locations"}
    present = {
        str(row["name"])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    if not required <= present:
        connection.close()
        raise PacketError("canonical ledger schema is incomplete")
    return connection


def _json_object(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _object_from_row(row: sqlite3.Row) -> LedgerObject:
    return LedgerObject(
        id=str(row["id"]),
        kind=str(row["kind"]),
        canonical_name=str(row["canonical_name"]),
        content_format=str(row["content_format"]),
        content=str(row["content"]),
        properties=_json_object(str(row["properties_json"])),
        created_event_id=int(row["created_event_id"]),
    )


def _apply_latest_correction(
    connection: sqlite3.Connection, target: LedgerObject
) -> LedgerObject:
    row = connection.execute(
        "SELECT payload_json FROM events WHERE subject_id = ? "
        "AND action = 'correction_recorded' "
        "AND json_extract(payload_json, '$.replacement_content') IS NOT NULL "
        "ORDER BY id DESC LIMIT 1",
        (target.id,),
    ).fetchone()
    if row is None:
        return target
    payload = _json_object(str(row["payload_json"]))
    properties = payload.get("replacement_properties")
    if not isinstance(properties, dict):
        raise PacketError(f"object correction for {target.id} is malformed")
    return LedgerObject(
        id=target.id,
        kind=target.kind,
        canonical_name=str(payload["replacement_canonical_name"]),
        content_format=str(payload["replacement_content_format"]),
        content=str(payload["replacement_content"]),
        properties=dict(properties),
        created_event_id=target.created_event_id,
    )


def _objects(connection: sqlite3.Connection) -> dict[str, LedgerObject]:
    rows = connection.execute(
        "SELECT id, kind, canonical_name, content_format, content, "
        "properties_json, created_event_id FROM objects"
    )
    return {
        str(row["id"]): _apply_latest_correction(connection, _object_from_row(row))
        for row in rows
    }


def resolve_object(
    connection: sqlite3.Connection,
    identifier: str,
) -> LedgerObject:
    objects = _objects(connection)
    target = objects.get(identifier)
    if target is None:
        alias = connection.execute(
            "SELECT object_id FROM aliases WHERE alias = ?", (identifier,)
        ).fetchone()
        if alias is not None:
            target = objects.get(str(alias["object_id"]))
        matches = (
            []
            if target is not None
            else [
                candidate
                for candidate in objects.values()
                if candidate.canonical_name == identifier
                or candidate.properties.get("declaration") == identifier
            ]
        )
        if target is None and len(matches) != 1:
            raise PacketError(
                f"object lookup for {identifier!r} matched {len(matches)} records"
            )
        if target is None:
            target = matches[0]
    return target


def _events_for(
    connection: sqlite3.Connection, object_ids: Iterable[str]
) -> dict[str, list[sqlite3.Row]]:
    identifiers = tuple(dict.fromkeys(object_ids))
    if not identifiers:
        return {}
    placeholders = ",".join("?" for _ in identifiers)
    rows = connection.execute(
        f"SELECT id, subject_id, action, payload_json, evidence_object_id "
        f"FROM events WHERE subject_id IN ({placeholders}) ORDER BY id",
        identifiers,
    )
    result: dict[str, list[sqlite3.Row]] = {item: [] for item in identifiers}
    for row in rows:
        result[str(row["subject_id"])].append(row)
    return result


def _direct_state(events: Sequence[sqlite3.Row]) -> ExactState:
    proved = False
    lifecycle = "active"
    verification = "untested"
    for event in events:
        action = str(event["action"])
        if action == "kernel_certified":
            proved = True
        elif action == "object_retracted":
            proved = False
            lifecycle = "retracted"
        elif action == "object_superseded":
            lifecycle = "superseded"
        candidate = VERIFICATION_EVENTS.get(action)
        if (
            candidate is not None
            and VERIFICATION_RANK[candidate] > (VERIFICATION_RANK[verification])
        ):
            verification = candidate
    return ExactState(
        truth="proved" if proved else "open",
        verification=verification,
        lifecycle=lifecycle,
    )


def _exact_proposition_state(
    connection: sqlite3.Connection,
    target: LedgerObject,
    objects: Mapping[str, LedgerObject],
) -> ExactState:
    """Combine direct events with exact-proposition twins only."""

    proposition = target.properties.get("proposition_sha256")
    twins = [target.id]
    if isinstance(proposition, str) and proposition:
        twins.extend(
            item.id
            for item in objects.values()
            if item.kind == "formal_claim"
            and item.properties.get("proposition_sha256") == proposition
            and item.id != target.id
        )
    event_map = _events_for(connection, twins)
    states = [_direct_state(event_map.get(item, [])) for item in twins]
    lifecycle = _direct_state(event_map.get(target.id, [])).lifecycle
    verification = max(
        (item.verification for item in states),
        key=lambda value: VERIFICATION_RANK[value],
    )
    return ExactState(
        "proved" if any(item.truth == "proved" for item in states) else "open",
        verification,
        lifecycle,
    )


def exact_state(
    connection: sqlite3.Connection,
    target: LedgerObject,
    *,
    all_objects: Mapping[str, LedgerObject] | None = None,
) -> ExactState:
    """Derive state from exact events and kernel-trusted refutation edges.

    A formal goal also inherits a proof receipt from an exact proposition twin
    with the same proposition hash.  Semantic annotations are never consulted.
    """

    objects = dict(all_objects) if all_objects is not None else _objects(connection)
    direct = _exact_proposition_state(connection, target, objects)
    if direct.truth == "proved":
        return direct

    refuters = connection.execute(
        "SELECT c.from_id FROM connections c "
        "WHERE c.to_id = ? AND c.relation = 'refutes' "
        "AND c.retracted_event_id IS NULL "
        "AND json_extract(c.properties_json, '$.trust_level') = 'kernel'",
        (target.id,),
    ).fetchall()
    for refuter in refuters:
        source_id = str(refuter["from_id"])
        source = objects.get(source_id)
        if source is None:
            raise PacketError(f"refutation source {source_id} is missing")
        source_state = _exact_proposition_state(connection, source, objects)
        if source_state.truth == "proved":
            verification = max(
                direct.verification,
                source_state.verification,
                key=lambda value: VERIFICATION_RANK[value],
            )
            return ExactState("refuted", verification, direct.lifecycle)

    # Historical catalog imports encode a refutation as a proved relation
    # goal ``witness -> not target`` plus its proved witness.  The ledger's
    # active decomposition edge identifies that relation without consulting
    # the legacy JSON catalog.  Require the exact canonical contract before
    # applying it, so metadata alone can never refute a goal.
    relation_rows = connection.execute(
        "SELECT to_id FROM connections "
        "WHERE from_id = ? AND relation = 'decomposes_into' "
        "AND retracted_event_id IS NULL ORDER BY created_event_id, id",
        (target.id,),
    ).fetchall()
    for row in relation_rows:
        relation = objects.get(str(row["to_id"]))
        if relation is None or relation.properties.get("legacy_kind") != "refutation":
            continue
        dependency_rows = connection.execute(
            "SELECT to_id FROM connections "
            "WHERE from_id = ? AND relation = 'depends_on' "
            "AND retracted_event_id IS NULL ORDER BY created_event_id, id",
            (relation.id,),
        ).fetchall()
        if len(dependency_rows) != 1:
            raise PacketError(
                f"canonical refutation relation {relation.id} needs one premise"
            )
        witness_id = str(dependency_rows[0]["to_id"])
        witness = objects.get(witness_id)
        if witness is None:
            raise PacketError(
                f"canonical refutation relation {relation.id} has missing premise"
            )
        expected = f"({witness.content}) → ¬ ({target.content})"
        if " ".join(relation.content.split()) != " ".join(expected.split()):
            raise PacketError(
                f"canonical refutation relation {relation.id} has wrong contract"
            )
        relation_state = _exact_proposition_state(connection, relation, objects)
        witness_state = _exact_proposition_state(connection, witness, objects)
        if relation_state.truth == "proved" and witness_state.truth == "proved":
            verification = max(
                relation_state.verification,
                witness_state.verification,
                key=lambda value: VERIFICATION_RANK[value],
            )
            return ExactState("refuted", verification, direct.lifecycle)
    return direct


def _active_edges(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute(
        "SELECT id, from_id, relation, to_id, properties_json, "
        "created_event_id FROM connections WHERE retracted_event_id IS NULL "
        "ORDER BY created_event_id, id"
    ).fetchall()


def dependency_neighborhood(
    connection: sqlite3.Connection,
    focus_id: str,
    *,
    depth: int = 3,
    maximum: int = 80,
) -> tuple[str, ...]:
    """Return a bounded deterministic relevance neighborhood."""

    edges = _active_edges(connection)
    outgoing: dict[str, list[tuple[str, str]]] = {}
    incoming: dict[str, list[tuple[str, str]]] = {}
    for edge in edges:
        relation = str(edge["relation"])
        if relation not in NEIGHBOR_RELATIONS:
            continue
        source, target = str(edge["from_id"]), str(edge["to_id"])
        outgoing.setdefault(source, []).append((relation, target))
        incoming.setdefault(target, []).append((relation, source))

    seen = {focus_id}
    queue: deque[tuple[str, int]] = deque([(focus_id, 0)])
    ordered = [focus_id]
    while queue and len(ordered) < maximum:
        current, distance = queue.popleft()
        if distance >= depth:
            continue
        neighbors = [
            target
            for relation, target in outgoing.get(current, [])
            if relation in NEIGHBOR_RELATIONS
        ]
        neighbors.extend(
            source
            for relation, source in incoming.get(current, [])
            if relation in NEIGHBOR_RELATIONS
        )
        for neighbor in sorted(dict.fromkeys(neighbors)):
            if neighbor in seen:
                continue
            seen.add(neighbor)
            ordered.append(neighbor)
            queue.append((neighbor, distance + 1))
            if len(ordered) >= maximum:
                break
    return tuple(ordered)


def _as_text(value: object) -> str:
    return str(value).strip() if isinstance(value, str) and value.strip() else ""


def _as_text_list(value: object) -> tuple[str, ...]:
    if isinstance(value, str) and value.strip():
        return (value.strip(),)
    if isinstance(value, list):
        return tuple(
            str(item).strip()
            for item in value
            if isinstance(item, str) and item.strip()
        )
    return ()


def _semantic_annotation(
    connection: sqlite3.Connection,
    target_id: str,
    objects: Mapping[str, LedgerObject],
) -> tuple[dict[str, Any], tuple[str, ...]]:
    """Load the newest explicit semantic card and its supersession trail."""

    rows = connection.execute(
        "SELECT c.from_id, c.created_event_id FROM connections c "
        "JOIN objects o ON o.id = c.from_id "
        "WHERE c.to_id = ? AND c.relation = 'annotates' "
        "AND c.retracted_event_id IS NULL AND o.kind = 'annotation' "
        "ORDER BY c.created_event_id DESC",
        (target_id,),
    ).fetchall()
    provenance: list[str] = []
    for row in rows:
        annotation = objects.get(str(row["from_id"]))
        if annotation is None:
            continue
        if annotation.properties.get("role") != "semantic_theorem_card":
            continue
        card = annotation.properties.get("card")
        if not isinstance(card, dict):
            continue
        provenance.append(annotation.id)
        return dict(card), tuple(provenance)
    return {}, ()


def _local_locations(
    connection: sqlite3.Connection, artifact_ids: Iterable[str]
) -> tuple[str, ...]:
    identifiers = tuple(dict.fromkeys(artifact_ids))
    if not identifiers:
        return ()
    placeholders = ",".join("?" for _ in identifiers)
    rows = connection.execute(
        f"SELECT object_id, location FROM artifact_locations "
        f"WHERE object_id IN ({placeholders}) AND state != 'missing' "
        "ORDER BY object_id, location",
        identifiers,
    )
    root = REPOSITORY_ROOT.resolve()
    local: list[str] = []
    for row in rows:
        location = Path(str(row["location"]))
        try:
            resolved = location.resolve(strict=False)
        except OSError:
            continue
        if resolved == root or resolved.is_relative_to(root):
            local.append(str(resolved))
    return tuple(dict.fromkeys(local))[:4]


def _source_locations(
    declaration: str,
    search_root: Path | None = None,
    *,
    exact_only: bool = False,
) -> tuple[str, ...]:
    if not declaration or not re.fullmatch(
        r"[A-Za-z_][A-Za-z0-9_']*(?:\.[A-Za-z_][A-Za-z0-9_']*)*",
        declaration,
    ):
        return ()
    short = declaration.rsplit(".", 1)[-1]

    def _spelling(name: str) -> re.Pattern[str]:
        return re.compile(
            rf"^\s*(?:@\[[^\]]*\]\s*)?(?:private\s+|protected\s+|noncomputable\s+)*"
            rf"(?:def|theorem|lemma|structure|inductive|abbrev|instance)\s+"
            rf"{re.escape(name)}\b"
        )

    # A fully qualified spelling must win outright: searching for
    # `List.Perm.symm` must not return an unrelated bare `theorem symm`.
    names = [declaration] if exact_only else list(dict.fromkeys((declaration, short)))
    spellings = [_spelling(name) for name in names]
    roots = (
        (search_root,) if search_root is not None else (REPOSITORY_ROOT / "formal/lean",)
    )
    candidates: Iterable[Path]
    for pattern in spellings:
        matches: list[str] = []
        for root in roots:
            candidates = (root,) if root.is_file() else sorted(root.rglob("*.lean"))
            for path in candidates:
                try:
                    for line_number, line in enumerate(
                        path.read_text(encoding="utf-8").splitlines(), start=1
                    ):
                        if pattern.search(line):
                            matches.append(f"{path.resolve()}:{line_number}")
                            break
                except (OSError, UnicodeDecodeError):
                    continue
            if matches:
                break
        if matches:
            return tuple(matches[:4])
    return ()


def theorem_card(
    connection: sqlite3.Connection,
    target: LedgerObject,
    *,
    all_objects: Mapping[str, LedgerObject] | None = None,
) -> SemanticTheoremCard:
    objects = dict(all_objects) if all_objects is not None else _objects(connection)
    semantic, semantic_ids = _semantic_annotation(connection, target.id, objects)
    state = exact_state(connection, target, all_objects=objects)
    dependencies = tuple(
        str(row["to_id"])
        for row in connection.execute(
            "SELECT to_id FROM connections WHERE from_id = ? "
            "AND relation = 'depends_on' AND retracted_event_id IS NULL "
            "ORDER BY created_event_id, id",
            (target.id,),
        )
    )
    receipt_ids = tuple(
        str(row["to_id"])
        for row in connection.execute(
            "SELECT to_id FROM connections WHERE from_id = ? "
            "AND relation = 'certified_by' AND retracted_event_id IS NULL "
            "ORDER BY created_event_id DESC, id DESC",
            (target.id,),
        )
    )
    proposition = _as_text(target.properties.get("proposition_sha256"))
    if not receipt_ids and proposition:
        twins = [
            item.id
            for item in objects.values()
            if item.kind == "formal_claim"
            and item.properties.get("proposition_sha256") == proposition
        ]
        if twins:
            placeholders = ",".join("?" for _ in twins)
            receipt_ids = tuple(
                str(row["to_id"])
                for row in connection.execute(
                    f"SELECT to_id FROM connections WHERE from_id IN "
                    f"({placeholders}) AND relation = 'certified_by' "
                    "AND retracted_event_id IS NULL "
                    "ORDER BY created_event_id DESC, id DESC",
                    tuple(twins),
                )
            )
    declaration_property = _as_text(target.properties.get("declaration"))
    explicit_source = _as_text(target.properties.get("source_file"))
    if proposition and not re.fullmatch(
        r"[A-Za-z_][A-Za-z0-9_']*(?:\.[A-Za-z_][A-Za-z0-9_']*)*",
        declaration_property,
    ):
        # Goal objects store an exact target expression in ``declaration``;
        # the proposition-identical certified claim stores the actual Lean
        # declaration name.  Identity comes from the proposition hash, never
        # from a fuzzy title match.
        linked_claim_ids = {
            str(row["from_id"])
            for row in connection.execute(
                "SELECT from_id FROM connections WHERE to_id = ? "
                "AND relation = 'advances' AND retracted_event_id IS NULL",
                (target.id,),
            )
        }
        goal_name = target.id.removeprefix("goal:")
        named_twins = sorted(
            (
                item
                for item in objects.values()
                if item.kind == "formal_claim"
                and (
                    item.properties.get("proposition_sha256") == proposition
                    or (
                        item.id in linked_claim_ids
                        and _as_text(item.properties.get("declaration")) == goal_name
                    )
                )
                and re.fullmatch(
                    r"[A-Za-z_][A-Za-z0-9_']*(?:\.[A-Za-z_][A-Za-z0-9_']*)*",
                    _as_text(item.properties.get("declaration")),
                )
            ),
            key=lambda item: item.created_event_id,
            reverse=True,
        )
        if named_twins:
            declaration_property = _as_text(
                named_twins[0].properties.get("declaration")
            )
            explicit_source = _as_text(named_twins[0].properties.get("source_file"))
    lean_declaration = (
        declaration_property
        if re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_']*(?:\.[A-Za-z_][A-Za-z0-9_']*)*",
            declaration_property,
        )
        else "unavailable"
    )
    ordinary_statement = _as_text(semantic.get("statement"))
    if not ordinary_statement and target.id.startswith("goal:"):
        ordinary_statement = target.canonical_name
    title = _as_text(semantic.get("title")) or target.canonical_name
    explicit_source_path = Path(explicit_source) if explicit_source else None
    explicit_pointers = (
        (str(explicit_source_path.resolve()),)
        if explicit_source_path is not None
        and explicit_source_path.is_file()
        and explicit_source_path.resolve().is_relative_to(REPOSITORY_ROOT.resolve())
        else ()
    )
    source_pointers = tuple(
        dict.fromkeys((*explicit_pointers, *_source_locations(lean_declaration)))
    )[:4]
    return SemanticTheoremCard(
        format=CARD_FORMAT,
        object_id=target.id,
        mathematical_title=title,
        ordinary_statement=ordinary_statement or "unavailable",
        hypotheses=_as_text_list(semantic.get("hypotheses")),
        conclusion=_as_text(semantic.get("conclusion")) or "unavailable",
        proof_mechanism=(_as_text(semantic.get("proof_mechanism")) or "unavailable"),
        useful_consequences=_as_text_list(semantic.get("useful_consequences")),
        research_significance=(
            _as_text(semantic.get("research_significance")) or "unavailable"
        ),
        limitations=_as_text_list(semantic.get("limitations")),
        known_counterexamples=_as_text_list(semantic.get("known_counterexamples")),
        dependencies=dependencies,
        truth_state=state.truth,
        verification_state=state.verification,
        lifecycle_state=state.lifecycle,
        lean_declaration=lean_declaration,
        proposition_sha256=proposition or "unavailable",
        exact_contract=target.content,
        source_pointers=source_pointers,
        receipt_pointers=_local_locations(connection, receipt_ids),
        semantic_provenance=semantic_ids,
    )


def _render_list(values: Sequence[str]) -> str:
    return "; ".join(values) if values else "unavailable"


def render_card(card: SemanticTheoremCard) -> str:
    lines = [
        f"### {card.mathematical_title}",
        "",
        f"- canonical object: `{card.object_id}`",
        f"- mathematical statement: {card.ordinary_statement}",
        f"- hypotheses: {_render_list(card.hypotheses)}",
        f"- conclusion: {card.conclusion}",
        f"- proof mechanism: {card.proof_mechanism}",
        f"- useful consequences: {_render_list(card.useful_consequences)}",
        f"- research significance: {card.research_significance}",
        f"- limitations/nonclaims: {_render_list(card.limitations)}",
        f"- counterexamples to stronger variants: "
        f"{_render_list(card.known_counterexamples)}",
        f"- dependencies: {_render_list(card.dependencies)}",
        f"- exact state: truth=`{card.truth_state}`, "
        f"verification=`{card.verification_state}`, "
        f"lifecycle=`{card.lifecycle_state}`",
        f"- Lean declaration: `{card.lean_declaration}`",
        f"- proposition SHA-256: `{card.proposition_sha256}`",
        f"- exact source: {_render_list(card.source_pointers)}",
        f"- authoritative receipts: {_render_list(card.receipt_pointers)}",
        f"- semantic prose provenance: {_render_list(card.semantic_provenance)}",
    ]
    return "\n".join(lines)


def _global_map(
    connection: sqlite3.Connection,
    objects: Mapping[str, LedgerObject],
    focus: LedgerObject,
    neighborhood: Sequence[str],
) -> str:
    states: dict[str, list[LedgerObject]] = {
        "proved": [],
        "open": [],
        "refuted": [],
    }
    for identifier in neighborhood:
        target = objects.get(identifier)
        if target is None or target.kind != "formal_claim":
            continue
        state = exact_state(connection, target, all_objects=objects).truth
        states.setdefault(state, []).append(target)
    focus_state = exact_state(connection, focus, all_objects=objects)
    counts = ", ".join(f"{name}={len(items)}" for name, items in states.items())
    proved = (
        "; ".join(
            f"`{item.id}` — {item.canonical_name}" for item in states["proved"][:8]
        )
        or "none selected"
    )
    refuted = (
        "; ".join(
            f"`{item.id}` — {item.canonical_name}" for item in states["refuted"][:6]
        )
        or "none selected"
    )
    lines = [
        "# Global mathematical map",
        "",
        "This is a domain-neutral projection of the selected ledger "
        "neighborhood. Mathematical strategy appears only when it is present "
        "in canonical annotations below; the packet builder does not invent it.",
        "",
        f"- focus: `{focus.id}` — {focus.canonical_name}",
        f"- focus exact state: truth=`{focus_state.truth}`, "
        f"verification=`{focus_state.verification}`",
        f"- selected formal neighborhood: {counts}",
        f"- settled results selected for this packet: {proved}",
        f"- kernel-backed refutations selected for this packet: {refuted}",
        "- open children, assembly obligations, and canonical research "
        "findings are listed in dedicated sections below.",
    ]
    return "\n".join(lines)


def _research_findings(
    connection: sqlite3.Connection,
    neighborhood: set[str],
    objects: Mapping[str, LedgerObject],
    *,
    maximum: int,
) -> list[LedgerObject]:
    rows = (
        connection.execute(
            "SELECT DISTINCT o.id, o.kind, o.canonical_name, o.content_format, "
            "o.content, o.properties_json, o.created_event_id "
            "FROM objects o JOIN connections c ON c.from_id = o.id "
            "WHERE o.kind IN ('research_claim', 'annotation', 'counterexample') "
            "AND COALESCE(json_extract(o.properties_json, '$.role'), '') "
            "!= 'semantic_theorem_card' "
            "AND c.to_id IN ("
            + ",".join("?" for _ in neighborhood)
            + ") AND c.retracted_event_id IS NULL "
            "AND c.relation IN ('advances','supports','annotates','refutes') "
            "ORDER BY o.created_event_id DESC LIMIT ?",
            (*sorted(neighborhood), maximum),
        ).fetchall()
        if neighborhood
        else []
    )
    # Historical prose can retain obsolete removable-volume locations.  Such
    # records remain canonical history but are not valid current retrieval
    # guidance, so the laptop-local projection omits them rather than emitting
    # a stale path or silently rewriting the underlying record.
    return [
        item
        for item in (
            _apply_latest_correction(connection, _object_from_row(row)) for row in rows
        )
        if "/Volumes/" not in item.content
        and item.properties.get("role") != "semantic_theorem_card"
    ]


def _open_obligations(
    connection: sqlite3.Connection,
    focus_id: str,
    objects: Mapping[str, LedgerObject],
) -> str:
    rows = list(
        connection.execute(
            "SELECT from_id, relation, to_id FROM connections "
            "WHERE retracted_event_id IS NULL "
            "AND relation IN ('depends_on','decomposes_into') "
            "ORDER BY created_event_id, id"
        )
    )
    outgoing: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        outgoing.setdefault(str(row["from_id"]), []).append(row)
    queue: deque[tuple[str, int]] = deque([(focus_id, 0)])
    seen_sources = {focus_id}
    selected: list[sqlite3.Row] = []
    while queue and len(selected) < 30:
        source, depth = queue.popleft()
        if depth >= 3:
            continue
        for row in outgoing.get(source, []):
            target = str(row["to_id"])
            target_object = objects.get(target)
            if target_object is None or target_object.kind != "formal_claim":
                continue
            selected.append(row)
            if target not in seen_sources:
                seen_sources.add(target)
                queue.append((target, depth + 1))
    items: list[str] = []
    seen_targets: set[str] = set()
    for row in selected:
        source, target = str(row["from_id"]), str(row["to_id"])
        if target in seen_targets:
            continue
        target_object = objects.get(target)
        if target_object is None or target_object.kind != "formal_claim":
            continue
        state = exact_state(connection, target_object, all_objects=objects)
        if state.truth != "open":
            continue
        seen_targets.add(target)
        items.append(
            f"- `{target}` via `{row['relation']}` from `{source}`: "
            f"{target_object.canonical_name}"
        )
    return "\n".join(dict.fromkeys(items)) or "- none recorded"


def _retrieval_instructions() -> str:
    command = (
        "python input_snapshot/formal/shinka/spotlight_packet.py "
        "--ledger canonical_ledger_input.sqlite3"
    )
    return "\n".join(
        [
            "# Exact Lean retrieval (read-only)",
            "",
            "These commands query the WHOLE canonical ledger, not just the "
            "cards above. The cards are a small selected projection; the "
            "ledger holds every recorded declaration, claim, and receipt. Use "
            "these commands to explore the wider project as well as to confirm "
            "a signature. Do not reconstruct signatures from prose, and prefer "
            "these over paging raw Lean with `sed` or `rg`.",
            "",
            "Start here when you do not already know a name:",
            "",
            f"- index the whole promoted frontier source (~4k tokens for every "
            f"declaration): `{command} outline --file "
            "compiled_checkpoint_input/frontier.lean`",
            f"- narrow that index: `{command} outline --grep cycle "
            "--kind theorem`",
            f"- index your own parent or an earlier candidate: "
            f"`{command} outline --file best/main.lean`",
            f"- enumerate ledger claims: `{command} list --kind formal_claim "
            "--state proved`",
            f"- narrow the listing: `{command} list --grep cycle --limit 40`",
            f"- ranked full-text search with snippets: `{command} search 'PHRASE'`",
            "",
            "`outline` reads the Lean source itself and reports each "
            "declaration with its kind, exact `file:line`, and ledger truth "
            "state, including declarations marked `not_in_ledger` that no "
            "ledger object covers. Prefer it over `sed` or `rg`: it indexes "
            "the entire frontier for a fraction of one bulk read. With "
            "`--library` it indexes the pinned Lean toolchain source instead, "
            "which is how to find an existing standard-library lemma rather "
            "than reproving it. A dotted name is matched exactly first; if "
            "only its final component matches, the result is labelled as a "
            "possibly unrelated bare-name match.",
            "",
            "Then retrieve exact records:",
            "",
            f"- exact signature/contract: `{command} signature OBJECT_OR_DECL`",
            f"- exact definition and nearby source: `{command} definition NAME`",
            f"- page a long declaration: `{command} definition NAME --offset 120`",
            f"- pull a declaration out of an earlier candidate: "
            f"`{command} definition NAME --file PATH/main.lean`",
            f"- find a Lean library lemma: `{command} outline --library "
            "--grep erase_cons`",
            f"- read that lemma's exact statement: `{command} definition "
            "Perm.symm --library`",
            f"- source surrounding a declaration: `{command} source NAME`",
            f"- authoritative receipts and state: `{command} receipts OBJECT_ID`",
            f"- full exact goal contract: `{command} contract GOAL_ID`",
            f"- labelled relations in both directions: `{command} relations "
            "OBJECT_ID`",
            "",
            "`relations` shows how objects connect — `decomposes_into`, "
            "`advances`, `refutes`, `certified_by`, and every other recorded "
            "edge — which `neighborhood` does not report.",
            "",
            "Two commands are much more expensive than the rest; reach for "
            "them deliberately, not by default:",
            "",
            f"- dependency neighborhood (~6k tokens at the default depth): "
            f"`{command} neighborhood OBJECT_ID --limit 40`",
            f"- re-aim this whole projection (~12k tokens): "
            f"`{command} packet OTHER_GOAL_ID`",
            "",
            "`search` tokenizes snake_case and CamelCase, so 'nearly disjoint "
            "family' matches `NearlyDisjointCycleFamily`. `definition` returns "
            "a whole declaration and tells you how to page the rest, and never "
            "emits EVOLVE-BLOCK markers — use it with `--file` to recover work "
            "from an earlier candidate rather than copying a raw slice, which "
            "drags markers along and the scratch checker rejects. `list` "
            "defaults to 80 rows; narrow with `--grep` rather than raising "
            "`--limit`, because a full listing costs thousands of tokens. A "
            "declaration with local source but no ledger object is reported "
            "with truth `unknown` and `ledger_backed: false`; that is source "
            "text, never a receipt.",
            "",
            "The promoted frontier and compiled checkpoint stay out of the "
            "prompt. Lean can still import the checkpoint normally.",
        ]
    )


def _section(name: str, text: str, provenance: Sequence[str]) -> dict[str, Any]:
    encoded = text.encode("utf-8")
    return {
        "name": name,
        "chars": len(text),
        "bytes": len(encoded),
        "sha256": _sha256(encoded),
        "provenance": list(provenance),
    }


def build_spotlight_packet(
    database: Path,
    focus_identifier: str,
    *,
    exact_contract: str | None = None,
    field_context_path: Path | None = None,
    mode: str = "solve",
    hard_cap_bytes: int | None = None,
) -> SpotlightPacket:
    """Build a deterministic, budgeted mathematical projection."""

    if mode not in {"solve", "field_expansion"}:
        raise PacketError(f"unsupported packet mode: {mode}")
    maximum = hard_cap_bytes or (
        NORMAL_PACKET_SOFT_TARGET_BYTES
        if mode == "solve"
        else FIELD_PACKET_HARD_CAP_BYTES
    )
    if maximum <= 0 or maximum > NORMAL_PROMPT_HARD_CAP_BYTES:
        raise PacketError(
            "packet hard cap must be positive and no greater than "
            f"{NORMAL_PROMPT_HARD_CAP_BYTES:,} bytes"
        )
    with _connect_read_only(database) as connection:
        ledger_head = connection.execute(
            "SELECT id, event_hash FROM events ORDER BY id DESC LIMIT 1"
        ).fetchone()
        objects = _objects(connection)
        focus = resolve_object(connection, focus_identifier)
        contract = exact_contract if exact_contract is not None else focus.content
        if not contract.strip():
            raise PacketError(f"focus {focus.id} has no exact contract")
        ledger_contract = focus.content.strip()
        if exact_contract is not None and ledger_contract != exact_contract.strip():
            raise PacketError(
                "spotlight catalog contract does not match canonical ledger "
                f"object {focus.id}; refusing a split-brain prompt"
            )
        neighborhood = dependency_neighborhood(connection, focus.id)
        neighborhood_set = set(neighborhood)

        global_map = _global_map(connection, objects, focus, neighborhood)
        metadata = {
            key: focus.properties[key]
            for key in (
                "route",
                "strategic_scope",
                "subnet",
                "fixed_parameter_credit",
            )
            if key in focus.properties
        }
        contract_section = "\n".join(
            [
                "# Exact spotlight goal contract",
                "",
                f"Canonical goal: `{focus.id}`",
                f"Proposition SHA-256: "
                f"`{focus.properties.get('proposition_sha256', 'unavailable')}`",
                "",
                "```lean",
                contract.rstrip(),
                "```",
                "",
                "This contract is exact and untruncated. Semantic prose never "
                "changes it.",
                "Canonical research metadata: "
                + (json.dumps(metadata, sort_keys=True) if metadata else "unavailable"),
            ]
        )

        card_objects: list[LedgerObject] = []
        direct_focus = {
            identifier
            for edge in _active_edges(connection)
            for identifier in (
                (str(edge["to_id"]),)
                if str(edge["from_id"]) == focus.id
                else ((str(edge["from_id"]),) if str(edge["to_id"]) == focus.id else ())
            )
        }
        for object_id in neighborhood:
            item = objects.get(object_id)
            if item is None or item.kind != "formal_claim":
                continue
            state = exact_state(connection, item, all_objects=objects)
            semantic, _semantic_ids = _semantic_annotation(connection, item.id, objects)
            if (
                item.id == focus.id
                or bool(semantic)
                or (
                    state.truth in {"proved", "refuted"}
                    and (item.id.startswith("goal:") or item.id in direct_focus)
                )
            ):
                card_objects.append(item)
        selected_goal_names = {
            item.id.removeprefix("goal:")
            for item in card_objects
            if item.id.startswith("goal:")
        }
        card_objects = [
            item
            for item in card_objects
            if not (
                item.id.startswith("claim:")
                and _as_text(item.properties.get("declaration")) in selected_goal_names
                and not _semantic_annotation(connection, item.id, objects)[0]
            )
        ]
        by_semantic_annotation: dict[str, LedgerObject] = {}
        without_semantic_annotation: list[LedgerObject] = []
        for item in card_objects:
            _semantic, semantic_ids = _semantic_annotation(connection, item.id, objects)
            if not semantic_ids:
                without_semantic_annotation.append(item)
                continue
            key = semantic_ids[0]
            existing = by_semantic_annotation.get(key)
            if existing is None or (
                item.id.startswith("goal:") and not existing.id.startswith("goal:")
            ):
                by_semantic_annotation[key] = item
        card_objects = [
            *without_semantic_annotation,
            *by_semantic_annotation.values(),
        ]
        neighborhood_order = {
            identifier: index for index, identifier in enumerate(neighborhood)
        }
        card_objects.sort(
            key=lambda item: neighborhood_order.get(item.id, len(neighborhood))
        )
        cards = [
            theorem_card(connection, item, all_objects=objects)
            for item in card_objects[:20]
        ]
        card_texts = [render_card(card) for card in cards]
        cards_section = "# Relevant theorem cards\n\n" + (
            "\n\n".join(card_texts)
            if card_texts
            else "No explicit theorem card is available in this neighborhood."
        )
        findings = _research_findings(connection, neighborhood_set, objects, maximum=14)
        findings_section = "\n".join(
            [
                "# Recent relevant findings, refutations, and dead routes",
                "",
                *(
                    [
                        f"- `{item.id}` — {item.canonical_name}: {item.content}"
                        for item in findings
                    ]
                    or ["- none recorded in the selected neighborhood"]
                ),
            ]
        )
        obligations_section = "\n".join(
            [
                "# Open children and assembly obligations",
                "",
                _open_obligations(connection, focus.id, objects),
            ]
        )
        retrieval_section = _retrieval_instructions()
        delta_section = "\n".join(
            [
                "# Editable Lean delta",
                "",
                "The current program supplied by Shinka is the only editable "
                "Lean source in this prompt. It imports the verified compiled "
                "checkpoint. Do not copy settled source into the delta.",
            ]
        )
        advisory_section = ""
        advisory_provenance: list[str] = []
        if field_context_path is not None and field_context_path.is_file():
            advisory = field_context_path.read_text(encoding="utf-8")
            digest = _sha256(advisory.encode("utf-8"))
            advisory_provenance.append(str(field_context_path.resolve()))
            # Advisory context may be compacted; exact contracts and editable
            # code are handled separately and are never truncated here.
            if len(advisory.encode("utf-8")) <= 24_000:
                body = advisory.rstrip()
            else:
                body = (
                    "Prior advisory context is available by path and hash; its "
                    "full recursive body is intentionally not re-embedded."
                )
            advisory_section = "\n".join(
                [
                    "# Immediately preceding advisory context",
                    "",
                    f"Path: `{field_context_path.resolve()}`",
                    f"SHA-256: `{digest}`",
                    "",
                    body,
                ]
            )

    sections: list[tuple[str, str, Sequence[str]]] = [
        ("global_map", global_map, (str(database.resolve()),)),
        ("exact_contract", contract_section, (focus.id,)),
        (
            "theorem_cards",
            cards_section,
            tuple(card.object_id for card in cards),
        ),
        (
            "recent_findings",
            findings_section,
            tuple(item.id for item in findings),
        ),
        ("open_obligations", obligations_section, neighborhood),
        ("retrieval", retrieval_section, (str(database.resolve()),)),
        ("editable_delta_policy", delta_section, ()),
    ]
    if advisory_section:
        sections.append(("advisory_context", advisory_section, advisory_provenance))
    rendered = (
        "\n\n".join(text for _name, text, _provenance in sections).rstrip() + "\n"
    )
    encoded = rendered.encode("utf-8")
    if len(encoded) > maximum:
        breakdown = ", ".join(
            f"{name}={len(text.encode('utf-8'))}B"
            for name, text, _provenance in sections
        )
        raise PromptBudgetError(
            f"semantic spotlight packet is {len(encoded):,} bytes, over the "
            f"{maximum:,}-byte cap ({breakdown}); exact contract was not "
            "truncated"
        )
    section_records = [
        _section(name, text, provenance) for name, text, provenance in sections
    ]
    manifest = {
        "format": MANIFEST_FORMAT,
        "packet_format": PACKET_FORMAT,
        "mode": mode,
        "focus_id": focus.id,
        "focus_proposition_sha256": focus.properties.get("proposition_sha256"),
        "ledger": str(database.resolve()),
        "ledger_head_event_id": int(ledger_head["id"]) if ledger_head else 0,
        "ledger_head_hash": str(ledger_head["event_hash"]) if ledger_head else "0" * 64,
        "total_chars": len(rendered),
        "total_bytes": len(encoded),
        "sha256": _sha256(encoded),
        "hard_cap_bytes": maximum,
        "sections": section_records,
        "excluded_by_design": [
            "full promoted frontier source",
            "compiled checkpoint source",
            "complete goal catalog",
            "complete prior programs",
            "repeated accepted-goal lists",
            "duplicated campaign histories",
        ],
        "truth_policy": (
            "semantic prose is advisory; truth and verification are derived "
            "only from canonical ledger events and kernel-trusted refutations"
        ),
    }
    return SpotlightPacket(rendered, manifest)


def enforce_complete_prompt_budget(
    system: str,
    user: str,
    *,
    cap_bytes: int = NORMAL_PROMPT_HARD_CAP_BYTES,
) -> dict[str, Any]:
    system_bytes = len(system.encode("utf-8"))
    user_bytes = len(user.encode("utf-8"))
    total = system_bytes + user_bytes
    if total > cap_bytes:
        current_marker = "# Current program"
        current_bytes = 0
        if current_marker in user:
            current_bytes = len(user[user.index(current_marker) :].encode("utf-8"))
        raise PromptBudgetError(
            f"model prompt is {total:,} bytes, over the {cap_bytes:,}-byte "
            f"hard cap (system={system_bytes:,}, user={user_bytes:,}, "
            f"current-program-tail≈{current_bytes:,}). Exact contracts and "
            "editable code were not truncated. Start from the compact "
            "checkpoint delta or reduce advisory history."
        )
    return {
        "format": "shinka-complete-prompt-budget-v1",
        "hard_cap_bytes": cap_bytes,
        "system_bytes": system_bytes,
        "user_bytes": user_bytes,
        "total_bytes": total,
        "sha256": _sha256((system + user).encode("utf-8")),
    }


def snapshot_database(source: Path, destination: Path) -> None:
    """Take a transactionally consistent SQLite backup, including WAL data."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.unlink(missing_ok=True)
    with _connect_read_only(source) as origin:
        target = sqlite3.connect(temporary)
        try:
            origin.backup(target)
        finally:
            target.close()
    temporary.replace(destination)


def verify_packet_ledger_snapshot(database: Path, manifest: Mapping[str, Any]) -> None:
    """Require a persisted ledger snapshot to be the packet's exact view."""

    with _connect_read_only(database) as connection:
        row = connection.execute(
            "SELECT id, event_hash FROM events ORDER BY id DESC LIMIT 1"
        ).fetchone()
    actual_id = int(row["id"]) if row else 0
    actual_hash = str(row["event_hash"]) if row else "0" * 64
    expected_id = manifest.get("ledger_head_event_id")
    expected_hash = manifest.get("ledger_head_hash")
    if actual_id != expected_id or actual_hash != expected_hash:
        raise PacketError(
            "canonical ledger changed between spotlight-packet projection "
            "and run snapshot: "
            f"packet head=({expected_id}, {expected_hash}), "
            f"snapshot head=({actual_id}, {actual_hash})"
        )


TOP_LEVEL_DECLARATION = re.compile(
    r"^\s*(?:@\[[^\]]*\]\s*)?(?:private\s+|protected\s+|noncomputable\s+)*"
    r"(?:def|theorem|lemma|structure|inductive|abbrev|instance|example)\b"
)


def _declaration_extent(lines: Sequence[str], start_index: int) -> int:
    """Return the exclusive end index of the declaration opening at ``start``.

    A declaration runs until the next top-level declaration, so the whole
    statement and proof are returned instead of a fixed window that silently
    truncates the conclusion.
    """

    for index in range(start_index + 1, len(lines)):
        if TOP_LEVEL_DECLARATION.match(lines[index]):
            return index
    return len(lines)


EVOLVE_MARKERS = ("-- EVOLVE-BLOCK-START", "-- EVOLVE-BLOCK-END")


def _source_excerpt(
    declaration: str,
    *,
    max_lines: int = 120,
    offset: int = 0,
    context_lines: int = 2,
    search_root: Path | None = None,
) -> str:
    locations = _source_locations(declaration, search_root)
    if not locations:
        raise PacketError(f"no local source found for {declaration!r}")
    if max_lines <= 0:
        raise PacketError("--max-lines must be positive")
    if offset < 0:
        raise PacketError("--offset must not be negative")
    location = locations[0]
    qualified = _source_locations(declaration, search_root, exact_only=True)
    path_text, line_text = location.rsplit(":", 1)
    path = Path(path_text)
    line_number = int(line_text)
    lines = path.read_text(encoding="utf-8").splitlines()
    start_index = line_number - 1
    end_index = _declaration_extent(lines, start_index)
    # A couple of leading lines keep any attribute or docstring visible without
    # spending the budget on the tail of the previous declaration.
    lead = max(0, start_index - context_lines)
    body_start = min(start_index + offset, end_index)
    body_end = min(body_start + max_lines, end_index)
    window = list(range(lead, start_index)) if offset == 0 else []
    # Encoded research annotations are single lines thousands of characters
    # long. They carry no Lean meaning and would consume the whole first page.
    window = [
        index
        for index in window
        if not lines[index].lstrip().startswith(RESEARCH_ANNOTATION_PREFIX)
    ]
    window.extend(range(body_start, body_end))
    # Evolve markers belong to a candidate's structure, not to a declaration.
    # Emitting them invites copying them into an append snippet, which the
    # scratch checker rejects.
    window = [
        index
        for index in window
        if not any(marker in lines[index] for marker in EVOLVE_MARKERS)
    ]
    numbered = [f"{index + 1:6d}  {lines[index]}" for index in window]
    total = end_index - start_index
    shown_to = body_end - start_index
    header = f"{location}  ({total} lines in declaration)"
    if "." in declaration and not qualified:
        # The qualified spelling was not found, so this is a bare-name match
        # that may belong to a different namespace entirely.
        header += (
            f"; WARNING: no declaration literally named {declaration!r} was "
            f"found — this is a bare-name match on "
            f"{declaration.rsplit('.', 1)[-1]!r} and may be unrelated"
        )
    if shown_to < total:
        header += (
            f"; showing lines {offset + 1}-{shown_to} — continue with "
            f"`--offset {shown_to}`"
        )
    return f"{header}\n" + "\n".join(numbered)


_IDENTIFIER_SPLIT = re.compile(r"[^A-Za-z0-9]+|(?<=[a-z0-9])(?=[A-Z])")


def _bounded(text: object, limit: int) -> str | None:
    """Clip a retrieval field so one record cannot dominate a prompt.

    Some ledger records carry a whole proposition where a short name is
    expected, so every field rendered into a listing or search hit is bounded.
    """

    if text is None:
        return None
    collapsed = " ".join(str(text).split())
    if not collapsed:
        return None
    if len(collapsed) > limit:
        return collapsed[: limit - 1] + "…"
    return collapsed


def _identifier_words(text: str) -> str:
    """Split snake_case and CamelCase so multi-word queries can match names."""

    return " ".join(part for part in _IDENTIFIER_SPLIT.split(text) if part)


def _fts_query(phrase: str) -> str:
    tokens = [token for token in _IDENTIFIER_SPLIT.split(phrase) if token]
    if not tokens:
        raise PacketError("search phrase has no searchable tokens")
    return " AND ".join(f'"{token}"*' for token in tokens)


def _search_objects(
    connection: sqlite3.Connection,
    phrase: str,
    limit: int,
) -> list[dict[str, Any]]:
    """Rank ledger objects for ``phrase`` with snippets.

    An FTS5 index is built in memory because the canonical ledger is opened
    read-only and must never be modified by a retrieval command.
    """

    objects = _objects(connection)
    rows = [
        (
            identifier,
            item.kind,
            item.canonical_name or "",
            _as_text(item.properties.get("declaration")) or "",
            item.content or "",
        )
        for identifier, item in objects.items()
    ]
    try:
        index = sqlite3.connect(":memory:")
        index.row_factory = sqlite3.Row
        index.execute(
            "CREATE VIRTUAL TABLE fts USING fts5("
            "id UNINDEXED, kind UNINDEXED, name, declaration, body)"
        )
        index.executemany(
            "INSERT INTO fts (id, kind, name, declaration, body) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (
                    identifier,
                    kind,
                    f"{name} {_identifier_words(name)}",
                    f"{declaration} {_identifier_words(declaration)}",
                    body,
                )
                for identifier, kind, name, declaration, body in rows
            ],
        )
        found = index.execute(
            "SELECT id, kind, name, declaration, "
            "snippet(fts, 4, '<<', '>>', '…', 14) AS excerpt "
            "FROM fts WHERE fts MATCH ? ORDER BY bm25(fts) LIMIT ?",
            (_fts_query(phrase), limit),
        ).fetchall()
    except sqlite3.Error:
        # FTS5 is optional in a stripped SQLite build; fall back to the
        # previous substring behaviour rather than failing the retrieval.
        pattern = f"%{phrase.lower()}%"
        found = connection.execute(
            "SELECT id, kind, canonical_name AS name, "
            "properties_json AS declaration, '' AS excerpt FROM objects "
            "WHERE lower(canonical_name) LIKE ? OR lower(content) LIKE ? "
            "OR lower(properties_json) LIKE ? ORDER BY created_event_id DESC "
            "LIMIT ?",
            (pattern, pattern, pattern, limit),
        ).fetchall()
    results: list[dict[str, Any]] = []
    for row in found:
        item = objects.get(str(row["id"]))
        results.append(
            {
                "id": str(row["id"]),
                "kind": str(row["kind"]),
                "canonical_name": _bounded(
                    item.canonical_name if item else None, 120
                ),
                "declaration": _bounded(
                    _as_text(item.properties.get("declaration")) if item else None,
                    96,
                ),
                "excerpt": _bounded(row["excerpt"], 200),
            }
        )
    return results


def _list_objects(
    connection: sqlite3.Connection,
    *,
    kind: str | None,
    state: str | None,
    grep: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    """Enumerate ledger objects so a name never has to be guessed."""

    objects = _objects(connection)
    needle = grep.lower() if grep else None
    results: list[dict[str, Any]] = []
    for identifier, item in sorted(objects.items()):
        if kind and item.kind != kind:
            continue
        declaration = _as_text(item.properties.get("declaration")) or ""
        name = item.canonical_name or ""
        if needle and needle not in f"{name} {declaration} {identifier}".lower():
            continue
        truth = exact_state(connection, item, all_objects=objects)
        if state and truth.truth != state:
            continue
        results.append(
            {
                "id": identifier,
                "kind": item.kind,
                "declaration": declaration or None,
                "canonical_name": name,
                "truth": truth.truth,
                "verification": truth.verification,
            }
        )
        if len(results) >= limit:
            break
    return results


def _relation_rows(
    connection: sqlite3.Connection,
    target: LedgerObject,
    *,
    relation: str | None,
    direction: str,
    limit: int,
) -> list[dict[str, Any]]:
    """Return the actual labelled edges touching ``target``.

    ``neighborhood`` answers which objects are near; this answers how they are
    related, across every relation the ledger records rather than only the
    dependency-traversal subset.
    """

    objects = _objects(connection)
    clauses: list[tuple[str, str]] = []
    if direction in {"out", "both"}:
        clauses.append(("from_id", "out"))
    if direction in {"in", "both"}:
        clauses.append(("to_id", "in"))
    if not clauses:
        raise PacketError("--direction must be in, out, or both")
    rows: list[dict[str, Any]] = []
    for column, heading in clauses:
        query = (
            f"SELECT from_id, relation, to_id FROM connections "
            f"WHERE {column} = ? AND retracted_event_id IS NULL"
        )
        parameters: list[Any] = [target.id]
        if relation:
            query += " AND relation = ?"
            parameters.append(relation)
        query += " ORDER BY relation, id LIMIT ?"
        parameters.append(limit)
        for row in connection.execute(query, parameters):
            other_id = str(row["to_id"] if heading == "out" else row["from_id"])
            other = objects.get(other_id)
            rows.append(
                {
                    "direction": heading,
                    "relation": str(row["relation"]),
                    "other_id": other_id,
                    "other_kind": other.kind if other else None,
                    "other_name": _bounded(
                        other.canonical_name if other else None, 80
                    ),
                    "other_declaration": _bounded(
                        _as_text(other.properties.get("declaration"))
                        if other
                        else None,
                        72,
                    ),
                }
            )
    return rows[:limit]


def _render_relations(target: str, rows: Sequence[Mapping[str, Any]]) -> str:
    if not rows:
        return f"{target}: no recorded relations"
    lines = [f"{target}"]
    for row in rows:
        arrow = "->" if row["direction"] == "out" else "<-"
        label = row["other_declaration"] or row["other_name"] or row["other_id"]
        lines.append(f"  {arrow} {row['relation']:<18} {label}")
    return "\n".join(lines)


def lean_library_source_root(snapshot_root: Path | None = None) -> Path:
    """Resolve the pinned toolchain's Lean source tree.

    The path is derived from the frozen ``lean-toolchain`` rather than hard
    coded, so it stays correct in a solve sandbox and across toolchain bumps.
    """

    root = snapshot_root or (REPOSITORY_ROOT / "formal/lean")
    toolchain_file = root / "lean-toolchain"
    try:
        specification = toolchain_file.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise PacketError(f"cannot read {toolchain_file}: {error}") from error
    if not specification or any(c.isspace() for c in specification):
        raise PacketError(f"lean-toolchain is empty or malformed: {toolchain_file}")
    directory = specification.replace("/", "--").replace(":", "---")
    source = Path.home() / ".elan" / "toolchains" / directory / "src" / "lean"
    if not source.is_dir():
        raise PacketError(
            f"pinned Lean source tree is unavailable: {source}"
        )
    return source


def _outline_rows(
    root: Path,
    *,
    grep: str | None,
    kind: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    """Enumerate declarations in the Lean source itself.

    The ledger does not hold an object for every declaration, so a source
    outline is the only complete index of what the frontier actually defines.
    """

    if not root.exists():
        raise PacketError(f"lean source root is missing: {root}")
    pattern = re.compile(
        r"^\s*(?:@\[[^\]]*\]\s*)?(?:private\s+|protected\s+|noncomputable\s+)*"
        r"(def|theorem|lemma|structure|inductive|abbrev|instance)\s+"
        # Dotted names are real names: `theorem Perm.symm` must not be
        # reported as `Perm`, or distinct lemmas collapse into one row.
        r"([A-Za-z_][A-Za-z0-9_']*(?:\.[A-Za-z_][A-Za-z0-9_']*)*)"
    )
    needle = grep.lower() if grep else None
    files = (root,) if root.is_file() else sorted(root.rglob("*.lean"))
    rows: list[dict[str, Any]] = []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            match = pattern.match(line)
            if match is None:
                continue
            declaration_kind, name = match.group(1), match.group(2)
            if kind and declaration_kind != kind:
                continue
            if needle and needle not in name.lower():
                continue
            try:
                display = str(path.relative_to(REPOSITORY_ROOT))
            except ValueError:
                display = str(path)
            rows.append(
                {
                    "declaration": name,
                    "declaration_kind": declaration_kind,
                    "location": f"{display}:{number}",
                }
            )
            if len(rows) >= limit:
                return rows
    return rows


def _annotate_outline(
    connection: sqlite3.Connection,
    rows: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Attach ledger truth to source declarations that have a ledger object."""

    objects = _objects(connection)
    by_declaration: dict[str, LedgerObject] = {}
    for item in objects.values():
        declaration = _as_text(item.properties.get("declaration"))
        if declaration:
            by_declaration.setdefault(declaration.rsplit(".", 1)[-1], item)
    annotated = []
    for row in rows:
        item = by_declaration.get(str(row["declaration"]))
        state = (
            exact_state(connection, item, all_objects=objects)
            if item is not None
            else None
        )
        annotated.append(
            {
                **row,
                "object_id": item.id if item else None,
                "truth": state.truth if state else "not_in_ledger",
            }
        )
    return annotated


def _render_outline(rows: Sequence[Mapping[str, Any]]) -> str:
    if not rows:
        return "no matching declarations"
    return "\n".join(
        f"{str(row['truth']):<13} {str(row['declaration_kind']):<10} "
        f"{str(row['declaration'])[:56]:<56} {row['location']}"
        for row in rows
    )


def _render_listing(rows: Sequence[Mapping[str, Any]]) -> str:
    """Render one compact line per object.

    Listings are read inside a proposal prompt, so this deliberately omits
    anything recoverable elsewhere: object ids are omitted because
    ``resolve_object`` already accepts a declaration or canonical name, and a
    canonical name is printed only when it carries prose the declaration does
    not.
    """

    if not rows:
        return "no matching objects"
    lines = []
    for row in rows:
        declaration = _bounded(row.get("declaration"), 72)
        name = _bounded(row.get("canonical_name"), 60)
        label = declaration or name or str(row.get("id") or "")
        entry = f"{row['truth']:<7} {row['verification']:<24} {label}"
        if name and name != declaration:
            entry += f"  — {name}"
        lines.append(entry)
    return "\n".join(lines)


def _unresolved_source_record(identifier: str, error: PacketError) -> str:
    """Describe a declaration that has local source but no ledger object.

    The record is explicitly marked as source-derived with unknown truth so a
    missing ledger object can never be mistaken for a verified claim.
    """

    locations = _source_locations(identifier)
    if not locations:
        raise error
    return json.dumps(
        {
            "id": None,
            "canonical_name": None,
            "lean_declaration": identifier,
            "proposition_sha256": None,
            "exact_contract": None,
            "source_locations": list(locations[:4]),
            "state": {
                "truth": "unknown",
                "verification": "unknown",
                "lifecycle": "unknown",
            },
            "ledger_backed": False,
            "note": (
                "No canonical ledger object exists for this declaration. The "
                "local Lean source below is not a receipt and carries no "
                "truth or verification state. Use `definition` for its exact "
                "text."
            ),
        },
        indent=2,
        sort_keys=True,
    )


def _command_output(args: argparse.Namespace) -> str:
    database = Path(args.ledger)
    with _connect_read_only(database) as connection:
        if args.command in {"signature", "contract"}:
            try:
                target = resolve_object(connection, args.identifier)
            except PacketError as error:
                return _unresolved_source_record(args.identifier, error)
            state = exact_state(connection, target)
            return json.dumps(
                {
                    "id": target.id,
                    "canonical_name": target.canonical_name,
                    "lean_declaration": target.properties.get("declaration"),
                    "proposition_sha256": target.properties.get("proposition_sha256"),
                    "exact_contract": target.content,
                    "state": asdict(state),
                    "ledger_backed": True,
                },
                indent=2,
                sort_keys=True,
            )
        if args.command == "list":
            rows = _list_objects(
                connection,
                kind=args.kind,
                state=args.state,
                grep=args.grep,
                limit=args.limit,
            )
            if args.json:
                return json.dumps(rows, indent=2, sort_keys=True)
            return _render_listing(rows)
        if args.command == "search":
            return json.dumps(
                _search_objects(connection, args.phrase, args.limit),
                indent=2,
                sort_keys=True,
            )
        if args.command == "relations":
            target = resolve_object(connection, args.identifier)
            rows = _relation_rows(
                connection,
                target,
                relation=args.relation,
                direction=args.direction,
                limit=args.limit,
            )
            if args.json:
                return json.dumps(rows, indent=2, sort_keys=True)
            return _render_relations(target.id, rows)
        if args.command == "outline":
            if getattr(args, "library", False):
                root = lean_library_source_root()
            elif getattr(args, "file", None):
                root = Path(args.file).resolve()
            else:
                root = REPOSITORY_ROOT / "formal/lean"
            rows = _outline_rows(
                root, grep=args.grep, kind=args.kind, limit=args.limit
            )
            annotated = _annotate_outline(connection, rows)
            if args.json:
                return json.dumps(annotated, indent=2, sort_keys=True)
            return _render_outline(annotated)
        if args.command == "neighborhood":
            target = resolve_object(connection, args.identifier)
            identifiers = dependency_neighborhood(
                connection, target.id, depth=args.depth
            )[: max(args.limit, 1)]
            objects = _objects(connection)
            return json.dumps(
                [
                    {
                        "id": item,
                        "kind": objects[item].kind,
                        "canonical_name": objects[item].canonical_name,
                        "state": asdict(
                            exact_state(connection, objects[item], all_objects=objects)
                        ),
                    }
                    for item in identifiers
                    if item in objects
                ],
                indent=2,
                sort_keys=True,
            )
        if args.command == "receipts":
            try:
                target = resolve_object(connection, args.identifier)
            except PacketError as error:
                return _unresolved_source_record(args.identifier, error)
            card = theorem_card(connection, target)
            return json.dumps(
                {
                    "id": target.id,
                    "truth": card.truth_state,
                    "verification": card.verification_state,
                    "receipts": card.receipt_pointers,
                    "source": card.source_pointers,
                },
                indent=2,
                sort_keys=True,
            )
        if args.command in {"source", "definition"}:
            try:
                target = resolve_object(connection, args.identifier)
                declaration = _as_text(target.properties.get("declaration"))
                if not re.fullmatch(
                    r"[A-Za-z_][A-Za-z0-9_']*(?:\.[A-Za-z_][A-Za-z0-9_']*)*",
                    declaration,
                ):
                    declaration = args.identifier
            except PacketError:
                declaration = args.identifier
            if getattr(args, "library", False):
                search_root = lean_library_source_root()
            elif getattr(args, "file", None):
                search_root = Path(args.file).resolve()
                if not search_root.exists():
                    raise PacketError(f"source path does not exist: {search_root}")
            else:
                search_root = None
            return _source_excerpt(
                declaration,
                max_lines=args.max_lines,
                offset=args.offset,
                search_root=search_root,
            )
    raise PacketError(f"unsupported command: {args.command}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build semantic packets and retrieve exact Lean metadata."
    )
    parser.add_argument(
        "--ledger", default=str(DEFAULT_CANONICAL_LEDGER), help="SQLite ledger"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("signature", "contract", "receipts"):
        command = subparsers.add_parser(name)
        command.add_argument("identifier")
    for name in ("source", "definition"):
        command = subparsers.add_parser(name)
        command.add_argument("identifier")
        command.add_argument(
            "--max-lines",
            type=int,
            default=120,
            help="maximum declaration lines to print (default: 120)",
        )
        command.add_argument(
            "--offset",
            type=int,
            default=0,
            help="skip this many lines into the declaration, for paging",
        )
        command.add_argument(
            "--file",
            help=(
                "search one .lean file or directory instead of the project tree, "
                "for example a previous candidate's main.lean"
            ),
        )
        command.add_argument(
            "--library",
            action="store_true",
            help="search the pinned Lean toolchain source instead",
        )
    listing = subparsers.add_parser(
        "list", help="enumerate ledger objects without guessing a name"
    )
    listing.add_argument(
        "--kind",
        help="restrict to one object kind, for example formal_claim",
    )
    listing.add_argument(
        "--state",
        help="restrict to one truth state: proved, refuted, open, unknown",
    )
    listing.add_argument("--grep", help="substring filter over name and declaration")
    listing.add_argument(
        "--limit",
        type=int,
        default=80,
        help="maximum rows (default: 80; a full listing costs prompt budget)",
    )
    listing.add_argument("--json", action="store_true")
    relations = subparsers.add_parser(
        "relations", help="labelled edges touching an object, in both directions"
    )
    relations.add_argument("identifier")
    relations.add_argument("--relation", help="restrict to one relation type")
    relations.add_argument(
        "--direction", choices=("in", "out", "both"), default="both"
    )
    relations.add_argument("--limit", type=int, default=60)
    relations.add_argument("--json", action="store_true")
    outline = subparsers.add_parser(
        "outline", help="enumerate declarations in the Lean source itself"
    )
    outline.add_argument(
        "--file", help="one .lean file or directory (default: the project Lean tree)"
    )
    outline.add_argument(
        "--library",
        action="store_true",
        help="index the pinned Lean toolchain source instead",
    )
    outline.add_argument("--grep", help="substring filter over declaration names")
    outline.add_argument(
        "--kind",
        choices=(
            "def",
            "theorem",
            "lemma",
            "structure",
            "inductive",
            "abbrev",
            "instance",
        ),
    )
    outline.add_argument("--limit", type=int, default=120)
    outline.add_argument("--json", action="store_true")
    search = subparsers.add_parser("search")
    search.add_argument("phrase")
    search.add_argument("--limit", type=int, default=20)
    neighborhood = subparsers.add_parser("neighborhood")
    neighborhood.add_argument("identifier")
    neighborhood.add_argument("--depth", type=int, default=3)
    neighborhood.add_argument(
        "--limit",
        type=int,
        default=40,
        help="maximum objects to return (default: 40)",
    )
    packet = subparsers.add_parser("packet")
    packet.add_argument("identifier")
    packet.add_argument("--manifest")
    packet.add_argument("--mode", choices=("solve", "field_expansion"), default="solve")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        if args.command == "packet":
            packet = build_spotlight_packet(
                Path(args.ledger), args.identifier, mode=args.mode
            )
            if args.manifest:
                Path(args.manifest).write_text(
                    json.dumps(packet.manifest, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
            print(packet.text, end="")
        else:
            print(_command_output(args))
    except (PacketError, OSError, sqlite3.Error) as error:
        parser.exit(2, f"spotlight packet error: {error}\n")


if __name__ == "__main__":
    main()


__all__ = [
    "CARD_FORMAT",
    "DEFAULT_CANONICAL_LEDGER",
    "MANIFEST_FORMAT",
    "NORMAL_PROMPT_HARD_CAP_BYTES",
    "PacketError",
    "PromptBudgetError",
    "SemanticTheoremCard",
    "SpotlightPacket",
    "build_spotlight_packet",
    "dependency_neighborhood",
    "enforce_complete_prompt_budget",
    "exact_state",
    "resolve_object",
    "snapshot_database",
    "theorem_card",
    "verify_packet_ledger_snapshot",
]
