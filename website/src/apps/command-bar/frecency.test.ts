import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  _clearUsageForTest,
  frecencyScore,
  loadUsage,
  recordUse,
  type UsageMap,
} from './frecency'

const KEY = 'mc-command-bar-frecency'
const DAY = 24 * 60 * 60 * 1000
const HALF_LIFE = 14 * DAY

describe('frecencyScore', () => {
  it('scores an unused row 0 so a first-time command ranks on match quality alone', () => {
    expect(frecencyScore(undefined)).toBe(0)
    expect(frecencyScore({ count: 0, last: Date.now() })).toBe(0)
    // A negative count is not reachable through recordUse, but the guard is what
    // keeps a hand-edited localStorage value from producing a negative score that
    // would sort a row below every unused one.
    expect(frecencyScore({ count: -3, last: Date.now() })).toBe(0)
  })

  it('halves a use every half-life instead of dropping it at a cutoff', () => {
    const now = 1_000_000_000_000
    expect(frecencyScore({ count: 4, last: now }, now)).toBeCloseTo(4)
    expect(frecencyScore({ count: 4, last: now - HALF_LIFE }, now)).toBeCloseTo(2)
    expect(frecencyScore({ count: 4, last: now - 2 * HALF_LIFE }, now)).toBeCloseTo(1)
    // Decay, never disappearance: heavy past use still outranks nothing.
    expect(frecencyScore({ count: 100, last: now - 20 * HALF_LIFE }, now)).toBeGreaterThan(0)
  })

  it('treats a future timestamp as now rather than boosting it', () => {
    // A clock change backwards would otherwise make `age` negative and the
    // exponent positive, scoring one use higher than its count.
    const now = 1_000_000_000_000
    expect(frecencyScore({ count: 2, last: now + 5 * DAY }, now)).toBeCloseTo(2)
  })
})

describe('loadUsage', () => {
  beforeEach(() => _clearUsageForTest())

  it('returns an empty map when nothing is stored', () => {
    expect(loadUsage()).toEqual({})
  })

  it('keeps well-shaped entries and drops malformed ones', () => {
    // The store is a per-browser preference a user can hand-edit, so a bad shape
    // must cost that one row's boost and nothing else.
    localStorage.setItem(
      KEY,
      JSON.stringify({
        good: { count: 3, last: 123 },
        missingLast: { count: 1 },
        wrongType: { count: 'two', last: 5 },
        notAnObject: 7,
        nulled: null,
      }),
    )
    expect(loadUsage()).toEqual({ good: { count: 3, last: 123 } })
  })

  it('returns an empty map on unparseable or non-object JSON', () => {
    localStorage.setItem(KEY, '{not json')
    expect(loadUsage()).toEqual({})
    localStorage.setItem(KEY, '"a string"')
    expect(loadUsage()).toEqual({})
    localStorage.setItem(KEY, 'null')
    expect(loadUsage()).toEqual({})
  })
})

describe('storage that throws', () => {
  const realGet = Storage.prototype.getItem
  const realSet = Storage.prototype.setItem
  const realRemove = Storage.prototype.removeItem

  afterEach(() => {
    Storage.prototype.getItem = realGet
    Storage.prototype.setItem = realSet
    Storage.prototype.removeItem = realRemove
  })

  it('degrades to no history when reading throws', () => {
    // Privacy modes throw on access rather than returning null; the launcher must
    // still open.
    Storage.prototype.getItem = vi.fn(() => {
      throw new Error('access denied')
    })
    expect(loadUsage()).toEqual({})
  })

  it('still returns the updated map when writing throws', () => {
    // A full or read-only store costs ranking persistence, not the activation.
    Storage.prototype.setItem = vi.fn(() => {
      throw new Error('quota exceeded')
    })
    const map = recordUse('cmd:a', 500, {})
    expect(map['cmd:a']).toEqual({ count: 1, last: 500 })
  })

  it('does not throw out of the clear seam', () => {
    Storage.prototype.removeItem = vi.fn(() => {
      throw new Error('nope')
    })
    expect(() => _clearUsageForTest()).not.toThrow()
  })
})

describe('recordUse', () => {
  beforeEach(() => _clearUsageForTest())

  it('increments an existing count and moves the timestamp forward', () => {
    const first = recordUse('cmd:a', 100, {})
    expect(first['cmd:a']).toEqual({ count: 1, last: 100 })
    const second = recordUse('cmd:a', 900, first)
    expect(second['cmd:a']).toEqual({ count: 2, last: 900 })
  })

  it('persists, so a later load sees the activation', () => {
    recordUse('cmd:a', 100, {})
    expect(loadUsage()['cmd:a']).toEqual({ count: 1, last: 100 })
  })

  it('reads storage when given no current map', () => {
    localStorage.setItem(KEY, JSON.stringify({ 'cmd:a': { count: 5, last: 10 } }))
    expect(recordUse('cmd:a', 20)['cmd:a']).toEqual({ count: 6, last: 20 })
  })

  it('does not mutate the map it was handed', () => {
    const current: UsageMap = { 'cmd:a': { count: 1, last: 1 } }
    recordUse('cmd:a', 2, current)
    expect(current['cmd:a']).toEqual({ count: 1, last: 1 })
  })

  it('caps the store and drops the lowest-scoring entries, not the oldest', () => {
    // The bound is what keeps the key from growing forever in a long-lived
    // profile; dropping by score is what keeps a daily command that was last used
    // a week ago over a one-off touched yesterday.
    const now = 10_000_000_000
    const current: UsageMap = {}
    for (let i = 0; i < 300; i++) {
      // 299 one-off rows touched moments ago, plus one heavily-used older row.
      current[`one-off-${i}`] = { count: 1, last: now - 60_000 }
    }
    current['daily'] = { count: 500, last: now - 7 * DAY }
    const map = recordUse('fresh', now, current)
    expect(Object.keys(map).length).toBe(300)
    expect(map.daily).toBeDefined()
    expect(map.fresh).toEqual({ count: 1, last: now })
  })

  it('leaves the store untouched at exactly the cap', () => {
    const now = 10_000_000_000
    const current: UsageMap = {}
    for (let i = 0; i < 299; i++) current[`row-${i}`] = { count: 1, last: now }
    const map = recordUse('row-299', now, current)
    expect(Object.keys(map).length).toBe(300)
  })
})
