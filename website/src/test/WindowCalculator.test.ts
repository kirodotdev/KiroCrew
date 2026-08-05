// Property tests for WindowCalculator covering:
// - Property 1: Window Correctness Invariant
// - Property 5: Jump Navigation Window Placement
// Plus targeted unit tests for edge cases the property tests don't reach
// (empty list, scroll past end, getOffset/getIndexAtOffset round-trip).

import { describe, it, expect } from 'vitest'
import * as fc from 'fast-check'
import {
  computeWindow,
  computeJumpWindow,
  getOffset,
  getIndexAtOffset,
  getTotalHeight,
  OffsetIndex,
} from '../hooks/virtualizer/WindowCalculator'

// Arbitrary: an item heights array of plausible chat message sizes.
const heightsArb = fc.array(fc.integer({ min: 20, max: 800 }), { minLength: 1, maxLength: 200 })

describe('Property 1: Window Correctness Invariant', () => {
  it('every item overlapping the viewport is inside [start, end)', () => {
    fc.assert(
      fc.property(
        heightsArb,
        fc.integer({ min: 0, max: 50_000 }),       // scrollTop
        fc.integer({ min: 100, max: 2000 }),       // viewportHeight
        fc.integer({ min: 0, max: 20 }),           // overscan
        (heights, scrollTop, viewportHeight, overscan) => {
          const getH = (i: number) => heights[i]
          const total = heights.reduce((a, b) => a + b, 0)
          // Clamp scroll to total content range so we don't always test the
          // "past end" branch (still tested separately below).
          const top = Math.min(scrollTop, Math.max(0, total - 1))
          const bottom = top + viewportHeight

          const { start, end } = computeWindow(top, viewportHeight, heights.length, getH, overscan)

          // Range bounds.
          expect(start).toBeGreaterThanOrEqual(0)
          expect(end).toBeLessThanOrEqual(heights.length)
          expect(start).toBeLessThanOrEqual(end)

          // Compute each item's [top, bottom] in content coordinates and verify
          // every visually-overlapping item is inside the mounted range.
          let off = 0
          for (let i = 0; i < heights.length; i++) {
            const itemTop = off
            const itemBottom = off + heights[i]
            const overlaps = itemBottom > top && itemTop < bottom
            if (overlaps) {
              expect(i).toBeGreaterThanOrEqual(start)
              expect(i).toBeLessThan(end)
            }
            off = itemBottom
          }
        },
      ),
      { numRuns: 100 },
    )
  })
})

describe('Property 5: Jump Navigation Window Placement', () => {
  it('computeJumpWindow always contains the target index', () => {
    fc.assert(
      fc.property(
        fc.integer({ min: 1, max: 5000 }),       // itemCount
        fc.integer({ min: 0, max: 4999 }),       // raw target (clamped inside)
        fc.integer({ min: 0, max: 50 }),         // overscan
        (itemCount, rawTarget, overscan) => {
          const target = rawTarget % itemCount
          const { start, end } = computeJumpWindow(target, itemCount, overscan)
          expect(start).toBeLessThanOrEqual(target)
          expect(target).toBeLessThan(end)
          // Window size is at most 2*overscan + 1 (clamped at edges).
          expect(end - start).toBeLessThanOrEqual(2 * overscan + 1)
        },
      ),
      { numRuns: 100 },
    )
  })

  it('jump-to-last-index returns window ending at itemCount', () => {
    fc.assert(
      fc.property(
        fc.integer({ min: 1, max: 1000 }),
        fc.integer({ min: 0, max: 20 }),
        (itemCount, overscan) => {
          const { end } = computeJumpWindow(itemCount - 1, itemCount, overscan)
          expect(end).toBe(itemCount)
        },
      ),
      { numRuns: 50 },
    )
  })
})

// Round-trip property: getOffset and getIndexAtOffset agree on item boundaries.
describe('Property: offset round-trip', () => {
  it('getIndexAtOffset(getOffset(i)) === i for every i in [0, itemCount)', () => {
    fc.assert(
      fc.property(
        fc.array(fc.integer({ min: 1, max: 500 }), { minLength: 1, maxLength: 100 }),
        (heights) => {
          const getH = (i: number) => heights[i]
          for (let i = 0; i < heights.length; i++) {
            const off = getOffset(i, heights.length, getH)
            // At the exact top edge of item i, we want item i (not i-1).
            expect(getIndexAtOffset(off, heights.length, getH)).toBe(i)
          }
        },
      ),
      { numRuns: 50 },
    )
  })
})

// Targeted unit tests for edge cases.

describe('computeWindow edge cases', () => {
  it('returns {0,0} for empty list', () => {
    expect(computeWindow(0, 500, 0, () => 100, 5)).toEqual({ start: 0, end: 0 })
  })

  it('handles scroll past end by showing the tail', () => {
    const heights = [100, 100, 100]
    const { start, end } = computeWindow(10_000, 500, 3, (i) => heights[i], 1)
    // Tail item must be inside the window.
    expect(start).toBeLessThanOrEqual(2)
    expect(end).toBe(3)
  })

  it('respects overscan at start of list (clamps to 0)', () => {
    const heights = [50, 50, 50, 50, 50]
    const { start, end } = computeWindow(0, 100, 5, (i) => heights[i], 10)
    // Two items visible (indices 0,1); overscan would push start negative
    // but it's clamped to 0.
    expect(start).toBe(0)
    expect(end).toBe(5) // overscan extends past end too, clamped to itemCount
  })

  it('zero-height items are tolerated', () => {
    const heights = [100, 0, 0, 100]
    const { start, end } = computeWindow(0, 50, 4, (i) => heights[i], 0)
    expect(start).toBe(0)
    // The first 0-height item lives inside item 0's bottom edge so it
    // overlaps the viewport top edge — it must be in the window.
    expect(end).toBeGreaterThan(0)
  })
})

describe('getOffset / getIndexAtOffset', () => {
  it('getOffset(0) === 0', () => {
    expect(getOffset(0, 5, () => 100)).toBe(0)
  })

  it('getOffset(itemCount) === total', () => {
    expect(getOffset(5, 5, () => 100)).toBe(500)
  })

  it('getIndexAtOffset clamps to last index when past the end', () => {
    expect(getIndexAtOffset(99_999, 3, () => 100)).toBe(2)
  })

  it('getIndexAtOffset returns 0 for empty list', () => {
    expect(getIndexAtOffset(0, 0, () => 100)).toBe(0)
  })

  it('getTotalHeight sums all item heights', () => {
    expect(getTotalHeight(4, (i) => (i + 1) * 10)).toBe(10 + 20 + 30 + 40)
  })
})

// ── OffsetIndex (Fenwick/prefix-sum tree) ───────────────────────────────────
// Correctness contract: OffsetIndex must answer offsetOf / indexAt /
// totalHeight IDENTICALLY to the O(N) free functions for every input, while
// scaling sub-linearly.

// Mixed height distribution incl. zero-height rows (tool pills → widgets).
const mixedHeightsArb = fc.array(fc.integer({ min: 0, max: 3000 }), {
  minLength: 1,
  maxLength: 300,
})

describe('OffsetIndex: parity with the O(N) free functions', () => {
  it('offsetOf(i) === getOffset(i) for every boundary across mixed heights', () => {
    fc.assert(
      fc.property(mixedHeightsArb, (heights) => {
        const getH = (i: number) => heights[i]
        const oi = new OffsetIndex(heights.length, getH)
        for (let i = 0; i <= heights.length; i++) {
          expect(oi.offsetOf(i)).toBe(getOffset(i, heights.length, getH))
        }
      }),
      { numRuns: 100 },
    )
  })

  it('totalHeight() === getTotalHeight() and === offsetOf(itemCount)', () => {
    fc.assert(
      fc.property(mixedHeightsArb, (heights) => {
        const getH = (i: number) => heights[i]
        const oi = new OffsetIndex(heights.length, getH)
        const total = getTotalHeight(heights.length, getH)
        expect(oi.totalHeight()).toBe(total)
        expect(oi.offsetOf(heights.length)).toBe(total)
      }),
      { numRuns: 100 },
    )
  })

  it('indexAt(scrollTop) === getIndexAtOffset(scrollTop) across the content range', () => {
    fc.assert(
      fc.property(
        mixedHeightsArb,
        fc.array(fc.integer({ min: 0, max: 1_000_000 }), { minLength: 1, maxLength: 20 }),
        (heights, probes) => {
          const getH = (i: number) => heights[i]
          const oi = new OffsetIndex(heights.length, getH)
          const total = getTotalHeight(heights.length, getH)
          for (const raw of probes) {
            // Sample both inside the content and past the end (clamp branch).
            const t = total > 0 ? raw % (total + 50) : raw
            expect(oi.indexAt(t)).toBe(getIndexAtOffset(t, heights.length, getH))
          }
          // Exact row boundaries — the round-trip the free functions guarantee.
          for (let i = 0; i < heights.length; i++) {
            const off = oi.offsetOf(i)
            expect(oi.indexAt(off)).toBe(getIndexAtOffset(off, heights.length, getH))
          }
        },
      ),
      { numRuns: 100 },
    )
  })

  it('clamps out-of-range / degenerate inputs like the free functions', () => {
    const heights = [40, 250, 0, 1200]
    const getH = (i: number) => heights[i]
    const oi = new OffsetIndex(heights.length, getH)
    // offsetOf clamps below 0 and above itemCount.
    expect(oi.offsetOf(-5)).toBe(0)
    expect(oi.offsetOf(0)).toBe(0)
    expect(oi.offsetOf(99)).toBe(getTotalHeight(4, getH))
    // indexAt clamps negative/NaN → row 0, past-end → last row.
    expect(oi.indexAt(-100)).toBe(0)
    expect(oi.indexAt(Number.NaN)).toBe(0)
    expect(oi.indexAt(9_999_999)).toBe(3)
    // Empty list.
    const empty = new OffsetIndex(0, () => 100)
    expect(empty.totalHeight()).toBe(0)
    expect(empty.offsetOf(0)).toBe(0)
    expect(empty.indexAt(500)).toBe(0)
  })
})

describe('OffsetIndex: sync (rebuild / extend / refine)', () => {
  it('append-growth matches a fresh build of the grown array', () => {
    const heights: number[] = [100, 40, 800, 0, 250]
    const getH = (i: number) => heights[i]
    const oi = new OffsetIndex(heights.length, getH)
    // Grow at the tail (transcript append) five rows at a time.
    for (let batch = 0; batch < 5; batch++) {
      for (let k = 0; k < 5; k++) heights.push((batch * 5 + k) * 37 % 900)
      oi.sync(heights.length, getH)
      // Every offset + total must match a from-scratch build.
      const fresh = new OffsetIndex(heights.length, getH)
      for (let i = 0; i <= heights.length; i++) {
        expect(oi.offsetOf(i)).toBe(fresh.offsetOf(i))
      }
      expect(oi.totalHeight()).toBe(getTotalHeight(heights.length, getH))
    }
  })

  it('refining measurements in place (same itemCount) matches a fresh build', () => {
    const heights = new Array<number>(200).fill(100) // all "estimated"
    const getH = (i: number) => heights[i]
    const oi = new OffsetIndex(heights.length, getH)
    // Rows measure in with their real (varied) heights.
    for (let i = 0; i < heights.length; i++) heights[i] = (i * 17) % 1500
    oi.sync(heights.length, getH)
    for (let i = 0; i <= heights.length; i++) {
      expect(oi.offsetOf(i)).toBe(getOffset(i, heights.length, getH))
    }
    expect(oi.totalHeight()).toBe(getTotalHeight(heights.length, getH))
  })

  it('shrink (rows removed) rebuilds correctly', () => {
    const heights = [100, 200, 300, 400, 500, 600]
    const getH = (i: number) => heights[i]
    const oi = new OffsetIndex(heights.length, getH)
    oi.sync(3, getH) // keep first three rows
    expect(oi.totalHeight()).toBe(600)
    expect(oi.offsetOf(3)).toBe(600)
    expect(oi.indexAt(250)).toBe(1)
  })

  it('a no-op sync (nothing changed) leaves answers identical', () => {
    const heights = [100, 40, 800, 250]
    const getH = (i: number) => heights[i]
    const oi = new OffsetIndex(heights.length, getH)
    const before = heights.map((_, i) => oi.offsetOf(i))
    oi.sync(heights.length, getH)
    heights.forEach((_, i) => expect(oi.offsetOf(i)).toBe(before[i]))
    expect(oi.totalHeight()).toBe(getTotalHeight(heights.length, getH))
  })
})

describe('OffsetIndex: sub-linear scaling benchmark', () => {
  // Wall-clock micro-benchmarks are noisy, so this test is built to be robust:
  //  • min-of-N trials (min rejects positive scheduler/GC spikes),
  //  • enough iterations per trial that each measurement is comfortably
  //    above timer resolution,
  //  • assertions are RELATIVE and self-calibrating against the known-linear
  //    free function measured on the SAME machine — no absolute ms budget that
  //    could flake on a slow/fast CI box.
  const makeHeights = (n: number): number[] =>
    Array.from({ length: n }, (_, i) => 20 + ((i * 131) % 780))

  // Deterministic probe offsets so timing reflects the algorithm, not RNG.
  const makeProbes = (count: number, span: number): number[] =>
    Array.from({ length: count }, (_, i) => ((i * 2654435761) % span))

  const bestOf = (trials: number, fn: () => void): number => {
    let best = Infinity
    for (let t = 0; t < trials; t++) {
      const t0 = performance.now()
      fn()
      const dt = performance.now() - t0
      if (dt < best) best = dt
    }
    return best
  }

  it('OffsetIndex.indexAt is far cheaper than the O(N) scan at N=5000', () => {
    const N = 5000
    const heights = makeHeights(N)
    const getH = (i: number) => heights[i]
    const total = getTotalHeight(N, getH)
    const oi = new OffsetIndex(N, getH)
    const ITER = 2000
    const probes = makeProbes(ITER, total)

    // Warm up both paths (JIT).
    for (const p of probes) { oi.indexAt(p); getIndexAtOffset(p, N, getH) }

    const oiTime = bestOf(5, () => {
      for (let i = 0; i < ITER; i++) oi.indexAt(probes[i])
    })
    const naiveTime = bestOf(5, () => {
      for (let i = 0; i < ITER; i++) getIndexAtOffset(probes[i], N, getH)
    })

    // Fenwick does ~log2(5000)≈12 steps vs the scan's up-to-5000; even with a
    // generous 3× cushion for constant-factor/noise the tree must win big.
    expect(oiTime * 3).toBeLessThan(naiveTime)
  })

  it('per-call cost grows sub-linearly (logarithmically) with N', () => {
    const sizes = [500, 2000, 5000]
    // OffsetIndex is so cheap it needs a large iteration count to rise well
    // above performance.now() resolution — that's what keeps the ratio stable.
    // Keep iteration counts modest so the test completes well within the 15s
    // timeout even under parallel CI load.
    const ITER = 20_000
    // The O(N) baseline is expensive PER CALL, so a much smaller count already
    // yields a stable, well-above-floor measurement (and keeps the test fast).
    const NAIVE_ITER = 5_000

    // One full measurement pass. Returns the two ratios being compared.
    const measure = () => {
      const perCall: Record<number, number> = {}
      const naivePerCall: Record<number, number> = {}
      for (const N of sizes) {
        const heights = makeHeights(N)
        const getH = (i: number) => heights[i]
        const total = getTotalHeight(N, getH)
        const oi = new OffsetIndex(N, getH)
        const probes = makeProbes(ITER, total)
        // Warm up.
        for (let i = 0; i < ITER; i++) oi.indexAt(probes[i])
        perCall[N] = bestOf(5, () => {
          for (let i = 0; i < ITER; i++) oi.indexAt(probes[i])
        }) / ITER
        // Only measure the linear baseline at the two endpoints we compare.
        if (N === 500 || N === 5000) {
          for (let i = 0; i < NAIVE_ITER; i++) getIndexAtOffset(probes[i], N, getH)
          naivePerCall[N] = bestOf(2, () => {
            for (let i = 0; i < NAIVE_ITER; i++) getIndexAtOffset(probes[i], N, getH)
          }) / NAIVE_ITER
        }
      }
      // Self-calibrating: the naive scan is O(N), so its 500→5000 ratio is the
      // machine's "what linear looks like" (~10×). OffsetIndex (O(log N)) must
      // scale dramatically better — its 500→5000 ratio should be ~1.4×.
      return {
        oiRatio: perCall[5000] / Math.max(perCall[500], 1e-9),
        naiveRatio: naivePerCall[5000] / Math.max(naivePerCall[500], 1e-9),
      }
    }

    // A wall-clock ratio can be distorted when the suite runs in parallel and
    // an unrelated worker steals CPU mid-measurement. Retry a bounded number of
    // times and require only that ONE clean pass agrees: a genuine O(N)
    // regression fails every attempt, while transient contention does not.
    // The assertions themselves are unchanged.
    let last = measure()
    for (let attempt = 1; attempt < 3; attempt++) {
      if (last.oiRatio < last.naiveRatio && last.oiRatio < 6) break
      last = measure()
    }
    const { oiRatio, naiveRatio } = last

    // Primary, machine-independent signal: OffsetIndex grows far slower than
    // the known-linear baseline on the SAME hardware.
    expect(oiRatio).toBeLessThan(naiveRatio)
    // Absolute backstop: nowhere near linear (which would be ~10×). Generous
    // enough (6×) to never flake, tight enough to fail an O(N) regression.
    expect(oiRatio).toBeLessThan(6)
  })
})
