/**
 * The Crewmates roster as data. `useMembersPageRoster` is the React Query read
 * merged with each member's pushed `roster` projection, field by field, with
 * the slug-collision guard every projection read goes through.
 * `useMembersPageRosterSignals` holds the live readings the rows, the folded
 * switcher and the status filters share (unread, patrol, the automation loop,
 * needs-you) and narrows the held display order by the user's filters.
 *
 * The `MembersPage` facade calls each hook here at the point its code sits in
 * the page's hook order, which `MembersPage.owners.test.tsx` pins: React runs
 * a component's effects in registration order, so moving one is a behaviour
 * change even when every test of the moved code still passes.
 */
import { useCallback, useMemo } from 'react'
import { api, type MemberRosterRow } from '../../api/client'
import { membersRosterQuery } from '../../api/membersQuery'
import { AUTONUDGE_LOOPS_QUERY_KEY } from '../../components/autoNudgeLoop'
import { useQuery } from '@tanstack/react-query'
import { useAppSelector } from '../../store'
import { selectAutomationForSlot } from '../../store/chatSlice'
import {
  isAutomationActive,
  isFullLegacyAutomationRecord,
  normalizeAutomationRecord,
  type AutomationRecord,
} from '../../monitoring/automation'
import { countByFilter, hasNoCrewmates, matchesSource, narrowRoster, queryNarrows, rowLabel, type MemberSignals } from './rosterFilter'
import { useMemberRosterViews } from '../../state/useMemberProjection'
import type { RosterView } from '../../state/memberProjectionTypes'
import { type MembersPageRosterView } from './MembersPageRoster'

/** Stable empty roster for the not-yet-answered read, so the memos keyed on
 *  `members` do not recompute on every render while the first fetch is out. */
const EMPTY_ROSTER: readonly MemberRosterRow[] = []

/** The roster read, merged with each member's pushed projection. */
export function useMembersPageRoster() {
  // The roster is a React Query read (issue #9418), not page state: a return
  // to the page renders the cached list at once and refreshes it in the
  // background, and a crew written anywhere else reaches it through the
  // `['kirocrew-agents']` prefix invalidation (see membersQuery.ts). Three
  // states, kept apart the way every block on this page keeps them: not yet
  // answered, answered, failed with no answer to fall back on — a refetch
  // error after a good read keeps showing the last roster.
  const rosterQuery = useQuery(membersRosterQuery)
  const rows = rosterQuery.data ?? EMPTY_ROSTER
  // The raw roster's keys and shown names (not the filtered/projected list):
  // what the create dialog refuses up front, and the premise of its
  // post-failure reconcile. The server refuses a name another crewmate shows
  // as well as a taken key (`members.key_new_crew`).
  const existingNames = useMemo(
    () => rows.flatMap((r) => (r.display_name ? [r.name, r.display_name] : [r.name])),
    [rows],
  )
  const loaded = rosterQuery.data !== undefined || rosterQuery.isError
  const loadError = rosterQuery.data === undefined && rosterQuery.isError
  // ONE source of truth for the roster fields the page derives from (starred
  // count, the Starred filter, search, sort, source chips): the react-query
  // rows merged with each member's pushed `roster` projection, projection
  // fields winning field-by-field when present. Keying the projection read on
  // the slug list means a `member_projection` frame flips the merged row here
  // WITHOUT a roster refetch, so a page-level count/filter and the row's own
  // star button never disagree. `running` is not projected — it stays live
  // presence, resolved from slots.
  const slugs = useMemo(() => rows.map((r) => r.slug), [rows])
  const rosterViews = useMemberRosterViews(slugs)
  // Slugs carried by MORE THAN ONE row. A live `member_projection` frame is keyed
  // by slug alone, so a colliding pair shares one entry in the store and both rows
  // would render whichever member's state arrived last. The backend's roster read
  // already withholds a projection for a colliding row; the live path reaches the
  // store directly and needs the same rule, or the two surfaces disagree.
  const collidingSlugs = useMemo(() => {
    const counts = new Map<string, number>()
    for (const r of rows) counts.set(r.slug, (counts.get(r.slug) ?? 0) + 1)
    return new Set([...counts].filter(([, n]) => n > 1).map(([slug]) => slug))
  }, [rows])
  // Every projection read goes through this. A slug carried by two rows cannot
  // say which member a frame describes, so the read is withheld rather than
  // guessed -- and the hook already treats a null slug as "no projection", so
  // withholding needs no change there. The merged list above applies the same
  // rule; the drawer and each row read the SAME slug-keyed frames, so a guard on
  // only one of them leaves the others rendering another member's state.
  const projectionSlug = (slug: string | null | undefined): string | null =>
    slug && !collidingSlugs.has(slug) ? slug : null
  const members = useMemo<MemberRosterRow[]>(
    () =>
      rows.map((r) => {
        // Withheld, not guessed: with two rows sharing a slug nothing in the frame
        // says which member it describes, so the row keeps its own roster values.
        if (collidingSlugs.has(r.slug)) return r
        const v = rosterViews.get(r.slug)
        if (!v) return r
        // Field-by-field, like MemberRow: a value the projection OMITS
        // (undefined) must not clobber the row's own field, so spreading the
        // whole view is wrong — only defined projection fields win.
        const merged: MemberRosterRow = { ...r }
        for (const key of Object.keys(v) as (keyof RosterView)[]) {
          // Identity fields are never taken from the projection: rows are keyed
          // and selected by the exact crew NAME, and slug is lossy (two names
          // can share one). The projection's own name/slug would rewrite a
          // row's identity across a shared-slug pair (MemberRow excludes them
          // for the same reason).
          if (key === 'name' || key === 'slug') continue
          // `last_message` is the one field the projection does NOT get to win,
          // and the direction is the point. Every other key here is
          // config-derived, so the event log is where it is written and the
          // projection IS the record. A message preview is not: the row carries
          // it from the conversation transcript, the store the message was
          // persisted through, and the member/message event is a second copy
          // appended afterwards on a best-effort hook. A refused append leaves
          // the projection holding the PREVIOUS message, so letting it win
          // renders a stale preview over the fresh value sitting beside it in
          // the same payload. The projection still fills in when the row has no
          // transcript value at all, which is what a pushed frame is for.
          if (key === 'last_message') {
            if (!r[key]) (merged as Record<string, unknown>)[key] = v[key]
            continue
          }
          // Recency takes the GREATER of the two, which is the only rule that
          // lets a live frame move a row. The server row carries the folded
          // `last_active_ts` itself (`api_members` reads it off the same roster
          // projection, floored by the transcript), so the two are readings of
          // ONE value at two moments: the row's is this refetch's, the
          // projection's is the newest pushed frame's. Preferring the row's
          // would freeze the pushed frame out, so a send that advanced recency
          // could not reorder the list until the next refetch; preferring the
          // projection's would let a lagging baseline walk a row back. Max is
          // monotone, so neither can happen.
          if (key === 'last_active_ts') {
            const pushed = typeof v[key] === 'number' ? (v[key] as number) : 0
            const own = typeof r[key] === 'number' ? (r[key] as number) : 0
            if (pushed > own) (merged as Record<string, unknown>)[key] = pushed
            continue
          }
          const pv = v[key]
          if (pv !== undefined) (merged as Record<string, unknown>)[key] = pv
        }
        return merged
      }),
    [rows, rosterViews, collidingSlugs],
  )

  return {
    rosterQuery, rows, existingNames, loaded, loadError, collidingSlugs, projectionSlug, members,
  }
}

export type MembersPageRosterRead = ReturnType<typeof useMembersPageRoster>

/** How many hidden crewmates the roster's hidden line names; the rest are counted. */
const HIDDEN_NAMES_MAX = 3

interface MembersPageRosterSignalsOptions {
  rosterRead: MembersPageRosterRead
  rosterView: MembersPageRosterView
  slotKeyOf: (m: MemberRosterRow) => string
}

/** Per-row live readings (unread, patrol, loop, needs-you) and the filtered roster. */
export function useMembersPageRosterSignals({ rosterRead, rosterView, slotKeyOf }: MembersPageRosterSignalsOptions) {
  const { loaded, loadError, members } = rosterRead
  const { isRunning, liveNeedsYou, filter, orderedMembers, rowsReady, rosterFilterQuery, shownMembers } = rosterView
  // Per-row unread marker: the rail badge says "1", this says WHICH member.
  // Keyed the same way isRunning resolves a member's slot (thread-endpoint
  // cache first, roster binding as the cold-start fallback), and read straight
  // from unreadSlots so the drain effect (`useMembersPageThreadView`, in
  // MembersPageThread.tsx) clears the dot the moment the thread is opened.
  const unreadSlots = useAppSelector((s) => s.dashboard.unreadSlots)
  const isUnread = useCallback(
    (m: MemberRosterRow) => {
      const key = slotKeyOf(m)
      return !!key && unreadSlots.includes(key)
    },
    [slotKeyOf, unreadSlots],
  )

  // The roster and header keep their existing live Redux source. The full
  // legacy registry is only a cold-start fallback before the first websocket
  // snapshot arrives; a live frame wins for its slot.
  const patrolQuery = useQuery({
    queryKey: AUTONUDGE_LOOPS_QUERY_KEY,
    queryFn: () => api.autonudgeList(),
    refetchOnReconnect: true,
  })
  const patrol = useMemo(() => ({ failed: patrolQuery.isError }), [patrolQuery.isError])
  const liveAutomations = useAppSelector((s) => s.chat.automations)
  const legacyAutomations = useMemo(() => {
    const records: Record<string, AutomationRecord> = {}
    if (patrolQuery.data === undefined || patrolQuery.isError) return records
    for (const raw of patrolQuery.data.loops ?? []) {
      if (!isFullLegacyAutomationRecord(raw)) continue
      const record = normalizeAutomationRecord(raw)
      if (record?.kind === 'legacy_goal_loop') records[record.slotKey] = record
    }
    return records
  }, [patrolQuery.data, patrolQuery.isError])
  const automationOf = useCallback(
    (m: MemberRosterRow): AutomationRecord | null => {
      const key = slotKeyOf(m)
      if (!key) return null
      return selectAutomationForSlot({ chat: { automations: liveAutomations } }, key)
        ?? legacyAutomations[key]
        ?? null
    },
    [slotKeyOf, liveAutomations, legacyAutomations],
  )
  const isLoopOn = useCallback(
    (m: MemberRosterRow) => isAutomationActive(automationOf(m)),
    [automationOf],
  )
  // The live facts the status filters read, resolved per row from the same
  // automation record as its avatar and Profile details.
  const signalsOf = useCallback(
    (m: MemberRosterRow): MemberSignals => {
      const key = slotKeyOf(m)
      return {
        running: !!isRunning(m),
        needsYou: !!key && !!liveNeedsYou[key],
        unread: isUnread(m),
        patrolling: isLoopOn(m),
      }
    },
    [slotKeyOf, isRunning, liveNeedsYou, isUnread, isLoopOn],
  )
  // The roster row's needs-you cue reads the SAME resolver, so the full roster,
  // the folded switcher and the status filter can never disagree about a crewmate
  // parked on a question.
  const isNeedsYou = useCallback((m: MemberRosterRow) => signalsOf(m).needsYou, [signalsOf])
  // Narrow the COMMITTED order (never re-sort here): a refetch that advances
  // a last_active_ts must not move rows under the cursor — see orderedMembers.
  const sortedMembers = useMemo(
    () => narrowRoster(orderedMembers, rosterFilterQuery, signalsOf),
    [orderedMembers, rosterFilterQuery, signalsOf],
  )
  // Per-row counts in the filter menu: the one-word labels do not explain
  // themselves and a zero-count row is exactly the one that blanks the list.
  // Over the shown population, so a tally never counts a hidden row.
  const filterCounts = useMemo(() => countByFilter(shownMembers, signalsOf), [shownMembers, signalsOf])
  const narrowed = queryNarrows(rosterFilterQuery)
  // True when the filters (not the search) hid everything — the empty-roster
  // copy would be wrong then, since the roster is not empty. Judged against
  // the shown population: hidden rows are not "filtered out", they are unlisted.
  const filteredOut =
    rowsReady && loaded && !loadError && shownMembers.length > 0 && sortedMembers.length === 0 && !filter.trim()
  // Every crewmate exists but the listing rule hides them all (none chatted
  // with, none starred): say so and name the search as
  // the way in, rather than an empty list under "0 crewmates".
  const allHidden =
    rowsReady && loaded && !loadError && !filter.trim() && shownMembers.length === 0 && !hasNoCrewmates(members)
  // How many crewmates the listing rule hides beside the ones it lists (#21852):
  // "2 crewmates" over a crew of three read as the whole crew, and the third
  // turned up only when a search happened to match it. The rule and the count
  // stay as they are; the list ends by saying which or how many more there are
  // and that the search reaches them. Zero (no line) while a search is typed, since the
  // search then decides the list alone; while a filter narrows it, since the
  // header then reads "N of M" over the listed rows and a third number about
  // the unfiltered crew would muddle it; and when `allHidden` already says it
  // for every row.
  const hiddenCount =
    rowsReady && loaded && !loadError && !filter.trim() && !narrowed && shownMembers.length > 0
      ? members.length - shownMembers.length
      : 0
  // The first HIDDEN_NAMES_MAX hidden crewmates are named on that line so a user
  // who does not remember a crewmate's name still knows what to type: the ones
  // the user made come first (origin Mine, or created on the dashboard), so a
  // crewmate just created is named rather than counted behind package and app
  // rows, then roster order. Past that the line counts the rest ("A, B, C and 4
  // more"): the hidden set is often dozens of package and app crewmates, and
  // naming them all is no help.
  const hiddenNames = useMemo(() => {
    if (hiddenCount === 0) return []
    const shown = new Set(shownMembers.map((m) => m.name))
    const hidden = orderedMembers.filter((m) => !shown.has(m.name))
    const madeByUser = (m: MemberRosterRow) => matchesSource(m, 'mine') || m.dashboard_created === true
    return [...hidden.filter(madeByUser), ...hidden.filter((m) => !madeByUser(m))]
      .slice(0, HIDDEN_NAMES_MAX)
      .map((m) => rowLabel(m))
  }, [hiddenCount, orderedMembers, shownMembers])

  return {
    isUnread, patrol, automationOf, isLoopOn, signalsOf, isNeedsYou, sortedMembers, filterCounts, narrowed, filteredOut,
    allHidden, hiddenCount, hiddenNames,
  }
}

export type MembersPageRosterSignals = ReturnType<typeof useMembersPageRosterSignals>
