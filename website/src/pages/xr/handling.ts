/**
 * Handling things with the hands: pulling a thing apart into its parts, and
 * throwing a thing away or to a place. Plain numbers, so the feel can be
 * tested: no scene, no clock.
 *
 * Every gesture is read as an angle, a ratio or a few centimetres, never as
 * a reach. So each gesture works small, close to the body, with the hands
 * resting on the desk.
 *
 * Pull apart. The gap between the two hands says how far the thing is open,
 * from 0 (closed) to 1 (open, its parts side by side). Two rules measure it
 * (`tune.ts`): a fixed window of the gap, or the gap against the gap when
 * the hands took hold. Let go past half way and it opens; before, it shuts.
 * The parts follow on a spring, so they overshoot a little and settle.
 *
 * Throw. What counts is how fast the thing itself was moving over the last
 * moment before it was let go. A real throw glides on, slowing as a thing
 * with weight does, and lands further along. Anything slower is a put-down:
 * the thing stays where the hand let go. A throw that would land off the
 * workbench (below the desk, or over the head) puts the thing away.
 */
import { TUNE, type TwoHandRule } from './tune'

export interface Spring { x: number; v: number }

/** One step of a spring toward `target`. It is a little underdamped: about a tenth of overshoot, then rest. */
export function springStep(s: Spring, target: number, dt: number, stiffness = 170, damping = 14): Spring {
  // Small fixed steps keep it stable when a frame is slow.
  let { x, v } = s
  let left = Math.min(dt, 0.1)
  while (left > 1e-6) {
    const h = Math.min(left, 0.008)
    v += (stiffness * (target - x) - damping * v) * h
    x += v * h
    left -= h
  }
  return { x, v }
}

export const atRest = (s: Spring, target: number): boolean => Math.abs(s.x - target) < 0.002 && Math.abs(s.v) < 0.02

/**
 * How far open a thing is, 0..1. `gapM` is how far apart the two hands are now. With the `double` rule, `firstM` is
 * how far apart they were when they took hold and `from` how open the thing was then.
 */
export function openAmount(gapM: number, rule: TwoHandRule = TUNE.twoHandRule, firstM = 0, from = 0): number {
  if (!(gapM >= 0)) return rule === 'double' ? from : 0
  const open = rule === 'double'
    ? (gapM > 0 && firstM > 0 ? from + Math.log2(gapM / firstM) : from)
    : (gapM - TUNE.gapCloseM) / (TUNE.gapOpenM - TUNE.gapCloseM)
  return Math.max(0, Math.min(1, open))
}

/** Where a thing that was let go comes to rest: open past half way, else shut. */
export const openRest = (open: number): number => (open >= 0.5 ? 1 : 0)

/** How many parts a thing shows side by side; more are counted on the last one. */
export const PARTS_MAX = 4
/** The angle between two parts when a thing is fully open, and how far the parts rise above the thing. */
export const PART_STEP_DEG = 23
export const PART_RISE_M = 0.1

/** Where part i of n stands when the thing is `open`: degrees to the right of the thing, as seen from the seat, and metres up. */
export function partPlace(i: number, n: number, open: number): { yaw: number; rise: number } {
  return { yaw: (i - (n - 1) / 2) * PART_STEP_DEG * open, rise: PART_RISE_M * open }
}

export const THROW = {
  /** Speeds are how fast the thing turns about the seat, in degrees per second, measured over this window. */
  WINDOW_MS: 120,
  /** A thrown thing glides on for as far as its speed would carry it in this many seconds. A harder throw than `MAX` goes no further. */
  GLIDE_S: 0.45,
  MAX: 200,
  /** A thing that would land below or above these is off the workbench: it is put away. */
  LOW: -45,
  HIGH: 55,
} as const

export interface RaySample { t: number; yaw: number; pitch: number }
export interface Flick { yaw: number; pitch: number; speed: number }
export type ThrowKind = 'place' | 'throw'

/** How the thing was moving when it was let go: degrees per second across and up, from the last moment of the drag. */
export function flick(samples: readonly RaySample[]): Flick {
  const last = samples.at(-1)
  if (!last) return { yaw: 0, pitch: 0, speed: 0 }
  // The oldest sample still inside the window. A hand that stopped before letting go has no speed.
  // When frames come slower than the window, the sample just before it is all there is to go by.
  const first = samples.find(s => last.t - s.t <= THROW.WINDOW_MS && last.t - s.t >= 20) ?? samples.findLast(s => last.t - s.t > THROW.WINDOW_MS && last.t - s.t <= THROW.WINDOW_MS * 3) ?? last
  const dt = (last.t - first.t) / 1000
  if (dt < 0.02) return { yaw: 0, pitch: 0, speed: 0 }
  const yaw = (((last.yaw - first.yaw + 540) % 360) - 180) / dt
  const pitch = (last.pitch - first.pitch) / dt
  return { yaw, pitch, speed: Math.hypot(yaw, pitch) }
}

/**
 * What letting go at this speed means. `degPerS` is how fast the thing turned about the seat and `distM` how far from
 * the seat it is, so the speed judged is the thing's own, in metres a second. Thrown, or put down where the hand let go:
 * a slow release has one meaning.
 */
export function throwKind(degPerS: number, distM: number): ThrowKind {
  return speedOf(degPerS, distM) >= TUNE.throwFlyMps ? 'throw' : 'place'
}

/** How fast a thing itself moves, in metres a second, when it turns about the seat at `degPerS` from `distM` away. */
export const speedOf = (degPerS: number, distM: number): number => (degPerS * Math.PI * distM) / 180

/**
 * The Bot that takes work a hand let go of, or '' for none. Put down, it goes to the Bot it is within reach of.
 * Thrown, it goes to the Bot that was lit a moment before the hand let go, and to no Bot when none was.
 */
export function takerOf(thrown: ThrowKind, inReach: string, litBot: string, litAgoMs: number): string {
  if (thrown === 'throw') return litBot && litAgoMs <= TUNE.throwAimMs ? litBot : ''
  return inReach
}

/** What is lit: a Bot, a thing, a token of the interaction layer (a marble or the bin), or the card a Bot holds out. */
export interface Lit { kind: 'bot' | 'thing' | 'token' | 'card'; id: string }

/**
 * What lights. First the thing a pinch would take now, and that same thing: `takes` is what the pinch is given.
 * With nothing under the hand, the Bot whose card is in its hand (`holder`), then what would take a thing that is
 * carried (`drop`: a Bot's slug or 'bin'). null lights nothing.
 */
export function litOf(takes: { tok?: string; bot?: string; obj?: string } | null, holder: string, drop: string): Lit | null {
  if (takes?.tok) return { kind: takes.tok === 'card' ? 'card' : 'token', id: takes.tok }
  if (takes?.bot) return { kind: 'bot', id: takes.bot }
  if (takes?.obj) return { kind: 'thing', id: takes.obj }
  if (holder) return { kind: 'bot', id: holder }
  if (drop) return drop === 'bin' ? { kind: 'token', id: 'bin' } : { kind: 'bot', id: drop }
  return null
}

/** Where a thrown thing lands: on along its way, by its speed, and never further than the hardest throw that counts. */
export function landing(at: { yaw: number; pitch: number }, v: Flick): { yaw: number; pitch: number } {
  const k = THROW.GLIDE_S * (v.speed > THROW.MAX ? THROW.MAX / v.speed : 1)
  return { yaw: at.yaw + v.yaw * k, pitch: at.pitch + v.pitch * k }
}

/** Whether a landing place is off the workbench: the thing is then put away, not placed. */
export const isAway = (place: { pitch: number }): boolean => place.pitch < THROW.LOW || place.pitch > THROW.HIGH
