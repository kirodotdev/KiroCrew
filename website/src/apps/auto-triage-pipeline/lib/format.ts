// Presentation helpers for the pipeline view — credits, duration and
// relative-time rendering. No React, no DOM, no ambient clock: every function
// takes its inputs explicitly (relative time takes `now`), so the whole module
// is unit-testable without a render and fully deterministic.
//
// Duration and relative-time rendering route their value+unit output through the
// app's locale seam (`../../../i18n/format`): the digits are localized and each
// unit is the active language's own spelling, so nothing welds a Latin `ms`/`s`
// onto a number. That seam reads the active language at call time (a plain
// function, not a hook, and not the wall clock), so the "no ambient clock"
// property is preserved — the tests pin English, the default the setup installs.
//
// The inputs come folded from a LIVE append-only log, so every value can be
// null, zero, negative (a clock skew between two hosts), NaN or Infinity. Each
// helper DEGRADES to a readable placeholder rather than emitting "NaN", "-3s" or
// "Infinity" into an operator's table.

import { fmtDuration, fmtNumber, fmtRelative, fmtUnit, type FormatUnit } from '../../../i18n/format'

/** Shown wherever a value is absent (null) or not a finite number. A single
 * constant so the table reads consistently and a view can special-case it. */
export const EMPTY_PLACEHOLDER = '—'

/** True only for a real, finite number. Guards every helper's entry so null,
 * undefined, NaN and ±Infinity all take the placeholder path. */
function isFiniteNumber(v: number | null | undefined): v is number {
  return typeof v === 'number' && Number.isFinite(v)
}

/**
 * Render a credit total readably at every magnitude the pipeline produces.
 *
 * Credits span three orders on the real trail — a single cheap item near 17.75,
 * a heavily-retried one near 4059.65 — so a fixed precision is wrong at one end
 * or the other: two decimals on 4059.65 is noise, zero decimals on 17.75 loses
 * the item. The rule keeps two decimals below 100 (where the fraction carries
 * signal) and rounds to a whole credit at or above it (where it does not),
 * always with a thousands separator.
 *
 *   17.75   -> "17.75"
 *   4059.65 -> "4,060"
 *
 * Null / non-finite -> placeholder. A negative value (should not occur, but the
 * source is untrusted) is rendered with its sign rather than hidden.
 */
export function formatCredits(value: number | null | undefined): string {
  if (!isFiniteNumber(value)) return EMPTY_PLACEHOLDER
  const abs = Math.abs(value)
  const fractionDigits = abs < 100 ? 2 : 0
  // The repo's own formatter, which resolves the ACTIVE locale. A hardcoded
  // 'en-US' here would group and punctuate every reader's digits as English --
  // 4.060 reads as four in most of Europe, so this is a wrong number, not merely
  // an untranslated one.
  return fmtNumber(value, {
    minimumFractionDigits: fractionDigits,
    maximumFractionDigits: fractionDigits,
  })
}

/**
 * Render a wall-clock duration given in MILLISECONDS as a compact human string.
 *
 * The largest non-zero unit leads and at most two units show, so a reading stays
 * glanceable: "1h 12m", "3m 4s", "820ms", "45s". Sub-second durations render in
 * milliseconds; an exact zero is "0s" (a real "it took no measurable time",
 * distinct from the placeholder for "unknown").
 *
 * Null / non-finite -> placeholder. A negative duration (clock skew across two
 * hosts) is clamped to 0 rather than printing "-2m".
 */
export function formatDuration(ms: number | null | undefined): string {
  if (!isFiniteNumber(ms)) return EMPTY_PLACEHOLDER
  const total = Math.max(0, ms)
  if (total === 0) return fmtUnit(0, 'second')
  if (total < 1000) return fmtUnit(Math.round(total), 'millisecond')

  const totalSeconds = Math.floor(total / 1000)
  const days = Math.floor(totalSeconds / 86400)
  const hours = Math.floor((totalSeconds % 86400) / 3600)
  const minutes = Math.floor((totalSeconds % 3600) / 60)
  const seconds = totalSeconds % 60

  const parts: Array<[number, FormatUnit]> = [
    [days, 'day'],
    [hours, 'hour'],
    [minutes, 'minute'],
    [seconds, 'second'],
  ]
  // Take at most two units, starting at the largest non-zero one, dropping
  // trailing-zero units so "1h 0m" reads "1h". `fmtDuration` renders each part
  // in the active locale's own unit spelling and joins them the way the language
  // joins a unit list (a space in en, a comma in de, nothing in zh) — so the
  // unit is translatable and the digits are localized, which a `${n}${unit}`
  // template literal could never be.
  const firstIndex = parts.findIndex(([n]) => n > 0)
  const shown = parts.slice(firstIndex, firstIndex + 2).filter(([n]) => n > 0)
  return fmtDuration(shown)
}

/**
 * Render an epoch-SECONDS timestamp as a relative time against a MILLISECOND
 * clock.
 *
 * A thin adapter over the repo's `fmtRelative`, NOT a second implementation. An
 * earlier version of this file hand-rolled a unit ladder and welded its own "ago"
 * wrapper, which shadowed a seam that was already better: `fmtRelative` uses
 * `Intl.RelativeTimeFormat` with `numeric: 'auto'`, so it answers with the locale's
 * idiom ("yesterday", "\u6628\u5929") instead of a mechanical "1d ago", handles the
 * sub-threshold "now" case itself, and formats future timestamps forwards so clock
 * skew stays visible. It is also memoized per locale.
 *
 * What remains here is the unit mismatch this app's payload creates: the folded
 * timestamps are epoch SECONDS while every caller's clock is `Date.now()` in
 * milliseconds. Naming both is the guard -- passing a millisecond clock into a
 * seconds parameter is what rendered "20,669,484d ago" in a real view.
 *
 * Null / non-finite (either argument) -> placeholder.
 */
export function formatRelativeTime(
  epochSeconds: number | null | undefined,
  nowMs: number | null | undefined,
): string {
  if (!isFiniteNumber(epochSeconds) || !isFiniteNumber(nowMs)) return EMPTY_PLACEHOLDER
  return fmtRelative(epochSeconds, { now: nowMs })
}
