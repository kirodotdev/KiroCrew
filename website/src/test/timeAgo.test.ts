import { describe, it, expect } from 'vitest'
import { timeAgo } from '../utils/timeAgo'

describe('timeAgo', () => {
  // Regression guard: a missing/unparseable timestamp (callers pass 0 / NaN
  // when a date is absent) must render '--', not a garbage age (ts=0 → ~20602d).
  it.each([0, NaN, undefined as unknown as number, null as unknown as number, -5, Infinity, -Infinity, 0.5])(
    'returns "--" for a non-positive / non-finite ts (%s)',
    (bad) => {
      expect(timeAgo(bad)).toBe('--')
    },
  )

  it('formats a valid recent timestamp', () => {
    const now = Math.floor(Date.now() / 1000)
    // `now` replaces the former 'just now': it is what CLDR words for a
    // sub-threshold gap, in every language. The other three are byte-identical
    // to the hand-rolled ladder this delegates away to `i18n/format.ts`.
    expect(timeAgo(now)).toBe('now')
    expect(timeAgo(now - 120)).toBe('2m ago')
    expect(timeAgo(now - 7200)).toBe('2h ago')
    expect(timeAgo(now - 172800)).toBe('2d ago')
  })

  it('treats minor clock skew (small future ts) as now', () => {
    const future = Math.floor(Date.now() / 1000) + 5
    // A slightly-future timestamp is a skewed clock, not a scheduled event, so
    // it collapses to "now" rather than reporting "in 5s".
    expect(timeAgo(future)).toBe('now')
  })

  it('reports a badly wrong clock as the future instead of hiding it', () => {
    // Beyond the skew tolerance the future IS shown — a clock an hour ahead is a
    // real problem and should not read as "now".
    // Unit-agnostic: the ts is floored to whole seconds, so an hour ahead
    // lands a hair under the hour boundary and reads '59m' rather than '1h'.
    expect(timeAgo(Math.floor(Date.now() / 1000) + 3600)).toMatch(/^in \d+[smhdy]/)
  })
})
