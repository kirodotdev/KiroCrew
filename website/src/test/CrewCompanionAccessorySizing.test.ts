/**
 * A prop's SIZE must not change with the companion's expression.
 *
 * Every accessory is laid out in eye-span units, and the span used to be read from the
 * CURRENT pose. But poses are expressions, not different heads: `primary` spans 15.5%
 * of the box and `4th` -- the squint that a celebration puts on -- spans 11.5%. So the
 * party hat and the shades rendered a quarter smaller exactly when the companion was
 * happy, which is when a user is most likely to be looking at it.
 *
 * These pin the two halves of the rule separately, because the fix is easy to undo by
 * "simplifying" one of them: lengths come from a fixed reference, the anchor still
 * follows the live pose.
 */
import { describe, it, expect } from 'vitest'
import { renderToStaticMarkup } from 'react-dom/server'
import { createElement } from 'react'

import GhostAccessoryLayer from '../apps/crew-companion/GhostAccessoryLayer'
import { CELEBRATE_PROPS } from '../apps/crew-companion/ghostAccessories'
import { GHOST_EYE_MAP } from '../apps/crew-companion/ghostEyes'

/** The `width: N%` of the prop's positioning box. */
function widthPct(html: string): number | null {
  const m = html.match(/width:\s*([\d.]+)%/)
  return m ? Number(m[1]) : null
}

/** The `left: N%` of the prop's positioning box. */
function leftPct(html: string): number | null {
  const m = html.match(/left:\s*([\d.]+)%/)
  return m ? Number(m[1]) : null
}

function markup(id: string, pose: string): string {
  return renderToStaticMarkup(createElement(GhostAccessoryLayer, { id, pose }))
}

const WEARABLE = CELEBRATE_PROPS.filter((p) => p !== 'none')

describe('accessory sizing', () => {
  it('has poses whose eye spans actually differ (the premise)', () => {
    // If this ever stops being true the rest of this file proves nothing.
    const span = (p: string) =>
      Math.abs(GHOST_EYE_MAP[p][1].x - GHOST_EYE_MAP[p][0].x)
    expect(span('primary')).toBeGreaterThan(span('4th') + 3)
  })

  for (const id of WEARABLE) {
    it(`draws '${id}' the same size at rest and mid-celebration`, () => {
      const atRest = widthPct(markup(id, 'primary'))
      const celebrating = widthPct(markup(id, '4th'))
      expect(atRest).not.toBeNull()
      expect(celebrating).toBeCloseTo(atRest as number, 5)
    })
  }

  it('still moves the prop with the face', () => {
    // The other half of the rule: size is fixed, POSITION is not. Shades that ignored
    // the pose would sit beside the eyes they are supposed to cover.
    const atRest = leftPct(markup('sunglasses', 'primary'))
    const celebrating = leftPct(markup('sunglasses', '4th'))
    expect(celebrating).not.toBeCloseTo(atRest as number, 1)
  })
})
