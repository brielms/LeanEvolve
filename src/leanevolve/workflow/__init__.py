"""Scientist- and agent-facing task interface for LeanEvolve.

This package is the layer `mise` calls. It owns environment diagnosis, the
fast/forensic gate split, schedule and cost validation, campaign discovery, and
the receipt every task emits. It deliberately owns no mathematics: the Lean
kernel remains the only thing that decides whether a declaration is accepted.
"""

from leanevolve.workflow.errors import Exit, WorkflowError
from leanevolve.workflow.receipt import Receipt
from leanevolve.workflow.settings import Settings, load_settings

__all__ = ["Exit", "Receipt", "Settings", "WorkflowError", "load_settings"]
