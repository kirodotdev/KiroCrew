import { i18nT } from '../../i18n/t'
import { compareText, fmtDateFields } from '../../i18n/format'

/**
 * Session ordering + timestamp formatting, shared by the session sidebar and
 * the collapsed-sidebar hover flyout.
 *
 * These lived inside ChatSidebar.tsx until the flyout needed the same
 * "most-recent-first" order. Two copies of a comparator drift silently — the
 * flyout would keep claiming "recent" while ranking by something else — so
 * there is one definition and both surfaces import it.
 */

export type SortKey = 'date-desc' | 'date-asc' | 'created-desc' | 'created-asc' | 'name-asc' | 'name-desc'

/** The subset of a session either surface needs in order to rank it. Active
 *  slots carry ISO `last_turn_ts` / `last_ts`; history items carry epoch-seconds
 *  `modified`. */
export interface Sortable {
  title?: string
  key: string
  created?: string
  last_turn_ts?: string
  last_ts?: string
  modified?: number
}

/** Settled activity instant of an ACTIVE slot, as the ISO string the backend
 *  sent. `last_turn_ts` moves only when a prompt arrives or a turn ends, whereas
 *  `last_ts` is the newest row of any role and advances on every streamed tool
 *  call — ranking or labelling a row by that makes the list churn while agents
 *  work. Display, date segmenting and the recency tint all read THIS so a row's
 *  visible timestamp cannot disagree with the position it was sorted into. */
export function slotActivityTs(
  slot: { last_turn_ts?: string; last_ts?: string; created?: string },
): string | undefined {
  return slot.last_turn_ts || slot.last_ts || slot.created
}

/** Last-activity instant in epoch SECONDS, with the fallback ladder both
 *  surfaces rely on. Returns 0 for a session with no usable timestamp, which
 *  sorts it last under `date-desc`. */
export function lastActivityEpoch(item: Sortable): number {
  if (item.modified != null) return item.modified
  const iso = slotActivityTs(item)
  if (!iso) return 0
  const ms = new Date(iso).getTime()
  // An unparseable timestamp ranks as "no timestamp" rather than poisoning the
  // comparator: NaN makes every comparison false, which leaves the whole list in
  // an arbitrary order rather than just misplacing the one broken row.
  return Number.isNaN(ms) ? 0 : ms / 1000
}

/** Shared comparator for both active sessions and history items. */
export function compareBySort(a: Sortable, b: Sortable, key: SortKey): number {
  if (key === 'name-asc' || key === 'name-desc') {
    // Session titles are free text, so ordering follows the app language:
    // `compareText` is case- and accent-insensitive with numeric collation, so
    // "reviewer-2" precedes "reviewer-10" instead of following it.
    const na = a.title || a.key
    const nb = b.title || b.key
    return key === 'name-asc' ? compareText(na, nb) : compareText(nb, na)
  }
  if (key === 'created-desc' || key === 'created-asc') {
    const ca = a.created || ''
    const cb = b.created || ''
    // BYTE order, deliberately not a Collator: `created` is an ISO-8601 string,
    // where lexicographic order IS chronological order. Collation weights `-`,
    // `:` and `T` at a lower level, which would make "newest first" depend on
    // the active language.
    const cmp = ca < cb ? -1 : ca > cb ? 1 : 0
    return key === 'created-desc' ? -cmp : cmp
  }
  // date-desc / date-asc: last SETTLED activity (modified epoch, else the
  // last_turn_ts → last_ts → created ISO ladder)
  const ta = lastActivityEpoch(a)
  const tb = lastActivityEpoch(b)
  return key === 'date-desc' ? tb - ta : ta - tb
}

/**
 * Pinned sessions first, then the chosen sort. Pinning is a reachability
 * promise, not a ranking hint: a pinned session the user parked stays findable
 * even when it is the least recently touched thing in the list. Both surfaces
 * apply it, so a pinned row does not jump position between them.
 */
export function comparePinnedThenSort(
  a: Sortable,
  b: Sortable,
  key: SortKey,
  pinned: ReadonlySet<string>,
): number {
  const pa = pinned.has(a.key) ? 0 : 1
  const pb = pinned.has(b.key) ? 0 : 1
  if (pa !== pb) return pa - pb
  return compareBySort(a, b, key)
}

/**
 * The local-midnight instants `fmtRelativeTime` classifies against, in epoch ms.
 *
 * Every visible session row compares against the same boundaries, so they are
 * built once per day rather than once per call. `dayEnd` is the next local
 * midnight, which is when they stop being true.
 *
 * The lower bound matters as much as the upper one: a clock that moves BACKWARDS
 * is also outside the cached day, and must not be served boundaries from a day
 * that has not happened yet.
 */
let dayStart = 0
let yesterdayStart = 0
let sixDaysAgoStart = 0
let dayEnd = 0
let dayYear = 0

function ensureDayBoundaries(nowMs: number): void {
  if (nowMs >= dayStart && nowMs < dayEnd) return
  const now = new Date(nowMs)
  const y = now.getFullYear()
  const m = now.getMonth()
  const day = now.getDate()
  // Day arithmetic goes through the Date constructor rather than ms subtraction
  // because it has to survive DST: a local day is not always 86_400_000 ms long.
  dayStart = new Date(y, m, day).getTime()
  yesterdayStart = new Date(y, m, day - 1).getTime()
  sixDaysAgoStart = new Date(y, m, day - 6).getTime()
  dayEnd = new Date(y, m, day + 1).getTime()
  dayYear = y
}

/** Relative timestamp for a session row.
 *  Accepts ISO string (active slots) or Unix epoch seconds (history `modified`). */
export function fmtRelativeTime(ts: string | number | undefined): string {
  if (ts == null) return ''
  const d = typeof ts === 'number' ? new Date(ts * 1000) : new Date(ts)
  const at = d.getTime()
  if (isNaN(at)) return ''
  ensureDayBoundaries(Date.now())
  // Every branch read the BROWSER's locale before this, so a zh dashboard on an
  // en-US browser showed "3:04 PM" and "Jul 30". This is the twin of
  // `commandPalette/providers/recentsProvider.ts`; the two are now consistent.
  const time = fmtDateFields(d, { hour: '2-digit', minute: '2-digit' })
  if (at >= dayStart) return time
  // The existing catalog key, NOT `fmtRelative`: CLDR returns a lowercase
  // "yesterday", which clashed with the capitalized group header in ChatSidebar
  // that already uses this same key. One key, one casing.
  if (at >= yesterdayStart) return `${i18nT('pages.chatSidebar.yesterday')} ${time}`
  if (at >= sixDaysAgoStart) return `${fmtDateFields(d, { weekday: 'short' })} ${time}`
  if (d.getFullYear() === dayYear) return fmtDateFields(d, { month: 'short', day: 'numeric' })
  return fmtDateFields(d, { year: 'numeric', month: 'short', day: 'numeric' })
}
