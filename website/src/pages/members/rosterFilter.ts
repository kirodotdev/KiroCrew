/**
 * The Crew Members roster's filter and sort model, as pure functions over the
 * roster array. Nothing here reads React state or storage: the page hands in
 * the members, a `RosterQuery`, and a `signalsOf` resolver for the live
 * per-member facts (running / needs-you / unread / patrolling) that live
 * outside the roster row, and gets the rows to render back. Keeping the model
 * data-source-agnostic is what lets the page's state layer move without
 * touching how a filter decides.
 */
import { compareText } from '../../i18n/format'

/** Crew origin. `mine` = crews created in the crew manager (source
 *  'kirocrew'); `builtin` = shipped with Kiro Crew; `package` = written by the
 *  agent sync from installed capability packages — on a busy host the large
 *  majority of the roster, and the reason the filter exists. */
export type MemberSourceFilter = 'all' | 'mine' | 'builtin' | 'package'
export const SOURCE_FILTERS: readonly Exclude<MemberSourceFilter, 'all'>[] = ['mine', 'builtin', 'package']

export function parseSourceFilter(raw: string | null): MemberSourceFilter {
  return raw === 'mine' || raw === 'builtin' || raw === 'package' ? raw : 'all'
}

/** The server normalizes `source` to kirocrew | builtin | package before it
 *  reaches the wire; the fallback-to-package here only covers a row from an
 *  older gateway that omits the field. */
export function matchesSource(m: { source?: unknown }, f: MemberSourceFilter): boolean {
  if (f === 'all') return true
  const src = typeof m.source === 'string' ? m.source : ''
  if (f === 'mine') return src === 'kirocrew'
  if (f === 'builtin') return src === 'builtin'
  return src !== 'kirocrew' && src !== 'builtin'
}

/** Live state a member is in right now — the roster's counterpart to the
 *  sidebar's session filters (unread / running / …). Resolved per row by the
 *  page, because three of the four come from the WS slot frames and the patrol
 *  registry, not from the roster row itself. */
export interface MemberSignals {
  /** The member's DM slot is mid-turn. */
  running: boolean
  /** The turn is parked on an approval or a question only the user can answer. */
  needsYou: boolean
  /** The DM thread has a message the user has not seen. */
  unread: boolean
  /** An auto-nudge loop is armed on the member's slot and active. */
  patrolling: boolean
}

export type MemberStatusFilter = 'working' | 'needs_you' | 'unread' | 'patrolling'
export const STATUS_FILTERS: readonly MemberStatusFilter[] = ['working', 'needs_you', 'unread', 'patrolling']

/** OR across the chosen statuses, like the sidebar's session filters: picking
 *  "Working" and "Needs you" shows a member in either state. */
export function matchesStatus(signals: MemberSignals, status: ReadonlySet<MemberStatusFilter>): boolean {
  if (status.size === 0) return true
  return (
    (status.has('working') && signals.running) ||
    (status.has('needs_you') && signals.needsYou) ||
    (status.has('unread') && signals.unread) ||
    (status.has('patrolling') && signals.patrolling)
  )
}

export function parseStatusFilters(raw: string | null): Set<MemberStatusFilter> {
  const out = new Set<MemberStatusFilter>()
  if (!raw) return out
  try {
    const parsed: unknown = JSON.parse(raw)
    if (Array.isArray(parsed)) {
      for (const v of parsed) if ((STATUS_FILTERS as readonly string[]).includes(String(v))) out.add(v as MemberStatusFilter)
    }
  } catch {
    // Storage is hand-editable; junk reads as "no status filter".
  }
  return out
}

export type MemberSort = 'recent' | 'name'
export const SORT_OPTIONS: readonly MemberSort[] = ['recent', 'name']

export function parseSort(raw: string | null): MemberSort {
  return raw === 'name' ? 'name' : 'recent'
}

export interface RosterQuery {
  /** Free-text needle against the member name (case-insensitive, trimmed). */
  search: string
  starredOnly: boolean
  source: MemberSourceFilter
  status: ReadonlySet<MemberStatusFilter>
  sort: MemberSort
}

interface RosterRowLike { name: string; starred?: boolean; source?: unknown; last_active_ts?: number }

/** Most-recently-active first (like any IM member list); never-talked members
 *  fall to the bottom alphabetically. `name` is a plain locale-aware sort. */
export function sortRoster<M extends RosterRowLike>(members: readonly M[], sort: MemberSort): M[] {
  const out = [...members]
  if (sort === 'name') return out.sort((a, b) => compareText(a.name, b.name))
  return out.sort((a, b) => (b.last_active_ts ?? 0) - (a.last_active_ts ?? 0) || compareText(a.name, b.name))
}

/** True when `query` narrows the roster by something other than the typed
 *  search — the "N of M" header case and the filtered-out-everyone notice. */
export function queryNarrows(query: RosterQuery): boolean {
  return query.starredOnly || query.source !== 'all' || query.status.size > 0
}

/** Narrow an ALREADY-ORDERED roster by every active dimension (AND across
 *  dimensions, OR inside the status set), keeping the order it came in. The
 *  page feeds this its committed display order (sorted once per membership
 *  and per chosen sort with `sortRoster`), so a refetch that advances a
 *  `last_active_ts` never re-sorts rows under the cursor. */
export function narrowRoster<M extends RosterRowLike>(
  ordered: readonly M[],
  query: Omit<RosterQuery, 'sort'>,
  signalsOf: (m: M) => MemberSignals,
): M[] {
  const q = query.search.trim().toLowerCase()
  return ordered.filter(
    (m) =>
      (!query.starredOnly || !!m.starred) &&
      matchesSource(m, query.source) &&
      (query.status.size === 0 || matchesStatus(signalsOf(m), query.status)) &&
      (!q || m.name.toLowerCase().includes(q)),
  )
}

/** How many members each filter would keep on its own — the counts the menu
 *  shows beside each row, so a zero-count filter is visibly the one that would
 *  blank the list. */
export function countByFilter<M extends RosterRowLike>(
  members: readonly M[],
  signalsOf: (m: M) => MemberSignals,
): { starred: number; status: Record<MemberStatusFilter, number>; source: Record<Exclude<MemberSourceFilter, 'all'>, number> } {
  const out = {
    starred: 0,
    status: { working: 0, needs_you: 0, unread: 0, patrolling: 0 } as Record<MemberStatusFilter, number>,
    source: { mine: 0, builtin: 0, package: 0 } as Record<Exclude<MemberSourceFilter, 'all'>, number>,
  }
  for (const m of members) {
    if (m.starred) out.starred += 1
    const s = signalsOf(m)
    if (s.running) out.status.working += 1
    if (s.needsYou) out.status.needs_you += 1
    if (s.unread) out.status.unread += 1
    if (s.patrolling) out.status.patrolling += 1
    for (const f of SOURCE_FILTERS) if (matchesSource(m, f)) out.source[f] += 1
  }
  return out
}
