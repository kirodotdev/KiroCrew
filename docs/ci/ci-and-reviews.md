# CI and the review gates

What runs on a pull request, what each gate is for, and how they fold into one
verdict. The source of truth is `.github/workflows/`; this doc explains the
shape and the rationale.

The `prepare-pr` skill
(`src/kiro_crew/builtin_skills/kirocrew-dev/prepare-pr/SKILL.md`) is the agent
side of this: it drives a working tree to review-ready by working with these
gates. Its phase flow, exit-code contract and PR-description contract live in
that skill, not here. Its portability design is
[prepare-pr-portability.md](prepare-pr-portability.md). The human release process
is [CONTRIBUTING.md](../../CONTRIBUTING.md).

## Shape

CI is a **fan-out of independent workflows that one aggregator folds into a single
verdict**:

```
pull_request
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

Two structural facts explain most of the rest:

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
  PR's author to the README Contributors block via
  `scripts/update_contributors.py`; because the default branch is protected it
  opens a rolling PR rather than committing directly, like `test-durations.yml`.
  A login in `.github/contributors-optout.txt` is never added, which keeps the
  README's removal promise enforceable against the full-rebuild collector).

## `ci.yml`: correctness

Every job here is blocking.

| Job | What it enforces |
|---|---|
| `scrub-lint` | `scripts/scrub-lint.sh --no-history`. Fails on any internal marker in this public tree, so a sync cannot reintroduce a coupling |
| `vendor-manifest` | `scripts/verify_vendor_manifest.py`. Hashes every file under `src/kiro_crew/_vendor` against the committed `scripts/vendor_manifest.sha256` — the tree is excluded from semgrep and the AI reviewers' diff, so this checksum is its only content review. Always-on (not behind the `changes` path filter) |
| `backend-lint` | `isort --check-only`, `flake8`, `mypy` on Python 3.10 and 3.12. `black --check` is commented out pending a bulk format pass |
| `harness-parity` | `scripts/check_harness_parity.py`, self-test first. Fails on a newly added line that expresses "this is the Kiro harness" as the absence of another one — a shape that fails toward the permissive answer, so nothing else goes red. Diff-scoped; the whole-tree backlog is a non-failing report |
| `backend-test` | 2 Python versions x 4 duration-balanced pytest-split shards (8 jobs), `-n auto` within each. Coverage only on 3.12 (3.10 passes `--no-cov` for a trace-free run) |
| `backend-test-windows` | windows-latest, 4 shards, `--no-cov`, 180s per-test timeout. The backend supports Windows natively via `platform_compat`, and nothing else in CI holds that line |
| `backend-test-macos` | macos-14, deliberately SCOPED (gateway, socketsec, platform-compat, pod and MCP-apps suites via a glob). A full macOS run needs its own exclusion burn-down first, and a job that is red on arrival trains people to ignore it |
| `backend-test-sandbox` | The one job that clears the AppArmor userns restriction, so the tests guarded by `skipif(not userns_available())` EXECUTE instead of skipping. Runs `test_script_hooks` and `test_cron_script`. The shards collect the same files — nothing is deselected any more — but there the sandbox-guarded tests skip, so only here do those assertions actually run |
| `coverage-combine` then `coverage-gate` | Combines the 3.12 shard data, then enforces the project line-rate floors, plus a per-file floor with a shrink-only baseline (all floors live in the job's `env:` block) |
| `frontend-lint` | `tsc -b`, `eslint --max-warnings 1116`, `jscpd`, and `npm run i18n:check` |
| `electron-test` | The Electron shell's own node:test suite (`website/electron`) |
| `frontend-test` | `vitest run --coverage` |
| `cfn-lint` | Lints the artifact-deploy templates with a pinned `cfn-lint` |
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
- **`coverage-gate` is fail-closed.** It runs `if: always()` and its first step
  converts any non-success upstream result into an explicit failure, because GitHub
  treats a **skipped** required check as satisfied. It also compares the raw
  line-rate and rounds only for display, so 79.95% cannot pass an 80% floor.
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
- **`eslint --max-warnings 1116` is a ratchet baseline.** Burn it down, never raise
  it.
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

**Neither desktop lane ever RUNS the frozen binary.** `build-desktop` here and
`build-desktop.yml` in the release lane both build the real PyInstaller
`kirocrew-backend` (via `packaging/kirocrew-backend.spec`) and then only upload the
artifact. The wheel lane at least runs `kirocrew --version`. So a packaging change
that breaks the frozen app (a PyInstaller layout change, an executable rename, a
missing hidden import that stops the binary from booting) passes every gate: the
tests that cover frozen behavior monkeypatch `sys.frozen` and `sys.executable`, so
they stay green against a simulated environment. The cheap fix is to run the
already-built binary once in `build-desktop`, the frozen analogue of the wheel
lane's `--version`.

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
- **`inclusive-language`** runs a SHA-pinned `woke` over added lines only and fails
  on `(error)` severity. Legacy violations are burned down separately; this stops
  new ones.
- **`sast`** runs Semgrep in a pinned container, diff-only against the base,
  `p/python p/typescript p/security-audit p/secrets`, with `--error`. Blocking.
- **`dep-audit`** calls the reusable `dependency-vulnerability.yml`, which runs
  `scripts/check_npm_audit.py` over every lockfile-backed Node project and fails
  closed on **high or critical production** vulnerabilities. Time-boxed exceptions
  live in `.vulnerability-exceptions.json`.
- **`pr-hygiene`** enforces a Conventional-Commits PR title (it becomes the
  squash-merge message) and exactly one commit (`git rev-list --count == 1`). Both
  blocking.

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
| UX Review | `UX Review` | Agentic Fable 5, with the same fallback | Code plus committed screenshot PNGs, read directly | Does the shipped experience read correctly? | Advisory; red only on a genuine `BLOCK` |
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
"possible issue" tier. A finding must state a concrete input or condition that
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
in. In the Opus lane such a finding is tagged `(origin: validation)` in the posted
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
- GPT 5.6 emits `[GPT-REVIEWED] <sha>` / `[BLOCK-MERGE] <sha>`.
- Design and UX emit `Design-Verdict:` / `UX-Verdict: PASS | CONCERNS | BLOCK`,
  parsed from a header line.

A missing reviewed-marker for the current head fails the gate closed, because a
no-output review must not look clean. A BLOCKING-labelled finding without the
`[BLOCK-MERGE]` marker is only a non-gating **advisory warning**, since a coherence
check on that pairing mis-fires whenever the model quotes prior text.

### Security posture of the reviewer jobs

- Explicit fork guards (`head.repo.full_name == github.repository`), so the job
  **skips** on a fork rather than failing an unsatisfiable credential step. GitHub
  treats a skipped required check as satisfied, which is why fork coverage needs
  the separate `fork-*` pipeline below.
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
verdict cannot race the human decision.

## `pr-readiness.yml`: the aggregator

It executes no tests. It resolves the PR's current head SHA, **drops stale events**,
queries the latest run per monitored workflow, and publishes **one `PR Readiness`
commit status plus one `readiness:` label**.

- **Always required:** CI, Build, Code Review.
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

Two subtleties:

- **It refreshes while a workflow is re-running.** It triggers on `workflow_run`
  `requested` and `in_progress` as well as `completed`, so when a monitored workflow
  flips back to running (most often a reviewer re-run after a human override) the
  live query buckets it into `pending` and the label honestly drops from a stale
  `action required` back to `checking`, instead of freezing on the previous commit's
  verdict. The `pr+sha` concurrency group collapses the resulting burst into one
  evaluation.
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
`fork-opus-review.yml`, `fork-gpt-review.yml`, `fork-design-review.yml` and
`fork-ux-review.yml` each trigger on the **completion of CI** (stage 1) and run
privileged from the default branch (stage 2), gated on
`workflow_run.head_repository.full_name != github.repository`. Each posts a check-run
named exactly like its same-repo twin (`Opus 4.8 Review`, `GPT 5.6 Review`,
`Design Review`, `UX Review`), so branch protection is satisfied on either path, and
it opens that check-run as early as possible keyed to `head_sha` so a job that dies
still leaves a fail-closed result.

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
