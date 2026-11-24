/**
 * The feel of a light sheet over a ghost, on the body as it is.
 *
 * Nothing here moves a point of the body at rest. When a Bot moves, its lower body, the lobes of its hem and its
 * flick give softly and trail a little; when it stops they swing on and settle; when it drops they lift; when it
 * stands still they breathe. Everything above `FLOW.top` of the height stays calm, so the eyes and what rides on
 * the head do not move.
 *
 * The driver is a set of followers. A follower is a small weight that rests at a place on the body (the middle of
 * the lower body, the hem, the tip of the flick). A spring pulls it to its place, and still air holds it back. So
 * it lags behind a body that moves (by its speed times a constant), swings past its place when the body stops, and
 * settles. How far each follower is from its place is all that the body's vertex shader reads: it bends the lower
 * body by a few lines of maths, with no new data in the model file.
 *
 * Lengths are in body heights, in the body's own space at rest, unless a line says "world".
 */
import {
  Matrix4, Vector2, Vector3, Vector4,
  type Bone, type BufferAttribute, type Material, type Object3D, type SkinnedMesh,
} from 'three'
import type { BotPose } from './body'
import { bendInto, bendKey } from './botShader'

/** How far it goes: the same motion, scaled. The page uses the middle. */
export const AMOUNTS = { little: 0.5, middle: 1, lot: 1.7 } as const

export const FLOW = {
  // Above this height nothing gives: the face, the eyes and what rides on the head stay calm.
  top: 0.58,
  // Where the two followers of the lower body rest: half way down, and at the hem. (The body leans back a little
  // toward its hem, so its middle line is not quite at z = 0.)
  mid: [0, 0.26, -0.017], hem: [0, 0, -0.03],
  // A follower: how many swings a second it would make in no air, and how much the air holds it (1 would stop it
  // dead; under 0.3 it wobbles on like jelly). Its lag behind a moving body is speed * air / (pi * swings).
  // The hem is slow and lightly held: it swings on past its place by a third of its lag, comes back by a tenth,
  // and is still. (At 1.7 swings a second and air 0.42 the swing at a stop is over before it is seen.)
  midHz: 2.2, midAir: 0.4,
  hemHz: 1.4, hemAir: 0.34,
  flickHz: 1.8, flickAir: 0.34,
  twistHz: 1.5, twistAir: 0.5,
  most: 0.3,          // no follower goes further from its place than this
  // Cloth over a ghost is pressed onto it on the side that leads and streams off it on the side that trails. So
  // the side that trails gives this share more than the middle, and the side that leads this share less.
  trail: 0.25,
  soft: 0.15,         // how far down (a share of the way to the hem) the giving takes to reach its full bend
  tilt: 0.6,          // how much of a true bend's tilt each level takes (0 would be a shear: the hem stays level)
  flickMost: 0.12,    // the flick's tip, from where the bend of the body puts it
  // Up and down a Bot is quick: a hop leaves at 1.9 body heights a second, a walk goes at 1.4. A lower body that
  // gave as much up and down as it does sideways would go flat as a pancake when a Bot lands, and long as taffy
  // when it rises. So up and down it gives this share, and no further than `lift` (pushed up) and `stretch`
  // (drawn down).
  rise: 0.35, lift: 0.045, stretch: 0.085,
  // A turn in place: the lower body comes round this share of the way behind, and never more than `twistMost` (radians).
  twist: 0.45, twistMost: 0.32,
  // A body that is turned faster than this (radians a second) was not turned, it was put: the page faces a Bot
  // the way it walks at once, in one frame. The cloth is then carried round with the body, it does not tear.
  snap: 12,
  // When the hem is lifted (the body drops) the lower body widens by this share of the lift for each body height
  // of its own width; when the hem is pulled down (the body rises) it narrows.
  air: 1.6,
  // Still air is never quite still. A slow pull on every follower (body heights a second, squared), and a slow
  // swell of the lower body (a share of its width). A draught of 2 sways the hem by 0.04 body heights, as much as
  // the body's own lean; at 1.5 it is 0.03.
  draught: 1.5, breath: 0.03, breathHz: 0.27,
  // The numbers above were set for the land's walk, 1.4 body heights a second. The room of places walks a Bot at
  // 3.3 and a hand carries one at 6.7: there those numbers trail twice as far and swing on three times. So the
  // lower body fits the pace it last had (`Pace`): from `fitFrom` body heights a second it starts to fit, from
  // `fitFull` it fits fully. A fitted follower feels the body's speed only up to `fitMps` (body heights a second):
  // past that, the cloth streams at its trail and is carried with the body. And its air goes up to `fitAir`, so it
  // swings past its place once at a stop, a little, and is still. The pace it had fades over `fitKeep` seconds after
  // a stop, so that the swing it settles from is the fitted one (at 1.2 s or more it does not swing at all). Slower
  // than `fitFrom`, nothing changes. At the room's walk the hem then trails 0.22 and swings past by 0.01.
  fitFrom: 1.6, fitFull: 2.6, fitMps: 0.7, fitAir: 0.9, fitKeep: 0.8,
} as const
export type FlowSet = typeof FLOW

/** How each pose breathes: how much, and how fast. A Bot that slumps hangs limp; one that works flutters. */
export const FLOW_POSE: Record<BotPose, { breath: number; rate: number }> = {
  rest: { breath: 1, rate: 1 }, stand: { breath: 0.8, rate: 1 }, work: { breath: 0.7, rate: 2.1 }, think: { breath: 0.9, rate: 0.8 },
  ask: { breath: 1.5, rate: 1.4 }, slump: { breath: 0.2, rate: 0.5 }, listen: { breath: 0.5, rate: 0.8 }, point: { breath: 0.6, rate: 1 },
  sleep: { breath: 0.6, rate: 0.4 },
}

const clamp = (x: number, a: number, b: number) => Math.min(b, Math.max(a, x))

/**
 * How much of the middle's lag and of the hem's a point takes, at `t` of the way down from where the giving starts
 * (0) to the hem (1). The shader has the same lines: see there for why the curve is what it is. At the top a point
 * takes nothing, half way down all of the middle's and none of the hem's, at the hem all of the hem's.
 */
export function bendAt(t: number, soft: number = FLOW.soft, half: number = bendAt(0.5, soft, 0)[1]): [number, number] {
  const b = (1 + soft) * t ** 3 / (t + soft), a = 64 * (t * (1 - t)) ** 3
  return [a, b - half * a]
}

const v1 = new Vector3(), v2 = new Vector3(), v3 = new Vector3(), m1 = new Matrix4(), m2 = new Matrix4(), m3 = new Matrix4()
const UP = new Vector3(0, 1, 0)

/**
 * How far the lower body fits a fast pace, 0 (not at all) to 1 (fully), and what that does to a follower: the
 * share of the way its air goes to `FLOW.fitAir`, and the most speed it feels (world units a second).
 */
export interface Fit { share: number; air: number; mps: number }
export const NO_FIT: Fit = { share: 0, air: 0, mps: Infinity }

/**
 * The pace a body had lately, in body heights a second, read from where it stands each frame (across the ground
 * only: a hop is no pace). It is the fastest of the last moments, fading over `FLOW.fitKeep` seconds.
 */
export class Pace {
  felt = 0
  private x = 0
  private z = 0
  private live = false
  private readonly fit: Fit = { share: 0, air: 0, mps: Infinity }

  /**
   * The body stands at `x`, `z` (world) after `dt` seconds; `size` is one body height in world units. `moved`: the
   * room walks it or his hand carries it now (`BotDrive.roomMoves`): then it fits fully at any pace, so a short walk
   * or a slow hand settles as the room's full walk does (M101). The land's own walk and a Bot at rest fit by their pace.
   */
  feel(x: number, z: number, dt: number, size: number, k: FlowSet = FLOW, moved = false): Fit {
    const now = this.live && dt > 0 && size > 0 ? Math.hypot(x - this.x, z - this.z) / dt / size : 0
    this.live = true; this.x = x; this.z = z
    // A body that was put far away at once (a room laid out again) had no pace: the followers snap there (`hold`).
    const pace = Math.max(now * dt > k.most * 4 ? 0 : now, moved ? k.fitFull : 0)
    this.felt = Math.max(pace, this.felt * Math.exp(-dt / k.fitKeep))
    const u = clamp((this.felt - k.fitFrom) / (k.fitFull - k.fitFrom), 0, 1), share = u * u * (3 - 2 * u)
    const f = this.fit
    f.share = share; f.air = k.fitAir; f.mps = share > 0 ? (k.fitMps * size) / share : Infinity
    return f
  }

  reset(): void { this.live = false; this.felt = 0 }
}

/** A weight on a spring, in still air. It lives in the world: that is what makes it lag. */
export class Follower {
  readonly x = new Vector3()
  readonly v = new Vector3()
  /** Where its place was at the last step (world). */
  readonly at = new Vector3()
  live = false
  private readonly w: number
  private readonly air: number
  /** Each follower feels the draught at its own time, so no two parts of a body sway in step. */
  private readonly p: readonly [number, number, number]

  constructor(hz: number, air: number, seed = 0) {
    this.w = 2 * Math.PI * hz
    this.air = air
    this.p = [0.7 + (seed * 0.37) % 1, 1.9 + (seed * 0.73) % 2, 3.1 + (seed * 1.31) % 3]
  }

  /**
   * Go toward `to` (world) for dt seconds. `push` is the draught's strength at time `t`, in world units a second
   * squared. `fit` is how far it fits a fast pace (`Pace.feel`).
   */
  step(to: Vector3, dt: number, push = 0, t = 0, fit: Fit = NO_FIT): Vector3 {
    if (!this.live) { this.live = true; this.v.set(0, 0, 0); this.at.copy(to); return this.x.copy(to) }
    const air = this.air + fit.share * Math.max(0, fit.air - this.air)
    // The more air holds it, the further it would trail: it feels its place's speed less by as much, so that a
    // fitted follower never trails further than it did. And never faster than `fit.mps`. What its place moved past
    // the speed it feels, it is carried with.
    const from = this.at, moved = v3.copy(to).sub(from), far = moved.length(), speed = dt > 0 ? far / dt : 0
    const feels = Math.min(speed * (this.air / air), fit.mps), carry = speed > feels ? 1 - feels / speed : 0
    // Steps of 1/120 s or less, whatever the frame rate: the spring is the same at 30 frames a second and at 90. Its
    // place goes the way it went in this frame step by step, so a late frame is not a place that stood still.
    const n = Math.max(1, Math.ceil(dt * 120)), h = dt / n, k = this.w * this.w, c = 2 * air * this.w, x = this.x, v = this.v, p = this.p
    const gx = push * Math.sin(0.9 * t + p[0]), gy = push * 0.35 * Math.sin(1.3 * t + p[1]), gz = push * Math.sin(0.7 * t + p[2])
    for (let i = 1; i <= n; i += 1) {
      const s = i / n, tx = from.x + moved.x * s, ty = from.y + moved.y * s, tz = from.z + moved.z * s
      if (carry) { x.x += (moved.x / n) * carry; x.y += (moved.y / n) * carry; x.z += (moved.z / n) * carry }
      v.x += (k * (tx - x.x) - c * v.x + gx) * h; v.y += (k * (ty - x.y) - c * v.y + gy) * h; v.z += (k * (tz - x.z) - c * v.z + gz) * h
      x.x += v.x * h; x.y += v.y * h; x.z += v.z * h
    }
    this.at.copy(to)
    return x
  }

  /** Never further from its place than `most` (world): a body that is put somewhere else at once does not tear. */
  hold(to: Vector3, most: number): void {
    const d = v3.copy(this.x).sub(to), far = d.length()
    if (far > most * 4) { this.x.copy(to); this.v.set(0, 0, 0) } else if (far > most) this.x.copy(to).addScaledVector(d, most / far)
  }
}

/** An angle that follows another, as a follower does. */
class Turner {
  x = 0
  live = false
  private v = 0
  private readonly w: number
  private readonly c: number

  constructor(hz: number, air: number) { this.w = 2 * Math.PI * hz; this.c = 2 * air * this.w }

  step(to: number, dt: number): number {
    if (!this.live) { this.live = true; this.v = 0; return (this.x = to) }
    const n = Math.max(1, Math.ceil(dt * 120)), h = dt / n
    for (let i = 0; i < n; i += 1) { this.v += (this.w * this.w * (to - this.x) - this.c * this.v) * h; this.x += this.v * h }
    return this.x
  }
}

/** The parts of a Bot's rig that the flow reads. `root` is any object that holds the whole body. */
/** `flick2` is the bone of the second flick, on the body's other side. A body file of one flick has none. */
export interface FlowBot { root: Object3D; body: SkinnedMesh; belly: Bone; flick: Bone | null; flick2?: Bone | null }

/** The numbers the shader reads, written once a frame. Two materials of one Bot share them. */
export interface FlowUniforms {
  flowMid: { value: Vector3 }
  flowHem: { value: Vector3 }
  flowTurn: { value: Vector4 }
  flowSides: { value: Vector2 }
  flowFlick: { value: Vector3 }
  flowHinge: { value: Vector4 }
  flowFlick2: { value: Vector3 }
  flowHinge2: { value: Vector4 }
}

/** One flick of a body: its bone, where its tip is at rest, the follower of that tip, and the numbers it writes. */
interface Flick { bone: Bone; inv: Matrix4; tip: Vector3; follower: Follower; lag: { value: Vector3 }; clock: number }

/** The materials that carry the bend, with their numbers: the lines go into a shader once. */
const wired = new WeakMap<Material, FlowUniforms>()

/**
 * Put the bend into a three material's vertex shader. It is done before the bones move the point, so the bones
 * work as they do without it. Returns the numbers to write each frame. `shared` is the numbers of another material
 * of the same Bot: the flat art that lies on the body's front must bend exactly as the body under it.
 * A second call for the same material changes nothing and returns the numbers of the first.
 */
export function flowInto(material: Material, k: FlowSet = FLOW, shared: FlowUniforms | null = null): FlowUniforms {
  const done = wired.get(material)
  if (done) return done
  const u: FlowUniforms = shared ?? {
    flowMid: { value: new Vector3() }, flowHem: { value: new Vector3() }, flowTurn: { value: new Vector4(0, 0, 0, 0) },
    flowSides: { value: new Vector2(1, 0) }, flowFlick: { value: new Vector3() }, flowHinge: { value: new Vector4(0, 0, 0, 1) },
    flowFlick2: { value: new Vector3() }, flowHinge2: { value: new Vector4(0, 0, 0, 1) },
  }
  const before = material.onBeforeCompile, key = material.customProgramCacheKey
  const set = { top: k.top, lean: (k.hem[2] - k.mid[2]) / (k.hem[1] - k.mid[1]), trail: k.trail, soft: k.soft, tilt: k.tilt, half: bendAt(0.5, k.soft, 0)[1] }
  material.onBeforeCompile = function (shader, renderer) {
    before.call(this, shader, renderer)
    Object.assign(shader.uniforms, u)
    if (!bendInto(shader, set)) throw new Error('the vertex shader has no place for the bend')
  }
  material.customProgramCacheKey = function () { return bendKey(key.call(this), set) }
  material.needsUpdate = true
  wired.set(material, u)
  return u
}

/**
 * The flow of one Bot: its followers, and the numbers its skin's shader reads.
 * Make it once the skin is on the body, and make it again when the skin changes: it puts its lines into that
 * material's shader. Step it last in the frame: it reads where the bones and the Bot are now.
 */
export class WaveFlow {
  readonly u: FlowUniforms
  private readonly k: FlowSet
  private readonly gain: number
  private readonly bellyInv: Matrix4
  private readonly flicks: Flick[] = []
  private readonly A = new Matrix4()
  private readonly Ainv = new Matrix4()
  private readonly pMid: Vector3
  private readonly pHem: Vector3
  private readonly fMid: Follower
  private readonly fHem: Follower
  /** Every follower of this Bot. A body that is put round at once carries them all with it. */
  private readonly followers: Follower[]
  private readonly tMid: Turner
  private readonly tHem: Turner
  private readonly mid = new Vector3()
  private readonly hem = new Vector3()
  private readonly twist: [number, number] = [0, 0]
  private yaw: number | null = null
  /** The pace the body had lately, and how far the followers fit it in this frame. */
  private readonly pace = new Pace()
  private fit: Fit = NO_FIT
  private t: number
  private phase: number
  private breath = 1
  private size = 1

  constructor(private readonly bot: FlowBot, o: { amount?: keyof typeof AMOUNTS | number; name?: string } = {}) {
    const k = this.k = FLOW
    this.gain = typeof o.amount === 'number' ? o.amount : AMOUNTS[o.amount ?? 'middle']
    this.bellyInv = this.inverse('belly')
    this.pMid = new Vector3(...k.mid)
    this.pHem = new Vector3(...k.hem)
    this.fMid = new Follower(k.midHz, k.midAir, 1)
    this.fHem = new Follower(k.hemHz, k.hemAir, 2)
    this.followers = [this.fMid, this.fHem]
    this.tMid = new Turner(k.twistHz * 1.3, k.twistAir)
    this.tHem = new Turner(k.twistHz, k.twistAir)
    // No two Bots breathe in step: each starts its clocks at a time taken from its name.
    this.t = [...(o.name ?? '')].reduce((s, ch) => s + ch.charCodeAt(0), 0) % 17
    this.phase = this.t
    this.u = flowInto(bot.body.material as Material, k)
    this.flick(bot.flick, 'flick', this.u.flowFlick, this.u.flowHinge, 3, 9)
    this.flick(bot.flick2 ?? null, 'flick2', this.u.flowFlick2, this.u.flowHinge2, 4, 13)
  }

  /** Take up one flick of the body, when the body has it: its tip gets a follower of its own. */
  private flick(bone: Bone | null, name: string, lag: { value: Vector3 }, hang: { value: Vector4 }, seed: number, clock: number): void {
    if (!bone) return
    const sk = this.bot.body.skeleton, at = sk.bones.findIndex(b => b.name === name), tip = at < 0 ? null : this.tip(at)
    if (!tip) return
    const inv = sk.boneInverses[at]
    // Where the flick's bone is in the body at rest: its bind matrix only moves it there and back.
    const e = inv.elements, hinge = new Vector3(-e[12], -e[13], -e[14])
    hang.value.set(hinge.x, hinge.y, hinge.z, 1 / hinge.distanceTo(tip))
    const follower = new Follower(this.k.flickHz, this.k.flickAir, seed)
    this.followers.push(follower)
    this.flicks.push({ bone, inv, tip, follower, lag, clock })
  }

  private inverse(name: string): Matrix4 {
    const sk = this.bot.body.skeleton, i = sk.bones.findIndex(b => b.name === name)
    if (i < 0) throw new Error(`the body has no bone "${name}"`)
    return sk.boneInverses[i]
  }

  /** A flick's tip at rest: the point of the body that follows that flick's bone (number `bone` of the body's bones) most, lowest first. */
  private tip(bone: number): Vector3 | null {
    const g = this.bot.body.geometry, pos = g.attributes.position as BufferAttribute
    const si = g.attributes.skinIndex as BufferAttribute, sw = g.attributes.skinWeight as BufferAttribute
    let best = -1, at: Vector3 | null = null
    for (let i = 0; i < pos.count; i += 1) {
      let w = 0
      for (let c = 0; c < 4; c += 1) if (si.getComponent(i, c) === bone) w += sw.getComponent(i, c)
      // Far out and low: the tip.
      const score = w * (Math.abs(pos.getX(i)) - pos.getY(i))
      if (w > 0.6 && score > best) { best = score; at = new Vector3(pos.getX(i), pos.getY(i), pos.getZ(i)) }
    }
    return at
  }

  /**
   * Read where the body is now and move the followers of the lower body. After it, `mid` and `hem` say how far the
   * middle of the lower body and its hem are from their places (in the body's own space), `twist` how far each is
   * turned behind the body.
   */
  private sense(dt: number, pose: BotPose, moved: boolean): void {
    const k = this.k, bot = this.bot, mood = FLOW_POSE[pose] ?? FLOW_POSE.rest
    this.t += dt
    // The page moves a Bot's stand in this same frame, before any matrix is worked out: work them out here.
    bot.root.updateWorldMatrix(true, true)
    // From the body's own space at rest to the world, for what is stiff on the belly: its lean is in it.
    const before = m3.copy(this.Ainv)
    const A = this.A.multiplyMatrices(bot.belly.matrixWorld, this.bellyInv)
    this.Ainv.copy(A).invert()
    const e = A.elements, det = A.determinant(), size = Math.cbrt(Math.abs(det))   // one body height, in world units
    // The page mirrors a body whose art faces the other way. A turn that goes one way round in the world goes the
    // other way round in a mirrored body's own space.
    const mirror = det < 0 ? -1 : 1
    this.size = size
    // Which way the body faces, as an angle that never jumps back by a whole turn.
    const yaw = Math.atan2(e[8], e[10])
    if (this.yaw === null) this.yaw = yaw
    else {
      const turn = Math.atan2(Math.sin(yaw - this.yaw), Math.cos(yaw - this.yaw))
      if (dt > 0 && Math.abs(turn) > k.snap * dt) {
        // Put, not turned: every follower keeps its place on the body.
        const carry = before.premultiply(A)
        for (const f of this.followers) if (f.live) { f.x.applyMatrix4(carry); f.at.applyMatrix4(carry); f.v.applyAxisAngle(UP, turn) }
        this.tMid.x += turn; this.tHem.x += turn
      }
      this.yaw += turn
    }
    this.breath += (mood.breath - this.breath) * (1 - Math.exp(-dt * 3))
    const push = k.draught * this.breath * size
    const r = bot.root.matrixWorld.elements, fit = this.fit = this.pace.feel(r[12], r[14], dt, size, k, moved)
    const mid = v1.copy(this.pMid).applyMatrix4(A)
    this.fMid.step(mid, dt, push, this.t, fit); this.fMid.hold(mid, k.most * size)
    // The hem hangs from the middle, so it takes the middle's lag and adds its own: it is the last to arrive.
    const hem = v2.copy(this.pHem).sub(this.pMid)
    hem.set(e[0] * hem.x + e[4] * hem.y + e[8] * hem.z, e[1] * hem.x + e[5] * hem.y + e[9] * hem.z, e[2] * hem.x + e[6] * hem.y + e[10] * hem.z).add(this.fMid.x)
    this.fHem.step(hem, dt, push, this.t + 5, fit); this.fHem.hold(hem, k.most * size)
    this.mid.copy(this.fMid.x).applyMatrix4(this.Ainv).sub(this.pMid)
    this.hem.copy(this.fHem.x).applyMatrix4(this.Ainv).sub(this.pHem)
    this.mid.y = clamp(this.mid.y * k.rise, -k.stretch, k.lift); this.hem.y = clamp(this.hem.y * k.rise, -k.stretch, k.lift)
    this.twist[0] = mirror * clamp(k.twist * (this.tMid.step(this.yaw, dt) - this.yaw), -k.twistMost, k.twistMost)
    this.twist[1] = mirror * clamp(k.twist * (this.tHem.step(this.tMid.x, dt) - this.yaw), -k.twistMost, k.twistMost)
    this.phase += dt * 2 * Math.PI * k.breathHz * mood.rate   // the clock of the breath
  }

  /** One frame. Call it after the Bot's bones are set. */
  step(dt: number, pose: BotPose, moved = false): void {
    this.sense(dt, pose, moved)
    const k = this.k, g = this.gain, u = this.u, size = this.size
    u.flowMid.value.copy(this.mid).multiplyScalar(g)
    u.flowHem.value.copy(this.hem).multiplyScalar(g)
    const swell = k.breath * this.breath * Math.sin(this.phase * 0.5)
    u.flowTurn.value.set(this.twist[0] * g, this.twist[1] * g, k.air * this.hem.y * g, swell * g)
    u.flowSides.value.set(Math.cos(this.phase * 0.31), Math.sin(this.phase * 0.31))
    for (const f of this.flicks) {
      // A flick's tip trails on its own, further than the flank it sits on.
      const F = m1.multiplyMatrices(f.bone.matrixWorld, f.inv)
      const place = v1.copy(f.tip).applyMatrix4(F)
      f.follower.step(place, dt, k.draught * this.breath * size, this.t + f.clock, this.fit); f.follower.hold(place, k.most * size)
      const lag = v2.copy(f.follower.x).applyMatrix4(m2.copy(F).invert()).sub(f.tip)
      // What the lower body does at the tip's height is in the bend already.
      const [a, b] = bendAt(clamp((k.top - f.tip.y) / k.top, 0, 1), k.soft)
      lag.y *= k.rise
      lag.addScaledVector(this.mid, -a).addScaledVector(this.hem, -b).clampLength(0, k.flickMost)
      f.lag.value.copy(lag).multiplyScalar(g)
    }
  }

  /** Reduced motion, or a Bot that is put down afresh: everything is at its place at once, and the body is as the file has it. */
  rest(): void {
    for (const f of this.followers) f.live = false
    this.pace.reset(); this.fit = NO_FIT
    this.tMid.live = false; this.tHem.live = false
    this.mid.set(0, 0, 0); this.hem.set(0, 0, 0); this.twist[0] = this.twist[1] = 0; this.yaw = null
    const u = this.u
    u.flowMid.value.set(0, 0, 0); u.flowHem.value.set(0, 0, 0); u.flowFlick.value.set(0, 0, 0); u.flowFlick2.value.set(0, 0, 0)
    u.flowTurn.value.set(0, 0, 0, 0); u.flowSides.value.set(1, 0)
  }
}
