// Feature: chat-virtualizer — follow controller (stick-to-bottom) logic.
//
// These tests pin down the exact behaviours that regressed repeatedly when the
// follow logic was a tangle of event-ordering-dependent refs:
//   - slot enter / streaming with a large single growth step still follows
//   - a user scroll-up is never overridden by a late widget load (race-proof)
//   - our own programmatic pins are not mistaken for user scrolls
import { describe, it, expect } from 'vitest'
import * as fc from 'fast-check'
import {
  computeAtBottom,
  distanceFromBottom,
  bottomTarget,
  isSelfScroll,
  stickAfterUserScroll,
  evaluateAutoPin,
  atBottomEpsilon,
  SELF_SCROLL_EPSILON,
  DEFAULT_BOTTOM_THRESHOLD,
} from '../hooks/virtualizer/FollowController'

describe('geometry helpers', () => {
  it('bottomTarget is scrollHeight - clientHeight, clamped at 0', () => {
    expect(bottomTarget({ scrollTop: 0, scrollHeight: 1000, clientHeight: 400 })).toBe(600)
    // Content shorter than viewport → target 0, never negative.
    expect(bottomTarget({ scrollTop: 0, scrollHeight: 200, clientHeight: 400 })).toBe(0)
  })

  it('distanceFromBottom and computeAtBottom agree with the threshold', () => {
    const geom = { scrollTop: 550, scrollHeight: 1000, clientHeight: 400 }
    expect(distanceFromBottom(geom)).toBe(50)
    expect(computeAtBottom(geom, DEFAULT_BOTTOM_THRESHOLD)).toBe(true)
    expect(computeAtBottom({ ...geom, scrollTop: 400 }, DEFAULT_BOTTOM_THRESHOLD)).toBe(false)
  })
})

describe('isSelfScroll', () => {
  it('treats writes within epsilon as our own', () => {
    expect(isSelfScroll(600, 600)).toBe(true)
    expect(isSelfScroll(601, 600)).toBe(true) // within 2px
    expect(isSelfScroll(610, 600)).toBe(false) // 10px = user
  })

  it('never self-attributes when nothing was written this session (lastWriteTop < 0)', () => {
    expect(isSelfScroll(0, -1)).toBe(false)
    expect(isSelfScroll(600, -1)).toBe(false)
  })
})

describe('stickAfterUserScroll', () => {
  it('follows only when followOutput AND at bottom', () => {
    fc.assert(
      fc.property(fc.boolean(), fc.boolean(), (atBottom, followOutput) => {
        expect(stickAfterUserScroll(atBottom, followOutput)).toBe(followOutput && atBottom)
      }),
      { numRuns: 50 },
    )
  })
})

describe('evaluateAutoPin — the race-proof core', () => {
  const tall = { scrollTop: 600, scrollHeight: 1000, clientHeight: 400 } // at bottom (600)

  it('does not pin when not sticking', () => {
    const r = evaluateAutoPin({ stick: false, geom: tall, lastWriteTop: 600 })
    expect(r).toEqual({ pin: false, stick: false, target: 600 })
  })

  it('STREAMING/WIDGET: large single growth while glued at bottom still follows', () => {
    // We last pinned at 600. Content grew by 300 below the fold; scrollTop is
    // unchanged at 600, the new bottom is 900. Distance (300) is far past the
    // 100px threshold — the old distance gate rejected this and follow died.
    const grown = { scrollTop: 600, scrollHeight: 1300, clientHeight: 400 } // target 900
    const r = evaluateAutoPin({ stick: true, geom: grown, lastWriteTop: 600 })
    expect(r.stick).toBe(true)
    expect(r.pin).toBe(true)
    expect(r.target).toBe(900)
  })

  it('SCROLL-UP RACE: user scrolled up since our last write → release, never pin', () => {
    // We last wrote 600 (bottom). The user scrolled up to 200. A widget then
    // finishes loading and fires its RO before the scroll event dispatches.
    // The live scrollTop (200) is below lastWriteTop (600) → release + no pin.
    const afterScrollUp = { scrollTop: 200, scrollHeight: 1300, clientHeight: 400 }
    const r = evaluateAutoPin({ stick: true, geom: afterScrollUp, lastWriteTop: 600 })
    expect(r.stick).toBe(false)
    expect(r.pin).toBe(false)
  })

  it('does not move when already exactly at the bottom (no redundant write)', () => {
    const r = evaluateAutoPin({ stick: true, geom: tall, lastWriteTop: 600 })
    expect(r.stick).toBe(true)
    expect(r.pin).toBe(false) // already at 600
    expect(r.target).toBe(600)
  })

  it('slot-entry (lastWriteTop < 0) pins freely regardless of leftover scrollTop', () => {
    // Fresh session: scroller still shows the previous session's scrollTop
    // (e.g. 200) but we have written nothing this session. Must pin to bottom.
    const leftover = { scrollTop: 200, scrollHeight: 1300, clientHeight: 400 }
    const r = evaluateAutoPin({ stick: true, geom: leftover, lastWriteTop: -1 })
    expect(r.stick).toBe(true)
    expect(r.pin).toBe(true)
    expect(r.target).toBe(900)
  })

  it('a 1px jitter at the bottom is within epsilon and keeps following', () => {
    const jitter = { scrollTop: 599, scrollHeight: 1300, clientHeight: 400 }
    const r = evaluateAutoPin({ stick: true, geom: jitter, lastWriteTop: 600, epsilon: SELF_SCROLL_EPSILON })
    expect(r.stick).toBe(true)
    expect(r.pin).toBe(true)
  })

  it('property: sticking + not-scrolled-up always keeps stick true', () => {
    fc.assert(
      fc.property(
        fc.integer({ min: 0, max: 5000 }), // lastWriteTop
        fc.integer({ min: 0, max: 5000 }), // extra growth
        (lastWriteTop, growth) => {
          // scrollTop stays at lastWriteTop (user hasn't moved), content grew.
          const geom = {
            scrollTop: lastWriteTop,
            scrollHeight: lastWriteTop + 400 + growth,
            clientHeight: 400,
          }
          const r = evaluateAutoPin({ stick: true, geom, lastWriteTop })
          expect(r.stick).toBe(true)
        },
      ),
      { numRuns: 100 },
    )
  })

  it('property: any upward move past epsilon releases stick and never pins', () => {
    fc.assert(
      fc.property(
        fc.integer({ min: 100, max: 5000 }), // lastWriteTop (bottom)
        fc.integer({ min: SELF_SCROLL_EPSILON + 1, max: 100 }), // upward delta
        (lastWriteTop, up) => {
          const geom = {
            scrollTop: lastWriteTop - up,
            scrollHeight: lastWriteTop + 400,
            clientHeight: 400,
          }
          const r = evaluateAutoPin({ stick: true, geom, lastWriteTop })
          expect(r.stick).toBe(false)
          expect(r.pin).toBe(false)
        },
      ),
      { numRuns: 100 },
    )
  })

  it('mid-stream content shrink while at the bottom keeps stick (distance guard)', () => {
    // The discriminating case for the distance guard: scrollTop dropped below
    // lastWriteTop (so it LOOKS like a scroll-up) BUT the viewport is still at
    // the new bottom (distance ~0) — e.g. a partial markdown line re-parsing or
    // a code fence reclassifying shrinks content. This must NOT release stick;
    // deleting the `distanceFromBottom > epsilon` clause makes this case fail.
    const geom = { scrollTop: 596, scrollHeight: 996, clientHeight: 400 }
    expect(distanceFromBottom(geom)).toBeLessThanOrEqual(SELF_SCROLL_EPSILON)
    const r = evaluateAutoPin({ stick: true, geom, lastWriteTop: 600 })
    expect(r.stick).toBe(true)
  })
})

// Feature: chat-virtualizer, T3/#4 — DPR-aware "at bottom" epsilon.
//
// A flat 0.5px gate is UNDER one device pixel at fractional device-pixel ratios
// (0.67 CSS px at 150% zoom), so at the fractional resting scrollTop the pin
// re-fired on every ResizeObserver tick even though the viewport was visually
// pinned. atBottomEpsilon() scales to the device pixel (never below 1 CSS px).
describe('atBottomEpsilon — fractional-DPR resting gate', () => {
  const desc = Object.getOwnPropertyDescriptor(window, 'devicePixelRatio')
  const setDpr = (v: number | undefined) => {
    if (v === undefined) {
      // Simulate an environment (jsdom/SSR) that leaves it undefined.
      Object.defineProperty(window, 'devicePixelRatio', { configurable: true, value: undefined })
    } else {
      Object.defineProperty(window, 'devicePixelRatio', { configurable: true, value: v })
    }
  }
  const restore = () => {
    if (desc) Object.defineProperty(window, 'devicePixelRatio', desc)
    else setDpr(1)
  }

  it('at DPR 1.5 the fractional resting max reports at-bottom → no re-pin', () => {
    setDpr(1.5)
    try {
      // eps = max(1, 1/1.5 + 0.5) ≈ 1.167px — covers the 0.67 CSS px error.
      expect(atBottomEpsilon()).toBeCloseTo(1.1667, 3)
      // Resting scrollTop lands 0.67px short of the true bottom target (900).
      const geom = { scrollTop: 900 - 0.67, scrollHeight: 1300, clientHeight: 400 }
      const r = evaluateAutoPin({ stick: true, geom, lastWriteTop: 900 })
      expect(r.stick).toBe(true)
      expect(r.pin).toBe(false) // within epsilon — the RO tick does NOT re-fire
      // The retired flat 0.5 literal WOULD have re-fired here (0.67 > 0.5).
      expect(0.67).toBeGreaterThan(0.5)
    } finally {
      restore()
    }
  })

  it('at DPR 1.25 the 0.8px resting error is still within epsilon', () => {
    setDpr(1.25)
    try {
      expect(atBottomEpsilon()).toBeCloseTo(1.3, 5) // 1/1.25 + 0.5
      const geom = { scrollTop: 600 - 0.8, scrollHeight: 1000, clientHeight: 400 }
      const r = evaluateAutoPin({ stick: true, geom, lastWriteTop: 600 })
      expect(r.pin).toBe(false)
    } finally {
      restore()
    }
  })

  it('falls back to 1.5px when devicePixelRatio is undefined (jsdom/SSR guard)', () => {
    setDpr(undefined)
    try {
      expect(atBottomEpsilon()).toBe(1.5) // 1/1 + 0.5
    } finally {
      restore()
    }
  })
})
