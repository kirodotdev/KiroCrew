/**
 * The desk's set-up by flat hands (`desk=1`): both hands lie flat and still on the real desk, and after `stillMs`
 * the desk is set there, its top at the hands' height and its near edge under the wrists, facing the way the hands
 * lie. Plain numbers and a small state machine; `hands.tsx` tells it each hand's joints and `DeskSetup.tsx` reads
 * it each frame and draws its grid of dots.
 *
 * It runs only when it is armed (`useDesk`): at the start of a session with no desk confirmed, or from the tile
 * "set the desk"; either arming ends at his first press of anything or `armedMs` after it, whichever comes first.
 * Flat hands at any other time move nothing. It moves the desk only when the new place differs
 * from the one that stands by more than `moveM` or `moveDeg`; else the standing desk is confirmed.
 */
import type { DeskPlace } from './desk'

type V3 = [number, number, number]

export const SETUP = {
  /** Both hands flat and still this long set the desk (ms). */
  stillMs: 1500,
  /** A hand lies flat when the skin's lowest points of its wrist, palm and finger tips are this close in height. */
  flatM: 0.015,
  /** A hand is still while its wrist stays this close to where it was when the stillness began. */
  stillM: 0.01,
  /** Both wrists lie at least this far ahead of the eyes along the way the head faces, and this far under them. */
  aheadM: 0.15,
  underM: 0.2,
  /** The new place moves the desk only past this far, or this many degrees of facing. */
  moveM: 0.02,
  moveDeg: 3,
  /** An armed set-up ends this long after it was armed, or at his first press of anything, whichever comes first (ms). */
  armedMs: 10000,
} as const

/** One hand as the set-up reads it: its wrist, and the joints' lowest skin points (each place less its radius). */
export interface SetupHand { wrist: V3; points: V3[] }

/** The hands of this frame, as `hands.tsx` writes them with `desk=1`; `null` while that hand's joints are not tracked. */
export const deskHands: { left: SetupHand | null; right: SetupHand | null } = { left: null, right: null }

/** Whether a hand lies flat: its points' heights within `flatM`. */
export const flatHand = (h: SetupHand): boolean => {
  const ys = h.points.map(p => p[1])
  return ys.length > 0 && Math.max(...ys) - Math.min(...ys) < SETUP.flatM
}

/**
 * Whether a hand lies where a desk can be: its wrist ahead of the eyes at `eye` (along `ahead`, the head's facing
 * across the floor) and under them. Hands that hang at the sides or rest in the lap are no desk, however flat.
 */
export const onADesk = (h: SetupHand, eye: V3, ahead: readonly [number, number]): boolean =>
  (h.wrist[0] - eye[0]) * ahead[0] + (h.wrist[2] - eye[2]) * ahead[1] >= SETUP.aheadM && eye[1] - h.wrist[1] >= SETUP.underM

/** Where two flat hands put the desk: its top at their mean height, its near edge's middle under the wrists' middle. */
export function placeOf(left: SetupHand, right: SetupHand): { at: V3; yawDeg: number } {
  const all = [...left.points, ...right.points]
  const top = all.reduce((s, p) => s + p[1], 0) / all.length
  const mid: V3 = [(left.wrist[0] + right.wrist[0]) / 2, top, (left.wrist[2] + right.wrist[2]) / 2]
  // Across runs from the left wrist to the right one; at a facing of 0 that is +x (`deskPoint`).
  const yawDeg = (Math.atan2(right.wrist[2] - left.wrist[2], right.wrist[0] - left.wrist[0]) * 180) / Math.PI
  return { at: mid, yawDeg: Math.round(yawDeg * 10) / 10 }
}

/** Whether a new place differs from the desk that stands by more than `moveM` or `moveDeg`. */
export function movesDesk(now: Pick<DeskPlace, 'at' | 'yawDeg'> | null, next: { at: V3; yawDeg: number }): boolean {
  if (!now) return true
  const turn = Math.abs((((next.yawDeg - now.yawDeg) % 360) + 540) % 360 - 180)
  return Math.hypot(next.at[0] - now.at[0], next.at[1] - now.at[1], next.at[2] - now.at[2]) > SETUP.moveM || turn > SETUP.moveDeg
}

/** The set-up's reading, frame by frame: how far the stillness has come (0 to 1), and the place once it is whole. */
export class SetupClock {
  private since: number | null = null
  private from: [V3, V3] | null = null

  read(left: SetupHand | null, right: SetupHand | null, now: number): { share: number; place: { at: V3; yawDeg: number } | null } {
    if (!left || !right || !flatHand(left) || !flatHand(right)) { this.since = null; this.from = null; return { share: 0, place: null } }
    const moved = this.from && (Math.hypot(...sub(left.wrist, this.from[0])) > SETUP.stillM || Math.hypot(...sub(right.wrist, this.from[1])) > SETUP.stillM)
    if (this.since === null || moved) { this.since = now; this.from = [left.wrist, right.wrist] }
    const share = Math.min(1, (now - this.since) / SETUP.stillMs)
    return { share, place: share >= 1 ? placeOf(left, right) : null }
  }
}

const sub = (a: V3, b: V3): V3 => [a[0] - b[0], a[1] - b[1], a[2] - b[2]]
