/**
 * Locale-aware formatting — the single seam for dates, numbers, lists and collation.
 *
 * ## Why this module exists
 *
 * Every formatter here reads the ACTIVE UI language at call time — the one thing
 * the raw `Intl` spellings cannot do. `toLocaleString()`,
 * `toLocaleDateString([])` and `toLocaleTimeString(undefined, {…})` all mean
 * *host* locale (the browser's), not the app's `dashboard.language`:
 * `LanguageProvider` sets `<html lang>`, but `<html lang>` has no effect on
 * `Intl`, so a dashboard running in Chinese on an en-US browser would render
 * `7/30/2026` and `Jul 30` inside otherwise-Chinese UI. Routing every call site
 * through this single seam is what lets formatting follow the selected language.
 *
 * ## Why plain functions and not hooks
 *
 * Same reasoning as `i18nT` in `./t.ts`, and deliberately the same shape so the
 * two are used side by side without ceremony: formatting happens inside
 * `.map()` callbacks, in comparator functions passed to `.sort()`, and in plain
 * helper modules with no component around them — all positions a hook cannot
 * legally go. A language switch repaints the tree (`LanguageProvider` re-renders
 * via `cloneElement` after the catalog swap — an update, not a remount), so a
 * function that reads the language at call time re-evaluates on switch without
 * subscribing to anything. The one shape that repaint cannot reach is a
 * `React.memo` boundary, whose props-equality bailout swallows it —
 * `MemoI18nSubscriptionRatchet.test.ts` pins the subscription every such
 * boundary needs.
 *
 * ## Why `localeMatcher: 'lookup'`
 *
 * `'lookup'` is RFC 4647 §3.4 — it truncates `zh-CN` → `zh` → root until it
 * finds data. That is precisely the fallback chain this app would otherwise
 * hand-roll, so it is requested explicitly rather than relying on the
 * `'best fit'` default whose behaviour is implementation-defined.
 * <https://www.rfc-editor.org/rfc/rfc4647.html>
 *
 * ## Formatter caching
 *
 * `Intl.*` constructors are expensive — they resolve a locale and build a
 * pattern — and these run inside render and inside comparators, where a
 * comparator is called O(n log n) times per sort. Every formatter is therefore
 * memoized on `(kind, locale, options)`. The locale is part of the key, so a
 * language switch simply misses the cache once; nothing needs invalidating.
 *
 * ## Known limitations, stated explicitly
 *
 *  1. **`Intl.DurationFormat` is NOT used**, because it does not exist on the
 *     minimum supported runtime: `typeof Intl.DurationFormat === 'undefined'`
 *     on Node 22 (the contributor floor; CI and Electron run Node 24, which
 *     has it). `fmtUnit` uses `NumberFormat` with `style: 'unit'` instead,
 *     which is Baseline and covers every duration shape this app renders (a
 *     single value plus a unit). Revisit when the supported floor reaches a
 *     runtime with `DurationFormat`.
 *  2. **Locale-formatted digits are not machine-readable.** `hi` groups as
 *     `12,34,567` (Indian grouping) and `bn` renders `১২,৩৪,৫৬৭` in Bengali
 *     digits by default — correct for display, catastrophic for a CSS length, a
 *     URL parameter, a JSON payload or an `aria-valuenow`. Nothing in this
 *     module may be used for a value that is later parsed, serialised, or
 *     interpolated into a style. Those sites keep bare arithmetic and are
 *     recorded in `localeFormatting.test.ts`'s allowlist with a reason.
 *  3. **No timezone is pinned.** Dates render in the host timezone, which is
 *     correct for a desktop app showing local activity times. Tests MUST pass an
 *     explicit `timeZone` — the vitest setup pins no `TZ`, so an unpinned date
 *     assertion is machine-dependent and will flake in CI.
 *  4. **`en-XA` passes straight through to `Intl`**, where it resolves to `en`
 *     (verified: `new Intl.NumberFormat('en-XA').resolvedOptions().locale === 'en'`).
 *     That is the correct behaviour for a pseudolocale: its job is to expose
 *     untranslated *strings*, and a formatted number is not a translatable
 *     string. Accented pseudolocale text around a plain `1,234.5` is not a
 *     finding.
 */

import { i18next } from './index'

/**
 * The language every formatter here resolves against.
 *
 * `resolvedLanguage` rather than `language`: `language` is what was *requested*
 * (possibly `zh` or an unsupported tag), while `resolvedLanguage` is the one
 * i18next actually selected from `supportedLngs` after fallback. Formatting
 * against the requested tag would let a language i18next rejected still steer
 * `Intl`, so the UI text and the dates around it could disagree.
 *
 * Falls back to `'en'` when i18next has not initialized — reached only by a unit
 * test importing this module in isolation, but it must not throw there.
 */
export function activeLocale(): string {
  return i18next.resolvedLanguage || i18next.language || 'en'
}

/**
 * Memo for constructed `Intl` formatters, keyed on kind + locale + options.
 *
 * Unbounded by design: the key space is (11 languages × the handful of option
 * shapes this module offers), so it settles in the low dozens of entries and
 * never grows with data. A user-supplied option object could in principle widen
 * it, which is why `fmtUnit` takes `unit` as a discrete argument rather than
 * letting callers pass arbitrary `Intl.NumberFormatOptions` through.
 */
const formatters = new Map<string, unknown>()

function memo<T>(kind: string, locale: string, options: unknown, build: () => T): T {
  const key = `${kind}|${locale}|${JSON.stringify(options ?? null)}`
  const hit = formatters.get(key)
  if (hit !== undefined) return hit as T
  const made = build()
  formatters.set(key, made)
  return made
}

/** Test seam: drop the formatter memo so a test can switch language mid-file. */
export function __resetFormatterCache(): void {
  formatters.clear()
}

/**
 * Coerce the several timestamp shapes this codebase carries into a `Date`.
 *
 * The API layer is not consistent — some fields are ISO strings, some are epoch
 * seconds (cron, artifacts), some epoch milliseconds (GitHub-derived data in
 * Issue Radar). Seconds-vs-milliseconds has already produced a shipped bug (a
 * millisecond value read as seconds rendered "just now" forever; see
 * `RemoteArtifactCard.test.tsx`), so the disambiguation lives here once:
 * a number below `SECONDS_CEILING` is treated as seconds.
 *
 * Returns `null` for anything unparseable, so callers render their own
 * placeholder rather than "Invalid Date".
 */
const SECONDS_CEILING = 1e11

export function toDate(value: Date | string | number | null | undefined): Date | null {
  if (value === null || value === undefined || value === '') return null
  if (value instanceof Date) return Number.isNaN(value.getTime()) ? null : value
  if (typeof value === 'number') {
    if (!Number.isFinite(value) || value <= 0) return null
    const ms = value < SECONDS_CEILING ? value * 1000 : value
    const d = new Date(ms)
    return Number.isNaN(d.getTime()) ? null : d
  }
  const d = new Date(value)
  return Number.isNaN(d.getTime()) ? null : d
}

/* ------------------------------------------------------------------ numbers */

export type NumberOptions = Omit<Intl.NumberFormatOptions, 'localeMatcher'>

/**
 * A count, size or measurement for DISPLAY.
 *
 * Never use for a value that is parsed, serialised, or interpolated into CSS —
 * see limitation 2 in the file header.
 */
export function fmtNumber(value: number, options?: NumberOptions): string {
  if (!Number.isFinite(value)) return '—'
  const locale = activeLocale()
  return memo('number', locale, options, () =>
    new Intl.NumberFormat(locale, { localeMatcher: 'lookup', ...options }),
  ).format(value)
}

/** Trim credit noise: 2 decimals under 10, 1 decimal beyond ("0.25", "12.5"). */
export function fmtCredits(credits: number): string {
  const digits = credits >= 10 ? 1 : 2
  return fmtNumber(credits, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })
}

/**
 * A ratio in 0..1 as a percentage.
 *
 * Takes the RATIO, not the pre-multiplied percentage, so the `× 100` cannot be
 * applied twice and so the locale decides where the `%` sits and whether a
 * (non-breaking) space precedes it — `46%` in en, `46 %` in de/fr/ru.
 */
export function fmtPercent(ratio: number, options?: NumberOptions): string {
  if (!Number.isFinite(ratio)) return '—'
  return fmtNumber(ratio, { style: 'percent', maximumFractionDigits: 0, ...options })
}

/** Money. `currency` is an ISO 4217 code; placement and symbol are the locale's. */
export function fmtCurrency(value: number, currency = 'USD', options?: NumberOptions): string {
  if (!Number.isFinite(value)) return '—'
  return fmtNumber(value, { style: 'currency', currency, ...options })
}

/**
 * A value with a unit — durations (`1.5s`, `90m`) and sizes (`512MB`).
 *
 * This is the `Intl.DurationFormat` replacement (limitation 1). `unitDisplay`
 * defaults to `'narrow'` because these render in tight chrome: log rows, tab
 * bars, file pickers. `narrow` is what makes en read `512MB` and `90m` rather
 * than `512 megabytes` and `90 minutes`, while still letting de say `90 Min.`
 * and ru `90 мин`.
 */
export type FormatUnit =
  | 'millisecond' | 'second' | 'minute' | 'hour' | 'day' | 'week' | 'month' | 'year'
  | 'byte' | 'kilobyte' | 'megabyte' | 'gigabyte' | 'terabyte'
  // Rates. ECMA-402 sanctions a fixed set of `-per-` compounds, and these two are
  // in it — so a download speed renders fully localized (`500 кБ/c` in ru)
  // instead of a localized size with a Latin `/s` welded on.
  | 'kilobyte-per-second' | 'megabyte-per-second'

export function fmtUnit(value: number, unit: FormatUnit, options?: NumberOptions): string {
  if (!Number.isFinite(value)) return '—'
  // A quantity is one atom: never let a line break fall between the number and
  // its unit, or inside the digit grouping. CLDR does not guarantee this for us
  // and is not even self-consistent about it — measured with `narrow`, en emits
  // `5,289MB` (no separator at all), fr uses U+202F, ru uses U+00A0 for bytes but
  // a plain U+0020 for hours, and de uses U+0020 for MB yet U+00A0 for GB. zh-CN
  // uses a plain U+0020, which is a UAX #14 break opportunity, so `1,280 GB`
  // could render with `GB` orphaned on its own line while the same value in
  // English could not. Promoting every plain space in the formatted quantity to
  // U+00A0 (class GL: breaks prohibited on both sides) makes the behaviour the
  // same in all 12 locales. Only plain spaces are touched; U+202F and U+00A0 are
  // already non-breaking.
  return fmtNumber(value, { style: 'unit', unit, unitDisplay: 'narrow', ...options }).replace(/ /g, '\u00A0')
}

/**
 * A compound duration — `1h 2m 3s`, `6m 38s`, `2h 15m`.
 *
 * Takes the parts ALREADY SPLIT by the caller, deliberately. Every duration
 * surface in this app has its own granularity rule (a log row drops to `ms`
 * under a second, a tab pill never shows seconds and floors at `<1m`, a turn
 * counter shows one decimal under ten seconds), and those rules are product
 * decisions, not formatting ones. Passing parts in keeps each caller's
 * thresholds exactly as they were and localizes only the rendering — so English
 * output is unchanged while zh gets `6分钟38秒` and ru `6 мин 38 с`.
 *
 * The join is `ListFormat` with `type: 'unit'`, not a hardcoded space. That
 * matters: narrow unit lists are space-joined in en/ru/fr but comma-joined in
 * de (`6 Min., 38 Sek.`) and joined with NOTHING in zh — a literal space would
 * put a stray gap in `6分钟 38秒`.
 *
 * `Intl.DurationFormat` would do all of this in one call and is deliberately
 * not used: it is `undefined` on the Node 22 contributor floor (see
 * limitation 1 in the file header).
 *
 * Every part passed is RENDERED, including zeros. That is deliberate: several
 * callers depend on a zero surviving — `fmtTurnElapsed` rounds to whole seconds
 * before splitting specifically so 119.6s reads `2m 0s` and never the invalid
 * `1m 60s`, and dropping the zero there would silently undo that fix. Pass
 * `dropZero` for the opposite case, where the caller previously branched to
 * avoid printing a leading `0h`.
 */
export function fmtDuration(
  parts: Array<[number, FormatUnit]>,
  options?: NumberOptions & { dropZero?: boolean },
): string {
  const { dropZero, ...numberOptions } = options ?? {}
  const finite = parts.filter(([value]) => Number.isFinite(value))
  if (finite.length === 0) return '—'
  let used = finite
  if (dropZero) {
    const nonZero = finite.filter(([value]) => value !== 0)
    // All-zero keeps the last unit so the result reads `0s`, never empty.
    used = nonZero.length > 0 ? nonZero : finite.slice(-1)
  }
  const rendered = used.map(([value, unit]) => fmtUnit(value, unit, numberOptions))
  if (rendered.length === 1) return rendered[0]
  const locale = activeLocale()
  return memo('durationList', locale, null, () =>
    new Intl.ListFormat(locale, { localeMatcher: 'lookup', type: 'unit', style: 'narrow' }),
  ).format(rendered)
}

/**
 * `value` rounded to the `digits` decimal places it will be PRINTED with.
 *
 * Every band below picks a unit by comparing against that band's ceiling, then
 * prints the value with fewer digits than it compared. Testing the raw value
 * lets a band print its own upper limit — `60s` from the sub-minute band,
 * `1,000kB` from the kilobyte band — because the printed rounding crosses a
 * ceiling the test had already cleared. Rounding to the displayed precision
 * first moves such a value into the next band instead.
 *
 * Rounds half away from zero, which is `Intl.NumberFormat`'s default
 * `roundingMode`, so the band test and the digits `Intl` prints agree on every
 * finite input. A bare `Math.round` rounds a negative half toward zero and
 * would disagree.
 */
function roundToDigits(value: number, digits: number): number {
  const step = 10 ** digits
  return Math.sign(value) * Math.round(Math.abs(value) * step) / step
}

/**
 * A settled elapsed time — `4.2s`, `37s`, `6m 38s`, `1h 7m 11s`.
 *
 * Three bands under an hour, because the useful precision changes with magnitude: a sub-10s
 * step is interesting to a tenth, a sub-minute one is not, and past a minute the
 * minutes place carries the meaning. Rounds to whole seconds BEFORE splitting so
 * a value like 119.6s reads `2m 0s` and never the invalid `1m 60s` — flooring
 * minutes first and rounding the remainder can push it to 60.
 *
 * Each band is tested on the value rounded to the precision that band prints,
 * so a span that would read as the band's own ceiling steps up instead: 59.6s
 * is `1m 0s`, never `60s`, and 9.96s is `10s`, never `10.0s`.
 *
 * A fourth band starts at an hour (`1h 7m 11s`), so a long run never reads as
 * `67m 11s`.
 *
 * The seconds place is kept above a minute rather than dropped, so a series
 * steps `2m 1s` -> `2m 0s` -> `59s` instead of collapsing to a bare `2m`.
 *
 * Non-finite input renders the same em dash `fmtDuration` uses, so a caller can
 * hand over an unmeasurable span without branching.
 *
 * `fmtTurnElapsed` (chat) delegates here. The remaining-time label in
 * `ToolCallLine` is a countdown, not an elapsed figure, and keeps its own bands.
 */
export function fmtElapsed(ms: number, options?: { wholeSeconds?: boolean }): string {
  if (!Number.isFinite(ms)) return '—'
  const s = ms / 1000
  const tenths = roundToDigits(s, 1)
  const total = Math.round(s)
  // Each band prints the very value it was chosen by, so the two cannot disagree.
  // `wholeSeconds` drops the tenths band for a live 1 Hz counter, whose input is
  // already whole seconds and would otherwise read `4.0s`, `5.0s`, ...
  if (tenths < 10 && !options?.wholeSeconds) return fmtUnit(tenths, 'second', { maximumFractionDigits: 1, minimumFractionDigits: 1 })
  if (total < 60) return fmtUnit(total, 'second', { maximumFractionDigits: 0 })
  // Past an hour the hours place carries the meaning: `1h 7m 11s`, not `67m 11s`.
  if (total < 3600) return fmtDuration([[Math.floor(total / 60), 'minute'], [total % 60, 'second']])
  return fmtDuration([[Math.floor(total / 3600), 'hour'], [Math.floor((total % 3600) / 60), 'minute'], [total % 60, 'second']])
}

/**
 * An abbreviated large number — `1.2K`, `15.3K`, `2.4M`.
 *
 * `notation: 'compact'` rather than a hand-rolled `/1000 + 'K'` ladder, because
 * the threshold and the suffix are both language-specific: zh abbreviates on
 * 万 (10^4), so `15300` is `1.5万` and `1234` does not abbreviate at all, while
 * de has no short form at these magnitudes and renders `15.300`. A `K` suffix
 * is an English fact, not a numeric one.
 *
 * Note this changes rendered WIDTH per locale (de gets wider, zh narrower).
 * Callers in tight chrome should confirm the container tolerates it.
 */
export function fmtCompact(value: number, options?: NumberOptions): string {
  if (!Number.isFinite(value)) return '—'
  return fmtNumber(value, { notation: 'compact', maximumFractionDigits: 1, ...options })
}

/**
 * A byte size — `512kB`, `1.5MB`, `2.3GB`.
 *
 * One implementation replacing four that disagreed with each other: the same
 * file could render `1.5 KB` in the skill browser, `2KB` in the file picker and
 * `1.50 KB` in the storage debugger, differing in spacing, precision AND
 * capitalisation.
 *
 * **Divides by 1000, not 1024.** The previous helpers all divided by 1024 while
 * labelling the result `KB`, which is the decimal SI unit — so a "1.0 KB" file
 * was really 1024 bytes. CLDR's `kilobyte` means 1000 bytes, and `Intl` offers
 * no binary (`kibibyte`) unit at all, so labelling 1024-based arithmetic with a
 * CLDR unit would keep that mislabel and make it look sanctioned. Sizes now say
 * what they mean. At one decimal the visible difference is under a rounding
 * step for most values.
 *
 * English output changes `KB` → `kB`, which is the SI spelling CLDR uses.
 *
 * Each band is chosen by, and then prints, the value rounded to the digits that
 * band shows — so a size never renders as its own band's ceiling: 999,950 bytes
 * is `1MB`, not `1,000kB`. The digit count comes from the caller's
 * `maximumFractionDigits` when it supplies one, because that is what decides
 * the printed precision. The terabyte band has no ceiling, so it rounds nothing
 * and leaves the digits entirely to `Intl`.
 */
export function fmtBytes(bytes: number, options?: NumberOptions): string {
  if (!Number.isFinite(bytes)) return '—'
  const digits = options?.maximumFractionDigits
  const b = roundToDigits(bytes, digits ?? 0)
  if (Math.abs(b) < 1000) return fmtUnit(b, 'byte', { maximumFractionDigits: 0, ...options })
  const kb = roundToDigits(bytes / 1000, digits ?? 1)
  if (Math.abs(kb) < 1000) return fmtUnit(kb, 'kilobyte', { maximumFractionDigits: 1, ...options })
  const mb = roundToDigits(bytes / 1000 ** 2, digits ?? 1)
  if (Math.abs(mb) < 1000) return fmtUnit(mb, 'megabyte', { maximumFractionDigits: 1, ...options })
  const gb = roundToDigits(bytes / 1000 ** 3, digits ?? 1)
  if (Math.abs(gb) < 1000) return fmtUnit(gb, 'gigabyte', { maximumFractionDigits: 1, ...options })
  return fmtUnit(bytes / 1000 ** 4, 'terabyte', { maximumFractionDigits: 1, ...options })
}

/* -------------------------------------------------------------------- dates */

export type DateOptions = Omit<Intl.DateTimeFormatOptions, 'localeMatcher'>

/**
 * The individual date/time COMPONENT options.
 *
 * ECMA-402 `CreateDateTimeFormat` step 37 throws `TypeError` if `dateStyle` or
 * `timeStyle` is combined with any of these. The style-preset helpers below
 * supply a style, so accepting a component alongside it would compile and then
 * throw at runtime for every user — which is exactly what happened during this
 * migration: `fmtTime(d, { hour: '2-digit', minute: '2-digit' })` type-checked
 * and would have emptied the command palette. Splitting the option types makes
 * that a compile error instead of a production one.
 */
type DateComponent =
  | 'weekday' | 'era' | 'year' | 'month' | 'day' | 'dayPeriod'
  | 'hour' | 'minute' | 'second' | 'fractionalSecondDigits'

/** Options accepted alongside a `dateStyle`/`timeStyle` preset. */
export type DateStyleOptions = Omit<DateOptions, DateComponent | 'dateStyle' | 'timeStyle'>

/** Options for building a date from explicit components, with no preset. */
export type DateFieldOptions = Omit<DateOptions, 'dateStyle' | 'timeStyle'>

function dateTime(value: Date | string | number | null | undefined, options: DateOptions): string {
  const d = toDate(value)
  if (!d) return '—'
  const locale = activeLocale()
  return memo('datetime', locale, options, () =>
    new Intl.DateTimeFormat(locale, { localeMatcher: 'lookup', ...options }),
  ).format(d)
}

/** Calendar date. en `Jul 30, 2026` · zh-CN `2026年7月30日` · de `30.07.2026`. */
export function fmtDate(value: Date | string | number | null | undefined, options?: DateStyleOptions): string {
  return dateTime(value, { dateStyle: 'medium', ...options })
}

/** Clock time. en `3:04 PM` · zh-CN/de/ru `15:04` — the locale decides 12h vs 24h. */
export function fmtTime(value: Date | string | number | null | undefined, options?: DateStyleOptions): string {
  return dateTime(value, { timeStyle: 'short', ...options })
}

/** Date and time together. */
export function fmtDateTime(value: Date | string | number | null | undefined, options?: DateStyleOptions): string {
  return dateTime(value, { dateStyle: 'medium', timeStyle: 'short', ...options })
}

/**
 * The three ALL-NUMERIC widths, which are what `Date.prototype.toLocale*String()`
 * renders when called with no options.
 *
 * These exist so the migration off the host locale can be *exactly* that and
 * nothing else. A bare `d.toLocaleString()` is not a neutral call: it picks the
 * locale's own default width, which is all-numeric and includes the second.
 * Reaching for `fmtDateTime` instead would have quietly restyled 21 call sites
 * from `7/30/2026, 3:04:05 PM` to `Jul 30, 2026, 3:04 PM` — a different date
 * width AND a dropped second — inside a PR whose subject is which locale the
 * formatter reads. `format.test.ts` pins each one against the platform call it
 * replaced, in all ten shipped locales, so "English is byte-identical" is
 * asserted rather than asserted-about.
 *
 * Prefer `fmtDate` / `fmtTime` / `fmtDateTime` for NEW code: `Jul 30, 2026` is
 * unambiguous where `30/07/2026` is not. These three are for faithfully porting
 * a call site that was already rendering the numeric width.
 */
const NUMERIC_DATE = { year: 'numeric', month: 'numeric', day: 'numeric' } as const
const NUMERIC_TIME = { hour: 'numeric', minute: '2-digit', second: '2-digit' } as const

/** What `d.toLocaleDateString()` rendered — en `7/30/2026`, de `30.7.2026`. */
export function fmtDateNumeric(value: Date | string | number | null | undefined): string {
  return dateTime(value, NUMERIC_DATE)
}

/** What `d.toLocaleTimeString()` rendered, second included — en `3:04:05 PM`. */
export function fmtTimeNumeric(value: Date | string | number | null | undefined): string {
  return dateTime(value, NUMERIC_TIME)
}

/** What `d.toLocaleString()` rendered — en `7/30/2026, 3:04:05 PM`. */
export function fmtDateTimeNumeric(value: Date | string | number | null | undefined): string {
  return dateTime(value, { ...NUMERIC_DATE, ...NUMERIC_TIME })
}

/**
 * A date built from explicit COMPONENTS rather than a style preset.
 *
 * `Intl.DateTimeFormat` throws if `dateStyle`/`timeStyle` is combined with a
 * component like `weekday` or `hour`, and the helpers above supply a style — so
 * a caller wanting "just the weekday", "Jul 30" or "15:04 with explicit hour and
 * minute" needs an entry point that never sets a style. The option types keep
 * the two apart, so reaching for the wrong one fails to compile.
 */
export function fmtDateFields(
  value: Date | string | number | null | undefined,
  options: DateFieldOptions,
): string {
  return dateTime(value, options)
}

/**
 * A weekday name by ISO index — 1 = Monday … 7 = Sunday.
 *
 * The index is the contract, the name is the rendering. That split is the whole
 * point: this app's weekday arrays double as cron day-of-week data, so the
 * NUMBER must stay stable while the LABEL becomes translatable. Callers keep
 * indexing by number exactly as before and only the displayed string changes.
 *
 * Implemented against a fixed reference week in UTC (2024-01-01 was a Monday)
 * with `timeZone: 'UTC'` pinned, so the mapping index → name can never be
 * shifted by the host timezone.
 */
const ISO_WEEK_REFERENCE = [1, 2, 3, 4, 5, 6, 7] as const

export function fmtWeekday(isoIndex: number, style: 'long' | 'short' | 'narrow' = 'short'): string {
  const idx = Math.trunc(isoIndex)
  if (!ISO_WEEK_REFERENCE.includes(idx as 1)) return '—'
  // 2024-01-01 is a Monday, so day-of-month === ISO weekday index for 1..7.
  return dateTime(new Date(Date.UTC(2024, 0, idx)), { weekday: style, timeZone: 'UTC' })
}

/* ----------------------------------------------------------- relative time */

export type RelativeStyle = 'long' | 'short' | 'narrow'

const RELATIVE_THRESHOLDS: Array<{ unit: Intl.RelativeTimeFormatUnit; seconds: number }> = [
  { unit: 'year', seconds: 365 * 86400 },
  { unit: 'month', seconds: 30 * 86400 },
  { unit: 'day', seconds: 86400 },
  { unit: 'hour', seconds: 3600 },
  { unit: 'minute', seconds: 60 },
  { unit: 'second', seconds: 1 },
]

/**
 * Elapsed time as the locale words it.
 *
 * `numeric: 'auto'` is deliberate: it is what lets CLDR answer with *yesterday*
 * / *昨天* / *vorgestern* instead of a mechanical "1 day ago", which is the
 * whole reason to use the platform rather than a ladder of template literals.
 * It also gives `now` for the sub-threshold case, so callers no longer carry
 * their own "just now" literal.
 *
 * `style: 'narrow'` is the default because these sit in dense rows and narrow
 * English stays compact (`45s ago`, `2m ago`, `3h ago`, `5d ago`).
 *
 * English idioms, deliberate:
 *   - sub-10-second now reads `now`
 *   - exactly one day back reads `yesterday`
 *
 * Future timestamps format forwards (`in 5m`) rather than being clamped, so
 * clock skew is visible instead of silently reading as "now".
 *
 * `unit` pins the unit instead of picking the largest that fits. A caller that
 * has already reduced its input to whole CALENDAR days needs this: with an
 * auto-picked unit a zero delta means "under one second" and renders "now",
 * which is wrong for something that happened earlier the same day. Pinning
 * `'day'` makes a zero delta render "today" / "今天" / "heute".
 */
export function fmtRelative(
  value: Date | string | number | null | undefined,
  options?: { style?: RelativeStyle; now?: Date | number; unit?: Intl.RelativeTimeFormatUnit },
): string {
  const d = toDate(value)
  if (!d) return '—'
  const style = options?.style ?? 'narrow'
  const nowMs = options?.now instanceof Date ? options.now.getTime() : (options?.now ?? Date.now())
  const deltaSeconds = (d.getTime() - nowMs) / 1000
  const magnitude = Math.abs(deltaSeconds)

  const locale = activeLocale()
  const rtf = memo('relative', locale, style, () =>
    new Intl.RelativeTimeFormat(locale, { localeMatcher: 'lookup', numeric: 'auto', style }),
  )

  if (options?.unit) {
    const seconds = RELATIVE_THRESHOLDS.find((t) => t.unit === options.unit)?.seconds ?? 1
    return rtf.format(Math.trunc(deltaSeconds / seconds), options.unit)
  }

  // Below one second there is no unit to pick; `format(0, 'second')` is what
  // renders the locale's idiomatic "now" / "现在" / "jetzt".
  if (magnitude < 1) return rtf.format(0, 'second')

  for (const { unit, seconds } of RELATIVE_THRESHOLDS) {
    if (magnitude >= seconds) {
      // Truncate toward zero so an elapsed 119s reads "1m ago", never "2m ago" —
      // an age must not round up into a future it has not reached.
      return rtf.format(Math.trunc(deltaSeconds / seconds), unit)
    }
  }
  return rtf.format(0, 'second')
}

/* --------------------------------------------------------------------- lists */

/**
 * Join items the way the language joins them.
 *
 * `join(', ')` plus a translated `" and "` re-introduces concatenation *after*
 * i18n: CJK does not use a comma-space separator at all (zh renders
 * `A、B和C`), and the conjunction's position is language-specific. `ListFormat`
 * is the only correct answer.
 *
 * Only for natural-language enumerations shown to a user. A technical list — a
 * CSS value, a path, a payload, anything with a `split()` on the other side —
 * must keep its literal separator.
 */
export function fmtList(
  items: readonly string[],
  options?: { type?: 'conjunction' | 'disjunction' | 'unit'; style?: 'long' | 'short' | 'narrow' },
): string {
  const type = options?.type ?? 'conjunction'
  const style = options?.style ?? 'long'
  const locale = activeLocale()
  return memo('list', locale, { type, style }, () =>
    new Intl.ListFormat(locale, { localeMatcher: 'lookup', type, style }),
  ).format(items.filter((item) => item !== ''))
}

/* ----------------------------------------------------------------- collation */

/**
 * The name of a BCP-47 locale code in the app language: `vi-VN` reads
 * "Vietnamese (Vietnam)" in English and "越南语（越南）" in Chinese.
 *
 * For a list of codes a person picks from. Returns the code itself when `Intl`
 * has no name for it or rejects it, so an unusual code is still shown rather
 * than dropped.
 */
export function fmtLanguageName(code: string): string {
  const locale = activeLocale()
  try {
    const names = memo('languageName', locale, null, () =>
      new Intl.DisplayNames(locale, { type: 'language', fallback: 'code' }),
    )
    return names.of(code) || code
  } catch {
    return code
  }
}

/**
 * A collator for the active language.
 *
 * `numeric: true` is on by default because the strings this app sorts are
 * user-named things that routinely carry digits — `reviewer-2` must precede
 * `reviewer-10`, and byte order puts it after. `sensitivity: 'base'` makes the
 * order case- and accent-insensitive, so `apple` and `Apple` no longer land in
 * two different places in one list.
 *
 * NOT for machine identifiers. Sorting ISO-8601 timestamps or filesystem paths
 * needs byte order — collation weights `-`, `:` and `/` at a lower level, which
 * makes "chronological" and "parent before child" host-dependent. Those call
 * sites keep plain comparison and are recorded in the gate's allowlist.
 */
export function collator(options?: Intl.CollatorOptions): Intl.Collator {
  const locale = activeLocale()
  const resolved: Intl.CollatorOptions = {
    localeMatcher: 'lookup',
    numeric: true,
    sensitivity: 'base',
    ...options,
  }
  return memo('collator', locale, resolved, () => new Intl.Collator(locale, resolved))
}

/**
 * Comparator for user-visible text, for direct use in `.sort()`.
 *
 * `[...names].sort(compareText)` replaces `.sort((a, b) => a.localeCompare(b))`,
 * which compares in the HOST locale and ignores the app's language entirely.
 */
export function compareText(a: string, b: string): number {
  return collator().compare(a, b)
}
