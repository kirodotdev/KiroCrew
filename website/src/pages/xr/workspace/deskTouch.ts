/**
 * A finger's touch on the desk (`desk=1`): the index tip of a pointing hand comes down onto a pad or a tile and
 * lifts off it again, and that is a press of it. Plain numbers and one small state machine per hand; `DeskTouch.tsx`
 * feeds it each frame from the hands' joints (`deskSetup.ts`, `deskHands`) and presses what it says.
 *
 * Every height is from skin to desk: each joint's place less its radius.
 */
type V3 = readonly [number, number, number]

export const TOUCH = {
  /** A tip comes from higher than this over the desk, and stays within `downM` of it `downMs`: the down. */
  fromM: 0.03,
  downM: 0.015,
  downMs: 60,
  /** It leaves past this: the up. A press is a down and an up on the same pad or tile within `pressMs`. */
  upM: 0.025,
  pressMs: 800,
  /** A touch starts only from a tip tracked in every frame of this long (ms). */
  trackedMs: 200,
  /** A hand points when its index tip is this far from the palm, and its middle and ring tips this much higher than it. */
  reachM: 0.06,
  curlM: 0.02,
  /**
   * While the index tip of a pointing hand is nearer than this to a pad's or tile's box, from any side, the dot is
   * off and no pinch presses (`pinchRoute.tipNear`; `nearestM`). A hand that does not point (one that rests on the
   * hands' rest with its tips by the first seats) leaves the dot on.
   */
  nearM: 0.05,
} as const

/** One hand in one frame, as the touch reads it: its palm, and its index, middle and ring tips (skin points). */
export interface TouchHand { palm: V3; index: V3; middle: V3; ring: V3 }

/** Whether a hand points: the index reaches out, and the middle and ring fingers are curled up away from the desk. */
export const pointing = (h: TouchHand): boolean =>
  Math.hypot(h.index[0] - h.palm[0], h.index[1] - h.palm[1], h.index[2] - h.palm[2]) >= TOUCH.reachM &&
  h.middle[1] - h.index[1] >= TOUCH.curlM && h.ring[1] - h.index[1] >= TOUCH.curlM

/** What changed this frame: a down on a target, an up (a press when `press`), or a touch called off. */
export type TouchChange = { down: string } | { up: string; press: boolean } | { cancel: string } | null

/** One hand's touch, frame by frame. */
export class TouchClock {
  /** Whether the tip was higher than `fromM` since it last came down. */
  private high = false
  private trackedSince: number | null = null
  private nearSince: number | null = null
  private nearOn: string | null = null
  /** The target that the tip is down on, and when it came down; `null` while it is not down. */
  down: { on: string; at: number } | null = null
  /** Whether the tip of this hand, pointing, is within `nearM` of a pad's or tile's box now. */
  near = false

  /**
   * `hand`: the hand this frame (`null`: not tracked); `heightM`: its index tip over the desk; `on`: the pad or tile
   * under the tip, or `null`; `nearestM`: the tip's distance to the nearest pad's or tile's box; `may`: whether a
   * down may start now (no other press owns the button).
   */
  read(hand: TouchHand | null, heightM: number, on: string | null, nearestM: number, now: number, may: boolean): TouchChange {
    if (!hand) {
      this.trackedSince = this.nearSince = null
      this.high = false
      this.near = false
      return this.callOff()
    }
    this.trackedSince ??= now
    this.near = false
    if (!pointing(hand)) { this.high = false; this.nearSince = null; return this.callOff() }
    this.near = nearestM < TOUCH.nearM
    if (this.down) {
      if (heightM <= TOUCH.upM) return null
      const was = this.down
      this.down = null
      this.high = heightM > TOUCH.fromM
      return { up: was.on, press: on === was.on && now - was.at <= TOUCH.pressMs }
    }
    if (heightM > TOUCH.fromM) { this.high = true; this.nearSince = null; return null }
    if (!this.high || heightM >= TOUCH.downM || on === null || now - this.trackedSince < TOUCH.trackedMs) { this.nearSince = null; return null }
    if (this.nearSince === null || this.nearOn !== on) { this.nearSince = now; this.nearOn = on }
    if (now - this.nearSince < TOUCH.downMs || !may) return null
    this.down = { on, at: now }
    this.high = false
    this.nearSince = null
    return { down: on }
  }

  private callOff(): TouchChange {
    const was = this.down
    this.down = null
    return was ? { cancel: was.on } : null
  }
}

/**
 * A pad or a tile that a touch may press: its middle on the desk, its reach from there across the desk, its press,
 * and the upright box that takes a pointer over it (half its side, its height, and the desk's facing).
 */
export interface TouchTarget { center: V3; radiusM: number; press: () => void; box: { halfM: number; heightM: number; yawDeg: number } }

/** The nearest distance in the room from a point to a target's box, from any side (0 inside it). */
export function nearestM(p: V3, t: TouchTarget): number {
  const yaw = (t.box.yawDeg * Math.PI) / 180
  const dx = p[0] - t.center[0]
  const dz = p[2] - t.center[2]
  // Across the desk and into it, as `deskPoint` turns them.
  const u = dx * Math.cos(yaw) + dz * Math.sin(yaw)
  const v = dx * Math.sin(yaw) - dz * Math.cos(yaw)
  const up = p[1] - t.center[1]
  const out = (d: number, lo: number, hi: number) => (d < lo ? lo - d : d > hi ? d - hi : 0)
  return Math.hypot(out(u, -t.box.halfM, t.box.halfM), out(v, -t.box.halfM, t.box.halfM), out(up, 0, t.box.heightM))
}

/** The tip's distance to the nearest box of all the targets (`Infinity` with none). */
export const nearestOf = (p: V3, targets: ReadonlyMap<string, TouchTarget>): number => Math.min(Infinity, ...[...targets.values()].map(t => nearestM(p, t)))

/** The pads and tiles on the desk now, by id; `Desk.tsx` and `Crew.tsx` keep their own in it while they are drawn. */
export const touchTargets = new Map<string, TouchTarget>()

/** The target under a tip: the nearest whose middle lies within its reach, across the desk (heights aside). */
export function targetUnder(tip: V3, targets: ReadonlyMap<string, TouchTarget>): string | null {
  let best: string | null = null
  let bestD = Infinity
  for (const [id, t] of targets) {
    const d = Math.hypot(tip[0] - t.center[0], tip[2] - t.center[2])
    if (d <= t.radiusM && d < bestD) { best = id; bestD = d }
  }
  return best
}
