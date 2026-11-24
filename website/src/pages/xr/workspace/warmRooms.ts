/**
 * Warm rooms (D46): the rules. The pages of the rooms he left stay open at the gateway, out of view, so that a switch
 * back shows whole pages at once. A page out of view gets no picture and costs the gateway next to nothing; one that
 * was whole when it was hidden comes back whole at its first frame, and one that was hidden before it had filled
 * comes back as its shell (measured, `WARM-PLAN.md`). So only a page that was whole is ever kept.
 *
 * The gateway opens twelve pages for one person (`source.ts`, `MOST_LIVE_PAGES`). The rooms hold ten of them at most,
 * the standing room's and the warm ones' together; two are left for a page he opens and a Bot's page that comes up.
 * The room he stands in comes first, then the rooms he left, the most recent first. A room is warm whole or not at
 * all, so no room comes half at once and half late. A warm page always gives way: it is closed before anything of
 * his is refused for want of room.
 *
 * No state here: `useWarmRooms.ts` holds the pages.
 */
import { MOST_LIVE_PAGES, type RestFacts } from './source'

/** How many of the gateway's pages the rooms may hold, standing and warm together. */
export const ROOM_PAGES_MOST = MOST_LIVE_PAGES - 2
/**
 * How long a page must have shown its picture to count as whole (ms). A page's first pictures show its shell, then
 * grey boxes; its words come about two seconds after its first picture on the build host, later when it is busy.
 */
export const WHOLE_MS = 4000

/**
 * Warm ahead: a room he has not been in since the page loaded is opened out of his sight, one room at a time, and
 * kept once each of its pages has filled. Nothing in the tab says "this page is whole" but the gateway's lossless
 * still, which it sends once a page has not changed for 0.4 s (`screencast.py`, `SETTLE_S`), at most one every two
 * seconds. A still of the page's shell comes early, so only a still that came `afterLiveMs` after the page was live
 * counts. It need not be the newest picture: a page that is whole may go on moving a little (a caret, a pulse), and
 * then sends moving pictures between its stills for ever. A page that sends no such still counts `lateMs` after it
 * was live. A room that is not filled after `giveUpMs` is closed again, and is not tried again for `againMs`.
 */
export const AHEAD = { afterLiveMs: 6000, lateMs: 15_000, giveUpMs: 30_000, againMs: 300_000 } as const

/** Whether a page that is held open out of sight has filled (`AHEAD`). */
export function filled(f: RestFacts): boolean {
  if (f.liveForMs === null || f.lastPictureMs === null) return false
  return (f.stillAfterLiveMs !== null && f.stillAfterLiveMs >= AHEAD.afterLiveMs) || f.liveForMs >= AHEAD.lateMs
}

/**
 * The next room to warm ahead: of the rooms that are neither the standing one nor warm, the first (as the list has
 * them, after the ones he left most recently) whose pages that would have to come all fit inside `most` beside what
 * the rooms hold now. `null`: none fits, or none is cold.
 */
export function nextAhead<R extends { id: string; come: number }>(cold: readonly R[], held: number, most = ROOM_PAGES_MOST): R | null {
  return cold.find(r => r.come > 0 && held + r.come <= most) ?? null
}

/** One warm page: the id its panel had (the gateway knows it by this), its path and its size in CSS pixels. */
export interface WarmPage { id: string; path: string; css: [number, number] }
/** The warm pages of one room, and when he left it. */
export interface WarmRoom<P extends WarmPage = WarmPage> { room: string; pages: P[]; leftAt: number }

/** Most recent first. */
const recent = <P extends WarmPage>(rooms: readonly WarmRoom<P>[]): WarmRoom<P>[] => [...rooms].sort((a, b) => b.leftAt - a.leftAt)

/**
 * Whether the pages a room leaves behind may be kept: every one is live and was whole. A room with no page to leave
 * is not warm (there is nothing to keep).
 */
export function mayWarm(pages: readonly { live: boolean; whole: boolean }[]): boolean {
  return pages.length > 0 && pages.every(p => p.live && p.whole)
}

/**
 * Which warm rooms stay beside `standing` pages: the most recent first, each whole or not at all, inside `most`. A
 * room that does not fit closes, and so does every room left before it: the room left longest ago goes first.
 */
export function fitWarm<P extends WarmPage>(rooms: readonly WarmRoom<P>[], standing: number, most = ROOM_PAGES_MOST): { keep: WarmRoom<P>[]; close: WarmRoom<P>[] } {
  const keep: WarmRoom<P>[] = []
  const close: WarmRoom<P>[] = []
  let held = standing
  let full = false
  for (const r of recent(rooms)) {
    if (!full && held + r.pages.length <= most) { keep.push(r); held += r.pages.length } else { full = true; close.push(r) }
  }
  return { keep, close }
}

/**
 * The warm rooms that close so that `need` more pages can open when `open` are open (standing and warm together):
 * the room left longest ago first, until the gateway's `most` holds them. Empty when there is room already.
 */
export function giveWay<P extends WarmPage>(rooms: readonly WarmRoom<P>[], open: number, need: number, most = MOST_LIVE_PAGES): WarmRoom<P>[] {
  const close: WarmRoom<P>[] = []
  let total = open
  for (const r of recent(rooms).reverse()) {
    if (total + need <= most) break
    close.push(r)
    total -= r.pages.length
  }
  return close
}

/**
 * For the room he goes to: which warm page each page of its record takes. The record decides: a kept page takes a warm
 * page of the same path (each warm page once), from the room's own warm pages first, then from another warm room's.
 * `left` are the room's own warm pages that no kept page took (he closed that page since, here or in another tab):
 * they close. `robbed` are the other rooms that gave a page: they are no longer whole, and close.
 */
export function takeWarm<P extends WarmPage>(rooms: readonly WarmRoom<P>[], to: string, kept: readonly { path: string }[]): { taken: (P | null)[]; left: P[]; robbed: string[] } {
  const own = rooms.find(r => r.room === to)?.pages ?? []
  const others = recent(rooms.filter(r => r.room !== to))
  const used = new Set<string>()
  const robbed = new Set<string>()
  const taken = kept.map(k => {
    const mine = own.find(p => !used.has(p.id) && p.path === k.path)
    if (mine) { used.add(mine.id); return mine }
    for (const r of others) {
      const p = r.pages.find(q => !used.has(q.id) && q.path === k.path)
      if (p) { used.add(p.id); robbed.add(r.room); return p }
    }
    return null
  })
  return { taken, left: own.filter(p => !used.has(p.id)), robbed: [...robbed] }
}

/** The warm rooms that hold a page of this path: a page he opens, or a Bot's page that comes up, takes its place. */
export function holding<P extends WarmPage>(rooms: readonly WarmRoom<P>[], path: string): WarmRoom<P>[] {
  return rooms.filter(r => r.pages.some(p => p.path === path))
}
