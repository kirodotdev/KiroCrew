# Internationalization: authoring rules

The dashboard is translated. **Never hardcode a user-facing English string**, and
**never format a date, number, or sort order without naming a locale.** Both fail
silently: the string renders as English in every language, and the formatted value
follows whatever locale the browser happens to have.

This file is the authoring contract. The gate chain that enforces it (what
`npm run i18n:check` runs, what each check catches, and what a ratchet may
legally do) is in [`docs/ci/i18n-gates.md`](../../docs/ci/i18n-gates.md).

## Calling the translator

- **Inside a component body:** `const { t } = useTranslation()`, then `t('key')`.
  Preferred for new code, because it subscribes to language changes.
- **Anywhere a hook is illegal** (render callbacks, plain helpers, non-component
  modules): `import { i18nT } from '../i18n/t'`, then `i18nT('key')`. It reads the
  current language at call time but does not subscribe.
- **Never `import { t } from 'i18next'`.** `t` is a very common local identifier
  here (`.map(t => …)` over tabs, turns, tasks, themes), so a bare `t` gets
  shadowed and the call lands on a domain object instead.

`LanguageProvider` forces a re-RENDER of the tree on a language change, using
`cloneElement` (which defeats React's referential-equality bailout) rather than a
changing `key`. It deliberately does **not** remount, because a remount discards
in-flight component state (the theme-install URL input sits in the same Display
panel as the language picker, so a switch is exactly when a user would lose what
they typed). The consequence for `i18nT()`: a call in RENDER position re-resolves,
but a value baked into a `useMemo` whose deps exclude the language does not. Put
such a lookup behind a getter or a function, never inside the memoized value.

## Catalog structure

Catalogs live in `src/i18n/locales/`:

| File | Owner |
|---|---|
| `en.json` | **generated**. `node scripts/i18n-codemod.mjs` rewrites it wholesale. Never hand-edit. |
| `en.manual.json` | hand-authored English with no source literal to extract, for example the language picker's own labels. |
| `<tag>.json` | one per translation. Its key set must match the English key set exactly. |
| `en-XA.json` | generated pseudolocale, dev-only. Not a language. |

Shipped languages, ordered by global speaker count (which is also the picker
order): `en`, `zh-CN`, `hi`, `es`, `fr`, `bn`, `pt`, `ru`, `de`, `ja`, `ko`, `it`.

**Right-to-left languages (Arabic, Urdu) are intentionally not shipped.** The
layout is built from physical-direction utilities (`pl-*`, `left-*`, `text-left`)
and unmirrored directional icons, so an RTL catalog would render correct text in a
visibly wrong shell. Adding one needs `dir="rtl"` plus a logical-property
conversion (`ps-*`/`pe-*`, `start-*`/`end-*`) first, not just a catalog.

Adding a language is a **data change**: three edits, no component or test changes.

1. `locales/<tag>.json`, with the same key set as `en.json` plus `en.manual.json`.
2. One entry in `SUPPORTED_LANGUAGES` (`src/i18n/languages.ts`).
3. One line in `CATALOGS` (`src/i18n/index.ts`).

The parity tests generate their cases from `SUPPORTED_LANGUAGES` and read catalogs
from the runtime `CATALOGS` map, so a new language automatically gets its
key-parity, placeholder-preservation, and no-empty-value coverage. Miss one of the
three edits and CI fails naming the gap; it cannot silently ship as English. There
is **no allowlist**, so every language lands in the same commit. That is what makes
each new language add marginal cost to every subsequent i18n change.

Three code lists answer three different questions, and conflating them is a real
bug (registering the pseudolocale made `en` ambiguous, so `en-GB` stopped
resolving confidently to `en`):

- `SUPPORTED_CODES`: what the runtime can resolve when asked explicitly.
- `DETECTABLE_CODES`: what a browser `Accept-Language` tag may resolve to.
- `PICKABLE_LANGUAGES`: what a user may choose in the UI.

**Do not pin a test fixture to a language you might later ship.** An assertion
like "`fr` is unsupported, so it falls back" silently inverts the moment French
ships. Use a language the project has no plans for for negative cases, and derive
positive cases from `SUPPORTED_CODES` so a new language is covered automatically.

## Counts: never concatenate a plural suffix

**Never append a plural marker outside the translate call.** This is a bug:

```tsx
// WRONG: renders 会话s, 3 sesións, এজেন্টs
{n} {i18nT('pages.overview.memoryTab.session')}{n === 1 ? '' : 's'}
```

The `s` is added *outside* the call, so no catalog value can fix it. English
plural rules are also not universal: Russian needs 4 forms, Spanish 3, Chinese 1.
Pass the count and let i18next pick the form via `Intl.PluralRules`:

```tsx
// RIGHT
{i18nT('pages.overview.memoryTab.session', { count: n })}
```

The count goes *inside* the string (`"{{count}} sessions"`) so a translation can
place the number where its grammar requires. Add one catalog key per category the
language actually has (`_one` / `_other`, plus `_few` / `_many` where needed).
Each language is checked against its OWN plural categories, so a missing or
unreachable form fails.

`scripts/i18n-plural-codemod.mjs` performs the conversion and maintains
`src/i18n/pluralKeys.json`, the registry of pluralized keys. Run it with `--check`
to verify none crept back in. Which keys are plural comes from that registry,
never from sniffing a `_one` / `_other` suffix, because real copy ends in those
words (`panel_to_add_one` is "panel to add one.").

## One key, one meaning

**Never reuse a key across two grammatical roles.** English collapses distinctions
other languages keep, so a shared key forces a translator to guess:

- `schedulePage.type` was both a table column header (the noun "Type") and the
  imperative verb in "Type `delete` to confirm". es/pt/ru picked the noun and
  broke the instruction; zh-CN/hi/bn picked the verb, so the column header read
  "please enter". It is two keys now, the verb one named `type_verb_to_confirm`.

If a value's part of speech is not obvious from the key, **put it in the key**.

**A literal token the user must type must never be a catalog value.** Keep it a
code constant (`BULK_DELETE_TOKEN`), or translating it makes the action impossible
to complete. A test pins that the constant exists, that the comparison is against
it, and that the UI renders it verbatim in a `<code>` element.

**Never dedupe translation work by English value alone.** The corpus is thousands
of keys with materially fewer distinct English strings, so translating each unique
string once is tempting, and it silently merges keys whose shared English word
carries two meanings. Adding de and it that way collapsed `Open` (the verb versus
an issue status), `Review` (verb versus noun), `Plan` / `Schedule` (button versus
label), and `Type`. Only one of those was caught by a test; the rest surfaced in an
audit. If you dedupe, afterwards **diff each duplicate group against the
already-shipped catalogs**: where several existing languages chose different words
for one English string, English is hiding a distinction and the merged value is
wrong.

## Built-in app copy comes from Python, and is localised without touching it

An app's `displayName`, `description`, `highlights[]` and `ui.pages[0].label` live in
`src/kiro_crew/apps/builtins/<app>/app.json` on the **Python** side, and the App Store
components interpolate them raw. So they were English in every locale, and the nav rail
read `Papyrus` while that app's own page header was translated.

`src/components/appstore/appManifest.ts` holds `APP_MANIFEST_KEY`: one entry per
built-in id, mapping each field to a catalog key under `apps.<camelId>.manifest.*`.
Render through its resolvers — `appDisplayName`, `appDescription`, `appPageLabel`,
`appHighlights` — never off the raw record.

**It is additive on purpose: `app.json` keeps its English.** The obvious design is VS
Code's, a `%key%` placeholder inside the manifest, and it was rejected because it
*replaces* the English. `kirocrew app list` prints `displayName` straight to a terminal
with no catalog, and `ui_language_tag()` returns `''` whenever the user is on "follow the
browser" — so resolving there would mean a second localisation stack in Python plus a
request locale the backend does not have. Keeping the manifest untouched leaves every
catalog-less consumer correct **by construction** rather than by a fallback.

The price is two copies of the same English, and `scripts/check-app-manifest-sync.mjs`
is what makes that safe. It is a hard zero: it derives the expected keys from each app
id and fails if one is missing from `en.json` or holds anything but the manifest's own
prose, byte for byte.

**Adding or editing a built-in — the order that avoids a red build:**

1. Edit `app.json` (or add the app under `builtins/<dir>/app.json`).
2. Add the matching keys to `locales/en.json` under `apps.<camelId>.manifest.*`
   (`display_name`, `description`, `page_label`, `highlight_1..N`) with values
   **identical** to the manifest.
3. Add the entry to `APP_MANIFEST_KEY`, one `highlights` key per bullet.
4. Translate into the other eleven catalogs — `catalogParity.test.ts` is all-or-nothing.
5. Run `npm run i18n:check`.

Two traps worth knowing before you debug them:

- **These keys are NOT covered by `[key-refs]`.** The resolvers read
  `i18nT(k.displayName)` off a local, which `check-i18n-keys.mjs` cannot follow — it
  reports `appManifest.ts: 0 -> 4` under the report-only `[dynamic-keys]`. Key existence
  is proved by `[manifest-sync]` instead. Do not read a green `[key-refs]` as coverage
  here.
- **A `highlights` length mismatch is silent by design.** `appHighlights()` falls back to
  the manifest's full English list rather than truncating, because losing a bullet is
  worse than showing it untranslated. `[manifest-sync]` fails on the mismatch, and
  `src/test/appManifest.test.ts` pins the count.

Third-party apps are deliberately out of scope: their copy is their author's to
translate, so they fall through to whatever the manifest supplied. That fallthrough is
also a **trust boundary** — `keysFor()` refuses to resolve when `_registry` is set, so a
registry row that reuses a built-in id cannot wear the built-in's localised identity next
to an Install button. `_registry` is attached server-side and cannot be forged by index
content; `origin` can, which is why it is not the signal. Same ordering as `sourceLabel()`
and `isVerified()` in `src/components/appstore/types.ts`.

## Formatting follows the app language, not the browser

`d.toLocaleDateString()`, `d.toLocaleDateString([])` and
`d.toLocaleTimeString(undefined, { … })` all mean the same thing: **format in the
host locale**. They ignore the language setting entirely. `LanguageProvider` sets
`<html lang>`, but `<html lang>` has no effect on `Intl`, so a dashboard running
in Chinese on an en-US browser renders an American date inside Chinese UI.
`a.localeCompare(b)` has the same flaw for ordering: the sort order of a list of
names silently depends on the browser.

Route it through `src/i18n/format.ts`. That module is the **seam**: the only place
allowed to resolve a locale, and it reads the active language per call, so a
language switch takes effect without a remount. It resolves
`i18next.resolvedLanguage` (what i18next actually selected after fallback) rather
than the requested tag, so the UI text and the dates around it cannot disagree.

```ts
import { fmtDate, fmtRelative, compareText } from '../i18n/format'

fmtDate(iso)                 // not new Date(iso).toLocaleDateString()
fmtRelative(ts)              // not a hand-written "3d ago"
names.sort(compareText)      // not (a, b) => a.localeCompare(b)
```

Each helper carries its own preset, and the options type omits the field the preset
owns: `fmtDate` is already `dateStyle: 'medium'`, so use `fmtDateFields(value, { … })`
when you need explicit components instead.

Available: `fmtNumber`, `fmtPercent`, `fmtCurrency`, `fmtUnit`, `fmtDuration`,
`fmtCompact`, `fmtBytes`, `fmtDate`, `fmtTime`, `fmtDateTime`, `fmtDateNumeric`,
`fmtTimeNumeric`, `fmtDateTimeNumeric`, `fmtDateFields`, `fmtWeekday`,
`fmtRelative`, `fmtList`, `collator`, `compareText`, plus `activeLocale` and
`toDate`.

**Naming a locale IS the opt-out**, which is why there is no allowlist file:

```ts
d.toLocaleDateString()                  // finding
d.toLocaleDateString([])                // finding: 2 args, still the host locale
d.toLocaleTimeString(undefined, opts)   // finding
a.localeCompare(b)                      // finding
d.toLocaleDateString('en-US', opts)     // allowed: the pin is visible to a reviewer
a.localeCompare(b, 'en-US')             // allowed
a < b ? -1 : 1                          // allowed: byte order, not matched at all
```

A machine-parse site (an ISO timestamp sort, a filesystem path sort, a value fed
to `Date.parse` on the other side) states its pin **in the code**, not in a
registry a reviewer has to go look up.

Two things a source scan cannot see: a pinned locale can still be the *wrong*
locale, and `toFixed` / `String(n)` / `join(', ')` are not locale-aware APIs at
all, so nothing syntactic detects them. Do not hand-format numbers: Latin digits
are wrong for `bn`, and for `ar-EG`, `ar-SA` and `fa` if they ever ship.

`fmtDuration` takes the parts **already split** by the caller, deliberately. Every
duration surface has its own granularity rule (a log row drops to `ms` under a
second, a tab pill floors at "under a minute"), and those are product decisions,
not formatting ones. It joins with `Intl.ListFormat` `type: 'unit'` rather than a
hardcoded space, because narrow unit lists are space-joined in en/ru/fr,
comma-joined in de, and joined with NOTHING in zh. `Intl.DurationFormat` would do
all of this in one call and is deliberately unused: it is `undefined` on the
Node 20 and Electron baseline.

`fmtCompact` changes rendered WIDTH per locale (zh abbreviates on 万, de has no
short form at these magnitudes), so a caller in tight chrome should confirm the
container tolerates it.

## Script fonts: keep the aliases first

`index.css` declares `@font-face` aliases carrying `unicode-range` for Han,
Kana, Hangul, Devanagari and Bengali, collects them into `--script-fallbacks` and
`--script-fallbacks-mono`, and puts **that token first** in `--font-body` and
`--mono`. The range restriction is what makes this safe: the aliases are never
consulted for Latin or general punctuation, so they cannot change Latin metrics
or leading, and they are a no-op when the named face is not installed.

The `:root` tokens use the Simplified Chinese `KC Han Fallback` and
`KC Han Mono Fallback` aliases. Under `html:lang(ja)`, both shared tokens switch
to `KC Japanese Fallback` and `KC Japanese Mono Fallback`, whose ranges include
Kana as well as shared ideographs; under `html:lang(ko)` they switch to
`KC Korean Fallback` and `KC Korean Mono Fallback`, whose ranges add the Hangul
syllable and Jamo blocks. Keep every other locale's aliases out of these tokens:
if the named face is unavailable, the browser must reach its language-aware
fallback for that script instead of being forced through a Simplified Chinese
alias — which for Korean cannot draw Hangul at all. Every user font choice and
theme declaration consumes the shared tokens, so changing the document language
updates proportional and monospace fallbacks without a component-specific font
stack.

**Do not reorder those stacks or drop the token when adding a family.** Moving a
Latin family in front silently returns zh-CN, ja, ko, hi and bn to whatever the
platform picks for a missing glyph. A test pins the `:root` tokens, every
declaration site (including the theme blocks, which redeclare both), the Japanese
and Korean locale overrides, and the ordering.

## Translating the corpus

Shard the work rather than doing one pass. `node scripts/i18n-shard.mjs split <dir>`
writes flat key-to-value shards. Keep shard dirs OUTSIDE the worktree, because a
dirty tree blocks worktree pruning.

`split` also writes `shard-NN.context.json` beside each shard, carrying the
translator context from `src/i18n/en.context.json` for the keys in that shard.
**Read it before translating the shard.** It is the only thing that tells you `KB`
is kilobytes and not "knowledge base", that `K` is a keyboard key you must leave
alone, and that `Run` is the verb. If a short or ambiguous string has no entry, add
one to `en.context.json` rather than guessing twice. `split` warns and emits no
context files when the sidecar is missing.

**Reassemble with `node scripts/i18n-translate.mjs merge <baseDir>`, never with
`i18n-shard.mjs join`.** `join` rewrites the catalog from shards keyed off the
**English** corpus, so any form the locale has and English does not is silently
dropped: a measured round trip removes over a hundred lines from `ru.json` and
dozens of keys from each of es, fr, pt and it, all `_few` / `_many` CLDR plural
forms. It also cannot accept the locale-specific plural keys `emit` asks for,
because it validates against the English key set. `merge` is insert-only by default
and preserves both. Never hand-assemble a catalog either: `merge`'s fail-closed
checks are what stop English text shipping disguised as a translation.

`i18n-translate.mjs` is the whole pipeline, and it is deliberately offline. It
writes prompts and validates answers, but sends nothing:

| Command | Does |
|---|---|
| `plan [pathPrefix]` | what still needs translating, read from `untranslated-baseline.json` |
| `emit <baseDir> [--locales a,b]` | writes one prompt per (locale, shard), including the plural forms that locale requires |
| `verify <baseDir> --locale <tag>` | every rule that can be decided mechanically. Run it before `merge` |
| `merge <baseDir> [--overwrite]` | insert-only reassembly |

Per-language style guides live in `src/i18n/style/<tag>.md`, each with a test that
holds the shipped catalog to it.
