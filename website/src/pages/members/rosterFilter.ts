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
  /** The default crew's name (top-level `default_agent`). It is listed whatever
   *  its record says — see `listedByDefault`. `''` while unknown; `null` when
   *  the lookup FAILED, which turns the hide rule off so the default crew is
   *  never hidden by a read error. */
  defaultAgent: string | null
  /** The crewmate open in the thread, if any. It stays listed while open, so a
   *  hidden row the user reached through the search does not vanish from the
   *  roster under its own chat when the search is cleared. */
  chosen?: string
}

interface RosterRowLike {
  name: string; display_name?: string; starred?: boolean; source?: unknown; last_active_ts?: number
  dashboard_created?: unknown; has_dm_message?: unknown; last_message?: unknown
}

/** What the roster row RENDERS as its title: the display label when set, the
 *  name otherwise. Sort and search go through the same accessor so the list
 *  the user reads is the list these functions order and narrow. */
function rowLabel(m: RosterRowLike): string {
  return m.display_name?.trim() || m.name
}

/** The search box's match: a case-insensitive substring of the name or of
 *  the displayed label. One function so the hide rule below and the narrowing
 *  agree on what "the search reaches" means. */
function matchesSearch(m: RosterRowLike, needle: string): boolean {
  return m.name.toLowerCase().includes(needle) || rowLabel(m).toLowerCase().includes(needle)
}

/** Whether the roster lists a row WITHOUT being asked for it. Listed when
 *  EITHER its Crewmates-page DM thread already holds a message (any origin:
 *  a user who chatted with it is using it), OR it was created on the
 *  dashboard (`source` kirocrew AND a member id -- covers a greeting that
 *  failed or never landed). The default crew is listed whatever its record
 *  says, as it always has been. Everything else -- an app's row, a
 *  sync-generated row, a legacy row, none of them chatted with -- is hidden
 *  until the search reaches it.
 *
 *  A thread's first message is also read from the row's live preview
 *  (`last_message`, pushed through the member projection), so a row the user
 *  just chatted with stays listed without waiting for a roster refetch. A row
 *  from an older gateway that carries NEITHER field is listed: hiding on an
 *  absent field would blank the roster on a mixed-version deploy.
 *
 *  `defaultAgent === null` means the default-crew lookup failed: every row is
 *  listed, since the rule cannot tell which row it must never hide. */
export function listedByDefault(m: RosterRowLike, defaultAgent: string | null): boolean {
  if (defaultAgent === null) return true
  if (defaultAgent !== '' && m.name === defaultAgent) return true
  if (m.dashboard_created === undefined && m.has_dm_message === undefined) return true
  return (
    m.has_dm_message === true ||
    m.dashboard_created === true ||
    // A star is the user marking the row as theirs: listed like a chatted one.
    m.starred === true ||
    (typeof m.last_message === 'string' && m.last_message.trim() !== '')
  )
}

/** Whether the roster shows this row for `query`, before the star / origin /
 *  status filters: listed by default, or reached by a typed search. With a
 *  search typed the search decides alone — a hidden row it reaches shows, a
 *  listed row it misses does not. */
export function rosterShows(
  m: RosterRowLike,
  query: Pick<RosterQuery, 'search' | 'defaultAgent' | 'chosen'>,
): boolean {
  const q = query.search.trim().toLowerCase()
  if (q) return matchesSearch(m, q)
  return listedByDefault(m, query.defaultAgent) || (!!query.chosen && m.name === query.chosen)
}

/** The rows the roster is ABOUT for `query`: every row listed by default plus
 *  any hidden row the typed search reaches. This is the population the header
 *  count, the "N of M" and the filter menu's tallies read, so a count never
 *  includes a row the user cannot get to -- and, as before, the search itself
 *  never SHRINKS the count (it is transient, not a filter), it can only add the
 *  hidden rows it surfaces. */
export function rosterPopulation<M extends RosterRowLike>(
  members: readonly M[],
  query: Pick<RosterQuery, 'search' | 'defaultAgent' | 'chosen'>,
): M[] {
  const q = query.search.trim().toLowerCase()
  return members.filter(
    (m) =>
      listedByDefault(m, query.defaultAgent) ||
      (!!query.chosen && m.name === query.chosen) ||
      (q !== '' && matchesSearch(m, q)),
  )
}

/** Most-recently-active first (like any IM member list); never-talked members
 *  fall to the bottom alphabetically. `name` is a plain locale-aware sort over
 *  the DISPLAYED label, since that is the text the user scans. */
export function sortRoster<M extends RosterRowLike>(members: readonly M[], sort: MemberSort): M[] {
  const out = [...members]
  if (sort === 'name') return out.sort((a, b) => compareText(rowLabel(a), rowLabel(b)))
  return out.sort((a, b) => (b.last_active_ts ?? 0) - (a.last_active_ts ?? 0) || compareText(rowLabel(a), rowLabel(b)))
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
 *  `last_active_ts` never re-sorts rows under the cursor. Rows the roster
 *  does not show for this query (`rosterShows`) are out before any filter. */
export function narrowRoster<M extends RosterRowLike>(
  ordered: readonly M[],
  query: Omit<RosterQuery, 'sort'>,
  signalsOf: (m: M) => MemberSignals,
): M[] {
  return ordered.filter(
    (m) =>
      rosterShows(m, query) &&
      (!query.starredOnly || !!m.starred) &&
      matchesSource(m, query.source) &&
      (query.status.size === 0 || matchesStatus(signalsOf(m), query.status)),
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
