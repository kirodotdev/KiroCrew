/**
 * Locale-aware formatting — the single seam for dates, numbers, lists and collation.
 *
 * ## The defect this closes
 *
 * Before this module there were ~300 formatting call sites and **not one of them
 * could consult the selected language**, because there was nowhere to ask. Every
 * call was `toLocaleString()`, `toLocaleDateString([])` or
 * `toLocaleTimeString(undefined, {…})` — all three spellings mean *host* locale,
 * i.e. the browser's, not the app's `dashboard.language`. `LanguageProvider`
 * correctly sets `<html lang>`, but `<html lang>` has no effect on `Intl`. So a
 * dashboard running in Chinese on an en-US browser rendered `7/30/2026` and
 * `Jul 30` inside otherwise-Chinese UI.
 *
 * The root cause was one missing module, not 300 independent bugs. This is that
 * module: every formatter here reads the ACTIVE UI language at call time.
 *
 * ## Why plain functions and not hooks
 *
 * Same reasoning as `i18nT` in `./t.ts`, and deliberately the same shape so the
 * two are used side by side without ceremony: formatting happens inside
 * `.map()` callbacks, in comparator functions passed to `.sort()`, and in plain
 * helper modules with no component around them — all positions a hook cannot
 * legally go. A language switch remounts the tree (`<App>` is keyed on the
 * active language in `main.tsx`), so a function that reads the language at call
 * time re-evaluates on switch without subscribing to anything.
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
 *     runtime this ships to: `typeof Intl.DurationFormat === 'undefined'` on
 *     Node 20 (the CI and Electron baseline). `fmtUnit` uses `NumberFormat`
 *     with `style: 'unit'` instead, which is Baseline and covers every duration
 *     shape this app renders (a single value plus a unit). Revisit when the
 *     Electron floor reaches a Chromium with `DurationFormat`.
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

export function fmtUnit(value: number, unit: FormatUnit, options?: NumberOptions): string {
  if (!Number.isFinite(value)) return '—'
  return fmtNumber(value, { style: 'unit', unit, unitDisplay: 'narrow', ...options })
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
 * Elapsed time as the locale words it — replaces the hand-rolled `Nm ago`.
 *
 * `numeric: 'auto'` is deliberate: it is what lets CLDR answer with *yesterday*
 * / *昨天* / *vorgestern* instead of a mechanical "1 day ago", which is the
 * whole reason to use the platform rather than a ladder of template literals.
 * It also gives `now` for the sub-threshold case, so callers no longer carry
 * their own "just now" literal.
 *
 * `style: 'narrow'` is the default because the previous hand-rolled output was
 * compact and it sits in dense rows. Narrow English is byte-identical to what
 * this app rendered before for seconds, minutes, hours and multi-day gaps
 * (`45s ago`, `2m ago`, `3h ago`, `5d ago`); only the two deltas below change.
 *
 * English deltas, deliberate and reviewed:
 *   - sub-10-second now reads `now` where it read `just now`
 *   - exactly one day back reads `yesterday` where it read `1d ago`
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
