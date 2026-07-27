# Supported workflows

`mise` is LeanEvolve's public task interface. `uv` supplies the exact locked Python
environment behind every Python-backed task, and Lake remains the Lean build and
kernel interface. The commands in `mise.toml` are deliberately visible and are the
same commands used by scientists, CI, and automated agents.

## First run

```bash
mise trust
mise install
mise run setup
mise run doctor
mise tasks
mise run demo
```

Trust is a mise safety gate required once per checkout. Setup pins Python and uv,
installs the Git-revision-pinned ShinkaEvolve dependency from `uv.lock`, and is safe
to repeat. It ends with the next useful commands. `doctor --json` reports resolved
absolute paths and versions without printing credential values.

## Everyday and release validation

- `mise run check` runs lint, tests, and incremental Lake builds. It is the fast
  edit-time signal and does not claim a clean forensic replay.
- `mise run audit` checks `uv.lock`, runs the configured publication scan and
  documentation link check, cleans and rebuilds each Lake project, runs configured
  axiom gates, and verifies the offline demo. Add `--replay latest` or `--replay all`
  to include campaign replay.
- `mise run demo` evaluates the bundled candidate through the ordinary Lean trust
  boundary and writes a small hash-verified receipt without a model call.

## Campaigns

```bash
mise run plan -- shinka --proposal-steps 3
mise run shinka -- --proposal-steps 3
mise run shinka -- --yes --proposal-steps 3
mise run campaigns
mise run replay -- --run-dir runs/<campaign-id>
```

Planning performs the same lock, interpreter, tool, storage, schedule, configuration,
and cost checks as launch, but creates no campaign directory and consumes no model
turn. Interactive launch requests confirmation. `--yes` is the explicit automation
policy for non-interactive launch and appears in the task receipt.

The current reusable runner accepts sequential `--proposal-steps`. Ordered
solve/field-expansion chunk schedules belong to campaign adapters that declare a
`chunks` schedule in `leanevolve.toml`; the parser preserves each solve and expansion
epoch in order and never uses mise job parallelism to alter that trajectory.

## Documentation site

<https://brielms.github.io/LeanEvolve/> is generated from this repository, never
hand-maintained: `index.html` is rendered from `README.md`, this page from
`docs/workflows.md`, and the architecture page is copied from `docs/architecture.html`.

```bash
mise run docs
python -m http.server --directory _site 8000
```

`docs` writes the site to the ignored `_site/` directory and then checks it. Because
Pages serves the site under a repository prefix, a repo-relative link such as
`../README.md` would break once published; the builder rewrites links that name a
site page to that page, and every other repository path to a commit-pinned GitHub
URL. The check fails on any internal link that would not resolve, on a link that
would publish raw Markdown, and on a site with no `index.html`. Each page records the
commit it was built from in a `leanevolve-source-commit` meta tag and its footer.

The `Documentation site` GitHub Actions workflow runs the same command through the
same locked environment on every pull request, and deploys to Pages only from
`master`. A pull request therefore proves the site builds before it can publish.

## Portable local settings

Version-controlled scientific defaults live in `leanevolve.toml`. Machine-specific
paths and limits belong in the ignored `leanevolve.local.toml`:

```bash
mise run configure -- --artifact-root /mounted/evidence/runs
mise run configure -- --cache-root /local/fast-cache
mise run configure
```

The first two commands write overrides; the last prints effective settings. The
resolved absolute paths, storage reserve, filesystem location, tool versions, and
input hashes are retained in task or campaign receipts.

## Machine-readable output and failures

Append `--json` to a mise task, for example `mise run status -- --json`. Receipts use
the versioned `leanevolve-task-receipt-v1` format and include the task version, input
and environment details, output paths, scientific status, guarantees, exclusions,
and recommended next action.

Stable exit classes are: `0` success, `2` malformed input, `3` missing tool or
environment, `4` validation rejection, `5` completed scientific non-result, `6`
infrastructure failure, and `130` interruption. Detailed subprocess logs are kept
under `.cache/leanevolve/logs/`; the default output stays short.

## Ownership boundary

`src/leanevolve/workflow/` owns the public task experience: environment diagnosis,
portable settings, planning, cost and storage gates, status, and task receipts. The
existing `evaluate.py`, `run.py`, and `replay.py` modules are library/runner adapters
invoked by that layer. Old console scripts remain compatibility surfaces, but the
documented supported entry points are the mise tasks.
