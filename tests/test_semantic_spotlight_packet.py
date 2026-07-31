from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from formal.shinka.spotlight_packet import (  # noqa: E402
    PacketError,
    PromptBudgetError,
    _command_output,
    _connect_read_only,
    _source_excerpt,
    build_spotlight_packet,
    enforce_complete_prompt_budget,
    exact_state,
    resolve_object,
    snapshot_database,
    theorem_card,
    verify_packet_ledger_snapshot,
)


def _fixture_ledger(path: Path) -> Path:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE events (
          id INTEGER PRIMARY KEY, occurred_at TEXT NOT NULL,
          recorded_at TEXT NOT NULL, campaign_id TEXT, epoch_id TEXT,
          turn_id TEXT, actor_class TEXT NOT NULL, actor_id TEXT NOT NULL,
          action TEXT NOT NULL, subject_type TEXT NOT NULL,
          subject_id TEXT NOT NULL, payload_json TEXT NOT NULL DEFAULT '{}',
          evidence_object_id TEXT, idempotency_key TEXT UNIQUE,
          previous_event_hash TEXT NOT NULL, event_hash TEXT NOT NULL UNIQUE
        );
        CREATE TABLE objects (
          id TEXT PRIMARY KEY, kind TEXT NOT NULL, canonical_name TEXT NOT NULL,
          content_format TEXT NOT NULL, content TEXT NOT NULL DEFAULT '',
          properties_json TEXT NOT NULL DEFAULT '{}', created_event_id INTEGER NOT NULL
        );
        CREATE TABLE aliases (
          alias TEXT PRIMARY KEY, object_id TEXT NOT NULL,
          created_event_id INTEGER NOT NULL
        );
        CREATE TABLE connections (
          id INTEGER PRIMARY KEY, from_id TEXT NOT NULL, relation TEXT NOT NULL,
          to_id TEXT NOT NULL, properties_json TEXT NOT NULL DEFAULT '{}',
          created_event_id INTEGER NOT NULL, retracted_event_id INTEGER,
          UNIQUE(from_id, relation, to_id)
        );
        CREATE TABLE artifact_locations (
          id INTEGER PRIMARY KEY, object_id TEXT NOT NULL, location TEXT NOT NULL,
          state TEXT NOT NULL, verified_at TEXT, created_event_id INTEGER NOT NULL,
          UNIQUE(object_id, location)
        );
        """
    )
    events = [
        (1, "object_created", "goal:test_focus"),
        (2, "object_created", "goal:settled_tool"),
        (3, "object_created", "claim:settled_tool"),
        (4, "kernel_certified", "claim:settled_tool"),
        (5, "object_created", "goal:unrelated"),
        (6, "object_created", "annotation:settled_card"),
        (7, "object_created", "artifact:sha256:receipt"),
        (8, "object_created", "goal:full"),
    ]
    for event_id, action, subject in events:
        connection.execute(
            "INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                event_id,
                "2026-07-30T00:00:00Z",
                "2026-07-30T00:00:00Z",
                None,
                None,
                None,
                "system",
                "test",
                action,
                "object",
                subject,
                "{}",
                None,
                f"event-{event_id}",
                "0" * 64,
                f"{event_id:064x}",
            ),
        )

    def add_object(
        object_id: str,
        kind: str,
        title: str,
        content: str,
        properties: dict[str, object],
        event_id: int,
    ) -> None:
        connection.execute(
            "INSERT INTO objects VALUES (?,?,?,?,?,?,?)",
            (
                object_id,
                kind,
                title,
                "lean" if kind == "formal_claim" else "markdown",
                content,
                json.dumps(properties),
                event_id,
            ),
        )

    add_object(
        "goal:test_focus",
        "formal_claim",
        "Every test configuration has the desired short cycle.",
        "∀ n : Nat, 6 ≤ n → n = n",
        {
            "declaration": "∀ n : Nat, 6 ≤ n → n = n",
            "formal_system": "lean4",
            "environment_identity": "test",
            "proposition_sha256": "a" * 64,
            "role": "goal",
        },
        1,
    )
    add_object(
        "goal:settled_tool",
        "formal_claim",
        "A settled combinatorial tool.",
        "Example.Discovery.FixedDegreeFiveGoal",
        {
            "declaration": "Example.Discovery.FixedDegreeFiveGoal",
            "formal_system": "lean4",
            "environment_identity": "test",
            "proposition_sha256": "b" * 64,
            "role": "goal",
        },
        2,
    )
    add_object(
        "claim:settled_tool",
        "formal_claim",
        "fixed_degree_five",
        "Example.Discovery.FixedDegreeFiveGoal",
        {
            "declaration": "fixed_degree_five",
            "formal_system": "lean4",
            "environment_identity": "test",
            "proposition_sha256": "b" * 64,
        },
        3,
    )
    add_object(
        "goal:unrelated",
        "formal_claim",
        "IRRELEVANT_FORMAL_SOURCE_SENTINEL",
        "IRRELEVANT_FORMAL_SOURCE_SENTINEL",
        {
            "declaration": "unrelated",
            "formal_system": "lean4",
            "environment_identity": "test",
            "proposition_sha256": "c" * 64,
            "role": "goal",
        },
        5,
    )
    add_object(
        "annotation:settled_card",
        "annotation",
        "Semantic explanation of the settled tool",
        "This prose is advisory.",
        {
            "role": "semantic_theorem_card",
            "card": {
                "title": "Settled five-degree tool",
                "statement": "Minimum outdegree five forces the required cycle.",
                "hypotheses": ["finite loopless digraph", "minimum outdegree five"],
                "conclusion": "a directed cycle within the stated bound",
                "proof_mechanism": "finite-band analysis plus Shen's cutoff",
                "useful_consequences": ["the uniform residual starts at r ≥ 6"],
                "research_significance": "removes degree five from the frontier",
                "limitations": ["does not settle any r ≥ 6 case"],
                "known_counterexamples": [],
                "truth_state": "refuted",
                "verification_state": "untested",
            },
        },
        6,
    )
    add_object(
        "artifact:sha256:receipt",
        "artifact",
        "kernel receipt",
        "",
        {
            "sha256": "d" * 64,
            "artifact_type": "kernel_receipt",
            "byte_size": 1,
            "media_type": "application/json",
        },
        7,
    )
    add_object(
        "goal:full",
        "formal_claim",
        "full",
        "Example.Discovery.FullConjectureGoal",
        {
            "declaration": "Example.Discovery.FullConjectureGoal",
            "formal_system": "lean4",
            "environment_identity": "test",
            "proposition_sha256": "e" * 64,
            "role": "goal",
        },
        8,
    )
    connections = [
        (1, "goal:test_focus", "depends_on", "goal:settled_tool", "{}", 2),
        (
            2,
            "annotation:settled_card",
            "annotates",
            "goal:settled_tool",
            "{}",
            6,
        ),
        (
            3,
            "claim:settled_tool",
            "certified_by",
            "artifact:sha256:receipt",
            "{}",
            7,
        ),
    ]
    connection.executemany(
        "INSERT INTO connections VALUES (?,?,?,?,?,?,NULL)", connections
    )
    connection.execute(
        "INSERT INTO artifact_locations VALUES (1,?,?,?,?,?)",
        (
            "artifact:sha256:receipt",
            str((REPO_ROOT / "formal/lean/Generated.lean").resolve()),
            "present",
            "2026-07-30T00:00:00Z",
            7,
        ),
    )
    connection.commit()
    connection.close()
    return path


def test_packet_is_semantic_exact_and_deduplicated(tmp_path: Path) -> None:
    database = _fixture_ledger(tmp_path / "ledger.sqlite3")
    contract = "∀ n : Nat, 6 ≤ n → n = n"
    packet = build_spotlight_packet(
        database, "goal:test_focus", exact_contract=contract
    )
    assert contract in packet.text
    assert packet.text.count(contract) == 1
    assert "Settled five-degree tool" in packet.text
    assert "finite-band analysis plus Shen's cutoff" in packet.text
    assert "IRRELEVANT_FORMAL_SOURCE_SENTINEL" not in packet.text
    assert "PromotedFrontier.lean" not in packet.text
    assert "compiled checkpoint source" not in packet.text.lower()
    assert "Complete prior programs" not in packet.text
    assert "Kernel frontier: [" not in packet.text
    assert packet.manifest["total_bytes"] < 180_000
    assert all(
        "/Volumes/" not in str(value)
        for section in packet.manifest["sections"]
        for value in section["provenance"]
    )


def test_semantic_card_cannot_change_exact_state(tmp_path: Path) -> None:
    database = _fixture_ledger(tmp_path / "ledger.sqlite3")
    with _connect_read_only(database) as connection:
        focus = resolve_object(connection, "goal:test_focus")
        settled = resolve_object(connection, "goal:settled_tool")
        assert exact_state(connection, focus).truth == "open"
        card = theorem_card(connection, settled)
    # The annotation deliberately lies about both fields; exact events win.
    assert card.truth_state == "proved"
    assert card.verification_state == "authoritatively_evaluated"
    assert card.semantic_provenance == ("annotation:settled_card",)
    assert card.receipt_pointers == (
        str((REPO_ROOT / "formal/lean/Generated.lean").resolve()),
    )


@pytest.mark.skipif(
    not (REPO_ROOT / "formal/lean/Generated").is_dir(),
    reason="requires a project Lean tree, which is absent on branches without one",
)
def test_exact_retrieval_resolves_contract_and_source(tmp_path: Path) -> None:
    database = _fixture_ledger(tmp_path / "ledger.sqlite3")
    namespace = type(
        "Args",
        (),
        {
            "ledger": str(database),
            "command": "signature",
            "identifier": "goal:settled_tool",
        },
    )()
    payload = json.loads(_command_output(namespace))
    assert payload["exact_contract"] == "Example.Discovery.FixedDegreeFiveGoal"
    assert payload["state"]["truth"] == "proved"
    excerpt = _source_excerpt("fixed_degree_five")
    assert "fixed_degree_five" in excerpt
    assert str(REPO_ROOT) in excerpt


def test_prompt_budget_fails_precisely_without_truncation(tmp_path: Path) -> None:
    database = _fixture_ledger(tmp_path / "ledger.sqlite3")
    contract = "∀ n : Nat, 6 ≤ n → n = n"
    with pytest.raises(PromptBudgetError, match="exact contract was not truncated"):
        build_spotlight_packet(
            database,
            "goal:test_focus",
            exact_contract=contract,
            hard_cap_bytes=100,
        )
    with pytest.raises(PromptBudgetError, match="current-program-tail"):
        enforce_complete_prompt_budget(
            "system",
            "# Current program\n" + ("x" * 400_000),
        )


def test_packet_rendering_is_deterministic(tmp_path: Path) -> None:
    database = _fixture_ledger(tmp_path / "ledger.sqlite3")
    first = build_spotlight_packet(database, "goal:test_focus")
    second = build_spotlight_packet(database, "goal:test_focus")
    assert first == second


def test_persisted_ledger_snapshot_must_match_packet_head(
    tmp_path: Path,
) -> None:
    database = _fixture_ledger(tmp_path / "ledger.sqlite3")
    packet = build_spotlight_packet(database, "goal:test_focus")
    snapshot = tmp_path / "snapshot.sqlite3"
    snapshot_database(database, snapshot)
    verify_packet_ledger_snapshot(snapshot, packet.manifest)

    connection = sqlite3.connect(snapshot)
    connection.execute(
        "INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            9,
            "2026-07-30T00:00:01Z",
            "2026-07-30T00:00:01Z",
            None,
            None,
            None,
            "system",
            "test",
            "annotation_recorded",
            "object",
            "goal:test_focus",
            "{}",
            None,
            "event-9",
            f"{8:064x}",
            f"{9:064x}",
        ),
    )
    connection.commit()
    connection.close()
    with pytest.raises(
        ValueError, match="changed between spotlight-packet projection"
    ):
        verify_packet_ledger_snapshot(snapshot, packet.manifest)


@pytest.mark.skipif(
    not (REPO_ROOT / "formal/shinka/run_evolution.py").is_file(),
    reason="requires the Shinka runner, which is absent on branches without it",
)
def test_normal_shinka_system_prompt_uses_packet_not_repository_dump() -> None:
    from formal.shinka.run_evolution import build_configs

    environment = {
        "SHINKA_PROOF_TARGET": "full",
        "SHINKA_ACCEPTED_GOALS_JSON": "[]",
        "SHINKA_OBLIGATION_STATUS_JSON": "{}",
        "SHINKA_PROPOSAL_STEPS": "1",
    }
    with patch.dict("os.environ", environment, clear=True):
        evolution, *_rest = build_configs(
            REPO_ROOT / "formal/shinka", check_only=True
        )
    prompt = evolution.task_sys_msg
    assert "# Exact spotlight goal contract" in prompt
    assert "# Relevant theorem cards" in prompt
    assert "# Generated scored-goal catalog" not in prompt
    assert "## Context input: `formal/lean/Generated/Statement.lean`" not in prompt
    assert "theorem fixed_degree_five" not in prompt
    assert len(prompt.encode("utf-8")) < 200_000


def _args(database: Path, command: str, **fields: object) -> object:
    return type("Args", (), {"ledger": str(database), "command": command, **fields})()


def test_list_enumerates_without_needing_a_name(tmp_path: Path) -> None:
    database = _fixture_ledger(tmp_path / "ledger.sqlite3")
    payload = json.loads(
        _command_output(
            _args(
                database,
                "list",
                kind="formal_claim",
                state=None,
                grep=None,
                limit=200,
                json=True,
            )
        )
    )
    assert payload, "list must enumerate ledger objects"
    assert {"id", "kind", "truth", "verification"} <= set(payload[0])
    assert all(row["kind"] == "formal_claim" for row in payload)


def test_list_state_filter_never_invents_a_truth_state(tmp_path: Path) -> None:
    database = _fixture_ledger(tmp_path / "ledger.sqlite3")
    proved = json.loads(
        _command_output(
            _args(
                database,
                "list",
                kind=None,
                state="proved",
                grep=None,
                limit=200,
                json=True,
            )
        )
    )
    assert all(row["truth"] == "proved" for row in proved)
    with _connect_read_only(database) as connection:
        for row in proved:
            target = resolve_object(connection, row["id"])
            assert exact_state(connection, target).truth == "proved"


def test_search_matches_across_camel_case_word_boundaries(tmp_path: Path) -> None:
    database = _fixture_ledger(tmp_path / "ledger.sqlite3")
    payload = json.loads(
        _command_output(_args(database, "search", phrase="fixed degree", limit=20))
    )
    assert payload, "tokenized search must match split identifier words"
    assert all({"id", "kind", "excerpt"} <= set(row) for row in payload)


def test_source_excerpt_returns_whole_declaration_and_pages(tmp_path: Path) -> None:
    lean = tmp_path / "Sample.lean"
    body = "\n".join(f"  step{index}" for index in range(40))
    lean.write_text(
        f"theorem sample_decl : True :=\n{body}\ntheorem next_decl : True := trivial\n",
        encoding="utf-8",
    )
    with patch(
        "formal.shinka.spotlight_packet._source_locations",
        return_value=(f"{lean}:1",),
    ):
        full = _source_excerpt("sample_decl")
        assert "41 lines in declaration" in full
        assert "step39" in full
        assert "next_decl" not in full, "must stop at the next top-level declaration"
        capped = _source_excerpt("sample_decl", max_lines=10)
        assert "--offset 10" in capped
        assert "step39" not in capped
        rest = _source_excerpt("sample_decl", max_lines=100, offset=10)
        assert "step39" in rest


def test_unresolved_declaration_is_reported_as_source_only(tmp_path: Path) -> None:
    database = _fixture_ledger(tmp_path / "ledger.sqlite3")
    with patch(
        "formal.shinka.spotlight_packet._source_locations",
        return_value=("/tmp/Only.lean:12",),
    ):
        payload = json.loads(
            _command_output(
                _args(database, "signature", identifier="declaration_absent_from_ledger")
            )
        )
    assert payload["ledger_backed"] is False
    assert payload["state"]["truth"] == "unknown"
    assert payload["state"]["verification"] == "unknown"
    assert payload["exact_contract"] is None


def test_retrieval_section_states_ledger_wide_scope() -> None:
    from formal.shinka.spotlight_packet import _retrieval_instructions

    text = _retrieval_instructions()
    assert "WHOLE canonical ledger" in text
    assert " list --kind formal_claim" in text
    assert "packet OTHER_GOAL_ID" in text


def test_listing_lines_stay_bounded_for_proposition_sized_records(
    tmp_path: Path,
) -> None:
    from formal.shinka.spotlight_packet import _render_listing

    rendered = _render_listing(
        [
            {
                "id": "claim:huge",
                "kind": "formal_claim",
                "declaration": "x" * 5000,
                "canonical_name": "y" * 5000,
                "truth": "proved",
                "verification": "promotion_audited",
            }
        ]
    )
    assert len(rendered) < 220, "a listing row must stay one bounded line"
    assert "\n" not in rendered.rstrip("\n")


def test_neighborhood_is_bounded_by_limit(tmp_path: Path) -> None:
    database = _fixture_ledger(tmp_path / "ledger.sqlite3")
    payload = json.loads(
        _command_output(
            _args(
                database,
                "neighborhood",
                identifier="goal:settled_tool",
                depth=3,
                limit=1,
            )
        )
    )
    assert len(payload) <= 1


def test_search_fields_are_bounded(tmp_path: Path) -> None:
    database = _fixture_ledger(tmp_path / "ledger.sqlite3")
    payload = json.loads(
        _command_output(_args(database, "search", phrase="degree", limit=20))
    )
    for row in payload:
        assert len(row.get("declaration") or "") <= 96
        assert len(row.get("canonical_name") or "") <= 120
        assert len(row.get("excerpt") or "") <= 200


def test_relations_expose_labelled_edges_in_both_directions(tmp_path: Path) -> None:
    database = _fixture_ledger(tmp_path / "ledger.sqlite3")
    rows = json.loads(
        _command_output(
            _args(
                database,
                "relations",
                identifier="goal:settled_tool",
                relation=None,
                direction="both",
                limit=60,
                json=True,
            )
        )
    )
    assert rows, "an object with connections must report its edges"
    assert all({"relation", "direction", "other_id"} <= set(row) for row in rows)
    assert all(row["direction"] in {"in", "out"} for row in rows)


def test_outline_indexes_source_including_non_ledger_declarations(
    tmp_path: Path,
) -> None:
    database = _fixture_ledger(tmp_path / "ledger.sqlite3")
    lean = tmp_path / "Outline.lean"
    lean.write_text(
        "def helper_without_object : Nat := 0\n"
        "theorem fixed_degree_five : True := trivial\n",
        encoding="utf-8",
    )
    rows = json.loads(
        _command_output(
            _args(
                database,
                "outline",
                file=str(lean),
                grep=None,
                kind=None,
                limit=120,
                json=True,
            )
        )
    )
    names = {row["declaration"]: row for row in rows}
    assert "helper_without_object" in names
    assert names["helper_without_object"]["truth"] == "not_in_ledger"
    assert names["helper_without_object"]["object_id"] is None
    assert all(":" in row["location"] for row in rows)


def test_outline_kind_filter_selects_only_that_form(tmp_path: Path) -> None:
    database = _fixture_ledger(tmp_path / "ledger.sqlite3")
    lean = tmp_path / "Kinds.lean"
    lean.write_text(
        "def a_def : Nat := 0\ntheorem a_theorem : True := trivial\n",
        encoding="utf-8",
    )
    rows = json.loads(
        _command_output(
            _args(
                database,
                "outline",
                file=str(lean),
                grep=None,
                kind="theorem",
                limit=120,
                json=True,
            )
        )
    )
    assert [row["declaration"] for row in rows] == ["a_theorem"]


def test_source_excerpt_omits_encoded_research_annotations(tmp_path: Path) -> None:
    lean = tmp_path / "Annotated.lean"
    blob = "A" * 3000
    lean.write_text(
        f"-- RESEARCH: {blob}\n\ntheorem annotated_decl : True := trivial\n",
        encoding="utf-8",
    )
    with patch(
        "formal.shinka.spotlight_packet._source_locations",
        return_value=(f"{lean}:3",),
    ):
        excerpt = _source_excerpt("annotated_decl")
    assert "RESEARCH:" not in excerpt
    assert blob not in excerpt
    assert "annotated_decl" in excerpt


def test_read_only_open_survives_a_wal_ledger_with_no_writer(
    tmp_path: Path,
) -> None:
    """A clean writer shutdown deletes -wal/-shm; reads must still work."""

    from formal.shinka.spotlight_packet import _open_read_only

    database = tmp_path / "wal.sqlite3"
    writer = sqlite3.connect(database)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("CREATE TABLE x(a)")
    writer.execute("INSERT INTO x VALUES(1)")
    writer.commit()
    writer.close()
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{database}{suffix}")
        if sidecar.exists():
            sidecar.unlink()
    connection = _open_read_only(database)
    try:
        assert connection.execute("SELECT count(*) FROM x").fetchone()[0] == 1
    finally:
        connection.close()


def test_read_only_open_refuses_immutable_when_wal_holds_data(
    tmp_path: Path,
) -> None:
    """Never read past a non-empty write-ahead log with immutable=1."""

    from formal.shinka.spotlight_packet import PacketError, _open_read_only

    database = tmp_path / "busy.sqlite3"
    writer = sqlite3.connect(database)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("CREATE TABLE x(a)")
    writer.commit()
    log = Path(f"{database}-wal")
    log.write_bytes(b"\x00" * 64)
    shm = Path(f"{database}-shm")
    if shm.exists():
        shm.unlink()
    with patch(
        "formal.shinka.spotlight_packet.sqlite3.connect",
        side_effect=sqlite3.OperationalError("unable to open database file"),
    ):
        with pytest.raises(PacketError, match="busy or unreadable"):
            _open_read_only(database)
    writer.close()


def test_definition_reads_a_prior_candidate_without_evolve_markers(
    tmp_path: Path,
) -> None:
    """Copying a slice out of a candidate must never drag its markers along."""

    database = _fixture_ledger(tmp_path / "ledger.sqlite3")
    candidate = tmp_path / "main.lean"
    candidate.write_text(
        "import Generated\n"
        "namespace Generated\n"
        "-- EVOLVE-BLOCK-START\n"
        "theorem carried_over : True := trivial\n"
        "-- EVOLVE-BLOCK-END\n"
        "end Generated\n",
        encoding="utf-8",
    )
    output = _command_output(
        _args(
            database,
            "definition",
            identifier="carried_over",
            max_lines=120,
            offset=0,
            file=str(candidate),
        )
    )
    assert "carried_over" in output
    assert "EVOLVE-BLOCK" not in output


def test_definition_file_must_exist(tmp_path: Path) -> None:
    database = _fixture_ledger(tmp_path / "ledger.sqlite3")
    with pytest.raises(PacketError, match="source path does not exist"):
        _command_output(
            _args(
                database,
                "definition",
                identifier="anything",
                max_lines=120,
                offset=0,
                file=str(tmp_path / "absent.lean"),
            )
        )


def test_outline_keeps_dotted_declaration_names_distinct(tmp_path: Path) -> None:
    """`theorem Perm.symm` must not collapse to `Perm`."""

    database = _fixture_ledger(tmp_path / "ledger.sqlite3")
    lean = tmp_path / "Dotted.lean"
    lean.write_text(
        "theorem Perm.symm : True := trivial\n"
        "@[simp] protected theorem Perm.refl : True := trivial\n"
        "structure Perm where\n  field : Nat\n",
        encoding="utf-8",
    )
    rows = json.loads(
        _command_output(
            _args(
                database,
                "outline",
                file=str(lean),
                grep=None,
                kind=None,
                limit=120,
                library=False,
                json=True,
            )
        )
    )
    names = [row["declaration"] for row in rows]
    assert "Perm.symm" in names
    assert "Perm.refl" in names
    assert names.count("Perm") == 1


def test_bare_name_fallback_is_labelled_as_possibly_unrelated(
    tmp_path: Path,
) -> None:
    """A qualified miss must never masquerade as an exact hit."""

    lean = tmp_path / "Bare.lean"
    lean.write_text("theorem symm : True := trivial\n", encoding="utf-8")
    excerpt = _source_excerpt("Some.Other.symm", search_root=lean)
    assert "WARNING" in excerpt
    assert "bare-name match" in excerpt
    exact = _source_excerpt("symm", search_root=lean)
    assert "WARNING" not in exact
