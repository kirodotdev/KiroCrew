/**
 * Walk geometry, tested at the pure-function layer (like the bubble layout) so the
 * rAF/React shell around it does not have to be simulated.
 *
 * These pin the numbers the desktop app shipped — 6ms/px floored at 800ms, ±6°
 * diagonal tilt — that make a hop in the overlay feel identical to the one that
 * shipped in the standalone app. The idle fidget drives `walkPath` with these.
 */
import { describe, it, expect } from 'vitest'
import * as fc from 'fast-check'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import {
  walkDirFor,
  walkTiltFor,
  walkDurationMs,
  WALK_MIN_DIST,
} from '../apps/crew-companion/walkMath'

/** A hop is never shorter than this (HOP_MIN), so WALK_MIN_DIST must sit below it. */
const HOP_FLOOR = 30

describe('walkDirFor', () => {
  it('faces left when the target is to the left, right otherwise', () => {
    expect(walkDirFor(500, 100)).toBe(-1)
    expect(walkDirFor(100, 500)).toBe(1)
    // A zero-length leg is not "left".
    expect(walkDirFor(300, 300)).toBe(1)
  })

  it('only ever returns -1 or 1', () => {
    fc.assert(
      fc.property(
        fc.integer({ min: -2000, max: 2000 }),
        fc.integer({ min: -2000, max: 2000 }),
        (a, b) => {
          expect([-1, 1]).toContain(walkDirFor(a, b))
        },
      ),
      { numRuns: 100 },
    )
  })
})

describe('walkTiltFor', () => {
  it('a straight horizontal leg does not tilt', () => {
    expect(walkTiltFor(0, 200, 400, 200)).toBe(0)
    expect(walkTiltFor(400, 200, 0, 200)).toBe(0)
  })

  it('a straight vertical leg does not tilt (90° is outside the diagonal bands)', () => {
    expect(walkTiltFor(200, 0, 200, 400)).toBe(0)
  })

  it('a 45° leg tilts, clamped to ±6°', () => {
    const t = walkTiltFor(0, 0, 100, 100)
    expect(t).not.toBe(0)
    expect(Math.abs(t)).toBeLessThanOrEqual(6)
  })

  it('tilt is always within ±6° for any leg', () => {
    fc.assert(
      fc.property(
        fc.integer({ min: -2000, max: 2000 }),
        fc.integer({ min: -2000, max: 2000 }),
        fc.integer({ min: -2000, max: 2000 }),
        fc.integer({ min: -2000, max: 2000 }),
        (x1, y1, x2, y2) => {
          const t = walkTiltFor(x1, y1, x2, y2)
          expect(t).toBeGreaterThanOrEqual(-6)
          expect(t).toBeLessThanOrEqual(6)
        },
      ),
      { numRuns: 200 },
    )
  })
})

describe('walkDurationMs', () => {
  it('is never below the 800ms floor', () => {
    expect(walkDurationMs(0, 0, 0, 0)).toBe(800)
    expect(walkDurationMs(0, 0, 10, 0)).toBe(800)
  })

  it('is 6ms per pixel once past the floor', () => {
    // 200px * 6 = 1200ms, above the 800 floor.
    expect(walkDurationMs(0, 0, 200, 0)).toBe(1200)
  })

  it('always returns at least 800 and matches the max(800, dist*6) formula', () => {
    fc.assert(
      fc.property(
        fc.integer({ min: -2000, max: 2000 }),
        fc.integer({ min: -2000, max: 2000 }),
        fc.integer({ min: -2000, max: 2000 }),
        fc.integer({ min: -2000, max: 2000 }),
        (x1, y1, x2, y2) => {
          const dist = Math.hypot(x2 - x1, y2 - y1)
          const d = walkDurationMs(x1, y1, x2, y2)
          expect(d).toBeGreaterThanOrEqual(800)
          expect(d).toBe(Math.max(800, dist * 6))
        },
      ),
      { numRuns: 200 },
    )
  })
})

describe('WALK_MIN_DIST', () => {
  it('is a small positive skip threshold, below the hop floor', () => {
    expect(WALK_MIN_DIST).toBeGreaterThan(0)
    expect(WALK_MIN_DIST).toBeLessThan(HOP_FLOOR)
  })
})

// ── the companion page wires the system in additively ───────────────────────
// Guards the brief's requirement: the fidget/walk system is ADDED, the roaming
// "wander" invention is GONE, and the bubble and playful-motion wiring that already
// lived in pet.tsx is left intact.

describe('pet.tsx wiring', () => {
  const SOURCE = readFileSync(
    resolve(__dirname, '../apps/crew-companion/pet.tsx'),
    'utf-8',
  )

  it('mounts the walk, idle-fidget and random-clip hooks', () => {
    expect(SOURCE).toContain('useWalking(')
    expect(SOURCE).toContain('useIdleFidget(')
    expect(SOURCE).toContain('useRandomClips(')
  })

  it('no longer references the deleted roaming "wander" invention', () => {
    expect(SOURCE).not.toContain('useWander')
    expect(SOURCE).not.toContain('wanderMath')
    expect(SOURCE).not.toContain('dockEdgeFor')
  })

  it('still wires the bubble placement it always had', () => {
    expect(SOURCE).toMatch(/pickBubblePlacement|nextBubble/)
  })

  it('still wires the playful idle motion', () => {
    expect(SOURCE).toContain('usePlayfulMotion')
  })

  it('docks by checking the edge when a walk ends', () => {
    expect(SOURCE).toContain('handleWalkEnd')
  })

  it('honours reduced motion at the gate', () => {
    expect(SOURCE).toContain('prefers-reduced-motion')
  })
})
