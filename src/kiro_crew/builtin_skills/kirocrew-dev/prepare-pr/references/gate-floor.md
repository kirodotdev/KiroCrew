# Why the gate floor looks the way it does

Maintainer-facing rationale for `profiles/kirocrew.json` `setup[]` and `gates[]`. The loop does
not need to read this to run — `SKILL.md` Phase 2 carries the rules it executes.
Read this when **adding, changing or removing setup or a gate**, because every entry below
is shaped by a failure that cost review rounds, and the shapes are not obvious.

The two lists have different contracts:

- **`setup[]` provisions prerequisites once per worktree.** It may add to a
  per-user, version-scoped tool cache. Failure means the environment is not
  ready; it is not a verdict on the diff.
- **`gates[]` contains pure checks run on every review iteration.** A gate must
  not provision prerequisites. Failure means the diff is not ready.

Every command in either list must still satisfy three constraints:

1. **No privilege.** A command that needs root either blocks on a password prompt or
   changes the machine.
2. **No replacing anything the developer relies on.** Adding to a per-user tool
   cache is fine — it is additive, idempotent and version-scoped, which is what
   both the Playwright browser download and `uv tool run` do. *Replacing* a tool
   they already have is not: `pip install "cfn-lint==1.22.3"` satisfies
   constraint 1 and still downgrades their copy. The line is additive-vs-
   destructive, not inside-vs-outside the worktree.
3. **CI's exact version.** A different version diverges from CI in *both*
   directions — newer reports findings CI will not, older misses findings CI will.

Provisioning that satisfies all three belongs in `setup[]`; checks belong in
`gates[]`. Provisioning that cannot satisfy them (the Playwright **system
libraries**, which need root) belongs to documented one-time host setup instead.

## Copy the whole CI step, not the half that looks like the check

A workflow step is often two commands: the detector's own self-test, then the
scan. CI runs `check_brand_name.py --test` before the scan and `docs_lint.py
--test` before `docs-lint.sh`. A PR that *changes a detector* fails the self-test
while the scan stays clean, so a floor carrying only the scan passes locally and
fails after push. Both self-tests sit ahead of their scans in `gates[]`.

Note that "CI" here means both workflows. The eleven cheapest blocking gates —
`scrub-lint`, `vendor-manifest`, `brand-lint`, `focus-cue-lint`,
`feature-map-lint`, `changelog-history`, `builtin-skill-scope`,
`loop-bound-locks`, `testpaths-coverage`, `harness-parity` and `docs-lint` — run
in `.github/workflows/fast-gate.yml`, not `ci.yml`, so `ci.yml` gaining a
blocking scan is no longer the only way the floor can fall behind. The commands
are unchanged, so `gates[]` needs no edit for the move itself; what the split
costs is described under "Scan by every shape a step can take" below.

## Derive a ratchet; never transcribe it

`npx eslint src/ --max-warnings <n>` is not interchangeable with the repo's own
`npm run lint` (`eslint src --ext .ts,.tsx`), which carries no warning ceiling —
the convenient one passes locally while CI fails on a new warning.

But a baseline number transcribed into the profile is a countdown, not a gate:
the profile is frozen into every install, so the next ratchet in the workflow
turns the entry into a silent false green. The eslint entry therefore reads the
ceiling out of `.github/workflows/ci.yml` at gate time, requires the captured
value to be non-empty, and only then runs.

Anchor such a pattern to the surrounding command, not to the flag alone: a nearby
*comment* mentioning the same flag would otherwise be matched first.

## Reproduce what CI PROVISIONS, not only what CI runs

A job installs things before its steps, and a gate lifted out of the step list
inherits none of it — so it fails on the missing prerequisite instead of on a real
finding. That is a spurious red, and it costs a round to diagnose.

The render-time i18n gate is the case in point: the script imports `playwright`
and calls `chromium.launch()`, and CI installs the browser in a preceding step of
the same job — which is *why* the check lives in that job at all. A floor carrying
only the npm script dies with a browser-launch error on any fresh worktree, whose
documented setup is `npm ci` alone.

So: when adding a gate, read its whole **job**, not just its step.

## Provision only what needs no privilege

CI provisions that browser with `--with-deps`. Copying the flag into the floor
would be wrong: on a non-root Linux box Playwright turns `--with-deps` into
`sudo -- sh -c 'apt-get update && apt-get install …'` (falling back to `su root`
when `sudo` is absent). A review gate would then either block on a password
prompt or silently change the machine's system packages. CI can use it because CI
*is* root in a disposable container; a workstation is neither.

The profile's `setup[]` therefore installs the **browser binary** only — no
privilege required — and a genuinely missing system library surfaces at launch
with Playwright's own message naming the exact `apt-get install` line. The
**system libraries** are per-machine and privileged, so they belong to documented
one-time host setup: on a fresh Linux host run
`sudo npx playwright install --with-deps chromium` once, alongside `npm ci`.

## For a pinned external tool, run it ephemerally rather than installing it

`uv tool run --from "cfn-lint==1.22.3" cfn-lint …` fetches CI's exact version,
needs no root, and leaves whatever the developer already has installed untouched.
Both obvious alternatives fail one of the three constraints:

| form | what goes wrong |
|---|---|
| bare `cfn-lint …` | exits **127** on a fresh checkout — the `dev` dependency group does not carry it, CI installs it in its own step — so it blocks every PR |
| `pip install "cfn-lint==1.22.3"` first | privilege-free, but **downgrades** the developer's own copy as a side effect of a review gate |

## Prefer the workflow's command over the package's convenience script

They are not always the same check, and the difference is usually invisible from
the script name. The `Bundle Size Gate` is the live example: `npm run build`
deliberately writes no `dist/bundle-report.json`, so CI runs
`vite build --mode analyze` FIRST and only then `scripts/check-bundle-size.mjs`.
Reproduce the gate with `npm run build` alone and the checker exits 2 on a missing
report — a floor entry built from the convenience script would be enforced in
appearance only, which is strictly worse than a missing gate because nobody goes
looking for it.

## Working directory is part of the command

`npm --prefix website run <script>` works, because npm runs a *script* with the
prefix as cwd. `npm --prefix website exec -- <binary>` does **not** change cwd, so
a tool resolving config relative to cwd (eslint looking for `eslint.config.js`)
fails with a config-not-found error that looks nothing like a lint failure. For a
bare binary use a subshell — `(cd website && npx eslint …)` — matching the
workflow's own `working-directory:`.

## A diff-scoped gate without its base ref is a no-op that always passes

Worse than a missing gate, because it looks enforced. `check_brand_name.py`
reports tree-wide and exits 0 unless `BRAND_BASE_REF` is set; the i18n checks only
compare against a base when `I18N_BASE_REF` is set.

Worse still, an *unresolvable* base fails **open** if the substitution is inlined.
The profile resolves it first and short-circuits — `BASE="$(git merge-base HEAD
origin/main)" && BRAND_BASE_REF="$BASE" …` — which returns nonzero when the base
cannot be resolved instead of silently reporting nothing.

When adding any gate: supply its base ref, make an unresolvable base fail closed,
and **prove the gate can FAIL** by running it against a deliberate violation
before trusting a green from it.

## Scan by every shape a step can take

`test/test_prepare_pr_profiles.py` holds both floor lists to `ci.yml` so that CI gaining
a blocking scan fails a test rather than surfacing as a review round on a later
PR. A check written as a bare binary (`cfn-lint`, `mypy`, `flake8`) is invisible to
a `scripts/`-and-`npm run` scan, so the parity test also enumerates the **tool
names** `ci.yml` invokes and makes each one either a gate or a named exemption.

That test reads `ci.yml` and only `ci.yml`, which the Fast Gate split turned into a
hole rather than a failure: the eleven gates it moved are all still in `gates[]`, so
nothing goes red, but a NEW blocking gate added to `fast-gate.yml` would no longer
fail this test when the floor misses it — exactly the review round the parity test
exists to prevent. Extend the scan to both workflow files before adding a gate
there.

Strip comment-only lines before any such scan. `ci.yml` names commands in prose as
well as running them — the Type check step's comment explains why it spells out
`npx tsc -b` instead of going through a script — so a naive grep for
`npm run <script>` "finds" scripts no step invokes, the same trap as reading a
ratchet number out of a comment.

## A test gate may SKIP a surface, but must not narrow within one

The two test gates are the most expensive entries on the floor by orders of
magnitude: 62,108 collected backend tests — collection alone takes ~100s before a
single test executes — and ~1,444 frontend spec files. CI shards that across eight
backend runners plus several frontend shards and runs it on `refs/pull/<N>/merge`
regardless, so paying for it serially on a workstation, once per iteration of a
ten-round inner loop, buys a signal CI produces anyway.

`scripts/run_scoped_tests.py` therefore does exactly one reduction, and it is CI's
own: when a diff touches only ONE surface, the other surface runs the
cross-surface set instead of its full suite. Measured on this checkout, that is
**350** backend files for a frontend-only diff and **146** frontend specs for a
backend-only one. Four verdicts, and every one that is not a reduction runs
everything:

| condition | verdict |
|---|---|
| base ref absent or unresolvable | **exit 2** — fail closed, run nothing |
| broad-impact file changed (fixtures, collection config, workflows, lockfiles, the vitest setup graph, the runner itself) | full suite |
| the diff touches CI **meta** paths (`.github/**`, `scripts/**`) | full suite |
| the diff touches THIS surface | full suite |
| the diff touches only the OTHER surface | cross-surface set |

**The surface split is transcribed from `ci.yml`'s `changes` job, buckets and veto
alike, because that job is the authority for the question.** Its three buckets are
`frontend: website/**`, `meta: .github/** scripts/**`, and `backend: **` minus the
other two, and it disables BOTH reductions whenever `meta` is touched. An earlier
revision folded `meta` into `backend`, which is invisible until it bites:
`.github/scripts/frontend-blob-reconcile.mjs` is asserted on by
`website/src/test/frontendBlobReconcile.wireFormat.test.ts`, so a reduced frontend
run dropped that spec, and `scripts/` and `docs/` are read by several i18n and
settings specs too. Meta paths belong to neither surface and can be read from
both. Note the corollary: this runner lives under `scripts/`, so any change to it
disables its own reduction.

### Why it does not narrow within a surface, and why that is not timidity

The obvious next step is to run only the tests that reference what the diff
touched. That was implemented, reviewed six times, and removed. Nine findings
came back, all real, and all one impossibility: **a text scan cannot enumerate the
ways a test can reach a module.** The spellings found were absolute import,
relative import (`from .store import ...`), barrel re-export, in-package fixture,
global vitest setup, data-file read, cross-surface parity comparison, and
documentation contract. Nothing suggested that list was finished.

Every remedy also shrank the allowlist — "fall back to the full suite here",
"escalate `index.ts`", "documentation is not inert". Extrapolated, the allowlist
converges on "escalate everything", which is the gate this was meant to replace.
Doing it soundly needs a real import graph: a Python AST pass, and a TS resolver
that follows barrel re-exports. That is tracked separately, deliberately not
smuggled in here.

The measured traps from that attempt are recorded because they are not obvious and
any future import-graph work will meet the same ones:

| trap | what went wrong |
|---|---|
| matching a module by its **bare stem** | `session` and `config` occur in nearly every test file's prose, so a one-file diff selected 621 of ~700 test files — the full suite wearing a smaller number |
| matching a broad-impact marker as a **bare substring** | `clone_setup.py` contains `setup.py`, so an ordinary module was escalated as packaging config. Markers now match on file NAME (exact, or a prefix for `tsconfig*`/`vite.config*`/`requirements*`) or on a path prefix |
| matching a data file by its **bare basename** | `prepare-pr/profiles/kirocrew.json` selected 38 test files that all mention `kirocrew.json` meaning Kiro Crew's own config file in a different directory |
| matching a barrel module by its stem | `store/index.ts` has **128** real consumers, while **235** specs merely contain the word `index` — simultaneously too wide and missing the right ones |
| classifying a file by **location** instead of role | `src/kiro_crew/apps/builtins` is a source tree AND a configured testpath, so "inside a root" was read as "is a test", making production modules look like helpers |
| assuming an extension makes a file **inert** | `.md` under `docs/` is prose, but `test/test_build_target_parity.py` reads `docs/`, and `.md` under `src/` is packaged skill content with contract tests. Nothing is classified inert now |
| mixing **path spaces** | `vitest` runs with `cwd=website` and needs `src/…`, while a repo-wide scan answers `website/src/…`. Handing the runner the latter fails the gate with a file-not-found that looks nothing like a test failure |
| adding `--` to **both** runners | correct for pytest; for vitest, `run -- <paths>` silently stops filtering and runs the WHOLE suite (measured: 1,474 files / 22,939 tests) while the gate still reported a narrow scope |
| keeping only the **new** side of a rename | hid the old path's disappearance, so a rename with a stale importer looked like an ordinary edit. `--no-renames` reports both endpoints |

### Rules this section leaves behind

**Roots and paths are read, never transcribed, and every hardcoded path is
asserted to exist.** `ALWAYS_ON` carried such an assertion from the start; the
broad-impact prefixes did not, and one of them — `website/src/test/setup` —
resolved to nothing at all, so the real vitest setup graph (`integration/setup.ts`
per `vite.config.ts`, plus the global MSW handlers in `integration/mocks/server.ts`
that every integration spec inherits without naming) was never treated as
broad-impact. That gap survived four review rounds because a dead path is
indistinguishable from a working one. The self-test now walks the whole tuple.

**Reuse CI's selector rather than inventing a second answer.** The cross-surface
set comes from `scripts/ci-surface-tests.py`, the same script and the same
post-processing `ci.yml` uses (strip `website/`, drop the `electron/` lane, which
runs under `node --test`). A skip would have been unsafe — a frontend-only change
can break a backend test that reads a frontend module — and a locally-invented
selector would be one more thing that can silently disagree with CI.

**Targets are validated before they reach argv.** They come from a selector's
stdout, so a file committed as `--config=evil.ini` would otherwise arrive as an
OPTION. There is no shell (argv is a list, never `shell=True`), so this is
argument injection rather than command injection, but a test runner's own flags are
quite enough to do damage. `validated_targets()` requires a plain relative path
resolving to a real file inside the runner's root.

**Narrowing un-bundles what `npm --prefix website test` ran for free.** That
script is `pretest` (jscpd) + `test:website` (vitest) + `test:electron`; the
cross-surface path runs only vitest, so **jscpd and the Electron specs are
explicit floor entries**. Without them they would disappear from the floor as a
side effect of a speed change.

## Checks with no local entry point

Deliberately absent from the floor, because nothing local reproduces them: the
`Automated Rule Check` greps, the inclusive-language scan, and the
conventional-commit **PR-title** check. They are named here so their absence is a
decision on the record rather than an omission.

`check_per_file_coverage.py` is a partial member of this class. Its **self-test**
is a floor gate, because the gate's own decision logic is exactly what a local
run can falsify. Its **enforcement** form is not, and cannot be: it reads the
Cobertura report that `coverage-combine` produces by merging the 3.12 shards, so
reproducing it locally means running the full backend suite under coverage and
the whole vitest suite with `--coverage` — minutes of work to re-derive a number
the PR's own CI run publishes for free. A per-file regression therefore surfaces
in Phase 3's server poll rather than Phase 2's local gate. That is an accepted
asymmetry: the failure names the offending file and its rate, so triage costs one
read, not a bisect.
