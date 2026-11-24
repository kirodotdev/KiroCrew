/**
 * How a Bot's body moves, as numbers. No drawing here: `Bot.tsx` reads a frame of this and turns bones.
 *
 * Every pose the page asks for is a set of resting values. Springs chase them, so a change at any moment
 * starts from what is on screen. On top of that sits a little life: a blink, a glance and a breath, all drawn
 * from one seed per Bot, so two Bots never move in step and the same Bot always moves the same way.
 *
 * Lengths are in body heights. Angles are in radians.
 */
import type { BotPose, BotState } from './body'

/** The pose a Bot takes for a state when nothing else is asked of it. */
export const POSE_FOR_STATE: Record<BotState, BotPose> = { idle: 'rest', working: 'work', needs: 'ask', failed: 'slump' }

/** What the page asked of a Bot, and the one pose that follows from it. A Bot that moves or carries stands; a Bot that listens does only that. */
export function poseFor(a: { moving: boolean; carrying: boolean; listening: boolean; aiming: boolean; forced: BotPose | null; sleeping: boolean; state: BotState }): BotPose {
  return a.moving || a.carrying ? 'stand' : a.listening ? 'listen' : a.aiming ? 'point' : a.forced ?? (a.sleeping ? 'sleep' : POSE_FOR_STATE[a.state])
}

/** A number from the letters of a name: the seed of a Bot. */
export function seedOf(name: string): number {
  let h = 2166136261
  for (let i = 0; i < name.length; i += 1) h = Math.imul(h ^ name.charCodeAt(i), 16777619)
  return h >>> 0 || 1
}

/** The same seed always gives the same numbers, each from 0 up to 1. */
export function rng(seed: number): () => number {
  let s = seed >>> 0 || 1
  return () => {
    s ^= s << 13; s >>>= 0
    s ^= s >>> 17
    s ^= s << 5; s >>>= 0
    return s / 4294967296
  }
}

/** A value that chases a target. `hz` is how fast, `zeta` how soon it settles: 1 never overshoots, 0.3 wobbles like jelly. */
export class Spring {
  v = 0
  constructor(public x = 0) {}
  step(target: number, dt: number, hz: number, zeta: number): number {
    const w = 2 * Math.PI * hz
    // Small steps keep it steady on a frame that came late.
    const n = Math.max(1, Math.ceil(dt * 120))
    const h = dt / n
    for (let i = 0; i < n; i += 1) { this.v += (w * w * (target - this.x) - 2 * zeta * w * this.v) * h; this.x += this.v * h }
    return this.x
  }
  /** Go to the target at once: for a person who asked for less motion. */
  jump(target: number): number { this.x = target; this.v = 0; return target }
}

/** Where a pose rests. `gaze` is where the eyes stay, from -1 to 1 across and up; `null` leaves them to wander. */
export interface PoseRest {
  /** How high it floats. */
  lift: number
  /** Shorter and wider above 0, taller and thinner below. The body keeps its volume. */
  squash: number
  /** The whole body tips forward. */
  lean: number
  /** The head tips forward, and to its side. */
  head: number
  tilt: number
  /** How far the eyes are shut, 0 to 1, and how much bigger than usual they are, 0 to 1. */
  lid: number
  wide: number
  gaze: readonly [number, number] | null
  /** The slow rise and fall of a thing that floats: how far, and how many times a second. */
  bob: number
  bobHz: number
  /** How much longer than usual it goes between blinks. 0 is no blink at all. */
  blinkEvery: number
}

export const POSES: Record<BotPose, PoseRest> = {
  // Idle: it floats and looks about.
  rest: { lift: 0.06, squash: 0, lean: 0, head: 0, tilt: 0, lid: 0, wide: 0, gaze: null, bob: 0.012, bobHz: 0.35, blinkEvery: 1 },
  // Upright and ready: it was told to stop, or it is on its way somewhere.
  stand: { lift: 0.08, squash: -0.03, lean: 0, head: -0.03, tilt: 0, lid: 0, wide: 0, gaze: null, bob: 0.008, bobHz: 0.35, blinkEvery: 1 },
  // Working: bent over the work, eyes down on it, a quick small bob. Busy, not puzzled.
  work: { lift: 0.05, squash: 0.02, lean: 0.16, head: 0.14, tilt: 0, lid: 0.12, wide: 0, gaze: [0, -0.7], bob: 0.006, bobHz: 1.6, blinkEvery: 1.3 },
  // A reply is awaited: head to one side, eyes up and away.
  think: { lift: 0.07, squash: 0, lean: -0.05, head: -0.12, tilt: 0.2, lid: 0, wide: 0, gaze: [-0.45, 0.8], bob: 0.012, bobHz: 0.25, blinkEvery: 1.8 },
  // Needs you: it rises and stands tall, eyes big and on you.
  ask: { lift: 0.13, squash: -0.06, lean: 0, head: -0.06, tilt: 0, lid: 0, wide: 0.6, gaze: [0, 0], bob: 0.014, bobHz: 0.5, blinkEvery: 1 },
  // Failed: low, sunk, eyes half shut and down.
  slump: { lift: 0.01, squash: 0.09, lean: 0.26, head: 0.3, tilt: -0.16, lid: 0.5, wide: 0, gaze: [0, -0.6], bob: 0.003, bobHz: 0.2, blinkEvery: 2 },
  // Lit, because a finger is on it: it leans in, eyes big and on you, and it blinks less.
  listen: { lift: 0.08, squash: -0.02, lean: 0.1, head: -0.05, tilt: 0.12, lid: 0, wide: 1, gaze: [0, 0], bob: 0.006, bobHz: 0.35, blinkEvery: 2.2 },
  // It shows you something: turned to it, a little forward.
  point: { lift: 0.08, squash: -0.02, lean: 0.06, head: 0, tilt: 0, lid: 0, wide: 0.3, gaze: [0, 0], bob: 0.006, bobHz: 0.35, blinkEvery: 1 },
  // A nap: on the ground, eyes shut, a slow breath.
  sleep: { lift: 0, squash: 0.07, lean: 0.3, head: 0.4, tilt: 0.2, lid: 1, wide: 0, gaze: [0, -0.3], bob: 0.01, bobHz: 0.12, blinkEvery: 0 },
}

/** How high a hop goes and how long it lasts. A Bot hops when it takes work or a card, when it holds a card out, and when it is thanked. */
export const HOP = { rise: 0.2, ms: 420 } as const
/** How far a Bot that moves leans the way it goes (radians). */
const WALK_LEAN = 0.18
/** The small hop of a Bot whose work just ended. */
const DONE_HOP = 0.5

/** One moment of a Bot's body: what `Bot.tsx` writes to the bones and the eyes. */
export interface BotFrame {
  /** How high the body is above the ground. */
  lift: number
  /** The body's scale up, and across. Across is 1 / sqrt(up): the volume stays. */
  up: number
  across: number
  lean: number
  /** The whole body tips to its +x side (radians): it is carried that way. */
  side: number
  head: number
  tilt: number
  /** The hem's own turn: it trails the body. */
  hem: number
  /** The eyes: how shut, how big, and where they look, each from -1 to 1. */
  blink: number
  wide: number
  lookX: number
  lookY: number
}

/** The springs and the life of one Bot. Step it once a frame. */
export class BotMotion {
  private readonly rand: () => number
  private readonly phase: number
  private readonly lift = new Spring(POSES.rest.lift)
  private readonly squash = new Spring()
  private readonly lean = new Spring()
  private readonly side = new Spring()
  private readonly head = new Spring()
  private readonly tilt = new Spring()
  private readonly hem = new Spring()
  private readonly lid = new Spring()
  private readonly wide = new Spring()
  private readonly gx = new Spring()
  private readonly gy = new Spring()
  private t = 0
  private nextBlink: number
  private blinkAt = -9
  private nextLook = 0
  private lookX = 0
  private lookY = 0
  private pose: BotPose = 'rest'
  private hopAt = -9
  private hopRise = 0
  private landed = true

  constructor(seed: number) {
    this.rand = rng(seed)
    this.phase = this.rand() * 10
    this.nextBlink = 1 + this.rand() * 3
  }

  /** One hop, starting now: it stretches as it leaves, and wobbles when it is back. */
  hop(scale = 1): void { this.hopAt = this.t; this.hopRise = HOP.rise * scale; this.landed = false; this.squash.v -= 1.2 * scale }
  /** The work it was on ended: one small hop. */
  done(): void { this.hop(DONE_HOP) }
  /** A nod: a push on the head, and the spring brings it back. */
  nod(): void { this.head.v += 6 }

  /**
   * `dt` in seconds. `walking` tips it the way it goes and lets the hem trail. `speaking` bobs the head.
   * `still` is reduced motion: every value goes to its rest at once, and nothing bobs, hops, blinks or glances.
   */
  step(dt: number, pose: BotPose, o: { walking: boolean; speaking: boolean; still: boolean; own?: BotPose; way?: readonly [number, number] }): BotFrame {
    const rest = POSES[pose]
    this.t += dt
    const t = this.t
    if (o.still) {
      const up = 1 - this.squash.jump(rest.squash)
      this.landed = true
      this.hopAt = -9
      this.pose = o.own ?? pose
      return {
        lift: this.lift.jump(rest.lift), up, across: 1 / Math.sqrt(up), lean: this.lean.jump(rest.lean), side: this.side.jump(0), head: this.head.jump(rest.head), tilt: this.tilt.jump(rest.tilt),
        hem: this.hem.jump(0), blink: this.lid.jump(rest.lid), wide: this.wide.jump(rest.wide), lookX: this.gx.jump(rest.gaze?.[0] ?? 0), lookY: this.gy.jump(rest.gaze?.[1] ?? 0),
      }
    }
    // A blink comes right after a change of pose: it hides the change in the eyes. Only a change of its own pose
    // (`own`: it asks, it fails, it listens), never the stand that a move puts it in and takes it out of: a Bot that
    // is walked or carried blinks by its own clock (M99).
    const own = o.own ?? pose
    if (own !== this.pose) { this.pose = own; if (rest.blinkEvery) this.nextBlink = Math.min(this.nextBlink, t + 0.05) }
    if (rest.blinkEvery && t >= this.nextBlink) {
      this.blinkAt = t
      // Now and then a second blink follows the first at once.
      this.nextBlink = t + (this.rand() < 0.18 ? 0.3 : (2.2 + this.rand() * 3.3) * rest.blinkEvery)
    }
    // Shut in 0.08 s, held for 0.03 s, open in 0.16 s.
    const u = t - this.blinkAt
    const blink = u > 0.27 || u < 0 ? 0 : u < 0.08 ? u / 0.08 : u < 0.11 ? 1 : 1 - (u - 0.11) / 0.16
    // A new glance every 1.2 to 3.6 s, often back to the middle.
    if (t >= this.nextLook) {
      this.nextLook = t + 1.2 + this.rand() * 2.4
      const middle = this.rand() < 0.35
      this.lookX = middle ? 0 : (this.rand() * 2 - 1) * 0.6
      this.lookY = middle ? 0 : (this.rand() * 2 - 1) * 0.35
    }
    // The hop: up and down on a fixed arc, then a wobble as it lands.
    const hu = ((t - this.hopAt) * 1000) / HOP.ms
    const hop = hu >= 0 && hu < 1 ? 4 * hu * (1 - hu) * this.hopRise : 0
    if (hu >= 1 && !this.landed) { this.landed = true; this.squash.v += 2.4 * (this.hopRise / HOP.rise) }

    // It leans the way it goes (`way`: ahead and to its +x, in its own frame): ahead on a walk of its own, to the side
    // or back when it is carried that way (D118 4).
    const [ahead, across] = o.way ?? [1, 0]
    const lean = this.lean.step(rest.lean + (o.walking ? WALK_LEAN * ahead : 0), dt, 2.2, 0.5)
    const side = this.side.step(o.walking ? WALK_LEAN * across : 0, dt, 2.2, 0.5)
    const squash = this.squash.step(rest.squash, dt, 3, 0.32)
    const breath = 0.012 * Math.sin((t + this.phase) * 2)
    const up = Math.max(0.6, 1 - squash + breath)
    const reading = pose === 'work' ? Math.sin((t + this.phase) * 1.7) * 0.5 : 0
    return {
      lift: this.lift.step(rest.lift, dt, 1.8, 0.6) + hop + rest.bob * Math.sin((t + this.phase) * 2 * Math.PI * rest.bobHz),
      up,
      across: 1 / Math.sqrt(up),
      lean,
      side,
      head: this.head.step(rest.head, dt, 2.6, 0.45) + (o.speaking ? Math.sin(t * 9) * 0.04 : 0),
      tilt: this.tilt.step(rest.tilt, dt, 2.2, 0.5),
      // The hem is loose: it lags behind the lean, swings past and comes back.
      hem: this.hem.step(-0.7 * (lean - rest.lean) - (o.walking ? 0.22 : 0), dt, 1.9, 0.28) - 0.5 * (lean - rest.lean),
      blink: Math.max(blink, this.lid.step(rest.lid, dt, 5, 1)),
      wide: this.wide.step(rest.wide, dt, 4, 0.8),
      // Eyes are quick and do not swing past.
      lookX: this.gx.step(rest.gaze ? rest.gaze[0] + reading : this.lookX, dt, 2.6, 0.9),
      lookY: this.gy.step(rest.gaze ? rest.gaze[1] : this.lookY, dt, 2.6, 0.9),
    }
  }
}

/** Where a Bot is on its plot and which way it faces, as plain numbers. It goes in a straight line, at one speed. */
export class BotWalk {
  x = 0
  z = 0
  facing = 0
  moving = false
  private to: { x: number; z: number; done?: () => void } | null = null

  get walking(): boolean { return !!this.to }
  walkTo(x: number, z: number, done?: () => void): void { this.to = { x, z, done } }
  /** Stop where it stands, at once: the walk is dropped and its `done` is not told. */
  halt(): void { this.to = null; this.moving = false }

  /** `want` is the way it should face when it stands. `still` is reduced motion: it is there at once. */
  step(dt: number, speed: number, want: number, still: boolean): void {
    const k = still ? 1 : 1 - Math.exp(-dt * 8)
    this.moving = false
    if (this.to) {
      const dx = this.to.x - this.x
      const dz = this.to.z - this.z
      const dist = Math.hypot(dx, dz)
      if (dist < 0.004 || still) {
        this.x = this.to.x
        this.z = this.to.z
        const done = this.to.done
        this.to = null
        done?.()
      } else {
        const step = Math.min(dist, speed * dt)
        this.x += (dx / dist) * step
        this.z += (dz / dist) * step
        this.facing = Math.atan2(dx, dz)
        this.moving = true
      }
    }
    if (!this.moving) {
      const delta = want - this.facing
      this.facing += Math.atan2(Math.sin(delta), Math.cos(delta)) * k
    }
  }
}
