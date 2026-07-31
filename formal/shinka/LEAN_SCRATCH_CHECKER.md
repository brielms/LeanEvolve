# Read-only Lean proposal checker

`shinka_lean_check.py` gives a Headless proposal agent fast Lean feedback
without weakening the read-only sandbox. It is a scratch tool, not a second
evaluator.

## Trust boundary

The checker:

- reads the solve directory's frozen `input_snapshot/formal/lean` tree;
- optionally reads the hash-recorded `checkpoint_input.lean`;
- reads a selected parent and the proposed Lean text;
- assembles those inputs in memory in import order;
- invokes the Lean binary named by the frozen `lean-toolchain` file as
  `lean --stdin`;
- ignores caller-supplied Elan/Lean module-path variables and bounds captured
  subprocess output;
- writes no candidate, build directory, cache, result, or receipt;
- computes no board status, spotlight score, fitness, or promotion decision.

A successful scratch check means only that this in-memory stream elaborated.
The ordinary evaluator remains authoritative: it independently stages the
candidate, applies the full source policy, audits fixed contracts and axiom
dependencies, calculates goal status, and records replayable evidence.

The default route must remain `--allow read-only`. Do not enable Headless
`yolo` or Codex workspace-write for this tool.

## Usage inside a solve

The script is intended to be copied into the solve's frozen input snapshot.
Run it from the solve root. Proposal text is always supplied through stdin, so
heredocs and temporary files are unnecessary.

Check a small block inserted immediately before `-- SHINKA-APPEND-HERE` in the
selected parent:

```sh
printf '%s\n' 'theorem trial : 1 = 1 := rfl' |
  python input_snapshot/formal/shinka/shinka_lean_check.py \
    --mode append \
    --parent best/main.lean
```

Check a complete candidate:

```sh
base64 -D <candidate.b64 |
  python input_snapshot/formal/shinka/shinka_lean_check.py \
    --mode candidate
```

The `candidate.b64` example is illustrative for a human shell. A read-only
agent should generate the base64 payload in its tool call and pipe it directly;
it must not try to create that file.

Check one strict, single-file unified diff against the selected parent:

```sh
printf '%s' "$PATCH_TEXT" |
  python input_snapshot/formal/shinka/shinka_lean_check.py \
    --mode diff \
    --parent best/main.lean
```

Request scratch-only axiom output for declarations that the proposal defines:

```sh
printf '%s\n' 'theorem trial : 1 = 1 := rfl' |
  python input_snapshot/formal/shinka/shinka_lean_check.py \
    --mode append \
    --parent best/main.lean \
    --axiom Demo.Generated.trial
```

Use `--json` for the stable `shinka-lean-scratch-v1` response. Exit codes
are:

| Code | Meaning |
| ---: | --- |
| 0 | Lean elaborated the stream |
| 2 | CLI usage error |
| 3 | input or source-policy rejection |
| 4 | frozen-source assembly rejection |
| 5 | Lean rejected the stream |
| 6 | bounded Lean timeout |
| 7 | pinned toolchain or subprocess infrastructure failure |

## Modes and policy

`candidate` expects a complete candidate on stdin. `append` inserts stdin at
the unique append sentinel, falling back to immediately before the evolve-block
end marker. `diff` applies one standard unified diff in memory with exact
context matching. Parent-based modes reject any change outside the unique
evolve block.

The two parent-based modes treat evolve markers in opposite ways, and mixing
them up is the most common avoidable rejection. `candidate` **requires** the
ordered `-- EVOLVE-BLOCK-START` / `-- EVOLVE-BLOCK-END` pair, because it
receives a whole file. `append` **forbids** them, because stdin is inserted
inside the parent's existing block and a nested marker would corrupt it.

The usual cause is recovering earlier work by copying a raw slice out of a
previous `main.lean`, which carries that candidate's markers along. Retrieve
the declaration instead of slicing it:

```bash
python input_snapshot/formal/shinka/spotlight_packet.py \
  --ledger canonical_ledger_input.sqlite3 \
  definition DECLARATION --file ../solve_0001/gen_1/main.lean
```

That returns the whole declaration and never emits marker lines. When the
checker does reject a snippet it now reports each offending line number and
both remedies.

The checker enforces the evaluator's general candidate restrictions (one
ordered marker pair, library-prefixed imports, the library namespace, and no admissions, new
axioms, unsafe/opaque declarations, metaprogramming, compile-time IO, file
inclusion, evaluation commands, or early exit). It also rejects `private`
declarations because a single in-memory Lean stream cannot reproduce private
name isolation between separately compiled modules. It deliberately does not
require or score a catalog declaration. Optional `--axiom` commands are added
by the checker after policy validation and are never part of the candidate.

If the exact `checkpoint_input.lean` exists, it is loaded automatically and
policy-checked, and the candidate must contain the evaluator's exact
`import <Library>.Generated.Checkpoint` line. `--no-checkpoint` disables only this
automatic input. The checkpoint path cannot be redirected by the proposal.
The frozen module inventory mirrors the evaluator allowlist: audit modules and
unpromoted generated modules are unavailable. No convenience tactic import is
injected beyond dependencies actually present in that inventory.

All solve inputs must resolve to regular, non-symlink paths beneath
`--solve-root`. The executable is resolved from the frozen toolchain name under
the operating-system account's Elan directory, not caller-controlled `HOME` or
`ELAN_HOME`. Source bytes, assembled bytes, captured output, runtime, and
rendered diagnostics are independently bounded.

## Orchestrator integration

The live path uses two integration points:

1. Add `formal/shinka/shinka_lean_check.py` to `run_evolution.py`'s snapshotted
   input paths.
2. Add a short prompt block naming the exact append command with the selected
   parent, and tell the proposal agent to run it before returning a nontrivial
   patch.

No Headless adapter, max-effort bridge, evaluator, replay, scoring, or sandbox
change is required.

## Library identity

`--library` names the Lean library and is a security-relevant setting, not
cosmetics. It is simultaneously the import allowlist prefix, the frozen
snapshot root, and the namespace root:

- a candidate may import only the library root or a dotted submodule of it,
  so `Demo` and `Demo.Targets` are accepted while `System.IO` and the
  lookalike `DemoEvil.Payload` are refused;
- a candidate must declare `namespace <Library>.Generated`;
- the frozen snapshot must contain `<Library>.lean`, and any module named
  `<Library>.…` that is absent from the snapshot is an assembly failure
  rather than an external import.

Set it too broadly and the allowlist stops constraining imports; set it too
narrowly and valid candidates are rejected. It defaults to `Demo`, matching
the bundled example project.
