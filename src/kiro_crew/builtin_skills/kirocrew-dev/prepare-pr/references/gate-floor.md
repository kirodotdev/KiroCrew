# Why the gate floor looks the way it does

Maintainer-facing rationale for `profiles/kirocrew.json` `gates[]`. The loop does
not need to read this to run — `SKILL.md` Phase 2 carries the rules it executes.
Read this when **adding, changing or removing a gate**, because every entry below
is shaped by a failure that cost review rounds, and the shapes are not obvious.

The three constraints every gate must satisfy at once:

1. **No privilege.** A gate that needs root either blocks on a password prompt or
   changes the machine.
2. **No replacing anything the developer relies on.** Adding to a per-user tool
   cache is fine — it is additive, idempotent and version-scoped, which is what
   both the Playwright browser download and `uv tool run` do. *Replacing* a tool
   they already have is not: `pip install "cfn-lint==1.22.3"` satisfies
   constraint 1 and still downgrades their copy. The line is additive-vs-
   destructive, not inside-vs-outside the worktree.
3. **CI's exact version.** A different version diverges from CI in *both*
   directions — newer reports findings CI will not, older misses findings CI will.

Provisioning that satisfies all three may live in `gates[]`; provisioning that
cannot (the Playwright **system libraries**, which need root) belongs to
documented one-time setup instead. Splitting the floor into explicit `setup[]`
and `gates[]` keys would make that boundary structural rather than conventional —
worth doing, and tracked separately (#2599) rather than smuggled into a docs change.

## Copy the whole CI step, not the half that looks like the check

A workflow step is often two commands: the detector's own self-test, then the
scan. CI runs `check_brand_name.py --test` before the scan and `docs_lint.py
--test` before `docs-lint.sh`. A PR that *changes a detector* fails the self-test
while the scan stays clean, so a floor carrying only the scan passes locally and
fails after push. Both self-tests sit ahead of their scans in the profile.

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

The floor therefore installs the **browser binary** only — no privilege required —
and a genuinely missing system library surfaces at launch with Playwright's own
message naming the exact `apt-get install` line. The **system libraries** are
per-machine and privileged, so they belong to one-time setup: on a fresh Linux
host run `sudo npx playwright install --with-deps chromium` once, alongside
`npm ci`.

## For a pinned external tool, run it ephemerally rather than installing it

`uv tool run --from "cfn-lint==1.22.3" cfn-lint …` fetches CI's exact version,
needs no root, and leaves whatever the developer already has installed untouched.
Both obvious alternatives fail one of the three constraints:

| form | what goes wrong |
|---|---|
| bare `cfn-lint …` | exits **127** on a fresh checkout — the `dev` dependency group does not carry it, CI installs it in its own step — so it blocks every PR |
| `pip install "cfn-lint==1.22.3"` first | privilege-free, but **downgrades** the developer's own copy as a side effect of a review gate |

## Prefer the workflow's command over the package's convenience script

They are not always the same check. `npm run typecheck` runs `tsc --noEmit`, and
the root tsconfig is `files: []` plus project references, so it checks **zero
files and always passes**. CI type-checks with `tsc -b` for exactly that reason. A
floor built from `package.json` script names would carry a gate that is enforced
in appearance only — strictly worse than a missing gate, because nobody goes
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

`test/test_prepare_pr_profiles.py` holds the floor to `ci.yml` so that CI gaining
a blocking scan fails a test rather than surfacing as a review round on a later
PR. A check written as a bare binary (`cfn-lint`, `mypy`, `flake8`) is invisible to
a `scripts/`-and-`npm run` scan, so the parity test also enumerates the **tool
names** `ci.yml` invokes and makes each one either a gate or a named exemption.

Strip comment-only lines before any such scan. `ci.yml` explains in prose why the
Type check step uses `tsc -b` and *not* `npm run typecheck`, so a naive grep for
`npm run <script>` "finds" a script CI deliberately avoids — the same trap as
reading a ratchet number out of a comment.

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
