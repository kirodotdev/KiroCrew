import { describe, it, expect } from 'vitest'
import { RECENT_TINT_COUNT, MAX_RECENT_TINT_COUNT, clampTintCount, computeRecentRank, recencyTintShadow } from '../utils/recencyTint'

const iso = (min: number) => `2026-07-09T20:${String(min).padStart(2, '0')}:00Z`
const shadow = (w: number, op: number) => `inset ${w}px 0 0 color-mix(in srgb, var(--accent) ${op}%, transparent)`

describe('computeRecentRank', () => {
  it('ranks by settled activity descending (1 = most recent)', () => {
    const r = computeRecentRank([
      { key: 'a', last_turn_ts: iso(10) },
      { key: 'b', last_turn_ts: iso(30) },
      { key: 'c', last_turn_ts: iso(20) },
    ], 5)
    expect(r.get('b')).toBe(1)
    expect(r.get('c')).toBe(2)
    expect(r.get('a')).toBe(3)
  })

  it('reads last_turn_ts over last_ts, so a mid-turn row does not repaint the stripe', () => {
    // The tint must mark the rows the sidebar actually SORTED to the top. Ranking
    // by last_ts would hand rank 1 to the running session while it sits second.
    const r = computeRecentRank([
      { key: 'running', last_turn_ts: iso(10), last_ts: iso(40) },
      { key: 'idle', last_turn_ts: iso(20), last_ts: iso(20) },
    ], 5)
    expect(r.get('idle')).toBe(1)
    expect(r.get('running')).toBe(2)
  })

  it('falls back to last_ts for a payload without the settled field', () => {
    const r = computeRecentRank([
      { key: 'a', last_ts: iso(10) },
      { key: 'b', last_ts: iso(30) },
    ], 5)
    expect(r.get('b')).toBe(1)
    expect(r.get('a')).toBe(2)
  })

  it('keeps only the `count` most-recent and excludes the rest', () => {
    const slots = Array.from({ length: 8 }, (_, i) => ({ key: `s${i}`, last_turn_ts: iso(i + 1) }))
    const r = computeRecentRank(slots, 5)
    expect(r.size).toBe(5)
    expect(r.get('s7')).toBe(1)     // newest (minute 8)
    expect(r.get('s3')).toBe(5)     // 5th newest (minute 4)
    expect(r.has('s2')).toBe(false) // 6th newest — beyond the count
  })

  it('excludes sessions with missing or unparseable timestamps', () => {
    const r = computeRecentRank([
      { key: 'a', last_turn_ts: iso(10) },
      { key: 'b' },
      { key: 'c', last_turn_ts: '' },
      { key: 'd', last_turn_ts: 'not-a-date' },
    ], 5)
    expect(r.size).toBe(1)
    expect(r.get('a')).toBe(1)
    expect(r.has('b')).toBe(false)
    expect(r.has('c')).toBe(false)
    expect(r.has('d')).toBe(false)
  })

  it('returns an empty map when count is 0 (tint disabled)', () => {
    expect(computeRecentRank([{ key: 'a', last_turn_ts: iso(10) }], 0).size).toBe(0)
  })

  it('RECENT_TINT_COUNT defaults to 0 and MAX_RECENT_TINT_COUNT is 50', () => {
    expect(RECENT_TINT_COUNT).toBe(0)
    expect(MAX_RECENT_TINT_COUNT).toBe(50)
  })
})

describe('clampTintCount', () => {
  it('rounds and passes through in-range values', () => {
    expect(clampTintCount(3)).toBe(3)
    expect(clampTintCount(4.6)).toBe(5)
  })
  it('clamps to [0, 50]', () => {
    expect(clampTintCount(-2)).toBe(0)
    expect(clampTintCount(99)).toBe(50)
  })
  it('falls back to the default for missing / non-numeric values', () => {
    expect(clampTintCount(undefined)).toBe(RECENT_TINT_COUNT)
    expect(clampTintCount(null)).toBe(RECENT_TINT_COUNT)
    expect(clampTintCount('abc')).toBe(RECENT_TINT_COUNT)
  })
})

describe('recencyTintShadow', () => {
  it('grades width 7→3px and opacity 100→40% across 5 ranks', () => {
    expect(recencyTintShadow(1, 5)).toBe(shadow(7, 100))
    expect(recencyTintShadow(2, 5)).toBe(shadow(6, 85))
    expect(recencyTintShadow(3, 5)).toBe(shadow(5, 70))
    expect(recencyTintShadow(4, 5)).toBe(shadow(4, 55))
    expect(recencyTintShadow(5, 5)).toBe(shadow(3, 40))
  })

  it('caps width at 7px and opacity at 100% when count > 5', () => {
    // total=8: the four most-recent ranks exceed the cap and clamp to 7px / 100%
    for (const rank of [1, 2, 3, 4]) {
      expect(recencyTintShadow(rank, 8)).toBe(shadow(7, 100))
    }
    expect(recencyTintShadow(5, 8)).toBe(shadow(6, 85)) // first uncapped step
    expect(recencyTintShadow(8, 8)).toBe(shadow(3, 40)) // floor
  })

  it('floors the least-recent tinted rank at 3px / 40%', () => {
    expect(recencyTintShadow(3, 3)).toBe(shadow(3, 40))
    expect(recencyTintShadow(1, 1)).toBe(shadow(3, 40))
  })
})
