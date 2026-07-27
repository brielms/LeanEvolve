# LeanEvolve

[![Lean 4](https://img.shields.io/badge/Lean-4.32.1-0f766e)](https://lean-lang.org/)
[![proofs-kernel_checked-16a34a](https://img.shields.io/badge/proofs-kernel__checked-16a34a)](docs/architecture.html)

LeanEvolve is a small, auditable bridge between
[ShinkaEvolve](https://github.com/SakanaAI/ShinkaEvolve) and Lean 4. A model may
suggest definitions, lemmas, tactics, and whole proof programs. Those suggestions
remain untrusted. Fitness comes only from declarations that Lean accepts under an
explicit axiom policy.

> **ShinkaEvolve generates proof-producing ideas; the Lean kernel judges them.**

This repository is proof-search infrastructure, not a claim that generated text is
correct. A result becomes part of the verified frontier only when its declaration
elaborates, its dependency receipt satisfies the configured policy, and the run can
be replayed from hash-pinned inputs.

## What it provides

- a Shinka-compatible evaluator for Lean candidate files;
- weighted, dependency-aware proof goals defined in portable JSON;
- strict candidate checks for placeholders and custom assumption declarations;
- per-evaluation receipts with source, project, configuration, and toolchain hashes;
- hash-chained run events and a content-addressed proof-search lineage;
- replay that rechecks saved candidates instead of replaying stochastic model calls;
- a compatibility bridge for Codex `max` reasoning with the pinned ShinkaEvolve
  revision;
- a tiny end-to-end example and tests, including a real Lean kernel check.

The visual trust model and artifact flow are in the self-contained
[architecture document](docs/architecture.html).

## Quick start

Prerequisites are Python 3.11+, Git, and Lean through `elan`. Node.js and the Codex
CLI are needed only when using the bundled headless model bridge.

```bash
git clone https://github.com/brielms/LeanEvolve.git
cd LeanEvolve
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
(cd examples/demo/lean && lake build)
```

Evaluate the checked-in seed without contacting a model:

```bash
leanevolve-evaluate \
  --program_path examples/demo/seed.lean \
  --results_dir /tmp/leanevolve-evaluation \
  --config examples/demo/evolve.json
cat /tmp/leanevolve-evaluation/feedback.txt
```

The example closes the first goal and leaves the second goal available as useful
search gradient. To run ShinkaEvolve, install the pinned optional dependency and
choose a fresh results directory:

```bash
python -m pip install -e '.[shinka]'
leanevolve-run \
  --config examples/demo/evolve.json \
  --results-dir runs/demo-001 \
  --model 'headless/codex@gpt-5.6-sol?effort=max' \
  --proposal-steps 3 \
  --max-api-costs 5
```

Model calls are stochastic. Verification is not. Replay every saved candidate and
compare the fresh kernel result with its recorded receipt:

```bash
leanevolve-replay --run-dir runs/demo-001
```

## Configure a proof search

Copy `examples/demo/evolve.json`, its prompt, and its seed. The important fields are:

```json
{
  "format": "leanevolve-config-v1",
  "lean_project": "lean",
  "seed": "seed.lean",
  "prompt": "prompt.md",
  "kernel": {
    "allowed_axioms": ["Classical.choice", "Quot.sound", "propext"],
    "timeout_seconds": 120,
    "warning_as_error": true
  },
  "goals": [
    {
      "name": "base",
      "declaration": "Evolved.base",
      "target_type": "Demo.BaseTarget",
      "weight": 10,
      "depends_on": []
    }
  ]
}
```

Paths are relative to the configuration file. Goal credit is cumulative and gated
by `depends_on`: a later declaration receives credit only when Lean accepts it and
all configured predecessors. Goal names are bookkeeping labels; `declaration` is
the fully qualified Lean constant audited by the evaluator.

Candidates must retain exactly one pair of evolution markers:

```lean
-- EVOLVE-BLOCK-START
-- Shinka edits this region.
-- EVOLVE-BLOCK-END
```

The evaluator rejects placeholders such as `sorry` and `admit`, custom assumption
declarations, unsafe declarations, and compile-time commands that could counterfeit
diagnostic output. This textual gate is defense in depth. The decisive checks are
Lean elaboration and the declaration dependency receipts.

## Trust boundary

Trusted for a particular run:

1. the formal statement and supporting Lean project identified by the manifest;
2. the configured axiom policy;
3. the Lean toolchain identified by `lean-toolchain`;
4. the Lean kernel and the small receipt parser.

Not trusted: ShinkaEvolve, model output, prompts, Python orchestration, ranking,
search history, and human inspection. These components can discover or prioritize a
proof, but cannot make a rejected declaration pass the acceptance gate.

Lean compilation can execute metaprograms. The evaluator limits time and environment
but is not a general operating-system sandbox. Run untrusted campaigns in a
disposable container or virtual machine; see [SECURITY.md](SECURITY.md).

## Reproducibility and audit

Each run records:

- exact input snapshots and SHA-256 records;
- configuration and model parameters;
- an append-only, hash-linked event stream;
- every candidate and its kernel evaluation receipt;
- the parent-linked frontier selected from Shinka's run database;
- a final result inventory.

`leanevolve-replay` verifies the stored inventory first, rebuilds the snapshotted
Lean project, reevaluates candidates, and fails if accepted goals differ. The model
does not need to be available for replay.

## Development

```bash
python -m pytest
python -m ruff check .
python scripts/release_audit.py
```

The source tree intentionally keeps the evaluator small. New search features should
remain outside the mathematical trust boundary, emit auditable artifacts, and have
tests that demonstrate tamper detection or a real kernel rejection.

See [CONTRIBUTING.md](CONTRIBUTING.md) for the change checklist.
