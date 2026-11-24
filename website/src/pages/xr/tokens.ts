/**
 * The objects that carry the acts a hand cannot mime: the tray and its
 * marbles (what waits on the person), the bin (where thrown things go), and
 * the beads a Bot holds up (its plan). The card a Bot holds out for one
 * request is a surface of the scene; this file says where it is held out.
 *
 * They are the interaction layer's, the same in every scene. A scene document
 * cannot add, restyle, hide or imitate one: its kinds are a fixed list and
 * none of these is in it.
 *
 * This file is where each one is, as plain numbers. `Tray.tsx` draws them as
 * plain stand-in shapes. Everything is in the seat's space, low and close:
 * the tray on the desk to the left of the land, the bin beside the desk on
 * the right, and the card held out over the front of the desk, where a hand
 * that rests on the desk can point at it or take it.
 */
import { Vector3 } from 'three'
import { BEADS_MAX, type Bead } from './beads'
import { DESK } from './land'

const at = (x: number, y: number, z: number) => new Vector3(x, y, z)

export const TRAY_AT = at(-0.47, DESK.y + 0.012, -0.5)
export const BIN_AT = at(0.5, DESK.y - 0.03, -0.44)
/**
 * The middle of the card a Bot holds out, and how big it is drawn. It is close, so it is small: its text reads as large
 * as on a card at reading distance. It stands over the far edge of the land, clear of the Bots and what they say.
 */
export const OFFER_AT = at(0, DESK.y + 0.38, -0.42)
export const OFFER_SCALE = 0.6
/** How many marbles the tray shows; the count on the land says how many there are in all. */
export const TRAY_MAX = 6
/** A marble is a thing a hand pinches: across, it is over the least height a pinchable thing may have. */
export const MARBLE_R = 0.018
const ROLL_MS = 900
/** A token can be pointed at within this angle of its middle. */
export const TOKEN_PICK_DEG = 2.2

/** One marble: where it is now, where it rolled in from, and whether it is on the tray or held out as a card. */
export interface Marble { id: string; at: Vector3; from: Vector3 | null; born: number; shown: boolean }
/** One bead of a Bot's plan: where it is, how big, and whether its step is done. */
export interface BeadDot { at: Vector3; size: number; done: boolean; shown: boolean }

export class Tokens {
  /** The marbles on the tray, by request. */
  readonly marbles = new Map<string, Marble>()
  /** The beads each Bot holds up, by Bot. */
  readonly beadRows = new Map<string, BeadDot[]>()
  private order: string[] = []
  /** The request whose card is held out, or ''. Its marble is off the tray for as long. */
  offer = ''

  /** The requests that wait, oldest first. A new one rolls in from its Bot; one that is gone leaves the tray. */
  setNeeds(ids: readonly string[], fromOf: (id: string) => Vector3 | null, clock: number): void {
    for (const id of this.marbles.keys()) if (!ids.includes(id)) this.marbles.delete(id)
    for (const id of ids.slice(0, TRAY_MAX)) {
      if (this.marbles.has(id)) continue
      this.marbles.set(id, { id, at: at(0, 0, 0), from: this.order.length || this.marbles.size ? fromOf(id) : null, born: clock, shown: true })
    }
    this.order = ids.slice(0, TRAY_MAX)
    if (this.offer && !ids.includes(this.offer)) this.offer = ''
  }

  private slot(i: number): Vector3 { return at(TRAY_AT.x - 0.12 + i * 0.048, TRAY_AT.y + 0.004 + MARBLE_R, TRAY_AT.z) }

  /** Where a token is, in the seat's space: `marble:<id>` or `bin`. */
  where(id: string): Vector3 | null {
    if (id === 'bin') return BIN_AT.clone().add(at(0, 0.06, 0))
    return this.marbles.get(id.slice(7))?.at.clone() ?? null
  }

  ids(): string[] { return [...this.order.filter(id => id !== this.offer).map(id => `marble:${id}`), 'bin'] }

  /** One frame. `beads` are the plans Bots hold up, placed over `topOf(slug)` at a size that keeps them seen from `eye`. */
  update(clock: number, still: boolean, beads: ReadonlyMap<string, Bead>, topOf: (slug: string) => Vector3 | null, eye: Vector3): void {
    this.order.forEach((id, i) => {
      const m = this.marbles.get(id)
      if (!m) return
      const to = this.slot(i)
      const t = still || !m.from ? 1 : Math.min(1, (clock - m.born) / ROLL_MS)
      if (t < 1 && m.from) m.at.lerpVectors(m.from, to, t * t * (3 - 2 * t))
      else m.at.lerp(to, still ? 1 : 0.2)
      m.shown = id !== this.offer
    })
    for (const slug of this.beadRows.keys()) if (!beads.has(slug)) this.beadRows.delete(slug)
    for (const [slug, bead] of beads) {
      const top = topOf(slug)
      let row = this.beadRows.get(slug)
      if (!row) { row = Array.from({ length: BEADS_MAX }, () => ({ at: at(0, 0, 0), size: 1, done: false, shown: false })); this.beadRows.set(slug, row) }
      const far = top ? Math.max(1, top.distanceTo(eye) / 0.8) : 1
      row.forEach((b, i) => {
        b.shown = !!top && i < bead.total
        if (!top || i >= bead.total) return
        b.at.copy(top).add(at((i - (bead.total - 1) / 2) * 0.026 * far, 0.02 * far, 0))
        // The step under way beats.
        b.size = far * (i === bead.done && !still ? 1 + 0.25 * Math.sin(clock / 180) : 1)
        b.done = i < bead.done
      })
    }
  }
}
