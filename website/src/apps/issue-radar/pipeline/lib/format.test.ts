/**
 * Tests for the pure presentation helpers (`./format`).
 *
 * These assert the CONTRACT — the exact rendered string at each magnitude and
 * every degraded input (null, NaN, Infinity, negative) — and cover every branch,
 * because the module is pure and the repo enforces a per-file coverage floor.
 */
import { describe, expect, it } from 'vitest'

import {
  EMPTY_PLACEHOLDER,
  formatCredits,
  formatDuration,
  formatRelativeTime,
} from './format'

describe('formatCredits', () => {
  it('keeps two decimals below 100 (small item)', () => {
    // The load-bearing low anchor from the user's brief.
    expect(formatCredits(17.75)).toBe('17.75')
  })

  it('rounds to a whole credit with a thousands separator at/above 100 (retried item)', () => {
    // The load-bearing high anchor: 4059.65 must read as a whole, grouped number.
    expect(formatCredits(4059.65)).toBe('4,060')
  })

  it('renders exactly 100 with no decimals (boundary is inclusive)', () => {
    expect(formatCredits(100)).toBe('100')
  })

  it('renders just under 100 with two decimals', () => {
    expect(formatCredits(99.99)).toBe('99.99')
  })

  it('renders zero as 0.00', () => {
    expect(formatCredits(0)).toBe('0.00')
  })

  it('renders a negative value with its sign rather than hiding it', () => {
    expect(formatCredits(-17.75)).toBe('-17.75')
    expect(formatCredits(-4059.65)).toBe('-4,060')
  })

  it('degrades to the placeholder on null, undefined, NaN and Infinity', () => {
    expect(formatCredits(null)).toBe(EMPTY_PLACEHOLDER)
    expect(formatCredits(undefined)).toBe(EMPTY_PLACEHOLDER)
    expect(formatCredits(Number.NaN)).toBe(EMPTY_PLACEHOLDER)
    expect(formatCredits(Number.POSITIVE_INFINITY)).toBe(EMPTY_PLACEHOLDER)
    expect(formatCredits(Number.NEGATIVE_INFINITY)).toBe(EMPTY_PLACEHOLDER)
  })
})

describe('formatDuration', () => {
  it('renders an exact zero as a real "0 seconds", not the unknown placeholder', () => {
    // 0s is a MEASURED "no time", distinct from the — placeholder for "unknown".
    const out = formatDuration(0)
    expect(out).not.toBe(EMPTY_PLACEHOLDER)
    expect(out).toBe('0s')
  })

  it('renders sub-second durations in the millisecond unit', () => {
    expect(formatDuration(820)).toBe('820ms')
    expect(formatDuration(1)).toBe('1ms')
    expect(formatDuration(999)).toBe('999ms')
    // Rounds to the nearest whole millisecond.
    expect(formatDuration(45.4)).toBe('45ms')
  })

  it('renders whole seconds with the second unit', () => {
    expect(formatDuration(45_000)).toBe('45s')
    expect(formatDuration(1000)).toBe('1s')
  })

  it('renders a minutes+seconds compound: both parts, both units, largest first', () => {
    const out = formatDuration(184_000) // 3m 4s
    expect(out).toBe('3m 4s')
    // Not a single collapsed unit — the two parts really are both present, each
    // carrying its own unit, in largest-first order.
    expect(out.indexOf('3m')).toBeLessThan(out.indexOf('4s'))
  })

  it('renders an hours+minutes compound for a long duration (two largest units)', () => {
    // 1h 12m 30s -> only the two largest units survive.
    const out = formatDuration((1 * 3600 + 12 * 60 + 30) * 1000)
    expect(out).toBe('1h 12m')
    expect(out.indexOf('1h')).toBeLessThan(out.indexOf('12m'))
  })

  it('renders a days+hours compound for a multi-day duration', () => {
    const out = formatDuration((2 * 86400 + 3 * 3600 + 40 * 60) * 1000)
    expect(out).toBe('2d 3h')
  })

  it('drops a trailing zero unit so 1h 0m 5s collapses to just 1h', () => {
    // 1h 0m 5s: window of two from the largest non-zero is [1h, 0m]; the zero
    // minute is dropped, leaving a single "1h" (no stray "0m").
    const out = formatDuration((1 * 3600 + 5) * 1000)
    expect(out).toBe('1h')
    expect(out).not.toMatch(/m/)
  })

  it('renders exactly one minute as 1m', () => {
    expect(formatDuration(60_000)).toBe('1m')
  })

  it('clamps a negative duration (clock skew) to a real 0s, never a signed value', () => {
    const out = formatDuration(-2000)
    expect(out).toBe('0s')
    expect(out).not.toMatch(/-/)
  })

  it('degrades to the placeholder on null, undefined, NaN and Infinity', () => {
    expect(formatDuration(null)).toBe(EMPTY_PLACEHOLDER)
    expect(formatDuration(undefined)).toBe(EMPTY_PLACEHOLDER)
    expect(formatDuration(Number.NaN)).toBe(EMPTY_PLACEHOLDER)
    expect(formatDuration(Number.POSITIVE_INFINITY)).toBe(EMPTY_PLACEHOLDER)
  })
})

describe('formatRelativeTime', () => {
  // The two arguments carry DIFFERENT units on purpose: a payload timestamp in
  // epoch SECONDS, and a clock in MILLISECONDS because that is what `Date.now()`
  // returns. Naming both here keeps the asymmetry visible -- an earlier version of
  // this suite used one number for both, which is exactly why it stayed green
  // while every real caller passed a millisecond clock and the view rendered
  // "20,669,484d ago".
  const nowSec = 1_000_000
  const nowMs = nowSec * 1000

  it('renders the locale idiom for the sub-threshold case', () => {
    // `now`, not a hand-written "just now": the formatter is
    // `Intl.RelativeTimeFormat` with numeric:'auto', so the sub-threshold answer is
    // whatever CLDR says for the active locale. Asserting our own wording here
    // would be asserting the duplicate implementation this file no longer has.
    expect(formatRelativeTime(nowSec, nowMs)).toBe('now')
    expect(formatRelativeTime(nowSec - 4, nowMs)).toBe('4s ago')
    expect(formatRelativeTime(nowSec + 4, nowMs)).toBe('in 4s')
  })

  it('renders a magnitude + unit + "ago" at the 5s boundary', () => {
    const out = formatRelativeTime(nowSec - 5, nowMs)
    expect(out).toBe('5s ago')
    // The magnitude carries a real unit, not a bare number.
    expect(out).toMatch(/5s/)
    expect(out).toMatch(/ago$/)
  })

  it('renders minutes ago', () => {
    expect(formatRelativeTime(nowSec - 5 * 60, nowMs)).toBe('5m ago')
  })

  it('renders hours ago', () => {
    expect(formatRelativeTime(nowSec - 3 * 3600, nowMs)).toBe('3h ago')
  })

  it('renders days ago', () => {
    expect(formatRelativeTime(nowSec - 2 * 86400, nowMs)).toBe('2d ago')
  })

  it('renders a future timestamp as "in X" rather than a negative ago', () => {
    expect(formatRelativeTime(nowSec + 2 * 86400, nowMs)).toBe('in 2d')
    expect(formatRelativeTime(nowSec + 30, nowMs)).toBe('in 30s')
  })

  it('floors within a unit (89s -> 1m ago)', () => {
    expect(formatRelativeTime(nowSec - 89, nowMs)).toBe('1m ago')
  })

  it('degrades to the placeholder when the timestamp is null / non-finite', () => {
    expect(formatRelativeTime(null, nowMs)).toBe(EMPTY_PLACEHOLDER)
    expect(formatRelativeTime(undefined, nowMs)).toBe(EMPTY_PLACEHOLDER)
    expect(formatRelativeTime(Number.NaN, nowMs)).toBe(EMPTY_PLACEHOLDER)
    expect(formatRelativeTime(Number.POSITIVE_INFINITY, nowMs)).toBe(EMPTY_PLACEHOLDER)
  })

  it('degrades to the placeholder when the clock is null / non-finite', () => {
    expect(formatRelativeTime(nowSec, null)).toBe(EMPTY_PLACEHOLDER)
    expect(formatRelativeTime(nowSec, Number.NaN)).toBe(EMPTY_PLACEHOLDER)
  })
})
