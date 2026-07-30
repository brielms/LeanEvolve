# LeanEvolve

[![Lean 4](https://img.shields.io/badge/Lean-4.32.1-0f766e)](https://lean-lang.org/)
[![proofs-kernel_checked-16a34a](https://img.shields.io/badge/proofs-kernel__checked-16a34a)](docs/architecture.html)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)

LeanEvolve connects [ShinkaEvolve](https://github.com/SakanaAI/ShinkaEvolve) to Lean 4. A
language model proposes definitions, lemmas, tactics, and whole proof programs. Every one
of those proposals is untrusted text. Fitness comes from one source only: declarations that
Lean accepts, whose axiom dependencies fall inside a policy fixed before the run started.

> ShinkaEvolve generates proof-producing ideas; the Lean kernel judges them.

This is proof-search infrastructure. It is not a claim that generated text is correct, and
nothing here presents an open proposition as a proved theorem.

**Documentation:** [architecture](docs/architecture.html) ·
[research ledger](docs/ledger.html) · [workflows](docs/workflows.md). The published site is
generated from these sources and stamped with the commit it was built from.

## What it provides

A scaffold for pointing [ShinkaEvolve](https://github.com/SakanaAI/ShinkaEvolve) — Sakana AI's evolutionary program-search loop — at a Lean 4 project, so that the search can propose incremental proofs without ever being trusted to have found one.

- A Shinka-compatible evaluator: Shinka mutates candidate Lean files, the evaluator builds them and reports back whether the Lean kernel accepted the result, so the fitness signal is a kernel verdict rather than a model's judgment
- Weighted, dependency-aware goals: the open theorems are scored so search effort follows what unblocks the most downstream results, instead of a flat queue
- Per-evaluation receipts: each result is bound to the hashes of its candidate source, the Lean project, the config, and the toolchain — so a score can be re-derived, and can't silently drift when the environment changes
- Hash-chained events and content-addressed lineage: every run appends to a tamper-evident log, and each proof is addressed by the content of its search history, so you can trace any accepted theorem back to what produced it
- Replay by recheck: saved candidates are re-verified against the kernel rather than reproduced by re-running stochastic model calls


## Design notes

- Credit comes from Lean, not from the model. For each goal, LeanEvolve asks Lean to elaborate example : <target_type> := <declaration> and print that declaration's axiom dependencies. A proof that type-checks but leans on an out-of-policy axiom scores zero; so does a declaration published under the right name that proves something else. There is no regex over Lean's output and no self-report.

- Verification is a ladder, not a boolean. "Elaborated in a scratch buffer" and "passed a standalone promotion audit" are different rungs with different names, and a lower one is never silently promoted. An exhausted budget is unresolved, never refuted — running out of turns is a fact about a search, not about mathematics.

- The architecture document (docs/architecture.html) covers the trust boundary, acceptance rule, module map, and data flow, including a plain list of what this design does not give you.

## Install

The supported interface is [mise](https://mise.jdx.dev/). It pins Python and uv, runs every
Python command through uv's checked-in lockfile, and leaves Lean builds to Lake. Install
Git, mise, and Lean through `elan` first. Node.js and the Codex CLI are needed only for the
bundled headless model route.

```bash
git clone <repository-url>
cd LeanEvolve
mise trust
mise install
mise run setup
mise tasks
```

`setup` is safe to repeat. If anything is missing or mismatched, run `mise run doctor`: its
receipt gives one concrete recovery command per failure. No virtual-environment activation
or interpreter path is ever part of the workflow.

## See it work

The demonstration is offline and deterministic. It spends no model credits and needs no API
key:

```bash
mise run demo
mise run check
```

The bundled example is honest about being partly unsolved, which is what makes it a useful
demonstration. `examples/demo/lean/Demo/Targets.lean` states two propositions:

```lean
def ZeroRightTarget : Prop := ∀ n : Nat, n + 0 = n
def AdditionCommutesTarget : Prop := ∀ a b : Nat, a + b = b + a
```

The checked-in seed proves the first and deliberately leaves the second absent. So the demo
scores 10 of a possible 35 and reports exactly that — an accepted goal and an open one,
never a total. That gap is the measurable improvement a search engine is pointed at.

`check` is the fast edit-time gate and keeps Lake's incremental products. `mise run audit`
is the slower release gate: it verifies the lockfile, runs the publication scan and the
documentation link check, rebuilds Lean from clean, runs configured axiom gates, and
re-verifies the offline demonstration.

## How scoring works

A candidate is checked as a whole first; if that fails, nothing is credited and no goal is
even audited. Otherwise each goal is audited in its own Lean run, and goals are settled in
configuration order. A goal is accepted when all four hold:

1. Lean elaborates `example : <target_type> := <declaration>`, binding the configured
   proposition to the candidate's declaration;
2. that same run exits zero and prints a `#print axioms` line for the exact declaration
   name;
3. the reported axioms are a subset of `kernel.allowed_axioms`;
4. every goal named in `depends_on` has already been accepted.

The score is the sum of `weight` over accepted goals. All Lean invocations for one candidate
share a single `kernel.timeout_seconds` budget, so a slow candidate cannot buy extra time by
declaring more goals.

## Configure a proof search

Copy `examples/demo/evolve.json` with its prompt and seed. Paths resolve relative to the
configuration file:

```json
{
  "format": "leanevolve-config-v1",
  "lean_project": "lean",
  "seed": "seed.lean",
  "prompt": "prompt.md",
  "candidate": { "max_bytes": 1048576 },
  "kernel": {
    "allowed_axioms": ["Classical.choice", "Quot.sound", "propext"],
    "timeout_seconds": 120,
    "warning_as_error": true,
    "sandbox_prefix": []
  },
  "goals": [
    {
      "name": "zero_right",
      "declaration": "Evolved.zero_right",
      "target_type": "Demo.ZeroRightTarget",
      "weight": 10,
      "depends_on": []
    },
    {
      "name": "addition_commutes",
      "declaration": "Evolved.addition_commutes",
      "target_type": "Demo.AdditionCommutesTarget",
      "weight": 25,
      "depends_on": ["zero_right"]
    }
  ]
}
```

| Field | Meaning |
|---|---|
| `name` | A bookkeeping label. `depends_on` refers to these. |
| `declaration` | The fully qualified Lean constant the evaluator audits. |
| `target_type` | The proposition the declaration must inhabit. This is what makes the name unfakeable. |
| `weight` | Points added to the score when the goal is accepted. |
| `depends_on` | Goal names that must already be accepted for this one to count. |
| `allowed_axioms` | The complete permitted axiom base. An empty list allows none. |
| `warning_as_error` | Adds `-DwarningAsError=true`. This includes style linters. |
| `sandbox_prefix` | Prepended to the Lean command line, for attaching an external sandbox. |

Candidates must retain exactly one ordered pair of evolution markers:

```lean
-- EVOLVE-BLOCK-START
-- Shinka edits this region.
-- EVOLVE-BLOCK-END
```

Before Lean runs, comments and string contents are scrubbed and the remaining source is
scanned for `sorry`, `admit`, `axiom`, `constant`, `#eval`, `#reduce`, `#guard`, `elab`,
`macro`, `syntax`, `extern`, `foreign`, `implemented_by`, `initialize`, `run_tac`, `unsafe`,
and `partial`. This textual gate is defence in depth; the axiom receipt is what decides.

## Run a campaign

Preview first. `plan` validates configuration, storage reserve, schedule, pinned
environment, per-chunk ceiling, and aggregate spend authorization without creating a
campaign directory or contacting a model:

```bash
mise run plan -- shinka --proposal-steps 3
mise run shinka -- --proposal-steps 3
```

Interactive runs ask before spending. Agents must pass `--yes`, and that authorization is
recorded in the receipt: `mise run shinka -- --yes --proposal-steps 3`.

Project adapters may expose short Spotlight schedules such as
`--spotlight 'intermediate_goal for 3 turns'`. A Spotlight freezes one exact objective and
its kernel-backed relevance path while keeping the full proof field visible; the outcome may
only be `proved`, `refuted`, or `unresolved`.

Then inspect and recheck:

```bash
mise run status
mise run campaigns
mise run replay -- --run-dir runs/<campaign-id>
```

`mise run menu` is the full workflow catalog with inputs, outputs, cost, runtime, and
examples. See [the workflow guide](docs/workflows.md) for configuration, portable storage
profiles, receipts, and the boundary between mise, uv, Lake, and the library commands.

## Trust boundary

Trusted for a particular run:

1. the formal statements in the Lean project identified by the manifest;
2. the configured axiom policy;
3. the Lean toolchain identified by `lean-toolchain`;
4. Lean's elaborator and kernel, and the small receipt parser.

Not trusted: ShinkaEvolve, model output, prompts, Python orchestration, ranking, search
history, the research ledger, and human inspection. Those components can discover or
prioritize a proof. None of them can make a rejected declaration pass.

Each Lean invocation runs in the Lake project directory under an environment reduced to
`ELAN_HOME`, `HOME`, `LANG`, `LC_ALL`, `PATH`, `TMPDIR`, and `USER`. That is not
containment. Lean compilation can execute metaprograms at compile time, so run untrusted
campaigns in a disposable container or virtual machine; see [SECURITY.md](SECURITY.md).

## Reproducibility and audit

Each run records exact input snapshots with SHA-256 records, the configuration and model
parameters, an append-only hash-linked event stream, every candidate with its kernel
receipt, the parent-linked frontier selected from Shinka's run database, and a final result
inventory.

`mise run replay` verifies the stored inventory and lineage hashes, rebuilds the snapshotted
Lean project, re-evaluates every recorded candidate, and fails if the accepted goals differ.
The model need not be available.

One limit stated plainly: these hashes are tamper-*evident*, not tamper-proof. They detect
edits relative to a manifest an auditor already trusts. Against someone who can rewrite the
manifest too, they establish nothing on their own.

## The research ledger

Optional, and off unless configured. It stores typed research objects, relationships,
append-only events, evaluator receipts, and content-addressed artifacts in one auditable
system. Goal boards, chronologies, proof graphs, prior-art crosswalks, recovery queues, and
status reports are disposable projections rather than competing sources of truth.

```bash
mise run configure -- \
  --ledger-database /path/to/research.sqlite3 \
  --ledger-artifacts /path/to/ledger-artifacts
mise run ledger -- --database /path/to/research.sqlite3 verify \
  --artifacts /path/to/ledger-artifacts --deep
```

The two paths are an all-or-nothing pair. Projects that complete cutover list the workflows
that must fail closed under `ledger.required_workflows` in `leanevolve.toml`.

The core is domain-neutral: it names no theorem namespace and no project layout.
Theorem-specific corpus importers and compatibility adapters belong in downstream proof
projects. See [the ledger page](docs/ledger.html) for the verification ladder, the authority
model, three-valued truth, and the projections.

## Task interface

Every important task accepts `--json`, for example `mise run status -- --json`. Receipts use
the versioned `leanevolve-task-receipt-v1` format and are written to disk before the process
exits, including on interruption. Human and JSON results are retained under
`.cache/leanevolve/receipts/`, and detailed subprocess logs under
`.cache/leanevolve/logs/`.

| Exit | Class | Meaning |
|---|---|---|
| `0` | `OK` | Task succeeded. |
| `2` | `USAGE` | The request was malformed or referenced missing inputs. |
| `3` | `MISSING_TOOL` | A required tool or environment is unavailable. |
| `4` | `VALIDATION` | A gate rejected the repository or an artifact. |
| `5` | `NO_RESULT` | The workflow ran but produced no scientific result. |
| `6` | `INFRASTRUCTURE` | Storage, network, or a subprocess failed unexpectedly. |
| `130` | `INTERRUPTED` | The task was interrupted before it finished. |

Exit `5` is worth dwelling on: a search that ran correctly and found nothing is a different
outcome from a search that broke, and the two get different codes.

## Development

```bash
mise run test
mise run lint
mise run docs
mise run audit
```

`mise run docs` regenerates the site into `_site/` and fails on any internal link that would
not resolve once GitHub Pages serves it under a repository prefix. Preview it with
`python -m http.server --directory _site 8000`. Building proves the links resolve; it proves
nothing about whether the prose is true.

Keep the evaluator small. New search features should stay outside the mathematical trust
boundary, emit auditable artifacts, and come with a test that demonstrates tamper detection
or a real kernel rejection. See [CONTRIBUTING.md](CONTRIBUTING.md) for the full checklist.
