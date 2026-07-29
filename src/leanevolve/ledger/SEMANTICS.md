# Ledger semantics

What each guarantee means, who may assert it, and what it does not imply. This
is the frozen reading of `vocabulary.py`; the golden scenarios in `fixtures.py`
are its executable form.

## The five guarantees

Verification is a ladder. A rung is *earned*, never assigned, and a lower rung
is never silently upgraded.

| Level | Earned by | Binds | Does not mean |
|---|---|---|---|
| `scratch_checked` | An in-memory Lean stream elaborated through the scratch gateway | exact submitted source, checkpoint key, environment identity, declaration, normalized proposition hash | that the declaration is in any candidate, frontier, or maintained file |
| `axiom_policy_audited` | `#print axioms` output for the declaration with dependencies ⊆ the standard-axiom policy | declaration, axiom list, policy name | that anything else about the proof was independently re-checked |
| `authoritatively_evaluated` | A separate evaluation applied its own source policy, staged the candidate, and produced receipts that load and verify | evaluator version, stage, source, dependencies, toolchain, catalog, checkpoint | that the result has been promoted or is reachable from the maintained frontier |
| `promotion_audited` | A clean standalone audit accepted a prospective promotion | manifest and catalog hashes, composed declarations | that the promotion has been recorded as active |
| — | `promotion_recorded` makes the audited frontier active | manifest sha256, catalog sha256 | anything about mathematical strength beyond what the receipt already bound |

Concrete proof projects bind these rungs to their own scratch checker,
axiom-policy auditor, authoritative evaluator, and promotion gate. Those
adapters must record exact source, toolchain, proposition, policy, and receipt
identities; the ledger core deliberately does not name a project layout or
theorem namespace.

Scratch success surviving a killed turn is the point of the durable lifecycle.
It is *not* a shortcut around any later rung.

## Truth

Truth has exactly three values and only one way in.

- `open` — no trusted proof or refutation is present. This is the default and
  the honest answer for everything unresolved, including timeouts.
- `proved` — an exact proposition has a currently valid `kernel_certified`
  event whose receipt binds source, dependencies, toolchain, evaluator, catalog,
  checkpoint, and axiom policy.
- `refuted` — a trusted formal negation or a trusted refutation bridge applies.

`kernel_certified` is the only truth-bearing action, and only the
`authoritative_evaluator` may emit it. A research agent, the scratch gateway,
the computation checker, and a human researcher all cannot — enforced by
`authorize()`, tested in `tests/test_ledger_vocabulary.py::TestAuthority`.

### Refutation needs a bridge

A witness alone never refutes. A `refutes` edge must originate at a
`formal_claim` carrying `trust_level: "kernel"`; the witness attaches to that
bridge with `supports`. This is deliberate: a counterexample plus a proved
bridge is an n-ary relationship, and one binary edge cannot honestly carry two
premises. Retracting the bridge's certification returns the target to `open`.

## What must never imply what

Each dimension is independent, and the vocabulary keeps their value sets
disjoint so an overloaded status cannot creep back in.

- A **timeout** is `operational: timed_out`. It is unresolved — never `refuted`,
  never `failed`.
- An **operational run failure** does not invalidate a check that already
  completed. The check, not the turn, is the transaction boundary.
- **Computational evidence** — even an exhaustive, independently checked DRAT
  refutation — supports a claim. It is not a kernel theorem.
- **`fully_encoded` is not `proved`.** Encoding state describes how completely a
  claim is written down in a formal system, nothing about whether it holds.
- **`superseded` is not `refuted`.** Replaced is a lifecycle fact; it makes no
  claim about falsity, and replaying history to an earlier event must still show
  the old claim active.
- **A citation or a retained PDF** does not make a formal claim trusted.
  `source_evidence_state` is its own dimension for exactly this reason.

## Identity

A formal claim's identity is its normalized proposition, not its name.

- Changing the proposition creates a **new object** plus an explicit
  `supersedes`, `specializes`, `strengthens`, or `weakens` edge.
- Renaming a declaration without changing the proposition adds an alias
  (`alias_added`), not a new claim.
- A proposition written as `def ... : Prop` is a formal claim with no proof.
- A goal does not become a second object when it is proved. `kind` says what a
  thing *is*; roles like goal, spotlight target, or frontier member are
  properties and projection concerns.

Artifacts are identified by content hash. Locations are replaceable, and every
add, verify, move, archive, or loss is an event.

## Authority

| Actor | May | May not |
|---|---|---|
| `research_agent` | propose claims and relationships, annotate, abandon its own routes, request checks | certify, promote, declare a computation proved, revise an exact proposition |
| `lean_scratch_gateway` | store exact source, record elaboration and scratch outcomes, create formal claims for checked declarations | certify, promote |
| `authoritative_evaluator` | emit evaluation, certification, materialization, promotion | — |
| `axiom_auditor` | emit axiom-policy outcomes | certify |
| `computation_checker` | record computations and verified certificates | certify |
| `human_researcher` | create, annotate, correct metadata, choose spotlights, authorize campaigns | certify |
| `importer` | replay history, declaring its source and ordering fidelity | invent guarantees the old records never carried |
| `ledger_service` | reconcile, manage retention and artifact locations, record lifecycle | certify |

## Corrections

History is append-only. A correction adds a new fact naming the event it
corrects (`correction_recorded`); it never rewrites the old one. A retracted
connection leaves the current projection and stays in the audit history. An
imported event states whether its original time and ordering are `exact`,
`inferred`, or `unknown` — the importer may not launder an unknown ordering into
a confident one.
