# The i18n gate chain

The dashboard ships in twelve languages. This doc covers the **gates**: what runs,
what can fail a PR, what only reports, and the rule that governs relaxing a
ratchet. The authoring rules (how to add a catalog key, the `src/i18n/format.ts`
seam, the glossary) live in the frontend docs under `website/`.

Run the whole chain locally before pushing:

```bash
cd website && npm run i18n:check
```

## Where each gate runs in CI

| CI job | Step | What it runs |
|---|---|---|
| `frontend-lint` | Check i18n extraction, key references and plurals | `npm run i18n:check` (the runner below) |
| `frontend-test` | Unit tests | `npx vitest run --coverage`, which includes the diff-scoped `localeFormatting.test.ts` gates and the catalog duplicate-key guard `duplicateKeys.test.ts` (see below) |
| `e2e` | i18n render-time gate | `npm run i18n:render` (`scripts/check-i18n-render.mjs --build`) |

The render gate lives in the `e2e` job to reuse the Chromium install that job
already pays for. It needs no gateway, no token and no backend: it serves the
build over loopback and answers every `/api/**` call from fixtures.

## `npm run i18n:check` is a RUNNER, not an `&&` chain

`scripts/i18n-check.mjs` spawns eight scripts, keeps every byte of their output,
then reports the twelve checks they contain in one table.

An `&&` chain short-circuits. With twelve checks that means a PR only ever learns
about its **first** failure, fixes it, pushes, waits for another full frontend-lint
round, and discovers the next. Up to twelve rounds for one PR, over independent
measurements of the same tree. The runner reports all of them once, and folds each
script's raw output into its own collapsed CI group so nothing is lost.

Two structural properties of the runner:

- **The verdict is a pure function over plain data**, in
  `scripts/lib/i18n-gate-table.mjs` (`resolveRows` + `verdict`), not inline in the
  runner. That is so the exit code is unit-testable rather than discovered in CI.
  The pinned invariant is: **a non-zero child always fails the run**, whatever the
  rows say. Row states exist for ATTRIBUTION (telling the author which check to
  look at), never for deciding whether the step passes. The first version decided
  by "some row is FAIL" and exited 0 while a script had crashed, because every row
  it owned had been classified as an unknown.
- **A script that cannot run is exit 2, not a finding.** A spawn error, a signal
  kill, or a stdout-buffer overflow means the table would be built from truncated
  text a regex might read as a pass, so the runner bails out instead.
  Symmetrically, a script that exits 0 while printing nothing a row recognises is
  `MISSING` and **fails** the step: a check that quietly stopped measuring is the
  defect the runner exists to remove.

The `--check` flag on the two codemods is deliberately not `--dry-run`: dry-run
reports and exits 0, which would make the step decorative. An unrecognised flag
makes those scripts refuse to run rather than fall through to their destructive
conversion path.

## The table

Thirteen checks over eight scripts. The split is by the only question an author
has: **is this mine to fix?** A `diff`-scoped finding is on a line this branch
wrote or in a file it touched. A `repo`-scoped finding is a whole-repo measurement
the branch may simply have inherited.

| Check | Scope | Enforcement | Catches |
|---|---|---|---|
| `[added-lines]` | diff | zero tolerance | a user-visible literal on a line THIS BRANCH WROTE |
| `[vs-base]` | diff | zero tolerance | a file you touched holds more untranslated strings than at the base |
| `[source-strings]` | diff | zero tolerance | badly shaped copy among only the English keys your branch adds |
| `[changed-values]` | diff | zero tolerance | catalog QA over every value the branch added or changed, all languages |
| `[key-refs]` | repo | hard zero | a `t('key')` naming a key that does not exist |
| `[plurals]` | repo | hard zero | a plural suffix concatenated outside the translation call |
| `[pseudolocale]` | repo | hard zero | `en-XA.json` stale relative to its generator |
| `[dnt]` | repo | hard zero | a do-not-translate term respelt in a shipped catalog |
| `[manifest-sync]` | repo | hard zero | a built-in `app.json` string and its `en.json` value stopped matching |
| `[dynamic-keys]` | repo | report only | a call site whose key cannot be resolved statically |
| `[extractable]` | repo | report only | a literal in markup the codemod could have extracted |
| `[untranslated]` | repo | report only | per-file ceilings over the frozen untranslated debt |
| `[allcaps]` | repo | report only | untranslated strings inside ALL-CAPS module constants |

### Only two kinds of check can fail the step

1. **A diff-scoped one.** A finding on a line you wrote is yours and there is no
   number to raise.
2. **A whole-repo HARD ZERO.** A hard zero has no ceiling to inherit, so a new one
   is always somebody's diff. `[key-refs]` is necessarily whole-repo: a reference
   and its catalog entry are edited independently, so a catalog-only rename dangles
   references in files the PR never opens. It also needs no base ref, so it cannot
   skip itself. The user-visible symptom it prevents is a raw dotted key rendered
   into the UI, because a missing key is returned as its own fallback.

Everything else is **report only**. A stored whole-repo total is written by
whichever branch measured it last, so another branch can push it past its number
without touching your files, and then the failure names no diff anyone can fix.
That is not theoretical: a main-branch count sat above its own freshly-set ceiling
within minutes, and every open PR inherited the red. The worst case failed when a
file **improved**, so fixing something broke CI until a new count was committed to
a file every branch shares.

The numbers are still measured and still printed on every run, because they are
how the remediation gets planned. They just do not gate.

## Diff-scoped gates need a base commit

Four checks read the branch against a base ref, supplied as `I18N_BASE_REF`:

- On a pull request it is `github.event.pull_request.base.sha`, the commit the
  **merge ref** was computed against. NOT `origin/<base.ref>`: the branch tip is a
  moving target measured at step time while the checked-out tree is a snapshot from
  job start, so anything that lands on main in between appears only on the base
  side and is charged to every PR running in that window. `base.sha` makes the two
  sides consistent by construction.
- On a push to main it is `github.event.before`, the commit the push replaced, so
  a merge that only breaks in combination with another merge is still charged to a
  diff.

`actions/checkout` fetches depth 1, so the base is not present and a `git show`
against it would fail. `.github/scripts/resolve-i18n-base.sh` fetches exactly that
one ref, and the three call sites `|| exit 1` on it.

**A gate that cannot run must fail, not pass.** `check-source-strings.mjs` skips
itself and exits 0 when the base ref is unreadable, and a failed fetch used to sail
past the step with a printed "OK: skipped" while the new-key gate was silently
disabled for the whole PR. An unavailable base ref is a CI problem to fix, not a
reason to stop checking. The render scanner and the diff-scoped vitest gates exit
non-zero on an unresolvable configured ref for the same reason.

A local run normally has no `I18N_BASE_REF`, and the table says so
(`NOT RUN: no base commit supplied`) rather than accusing four working checks of
having stopped measuring.

## Why a separate ESLint invocation

`eslint.i18n.config.js` is a deliberately separate ESLint run with
`--no-inline-config`, so an i18n finding cannot be silenced with an inline comment.
`eslint.i18n.strict.config.js` is the config the diff-scoped gates use;
`check-i18n-strings.mjs` drives it as a second invocation.

Keeping it separate also keeps the budgets meaningful. `no-literal-string` at
`mode: 'all'` reports thousands of findings, and folding those into
`frontend-lint`'s general `--max-warnings` budget would make an i18n regression
indistinguishable from a new `no-explicit-any`.

Do not treat a green chain as proof of coverage. `eslint.i18n.config.js` names its
own false-negative classes inline, beside the shape regexes that cause them:
single-word copy (`'saved'`, `'done'`), prose containing a hyphen or a digit
(`'read-only mode'`, `'2 items'`), a bare Tailwind-utility pair with no hyphen, and
snake_case copy. A shape that excludes Tailwind class strings cannot also catch
those. `mode: 'all'` is used rather than a narrower mode for the same reason: a
narrower mode does not report fewer false positives, it reports fewer findings of
every kind.

## The ratchet rule

**A ratchet may only be upward-only if a DIFF-SCOPED gate covers the same
defect.**

A frozen count says "this much debt is tolerated". It cannot tell "one fixed" from
"one fixed and one broken". So a count is allowed to stop failing on improvement
only when something else fails on the regression regardless of the count, and that
something has to be anchored to the diff, because a gate anchored to a committed
number can always be re-snapshotted past.

Two corollaries:

- **Never re-snapshot a ledger just because your change improved it.** Leaving it
  alone is correct and it keeps the file from conflicting with every other branch
  in flight.
- **Keep a relaxed ceiling tight against its live count.** Upward-only means an
  improving branch never has to edit the number, so tightening costs one line once,
  and each relaxed gate reports its own decrease so drift shows up in CI output
  instead of being discovered later. Slack in a ceiling is slack a merge-conflict
  resolution can spend.

**The goal for every number on either list is zero.** That is what makes an
upward-only ceiling a convergence rather than a loosening: at 0 there is nothing
left to decrease, so "only an increase fails" IS the strict gate, and the ledger
can be deleted.

Why this rule exists rather than full bidirectional ratchets: each number lives in
a single generated ledger, so demanding that every improving branch re-snapshot it
made the ledger conflict between branches whose source edits were disjoint, and
made every merge to main invalidate the number in every other open branch. It also
did not achieve what it looked like, because the bypass was one `--update` flag: a
commit shipped a new app with 113 untranslated strings while moving the total
upward, green, under the fully bidirectional gate. That same commit fails
`[added-lines]` today, so moving enforcement to the diff was a net tightening.

A few ratchets stay **exact in both directions** because no diff-scoped check can
replace an AST-counted site: improving one of those requires lowering its number in
the same change. Being exact, they break on unrelated drift in main, so expect to
re-measure when you rebase. If you add a diff-scoped gate covering one of them, it
may be relaxed.

## The render-time gate: what a source scan structurally cannot see

Every check above reads **source** (an ESLint pass over `src`) or **catalog JSON**.
Three defect classes are invisible to all of them, by construction and not by
oversight:

| Defect | Why the static checks miss it |
|---|---|
| a string that was never extracted | it is ordinary TypeScript; a template literal assembling a duration reads as arithmetic |
| a sentence assembled from several keys | it is several *correct* translation calls; only the rendered line shows the seam |
| English in `title` / `aria-label` / `placeholder` | attribute text never enters the text flow, so nothing that walks prose sees it |

`npm run i18n:render` renders the real built SPA under the **`en-XA` pseudolocale**,
where the generator has already guaranteed the two properties the whole check rests
on: every catalog value is wrapped in `[` … `]`, and every ASCII letter outside a
preserved region is accented. So inside one inline run, a `]…[` seam **is** a
surviving concatenation, and plain Latin **is** text that never reached a catalog.

Five things to know before touching it:

1. **It builds its own bundle with `NODE_ENV=development`.** `en-XA` is DEV-only in
   three independent places, all keyed on `import.meta.env.DEV`. `vite build --mode
   development` is not enough, because Vite derives DEV from `NODE_ENV`, not from
   `--mode`. Get this wrong and the gate does not fail: it renders English and
   passes. `assertPseudoActive()` exists so that can never be silent.
2. **A crashed surface is exit 2, not findings.** A wrong-shaped fixture makes the
   error boundary replace the panel with its own English message, and a naive scan
   would report that as untranslated copy. Any uncaught page error aborts the run.
3. **DNT integrity runs only in real locales.** The pseudolocale accents
   do-not-translate terms too, so asserting them under `en-XA` would report every
   one as mangled. Catalog-value comparison (`[dnt]` in the chain above) covers all
   shipped locales instead, at no render cost.
4. **`[vs-base]` decides the run; the debt record decides only when there is no
   base.** The gate renders the BASE commit with the same scanner and fails on any
   per-surface increase, reading no committed number, so there is nothing to
   re-snapshot and nothing to absorb a regression with. `src/i18n/render-baseline.json`
   is a debt record, printed above and below reality alike. It decides a run in
   exactly one case: a run with no base commit, where it is all that is left and the
   alternative is exiting 0 having checked nothing. `scripts/lib/render-verdict.mjs`'s
   `totalIsFallback` is that flag, and it is deliberately NOT set by the three
   opt-outs (`--no-vs-base`, `--surface`/`--locale`, `--update`), which asked for a
   report and keep getting one.
5. **Fixture values are digit-shaped, except where the fixture IS the subject.**
   Anything word-shaped in the fixture overrides renders into the page and is
   indistinguishable from untranslated product copy, so boot payloads use
   `'0001'`-style placeholders. The deliberate exception is app-manifest metadata
   (`displayName`, `description`, `highlights[]`, `tags[]`), which the App Store
   components interpolate raw: bare Latin at those nodes is the finding, and a digit
   placeholder would render the surface while hiding the defect it exists to show.
   Read those numbers as a fixed structural probe, not as the size of the debt,
   because the scanner counts per word.

Known limits of the render gate, named rather than papered over:

- **A PR whose base is not `main` never runs `ci.yml` at all**, because
  `pull_request: branches: [main]` filters on the base branch. A stacked PR gets no
  render gate until it is rebased.
- **`main` verdicts are sampled, not per-commit.** The concurrency group permits
  one pending run and GitHub evicts it when a newer one queues, so a commit whose
  run was evicted has its delta fall between two `github.event.before` boundaries
  and is never diffed.
- **Truncation measurement is not fully deterministic.** It compares a measured
  truncation ratio against a budget, so a label within roughly 10% of its budget can
  flip between runs as font metrics settle. Re-run before chasing a `[vs-base]`
  failure on that signature alone, and if a label is genuinely near its budget,
  widen the container rather than the budget.
- **`[vs-base]` is blind to dependency drift by construction.** The base bundle
  symlinks HEAD's `node_modules` rather than installing the base's, so a lockfile
  bump is measured with the new dependency set on both sides.

## The one uncovered population

`[dynamic-keys]`. The other three report-only demotions handed their population to
`[added-lines]`, which counts untranslated *literals*. A dynamic site (a
translation call whose key is a variable) has no literal to count, so
`[added-lines]` cannot see it, and `[key-refs]` is static-only by construction:
the whole point of the dynamic ledger is that those sites are the ones it cannot
verify. The only residual cover is the render gate's `[vs-base]` under `en-XA`, and
only for surfaces its harness actually mounts. Closing it needs a base-ref-anchored
per-file dynamic-site diff, the same shape as `[vs-base]`.

## Catalog duplicate keys (`duplicateKeys.test.ts`)

Runs in `frontend-test`, outside the `i18n:check` runner above, because it is a
plain vitest assertion over the catalog files rather than a diff-scoped gate.

It fails on any key defined **twice inside one object** in any
`src/i18n/locales/*.json`. Every catalog on disk is covered, so adding a language
needs no edit.

Why it exists: `ja.json` once carried eleven such keys in
`components.kiroPrerequisiteGate`. `JSON.parse` keeps the LAST occurrence, so the
earlier value was dead weight no reader ever saw — and the file could not be
safely round-tripped, because any tool that reserialised it silently DROPPED the
shadowed translations. That is exactly how they were eventually removed: as an
incidental side effect of an unrelated feature PR, with eleven Japanese strings
changing value inside a diff nobody was reviewing for translation content.

Two properties make it work, and both are asserted directly in the same file:

- It scans **raw text**, not `JSON.parse` output. Parsing collapses duplicates
  before any reviver runs, so a parse-based check passes on a broken catalog.
- It compares keys by their **decoded** value, because `"n\u0061me"` and
  `"name"` are the same key per ECMA-404. A byte-wise comparison would call them
  distinct and wave a real duplicate through.

