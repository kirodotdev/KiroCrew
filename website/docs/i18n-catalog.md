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

### Settings registry labels

`scripts/settingsExtract.ts` resolves translated setting labels to English for
command-palette search and also stores their catalog key in the generated
registry. Keep both values: `useSettingHighlight` resolves the key in the active
locale before matching `data-setting-label`, while the palette deliberately
continues to index the stable English text. Run `npm run gen:settings` after
changing a setting label or its translation key.

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
3. One line in `AUTHORED_CATALOGS` (`src/i18n/catalogs.ts`, the module that owns
   every catalog import; `src/i18n/all.ts` is the entry that registers them).

The parity tests generate their cases from `SUPPORTED_LANGUAGES` and read catalogs
from the `CATALOGS` map in `src/i18n/catalogs.ts` (the map registration is fed
from), so a new language automatically gets its
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

## The product name is an interpolation variable

Catalog values never hardcode the displayed product name. They interpolate
`{{productName}}`, which `initI18n()` supplies to i18next as
`interpolation.defaultVariables` with the stock value `Kiro Crew`, so the stock
build renders exactly what a literal would. The indirection exists for
downstream editions: overriding one variable rebrands every catalog string,
instead of forking 13 locale files through every upstream sync (see
[extension-seams](extension-seams.md)).

> The pre-existing catalog values were converted in batches (the full-catalog
> diff exceeds the reviewable size limit). The conversion is complete; a
> catalog-wide test in `productName.test.ts` pins that no value outside the
> exceptions below carries the literal.

Authoring rules that follow:

- **New copy naming the product writes `{{productName}}`, not the literal.**
  A translation must carry the same placeholder — `catalogParity.test.ts`
  placeholder parity fails the catalog that drops it.
- **The `apps.<id>.manifest.*` keys are the deliberate exception.** They must
  stay byte-identical to the Python-side `app.json` prose (`[manifest-sync]`
  is a hard zero), so they keep the literal English name.
- **Attribution and data-egress copy keeps the literal too.** A string whose
  referent does not change with an edition must not interpolate the name:
  `app.star_kirocrew_on_github` wraps a hardcoded upstream repo URL, and the
  survey (`components.sessionPulseSurveyCard.email_disclosure`) and install
  receipts (`privacyDisclosure.installReceipt*`) name the recipient of data
  sent to hardcoded upstream endpoints. Interpolating those would make an
  edition misattribute a link target or where user data goes.
- **Wire-format identifiers keep their unspaced literal.** The generated
  Slack app name (`KiroCrew-{{alias}}`) and the webhook signature headers
  (`X-KiroCrew-Timestamp` / `X-KiroCrew-Signature`) are fixed by the backend,
  so the UI must spell them exactly whatever the edition renders elsewhere.
- **A call-time variable of the same name wins** over the default, per
  i18next's merge order — useful when a string names a *different* crew.
- German compounds hyphenate through the placeholder
  (`{{productName}}-Katalog`), matching how the literal compound was written.

`setProductName()` (exported beside `initI18n`) is the edition override. It
must run before `initI18n()`; the edition composition root is imported first
in `main.tsx`, so that ordering holds by construction. A late call throws in
dev rather than half-applying; in production it returns silently rather
than crash the shell.

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

A **fully hardcoded** literal commits the same defect with no `i18nT` in it,
in any of four spellings:

```tsx
// WRONG for the same reason — the plural form is chosen in JS, in English
aria-label={`Retry ${n} failed subagent${n > 1 ? 's' : ''}`}   // template glue
<span>{n} agent{n > 1 ? 's' : ''}</span>                        // JSX-text glue
const label = 'agent' + (n > 1 ? 's' : '')                      // concatenation
const word = n === 1 ? 'category' : 'categories'                // whole words
```

`--check` counts all of these too (`[plurals-hardcoded]`), against a ceiling that
fails only when the class grows: the frozen sites each need a new catalog key, so
they are converted by hand and the ceiling ratchets down with them.

## One key, one meaning

**Never reuse a key across two grammatical roles.** English collapses distinctions
other languages keep, so a shared key forces a translator to guess:

- `schedulePage.type` was both a table column header (the noun "Type") and the
  imperative verb in "Type `delete` to confirm". es/pt/ru picked the noun and
  broke the instruction; zh-CN/hi/bn picked the verb, so the column header read
  "please enter". It is two keys now, the verb one named `type_verb_to_confirm`.

If a value's part of speech is not obvious from the key, **put it in the key**.

## Destructive-confirm operands must be quoted

A confirm string that interpolates a user-supplied name without quotes lets an
ordinary-word name blend into the sentence: a pet named "Everything" produced
"Reset Everything?", indistinguishable from a sentence about resetting
everything (#4653, #4657, #4676, #4821).

**Quote the operand in every authored catalog**, using that locale's pair from
`OPERAND_QUOTE_PAIRS` in `scripts/lib/qa-checks.mjs` (curly doubles in English,
guillemets with U+202F in French, `„“` in German, `「」` in Japanese, and so on).
ASCII `"{{name}}"` is not enough.

`src/i18n/destructiveConfirm.test.ts` is the convention detector, not an
allowlist you can forget to extend:

- every key whose **name** matches `/confirm/i` and whose English value
  interpolates a placeholder must be on `QUOTED_OPERAND_CONFIRM_KEYS`, **or**
- listed in `CONFIRM_OPERAND_KEY_EXEMPTIONS` with a reason (today: the #4657
  kind-word forms, where "template" / "crew" already sit next to the name), **or**
- interpolate **only** placeholder names in `EXEMPT_CONFIRM_PLACEHOLDER_NAMES`
  (numerals, closed-set schedule fragments, version ids, and system error
  text — they cannot parse as prose). The set lives next to the pin; do not
  restate it here.

A new confirm key with `{{name}}` and no kind word fails CI until it is quoted
in all 12 catalogs and added to the pin. The glyph pin then requires **every**
non-exempt placeholder in a pinned key to be wrapped, not merely one of them.
After changing English, regenerate `en-XA.json` with `npm run i18n:pseudo`.

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

Bounded-monitor evidence follows the same seam. Probe, wake, agent-turn, token,
provider-error, cadence, and budget values pass through `fmtNumber`; probe
deadlines pass through `fmtDateTimeNumeric`. The catalog keeps these usage lines
label-first (`"Probes: {{count}}"`) because `count` is already formatted text and
may also be the translated unknown-state label, so it must not be used as an
i18next plural selector. Human-readable monitor statuses are catalog values in
all shipped locales. Provider classifications, scheduler decisions, terminal
reason codes, and target URLs are machine or user data instead: render them with
`translate="no"` and never add their open-ended values to the catalog.
Bounded-monitor validation formats the backend minimum and maximum before passing
them to the field-specific catalog message. The pull-request example translates
only its local “e.g.” prefix; the URL remains byte-identical under the catalog's
do-not-translate URL rule.

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

The `:root` tokens carry only the non-Han script aliases (Devanagari, Bengali).
Regional Han faces are scoped with `html:lang(zh-CN)`, `html:lang(ja)`, and
`html:lang(ko)` so untagged CJK in an English UI reaches the browser/OS
locale-aware cascade instead of being forced through Simplified Chinese glyph
forms. A bare `:lang(zh)` is not used: it also matches Traditional tags
(`zh-TW`, `zh-HK`, `zh-Hant`). Under `html:lang(zh-CN)` the tokens switch to
`KC Han Fallback` and `KC Han Mono Fallback`; under `html:lang(ja)` they switch
to `KC Japanese Fallback` and `KC Japanese Mono Fallback`, whose ranges include
Kana as well as shared ideographs; under `html:lang(ko)` they switch to
`KC Korean Fallback` and `KC Korean Mono Fallback`, whose ranges add the Hangul
syllable and Jamo blocks. Keep every other locale's aliases out of these tokens:
if the named face is unavailable, the browser must reach its language-aware
fallback for that script instead of being forced through a foreign Han alias —
which for Korean cannot draw Hangul at all. The rules set only the fallback
tokens: `--font-body` / `--mono` already resolve `var(--script-fallbacks)` on
`<html>` (`:root` and `useZoom`), so document language updates both stacks
without redeclaring them. Content `lang=` inside an English document is not
wired; there is no in-repo producer of those attributes yet.

**Do not reorder those stacks or drop the token when adding a family.** Moving a
Latin family in front silently returns zh-CN, ja, ko, hi and bn to whatever the
platform picks for a missing glyph. A test pins the `:root` tokens, every
declaration site (including the theme blocks, which redeclare both), the
`html:lang(zh-CN/ja/ko)` overrides, and the ordering.

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
