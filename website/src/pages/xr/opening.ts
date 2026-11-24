/**
 * A thing pulled open. Its parts stand side by side above it, one card each,
 * and follow the hands on a spring (`handling.ts`), so they overshoot a
 * little and settle.
 *
 * One thing is open at a time. What the parts are is not decided here: the
 * scene reads them from the Crew's own fields (`parts.ts`) and hands them in
 * each frame, so an open thing changes when its data does.
 */
import { Vector3 } from 'three'
import { PARTS_MAX, atRest, openAmount, openRest, partPlace, springStep, type Spring } from './handling'
import { CARD, DEG } from './metrics'
import type { Painter } from './painter'
import type { XrPalette } from './palette'
import type { Part } from './parts'
import type { Binding } from './sceneDoc'
import type { TwoHandRule } from './tune'
import { Surface } from './sceneParts'

/** What is opened: a Bot, a thing of the land or a page, by its pick id, with the field its parts are read from. */
export interface OpenThing { id: string; bind: Binding | null; label: string }

const UP = new Vector3(0, 1, 0)
const PART_PX = { w: 720, h: 250 }

export class Opening {
  readonly cards: Surface[]
  thing: OpenThing | null = null
  private spring: Spring = { x: 0, v: 0 }
  private target = 0
  /** Two hands hold it: how far apart they were when they took hold, and how open it was then. */
  private grab: { first: number; from: number } | null = null
  private painted = ''
  private count = 0
  private lit = 0

  constructor(pal: XrPalette, icons: ReadonlyMap<string, CanvasImageSource>) {
    this.cards = Array.from({ length: PARTS_MAX }, (_, i) => new Surface(`part:${i}`, PART_PX.w, PART_PX.h, CARD.width, pal, icons, 34 + i))
  }

  /** Two hands take hold of a thing, `gap` metres apart. A different thing that was open shuts at once. */
  take(thing: OpenThing, gap: number): void {
    if (this.thing?.id !== thing.id) { this.spring = { x: 0, v: 0 }; this.target = 0; this.painted = '' }
    this.thing = thing
    this.grab = { first: gap, from: this.target }
  }

  /** The hands are now `gap` metres apart: the thing is that far open, by the rule in force. */
  pull(gap: number, rule: TwoHandRule): void {
    if (this.grab) this.target = openAmount(gap, rule, this.grab.first, this.grab.from)
  }

  /** The hands let go: past half way the thing opens, before it shuts. */
  release(): void {
    if (!this.grab) return
    this.grab = null
    this.target = openRest(this.target)
  }

  /** Open a thing, or shut it when it is the one that is open: what one key does in place of two hands. */
  toggle(thing: OpenThing): void {
    const shut = this.thing?.id === thing.id && this.target > 0
    this.take(thing, 0)
    this.grab = null
    this.target = shut ? 0 : 1
  }

  /** Open a thing without two hands: what a finger that rests on it does. */
  show(thing: OpenThing): void {
    this.take(thing, 0)
    this.grab = null
    this.target = 1
  }

  /** Whether this thing is open, or opening. */
  isOpen(id: string): boolean { return this.thing?.id === id && this.target > 0 }

  shut(): void { this.grab = null; this.target = 0 }

  /**
   * One frame. `centre` is where the thing is and `eye` where the person's head is, both in the seat's space;
   * `eyeWorld` is the head in the world, for the cards to face. A thing that is gone, or has no parts, is not open.
   */
  update(dt: number, still: boolean, centre: Vector3 | null, eye: Vector3, eyeWorld: Vector3, parts: readonly Part[], paint: (p: Painter, part: Part) => void): void {
    const hide = () => { for (const s of this.cards) s.at.visible = false; this.count = 0 }
    if (!this.thing || !centre || !parts.length) { this.thing = null; this.grab = null; hide(); return }
    this.spring = still ? { x: this.target, v: 0 } : springStep(this.spring, this.target, dt)
    if (!this.grab && this.target === 0 && atRest(this.spring, 0)) { this.thing = null; hide(); return }
    const open = this.spring.x
    const key = JSON.stringify(parts)
    const repaint = key !== this.painted || this.cards.some((s, i) => i < parts.length && s.dirty)
    this.painted = key
    this.count = parts.length
    this.lit = parts.filter(p => p.lit).length
    const to = centre.clone().sub(eye)
    // The cards keep their angle at the eye: bigger for a far thing, smaller for one that is held close, so they never fill the view.
    const far = Math.max(0.45, to.length() / CARD.distance)
    const size = far * Math.max(0.05, Math.min(1.15, 0.3 + 0.7 * open))
    this.cards.forEach((s, i) => {
      const part = parts[i]
      s.at.visible = !!part && open > 0.02
      if (!part) return
      const at = partPlace(i, parts.length, open)
      s.at.position.copy(eye).add(to.clone().applyAxisAngle(UP, -at.yaw * DEG)).add(new Vector3(0, at.rise * far + (s.height * size) / 2, 0))
      s.at.scale.setScalar(size)
      s.at.lookAt(eyeWorld)
      s.opacity = Math.max(0, Math.min(1, open * 2.5))
      if (repaint) s.paint(p => paint(p, part))
    })
  }

  /** For the tests: what is open, how far, where it is going, how many parts show, and how many of them are lit. */
  probe(): { id: string; open: number; target: number; parts: number; lit: number; held: boolean } | null {
    return this.thing ? { id: this.thing.id, open: +this.spring.x.toFixed(3), target: +this.target.toFixed(3), parts: this.count, lit: this.lit, held: !!this.grab } : null
  }
}
