/**
 * The numbers and the plain maths of the workspace of pages: how big a page is in the room, where panels stand,
 * how a held panel turns to the person and comes nearer, and how a dragged corner changes a page's size.
 *
 * Nothing here draws. Lengths are metres, angles are radians unless a name says degrees, and a page's own size is
 * in its CSS pixels.
 */
import { Matrix4, Quaternion, Vector3 } from 'three'
import { TUNE } from '../tune'

export const PAGES = {
  /** Android XR opens a window this far from the person. A panel opens there too. */
  distanceM: 1.75,
  /** Android XR: one dp is seen as 0.868 mm from 1 m away. Text of 14 dp or more is what it asks for. */
  mmPerDpAt1M: 0.868,
  minTextDp: 14,
  /**
   * The size a page opens at, in CSS pixels: the size Android XR opens its own windows at. It is 48 degrees wide at
   * the distance above, inside the middle 50 degrees of the view. The dashboard's chat sets its body text at 14 px.
   */
  cssWidth: 1024,
  cssHeight: 720,
  /** A picture has this many pixels for each pixel of the display that its page covers: enough for a mesh's second filter, and no more. */
  texelsPerEyePx: 1.4,
  /** How far the page's material leans toward the larger of two small copies of its picture. */
  mipBias: -0.75,
  /** A panel that left the view is told so after this long, so a head that sweeps past does not stop and start its page. */
  hiddenAfterMs: 400,
  /**
   * The smallest and the biggest a corner can make a page. The smallest is as narrow as a phone's page, which is
   * also the least that the gateway lays a page out at; the biggest is Android XR's own limit for a window, 2560 dp wide.
   */
  minCssWidth: 320,
  minCssHeight: 400,
  maxCssWidth: 2560,
  maxCssHeight: 1600,
  /** How wide the edge and the corner a hand aims at are, as an angle at the eye: no smaller than a hand can hold a ray on. */
  edgeDeg: 1.7,
  cornerDeg: 4,
  /** A hand pushed this far forward doubles the distance of the panel it holds. Pulled back as far, it halves it. */
  depthDoubleM: 0.2,
  nearM: 0.5,
  farM: 6,
  /** A press on a page that has moved this many CSS pixels is a scroll, and no longer a click. */
  scrollStartPx: 24,
  /** A pinch counts as open only after it has stayed open this long: the same measured rule as on the land. */
  pinchOpenMs: TUNE.pinchOpenMs,
  /** Eye height of the desktop preview. */
  eyeM: 1.6,
  /**
   * The pitch a panel opens at (degrees, up is +): 1.4 under level, so that Home's strip has room over a first page's
   * top edge (D74). The glasses show 20.35 degrees up and down; a first page is 34.7 high, so its lower edge stands
   * 18.75 down, 1.6 inside.
   */
  lowerDeg: -1.4,
} as const

/**
 * How many metres one CSS pixel of a page is on its panel. At the distance a panel opens at, one CSS pixel is one dp:
 * the page's 14 px body text is then exactly the 14 dp that Android XR asks for. A panel keeps this size when it is
 * moved: pulled near, its text is bigger to the eye; pushed away, smaller.
 */
export const M_PER_PX = (PAGES.mmPerDpAt1M / 1000) * PAGES.distanceM

/** The smallest text, in CSS pixels, that meets the 14 dp rule on a panel `distanceM` away. */
export const minTextPx = (distanceM: number, mPerPx = M_PER_PX): number => ((PAGES.mmPerDpAt1M / 1000) * PAGES.minTextDp * distanceM) / mPerPx

const UP = new Vector3(0, 1, 0)
const M = new Matrix4()

/** Which way a panel at `at` turns to face a head at `head`: its front to the head, level, with no roll. */
export function facing(at: Vector3, head: Vector3, out = new Quaternion()): Quaternion {
  // `lookAt` aims -z from the eye to the target; a panel shows its +z side, so it looks from the head to itself.
  return out.setFromRotationMatrix(M.lookAt(head, at, UP))
}

/** The picture sizes a live page can be asked for, in picture pixels for each CSS pixel. */
export const DETAIL_STEPS: readonly number[] = [0.5, 0.75, 1, 1.4, 2]

/**
 * The step to ask for when the page is worth `want` picture pixels for each CSS pixel, and has `current` now: the
 * smallest step that is enough. A new size makes a new picture and a new texture, so a panel that sits right at a
 * step's edge keeps the bigger step it has until it is clearly inside the smaller one.
 */
export function detailStep(want: number, current: number): number {
  const top = DETAIL_STEPS[DETAIL_STEPS.length - 1]
  if (!(want > 0)) return current
  const i = DETAIL_STEPS.findIndex(step => want <= step)
  if (i < 0) return top
  const enough = DETAIL_STEPS[i]
  if (enough >= current || want <= enough * 0.93) return enough
  return Math.min(current, DETAIL_STEPS[i + 1] ?? top)
}

/**
 * How many pixels of the display one CSS pixel of a page covers, for a page `distanceM` away that faces the eye.
 * `viewHeightPx` is the height of one eye's picture and `focal` its lens: 1 / tan(half the angle it shows, up and down).
 */
export const eyePxPerCssPx = (distanceM: number, viewHeightPx: number, focal: number, mPerPx = M_PER_PX): number => ((viewHeightPx / 2) * focal * mPerPx) / Math.max(0.05, distanceM)

/** How many times farther a held panel is when the hand has moved `deltaM` away from the head since it took hold. */
export const depthScale = (deltaM: number): number => 2 ** (deltaM / PAGES.depthDoubleM)

/**
 * The two rows that pages stand in round the head. Six fit in a level ring, a hand's width apart, from 1.8 m. The
 * next six stand in a second ring over the first, each over a page of the level ring: a panel that leans to face
 * the head has its far corners nearer the middle, where there is less room, so the upper ring is a little farther
 * off. No row stands under the level one: the place under the first page is the crew's (`crewRow.ts`), and with
 * the drawn room on, the desk's.
 */
export const RINGS = { level: { pitchDeg: PAGES.lowerDeg, distanceM: 1.8 }, upper: { pitchDeg: 34, distanceM: 2.2 } } as const

/**
 * Where `count` panels of the size a page opens at stand round the head: `yawDeg` to the right of straight ahead,
 * `pitchDeg` up, `distanceM` away. Flat panels that each face the head must not cross.
 * shortcut: more than twelve cross each other; lay them out by their real sizes when a person can open that many.
 */
export function ring(count: number): { yawDeg: number; pitchDeg: number; distanceM: number }[] {
  const step = 60
  if (count <= 3) return [0, -step, step].slice(0, count).sort((a, b) => a - b).map(yawDeg => ({ yawDeg, pitchDeg: PAGES.lowerDeg, distanceM: PAGES.distanceM }))
  const out: { yawDeg: number; pitchDeg: number; distanceM: number }[] = []
  for (let i = 0; i < count; i += 1) out.push({ yawDeg: ((i % 6) * step + 180) % 360 - 180, ...(Math.floor(i / 6) % 2 ? RINGS.upper : RINGS.level) })
  return out
}

/** A place round the head, as a point `distanceM` away. Straight ahead is -z. */
export function placeAt(head: Vector3, yawDeg: number, pitchDeg: number, distanceM: number, out = new Vector3()): Vector3 {
  const yaw = (yawDeg * Math.PI) / 180
  const pitch = (pitchDeg * Math.PI) / 180
  return out.set(Math.sin(yaw) * Math.cos(pitch), Math.sin(pitch), -Math.cos(yaw) * Math.cos(pitch)).multiplyScalar(distanceM).add(head)
}

/** A page's rectangle on its panel: its middle and its size, in the panel's own plane, in metres. */
export interface Rect { cx: number; cy: number; w: number; h: number }

/**
 * A corner of `rect` is dragged to `x`, `y` on the panel's plane. `sx` and `sy` say which corner (+1 right or top,
 * -1 left or bottom). The opposite corner stays where it is, and the size stays inside `min` and `max`.
 */
export function dragCorner(rect: Rect, sx: 1 | -1, sy: 1 | -1, x: number, y: number, min: readonly [number, number], max: readonly [number, number]): Rect {
  const ax = rect.cx - (sx * rect.w) / 2
  const ay = rect.cy - (sy * rect.h) / 2
  const w = Math.min(max[0], Math.max(min[0], (x - ax) * sx))
  const h = Math.min(max[1], Math.max(min[1], (y - ay) * sy))
  return { cx: ax + (sx * w) / 2, cy: ay + (sy * h) / 2, w, h }
}

/** The same rectangle with another size, with the corner opposite `sx`, `sy` kept where it is. */
export function sizeFrom(rect: Rect, sx: 1 | -1, sy: 1 | -1, w: number, h: number): Rect {
  return { cx: rect.cx - (sx * rect.w) / 2 + (sx * w) / 2, cy: rect.cy - (sy * rect.h) / 2 + (sy * h) / 2, w, h }
}

/* ---------------- where a new page stands ---------------- */

/** A place round the head: so far to the right of straight ahead, so far up, so far away. */
export interface Slot { yawDeg: number; pitchDeg: number; distanceM: number }

/** A page in the room, as the placing reads it: where it is and how big. */
export interface Sized { at: Slot; widthM: number; heightM: number }

/** A page that stands in the room, and whether the person put it there by hand. */
export interface Standing extends Sized { id: string; user: boolean }

const DEG = 180 / Math.PI

/** The place of a point in the room, as the head sees it. */
export function slotOf(head: Vector3, at: Vector3): Slot {
  const dx = at.x - head.x, dy = at.y - head.y, dz = at.z - head.z
  const flat = Math.hypot(dx, dz)
  return { yawDeg: Math.atan2(dx, -dz) * DEG, pitchDeg: Math.atan2(dy, flat) * DEG, distanceM: Math.hypot(flat, dy) }
}

/**
 * How much of the view a flat page takes that faces the head: half its width and half its height, as angles. A page
 * that leans to face a head below or above it has its far edge nearer the head's upright line, where a width is a
 * wider angle: the half width is taken there, at the edge that is farthest from level.
 */
function reach(p: Sized): { yaw: number; pitch: number } {
  const d = Math.max(0.05, p.at.distanceM)
  const pitch = Math.atan2(p.heightM / 2, d) * DEG
  const far = Math.min(89, Math.abs(p.at.pitchDeg) + pitch)
  return { yaw: Math.atan2(p.widthM / 2, d * Math.cos(far / DEG)) * DEG, pitch }
}

/** Pages keep at least this much of the view between them. */
export const PAGE_GAP_DEG = 2

/**
 * By how much two pages cross as the head sees them, in degrees: 0 when they are clear of each other with the gap
 * between them. Two flat pages that touch anywhere in the room share a direction from the head, so pages that are
 * clear here are clear in the room; a page that would only stand behind another counts as crossing it too.
 */
export function crossing(a: Sized, b: Sized, gapDeg = PAGE_GAP_DEG): number {
  const ra = reach(a), rb = reach(b)
  const apart = Math.abs(((a.at.yawDeg - b.at.yawDeg + 540) % 360) - 180)
  const yaw = ra.yaw + rb.yaw + gapDeg - apart
  const pitch = ra.pitch + rb.pitch + gapDeg - Math.abs(a.at.pitchDeg - b.at.pitchDeg)
  return yaw > 0 && pitch > 0 ? Math.min(yaw, pitch) : 0
}

/**
 * A part of the view that is not a page's to stand in, as the head at the room's middle sees it: from so far to the
 * right of straight ahead to so far, and from so far up to so far.
 */
export interface Zone { yawMinDeg: number; yawMaxDeg: number; pitchMinDeg: number; pitchMaxDeg: number }

/** Whether any part of a page is in a zone, as the head sees it. */
export function inZone(p: Sized, zone: Zone): boolean {
  const r = reach(p)
  const middle = (zone.yawMinDeg + zone.yawMaxDeg) / 2
  const apart = Math.abs(((p.at.yawDeg - middle + 540) % 360) - 180)
  return apart < r.yaw + (zone.yawMaxDeg - zone.yawMinDeg) / 2 && p.at.pitchDeg - r.pitch < zone.pitchMaxDeg && p.at.pitchDeg + r.pitch > zone.pitchMinDeg
}

/** The directions of a ring, nearest to straight ahead first: ahead, right, left, and so round to the back. */
const ROUND = [0, 60, -60, 120, -120, 180] as const
const row = (pitchDeg: number, distanceM: number, yaws: readonly number[] = ROUND): Slot[] => yaws.map(yawDeg => ({ yawDeg, pitchDeg, distanceM }))
/** One level ring of six, where a page is one glance wide and the next is one turn of the head. */
const LEVEL = row(RINGS.level.pitchDeg, RINGS.level.distanceM)
/** The second ring of six, over the first. */
const UPPER = row(RINGS.upper.pitchDeg, RINGS.upper.distanceM)
/**
 * Where a new page looks for room, in order: the level places first (where the first three open, then the level
 * ring), and only when none of them is free the ring over it.
 */
const TIERS: readonly (readonly Slot[])[] = [[...row(PAGES.lowerDeg, PAGES.distanceM, [0, 60, -60]), ...LEVEL], UPPER]
/**
 * When pages that a hand placed are in the way of those places: any direction on the level ring, on the ring over
 * it, or on a low row, looked for in steps of `STEP_DEG`. The low row is as far under level as the upper ring is
 * over it; a page there is clear of the level ring, and it is stood only outside what is kept out, so at the sides
 * and behind, where no Bot stands.
 * shortcut: with the drawn room on, the lowest 6 cm of a page of the low row are under the floor; raise the row,
 * or the floor's limit, when a person fills the room so far that the low row is used.
 */
// The low row stands as far under the level ring as the upper one over it: it goes down with the level ring (D74).
const ROWS: readonly { pitchDeg: number; distanceM: number }[] = [RINGS.level, RINGS.upper, { pitchDeg: -RINGS.upper.pitchDeg + 1 + PAGES.lowerDeg, distanceM: RINGS.upper.distanceM }]
const STEP_DEG = 5

export interface NewPlace {
  /** Where the new page stands. */
  at: Slot
  /** Pages that made room. Never one that the person placed by hand. */
  moved: { id: string; at: Slot }[]
  /** True when no place was clear of every other page: the new page then stands where it crosses the least. */
  crosses: boolean
}

const same = (a: Slot, b: Slot) => a.yawDeg === b.yawDeg && a.pitchDeg === b.pitchDeg && a.distanceM === b.distanceM

/**
 * Where a new page of this size stands among the pages that are there.
 *
 * It takes the first place of the level ring where it crosses no other page, or, when that ring is full, of the
 * ring over it, and then nothing else moves: the seventh page stands over the first, and the six that were there
 * stay. When none of those places is free (a page that the person made big, or put between two places; a room
 * that an older build stood in other rows), the pages that the person has not placed by hand are stood again with
 * it, in the two rings; a page that the person placed stays where it is, and the others keep clear of it. When
 * that leaves no room either, it takes the nearest free direction on one of the rows, and nothing moves. Only
 * when no place at all is clear does the new page stand where it crosses the least, and says so.
 *
 * `lookYawDeg` is where the person looks, to the right of straight ahead: of the free places of a ring, the new
 * page takes the one nearest to that direction, so it opens in front of them. Looking straight ahead, the order is
 * the list's own.
 *
 * `keepOut` names the parts of the view where no page is stood: the crew's row and the desk (`crewRow.ts`). No
 * place that reaches into one is taken, by a newcomer or by a page that is stood again.
 */
export function placeNew(standing: readonly Standing[], widthM: number, heightM: number, lookYawDeg = 0, keepOut: readonly Zone[] = []): NewPlace {
  const allowed = (p: Sized) => !keepOut.some(zone => inZone(p, zone))
  const clear = (p: Sized, others: readonly Sized[]) => allowed(p) && others.every(o => crossing(p, o) === 0)
  const off = (yawDeg: number) => Math.abs(((yawDeg - lookYawDeg + 540) % 360) - 180)
  for (const tier of TIERS) {
    // The sort keeps the list's own order among places that are as far from where the person looks.
    const free = (lookYawDeg ? [...tier].sort((a, b) => off(a.yawDeg) - off(b.yawDeg)) : tier).find(at => clear({ at, widthM, heightM }, standing))
    if (free) return { at: free, moved: [], crosses: false }
  }
  // Nothing is free as things stand. The pages that the page itself stood somewhere stand again, round the ones
  // that the person placed.
  const loose = standing.filter(p => !p.user)
  const formation = [...LEVEL, ...UPPER]
  const taken: Sized[] = standing.filter(p => p.user)
  const moved: { id: string; at: Slot }[] = []
  let room = true
  for (const p of loose) {
    const at = formation.find(s => clear({ at: s, widthM: p.widthM, heightM: p.heightM }, taken))
    if (!at) { room = false; break }
    taken.push({ at, widthM: p.widthM, heightM: p.heightM })
    if (!same(at, p.at)) moved.push({ id: p.id, at })
  }
  const at = room ? formation.find(s => clear({ at: s, widthM, heightM }, taken)) : undefined
  if (at) return { at, moved, crosses: false }

  // Pages that a hand placed are in the way of the rings: any direction on a row, the nearest to where the person
  // looks first, to the right before to the left. Nothing moves.
  const look = Math.round(lookYawDeg / STEP_DEG) * STEP_DEG
  for (const ring of ROWS) {
    for (let turn = 0; turn <= 180; turn += STEP_DEG) {
      for (const side of turn === 0 || turn === 180 ? [1] : [1, -1]) {
        const at = { yawDeg: ((look + side * turn + 540) % 360) - 180, ...ring }
        if (clear({ at, widthM, heightM }, standing)) return { at, moved: [], crosses: false }
      }
    }
  }

  // No clear place at all: nothing moves, and the new page stands where it crosses the others the least.
  let best = TIERS[0][0], least = Infinity
  for (const s of TIERS.flat()) {
    if (!allowed({ at: s, widthM, heightM })) continue
    const sum = standing.reduce((n, o) => n + crossing({ at: s, widthM, heightM }, o), 0)
    if (sum < least) { least = sum; best = s }
  }
  return { at: best, moved: [], crosses: true }
}
