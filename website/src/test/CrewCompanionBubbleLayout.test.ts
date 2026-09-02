/**
 * Bubble placement geometry, ported property-for-property from the desktop app's
 * `src/test/bubbleLayout.test.ts`. The TypeScript is the specification: these pin the
 * scoring, the gap formula, the three-candidate structure, the bounds clamp and the
 * priority rule that together keep the companion's bubble directly above it and on
 * screen. Only the import path changed from the source.
 */
import { describe, it, expect } from 'vitest'
import * as fc from 'fast-check'
import {
  clamp,
  expandRect,
  rectOverlapArea,
  resolveBubbleGap,
  buildBubbleCandidates,
  resolveBubbleRectWithinBounds,
  resolveBubbleRect,
  scoreBubblePlacementWithinBounds,
  pickBubblePlacementWithinBounds,
  pickBubblePlacement,
  resolveWideBubbleSize,
  canReplaceBubble,
  BubblePriority,
  Rect,
  AnchorRect,
  Candidate,
  BUBBLE_LAYOUT_DEFAULTS,
} from '../apps/crew-companion/bubbleLayout'

// ── Property 12: clamp returns min when max < min ──────────────────────────
// Feature: bubble-layout, Property 12: clamp returns min when max < min
// Validates: Requirements 8.1

describe('clamp', () => {
  it('Property 12 — when max < min, clamp returns min', () => {
    fc.assert(
      fc.property(
        fc.double({ min: -1e6, max: 1e6, noNaN: true }),
        fc.double({ min: -1e6, max: 1e6, noNaN: true }),
        fc.double({ min: -1e6, max: 1e6, noNaN: true }),
        (value, a, b) => {
          const min = Math.max(a, b)
          const max = Math.min(a, b)
          // Only test when max < min
          fc.pre(max < min)
          expect(clamp(value, min, max)).toBe(min)
        }
      ),
      { numRuns: 100 }
    )
  })
})

// ── Property 13: expandRect expands all four sides evenly ────────────────────
// Feature: bubble-layout, Property 13: expandRect expands all four sides evenly
// Validates: Requirements 8.2, 8.3

describe('expandRect', () => {
  it('Property 13 — expandRect expands all four sides by padding', () => {
    const arbRect = fc.record({
      left: fc.double({ min: -3000, max: 3000, noNaN: true }),
      top: fc.double({ min: -3000, max: 3000, noNaN: true }),
      right: fc.double({ min: -3000, max: 3000, noNaN: true }),
      bottom: fc.double({ min: -3000, max: 3000, noNaN: true }),
    })
    const arbPadding = fc.double({ min: -50, max: 50, noNaN: true })

    fc.assert(
      fc.property(arbRect, arbPadding, (rect, padding) => {
        const result = expandRect(rect, padding)
        expect(result.left).toBe(rect.left - padding)
        expect(result.top).toBe(rect.top - padding)
        expect(result.right).toBe(rect.right + padding)
        expect(result.bottom).toBe(rect.bottom + padding)
      }),
      { numRuns: 100 }
    )
  })
})

// ── Property 3: overlap area is symmetric and non-negative ───────────────────
// Feature: bubble-layout, Property 3: overlap area is symmetric and non-negative
// Validates: Requirements 2.1

describe('rectOverlapArea', () => {
  const arbRect: fc.Arbitrary<Rect> = fc.record({
    left: fc.integer({ min: -3000, max: 3000 }),
    top: fc.integer({ min: -3000, max: 3000 }),
    right: fc.integer({ min: -3000, max: 3000 }),
    bottom: fc.integer({ min: -3000, max: 3000 }),
  })

  it('Property 3 — overlap is symmetric and non-negative', () => {
    fc.assert(
      fc.property(arbRect, arbRect, (a, b) => {
        const ab = rectOverlapArea(a, b)
        const ba = rectOverlapArea(b, a)
        expect(ab).toBe(ba)
        expect(ab).toBeGreaterThanOrEqual(0)
      }),
      { numRuns: 100 }
    )
  })

  it('Property 3 — non-intersecting rects have zero overlap', () => {
    fc.assert(
      fc.property(
        fc.integer({ min: 0, max: 1000 }),
        fc.integer({ min: 0, max: 1000 }),
        fc.integer({ min: 1, max: 200 }),
        fc.integer({ min: 1, max: 200 }),
        fc.integer({ min: 1, max: 500 }),
        (x, y, w, h, separation) => {
          const a: Rect = { left: x, top: y, right: x + w, bottom: y + h }
          // Place b entirely to the right of a with separation
          const b: Rect = { left: x + w + separation, top: y, right: x + w + separation + w, bottom: y + h }
          expect(rectOverlapArea(a, b)).toBe(0)
        }
      ),
      { numRuns: 100 }
    )
  })
})


// ── Shared generators ────────────────────────────────────────────────────

const arbAnchorRect: fc.Arbitrary<AnchorRect> = fc
  .record({
    left: fc.integer({ min: -3000, max: 3000 }),
    top: fc.integer({ min: -3000, max: 3000 }),
    width: fc.integer({ min: 50, max: 200 }),
    height: fc.integer({ min: 50, max: 200 }),
  })
  .map(({ left, top, width, height }) => ({
    left,
    top,
    right: left + width,
    bottom: top + height,
    width,
    height,
  }))

// ── Property 9: gap formula correctness ──────────────────────────────────────
// Feature: bubble-layout, Property 9: gap formula correctness
// Validates: Requirements 5.1, 5.2, 5.3, 5.4

describe('resolveBubbleGap', () => {
  it('Property 9 — gap is in [minGap, maxGap] and matches formula', () => {
    fc.assert(
      fc.property(
        arbAnchorRect,
        fc.integer({ min: 40, max: 300 }),
        (anchor, bubbleHeight) => {
          const minGap = BUBBLE_LAYOUT_DEFAULTS.minGap
          const maxGap = BUBBLE_LAYOUT_DEFAULTS.maxGap
          const gapGrowthFactor = BUBBLE_LAYOUT_DEFAULTS.gapGrowthFactor
          const heightThreshold = anchor.height * 0.5

          const gap = resolveBubbleGap(anchor, bubbleHeight)

          expect(gap).toBeGreaterThanOrEqual(minGap)
          expect(gap).toBeLessThanOrEqual(maxGap)

          const heightOffset = Math.max(0, bubbleHeight - heightThreshold)
          const expected = clamp(
            Math.round(minGap + heightOffset * gapGrowthFactor),
            minGap,
            maxGap
          )
          expect(gap).toBe(expected)
        }
      ),
      { numRuns: 100 }
    )
  })

  it('Property 9 — custom heightThreshold is used when provided', () => {
    fc.assert(
      fc.property(
        arbAnchorRect,
        fc.integer({ min: 40, max: 300 }),
        fc.integer({ min: 10, max: 150 }),
        (anchor, bubbleHeight, customThreshold) => {
          const minGap = BUBBLE_LAYOUT_DEFAULTS.minGap
          const maxGap = BUBBLE_LAYOUT_DEFAULTS.maxGap
          const gapGrowthFactor = BUBBLE_LAYOUT_DEFAULTS.gapGrowthFactor

          const gap = resolveBubbleGap(anchor, bubbleHeight, { heightThreshold: customThreshold })

          const heightOffset = Math.max(0, bubbleHeight - customThreshold)
          const expected = clamp(
            Math.round(minGap + heightOffset * gapGrowthFactor),
            minGap,
            maxGap
          )
          expect(gap).toBe(expected)
        }
      ),
      { numRuns: 100 }
    )
  })
})

// ── Property 1: candidate generation invariants ──────────────────────────────
// Feature: bubble-layout, Property 1: candidate generation invariants
// Validates: Requirements 1.1, 1.2, 1.4

describe('buildBubbleCandidates', () => {
  it('Property 1 — returns 3 candidates, all with correct top, constant relative offsets', () => {
    fc.assert(
      fc.property(
        arbAnchorRect,
        fc.integer({ min: 100, max: 400 }),
        fc.integer({ min: 40, max: 300 }),
        fc.integer({ min: -50, max: 50 }),
        fc.integer({ min: -50, max: 50 }),
        fc.integer({ min: 5, max: 30 }),
        (anchor, bw, bh, jx, jy, gap) => {
          const candidates = buildBubbleCandidates(anchor, bw, bh, jx, jy, gap)

          // Exactly 3 candidates
          expect(candidates).toHaveLength(3)

          // All have correct top
          const expectedTop = anchor.top - gap - bh + jy
          for (const c of candidates) {
            expect(c.top).toBe(expectedTop)
          }

          // Relative horizontal offsets are constant regardless of jitter
          const candidates0 = buildBubbleCandidates(anchor, bw, bh, 0, 0, gap)
          const relOffsets = candidates.map((c, i) => c.left - candidates0[i].left)
          // All offsets should equal jitterX
          for (const offset of relOffsets) {
            expect(offset).toBe(jx)
          }
        }
      ),
      { numRuns: 100 }
    )
  })

  // ── Property 2: candidate structure correctness ────────────────────────────
  // Feature: bubble-layout, Property 2: candidate structure correctness
  // Validates: Requirements 1.3, 1.5

  it('Property 2 — zero jitter top candidate centered, bias values correct', () => {
    fc.assert(
      fc.property(
        arbAnchorRect,
        fc.integer({ min: 100, max: 400 }),
        fc.integer({ min: 40, max: 300 }),
        fc.integer({ min: 5, max: 30 }),
        (anchor, bw, bh, gap) => {
          const candidates = buildBubbleCandidates(anchor, bw, bh, 0, 0, gap)
          const cx = anchor.left + anchor.width / 2

          // Top candidate centered when jitterX=0
          expect(candidates[0].left).toBe(cx - bw / 2)

          // Bias values
          expect(candidates[0].bias).toBe(0)
          expect(candidates[1].bias).toBe(2)
          expect(candidates[2].bias).toBe(2)

          // Names
          expect(candidates[0].name).toBe('top')
          expect(candidates[1].name).toBe('top-right')
          expect(candidates[2].name).toBe('top-left')
        }
      ),
      { numRuns: 100 }
    )
  })
})


// ── Property 8: bubble rect stays within screen bounds (incl. negative coords) ──
// Feature: bubble-layout, Property 8: bubble rect stays within screen bounds
// Validates: Requirements 4.1, 4.2, 4.3

describe('resolveBubbleRectWithinBounds', () => {
  it('Property 8 — resolved rect is within bounds+margin when bounds are large enough', () => {
    fc.assert(
      fc.property(
        fc.integer({ min: 100, max: 400 }),  // bubbleWidth
        fc.integer({ min: 40, max: 300 }),   // bubbleHeight
        fc.integer({ min: 0, max: 50 }),     // margin
        fc.integer({ min: -3000, max: 3000 }), // boundsLeft
        fc.integer({ min: -3000, max: 3000 }), // boundsTop
        fc.integer({ min: 0, max: 2000 }),   // extra width beyond minimum
        fc.integer({ min: 0, max: 2000 }),   // extra height beyond minimum
        fc.integer({ min: -5000, max: 5000 }), // candidate left
        fc.integer({ min: -5000, max: 5000 }), // candidate top
        (bw, bh, margin, bLeft, bTop, extraW, extraH, cLeft, cTop) => {
          // Ensure bounds are large enough: width >= bw + 2*margin, height >= bh + 2*margin
          const boundsRect: Rect = {
            left: bLeft,
            top: bTop,
            right: bLeft + bw + 2 * margin + extraW,
            bottom: bTop + bh + 2 * margin + extraH,
          }

          const candidate: Candidate = {
            name: 'top',
            left: cLeft,
            top: cTop,
            targetX: 0,
            targetY: 0,
            arrowSide: 'bottom',
            bias: 0,
          }

          const result = resolveBubbleRectWithinBounds(candidate, bw, bh, boundsRect, margin)

          expect(result.left).toBeGreaterThanOrEqual(boundsRect.left + margin)
          expect(result.right).toBeLessThanOrEqual(boundsRect.right - margin)
          expect(result.top).toBeGreaterThanOrEqual(boundsRect.top + margin)
          expect(result.bottom).toBeLessThanOrEqual(boundsRect.bottom - margin)
        }
      ),
      { numRuns: 100 }
    )
  })
})

// ── Property 4: score equals bias plus clamp penalty when no overlap ──────────
// Feature: bubble-layout, Property 4: score equals bias plus clamp penalty
// Validates: Requirements 2.2, 2.3

describe('scoreBubblePlacementWithinBounds — no overlap', () => {
  it('Property 4 — score = bias + 2*clampPenalty when no overlap', () => {
    fc.assert(
      fc.property(
        fc.integer({ min: 100, max: 300 }),  // bubbleWidth
        fc.integer({ min: 40, max: 200 }),   // bubbleHeight
        fc.integer({ min: 0, max: 20 }),     // margin
        fc.integer({ min: 0, max: 4 }),      // bias
        (bw, bh, margin, bias) => {
          // Create bounds large enough
          const boundsRect: Rect = { left: 0, top: 0, right: 2000, bottom: 2000 }

          // Place candidate in the middle of bounds
          const candidate: Candidate = {
            name: 'top',
            left: 500,
            top: 500,
            targetX: 0,
            targetY: 0,
            arrowSide: 'bottom',
            bias,
          }

          // Place avoidRect far away so no overlap
          const avoidRect: Rect = { left: 1500, top: 1500, right: 1600, bottom: 1600 }

          const resolved = resolveBubbleRectWithinBounds(candidate, bw, bh, boundsRect, margin)
          const overlap = rectOverlapArea(resolved, avoidRect)
          fc.pre(overlap === 0)

          const clampPenalty = Math.abs(resolved.left - candidate.left) + Math.abs(resolved.top - candidate.top)
          const score = scoreBubblePlacementWithinBounds(candidate, bw, bh, boundsRect, avoidRect, margin)

          expect(score).toBe(bias + clampPenalty * 2)
        }
      ),
      { numRuns: 100 }
    )
  })
})

// ── Property 5: score carries a large penalty on overlap ──────────────────────
// Feature: bubble-layout, Property 5: score carries a large penalty on overlap
// Validates: Requirements 2.4

describe('scoreBubblePlacementWithinBounds — overlap', () => {
  it('Property 5 — score >= 100000 when overlap exists', () => {
    fc.assert(
      fc.property(
        fc.integer({ min: 100, max: 300 }),  // bubbleWidth
        fc.integer({ min: 40, max: 200 }),   // bubbleHeight
        (bw, bh) => {
          const boundsRect: Rect = { left: 0, top: 0, right: 2000, bottom: 2000 }

          // Place candidate at a known position
          const candidate: Candidate = {
            name: 'top',
            left: 500,
            top: 500,
            targetX: 0,
            targetY: 0,
            arrowSide: 'bottom',
            bias: 0,
          }

          // Place avoidRect overlapping with the candidate position
          const avoidRect: Rect = {
            left: 500,
            top: 500,
            right: 500 + bw,
            bottom: 500 + bh,
          }

          const resolved = resolveBubbleRectWithinBounds(candidate, bw, bh, boundsRect, 0)
          const overlap = rectOverlapArea(resolved, avoidRect)
          fc.pre(overlap > 0)

          const score = scoreBubblePlacementWithinBounds(candidate, bw, bh, boundsRect, avoidRect, 0)
          expect(score).toBeGreaterThanOrEqual(100000)
        }
      ),
      { numRuns: 100 }
    )
  })
})


// ── Property 6: chosen placement is within 6 of the best score ────────────────
// Feature: bubble-layout, Property 6: chosen placement is within 6 of the best score
// Validates: Requirements 3.1

describe('pickBubblePlacementWithinBounds', () => {
  it('Property 6 — returned candidate score is within 6 of the minimum score', () => {
    fc.assert(
      fc.property(
        arbAnchorRect,
        fc.integer({ min: 100, max: 400 }),  // bubbleWidth
        fc.integer({ min: 40, max: 300 }),   // bubbleHeight
        fc.double({ min: 0, max: 1, noNaN: true, maxExcluded: true }), // random seed
        (anchor, bw, bh, randomVal) => {
          // Construct bounds large enough to contain everything
          const margin = BUBBLE_LAYOUT_DEFAULTS.margin
          const boundsRect: Rect = {
            left: anchor.left - 600,
            top: anchor.top - 600,
            right: anchor.right + 600,
            bottom: anchor.bottom + 600,
          }

          // Use a deterministic random that returns the fixed value
          let callCount = 0
          const fixedRandom = () => {
            callCount++
            return randomVal
          }

          const result = pickBubblePlacementWithinBounds(anchor, bw, bh, boundsRect, {
            random: fixedRandom,
          })

          // Recompute with same jitter to get all candidates and their scores
          const jitterX = Math.round((randomVal - 0.5) * BUBBLE_LAYOUT_DEFAULTS.jitterX)
          const jitterY = Math.round((randomVal - 0.5) * BUBBLE_LAYOUT_DEFAULTS.jitterY)
          const gap = resolveBubbleGap(anchor, bh)
          const petPadding = BUBBLE_LAYOUT_DEFAULTS.petPadding
          const avoidRect = expandRect(anchor as unknown as Rect, petPadding)
          const candidates = buildBubbleCandidates(anchor, bw, bh, jitterX, jitterY, gap)

          const scores = candidates.map(c =>
            scoreBubblePlacementWithinBounds(c, bw, bh, boundsRect, avoidRect, margin)
          )
          const minScore = Math.min(...scores)

          const resultScore = scoreBubblePlacementWithinBounds(result, bw, bh, boundsRect, avoidRect, margin)
          expect(resultScore).toBeLessThanOrEqual(minScore + 6)
        }
      ),
      { numRuns: 100 }
    )
  })
})

// ── Property 7: jitter value stays in range ───────────────────────────────────
// Feature: bubble-layout, Property 7: jitter value stays in range
// Validates: Requirements 3.3

describe('jitter formula', () => {
  it('Property 7 — round((random - 0.5) * range) is in [-range/2, range/2]', () => {
    fc.assert(
      fc.property(
        fc.double({ min: 0, max: 1, noNaN: true, maxExcluded: true }),
        fc.integer({ min: 0, max: 200 }),
        (randomVal, range) => {
          const jitter = Math.round((randomVal - 0.5) * range)
          expect(jitter).toBeGreaterThanOrEqual(-range / 2)
          expect(jitter).toBeLessThanOrEqual(range / 2)
        }
      ),
      { numRuns: 100 }
    )
  })
})


// ── Property 10: priority replacement rule ────────────────────────────────────
// Feature: bubble-layout, Property 10: priority replacement rule
// Validates: Requirements 6.2, 6.3

describe('canReplaceBubble', () => {
  it('Property 10 — canReplaceBubble(a, b) === (a >= b) for all priority pairs', () => {
    const priorities = [
      BubblePriority.Chat,
      BubblePriority.Notification,
      BubblePriority.Error,
      BubblePriority.Approval,
    ]

    fc.assert(
      fc.property(
        fc.constantFrom(...priorities),
        fc.constantFrom(...priorities),
        (incoming, current) => {
          expect(canReplaceBubble(incoming, current)).toBe(incoming >= current)
        }
      ),
      { numRuns: 100 }
    )
  })
})

// ── Property 11: wide-bubble size constraint ──────────────────────────────────
// Feature: bubble-layout, Property 11: wide-bubble size constraint
// Validates: Requirements 7.1, 7.2, 7.3

describe('resolveWideBubbleSize', () => {
  it('Property 11 — result has width > height OR width == maxWidth, and minWidth <= width <= maxWidth', () => {
    fc.assert(
      fc.property(
        fc.integer({ min: 100, max: 200 }),  // naturalWidth
        fc.integer({ min: 150, max: 400 }),  // naturalHeight (tall content)
        (naturalWidth, naturalHeight) => {
          const minWidth = BUBBLE_LAYOUT_DEFAULTS.minWidth
          const maxWidth = BUBBLE_LAYOUT_DEFAULTS.maxWidth

          // Simulate text wrapping: wider = shorter, area roughly constant
          const area = naturalWidth * naturalHeight
          const measureAtWidth = (width: number | null) => {
            if (width == null) return { width: naturalWidth, height: naturalHeight }
            const h = Math.max(1, Math.round(area / width))
            return { width, height: h }
          }

          const result = resolveWideBubbleSize(measureAtWidth)

          expect(result.width).toBeGreaterThanOrEqual(minWidth)
          expect(result.width).toBeLessThanOrEqual(maxWidth)
          expect(result.width > result.height || result.width === maxWidth).toBe(true)
        }
      ),
      { numRuns: 100 }
    )
  })
})


// ── Integration-level unit tests ────────────────────────────────────────

describe('pickBubblePlacement — integration', () => {
  it('keeps the bubble off the pet when there is room above', () => {
    const rect: AnchorRect = { left: 500, top: 500, right: 620, bottom: 620, width: 120, height: 120 }
    const placement = pickBubblePlacement(rect, 200, 90, 1440, 900, {
      random: () => 0.5,
    })
    const bubbleRect = resolveBubbleRect(
      placement,
      200,
      90,
      1440,
      900,
      BUBBLE_LAYOUT_DEFAULTS.margin
    )
    const keepOutRect = expandRect(rect as unknown as Rect, BUBBLE_LAYOUT_DEFAULTS.petPadding)

    expect(['top', 'top-right', 'top-left']).toContain(placement.name)
    expect(rectOverlapArea(bubbleRect, keepOutRect)).toBe(0)
  })

  it('clamps the resolved bubble inside the screen margin', () => {
    const rect: AnchorRect = { left: 12, top: 180, right: 132, bottom: 300, width: 120, height: 120 }
    const placement = pickBubblePlacement(rect, 220, 90, 320, 480, {
      random: () => 0.5,
    })
    const bubbleRect = resolveBubbleRect(
      placement,
      220,
      90,
      320,
      480,
      BUBBLE_LAYOUT_DEFAULTS.margin
    )

    expect(bubbleRect.left).toBeGreaterThanOrEqual(BUBBLE_LAYOUT_DEFAULTS.margin)
    expect(bubbleRect.top).toBeGreaterThanOrEqual(BUBBLE_LAYOUT_DEFAULTS.margin)
    expect(bubbleRect.right).toBeLessThanOrEqual(320 - BUBBLE_LAYOUT_DEFAULTS.margin)
  })

  it('keeps the bubble inside negative-coordinate monitor bounds', () => {
    const rect: AnchorRect = { left: -1320, top: 320, right: -1200, bottom: 440, width: 120, height: 120 }
    const boundsRect: Rect = { left: -1440, top: 0, right: 0, bottom: 900 }
    const placement = pickBubblePlacementWithinBounds(rect, 220, 90, boundsRect, {
      random: () => 0.5,
    })
    const bubbleRect = resolveBubbleRectWithinBounds(
      placement,
      220,
      90,
      boundsRect,
      BUBBLE_LAYOUT_DEFAULTS.margin
    )

    expect(bubbleRect.left).toBeGreaterThanOrEqual(boundsRect.left + BUBBLE_LAYOUT_DEFAULTS.margin)
    expect(bubbleRect.top).toBeGreaterThanOrEqual(boundsRect.top + BUBBLE_LAYOUT_DEFAULTS.margin)
    expect(bubbleRect.right).toBeLessThanOrEqual(boundsRect.right - BUBBLE_LAYOUT_DEFAULTS.margin)
    expect(rectOverlapArea(bubbleRect, expandRect(rect as unknown as Rect, BUBBLE_LAYOUT_DEFAULTS.petPadding))).toBe(0)
  })
})
