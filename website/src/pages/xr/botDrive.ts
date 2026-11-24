/**
 * What the scene asked of one Bot with the ghost body, and where it stands.
 *
 * It is the body the scene talks to (the verbs are listed in `body.ts`), and it draws nothing: `root` is a plain transform on the
 * land. The component that draws the Bot (`Bot.tsx`) steps it once a frame and puts the body where it says.
 */
import { Group, Object3D, type Vector3 } from 'three'
import type { BotPose, BotState } from './body'
import { BotMotion, BotWalk, poseFor, type BotFrame } from './botMotion'
import { HEAD_TOP_Y } from './character'

/** The body is one unit tall in its file. In the scene it is this tall at scale 1. */
export const BOT_H = HEAD_TOP_Y
const WALK_SPEED = 0.45
/**
 * A Bot that the scene moves (`setMoved`) is seen moving from `go` body heights a second, and until it is under
 * `stop`. A jump of `jump` body heights or more in one frame was no move: the scene put it somewhere else at once.
 */
const PUT = { go: 0.6, stop: 0.3, jump: 3 } as const

export class BotDrive {
  readonly root = new Group()
  readonly top = new Object3D()
  readonly height = BOT_H
  /** A card this Bot takes rests in front of its belly, clear of the body. */
  readonly front = BOT_H * 0.5
  state: BotState = 'idle'
  poseNow: BotPose = 'rest'
  /** Reduced motion, as the scene last said. */
  still = false
  /**
   * A body of it is on screen: the component that draws it says so in its first frame, and takes it back when it
   * goes. Its look being known is not that: the model may still be loading, and nothing is drawn meanwhile.
   */
  drawn = false
  readonly motion: BotMotion
  private readonly walk = new BotWalk()
  private forced: BotPose | null = null
  private restYaw = 0
  private lookAt: Vector3 | null = null
  private listening = false
  private aiming = false
  private sleeping = false
  private speaking = false
  private carried: Object3D | null = null
  private speed = WALK_SPEED
  /** The scene puts this Bot where it goes, frame by frame (`setMoved`), and it went fast enough to be seen moving. */
  private moved = false
  private put = false
  /** The way it goes, in its own frame: ahead (+z) and to its +x, as shares of one. */
  private readonly way: [number, number] = [1, 0]
  /** Things that stand where the body stands, moved at each step of `frame` (the workspace's ring, sign and press box). */
  readonly riders = new Set<Object3D>()

  constructor(seed: number) {
    this.motion = new BotMotion(seed)
    this.top.position.y = BOT_H * 1.1
    this.root.add(this.top)
  }

  setState(state: BotState): void {
    // Work that ended, or a request that was answered: one small hop.
    if (state === 'idle' && (this.state === 'working' || this.state === 'needs')) this.motion.done()
    this.state = state
  }
  setPose(pose: BotPose | null): void { this.forced = pose }
  setRest(yaw: number): void { this.restYaw = yaw }
  listen(target: Vector3 | null): void { this.lookAt = target; this.listening = !!target; this.aiming = false }
  pointAt(target: Vector3 | null): void { if (!this.listening) this.lookAt = target; this.aiming = !!target }
  hop(): void { this.motion.hop() }
  sleep(on: boolean): void { this.sleeping = on }
  nod(): void { this.motion.nod() }
  speak(on: boolean): void { this.speaking = on }
  get walking(): boolean { return this.walk.walking }
  /** Walk to `to`, at `speed` metres a second (the land's pace when it is left out). */
  walkTo(to: Vector3, done?: () => void, speed = WALK_SPEED): void {
    // A walk starts where the body stands now, also when no frame has stepped it there yet.
    if (!this.walk.walking) { this.walk.x = this.root.position.x; this.walk.z = this.root.position.z }
    this.speed = speed
    this.walk.walkTo(to.x, to.z, done)
  }
  /** Stop where it stands, at once, with no glide (`BotWalk.halt`). */
  halt(): void { this.walk.halt() }

  /**
   * Hold a thing in front of the belly, or let go of it with `null`.
   * shortcut: the pose shows it and the thing is placed, but nothing draws it; draw it from the component when the scene first gives a Bot a thing.
   */
  carry(obj: Object3D | null): void {
    this.carried?.removeFromParent()
    this.carried = obj
    if (obj) { obj.position.set(0, BOT_H * 0.4, this.front); this.root.add(obj) }
  }

  /** The frame is the framework's (`useFrame`). The scene's own call only says whether motion is reduced. */
  update(_dt: number, _clock: number, still: boolean): void { this.still = still }
  /**
   * The scene moves this Bot itself, by putting it where it goes each frame: the room's walk, or a hand that carries
   * it. Then it moves as a Bot on its own walk does (it stands, and leans into the way it goes) while it is put
   * somewhere new fast enough to be seen, and it stands upright again when it is held still. A Bot that the scene
   * only sways in place, or lays out again, is not moved.
   */
  setMoved(on: boolean): void { this.moved = on }
  /** The scene moves it now (`setMoved`), and motion is not reduced: its lower body fits fully (`Pace.feel`). */
  get roomMoves(): boolean { return this.moved && !this.still }

  /** One frame: go where it is going, face what it attends to, and move as its pose says. */
  frame(dt: number): BotFrame {
    const p = this.root.position
    // The scene may put a Bot that stands somewhere else, when the land is laid out again.
    if (!this.walk.walking) {
      // Where it stood after the last frame is in `walk`: the way from there, in body heights a second.
      const dx = p.x - this.walk.x, dz = p.z - this.walk.z
      const size = BOT_H * this.root.scale.y, way = Math.hypot(dx, dz) / Math.max(size, 1e-6)
      const pace = this.moved && !this.still && dt > 0 && way < PUT.jump ? way / dt : 0
      this.put = pace > (this.put ? PUT.stop : PUT.go)
      // The way it is put, in its own frame (it faces +z): it leans that way, whatever it faces (D118 4).
      if (this.put) { const f = this.walk.facing, c = Math.cos(f), s = Math.sin(f), n = Math.hypot(dx, dz) || 1; this.way[0] = (dx * s + dz * c) / n; this.way[1] = (dx * c - dz * s) / n }
      this.walk.x = p.x; this.walk.z = p.z
    } else this.put = false
    this.walk.step(dt, this.speed, this.lookAt ? Math.atan2(this.lookAt.x - p.x, this.lookAt.z - p.z) : this.restYaw, this.still)
    p.x = this.walk.x
    p.z = this.walk.z
    // What rides with the body stands where the body is after this step, not where it was.
    for (const rider of this.riders) rider.position.copy(p)
    this.root.rotation.y = this.walk.facing
    const moving = this.walk.moving || this.put
    // Its own walk faces the way it goes.
    if (this.walk.moving) { this.way[0] = 1; this.way[1] = 0 }
    const pose = { carrying: !!this.carried, listening: this.listening, aiming: this.aiming, forced: this.forced, sleeping: this.sleeping, state: this.state }
    this.poseNow = poseFor({ moving, ...pose })
    // A change of pose that only the room's move makes brings no blink (M99): the blink reads the pose it would
    // have if the room did not move it.
    const own = this.put ? poseFor({ moving: this.walk.moving, ...pose }) : this.poseNow
    return this.motion.step(dt, this.poseNow, { walking: moving, speaking: this.speaking, still: this.still, own, way: this.way })
  }
}
