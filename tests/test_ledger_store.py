"""The store commits whole event groups, chains them, and stays idempotent."""

from __future__ import annotations

import sqlite3

import pytest

from leanevolve.ledger import schema
from leanevolve.ledger.events import Envelope, EventError, prepare
from leanevolve.ledger.store import ConflictError, Ledger, LedgerError

RECEIPT = "artifact:sha256:" + "1" * 64


@pytest.fixture()
def ledger(tmp_path) -> Ledger:
    with Ledger.open(tmp_path / "ledger.sqlite3") as store:
        yield store


def _claim(session, object_id: str, digest: str = "a") -> None:
    session.create_object(
        object_id,
        "formal_claim",
        object_id,
        content_format="lean",
        content=f"Example.Generated.{object_id.split(':')[-1]}",
        properties={
            "formal_system": "lean4",
            "declaration": f"Example.Generated.{object_id.split(':')[-1]}",
            "proposition_sha256": digest * 64,
            "environment_identity": "ckpt-a",
        },
    )


def _receipt(session) -> None:
    session.create_object(
        RECEIPT,
        "artifact",
        "Kernel receipt",
        content_format="none",
        properties={
            "sha256": "1" * 64,
            "artifact_type": "kernel_receipt",
            "byte_size": 8421,
            "media_type": "application/json",
        },
    )


class TestSchema:
    def test_pragmas_are_set(self, ledger: Ledger) -> None:
        connection = ledger._connection
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1

    def test_version_and_vocabulary_are_bound(self, ledger: Ledger) -> None:
        assert ledger.schema_version == schema.SCHEMA_VERSION
        version, digest = schema.vocabulary_binding(ledger._connection)
        assert version >= 1
        assert len(digest) == 64

    def test_a_newer_schema_is_refused(self, tmp_path) -> None:
        path = tmp_path / "future.sqlite3"
        with Ledger.open(path):
            pass
        connection = sqlite3.connect(path)
        connection.execute(
            "UPDATE schema_meta SET value = ? WHERE key = 'schema_version'",
            (str(schema.SCHEMA_VERSION + 5),),
        )
        connection.commit()
        connection.close()
        with pytest.raises(schema.SchemaError, match="newer than this code"):
            Ledger.open(path)

    def test_reopening_is_not_a_migration(self, tmp_path) -> None:
        path = tmp_path / "again.sqlite3"
        with Ledger.open(path) as first:
            assert first.schema_version == schema.SCHEMA_VERSION
        with Ledger.open(path) as second:
            assert second.schema_version == schema.SCHEMA_VERSION


class TestEventChain:
    def test_first_event_follows_genesis(self, ledger: Ledger) -> None:
        with ledger.write("research_agent", "agent:test") as session:
            _claim(session, "claim:one")
        head = ledger.head()
        assert head is not None
        assert head.previous_event_hash == schema.GENESIS_HASH
        assert head.event_hash == head.compute_hash()

    def test_each_event_commits_to_its_predecessor(self, ledger: Ledger) -> None:
        with ledger.write("research_agent", "agent:test") as session:
            _claim(session, "claim:one")
            _claim(session, "claim:two", "b")
            session.connect("claim:two", "depends_on", "claim:one")
        events = ledger.events()
        assert len(events) == 3
        previous = schema.GENESIS_HASH
        for event in events:
            assert event.previous_event_hash == previous
            assert event.event_hash == event.compute_hash()
            previous = event.event_hash

    def test_ids_are_the_authoritative_order(self, ledger: Ledger) -> None:
        with ledger.write("research_agent", "agent:test") as session:
            _claim(session, "claim:one")
            _claim(session, "claim:two", "b")
        ids = [event.id for event in ledger.events()]
        assert ids == sorted(ids) == [1, 2]

    def test_recorded_at_is_outside_the_digest(self, ledger: Ledger) -> None:
        # Re-importing the same history must reproduce the same chain, so the
        # moment a row was written cannot be part of what happened.
        with ledger.write("research_agent", "agent:test") as session:
            _claim(session, "claim:one")
        event = ledger.head()
        assert event is not None
        assert "recorded_at" not in event.hashed_core()

    def test_envelope_is_inherited_by_every_event(self, ledger: Ledger) -> None:
        with ledger.write(
            "research_agent",
            "agent:test",
            campaign_id="campaign:example",
            epoch_id="epoch:7",
            turn_id="turn:solve_0001:g1",
        ) as session:
            _claim(session, "claim:one")
            _claim(session, "claim:two", "b")
        for event in ledger.events():
            assert event.campaign_id == "campaign:example"
            assert event.epoch_id == "epoch:7"
            assert event.turn_id == "turn:solve_0001:g1"


class TestTransactionality:
    def test_a_failed_group_writes_nothing(self, ledger: Ledger) -> None:
        with pytest.raises(LedgerError):
            with ledger.write("research_agent", "agent:test") as session:
                _claim(session, "claim:one")
                session.connect("claim:one", "depends_on", "claim:missing")
        assert ledger.event_count() == 0
        assert ledger.object("claim:one") is None

    def test_an_exception_rolls_the_whole_group_back(self, ledger: Ledger) -> None:
        with pytest.raises(RuntimeError, match="deliberate"):
            with ledger.write("research_agent", "agent:test") as session:
                _claim(session, "claim:one")
                raise RuntimeError("deliberate")
        assert ledger.event_count() == 0

    def test_committed_groups_survive_reopening(self, tmp_path) -> None:
        path = tmp_path / "persist.sqlite3"
        with Ledger.open(path) as store:
            with store.write("research_agent", "agent:test") as session:
                _claim(session, "claim:one")
        with Ledger.open(path) as store:
            assert store.object("claim:one") is not None
            assert store.event_count() == 1


class TestIdempotency:
    def test_identical_object_creation_is_a_no_op(self, ledger: Ledger) -> None:
        with ledger.write("research_agent", "agent:test") as session:
            _claim(session, "claim:one")
        with ledger.write("research_agent", "agent:test") as session:
            _claim(session, "claim:one")
        assert ledger.event_count() == 1

    def test_conflicting_object_creation_is_refused(self, ledger: Ledger) -> None:
        with ledger.write("research_agent", "agent:test") as session:
            _claim(session, "claim:one")
        with pytest.raises(ConflictError, match="different content"):
            with ledger.write("research_agent", "agent:test") as session:
                _claim(session, "claim:one", "c")

    def test_identical_connection_is_a_no_op(self, ledger: Ledger) -> None:
        with ledger.write("research_agent", "agent:test") as session:
            _claim(session, "claim:one")
            _claim(session, "claim:two", "b")
            session.connect("claim:two", "depends_on", "claim:one")
        before = ledger.event_count()
        with ledger.write("research_agent", "agent:test") as session:
            session.connect("claim:two", "depends_on", "claim:one")
        assert ledger.event_count() == before

    def test_repeated_idempotency_key_returns_the_original(
        self, ledger: Ledger
    ) -> None:
        with ledger.write("human_researcher", "human:researcher") as session:
            _claim(session, "claim:one")
            first = session.record(
                "object_retracted",
                "claim:one",
                {"reason": "duplicate"},
                idempotency_key="retract-claim-one",
            )
        with ledger.write("human_researcher", "human:researcher") as session:
            again = session.record(
                "object_retracted",
                "claim:one",
                {"reason": "duplicate"},
                idempotency_key="retract-claim-one",
            )
        assert again.id == first.id
        assert ledger.event_count() == 2


class TestAuthority:
    def test_an_agent_cannot_certify(self, ledger: Ledger) -> None:
        with pytest.raises(LedgerError, match="may not emit"):
            with ledger.write("research_agent", "agent:test") as session:
                _claim(session, "claim:one")
                _receipt(session)
                session.record(
                    "kernel_certified",
                    "claim:one",
                    {
                        "declaration": "Example.Generated.one",
                        "proposition_sha256": "a" * 64,
                        "toolchain": "leanprover--lean4---v4.32.1",
                        "evaluator_version": "test",
                        "axiom_policy": "standard",
                    },
                    evidence_object_id=RECEIPT,
                )
        assert ledger.event_count() == 0

    def test_the_evaluator_may_certify(self, ledger: Ledger) -> None:
        with ledger.write("authoritative_evaluator", "tool:evaluate") as session:
            _claim(session, "claim:one")
            _receipt(session)
            session.record(
                "kernel_certified",
                "claim:one",
                {
                    "declaration": "Example.Generated.one",
                    "proposition_sha256": "a" * 64,
                    "toolchain": "leanprover--lean4---v4.32.1",
                    "evaluator_version": "test",
                    "axiom_policy": "standard",
                },
                evidence_object_id=RECEIPT,
            )
        assert ledger.events(action="kernel_certified")

    def test_unknown_actor_is_refused(self, ledger: Ledger) -> None:
        with pytest.raises(LedgerError):
            with ledger.write("someone", "x") as session:
                _claim(session, "claim:one")

    def test_evidence_must_be_a_known_object(self, ledger: Ledger) -> None:
        with pytest.raises(LedgerError, match="not a known object"):
            with ledger.write("authoritative_evaluator", "tool:evaluate") as session:
                _claim(session, "claim:one")
                session.record(
                    "kernel_certified",
                    "claim:one",
                    {
                        "declaration": "Example.Generated.one",
                        "proposition_sha256": "a" * 64,
                        "toolchain": "leanprover--lean4---v4.32.1",
                        "evaluator_version": "test",
                        "axiom_policy": "standard",
                    },
                    evidence_object_id="artifact:sha256:" + "9" * 64,
                )


class TestObjectsAndConnections:
    def test_required_properties_are_enforced(self, ledger: Ledger) -> None:
        with pytest.raises(LedgerError, match="requires properties"):
            with ledger.write("research_agent", "agent:test") as session:
                session.create_object(
                    "claim:bare", "formal_claim", "Bare", content_format="lean"
                )

    def test_content_format_must_suit_the_kind(self, ledger: Ledger) -> None:
        with pytest.raises(LedgerError, match="content format"):
            with ledger.write("research_agent", "agent:test") as session:
                session.create_object(
                    "turn:one", "turn", "A turn", content_format="lean"
                )

    def test_endpoint_kinds_are_enforced(self, ledger: Ledger) -> None:
        with pytest.raises(LedgerError, match="does not accept"):
            with ledger.write("research_agent", "agent:test") as session:
                _claim(session, "claim:one")
                _receipt(session)
                session.connect("artifact:sha256:" + "1" * 64,
                                "depends_on", "claim:one")

    def test_retraction_preserves_history(self, ledger: Ledger) -> None:
        with ledger.write("research_agent", "agent:test") as session:
            _claim(session, "claim:one")
            _claim(session, "claim:two", "b")
            session.connect("claim:two", "depends_on", "claim:one")
        with ledger.write("human_researcher", "human:researcher") as session:
            session.retract("claim:two", "depends_on", "claim:one", reason="wrong")
        assert ledger.connections(from_id="claim:two") == []
        retained = ledger.connections(from_id="claim:two", include_retracted=True)
        assert len(retained) == 1
        assert not retained[0].is_active

    def test_aliases_resolve_without_creating_a_second_claim(
        self, ledger: Ledger
    ) -> None:
        with ledger.write("human_researcher", "human:researcher") as session:
            _claim(session, "claim:one")
            session.add_alias("Example.Legacy.oldName", "claim:one")
        assert ledger.resolve("Example.Legacy.oldName") == "claim:one"
        assert len(ledger.objects(kind="formal_claim")) == 1

    def test_an_alias_cannot_be_repointed(self, ledger: Ledger) -> None:
        with ledger.write("human_researcher", "human:researcher") as session:
            _claim(session, "claim:one")
            _claim(session, "claim:two", "b")
            session.add_alias("shared", "claim:one")
        with pytest.raises(ConflictError, match="already points at"):
            with ledger.write("human_researcher", "human:researcher") as session:
                session.add_alias("shared", "claim:two")


class TestQueries:
    def test_events_can_be_replayed_to_a_past_point(self, ledger: Ledger) -> None:
        with ledger.write("research_agent", "agent:test") as session:
            _claim(session, "claim:one")
            _claim(session, "claim:two", "b")
            _claim(session, "claim:three", "c")
        assert len(ledger.events(until=2)) == 2
        assert len(ledger.events(since=2)) == 1

    def test_events_filter_by_turn(self, ledger: Ledger) -> None:
        with ledger.write("research_agent", "a", turn_id="turn:1") as session:
            _claim(session, "claim:one")
        with ledger.write("research_agent", "a", turn_id="turn:2") as session:
            _claim(session, "claim:two", "b")
        assert len(ledger.events(turn_id="turn:1")) == 1

    def test_payloads_must_be_json_serializable(self, ledger: Ledger) -> None:
        with pytest.raises(LedgerError, match="JSON"):
            with ledger.write("human_researcher", "human:researcher") as session:
                _claim(session, "claim:one")
                session.record(
                    "object_retracted", "claim:one", {"reason": {1, 2, 3}}
                )


class TestEnvelopeValidation:
    def test_unknown_subject_type_is_refused(self) -> None:
        with pytest.raises(EventError, match="unknown subject type"):
            prepare(
                event_id=1,
                envelope=Envelope("research_agent", "agent:test"),
                action="object_created",
                subject_type="galaxy",
                subject_id="claim:one",
                payload={"kind": "research_claim"},
                previous_event_hash=schema.GENESIS_HASH,
            )

    def test_action_subject_type_pairing_is_enforced(self) -> None:
        with pytest.raises(EventError, match="does not accept subject type"):
            prepare(
                event_id=1,
                envelope=Envelope("ledger_service", "svc"),
                action="schema_migrated",
                subject_type="object",
                subject_id="claim:one",
                payload={"from_version": 0, "to_version": 1},
                previous_event_hash=schema.GENESIS_HASH,
            )
