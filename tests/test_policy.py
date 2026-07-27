from __future__ import annotations

import pytest

from leanevolve.policy import validate_candidate

VALID = """-- EVOLVE-BLOCK-START
theorem fine : True := by trivial
-- EVOLVE-BLOCK-END
"""


def test_valid_candidate_passes() -> None:
    validate_candidate(VALID, 4096)


def test_policy_words_in_comments_and_strings_do_not_trigger() -> None:
    source = VALID.replace(
        "theorem fine",
        '-- "sorry axiom" is explanatory text\ntheorem fine',
    )
    validate_candidate(source, 4096)


@pytest.mark.parametrize(
    "fragment",
    [
        "theorem bad : True := by sorry",
        "axiom bad : False",
        "unsafe def bad : Nat := 0",
        "run_tac Lean.Elab.Tactic.closeMainGoalUsing `True.intro",
        "#eval 1 + 1",
    ],
)
def test_untrusted_shortcuts_are_rejected(fragment: str) -> None:
    source = f"-- EVOLVE-BLOCK-START\n{fragment}\n-- EVOLVE-BLOCK-END\n"
    with pytest.raises(ValueError, match="source policy"):
        validate_candidate(source, 4096)
