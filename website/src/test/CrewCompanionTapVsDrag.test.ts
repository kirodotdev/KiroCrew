/**
 * Tap vs drag on the companion.
 *
 * A drag also fires a click when the button is released, so without a distance test
 * every drag ended by opening the panel — which is exactly what happened before this
 * was wired. The threshold mirrors CLICK_SLOP in pet.tsx (and `moved > 6` in the
 * desktop app's PetWidget).
 */
import { describe, expect, it } from 'vitest'

/** CLICK_SLOP in pet.tsx. */
const CLICK_SLOP = 6

/** The predicate pet.tsx's onClick applies. */
function isTap(
  down: { x: number; y: number } | null,
  up: { x: number; y: number },
): boolean {
  const moved = down ? Math.hypot(up.x - down.x, up.y - down.y) : 999
  return moved <= CLICK_SLOP
}

describe('tap vs drag', () => {
  it('treats a still press as a tap', () => {
    expect(isTap({ x: 100, y: 100 }, { x: 100, y: 100 })).toBe(true)
  })

  it('tolerates the hand-shake a real tap has', () => {
    expect(isTap({ x: 100, y: 100 }, { x: 103, y: 102 })).toBe(true)
  })

  it('rejects a release that travelled past the threshold', () => {
    expect(isTap({ x: 100, y: 100 }, { x: 140, y: 260 })).toBe(false)
  })

  it('rejects movement on a single axis too', () => {
    expect(isTap({ x: 100, y: 100 }, { x: 100, y: 130 })).toBe(false)
  })

  it('measures diagonally, not per-axis', () => {
    // 5px on each axis is ~7.07px of travel — a drag, even though neither axis alone
    // exceeds the threshold.
    expect(isTap({ x: 0, y: 0 }, { x: 5, y: 5 })).toBe(false)
  })

  it('is not a tap when the press point was never recorded', () => {
    // Defensive: a click with no preceding mousedown must not open the panel.
    expect(isTap(null, { x: 0, y: 0 })).toBe(false)
  })
})
