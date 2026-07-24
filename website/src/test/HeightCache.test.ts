// Feature: chat-virtualizer
//
// Property tests for HeightCache covering:
// - Property 3: Height Cache Consistency
// - Property 4: Height Cache Round-Trip
// Plus targeted unit tests for LRU eviction, flush debounce, and corruption
// recovery — areas that property tests don't cover well.

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import * as fc from 'fast-check'
import { HeightCache } from '../hooks/virtualizer/HeightCache'

beforeEach(() => {
  // Reset persisted state between tests so sessions don't bleed into each
  // other. Tests use unique session IDs anyway, but this is belt-and-braces.
  window.localStorage.clear()
})

afterEach(() => {
  vi.useRealTimers()
})

// Arbitrary: short alphanumeric keys (real item keys are stable IDs).
const keyArb = fc.stringMatching(/^[a-zA-Z0-9_-]{1,12}$/)
// Arbitrary: non-negative finite heights (real heights are pixel measurements).
const heightArb = fc.integer({ min: 0, max: 5000 })

// Feature: chat-virtualizer, Property 3: Height Cache Consistency
// **Validates: Requirements 3.1, 3.2**
describe('Property 3: Height Cache Consistency', () => {
  it('cache.get returns the last value written for each key', () => {
    fc.assert(
      fc.property(
        // A sequence of (key, height) writes. Keys can repeat — the last
        // write wins. We verify that property holds.
        fc.array(fc.tuple(keyArb, heightArb), { minLength: 1, maxLength: 80 }),
        (ops) => {
          const cache = new HeightCache(`prop3-${Math.random()}`)

          // Track the expected last-write-wins state in a plain Map.
          const expected = new Map<string, number>()
          for (const [k, h] of ops) {
            cache.set(k, h)
            expected.set(k, h)
          }

          // Every key written must read back as its most recent height.
          for (const [k, h] of expected) {
            expect(cache.get(k)).toBe(h)
          }
          // Keys never written must read back as undefined. Use a key
          // outside the arbitrary's character class so it can't collide
          // with anything fast-check generated.
          expect(cache.get('!!never written!!')).toBeUndefined()
        },
      ),
      { numRuns: 100 },
    )
  })
})

// Feature: chat-virtualizer, Property 4: Height Cache Round-Trip
// **Validates: Requirements 3.3, 3.4**
describe('Property 4: Height Cache Round-Trip', () => {
  it('flush + new instance with same sessionId returns same values', () => {
    fc.assert(
      fc.property(
        fc.array(fc.tuple(keyArb, heightArb), { minLength: 1, maxLength: 50 }),
        // Random session ID per run keeps localStorage isolated between
        // shrinking attempts.
        fc.uuid(),
        (ops, sid) => {
          const a = new HeightCache(sid)
          const expected = new Map<string, number>()
          for (const [k, h] of ops) {
            a.set(k, h)
            expected.set(k, h)
          }
          a.flush()

          // Fresh instance reading from the same persisted slot must see
          // the same key→height mapping.
          const b = new HeightCache(sid)
          for (const [k, h] of expected) {
            expect(b.get(k)).toBe(h)
          }

          // Cleanup so the next shrink attempt starts clean.
          b.clear()
        },
      ),
      { numRuns: 50 },
    )
  })
})

// Targeted unit tests — areas where property tests are awkward (timers,
// quota, corruption).

describe('HeightCache: LRU eviction', () => {
  it('caps at 2000 entries and evicts the oldest first', () => {
    const c = new HeightCache('lru-test')
    // Insert 2050 keys: keys 0..49 should be evicted, 50..2049 should remain.
    for (let i = 0; i < 2050; i++) c.set(`k${i}`, i)
    expect(c.size()).toBe(2000)
    expect(c.get('k0')).toBeUndefined()
    expect(c.get('k49')).toBeUndefined()
    expect(c.get('k50')).toBe(50)
    expect(c.get('k2049')).toBe(2049)
  })

  it('get() promotes a key to most-recently-used', () => {
    const c = new HeightCache('lru-promote')
    for (let i = 0; i < 2000; i++) c.set(`k${i}`, i)
    // Touch k0 — it should now be most-recently-used.
    c.get('k0')
    // Insert one more; k1 (now oldest) should be evicted, not k0.
    c.set('k_new', 9999)
    expect(c.get('k0')).toBe(0)
    expect(c.get('k1')).toBeUndefined()
    expect(c.get('k_new')).toBe(9999)
  })
})

describe('HeightCache: debounced flush', () => {
  it('schedules a single flush within the debounce window regardless of write count', () => {
    vi.useFakeTimers()
    const c = new HeightCache('debounce')
    const setItemSpy = vi.spyOn(Storage.prototype, 'setItem')

    for (let i = 0; i < 50; i++) c.set(`k${i}`, i)
    // No flush yet — still inside the debounce window.
    expect(setItemSpy).not.toHaveBeenCalled()

    // Partway through the (lengthened) window: still no flush.
    vi.advanceTimersByTime(100)
    expect(setItemSpy).not.toHaveBeenCalled()

    // Cross the full debounce window: exactly one flush, not 50.
    vi.advanceTimersByTime(700)
    expect(setItemSpy).toHaveBeenCalledTimes(1)
    setItemSpy.mockRestore()
  })

  it('flush() writes immediately and cancels the pending timer', () => {
    vi.useFakeTimers()
    const c = new HeightCache('flush-now')
    const setItemSpy = vi.spyOn(Storage.prototype, 'setItem')

    c.set('a', 10)
    c.flush()
    expect(setItemSpy).toHaveBeenCalledTimes(1)

    // Advance past the debounce window — no second write should fire.
    vi.advanceTimersByTime(500)
    expect(setItemSpy).toHaveBeenCalledTimes(1)
    setItemSpy.mockRestore()
  })
})

describe('HeightCache: corruption recovery', () => {
  it('console.warns and resets when localStorage holds invalid JSON', () => {
    const sid = 'corrupt-test'
    window.localStorage.setItem(`vc_heights_${sid}`, '{not valid json')
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})

    const c = new HeightCache(sid)
    expect(warnSpy).toHaveBeenCalledTimes(1)
    expect(warnSpy.mock.calls[0][0]).toMatch(/corrupted localStorage/)
    expect(c.size()).toBe(0)
    // Corrupted blob should have been removed.
    expect(window.localStorage.getItem(`vc_heights_${sid}`)).toBeNull()
    warnSpy.mockRestore()
  })

  it('skips non-numeric and negative values when loading', () => {
    const sid = 'bad-values'
    window.localStorage.setItem(
      `vc_heights_${sid}`,
      JSON.stringify({ a: 100, b: 'oops', c: -5, d: NaN, e: 200 }),
    )
    const c = new HeightCache(sid)
    expect(c.get('a')).toBe(100)
    expect(c.get('b')).toBeUndefined()
    expect(c.get('c')).toBeUndefined()
    expect(c.get('d')).toBeUndefined()
    expect(c.get('e')).toBe(200)
  })
})

describe('HeightCache: storage failure modes', () => {
  it('falls back to memory-only when setItem throws (e.g. quota)', () => {
    vi.useFakeTimers()
    // Throw only for the cache's own key so the constructor's getStorage probe
    // (which writes/removes "__vc_probe__") still succeeds and storage is
    // considered available. Otherwise the probe throws, storage is null, and
    // flush() early-returns — the quota catch this test claims to cover never
    // actually runs, yet the in-memory assertion passes regardless.
    const setItemSpy = vi
      .spyOn(Storage.prototype, 'setItem')
      .mockImplementation((key: string) => {
        if (key.startsWith('vc_heights_')) {
          throw new DOMException('quota', 'QuotaExceededError')
        }
      })

    const c = new HeightCache('quota')
    c.set('a', 100)
    vi.advanceTimersByTime(750)
    // The debounced flush actually attempted the persisting write (and threw)…
    expect(setItemSpy).toHaveBeenCalledWith('vc_heights_quota', expect.any(String))
    // …and the cache degraded gracefully to memory-only.
    expect(c.get('a')).toBe(100)
    setItemSpy.mockRestore()
  })

  it('clear() removes the persisted blob and resets memory', () => {
    const c = new HeightCache('clear-test')
    c.set('a', 1)
    c.flush()
    expect(window.localStorage.getItem('vc_heights_clear-test')).not.toBeNull()
    c.clear()
    expect(c.get('a')).toBeUndefined()
    expect(window.localStorage.getItem('vc_heights_clear-test')).toBeNull()
  })
})

describe('HeightCache: averageHeight (running mean of measured heights)', () => {
  it('returns the fallback estimate when nothing is measured', () => {
    const c = new HeightCache('avg-empty')
    // No measurements yet → the historical flat estimate (100).
    expect(c.averageHeight()).toBe(100)
  })

  it('is the exact mean of measured heights once past the sample threshold', () => {
    const c = new HeightCache('avg-basic')
    c.set('a', 40)
    c.set('b', 100)
    c.set('c', 400)
    // The mean is only trusted at >= MIN_MEAN_SAMPLES (12); pad with knowns so
    // the expected value stays exact. (Below the threshold the fallback wins —
    // covered in the small-sample guard block.)
    for (let i = 0; i < 9; i++) c.set(`pad${i}`, 100)
    expect(c.averageHeight()).toBeCloseTo((40 + 100 + 400 + 9 * 100) / 12, 10)
  })

  it('updates the mean correctly on overwrite (no skew)', () => {
    const c = new HeightCache('avg-overwrite')
    c.set('a', 100)
    c.set('b', 200)
    for (let i = 0; i < 10; i++) c.set(`pad${i}`, 100) // reach the sample threshold
    expect(c.averageHeight()).toBeCloseTo((100 + 200 + 10 * 100) / 12, 10)
    // Overwrite 'a' — the mean must reflect only the new value, not both.
    c.set('a', 400)
    expect(c.averageHeight()).toBeCloseTo((400 + 200 + 10 * 100) / 12, 10)
  })

  it('updates the mean correctly on eviction (evicted values leave the sum)', () => {
    // Small cap (default 2000). Insert 2001 keys of known heights so exactly
    // the oldest one is evicted, then assert the mean is over the survivors.
    const c = new HeightCache('avg-evict')
    // k0 has a huge outlier height; it will be the oldest and get evicted.
    c.set('k0', 1_000_000)
    for (let i = 1; i <= 2000; i++) c.set(`k${i}`, 100)
    // Size capped at 2000, k0 evicted → mean is exactly 100 (all survivors),
    // NOT skewed by the evicted outlier.
    expect(c.size()).toBe(2000)
    expect(c.get('k0')).toBeUndefined()
    expect(c.averageHeight()).toBeCloseTo(100, 10)
  })

  it('mean is exact across a random set/overwrite/evict sequence', () => {
    fc.assert(
      fc.property(
        fc.array(fc.tuple(keyArb, heightArb), { minLength: 1, maxLength: 200 }),
        (ops) => {
          const c = new HeightCache(`avg-prop-${Math.random()}`)
          const expected = new Map<string, number>()
          for (const [k, h] of ops) {
            c.set(k, h)
            expected.set(k, h)
          }
          // Default cap is 2000 and keyArb yields far fewer distinct keys, so
          // no eviction here — the expected map mirrors the cache exactly.
          const vals = [...expected.values()]
          const mean = vals.reduce((a, b) => a + b, 0) / vals.length
          // averageHeight is capped at MAX_MEAN_PX; assert exactness below it.
          if (mean <= 4000) {
            expect(c.averageHeight()).toBeCloseTo(mean, 6)
          } else {
            expect(c.averageHeight()).toBeGreaterThan(0)
          }
        },
      ),
      { numRuns: 50 },
    )
  })
})

describe('HeightCache: averageHeight outlier ceiling (GPT MEDIUM)', () => {
  // GPT flagged that the mean is biased by the tall mounted tail at cold open.
  // The OUTLIER CEILING is the part adopted. A minimum-sample gate (hold the
  // flat estimate until the sample grows) was implemented and measured in a real
  // browser and was WORSE — it reintroduced the ~5x under-estimate and pushed
  // the peak scrollHeight correction from ~51,500px to ~387,000px — so the mean
  // is trusted from the first measurement and only clipped at the extreme.
  it('caps the estimate so one pathological row cannot dominate', () => {
    const c = new HeightCache('avg-guard-cap')
    for (let i = 0; i < 12; i++) c.set(`k${i}`, 40)
    c.set('monster', 5_000_000)
    // Uncapped this mean would be ~385,000px per unmeasured row.
    expect(c.averageHeight(100)).toBe(4000)
  })

  it('leaves a normal transcript mean untouched by the ceiling', () => {
    const c = new HeightCache('avg-guard-normal')
    for (let i = 0; i < 20; i++) c.set(`k${i}`, i % 2 === 0 ? 40 : 1200)
    // Real bimodal transcripts average in the hundreds — well under the cap.
    expect(c.averageHeight(100)).toBeCloseTo(620, 6)
  })

  it('adapts from the very FIRST measurement (no sample gate)', () => {
    const c = new HeightCache('avg-guard-first')
    c.set('a', 600)
    // Must NOT hold the flat fallback: that is the original under-estimate bug.
    expect(c.averageHeight(100)).toBeCloseTo(600, 6)
  })

  it('honours an explicit fallback only when nothing is measured', () => {
    const c = new HeightCache('avg-guard-fallback')
    expect(c.averageHeight(250)).toBe(250)
    c.set('a', 900)
    expect(c.averageHeight(250)).toBeCloseTo(900, 6)
  })
})

describe('HeightCache: peek() does not disturb LRU order (GPT MEDIUM)', () => {
  // Regression: OffsetIndex.sync() reads EVERY row's height. Routing that
  // through get() rewrote LRU order into transcript-index order on each sync,
  // so at capacity eviction dropped rows the user had just viewed — defeating
  // the access-based retention the size-aware cap exists to provide.
  it('peek returns the value without promoting it', () => {
    const c = new HeightCache('peek-order')
    for (let i = 0; i < 2000; i++) c.set(`k${i}`, 10)
    expect(c.peek('k0')).toBe(10) // oldest; peeking must not save it
    c.set('fresh', 10) // over the 2000 cap → oldest evicted
    expect(c.peek('k0')).toBeUndefined()
  })

  it('get() still promotes, so genuine access is protected', () => {
    const c = new HeightCache('peek-vs-get')
    for (let i = 0; i < 2000; i++) c.set(`k${i}`, 10)
    expect(c.get('k0')).toBe(10) // promote k0 to most-recent
    c.set('fresh', 10)
    expect(c.peek('k0')).toBe(10) // survived
    expect(c.peek('k1')).toBeUndefined() // k1 evicted instead
  })

  it('peek on a missing key is undefined and adds nothing', () => {
    const c = new HeightCache('peek-missing')
    expect(c.peek('nope')).toBeUndefined()
    expect(c.size()).toBe(0)
  })
})

describe('HeightCache: size-aware eviction cap', () => {
  it('defaults to a 2000 floor when no rowCount is supplied', () => {
    const c = new HeightCache('cap-default')
    for (let i = 0; i < 2100; i++) c.set(`k${i}`, i)
    expect(c.size()).toBe(2000)
  })

  it('grows the cap with rowCount (max(2000, min(rowCount, 20000)))', () => {
    const c = new HeightCache('cap-grow', { rowCount: 5000 })
    for (let i = 0; i < 5100; i++) c.set(`k${i}`, i)
    // Cap = max(2000, min(5000, 20000)) = 5000.
    expect(c.size()).toBe(5000)
    expect(c.get('k0')).toBeUndefined()  // oldest 100 evicted
    expect(c.get('k99')).toBeUndefined()
    expect(c.get('k100')).toBe(100)
    expect(c.get('k5099')).toBe(5099)
  })

  it('a small rowCount still honours the 2000 floor', () => {
    const c = new HeightCache('cap-floor', { rowCount: 500 })
    for (let i = 0; i < 2100; i++) c.set(`k${i}`, i)
    // Cap = max(2000, min(500, 20000)) = 2000.
    expect(c.size()).toBe(2000)
  })

  it('honours the 20000 hard ceiling even for an enormous rowCount', () => {
    const c = new HeightCache('cap-ceiling', { rowCount: 1_000_000 })
    for (let i = 0; i < 20050; i++) c.set(`k${i}`, i)
    // Cap = max(2000, min(1_000_000, 20000)) = 20000.
    expect(c.size()).toBe(20000)
    expect(c.get('k0')).toBeUndefined()
    expect(c.get('k49')).toBeUndefined()
    expect(c.get('k50')).toBe(50)
    expect(c.get('k20049')).toBe(20049)
  })

  // setRowCount is a HIGH-WATER MARK: a smaller count never shrinks the cap,
  // because a transient under-count (one WS message arriving before slot
  // hydration reports itemCount 1 for a 5,000-row session) would evict
  // measurements the authoritative count cannot restore. Growth still evicts
  // oldest-first, which is what bounds retention.
  it('setRowCount never shrinks the cap, and growth still evicts oldest-first', () => {
    const c = new HeightCache('cap-shrink', { rowCount: 5000 })
    for (let i = 0; i < 5000; i++) c.set(`k${i}`, i)
    expect(c.size()).toBe(5000)
    // A smaller count is ignored — nothing is discarded.
    c.setRowCount(2500)
    expect(c.size()).toBe(5000)
    expect(c.peek('k0')).toBe(0)
    expect(c.peek('k4999')).toBe(4999)
    // The cap is still enforced on the way UP: with the count pinned at 5000,
    // inserting past it evicts the oldest first.
    for (let i = 5000; i < 5200; i++) c.set(`k${i}`, i)
    expect(c.size()).toBe(5000)
    expect(c.peek('k199')).toBeUndefined()
    expect(c.peek('k200')).toBe(200)
    expect(c.peek('k5199')).toBe(5199)
    // measuredSum stays consistent after the growth evictions: survivors are
    // k200..k5199.
    const survivors = 5000
    const meanExpected =
      Array.from({ length: survivors }, (_, j) => 200 + j).reduce((a, b) => a + b, 0) /
      survivors
    // 2699.5 sits below MAX_MEAN_PX, so the ceiling does not engage here and
    // measuredSum consistency is asserted directly.
    expect(meanExpected).toBeLessThan(4000)
    expect(c.averageHeight()).toBeCloseTo(meanExpected, 6)
  })

  // Regression: a slot switch sets sessionId one render before the transcript
  // loads, so the cache is constructed with rowCount 0. Treating that as a real
  // count trimmed a long session's persisted heights to the 2000 floor at load
  // time, and the trim is irreversible — the later setRowCount() raises the cap
  // but cannot restore evicted measurements, so the revisited session fell back
  // to estimated offsets. 0 must mean "unknown".
  it('does not trim a long persisted blob when constructed before the count is known', () => {
    const sid = 'cap-unknown-load'
    const blob: Record<string, number> = {}
    for (let i = 0; i < 5000; i++) blob[`k${i}`] = 100
    window.localStorage.setItem(`vc_heights_${sid}`, JSON.stringify(blob))

    // rowCount 0 == "transcript not loaded yet", NOT "session has no rows".
    const c = new HeightCache(sid, { rowCount: 0 })
    expect(c.size()).toBe(5000)
    expect(c.peek('k0')).toBe(100)

    // The real count arriving later must not discard anything either.
    c.setRowCount(5000)
    expect(c.size()).toBe(5000)
    expect(c.peek('k0')).toBe(100)
  })

  it('omitting rowCount entirely also preserves a long persisted blob', () => {
    const sid = 'cap-omitted-load'
    const blob: Record<string, number> = {}
    for (let i = 0; i < 3000; i++) blob[`k${i}`] = 100
    window.localStorage.setItem(`vc_heights_${sid}`, JSON.stringify(blob))
    expect(new HeightCache(sid).size()).toBe(3000)
  })

  it('still enforces the hard ceiling on a blob loaded with an unknown count', () => {
    const sid = 'cap-unknown-ceiling'
    const blob: Record<string, number> = {}
    for (let i = 0; i < 20050; i++) blob[`k${i}`] = 100
    window.localStorage.setItem(`vc_heights_${sid}`, JSON.stringify(blob))
    // Seeding the cap from the blob must not let it exceed HARD_CEILING.
    expect(new HeightCache(sid).size()).toBe(20000)
  })

  it('ignores an under-reported setRowCount instead of shrinking to the floor', () => {
    const c = new HeightCache('cap-zero-set', { rowCount: 5000 })
    for (let i = 0; i < 5000; i++) c.set(`k${i}`, 100)
    expect(c.size()).toBe(5000)
    // A transient 0 (or NaN) must not be read as "the session emptied".
    c.setRowCount(0)
    expect(c.size()).toBe(5000)
    c.setRowCount(Number.NaN)
    expect(c.size()).toBe(5000)
    // Nor a transient POSITIVE under-count: one WS message arriving before slot
    // hydration reports itemCount 1 for this 5,000-row session, and the trim it
    // would cause is irreversible.
    c.setRowCount(1)
    expect(c.size()).toBe(5000)
    c.setRowCount(2500)
    expect(c.size()).toBe(5000)
    expect(c.peek('k0')).toBe(100)
    // The authoritative larger count still applies.
    c.setRowCount(6000)
    for (let i = 5000; i < 6000; i++) c.set(`k${i}`, 100)
    expect(c.size()).toBe(6000)
  })

  // Regression: evictToCap()'s eviction only changed the in-memory map. The
  // oversized blob stayed in localStorage, so every subsequent open re-read and
  // re-trimmed it and the wasted quota was never reclaimed. Driven here through
  // load()'s HARD_CEILING trim, which is the eviction path that does NOT go via
  // set() — set() dirties the cache on its own, so it cannot discriminate.
  it('persists the trimmed map after a load-time eviction', () => {
    const sid = 'cap-load-trim-persist'
    const blob: Record<string, number> = {}
    for (let i = 0; i < 20050; i++) blob[`k${i}`] = 100
    window.localStorage.setItem(`vc_heights_${sid}`, JSON.stringify(blob))

    const c = new HeightCache(sid)
    expect(c.size()).toBe(20000)
    // The load-time trim must have dirtied the cache, so flush() writes the
    // reclaimed blob out rather than treating it as a no-op.
    c.flush()
    const persisted = JSON.parse(window.localStorage.getItem(`vc_heights_${sid}`)!)
    expect(Object.keys(persisted).length).toBe(20000)
    expect(persisted.k49).toBeUndefined()
    expect(persisted.k50).toBe(100)
  })
})

describe('HeightCache: access-recency eviction', () => {
  it('recently READ rows survive eviction instead of dying by insertion age', () => {
    const c = new HeightCache('recency')
    // Fill to the default 2000-entry cap.
    for (let i = 0; i < 2000; i++) c.set(`k${i}`, i)
    // Read the three oldest-by-insertion keys — they should be promoted to MRU.
    expect(c.get('k0')).toBe(0)
    expect(c.get('k1')).toBe(1)
    expect(c.get('k2')).toBe(2)
    // Insert three new keys → three evictions. The now-oldest are k3,k4,k5,
    // NOT the freshly-read k0,k1,k2.
    c.set('n0', 9000)
    c.set('n1', 9001)
    c.set('n2', 9002)
    expect(c.get('k0')).toBe(0)   // survived via read-recency
    expect(c.get('k1')).toBe(1)
    expect(c.get('k2')).toBe(2)
    expect(c.get('k3')).toBeUndefined()  // evicted by age
    expect(c.get('k4')).toBeUndefined()
    expect(c.get('k5')).toBeUndefined()
    expect(c.get('n2')).toBe(9002)
  })
})
