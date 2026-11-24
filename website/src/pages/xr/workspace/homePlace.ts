/**
 * Where Home's strip stands in the room now, for what must keep clear of it: Home is the room's, and the palette on
 * his hand is his (`Palette.tsx` lowers itself until its cards' row is under the strip). `Home.tsx` writes it; `null`
 * while Home is not the strip (its large form, or not drawn). `since`: the line of what changed since he left, which
 * stands over the strip, or under it when a page is open (`HomeSince.tsx` writes it): the strip and the line are one
 * band, and the cards come down under both.
 */
import type { Vector3 } from 'three'

type Flat = { center: Vector3; widthM: number; heightM: number }
export type P3 = readonly [number, number, number]
/**
 * `closeMarks`: each panel's close-mark press area with its top right corner's handle, two opposite corners in the
 * room (`Panel.tsx` writes it every
 * frame). The strip gives way to them (D54): the thing he aims at wins over the thing that is always there.
 */
export const homePlace: { strip: Flat | null; since: Flat | null; closeMarks: Map<string, readonly [P3, P3]> } = { strip: null, since: null, closeMarks: new Map() }

const DEG = 180 / Math.PI
/** What the eye at `head` sees of a point: so far to the right of -z, so far up (degrees). */
export const lookAt = (head: Vector3, p: { x: number; y: number; z: number }): { yaw: number; pitch: number } => {
  const dx = p.x - head.x
  const dz = p.z - head.z
  return { yaw: Math.atan2(dx, -dz) * DEG, pitch: Math.atan2(p.y - head.y, Math.hypot(dx, dz)) * DEG }
}

/** The air kept between the cards and the strip, as the eye sees it (degrees). */
export const STRIP_CLEAR_DEG = 1

/**
 * How far a thing must come down (degrees of pitch at the eye) to stand under the strip: `box` is what the eye sees
 * of it (yaw from `yaw0` to `yaw1`, pitch from `low` to `top`). 0 when it is beside the strip, or over or under it
 * already with air between.
 */
export function underStrip(head: Vector3, box: { yaw0: number; yaw1: number; low: number; top: number }): number {
  const s = homePlace.strip
  if (!s) return 0
  const at = lookAt(head, s.center)
  // The strip is flat and faces him: its corners are farther than its middle, so its edges lie lower at its ends.
  // What the eye sees of it is taken from its four corners: the lowest and the highest of them, the outermost two.
  // The since line's corners join them: the band is both.
  const cornersOf = (f: Flat) => {
    const flat = Math.hypot(f.center.x - head.x, f.center.z - head.z) || 1e-6
    const right = { x: -(f.center.z - head.z) / flat, z: (f.center.x - head.x) / flat }
    return [[-1, -1], [1, -1], [1, 1], [-1, 1]].map(([sx, sy]) => lookAt(head, { x: f.center.x + (right.x * sx * f.widthM) / 2, y: f.center.y + (sy * f.heightM) / 2, z: f.center.z + (right.z * sx * f.widthM) / 2 }))
  }
  const corners = [...cornersOf(s), ...(homePlace.since ? cornersOf(homePlace.since) : [])]
  const turn = (v: number) => ((((v - at.yaw) % 360) + 540) % 360) - 180
  const halfYaw = Math.max(...corners.map(c => Math.abs(turn(c.yaw)))) + STRIP_CLEAR_DEG
  const low = Math.min(...corners.map(c => c.pitch)) - STRIP_CLEAR_DEG
  const top = Math.max(...corners.map(c => c.pitch)) + STRIP_CLEAR_DEG
  if (turn(box.yaw0) > halfYaw || turn(box.yaw1) < -halfYaw) return 0
  if (box.low > top || box.top < low) return 0
  return box.top - low
}

/** The air kept between the strip's right end and a close mark's press area, as the eye sees it (degrees). */
export const CLOSE_CLEAR_DEG = 0.3

/**
 * How much of each end the strip gives up (metres, each 0 to half its width) so that no close mark's press area lies
 * on it, as the eye at `head` sees both (D54). `strip` is the strip at its full width, facing the eye; `marks` are
 * the press areas (`homePlace.closeMarks`). A press area right of the strip's middle moves its right end left (the
 * front page's mark, at the right end of its top edge); one left of it moves its left end right (a page that stands
 * to the left).
 */
export function stripCuts(head: Vector3, strip: { center: { x: number; y: number; z: number }; widthM: number; heightM: number }, marks: Iterable<readonly [P3, P3]>, sides?: readonly (1 | -1)[]): StripCuts {
  const c = strip.center
  const flat = Math.hypot(c.x - head.x, c.z - head.z) || 1e-6
  const right = { x: -(c.z - head.z) / flat, z: (c.x - head.x) / flat }
  const at = lookAt(head, c)
  const turn = (v: number) => ((((v - at.yaw) % 360) + 540) % 360) - 180
  const point = (t: number, sy: number) => ({ x: c.x + right.x * t, y: c.y + (sy * strip.heightM) / 2, z: c.z + right.z * t })
  const ends = [point(strip.widthM / 2, 1), point(strip.widthM / 2, -1), point(-strip.widthM / 2, 1), point(-strip.widthM / 2, -1)].map(p => lookAt(head, p))
  const top = Math.max(...ends.map(e => e.pitch))
  const low = Math.min(...ends.map(e => e.pitch))
  const rightYaw = Math.max(...ends.map(e => turn(e.yaw)))
  const leftYaw = Math.min(...ends.map(e => turn(e.yaw)))
  let cut = 0
  let leftCut = 0
  let rightInner = Infinity
  let rightOuter = Infinity
  let leftInner = -Infinity
  let leftOuter = -Infinity
  const along = (deg: number) => flat * Math.tan((deg * Math.PI) / 180)
  let i = -1
  for (const [a, b] of marks) {
    i += 1
    const seen = [a, b, [a[0], b[1], a[2]] as const, [b[0], a[1], b[2]] as const].map(p => lookAt(head, { x: p[0], y: p[1], z: p[2] }))
    const yaw0 = Math.min(...seen.map(s => turn(s.yaw)))
    const yaw1 = Math.max(...seen.map(s => turn(s.yaw)))
    const markLow = Math.min(...seen.map(s => s.pitch))
    const markTop = Math.max(...seen.map(s => s.pitch))
    if (markTop <= low || markLow >= top) continue
    // A mark whose corners lie on both sides of straight behind the strip's middle (the yaw wraps at 180 degrees)
    // stands behind him, nowhere near the strip (small 7).
    if (yaw1 - yaw0 > 180) continue
    // The side the strip gives way on: the one held for this mark (`markSides`), else the side of its middle.
    if ((sides?.[i] ?? ((yaw0 + yaw1) / 2 > 0 ? 1 : -1)) > 0) {
      if (yaw0 - CLOSE_CLEAR_DEG >= rightYaw) continue
      cut = Math.max(cut, strip.widthM / 2 - along(yaw0 - CLOSE_CLEAR_DEG))
      rightInner = Math.min(rightInner, along(yaw0 - CLOSE_CLEAR_DEG))
      rightOuter = rightOuter === Infinity ? along(Math.min(89, yaw1 + CLOSE_CLEAR_DEG)) : Math.max(rightOuter, along(Math.min(89, yaw1 + CLOSE_CLEAR_DEG)))
    } else {
      if (yaw1 + CLOSE_CLEAR_DEG <= leftYaw) continue
      leftCut = Math.max(leftCut, strip.widthM / 2 + along(yaw1 + CLOSE_CLEAR_DEG))
      leftInner = Math.max(leftInner, along(yaw1 + CLOSE_CLEAR_DEG))
      leftOuter = leftOuter === -Infinity ? along(Math.max(-89, yaw0 - CLOSE_CLEAR_DEG)) : Math.min(leftOuter, along(Math.max(-89, yaw0 - CLOSE_CLEAR_DEG)))
    }
  }
  const half = strip.widthM / 2
  return { right: Math.min(half, Math.max(0, cut)), left: Math.min(half, Math.max(0, leftCut)), rightInner, rightOuter, leftInner, leftOuter }
}

/**
 * What `stripCuts` found: what each end gives up (metres, 0 to half the width), and where the marks that cut each
 * end begin and end along the strip (metres from its middle, the clear air added, not capped; infinite with none on
 * that side). `rightInner` is the near side of the right marks, `rightOuter` their far side; the same on the left.
 */
export interface StripCuts { right: number; left: number; rightInner: number; rightOuter: number; leftInner: number; leftOuter: number }

/**
 * Where the strip stands, cut so that no mark lies on it (D54, small 8): its width and how far its middle moves
 * along its right (metres). The strip is the room between the marks when its switch fits there (`minM`). When it
 * does not, it is the switch alone, moved clear of the marks: just past the right marks' far side, just past the
 * left marks' far side, or along its one cut end with no mark on the other side; the place nearest his straight
 * ahead wins. Chosen over splitting the strip in two, which would put its counts and its switch apart.
 * shortcut: a place past one mark is not tested against a third mark beyond it; upgrade when three pages stand
 * close under the strip.
 */
export function stripFit(widthM: number, minM: number, c: StripCuts): { widthM: number; shiftM: number } {
  const a = Math.max(-widthM / 2, c.leftInner)
  const b = Math.min(widthM / 2, c.rightInner)
  if (b - a >= minM) return { widthM: b - a, shiftM: (a + b) / 2 }
  const right = Number.isFinite(c.rightInner)
  const left = Number.isFinite(c.leftInner)
  const at: number[] = []
  if (right) at.push(left ? c.rightOuter + minM / 2 : b - minM / 2)
  if (left) at.push(right ? c.leftOuter - minM / 2 : a + minM / 2)
  const shiftM = at.reduce((best, x) => (Math.abs(x) < Math.abs(best) ? x : best), at[0] ?? 0)
  return { widthM: minM, shiftM }
}

/**
 * The eyes the strip's cut is reckoned from: the start eye, and eight eyes 10 cm from it to each side and forward
 * and back (small 9). The cut is the largest any of them needs, so a lean of up to 10 cm never brings a mark onto the
 * strip, and the strip does not change size as he leans. Measured from the start eye alone, a mark of a page 6 m off
 * came 1.7 degrees onto the strip for a 10 cm lean. shortcut: an eye farther than 10 cm (he walks) is not counted;
 * upgrade when the strip follows him.
 */
export const LEAN_EYES: readonly (readonly [number, number])[] = [[0, 0], [0.1, 0], [-0.1, 0], [0, 0.1], [0, -0.1], [0.07, 0.07], [-0.07, 0.07], [0.07, -0.07], [-0.07, -0.07]]

/** How far past the strip's middle a mark must move before the strip gives way on its other side (degrees, D56 4). */
export const SIDE_SLACK_DEG = 1

/**
 * The side each mark makes the strip give way on (1 its right end, -1 its left), held across frames in `held` by the
 * mark's name: a mark keeps its side until its middle is `SIDE_SLACK_DEG` past the strip's middle on the other side,
 * so a mark held near the middle does not flip the strip from end to end (REVIEW-19 small 5). Marks gone are
 * forgotten. Seen from the start eye `head`.
 */
export function markSides(head: Vector3, strip: { center: { x: number; y: number; z: number } }, marks: readonly (readonly [string, readonly [P3, P3]])[], held: Map<string, 1 | -1>): (1 | -1)[] {
  const at = lookAt(head, strip.center)
  const turn = (v: number) => ((((v - at.yaw) % 360) + 540) % 360) - 180
  const out = marks.map(([id, [a, b]]) => {
    const mid = turn(lookAt(head, { x: (a[0] + b[0]) / 2, y: (a[1] + b[1]) / 2, z: (a[2] + b[2]) / 2 }).yaw)
    const was = held.get(id)
    const side: 1 | -1 = was !== undefined && mid * was > -SIDE_SLACK_DEG ? was : mid > 0 ? 1 : -1
    held.set(id, side)
    return side
  })
  const now = new Set(marks.map(([id]) => id))
  for (const id of [...held.keys()]) if (!now.has(id)) held.delete(id)
  return out
}

/** `stripCuts` from each eye of `LEAN_EYES` round `head`, joined: each end gives up the most that any eye needs. */
export function stripCutsLeaning(head: Vector3, strip: Parameters<typeof stripCuts>[1], marks: Iterable<readonly [P3, P3]>, sides?: readonly (1 | -1)[]): StripCuts {
  const list = [...marks]
  const eye = head.clone()
  const all = LEAN_EYES.map(([dx, dz]) => stripCuts(eye.set(head.x + dx, head.y, head.z + dz), strip, list, sides))
  // An eye that sees no mark on a side says so by an infinite edge: only the eyes that see one count for its far side.
  const most = (v: number[], none: number) => { const seen = v.filter(Number.isFinite); return seen.length ? (none > 0 ? Math.max(...seen) : Math.min(...seen)) : none }
  return {
    right: Math.max(...all.map(c => c.right)), left: Math.max(...all.map(c => c.left)),
    rightInner: Math.min(...all.map(c => c.rightInner)), rightOuter: most(all.map(c => c.rightOuter), Infinity),
    leftInner: Math.max(...all.map(c => c.leftInner)), leftOuter: most(all.map(c => c.leftOuter), -Infinity),
  }
}

/** What the strip gives up of its right end (`stripCuts`). */
export const stripCut = (...args: Parameters<typeof stripCuts>): number => stripCuts(...args).right
/**
 * The room's front: the way (degrees, to the right of the room's -z, as `placeAt` reads a yaw) that Home's strip
 * stands, straight ahead of the place the room is counted round. A board in his hand comes there when he faces it
 * (`Board.tsx`, D76 1, D79 3). One name, read by both, so a room whose front moves takes its boards with it.
 */
export const ROOM_FRONT_YAW_DEG = 0
