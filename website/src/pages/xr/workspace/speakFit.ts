/**
 * Where the sheet he speaks on stands in his view (P2 with places). What he writes on is the thing he looks at: it
 * is whole inside the view, and it stays joined to its Bot, its lower edge at the Bot's clipboard so that the Bot's
 * face is free under it. A sheet that would run past the view's top is made smaller, down to its least width; one
 * that would run past a side shifts in. It never comes down: a sheet too tall at its least size runs past the top.
 *
 * All in the camera's own frame, in metres: x to the right, y up, `d` how far ahead.
 */

/** The share of the view's half that the sheet may reach: a little air at the edge. */
export const SPEAK_EDGE = 0.96

export interface SpeakFitAsk {
  /** The middle of the sheet's lower edge, where its Bot holds it up. */
  low: readonly [number, number]
  /** How far ahead of the eye it stands. */
  d: number
  /** The view's half width and half height at one metre ahead. */
  tanX: number
  tanY: number
  /** The sheet's size as it reads best at this distance, and the least width it may have. */
  w: number
  h: number
  leastW: number
}

/** The sheet's scale and its middle. */
export interface SpeakFit { scale: number; x: number; y: number }

export function speakFit({ low, d, tanX, tanY, w, h, leastW }: SpeakFitAsk): SpeakFit {
  const [halfX, halfY] = [d * tanX * SPEAK_EDGE, d * tanY * SPEAK_EDGE]
  const least = Math.min(1, leastW / w)
  // Smaller, so that it ends under the view's top and under no part of its own Bot; not narrower than the view either.
  const room = halfY - low[1]
  const scale = Math.max(least, Math.min(1, room / h, (2 * halfX) / w))
  const [hw, hh] = [(w * scale) / 2, (h * scale) / 2]
  const side = Math.max(0, halfX - hw)
  const x = Math.max(-side, Math.min(side, low[0]))
  // Its lower edge stays at its Bot: one too tall at its least size grows upward, past the view's top.
  return { scale, x, y: low[1] + hh }
}
