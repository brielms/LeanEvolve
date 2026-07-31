"""Policy tests for the read-only Lean scratch checker.

The library name is the import allowlist prefix, the frozen snapshot root,
and the namespace root. Getting it wrong fails in two directions: too broad
and the allowlist stops constraining imports, too narrow and valid candidates
are rejected. These tests pin both edges.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from formal.shinka.shinka_lean_check import (  # noqa: E402
    DEFAULT_LIBRARY,
    PolicyFailure,
    checkpoint_module,
    evolve_namespace,
    validate_candidate_policy,
)

MAXIMUM = 1 << 20


def _candidate(imports: str, namespace: str, body: str = "") -> str:
    return (
        f"{imports}\n\n"
        f"namespace {namespace}\n\n"
        "-- EVOLVE-BLOCK-START\n"
        f"{body}\n"
        "-- EVOLVE-BLOCK-END\n\n"
        f"end {namespace}\n"
    )


def test_library_identity_is_composed_consistently() -> None:
    assert evolve_namespace("Demo") == "Demo.Generated"
    assert checkpoint_module("Demo") == "Demo.Generated.Checkpoint"
    assert checkpoint_module("Other") == "Other.Generated.Checkpoint"


def test_candidate_inside_the_library_is_accepted() -> None:
    source = _candidate(
        f"import {DEFAULT_LIBRARY}.Targets", evolve_namespace(DEFAULT_LIBRARY)
    )
    validate_candidate_policy(source, maximum=MAXIMUM)


def test_bare_library_import_is_accepted() -> None:
    """Importing the library root itself is inside the allowlist."""

    source = _candidate(
        f"import {DEFAULT_LIBRARY}", evolve_namespace(DEFAULT_LIBRARY)
    )
    validate_candidate_policy(source, maximum=MAXIMUM)


def test_import_outside_the_library_is_rejected() -> None:
    source = _candidate("import System.IO", evolve_namespace(DEFAULT_LIBRARY))
    with pytest.raises(PolicyFailure, match="imports modules outside"):
        validate_candidate_policy(source, maximum=MAXIMUM)


def test_prefix_lookalike_module_is_rejected() -> None:
    """`DemoEvil` must not pass as `Demo`: the boundary is a dotted prefix."""

    source = _candidate(
        f"import {DEFAULT_LIBRARY}Evil.Payload", evolve_namespace(DEFAULT_LIBRARY)
    )
    with pytest.raises(PolicyFailure, match="imports modules outside"):
        validate_candidate_policy(source, maximum=MAXIMUM)


def test_candidate_without_any_import_is_rejected() -> None:
    source = _candidate("", evolve_namespace(DEFAULT_LIBRARY))
    with pytest.raises(PolicyFailure, match="must import an existing"):
        validate_candidate_policy(source, maximum=MAXIMUM)


def test_wrong_namespace_is_rejected() -> None:
    source = _candidate(f"import {DEFAULT_LIBRARY}.Targets", "Elsewhere")
    with pytest.raises(PolicyFailure, match="must use namespace"):
        validate_candidate_policy(source, maximum=MAXIMUM)


def test_allowlist_follows_a_custom_library() -> None:
    """A project with another library name gets the same guarantees."""

    source = _candidate("import Other.Targets", evolve_namespace("Other"))
    validate_candidate_policy(source, maximum=MAXIMUM, library="Other")
    with pytest.raises(PolicyFailure, match="imports modules outside"):
        validate_candidate_policy(source, maximum=MAXIMUM, library=DEFAULT_LIBRARY)


def test_default_library_rejects_another_projects_candidate() -> None:
    """The allowlist is not a no-op: a foreign library is refused by default."""

    source = _candidate("import Other.Targets", evolve_namespace("Other"))
    with pytest.raises(PolicyFailure):
        validate_candidate_policy(source, maximum=MAXIMUM)


def test_admission_is_still_rejected() -> None:
    source = _candidate(
        f"import {DEFAULT_LIBRARY}.Targets",
        evolve_namespace(DEFAULT_LIBRARY),
        body="theorem t : True := by sorry",
    )
    with pytest.raises(PolicyFailure):
        validate_candidate_policy(source, maximum=MAXIMUM)


def test_oversized_candidate_is_rejected() -> None:
    source = _candidate(
        f"import {DEFAULT_LIBRARY}.Targets", evolve_namespace(DEFAULT_LIBRARY)
    )
    with pytest.raises(PolicyFailure, match="exceeds"):
        validate_candidate_policy(source, maximum=10)
