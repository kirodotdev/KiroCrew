/**
 * Where a desk line stands and how large it is (D39, ruling 4, and the lead's 17:46). It stands right over its own
 * Bot's head, is never wider to his eye than the line of a near desk, and never reaches into Home's band (the since
 * line and the strip): it grows smaller toward its lower edge to stay out. It never grows smaller than a floor where
 * its words are as high to his eye as the strip's small words ("working", "need you"). When it cannot stand over the
 * head at that size, it stands BESIDE its Bot at head height, on the side away from Home's band, and never on another
 * desk line. No desk moves. Everything is read from his head, as yaw (to the right) and pitch (up).
 */

import { i18nT } from '../../../i18n/t'

type V3 = readonly [number, number, number]

/**
 * A desk line's words (D42): whose it is, the name first as on the sheet's head, then what it says ("Mira · done ·
 * 8:07 PM"). A desk line far off has no sign beside it, so without the name a Bot behind another reads as its owner.
 */
export const deskWords = (name: string, line: string): string => (name ? i18nT('xr.desk_named', { name, line }) : line)

/** A box that faces him: its middle in the room and its width and height in metres. */
export interface FacingBox { center: V3; size: readonly [number, number] }

/** A desk line's place as he sees it: yaw from left to right and pitch from bottom to top, in radians. */
export interface SeenBox { y0: number; y1: number; p0: number; p1: number }

export const DESK_FIT = {
  /** The gap kept under or beside Home's band, and between two desk lines, in degrees. */
  gapDeg: 0.8,
  /** Beside its Bot: how far from the Bot's middle its near edge stands, and how far under the head's top its middle. */
  besideM: 0.24,
  headDropM: 0.14,
  /** Beside its Bot its words stand this much over the floor. */
  besideOverFloor: 1.1,
  /**
   * The farthest a line's near edge stands from its own Bot's middle (the lead's 19:45): a desk line carries no name,
   * so a line farther off cannot be told as that Bot's. Past it, the line stands under its Bot, at its desk's height.
   */
  reachM: 0.4,
  /** Under its Bot with no desk known: its middle this high over the Bot's feet. */
  underM: 0.45,
} as const

const DEG = Math.PI / 180

/** Yaw and pitch of a point seen from the head, and its flat distance. */
export function seen(head: V3, p: V3): { yaw: number; pitch: number; flat: number } {
  const dx = p[0] - head[0]
  const dz = p[2] - head[2]
  const flat = Math.hypot(dx, dz)
  return { yaw: Math.atan2(dx, -dz), pitch: Math.atan2(p[1] - head[1], flat), flat }
}

/** A facing box's span seen from the head. */
export function span(head: V3, b: FacingBox): SeenBox {
  const c = seen(head, b.center)
  const half = Math.atan2(b.size[0] / 2, c.flat)
  const p0 = Math.atan2(b.center[1] - b.size[1] / 2 - head[1], c.flat)
  const p1 = Math.atan2(b.center[1] + b.size[1] / 2 - head[1], c.flat)
  return { y0: c.yaw - half, y1: c.yaw + half, p0, p1 }
}

/**
 * The desk line's place: `share` of its size, and `side` (0 over the head; -1 left of the Bot, 1 right of it, at head
 * height). `low` is its lower edge's middle over the head, `widthM`/`heightM` its size at share 1, `letterM` how high
 * its words are at share 1; `nearRad` how wide a near desk's line is to his eye, `floorRad` how high the strip's
 * small words are to his eye; `bands` Home's band.
 */
export function deskPlace(head: V3, low: V3, size: { widthM: number; heightM: number; letterM: number }, nearRad: number, floorRad: number, bands: readonly FacingBox[]): { share: number; side: -1 | 0 | 1 } {
  const at = seen(head, low)
  const { widthM, heightM, letterM } = size
  if (at.flat < 1e-6 || widthM <= 0 || heightM <= 0) return { share: 1, side: 0 }
  // Never wider to his eye than a near desk's line; never smaller than the floor.
  const cap = Math.min(1, (2 * at.flat * Math.tan(nearRad / 2)) / widthM)
  const floor = letterM > 0 ? Math.min(cap, (floorRad * at.flat) / letterM) : 0
  const gap = DESK_FIT.gapDeg * DEG
  let share = cap
  let blocked: SeenBox | null = null
  for (const band of bands) {
    const b = span(head, band)
    const half = Math.atan2((widthM * share) / 2, at.flat)
    if (at.yaw + half <= b.y0 || at.yaw - half >= b.y1 || at.pitch >= b.p1) continue
    // Smaller, it clears the band one of two ways, and the larger share is kept: its top stays a little under the
    // band's lower edge (its lower edge where it is), or its side stays a little off the band's nearer end.
    const under = (Math.tan(b.p0 - gap) * at.flat - (low[1] - head[1])) / heightM
    const off = at.yaw < b.y0 ? b.y0 - gap - at.yaw : at.yaw > b.y1 ? at.yaw - b.y1 - gap : -1
    const aside = off > 0 ? (2 * at.flat * Math.tan(off)) / widthM : 0
    const fits = Math.max(under, aside)
    if (fits < share) { share = fits; blocked = b }
  }
  if (share >= floor) return { share: Math.min(1, share), side: 0 }
  // Too small over the head: beside its Bot, on the side away from the band, a little over the floor's size, so it
  // keeps close to its Bot and inside the view.
  const mid = blocked ? (blocked.y0 + blocked.y1) / 2 : 0
  return { share: Math.min(1, cap, floor * DESK_FIT.besideOverFloor), side: at.yaw < mid ? -1 : 1 }
}

/**
 * Where a line beside its Bot ends up (the lead's 19:45): beside it while its near edge stays within `reachM` of the
 * Bot's middle once it is clear of the other lines and the band (`besideM` and the shift that `besideClear` asks, in
 * metres), else under its Bot.
 */
export const besideOrUnder = (shiftM: number): 'beside' | 'under' => (DESK_FIT.besideM + Math.max(0, shiftM) <= DESK_FIT.reachM + 1e-9 ? 'beside' : 'under')

/**
 * A line's middle under its Bot: where its board stands over the head (`low`, its lower edge's middle, x and z), at
 * the height of the Bot's desk (`deskY`), or `underM` over its feet when no desk is known.
 */
export const underAt = (low: V3, bot: { feet: V3; deskY?: number } | undefined): [number, number, number] => [low[0], bot?.deskY ?? (bot ? bot.feet[1] + DESK_FIT.underM : low[1] - 1), low[2]]

/**
 * How much farther out (radians of yaw, toward `side`) a line beside its Bot must stand so that it lies on no other
 * desk line and not on Home's band, with the gap between. `others` are the other desk lines and the band's parts as
 * he sees them.
 */
export function besideClear(box: SeenBox, side: -1 | 1, others: readonly SeenBox[]): number {
  const gap = DESK_FIT.gapDeg * DEG
  let shift = 0
  for (let pass = 0; pass < others.length + 1; pass += 1) {
    const y0 = box.y0 + shift * side
    const y1 = box.y1 + shift * side
    const hit = others.find(o => y1 > o.y0 - gap && y0 < o.y1 + gap && box.p1 > o.p0 - gap && box.p0 < o.p1 + gap)
    if (!hit) return shift
    shift += side > 0 ? hit.y1 + gap - y0 : y1 - (hit.y0 - gap)
  }
  return shift
}
