import { describe, it, expect } from 'vitest'
import {
  SETTLE_STABLE_SAMPLES,
  createSettleTracker,
  sameRender,
  sampleIsQuiet,
} from '../../scripts/lib/render-settle.mjs'

/**
 * When the render gate decides a surface is done.
 *
 * The property under test is the one the fixed sleep got wrong: a page still
 * waiting on a post-mount fetch must NEVER be declared settled, because scanning it
 * yields a smaller number rather than an error, and `[vs-base]` reports that
 * shortfall as findings the branch introduced.
 *
 * Tested as a pure state machine for the reason `renderVerdict.test.ts` gives for
 * its own subject: reaching this logic through two vite builds and a browser is a
 * ten-minute round trip, which is what kept the old behaviour untested while it
 * mis-blamed pull requests.
 */

const quiet = (chars = 500, nodes = 300) => ({ chars, nodes, inflight: 0 })
const busy = (chars = 500, nodes = 300) => ({ chars, nodes, inflight: 1 })

/** Feed a sequence and report the 1-based sample that settled it, or null. */
function settledAt(samples) {
  const tracker = createSettleTracker()
  for (let i = 0; i < samples.length; i += 1) {
    if (tracker.observe(samples[i])) return i + 1
  }
  return null
}

describe('sameRender', () => {
  it('compares text volume and node count together', () => {
    expect(sameRender({ chars: 10, nodes: 5 }, { chars: 10, nodes: 5 })).toBe(true)
    expect(sameRender({ chars: 10, nodes: 5 }, { chars: 11, nodes: 5 })).toBe(false)
  })

  it('separates a same-length swap, which is a different page', () => {
    expect(sameRender({ chars: 42, nodes: 20 }, { chars: 42, nodes: 31 })).toBe(false)
  })

  it('has no opinion without two samples', () => {
    expect(sameRender(null, { chars: 1, nodes: 1 })).toBe(false)
  })
})

describe('sampleIsQuiet', () => {
  it('is quiet only when the page owes the network nothing', () => {
    expect(sampleIsQuiet({ chars: 1, nodes: 1, inflight: 0 })).toBe(true)
    expect(sampleIsQuiet({ chars: 1, nodes: 1, inflight: 1 })).toBe(false)
  })

  it('reads a straggler from an earlier surface as idle, not as a pending fetch', () => {
    expect(sampleIsQuiet({ chars: 1, nodes: 1, inflight: -2 })).toBe(true)
  })
})

describe('createSettleTracker', () => {
  it('needs the configured run of matching quiet samples', () => {
    expect(settledAt(Array(SETTLE_STABLE_SAMPLES).fill(quiet()))).toBe(null)
    expect(settledAt(Array(SETTLE_STABLE_SAMPLES + 1).fill(quiet())))
      .toBe(SETTLE_STABLE_SAMPLES + 1)
  })

  it('never settles a page that is still fetching, however still its DOM is', () => {
    // The `app-detail` skeleton: nothing moves, because the answer has not arrived.
    expect(settledAt(Array(20).fill(busy()))).toBe(null)
  })

  it('never settles a page that is still mutating, however idle the network is', () => {
    const growing = Array.from({ length: 20 }, (_, i) => quiet(100 + i * 10, 50 + i))
    expect(settledAt(growing)).toBe(null)
  })

  it('restarts the run when the awaited body finally lands', () => {
    // Skeleton held still while busy, then the real body arrives and holds.
    const samples = [
      busy(120, 40), busy(120, 40), busy(120, 40),
      quiet(900, 260), quiet(900, 260), quiet(900, 260), quiet(900, 260),
    ]
    // Settles on the 4th consecutive quiet, unchanged sample -- not on the
    // skeleton's stillness that preceded it.
    expect(settledAt(samples)).toBe(7)
  })

  it('restarts the run on a late mutation instead of settling early', () => {
    const samples = [
      quiet(900, 260), quiet(900, 260), quiet(900, 260),
      quiet(950, 265),
      quiet(950, 265), quiet(950, 265), quiet(950, 265),
    ]
    expect(settledAt(samples)).toBe(7)
  })

  it('stays settled once settled, and keeps the last sample for the failure text', () => {
    const tracker = createSettleTracker()
    for (const s of Array(SETTLE_STABLE_SAMPLES + 1).fill(quiet(400, 111))) tracker.observe(s)
    expect(tracker.settled).toBe(true)
    expect(tracker.observe(busy(1, 1))).toBe(true)
    expect(tracker.last).toEqual({ chars: 400, nodes: 111, inflight: 0 })
  })
})
