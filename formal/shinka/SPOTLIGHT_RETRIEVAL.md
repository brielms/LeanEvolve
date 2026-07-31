# Canonical ledger retrieval for proposal agents

`spotlight_packet.py` builds the semantic spotlight packet and serves the
read-only retrieval commands a Headless proposal agent uses to inspect the
canonical SQLite ledger. It is a projection and query surface, not a second
source of truth.

## Trust boundary

Every command opens the ledger with `mode=ro` and `PRAGMA query_only`, and the
full-text index is built in memory. No retrieval command mutates the ledger,
writes a receipt, or changes a truth state.

Truth and verification states come only from ledger events and kernel-backed
edges. Two consequences matter when reading output:

- `ledger_backed: true` means the record is a canonical ledger object.
- `ledger_backed: false` means only local Lean source was found. Such a record
  reports `truth: unknown` and `verification: unknown`. It is source text, and
  never evidence that a declaration is proved.

## Scope

The theorem cards inside a packet are a small selected neighborhood. The
commands below are **not** scoped to that neighborhood: they query the whole
ledger. Use them to explore the wider project, not only to confirm a signature
that a card already named.

## Discovery: when you do not know a name

Reach for these first. They exist so that a declaration never has to be
guessed, and so that raw Lean paging with `sed`, `rg`, or `head` stays a last
resort. The promoted frontier is tens of thousands of lines; bulk reads crowd
out the mathematics available in a turn.

```bash
# index the whole promoted frontier source
python input_snapshot/formal/shinka/spotlight_packet.py \
  --ledger canonical_ledger_input.sqlite3 \
  outline --file compiled_checkpoint_input/frontier.lean

# narrow that index
... outline --grep cycle --kind theorem
... outline --file <path> --json

# find an existing Lean standard-library lemma instead of reproving it
... outline --library --grep erase_cons
... definition Perm.symm --library

# enumerate ledger claims
... list --kind formal_claim --state proved

# narrow the listing
... list --grep cycle --limit 40
... list --kind formal_claim --state open --json
```

`outline` reads the Lean source rather than the ledger, so it is the only
complete index of what the frontier actually defines. Each row carries the
declaration kind, name, exact `file:line`, and the ledger truth state — or
`not_in_ledger` when no ledger object covers that declaration, which is true
of roughly 140 of the frontier's 733 declarations. Indexing the entire
29,000-line frontier costs about 4k tokens, far less than a single bulk `sed`
read of it. `--file` accepts one `.lean` file or a directory; the default is
the whole project Lean tree.

`--library` points `outline` and `definition` at the pinned Lean toolchain's
source tree instead, so a standard-library lemma can be located and read
rather than reproved. The path is derived from the frozen `lean-toolchain`
file, not hard coded, so it stays correct across toolchain bumps and inside a
solve sandbox.

Dotted names are preserved and matched exactly first: `Perm.symm` is reported
as `Perm.symm`, never collapsed to `Perm`. If only the final component
matches, the excerpt header carries an explicit `WARNING` that it is a
bare-name match and may belong to an unrelated namespace — a qualified miss
never masquerades as an exact hit.

`list` accepts `--kind`, `--state` (`proved`, `refuted`, `open`, `unknown`),
`--grep` over name and declaration, `--limit` (default 80), and `--json`. Text
output is one bounded line per object: truth state, verification state,
declaration, and the canonical name when it carries prose the declaration does
not. Object ids are omitted because `signature`, `receipts`, and `neighborhood`
all accept a declaration or canonical name directly; use `--json` when ids are
needed.

Narrow with `--grep` rather than raising `--limit`. The default listing costs
roughly 1.3k tokens; all 417 proved claims cost about 10k, and an unfiltered
listing of every object costs about 68k.

```bash
# ranked full-text search with snippets
... search 'nearly disjoint family' --limit 20
```

`search` tokenizes snake_case and CamelCase before matching, so
`nearly disjoint family` finds `NearlyDisjointCycleFamily`. Results are ranked
by BM25 and carry an `excerpt` with the matched span delimited by `<<` and
`>>`, so hits can be triaged without a follow-up call. If the host SQLite lacks
FTS5, the command falls back to the older substring behaviour.

## Exact records

```bash
... signature OBJECT_OR_DECL   # exact contract, proposition hash, state
... contract GOAL_ID           # same, conventionally for a goal
... receipts OBJECT_ID         # truth, verification, receipt and source pointers
... neighborhood OBJECT_ID     # dependency neighbourhood, --depth N --limit 40
... packet OTHER_GOAL_ID       # re-aim the whole projection at another focus
```

`neighborhood` and `packet` are the two expensive commands: about 3k and 12k
tokens respectively at their defaults. Use them deliberately. `neighborhood`
is bounded by `--limit` (default 40) as well as `--depth`.

## Relations

```bash
... relations OBJECT_ID
... relations OBJECT_ID --relation refutes --direction in
... relations OBJECT_ID --json
```

`neighborhood` answers which objects are near; `relations` answers how they
connect. It reports labelled edges in both directions — `decomposes_into`,
`advances`, `refutes`, `certified_by`, `produced_by`, `specializes`, and every
other relation the ledger records — including the majority that
`neighborhood`'s dependency traversal does not follow. Retracted edges are
excluded. Cost is roughly 230 tokens at the default limit of 60.

`signature`, `contract`, and `receipts` fall back to a source-derived record
when an identifier has local Lean source but no ledger object, rather than
failing. That record is marked `ledger_backed: false` as described above.

## Source text

```bash
... definition NAME
... source NAME
... definition NAME --max-lines 60 --offset 120
... definition NAME --file ../solve_0001/gen_1/main.lean
```

`--file` searches one `.lean` file or directory instead of the project tree, which
is how to recover a declaration from an earlier candidate. Prefer it over
copying a raw `sed` slice: a slice out of a candidate drags that candidate's
`-- EVOLVE-BLOCK-START` / `-- EVOLVE-BLOCK-END` lines along, and the scratch
checker rejects an append proposal containing them. `definition` and `source`
never emit those markers, and they also drop encoded `-- RESEARCH:`
annotations, which are single lines thousands of characters long.

`definition` and `source` return a **whole declaration**, from its opening line
through the line before the next top-level declaration, rather than a fixed
window that can truncate a conclusion. The header reports the declaration's
total length; when output is capped by `--max-lines` (default 120) it also
prints the exact `--offset` needed to continue. Two leading lines of context
are included on the first page so an attribute or docstring stays visible.

## Sandbox paths

Inside a frozen solve directory the ledger snapshot is
`canonical_ledger_input.sqlite3`, taken with a transactionally consistent
SQLite backup and verified against the packet's recorded event-cursor head, so
retrieval and the packet always describe the same ledger state.
