# Contributing

LeanEvolve welcomes small, auditable improvements to proof-search infrastructure.

Before opening a change:

1. keep model and solver behavior outside the proof trust boundary;
2. make every new acceptance path end in a Lean kernel check;
3. record inputs and outputs needed for deterministic verification replay;
4. add a regression test for trust-boundary or artifact-format changes;
5. run `python -m pytest`, `python -m ruff check .`, and
   `python scripts/release_audit.py`.

Do not commit API keys, model transcripts containing private material, generated run
directories, Lean build products, or machine-specific absolute paths.

Changes to an artifact schema should introduce a new format identifier or retain a
backward-compatible reader. A replay failure is preferable to silently interpreting
old evidence under new rules.
