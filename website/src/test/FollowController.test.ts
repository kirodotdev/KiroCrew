// Feature: chat-virtualizer — follow controller (stick-to-bottom) logic.
//
// These tests pin down the exact behaviours the follow logic must guarantee:
//   - slot enter / streaming with a large single growth step still follows
//   - a user scroll-up is never overridden by a late widget load (race-proof)
//   - our own programmatic pins are not mistaken for user scrolls
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import * as fc from 'fast-check'
import {
  computeAtBottom,
  distanceFromBottom,
  bottomTarget,
  isSelfScroll,
  heightAnchorStillUsable,
  resolveUserScrollStick,
  evaluateAutoPin,
  atBottomEpsilon,
  SELF_SCROLL_EPSILON,
  DEFAULT_BOTTOM_THRESHOLD,
  FOLLOW_REENGAGE_PX,
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

describe('heightAnchorStillUsable', () => {
  it('honours an anchor the viewport never moved away from, however late', () => {
    // A reprice ABOVE the viewport moves where rows sit, never scrollTop — so an
    // unchanged scrollTop means the whole delta belongs to the reprice. A turn
    // ending is the busiest the main thread gets, so the consumer runs late;
    // dropping the anchor there made a still reader pay the reprice as one
    // displacement.
    expect(heightAnchorStillUsable(1000, 1000)).toBe(true)
    expect(heightAnchorStillUsable(1000, 1001)).toBe(true) // sub-pixel/rounding
  })

  it('drops an anchor once the viewport has moved (finger or iOS momentum)', () => {
    // The delta is contaminated by the reader's own motion; correcting it
    // corrects their scrolling (2706px teleport on the phone rig). Momentum
    // keeps moving with NO further hard input, which is why an input-timestamp
    // gate misses it and a scrollTop comparison does not.
    expect(heightAnchorStillUsable(1000, 1600)).toBe(false)
    expect(heightAnchorStillUsable(1000, 400)).toBe(false)
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

describe('resolveUserScrollStick — direction-aware follow decision', () => {
  // 1000px content in a 400px viewport → bottom target 600.
  const geomAt = (scrollTop: number) => ({ scrollTop, scrollHeight: 1000, clientHeight: 400 })

  it('releases on ANY upward move away from the true bottom, even inside the 100px band', () => {
    for (const dist of [3, 30, 99]) {
      expect(
        resolveUserScrollStick({
          stick: true,
          followOutput: true,
          scrollTop: 600 - dist,
          prevScrollTop: 600,
          geom: geomAt(600 - dist),
        }),
      ).toBe(false)
    }
  })

  it('keeps following across a layout clamp (scrollTop drops but lands at the true bottom)', () => {
    // Content shrank 227px; the browser clamped scrollTop by the same amount.
    const geom = { scrollTop: 373, scrollHeight: 773, clientHeight: 400 }
    expect(
      resolveUserScrollStick({
        stick: true, followOutput: true, scrollTop: 373, prevScrollTop: 600, geom,
      }),
    ).toBe(true)
  })

  it('re-engages on a downward arrival within FOLLOW_REENGAGE_PX of the bottom', () => {
    const dist = FOLLOW_REENGAGE_PX - 1
    expect(
      resolveUserScrollStick({
        stick: false, followOutput: true, scrollTop: 600 - dist, prevScrollTop: 200,
        geom: geomAt(600 - dist),
      }),
    ).toBe(true)
  })

  it('does NOT re-engage on a downward move that stops short of the re-engage band', () => {
    // 60px above the bottom: inside the old 100px band, outside the new one.
    expect(
      resolveUserScrollStick({
        stick: false, followOutput: true, scrollTop: 540, prevScrollTop: 200,
        geom: geomAt(540),
      }),
    ).toBe(false)
  })

  it('keeps the previous state for a mid-list downward move (no flapping)', () => {
    for (const stick of [true, false]) {
      expect(
        resolveUserScrollStick({
          stick, followOutput: true, scrollTop: 300, prevScrollTop: 200,
          geom: geomAt(300),
        }),
      ).toBe(stick)
    }
  })

  it('is position-only and conservative with no prior observation (prevScrollTop < 0)', () => {
    // At the bottom → follow; away from it → release EVEN IF stick was armed
    // (an unattributable scroll must not keep a stale follow).
    expect(
      resolveUserScrollStick({
        stick: true, followOutput: true, scrollTop: 600, prevScrollTop: -1, geom: geomAt(600),
      }),
    ).toBe(true)
    expect(
      resolveUserScrollStick({
        stick: true, followOutput: true, scrollTop: 300, prevScrollTop: -1, geom: geomAt(300),
      }),
    ).toBe(false)
    expect(
      resolveUserScrollStick({
        stick: false, followOutput: true, scrollTop: 300, prevScrollTop: -1, geom: geomAt(300),
      }),
    ).toBe(false)
  })

  it('never follows with followOutput disabled', () => {
    fc.assert(
      fc.property(fc.boolean(), fc.integer({ min: 0, max: 600 }), (stick, top) => {
        expect(
          resolveUserScrollStick({
            stick, followOutput: false, scrollTop: top, prevScrollTop: 600, geom: geomAt(top),
          }),
        ).toBe(false)
      }),
      { numRuns: 50 },
    )
  })

  it('the re-engage band is meaningfully tighter than the pill band', () => {
    expect(FOLLOW_REENGAGE_PX).toBeLessThan(DEFAULT_BOTTOM_THRESHOLD / 2)
    expect(FOLLOW_REENGAGE_PX).toBeGreaterThan(atBottomEpsilon())
  })

  it('does NOT re-engage when the band arrives at a STILL reader', () => {
    // Rows outside the window repricing smaller than their estimates collapses
    // the remaining content under a mid-transcript reader, so the bottom band
    // reaches them without them moving. A neutral event there used to re-arm
    // follow, and the next pin took them to the end -- reported as scrolling
    // along and suddenly landing at the bottom. Their scrollTop is identical:
    // nothing about this is the reader returning to the bottom.
    expect(
      resolveUserScrollStick({
        stick: false, followOutput: true,
        scrollTop: 590, prevScrollTop: 590, geom: { scrollTop: 590, scrollHeight: 1000, clientHeight: 400 },
      }),
    ).toBe(false)
  })

  it('DOES re-engage when the reader moves down into the band themselves', () => {
    // The behaviour the band exists for, and the discriminator: same geometry,
    // same distance -- the only difference is that this reader moved toward the
    // bottom.
    expect(
      resolveUserScrollStick({
        stick: false, followOutput: true,
        scrollTop: 590, prevScrollTop: 400, geom: { scrollTop: 590, scrollHeight: 1000, clientHeight: 400 },
      }),
    ).toBe(true)
  })

  it('still follows at the TRUE bottom however the reader got there', () => {
    // Rule 1 is untouched: a mid-stream shrink drops scrollTop to exactly the
    // new bottom (which reads as an upward move), and releasing there froze
    // streaming follow for the rest of the turn. At the true bottom there is
    // nothing below to be yanked to.
    expect(
      resolveUserScrollStick({
        stick: false, followOutput: true,
        scrollTop: 600, prevScrollTop: 900, geom: { scrollTop: 600, scrollHeight: 1000, clientHeight: 400 },
      }),
    ).toBe(true)
  })
})

describe('evaluateAutoPin — the race-proof core', () => {
  const tall = { scrollTop: 600, scrollHeight: 1000, clientHeight: 400 } // at bottom (600)

  it('does not pin when not sticking', () => {
    const r = evaluateAutoPin({ stick: false, geom: tall, lastWriteTop: 600 })
    expect(r).toEqual({ pin: false, stick: false, target: 600 })
  })

  it('IDLE: releases a reader sitting above the bottom instead of pinning them', () => {
    // Follow means "keep me at the end of a LIVE turn". With nothing running
    // there is no output to follow, so a reader 120px up is not following — and
    // pinning them is a spring-back with no cause (reported from a phone after
    // scrolling up about a hundred pixels with nothing streaming). Releasing
    // rather than merely skipping matters: leaving follow armed would hand the
    // yank to whichever turn starts next.
    //
    // `lastWriteTop` EQUALS scrollTop on purpose, so the pre-existing
    // scroll-up release cannot fire and this pins the idle rule alone: the gap
    // opened because content grew below the fold, not because anyone scrolled.
    const up = { scrollTop: 480, scrollHeight: 1000, clientHeight: 400 } // 120px above bottom
    const r = evaluateAutoPin({ stick: true, geom: up, lastWriteTop: 480, runActive: false })
    expect(r).toEqual({ pin: false, stick: false, target: 600 })
  })

  it('IDLE: a reader ALREADY at the bottom keeps following', () => {
    // Rows settling under a reader parked at the very bottom must still keep them
    // there; the idle rule is about not MOVING someone who left the bottom.
    const r = evaluateAutoPin({ stick: true, geom: tall, lastWriteTop: 600, runActive: false })
    expect(r.stick).toBe(true)
    expect(r.pin).toBe(false)
  })

  it('RUNNING: the same reader 120px up is still followed', () => {
    // The gate is the run, not the distance: mid-turn, follow deliberately
    // survives a large gap so a burst of output does not strand the reader.
    const up = { scrollTop: 480, scrollHeight: 1000, clientHeight: 400 }
    const r = evaluateAutoPin({ stick: true, geom: up, lastWriteTop: 480, runActive: true })
    expect(r).toEqual({ pin: true, stick: true, target: 600 })
  })

  it('omitting runActive assumes a live run, so a caller with no signal is unchanged', () => {
    const up = { scrollTop: 480, scrollHeight: 1000, clientHeight: 400 }
    expect(evaluateAutoPin({ stick: true, geom: up, lastWriteTop: 480 }).pin).toBe(true)
  })

  it('STREAMING/WIDGET: large single growth while glued at bottom still follows', () => {
    // We last pinned at 600. Content grew by 300 below the fold; scrollTop is
    // unchanged at 600, the new bottom is 900. Distance (300) is far past the
    // 100px threshold — a plain distance gate would reject this and break follow.
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

  it('OUR OWN viewport shrink does not read as a scroll-up, even on top of a clamp', () => {
    // Both halves of the queue-band race in one geometry: a tail-row remount
    // shrank content by 4px (so the browser clamped scrollTop from 600 to 596,
    // below our last write) AND the band's animation shrank the box by 29px
    // (400 -> 371). Distance is now 29px, which without the allowance is
    // "meaningfully away from the bottom" — a full scroll-up signature built
    // from two of our own layout changes. Forgiving the box's own 29px keeps
    // follow armed and re-pins to the new bottom (996 - 371 = 625).
    const geom = { scrollTop: 596, scrollHeight: 996, clientHeight: 371 }
    expect(distanceFromBottom(geom)).toBe(29)
    expect(evaluateAutoPin({ stick: true, geom, lastWriteTop: 600 }).stick).toBe(false)
    const r = evaluateAutoPin({ stick: true, geom, lastWriteTop: 600, viewportShrink: 29 })
    expect(r.stick).toBe(true)
    expect(r.pin).toBe(true)
    expect(r.target).toBe(625)
  })

  it('the allowance forgives only its own pixels — a real drag inside it still releases', () => {
    // Same 29px shrink, but the user also dragged 200px up: distance 229, of
    // which only 29 is ours. The remaining 200 is still user input.
    const geom = { scrollTop: 396, scrollHeight: 996, clientHeight: 371 }
    const r = evaluateAutoPin({ stick: true, geom, lastWriteTop: 600, viewportShrink: 29 })
    expect(r.stick).toBe(false)
    expect(r.pin).toBe(false)
  })

  it('a viewport GROW never widens the guard (negative shrink is clamped to 0)', () => {
    // The box grew (chrome unmounted), so the caller passes a negative value.
    // Treating it as an allowance would be a subtraction the wrong way; a
    // genuine 100px scroll-up must still release.
    const geom = { scrollTop: 500, scrollHeight: 1000, clientHeight: 400 }
    const r = evaluateAutoPin({ stick: true, geom, lastWriteTop: 600, viewportShrink: -60 })
    expect(r.stick).toBe(false)
    expect(r.pin).toBe(false)
  })
})

// Feature: chat-virtualizer — DPR-aware "at bottom" epsilon.
//
// A flat 0.5px gate is UNDER one device pixel at fractional device-pixel ratios
// (0.67 CSS px at 150% zoom), so at the fractional resting scrollTop a flat gate
// re-fires the pin on every ResizeObserver tick even though the viewport is
// visually pinned. atBottomEpsilon() scales to the device pixel (never below 1
// CSS px).
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
      // A flat 0.5 literal WOULD re-fire here (0.67 > 0.5).
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

describe('resolveUserScrollStick — what brought the reader to the bottom', () => {
  it('a VIEWPORT growth that clamps the reader to the bottom does not arm follow', () => {
    // Deleting a draft shrinks the composer, so the scroller GROWS, the maximum
    // scrollTop drops, and the engine clamps a near-bottom reader flush — with no
    // application write anywhere. That clamp arrives as an ordinary scroll event
    // sitting at distance ~0. Reading it as "the reader came back" arms follow for
    // someone who never touched the scroller, and the next turn to start takes
    // them to the end.
    const armed = resolveUserScrollStick({
      stick: false,
      followOutput: true,
      scrollTop: 600,
      prevScrollTop: 600,
      geom: { scrollTop: 600, scrollHeight: 1000, clientHeight: 400 },
      viewportGrowth: 96,
    })
    expect(armed).toBe(false)
  })

  it('a CONTENT-shrink clamp still arms follow, which is what rule 1 is for', () => {
    // Mid-stream a partial markdown line re-parsing shrinks the CONTENT, clamping
    // scrollTop while leaving the reader genuinely at the new bottom. Follow must
    // survive that or streaming stops following for the rest of the response.
    const armed = resolveUserScrollStick({
      stick: true,
      followOutput: true,
      scrollTop: 600,
      prevScrollTop: 620,
      geom: { scrollTop: 600, scrollHeight: 1000, clientHeight: 400 },
      viewportGrowth: 0,
    })
    expect(armed).toBe(true)
  })

  it('omitting viewportGrowth keeps the previous meaning for callers with no signal', () => {
    const armed = resolveUserScrollStick({
      stick: false,
      followOutput: true,
      scrollTop: 600,
      prevScrollTop: 600,
      geom: { scrollTop: 600, scrollHeight: 1000, clientHeight: 400 },
    })
    expect(armed).toBe(true)
  })
})

describe('resolveUserScrollStick — a clamp only ever lowers scrollTop', () => {
  it('a deliberate downward move concurrent with viewport growth still re-engages', () => {
    // Reported by review: without a direction term, a reader who scrolls DOWN to
    // the bottom while the keyboard closes has their own re-engagement refused,
    // because the growth alone was taken as proof the engine moved them.
    // scrollHeight 1000, clientHeight 400 -> 450: bottom moves 600 -> 550, and the
    // reader moved 500 -> 550 by hand. A clamp could not have raised 500 to 550.
    const armed = resolveUserScrollStick({
      stick: false,
      followOutput: true,
      scrollTop: 550,
      prevScrollTop: 500,
      geom: { scrollTop: 550, scrollHeight: 1000, clientHeight: 450 },
      viewportGrowth: 50,
    })
    expect(armed).toBe(true)
  })

  it('the same growth with no movement is still classified as the clamp', () => {
    const armed = resolveUserScrollStick({
      stick: false,
      followOutput: true,
      scrollTop: 550,
      prevScrollTop: 550,
      geom: { scrollTop: 550, scrollHeight: 1000, clientHeight: 450 },
      viewportGrowth: 50,
    })
    expect(armed).toBe(false)
  })
})

describe('both consumers report the viewport signal', () => {
  it('the app-sdk hook passes viewportGrowth from its own scroll-event baseline', () => {
    // Review finding: this hook observes pane resizes and the soft keyboard — the
    // exact causes of a viewport-growth clamp — yet omitted the signal, so it kept
    // the original defect while the chat virtualizer was fixed. The baseline must
    // be its own, advanced by the scroll handler: a ref the ResizeObserver could
    // advance first would fold the growth away before the clamp is classified.
    const src = readFileSync(join(__dirname, '..', 'app-sdk', 'useChatScrollFollow.ts'), 'utf8')
    const call = src.slice(src.indexOf('resolveUserScrollStick({'))
    const args = call.slice(0, call.indexOf('})'))
    expect(args).toMatch(/viewportGrowth:/)
    expect(args).toContain('lastScrollClientHRef.current')
    // Advanced in the scroll handler, not in the observer.
    expect(src).toMatch(/prevScrollTopRef\.current = geom\.scrollTop\s*\n\s*lastScrollClientHRef\.current = geom\.clientHeight/)
    // Not reusing the write-tracking ref, whose meaning is different.
    expect(args).not.toContain('lastWriteClientHRef')
  })
})
