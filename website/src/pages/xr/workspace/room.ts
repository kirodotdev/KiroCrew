/**
 * The room that comes back: which pages stand where, kept in the browser's own store on this device, and read
 * again when the workspace opens. Nothing of it is sent to the gateway (a panel's tab gets the page's stored
 * preferences, but none of the XR page's own keys: `streamSource.ts`), and the phone and the glasses each have
 * their own.
 *
 * One key holds one record with a version: for each page the path its tab showed last, its place round the head
 * (so far to the right of straight ahead, so far up, so far away), its size in CSS pixels, and whether a hand
 * placed it. The record also says whether the drawn room is switched on (`walls`; left out when it is off, so a
 * record of an older build reads as off). What is read is treated as anyone's text: a record of another version, or one that is not this
 * shape, is no room at all; a page whose path the gateway no longer lets through, or whose numbers are out of
 * range, is dropped with no word. Nothing here touches the store or draws: the workspace does both.
 */
import { CREW_BAND, CREW_ZONE, type KeptCrew } from './crewRow'
import { deskToKeep, keptDesk, type DeskPlace } from './desk'
import { PLACES, type KeptPlaces, type Pt } from './places'
import { PAGES, inZone, placeNew, type Slot, type Standing, M_PER_PX } from './layout'
import { twoBeforeOf, type TwoBefore } from './twoUp'
import { MOST_LIVE_PAGES } from './source'
import { livePath } from './streamPath'
import { parseSeen, parseSince, type Seen, type Since } from './since'

/** Where the room is kept in the browser's store. */
export const ROOM_KEY = 'kirocrew:xr-room'
/** The record's shape. A record of another number is dropped. */
export const ROOM_VERSION = 1

/** One page of the room, as it is kept. */
export interface KeptPage {
  /** The path its tab showed last, with no query. */
  path: string
  /** Its place round the head. */
  at: Slot
  /** Its size, in CSS pixels. */
  css: [number, number]
  /** A hand placed it: it never moves for a newcomer. */
  byHand: boolean
  /** It is one of two in view (`twoUp.ts`): where and as big as it was before, to go back to when the pair ends. */
  two?: TwoBefore
}

/** A page of the room as the workspace opens it. */
export interface RoomPage extends KeptPage { id: string }

const num = (v: unknown, min: number, max: number): number | null => (typeof v === 'number' && Number.isFinite(v) && v >= min && v <= max ? v : null)

function keptPage(raw: unknown): KeptPage | null {
  if (!raw || typeof raw !== 'object') return null
  const r = raw as Record<string, unknown>
  const path = livePath(typeof r.path === 'string' ? r.path : null)
  const at = (r.at && typeof r.at === 'object' ? r.at : {}) as Record<string, unknown>
  const yawDeg = num(at.yawDeg, -180, 180)
  const pitchDeg = num(at.pitchDeg, -89, 89)
  const distanceM = num(at.distanceM, PAGES.nearM, PAGES.farM)
  const css = Array.isArray(r.css) ? r.css : []
  const w = num(css[0], PAGES.minCssWidth, PAGES.maxCssWidth)
  const h = num(css[1], PAGES.minCssHeight, PAGES.maxCssHeight)
  if (!path || yawDeg === null || pitchDeg === null || distanceM === null || w === null || h === null) return null
  const two = twoBeforeOf(r.two)
  const byHand = r.byHand === true
  // A page no hand placed, kept level by a build before D74 (level was the default then), takes this build's
  // default pitch; a page he placed keeps its record.
  const pitch = !byHand && Math.abs(pitchDeg) < 0.01 ? PAGES.lowerDeg : pitchDeg
  return { path, at: { yawDeg, pitchDeg: pitch, distanceM }, css: [Math.round(w), Math.round(h)], byHand, ...(two ? { two } : {}) }
}

/** The pages of the room that `text` keeps: none when it is no record of this version. */
export function readRoom(text: string | null | undefined): KeptPage[] {
  if (!text || text.length > 64_000) return []
  let record: unknown
  try { record = JSON.parse(text) } catch { return [] }
  if (!record || typeof record !== 'object') return []
  const r = record as Record<string, unknown>
  if (r.v !== ROOM_VERSION || !Array.isArray(r.pages)) return []
  const pages: KeptPage[] = []
  for (const raw of r.pages) {
    const page = keptPage(raw)
    if (page && pages.length < MOST_LIVE_PAGES) pages.push(page)
  }
  return pages
}

/** Whether the record that `text` keeps says that the drawn room is switched on. It starts off. */
export function readWalls(text: string | null | undefined): boolean {
  if (!text || text.length > 64_000) return false
  try {
    const record = JSON.parse(text) as unknown
    return !!record && typeof record === 'object' && (record as Record<string, unknown>).v === ROOM_VERSION && (record as Record<string, unknown>).walls === true
  } catch { return false }
}

/**
 * The desk that the record keeps (`desk.ts`), or `null`. It is an optional field of the same record at the same
 * version: a reader that does not know it reads the pages as before.
 */
export function readDesk(text: string | null | undefined): DeskPlace | null {
  if (!text || text.length > 64_000) return null
  try {
    const record = JSON.parse(text) as unknown
    if (!record || typeof record !== 'object' || (record as Record<string, unknown>).v !== ROOM_VERSION) return null
    return keptDesk((record as Record<string, unknown>).desk)
  } catch { return null }
}

/**
 * Whether the workspace opens with its real pages put away (the Home surface's switch, `useHome.ts`). With no record
 * kept (none, or one that is not a record of this version) it does: Home is the room's main panel, and a page is
 * taken out when he asks for one. With a record kept the room opens as it was kept. The switch is an optional field
 * of the same record at the same version, left out while the pages show: a record of an older build, which has no
 * such field, opens with its pages showing, and an older build reads this record's pages as before.
 */
export function pagesHiddenAtStart(text: string | null | undefined, showWord = false): boolean {
  // `showWord`: the address says `pages=show`. It is read only where no record is kept: a room with no record then
  // opens with its pages showing, as a check that reads a page needs it. A kept room opens as it was kept.
  if (!text || text.length > 64_000) return !showWord
  try {
    const record = JSON.parse(text) as unknown
    if (!record || typeof record !== 'object' || (record as Record<string, unknown>).v !== ROOM_VERSION || !Array.isArray((record as Record<string, unknown>).pages)) return !showWord
    return (record as Record<string, unknown>).pagesHidden === true
  } catch { return !showWord }
}

/** The most Bots whose places the record keeps. */
const CREW_KEPT_MOST = 64

/**
 * The places in the row that a drag gave Bots (`crewRow.crewHomes`), by slug, or none. It is an optional field of the
 * same record at the same version: a reader that does not know it reads the pages as before. A yaw out of the row's
 * band, or a slug that is no short text, is dropped.
 */
export function readCrew(text: string | null | undefined): KeptCrew {
  if (!text || text.length > 64_000) return {}
  try {
    const record = JSON.parse(text) as unknown
    if (!record || typeof record !== 'object' || (record as Record<string, unknown>).v !== ROOM_VERSION) return {}
    const raw = (record as Record<string, unknown>).crew
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return {}
    const out: Record<string, number> = {}
    for (const [slug, yaw] of Object.entries(raw as Record<string, unknown>).slice(0, CREW_KEPT_MOST)) {
      const y = num(yaw, CREW_BAND[0] - 0.01, CREW_BAND[1] + 0.01)
      if (slug && slug.length <= 128 && y !== null) out[slug] = y
    }
    return out
  } catch { return {} }
}

/**
 * The places that a drag gave Bots with no row (`places.ts`), by slug, as points of the room: a Bot's
 * feet where it works. An optional field of the same record, beside `crew`: a reader that does not know it reads
 * the rest as before, and the row's yaws in `crew` are kept whatever this one holds. A point out of the room's
 * reach, or one that is no three numbers, is dropped.
 */
export function readPlaces(text: string | null | undefined): KeptPlaces {
  if (!text || text.length > 64_000) return {}
  try {
    const record = JSON.parse(text) as unknown
    if (!record || typeof record !== 'object' || (record as Record<string, unknown>).v !== ROOM_VERSION) return {}
    const raw = (record as Record<string, unknown>).places
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return {}
    const out: Record<string, Pt> = {}
    for (const [slug, at] of Object.entries(raw as Record<string, unknown>).slice(0, CREW_KEPT_MOST)) {
      if (!slug || slug.length > 128 || !Array.isArray(at) || at.length !== 3) continue
      const x = num(at[0], -PLACES.farM, PLACES.farM)
      const y = num(at[1], -1, 5)
      const z = num(at[2], -PLACES.farM, PLACES.farM)
      if (x !== null && y !== null && z !== null) out[slug] = [x, y, z]
    }
    return out
  } catch { return {} }
}

/** The Bot in front with no row, as the record keeps it: its slug, the ask it came for, and where he looked then (degrees to the right). */
export interface KeptFront { slug: string; ask: string; yaw: number }

/**
 * The Bot that stood in front when the room was kept (`places.ts`, `frontNow`), or `null`. After a reload it stands at
 * the look it came to, while it still holds that ask: it does not jump to the start look. An optional field.
 */
export function readFront(text: string | null | undefined): KeptFront | null {
  if (!text || text.length > 64_000) return null
  try {
    const record = JSON.parse(text) as Record<string, unknown> | null
    const raw = record && record.v === ROOM_VERSION ? record.front as Record<string, unknown> | undefined : undefined
    if (!raw || typeof raw !== 'object') return null
    const yaw = num(raw.yaw, -180, 180)
    if (typeof raw.slug !== 'string' || !raw.slug || raw.slug.length > 128 || typeof raw.ask !== 'string' || !raw.ask || raw.ask.length > 256 || yaw === null) return null
    return { slug: raw.slug, ask: raw.ask, yaw }
  } catch { return null }
}

/**
 * His unsent note on an ask's board, and whether a third take put that board by (D83 1), by the ask's id (M45): a
 * reload brings the board up at its Bot with the note on it. Kept here, in this browser's own store, and nowhere
 * else: the record is never sent to the gateway (`useKeptRoom.ts`, `keepRoom`). An optional field.
 */
export type KeptNotes = Readonly<Record<string, { note: string; put: boolean; /** The ask's Bot, so a note whose ask ended can be shown at it once (F2). */ bot?: string; /** Its ask ended with the note unsent (M55): the note waits for his hand-over or his throw, and no ask takes it. */ ended?: boolean }>>
/** At the most this many notes (the newest: the book is in the order they were written), each this long: the whole record must stay under what `readRoom` reads. */
export const NOTES_KEPT = { most: 8, chars: 1000 } as const
export function readNotes(text: string | null | undefined): Record<string, { note: string; put: boolean; bot?: string; ended?: boolean }> {
  if (!text || text.length > 64_000) return {}
  try {
    const record = JSON.parse(text) as Record<string, unknown> | null
    const raw = record && record.v === ROOM_VERSION ? record.notes : undefined
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return {}
    const out: Record<string, { note: string; put: boolean; bot?: string; ended?: boolean }> = {}
    for (const [id, v] of Object.entries(raw as Record<string, unknown>).slice(0, NOTES_KEPT.most)) {
      const n = v as { note?: unknown; put?: unknown; bot?: unknown; ended?: unknown } | null
      if (!id || id.length > 256 || !n || typeof n.note !== 'string' || n.note.length > NOTES_KEPT.chars) continue
      if (!n.note.trim() && n.put !== true) continue
      out[id] = { note: n.note, put: n.put === true, ...(typeof n.bot === 'string' && n.bot && n.bot.length <= 128 ? { bot: n.bot } : {}), ...(n.ended === true ? { ended: true } : {}) }
    }
    return out
  } catch { return {} }
}

/**
 * The asks whose board he handed back (`places.ts`, `staysHome`): by the Bot's slug, the ask's id. After a reload such
 * a Bot stays at its desk while it still holds that ask. An optional field.
 */
export function readHome(text: string | null | undefined): Record<string, string> {
  if (!text || text.length > 64_000) return {}
  try {
    const record = JSON.parse(text) as Record<string, unknown> | null
    const raw = record && record.v === ROOM_VERSION ? record.home : undefined
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return {}
    return Object.fromEntries(Object.entries(raw as Record<string, unknown>).slice(0, CREW_KEPT_MOST).filter(([slug, ask]) => !!slug && slug.length <= 128 && typeof ask === 'string' && !!ask && ask.length <= 256)) as Record<string, string>
  } catch { return {} }
}

/** The most characters that the visit and what is left of it may take of the record, which is read only up to 64,000. */
export const VISIT_MOST_CHARS = 24_000

/** His last visit (`since.ts`, `Seen`): when it was, and each Bot's asks and cuts then. An optional field. */
export function readSeen(text: string | null | undefined): Seen | null {
  if (!text || text.length > 64_000) return null
  try {
    const record = JSON.parse(text) as Record<string, unknown> | null
    return record && record.v === ROOM_VERSION ? parseSeen(record.seen) : null
  } catch { return null }
}

/** What changed since the visit before and is not read yet (`since.ts`, `Since`). An optional field. */
export function readSince(text: string | null | undefined): Since | null {
  if (!text || text.length > 64_000) return null
  try {
    const record = JSON.parse(text) as Record<string, unknown> | null
    return record && record.v === ROOM_VERSION ? parseSince(record.since) : null
  } catch { return null }
}

/** The states a put-down mark may hold (`XrBot.state`). */
const PUT_STATES = new Set(['working', 'idle', 'failed', 'needs'])

/**
 * The Bots that he put down and that stand at that point still (`places.ts`, `StageAsk.put`): by slug, the state each
 * was in when he let go. The mark holds until that state changes, over a reload too. An optional field beside
 * `places`; a mark for a Bot with no kept point, or with a state that is none, is dropped.
 */
export function readPut(text: string | null | undefined): Record<string, string> {
  const points = readPlaces(text)
  try {
    const raw = (JSON.parse(text ?? '') as Record<string, unknown>).put
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return {}
    return Object.fromEntries(Object.entries(raw as Record<string, unknown>).filter(([slug, state]) => !!points[slug] && typeof state === 'string' && PUT_STATES.has(state))) as Record<string, string>
  } catch { return {} }
}

/**
 * The record to keep for these pages, for the drawn room when it is switched on, for the desk when one stands, and
 * for the places in the row that a drag gave Bots, for the real pages when they are put away, and for the places
 * with no row that a drag gave Bots, with the mark of each that stands where he put it, the one that stands in front,
 * and the asks whose board he handed back; and for the line of what changed since he left (`since.ts`), this visit
 * and what is left to read.
 */
export function writeRoom(pages: readonly KeptPage[], walls = false, desk: DeskPlace | null = null, crew: KeptCrew = {}, pagesHidden = false, places: KeptPlaces = {}, put: Readonly<Record<string, string>> = {}, front: KeptFront | null = null, home: Readonly<Record<string, string>> = {}, seen: Seen | null = null, since: Since | null = null, notes: KeptNotes = {}): string {
  const round = (v: number, places: number) => Math.round(v * 10 ** places) / 10 ** places
  const kept = Object.entries(crew).slice(0, CREW_KEPT_MOST)
  const points = Object.entries(places).slice(0, CREW_KEPT_MOST)
  const marks = Object.entries(put).filter(([slug]) => points.some(([s]) => s === slug))
  // The visit never crowds the room out of its record: a record that is too long is read as none (`readRoom`), so a
  // visit too long to keep is left out, and the line of what changed is then not shown at the next entry.
  const visitFits = JSON.stringify([seen, since]).length <= VISIT_MOST_CHARS
  return JSON.stringify({
    v: ROOM_VERSION,
    ...(walls ? { walls: true } : {}),
    ...(pagesHidden ? { pagesHidden: true } : {}),
    ...(desk ? { desk: deskToKeep(desk) } : {}),
    ...(kept.length ? { crew: Object.fromEntries(kept.map(([slug, yaw]) => [slug, round(yaw, 2)])) } : {}),
    ...(points.length ? { places: Object.fromEntries(points.map(([slug, p]) => [slug, p.map(v => round(v, 2))])) } : {}),
    ...(marks.length ? { put: Object.fromEntries(marks) } : {}),
    ...(front ? { front: { slug: front.slug, ask: front.ask, yaw: round(front.yaw, 1) } } : {}),
    ...(Object.keys(home).length ? { home: Object.fromEntries(Object.entries(home).slice(0, CREW_KEPT_MOST)) } : {}),
    ...(Object.keys(notes).length ? { notes: Object.fromEntries(Object.entries(notes).slice(-NOTES_KEPT.most).map(([id, n]) => [id, { note: n.note.slice(0, NOTES_KEPT.chars), ...(n.put ? { put: true } : {}), ...(n.bot ? { bot: n.bot } : {}), ...(n.ended ? { ended: true } : {}) }])) } : {}),
    ...(visitFits ? { ...(seen ? { seen } : {}), ...(since ? { since } : {}) } : {}),
    pages: pages.slice(0, MOST_LIVE_PAGES).map(p => ({ path: p.path, at: { yawDeg: round(p.at.yawDeg, 2), pitchDeg: round(p.at.pitchDeg, 2), distanceM: round(p.at.distanceM, 3) }, css: [Math.round(p.css[0]), Math.round(p.css[1])], byHand: p.byHand, ...(p.two ? { two: { at: { yawDeg: round(p.two.at.yawDeg, 2), pitchDeg: round(p.two.at.pitchDeg, 2), distanceM: round(p.two.at.distanceM, 3) }, css: p.two.css, byHand: p.two.byHand } } : {}) })),
  })
}

/** One open page as the workspace sees it when it keeps the room. */
export interface StandingPage {
  id: string
  /** What would be kept of it: the path its tab shows, where it stands now, its size, whether a hand placed it. */
  page: KeptPage
  /** The gateway had no room for it: it holds twelve pages for all of the owner's sockets together. */
  full: boolean
}

/**
 * The pages to keep of those that are open. A page that the gateway had no room for stays in the record when it
 * came from the record (`fromRecord` holds the ids the workspace opened with from it): a socket that dropped is
 * held by the gateway for a while, its pages still count, and the same room opened again on a new socket does not
 * fit beside them. Left out is only a page that was opened in this visit and had no room.
 */
export function roomToKeep(open: readonly StandingPage[], fromRecord: ReadonlySet<string>): KeptPage[] {
  return open.filter(p => !p.full || fromRecord.has(p.id)).map(p => p.page)
}

/** How often a page of the kept room that the gateway had no room for is asked for again, and how many times. */
export const ASK_AGAIN = { everyMs: 5000, most: 8 } as const

/**
 * The pages to ask the gateway for once more: those of the kept record that it had no room for, each
 * `ASK_AGAIN.most` times at most (`asked` counts them, and is counted up here). The room it lacked is held by a
 * socket that dropped, and is free again when the gateway lets that one go. A page that was opened in this visit
 * as one too many is not asked for again: the person closes a page first.
 */
export function pagesToAskAgain(open: readonly StandingPage[], fromRecord: ReadonlySet<string>, asked: Map<string, number>): string[] {
  const ids: string[] = []
  for (const p of open) {
    const times = asked.get(p.id) ?? 0
    if (!p.full || !fromRecord.has(p.id) || times >= ASK_AGAIN.most) continue
    asked.set(p.id, times + 1)
    ids.push(p.id)
  }
  return ids
}

/** Whether this load is a reload of the tab: the address was read once already. */
export function reloaded(): boolean {
  try {
    return (performance.getEntriesByType('navigation')[0] as PerformanceNavigationTiming | undefined)?.type === 'reload'
  } catch { return false }
}

/** Whether a kept page is the page that was asked for: that path, or a page under it (a session of `/chat`, a tab of `/settings`). */
const holds = (kept: KeptPage, asked: string): boolean => kept.path === asked || kept.path.startsWith(`${asked}/`)

/**
 * The pages the workspace opens with: the kept room; the page that the address asks for, in a free place in front,
 * when the room does not hold it. A room with no record and no page asked for opens with no page (T9, D19): Home, the
 * Bots and the palette are the room, and the crew's chat page comes only when he opens it. The page that the address asks for
 * has the id `page`, whether it was kept or is new. The kept pages come first, in the record's order: the first
 * `kept.length` of what is given back came from the record. On a reload of a room that has a record (`again`) the
 * address adds no page: it asked once, and a room he emptied or made empty (`rooms.ts`) stays as he left it.
 */
export function startRoom(kept: readonly KeptPage[], asked: string | null, again = false): RoomPage[] {
  const held = asked ? kept.findIndex(p => holds(p, asked)) : -1
  const pages: RoomPage[] = kept.map((p, i) => ({ ...p, id: i === held ? 'page' : `kept-${i + 1}` }))
  const add = asked && held < 0 && !again ? asked : null
  if (add) {
    const css: [number, number] = [PAGES.cssWidth, PAGES.cssHeight]
    const standing: Standing[] = pages.map(p => ({ id: p.id, at: p.at, widthM: p.css[0] * M_PER_PX, heightM: p.css[1] * M_PER_PX, user: true }))
    // The kept pages are where the person left them: none moves for the newcomer.
    pages.push({ id: 'page', path: add, at: placeNew(standing, css[0] * M_PER_PX, css[1] * M_PER_PX, 0, [CREW_ZONE]).at, css, byHand: false })
  }
  return pages
}

/**
 * The kept room with no page in the crew's place (`crewRow.ts`) that the layout itself stood there. A record of an
 * older build may hold a page in a low row behind the Bots. Such a page is stood once more, as a new page would
 * be, in the free place nearest to the direction it stood in, at its size. The other pages that no hand placed
 * may be stood again to make room for it, as for any newcomer. A page for which no clear place is left stays
 * where it was. The room is then kept as it stands, so this is done once.
 *
 * A page that a hand placed stays where the hand put it, also where it reaches into the crew's place: a page that
 * is pulled near is tall in the view, and the person set it up so.
 */
export function settleRoom(kept: readonly KeptPage[]): KeptPage[] {
  const pages = kept.map(p => ({ ...p }))
  const sized = (p: KeptPage) => ({ at: p.at, widthM: p.css[0] * M_PER_PX, heightM: p.css[1] * M_PER_PX })
  pages.forEach((page, i) => {
    if (page.byHand || !inZone(sized(page), CREW_ZONE)) return
    const others: Standing[] = pages.flatMap((p, j) => (j === i ? [] : [{ id: String(j), user: p.byHand, ...sized(p) }]))
    const got = placeNew(others, page.css[0] * M_PER_PX, page.css[1] * M_PER_PX, page.at.yawDeg, [CREW_ZONE])
    if (got.crosses) return
    for (const m of got.moved) pages[Number(m.id)].at = m.at
    page.at = got.at
  })
  return pages
}
