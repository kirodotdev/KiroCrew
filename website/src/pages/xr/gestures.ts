/**
 * Three hand verbs that are read from the shape and the turn of a hand, with
 * no scene and no clock of their own:
 *
 * Turn. A key is held and the wrist rolls a quarter turn, to the click. A
 * hand that lets go before the click has done nothing, and a hand that only
 * shakes never gets there. The same hold can take the key out of its lock
 * instead. While the hand holds it, nothing has happened: pushing it back
 * restores the request. Letting go of it away from the lock is the no.
 * The key is parked: no scene draws one and nothing reads a hand into
 * `KeyHold`. It stays here, tested, as a way of answering that can be put
 * back in `approval.ts` without touching the route a yes takes.
 *
 * Palm. An open hand, fingers up, facing a Bot and held still: "Stop. Not
 * now." It never refuses and never dismisses for good.
 *
 * Pat. An open hand that comes down on top of a Bot twice: "Thanks."
 * Nothing else.
 *
 * Both palms. Two open hands held forward: "Everyone stop." Both hands then
 * pulled back: "Carry on."
 *
 * Palm and Pat are both an open hand. They are told apart by where the palm
 * faces: at the Bot, upright, or down on top of it. Mixed up, one stops and
 * one thanks, and neither changes any work.
 */
import { palmNormal, type PalmJoints, type V3 } from './hand'
import type { FingerShape } from './pointTalk'
import { TUNE } from './tune'

const DEG = Math.PI / 180
const wrap = (d: number): number => ((d + 540) % 360) - 180
const sub = (a: V3, b: V3): V3 => [a[0] - b[0], a[1] - b[1], a[2] - b[2]]
const unit = (a: V3): V3 => { const l = Math.hypot(a[0], a[1], a[2]) || 1; return [a[0] / l, a[1] / l, a[2] / l] }
const dot = (a: V3, b: V3): number => a[0] * b[0] + a[1] * b[1] + a[2] * b[2]

export const KEY = {
  /** The wrist must roll this far, either way, for the click. */
  TURN_DEG: TUNE.keyTurnDeg,
  /** Once the wrist has rolled this far the hold is a turn, and can no longer pull the key out. */
  COMMIT_DEG: 25,
  /** The key comes out when the hand's ray has moved this far from where it took hold, or the hand itself this far. */
  PULL_DEG: TUNE.keyPullDeg,
  PULL_M: TUNE.keyPullM,
} as const

/** How far a pose is rolled about its own forward axis, in degrees: 0 with its right side level, positive rolled to the right. */
export function rollDeg(q: readonly [number, number, number, number]): number {
  const [x, y, z, w] = q
  // The y of the pose's own right and up axes. With no roll the right axis is level.
  const rightY = 2 * (x * y + w * z)
  const upY = 1 - 2 * (x * x + z * z)
  return -Math.atan2(rightY, upY) / DEG
}

export type KeyEnd = 'nothing' | 'turned' | 'pulled'

/** One hold of a key, from the pinch to the release. */
export class KeyHold {
  /** How far the key is turned now, whether it has clicked, and whether it is out of its lock right now. */
  turn = 0
  clicked = false
  out = false

  constructor(private readonly roll0: number) {}

  /** `roll` is the hand's roll now; `rayDeg` and `moveM` are how far its ray and the hand itself are from where they took hold. */
  feed(roll: number, rayDeg: number, moveM: number): void {
    const away = Math.max(rayDeg / KEY.PULL_DEG, moveM / KEY.PULL_M)
    // A key that is out comes with the hand, and goes back in when the hand brings it back: half way, so a tremor at the mark does not flip it.
    if (this.out) { this.out = away > 0.5; return }
    this.turn = wrap(roll - this.roll0)
    if (Math.abs(this.turn) >= KEY.TURN_DEG) this.clicked = true
    // A key that has clicked stays turned. One that is still nearly straight can come out.
    if (!this.clicked && Math.abs(this.turn) < KEY.COMMIT_DEG && away >= 1) this.out = true
  }

  /** What letting go now means. Turned to the click: yes. Let go out of the lock: no. Anything else springs back: nothing happened. */
  get end(): KeyEnd { return this.clicked ? 'turned' : this.out ? 'pulled' : 'nothing' }
}

export const PALM = {
  /** Every finger this straight is an open hand. */
  OPEN: 0.8,
  /** How upright the fingers are (the y of wrist to middle fingertip), and how squarely the palm faces. */
  UP: 0.6,
  FACING: 0.6,
  /** A palm must be held this long to count, and a pat's two taps come within this long. */
  HOLD_MS: 350,
  PAT_MS: 1200,
  /** Half the width of a palm, in metres. */
  WIDTH: 0.07,
} as const

export interface OpenHand {
  joints: PalmJoints
  handed: 'left' | 'right'
  shape: FingerShape | null
  /** `middle-finger-tip` */
  tip: V3
}

const open = (h: OpenHand): boolean => !!h.shape && h.shape.index >= PALM.OPEN && h.shape.others >= PALM.OPEN
const centre = (j: PalmJoints): V3 => [(j.wrist[0] + j.index[0] + j.pinky[0]) / 3, (j.wrist[1] + j.index[1] + j.pinky[1]) / 3, (j.wrist[2] + j.index[2] + j.pinky[2]) / 3]

/** How squarely an open, upright hand's palm faces a point, -1..1. 0 when the hand is not open or its fingers are not up. */
export function palmToward(h: OpenHand, at: V3): number {
  if (!open(h) || unit(sub(h.tip, h.joints.wrist))[1] < PALM.UP) return 0
  return dot(palmNormal(h.joints, h.handed), unit(sub(at, centre(h.joints))))
}

/** Whether an open hand lies palm down over a point: within `reach` of it sideways. Returns how far above it the palm is, or null. */
export function palmOver(h: OpenHand, top: V3, reach: number): number | null {
  if (!open(h) || palmNormal(h.joints, h.handed)[1] > -0.5) return null
  const c = centre(h.joints)
  // A palm is wider than a small Bot: the Bot may be under any part of it.
  return Math.hypot(c[0] - top[0], c[2] - top[2]) <= Math.max(reach, PALM.WIDTH) ? c[1] - top[1] : null
}

/** How squarely an open, upright hand's palm faces away from a head at `head`, -1..1. 0 when the hand is not open or its fingers are not up. */
export function palmForward(h: OpenHand, head: V3): number {
  const c = centre(h.joints)
  return palmToward(h, [2 * c[0] - head[0], 2 * c[1] - head[1], 2 * c[2] - head[2]])
}

export type BothVerb = 'stop' | 'carry-on'

/**
 * Two hands together. Both palms pushed forward and held there: "Everyone stop." After that, both hands pulled back toward the body:
 * "Carry on." One palm is neither: it is "not now" to one Bot, and is read elsewhere.
 */
export class BothHands {
  private readonly held = new Dwell(TUNE.bothPalmsMs)
  /** How near the head each hand has been since both palms came up: the push is measured from there. */
  private from: [number, number] | null = null
  /** How far each hand was from the head when everyone stopped, or null when nobody is stopped. */
  private stoppedAt: [number, number] | null = null
  /** Both palms are forward right now, so neither is read as "not now" to one Bot. */
  both = false

  /** `left` and `right` are the open hands (null when a hand is not open or not seen); the wrists are where the hands are, open or not. */
  feed(left: OpenHand | null, right: OpenHand | null, wrists: readonly [V3 | null, V3 | null], head: V3, now: number): BothVerb | null {
    const far = (w: V3 | null): number | null => (w ? Math.hypot(w[0] - head[0], w[1] - head[1], w[2] - head[2]) : null)
    const [dl, dr] = [far(wrists[0]), far(wrists[1])]
    this.both = !!left && !!right && palmForward(left, head) >= PALM.FACING && palmForward(right, head) >= PALM.FACING
    this.from = this.both && dl !== null && dr !== null ? (this.from ? [Math.min(this.from[0], dl), Math.min(this.from[1], dr)] : [dl, dr]) : null
    // Two open hands that are only held up push nothing: both must have gone forward, and stay there.
    const pushed = !!this.from && dl !== null && dr !== null && dl - this.from[0] >= TUNE.palmsPushM && dr - this.from[1] >= TUNE.palmsPushM
    if (this.held.feed(pushed, now) && dl !== null && dr !== null) { this.stoppedAt = [dl, dr]; return 'stop' }
    if (!this.stoppedAt || dl === null || dr === null) return null
    // The hands may push on after the stop: the pull back is measured from the furthest they went.
    this.stoppedAt = [Math.max(this.stoppedAt[0], dl), Math.max(this.stoppedAt[1], dr)]
    if (this.stoppedAt[0] - dl < TUNE.pullBackM || this.stoppedAt[1] - dr < TUNE.pullBackM) return null
    this.stoppedAt = null
    return 'carry-on'
  }

}

/** True once, when something has held without a break for long enough. */
export class Dwell {
  private since = 0
  private fired = false
  constructor(private readonly ms: number) {}
  feed(on: boolean, now: number): boolean {
    if (!on) { this.since = 0; this.fired = false; return false }
    if (!this.since) this.since = now
    if (this.fired || now - this.since < this.ms) return false
    this.fired = true
    return true
  }
}

/** Two soft taps on top of a Bot. `above` is how far the palm is over the Bot's top (null when it is not over it), `reach` the Bot's own size. */
export class PatDetector {
  private taps: number[] = []
  private up = true
  feed(above: number | null, reach: number, now: number): boolean {
    if (above === null) { this.up = true; return false }
    const near = above < reach * 0.6
    if (near && this.up) {
      this.up = false
      this.taps = [...this.taps.filter(t => now - t <= PALM.PAT_MS), now]
      if (this.taps.length >= 2) { this.taps = []; return true }
    } else if (above > reach) this.up = true
    return false
  }
}
