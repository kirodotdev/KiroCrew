# KiroCrewWebsite — Agent Guidelines

This is the **single source of truth** for frontend work in `website/` (the
backend has its own `AGENTS.md` at the repo root). KiroCrew dashboard frontend —
React + TypeScript SPA. Production builds output to `website/dist/`, staged into
`KiroCrew/src/kiro_crew/static/dist/` and served by the Python backend.

## Stack

React 18, Redux Toolkit (`@reduxjs/toolkit`), React Query (`@tanstack/react-query`), React Router v7 (`react-router-dom`), Framer Motion (`framer-motion`), Tailwind CSS 3, Lucide React (`lucide-react`), DOMPurify, highlight.js, Monaco, TypeScript, Vite 5.

## Build / dev / test

```bash
npm install
npm run build        # tsc -b + vite build → website/dist   (this is the real typecheck)
npm run dev          # Vite dev server on :3000, proxies /api to backend :5476
npm run check        # typecheck + lint + tests
npm run test         # vitest (website + electron)
npm run lint         # eslint src
```

After building, stage the bundle so the backend serves it:
`cp -R website/dist ../src/kiro_crew/static/dist`.

**Gotcha — `npm run typecheck` is a FALSE PASS.** It runs `tsc --noEmit`, but the
root `tsconfig.json` has `files: []` + project references, so `--noEmit` checks
**zero files** and always passes. **Always use `tsc -b`** (which `npm run build`
runs) to actually type-check. Don't trust a green `npm run typecheck` alone.

**Gotcha — localStorage polyfill in tests.** `website/integration/setup.ts`
installs an in-memory `localStorage`/`sessionStorage` polyfill. Required: Node
25's native `--localstorage-file` storage shadows jsdom's spec-complete `Storage`
and lacks `.clear()`. The polyfill puts methods on `Storage.prototype` so
quota-error spies still work — don't remove it or move methods off the prototype.

## This is a public OSS fork — don't reintroduce internal couplings

When changing the frontend, **do not reintroduce**:
- Build/infra: `npm-pretty-much`, Brazil, AIM, CodeArtifact registries,
  Coverlay/jscpd-as-a-build-gate. The public build is plain **npm + Vite**;
  `website/.npmrc` pins the **public** registry (`registry.npmjs.org`).
- Identity/telemetry: live Cognito pools or RUM app ids (`src/rum.ts` is a no-op
  telemetry stub — keep it inert), `aws-rum-web`. The backend is KiroACP
  (`kiro-cli`) only; the frontend never needs an ACP adapter as a web dependency.
- Removed product surfaces: internal feature-app pages/tabs/API-client methods and
  the credential-TTL card on the Overview page. They were deleted with their
  backend; don't re-add the UI (a downstream edition re-adds them additively via
  the extension seams below — never by editing core).

> Stale references: `website/Config` and `website/AUTOSDE.yaml` are leftover
> internal files not used by the public build — ignore them, and treat any
> "brazil-build"/"Coverlay" mentions as historical.

## Browser support

Chrome, Firefox, Safari, Edge. Use standard Web APIs only; guard browser-specific
ones (e.g. `typeof Notification !== 'undefined'`).

## Icons: Lucide Only, No Emoji

Use `lucide-react` for all icons with `className="lucide-inline"` for inline placement. The `lucide-inline` CSS class handles sizing and vertical alignment so icons stay on the same line as adjacent text.

```tsx
// Good
import { Search, AlertTriangle, Inbox } from 'lucide-react'
<button><Search className="lucide-inline" /> Search</button>

// Bad
<button>🔍 Search</button>
<button><Search size={13} /> Search</button>  // use lucide-inline, not size={}
```

Do NOT use emojis, `size={N}` props, `inline-flex` wrappers, inline SVG icon components, or hand-rolled SVG paths. Emojis in rendered UI are a bug — replace with the nearest Lucide equivalent.

**Exceptions** (emoji allowed):
- `src/components/EmojiPicker.tsx` — emoji catalog component
- `src/pages/scenes/` — decorative scene elements
- `src/hooks/useTheme.ts` and `src/components/themeEditor.tsx` — theme display names
- `src/components/folderEmoji.ts` and `src/components/FolderConfigModal.tsx`
  folder icons — a folder's icon is a single emoji the backend auto-generates
  (and the user may override in the folder create/settings modal). It is folder
  *data* rendered by `FolderGlyph`, not a status/UI icon, so `FOLDER_EMOJIS`
  (the curated picker grid) and the free-emoji input are intentional.

See `AUTOSDE.yaml` rules `use-lucide-icons` and `no-emoji-as-icons` for enforcement.

## Internationalization (i18n)

The dashboard is translated. **Never hardcode a user-facing English string** —
route it through the catalog or it will render as English in every language.

- **Inside a component body:** `const { t } = useTranslation()` then `t('key')`.
  Preferred for new code — it subscribes to language changes.
- **Anywhere a hook is illegal** (render callbacks, plain helpers, non-component
  modules): `import { i18nT } from '../i18n/t'` then `i18nT('key')`. It reads the
  current language but does NOT subscribe. `LanguageProvider` forces a re-RENDER
  of the tree on a language change (via `cloneElement`, which defeats React's
  referential-equality bailout) — it deliberately does **not** remount, because
  remounting discards in-flight state. So an `i18nT()` call in RENDER position
  re-resolves, but a value baked into a `useMemo` whose deps exclude the language
  does **not**: put such lookups behind a getter or a function, never in the
  memoized value itself.
- **Never `import { t } from 'i18next'`** — `t` is a very common local identifier
  here (`.map(t => …)` over tabs/turns/tasks/themes) and a bare `t` gets shadowed,
  turning the call into `SomeObject(...)`.

Catalogs live in `src/i18n/locales/`:

| File | Owner |
|---|---|
| `en.json` | **generated** — `node scripts/i18n-codemod.mjs` rewrites it wholesale. Never hand-edit. |
| `en.manual.json` | hand-authored English with no source literal to extract (e.g. the language picker's own labels). |
| `<tag>.json` | one per translation, key set must match `en.json` exactly. |

Shipped: `en`, `zh-CN`, `hi`, `es`, `fr`, `bn`, `pt`, `ru`, `de`, `it` — ordered by global
speaker count, which is also the picker order. **Right-to-left languages
(Arabic, Urdu) are intentionally not shipped**: the layout is built from
physical-direction utilities (`pl-*`, `left-*`, `text-left`) and unmirrored
directional icons, so an RTL catalog would render correct text in a visibly
wrong shell. Adding one needs `dir="rtl"` + a logical-property conversion first
(`ps-*`/`pe-*`, `start-*`/`end-*`), not just a catalog.

Adding a language is a **data change** — three edits, no component or test changes:

1. `locales/<tag>.json` (same key set as `en.json` + `en.manual.json`)
2. one entry in `SUPPORTED_LANGUAGES` (`src/i18n/languages.ts`)
3. one line in `CATALOGS` (`src/i18n/index.ts`)

To translate the corpus, shard it rather than doing one pass — `node
scripts/i18n-shard.mjs split <dir>` writes flat key→value shards. Keep shard
dirs OUTSIDE the worktree (Rule 9 — a dirty tree blocks worktree pruning).

**Reassemble with `node scripts/i18n-translate.mjs merge <baseDir>`, never with
`i18n-shard.mjs join`.** `join` rewrites the catalog from shards keyed off the
**English** corpus, so any form the locale has and English does not is silently
dropped — a measured round-trip removes 108 lines from `ru.json` and 45 keys from
each of `es`/`fr`/`pt`/`it`, all `_few`/`_many` CLDR plural forms. It also cannot
accept the locale-specific plural keys `emit` asks for, because it validates
against the English key set. `merge` is insert-only by default and preserves
both. Never hand-assemble a catalog either — `merge`'s fail-closed checks are
what stop English text shipping disguised as a translation.

`i18n-translate.mjs` is the whole pipeline, and it is deliberately offline — it
writes prompts and validates answers, but sends nothing:

| Command | Does |
|---|---|
| `plan [pathPrefix]` | what still needs translating, read from `untranslated-baseline.json` |
| `emit <baseDir> [--locales a,b]` | writes one prompt per (locale, shard), including the per-locale plural forms that locale requires |
| `verify <baseDir> --locale <tag>` | every rule that can be decided mechanically — run it before `merge` |
| `merge <baseDir> [--overwrite]` | insert-only reassembly |

`split` also writes `shard-NN.context.json` beside each shard, carrying the
translator context from `src/i18n/en.context.json` for the keys in that shard.
**Read it before translating the shard** — it is the only thing that tells you
`KB` is kilobytes and not "knowledge base", that `K` is a keyboard key you must
leave alone, and that `Run` is the verb. If a short or ambiguous string has no
entry, add one to `en.context.json` rather than guessing twice. `split` warns and
emits no context files if the sidecar is not present.

**Don't pin a test fixture to a language you might later ship.** Assertions like
"`fr` is unsupported, so it falls back" silently invert the moment French ships.
This has now bitten twice — `fr` when French shipped, then `de-DE` in
`detect.test.ts` when German shipped. Use a language the project has no plans for
(`ja`, `ko`) for negative cases, and derive positive cases from
`SUPPORTED_CODES` so a new language is covered automatically.

### Counts: never concatenate a plural suffix

**Never append a plural marker outside `i18nT()`.** This pattern is a bug:

```tsx
// WRONG — renders 会话s, 3 sesións, এজেন্টs
{n} {i18nT('pages.overview.memoryTab.session')}{n === 1 ? '' : 's'}
```

The `s` is added *outside* the translate call, so **no catalog value can fix it**.
English plural rules also aren't universal: Russian needs 4 forms, Spanish 3,
Chinese 1. Pass the count instead and let i18next pick the form via
`Intl.PluralRules`:

```tsx
// RIGHT
{i18nT('pages.overview.memoryTab.session', { count: n })}
```

The count goes *inside* the string (`"{{count}} sessions"`) so a translation can
place the number where its grammar requires. Add one catalog key per category
the language actually has (`_one`/`_other`, plus `_few`/`_many` where needed) —
`catalogParity.test.ts` checks each language against its OWN categories and
fails on a missing or unreachable form.

`scripts/i18n-plural-codemod.mjs` performs the conversion and maintains
`src/i18n/pluralKeys.json`, the registry of pluralized keys. **Run it with
`--check` to verify none crept back in** — an upstream sync reintroduced this
exact pattern once. Which keys are plural comes from that registry, never from
sniffing a `_one`/`_other` suffix: real copy ends in those words
(`panel_to_add_one` = "panel to add one.").

### One key, one meaning

**Never reuse a key across two grammatical roles.** English collapses
distinctions other languages keep, so a shared key forces translators to guess:

- `schedulePage.type` was both a table column header ("Type", the noun) *and*
  the imperative verb in "Type `delete` to confirm". es/pt/ru picked the noun
  and broke the instruction; zh-CN/hi/bn picked the verb, so the column header
  read "please enter". Now two keys, the verb one named `type_verb_to_confirm`.
- `artifactDeployPage.webapp` was both a type badge and a counted phrase.

If a value's part of speech isn't obvious from the key, **put it in the key**.

**A literal token the user must type must never be a catalog value** — keep it a
code constant (see `BULK_DELETE_TOKEN`), or translating it makes the action
impossible to complete. `destructiveConfirm.test.ts` pins this.

**Never dedupe translation work by English value alone.** The corpus has ~3.9k
keys but only ~3.2k distinct English strings, so translating each unique string
once is tempting — and it silently merges keys whose shared English word carries
two different meanings. Adding de/it this way collapsed `Open` (verb "Apri" vs.
issue status "Aperta"), `Review` (verb vs. noun), `Plan`/`Schedule` (button vs.
label), and `Type` — the last caught by `destructiveConfirm.test.ts`, the rest
only by auditing. If you dedupe, afterwards **diff each duplicate group against
the already-shipped catalogs**: where several existing languages chose different
words for one English string, English is hiding a distinction and the merged
value is wrong.

`catalogParity.test.ts` generates its cases from `SUPPORTED_LANGUAGES` and reads
catalogs from the runtime `CATALOGS` map, so the new language automatically gets
its own key-parity, placeholder-preservation, and no-empty-value tests. Miss one
of the three edits and CI fails naming the gap; it can't silently ship as
English. **There is no allowlist**, so every language has to land in the same
commit — this is what makes each new language add marginal cost to every
subsequent i18n change.

### Formatting follows the app language, not the browser

`d.toLocaleDateString()`, `d.toLocaleDateString([])` and
`d.toLocaleTimeString(undefined, { … })` all mean the same thing: **format in the
host locale**. They ignore the language setting entirely. `LanguageProvider` sets
`<html lang>`, but `<html lang>` has no effect on `Intl`, so a dashboard running
in Chinese on an en-US browser rendered `7/30/2026` inside Chinese UI.
`a.localeCompare(b)` has the same flaw for ordering — the sort order of a list of
names silently depended on the browser.

Route it through `src/i18n/format.ts` instead. It is the **seam**: the only module
allowed to resolve a locale, and it reads the active language per call, so a
language switch takes effect without a remount.

```ts
import { fmtDate, fmtRelative, compareText } from '../i18n/format'

fmtDate(iso)                 // not new Date(iso).toLocaleDateString()
fmtRelative(ts)              // not a hand-written "3d ago"
names.sort(compareText)      // not (a, b) => a.localeCompare(b)
```

Each helper carries its own preset, and the options type omits the field the
preset owns — `fmtDate` is already `dateStyle: 'medium'`, so pass
`fmtDateFields(value, { … })` when you need explicit components instead.

Available: `fmtNumber`, `fmtPercent`, `fmtCurrency`, `fmtUnit`, `fmtDate`,
`fmtTime`, `fmtDateTime`, `fmtDateFields`, `fmtWeekday`, `fmtRelative`, `fmtList`,
`collator`, `compareText`, plus `activeLocale` and `toDate`.

`localeFormatting.test.ts` is an AST ratchet over the remaining un-migrated
calls. **Naming a locale IS the opt-out**, which is why there is no allowlist
file:

```ts
d.toLocaleDateString()                  // finding
d.toLocaleDateString([])                // finding — 2 args, still the host locale
d.toLocaleTimeString(undefined, opts)   // finding
a.localeCompare(b)                      // finding
d.toLocaleDateString('en-US', opts)     // allowed — the pin is visible to a reviewer
a.localeCompare(b, 'en-US')             // allowed
a < b ? -1 : 1                          // allowed — byte order, not matched at all
```

A machine-parse site (an ISO timestamp sort, a filesystem path sort, a value fed
to `Date.parse` on the other side) has to state its pin **in the code**, not in a
registry a reviewer has to go look up. Two things the gate cannot see: a pinned
locale can still be the *wrong* locale, and `toFixed`/`String(n)`/`join(', ')` are
not locale-aware APIs at all, so nothing syntactic detects them.

Do not hand-format numbers. Latin digits are wrong for `bn`, and for `ar-EG`,
`ar-SA` and `fa` if they ever ship. `Intl.DurationFormat` is unavailable on Node
20, so durations go through `fmtUnit`.

### Script fonts: keep the aliases first

`index.css` declares `@font-face` aliases carrying `unicode-range` for Han,
Devanagari and Bengali, collects them into `--script-fallbacks` and
`--script-fallbacks-mono`, and puts **that token first** in `--font-body` and
`--mono`. The range restriction is what makes this safe: the alias is never
consulted for Latin, so it cannot change Latin metrics or leading, and it is a
no-op when the face is not installed.

**Do not reorder those stacks or drop the token when adding a family** — moving a
Latin family in front silently returns zh-CN/hi/bn to whatever the platform picks
for a missing glyph. `scriptFonts.test.ts` pins the `:root` tokens, every
declaration site, and the ordering.

### The gates

`npm run i18n:check` is the whole chain, and it runs in CI as part of
**Frontend Lint & Type Check**. Run it locally before pushing.

It is a RUNNER (`scripts/i18n-check.mjs`), not a `&&` chain, and that matters: `&&`
short-circuits, so a PR only ever learned about its FIRST failing gate and paid another
~5-minute CI round to discover the next. The runner executes all six scripts, then
reports the eleven checks they contain in one table. Each script's raw output is folded
into its own collapsed group, so nothing is lost.

The table is split by the only question an author has — **is this mine to fix?**

| Check | Scope | Fails when | Catches |
|---|---|---|---|
| `[added-lines]` | diff | any | a user-visible literal on a line THIS BRANCH WROTE |
| `[vs-base]` | diff | any | a file you touched holds more untranslated strings than at the base |
| `[source-strings]` | diff | any | poor copy among **only the keys your branch adds** |
| `[changed-values]` | diff | any | catalog QA over every value the branch added or changed, all languages |
| `[key-refs]` | repo | any | a `t('key')` naming a key that does not exist — renders the raw key to a user |
| `[plurals]` | repo | any | a plural suffix concatenated outside `i18nT()` |
| `[pseudolocale]` | repo | any | `en-XA.json` stale relative to its generator |
| `[dynamic-keys]` | repo | never (report) | a call site whose key cannot be resolved statically |
| `[extractable]` | repo | never (report) | a literal in markup the codemod could have extracted |
| `[untranslated]` | repo | never (report) | per-file ceilings over the frozen debt in `untranslated-baseline.json` |
| `[allcaps]` | repo | never (report) | strings inside ALL-CAPS module constants, which the per-file ceilings do not see (`untranslated-strict-baseline.json`) |

**Only two kinds of check can fail this step.** A `diff`-scoped one, because a finding on
a line you wrote is yours and there is no number to raise. And a whole-repo **hard zero**,
because a hard zero has no ceiling to inherit — a dangling key reference or a stale
`en-XA.json` is always somebody's diff.

Every other whole-repo number is **report only**. A stored total is written by whichever
branch measured it last, so another branch can push it past its ceiling without touching
your files — and then the failure names no diff anyone can fix. That is not theoretical:
`main` sat at 1120 against its own ALL-CAPS ceiling of 1118 within minutes of #1099
setting it, and every open PR inherited the red. The same shape turned eleven PRs red in
the `PrivacyPanel` incident and again in `projects.latent`. `[dynamic-keys]` was the worst
of them, failing when the number went DOWN as well as up, so improving a file broke CI
until someone committed a new count to a file every branch shares.

The debt is still measured and still printed on every run — the numbers are how the
remediation gets planned. They just do not gate. What enforces the same populations is the
diff-scoped half: `[added-lines]` covers everything `[untranslated]`, `[extractable]` and
`[allcaps]` count, against the base ref rather than a committed number.

**One population is now uncovered, and it is `[dynamic-keys]`.** The other three demotions
handed their population to `[added-lines]`, which counts untranslated *literals*. A
dynamic site — `i18nT(labelKey)` where the key is a variable — has no literal to count, so
`[added-lines]` cannot see it, and `[key-refs]` is static-only by construction: the whole
point of the dynamic ledger is that those sites are the ones it cannot verify. The only
residual cover is the render gate's `[vs-base]` under `en-XA`, and only for surfaces its
harness actually mounts. So this demotion traded a merge-conflict generator — a ratchet
that failed when a file IMPROVED — for an uncovered class, knowingly. Closing it needs a
base-ref-anchored per-file dynamic-site diff, the same shape as `[vs-base]`; tracked in
#1004.

`eslint.i18n.config.js` is a deliberately separate ESLint invocation with
`--no-inline-config`, so an i18n finding cannot be silenced with an inline
comment. It documents its own false-negative classes at the top — single-word
copy like `'saved'`, and prose containing a hyphen or a digit — because a shape
that excludes Tailwind class strings cannot also catch those. **Do not treat a
green gate as proof of coverage.** Untranslated buttons have twice been found in a
screenshot while every static gate passed; the pseudolocale and a real render are
the ground truth.

**Two kinds of gate, and the difference decides what you edit.**

*Diff-scoped, zero tolerance, no stored state.* These read your diff against the
base ref and cannot be bought past — there is no number to regenerate:
`[added-lines]` fails on any user-visible literal sitting on a line **this branch
wrote**, including copy you did not author but merely shared a line with;
`[vs-base]` fails if a file you touched gained untranslated strings; and
`[changed-values]` runs catalog QA over every value you added or changed, in all
languages. `[added-lines]` is the real coverage gate — wrap the literal, or exclude
it by shape in `eslint.i18n.config.js` if it is genuinely not copy.

*Ledgers.* Never re-snapshot one just because your change improved it — leaving it
alone is correct, and it keeps the file from conflicting with every other branch in
flight. Only ONE still fails when it is exceeded: the `CEILINGS` map in `qa.test.ts`.
The other three are **report only** — `untranslated-baseline.json` (a per-file ceiling,
and the remaining worklist), the `--baseline=N` literal in
`scripts/lib/i18n-gate-table.mjs`, and `dynamic-keys-baseline.json`. A whole-repo total
is written by whichever branch measured it last, so another branch can push it past its
number without touching your files; the diff-scoped checks enforce the same populations
against the base ref. The goal for each is still zero, at which point the ledger is
deleted and its gate becomes unconditional.

A few AST ratchets are still exact (`.toBe`) because no diff-scoped check can
replace an AST-counted site: the `BASELINE` consts in `deadKeys.test.ts`,
`localeFormatting.test.ts`, and `unitLiterals.test.ts`. Lower these when you
improve them — never raise them. Being exact, they break on unrelated drift in
`main`, so expect to re-measure when you rebase.

Guard tests that must stay green: `catalogParity.test.ts` (cross-language key
parity), `englishIdentity.test.ts` (catalog holds real prose — no encoded HTML
entities, raw keys, or JSX fragments), `detect.test.ts` (resolution precedence),
`LanguageProvider.test.tsx` (persistence + cross-tab sync), `renderSwitch.test.tsx`
and `memoBailout.test.tsx` (a language change actually repaints, including through
a `memo()` boundary), `moduleLevel.test.ts` (`i18nT` never evaluated at module
load), `dynamicKeys.test.ts` (every key is a static literal),
`contextSidecar.test.ts`, `glossary.test.ts` (DNT terms verbatim in every
language), `pseudolocaleBundle.test.ts` (`en-XA` in dev builds, absent from
production), and one `style/<lang>Style.test.ts` per language, each citing the
clause it enforces in that language's `style/<lang>.md`.

### A ratchet may only be upward-only if a DIFF-SCOPED gate covers the same defect

That is the rule. A frozen count says "this much debt is tolerated"; it cannot tell "one
fixed" from "one fixed and one broken". So a count is allowed to stop failing on
improvement **only** when something else fails on the regression regardless of the
count — and that something has to be anchored to the diff, because a gate anchored to a
committed number can always be re-snapshotted past.

**Relaxed, because a diff-scoped gate replaces the floor:**

| Ratchet | Where the number lives | What now catches the regression |
|---|---|---|
| untranslated strings, per file | `src/i18n/untranslated-baseline.json` | `check-i18n-strings.mjs` **[added-lines]** — a literal on a line you wrote |
| catalog QA violations, per check | `CEILINGS` in `src/i18n/qa.test.ts` | `check-source-strings.mjs` **[changed-values]** — QA on any value you added or changed |
| unextracted JSX strings | `--baseline=N` in `scripts/lib/i18n-gate-table.mjs` | **[added-lines]** — same population, no ledger |
| host-locale calls | `BASELINE` in `src/i18n/localeFormatting.test.ts` | that file's own **[added-lines]** / **[vs-base]** — a `toLocale*`/`localeCompare` on a line you wrote, or a touched file whose count grew vs the base ref |
| render-time defects, per surface | `src/i18n/render-baseline.json` | `check-i18n-render.mjs` **[vs-base]** — renders the base commit and fails on any per-surface increase. This one is not merely relaxed: the number is a REPORT, and decides a run in exactly one case — when no base commit was available to diff (see below) |
| untranslated strings inside ALL-CAPS constants | `src/i18n/untranslated-strict-baseline.json` (one aggregate) | **[added-lines]** — same population, per line |

For these six, a decrease is reported and tolerated: you do not re-snapshot anything,
and a change that improves one of these numbers without editing it will pass.

`eslint-plugin-i18next` exempts every literal under an ALL-CAPS variable declarator, which
is where this dashboard keeps its module-level UI-copy tables — so `SESSION_FILTERS`,
`SORT_OPTIONS` and `EFFORT_DISPLAY` shipped untranslated in ten locales while their files
reported clean. `eslint-rules/i18n-strict.js` closes that hole and marks the findings it
recovers; `eslint.i18n.strict.config.js` is the config the diff-scoped gates run.

**Two consequences worth knowing before you are surprised by them:**

- **`[added-lines]` now sees inside ALL-CAPS constants.** Touch any line in a
  pre-existing label table and the gate fails on copy you did not write. That is the
  intended tightening — the fix is the same either way: store a catalog key in the table
  and translate where it renders (`FILTER_LABEL_KEY` in `pages/ChatSidebar.tsx`).
- **The per-file ceilings still use the UPSTREAM rule.** They measure only the findings
  the marker does not tag, so `untranslated-baseline.json` keeps the population it was
  snapshotted from and closing the hole forced no bulk `--update`. The recovered strings
  get their own aggregate instead, so the class has a number to drive to zero.

**Still exact in both directions, because nothing diff-scoped covers them:**
`deadKeys.test.ts` and `glossary.test.ts` (DNT). Improving one of these *does* require
lowering its number in the same change. `check-i18n-keys.mjs`'s dynamic-site counts used
to be on this list and are now report-only — see the gap named under "Only two kinds of
check can fail this step": nothing diff-scoped covers them either, so that demotion
traded a merge-conflict generator for an uncovered population, deliberately.
They are single literals touched by roughly one PR at a time, so they were never the
contention problem, and relaxing them would have bought nothing at the cost of real
slack. If you add a diff-scoped gate for one of them, it may move to the table above.

**A total is not a diff, and cannot be asked of one.** A stored count is written by
whichever branch measured it last, so two branches that never touch the same file can
combine after merge into a number neither of them produced — and then the failure names
no diff that anyone can fix. That happened to the render gate: #1107 recorded
`projects.latent: 16` measured on its own base, #985 independently added one `truncate`
with no `min-w-0` to the Projects rail, and once both merged `main` measured 24 against
16. Every open PR inherited a red gate over a defect none of their diffs introduced.
So the render gate's number is a REPORT — printed both above and below reality, with
`[vs-base]` deciding the run. It decides a run in exactly one case: when there was no
base commit to diff, where it is all that is left and the alternative is exiting 0
having checked nothing. The other whole-repo counts were demoted the same way — only
`qa.test.ts`'s `CEILINGS` and the two exact AST ratchets still fail on a stored number,
and converting those is tracked in #1004.

**Every path to `main` supplies a base, so the diff always has something to bite on.**
A PR is measured against its base branch; a push to `main` is measured against the commit
it replaced (`ci.yml` passes `github.event.before` through
`.github/scripts/resolve-i18n-base.sh`). That is what makes it safe for a total to be a
report: the two-branch interaction above is still caught, as a `main`-push diff naming
the merge that produced it. **A run with no base at all is the one case where the total
decides the run** — see `scripts/lib/render-verdict.mjs`'s `totalIsFallback`. A gate must
not exit 0 having checked nothing, so where there is no diff the weaker check runs.

Three gaps in that claim, named rather than papered over:

- **`main` verdicts are sampled, not per-commit.** `ci.yml`'s concurrency group permits one
  pending run and GitHub evicts it when a newer one queues, so with a ~19min run and a
  ~1.4min median merge gap most `main` commits never get their own verdict. A commit whose
  run was evicted has its delta fall between two `github.event.before` boundaries and is
  never diffed by anything. Closing this needs lossless `main` runs (a per-SHA concurrency
  group, or a base resolved from the last successful run) — tracked in #1004.
- **A PR whose base is not `main` never runs `ci.yml` at all.** `pull_request:
  branches: [main]` filters on the base branch, so a stacked PR gets no render gate until
  it is re-based onto `main`. Its content first meets a diff on the sampled `main`-push path.
- **`over-budget-truncation` is not fully deterministic.** It compares a measured
  truncation RATIO against a budget, so a label sitting within ~10% of its budget can flip
  between runs as font metrics settle. Observed: `artifacts.layout` reported `0 -> 2` on
  the `Gallery` (1.87x vs a 1.7x budget) and `Table` (1.97x vs 1.9x) segments of
  `components/SegmentedControl.tsx`, then `PASS` on an immediate re-run of the same two
  commits. Before chasing a `[vs-base]` failure on this signature alone, re-run it — and
  if a label is genuinely near its budget, widen the container rather than the budget.
- **`[vs-base]` is blind to dependency drift by construction.** `buildBaseBundle` symlinks
  HEAD's `node_modules` rather than installing the base's, so a lockfile bump is measured
  with the new dependency set on both sides.

**Keep a relaxed ceiling TIGHT against its live count.** Upward-only means an improving
branch never has to edit the number, so tightening costs one line once, and each relaxed
gate reports its own decrease so the drift is visible in CI output rather than discovered
later. Tight ceilings are no longer load-bearing for `main` — the diff-scoped gates run
there too now — but slack in a ceiling is still slack that a merge-conflict resolution
can spend.

**The ultimate goal is zero for every number on either list.** That is what makes an
upward-only ceiling a convergence rather than a loosening: at 0 there is nothing left to
decrease, so "only an increase fails" *is* the strict gate. Each phase drives some to
zero and deletes its ceiling.

Why the relaxed ratchets changed at all: each number lives in a single generated ledger, so
demanding that every improving branch re-snapshot it made the ledger conflict between
branches whose source edits were disjoint, and made every merge to `main` invalidate the
number in every other open branch. It also did not achieve what it looked like — the
bypass is one `--update`, and commit `195904c` shipped a new app with 113 untranslated
strings while moving `_total` from 1747 to 1860, green, under the fully bidirectional
gate. Moving enforcement to the diff is a net tightening: that commit fails
**[added-lines]** today.

The QA predicates live in `scripts/lib/qa-checks.mjs` and nowhere else, shared by
`qa.test.ts` and the diff-scoped gate, so the counted set and the strict set cannot
drift.

Also untouched, and still zero-tolerance: `catalogParity`, `dynamicKeys`, `moduleLevel`,
`gen-pseudolocale --check`, `check-i18n-keys.mjs`'s dangling-reference check, and the
settings registry. None of them carries a number to argue about, which is exactly why
none of them caused this.

To see the QA worklist the deleted allowlist used to hold:
`I18N_QA_REPORT=1 npx vitest run src/i18n/qa.test.ts`.

### The render-time gate: what a source scan structurally cannot see

Every gate above reads **source** (an eslint pass over `src`) or **catalog JSON**. Three
defect classes are invisible to all of them, not by oversight but by construction:

| Defect | Why the static gates miss it |
|---|---|
| a string that was never extracted | it is ordinary TypeScript — `` `${h}h ${m}m ${s}s` `` in `useUptime.ts` reads as arithmetic, and shipped for months |
| a sentence assembled from several keys | it is several *correct* `t()` calls; only the rendered line shows the seam |
| English in `title` / `aria-label` / `placeholder` | attribute text never enters the text flow, so nothing that walks prose sees it |

`npm run i18n:render` (`scripts/check-i18n-render.mjs`) closes them. It renders the real
built SPA under the **`en-XA` pseudolocale**, where `gen-pseudolocale.mjs` has already
guaranteed the two properties the whole check rests on: every catalog value is wrapped in
`[` … `]`, and every ASCII letter outside a preserved region is accented. So inside one
inline run, a `]…[` seam **is** a surviving concatenation, and plain Latin **is** text
that never reached a catalog. It needs no gateway, no token and no backend — it serves
the build over loopback and answers every `/api/**` call from fixtures, exactly like the
`capture-*.mjs` harnesses.

Four things to know before you touch it:

1. **It builds its own bundle, with `NODE_ENV=development`.** `en-XA` is DEV-only in
   three independent places (`index.ts` tree-shakes the catalog, `isRestorableLanguage()`
   refuses the stored code, `DETECTABLE_CODES` hides it), all keyed on
   `import.meta.env.DEV`. `vite build --mode development` is **not** enough — Vite derives
   DEV from `NODE_ENV`, not from `--mode`. Get this wrong and the gate does not fail, it
   renders English and passes; `assertPseudoActive()` exists so that can never be silent.
2. **A crashed surface is exit 2, not findings.** If a fixture is the wrong shape the
   error boundary replaces the panel with its own English message, and a naive scan would
   report *that* as untranslated copy. Any uncaught page error aborts the run instead.
3. **DNT integrity runs only in real locales.** The pseudolocale accents DNT terms too
   (`GitHub` → `ĞìţĤùƀ`), so asserting them under `en-XA` would report all 19 as mangled.
   It is zero-tolerance, and it deliberately accepts an all-lowercase hit as the command
   (`git push`, `docker run`) rather than a mangled product name.
4. **`[vs-base]` decides the run; the record decides only when there is no base.** The
   gate renders the BASE commit with the same scanner and fails on any per-surface
   increase, reading no committed number — so there is nothing to re-snapshot and
   nothing to absorb a regression with. `src/i18n/render-baseline.json` (three buckets:
   `text`, `layout`, `latent`, keyed per surface) is a **debt record**, printed above
   and below reality alike; goal is 0 for every entry.
   It decides a run in exactly ONE case: a run that had no base commit to diff, where
   the record is all that is left and the alternative is exiting 0 having checked
   nothing but DNT. Both a PR and a push to `main` normally supply a base (the base
   branch, and the commit the push replaced), so that case is a bare local run or a
   branch-creation push. `scripts/lib/render-verdict.mjs`'s `totalIsFallback` is the
   flag, and it is deliberately NOT set by the three opt-outs (`--no-vs-base`,
   `--surface`/`--locale`, `--update`) which asked for a report and keep getting one.

   Why it is a report the rest of the time: a total is not attributable to a diff.
   #1107 recorded
   `projects.latent: 16` measured on its own base while #985 independently added one
   `min-w-0`-less `truncate` to the Projects rail; after both merged, `main` measured 24
   against 16 and every open PR inherited a failure its own diff had not caused. Finer
   per-surface keys did not prevent that and could not — they only shrink the range you
   can trade within. The measured proof that the diff is the stronger check: inflate
   `settings-about.text` from 28 to 100, add 18 real defects, and the ledger reports an
   *improvement* while `[vs-base]` fails with `28 -> 46 (+18)`.

`latent` holds `ellipsis-with-flex-parent` on its own — ~544 sites where
`text-overflow: ellipsis` cannot apply because a flex child's `min-width` is still `auto`,
so the truncation those sites think they have does nothing. Real, but a uniform `min-w-0`
cleanup that would otherwise bury the handful of actual overflows in `layout`.

The base tree is exported with `git archive`, never `git worktree add`. A worktree
registers itself in the repo, so a run killed mid-flight leaves a stale registration that
breaks every later run — and the obvious recovery, `git worktree prune`, removes the
registration of any worktree whose path is not visible from where it runs, which in a
container that mounts the checkout elsewhere means the caller's own. `git archive` touches
no repo state, needs no cleanup, and works on CI's shallow checkout.

**Regenerate the ledger on Linux + the pinned Chromium, not on your laptop.** `layout` and
`latent` are pixel measurements, so they depend on the platform's font metrics and on which
faces are installed. A macOS-regenerated ledger will not match CI, and one that lands
*lower* than CI measures silently understates the debt — CI prints an `above the debt
record` report and still exits 0, so nobody is forced to notice. It only fails a run in the
one case where there was no base commit to diff. `[vs-base]` is immune either way (both
sides are measured in the same run on the same box), which is another reason it carries
enforcement — but `--update` is not. Use the same image CI does:

```sh
docker run --rm --ipc=host -v "$PWD":/w -w /w/website \
  mcr.microsoft.com/playwright:v1.58.2-jammy \
  node scripts/check-i18n-render.mjs --build --update
```

The analysis is in `scripts/lib/render-scan.mjs`, which runs in **two** runtimes from one
copy: Node (so `renderScan.test.ts` can unit-test the string logic) and the browser (via
`browserBundle()`, which strips the `export` keywords and injects it with
`addScriptTag`). Keep it import-free, or the browser half stops loading. Its tests also
lock the pseudolocale invariants it borrows, so a change to the generator's delimiters
fails a test instead of quietly making the gate find nothing.

**Gotcha — the settings-search extractor.** `scripts/settingsExtract.ts` parses
Settings panels to generate `settingsRegistry.gen.ts` (which powers
command-palette settings search). It resolves BOTH `i18nT('k')` and `t('k')`
against the English catalogs. If you introduce a third way to render a settings
label, teach the extractor about it — otherwise those settings silently vanish
from search with no error anywhere (the generator has a floor check that now
fails loudly instead). Re-run `npm run gen:settings` after touching a panel.

## Data Fetching

Always use React Query (`useQuery`/`useMutation`) for server state. Do NOT use manual `useState` + `useEffect` + `useCallback` patterns for API calls. Use optimistic updates via `queryClient.setQueryData` where possible. Query keys follow `['resource-name']` convention (e.g. `['mcp-servers']`, `['mcp-registry']`, `['skills']`).

## Animations

Use Framer Motion for orchestrated component transitions (enter/exit, layout animations, gesture-driven). Use Tailwind `transition-*` for simple state changes (hover, toggle, color). Use Tailwind `animate-*` only for simple indicators (spin, pulse). Do NOT add new CSS `@keyframes` — use Framer Motion instead.

## Styling

Tailwind CSS with custom theme in `tailwind.config.js` — `darkMode: ['selector', '[data-theme="dark"]']`. 11-theme color system (dark/light variants) with CSS custom properties (design tokens) defined in `index.css`, including semantic colors (`--aim`, `--clarify`, `--diff-*`). Color Theme picker in Overview > Display tab with cross-instance sync. CSS utilities: `.top-bar-pill`, `.topbar-glass`, `.scroll-shadow`, `.table-striped`, `.skeleton`, `.focus-ring`. Theme toggle crossfades via `transition` on `body`.

## Shared Components

`src/components/ui.tsx`: `Card`, `CardTitle`, `Btn`, `SendBtn`, `Input`, `Badge`, `AimBadge`, `StatCard`, `Skeleton`, `ContentSkeleton`, `EmptyState`, `PageHeader`, `PanelSectionHeader`, `SearchInput`, `Toggle`

`PanelSectionHeader` is the one idiom for a counted list-section header inside a side panel (label + count node + hairline rule). Route a new panel section through it rather than hand-rolling a header — the Files and Artifacts tabs each grew their own and silently diverged on case, size, colour, and whether the count was a node or punctuation baked into the translated label. Hierarchy comes from weight and size, never from an opacity modifier, and the label is not uppercased (`text-transform` is a no-op on CJK).

Other shared: `SegmentedControl.tsx` (iOS-style sliding tab selector with Framer Motion), `DetailPanel.tsx` (resizable side panel with animated open/close), `SidePanelLayout.tsx` (shared side-panel page layout), `AgentSelector.tsx` (portal dropdown with ARIA), `layout.ts` (`LAYOUT` constants), `InfoTip.tsx`, `MarkdownRenderer.tsx` (with highlight.js syntax highlighting), `TypewriterText.tsx`

## Typography Scale

body 14px, descriptions/details `text-sm` (14px), labels/buttons/sidebar `text-[13px]`, badges/captions `text-[12px]`, decorative icons `text-[10px]`–`text-[11px]`. Minimum readable text: 11px. Code blocks: 13px mono. **No text below 10px.** Do not use `text-xs` (use `text-[13px]`), do not use `text-[9px]` or smaller.

## Security

All `dangerouslySetInnerHTML` content sanitized via DOMPurify (`src/api/helpers.ts`). `md()` renders markdown-like formatting + sanitizes. `sanitize()` for pre-escaped HTML. `esc()` for plain text escaping.

## Accessibility (a11y)

All interactive elements MUST be keyboard accessible. Use `<Clickable>` from `src/components/Clickable.tsx` instead of `<div onClick>`.

```tsx
// Good
import Clickable from '../components/Clickable'
<Clickable onClick={handler} className="...">Click me</Clickable>

// Bad — not keyboard accessible, fails jsx-a11y lint
<div onClick={handler} className="...">Click me</div>
```

For animated interactive elements, wrap `Clickable` with Framer Motion — it forwards refs and spreads props, so animation and a11y compose cleanly:

```tsx
import { motion } from 'framer-motion'
import Clickable from '../components/Clickable'
const MotionClickable = motion.create(Clickable)  // motion(Clickable) on older versions
// <MotionClickable onClick={handler} whileHover={{ scale: 1.02 }}>…</MotionClickable>
```

Alternatively add `role="button"` + `tabIndex` + `onKeyDown` directly on the `motion.div`.

Rules:
- Never use `<div onClick>` or `<span onClick>` without `role="button"` + `tabIndex` + `onKeyDown`. Prefer `<Clickable>` which handles all three.
- All icon-only buttons MUST have `aria-label` describing the action.
- Modals MUST have `role="dialog"`, `aria-modal="true"`, `aria-label`, Escape key dismissal, and focus trap.
- Dynamic content updates (streaming messages, notifications) should use `aria-live="polite"`.
- Do NOT use `<button>` elements directly — use `<Btn>` or `<SendBtn>` from `ui.tsx` (which handle styling), or `<Clickable>` for div-based interactive elements.

Tooling: `eslint-plugin-jsx-a11y` (warns on violations), `@axe-core/react` (runtime DOM scanning in dev mode — check browser console).

## Page Layout Guide

All dashboard pages MUST follow this consistent layout pattern. Do NOT invent custom layouts.

**Page skeleton** (every page):
```tsx
<>
  <PageHeader title="PageName" subtitle="Short description" />
  <div className="px-6 pb-8 overflow-y-auto flex-1 min-h-0">
    {/* optional StatCard row → Cards with tables/forms */}
  </div>
</>
```

**Stat cards** — OPTIONAL summary metrics above the content. Add a row only when
a number is not already visible in the content below it (a rolled-up total, a
rate, an error count). Do NOT add one that restates `items.length` for a list
rendered on the same screen: it costs ~90px above the fold and carries no action.
A page with no stat card row is conformant.
```tsx
<div className="grid gap-3.5 grid-cols-[repeat(auto-fit,minmax(150px,1fr))] mb-6">
  <StatCard label="Total" value={count} accent />
  <StatCard label="Active" value={active} />
</div>
```

**Data sections** — use `Card` + `CardTitle` + `InfoTip`:
```tsx
<Card>
  <CardTitle>Section Name <InfoTip text="Explanation." /></CardTitle>
  <SearchInput placeholder="Filter…" value={filter} onChange={…} />
  {items.length === 0 ? <EmptyState icon={<Anchor className="lucide-inline" />} title="None yet" /> : (
    <table className="w-full border-collapse table-striped">…</table>
  )}
</Card>
```

**Tables** — striped with standard header style:
```tsx
<th className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium">
```

**Forms** — inline within `Card`, using shared components:
- `Input` for text fields
- `SendBtn` for primary actions (accent color)
- `Btn` for secondary actions, `Btn danger` for destructive
- Styled `select`: `bg-bg-elevated border border-border rounded-md px-3 py-2 text-text text-sm font-body outline-none cursor-pointer transition-colors focus-ring`
- `AgentSelector` for agent dropdowns (portal-based, ARIA)

**Status indicators**:
- `Badge variant="ok"` (green), `variant="err"` (red), `variant="warn"` (amber), `variant="aim"` (purple)
- `AimBadge source="kirocrew"` (orange), `source="aim"` (purple), `source="builtin"` (gray)
- Toggle switches: `w-9 h-5 rounded-full` with `bg-accent` (on) / `bg-border` (off)

**Errors** — dismissible banner:
```tsx
<div className="mb-4 bg-danger/10 border border-danger/20 rounded-lg p-3 flex items-start gap-3 animate-rise">
```

**Animations**: `animate-rise` on cards/banners, `animate-scale-in` on inline reveals.

**Do NOT**:
- Wrap pages in `<div className="p-6 max-w-[960px] mx-auto">` — use `PageHeader` + `px-6 pb-8` container
- Use raw `<input>` / `<select>` / `<button>` — use `Input`, `Btn`, `SendBtn`, `SearchInput`
- Use raw status text — use `Badge` component
- Use `text-xs` — use `text-[13px]`

## Architecture

- **Entry**: `src/main.tsx` — wraps `<App>` in `<Provider>` (Redux) and `<BrowserRouter>` (React Router)
- **Routing**: `App.tsx` uses `<Routes>` / `<Route>` with client-side navigation; SPA fallback middleware in `server.py` serves `index.html` for non-API GET requests
- **State management**: Redux store (`src/store/index.ts`) with three slices:
  - `dashboardSlice` — SSE/WS connection state, chat slots, approval mode, optimistic slot add/remove reducers, async thunks for slot fetch / approval mode change
  - `chatSlice` — active slot, messages, history (sessions list with pagination), WS chunk/done handling, optimistic slot mutations, async thunks for slot CRUD / history fetch / resume / delete
  - `notificationsSlice` — notification list with add/delete/clear, async thunks for fetch/delete/clear
- **Real-time**: Single WebSocket at `/api/ws` multiplexes all real-time events (dashboard status, slots, slot_title, notification, refresh, chat_chunk, chat_done, chat_message, log, refine). `useWebSocket` hook with exponential backoff reconnect (1s→2s→4s→max 10s); on reconnect re-fetches state via Redux (no page reload) unless server version changes
- **API client**: `src/api/client.ts` — typed wrapper around all `/api/*` endpoints
- **Helpers**: `src/api/helpers.ts` — `esc()` (HTML escape), `md()` (markdown + DOMPurify), `sanitize()` (DOMPurify wrapper), `fmtSpeed()` (network speed formatting)
- **Types**: `src/types/index.ts` — shared interfaces (`ChatSlot`, `SessionInfo`, `ChatMessage`, `StatusData`, `Notification`, etc.)
- **Diff rendering**: `MarkdownRenderer` auto-detects diff code blocks (standard `+`/`-` format and kiro-cli `+N:`/`-N:` format) and renders with colored lines (green additions, red deletions, blue hunks)
- **Build output**: `vite.config.ts` outputs to `KiroCrew/src/kiro_crew/static/dist/`; `build-frontend.sh` runs the production build
- **Dev mode**: `./dev-frontend.sh` runs Vite dev server on port 3000 with API proxy to backend on 5476

## New Features

### Widget Event Bridge

Widgets (`<mcwidget>`) now support bidirectional communication via `data-action` events. Widget JS can emit structured events back to the agent session:

```js
window.parent.postMessage({type: 'kirocrew:action', action: 'submit', payload: {value: 42}}, '*')
```

### Chat Embedding (App SDK)

Apps can embed a full chat interface via the `ChatEmbed` component from `@kirocrew/sdk`:

```tsx
import { ChatEmbed } from '@kirocrew/sdk'
<ChatEmbed agent="my-agent" height={400} />
```

### Testing

- **jscpd duplication gate** — the build fails if copy-paste duplication is detected. Extract shared logic into utilities.
- **Vitest cobertura** — coverage emitted as cobertura XML for CI coverage integration.
- **Coverage integration** — coverage reports visible on pull requests via CI badges.

### URL Sanitization

`react-markdown` URL sanitizer now allows `vscode://` protocol URLs. Add new protocols to the allowlist in `urlTransform.ts`.

### Builtin App Auto-Discovery

Builtin apps no longer need manual `NAV_ITEMS` entries. The `builtinRegistry.ts` auto-discovers routes from the `src/pages/apps/` directory structure. To add a new builtin app:

1. Create `src/pages/apps/MyApp/index.tsx`
2. Export default component
3. Add route config to `builtinRegistry.ts`

### Frontend extension seams

Additive registries let a **downstream edition** (a separate build that composes
this SPA — e.g. an internal fork) contribute UI without copy-and-shadowing core
files. The core registers nothing new into them, so every seam is inert in the
stock build. There are **eight** registry seams:

| Seam | Module | Registrar → reader |
|------|--------|--------------------|
| Builtin page routes | `apps/builtinRegistry.ts` | `registerBuiltinComponents()` → `getBuiltinComponent()` |
| Nav icons | `apps/builtinIcons.tsx` | `registerBuiltinIcons()` → `getBuiltinIcon()` |
| Theme branding | `themeBranding.tsx` | `registerThemeBranding()` → `getThemeBranding()` |
| Theme picker options | `hooks/useTheme.tsx` | `registerTheme()` → `getRegisteredThemes()` |
| Top-bar widgets | `apps/topBarWidgets.tsx` | `registerTopBarWidgets()` → `getTopBarWidgets()` |
| Readout-capsule segments | `apps/capsuleSegments.tsx` | `registerCapsuleSegment()` → `getCapsuleSegments()` |
| Overview status cards | `pages/overviewStatCards.tsx` | `registerOverviewStatCards()` → `getOverviewStatCards()` |
| Panel nav + migration | `hooks/useKeyboardShortcuts.ts`, `components/MigrationCheck.tsx` | `registerPanelShortcut()`, `registerNonAppPrefix()` |

Plus one **exported-transport** seam for edition-owned API methods (not a
registry — see "API methods" below): `api/apiTransport.ts` exports `apiTransport`,
and the edition builds its own typed API module on it.

**Composition root.** `src/extensions.ts` is **core-owned** and imported first in
`main.tsx` (before the store/providers/`App`), so all registration runs before
render. It imports the `virtual:kirocrew-edition` module, which the
`editionExtensionPlugin` in `vite.config.ts` resolves to:
- an **inert empty module** in the stock OSS build (`KIROCREW_EDITION_DIR`
  unset) — the stock build registers nothing, byte-identical to no seam;
- the **downstream edition's own** `$KIROCREW_EDITION_DIR/extensions.tsx` when
  that env var points at an edition repo — so the edition injects its
  `register*()` calls + component imports **by build config**, compiled through
  the same vite/rollup pass, **without shadowing/overlaying any core file**
  (the copy-and-shadow erosion the seams exist to eliminate). A misconfigured
  `KIROCREW_EDITION_DIR` (set but missing `extensions.tsx`/`.ts`) **fails the
  build loudly** rather than silently degrading to the stock SPA.

**Edition-build safety (fail-closed opt-in):** edition composition is **opt-in
and fail-closed** — `KIROCREW_EDITION_DIR` alone is NOT enough; the plugin also
requires **`KIROCREW_ALLOW_EDITION=1`** or it THROWS. So every pipeline
(including release/publish, and the backend `setup.py` → `build-frontend.sh`
path) is protected by default: a stray/inherited `KIROCREW_EDITION_DIR` can
never silently compile proprietary edition sources into `website/dist` (the dist
staged into the public OSS wheel — a contaminated public release cannot be
unpublished). Only the edition's own build sets `KIROCREW_ALLOW_EDITION=1`.
Forgetting the opt-in fails safe (stock); there is no guard var a release job
must "remember to set." An edition-mode build additionally prints a loud
`[kirocrew-edition] ⚠ BUILDING WITH EDITION COMPOSITION ROOT` line so the mode
is unmissable in local + CI logs.

**Edition peer-dependency rule:** an edition dir resolves bare imports from its
OWN `node_modules`, so any **context-carrying singleton** the core's provider
tree owns must be de-duplicated or its hooks bind to a second instance
(`Invalid hook call`, `No QueryClient set`, null router context, silently empty
data — only at runtime, only in the edition build). `vite.config.ts`
`resolve.dedupe` covers `react`, `react-dom`, `react-redux`, `react-router`,
`react-router-dom`, `@tanstack/react-query`, `framer-motion`. **When the core
adds a new global-context provider, add its package here** (and the edition
should declare these as peer deps).

The core must register **nothing of its own** in `extensions.ts` —
`extensionSeams.test.tsx` asserts its stock body is the edition import + `export
{}` (plus comments). Put core registrations in the seed maps, never here.
Registries are read at module-load / first-render and are **not reactive** —
the edition registers via this import path, not after mount.

**Builtin routes.** `registerBuiltinComponents()` accepts only a single, plain
top-level path segment (`/^\/[A-Za-z0-9][A-Za-z0-9._~-]*$/`) — `BuiltinAppRoute`
resolves the catch-all `/:builtinApp` from one path parameter and matches only
`location.pathname`, never the query/hash. So a multi-segment (`/reports/daily`),
query (`/reports?daily`), hash (`/reports#x`), whitespace, or `.`/`..` route
would register but never resolve (navigation redirects to chat). A non-conforming
route routes through `reportSeamCollision`.

**Panel shortcuts.** `registerPanelShortcut({ code, path, label })` identifies the
chord solely by `KeyboardEvent.code`; the displayed key is derived from it, so the
advertised chord can never diverge from the handled one. Beyond core panel chords
and prior extensions, it also rejects any code in `RESERVED_PANEL_CODES` — the Alt
chords the handler consumes before panel routing (shortcuts modal, settings,
focus-input, MRU, chat-jump digits, arrows) — since a panel on one of those would
be advertised but unreachable. All rejections route through `reportSeamCollision`.

**Theme picker options.** `registerTheme([{ value, label }])` adds a built-in
theme to the picker; `useTheme` reads it via `allThemes = [...THEMES,
...registered, ...customThemes]`. The theme's CSS block ships in the edition's
overlay — this seam only contributes the picker entry. A `value` already in
`THEMES` or previously registered is rejected via `reportSeamCollision` (core
wins).

**Readout-capsule segments.** `registerCapsuleSegment([{ id, order?, component,
hideOnMobile? }])` mounts a status segment INSIDE the header's readout capsule
(sharing its border, `|` dividers, and offline tint), not as a standalone sibling
pill — use this over `registerTopBarWidgets` when the readout must join that
grouping (e.g. a credential-TTL or spend segment). App.tsx splices registered
segments after the core segments in `order`; each renders with an `offline` prop
and is isolated in its own `ErrorBoundary`.

**Overview status cards.** `registerOverviewStatCards([{ id, order?, component }])`
adds a self-contained `StatCard` (owning its own query/state, like the core
`TunnelStatus`) to the Settings → Overview grid, after the core cards. Each
receives a `delay` prop for the grid's stagger animation and is `ErrorBoundary`-
isolated.

**Theme branding reaches two consumers.** `getThemeBranding(colorTheme)` drives
both the App.tsx shell chrome AND `WelcomeView.tsx` (the new-session/welcome
screen brand mark) — a registered theme's `logo` shows in both, falling back to
the stock `KiroGhost` when the theme registers none.

**API methods.** There is no registrar. An edition imports `apiTransport` from
`api/apiTransport.ts` and writes its own fully-typed API module on it — see the
"API methods (exported transport, not a registry)" note below for why.

**Collision policy (`apps/seamCollision.ts`).** A registration whose key collides
with a core (or already-registered) entry is resolved core-wins. It is
**fail-loud in dev/test** (`reportSeamCollision` throws under
`import.meta.env.DEV`) so a colliding upstream sync is caught at build/test time,
and **degrades safe in production** (warn + ignore) so a shipped app never
white-screens over a duplicate.

**API methods (exported transport, not a registry).** Unlike the six registry
seams, the core never *consumes* edition API methods — they are written and read
only by the edition. A registry the core never reads would add public,
stringly-typed (`unknown`-cast) seam surface for zero composition benefit. So
instead of a registrar, `api/apiTransport.ts` **exports** the blessed
`apiTransport` — the same `get`/`post`/`put`/`del`/`patch` + `j`/`jNullable` the
core methods use (`client.ts` installs them via `installApiTransport` at module
load). An edition builds its OWN fully-typed API module on `apiTransport`:

```ts
import { apiTransport as t } from '../api/apiTransport'
export const editionApi = {
  midwayTtl: () => t.get('/api/midway-ttl').then(t.j) as Promise<MidwayTtl>,
}
```

This gives the edition the one thing it needs — the `X-Session-Key` header (the
fail-open ephemeral-gate guard) and the auth-recovery/`ApiError` pipeline by
construction — with full static types on the edition side and **no new *registry*
contract**. It never forks `client.ts` and never writes raw `fetch` (which would
silently drop the session key).

`ApiTransport` (the seven helper signatures + the `j`/`jNullable` semantics) **is**
a small, **intentionally-frozen** downstream contract — a separately-built edition
compiles against it. There is no `CONTRACT_VERSION`-style guard on this frontend
seam, so treat it like the backend's "CONTRACT_VERSION pinned at 1 pre-launch":
changing a request helper's shape or `j`'s error behavior is **edition-breaking**
(the stock build stays green — the seam is inert — so breakage surfaces only at
runtime in the out-of-repo edition), not a free refactor. Evolve additively. Each
`apiTransport` method is a stable wrapper that resolves the installed helper at
call time, so an edition may import/destructure it at module-init without an
ordering hazard vs. `extensions.ts`. Trust boundary: the transport carries the
session key — it is for the edition composition root, **never**
app/plugin-contributed frontend code.

**`onActivate` timing.** A theme branding's `onActivate` side-effect fires on the
first render for the initially-active theme (not only on a later switch); keep it
idempotent and cheap. Inert in the stock build (no theme registers one).
