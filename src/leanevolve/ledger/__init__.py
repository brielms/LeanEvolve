"""Unified research ledger.

One canonical graph of objects, typed connections, and append-only events.  The
goal board, research chronology, certified proof graph, prior-art crosswalk,
campaign status, and recovery queue are projections over this state rather than
independent sources of truth.

The core is domain-neutral. Project-specific import, migration, and
compatibility adapters belong beside the project that owns those formats, not
in this package.
"""

from __future__ import annotations

from leanevolve.ledger.vocabulary import (
    VOCABULARY_FORMAT,
    VOCABULARY_VERSION,
    VocabularyError,
    vocabulary_payload,
    vocabulary_sha256,
)

__all__ = [
    "VOCABULARY_FORMAT",
    "VOCABULARY_VERSION",
    "VocabularyError",
    "vocabulary_payload",
    "vocabulary_sha256",
]
