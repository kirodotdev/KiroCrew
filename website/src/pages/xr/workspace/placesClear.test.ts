import { describe, expect, it } from 'vitest'
import { Vector3 } from 'three'
import { M_PER_PX, PAGES, placeAt, ring } from './layout'
import { MID_M, SPOTS, deskAt, spotOf } from './places'

const HEAD = new Vector3(0, PAGES.eyeM, 0)
const DEG = 180 / Math.PI

/** Whether a point stands in front of a page (nearer than its plane, inside its box as the eye sees it), and by how much. */
function inFront(p: readonly number[], page: { at: Vector3 }): { inside: boolean; nearerM: number; yawDeg: number; pitchDeg: number } {
  const n = page.at.clone().sub(HEAD).normalize()
  const v = new Vector3(p[0], p[1], p[2]).sub(HEAD)
  const along = v.dot(n)
  const planeM = page.at.clone().sub(HEAD).length()
  // Where the line from the eye through the point meets the page's plane, in the page's own axes.
  const hit = v.clone().multiplyScalar(planeM / along)
  const right = new Vector3(0, 1, 0).cross(n.clone().negate()).normalize()
  const up = n.clone().negate().cross(right).normalize()
  const off = hit.sub(page.at.clone().sub(HEAD))
  const halfW = (PAGES.cssWidth * M_PER_PX) / 2
  const halfH = (PAGES.cssHeight * M_PER_PX) / 2
  const inside = along > 0 && Math.abs(off.dot(right)) < halfW && Math.abs(off.dot(up)) < halfH
  return { inside, nearerM: +(planeM - along).toFixed(3), yawDeg: +(Math.atan2(v.x, -v.z) * DEG).toFixed(1), pitchDeg: +(Math.atan2(v.y, Math.hypot(v.x, v.z)) * DEG).toFixed(1) }
}

describe('the Bots\' places and the default page places (D78 1)', () => {
  const pages = ring(3).map(s => ({ yawDeg: s.yawDeg, at: placeAt(HEAD, s.yawDeg, s.pitchDeg, s.distanceM) }))
  it('lists each place whose desk or body stands in front of a default page', () => {
    const found: unknown[] = []
    for (let i = 0; i < SPOTS.length + 12; i += 1) {
      const feet = spotOf(i)
      const desk = deskAt(feet)
      const body = [feet[0], feet[1] + MID_M, feet[2]]
      for (const page of pages) {
        for (const [what, p] of [['desk', desk], ['body', body]] as const) {
          const f = inFront(p, page)
          if (f.inside && f.nearerM > 0) found.push({ place: i, what, page: page.yawDeg, ...f })
        }
      }
    }
    // Before D78 the first place (-16 degrees, 1.72 m) stood 0.09 m in front of the first page and its desk 0.19 m.
    expect(found).toEqual([])
  })
})
