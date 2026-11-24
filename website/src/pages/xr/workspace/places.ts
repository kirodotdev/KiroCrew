/**
 * The crew with no row (the room as it opens; `?places=0` gives the row back): each Bot has a desk of its own in the room, in depth and at its own height,
 * and where a Bot is says what it does. Plain numbers and rules; `Crew.tsx` draws them.
 *
 * - A place is a point in the room: the feet of a Bot that works there. Its desk stands beside it, on its outer side.
 * - A Bot that he never placed takes the next free one of a list of places made by hand (`SPOTS`). Each of those is
 *   whole in what a level head sees at the start (61 by 41 degrees), with its desk and its name sign and two degrees
 *   of air, and they hold the middle band of that view: the nearest left and right of the middle, the far ones behind
 *   and between them. Home stands as its strip over them. A crew larger than the list stands in a ring farther to
 *   the sides, whole outside that view, where a turn of the head shows it. A place that a drag gave a Bot is kept as
 *   a point (`room.ts`); a yaw that the row kept becomes the free place nearest that yaw.
 * - Where a Bot is says what it does: one that works is at its desk, turned to it, the desk's top lit in its colour,
 *   leaning to it in a beat of its own; one that has nothing to do stands beside its desk, which is dark, turned a
 *   little away from it, and drifts; one that failed is slumped at its desk and faces him; one that needs him comes to the low middle of
 *   the view, nearer than everything else.
 * - No body covers another one's face as he sees them, and no Bot stands on a page he has open, on Home's large
 *   surface or behind the one that came forward: a Bot that one of those would cover steps beside it.
 *
 * What "he sees" is worked out in the frame of a look, as a camera has it: a point is so far right and so far up of
 * the look's middle, as tangents. A box of yaw and pitch is wrong at a view's corners, where a body is cut first.
 *
 * Angles are degrees, lengths metres. The eye is the head at the start: `PAGES.eyeM` over the room's middle.
 */
import { seedOf } from '../botMotion'
import { CARD } from './chatCard'
import { CREW, type CrewPlace, type KeptCrew } from './crewRow'
import { HOME, homeLayout } from './home'
import { PAGES } from './layout'

const RAD = Math.PI / 180
const tan = (deg: number) => Math.tan(deg * RAD)
export type Pt = [number, number, number]

export const PLACES = {
  /** The glasses' view, half of each side; a place counts as in view with this much air inside its edge. */
  viewHalfYaw: 30.5,
  viewHalfPitch: 20.5,
  marginDeg: 2,
  /** Air between a body and a face, and around Home and a page, as the eye sees it. Wider than an idle Bot's drift. */
  airDeg: 1.5,
  /** The desk: so far to the outer side of its Bot's feet and toward him, its top so high over the feet, and its size. */
  desk: { sideM: 0.14, towardM: 0.07, topM: 0.06, wideM: 0.13, deepM: 0.09, legM: 0.08, tiltDeg: 32 },
  /** The name sign's middle stands this far over the top of the head, clear of the face from every side and while the body rises. */
  signOverM: 0.08,
  /** An idle Bot stands at its desk, turned this far away from it. */
  idleTurnDeg: 30,
  /** The one that needs him: so far away, its body's middle so far under the eye line, so far to the right. */
  forward: { distanceM: 1.02, pitchDeg: -7, yawDeg: 2 },
  /** What the one that came forward covers with its clipboard, in a level look: so wide to each side, and up to here. */
  forwardCovers: { halfDeg: 10, topDeg: 8 },
  /** While a taken tray or the card's keyboard holds the low middle, it stands at the low side. */
  aside: { distanceM: 1.2, pitchDeg: -7, yawDeg: 20 },
  /** The work beat: a lean to the desk this long in every so long, from a moment that its name gives. */
  beat: { everyMs: 2200, leanMs: 900 },
  /** An idle Bot's drift: so far to the side and up, at these speeds (radians a second). */
  drift: { sideM: 0.04, upM: 0.03, sideRate: 0.35, upRate: 0.5 },
  /** A Bot that changes spot shrinks away and grows at the new one over this long. */
  swapMs: 300,
  /** A dragged place stands no nearer than this, and no farther. */
  nearM: 0.9,
  farM: 3.2,
} as const

/** From a Bot's feet to the middle of its body, and to the top of its head, at rest. */
export const MID_M = CREW.heightM * 0.55
const TOP_M = CREW.heightM * (1 + CREW.hoverShare)
const HALF_WIDE_M = (CREW.heightM * CREW.widthShare) / 2

/** A look: where the middle of a view points. */
export interface Look { yaw: number; pitch: number }
const LEVEL: Look = { yaw: 0, pitch: 0 }

/** A rectangle of a look's picture, as tangents from its middle: right is +x, up is +y. */
export interface Rect { x0: number; x1: number; y0: number; y1: number }

/** Where a point of the room is in a look's picture, or `null` when it is not in front of the eye. */
export function seenAt(p: readonly number[], look: Look = LEVEL): [number, number] | null {
  const [vx, vy, vz] = [p[0], p[1] - PAGES.eyeM, p[2]]
  const [sy, cy, sp, cp] = [Math.sin(look.yaw * RAD), Math.cos(look.yaw * RAD), Math.sin(look.pitch * RAD), Math.cos(look.pitch * RAD)]
  // Forward of the level look, then tipped up by the pitch.
  const flat = vx * sy - vz * cy
  const z = flat * cp + vy * sp
  if (z < 0.05) return null
  return [(vx * cy + vz * sy) / z, (vy * cp - flat * sp) / z]
}

/** The way across the eye's line to a point, to the right as he sees it. */
const across = (p: readonly number[]): [number, number] => { const d = Math.hypot(p[0], p[2]) || 1; return [-p[2] / d, p[0] / d] }
/** Which way a thing at `p` faces to look at the eye (a yaw of the room, as `Crew.tsx` turns a body). */
const toHim = (p: readonly number[]) => Math.atan2(-p[0], -p[2])
/** How far to the right of straight ahead the eye sees a point, in degrees. */
export const yawOf = (p: readonly number[]): number => Math.atan2(p[0], -p[2]) / RAD

/** How far a thing reaches from a Bot's feet: to his left and right as he sees it, up, and down. */
export interface Reach { left: number; right: number; top: number; bottom: number }
/** The body alone; its face (the upper half); an idle body with its drift and its sign; a Bot at its place with its desk and its sign. */
const BODY: Reach = { left: HALF_WIDE_M, right: HALF_WIDE_M, top: TOP_M, bottom: 0 }
const FACE: Reach = { ...BODY, bottom: -MID_M }
const SIGN_M = PLACES.signOverM + 0.025
const LOOSE: Reach = { left: HALF_WIDE_M + 0.07, right: HALF_WIDE_M + 0.07, top: TOP_M + SIGN_M + 0.03, bottom: 0.07 }
const withDesk = (side: number): Reach => {
  const d = PLACES.desk
  const far = d.sideM + d.wideM / 2 + 0.01
  return { left: side < 0 ? far : HALF_WIDE_M + 0.03, right: side > 0 ? far : HALF_WIDE_M + 0.03, top: TOP_M + SIGN_M, bottom: 0.04 }
}
/** The one that came forward, with its clipboard over its head. */
export const WITH_BOARD: Reach = { left: 0.17, right: 0.17, top: TOP_M + 0.3, bottom: 0.04 }

/** The rectangle that a thing at `feet` takes in a look's picture, or `null` when a part of it is not in front of the eye. */
export function rectOf(feet: readonly number[], look: Look = LEVEL, reach: Reach = BODY): Rect | null {
  const [tx, tz] = across(feet)
  const out: Rect = { x0: Infinity, x1: -Infinity, y0: Infinity, y1: -Infinity }
  for (const side of [-reach.left, reach.right]) for (const up of [-reach.bottom, reach.top]) {
    const s = seenAt([feet[0] + tx * side, feet[1] + up, feet[2] + tz * side], look)
    if (!s) return null
    out.x0 = Math.min(out.x0, s[0]); out.x1 = Math.max(out.x1, s[0]); out.y0 = Math.min(out.y0, s[1]); out.y1 = Math.max(out.y1, s[1])
  }
  return out
}
const overlap = (a: Rect, b: Rect, air: number) => a.x0 < b.x1 + air && a.x1 > b.x0 - air && a.y0 < b.y1 + air && a.y1 > b.y0 - air

/** Whether one body covers the other's face as the eye sees them, with `air` degrees between: a body may stand over another's lower half, never over its face. */
export function facesClash(a: readonly number[], b: readonly number[], air: number = PLACES.airDeg): boolean {
  const look = { yaw: yawOf(a), pitch: 0 }
  const [ra, fa, rb, fb] = [rectOf(a, look), rectOf(a, look, FACE), rectOf(b, look), rectOf(b, look, FACE)]
  if (!ra || !fa || !rb || !fb) return false
  return overlap(ra, fb, tan(air)) || overlap(rb, fa, tan(air))
}

/** Whether a thing at `feet` is whole in what a head that looks `look` sees, with the margin inside the view's edge. */
export function inView(feet: readonly number[], look: Look = LEVEL, reach: Reach = BODY, marginDeg: number = PLACES.marginDeg): boolean {
  const r = rectOf(feet, look, reach)
  const [x, y] = [tan(PLACES.viewHalfYaw - marginDeg), tan(PLACES.viewHalfPitch - marginDeg)]
  return !!r && r.x0 >= -x && r.x1 <= x && r.y0 >= -y && r.y1 <= y
}

/** A flat thing that he sees: a look at it, and its rectangle in that look's picture; and how far it is. */
export interface SeenBox extends Look, Rect { distanceM: number }
const boxAt = (look: Look, halfX: number, halfY: number, distanceM: number): SeenBox => ({ ...look, x0: -halfX, x1: halfX, y0: -halfY, y1: halfY, distanceM })

/** A page that faces him with its middle at `at` (a point of the room), so wide and so tall. */
export function seenBox(at: { x: number; y: number; z: number }, widthM: number, heightM: number): SeenBox {
  const flat = Math.hypot(at.x, at.z) || 1e-6
  const distanceM = Math.hypot(flat, at.y - PAGES.eyeM)
  return boxAt({ yaw: Math.atan2(at.x, -at.z) / RAD, pitch: Math.atan2(at.y - PAGES.eyeM, flat) / RAD }, widthM / 2 / distanceM, heightM / 2 / distanceM, distanceM)
}

/** Home's large surface: straight ahead at a page's distance, level, as tall as it is with the most lines and filed lines. */
export const HOME_BOX: SeenBox = (() => {
  const l = HOME.large
  const tallM = homeLayout('large', HOME.mostLines, true, false, { head: true, lines: HOME.doneLines }).heightM
  return boxAt(LEVEL, l.widthM / 2 / HOME.distanceM, tallM / 2 / HOME.distanceM, HOME.distanceM)
})()

/** Home's strip where it stands in a room of places: over a default first page (`homeLayout`, D80 1). */
export const HOME_STRIP: SeenBox = (() => {
  const s = homeLayout('compact', 0, false, true)
  return boxAt({ yaw: 0, pitch: s.pitchDeg }, s.widthM / 2 / s.distanceM, s.heightM / 2 / s.distanceM, s.distanceM)
})()

/** What the one that came forward covers, with its clipboard: the low middle of a level look, up to the board's top. */
export const FORWARD_BOX: SeenBox = (() => {
  const f = PLACES.forward
  const c = PLACES.forwardCovers
  return { ...LEVEL, x0: tan(f.yawDeg) - tan(c.halfDeg), x1: tan(f.yawDeg) + tan(c.halfDeg), y0: -9, y1: tan(c.topDeg), distanceM: f.distanceM }
})()

/** What one that came forward to `at` covers with its clipboard, in the look it came to: its width, and from under the view up to its board's top. */
export function forwardCovers(at: readonly number[], look: Look): SeenBox | null {
  const r = rectOf(at, look, WITH_BOARD)
  const mid = seenAt([at[0], at[1] + MID_M, at[2]], look)
  if (!r || !mid) return null
  const distanceM = Math.hypot(at[0], at[2])
  // The board is as large wherever it stands: farther away it reaches less high over its Bot's middle.
  const over = (tan(PLACES.forwardCovers.topDeg) - tan(PLACES.forward.pitchDeg)) * (PLACES.forward.distanceM / distanceM)
  return { ...look, x0: r.x0, x1: r.x1, y0: -9, y1: mid[1] + over, distanceM }
}

/** Whether a thing at `feet` and a flat thing cover each other as the eye sees them (with `air` degrees between). */
export function onBox(feet: readonly number[], box: SeenBox, air: number = PLACES.airDeg, reach: Reach = BODY): boolean {
  const r = rectOf(feet, box, reach)
  return !!r && overlap(r, box, tan(air))
}

/**
 * The places by hand: so far to the right, the body's middle so far over the eye line, so far away (level). In the
 * crew's order: the two nearest, left and right of the middle; one far, between them; a far one over each near one;
 * and one in each upper corner, beside Home's strip. Each is whole in a level view with its desk and its sign.
 * Seven, and no more: an eighth between the near ones finds no spot in view to step to when one comes forward.
 * The low middle is left free: it is where a Bot that needs him comes. No three stand at one height.
 */
export const SPOTS: readonly (readonly [number, number, number])[] = [
  [-16, -9.6, 2.0], [17.5, -7.8, 1.95], [-4, 2.5, 3.06], [-20.5, 1, 2.8], [23.75, 3.2, 3.15],
  [-24.2, 10.8, 3.1], [24, 10.6, 2.97],
]

/** The ring past the list: columns to each side, two to a column, from this yaw on and so far from column to column. */
const RING = { fromDeg: 46, stepDeg: 17, rows: [[8, 2.6], [-9, 2.1]] as const }

/** The feet of a Bot whose body's middle the eye sees `yawDeg` right and `pitchDeg` up, `distanceM` away (level). */
export function spotPoint(yawDeg: number, pitchDeg: number, distanceM: number): Pt {
  const y = PAGES.eyeM + distanceM * tan(pitchDeg) - MID_M
  return [Math.sin(yawDeg * RAD) * distanceM, y, -Math.cos(yawDeg * RAD) * distanceM]
}

/** The place of number `i`: a list place; past the list, a place of the ring to the sides, right and left by turns. */
export function spotOf(i: number): Pt {
  if (i < SPOTS.length) return spotPoint(...SPOTS[i])
  const k = i - SPOTS.length
  const side = k % 2 ? -1 : 1
  const j = Math.floor(k / 2)
  const col = Math.floor(j / 2)
  // The upper and the lower place swap from column to column, so that no line runs along the ring.
  const [pitch, dist] = RING.rows[(j + col) % 2]
  return spotPoint(side * (RING.fromDeg + RING.stepDeg * col), pitch, dist)
}

/** The places that a drag gave Bots, by slug, as points of the room (`room.ts`). */
export type KeptPlaces = Readonly<Record<string, Pt>>

/**
 * Each Bot's place, in the crew's order: a kept point; else, for a yaw that the row kept, the free place whose yaw is
 * nearest; else the place of its own number, or the first free one.
 */
export function placeHomes(slugs: readonly string[], kept: KeptPlaces = {}, rowYaws: KeptCrew = {}): Pt[] {
  const taken = new Set<number>()
  const out: (Pt | null)[] = slugs.map(s => (kept[s] ? [...kept[s]] as Pt : null))
  const most = Math.max(SPOTS.length, slugs.length)
  slugs.forEach((s, k) => {
    const y = rowYaws[s]
    if (out[k] || typeof y !== 'number') return
    let best = -1
    for (let i = 0; i < most; i += 1) if (!taken.has(i) && (best < 0 || Math.abs(yawOf(spotOf(i)) - y) < Math.abs(yawOf(spotOf(best)) - y))) best = i
    if (best >= 0) { taken.add(best); out[k] = spotOf(best) }
  })
  // Each of the others has the place of its own number in the crew: a Bot that he put elsewhere leaves its place
  // empty, and nobody moves up into it. One whose place a kept yaw took has the first free place, so that a place
  // inside the view is filled before one of the ring.
  return out.map((p, k) => {
    if (p) return p
    let n = k
    if (taken.has(n)) n = 0
    while (taken.has(n)) n += 1
    taken.add(n)
    return spotOf(n)
  })
}

/** A place as the row's type, for what reads a Bot's place by yaw. */
export const asCrewPlace = (feet: Pt): CrewPlace => ({ yawDeg: yawOf(feet), radiusM: Math.hypot(feet[0], feet[2]), feetM: feet[1], row: 0 })

/** The side of a place that its desk stands on: the outer one, away from the view's middle (1 right, -1 left). */
export const deskSide = (home: readonly number[]): 1 | -1 => (home[0] >= 0 ? 1 : -1)

/** Where a place's desk stands: the middle of its top. */
export function deskAt(home: readonly number[]): Pt {
  const d = PLACES.desk
  const [tx, tz] = across(home)
  const a = toHim(home)
  const side = deskSide(home)
  return [home[0] + tx * side * d.sideM + Math.sin(a) * d.towardM, home[1] + d.topM, home[2] + tz * side * d.sideM + Math.cos(a) * d.towardM]
}

/** Whether a place is whole in a level view: its Bot, its desk and its name sign, with the margin. */
export const placeInView = (home: readonly number[]): boolean => inView(home, LEVEL, withDesk(deskSide(home)))

/**
 * Whether the room has no row: each Bot at a desk of its own. It is the room as it opens, with no word in the
 * address (he asked for no row). `places=0` gives the row back; any other word, or none, is the room of places. With
 * a desk in front of him (`desk=1`) the crew sits at its seats, so there are no places.
 */
export const placesWanted = (word: string | null, deskOn: boolean): boolean => word !== '0' && !deskOn

/** A Bot as the rule of who stands in front reads it: its state, and its asks with the time of each. */
export interface AskingBot { slug: string; state: string; needs?: readonly { ts: number; id?: string; kind?: string }[] }

/**
 * The Bot that stands in front. The one that came forward (`was`) stays until its own ask ends: a Bot that starts to
 * ask meanwhile does not take its place, and the board he reaches for is not swapped under his hand. Then the next is
 * the Bot whose oldest ask is oldest (the crew's order for a tie). One that he put down stays where he put it, and
 * the next comes instead. '' when nobody comes.
 */
export function frontOf(bots: readonly AskingBot[], was: string, put?: ReadonlySet<string>): string {
  const asking = bots.filter(b => b.state === 'needs' && !put?.has(b.slug))
  if (asking.some(b => b.slug === was)) return was
  let best = ''
  let oldest = Infinity
  for (const b of asking) {
    const since = Math.min(Infinity, ...(b.needs ?? []).map(n => n.ts))
    if (!best || since < oldest) { best = b.slug; oldest = since }
  }
  return best
}

/** The asks whose board he handed back ("not now", D27): by the Bot's slug, the ask's id. */
export type SentHome = Readonly<Record<string, string>>

/**
 * The asks that keep their Bot at its desk: each whose board he handed back, until that ask ends. It holds when he
 * takes that board again, so nothing but the board moves then. The same record comes back when nothing changed.
 */
export function staysHome(was: SentHome, back: SentHome | undefined, bots: readonly AskingBot[]): SentHome {
  const all = { ...was, ...back }
  const now = Object.fromEntries(Object.entries(all).filter(([slug, ask]) => bots.some(b => b.slug === slug && b.needs?.some(n => n.id === ask))))
  const same = Object.keys(now).length === Object.keys(was).length && Object.entries(now).every(([slug, ask]) => was[slug] === ask)
  return same ? was : now
}

/**
 * The Bots that such an ask holds at their desk. A Bot with a question besides it comes forward as any asker does:
 * a question comes up past a board he handed back (D27, `board.passesHandBack`). Its other approvals wait behind
 * that board, one board at a time, and bring nobody forward.
 */
export const heldHome = (home: SentHome, bots: readonly AskingBot[]): string[] =>
  bots.filter(b => home[b.slug] && !(b.needs ?? []).some(n => n.id !== home[b.slug] && n.kind === 'question')).map(b => b.slug)

/**
 * The count on the sign at a page's edge, for a Bot that could not come forward: all its asks. Not the count of
 * asks that wait without a board (`platesShown`): that is 0 for a Bot whose board is in view, and the sign would say
 * nothing waits. At least 1: the sign stands only for a Bot that asks.
 */
export const pageSignCount = (needs: readonly unknown[]): number => Math.max(1, needs.length)

/** The Bot in front, and where he looked when it came (to the right of straight ahead). '' and 0: nobody. */
export interface Front { slug: string; yaw: number }

/**
 * The Bot in front after a new read of the crew. While the states cannot be known nothing is chosen anew: the one
 * in front keeps its place and its look through the gap. Else `frontOf` picks, and a Bot that comes forward now
 * comes to where he looks now (`look`).
 */
export function frontNow(was: Front, bots: readonly AskingBot[], known: boolean, put: ReadonlySet<string> | undefined, look: () => number): Front {
  if (!known) return was
  const slug = frontOf(bots, was.slug, put)
  return slug === was.slug ? was : { slug, yaw: slug ? look() : 0 }
}

/** What holds the low middle: a taken tray or the card's keyboard. */
export type LowBlock = 'tray' | 'keys' | null

/** What the rule of where each Bot is reads. */
export interface StageAsk {
  bots: readonly AskingBot[]
  /**
   * The Bot that stands in front (`frontOf`): it stays there until its own ask ends, whoever starts to ask
   * meanwhile. Left out: the one that `frontOf` picks with nobody in front.
   */
  front?: string
  homes: readonly Pt[]
  /** The pages he has open (shown, not put away), as the eye sees them. */
  pages: readonly SeenBox[]
  block: LowBlock
  /** The card's keyboard as the eye sees it, while it is out (`block` is `keys`). */
  keys?: SeenBox
  /**
   * The Bots that he put down and whose state has not changed since: each stands at the point where he let it go,
   * its desk beside it, whatever it does, and gives way to nothing.
   */
  put?: ReadonlySet<string>
  /** Whether the states can be known: while not, every Bot is at its desk and nothing comes forward. */
  known: boolean
  /** Whether Home's large surface stands in the middle (he pressed its strip). */
  homeLarge: boolean
  /**
   * Where he looked, to the right of straight ahead, at the moment the one that needs him began to ask. It is kept
   * until that ask ends, so the Bot does not swim with his head. Left out: straight ahead.
   */
  lookYaw?: number
}

/** Where one Bot is now, and what it does there. */
export interface Staged {
  /** Its feet now (before its drift), and which way it faces: a yaw of the room, or `null` for him. */
  at: Pt
  face: number | null
  /** What it does: works at its desk, idle away from it, failed at it, or needs him. */
  does: 'work' | 'idle' | 'failed' | 'needs'
  nap: boolean
  /** Its desk: where its top's middle is, which way it faces, lit (its Bot works at it), and not drawn (a page or Home's large surface covers it, or its Bot gave way). */
  desk: { at: Pt; yaw: number; lit: boolean; hidden: boolean }
  /** It needs him and came forward. */
  forward: boolean
  /** It gave way: a page, Home's large surface or the one that came forward would cover it at its place. */
  moved: boolean
  /** It needs him and no place in view is clear of the page he reads: its ask's sign stands at that page's edge here. */
  sign: Pt | null
}

/** The same height and distance, so many degrees farther round. */
const turned = (feet: readonly number[], byDeg: number): Pt => {
  const d = Math.hypot(feet[0], feet[2])
  const yaw = (yawOf(feet) + byDeg) * RAD
  return [Math.sin(yaw) * d, feet[1], -Math.cos(yaw) * d]
}

/** A desk as a thing that reaches from the middle of its top. */
const DESK: Reach = { left: PLACES.desk.wideM / 2, right: PLACES.desk.wideM / 2, top: 0.03, bottom: PLACES.desk.legM + 0.01 }

/** Whether a body at `feet` and a desk cover each other as the eye sees them. */
export function onDesk(feet: readonly number[], desk: readonly number[]): boolean {
  const look = { yaw: yawOf(feet), pitch: 0 }
  const [a, b] = [rectOf(feet, look), rectOf(desk, look, DESK)]
  return !!a && !!b && overlap(a, b, tan(PLACES.airDeg) / 2)
}

/** Whether a body is whole outside a look's view: no part of it at the view's edge. */
function outOfView(feet: readonly number[], look: Look): boolean {
  const r = rectOf(feet, look, LOOSE)
  const edge = tan(PLACES.viewHalfYaw)
  return !r || r.x0 > edge || r.x1 < -edge
}

/**
 * A place that one of `covers` would cover, stepped beside it: at its own height and distance, round to the side it
 * is on, to the first spot that none of `avoid` covers, where no face of `others` is covered and where it stands
 * over no desk. For a place that was in the look's view, a spot whole in that view is taken first; else one whole
 * outside it: never one that the view's edge cuts.
 */
function besideCovers(feet: Pt, covers: readonly SeenBox[], avoid: readonly SeenBox[], others: readonly Pt[], desks: readonly Pt[], look: Look): Pt {
  const hit = covers.find(c => onBox(feet, c))
  if (!hit) return feet
  const side = yawOf(feet) >= hit.yaw ? 1 : -1
  const free = (p: Pt) => !avoid.some(c => onBox(p, c)) && !others.some(o => facesClash(o, p)) && !desks.some(d => onDesk(p, d))
  const passes: [number, boolean][] = inView(feet, look) ? [[side, true], [-side, true], [side, false]] : [[side, false]]
  for (const [way, seen] of passes) for (let step = 1; step <= 150; step += 1) {
    const p = turned(feet, way * step)
    if (seen ? !inView(p, look) : !outOfView(p, look)) continue
    if (free(p)) return p
  }
  return feet
}

/** The rule of where each Bot is. Pure. */
export function stage(ask: StageAsk): Staged[] {
  const { bots, pages, homes } = ask
  // Where he looked when the one that needs him began to ask: it comes there, and what is in view is judged from there.
  const look: Look = { yaw: ask.lookYaw ?? 0, pitch: 0 }
  // While the states cannot be known every Bot is at its desk, except the one in front: it keeps its spot through the gap.
  const does = (state: string, slug?: string): Staged['does'] => (!ask.known ? (slug && slug === ask.front ? 'needs' : 'idle') : state === 'working' ? 'work' : state === 'failed' ? 'failed' : state === 'needs' ? 'needs' : 'idle')
  const out: Staged[] = bots.map((b, i) => ({
    at: homes[i], face: null, does: does(b.state, b.slug), nap: false, forward: false, moved: false, sign: null,
    desk: { at: deskAt(homes[i]), yaw: toHim(homes[i]), lit: false, hidden: false },
  }))

  // Only one of those that need him comes forward (`frontOf`). The others stay at their desks, their boards up:
  // nothing fans out to the side, and nothing stands past the view's edge.
  const put = (i: number) => !!ask.put?.has(bots[i].slug)
  const front = ask.front ?? frontOf(bots, '', ask.put)
  const first = out.findIndex((s, i) => s.does === 'needs' && bots[i].slug === front && !put(i))
  let column: SeenBox | null = null
  const comes = (i: number) => {
    const s = out[i]
    const f = ask.block ? PLACES.aside : PLACES.forward
    const onPage = (q: Pt) => pages.find(c => onBox(q, c, PLACES.airDeg, WITH_BOARD))
    let p = spotPoint(look.yaw + f.yawDeg, f.pitchDeg, f.distanceM)
    if (ask.block) {
      // A taken tray or the card's keyboard holds the low middle: it stands at the low side of where he looked.
      const at = (side: number) => spotPoint(look.yaw + side * f.yawDeg, f.pitchDeg, f.distanceM)
      // The card stands where its keyboard does, to one side of his look (`CARD.outYawDeg` when no keyboard is told):
      // the side away from it first, and never behind the keys.
      const keysYaw = ask.keys ? ask.keys.yaw : look.yaw + CARD.outYawDeg
      const away = ((keysYaw - look.yaw + 540) % 360) - 180 < 0 ? 1 : -1
      const keys = ask.keys
      const clear = (q: Pt) => !onPage(q) && !(keys && onBox(q, keys, PLACES.airDeg, WITH_BOARD))
      // Beside a taken tray: its own side first, and the side where it covers no face of a Bot that stays at its place.
      const own = yawOf(homes[i]) < look.yaw ? -1 : 1
      const faces = (q: Pt) => homes.some((h, j) => j !== i && facesClash(h, q))
      const sides = ask.block === 'keys' ? [away, -away] : [own, -own]
      const spots = sides.map(at).filter(clear)
      p = (ask.block === 'tray' ? spots.find(q => !faces(q)) : undefined) ?? spots[0] ?? at(sides[0])
    }
    const page = onPage(p)
    if (!page) { s.at = p; s.forward = true; return }
    // A page he reads is never covered: beside or under it, whole in what he sees; else at its desk, with the sign at the page's edge.
    const side = yawOf(homes[i]) >= page.yaw ? 1 : -1
    const d = PLACES.aside.distanceM
    const halfBody = Math.atan2(WITH_BOARD.right, d) / RAD
    const [halfYaw, halfPitch] = [Math.atan(page.x1) / RAD, Math.atan(page.y1) / RAD]
    // Beside the page, it and its board stand in the middle of what he sees, up and down: its own middle is that much lower.
    const lower = (Math.atan2(WITH_BOARD.top - MID_M, d) - Math.atan2(MID_M + WITH_BOARD.bottom, d)) / RAD / 2
    const tries: Pt[] = [
      spotPoint(page.yaw + side * (halfYaw + PLACES.airDeg + halfBody + 1), look.pitch - lower, d),
      spotPoint(page.yaw - side * (halfYaw + PLACES.airDeg + halfBody + 1), look.pitch - lower, d),
      spotPoint(page.yaw, page.pitch - halfPitch - PLACES.airDeg - Math.atan2(WITH_BOARD.top - MID_M, d) / RAD, d),
    ]
    const there = tries.find(t => inView(t, look, WITH_BOARD) && !pages.some(q => onBox(t, q, PLACES.airDeg, WITH_BOARD)))
    if (there) { s.at = there; s.forward = true; return }
    const reach = page.distanceM - 0.05
    const [edge, under] = [page.yaw + side * (halfYaw - 4), page.pitch - halfPitch - 1.6]
    s.sign = [Math.sin(edge * RAD) * reach, PAGES.eyeM + reach * tan(under), -Math.cos(edge * RAD) * reach]
  }
  if (first >= 0) {
    comes(first)
    // Wherever it came forward, the others give way to it and its board, as they do to a page.
    if (out[first].forward) column = forwardCovers(out[first].at, look)
  }

  // What a Bot gives way to: a page he has open, Home's large surface, and the one that came forward with its board.
  const covers: SeenBox[] = [...pages, ...(ask.homeLarge ? [HOME_BOX] : []), ...(column ? [column] : [])]
  const avoid: SeenBox[] = [...covers, HOME_STRIP]
  // A desk stays where it is. One that a page or Home's large surface covers is not drawn while that shows.
  const flat = [...pages, ...(ask.homeLarge ? [HOME_BOX] : [])]
  out.forEach(s => { s.desk.hidden = flat.some(c => onBox(s.desk.at, c, 0, DESK)) })
  const stays = out.map((s, i) => put(i) || (!s.forward && !covers.some(c => onBox(homes[i], c))))
  const stood: Pt[] = homes.filter((_, i) => stays[i])
  const drawn = out.filter(s => !s.desk.hidden).map(s => s.desk.at)
  out.forEach((s, i) => {
    if (s.forward || stays[i]) return
    s.at = besideCovers(homes[i], covers, avoid, stood, drawn, look)
    s.moved = s.at !== homes[i]
    stood.push(s.at)
    // The desk of a Bot that gave way is not drawn: an empty desk at the view's edge reads as a missing Bot.
    if (s.moved) s.desk.hidden = true
  })

  // At its desk, turned to it, the desk lit: a Bot that works. One that gave way has no desk beside it, and faces him.
  out.forEach(s => {
    if (s.does !== 'work' || s.moved) return
    s.face = Math.atan2(s.desk.at[0] - s.at[0], s.desk.at[2] - s.at[2])
    s.desk.lit = true
  })

  // One with nothing to do stands right beside its desk, turned a little away from it; one in three rests asleep.
  // Idle against working is told by the dark desk and the turn, not by distance: a desk never stands alone.
  out.forEach((s, i) => {
    if (s.does !== 'idle' || !ask.known || s.moved) return
    s.face = toHim(homes[i]) - deskSide(homes[i]) * PLACES.idleTurnDeg * RAD
    s.nap = !put(i) && seedOf(`${bots[i].slug}:rest`) % 3 === 0
  })
  return out
}

/** Whether a working Bot leans to its desk now: a lean in each beat, from a moment that its name gives. */
export function leansNow(slug: string, nowMs: number): boolean {
  const { everyMs, leanMs } = PLACES.beat
  return (nowMs + (seedOf(`${slug}:beat`) % everyMs)) % everyMs < leanMs
}

/** An idle Bot's drift now: so far to the side (across the eye's line) and up. */
export function driftNow(slug: string, nowMs: number): [number, number] {
  const d = PLACES.drift
  const t = nowMs / 1000
  const a = (seedOf(`${slug}:drift`) % 628) / 100
  return [d.sideM * Math.sin(t * d.sideRate + a), d.upM * Math.sin(t * d.upRate + a * 1.7)]
}

/** The drift as a step in the room, at a place `p`. */
export function driftAt(p: readonly number[], drift: readonly [number, number]): Pt {
  const [tx, tz] = across(p)
  return [p[0] + tx * drift[0], p[1] + drift[1], p[2] + tz * drift[0]]
}

/**
 * Where a lifted Bot is carried: where the ray from `from` along `dir` meets the sphere round the eye that its place
 * is on, so a drag keeps its depth. `null` when the ray does not meet it.
 */
export function onSphere(from: readonly number[], dir: readonly number[], radiusM: number): Pt | null {
  const o = [from[0], from[1] - PAGES.eyeM, from[2]]
  const a = dir[0] ** 2 + dir[1] ** 2 + dir[2] ** 2
  if (a < 1e-9) return null
  const b = 2 * (o[0] * dir[0] + o[1] * dir[1] + o[2] * dir[2])
  const c = o[0] ** 2 + o[1] ** 2 + o[2] ** 2 - radiusM ** 2
  const disc = b * b - 4 * a * c
  if (disc < 0) return null
  const t = (-b + Math.sqrt(disc)) / (2 * a)
  if (t <= 0) return null
  return [from[0] + t * dir[0], from[1] + t * dir[1], from[2] + t * dir[2]]
}

/** The distance from the eye to a body's middle, for a place: what a drag keeps. */
export const reachOf = (feet: readonly number[]): number => Math.hypot(feet[0], feet[1] + MID_M - PAGES.eyeM, feet[2])

/** A dragged place, held to the room's near and far limits. */
export function keptPoint(feet: readonly number[]): Pt {
  const mid: Pt = [feet[0], feet[1] + MID_M - PAGES.eyeM, feet[2]]
  const d = Math.hypot(...mid) || 1
  const k = Math.min(PLACES.farM, Math.max(PLACES.nearM, d)) / d
  const r = (v: number) => Math.round(v * 100) / 100
  return [r(mid[0] * k), r(mid[1] * k + PAGES.eyeM - MID_M), r(mid[2] * k)]
}
