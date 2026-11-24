import { describe, expect, it } from 'vitest'
import type { OpenHand } from '../gestures'
import type { V3 } from '../hand'
import { PALM_MENU, PalmHold, holder, mPerDp, paletteSize, tiles } from './palm'

const HEAD: V3 = [0, 1.6, 0]
const WRIST: V3 = [-0.1, 1.3, -0.4]
const from = (dx: number, dy: number, dz: number): V3 => [WRIST[0] + dx, WRIST[1] + dy, WRIST[2] + dz]
const OPEN = { index: 1, others: 1 }

/** A left hand in front of the face with its fingers up and its palm to the face: the index finger is on the person's left. */
const toFace: OpenHand = { handed: 'left', shape: OPEN, tip: from(0, 0.17, 0), joints: { wrist: WRIST, index: from(-0.02, 0.03, 0), pinky: from(0.02, 0.03, 0) } }
/** The same hand turned round: its back to the face. */
const toRoom: OpenHand = { ...toFace, joints: { wrist: WRIST, index: from(0.02, 0.03, 0), pinky: from(-0.02, 0.03, 0) } }
const fist: OpenHand = { ...toFace, shape: { index: 0.35, others: 0.3 } }
const pointing: OpenHand = { ...toFace, shape: { index: 1, others: 0.3 } }
/** Open and turned to the face, but the fingers point to the side: a hand that waves across, or lies on a desk edge. */
const sideways: OpenHand = { ...toFace, tip: from(0.17, 0.02, 0) }
/** A palm turned by forty degrees: past the angle that shows the palette, inside the one that keeps it. */
const half: OpenHand = { ...toFace, joints: { wrist: WRIST, index: from(-0.0153, 0.03, -0.0129), pinky: from(0.0153, 0.03, 0.0129) } }

describe('the palm that holds the palette', () => {
  it('shows it when an open palm has been turned to the face for about a third of a second', () => {
    const palm = new PalmHold()
    expect(palm.feed(toFace, HEAD, 1000)).toBe(false)
    expect(palm.feed(toFace, HEAD, 1000 + PALM_MENU.holdMs - 1)).toBe(false)
    expect(palm.feed(toFace, HEAD, 1000 + PALM_MENU.holdMs)).toBe(true)
    expect(PALM_MENU.holdMs).toBeGreaterThanOrEqual(300)
    expect(PALM_MENU.holdMs).toBeLessThanOrEqual(400)
  })

  it('does not show it for a fist, a hand that points, a palm turned away, or fingers that are not up', () => {
    for (const hand of [fist, pointing, toRoom, sideways, null]) {
      const palm = new PalmHold()
      for (let t = 0; t <= 2000; t += 50) expect(palm.feed(hand, HEAD, t)).toBe(false)
    }
  })

  it('starts the count again when the palm turns away before the time is up', () => {
    const palm = new PalmHold()
    palm.feed(toFace, HEAD, 0)
    palm.feed(toRoom, HEAD, 200)
    expect(palm.feed(toFace, HEAD, 300)).toBe(false)
    expect(palm.feed(toFace, HEAD, 300 + PALM_MENU.holdMs - 1)).toBe(false)
    expect(palm.feed(toFace, HEAD, 300 + PALM_MENU.holdMs)).toBe(true)
  })

  it('keeps it through a palm that wavers, and takes it away when the palm turns away, closes or is lost', () => {
    for (const gone of [toRoom, fist, null]) {
      const palm = new PalmHold()
      palm.feed(toFace, HEAD, 0)
      expect(palm.feed(toFace, HEAD, 400)).toBe(true)
      // Half turned: it would not show the palette, and it does not take it away.
      expect(new PalmHold().feed(half, HEAD, 0) || new PalmHold().feed(half, HEAD, 999)).toBe(false)
      expect(palm.feed(half, HEAD, 500)).toBe(true)
      expect(palm.feed(gone, HEAD, 600)).toBe(false)
      // And it has to be held again to come back.
      expect(palm.feed(toFace, HEAD, 650)).toBe(false)
      expect(palm.feed(toFace, HEAD, 650 + PALM_MENU.holdMs)).toBe(true)
    }
  })

  it('is held by either hand, and stays with the hand that has it', () => {
    expect(holder(false, false, null)).toBeNull()
    expect(holder(true, false, null)).toBe('left')
    expect(holder(false, true, null)).toBe('right')
    expect(holder(true, true, 'right')).toBe('right')
    expect(holder(true, true, 'left')).toBe('left')
    expect(holder(true, false, 'right')).toBe('left')
    expect(holder(false, false, 'left')).toBeNull()
  })
})

describe('the palette\'s tiles', () => {
  it('are two rows of four that do not touch, the first on top, in reading order', () => {
    const all = tiles(8)
    expect(all).toHaveLength(8)
    expect(all.slice(0, 4).every(t => t.y > 0) && all.slice(4).every(t => t.y < 0)).toBe(true)
    for (const row of [all.slice(0, 4), all.slice(4)]) for (let i = 1; i < 4; i += 1) expect(row[i].x - row[i - 1].x).toBe(PALM_MENU.tileDp[0] + PALM_MENU.gapDp)
    expect(all[0].y - all[4].y).toBe(PALM_MENU.tileDp[1] + PALM_MENU.gapDp)
    expect(paletteSize(8)).toEqual([4 * 132 + 3 * 8, 2 * 76 + 8])
  })

  it('are each big enough for an icon over a label of 14 dp, at whatever distance the palm is', () => {
    expect(PALM_MENU.labelDp).toBe(14)
    // The longest of the dashboard's eight labels in English has 13 letters; a letter of 14 dp text is about 8 dp wide.
    expect(PALM_MENU.tileDp[0]).toBeGreaterThanOrEqual(13 * 8 + 16)
    expect(PALM_MENU.tileDp[1]).toBeGreaterThanOrEqual(PALM_MENU.iconDp + PALM_MENU.labelDp + 24)
    // One dp is the same angle at the eye at any distance: 0.868 mm at one metre.
    expect(mPerDp(1)).toBeCloseTo(0.000868, 9)
    expect(mPerDp(0.4) * PALM_MENU.labelDp * 1000).toBeCloseTo(4.86, 2)
  })
})
