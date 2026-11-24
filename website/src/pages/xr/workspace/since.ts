/**
 * SINCE YOU LEFT (D36), as plain rules. When he enters the room, one line says what changed since his last visit
 * in this browser, and each Bot that changed is lit at its desk. Nothing here draws or reads a store: `useSince.ts`
 * keeps the state, `HomeSince.tsx` draws the line, `Crew.tsx` draws the light.
 *
 * - The last visit is kept in the room's record (`room.ts`, `seen`): when it was, and for each Bot the asks and the
 *   cuts it had then and when it was last active. The record follows the crew while he is in the room, so what a Bot
 *   did while he was there is in it, and is not "since". A browser with no record shows no line: never "everything changed".
 * - A Bot is in one group only, the most pressing: it needs him (an ask that it did not have then), it failed (a cut
 *   that it did not have then, and that the gateway does not call old), or it finished (it is at rest, it was active
 *   after what the record holds of it, and its own thread has words that he has not read). "Finished" needs unread words, so the count
 *   is always a part of what there is to read, and a Bot that he read on another device is not counted here either.
 *   A cut needs none: a Bot's own thread turns unread only when the Bot spoke, and a cut is a turn with no words. That
 *   the cut is not in the record of his last visit is what says he has not seen it; its rim is red for as long
 *   (`sinceCuts`, `rims.ts`), so Home's "failed" counts it too.
 * - A Bot that needs him keeps its new cuts and whether it was active since: when the ask ends it falls to `failed`,
 *   then to `finished`, instead of going (`regroup`).
 * - It clears as he deals with each one: the ask is answered (on any device), the cut runs again or he taps that Bot,
 *   the words are read (on any device). A press on the line clears all of it. What is left is kept in the record (`since`), so a reload does
 *   not wipe it; what changes while he is in the room is not "since".
 * - The invariant: the Bots that are lit are exactly the Bots that the line counts.
 */
import type { XrBot } from '../model'

/** The groups, most pressing first. */
export type SinceGroup = 'needs' | 'failed' | 'finished'
export const SINCE_GROUPS: readonly SinceGroup[] = ['needs', 'failed', 'finished']

/** What the rules read of a Bot. */
export type SinceBot = Pick<XrBot, 'slug' | 'state' | 'lastTs' | 'ownUnread'> & {
  needs: readonly { id: string }[]
  cuts: readonly { id: string; stale: boolean }[]
}

/** His last visit: when it was (ms), and what each Bot had then: its asks, its cuts, and when it was last active (s). */
export interface Seen { at: number; bots: Readonly<Record<string, { asks: readonly string[]; cuts: readonly string[]; ts: number }>> }

/**
 * One Bot that changed: its group, and the asks or cuts that put it there (none for `finished`). A Bot that needs him
 * keeps its new cuts too (`cuts`), and one that was also active since (`active`): when its ask ends it falls to
 * `failed`, then to `finished`, instead of going (REVIEW-15 P4 M3).
 */
export interface SinceItem { group: SinceGroup; ids: readonly string[]; cuts?: readonly string[]; active?: boolean }

/** What changed since the visit at `at` (ms), by Bot; only what he has not dealt with yet. */
export interface Since { at: number; bots: Readonly<Record<string, SinceItem>> }

/** The most Bots and the most ids of a Bot that the record keeps. */
export const SINCE_MOST = { bots: 64, ids: 16 } as const

/** What to keep of this visit: every Bot's asks and cuts as they are now, and when it was last active. */
export function seenNow(bots: readonly SinceBot[], nowMs: number): Seen {
  return { at: nowMs, bots: Object.fromEntries(bots.slice(0, SINCE_MOST.bots).map(b => [b.slug, { asks: b.needs.map(n => n.id).slice(0, SINCE_MOST.ids), cuts: b.cuts.map(c => c.id).slice(0, SINCE_MOST.ids), ts: b.lastTs }])) }
}

const NONE: ReadonlySet<string> = new Set()

const atRest = (b: SinceBot) => b.state !== 'working' && b.state !== 'needs'

/** The group of one Bot against what it had at the last visit, or null: nothing changed, or it is new to the crew. */
export function changeOf(bot: SinceBot, seen: Seen): SinceItem | null {
  const was = seen.bots[bot.slug]
  if (!was) return null
  const asks = bot.needs.map(n => n.id).filter(id => !was.asks.includes(id)).slice(0, SINCE_MOST.ids)
  const cuts = bot.cuts.filter(c => !c.stale && !was.cuts.includes(c.id)).map(c => c.id).slice(0, SINCE_MOST.ids)
  // Active after what he was there for: the record holds when the Bot was last active as of his visit's last moment.
  const active = bot.lastTs > was.ts
  if (asks.length) return { group: 'needs', ids: asks, ...(cuts.length ? { cuts } : {}), ...(active ? { active } : {}) }
  if (cuts.length) return { group: 'failed', ids: cuts, ...(active ? { active } : {}) }
  if (atRest(bot) && bot.ownUnread && active) return { group: 'finished', ids: [] }
  return null
}

/**
 * Whether an item still stands: he has not dealt with it, here or on another device. A failure is the rim's to say
 * (REVIEW-15 P4 M2): it stands while one of its cuts is there, is not old, and is not among the cuts he has seen
 * (`seen`, the rim's book: a press on the Bot or on a panel of its page).
 */
export function stands(item: SinceItem, bot: SinceBot | undefined, seen: ReadonlySet<string> = NONE): boolean {
  return regroup(item, bot, seen) !== null
}

/**
 * The item as it is now: what still stands of its asks, then of its cuts, then whether it finished, in that order,
 * so a Bot whose ask ends falls to the next group it is in; null when nothing of it stands.
 */
export function regroup(item: SinceItem, bot: SinceBot | undefined, seen: ReadonlySet<string> = NONE): SinceItem | null {
  if (!bot) return null
  const asks = item.group === 'needs' ? item.ids.filter(id => bot.needs.some(n => n.id === id)) : []
  const cuts = (item.group === 'failed' ? item.ids : item.cuts ?? []).filter(id => bot.cuts.some(c => c.id === id && !c.stale && !seen.has(id)))
  const active = item.group === 'finished' || !!item.active
  if (asks.length) return { group: 'needs', ids: asks, ...(cuts.length ? { cuts } : {}), ...(item.active ? { active: true } : {}) }
  if (cuts.length) return { group: 'failed', ids: cuts, ...(item.active ? { active: true } : {}) }
  if (active && atRest(bot) && bot.ownUnread) return { group: 'finished', ids: [] }
  return null
}

/** The same items with those dropped that no longer stand; the same object when none was; null when none is left. */
export function stillSince(since: Since | null, bots: readonly SinceBot[], seen: ReadonlySet<string> = NONE): Since | null {
  if (!since) return null
  const left = Object.entries(since.bots).flatMap(([slug, item]) => { const now = regroup(item, bots.find(b => b.slug === slug), seen); return now ? [[slug, now] as const] : [] })
  if (JSON.stringify(left) === JSON.stringify(Object.entries(since.bots))) return since
  return left.length ? { at: since.at, bots: Object.fromEntries(left) } : null
}

/**
 * What the line says as he enters: what is left of the entry before (`left`), joined with what changed since the
 * last visit (`seen`). A Bot in both keeps the more pressing group. The time is the older visit's while something
 * of it is left. With no `seen` (a browser that never kept a visit) there is no line.
 */
export function enter(left: Since | null, seen: Seen | null, bots: readonly SinceBot[]): Since | null {
  if (!seen) return null
  const old = stillSince(left, bots)
  const out: Record<string, SinceItem> = { ...(old?.bots ?? {}) }
  for (const bot of bots.slice(0, SINCE_MOST.bots)) {
    const now = changeOf(bot, seen)
    if (!now) continue
    const was = out[bot.slug]
    if (!was || SINCE_GROUPS.indexOf(now.group) < SINCE_GROUPS.indexOf(was.group)) out[bot.slug] = now
  }
  return Object.keys(out).length ? { at: old ? Math.min(old.at, seen.at) : seen.at, bots: out } : null
}

/** He dealt with one Bot here (read its words, tapped a failed one): its item goes. */
export function dealt(since: Since | null, slug: string): Since | null {
  if (!since?.bots[slug]) return since
  const left = Object.entries(since.bots).filter(([s]) => s !== slug)
  return left.length ? { at: since.at, bots: Object.fromEntries(left) } : null
}

/**
 * He took one of a Bot's sheets in his hand (D39.1, made exact): only what he took is cleared. A done sheet drops that
 * the Bot finished; a failed sheet drops its failure. A Bot that still asks stays "needs", and lit.
 */
export function lookedAt(since: Since | null, slug: string, took: 'done' | 'failed'): Since | null {
  const item = since?.bots[slug]
  if (!since || !item) return since
  let now: SinceItem | null
  if (item.group === 'needs') now = took === 'failed' ? { group: 'needs', ids: item.ids, ...(item.active ? { active: true } : {}) } : { group: 'needs', ids: item.ids, ...(item.cuts?.length ? { cuts: item.cuts } : {}) }
  else if (item.group === 'failed') now = took === 'failed' ? (item.active ? { group: 'finished', ids: [] } : null) : { group: 'failed', ids: item.ids }
  else now = took === 'done' ? null : item
  if (now && JSON.stringify(now) === JSON.stringify(item)) return since
  const rest = Object.entries(since.bots).filter(([s]) => s !== slug)
  const bots = now ? [...rest, [slug, now] as const] : rest
  return bots.length ? { at: since.at, bots: Object.fromEntries(bots) } : null
}

/** How many Bots are in each group: what the line says. */
export function sinceCounts(since: Since | null): Record<SinceGroup, number> {
  const out = { needs: 0, failed: 0, finished: 0 }
  for (const item of Object.values(since?.bots ?? {})) out[item.group] += 1
  return out
}

/** The cuts that are new since his last visit and that he has not dealt with: the rim draws each red (`rims.ts`). */
export const sinceCuts = (since: Since | null): string[] => Object.values(since?.bots ?? {}).flatMap(item => (item.group === 'failed' ? [...item.ids] : [...(item.cuts ?? [])]))

/** The Bots that are lit: exactly the Bots that the line counts. */
export const sinceLit = (since: Since | null): string[] => Object.keys(since?.bots ?? {})

/** Whether the visit was on another day than now (the line then names the day). */
export const otherDay = (atMs: number, nowMs: number): boolean => new Date(atMs).toDateString() !== new Date(nowMs).toDateString()

/** The line's place and size: one line above Home's strip, as wide as the strip, read at the strip's size. */
export const SINCE_LINE = { heightM: 0.06, gapM: 0.012, numberM: 0.042, labelM: 0.03, mostPitchDeg: 20 } as const

/**
 * How far up the line's middle stands (degrees), from Home's own place: above Home, with a small gap. When that
 * would put its upper edge past what a pair of glasses shows of a level look (`mostPitchDeg`), it stands under
 * Home instead.
 */
export function sinceLinePitch(homePitchDeg: number, homeHeightM: number, distanceM: number): number {
  const deg = (m: number) => (Math.atan2(m, distanceM) * 180) / Math.PI
  const step = deg(homeHeightM / 2 + SINCE_LINE.gapM + SINCE_LINE.heightM / 2)
  const above = homePitchDeg + step
  return above + deg(SINCE_LINE.heightM / 2) <= SINCE_LINE.mostPitchDeg ? above : homePitchDeg - step
}

const idList = (raw: unknown): string[] | null =>
  Array.isArray(raw) && raw.every(v => typeof v === 'string' && v.length > 0 && v.length <= 256) ? (raw as string[]).slice(0, SINCE_MOST.ids) : null
const okSlug = (slug: string) => slug.length > 0 && slug.length <= 128
const okTime = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v) && v > 0

/** The last visit as a record holds it, or null: what is no visit is dropped whole, a Bot that is none is dropped alone. */
export function parseSeen(raw: unknown): Seen | null {
  if (!raw || typeof raw !== 'object') return null
  const r = raw as { at?: unknown; bots?: unknown }
  if (!okTime(r.at) || !r.bots || typeof r.bots !== 'object' || Array.isArray(r.bots)) return null
  const bots: Record<string, { asks: string[]; cuts: string[]; ts: number }> = {}
  for (const [slug, v] of Object.entries(r.bots as Record<string, unknown>).slice(0, SINCE_MOST.bots)) {
    const asks = idList((v as { asks?: unknown } | null)?.asks)
    const cuts = idList((v as { cuts?: unknown } | null)?.cuts)
    const ts = (v as { ts?: unknown } | null)?.ts
    if (okSlug(slug) && asks && cuts && typeof ts === 'number' && Number.isFinite(ts) && ts >= 0) bots[slug] = { asks, cuts, ts }
  }
  return { at: r.at, bots }
}

/** What changed, as a record holds it, or null. */
export function parseSince(raw: unknown): Since | null {
  if (!raw || typeof raw !== 'object') return null
  const r = raw as { at?: unknown; bots?: unknown }
  if (!okTime(r.at) || !r.bots || typeof r.bots !== 'object' || Array.isArray(r.bots)) return null
  const bots: Record<string, SinceItem> = {}
  for (const [slug, v] of Object.entries(r.bots as Record<string, unknown>).slice(0, SINCE_MOST.bots)) {
    const group = (v as { group?: unknown } | null)?.group
    const ids = idList((v as { ids?: unknown } | null)?.ids)
    const cuts = idList((v as { cuts?: unknown } | null)?.cuts)
    const active = (v as { active?: unknown } | null)?.active === true
    if (okSlug(slug) && ids && SINCE_GROUPS.includes(group as SinceGroup)) bots[slug] = { group: group as SinceGroup, ids, ...(cuts?.length ? { cuts } : {}), ...(active ? { active } : {}) }
  }
  return Object.keys(bots).length ? { at: r.at, bots } : null
}
