/**
 * A scene document and the Bots that live in it, as the state the page draws from.
 *
 * It keeps what the document says and nothing else: for each object where
 * it stands, by its anchor and zone, and the value of the real field it is
 * bound to. Give it another document and the world changes on the next
 * frame. It knows no scene by name, and it draws nothing: `Land.tsx` and
 * `Bot.tsx` draw from it.
 *
 * The Bots are not objects of a document. They are the crew, so they are
 * here in every scene, each on its own zone, and everything the interaction
 * layer needs from the world goes through this class: where the land is and
 * how big, which Bot a ray is near, which object, which point of the ground.
 */
import { Group, Object3D, Vector3, type Texture } from 'three'
import type { BotState } from './body'
import { BotDrive } from './botDrive'
import { seedOf } from './botMotion'
import { tintOf } from './character'
import { faceArtFor } from './faces'
import type { XrGlance } from './glance'
import {
  PLOT_M, botHeight, follow, landBounds, landPose, minScale, panBy, plotAt, plotCentre, settleT, visitorSpot, zoomBy,
  type LandBounds, type LandView,
} from './land'
import { DEG } from './metrics'
import type { XrBot } from './model'
import type { XrPalette } from './palette'
import { BOT_SPOT, idleLife } from './plots'
import type { Candidate } from './pointTalk'
import { boundValue, type BoundValue, type SceneDoc, type SceneObject } from './sceneDoc'
import { Surface, loadFace } from './sceneParts'

export interface WorldColors { state: Record<BotState, number> }

/** What a component needs to draw a Bot's body (`Bot.tsx`): its colour, what it wears, and which way its art faces. */
export interface BotLook { tint: number; wear: Texture | null; flip: boolean }

export interface BotNode {
  bot: XrBot
  /** What the Bot does and where it stands, once its art is loaded. It is not drawn here: a component draws it (`Bot.tsx`). */
  body: BotDrive | null
  /** Set with the body. */
  look: BotLook | null
  name: Surface
  /** The Bot's face as the dashboard shows it, for page headers. */
  thumb: HTMLCanvasElement | null
  artKey: string
  /** Where the outline of its plot lies, on the land. */
  outline: Object3D
  idleKey: string
  hopAt: number
}

/** One object of the document: what it is, the value of the field it is bound to, and where it stands. */
interface Drawn { o: SceneObject; at: Object3D; value: BoundValue }

const HOP_EVERY_MS = 2600
/** An object can be pointed at within this angle of its middle, however small it is drawn. */
export const OBJECT_PICK_DEG = 1.6

export class World {
  /** Everything that scales with the land. */
  readonly land = new Group()
  /** Everything that stays around the person. */
  readonly seat = new Group()
  doc: SceneDoc = { id: '', env: { ground: 'none', horizon: false }, zones: [], objects: [], pages: [] }
  view: LandView = { t: 0, fx: 0, fz: 0 }
  goal: LandView = { t: 0, fx: 0, fz: 0 }
  bounds: LandBounds = { cx: 0, cz: 0, radius: PLOT_M }
  sMin = 1
  scale = 1
  readonly nodes = new Map<string, BotNode>()
  private readonly drawn = new Map<string, Drawn>()
  private glances: ReadonlyMap<string, XrGlance> = new Map()
  private zonesKey = ''
  /** Reduced motion, and the wall clock in seconds, as of the last frame: for the parts that draw. */
  still = false
  nowS = 0
  private placed = false
  private disposed = false
  /** Counts up when what is drawn must be made again: who is in the crew, what state each is in, how each looks, how the plots lie. */
  stamp = 0
  private readonly watchers = new Set<() => void>()

  constructor(readonly colors: WorldColors, private readonly palette: XrPalette, private readonly icons: ReadonlyMap<string, CanvasImageSource>) {}

  /* ---------------- the document ---------------- */

  /** Draw this document. Objects are matched by id: new ones appear, changed ones change, missing ones go. */
  setDoc(doc: SceneDoc): void {
    this.doc = doc
    const seen = new Set<string>()
    // Which things there are, and of what kind: the parts that draw them are told when that changes.
    let fresh = false
    for (const o of doc.objects) {
      seen.add(o.id)
      let d = this.drawn.get(o.id)
      if (!d) { d = { o, at: new Object3D(), value: boundValue(null, undefined, null) }; this.drawn.set(o.id, d) }
      if (!d.at.parent || d.o.kind !== o.kind) fresh = true
      d.o = o
      const parent = o.anchor === 'seat' ? this.seat : this.land
      if (d.at.parent !== parent) parent.add(d.at)
      const [zx, zz] = o.anchor === 'land' ? this.zoneCentre(o.zone) : [0, 0]
      d.at.position.set(zx + o.pos[0], o.pos[1], zz + o.pos[2])
      d.at.rotation.y = o.yaw * DEG
      d.at.scale.setScalar(o.scale)
    }
    for (const [id, d] of this.drawn) if (!seen.has(id)) { d.at.removeFromParent(); this.drawn.delete(id); fresh = true }
    const key = JSON.stringify([doc.zones, doc.env])
    if (key !== this.zonesKey) { this.zonesKey = key; this.layZones() } else if (fresh) this.changed()
    this.resolve()
  }

  private zoneCentre(zone: string): [number, number] {
    const z = this.doc.zones.find(x => x.id === zone)
    return z ? plotCentre(z.cell) : [0, 0]
  }

  private layZones(): void {
    const slugs = this.doc.zones.map(z => z.owner)
    const map = Object.fromEntries(this.doc.zones.map(z => [z.owner, z.cell]))
    this.bounds = landBounds(map, slugs)
    this.sMin = minScale(this.bounds.radius)
    if (!this.placed && slugs.length) {
      this.placed = true
      this.view = { t: 0, fx: this.bounds.cx, fz: this.bounds.cz }
      this.goal = { ...this.view }
    }
    this.changed()
    for (const n of this.nodes.values()) this.settle(n)
  }

  /* ---------------- the crew ---------------- */

  /** Hear when `stamp` counts up. Returns the call that stops it. */
  readonly watch = (fn: () => void): (() => void) => { this.watchers.add(fn); return () => { this.watchers.delete(fn) } }
  private changed(): void { this.stamp += 1; for (const fn of this.watchers) fn() }

  setCrew(bots: readonly XrBot[], glances: ReadonlyMap<string, XrGlance>): void {
    this.glances = glances
    const seen = new Set<string>()
    for (const bot of bots) {
      seen.add(bot.slug)
      const node = this.nodes.get(bot.slug) ?? this.addNode(bot)
      node.bot = bot
      node.name.dirty = true
      node.body?.setState(bot.state)
      this.dress(node)
    }
    for (const [slug, node] of this.nodes) {
      if (seen.has(slug)) continue
      node.name.dispose()
      node.outline.removeFromParent()
      this.nodes.delete(slug)
    }
    this.resolve()
    this.changed()
  }

  private addNode(bot: XrBot): BotNode {
    const name = new Surface(`name:${bot.slug}`, 420, 72, 0.26, this.palette, this.icons, 9)
    const node: BotNode = { bot, body: null, look: null, name, thumb: null, artKey: '', outline: new Object3D(), idleKey: '', hopAt: 0 }
    this.nodes.set(bot.slug, node)
    this.land.add(node.outline)
    this.seat.add(name.at)
    this.settle(node)
    return node
  }

  /** Where a Bot stands when it has nothing else to do, in land metres. */
  private spot(slug: string): Vector3 {
    const [x, z] = this.zoneCentre(slug)
    return new Vector3(x + BOT_SPOT[0], 0, z + BOT_SPOT[1])
  }

  private settle(node: BotNode): void {
    const [x, z] = this.zoneCentre(node.bot.slug)
    node.outline.position.set(x, 0, z)
    node.idleKey = ''
    if (node.body && !node.body.walking) node.body.root.position.copy(this.spot(node.bot.slug))
  }

  /** Give the node its body, with the colour and the art the dashboard shows for this Bot. */
  private dress(node: BotNode): void {
    const { bot } = node
    const artKey = JSON.stringify([bot.name, bot.avatar ?? null])
    if (artKey === node.artKey) return
    node.artKey = artKey
    const art = faceArtFor(bot.avatar, bot.name)
    void Promise.all([loadFace(art), tintOf(art.layers.find(l => l.id === 'tile')?.uri)]).then(([tx, tint]) => {
      if (this.disposed || node.artKey !== artKey || !this.nodes.has(bot.slug)) return
      node.thumb = tx.thumb
      node.look = { tint, wear: tx.layers.find(l => l.id === 'extra')?.tex ?? null, flip: art.flip }
      // The body keeps what it was doing when only its look changes.
      if (!node.body) {
        node.body = new BotDrive(seedOf(bot.slug))
        node.body.setState(node.bot.state)
        node.body.root.position.copy(this.spot(bot.slug))
      }
      this.changed()
    })
  }

  /** Read every bound object's field again. */
  private resolve(): void {
    for (const d of this.drawn.values()) d.value = boundValue(d.o.bind, d.o.bind ? this.nodes.get(d.o.bind.bot)?.bot : undefined, d.o.bind ? this.glances.get(d.o.bind.bot) : null)
  }

  /* ---------------- per frame ---------------- */

  /**
   * `eyeY` is how high the eyes are above the floor, `head` the head in the
   * seat's space, `attend` the Bot a finger rests on. `clock` is in ms for
   * motion, `nowS` is the wall clock in seconds.
   */
  update(dt: number, clock: number, nowS: number, still: boolean, eyeY: number, head: Vector3, attend: string): void {
    this.view = follow(this.view, this.goal, dt, still)
    const pose = landPose(this.view, this.sMin, eyeY)
    this.scale = pose.s
    this.land.position.set(pose.x, pose.y, pose.z)
    this.land.scale.setScalar(pose.s)
    this.still = still
    this.nowS = nowS

    const headLand = head.clone().sub(this.land.position).divideScalar(pose.s)
    const seconds = Math.floor(clock / 1000)
    for (const node of this.nodes.values()) {
      const { bot, body } = node
      if (!body) { node.name.at.visible = false; continue }
      body.root.scale.setScalar(botHeight(pose.s) / (pose.s * body.height))
      const attended = attend === bot.slug
      body.listen(attended ? headLand : null)
      const home = this.spot(bot.slug)
      if (bot.state === 'needs') {
        // It hops so it is found, and holds a hand out at what it asks about.
        if (clock - node.hopAt > HOP_EVERY_MS) { node.hopAt = clock; body.hop() }
        const ask = [...this.drawn.values()].find(d => d.o.zone === bot.slug && d.o.kind === 'ask')
        body.pointAt(ask ? ask.at.position : null)
      } else body.pointAt(null)
      if (bot.state === 'idle' && !attended) {
        const [zx, zz] = this.zoneCentre(bot.slug)
        const mine = [...this.drawn.values()].filter(d => d.o.zone === bot.slug && d.o.anchor === 'land').map(d => ({ kind: d.o.kind as string, x: d.o.pos[0], z: d.o.pos[2] }))
        const step = idleLife(bot.slug, mine, seconds)
        const key = `${step.act}:${step.x.toFixed(2)}:${step.z.toFixed(2)}`
        if (key !== node.idleKey) {
          node.idleKey = key
          body.walkTo(new Vector3(zx + step.x, 0, zz + step.z))
          body.setPose(step.act === 'tend' ? 'work' : step.act === 'look' ? 'stand' : null)
          body.sleep(step.act === 'nap')
        }
      } else if (node.idleKey || body.root.position.distanceTo(home) > 0.05) {
        if (node.idleKey) { node.idleKey = ''; body.setPose(null); body.sleep(false); body.walkTo(home) }
      }
      body.update(dt, clock, still)

      // The name: always in the miniature, so every plot can be told apart, and for the Bot a finger rests on.
      const shown = this.view.t < 0.6 || attended
      node.name.at.visible = shown
      if (!shown) continue
      const [zx, zz] = this.zoneCentre(bot.slug)
      const lz = 2.2 + (0.9 - 2.2) * this.view.t
      const at = new Vector3(zx, 0.02, zz + lz).multiplyScalar(pose.s).add(this.land.position)
      at.y += 0.012
      node.name.at.position.copy(at)
      node.name.at.lookAt(this.seat.localToWorld(head.clone()))
      // Wide enough that the name keeps its angle at the eye at any distance.
      node.name.at.scale.setScalar((0.175 * at.distanceTo(head)) / 0.26)
      if (node.name.dirty) {
        const count = bot.needs.length
        node.name.paint(p => {
          p.hits = []
          p.ctx.clearRect(0, 0, p.w, p.h)
          p.font(34, { bold: true })
          const nw = Math.min(p.measure(bot.label), p.w - 90)
          p.text(bot.label, (p.w - nw) / 2 - (count ? 16 : 0), 46, p.pal.ink, p.w - 90)
          if (count) p.text(String(count), (p.w + nw) / 2 + 2, 46, p.pal.warn, 60)
        })
      }
    }
  }

  /* ---------------- what the interaction layer asks ---------------- */

  private toSeat(land: Vector3): Vector3 { return land.clone().multiplyScalar(this.scale).add(this.land.position) }

  /** The middle of a Bot's body, in the seat's space. */
  botCentre(slug: string): Vector3 | null {
    const body = this.nodes.get(slug)?.body
    return body ? this.toSeat(body.root.position).add(new Vector3(0, botHeight(this.scale) / 2, 0)) : null
  }

  /**
   * Where a card rests when a Bot has it, in the seat's space: at its middle, or in front of a body that has depth,
   * on the side that `eye` is on.
   */
  botHand(slug: string, eye: Vector3): Vector3 | null {
    const c = this.botCentre(slug)
    const body = this.nodes.get(slug)?.body
    if (!c || !body) return c
    return c.addScaledVector(eye.clone().sub(c).normalize(), (body.front / body.height) * botHeight(this.scale))
  }

  /** Just above a Bot's head, in the seat's space: where its cards go. */
  botTop(slug: string): Vector3 | null {
    const body = this.nodes.get(slug)?.body
    return body ? this.toSeat(body.root.position).add(new Vector3(0, botHeight(this.scale) * 1.12, 0)) : null
  }

  /** The middle of an object, in the seat's space. */
  objectCentre(id: string): Vector3 | null {
    const d = this.drawn.get(id)
    if (!d) return null
    // Most things stand about a metre tall; the screen is a low, wide thing.
    const up = (d.o.kind === 'screen' ? 0.15 : 0.5) * d.o.scale
    return d.o.anchor === 'seat' ? d.at.position.clone().add(new Vector3(0, up, 0)) : this.toSeat(d.at.position).add(new Vector3(0, up * this.scale, 0))
  }

  objectValue(id: string): { o: SceneObject; value: BoundValue } | null {
    const d = this.drawn.get(id)
    return d ? { o: d.o, value: d.value } : null
  }

  /** Every Bot as a ray sees it: how far off the ray it is, and how big it looks. */
  candidates(origin: Vector3, dir: Vector3): Candidate[] {
    const out: Candidate[] = []
    for (const slug of this.nodes.keys()) {
      const c = this.botCentre(slug)
      if (!c) continue
      const to = c.sub(origin)
      const dist = to.length()
      out.push({ id: slug, deg: to.angleTo(dir) / DEG, radius: Math.atan(botHeight(this.scale) / 2 / dist) / DEG })
    }
    return out
  }

  /** Where a thing stands: a plain transform on the land or in the seat. */
  placeOf(id: string): Object3D | null { return this.drawn.get(id)?.at ?? null }

  /** How big a thing is to a hand that points at it, in the seat's metres. */
  objectSize(id: string): number {
    const d = this.drawn.get(id)
    return d ? (d.o.kind === 'screen' ? 0.16 : 0.45) * d.o.scale * (d.o.anchor === 'seat' ? 1 : this.scale) : 0
  }

  /** A point of the ground, given in the seat's space, in the seat's space and in land metres. */
  groundOf(seat: Vector3): { seat: Vector3; x: number; z: number } {
    return { seat, x: (seat.x - this.land.position.x) / this.scale, z: (seat.z - this.land.position.z) / this.scale }
  }

  /** Where a ray meets the ground, in the seat's space and in land metres. Null when it does not. */
  groundAt(origin: Vector3, dir: Vector3): { seat: Vector3; x: number; z: number } | null {
    if (dir.y > -1e-3) return null
    const k = (this.land.position.y - origin.y) / dir.y
    if (k <= 0 || k > 80) return null
    const seat = origin.clone().addScaledVector(dir, k)
    return { seat, x: (seat.x - this.land.position.x) / this.scale, z: (seat.z - this.land.position.z) / this.scale }
  }

  /** The Bot whose plot a land point is in. */
  plotOf(x: number, z: number): string {
    return plotAt(Object.fromEntries(this.doc.zones.map(zn => [zn.owner, zn.cell])), this.doc.zones.map(zn => zn.owner), x, z)
  }

  private cell(slug: string): number | null { return this.doc.zones.find(z => z.owner === slug)?.cell ?? null }

  /** Two hands pulled apart or pushed together, toward the plot of `slug` ('' for no plot). */
  zoom(ratio: number, slug: string): void {
    const cell = slug ? this.cell(slug) : null
    this.goal = zoomBy(this.goal, ratio, this.sMin, cell === null ? null : visitorSpot(cell))
  }

  /** One hand drags the ground by this much, in the seat's metres. The land follows the hand at once. */
  pan(dx: number, dz: number): void {
    this.goal = panBy(this.goal, dx, dz, this.sMin, this.bounds)
    this.view = { ...this.view, fx: this.goal.fx, fz: this.goal.fz }
  }

  /** The hands let go: a zoom that is close to an end goes on to it. */
  release(): void { this.goal = { ...this.goal, t: settleT(this.goal.t) } }

  /** Back to the whole land on the desk. */
  whole(): void { this.goal = { t: 0, fx: this.bounds.cx, fz: this.bounds.cz } }

  /** Go to a Bot's plot: centred in the miniature, or standing in it when zoomed in. */
  goTo(slug: string): void {
    const cell = this.cell(slug)
    if (cell === null) return
    const [x, z] = this.goal.t < 0.5 ? plotCentre(cell) : visitorSpot(cell)
    this.goal = { t: this.goal.t < 0.5 ? this.goal.t : 1, fx: x, fz: z }
  }

  dispose(): void {
    this.disposed = true
    for (const n of this.nodes.values()) n.name.dispose()
  }
}
