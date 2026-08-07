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
 *  slots carry ISO `last_ts`; history items carry epoch-seconds `modified`. */
export interface Sortable {
  title?: string
  key: string
  created?: string
  last_ts?: string
  modified?: number
}

/** Last-activity instant in epoch SECONDS, with the fallback ladder both
 *  surfaces rely on. Returns 0 for a session with no usable timestamp, which
 *  sorts it last under `date-desc`. */
export function lastActivityEpoch(item: Sortable): number {
  if (item.modified != null) return item.modified
  if (item.last_ts) return new Date(item.last_ts).getTime() / 1000
  if (item.created) return new Date(item.created).getTime() / 1000
  return 0
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
  // date-desc / date-asc: last activity (modified epoch, last_ts ISO, or created ISO)
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

/** Relative timestamp for a session row.
 *  Accepts ISO string (active slots) or Unix epoch seconds (history `modified`). */
export function fmtRelativeTime(ts: string | number | undefined): string {
  if (ts == null) return ''
  const d = typeof ts === 'number' ? new Date(ts * 1000) : new Date(ts)
  if (isNaN(d.getTime())) return ''
  const now = new Date()
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate())
  const startOfYesterday = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1)
  const startOf6DaysAgo = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 6)
  // Every branch read the BROWSER's locale before this, so a zh dashboard on an
  // en-US browser showed "3:04 PM" and "Jul 30". This is the twin of
  // `commandPalette/providers/recentsProvider.ts`; the two are now consistent.
  const time = fmtDateFields(d, { hour: '2-digit', minute: '2-digit' })
  if (d >= startOfToday) return time
  // The existing catalog key, NOT `fmtRelative`: CLDR returns a lowercase
  // "yesterday", which clashed with the capitalized group header in ChatSidebar
  // that already uses this same key. One key, one casing.
  if (d >= startOfYesterday) return `${i18nT('pages.chatSidebar.yesterday')} ${time}`
  if (d >= startOf6DaysAgo) return `${fmtDateFields(d, { weekday: 'short' })} ${time}`
  if (d.getFullYear() === now.getFullYear()) return fmtDateFields(d, { month: 'short', day: 'numeric' })
  return fmtDateFields(d, { year: 'numeric', month: 'short', day: 'numeric' })
}
