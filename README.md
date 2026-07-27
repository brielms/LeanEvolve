# LeanEvolve

[![Lean 4](https://img.shields.io/badge/Lean-4.32.1-0f766e)](https://lean-lang.org/)
[![proofs-kernel_checked-16a34a](https://img.shields.io/badge/proofs-kernel__checked-16a34a)](docs/architecture.html)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)

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

The trust boundary, module map, data flow, run artifacts, and current limitations are
in the self-contained [architecture document](docs/architecture.html).

## Install and diagnose

The supported interface is [mise](https://mise.jdx.dev/). It pins Python and uv,
uses uv's checked-in lockfile for every Python command, and leaves Lean builds to
Lake. Install Git, mise, and Lean through `elan`; Node.js and the Codex CLI are
needed only for the bundled headless model route.

```bash
git clone https://github.com/brielms/LeanEvolve.git
cd LeanEvolve
mise trust
mise install
mise run setup
mise tasks
```

`setup` is safe to repeat. If anything is unavailable or mismatched, start with
`mise run doctor`; its receipt gives one concrete recovery command per failure.
No virtual-environment activation or interpreter path is part of the workflow.

## Validate

Run the deterministic, no-spend demonstration, then the fast edit-time gate:

```bash
mise run demo
mise run check
```

`check` keeps Lake's incremental products. `mise run audit` is the slower release
gate: it checks the lock, runs the publication scan, rebuilds Lean from clean, and
re-verifies the offline demonstration. Neither command presents an open proposition
as a proved theorem.

Every important task accepts `--json`, for example
`mise run status -- --json`. Human and JSON results are also retained under
`.cache/leanevolve/receipts/`.

## Run or preview a campaign

Preview validates the configuration, storage reserve, schedule, pinned environment,
and hard spend ceiling without creating a campaign directory or contacting a model:

```bash
mise run plan -- shinka --proposal-steps 3
mise run shinka -- --proposal-steps 3
```

Interactive runs ask before spending. Agents must add `--yes`, and that authorization
is recorded: `mise run shinka -- --yes --proposal-steps 3`.

Model calls are stochastic; verification is not. Inspect status and replay a saved
campaign through the same locked environment:

```bash
mise run status
mise run campaigns
mise run replay -- --run-dir runs/<campaign-id>
```

`mise run menu` is the detailed workflow catalog, including inputs, outputs, cost,
runtime, and examples. See [the workflow guide](docs/workflows.md) for configuration,
portable storage profiles, receipts, exit codes, and the boundary between mise, uv,
Lake, and the underlying library commands.

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
mise run test
mise run lint
mise run audit
```

The source tree intentionally keeps the evaluator small. New search features should
remain outside the mathematical trust boundary, emit auditable artifacts, and have
tests that demonstrate tamper detection or a real kernel rejection.

See [CONTRIBUTING.md](CONTRIBUTING.md) for the change checklist.
