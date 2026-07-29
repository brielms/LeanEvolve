# Unified-ledger integration audit

The SQLite event ledger is the only canonical project-information store.
Files emitted from projections are disposable compatibility, replay, or human
rendering artifacts. They must carry a ledger head or be hash-bound inside a
ledger-recorded run; changing one must never change the next runtime decision.

## Cut over

- Active goal catalog: `active_goal_catalog()` reconstructs all goal fields and
  graph edges from canonical `goal:*` objects and connections. The compatibility
  JSON carries the producing ledger head.
- Active obligation statuses: `active_goal_statuses()` derives truth from
  canonical events, including evaluator `kernel_certified` events.
- Spotlight relevance: a project adapter normalizes route mappings into
  canonical `advances` edges; `spotlight_relevance()` projects the runtime map.
- Campaign and solve configuration: with `LEANEVOLVE_LEDGER_DB` set, goal search
  reads the live projection. Campaign startup requires the database and artifact
  store and materializes a frozen, provenance-stamped evaluator input.
- Goal-board, chronology, formal-proof graph, recovery queue, prior-art summary,
  and unified status are existing disposable live-ledger projections.
- Research findings, dead-route prompt context, focus prior art, recovery, and
  status are frozen together by `materialize_runtime_bundle()` at one ledger
  head. Canonical runs exclude the historical board/crosswalk/dead-route files
  from their input snapshot.
- Model research annotations write through as new, open `research_claim`
  objects. They cannot assert proof or refutation; evaluator events remain the
  only proof-status write path.
- Active proof-state selection resolves the latest `promotion_recorded` event
  and reads its content-addressed manifest. Maintained Lean source is accepted
  only when its hash matches that ledger-selected manifest.
- Workflow and ledger status/recovery commands use `unified_status()` and
  `recovery_queue()` when a canonical ledger is configured.
- Frozen candidate catalogs, evaluator receipts, and proof lineages remain valid
  replay evidence. They are not current-state inputs.

## Project adapters and replay readers

- Legacy loaders and corpus importers stay in project-specific adapters for old
  run replay and migration tests. Canonical campaign paths do not invoke them
  for current information.
- Promotion, evaluator, field-expansion, and replay code reads frozen
  catalog/lineage/receipt artifacts belonging to the run being verified. Those
  are historical evidence, not current-state selection. The next active run is
  selected through the ledger promotion event.
- Static trust-contract, Lean-environment, and Lean source files remain direct
  inputs because they are executable policy/proof sources, not mutable research
  status. Canonical research Markdown is otherwise omitted from prompts.

Repository research JSON and Markdown are migration inputs or generated
compatibility artifacts only. They must never be used to answer current status
or select a new run.

## Adding goals to a live catalog

An expert may add or connect goal objects directly in the canonical ledger. That
write changes the live catalog immediately, but it does not retroactively change
the catalog frozen into the active Lean promotion. Before the next campaign:

1. Materialize the project's goal-catalog interface from
   `active_goal_catalog()` at the new ledger head.
2. Run the project's audited technical-refresh gate against the expanded
   catalog, recompute obligation statuses, and record
   `mathematical_change: false`.
3. Commit the resulting refresh manifest with `record_promotion()`, then run a
   deep ledger-integrity audit.
4. Run the campaign's configuration preflight. It must exercise the actual
   ledger-selected promotion/catalog binding and fail before model spend if any
   part of the handoff is stale.

Do not fix a catalog mismatch by editing a promotion receipt or by reading a
legacy goal-board projection. The refresh is the provenance-preserving bridge
between an expert's canonical goal update and the next proof-search campaign.
