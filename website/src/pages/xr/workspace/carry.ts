/**
 * How far the hand carries a page it holds by its edge, and how much a corner drag sizes it (D65, P15).
 *
 * Today's held dot turns half a radian for a metre of the hand (`pinchRoute.heldTurn`): six centimetres of hand move a
 * page four. A page held by its edge or its corner goes farther, by the hand's travel since the down (its distance,
 * not its speed, so a slow hand and a fast one put the page in the same place):
 *   - MOVE: under `stillM` the page does not go (a resting hand trembles a few millimetres, and a page held by its
 *     edge turns to face him once it moves). From there to `exactM` it goes one to one (a small set-right stays
 *     exact). Past it the gain grows in a straight line with the travel, so that `fullM` of hand carries the page its
 *     own width round him.
 *   - SIZE: the first `exactM` along the corner's diagonal move the corner one to one. Past it the page's size grows
 *     by the same factor for each centimetre: `fullM` out doubles it, `fullM` in halves it.
 * Inside the page the dot keeps `heldTurn`: a pinch-drag on the page's own content scrolls as it did.
 */
import type { V3 } from './pinchRoute'

export const CARRY = { stillM: 0.006, exactM: 0.02, fullM: 0.15 } as const

/**
 * The farthest a page held by its frame is carried from where he looked at the down (X4): 100 degrees round him,
 * so never behind him, and 60 degrees up or down, so never overhead.
 */
export const REACH = { yawRad: (100 * Math.PI) / 180, pitchRad: (60 * Math.PI) / 180 } as const

/**
 * HIS FRAME (D68): the hand's travel as he looks, the frame of his head at the down by its yaw only. `x` to the right
 * of where he looks, `y` the room's up, `z` back toward him. `yaw`: his head's turn at the down, radians, to his left
 * positive (as the room turns a thing about its up).
 */
export function inHisFrame(t: V3, yaw: number): [number, number, number] {
  const c = Math.cos(yaw)
  const s = Math.sin(yaw)
  // Where he looks is (-sin, 0, -cos); his right (cos, 0, -sin); back toward him (sin, 0, cos).
  return [t[0] * c - t[2] * s, t[1], t[0] * s + t[2] * c]
}

/**
 * The one pinch the dot holds: his head's yaw at its down (written by the dot, `HeadAim.tsx`), and, while it holds a
 * corner by the dot, where that corner stands in the room now (written by the panel), so that the dot rides it (X6).
 * One pinch owns the button, so one hold at a time. `ride`: the dot aims at that corner again at once (the panel calls
 * it after it writes the corner, in the same frame; `HeadAim.tsx` sets it).
 */
export const dotHold: { yaw: number; corner: V3 | null; ride: () => void } = { yaw: 0, corner: null, ride: () => {} }

/**
 * How far the page goes, in metres along the hand's way, for `handM` of hand (`handM` >= 0): nothing under `stillM`,
 * one to one from there to `exactM`, then a gain that grows with the travel until `fullM` of hand has carried it
 * `fullPageM`. Past `fullM` the gain stays as it was there.
 */
export function carryM(handM: number, fullPageM: number): number {
  const d = Math.max(0, handM)
  const { stillM, exactM, fullM } = CARRY
  if (d <= stillM) return 0
  if (d <= exactM) return d - stillM
  const span = fullM - exactM
  // P(d) = (d - still) + a (d - exact)^2, so the gain is 1 at `exactM`, and P(full) = fullPageM.
  const a = Math.max(0, (fullPageM - (fullM - stillM)) / (span * span))
  if (d <= fullM) return d - stillM + a * (d - exactM) ** 2
  const gainAtFull = 1 + 2 * a * span
  return fullPageM + gainAtFull * (d - fullM)
}

/**
 * The held dot's turn, `[yaw, pitch]` in radians, for a page held by its frame: the hand's travel across in his frame
 * (`inHisFrame`: `x` right, `y` up; `z`, toward him, is near and far's) carried as `carryM` says, on a page `holdM`
 * away whose width is `widthM`. To the right turns the ray right, down turns it down. It stops at `REACH`.
 */
export function carryTurn(travel: V3, holdM: number, widthM: number): [number, number] {
  const across = Math.hypot(travel[0], travel[1])
  if (across === 0 || holdM <= 0) return [0, 0]
  const turn = carryM(across, widthM) / holdM
  const clamp = (v: number, most: number) => Math.max(-most, Math.min(most, v))
  return [clamp((-travel[0] / across) * turn, REACH.yawRad), clamp((travel[1] / across) * turn, REACH.pitchRad)]
}

/** What a drag of a page held by its edge is: a move across, near and far, or not yet either (`null`). */
export type DragKind = 'across' | 'depth' | null

/**
 * NEAR AND FAR (D67): the hand's travel toward him or away brings the page nearer or farther. It starts only past
 * `startM` of that travel, and only while it is more than `over` times the travel across; `fullM` of it halves or
 * doubles the distance.
 */
export const DEPTH = { startM: 0.04, fullM: 0.15, over: 2 } as const

/**
 * What a drag is, for the hand's travel since the down in his frame (`inHisFrame`): away from him (`-z`) and across
 * (`x`, `y`). Once a drag is a move across it stays one, and once it is near and far it stays that (`was`): a drag that
 * has become a move changes no distance, one that has become near and far moves nothing across. `factor`: what the
 * distance is multiplied by (1 but for near and far).
 */
export function dragOf(travel: V3, was: DragKind): { kind: DragKind; factor: number; acrossM: number; awayM: number } {
  const awayM = -travel[2]
  const acrossM = Math.hypot(travel[0], travel[1])
  const along = Math.abs(awayM)
  const kind: DragKind = was
    ?? (acrossM >= CARRY.stillM && along <= DEPTH.over * acrossM ? 'across'
      : along >= DEPTH.startM && along > DEPTH.over * acrossM ? 'depth' : null)
  const factor = kind === 'depth' ? 2 ** ((Math.sign(awayM) * Math.max(0, along - DEPTH.startM)) / (DEPTH.fullM - DEPTH.startM)) : 1
  return { kind, factor, acrossM, awayM }
}

/** The kind each held pinch's drag has taken, by the pinch (one object a pinch): the dot and the panel read one answer. */
const taken = new WeakMap<object, DragKind>()
/** `dragOf` for one pinch, whose kind once taken is kept for the rest of that pinch. */
export function dragFor(pinch: object, travel: V3): ReturnType<typeof dragOf> {
  const d = dragOf(travel, taken.get(pinch) ?? null)
  if (d.kind) taken.set(pinch, d.kind)
  return d
}

/**
 * The page's size factor for `outM` of hand along the corner's diagonal (out of the page positive, in negative), on a
 * page whose diagonal is `diagonalM`: nothing under `stillM` (a tap on a corner sizes nothing, X1), one to one from
 * there to `exactM`, then a factor of 2 for each further `fullM - exactM`, so that `fullM` out doubles it and `fullM`
 * in halves it (as far as the page's least and most sizes let it).
 */
export function sizeFactor(outM: number, diagonalM: number): number {
  const { stillM, exactM, fullM } = CARRY
  const d = Math.abs(outM)
  const sign = Math.sign(outM)
  if (d <= stillM) return 1
  const near = 1 + (sign * (Math.min(d, exactM) - stillM)) / Math.max(diagonalM, 1e-6)
  if (d <= exactM) return near
  // From the factor at `exactM` to 2 (or 0.5) at `fullM`, the same factor for each centimetre.
  const end = sign > 0 ? 2 : 0.5
  const span = fullM - exactM
  const f = near * (end / near) ** ((Math.min(d, fullM) - exactM) / span)
  // Past `fullM` it goes on at a factor of 2 for each further span.
  return d > fullM ? f * 2 ** ((sign * (d - fullM)) / span) : f
}
