# CI and the review gates

What runs on a pull request, what each gate is for, and how they fold into one
verdict. The source of truth is `.github/workflows/`; this doc explains the
shape and the rationale.

The `prepare-pr` skill
(`src/kiro_crew/builtin_skills/kirocrew-dev/prepare-pr/SKILL.md`) is the agent
side of this: it drives a working tree to review-ready by working with these
gates. Its phase flow, exit-code contract and PR-description contract live in
that skill, not here. Its portability design is
[../request-for-change/rfc-prepare-pr-portability.md](../request-for-change/rfc-prepare-pr-portability.md). The human release process
is [CONTRIBUTING.md](../../CONTRIBUTING.md).

## Shape

CI is a **fan-out of independent workflows that one aggregator folds into a single
verdict**, with exactly one ordering edge inside it: the eleven cheap blocking
gates run in their own workflow, and both the expensive matrix and the fork
reviewers wait for its verdict rather than racing it.

```
pull_request
  |-- fast-gate.yml     "Fast Gate"    the 11 cheap blocking gates (~44s wall clock)
  |     |
  |     |-- ci.yml's `await-fast-gate` job releases the heavy jobs
  |     '-- the five fork-*-review.yml lanes trigger on its completion
  |
  |-- ci.yml            "CI"           lint, sharded tests, coverage gate, e2e
  |-- build.yml         "Build"        wheel + desktop artifacts still build
  |-- code-review.yml   "Code Review"  grep rules, woke, Semgrep, PR hygiene, dep audit
  |-- dependency-review.yml            license allowlist
  |-- docker-smoke.yml                 container contract (paths-filtered)
  |-- claude-review.yml "Opus 4.8 Review"     line-level, code-only, blocking
  |-- codex-review.yml  "GPT 5.6 Review"    line-level + PR intent, blocking
  |-- design-review.yml "Design Review"     design shape, advisory
  |-- ux-review.yml     "UX Review"         rendered experience, advisory
  |-- first-principles-review.yml
  |                     "First Principles Review"  why it exists, advisory
  |-- CodeQL                                GitHub default setup, not a checked-in file
  |
  '-> pr-readiness.yml  "PR Readiness"  one commit status + one readiness: label
```

Three structural facts explain most of the rest:

- **The cheap gates decide whether the expensive ones get to run.** The eleven
  gates in `Fast Gate` cost 198 job-seconds between them, about 70% of which is
  runner acquisition and checkout, and they finish in ~44 seconds because they run
  in parallel. A median CI run is 240 job-minutes and 54 minutes of wall clock, and
  the eight `backend-test` shards alone are 73.7% of those job-minutes. While the
  gates lived in `ci.yml` the matrix started alongside them, so a gate that went red
  in twenty seconds still let the whole matrix run to completion. They are now a
  separate workflow with the same triggers, and `ci.yml`'s `await-fast-gate` job —
  which every heavy job needs — is the edge that makes a red gate SKIP the matrix
  instead of racing it. A `needs:` edge cannot cross a workflow file, which is why
  that barrier is a job that reads the other workflow's run rather than a
  dependency GitHub resolves for us.
- **The real merge gate is human approval plus armed auto-merge.** `PR Readiness`
  is the one status worth watching; individual red checks are strong signals a
  human can weigh.
- **A fork PR is aggregated like any other and can reach a passing readiness
  state**; CodeQL is the one lane it cannot run. See [Fork PRs](#fork-prs).

Out-of-band lanes that never gate a PR:

- **Release and publish**, tag- or schedule-triggered: `release.yml`,
  `nightly.yml`, the reusable `build-wheel.yml` / `build-desktop.yml` /
  `build-windows.yml`, `sign-and-notarize.yml`, `publish-cli.yml`,
  `publish-linux.yml`, `publish-docker.yml`, `publish-installer.yml`,
  `pages.yml` (the marketing site in `site/`, path-scoped so it never runs for
  backend or dashboard changes).
- **Verification that is too slow or too expensive for a PR:** `ota-test.yml`
  builds two real app bundles and performs an actual update swap, because the
  Electron unit suite stops at the `autoUpdater` handoff and never proves a real
  bundle is replaced on disk and relaunches.
- **The ratchet verdict `main` otherwise never gets:** `main-ratchet-audit.yml`
  re-runs only the cheap ratchet, ceiling and baseline gates on every push to
  `main`. Two things make a push to `main` unable to answer for them in `ci.yml`:
  GitHub keeps one *pending* run per concurrency group, so on a busy `main` each
  run is evicted before its slower lanes report and a commit's checks end up
  `cancelled` rather than `failure` — which is not a red X, so `main` looks green
  while drift accumulates; and the lint lanes are surface-gated, so a
  backend-only merge *skips* the eslint ceiling outright. This lane's group is
  keyed on the SHA so no push can supersede an earlier push's audit, it runs both
  surfaces unconditionally, and it reconciles one `ratchet-audit`-labeled tracking
  issue — opened on drift, commented on each further drifting push, closed on the
  next all-green one. Because per-SHA groups let audits for different commits
  finish out of order, only a run whose commit is still `main`'s head writes to
  that shared issue: a slow green audit would otherwise close the live drift
  record a newer push just opened. An unreadable head resolves toward keeping
  drift visible in both directions — still recorded on drift, still not closed on
  green. Every gate step runs on `!cancelled()` rather than the default
  `success()`, so one drifting ratchet does not skip the rest and reduce the
  verdict to whichever gate is listed first; and the set of gate scripts is
  pinned equal to `ci.yml`'s `backend-lint`, because a gate *added* there and not
  mirrored here would never be measured on `main` at all. Two further details are
  load-bearing. It sets
  `RATCHET_SCOPE_WHOLE_TREE`, because the four diff-scoped gates
  (`scripts/ratchet_scope.py`) would otherwise resolve an EMPTY diff on a push to
  the branch they measure against and pass by judging nothing; and it *reads* the
  eslint ceiling out of `ci.yml` rather than transcribing it, because a second
  copy would keep granting the old budget after a burn-down and report green on a
  tree the PR gate reds. It deliberately does not touch `ci.yml`'s concurrency or
  add a second full run: full serialization or a merge queue is a runner-budget
  call, and `test-durations.yml` already pays for a full suite on `main`.
  Contributor-facing half: [CONTRIBUTING.md](../../CONTRIBUTING.md).
- **Maintenance:** `ship-report.yml` (a scheduled Slack summary),
  `cleanup-temp-screenshots.yml` (prunes the ephemeral `temp-screenshots/` dir,
  see [its README](../../temp-screenshots/README.md); safe because PR bodies
  embed commit-SHA-pinned raw URLs that keep resolving),
  `test-durations.yml` (re-measures `.test_durations` so pytest-split's shards stay
  balanced by recorded runtime, and opens a PR with the update), `issue-triage.yml`
  (a model picks `type:` / `area:` / `platform:` labels from the repository's own
  live label set, because keyword rules mislabel often enough to be worse than no
  label), `issue-summary.yml` (a second, deliberately separate lane posts ONE
  comment per new issue: the report restated for a maintainer, the information
  still missing, and the recent issues most likely to be duplicates. Split from
  triage because publishing prose gives a prompt injection an audience that the
  label path does not have — so this lane, and only this lane, carries the
  markdown neutralizer and the candidate-pool intersection that stop an issue
  body from minting a `#N` reference or a mention. It gets no checkout on
  purpose; grounded, file-level investigation is Issue Radar's Investigate
  button, not a CI comment), `pr-merge-conflict-label.yml` and `fork-pr-label.yml`
  (both mirror a fact GitHub does not surface in the `/pulls` list onto a label), and
  `add-contributor.yml` (a daily cron, plus manual dispatch, adds each merged
  PR's author AND the reporters of the issues that PR closed to the README
  Contributors block via
  `scripts/update_contributors.py`; because the default branch is protected it
  opens a rolling PR rather than committing directly, like `test-durations.yml`.
  A login in `.github/contributors-optout.txt` is never added, which keeps the
  README's removal promise enforceable against the full-rebuild collector).
  One paginated GraphQL sweep over `pullRequests(states: MERGED)` drives it,
  reading each node's `author` and its `closingIssuesReferences` authors. The
  reporter side is deliberately keyed on that link rather than on listing
  `/issues`: the connection is populated only when a PR declares it closes the
  issue, and only merged PRs are scanned, so an entry is evidence the report
  changed the product — which keeps duplicates, invalid reports and
  credit-farming issues out. It undercounts by design (a fix that omitted the
  closing keyword is invisible), and the remedy is the manual `--login` path, not
  loosening the rule. Dedup is two-layered: `sort -u` over the union, because
  someone can be both a PR author and a reporter, then the script's own README
  scan. The same block also holds contributors whose contribution left neither
  trace — a review, a translation, a private security report — added with
  `scripts/update_contributors.py --login`. Those entries survive every later run
  because the collector only ever inserts and never rewrites an existing line;
  that preservation is what makes one shared list workable instead of a second
  table.

  Note that opening that rolling PR is best-effort. This repository leaves
  "Allow GitHub Actions to create and approve pull requests" off — one switch
  covers creating AND approving, and `main`'s merge gate is a required review — so
  `gh pr create` with `GITHUB_TOKEN` is refused with `GitHub Actions is not
  permitted to create or approve pull requests`. It only bites after the previous
  rolling PR merged and its branch was deleted; while the PR is open, pushing to
  the branch is enough. The push happens first either way, so a refusal is a
  handoff, not a loss: the job stays green and files/updates one issue titled
  "Add Contributor needs a human to open the contributors PR" carrying the compare
  link. The same limitation applies to every workflow here that opens a PR
  (`test-durations.yml`, `cleanup-temp-screenshots.yml`, `memory-benchmark.yml`),
  which carry the same guard in a lighter form: they emit a `::notice::` with the
  compare link and exit 0 rather than filing an issue, because their branches are
  regenerated on the next scheduled run and so do not need a durable tracker. Any
  create failure that is NOT that refusal still fails the job in all four.
  `test/test_workflow_pr_create_handoff.py` holds them in step and fails a new
  `gh pr create` step that skips the guard.

### Code ownership

`.github/CODEOWNERS` assigns every repository path to `@kirodotdev/kirocrew-team`
through a single wildcard rule. GitHub reads that file to request reviews; the file
itself establishes no approval count and enforces no branch protection, so a tier, a
required number of reviewers, or a designated-maintainer requirement cannot be
inferred from it. The wildcard rule carries the ownership declaration, and GitHub
branch protection stays the enforcement point for any required approval policy.
`fork-workflow-guard.yml` is what keeps a fork PR from editing the file (see
[Fork PRs](#fork-prs)).

## `fast-gate.yml`: the cheap blocking gates

Every job here is blocking, and nothing here is behind a path filter — the
workflow has no `changes` job at all, because a gate that costs a few seconds is
cheaper to always run than to decide about, and a filter is one more thing that can
be dodged by an edge case in its own globs.

They live in their own workflow for two reasons that both come down to who has to
wait for them. `ci.yml`'s heavy jobs now wait through `await-fast-gate`, so a red
gate skips ~220 job-minutes of tests it was previously running beside. And the five
`fork-*-review.yml` lanes need SOME trusted workflow to vouch for a fork's head
commit before they start (see [Fork PRs](#fork-prs)); waiting for all of `CI` put a
fork PR's AI verdict ~54 minutes out, when the gates that verdict actually needs are
green after one.

The trigger set is copied from `ci.yml` deliberately, `branches: [main]` included.
The fork reviewers key on this workflow now, so a wider filter here would newly
review fork PRs opened against a non-main base — which today get no review at all,
because they wait on a `CI` run that `ci.yml`'s own branch filter never starts.
Widening that is a separate decision from moving the gates.

| Job | What it enforces |
|---|---|
| `scrub-lint` | `scripts/scrub-lint.sh --no-history`. Fails on any internal marker in this public tree, so a sync cannot reintroduce a coupling |
| `vendor-manifest` | `scripts/verify_vendor_manifest.py`. Hashes every file under `src/kiro_crew/_vendor` against the committed `scripts/vendor_manifest.sha256` — the tree is excluded from semgrep and the AI reviewers' diff, so this checksum is its only content review. Hashing the ~26MB tree takes seconds, so it is always-on like the rest of this workflow |
| `brand-lint` | `scripts/check_brand_name.py`, self-test first. Fails on a newly added line that joins the two words of the product name. Diff-scoped: the tree still carries thousands of pre-convention prose lines, so a whole-tree gate would charge that backlog to whoever pushed next; the whole-tree count is still printed as a non-failing report |
| `focus-cue-lint` | `scripts/check_focus_cue.py`, self-test first. Fails when a change writes the `className` of an element that then has no visible focus cue. Diff-scoped for the same reason as `brand-lint`, and reports whole-tree |
| `feature-map-lint` | `scripts/check_feature_map.py`, self-test first. Fails when a file is ADDED or DELETED under `website/src/pages/` or `src/kiro_crew/dashboard/handlers/`, or a `<Route>` entry arrives or leaves `website/src/App.tsx`, while `docs/feature-map/README.md` stays untouched. The blocking root AUTOSDE rule `feature-map-correctness` is the semantic half: it verifies changed rows against the code, rejects unrelated or cosmetic map churn, and checks that the map's net diff matches the PR's stated scope. Structural on purpose: an edit to an existing page changes a feature's behavior, which the map does not describe, so an edit-only diff never fires — a gate demanding a map review on every UI fix produces a map nobody reads. Fails OPEN on an unreadable diff, unlike the other gates here: this one guards a documentation habit, not an invariant a bad line carries into `main` forever |
| `changelog-history` | `scripts/check_changelog_history.py`, self-test first. Fails when a shipped `CHANGELOG.md` section loses lines. Every section already in that file describes software a user has installed, and it has been silently truncated once already — a commit titled "docs: add 0.3.0-insider.9 changelog" REPLACED the file (53 insertions, 322 deletions) and nothing noticed until the Releases page had gone nearly empty |
| `builtin-skill-scope` | `scripts/check_builtin_skill_scope.py`, self-test first. Fails on a marker for THIS repository (its GitHub slug, a `src/` checkout path, a test or workflow file) inside a skill body under `src/kiro_crew/builtin_skills/`, because those install on every machine and resolve for exactly one of them. The `kirocrew-dev/` family is exempt by directory, since this repository is its subject matter |
| `loop-bound-locks` | `scripts/check_loop_bound_locks.py`, self-test first. Fails on any module-global `asyncio.Lock()`/`Event()`/`Queue()` declaration — those bind to the import-time (or first-use) event loop and raise `RuntimeError` when acquired from another loop (Python 3.10+). #4800 converted the tree to `kiro_crew.loop_lock.LoopBoundLock`; whole-tree, since the backlog is zero |
| `testpaths-coverage` | `scripts/check_testpaths_coverage.py`, self-test first. Fails on a `test_*.py` file outside the roots `setup.cfg` pins in `testpaths` — such a file is never collected, so it is green by omission and rots against the code it claims to cover (#6577 found twelve). Whole-tree, since the backlog is zero |
| `harness-parity` | `scripts/check_harness_parity.py`, self-test first. Fails on a newly added line that expresses "this is the Kiro harness" as the absence of another one — a shape that fails toward the permissive answer, so nothing else goes red. Diff-scoped; the whole-tree backlog is a non-failing report |
| `docs-lint` | `scripts/docs_lint.py --test` then `scripts/docs-lint.sh`. Every internal link resolves, every doc is reachable from its directory index, every directory holding docs has one, no code comment cites a doc that does not exist, no doc cites a source LINE past the end of the file it names, no module spec names a source file that exists nowhere, and no doc whose filename is hardcoded in code has been renamed out from under its consumer. Four trees are walked: `docs/`, the packaged `src/kiro_crew/docs/`, `website/docs/`, and the markdown a builtin app ships under `src/kiro_crew/apps/builtins/`. Plus the fact checks below, behind a shrink-only baseline |

Each of these runs its own self-test in the same step, ahead of the real check. A
gate that has silently stopped matching reads as a green signal, which is worse than
no gate, so every rule is exercised against a planted probe first.

### `docs-lint`'s fact checks sit behind a shrink-only baseline

The structural docs checks hold at zero and fail outright. A second family inside
the same gate asks whether a sentence is still TRUE of the code, and that question
has a backlog, so its findings are `(check-id, path, token)` triples matched
against [`.github/docs-lint-baseline.txt`](../../.github/docs-lint-baseline.txt).
A listed triple passes; an unlisted one fails.

| Check | What fails |
|---|---|
| `path-exists` | A backticked repo-anchored source path (`src/**.py`, `scripts/*.py\|.sh`, `website/src/**.ts\|.tsx`, `.github/workflows/*.yml`, `docs/**/*.md`) that names no file. Written from the repo root, so it resolves or the doc is wrong — the suffix index is still a fallback, because a skill's own `scripts/` is one root down. A `path::Symbol` coordinate stays checked, since this repo addresses its own code that way too; only the docs describing a run against another repository are exempt |
| `line-ref` | A `file.py:NNN` citation anywhere in prose. The beyond-EOF check catches the citation that already rotted; this catches the one that rots on the next refactor with nothing going red. Cite a symbol name instead |
| `fenced-path` | A `docs/task-specs/**/*.md` path or a `kirocrew run` argument inside a fenced block that names no file. A fence is a sample everywhere else, but a reader PASTES these two |
| `table-row-merge` | Two index rows glued onto one physical line. Both links resolve, so every link-graph check stays green while the table renders one row short and a file loses its entry |
| `code-coupled-completeness` | A packaged doc named in a string literal under `website/src` and absent from `CODE_COUPLED_DOCS`. An unrecorded coupling can be renamed apart silently |
| `dead-identifier` | A backticked identifier absent from every first-party code tree. **Report-only** unless `--strict-identifiers`, because the class mixes real rot with names the repo cannot adjudicate |

Three checks skip a doc whose genre names things that do not exist yet
(`docs/request-for-change/`, which carries the plans, and `docs/task-specs/`):
`path-exists`, `fenced-path` and `dead-identifier`. A proposal
names a file or a symbol precisely BECAUSE it is not there yet. `fenced-path` also
skips the packaged user docs under `src/kiro_crew/docs/`, where a task-spec path is
a template for the reader's own project rather than a file in this checkout.

The builtin app tree is the mirror image of that exemption: it keeps every fact
check and every link check, and drops only the two CURATION rules, reachability and
the per-directory index. A `SKILL.md` is a skill definition an agent loads verbatim,
so a rotted path in one misroutes the agent rather than a human reader, but an index
file in `skills/<name>/` would be a file the app never loads. `UNCURATED_PREFIXES` in
`scripts/docs_lint.py` is where that line is drawn, alongside the archives.

`python3 scripts/docs_lint.py --update-baseline` prunes the list, and it is
prune-only by construction: it intersects the recorded triples with the ones firing
now, so it cannot record one, and it refuses to run when the file is missing —
read as an empty set, one `rm` plus one refresh would accept every current
violation forever. Adding is the separate `--accept-new`, which prints every triple
it records so each exemption lands in a diff a reviewer reads. That is the same
posture `check_black_formatting.py` takes.

A triple that no longer fires is **reported, not fatal**. That is a concession to
several changes consolidating the doc trees at once, so an entry graduates in a file
the current change never touched; it is not a claim that a triple is fragile, since
the recorded identity omits the line number and a reflow keeps it.

## `ci.yml`: correctness

Every job here is blocking. Every job that costs real runner time also `needs:`
`await-fast-gate`, so on a red gate it does not run at all.

| Job | What it enforces |
|---|---|
| `changes` | "Detect changed surface". Resolves the path filters every other job reads, so a diff that cannot affect a surface does not pay for it |
| `await-fast-gate` | Polls the `Fast Gate` run for this exact head commit and **fails closed** in all three ways it can go wrong: a run that never appears (180s budget), one that never completes (720s budget), and one that completes non-success. A barrier that passed when it could not read its subject would be worse than none, because the matrix would run anyway and the log would claim it was cleared to. One extra ~1-minute job buys the whole matrix the right to not start |
| `backend-lint` | `isort --check-only`, `flake8`, `mypy` on Python 3.12, plus `scripts/check_black_formatting.py` — black enforced on every file outside `.github/black-baseline.txt`, which can only shrink — and `scripts/check_subprocess_encoding.py` (self-test first) — no text-mode subprocess call without an explicit `encoding=`, `**UTF8_TEXT`, or a `# subprocess-encoding: locale` marker, outside `.github/subprocess-encoding-baseline.txt`, which can only shrink — and `scripts/check_sync_io_in_async.py` (self-test first) — no blocking db / subprocess / http / `time.sleep` call inside an `async def` under `src/`, outside `.github/sync-io-in-async-baseline.txt`, which can only shrink. A stall past `dashboard.loop_stall_exit_after_secs` (25s) makes the watchdog kill the gateway and drop every in-flight turn (#3057, #1572); the escape is an offload (`await asyncio.to_thread(...)`, or a named lane from `src/kiro_crew/executors.py`) or a `# on-loop-io-ok: <why it cannot block>` marker whose reason is mandatory. All four baselined gates in this job read their diff scope from the one shared resolver in `scripts/ratchet_scope.py`, so they cannot disagree about which lines a change added; the env-base gates (`check_brand_name.py`, `check_harness_parity.py`, `check_focus_cue.py`) share the same diff parsing through its explicit-base entry points while keeping their `*_BASE_REF` base semantics |
| `backend-test` | 4 duration-balanced pytest-split shards on Python 3.12, `-n auto` within each |
| `backend-test-windows` | windows-latest, 4 shards, `--no-cov`, 180s per-test timeout. The backend supports Windows natively via `platform_compat`, and nothing else in CI holds that line |
| `backend-test-macos` | macos-14, deliberately SCOPED (gateway, socketsec, platform-compat, pod and MCP-apps suites via a glob). A full macOS run needs its own exclusion burn-down first, and a job that is red on arrival trains people to ignore it |
| `backend-test-sandbox` | The one job that clears the AppArmor userns restriction, so the tests guarded by `skipif(not userns_available())` EXECUTE instead of skipping. Runs all eleven sandbox-dependent suites. The shards collect the same files — nothing is deselected — but there the sandbox-guarded tests skip, so this is the only lane where those 85 assertions (the `~/.kiro/crew` keystone among them) actually execute |
| `coverage-combine` then `coverage-gate` | Combines the 3.12 shard data, then enforces the project line-rate floors, plus a per-file floor with a shrink-only baseline (all floors live in the job's `env:` block) |
| `frontend-lint` | `tsc -b`, `eslint` under a hard-zero warning ceiling, `jscpd`, and `npm run i18n:check` |
| `electron-test` | The Electron shell's own node:test suite (`website/electron`) |
| `frontend-test` | `vitest run --coverage` |
| `frontend-coverage-merge` | Merges the frontend coverage shards so the gate reads one report |
| `cfn-lint` | Lints the artifact-deploy templates with a pinned `cfn-lint` |
| `linux-packaging` | "Linux Packaging (build + smoke-install)". Builds all three Linux desktop formats from one backend tree through `packaging/build-desktop.sh`, then installs them in their target distros with `scripts/smoke-linux-packages.sh`. Path-filtered on the packaging surface |
| `lockfile-engines-floor` | "Lockfile Installs On Declared Node Floor". Runs a real `npm ci` in `website/` on the LOWEST Node version `engines.node` declares, so a lockfile that only resolves under the newer npm major cannot land. The version is a literal pinned to that floor by `test_the_engines_floor_job_pins_the_declared_floor` rather than a range, because resolving a range picks the newest match and makes the job vacuous |
| `bundle-size` | "Bundle Size Gate". Builds the frontend with `--mode analyze` (which is the only build that emits `dist/bundle-report.json`) and enforces per-chunk ceilings from `website/scripts/check-bundle-size.mjs`, with a 500 KB default for any chunk not named there. Skipped on a backend-only diff, which cannot change the bundle |
| `e2e` | The i18n render-time gate, then `python setup.py test_e2e` |

Details worth knowing:

- **The macOS peer-identity canary is asserted by name.** `pytest -q` does not name
  passing tests and a skip exits 0, so a canary that quietly stopped running (a
  changed `skipif`, a collection change) would leave the job green while the gate
  it proves went unverified. The step runs that one node id with `-v` and greps for
  `1 passed`.
- **`backend-test-sandbox` fails loudly rather than skipping.** It clears
  `kernel.apparmor_restrict_unprivileged_userns`, then runs `unshare --mount
  --map-root-user true` as a probe. If the runner image ever stops allowing the
  namespace, the job fails instead of letting the suite silently skip and the gate
  go green having asserted nothing. This is what gives the `hooks.py`
  sensitive-path keystone real CI coverage.
- **`coverage-gate` is fail-closed, and the split made that load-bearing.** It runs
  `if: always()` and its first step converts any non-success upstream result into an
  explicit failure, because GitHub treats a **skipped** required check as satisfied.
  That was already the right shape when the only way to skip a test job was a path
  filter or a failed dependency. It is now the mechanism that keeps the whole
  `await-fast-gate` design honest: a red gate deliberately SKIPS `backend-test` and
  `frontend-test`, and without this step a required Coverage Gate would skip with
  them and be reported as satisfied — so the barrier that exists to save runner time
  would also have quietly removed the coverage floor. The `if: always()` is what
  makes it emit a real verdict, and the first step is what makes that verdict red.
  `frontend-coverage-merge` carries the other half of the same problem and solves it
  the opposite way: its `!cancelled()` needed an explicit
  `needs.frontend-test.result != 'skipped'` clause, because a skipped shard set has
  nothing to stitch and the merge would otherwise go red for missing an artifact
  instead of for the gate the developer actually has to fix. A FAILED shard set still
  has something to stitch, which is why the clause names `skipped` and not both. It
  also compares the raw line-rate and rounds only for display, so 89.95% cannot pass
  a 90% floor.
- **`coverage-gate` enforces two different shapes.** The project floors
  (`BACKEND_MIN`, `FRONTEND_MIN`) compare one lane-wide average; the per-file floor
  (`PER_FILE_MIN`, `scripts/check_per_file_coverage.py`) requires *every measured
  file* to clear it. Both are needed because an average is satisfiable without
  touching the files that carry the risk — a well-covered large file pays for a
  bare small one. The per-file gate exempts only the files listed in
  `.github/coverage-baselines/{backend,frontend}.txt`, and that list may only
  shrink: an unlisted file below the floor fails, a listed file that slides further
  fails, and a listed file that *clears* the floor by the same noise band fails
  until it is removed. Refresh with `--update-baseline`, which **prunes only** —
  it cannot add a path or rewrite a recorded rate, so neither a new offender nor a
  regression can be cleared by refreshing instead of by adding tests; seeding a
  new lane is a separate `--seed-baseline`. The floor's rationale and measured
  cost live in the script's docstring, not here, so they cannot go stale in two
  places. Per-file enforcement is skipped for a lane whose suite ran as a
  coverage-free subset, because subset rates are not comparable to a baseline
  recorded on the full suite.
- **`eslint src/ --max-warnings 0` is a hard ceiling, not a stored baseline.**
  The tree carries no warnings, so any warning a change introduces fails this
  job. Never lift the ceiling to admit one: a ceiling above the measured count is
  a budget new warnings land inside without anyone seeing them, and a warning
  admitted that way is indistinguishable from the rest. Fix it, or suppress that
  one line with `// eslint-disable-next-line <rule> -- <why the code is correct>`,
  which is reviewable in the diff where a lifted ceiling is not.
  `test_eslint_warning_ceiling.py` pins the zero and pins that `ci.yml` declares
  exactly one ceiling, so it cannot be lifted quietly — and because the value is
  fixed rather than measured, naming it here cannot go stale.
- **The i18n gates split into three tiers,** and only two can fail: diff-scoped
  zero-tolerance checks (a user-visible literal on a line this branch wrote, a
  file holding more than it did at the base, new English key shape, changed catalog
  values) and whole-repo hard zeros (a `t()` naming a key that does not exist,
  plural concatenation, a stale pseudolocale). Everything else is report-only,
  because a stored whole-repo total is written by whichever branch measured it last,
  so another branch can push it past its number without touching your files and the
  failure then names no diff anyone can fix. Full rules:
  [i18n-gates.md](i18n-gates.md).
- **Every gate that needs a base ref fails rather than skipping when it cannot
  resolve one.** `actions/checkout` fetches depth 1, so
  `.github/scripts/resolve-i18n-base.sh` fetches the one commit and exits non-zero
  if it cannot; a gate that cannot run must fail, not pass.
- **`I18N_BASE_REF` is `pull_request.base.sha`, not `origin/main`.** The base tip is
  a moving target measured at step time while the checked-out tree is a snapshot
  from job start, so anything landing on `main` in between would appear only on the
  base side and be charged to every PR in that window.
- **The e2e gateway boots with `KIROCREW_STRICT_ON_LOOP_PERSIST=1`**, so an
  un-offloaded session-JSONL mutator that enters the lock on the event loop raises
  and fails the gate at PR time. `KIROCREW_E2E_REQUIRE=1` turns an
  environment-resolution miss into a hard failure, since a skipped suite would
  otherwise count as a pass having run zero browser specs. Details:
  [e2e-gate.md](e2e-gate.md).

## `build.yml`: the artifacts still build

PR-time proof only, no publishing.

- **`build-wheel`** builds the frontend, stages it into the package, builds the
  wheel, then `pip install dist/*.whl` and `kirocrew --version` as a smoke test.
- **`build-desktop`** builds the Electron app unsigned on macos-15 and
  ubuntu-22.04 via `make desktop`, and uploads the artifacts.

**Neither desktop lane ever RUNS the bundled backend.** `build-desktop` here and
`build-desktop.yml` in the release lane both build the real `kirocrew-backend`
tree via `packaging/build-desktop.sh` — which provisions a
python-build-standalone interpreter and pip-installs the project into it — and
then only upload the artifact. The wheel lane at least runs `kirocrew --version`.
So a packaging change that breaks the packaged app (a layout change, a launcher
rename, a dependency that fails to install into the bundled interpreter) passes
every gate: the tests that cover packaged-app behavior monkeypatch `sys.frozen`
and `sys.executable`, so they stay green against a simulated environment. The
cheap fix is to run the already-built launcher once in `build-desktop`, the
packaged analogue of the wheel lane's `--version`.

## `code-review.yml`: the deterministic pre-gate

No model, no secrets, so it is safe on forks and always runs. It is the grep-half
of the AUTOSDE rules; the semantic half is delegated to the line reviewers.

- **`autosde-rules`** blocks unambiguous frontend violations on added lines: an
  inline `<svg viewBox>` outside brand-mark components (`KiroGhost.tsx`, `*Logo.tsx`,
  `*Ghost.tsx`), a `<div>`/`<span>` with `onClick` and no `role`, `.innerHTML =`,
  Mermaid `securityLevel: 'loose'`, and an oversized `max-w-[>=900px]` page wrapper.
  It also blocks three backend keystones: a sensitive credential or keystone path
  read that does not go through `is_sensitive_path()`, `denied_commands.json`
  dropping off `security._SENSITIVE_HOME_DIRS` or the governance boot-integrity
  tuple, and a bare `bool()` on an operator-editable boolean opt-out field
  (`bool("false")` is truthy, which would silently disable every protection).
  Advisory warnings, which never fail: unsanitized `dangerouslySetInnerHTML`,
  hardcoded Tailwind colors, new CSS `@keyframes`, sub-10px text.
- **`inclusive-language`** runs a SHA-pinned `woke` (`WOKE_VERSION`, fetched through `get-woke`) over added lines only, failing on `(error)` severity findings; grepping the terms in `.woke.yml` is NOT equivalent to the gate, and an intentional term is exempted with `# wokeignore:rule=<term>` **on the offending line itself** — `woke` matches per line, so a marker on its own line exempts nothing and leaves the gate red (see the markers beside `master_fd` in `dashboard/handlers/terminal.py`). <!-- wokeignore:rule=master --> Legacy violations are burned down separately; this stops
  new ones.
- **`sast`** runs Semgrep in a pinned container: first `semgrep --test` over the
  custom rules in `semgrep/` against the annotated fixtures in `semgrep-tests/`
  (both directions — a `ruleid:` line must match, an `ok:` line must not — so a
  rule regression goes red here, not on a later unrelated PR; the rules dir is
  non-hidden because semgrep 1.78's test mode cannot discover tests under a
  hidden directory), then the scan itself, diff-only against the base,
  community packs plus `semgrep/`, with `--error`. The fixtures are listed in
  `.semgrepignore` so the deliberately vulnerable fixture code is never read by
  the scan. Blocking.
- The production dependency audit (`dependency-vulnerability.yml`, which runs
  `scripts/check_npm_audit.py` over every lockfile-backed Node project and fails
  closed on **high or critical production** vulnerabilities) is **not** a PR
  job. It reaches the npm registry, whose slow hours made it the one red X on
  otherwise-green PRs and then failed nightlies for hours at a stretch. It runs
  where a vulnerable dependency would actually ship: before every release build,
  and — since main carries no dependency gate of its own — before every nightly
  **publish**. On the nightly it gates the publish jobs only, never the builds,
  so a slow registry delays publication of an already-built nightly instead of
  failing the build. Time-boxed exceptions live in
  `.vulnerability-exceptions.json`, and a registry stall or connection fault is
  retried inside one shared time budget before it fails (see the
  transient-failure contract in the security spec).
- **`pr-hygiene`** enforces a Conventional-Commits PR title (it becomes the
  squash-merge message) and at most two commits (`git rev-list --count <= 2`).
  One commit stays the norm; the second is there so a mechanical follow-up (a
  regenerated artifact, a formatting sweep) can stay separable from the change
  it accompanies. Both blocking.

Separately, **`dependency-review.yml`** fails a PR that adds or changes a
dependency whose license is off the curated allowlist in
`.github/dependency-review-config.yml`. A maintainer can bypass it for the commit
they reviewed with the `license-override` label, honored **only** on the `labeled`
event, so a later push arrives as `synchronize` and re-runs the gate; a new,
unvetted dependency cannot ride in on a stale override.

**`docker-smoke.yml`** is paths-filtered to the container surface (`docker/**` plus
the three source files the container contract spans: the bind override in
`dashboard/origin.py`, the probe Host-barrier exemption in `dashboard/server.py`,
and the liveness payload in `dashboard/handlers/core.py`). It builds the image from
a locally-built wheel and proves, across a real container boundary, that
`KIROCREW_BIND=0.0.0.0` makes the gateway reachable from the host, that token auth
still guards the API on that non-loopback path, that `/api/health` works (the image
HEALTHCHECK depends on it), that kiro-cli runs inside the image, and that channel
credentials passed as container env are moved into the data home's `.env` and
scrubbed from every long-lived process environ.

## The AI review ladder

Five reviewers, each with a distinct question and a distinct trust posture. The
design axis is **what each is allowed to read** (its prompt-injection surface) and
**whether it can block**.

| Reviewer | Check name | Harness | Reads | Question | Blocks? |
|---|---|---|---|---|---|
| Opus 4.8 | `Opus 4.8 Review` | Agentic, `--max-turns 120` per stage, **two real invocations** (discovery -> validation) | **Code only**: `Read`, `Grep`, `Glob`, `Bash(gh pr diff:*)` | Line-level correctness, security, AUTOSDE | Yes, fail-closed |
| GPT 5.6 | `GPT 5.6 Review` | Non-agentic, **two** invocations (discovery, then authoritative falsification), `reasoning_effort: medium` | Code plus PR title and body as nonce-wrapped **UNTRUSTED** context | Line-level second perspective, plus description-versus-diff consistency (advisory) | Yes, fail-closed |
| Design Review | `Design Review` | Agentic Fable 5, with an Opus fallback model | Code plus `gh pr view` (it must judge intent) | Should we build this, and is it the right *shape*? | Advisory; red only on a genuine `BLOCK` |
| UX Review | `UX Review` | Agentic Fable 5, with the same fallback; **two real invocations** on same-repo PRs (blind read -> reconcile) | Pass 1: the committed screenshot PNGs **only**; pass 2: code, PR text, and pass 1's report | Can a first-time user who has read nothing tell what each new element is and does, and do state changes stay one continuous element? | Advisory; red only on a genuine `BLOCK` |
| First Principles | `First Principles Review` | Agentic Fable 5, same fallback, `--max-turns 120` (inventorying and counting is grep-heavy) | Code, the whole repository, and `gh pr view` | What is the author trying to do, and does each thing this ships *deserve to exist*, already exist, or only patch a symptom? | Advisory; red only on a genuine `BLOCK` |

### Why a first-principles lane is not a second Design Review

Design Review takes the PR's **stated problem as its frame** and judges the shape of
the solution. Two blind spots survive that. The first is **plurality**: a change
with one stated purpose routinely ships several observable differences — a control
that moved, a relabelled button, a flipped default, a new knob, a retry — and only
the one named in the description gets examined. The second is **depth**: a fix aimed
at the symptom the author happened to trip over passes every lane, because each line
is correct, the shape fits and the surface renders.

So this lane is defined by a method rather than a topic. It states the author's
**intent** in one sentence and whether the change is a fix or an addition, then
**inventories** it into the **observable differences** it ships — written the way a
person would notice them, not the way the code expresses them — and runs every
remaining question **per item**:

A new capability is only one of the kinds that count. A **move, reorder or regroup**
is its own item, and it is the kind that goes unexamined most often precisely because
nothing became newly possible, so nothing reads as "added". The same applies to a
rename, a changed default, an added or removed confirmation, a change in what is
visible by default, and a change in when something happens. If the change is a *fix*,
every item that is not the fix is called out as **riding along**.

A move also carries a **higher** bar than an addition, not a lower one: the capability
already existed, so the only harm available is that people could not find it, and the
review must name who was failing and how that is known. "It groups better" is analogy,
and it does not outweigh the relearning cost every existing user pays.

- **Does it deserve to exist?** The zero option (what observably breaks if this item
  ships nothing), the delete option (could the same harm be removed by deleting code
  or a concept instead of adding one), and provenance — is the requirement *derived*
  from a constraint you can point at, or *inherited* from convention, symmetry, "for
  flexibility"? Reasoning by analogy is named and rejected explicitly, because
  analogy is how an unnecessary feature enters a codebase looking reasonable.
- **Does it already exist?** A grep for the mechanism that already does this job. A
  second spelling of one capability is a finding even when no code is duplicated,
  because both spellings must then be maintained and will diverge.
- **Does it fix the cause?** Each item is placed on a named chain — **symptom**
  (patched where it was observed), **mechanism** (the code that produced it), or
  **cause** (the decision or invariant gap that let it misbehave). Symptom-level
  with a reachable in-scope cause is a finding. Generality is then decided by
  *counting* unfixed sibling instances of the same cause, so "this is a point patch"
  has to come with paths.

Three constraints keep it honest:

- **One contract, read from the base ref.** The lenses live in
  `.github/review-prompts/first-principles.md`, and both lanes `git show` it from the
  PR's **base** commit — the same mechanism the Opus lanes use for their two prompts.
  That removes the second copy entirely, and it means a pull request cannot edit the
  reviewer that judges it. A contract *absent* from the base is not an error — it is
  what happens on the pull request that introduces or moves the contract, so the lane
  reports a non-blocking "no contract on the base commit" and produces no verdict. It
  never falls back to the head's copy, because a rename would then let a change hand
  the reviewer its own rubric.
- **Count before you claim.** Every duplication, consumer-count and unfixed-sibling
  finding must state the count and the pattern grepped; an uncounted claim is a
  fabrication and must be dropped. This is what stops the lane drifting into taste.
- **Every suggestion is a subtraction.** It may propose only deletions, shrinks,
  deferrals, or "use the thing that already exists" — it may not even ask for a doc
  or an RFC. A reviewer allowed to propose additions becomes a source of the exact
  surface it exists to remove.
- **The inventory is printed, even on a PASS.** A `PASS` here is a claim about *every*
  item, so the item list is the evidence a human needs to check that claim. This is
  a deliberate divergence from the sibling lanes, whose clean verdict collapses to
  one line.

It runs whenever a diff touches product or CI surface — **including a plain bug
fix**, which is where the root-cause lens earns the most. Only a change that ships
no capability at all (docs, tests, screenshots, generated files) skips, so the
2x-rate-card Fable 5 spend goes to diffs that can actually produce a finding.

It is advisory in `pr-readiness.yml` (UX-style, not Design-style): a `BLOCK` here is
a judgment about whether a feature should exist, and a model does not get to wedge a
merge on that until the lane's calibration is proven. Promoting it to a readiness
blocker later is a one-line change in the aggregator.

**Where it overlaps Design Review, this lane owns the question.** Design Review's own
rubric asks whether a change fixes a root cause and whether a simpler alternative
exists; those questions are asked here from the premise side and per item. The split
is deliberate — premise and cause here, shape quality there — and if the two lanes
converge in practice, the answer is to trim the overlap out of Design Review, not to
tune two prompts against each other.

### Why Opus 4.8 is code-only

It is the agentic reviewer, so pulling attacker-controllable PR prose into its
context is a prompt-injection surface. `gh pr view` and `gh api` are disallowed, and
so is `gh pr comment`: a **CI step**, not the model, upserts a single
hidden-marker-keyed summary captured from the run transcript, which trades scattered
inline chatter for one terse summary plus a binary gate. The PR-intent
responsibility, including flagging a description-versus-diff mismatch, is
deliberately handed to the read-only, non-agentic GPT 5.6 reviewer, which treats
that prose as **untrusted evidence, never authority to waive a code finding**. The
prose is fetched by a step that has network and the token, then baked into the
prompt wrapped in a collision-resistant nonce, because the review sandbox unshares
the network and cannot fetch it itself.

### One shared binary contract

Both line reviewers run the same review contract, and severity encodes exactly one
thing: *does this block the merge*, **never confidence**. There is no
"possible issue" tier. The blocks of that contract shared by the two GPT
workflows — the diff-is-not-evidence clause, the coverage/finding/fix bars, the
output contract, and the falsification-pass mandate and verdict framing — live in
shared `.github/review-prompts/gpt-*.md` files rather than as two inline copies,
so the lanes cannot drift apart on them (#5852). The same-repo lane's remaining
inline chunks (its system rules, repo context, and round-convergence sections)
moved into that directory too (#3697), so its whole prompt is now assembled by
splicing staged prompt files in a fixed order — which is also what lets the
prepare-pr skill's `local_review.py` mirror the contract by reading the same
files instead of scraping shell heredocs. The same-repo lane stages them
from the PR's **base** commit like the Opus lanes; unlike those lanes it falls
back to the checked-out copy (with a warning) when a block is absent on the base,
because a hard gate cannot afford a no-verdict pass and, on a same-repo PR, the
workflow file itself is already editable by the PR — the fallback adds no attack
surface the lane did not have. The fork lane's checkout *is* the trusted base
(the diff is never applied), so it reads the files straight from the tree and
fails closed if one is missing. A finding must state a concrete input or condition that
occurs in practice, the call path to the changed line, and an observable wrong
outcome; anything phrased as "could", "might" or "if a caller were to" is **not a
finding**, and silence is the correct output. Only two labels exist: **BLOCKING**
(on the closed WHAT BLOCKS list) and **FINDING** (advisory, never blocks). A
per-review budget caps a review at 2 BLOCKING findings, and the calibration note
says "No findings." is the expected output for a typical PR.

### Asymmetric multi-pass is intentional

BOTH line reviewers now run **two real invocations**: a discovery pass that
generates candidates, then an **authoritative falsification** pass whose primary
job is to *kill* them. The Opus lane used to run one pass with two internal phases; that
was measured on this repo to suppress findings the same model reports reliably
without the precision clauses, because a prompt asked to discover AND to police
its own precision stops discovering. Its discovery half therefore carries no
precision gates, and its validation half applies a confidence floor and the closed
blocking list. A
candidate survives only if pass 2 re-derived the input, the call path and the
observable outcome itself from code it opened in that pass. Pass 2 may also *add* a
defect discovery missed, in both lanes, but only under that same three-part
grounding and the same confidence floor — killing a candidate stays its primary
job, and a self-found finding gets no second opinion, so it earns no cheaper path
in. In both lanes such a finding is tagged `(origin: validation)` in the posted
review, because it is un-falsified by construction: the tag is what lets a reader
weight it accordingly, and what lets the precision of self-added findings be
compared against survivors' rather than assumed equal. Pass 2 is the only
gated verdict. Falsification raises precision *within a single run*, which is why
neither reviewer carries cross-round state: each judges only the current SHA's code
and therefore cannot contradict itself across rounds.

### Verdicts are structured markers

The markers are the **only** gate:

- Opus 4.8 emits `[OPUS-REVIEWED] <sha>` always, and `[BLOCK-MERGE] <sha>` only when a
  blocking finding exists. Both are parsed out of the action's `execution_file`
  transcript rather than a `--json-schema` structured output, because the harness's
  internal structured-output tool is unreliable when other tools are enabled:
  reviews completed with a success result yet returned no structured output,
  failing this gate closed on healthy reviews.
- GPT 5.6 emits `[GPT-REVIEWED] <sha>` / `[BLOCK-MERGE] <sha>`. When the provider
  *refuses* the request — declines to review the diff because of what it contains,
  as opposed to crashing or timing out — the **same-repo lane** publishes a distinct
  terminal state: the synthetic verdict body names the refusal in prose (no
  reviewed marker, so the gate still fails closed), and the classification rides
  the verdict assembly's `refused` step output. There is deliberately no refusal
  marker in the body — the body on the clean path is model prose, and a marker
  would invite prose-grepping, so a review that merely quotes the refusal wording
  cannot be reclassified. The gate's message names human adjudication
  (`/ai-review override`) instead of advising a re-run: the refusal is caused by
  the reviewed content and is empirically sticky — 12 consecutive identical
  refusals across 10 heads were measured on one PR — so a re-run is not a workable
  remedy. A failed pass is classified as refused only when the provider's own
  error line appears line-anchored in the tail of that pass's captured stream,
  because the stream also carries PR-controlled text (the prompt embeds the PR
  title/body, and the reviewer echoes the diff). Without the distinction, every
  security fix whose evidence is a working exploit read as a permanently
  re-runnable crash (#8685). The fork GPT lane (`fork-gpt-review.yml`) still
  reports only the generic incomplete state and is tracked separately.
- Design and UX emit `Design-Verdict:` / `UX-Verdict: PASS | CONCERNS | BLOCK`,
  parsed from a header line.

A missing reviewed-marker for the current head fails the gate closed, because a
no-output review must not look clean. A BLOCKING-labelled finding without the
`[BLOCK-MERGE]` marker is only a non-gating **advisory warning**, since a coherence
check on that pairing mis-fires whenever the model quotes prior text.

A lane's summary comment is **one slot shared by every run on the PR**, and it
is upserted in place. The comments API has no `If-Match`, so a write to that
slot is last-writer-wins, and it exposes no edit history, so the loss is
undetectable afterwards: a failed run's "review incomplete" body once replaced
a posted verdict and a `[BLOCK-MERGE]` finding vanished from every surface a
reader or tool checks (#8292).

Eight of the ten lanes that upsert a verdict comment now let exactly one kind of
run claim that slot — a **completed verdict for the PR's current head**. The two
other kinds each lose a live verdict, so each leaves an existing comment
untouched (#8344):

- **No `"<stamp> <head>"` proof marker** — a review failure. Preserving the
  verdict and prepending a staleness notice is *not* a safe alternative: it
  reads the body and writes a merge of it back, so a verdict published between
  the read and the write is restored away.
- **A completed verdict for a superseded head** — the same loss arriving late.
  An older run can finish after a newer one published, because the fork lanes'
  `concurrency` group is keyed per head so they are not cancelled, and a
  cancelled same-repo run still executes its `if: always()` posting step. The
  step reads the PR's head and stands down when it is not the head it
  reviewed.
- **A head that cannot be read.** Writing is the destructive half of the
  guard, so it does not proceed on an unknown: a head unreadable after three
  attempts is *not confirmed current* and the slot is left alone. The read
  retries first — with the same bounded backoff this lane's other gating reads
  use — because one blip is not evidence about the PR, and an API broken
  enough to fail all three would fail the write too.

Whether a comment exists is the other gating input, so that read retries on the
same bounded backoff. A lookup still erroring afterwards counts as "a comment
may exist", not as "none does", so a withheld run posts nothing rather than
plant a second marker comment over a possibly-live verdict. When the lookup
succeeded and found nothing there is no verdict to lose, so the body is posted
as a new comment. Nothing is lost by standing down: the comment names the head
it reviewed, each head's own check-run is finalized fail-closed, and
`pr_status.py` matches reviewer stamps against the current head, so a comment
left in place for an older head reads as *stale* and never as an approval of
this one.

Human overrides and skip notices are current-head determinations rather than
review failures, so their upsert sites keep replacing the comment
unconditionally, and a completed verdict for a **confirmed** current head whose
*comment* lookup failed still CREATEs rather than stay silent (a duplicate
comment is recoverable, an unposted verdict is not). The eight guarded lanes
define the guard as a `guarded_comment_upsert` bash function that
`test_ai_review_workflows.py` pins byte-identical across every lane, so the
invariant cannot drift lane by lane.

Two lanes stay outside that function, and both exclusions are deliberate:

- **`codex-review.yml`** carries #8342's own inline preserve-and-prepend shape,
  pinned byte-for-byte by its own tests — it is the one site left with a
  read-modify-write window on the slot.
- **`claude-review.yml`** keeps a plain lookup-then-PATCH. Its incomplete path
  posts **nothing at all**, so the #8292 class — a failure notice burying a
  verdict — cannot reach it. What remains is only the superseded-completed
  window: its `concurrency` group cancels an older run per PR, but the posting
  step is `if: always()`, which a cancelled run still executes, so an older run
  holding a completed verdict can still claim the slot.

### Security posture of the reviewer jobs

- Explicit fork guards (`head.repo.full_name == github.repository`) on **every
  step**, so on a fork the job starts and then does nothing rather than failing an
  unsatisfiable credential step. The guard is per-step and not job-level because
  GitHub never evaluates a **skipped** job's `name:` -- while it was job-level,
  every fork PR published the raw name expression as its check name. Fork coverage
  still comes from the separate `fork-*` pipeline below.
- **The job name is conditional on the head repository**, so a fork PR gets
  `<check> (same-repo lane, not applicable to forks)` instead of the protected
  name. Same-repo PRs keep the exact protected name. Without this, both lanes
  publish one name and GitHub resolves a required status check to the **newest**
  check-run of that name: a `pull_request` event firing after the fork lane
  posted its verdict (a reopen, or an `edited` title/body on `codex-review.yml`)
  would make the same-repo lane's own run the newest one and satisfy the
  gate on a review that never ran. `pr-readiness.yml` was never fooled by this
  -- it collapses every check-run of the name and treats "no completed run" as
  pending -- so the rename closes the branch-protection half of the gate.
- `persist-credentials: false` on checkout, so `actions/checkout` never writes the
  token into `.git/config` where a reviewer reading untrusted PR content could find
  it.
- AUTOSDE rules are extracted from the **base** commit, not the PR head, so a PR
  cannot weaken the rules that govern it.
- Bedrock credentials are assumed late, after dependency installation, so a
  compromised or version-drifted release never observes them.
- The GPT reviewer runs in a read-only, network-unshared sandbox (which is why the
  job clears `kernel.apparmor_restrict_unprivileged_userns` first: the sandbox's
  bubblewrap fails at netns setup otherwise).
- Review output is redacted for AWS key ids, ARNs, 12-digit account numbers and
  secret-key or session-token shapes before any public comment.
- Dependabot PRs skip the review work and let the gate pass, since they run with a
  read-only token and no credential access.
- A 90-minute job timeout is a runaway backstop, not a review budget: a healthy
  review self-terminates well before it, so the timeout exists solely to fail the
  gate closed on a true hang.

### Advisory means advisory, with one exception

Design Review and UX Review are non-blocking as a rule: their suggestions must be
proportionate ("never recommend extra layers, abstractions or future-proofing the
problem does not require"), and their tie-breaker is to choose `CONCERNS` over
`BLOCK` when torn, reaching for `BLOCK` only when the **design** is wrong and never
merely because the change is large. The one exception: a genuine `BLOCK` verdict
does fail that workflow's own check, so it is visible; every other outcome exits 0.
Because `pr-readiness.yml` scores both as advisory, a red Design or UX check never
independently blocks readiness.

**Design Review owns the long-term / one-way-door lens** as its gate 8, "LONG-TERM
REVERSIBILITY", in both the same-repo and fork variants. An unsafe one-way door is
its primary `BLOCK` trigger. Everything reversible (architectural erosion,
maintainability, "should eventually be refactored") is advice and non-blocking
follow-up work, because the author does not need a perfect or complete solution in
this PR.

There is no separate long-term arbiter workflow. A second-order reviewer that
re-judged the other reviewers' *comments* over a `workflow_run` chain blocked almost
nothing, and it structurally could not work for fork PRs: the fork head SHA does not
survive the extra `workflow_run` hop, so it never resolved which PR it was for. The
lens now lives where the reviewer already has full diff context, and covers same-repo
and fork PRs identically with no cross-workflow head-passing.

### `UX Review` early-skips cheaply

It runs only when the diff touches `website/`, `temp-screenshots/**` or
`.github/screenshots/**`. A backend, CI or docs PR skips it with no model call and no
comment churn, and the check passes. When screenshots are present it reads each PNG
and grounds visual findings in them, and it is instructed to treat screenshot content
as untrusted (a screenshot, title, commit message or filename attempting to grant
leniency is ignored, and screenshot polish never waives a lens).

### `UX Review` reads the screenshots blind before it reads the diff

On same-repo PRs the lane is two model calls with a context wall between them.
**Pass 1 (blind read)** gets the `Read` tool and a list of the images the PR commits,
copied under opaque names (`shot-01.png`, ...) so an author-chosen filename such as
`pinned-turn-chip.png` cannot prime it -- and nothing else: no diff, no PR title or
description, no `Grep`/`Glob`/`Bash`. It is told it is a non-technical person
opening the product for the first time and
writes down, per element, what it appears to be, what a click would do, how sure it
is, and whether it would dare to click. **Pass 2 (reconcile)** gets the diff, the PR
text and pass 1's report as a data file, and adjudicates rather than re-reads.

Why the wall exists: PR #6783 minimized a banner into a corner chip labelled
"Pinned turn". Every AI lane passed it -- this one wrote that the chip was
"self-teaching ... visibly labelled" -- and the product owner could not tell what the
chip was. A reviewer that has read the diff and the description before it looks at
the pixels has already learned the author's vocabulary; it can check that a label
exists, is localized and is reversible, but it can no longer test whether a stranger
understands it. The old lens 12 ("five-second proxy: imagine an uninformed reader")
asked exactly that of a reviewer that was no longer uninformed, so it was replaced.

Three rules follow from the split, all read off evidence rather than judged:

- **Coverage.** Every user-visible control the diff adds or changes must appear in a
  committed screenshot. One that does not is an *evidence gap*, listed under
  `### Evidence gaps`, and the verdict cannot be `PASS`. A diff that adds or changes
  no user-visible control has no gaps and needs no screenshot.
- **Primary controls.** A control on the change's main path that the blind reader
  misread (named a different thing or outcome than the diff implements), could not
  identify, or would not dare to click is a `BLOCK`, quoting the reader's words. A
  correct reading the reader rated only "a guess", secondary misreads, and
  vocabulary collisions ("Pinned turn" next to the existing "Pinned messages") are
  `CONCERNS`.
- **State-transition continuity (lens 13).** When a user action or state flip
  minimizes, collapses, relocates or replaces a *persistent* element the user has
  already identified, the change must animate *one* element between the two states
  (shared layout, a landing spot the eye can follow, continuous text, restore as the
  reverse), respecting `prefers-reduced-motion`. A hard swap in the diff
  (`flag ? <Chip/> : <Card/>`, an unmount/mount with no shared-element transition)
  with no stated reason is a `BLOCK`. Async lifecycle states (loading, empty, error
  -> content) are not in scope. The convention is also stated in `website/AGENTS.md`
  so authors meet it before the check does. Static screenshots cannot show
  continuity, so this class of change needs a recording (a committed or PR-body
  `.gif`/`.mp4`/`.webm`); none is an evidence gap. The reviewer cannot play the
  recording -- it verifies the mechanism in the diff and that the recording exists,
  and a human watches it.

The fork lane (`fork-ux-review.yml`) carries the same rules but has **no blind-read
pass**: the fork head is never checked out, so the screenshots a fork PR adds are not
on disk. It records every added or changed control as an evidence gap instead, which
caps a fork UI change at `CONCERNS` (advisory). A maintainer who wants the blind read
pushes the branch to this repository.

The PR identity (number, repository, shas, data-file paths) is passed to both passes
in `--append-system-prompt`, not in `prompt:`. GitHub rejects a workflow file
silently (zero jobs, nothing on the PR) when any expression-bearing string exceeds
21000 characters, and the review prompt is past that once it carries these rules, so
`prompt:` must stay expression-free.

### Human override

`ai-review-human-override.yml` lets a repository **writer** record a judgment with:

```
/ai-review override <fable|gpt|all> <current-head-sha>: <one-sentence reason>
```

`issue_comment` workflows execute from the trusted default branch, never from the PR
head. The handler validates the command shape, a 7-to-40-hex SHA that must be the
**current** head, writer-or-above permission, and a non-empty reason under 500
characters, then posts a **bot-authored** marker comment that the reviewer workflows
trust. Raw PR comments can never turn a gate green directly; only that marker can.
The scope is **this commit only**, so a new push needs a new judgment. The workflow
then re-runs the affected reviewer, cancelling an in-flight run first so its stale
verdict cannot race the human decision. On a fork PR the affected reviewer is the
`workflow_run`-triggered Stage-2 lane, whose run objects are keyed to the default
branch — the handler locates the lane run through the run URL the lane stamps into
the `details_url` of the check-run it posts on the PR head, verifies the resolved
run belongs to the expected fork workflow, and re-runs it. The fork lanes consume
no override marker, so that re-run is a fresh review roll rather than a forced
pass. A rerun failure after the judgment has recorded is reported as a warning
annotation plus a PR notice naming the lane to re-run manually — never as a failed
run, which would make a recorded judgment look rejected.
`test/test_ai_review_workflows.py` pins the contract from both ends:
`test_handler_requires_write_permission_fresh_sha_and_reason` for the authorization and
freshness checks, and `test_fable_consumes_only_a_bot_authored_sha_scoped_record` plus
`test_gpt_has_clear_verdict_banner_and_human_override` for the consumer side, so an
untrusted PR comment or a decision for an earlier push cannot turn a gate green.

## `pr-readiness.yml`: the aggregator

It executes no tests. It resolves the PR's current head SHA, **drops stale events**,
queries the latest run per monitored workflow, and publishes **one `PR Readiness`
commit status plus one `readiness:` label**.

- **Always required:** Fast Gate, CI, Build, Code Review. `Fast Gate` is a lane in
  its own right and not merely CI's precondition — a red gate must red the PR, and
  `await-fast-gate` reports `failure` rather than the gate that actually broke, so
  the readable verdict has to come from the gate workflow itself. It carries CI's
  `branches: [main]` filter, so it sits in the same stacked-PR carve-out: on a PR
  whose base is not the default branch it never starts, and a monitored lane that
  reads `(not started)` would freeze the verdict at pending forever.
- **Additionally required on a same-repo PR:** CodeQL, Opus 4.8 Review, GPT 5.6
  Review, and completion of Design Review, UX Review and First Principles Review.
- **UX Review and First Principles Review are completion-required but advisory:**
  once complete they score as `"(advisory)"` whatever their conclusion, so neither
  their opinion nor an infrastructure failure becomes an independent blocker.
  Completion is still required so the verdict is not premature.
- **Design Review is completion-required AND blocks on a genuine `BLOCK`:** the
  aggregator scores its `failure` conclusion as a readiness blocker. That is safe
  because the lane fails its own check *only* on a `BLOCK` verdict — an errored,
  throttled or verdict-less run exits 0 — so a `failure` here can only mean a
  design judged wrong, never infrastructure noise.
- **CodeQL is not a checked-in workflow.** It runs via GitHub default setup and is
  resolved by `path == "dynamic/github-code-scanning/codeql"`. `skipped` counts as
  passed for it.
- **Labels:** `readiness: checking` (pending), `readiness: action required` (a
  blocker), `readiness: passed`. Exactly one is ever present.
- **It also enforces the disposition rule.** Besides scoring lanes, readiness runs
  `pr_status.py --disposition-gate` (checked out from the default branch, never
  from the PR head — this workflow is `pull_request_target` and holds write
  tokens) and folds each violation of the one-lane / one-rationale-per-finding
  rule into its blocking list. That is the only enforcement point that binds a
  writer who never runs the prepare-pr loop, which is what a blanket
  single-rationale record used to escape through (#6658). The rule keeps ONE
  implementation: the readiness step calls the same script the local gate does
  rather than re-reading the marker grammar in shell. A record set it cannot read
  is `pending`, never red — a transient comments-API failure must not fail the
  required status — and a record whose author the collaborators permission API
  does not confirm as a writer is ignored, exactly as `codex-review.yml`'s
  adjudication ledger ignores it, so the gate never blocks on a record that holds
  no downgrade power. One consequence to know: readiness has **no
  `issue_comment` trigger**, so correcting the offending comment fires nothing by
  itself. `pr-readiness-sweep.yml` mode 5 covers that — it treats a disposition
  record whose `updated_at` is newer than the verdict as evidence the verdict is
  stale, and re-fires the recompute within ~15 minutes. Deleting the record with
  no replacement leaves nothing observable and waits for a push or a manual
  dispatch. The comparison is not race-free and is not claimed to be: the gate
  reads the comments early in the readiness job while the status is published at
  the end, so a record created in between is missed by that run and also looks
  older than the verdict to the sweep. What bounds that residual is the harm
  model, not the detection -- a violating record's only power is letting the
  adjudication ledger downgrade a REPEATED finding on a later review round, and a
  later review round takes a push, which recomputes readiness and catches the
  violation.
- **Unapproved fork runs remain blocking but are attributed separately.** GitHub
  reports a fork workflow held behind *Approve and run* as `action_required`
  even though it has not executed. Readiness keeps the failure status and
  `readiness: action required` label, but lists those lanes under **Awaiting
  maintainer approval** instead of **Blocking**. It does not call them pending:
  only a maintainer can clear the condition, while pending statuses are eligible
  for automatic self-healing.

Two subtleties:

- **It refreshes while a workflow is re-running, but not when one starts.** It triggers
  on `workflow_run` `in_progress` and `completed`, not on `requested`. `in_progress` is a
  merge guard, not a cosmetic: it is the only type that sees a monitored workflow go back
  to running, because a re-run reuses the same run and increments its attempt instead of
  creating a new one. Without it, a re-run of an already-green lane would leave readiness
  publishing the pre-re-run `success` for the whole re-run -- and since that status is the
  branch-protection handle for the entire fan-out, armed auto-merge could merge a revision
  whose lane is failing at that moment. `requested` is the type that carries nothing: it
  fires at run CREATION, when no lane can have a verdict yet and readiness has already
  published `checking` from the `pull_request_target` path. Since every type fires once per
  monitored workflow per revision, listing all three dispatched up to 42 readiness runs per
  head update and made readiness ~67% of every workflow run this repository created; two
  types put the ceiling at 28. The `pr+sha` concurrency group collapses the burst for
  execution, but a collapsed run has already consumed its dispatch slot, so the group does
  not bound that cost.
- **A `pull_request_target` run gets its own isolated concurrency group.** Those are
  the only readiness runs that surface as a CheckRun in the PR's rollup, and GitHub
  marks any superseded run "cancelled" whichever way `cancel-in-progress` is set, so
  sharing a cancelling group would show a spurious cancelled check on the PR even
  though the authoritative commit status is fine. Un-collapsed runs on superseded
  revisions simply no-op green, because the evaluate and publish steps are idempotent
  and stale-SHA guarded. The `workflow_run` and `workflow_dispatch` runs do not appear
  in the rollup, so they keep the cheap per-`(pr, sha)` burst collapse.
- **The pending sentinel is conditional.** A `pull_request_target` open/synchronize
  run is meant to surface a transient "checking" signal, but it can be
  runner-queue-delayed past the `workflow_run` runs that already published the
  terminal verdict for the same SHA. Adding the sentinel unconditionally would then
  clobber a decided verdict back to `checking` with no further event left to
  recompute it on an unchanged commit, freezing the status at pending indefinitely.
  So it is added only when the live evaluation still found something genuinely
  incomplete.
- **A transport error during evaluation is non-terminal.** Every read-only `gh`
  call goes through a bounded retry helper (3 attempts with backoff, 120s cap per
  attempt); a non-429 HTTP 4xx is treated as permanent misconfiguration and fails
  the job loudly instead of retrying. If an **evaluation** read still fails after
  the retries, the evaluate step publishes an explicit non-terminal "could not be
  evaluated" verdict (`pending` under `readiness: checking`) instead of exiting
  non-zero — so a transient network/TLS blip during evaluation never leaves a red
  check-run or skips the publish step (issue #2753: the same commit evaluated
  green then red 39 seconds apart). Exhausted retries in the other steps (context
  resolution, closed-PR label cleanup, the publish step's own reads) still fail
  the job — only the evaluation loop has the non-terminal branch. This does not
  weaken the gate: `pending` blocks merge exactly like `failure`, and only a
  transport error with no already-observed blocker takes that branch (a genuine
  failure recorded by an earlier lane dominates and the verdict stays the
  terminal red `action required`, with a summary note that the evaluation was
  truncated). Recovery is automatic — the self-heal sweep re-fires stale pending
  statuses, and any later monitored-workflow event recomputes sooner. A truncated
  run defers (publishes nothing) only when the revision already carries a
  **blocking** verdict — the merge is already held and pending would only discard
  the red's diagnostics. Every other prior state publishes pending: an existing
  *success* is re-pended (a rerun means validation state is unknown again, and a
  stale green left mergeable is the unsafe direction — pending can only ever
  block, never allow), and an unreadable verdict state gets the same fail-safe
  treatment. The status
  POST itself is never retried: commit statuses are last-write-wins with no
  conditional write, so any retry races a concurrent run's newer verdict — a
  failed POST fails the step loud and a re-run republishes. The label writes
  keep only the narrow 404/already-exists race tolerance they already have.
- **Nothing keys off `workflow_run.pull_requests`.** That array is empty whenever the
  head repository is a fork, the same GitHub behaviour the `fork-*` workflows already
  work around. The job gate admits every `pull_request` and `dynamic` run and lets the
  head SHA resolve to a PR via `repos/:repo/commits/:sha/pulls`, and a monitored run is
  bound back to the PR by `(head_repository.full_name, head_branch)` on top of the
  `head_sha=` query — a pair that is populated on a fork run, and unique because only
  one open PR can exist per source repository + branch. Keying either place on the PR
  number froze a fork PR at pending forever: the gate skipped every re-evaluation, so
  the verdict was whatever the `pull_request_target` run saw *before* the monitored
  workflows existed, and the lookup independently reported already-green workflows as
  `(not started)`.

## Fork PRs

A fork PR gets no repository OIDC credentials or secrets, and this repository's
managed CodeQL workflow is not scheduled for fork heads. Two consequences.

**A fork PR can still reach `readiness: passed`.** The `fork-*` pipeline below runs
the AI reviews from the trusted base branch and posts them as check-runs under the
same names the same-repo lanes use, so `pr-readiness.yml` evaluates a fork from
those check-runs and a fully green fork is fully validated. CodeQL is the single
ineligible lane, reported as a non-blocking "Not eligible" note rather than a
blocker. Readiness therefore says the same thing on a fork as anywhere else: the
eligible automated validation passed for this revision. Human approval and branch
protection remain separate gates.

**The `fork-*` pipeline gives fork PRs AI review anyway, in two stages.**
`fork-opus-review.yml`, `fork-gpt-review.yml`, `fork-design-review.yml`,
`fork-ux-review.yml` and `fork-first-principles-review.yml` each trigger on the
**completion of `Fast Gate`** (stage 1) and run privileged from the default branch
(stage 2), gated on
`workflow_run.head_repository.full_name != github.repository`. Each posts a check-run
named exactly like its same-repo twin (`Opus 4.8 Review`, `GPT 5.6 Review`,
`Design Review`, `UX Review`, `First Principles Review`), so branch protection is
satisfied on either path, and it opens that check-run as early as possible keyed to
`head_sha` so a job that dies still leaves a fail-closed result.

Stage 1 is `Fast Gate` rather than `CI` because what stage 2 needs from stage 1 is a
TRUSTED workflow's word on the head commit, and `Fast Gate` gives that in about a
minute where `CI` took ~54. That is a latency change, not a trust change: the
security properties below hold whatever stage 1 is, since none of them depend on
`CI` having gone green. `CI`'s verdict was a quality precondition here, never a
security one — and `fork-workflow-guard.yml`, which IS a security lane, still keys on
`CI` and is unaffected.

On a fork PR this lane is the **only** publisher of that name -- the same-repo twin
renames itself (see the reviewer-job security posture above) -- so the protected
status can only be reported by a review that actually ran.

`fork-gpt-review.yml` publishes its GPT verdict by editing one marker comment in
place, so an incomplete run must never bury a posted verdict (the class of bug tracked
as #8292). It goes further than a preserve-and-prepend approach: an incomplete run never
modifies an existing comment at all. Overlapping runs for different SHAs can read the
comment before a newer run publishes its verdict, so an incomplete run that read verdict
V1 first must not PATCH V1 back over a newer run's V2 that landed in between -- and even
a PATCH that only preserved the verdict and prepended a notice would restore that stale
body. So when an existing bot comment is present, an incomplete run leaves it entirely
untouched (a diagnostic log line only), whether or not it carries a `[GPT-REVIEWED]`
verdict. Completed and blocked verdicts still replace the comment as before, and the
fail-closed `Finalize check-run` step is unchanged, so an incomplete run is never
mistaken for an approval and merge safety is unaffected.

Nothing the fork controls can influence these reviews:

- `workflow_run` **always** runs the workflow definition from the **default branch**,
  so a fork editing these files in its PR has no effect on what runs.
- `github.event.workflow_run.head_sha` is set by GitHub and is the only authoritative
  input taken from the trigger. The PR is resolved by matching an open PR whose head
  SHA equals it, because `workflow_run.pull_requests` is empty for forks.
- The base SHA is re-fetched from the PR via the API and the diff is re-derived from
  GitHub's compare endpoint pinned to `(base_sha...head_sha)`. Stage 1's artifact is
  an untrusted **hint** only, so a fork faking it changes nothing.
- The fork's code is only **read** (the trusted base tree plus the authentic diff as
  a data file), never built, installed or executed.
- `step-security/harden-runner` with `egress-policy: block` and a narrow endpoint
  allowlist, plus short-lived Bedrock-only OIDC credentials, bound the blast radius
  of any prompt injection.

**`fork-workflow-guard.yml`** blocks a fork PR that modifies anything under
`.github/**`, the vector a fork would use to fake basic-CI results (rewrite `ci.yml`
to pass) or tamper with CODEOWNERS. It is deterministic on purpose: "does the diff
touch `.github/**`" is a file-path check, so a grep on the authentic changed-file
list is completely reliable, instant and free, where a model gate would be slower,
cost money and could hallucinate. It runs from the default branch (via `workflow_run`
of CI, plus `pull_request_target` for the override-label re-evaluation), so a fork
cannot disable it, and a fork's own `pull_request` runs have no `checks: write` to
forge its verdict. A maintainer who has reviewed a legitimate workflow change applies
the `allow-fork-workflow-change` label and the guard re-evaluates green; the label is
stripped on a new revision, so the override cannot carry over.

## `dependency-vulnerability.yml`: the production npm gate

Every publication runs one blocking production-dependency control in
`.github/workflows/dependency-vulnerability.yml`. It deliberately does NOT run per pull request: the
audit reaches the npm registry, whose slow hours made it the one red X on otherwise-green PRs
(re-run by hand until it passed) — and a gate people learn to re-run until green is not a gate. It
runs where a vulnerable dependency would actually ship, so nothing vulnerable is published, and a PR
that adds or bumps a dependency is checked by the release or nightly that would carry it.

The two callers hang it off different layers on purpose:

- **`release.yml`** — the release wheel and desktop builds depend directly on the gate, so all
  publish, sign, and GitHub Release jobs are transitively unreachable when it fails.
- **`nightly.yml`** — every job that ships bytes to a nightly-channel user (`publish-cli`, the six
  `publish-linux-*` callers, `publish-windows-x64`, `publish-docker`, `sign-and-notarize`) depends on
  the gate; no build job does. main has no dependency gate of its own, so without this a
  high/critical production vulnerability landing on main shipped to nightly users unaudited until
  the next tagged release. Gating the builds instead is what once failed the nightly for hours at a
  stretch — hanging it off publication means a slow registry delays publishing an already-built
  nightly, and a re-run publishes the same artifacts once the audit answers.
  `test_dependency_vulnerability_gate.py` pins both halves: every publish job gated, no build job
  gated.

The gate audits all lockfile-backed Node applications independently:

- `website/package-lock.json`
- `website/electron/package-lock.json`
- `site/package-lock.json`

CI pins Node `24.19.0`, then invokes the exact npm package `npm@10.8.2` through `npx` with
`audit --omit=dev --package-lock-only --ignore-scripts --audit-level=high --json`. It neither
installs project packages nor runs project lifecycle scripts. High and critical production
findings block; information, low, moderate, and development-only findings do not.

**Transient-failure contract.** The audit is an idempotent read, so a stall or connection fault is
retried rather than failed on the first try. The pinned npm is resolved once up front
(`npx --yes npm@10.8.2 --version`, verified to print exactly the pinned version) so the download a
cold runner pays is never charged against an audit's own timeout. Each attempt is bounded by
`AUDIT_TIMEOUT_SECONDS` (180s); an attempt that times out, raises a subprocess error, or exits with
a status other than npm's documented audit results 0/1 **and** carries one of npm's connection-level
markers on stderr (`ETIMEDOUT`, `ECONNRESET`, `EAI_AGAIN`, `E503`, ... — `TRANSIENT_STDERR_MARKERS`)
is retried up to `AUDIT_ATTEMPTS` (3) times with a short backoff. Every attempt of every audit in a
run draws on one shared wall-clock budget (`AUDIT_TOTAL_BUDGET_SECONDS`, 720s, under the job's 15-minute ceiling): no attempt gets
more than the time left, and no retry starts unless the budget still holds its backoff plus a full
attempt's ceiling, so retries cannot outgrow the job's own `timeout-minutes`. Exit 0/1 are never treated as transient
whatever stderr says (1 is the audit answering "vulnerable"), and every other failure below is
definitive and never retried. Exhausting the attempts or the budget fails closed, naming the attempt
count so a persistent registry outage reads as one rather than as a flaky gate.

**Fail-closed contract.** A missing `npx`, missing manifest or lockfile, a warm-up that does not
yield the pinned npm, a transient failure that outlives the retries or the budget, a non-transient
subprocess error, an exit status other than npm's documented audit-result statuses 0/1, empty or
malformed JSON, npm
`error` response, unsupported audit report version, inconsistent counts/status, broken advisory
reference, or high/critical record without a stable advisory identity fails the job. Exit 1 is
accepted only with a structurally valid report that contains high/critical findings. String `via`
references are recursively resolved to leaf advisories, cycles and missing references are errors,
and findings are deduplicated by lockfile, affected package, and advisory. npm registry/advisory
availability is consequently an explicit release dependency: an outage blocks rather than skips
the control.

**Exception contract.** `.vulnerability-exceptions.json` is validated before any audit against the
contract represented by `.vulnerability-exceptions.schema.json` and the stricter date checks in
the gate. The root has exactly `version: 1` and `exceptions`; each exception has exactly:

| Field | Contract |
|-------|----------|
| `package` | Exact npm package name; wildcards are forbidden. |
| `advisory` | Exact canonical `GHSA-xxxx-xxxx-xxxx` or fallback `npm:<numeric source>` identity. |
| `paths` | One or more exact audited lockfile paths from the list above; no duplicates. |
| `reason` | Trimmed 20–500 character risk justification and mitigation. |
| `owner` | Accountable GitHub `@user` or `@org/team`. |
| `expires` | Real ISO `YYYY-MM-DD` date, no more than 30 days ahead at validation time. |

An exception matches only the package + advisory + lockfile tuple; it cannot suppress another
package, advisory, or project. Duplicate scopes, unknown fields, unsupported paths, malformed
identifiers, or an expiry more than 30 days ahead invalidate the complete file. An expiry date is
valid through that UTC date; beginning the next UTC day, the stale entry fails the entire gate even
if its advisory is no longer reported. Renewal requires a reviewed edit that moves the date back
within the 30-day window and confirms the owner, reason, and mitigation remain current. Remove an
entry as soon as the dependency is fixed; Git history is the approval record.

Run the same control from the repository root with:

```bash
python scripts/check_npm_audit.py
```

The command contacts npm's registry/advisory service. Unit tests mock the subprocess boundary and
cover malformed output, operational failures, report resolution, schema constraints, expiry, and
exact-match exception behavior without network access.

## AI-review human overrides: the authorization rules

The command grammar and the marker contract are in [Human override](#human-override); this section states the authorization and freshness rules the handler enforces.

Human judgment is the final authority over the Fable 5 and GPT 5.6
AI-review results. A repository member with `write`, `maintain`, or `admin`
permission can record a false-positive, not-applicable, or accepted-risk
decision with:

```text
/ai-review override <fable|gpt|all> <current-sha>: <reason>
```

The decision is intentionally explicit and commit-scoped. The handler resolves
the current PR head and accepts a 7–40-character SHA prefix only when it matches
that head; the trusted record stores the full SHA. Any subsequent push therefore
invalidates the decision and causes normal AI review on the new commit.

**Trust boundary** — `.github/workflows/ai-review-human-override.yml` runs on
`issue_comment`, so GitHub loads it from the default branch. It never checks out
or executes PR-controlled code. Before changing a result it requires:

1. The exact command shape above and a non-empty, at-most-500-character reason.
2. A current-head SHA match.
3. The commenter to have `write`, `maintain`, or `admin` collaborator
   permission. PR authors receive no exemption.

After validation it posts a `github-actions[bot]` comment whose hidden marker
binds `{target, full head SHA, actor, source comment id}`. Reviewer workflows
trust only this bot-authored marker; a raw author or third-party comment cannot
turn a gate green. The handler has only review-control permissions
(`actions:write`, `checks:write`, `pull-requests:write`, and
`contents:read`), and receives no `id-token` or `contents:write`.
`pull-requests:write` is required for the handler to create the trusted record
on a pull request; `issues:write` alone does not make that write reliable for a
GitHub Actions installation token.

For Fable 5 and GPT 5.6, the handler re-runs the existing PR workflow. The
re-run resolves the trusted marker before acquiring AWS credentials, skips the
model invocation, updates the existing summary with a human-override banner,
and exits its original gate successfully. Either event ordering — an override
recorded before a reviewer starts, or one arriving during model execution —
leaves the SHA-scoped human decision authoritative.

The marker-keyed comments expose the override command to repository
writers. GPT 5.6 also normalizes each current-commit result into a
top verdict plus one sentence: `✅ no blocking findings`,
`🔴 changes requested (blocking)`, an incomplete state, or a human-override
state, so a green verdict from the previous commit is never left looking
current.

When no current-SHA override is active, GPT 5.6 injects a bounded
ADJUDICATION LEDGER into the review prompt: the bot-authored override
records, plus the marker and finding-title lines of review-disposition
comments whose authors' current collaborator permission is `write`,
`maintain`, or `admin` (verified per login against the collaborators
permission API — the same check the override handler applies to its actor).
Prior review bodies are never injected. The ledger is nonce-delimited,
capped at 6,000 bytes, and explicitly untrusted data: it can downgrade the
repetition of an adjudicated finding class to advisory, and it can never
waive a new defect or authorize a green verdict.

GPT makes exactly two model calls. Pass 1 discovers candidates across the
full diff; pass 2 attempts to falsify each candidate and emits the only
verdict exposed to the comment and gate. Pass 2 also drops or downgrades a
candidate whose proposed fix violates the FIX BAR, a BLOCKING candidate that
cannot be anchored to an AUTOSDE rule or residual defect class, and a
relocated variant of a ledger-adjudicated class; an adjudication goes stale
for lines the current head materially changed. A prior disposition never
hides a currently provable new defect. Any failed call makes the review
incomplete and leaves no current-SHA reviewed marker, so the gate fails
closed.

## Readiness: what the aggregate does and does not mask

The job's inputs and outputs are in [`pr-readiness.yml`: the aggregator](#pr-readinessyml-the-aggregator); this section states the masking guarantees.

`.github/workflows/pr-readiness.yml` publishes one current-revision answer for
the repository's fan-out of CI and AI reviews. The commit status context is
`PR Readiness`; the PR carries exactly one matching managed label:
`readiness: checking`, `readiness: action required`, or `readiness: passed`.
The workflow creates missing labels idempotently, replaces the prior readiness
label, and removes readiness labels when the PR closes. A passed label means
the automated lanes passed for that SHA; it does not represent human approval.
Making `PR Readiness` a required status remains an explicit branch-protection
or ruleset setting outside the workflow.

The aggregate covers the latest PR run for CI, Build,
Code Review, Opus 4.8 Review, GPT 5.6 Review (the reconciled result of its three
calls), and Design Review, plus the managed dynamic CodeQL workflow conclusion.
Grading the CodeQL
workflow conclusion, rather than its neutral summary check, preserves failures
from any managed Analyze job. Fork PRs cannot receive repository secrets or
OIDC credentials, and this repository's managed default-setup CodeQL workflow
is not scheduled for fork heads. The secret-backed AI reviews therefore run for
forks from the trusted base branch via the `fork-*` pipeline and are graded from
the head SHA's check-runs, leaving CodeQL as the only lane explicitly ineligible
for a fork. Missing or running eligible lanes
produce `checking`; blocking workflow/check failures produce
`action required`; drafts remain `checking`.
Design Review completion is required, but its verdict and
infrastructure conclusion are advisory. It emits one `PASS | CONCERNS | BLOCK`
verdict and no separate blast-radius rating, and it owns the long-term
reversibility (one-way-door) lens. Mergeability, behind-base state,
and human review decisions are not part of this event-driven aggregate because
they can change without an aggregate refresh event; branch protection and the
live `prepare-pr` status check own them.

Every event resolves the PR's current head through the GitHub API. An event
carrying an older expected SHA is ignored, so a late
run cannot relabel the new revision. A code-free `pull_request_target` handler
updates same-repository and fork PRs from the trusted base workflow. Actions
that start or restart validation for the same SHA, including a PR description
edit that re-runs Code Review, force the aggregate to `checking` before run
lookup so an older successful same-SHA run cannot keep readiness green. Trusted
base-repository `workflow_run` events refresh it as eligible lanes finish,
including the `fork-*` reviewer completions that carry a fork's verdicts.
Readiness-label events cannot recursively rerun or cancel a review: ignored label
events use a per-run concurrency key, so they cannot cancel an
active review or replace a pending authoritative reviewer event.

The bundled `prepare-pr` skill front-loads the same review contract before the
first push. Description/diff reconciliation and every allowed commit mutation
happen before review. After local gates, it dispatches two independent,
read-only subagents over the finished base-to-head diff: one owns correctness,
security, and platform compatibility; the other owns contracts, tests, error
paths, and the user workflow. Both use the canonical severity and output rules
from `.github/workflows/codex-review.yml`. Legitimate Critical/High findings are
fixed before publication; Medium/Low findings remain advisory unless a human
escalates them. If a blocker fix changes code, one focused verifier
checks that fix. The skill records the verifier-cleared SHA and fails closed if
HEAD changes before push; it does not start an unbounded local review loop.
During a post-submit round, it records one concise, marker-keyed GPT disposition
comment before re-pushing whenever findings were fixed or rebutted. That record
names the prior reviewed SHA, finding identity, outcome, and evidence so the
next reconciliation call can distinguish a real delta from a repeated argument;
the record remains untrusted evidence and does not carry an override forward.

`prepare-pr/scripts/pr_status.py` treats the aggregate status as authoritative
when present, including over stale failed or pending duplicate checks in
GitHub's rollup. Older PRs without the aggregate retain the fail-closed legacy
rollup behavior. Only the commit-status `context` named `PR Readiness` is
trusted as the aggregate; a same-named CheckRun cannot mask another failure.
Unresolved review threads are reported for visibility but are advisory rather
than an automatic readiness failure.

## Over-engineering resistance

AI-native coding skews toward over-engineering, and a naive AI reviewer compounds it
by demanding still more mechanisms, which produces unending review loops. Every layer
resists this:

- **Both line reviewers share an identical FIX BAR:** every finding must carry a fix
  expressible as an edit to lines **this PR changed**. If the fix would need a new
  function, module, abstraction, config knob, dependency, or an edit to untouched
  code, it is out of scope for the bot. GPT 5.6 drops such a finding; Opus 4.8
  **demotes it to advisory instead of dropping it** -- the author cannot land the
  remedy in this PR, so it must not gate the merge, but the signal is real and a
  human decides. A regression the diff itself introduces still blocks either way,
  since reverting the hunk is an in-diff fix. **The absence of a
  mechanism is never a finding.** This makes "add mechanism X" structurally
  un-reportable: the demand fails the bar before it can become a finding. A scope cap
  complements it: Opus 4.8 stays within the evident scope of the diff (it is code-only),
  and GPT 5.6 stays within the PR's stated purpose, flagging a
  description-versus-diff mismatch as an **advisory** finding rather than a block.
- **The WHAT BLOCKS list is closed:** exhaustive, never extended, never reasoned about
  by analogy, with no "and other serious issues" clause. A finding blocks only if it
  is a `blocking: true` AUTOSDE-rule violation on a changed file (or this PR
  weakening such a rule), or a **reachable and concrete** residual-class defect: a
  security hole with a named trigger, a crash or data loss or corruption on a path
  this diff changes, or a removed guard with no compensating replacement. Style,
  naming, speculative performance and hypotheticals never block.
- **Design and UX suggestions must be proportionate,** and Design carries the
  simpler-alternative ethos: actively flag when a materially simpler solution exists,
  but always advisory.
- **`prepare-pr`'s severity gate closes the loop:** validate each finding's
  legitimacy first, fix the true Critical and High ones, **rebut a false positive with
  evidence rather than appeasing it by changing correct code**, and defer the low ones.
  Combined with the single-commit rule and description reconciliation, that keeps a PR
  converging on its stated purpose instead of accreting scope round over round.

The net effect: expensive or irreversible risk blocks, and everything else is advice a
human can take or defer. "More mechanism" is deliberately not a demand that can block.
