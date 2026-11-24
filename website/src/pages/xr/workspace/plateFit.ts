/**
 * Where the readout's plate may stand (`ReadoutPlate.tsx`), as pure rules: what the eye sees of a flat thing or of a
 * box, whether two such shapes lie over each other in the eye's picture, and whether a shape lies whole in it.
 *
 * Everything is in the eye's own flat picture: a point of the room, in the eye's space (it looks along minus z), is
 * the pair `[x / -z, y / -z]`. One shape is tested against one shape, with a handful of sums: no ray is cast and no
 * triangle is read.
 */
export type P3 = readonly [number, number, number]
export type P2 = readonly [number, number]

/** Nearer to the eye's plane than this, in metres, a point is not seen. */
const NEAR_M = 0.05

/**
 * What the eye sees of a flat shape with these corners (in the eye's space, in order around the shape): the part
 * of it that lies in front of the eye, as a shape in the eye's picture. Empty when all of it is behind the eye.
 */
export function seen(corners: readonly P3[]): P2[] {
  const front: P3[] = []
  corners.forEach((a, i) => {
    const b = corners[(i + 1) % corners.length]
    const aIn = a[2] <= -NEAR_M
    if (aIn) front.push(a)
    if (aIn !== (b[2] <= -NEAR_M)) {
      // The edge crosses the plane in front of the eye: the shape is cut there.
      const t = (-NEAR_M - a[2]) / (b[2] - a[2])
      front.push([a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, -NEAR_M])
    }
  })
  return front.map(p => [p[0] / -p[2], p[1] / -p[2]] as const)
}

/**
 * What the eye sees of a box with these corners: the upright rectangle around them in the eye's picture, which is
 * never smaller than the box. A corner beside or behind the eye counts as far out to its side. Empty when the whole
 * box is behind the eye.
 */
export function seenBox(corners: readonly P3[]): P2[] {
  if (corners.every(p => p[2] > -NEAR_M)) return []
  let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity
  for (const p of corners) {
    const z = Math.max(NEAR_M, -p[2])
    x0 = Math.min(x0, p[0] / z); x1 = Math.max(x1, p[0] / z)
    y0 = Math.min(y0, p[1] / z); y1 = Math.max(y1, p[1] / z)
  }
  return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]
}

/** Whether two shapes with no dent lie over each other: no line along an edge of either has them on two sides. */
export function overlaps(a: readonly P2[], b: readonly P2[]): boolean {
  if (a.length < 3 || b.length < 3) return false
  for (const shape of [a, b]) {
    for (let i = 0; i < shape.length; i += 1) {
      const p = shape[i]
      const q = shape[(i + 1) % shape.length]
      const nx = q[1] - p[1]
      const ny = p[0] - q[0]
      if (nx === 0 && ny === 0) continue
      let a0 = Infinity, a1 = -Infinity, b0 = Infinity, b1 = -Infinity
      for (const v of a) { const d = v[0] * nx + v[1] * ny; a0 = Math.min(a0, d); a1 = Math.max(a1, d) }
      for (const v of b) { const d = v[0] * nx + v[1] * ny; b0 = Math.min(b0, d); b1 = Math.max(b1, d) }
      if (a1 <= b0 || b1 <= a0) return false
    }
  }
  return true
}

/** The eye's picture: from where to where it reaches, across and up. */
export interface View { x0: number; x1: number; y0: number; y1: number }

/** The picture of one eye, from its projection matrix (sixteen numbers, column by column). */
export function viewOf(m: ArrayLike<number>): View {
  return { x0: (m[8] - 1) / m[0], x1: (m[8] + 1) / m[0], y0: (m[9] - 1) / m[5], y1: (m[9] + 1) / m[5] }
}

/** What every one of these pictures shows, drawn in by `inset` (a share of its width and of its height) at each side. */
export function sharedView(views: readonly View[], inset = 0): View {
  const v = views.reduce((a, b) => ({ x0: Math.max(a.x0, b.x0), x1: Math.min(a.x1, b.x1), y0: Math.max(a.y0, b.y0), y1: Math.min(a.y1, b.y1) }))
  const dx = (v.x1 - v.x0) * inset
  const dy = (v.y1 - v.y0) * inset
  return { x0: v.x0 + dx, x1: v.x1 - dx, y0: v.y0 + dy, y1: v.y1 - dy }
}

/** Whether a flat shape with these corners (in the eye's space) lies whole in the picture: every corner is in front of the eye and inside it. */
export function within(corners: readonly P3[], view: View): boolean {
  return corners.every(p => p[2] <= -NEAR_M && p[0] / -p[2] >= view.x0 && p[0] / -p[2] <= view.x1 && p[1] / -p[2] >= view.y0 && p[1] / -p[2] <= view.y1)
}

/** A picture drawn in by `by` (in the picture's own measure) at each side. */
export function drawnIn(view: View, by: number): View {
  return { x0: view.x0 + by, x1: view.x1 - by, y0: view.y0 + by, y1: view.y1 - by }
}

/** A shape made larger by `by` (in the picture's own measure): each corner is moved out from the shape's middle, across and up. */
export function grown(shape: readonly P2[], by: number): P2[] {
  const mx = shape.reduce((sum, p) => sum + p[0], 0) / shape.length
  const my = shape.reduce((sum, p) => sum + p[1], 0) / shape.length
  return shape.map(p => [p[0] + Math.sign(p[0] - mx) * by, p[1] + Math.sign(p[1] - my) * by] as const)
}

/**
 * The give in the plate's choice, so that a head at rest on a boundary does not move the plate twice a second: a
 * place that it does not stand at must be free and whole with `margin` to spare (one degree, in the picture's
 * measure), and it leaves where it stands only when `fails` choices running say so.
 */
export const GIVE = { margin: Math.tan(Math.PI / 180), fails: 2 }

/**
 * Where the plate stands after a choice that says `next`: there at once when it stood nowhere; where it stood, while
 * the choices that say otherwise are fewer than `GIVE.fails` running (`fails` counts them).
 */
export function settle<T>(stands: T | null, next: T, fails: number): { stands: T; fails: number } {
  if (stands === null || stands === next) return { stands: next, fails: 0 }
  return fails + 1 >= GIVE.fails ? { stands: next, fails: 0 } : { stands, fails: fails + 1 }
}

/** Whether a point of the eye's picture lies in a shape with no dent, its edge counted. */
export function inside(p: P2, shape: readonly P2[]): boolean {
  if (shape.length < 3) return false
  let side = 0
  for (let i = 0; i < shape.length; i += 1) {
    const a = shape[i]
    const b = shape[(i + 1) % shape.length]
    const turn = Math.sign((b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0]))
    if (turn === 0) continue
    if (side !== 0 && turn !== side) return false
    side = turn
  }
  return true
}

/**
 * Whether a flat shape with these corners hides a point from the eye (both in the eye's space), when the shape is
 * drawn over everything, as the plate is: the point is in front of the eye and, in the eye's picture, in the shape.
 * How far the point is does not count: a thing nearer than the plate is hidden by it as well.
 */
export function hides(corners: readonly P3[], p: P3): boolean {
  return p[2] <= -NEAR_M && inside([p[0] / -p[2], p[1] / -p[2]], seen(corners))
}

/**
 * How far from the eye the nearest point of a flat rectangle is, in metres: its corners in the eye's space, in
 * order around it. A page that faces the eye is nearest at its middle, not at a corner.
 */
export function nearestFlatM(corners: readonly P3[]): number {
  const [o, a, , b] = corners
  const along = (to: P3): [number, number, number, number] => {
    const d = [to[0] - o[0], to[1] - o[1], to[2] - o[2]] as const
    const len2 = d[0] * d[0] + d[1] * d[1] + d[2] * d[2]
    // How far along this side the eye stands, held to the side itself.
    const t = len2 === 0 ? 0 : Math.min(1, Math.max(0, -(o[0] * d[0] + o[1] * d[1] + o[2] * d[2]) / len2))
    return [d[0] * t, d[1] * t, d[2] * t, t]
  }
  const u = along(a)
  const v = along(b)
  return Math.hypot(o[0] + u[0] + v[0], o[1] + u[1] + v[1], o[2] + u[2] + v[2])
}

/**
 * How far from the eye the nearest point of a box is, in metres: its eight corners in the eye's space, the corner
 * `x * 4 + y * 2 + z` at the box's own min (0) or max (1) on each axis. A box that faces the eye is nearest on its
 * face, not at a corner (as a page is, `nearestFlatM`).
 */
export function nearestBoxM(corners: readonly P3[]): number {
  const o = corners[0]
  const p: [number, number, number] = [o[0], o[1], o[2]]
  for (const to of [corners[4], corners[2], corners[1]]) {
    const d = [to[0] - o[0], to[1] - o[1], to[2] - o[2]] as const
    const len2 = d[0] * d[0] + d[1] * d[1] + d[2] * d[2]
    const t = len2 === 0 ? 0 : Math.min(1, Math.max(0, -(o[0] * d[0] + o[1] * d[1] + o[2] * d[2]) / len2))
    p[0] += d[0] * t; p[1] += d[1] * t; p[2] += d[2] * t
  }
  return Math.hypot(p[0], p[1], p[2])
}

/** A part of a thing that lies behind a place: the thing it belongs to, and how far from the eye its nearest point is. */
export interface Behind { thing: unknown; awayM: number }

/** What a place would cover: whether a marked thing nearer than the plate is among it, and how many farther things. */
export interface Cover { near: boolean; far: number }

/** The cover that every other is less than. */
export const NO_PLACE: Cover = { near: true, far: Infinity }

/**
 * What a plate that stands `plateM` from the eye would cover of these parts. Things are counted, not their
 * parts: a card of one part and a palette of nine are one thing each. A thing with any part nearer than the plate
 * is a near thing: the plate, drawn over everything, would hide it whole while it stands within reach. A page
 * that was brought nearer than the plate is such a thing as well.
 */
export function coverOf(behind: readonly Behind[], plateM: number): Cover {
  const nearest = new Map<unknown, number>()
  for (const part of behind) nearest.set(part.thing, Math.min(nearest.get(part.thing) ?? Infinity, part.awayM))
  let far = 0
  let near = false
  for (const awayM of nearest.values()) { if (awayM < plateM) near = true; else far += 1 }
  return { near, far }
}

/**
 * Whether cover `a` is less than `b`: a place over no near thing before any place over one (a near thing is never
 * covered while a corner without one is there); then the fewer far things.
 */
export function coversLess(a: Cover, b: Cover): boolean {
  return a.near !== b.near ? !a.near : a.far < b.far
}

/**
 * Whether cover `a` is less than `b` by the give of a corner where the plate takes presses (D60): over no near thing
 * where `b` is over one, or over at least `GIVE_COVER` fewer far things. A corner where the plate stands is left only
 * for one that covers clearly less, as a free place is left only for one free with room to spare (`GIVE`).
 */
export const GIVE_COVER = 3
export function coversClearlyLess(a: Cover, b: Cover): boolean {
  return a.near !== b.near ? !a.near : a.far + GIVE_COVER <= b.far
}
