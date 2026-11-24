/**
 * The crew in the room of pages: where each Bot stands, and which page a tap on one opens. Plain numbers; `Crew.tsx`
 * draws them.
 *
 * The Bots stand in one row on an arc round the person, a little nearer than the first page and under it. As the eye
 * sees them at the start, the top of a head is just under the line that a panel says under its page, so no Bot
 * stands in front of a page. With more than twelve, the rest stand in a second row behind the first, in its gaps: as
 * far up as the first page's bottom allows, which the eye sees a little higher than the row in front.
 */
import { M_PER_PX, PAGES, type Zone } from './layout'
import { chatDeepLinkSlot } from '../../../utils/navIntent'
import { MEMBER_PAGE, memberPath } from './streamPath'
import { WALLS } from './walls'

const RAD = Math.PI / 180

export const CREW = {
  /** How tall a Bot is. */
  heightM: 0.3,
  /** A body at rest floats this share of its height over the place of its feet (`botMotion.ts`, the rest pose and its bob). */
  hoverShare: 0.07,
  /** How wide a body is, as a share of its height (measured in a picture of the row; the small flick at each side reaches a little farther). */
  widthShare: 0.72,
  /** The front row is this much nearer than the first page. */
  nearerM: 0.3,
  /**
   * From one Bot to the next, along the arc: a body and a little air. Three whole bodies fit across an upright
   * phone's picture, and no more air than this lets them: a Bot that slumps may lean to within a hair of the next.
   */
  spacingM: 0.23,
  /**
   * So many stand in the front row: a crew of up to twelve stands in one row, where every Bot is seen whole and
   * takes its own tap. The row is then up to 100 degrees wide, and a person turns to its ends as to a page at the side.
   * shortcut: a Bot of the second row shows less than a third of its body over the row in front, and most of that
   * is inside the box of a Bot in front, which takes the tap. Give the body the first say, and the box a near miss,
   * when a crew of thirteen or more is to be tapped here.
   */
  frontMost: 12,
  /** The second row stands this much farther than the first. */
  behindM: 0.22,
  /** The line that a panel says under its page ends this far under the page (`Panel.tsx`: 32 CSS px tall, 12 mm under). */
  lineUnderM: 32 * M_PER_PX + 0.012,
  /** The top of a head in the front row is this many degrees under that line, as the eye sees it at the start. */
  underLineDeg: 0.3,
  /** The top of a head in the second row is this many degrees under the first page's bottom. */
  underPageDeg: 0.45,
  /** The ring at a Bot's feet that says its state, and the one that lights it: narrower than the space to its neighbour. */
  ringM: [0.09, 0.11],
} as const

/** Where one Bot stands: so far to the right of straight ahead, so far from the start head, the place of its feet so high (the body floats a little over it), and in which row. */
export interface CrewPlace { yawDeg: number; radiusM: number; feetM: number; row: 0 | 1 }

const pageBottomM = PAGES.eyeM - (PAGES.cssHeight * M_PER_PX) / 2
/** How far under level the eye sees a point that is `dropM` under it, `PAGES.distanceM` away. */
const underDeg = (dropM: number) => Math.atan(dropM / PAGES.distanceM) / RAD
/** How high a point is that the eye sees `deg` under level, `radiusM` away. */
const heightAt = (deg: number, radiusM: number) => PAGES.eyeM - radiusM * Math.tan(deg * RAD)

/** From the place of a Bot's feet to the top of its head, at rest. */
const TALL_M = CREW.heightM * (1 + CREW.hoverShare)

/** How far the front row is from the start head. */
export const crewRadiusM = PAGES.distanceM - CREW.nearerM

/**
 * The places of `count` Bots, in the crew's own order. Up to twelve stand in the front row, one of them straight
 * ahead; with an even number the row reaches half a place farther to the right than to the left, so that a Bot and
 * not a gap is under the page's middle. The rest stand behind, in the gaps.
 */
export function crewPlaces(count: number): CrewPlace[] {
  const front = Math.min(Math.max(0, Math.floor(count)), CREW.frontMost)
  const back = Math.max(0, Math.floor(count) - front)
  const r0 = crewRadiusM
  const r1 = r0 + CREW.behindM
  const top0 = heightAt(underDeg(PAGES.eyeM - pageBottomM + CREW.lineUnderM) + CREW.underLineDeg, r0)
  const top1 = heightAt(underDeg(PAGES.eyeM - pageBottomM) + CREW.underPageDeg, r1)
  const places: CrewPlace[] = []
  const step0 = (CREW.spacingM / r0) / RAD
  for (let i = 0; i < front; i += 1) places.push({ yawDeg: (i - Math.floor((front - 1) / 2)) * step0, radiusM: r0, feetM: top0 - TALL_M, row: 0 })
  // The second row: in the gaps of the first, as many to each side of the middle as there are.
  for (let i = 0; i < back; i += 1) places.push({ yawDeg: (i - Math.floor((back - 1) / 2) - 0.5) * step0, radiusM: r1, feetM: top1 - TALL_M, row: 1 })
  return places
}

/** The places a drag kept, by slug: how far to the right of straight ahead each Bot stands in its row, in degrees. */
export type KeptCrew = Readonly<Record<string, number>>

/** From one Bot to the next in a row, in degrees of its arc. */
export const crewStepDeg = (radiusM: number): number => (CREW.spacingM / radiusM) / RAD

/** The yaws that a Bot's middle may take in the front row: those of a crew of twelve, the band that no page stands in. */
export const CREW_BAND: readonly [number, number] = (() => {
  const yaws = crewPlaces(CREW.frontMost).map(p => p.yawDeg)
  return [Math.min(...yaws), Math.max(...yaws)] as const
})()

const clampBand = (yaw: number) => Math.max(CREW_BAND[0], Math.min(CREW_BAND[1], yaw))

/**
 * The yaw nearest `want`, inside the band, that is at least one step from every yaw of `taken`: `want` itself when it
 * is free. A drop near another Bot lands there, and nobody else moves. With no free yaw in the band, `want` in it.
 */
export function freeYaw(want: number, taken: readonly number[], stepDeg: number): number | null {
  const free = (y: number) => taken.every(t => Math.abs(y - t) >= stepDeg - 1e-6)
  const w = clampBand(want)
  if (free(w)) return w
  const tries = [CREW_BAND[0], CREW_BAND[1], ...taken.flatMap(t => [t - stepDeg, t + stepDeg])]
    .filter(y => y >= CREW_BAND[0] - 1e-6 && y <= CREW_BAND[1] + 1e-6 && free(y))
  if (!tries.length) return null
  return tries.reduce((best, y) => (Math.abs(y - w) < Math.abs(best - w) ? y : best))
}

/**
 * A Bot's moves of its own in the row: a stroll goes at most this far along the row and this much nearer or farther,
 * at this speed; a Bot that asks takes one step this long toward him; a Bot walks back to its place at the faster
 * speed. A stroll along the row takes at most half of the air between its body and the body of the neighbour on that
 * side, less `airM`, so two bodies, and their press boxes, never meet; in a full row it moves only nearer or farther.
 */
export const STROLL = {
  alongM: 0.09, depthM: 0.03, speedMps: 0.05, askStepM: 0.1, homeMps: 0.15, airM: 0.004,
  /** A stroll shorter than this along the row is not seen: with no side that free, the look around is a turn in place. */
  minAlongM: 0.03,
  /** The turn in place: this far to the side (about 40 degrees), out and back over `turnMs`. */
  turnRad: 0.7, turnMs: 3600,
} as const

/**
 * What a Bot walks to of its own: its place (`home`), a stroll (`look:…`), or the step toward him (`needs`). `turn`:
 * a look around in place, the body turned this far to one side and back (`STROLL.turnMs`); 0 or none for a walk.
 */
export interface StrollGoal { key: string; at: [number, number, number]; nap: boolean; turn?: number }

/** What the rule of a stroll reads (`strollGoal`). */
export interface StrollAsk {
  /** Its place, as a point of the room, and the head's place. */
  home: readonly [number, number, number]
  head: readonly [number, number, number]
  state: 'idle' | 'working' | 'needs' | 'failed'
  /** No moves of its own now: at the desk, with reduced motion, in the quiet time, or while the states cannot be known. */
  rest: boolean
  /** The pick at rest (the land's `idleLife`, or a test's switch): a stroll by its key and its way (radians, 0 along the row to the right), a nap, or neither. */
  pick: { stroll: { key: string; angle: number } | null; nap: boolean }
  /** A stroll that a pointer or a press ended: it does not come back. */
  ended: string
  /** Where the other Bots of its row stand or walk to now. */
  others: readonly (readonly [number, number, number])[]
  /** How wide a body is. */
  widthM: number
}

/** Where a Bot walks of its own now. A pure rule of `Crew.tsx`'s. */
export function strollGoal(ask: StrollAsk): StrollGoal {
  const [x, y, z] = ask.home
  const rest: StrollGoal = { key: 'home', at: [x, y, z], nap: false }
  if (ask.rest) return rest
  if (ask.state === 'needs') {
    const d = Math.hypot(ask.head[0] - x, ask.head[2] - z) || 1
    return { key: 'needs', at: [x + ((ask.head[0] - x) / d) * STROLL.askStepM, y, z + ((ask.head[2] - z) / d) * STROLL.askStepM], nap: false }
  }
  if (ask.state !== 'idle') return rest
  const stroll = ask.pick.stroll
  if (!stroll) return { ...rest, nap: ask.pick.nap }
  if (stroll.key === ask.ended) return rest
  // The row's tangent at its place (to the right as he sees it), and the way toward the room's middle.
  const yaw = Math.atan2(x, -z)
  const t: [number, number] = [Math.cos(yaw), Math.sin(yaw)]
  const r = Math.hypot(x, z) || 1
  const yawAt = (along: number) => Math.atan2(x + t[0] * along, -(z + t[1] * along)) / RAD
  /** How far it may go along the row to one side (`side` 1 or -1): inside the band, and half the air to the nearest body there. */
  const room = (side: 1 | -1) => {
    let most: number = STROLL.alongM
    for (const o of ask.others) {
      const ahead = ((o[0] - x) * t[0] + (o[2] - z) * t[1]) * side
      if (ahead <= 0) continue
      most = Math.min(most, Math.max(0, (ahead - ask.widthM) / 2 - STROLL.airM))
    }
    while (most > 0 && (yawAt(side * most) < CREW_BAND[0] || yawAt(side * most) > CREW_BAND[1])) most = Math.max(0, most - 0.01)
    return most
  }
  const want = Math.cos(stroll.angle) * STROLL.alongM
  const wanted: 1 | -1 = want >= 0 ? 1 : -1
  // The side it wanted, or the other one when only that one is free.
  const side: 1 | -1 = room(wanted) >= STROLL.minAlongM ? wanted : (-wanted as 1 | -1)
  const reach = room(side)
  // No side is free: the look around shows in place, as a turn of its body to the side it wanted and back.
  if (reach < STROLL.minAlongM) return { key: stroll.key, at: [x, y, z], nap: false, turn: wanted * STROLL.turnRad }
  const along = Math.min(Math.max(Math.abs(want), STROLL.minAlongM), reach) * side
  const out = (Math.sin(stroll.angle) * STROLL.depthM) / r
  return { key: stroll.key, at: [x + t[0] * along + x * out, y, z + t[1] * along + z * out], nap: false }
}

/**
 * Where each Bot of `slugs` stands, in the crew's order: a front-row Bot at the yaw a drag kept for it, when it is
 * free; every other one at the first free place of the row as `crewPlaces` lays it out, else at the free yaw
 * nearest its own. A slug that `kept` names and the crew does not is no one's place.
 */
export function crewHomes(slugs: readonly string[], kept: KeptCrew = {}): CrewPlace[] {
  const base = crewPlaces(slugs.length)
  const out: (CrewPlace | null)[] = base.map(() => null)
  const step = crewStepDeg(crewRadiusM)
  const taken: number[] = []
  slugs.forEach((slug, i) => {
    const yaw = kept[slug]
    if (base[i]?.row !== 0 || typeof yaw !== 'number' || !Number.isFinite(yaw)) return
    // A kept place with no free yaw left near it is no place: the Bot stands as a new one would.
    const at = freeYaw(yaw, taken, step)
    if (at === null) return
    taken.push(at)
    out[i] = { ...base[i], yawDeg: at }
  })
  slugs.forEach((_slug, i) => {
    if (out[i] || !base[i]) return
    if (base[i].row !== 0) { out[i] = base[i]; return }
    const first = base.find(p => p.row === 0 && taken.every(t => Math.abs(p.yawDeg - t) >= step - 1e-6))
    const at = first ? first.yawDeg : freeYaw(base[i].yawDeg, taken, step)
    if (at === null) return
    taken.push(at)
    out[i] = { ...base[i], yawDeg: at }
  })
  // The kept places leave no free place for every Bot: the row stands in its own order, as with nothing kept.
  if (out.some(p => !p)) return base
  return out as CrewPlace[]
}

/** The yaw at which a ray from `from` along `dir` meets the arc of `radiusM` round the room's middle, or `null`. */
export function yawOnArc(from: readonly [number, number, number], dir: readonly [number, number, number], radiusM: number): number | null {
  const [ox, , oz] = from
  const [dx, , dz] = dir
  const a = dx * dx + dz * dz
  if (a < 1e-9) return null
  const b = 2 * (ox * dx + oz * dz)
  const c = ox * ox + oz * oz - radiusM * radiusM
  const disc = b * b - 4 * a * c
  if (disc < 0) return null
  const t = (-b + Math.sqrt(disc)) / (2 * a)
  if (t <= 0) return null
  return Math.atan2(ox + t * dx, -(oz + t * dz)) / RAD
}

/** A place as a point of the room: the Bot's feet. Straight ahead is -z. */
export function crewPoint(p: CrewPlace): [number, number, number] {
  const yaw = p.yawDeg * RAD
  return [Math.sin(yaw) * p.radiusM, p.feetM, -Math.cos(yaw) * p.radiusM]
}

/** How far under level the eye at the start sees the top of a Bot's head, and its feet, in degrees. */
export function crewSeen(p: CrewPlace): { topDeg: number; feetDeg: number } {
  return { topDeg: Math.atan((PAGES.eyeM - p.feetM - TALL_M) / p.radiusM) / RAD, feetDeg: Math.atan((PAGES.eyeM - p.feetM) / p.radiusM) / RAD }
}

/**
 * The place under the first page that belongs to the crew, as the eye at the room's middle sees it: the front row
 * of a crew of twelve from the top of a head down to the ring at the feet, and under and beside it the desk of the
 * drawn room, down to its near edge. No page is stood there (`layout.ts`, `placeNew`): a page behind a Bot would
 * lose its presses to the Bot, and one behind the desk would be hidden by it. It is the same with a smaller crew
 * and with the room off, so that a page's place does not depend on who is in the crew today.
 */
export const CREW_ZONE: Zone = (() => {
  const row = crewPlaces(CREW.frontMost)
  const half = Math.atan(Math.max((CREW.heightM * CREW.widthShare) / 2, CREW.ringM[1]) / crewRadiusM) / RAD
  const yaws = row.map(p => p.yawDeg)
  const desk = WALLS.desk
  return {
    yawMinDeg: Math.min(Math.min(...yaws) - half, -desk.halfDeg),
    yawMaxDeg: Math.max(Math.max(...yaws) + half, desk.halfDeg),
    pitchMinDeg: -Math.atan((PAGES.eyeM - desk.topM + desk.thickM) / desk.innerM) / RAD,
    pitchMaxDeg: -crewSeen(row[0]).topDeg,
  }
})()

/**
 * The page that a tap on a Bot opens on a panel: the page the dashboard itself opens when a person picks that Bot in
 * its list, the Bots page with that Bot's name. Never the Bots page with no name: that one shows whichever Bot the
 * browser showed last, with its message box, and what a person types there goes to that other Bot.
 *
 * `name` is the name every route of a Bot is keyed on. Null when no address can name it: the tap then opens nothing.
 */
export const botPage = (bot: { name: string }): string | null => memberPath(bot.name)

/** A Bot as the rule of its page reads it: its name, its own thread and the sessions it is on. */
export interface BotSessions { name: string; thread: string; things: readonly { slot: string }[] }

/** The sessions of a Bot: its own thread and every session it is on. */
const sessionsOf = (bot: BotSessions): Set<string> => new Set([bot.thread, ...bot.things.map(t => t.slot)].filter(Boolean))

/**
 * Whether a panel is a Bot's page for a second press on its seat (`desk=1`). It is when it shows that Bot's page now,
 * or the chat of one of its sessions (the session the chat's address names, `chatDeepLinkSlot`); or when it was
 * opened with that Bot's page and has not gone to another Bot since. A panel that went to another Bot's page, or to
 * the chat of another Bot's session, is that Bot's, never the first one's.
 *
 * `now` is the panel's page now, path and query; `openedWithIt` says the panel was opened with the Bot's page.
 * With no `desk=1` none of this is read: a tap on a Bot finds its page as the eighth candidate did.
 */
export function isBotsPage(now: string | null, openedWithIt: boolean, bot: BotSessions, crew: readonly BotSessions[]): boolean {
  const own = botPage(bot)
  if (now !== null && now === own) return true
  const sid = now ? chatDeepLinkSlot(now) : ''
  if (sid && sessionsOf(bot).has(sid)) return true
  if (!openedWithIt) return false
  if (now !== null && now.startsWith(MEMBER_PAGE)) return false
  return !(sid && crew.some(o => o.name !== bot.name && sessionsOf(o).has(sid)))
}
