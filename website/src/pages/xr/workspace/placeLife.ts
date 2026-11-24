/**
 * The crew with no row (`places.ts`), in motion: how a Bot walks from one spot to another, and what an idle one does
 * at its desk. Plain numbers and rules; `Crew.tsx` runs them at each frame.
 *
 * - A Bot WALKS between spots: to the front when it asks, home when its ask ends, to its desk when work starts. A
 *   walk takes a second for a metre, eases in and out, passes around other bodies and never through a page he reads.
 *   Where no such way is found it does not walk: it shrinks away and grows at the new spot (`PLACES.swapMs`).
 * - An idle Bot's life is small and calm: a look around, a stretch, a nap, and now and then a few steps to a neighbour
 *   and back. Each has a clock of its own, from its name, so no two do the same thing in step.
 * - Never more than `LIFE.mostMoving` Bots walk at one time, and nothing new moves in the middle of his look while he
 *   reads a page (the middle of his look is on it) or holds a board. That middle never holds the walk of the one that
 *   needs him, and a walk that is under way finishes.
 *
 * Lengths are metres, times milliseconds, angles degrees unless a name says radians.
 */
import { seedOf } from '../botMotion'
import { PAGES } from './layout'
import { MID_M, PLACES, facesClash, inView, onBox, onDesk, seenAt, type Look, type Pt, type SeenBox } from './places'

export const LIFE = {
  /** A walk: so fast, and never shorter than this long. */
  mPerS: 1,
  leastMs: 400,
  /** A walking body's middle comes no nearer than this to another body's middle: a body is 0.22 m wide. */
  clearM: 0.27,
  /** The way is tested at points this far apart. */
  sampleM: 0.06,
  /** A way around: the walk bends this far to a side, up or down, at its middle. The nearest bend that is free is taken. */
  bendsM: [0.3, 0.55, 0.8] as const,
  /** An idle Bot picks what it does anew every so long. */
  beatMs: 8000,
  /** A look around: the body turns this far to one side and back over this long. A stretch lasts this long. */
  turnRad: 0.7,
  turnMs: 3600,
  stretchMs: 2600,
  /** A visit: to a neighbour no farther than this, to stand this far beside it. */
  visit: { mostM: 1.3, besideM: 0.34 },
  /** While he reads a page or holds a board nothing moves within this many degrees of the middle of his look. */
  quietDeg: 10,
  /** Never more Bots than this walk at one time. */
  mostMoving: 2,
  /** The one that needs him waits no longer than this for the others to stand aside. */
  yieldMs: 4000,
} as const

const dist = (a: readonly number[], b: readonly number[]) => Math.hypot(a[0] - b[0], a[1] - b[1], a[2] - b[2])
const mid = (p: readonly number[]): Pt => [p[0], p[1] + MID_M, p[2]]

/** Whether a body at `feet` is within `LIFE.quietDeg` of the middle of a look. */
export function inQuiet(feet: readonly number[], look: Look): boolean {
  const s = seenAt(mid(feet), look)
  return !!s && Math.hypot(s[0], s[1]) <= Math.tan((LIFE.quietDeg * Math.PI) / 180)
}

/** Whether he reads a page: the middle of his look is on one of the pages he has open. */
export function readsPage(look: Look, pages: readonly SeenBox[]): boolean {
  const r = Math.PI / 180
  // A point far along his look, as each page sees it in its own frame.
  const far: Pt = [Math.sin(look.yaw * r) * Math.cos(look.pitch * r) * 9, PAGES.eyeM + Math.sin(look.pitch * r) * 9, -Math.cos(look.yaw * r) * Math.cos(look.pitch * r) * 9]
  return pages.some(page => { const s = seenAt(far, page); return !!s && s[0] >= page.x0 && s[0] <= page.x1 && s[1] >= page.y0 && s[1] <= page.y1 })
}

/**
 * What of the quiet middle a walk must keep out of. None for the one that needs him, whose walk is meant to end
 * there, and none for a walk that is under way: it finishes, so nobody is left between spots.
 */
export const quietFor = (quiet: Look | null, forward: boolean, walking: boolean): Look | null => (forward || walking ? null : quiet)

/** A way from one spot to another: straight, or bent through `via` (a curve that is pulled toward it, not a corner). */
export interface Way { from: Pt; to: Pt; via: Pt | null }

/** The point of a way at `s` of its length (0 to 1, by the curve's own measure). */
export function wayPoint(way: Way, s: number): Pt {
  const { from: a, to: b, via: v } = way
  if (!v) return [a[0] + (b[0] - a[0]) * s, a[1] + (b[1] - a[1]) * s, a[2] + (b[2] - a[2]) * s]
  const [p, q, r] = [(1 - s) ** 2, 2 * s * (1 - s), s * s]
  return [p * a[0] + q * v[0] + r * b[0], p * a[1] + q * v[1] + r * b[1], p * a[2] + q * v[2] + r * b[2]]
}

/** Whether the rest of a way, from `t` of its time on, lies on one of the pages (as he sees them). */
export function crossesPage(way: Way, t: number, pages: readonly SeenBox[]): boolean {
  if (!pages.length) return false
  const n = Math.max(2, Math.ceil(wayLength(way) / LIFE.sampleM))
  for (let k = 0; k <= n; k += 1) {
    const u = Math.min(1, Math.max(0, t)) + ((1 - Math.min(1, Math.max(0, t))) * k) / n
    const p = walkAt(way, u).at
    if (pages.some(page => onBox(p, page, 0))) return true
  }
  return false
}

/** How long a way is. */
export function wayLength(way: Way): number {
  if (!way.via) return dist(way.from, way.to)
  let sum = 0
  let last = way.from
  for (let k = 1; k <= 16; k += 1) { const p = wayPoint(way, k / 16); sum += dist(last, p); last = p }
  return sum
}

/** How long the walk along a way takes: a second for a metre. */
export const walkMs = (way: Way): number => Math.max(LIFE.leastMs, (wayLength(way) / LIFE.mPerS) * 1000)

/** Where a walk is after `t` of its time (0 to 1), eased in and out, and which way it heads there (a yaw of the room). */
export function walkAt(way: Way, t: number): { at: Pt; yaw: number } {
  const u = Math.min(1, Math.max(0, t))
  const s = u * u * (3 - 2 * u)
  const at = wayPoint(way, s)
  const [a, b] = [wayPoint(way, Math.max(0, s - 0.02)), wayPoint(way, Math.min(1, s + 0.02))]
  return { at, yaw: Math.atan2(b[0] - a[0], b[2] - a[2]) }
}

/** What a way must keep clear of. */
export interface WayAsk {
  /** Where the other bodies are now and where they are going (their feet, and points of the ways they walk). */
  bodies: readonly Pt[]
  /** The pages he has open, as the eye sees them. */
  pages: readonly SeenBox[]
  /** His look while he reads a page or holds a board: nothing moves in its middle. `null`: he does neither. */
  quiet: Look | null
  /**
   * The walk of the one that needs him: it is the thing he looks at, so its way also passes over no other Bot's face
   * as he sees them. Where no such way is free, the nearest one that keeps clear of the bodies is taken.
   */
  faces?: boolean
}

/**
 * The way a Bot walks from `from` to `to`: straight, or around what is in the way, by the nearest bend that is free.
 * `null` when no way is free: a page he reads lies between, or the middle of his look is to be left alone. Past a
 * page the Bot changes spot with the fade; for the middle of his look it waits until he is done (`Crew.tsx`).
 */
export function walkWay(from: Pt, to: Pt, ask: WayAsk): Way | null {
  if (ask.faces) return wayThrough(from, to, ask, true) ?? wayThrough(from, to, ask, false)
  return wayThrough(from, to, ask, false)
}

function wayThrough(from: Pt, to: Pt, ask: WayAsk, faces: boolean): Way | null {
  const free = (way: Way) => {
    const n = Math.max(2, Math.ceil(wayLength(way) / LIFE.sampleM))
    for (let k = 0; k <= n; k += 1) {
      const p = wayPoint(way, k / n)
      if (ask.pages.some(page => onBox(p, page, 0))) return false
      if (ask.quiet && inQuiet(p, ask.quiet)) return false
      // Beside its own start a body may stand nearer: it stood there. At its end the way keeps clear like anywhere.
      if (dist(p, from) < LIFE.clearM) continue
      if (ask.bodies.some(b => dist(p, b) < LIFE.clearM)) return false
      // The end spot's faces are the rule of places' to keep clear (the others give way to it): near the eye a body
      // covers much of the view, so the last stretch is left to that rule.
      if (faces && dist(p, to) >= LIFE.clearM && ask.bodies.some(b => facesClash(p, b, 0))) return false
    }
    return true
  }
  const straight: Way = { from, to, via: null }
  if (free(straight)) return straight
  const m: Pt = [(from[0] + to[0]) / 2, (from[1] + to[1]) / 2, (from[2] + to[2]) / 2]
  // To a side of the way as it lies on the floor, or over or under it.
  const flat = Math.hypot(to[0] - from[0], to[2] - from[2])
  const side: [number, number] = flat > 1e-6 ? [-(to[2] - from[2]) / flat, (to[0] - from[0]) / flat] : [1, 0]
  for (const bend of LIFE.bendsM) {
    // The curve passes half as far from the straight way as its `via` point stands.
    const d = bend * 2
    const vias: Pt[] = [[m[0] + side[0] * d, m[1], m[2] + side[1] * d], [m[0] - side[0] * d, m[1], m[2] - side[1] * d], [m[0], m[1] - d, m[2]], [m[0], m[1] + d, m[2]]]
    for (const via of vias) { const way: Way = { from, to, via }; if (free(way)) return way }
  }
  return null
}

/** What a spot must stay clear of in the room as it is now: what covers things (pages, Home, the one in front's board) and the faces of the Bots that stand. */
export interface HoldAsk { covers: readonly SeenBox[]; faces: readonly Pt[] }

/**
 * Whether a spot that was free when it was picked is free still. A visit ends at once when its spot comes under
 * something the room has now: the one that came forward, its board, a page, Home grown.
 */
export const visitHolds = (spot: Pt, ask: HoldAsk): boolean => !ask.covers.some(c => onBox(spot, c)) && !ask.faces.some(f => facesClash(f, spot))

/** What an idle Bot does now: nothing, a look around, a stretch, a nap, or a visit to a neighbour. */
export type IdleAct = 'rest' | 'look' | 'stretch' | 'nap' | 'visit'

/**
 * What an idle Bot does in the beat that `nowMs` is in, the number of that beat, and how long the beat has run. Each
 * Bot's beats start at a moment that its name gives, so no two pick in step. Of twelve beats: three looks around, a
 * stretch, two naps, one visit, five of rest.
 */
export function idleAct(slug: string, nowMs: number): { act: IdleAct; beat: number; ranMs: number } {
  const t = nowMs + (seedOf(`${slug}:life`) % LIFE.beatMs)
  const beat = Math.floor(t / LIFE.beatMs)
  const pick = seedOf(`${slug}:life:${beat}`) % 12
  const act: IdleAct = pick < 3 ? 'look' : pick === 3 ? 'stretch' : pick < 6 ? 'nap' : pick === 6 ? 'visit' : 'rest'
  return { act, beat, ranMs: t - beat * LIFE.beatMs }
}

/** What a visit must keep to. */
export interface VisitAsk {
  /** Where every Bot stands now (its feet, before its drift), in the crew's order; and which of them may be visited. */
  at: readonly Pt[]
  open: readonly boolean[]
  /** The desks that are drawn (the middle of each top). */
  desks: readonly Pt[]
  /** What no Bot stands on: the pages he has open, Home, and what the one in front covers. */
  boxes: readonly SeenBox[]
}

/**
 * Where the Bot of number `i` stands when it visits a neighbour: beside the nearest Bot that is near enough, on the
 * side it comes from first, at that Bot's height. Over nobody's face, on no desk, on no page and no surface, and in
 * view when its own place is. `null` when no neighbour has such a spot: it stays.
 */
export function visitSpot(i: number, ask: VisitAsk): Pt | null {
  const home = ask.at[i]
  const near = ask.at.map((p, j) => ({ j, p, far: dist(p, home) })).filter(o => o.j !== i && ask.open[o.j] && o.far <= LIFE.visit.mostM).sort((a, b) => a.far - b.far)
  const level: Look = { yaw: 0, pitch: 0 }
  const seen = inView(home, level)
  for (const { j, p } of near) {
    const d = Math.hypot(p[0], p[2]) || 1
    const across: [number, number] = [-p[2] / d, p[0] / d]
    // The side that it comes from first: the side of the neighbour that its own place is on, as he sees them.
    const own = (home[0] - p[0]) * across[0] + (home[2] - p[2]) * across[1] >= 0 ? 1 : -1
    for (const way of [own, -own]) {
      const spot: Pt = [p[0] + across[0] * way * LIFE.visit.besideM, p[1], p[2] + across[1] * way * LIFE.visit.besideM]
      if (seen && !inView(spot, level)) continue
      if (ask.at.some((o, k) => k !== i && (facesClash(o, spot, k === j ? 0 : PLACES.airDeg) || dist(o, spot) < LIFE.clearM))) continue
      if (ask.desks.some(desk => onDesk(spot, desk))) continue
      if (ask.boxes.some(box => onBox(spot, box))) continue
      return spot
    }
  }
  return null
}
