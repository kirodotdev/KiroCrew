/**
 * Every eye pose must land inside the ghost's head.
 *
 * This exists because a pose regression is INVISIBLE to the rest of the suite: the
 * eye map is just numbers, so a pair placed outside the silhouette type-checks, keeps
 * every other test green, and only shows up as a companion that looks wrong on screen.
 * `4th` shipped that way — its right eye's centre sat past the head's right edge, so
 * the eye straddled the outline and hung into empty space. `happy`, `scared` and
 * `error` all resolve to `4th`, which is why it was visible on every session-complete
 * celebration.
 *
 * The bounds below were MEASURED from the shipped body art (`kiro_idle.svg` rendered
 * into the 128px pet box with `object-fit: contain`) by sampling the white silhouette
 * at each pose's own eye height — the head narrows toward the top, so a single bound
 * for all poses would be either too loose to catch anything or too tight to pass.
 * Re-measure if the body art ever changes; the numbers are only valid for that file.
 */
import { describe, it, expect } from 'vitest'
import { GHOST_EYE_MAP } from '../apps/crew-companion/ghostEyes'

/** Head silhouette extent (box percent) at each pose's eye height. */
const HEAD_AT_EYE_HEIGHT: Record<string, { left: number; right: number }> = {
  primary: { left: 26.3, right: 80.2 },
  '2nd': { left: 27.3, right: 79.3 },
  '3rd': { left: 27.3, right: 79.2 },
  '4th': { left: 26.0, right: 80.3 },
  docked: { left: 31.9, right: 74.2 },
}

/** The art's black outline. An eye inside this band still reads as clipped. */
const OUTLINE = 2.0

describe('ghost eye poses', () => {
  it('has measured head bounds for every pose in the map', () => {
    // A new pose with no bounds would otherwise skip the check silently.
    expect(Object.keys(GHOST_EYE_MAP).sort()).toEqual(
      Object.keys(HEAD_AT_EYE_HEIGHT).sort(),
    )
  })

  for (const [pose, eyes] of Object.entries(GHOST_EYE_MAP)) {
    it(`draws both '${pose}' eyes inside the head, clear of the outline`, () => {
      const head = HEAD_AT_EYE_HEIGHT[pose]
      for (const eye of eyes) {
        // Positioned with translate(-50%, -50%), so x is the eye's CENTRE.
        expect(eye.x - eye.w / 2).toBeGreaterThan(head.left + OUTLINE)
        expect(eye.x + eye.w / 2).toBeLessThan(head.right - OUTLINE)
      }
    })
  }

  it('keeps each pose looking the way it was drawn to look', () => {
    // The fix slid pairs sideways; it must not have reordered or collapsed them.
    // Without this, "move the eyes inside the head" could be satisfied by stacking
    // both eyes on the nose, which passes the bounds check and looks like a cyclops.
    for (const [pose, eyes] of Object.entries(GHOST_EYE_MAP)) {
      expect(eyes, `${pose} should be a pair`).toHaveLength(2)
      const gap = eyes[1].x - eyes[0].x
      expect(gap, `${pose} eyes must stay left-to-right`).toBeGreaterThan(0)
      // Narrower than an eye's own width would mean the pair had merged.
      expect(gap, `${pose} eyes must not overlap`).toBeGreaterThan(eyes[0].w)
    }
  })
})
