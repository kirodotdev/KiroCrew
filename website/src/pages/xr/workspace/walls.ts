/**
 * The room that a button switches on: its sizes and its colours, as plain numbers. `RoomWalls.tsx` draws them.
 *
 * It is a background and nothing more. It is to true scale, so that the eye has a floor, a skirting line, a door
 * and a desk to measure a page and a Bot against. It is matte and dim: flat colours with no light on them, none
 * brighter than a quarter of a page's white, so that a page reads as well as on the dark behind it.
 *
 * It is solid to a pointer. A ray that meets a wall, the floor, the ceiling, the door, the window or the desk before
 * it meets a page stops there, and what it does there is what it does on empty space: a page behind takes nothing.
 * And no page is put behind a wall while the room is on (`reachInRoom`).
 */
import { PAGES } from './layout'

/** No channel of a colour of the room is brighter than this: a quarter of white. */
export const WALLS_MOST = 0x40

export const WALLS = {
  /**
   * The room is square round the place where the person stands: so far to each wall, and so high. It is a tall
   * room: the second ring of pages stands over the first (`layout.ts`, `RINGS`), and the top of a page there is
   * 3.28 m up. Under a lower ceiling the upper half of such a page would be behind it.
   */
  halfM: 4,
  heightM: 3.4,
  /** With the room on, the middle of a page comes no nearer to a wall than this. */
  clearM: 0.3,
  /** The line where a wall meets the floor. */
  skirtingM: 0.09,
  /** One door in the wall ahead, to the right of where the first page stands. */
  door: { leftM: 2.15, widthM: 0.9, heightM: 2.05, frameM: 0.06 },
  /** One window in the wall at the left. */
  window: { fromM: -1.1, widthM: 1.8, sillM: 0.95, heightM: 1.25, frameM: 0.05 },
  /**
   * The desk: a curved top in front of the person, under the row where the crew stands. It reaches from a little
   * nearer than the crew to under the first page, and so far to each side.
   */
  desk: { topM: 0.72, thickM: 0.04, innerM: 1.12, outerM: PAGES.distanceM + 0.03, halfDeg: 52 },
} as const

/** The colours, as GL numbers. Each wall has its own shade, so a corner reads with no light in the room. */
export const WALLS_GL = {
  floor: 0x1b1d22,
  ceiling: 0x24272e,
  ahead: 0x2e333d,
  behind: 0x2a2f38,
  left: 0x272c35,
  right: 0x30353f,
  skirting: 0x3c3e40,
  door: 0x22262e,
  frame: 0x3a3d40,
  glass: 0x18263a,
  desk: 0x3a3128,
  deskSide: 0x2b251f,
} as const

/** Whether no channel of this colour is brighter than the room allows. */
export const dimEnough = (gl: number): boolean => Math.max((gl >> 16) & 255, (gl >> 8) & 255, gl & 255) <= WALLS_MOST

type V3 = readonly [number, number, number]

/**
 * How far along a ray a page may stand while the room is on: `lengthM`, or less where the ray would take it
 * nearer to a wall than `clearM`. `from` is where the ray starts and `dir` is its direction, of length one, both in
 * the room's own frame, whose middle is where the person stood at the start.
 *
 * The limit is a circle round the room's middle, measured level: straight toward a wall a page stops 0.3 m short
 * of it, and toward a corner it stops farther from the corner. So a page of the width it opens with, which faces
 * the person, has no part behind a wall at any place round them.
 *
 * shortcut: a ray that starts outside the circle (a person who walked to a wall) is not shortened. A room that
 * follows the person, or a limit by the walls themselves, when a person does that. The floor and the ceiling set
 * no limit either: a hand can still push a page through them.
 */
export function reachInRoom(from: V3, dir: V3, lengthM: number): number {
  const most = WALLS.halfM - WALLS.clearM
  const a = dir[0] * dir[0] + dir[2] * dir[2]
  const b = 2 * (from[0] * dir[0] + from[2] * dir[2])
  const c = from[0] * from[0] + from[2] * from[2] - most * most
  // Straight up or down, or from outside: the circle sets no limit.
  if (a < 1e-9 || c > 0) return lengthM
  return Math.min(lengthM, (-b + Math.sqrt(b * b - 4 * a * c)) / (2 * a))
}
