/**
 * `bubbleLayout` — the geometry the pet's speech bubble is placed by.
 *
 * Nothing here touches the DOM: it is pure arithmetic over rectangles, and that
 * is exactly why it goes wrong silently. A bubble placed half off-screen, or
 * placed on top of the pet it is supposed to be speaking from, still renders
 * happily — the only witness is the number a human never reads.
 *
 * So every case below pins a real decision rather than a return type:
 *
 *  - the clamp that keeps a bubble inside the screen, including the degenerate
 *    screen that is NARROWER than the bubble (max < min, where a naive
 *    `Math.min(max, ...)` would push the bubble off the left edge instead);
 *  - the score that makes an overlapping placement lose to a clamped one — the
 *    100000 term is the whole reason the bubble does not cover the pet's face;
 *  - the candidate filter in `pickBubblePlacementWithinBounds`, where a crowded
 *    screen edge must leave only one viable side rather than let the random pick
 *    choose a badly clamped one;
 *  - `resolveWideBubbleSize`'s widening loop, asserted on the SEQUENCE of widths
 *    it measures at, because the bug shape there is an off-by-one step or a loop
 *    that never terminates;
 *  - the `||` defaulting on the option bags, which means a caller passing `0`
 *    gets the default and not zero — surprising, but load-bearing, and callers
 *    do pass computed values.
 *
 * Randomness is injected, never stubbed globally: `pickBubblePlacement*` calls
 * `random()` exactly three times (jitter X, jitter Y, then the choice among
 * viable candidates), so a fixed sequence makes the whole placement exact.
 */
import { describe, expect, it } from 'vitest'

import {
  BUBBLE_LAYOUT_DEFAULTS,
  BubblePriority,
  buildBubbleCandidates,
  canReplaceBubble,
  clamp,
  expandRect,
  pickBubblePlacement,
  pickBubblePlacementWithinBounds,
  rectOverlapArea,
  resolveBubbleGap,
  resolveBubbleRect,
  resolveBubbleRectWithinBounds,
  resolveWideBubbleSize,
  scoreBubblePlacement,
  scoreBubblePlacementWithinBounds,
  type AnchorRect,
  type Candidate,
  type Rect,
  type Size,
} from '../apps/mochi/src/shared/bubbleLayout'

// ── fixtures ────────────────────────────────────────────────────────────────

/** A 100x100 pet window sitting in the middle of a 1000x800 screen. */
const centredAnchor: AnchorRect = {
  left: 450,
  top: 400,
  right: 550,
  bottom: 500,
  width: 100,
  height: 100,
}

const wideBounds: Rect = { left: 0, top: 0, right: 1000, bottom: 800 }

/**
 * `random` with a scripted tape. Reads, in order: jitter X, jitter Y, and the
 * pick among viable candidates. Falls back to 0 once exhausted so an extra call
 * cannot make a case pass by accident.
 */
function scriptedRandom(...values: number[]): () => number {
  let index = 0
  return () => (index < values.length ? values[index++] : 0)
}

/** A measurer whose height shrinks as it is given more width. */
function shrinkingMeasurer(natural: Size, heightAt: (width: number) => number) {
  const widths: (number | null)[] = []
  const measure = (width: number | null): Size => {
    widths.push(width)
    if (width == null) return natural
    return { width, height: heightAt(width) }
  }
  return { measure, widths }
}

// ── clamp / rect helpers ────────────────────────────────────────────────────

describe('clamp', () => {
  it('bounds a value on both sides and passes an in-range value through', () => {
    expect(clamp(5, 10, 20)).toBe(10)
    expect(clamp(25, 10, 20)).toBe(20)
    expect(clamp(14, 10, 20)).toBe(14)
  })

  it('returns min when the range is inverted, instead of the smaller max', () => {
    // A screen narrower than the bubble produces max < min. Returning `max`
    // here would place the bubble left of the screen's own left margin.
    expect(clamp(50, 12, -110)).toBe(12)
  })
})

describe('expandRect', () => {
  it('grows every side by the padding', () => {
    expect(expandRect({ left: 100, top: 200, right: 300, bottom: 400 }, 10)).toEqual({
      left: 90,
      top: 190,
      right: 310,
      bottom: 410,
    })
  })

  it('shrinks on a negative padding', () => {
    expect(expandRect({ left: 0, top: 0, right: 100, bottom: 100 }, -5)).toEqual({
      left: 5,
      top: 5,
      right: 95,
      bottom: 95,
    })
  })
})

describe('rectOverlapArea', () => {
  it('multiplies the overlapping width by the overlapping height', () => {
    const a: Rect = { left: 0, top: 0, right: 100, bottom: 50 }
    const b: Rect = { left: 50, top: 25, right: 150, bottom: 75 }
    expect(rectOverlapArea(a, b)).toBe(50 * 25)
  })

  it('is 0 for disjoint rectangles on either axis', () => {
    const base: Rect = { left: 0, top: 0, right: 100, bottom: 100 }
    expect(rectOverlapArea(base, { left: 200, top: 0, right: 300, bottom: 100 })).toBe(0)
    expect(rectOverlapArea(base, { left: 0, top: 200, right: 100, bottom: 300 })).toBe(0)
  })

  it('is 0 for rectangles that only touch on an edge', () => {
    expect(
      rectOverlapArea(
        { left: 0, top: 0, right: 100, bottom: 100 },
        { left: 100, top: 0, right: 200, bottom: 100 }
      )
    ).toBe(0)
  })
})

// ── gap ─────────────────────────────────────────────────────────────────────

describe('resolveBubbleGap', () => {
  it('uses minGap for a bubble shorter than half the pet', () => {
    expect(resolveBubbleGap(centredAnchor, 40)).toBe(BUBBLE_LAYOUT_DEFAULTS.minGap)
  })

  it('grows with the bubble height past the threshold', () => {
    // threshold = height * 0.5 = 50; offset = 30; 10 + 30 * 0.1 = 13.
    expect(resolveBubbleGap(centredAnchor, 80)).toBe(13)
  })

  it('saturates at maxGap for a very tall bubble', () => {
    expect(resolveBubbleGap(centredAnchor, 1000)).toBe(BUBBLE_LAYOUT_DEFAULTS.maxGap)
  })

  it('honours an explicit threshold, factor and range', () => {
    expect(
      resolveBubbleGap(centredAnchor, 30, {
        minGap: 5,
        maxGap: 50,
        gapGrowthFactor: 1,
        heightThreshold: 0,
      })
    ).toBe(35)
  })
})

// ── candidates ──────────────────────────────────────────────────────────────

describe('buildBubbleCandidates', () => {
  it('centres `top` on the pet and offsets the two side placements', () => {
    const candidates = buildBubbleCandidates(centredAnchor, 180, 80, 0, 0, 20)

    expect(candidates.map(candidate => candidate.name)).toEqual(['top', 'top-right', 'top-left'])
    expect(candidates.every(candidate => candidate.arrowSide === 'bottom')).toBe(true)
    expect(candidates.every(candidate => candidate.targetY === centredAnchor.top)).toBe(true)
    // All three sit on the same line above the pet: top - height - gap.
    expect(candidates.every(candidate => candidate.top === 300)).toBe(true)

    expect(candidates[0]).toMatchObject({ left: 410, targetX: 500, bias: 0 })
    expect(candidates[1]).toMatchObject({ left: 514, targetX: 526, bias: 2 })
    expect(candidates[2]).toMatchObject({ left: 306, targetX: 474, bias: 2 })
  })

  it('adds the jitter to every candidate', () => {
    const plain = buildBubbleCandidates(centredAnchor, 180, 80, 0, 0, 20)
    const jittered = buildBubbleCandidates(centredAnchor, 180, 80, 7, -3, 20)

    jittered.forEach((candidate, index) => {
      expect(candidate.left).toBe(plain[index].left + 7)
      expect(candidate.top).toBe(plain[index].top - 3)
      // The arrow target is the pet, so jitter must NOT move it.
      expect(candidate.targetX).toBe(plain[index].targetX)
    })
  })
})

// ── rect resolution ─────────────────────────────────────────────────────────

describe('resolveBubbleRectWithinBounds', () => {
  const candidate: Candidate = {
    name: 'top-left',
    left: -44,
    top: 300,
    targetX: 474,
    targetY: 400,
    arrowSide: 'bottom',
    bias: 2,
  }

  it('pulls a candidate hanging off the left edge back inside the margin', () => {
    expect(resolveBubbleRectWithinBounds(candidate, 180, 80, wideBounds, 12)).toEqual({
      left: 12,
      top: 300,
      right: 192,
      bottom: 380,
    })
  })

  it('offsets against a non-zero bounds origin (second monitor)', () => {
    const rightMonitor: Rect = { left: 1920, top: 0, right: 3200, bottom: 800 }
    expect(resolveBubbleRectWithinBounds(candidate, 180, 80, rightMonitor, 12)).toEqual({
      left: 1932,
      top: 300,
      right: 2112,
      bottom: 380,
    })
  })

  it('falls back to the min edge when the bubble is wider than the screen', () => {
    const tiny: Rect = { left: 0, top: 0, right: 100, bottom: 100 }
    expect(resolveBubbleRectWithinBounds(candidate, 200, 80, tiny, 10)).toEqual({
      left: 10,
      top: 10,
      right: 210,
      bottom: 90,
    })
  })

  it('clamps down to the bottom margin when there is no room above', () => {
    const shortBounds: Rect = { left: 0, top: 0, right: 1000, bottom: 360 }
    const rect = resolveBubbleRectWithinBounds(candidate, 180, 80, shortBounds, 12)
    expect(rect.top).toBe(268)
    expect(rect.bottom).toBe(348)
  })
})

describe('resolveBubbleRect', () => {
  it('is the 0-origin form of resolveBubbleRectWithinBounds', () => {
    const candidate: Candidate = {
      name: 'top',
      left: 410,
      top: 300,
      targetX: 500,
      targetY: 400,
      arrowSide: 'bottom',
      bias: 0,
    }
    expect(resolveBubbleRect(candidate, 180, 80, 1000, 800, 12)).toEqual(
      resolveBubbleRectWithinBounds(candidate, 180, 80, wideBounds, 12)
    )
  })
})

// ── scoring ─────────────────────────────────────────────────────────────────

describe('scoreBubblePlacementWithinBounds', () => {
  const farAway: Rect = { left: 900, top: 700, right: 950, bottom: 750 }

  it('scores an unclamped, non-overlapping candidate as its bias alone', () => {
    const candidate: Candidate = {
      name: 'top-right',
      left: 514,
      top: 300,
      targetX: 526,
      targetY: 400,
      arrowSide: 'bottom',
      bias: 2,
    }
    expect(scoreBubblePlacementWithinBounds(candidate, 180, 80, wideBounds, farAway, 12)).toBe(2)
  })

  it('charges twice the distance the candidate had to be clamped', () => {
    const candidate: Candidate = {
      name: 'top',
      left: -30,
      top: -20,
      targetX: 0,
      targetY: 0,
      arrowSide: 'bottom',
      bias: 2,
    }
    // Clamped by 40 on x and 30 on y => 70 * 2, plus the bias.
    expect(
      scoreBubblePlacementWithinBounds(candidate, 100, 50, { left: 0, top: 0, right: 500, bottom: 500 }, farAway, 10)
    ).toBe(142)
  })

  it('adds a 100000 penalty plus the area when it would cover the pet', () => {
    const candidate: Candidate = {
      name: 'top',
      left: 0,
      top: 0,
      targetX: 0,
      targetY: 0,
      arrowSide: 'bottom',
      bias: 0,
    }
    const pet: Rect = { left: 50, top: 25, right: 150, bottom: 75 }
    expect(
      scoreBubblePlacementWithinBounds(candidate, 100, 50, { left: 0, top: 0, right: 500, bottom: 500 }, pet, 0)
    ).toBe(100000 + 50 * 25)
  })

  it('prefers a heavily clamped placement over one that covers the pet', () => {
    const screen: Rect = { left: 0, top: 0, right: 500, bottom: 500 }
    const pet: Rect = { left: 50, top: 25, right: 150, bottom: 75 }
    const onThePet: Candidate = {
      name: 'top',
      left: 10,
      top: 10,
      targetX: 0,
      targetY: 0,
      arrowSide: 'bottom',
      bias: 0,
    }
    // Off-screen by 4610px, so its clamp penalty is enormous — and it still has
    // to win, or a tight screen puts the bubble on the pet's face.
    const wildlyOffScreen: Candidate = { ...onThePet, left: 5000, top: 10 }

    const overlapScore = scoreBubblePlacementWithinBounds(onThePet, 100, 50, screen, pet, 10)
    const clampScore = scoreBubblePlacementWithinBounds(wildlyOffScreen, 100, 50, screen, pet, 10)

    expect(overlapScore).toBeGreaterThan(100000)
    expect(clampScore).toBe((5000 - 390) * 2)
    expect(clampScore).toBeLessThan(overlapScore)
  })
})

describe('scoreBubblePlacement', () => {
  it('is the 0-origin form of scoreBubblePlacementWithinBounds', () => {
    const candidate: Candidate = {
      name: 'top',
      left: 410,
      top: 300,
      targetX: 500,
      targetY: 400,
      arrowSide: 'bottom',
      bias: 0,
    }
    const pet = expandRect(centredAnchor as unknown as Rect, 10)
    expect(scoreBubblePlacement(candidate, 180, 80, 1000, 800, pet, 12)).toBe(
      scoreBubblePlacementWithinBounds(candidate, 180, 80, wideBounds, pet, 12)
    )
  })
})

// ── picking ─────────────────────────────────────────────────────────────────

describe('pickBubblePlacementWithinBounds', () => {
  it('picks the centred placement on an open screen and applies no jitter at 0.5', () => {
    const picked = pickBubblePlacementWithinBounds(centredAnchor, 180, 80, wideBounds, {
      gap: 20,
      random: scriptedRandom(0.5, 0.5, 0),
    })
    expect(picked).toMatchObject({ name: 'top', left: 410, top: 300 })
  })

  it('can land on a lower-ranked but still viable candidate', () => {
    // All three score within the 6-point viability window on an open screen,
    // so the third random draw decides — this is the "bubble moves around"
    // behaviour, not a bug.
    const picked = pickBubblePlacementWithinBounds(centredAnchor, 180, 80, wideBounds, {
      gap: 20,
      random: scriptedRandom(0.5, 0.5, 0.99),
    })
    expect(picked.name).toBe('top-left')
  })

  it('drops badly clamped candidates when the pet is against the right edge', () => {
    const edgeAnchor: AnchorRect = {
      left: 300,
      top: 400,
      right: 400,
      bottom: 500,
      width: 100,
      height: 100,
    }
    const narrow: Rect = { left: 0, top: 0, right: 400, bottom: 800 }

    // `top` and `top-right` both have to be dragged left to fit, so only
    // `top-left` survives the filter — every random draw must return it.
    for (const draw of [0, 0.5, 0.99]) {
      const picked = pickBubblePlacementWithinBounds(edgeAnchor, 180, 80, narrow, {
        gap: 20,
        random: scriptedRandom(0.5, 0.5, draw),
      })
      expect(picked).toMatchObject({ name: 'top-left', left: 156 })
    }
  })

  it('derives the gap from the bubble height when none is given', () => {
    const picked = pickBubblePlacementWithinBounds(centredAnchor, 180, 80, wideBounds, {
      random: scriptedRandom(0.5, 0.5, 0),
    })
    expect(picked.top).toBe(centredAnchor.top - 80 - resolveBubbleGap(centredAnchor, 80))
  })

  it('uses an explicit gap of 0 verbatim rather than deriving one', () => {
    const picked = pickBubblePlacementWithinBounds(centredAnchor, 180, 80, wideBounds, {
      gap: 0,
      random: scriptedRandom(0.5, 0.5, 0),
    })
    expect(picked.top).toBe(centredAnchor.top - 80)
  })

  it('scales the jitter by the configured ranges', () => {
    const picked = pickBubblePlacementWithinBounds(centredAnchor, 180, 80, wideBounds, {
      gap: 20,
      jitterX: 100,
      jitterY: 40,
      random: scriptedRandom(1, 0, 0),
    })
    // (1 - 0.5) * 100 => +50 on x; (0 - 0.5) * 40 => -20 on y.
    expect(picked).toMatchObject({ left: 460, top: 280 })
  })

  it('defaults the jitter ranges when they are passed as 0', () => {
    // The implementation defaults with `||`, so 0 means "use the default" —
    // a caller wanting no jitter must inject a constant `random` instead.
    const picked = pickBubblePlacementWithinBounds(centredAnchor, 180, 80, wideBounds, {
      gap: 20,
      jitterX: 0,
      jitterY: 0,
      random: scriptedRandom(1, 1, 0),
    })
    expect(picked.left).toBe(410 + Math.round(0.5 * BUBBLE_LAYOUT_DEFAULTS.jitterX))
    expect(picked.top).toBe(300 + Math.round(0.5 * BUBBLE_LAYOUT_DEFAULTS.jitterY))
  })

  it('runs off Math.random when no random is injected', () => {
    const picked = pickBubblePlacementWithinBounds(centredAnchor, 180, 80, wideBounds, { gap: 20 })
    expect(['top', 'top-right', 'top-left']).toContain(picked.name)
    expect(Math.abs(picked.top - 300)).toBeLessThanOrEqual(BUBBLE_LAYOUT_DEFAULTS.jitterY)
  })
})

describe('pickBubblePlacement', () => {
  it('is the 0-origin form of pickBubblePlacementWithinBounds', () => {
    const options = { gap: 20, random: scriptedRandom(0.5, 0.5, 0) }
    expect(pickBubblePlacement(centredAnchor, 180, 80, 1000, 800, options)).toEqual(
      pickBubblePlacementWithinBounds(centredAnchor, 180, 80, wideBounds, {
        gap: 20,
        random: scriptedRandom(0.5, 0.5, 0),
      })
    )
  })
})

// ── widening ────────────────────────────────────────────────────────────────

describe('resolveWideBubbleSize', () => {
  it('accepts a naturally wide measurement without re-measuring', () => {
    const { measure, widths } = shrinkingMeasurer({ width: 200, height: 100 }, () => 100)
    expect(resolveWideBubbleSize(measure)).toEqual({ width: 200, height: 100 })
    expect(widths).toEqual([null])
  })

  it('widens in steps until the bubble is wider than tall or maxWidth is hit', () => {
    const { measure, widths } = shrinkingMeasurer({ width: 100, height: 300 }, width => 600 - width)
    expect(resolveWideBubbleSize(measure)).toEqual({ width: 280, height: 320 })
    // First re-measure lifts the natural 100 to minWidth, then +24 per step,
    // with the last step clamped to maxWidth rather than overshooting to 300.
    expect(widths).toEqual([null, 180, 204, 228, 252, 276, 280])
  })

  it('stops as soon as the height drops below the width', () => {
    const { measure, widths } = shrinkingMeasurer({ width: 100, height: 300 }, width => width - 1)
    expect(resolveWideBubbleSize(measure)).toEqual({ width: 180, height: 179 })
    expect(widths).toEqual([null, 180])
  })

  it('honours explicit min/max/step', () => {
    const { measure, widths } = shrinkingMeasurer({ width: 40, height: 80 }, width => 200 - width)
    expect(resolveWideBubbleSize(measure, { minWidth: 50, maxWidth: 100, widthStep: 10 })).toEqual({
      width: 100,
      height: 100,
    })
    expect(widths).toEqual([null, 50, 60, 70, 80, 90, 100])
  })

  it('falls back to the defaults when the options are passed as 0', () => {
    const { measure, widths } = shrinkingMeasurer({ width: 100, height: 90 }, () => 90)
    expect(resolveWideBubbleSize(measure, { minWidth: 0, maxWidth: 0, widthStep: 0 })).toEqual({
      width: BUBBLE_LAYOUT_DEFAULTS.minWidth,
      height: 90,
    })
    // A literal 0 step with a 0 max would loop forever; the defaults are what
    // keep this terminating.
    expect(widths).toEqual([null, BUBBLE_LAYOUT_DEFAULTS.minWidth])
  })
})

// ── priority ────────────────────────────────────────────────────────────────

describe('canReplaceBubble', () => {
  it('lets an equal or higher priority bubble take over', () => {
    expect(canReplaceBubble(BubblePriority.Chat, BubblePriority.Chat)).toBe(true)
    expect(canReplaceBubble(BubblePriority.Error, BubblePriority.Notification)).toBe(true)
    expect(canReplaceBubble(BubblePriority.Approval, BubblePriority.Chat)).toBe(true)
  })

  it('refuses to let chatter interrupt an approval prompt', () => {
    expect(canReplaceBubble(BubblePriority.Chat, BubblePriority.Approval)).toBe(false)
    expect(canReplaceBubble(BubblePriority.Notification, BubblePriority.Error)).toBe(false)
  })

  it('orders the priorities chat < notification < error < approval', () => {
    expect([
      BubblePriority.Chat,
      BubblePriority.Notification,
      BubblePriority.Error,
      BubblePriority.Approval,
    ]).toEqual([0, 1, 2, 3])
  })
})
