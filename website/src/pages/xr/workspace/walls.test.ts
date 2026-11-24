import { describe, expect, it } from 'vitest'
import { M_PER_PX, PAGES, RINGS } from './layout'
import { WALLS, WALLS_GL, WALLS_MOST, dimEnough, reachInRoom } from './walls'

describe('the drawn room', () => {
  it('has no colour brighter than a quarter of a page\'s white', () => {
    expect(WALLS_MOST).toBe(0x40)
    for (const [name, gl] of Object.entries(WALLS_GL)) expect(dimEnough(gl), name).toBe(true)
    expect(dimEnough(0x414141)).toBe(false)
    expect(dimEnough(0x00ff00)).toBe(false)
  })

  it('holds the ring of pages, to true scale', () => {
    // A page stands 1.75 m away and is 1.56 m wide: its far corners are well inside the walls.
    const corner = Math.hypot(PAGES.distanceM, (PAGES.cssWidth * M_PER_PX) / 2)
    expect(WALLS.halfM).toBeGreaterThan(corner + 1)
    // A room that a person knows, and a tall one: a ceiling between 2.4 and 3.5 m, a door 2 m high, a desk at a desk's height.
    expect(WALLS.heightM).toBeGreaterThanOrEqual(2.4)
    expect(WALLS.heightM).toBeLessThanOrEqual(3.5)
    // The top of a page of the second ring, which leans back to face the head, is under the ceiling.
    const lean = (RINGS.upper.pitchDeg * Math.PI) / 180
    expect(PAGES.eyeM + RINGS.upper.distanceM * Math.sin(lean) + ((PAGES.cssHeight * M_PER_PX) / 2) * Math.cos(lean)).toBeLessThan(WALLS.heightM - 0.05)
    expect(WALLS.door.heightM).toBeGreaterThan(1.95)
    expect(WALLS.door.heightM + WALLS.door.frameM).toBeLessThan(WALLS.heightM)
    expect(WALLS.desk.topM).toBeGreaterThanOrEqual(0.7)
    expect(WALLS.desk.topM).toBeLessThanOrEqual(0.76)
    // The top of a page that is level with the eyes is under the ceiling, and its bottom is over the desk.
    const half = (PAGES.cssHeight * M_PER_PX) / 2
    expect(PAGES.eyeM + half).toBeLessThan(WALLS.heightM)
    expect(PAGES.eyeM - half).toBeGreaterThan(WALLS.desk.topM + 0.2)
  })

  it('stops a page 0.3 m short of a wall', () => {
    const eye = [0, PAGES.eyeM, 0] as const
    const most = WALLS.halfM - WALLS.clearM
    expect(most).toBeCloseTo(3.7)
    // Straight ahead, to the side and toward a corner: so far from the room's middle, measured level.
    expect(reachInRoom(eye, [0, 0, -1], PAGES.farM)).toBeCloseTo(most)
    expect(reachInRoom(eye, [1, 0, 0], PAGES.farM)).toBeCloseTo(most)
    expect(reachInRoom(eye, [Math.SQRT1_2, 0, -Math.SQRT1_2], PAGES.farM)).toBeCloseTo(most)
    // A ray that climbs is longer by its slope: the page is still that far from the wall.
    const up = (30 * Math.PI) / 180
    expect(reachInRoom(eye, [0, Math.sin(up), -Math.cos(up)], PAGES.farM)).toBeCloseTo(most / Math.cos(up))
    // A hand that is held out 0.4 m has that much less to push.
    expect(reachInRoom([0, 1.3, -0.4], [0, 0, -1], PAGES.farM)).toBeCloseTo(most - 0.4)
    // A page that stands nearer stays where it is, and one that is told to go 5 m does not.
    expect(reachInRoom(eye, [0, 0, -1], PAGES.distanceM)).toBe(PAGES.distanceM)
    expect(reachInRoom(eye, [0, 0, -1], 5)).toBeLessThan(WALLS.halfM)
    // Straight up there is no wall, and a ray from outside the limit is left alone.
    expect(reachInRoom(eye, [0, 1, 0], 5)).toBe(5)
    expect(reachInRoom([0, 1.6, -3.9], [0, 0, -1], 2)).toBe(2)
  })

  it('keeps a page of the width it opens with in front of the wall, wherever round the person it stands', () => {
    const most = WALLS.halfM - WALLS.clearM
    // The page faces the person: its far corners are this far from the room's middle.
    expect(Math.hypot(most, (PAGES.cssWidth * M_PER_PX) / 2)).toBeLessThan(WALLS.halfM)
  })

  it('has its desk under the row where the crew stands, 0.30 m nearer than the first page', () => {
    const row = PAGES.distanceM - 0.3
    expect(WALLS.desk.innerM).toBeLessThan(row - 0.2)
    expect(WALLS.desk.outerM).toBeGreaterThan(row + 0.2)
  })
})
