/**
 * walkMath — pure geometry for the companion's walk, extracted so it can be
 * unit-tested without React, rAF or a DOM (the same split the bubble layout uses).
 *
 * Every number here is carried verbatim from the desktop app's `useWalking`: the
 * 6ms-per-pixel duration floored at 800ms and the ±6° diagonal tilt. This is a port,
 * not a redesign — the maths must match so a walk in the overlay feels identical to
 * the one that shipped in the standalone app.
 */

export type Edge = 'left' | 'right'

/** Which way the art faces for a leg from `fromX` to `toX` (−1 = left, 1 = right). */
export function walkDirFor(fromX: number, toX: number): -1 | 1 {
  return toX < fromX ? -1 : 1
}

/**
 * Body tilt in degrees for a walk leg, clamped to ±6. Only diagonal legs tilt; a
 * straight horizontal or vertical leg stays upright — the desktop app treats the
 * 30–60° and 120–150° bands as diagonal.
 */
export function walkTiltFor(fromX: number, fromY: number, toX: number, toY: number): number {
  const angle = Math.atan2(toY - fromY, toX - fromX) * (180 / Math.PI)
  const absDeg = Math.abs(angle)
  const isDiagonal = (absDeg > 30 && absDeg < 60) || (absDeg > 120 && absDeg < 150)
  return isDiagonal ? Math.max(-6, Math.min(6, angle * 0.07)) : 0
}

/** Walk duration in ms: 6ms per pixel of travel, never below 800ms. */
export function walkDurationMs(fromX: number, fromY: number, toX: number, toY: number): number {
  const dist = Math.hypot(toX - fromX, toY - fromY)
  return Math.max(800, dist * 6)
}

/**
 * A leg shorter than this reads as "already there": the desktop app skips the
 * animation and settles straight away rather than firing a sub-frame walk.
 */
export const WALK_MIN_DIST = 5
