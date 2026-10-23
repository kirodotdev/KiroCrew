/**
 * The roster column and the roster state it renders. `MembersPageRosterColumn`
 * is the list card: its header and add menu, the count, the search and filter
 * menu, the filter chip, the notices, the team groups with their rows
 * (`MemberRow`, in MembersPageRosterRows.tsx) and the resize grip.
 * `useMembersPageRosterView` is the state behind it: live presence from the
 * slot frames, the open crewmate's projected record and DM identity, the
 * persisted filters and sort with the question they ask (`rosterFilterQuery`)
 * and the population it lists, starring, and the held display order. The rows
 * the filters leave (`sortedMembers`) and the filter menu's tallies need the
 * live readings, so `useMembersPageRosterSignals` (useMembersPageRoster.ts)
 * computes them.
 *
 * CAPTURED: Settings > Developer > Feature Previews shows a screenshot of the
 * Crewmates page in its "See what it looks like" dialog. A visible change here
 * makes that picture stale — re-shoot with `scripts/capture-feature-previews.mjs`.
 *
 * The `MembersPage` facade calls each hook here at the point its code sits in
 * the page's hook order, which `MembersPage.owners.test.tsx` pins: React runs
 * a component's effects in registration order, so moving one is a behaviour
 * change even when every test of the moved code still passes.
 */
import { Fragment, useCallback, useMemo, useRef, useState } from 'react'
import { Check, Circle, FileUp, Goal, MessageCircleQuestionMark, Plus, Star, X, Zap } from 'lucide-react'
import { Btn } from '../../components/ui'
import { CrewMemberMark } from '../../components/CrewMemberMark'
import { useTranslation } from 'react-i18next'
import { api, type MemberRosterRow } from '../../api/client'
import { crewDisplayName } from '../../components/AgentSelector'
import { MEMBERS_ROSTER_QUERY_KEY } from '../../api/membersQuery'
import { useMutation, type QueryClient } from '@tanstack/react-query'
import { fmtList } from '../../i18n/format'
import { usePersistedBool } from '../../hooks/usePersistedBool'
import { usePersistedString } from '../../hooks/usePersistedString'
import { findReport, type ErrorReport } from '../../utils/errorReport'
import ErrorNotice from '../../components/ErrorNotice'
import { SearchFilterBar, FilterMenuButton, FilterChip, FILTER_CHIP_ROW_CLS, FilterMenuLabel, FilterMenuContent } from '../../components/SearchFilterBar'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator, DropdownMenuTrigger } from '../../components/ui/dropdown-menu'
import { hasNoCrewmates, listedByDefault, parseSort, parseSourceFilter, parseStatusFilters, rosterPopulation, sortRoster, SORT_OPTIONS, SOURCE_FILTERS, STATUS_FILTERS, type MemberSort, type MemberSourceFilter, type MemberStatusFilter, type RosterQuery } from './rosterFilter'
import ResizeHandle from '../../components/ResizeHandle'
import { LIST_SHELL_CLS, LIST_HEADER_CLS, LIST_TITLE_CLS, LIST_BODY_CLS, ROW_STATUS_CLS } from '../../components/listShell'
import { ListDock } from '../../components/ListDock'
import { tabStatus } from '../../lib/sessionTabs'
import TeamGroupHeader from './TeamGroupHeader'
import CrewmateImportDialog from './CrewmateImportDialog'
import { useMemberProjection } from '../../state/useMemberProjection'
import type { RosterView } from '../../state/memberProjectionTypes'
import type { CrewmateIdentity } from '../chat/CrewmateMessage'
import { uiLocation } from '../../uiLocations/uiLocation'
import { type ChatSlot } from '../../types'
import { type MembersPageProfileFlight } from './MembersPageProfile'
import { CrewmateEmptyHero, MemberRow } from './MembersPageRosterRows'
import { ROSTER_MAX, ROSTER_MIN, type GoneNotice, type PostCreateFailure, type TFn } from './membersPageModel'
import { type MembersPageDrawers } from './useMembersPageDrawers'
import { type MembersPageLayout } from './useMembersPageLayout'
import { type MembersPageNavigation } from './useMembersPageNavigation'
import { type MembersPageRosterRead, type MembersPageRosterSignals } from './useMembersPageRoster'
import { type MembersPageTeamsRead, type MembersPageTeamView } from './useMembersPageTeams'

/** Roster filter persistence — same `mc-` localStorage family as the rest of
 *  the dashboard's view preferences (ChatSidebar's session filters use the
 *  same idiom). Only the TOGGLES live here; the star mark itself is a crew
 *  field on the server. */
const STARRED_ONLY_KEY = 'mc-members-starred-only'
const SOURCE_FILTER_KEY = 'mc-members-source'
const STATUS_FILTER_KEY = 'mc-members-status'
const SORT_KEY = 'mc-members-sort'
/** Static key per menu row — a map, not a template, so `check-i18n-keys` can
 *  resolve every reference (assembled keys are a counted blind spot there). */
const SOURCE_LABEL_KEY: Record<Exclude<MemberSourceFilter, 'all'>, string> = {
  mine: 'pages.membersPage.filter_source_mine',
  builtin: 'pages.membersPage.filter_source_builtin',
  package: 'pages.membersPage.filter_source_package',
}
/** Hover tooltip per origin row: the one-word labels ("From packages") are not
 *  self-explaining to a reader who has never installed a capability package. */
const SOURCE_TITLE_KEY: Record<Exclude<MemberSourceFilter, 'all'>, string> = {
  mine: 'pages.membersPage.filter_source_mine_description',
  builtin: 'pages.membersPage.filter_source_builtin_description',
  package: 'pages.membersPage.filter_source_package_description',
}
/** The two states the sessions menu also filters on reuse ITS labels, so the
 *  same slot state never reads as two different words across the two menus. */
const STATUS_LABEL_KEY: Record<MemberStatusFilter, string> = {
  working: 'pages.chatSidebar.filter_running',
  needs_you: 'pages.membersPage.filter_status_needs_you',
  unread: 'pages.chatSidebar.filter_unread',
  patrolling: 'pages.membersPage.filter_status_patrolling',
}
const STATUS_TITLE_KEY: Record<MemberStatusFilter, string> = {
  working: 'pages.membersPage.filter_status_working_description',
  needs_you: 'pages.membersPage.filter_status_needs_you_description',
  unread: 'pages.membersPage.filter_status_unread_description',
  patrolling: 'pages.membersPage.filter_status_patrolling_description',
}
/** Each status row's marker — the same glyph the roster row and the sidebar's
 *  session filters use for that state, lit in the state's colour when active. */
const STATUS_ICON: Record<MemberStatusFilter, (active: boolean) => React.ReactNode> = {
  working: (active) => <Zap size={12} className={active ? 'text-[var(--warn)]' : 'text-muted'} {...(active ? { fill: 'var(--warn)', stroke: 'none' } : {})} />,
  needs_you: (active) => <MessageCircleQuestionMark size={12} className={active ? 'text-[var(--info)]' : 'text-muted'} />,
  // Status token, not brand accent: this row is the legend/toggle for the same
  // unread state whose roster dot reads `var(--ok)` (`MemberRow`, in MembersPageRosterRows.tsx),
  // and for the sidebar chip that was rebound in #10488 (#10479).
  unread: (active) => <Circle size={12} className={active ? 'text-[var(--ok)]' : 'text-muted'} {...(active ? { strokeWidth: 0, fill: 'var(--ok)' } : {})} />,
  patrolling: (active) => <Goal size={12} className={active ? 'text-accent' : 'text-muted'} />,
}
/** The A–Z row reuses the sidebar menu's label. The activity row does NOT
 *  reuse the sidebar's "Newest": on a list of people that reads as "newest
 *  member", while the order is last activity — so it keeps a member-specific
 *  word for what it actually sorts by. */
const SORT_LABEL_KEY: Record<MemberSort, string> = {
  recent: 'pages.membersPage.sort_recent',
  name: 'pages.chatSidebar.sort_name_asc',
}

interface MembersPageRosterViewOptions {
  rosterRead: MembersPageRosterRead
  teamsRead: MembersPageTeamsRead
  t: TFn
  queryClient: QueryClient
  activeName: string
  slotKeyOf: (m: MemberRosterRow) => string
  liveSlots: ChatSlot[]
  active: MemberRosterRow | undefined
}

/** Live presence, the open crewmate's projected view, filters, starring and the held order. */
export function useMembersPageRosterView({
  rosterRead, teamsRead, t, queryClient, activeName, slotKeyOf, liveSlots, active,
}: MembersPageRosterViewOptions) {
  const { projectionSlug, members } = rosterRead
  const { teamsQ } = teamsRead
  const liveRunning = useMemo(() => {
    const byKey: Record<string, boolean> = {}
    for (const s of liveSlots) {
      if (s.mode === 'member') byKey[s.key] = !!(s.running || s.subagents_running)
    }
    return byKey
  }, [liveSlots])
  const isRunning = useCallback(
    (m: MemberRosterRow) => {
      const key = slotKeyOf(m)
      return key && key in liveRunning ? liveRunning[key] : m.running
    },
    [slotKeyOf, liveRunning],
  )
  // A member turn parked on the user — an approval or a question — read off
  // the same slot frames; `tabStatus` is the shared ranking of those two.
  const liveNeedsYou = useMemo(() => {
    const byKey: Record<string, boolean> = {}
    for (const sl of liveSlots) {
      if (sl.mode !== 'member') continue
      const st = tabStatus(sl, [], sl.key)
      byKey[sl.key] = st === 'permission' || st === 'question'
    }
    return byKey
  }, [liveSlots])
  // The active member's projected roster view over its server row: the drawer
  // Configuration and header read config fields (kiro_agent/model/workspace/
  // memory_store) and last_active_ts from the projection when present, the row
  // otherwise. `running` stays live (isRunning), not projected.
  const activeRoster = useMemberProjection<RosterView>(projectionSlug(active?.slug), 'roster')
  const activeView = useMemo<MemberRosterRow | undefined>(() => {
    if (!active) return undefined
    if (!activeRoster) return active
    return {
      ...active,
      kiro_agent: activeRoster.kiro_agent ?? active.kiro_agent,
      workspace: activeRoster.workspace ?? active.workspace,
      memory_store: activeRoster.memory_store ?? active.memory_store,
      model: activeRoster.model ?? active.model,
      source: activeRoster.source ?? active.source,
      starred: activeRoster.starred ?? active.starred,
      avatar: activeRoster.avatar ?? active.avatar,
      display_name: activeRoster.display_name ?? active.display_name,
      slot_key: activeRoster.slot_key ?? active.slot_key,
      // Transcript-first, for the reason `MemberRow` (MembersPageRosterRows.tsx) states: the
      // projection's message copy is a second copy and a refused append leaves it behind.
      last_active_ts: active.last_active_ts || activeRoster.last_active_ts,
      last_message: active.last_message || activeRoster.last_message,
    }
  }, [active, activeRoster])
  // The identity the DM pane draws the crewmate's messages under. Memoised on
  // the two fields so the pane's renderer memo does not rebuild per render.
  const crewmateName = activeView?.name
  const crewmateAvatar = activeView?.avatar
  const crewmateLabel = activeView ? crewDisplayName(activeView) : undefined
  const crewmateIdentity = useMemo<CrewmateIdentity | undefined>(
    () => (crewmateName ? { name: crewmateName, avatar: crewmateAvatar, label: crewmateLabel } : undefined),
    [crewmateName, crewmateAvatar, crewmateLabel],
  )
  // Most-recently-active first (like any IM member list); never-talked
  // members fall to the bottom alphabetically. Sorted from the cached roster,
  // which changes only when the cache does — a return to the page, a focus
  // after the stale window, a registry write elsewhere — never on a live
  // message, so rows do not move under the cursor mid-conversation.
  const [filter, setFilter] = useState('')
  const [filterMenuOpen, setFilterMenuOpen] = useState(false)
  // Persistent roster filters. The agent sync writes every package-installed
  // agent spec into the roster, so a host with a few dozen installed packages
  // shows dozens of crews the user never drives. Both toggles survive a page
  // change (same localStorage idiom as ChatSidebar's session filters); the
  // star itself is server-side (`starred` on the crew), so it survives a
  // reinstall and follows the config to another dashboard.
  const [starredOnly, setStarredOnly] = usePersistedBool(STARRED_ONLY_KEY, false)
  const [rawSourceFilter, setRawSourceFilter] = usePersistedString(SOURCE_FILTER_KEY, 'all')
  // Storage is hand-editable: an unknown stored value reads as "all".
  const sourceFilter = parseSourceFilter(rawSourceFilter)
  const toggleStarredOnly = useCallback(() => setStarredOnly((prev) => !prev), [setStarredOnly])
  const pickSource = useCallback(
    (next: MemberSourceFilter) => {
      // Choosing the active origin clears it back to "all" — one radio-like
      // group, no separate reset row.
      setRawSourceFilter((prev) => (parseSourceFilter(prev) === next ? 'all' : next))
    },
    [setRawSourceFilter],
  )
  // Live-state filters (working / needs you / unread / patrolling) and the
  // sort, persisted like the sidebar's session filters. Stored as strings so
  // the parse is the single place junk from storage is rejected.
  const [rawStatusFilter, setRawStatusFilter] = usePersistedString(STATUS_FILTER_KEY, '[]')
  const statusFilter = useMemo(() => parseStatusFilters(rawStatusFilter), [rawStatusFilter])
  const toggleStatus = useCallback(
    (key: MemberStatusFilter) =>
      setRawStatusFilter((prev) => {
        const next = parseStatusFilters(prev)
        if (next.has(key)) next.delete(key)
        else next.add(key)
        return JSON.stringify([...next])
      }),
    [setRawStatusFilter],
  )
  const [rawSort, setRawSort] = usePersistedString(SORT_KEY, 'recent')
  const sort = parseSort(rawSort)
  const clearFilters = useCallback(() => {
    setStarredOnly(false)
    setRawSourceFilter('all')
    setRawStatusFilter('[]')
  }, [setStarredOnly, setRawSourceFilter, setRawStatusFilter])
  // Star toggle: optimistic flip, reverted if the PUT fails. The star lives
  // on the crew record, not the DM thread, so it goes through the crew
  // update endpoint rather than a members route. A failed write (403 for a
  // non-owner, 500 on a failed config save) is SURFACED, not just reverted:
  // a star that snaps back with no message reads as a broken button, and
  // AUTOSDE's errors-use-error-notice forbids the silent catch-to-default.
  // Display text is the localized `star_failed` copy; the structured report
  // (endpoint, status, code, detail) is looked up from the thrown message
  // and passed to ErrorNotice explicitly, so the agent hand-off keeps it.
  const [starError, setStarError] = useState<{ message: string; report?: ErrorReport } | null>(null)
  // Names with a star write in flight. The control is disabled while its
  // write is pending, so two rapid toggles cannot race: without this, a
  // second click whose write also fails would revert to the FIRST click's
  // value and leave the row starred while the server is not.
  const [starPending, setStarPending] = useState<Set<string>>(() => new Set())
  // The optimistic flip and its revert are per-ROW functional patches on the
  // roster cache, not a whole-roster snapshot restore: two members starred in
  // quick succession must not have the second's failure undo the first.
  const patchStar = useCallback(
    (name: string, starred: boolean) =>
      queryClient.setQueryData<MemberRosterRow[]>(MEMBERS_ROSTER_QUERY_KEY, (rows) =>
        rows?.map((r) => (r.name === name ? { ...r, starred } : r)),
      ),
    [queryClient],
  )
  const starMutation = useMutation({
    mutationFn: ({ m, next }: { m: MemberRosterRow; next: boolean }) =>
      api.updateKirocrewAgent(m.name, { starred: next }),
    onMutate: async ({ m, next }) => {
      // A roster refetch already in flight would land AFTER the optimistic
      // patch and overwrite it with the pre-write row: stop it first. (The
      // row's pending lock is taken synchronously in toggleStar, before this
      // async hook, so a second click in the same tick finds it disabled.)
      await queryClient.cancelQueries({ queryKey: MEMBERS_ROSTER_QUERY_KEY })
      setStarError(null)
      patchStar(m.name, next)
    },
    onSuccess: async (_data, { m, next }) => {
      // Re-apply the CONFIRMED value — and first cancel any roster refetch
      // that started after onMutate's cancel (focus, stale window, a refresh
      // frame): such a GET can have read the pre-write row while the PUT was
      // in flight, and resolving AFTER this patch it would overwrite the
      // confirmed star with the stale one. After a 2xx the server holds
      // `next`; the row must say so regardless of what was in flight.
      await queryClient.cancelQueries({ queryKey: MEMBERS_ROSTER_QUERY_KEY })
      patchStar(m.name, next)
    },
    onError: async (err: unknown, { m, next }) => {
      // Same reason as onSuccess: a refetch that started after onMutate's
      // cancel must not land its snapshot on top of the final row.
      await queryClient.cancelQueries({ queryKey: MEMBERS_ROSTER_QUERY_KEY })
      patchStar(m.name, !next)
      // Localized copy, never the raw server text: the client throws the
      // response body (or `HTTP 500`), which is neither translated nor
      // meant for a user. The journaled report is recovered from that
      // message and handed to ErrorNotice so "Ask the agent" still carries
      // endpoint / status / code / detail.
      setStarError({
        message: t('pages.membersPage.star_failed'),
        report: findReport(err instanceof Error ? err.message : undefined),
      })
    },
    onSettled: (_data, _err, { m }) => {
      setStarPending((prev) => {
        const n = new Set(prev)
        n.delete(m.name)
        return n
      })
    },
    // No invalidation on success, deliberately: the roster row is the only
    // reader of `starred`, and after a 2xx the re-applied row IS the server's
    // state — a refetch would re-render the whole list to change nothing.
  })
  const { mutate: mutateStar } = starMutation
  const toggleStar = useCallback(
    (m: MemberRosterRow) => {
      // Lock the row NOW, synchronously: useMutation's onMutate runs a
      // microtask later, and a second click landing before it would start a
      // second write whose failure could revert the first's value.
      setStarPending((prev) => new Set(prev).add(m.name))
      mutateStar({ m, next: !m.starred })
    },
    [mutateStar],
  )
  // Display order before the search filter — this is the roster the rows
  // render from and the list the landing fallback (`resolveDefaultMember`)
  // reads, so a typed filter never changes the order or which member a
  // return visit opens (the memory lookup is by NAME, so a re-sort cannot
  // change which member it finds).
  //
  // The order is HELD across refetches, and re-sorted on exactly three events:
  // a membership change, the user picking the other sort, and the OPEN
  // crewmate's recency advancing.
  //
  // The hold is what a refetch must not break. The roster query refetches on
  // every server refresh frame, on window focus and on staleness; re-sorting
  // when some background crewmate's `last_active_ts` advances would move rows
  // under the cursor mid-click and open a different member's durable pinned
  // thread. Row CONTENT (star, last-message preview, presence) still updates
  // live from every refetch — only the ordering is held.
  //
  // The third event is what the hold must not swallow, and it is not a
  // background one: the open crewmate's recency can only advance because of
  // what the user just did in the thread in front of them — their own send, or
  // the reply it started. Sorting by Recent and then watching the crewmate you
  // are messaging sit in its alphabetical place is the whole of #15276. It
  // settles after one move, because a row already at the top does not move
  // again when its recency advances further. A row becoming the open one
  // re-baselines the reading below WITHOUT re-sorting, so merely opening a
  // member still never shuffles the list.
  //
  // And it moves that ONE ROW, by lifting it out of the committed order and
  // putting it first — never by re-sorting the list. A whole re-sort would
  // publish every recency that arrived since the order was committed, so a
  // background crewmate that advanced quietly during this visit would jump too:
  // the hold would be spent, in a frame the user caused but did not ask for, and
  // rows WOULD move under the cursor. Lifting one row is also what the user's
  // model says happened: I messaged this crewmate, so this crewmate is first.
  // The other rows keep their positions relative to each other.
  //
  // Recency-ordered only. Under the by-name sort a recency advance says nothing
  // about where a row belongs, so the lift does not run and the name order
  // stands.
  const committedOrderRef = useRef<{
    sort: MemberSort
    names: string[]
    openName: string
    openTs: number
  }>({ sort, names: [], openName: '', openTs: 0 })
  const orderedMembers = useMemo(() => {
    const byName = new Map(members.map((m) => [m.name, m]))
    const prev = committedOrderRef.current
    const sameMembership =
      prev.sort === sort && prev.names.length === byName.size && prev.names.every((n) => byName.has(n))
    // Sorted on the MERGED member, which is the freshest reading of the one
    // folded recency: the server row carries `last_active_ts` from the same
    // roster projection, and the merge takes the greater of row and pushed
    // frame, so the merged value leads the raw row rather than lagging it.
    // Overlaying the raw rows here instead would discard every pushed frame the
    // merge has already taken, which is the recency a re-sort exists to read.
    const openTs = (activeName ? byName.get(activeName)?.last_active_ts : 0) ?? 0
    const openAdvanced =
      sort === 'recent' && !!activeName && activeName === prev.openName && openTs > prev.openTs
    let names = sameMembership ? prev.names : sortRoster(members, sort).map((m) => m.name)
    if (sameMembership && openAdvanced && names[0] !== activeName && byName.has(activeName)) {
      names = [activeName, ...names.filter((n) => n !== activeName)]
    }
    committedOrderRef.current = { sort, names, openName: activeName, openTs }
    return names.map((n) => byName.get(n)).filter((m): m is MemberRosterRow => !!m)
  }, [members, sort, activeName])
  // The rows wait for the team read: one team turns the flat list into
  // groups, so a list drawn before it answered reshuffled under team headers
  // a beat later. A cached read (any return to this tab) is settled on the first
  // render, so this holds only a cold load.
  const rowsReady = teamsQ.data !== undefined || teamsQ.isError
  // Named apart from `rosterQuery` (useMembersPageRoster.ts): that one is the React Query READ of
  // the roster, this one is the user's filter/sort question asked of it.
  const rosterFilterQuery = useMemo<RosterQuery>(
    () => ({ search: filter, starredOnly, source: sourceFilter, status: statusFilter, sort, chosen: activeName }),
    [filter, starredOnly, sourceFilter, statusFilter, sort, activeName],
  )
  // The rows the roster is about right now: crewmates the user chatted with
  // (or starred), or -- with a search typed -- whatever the search reaches, hidden rows
  // included. The header count, the "N of M" and the filter menu's tallies read
  // THIS list, never `members`, so no count includes a row the user cannot see.
  const shownMembers = useMemo(() => rosterPopulation(members, rosterFilterQuery), [members, rosterFilterQuery])
  // The auto-open's candidates: only rows the roster lists unasked. A hidden
  // row can still be opened by name (a deep link, the search), but must not be
  // what the page opens on its own -- a thread standing over a roster that does
  // not show its row reads as a misroute.
  const listedMembers = useMemo(
    () => orderedMembers.filter((m) => listedByDefault(m)),
    [orderedMembers],
  )

  return {
    isRunning, liveNeedsYou, activeRoster, activeView, crewmateLabel, crewmateIdentity, filter, setFilter,
    filterMenuOpen, setFilterMenuOpen, starredOnly, sourceFilter, toggleStarredOnly, pickSource, statusFilter,
    toggleStatus, setRawSort, sort, clearFilters, setStarError, starError, starPending, toggleStar,
    orderedMembers, rowsReady, rosterFilterQuery, shownMembers, listedMembers,
  }
}

export type MembersPageRosterView = ReturnType<typeof useMembersPageRosterView>

interface MembersPageRosterColumnProps {
  rosterRead: MembersPageRosterRead
  teamsRead: MembersPageTeamsRead
  layout: MembersPageLayout
  drawers: MembersPageDrawers
  rosterView: MembersPageRosterView
  rosterSignals: MembersPageRosterSignals
  profileFlight: MembersPageProfileFlight
  navigation: MembersPageNavigation
  teamView: MembersPageTeamView
  activeName: string
  scrollActiveRowIntoView: (el: HTMLButtonElement | null) => void
  gone: GoneNotice | null
  createOpen: boolean
  openAdvanced: () => void
  openQuick: () => void
  postCreateError: PostCreateFailure | null
  createHeld: string | null
  slotKeyOf: (m: MemberRosterRow) => string
  /** The crewmate whose face is centre stage in the first Mate's entrance
   *  (#23207), so its row holds the face unpainted until it lands; `''` for none. */
  faceOnStageFor: string
}

/** The roster card. On a phone the facade portals it into the roster drawer (`placeRoster`). */
export function MembersPageRosterColumn({
  rosterRead, teamsRead, layout, drawers, rosterView, rosterSignals, profileFlight, navigation, teamView,
  activeName, scrollActiveRowIntoView, gone, createOpen, openAdvanced, openQuick, postCreateError, createHeld,
  slotKeyOf, faceOnStageFor,
}: MembersPageRosterColumnProps) {
  const { t } = useTranslation()
  const [importing, setImporting] = useState(false)
  const { rosterQuery, loaded, loadError, collidingSlugs, members } = rosterRead
  const { teamsNoticeShown, dismissTeamsNotice, activeTeam, collapsedTeams, toggleTeamCollapsed } = teamsRead
  const { roster, rosterPinned, rosterFolded, rosterShown, rosterAsideRef, setRosterPin } = layout
  const { closeRosterDrawer, rosterDrawerAvailable } = drawers
  const {
    isRunning, filter, setFilter, filterMenuOpen, setFilterMenuOpen, starredOnly, sourceFilter,
    toggleStarredOnly, pickSource, statusFilter, toggleStatus, setRawSort, sort, clearFilters, setStarError,
    starError, starPending, toggleStar, rowsReady, shownMembers,
  } = rosterView
  const {
    isUnread, patrol, automationOf, isNeedsYou, sortedMembers, filterCounts, narrowed, filteredOut, allHidden,
    hiddenCount, hiddenNames,
  } = rosterSignals
  const { reduceMotion } = profileFlight
  const { openMember, pickFromRosterDrawer } = navigation
  const { openTeam, rosterGroups, grouped } = teamView
  // What the aggregate chip says: each active filter's menu label with its
  // count (Starred (2), In progress (1), Mine (6)) — and the bare names for
  // the chip's accessible "Clear … filter" name.
  const activeFilterNames = useMemo(() => {
    const out: string[] = []
    if (starredOnly) out.push(t('pages.membersPage.filter_starred'))
    for (const key of STATUS_FILTERS) if (statusFilter.has(key)) out.push(t(STATUS_LABEL_KEY[key]))
    if (sourceFilter !== 'all') out.push(t(SOURCE_LABEL_KEY[sourceFilter]))
    return out
  }, [t, starredOnly, statusFilter, sourceFilter])
  const activeFilterLabels = useMemo(() => {
    const out: string[] = []
    if (starredOnly) out.push(`${t('pages.membersPage.filter_starred')} (${filterCounts.starred})`)
    for (const key of STATUS_FILTERS) if (statusFilter.has(key)) out.push(`${t(STATUS_LABEL_KEY[key])} (${filterCounts.status[key]})`)
    if (sourceFilter !== 'all') out.push(`${t(SOURCE_LABEL_KEY[sourceFilter])} (${filterCounts.source[sourceFilter]})`)
    return out
  }, [t, starredOnly, statusFilter, sourceFilter, filterCounts])
  return (
    <aside
            // Below md the roster is the whole width, so it yields whenever the
            // chat column must be seen: an open chat, or a ROSTER post-create
            // notice (a failed re-read opens no chat, and a full-width `shrink-0`
            // roster would push the notice and its retry off-screen). A GREETING
            // notice does not imply an open chat — the header back can close the
            // chat under it — so it alone must not hide the roster, or a phone
            // shows a notice bar over a blank column until it is dismissed. An
            // open team view takes the column the same way an open chat does.
            // Pinned from the switcher, the column returns beside the thread at
            // md and up only: below md the roster is the drawer over the chat.
            // A team view keeps the column at md and up whether or not it is
            // pinned: the team header draws no switcher and its own Back is
            // `md:hidden`, so without the column a desktop `/members?team=<id>`
            // had no way back to the roster.
            className={`${
              rosterDrawerAvailable
                ? 'flex h-full'
                : rosterFolded && !activeTeam
                  ? 'hidden'
                  : activeName || activeTeam || createOpen || postCreateError
                    ? 'hidden md:flex'
                    : 'flex'
            } ${LIST_SHELL_CLS} relative w-full md:w-[var(--roster-w)] shrink-0 flex-col min-h-0`}
            // CSS owns the breakpoint: the var is set unconditionally and only the
            // md: class consumes it, so resizing the window across 768px reacts
            // without any JS media-query snapshot going stale.
            style={{ '--roster-w': `${roster.width}px` } as React.CSSProperties}
            ref={rosterAsideRef}
            data-testid="member-roster"
          >
            <div className={LIST_HEADER_CLS}>
              {/* pl-1.5 is the sidebar's title inset when no rail toggle sits
                  before it; the page icon leads the title where the sidebar's
                  reads bare, because this header names a page, not a pane.
                  The icon is the same two-ghost brand mark the nav rail draws
                  for this page (`components/CrewMemberMark.tsx`), so the rail
                  row and the page it opens name the thing with one glyph. */}
              <div className="flex items-center gap-1.5 min-w-0 flex-1 pl-1.5">
                <CrewMemberMark size={15} className="inline-block text-muted shrink-0" />
                <h1 className={LIST_TITLE_CLS}>{t('pages.membersPage.title')}</h1>
              </div>
              {/* Two things can be added here, so the "+" opens a menu: a crewmate
                  or a team (the dialogs, in MembersPage.tsx). The trigger keeps the bare Plus
                  and its label. */}
              {/* The crewmate item opens the in-page New crewmate dialog
                  (`NewCrewmateDialog`): creating a crewmate IS creating a crew,
                  and the dialog posts to the same endpoint the crew manager's
                  form does, so the page needs no hand-off to that manager (#9513
                  named the hand-off landing; the dialog replaced it). A bare
                  `Plus`, not `UserPlus`: the page icon beside it already says
                  "crewmates", and a person-figure here would be the one Lucide
                  person on a page whose crewmates are drawn as ghosts. */}
              {/* The crewmate item is held while a create's follow-up step
                  (roster re-read, chat open) is still in flight or failed:
                  `openCreated` starts by clearing that record, so a second
                  create here would silently drop the first one's retry. The
                  notice above the chat column names the step; its retry or
                  dismissal is what re-enables the item. The hold is the ITEM's,
                  not the menu's, so a second door added here is unaffected. */}
              {/* NEW TEAM IS HIDDEN FOR THE CURRENT PHASE. Teams are not ready to
                  be offered as something to create, so the only door that made one
                  is not drawn. Everything a team already has keeps working: the
                  roster still groups by team, a group header still opens its team
                  view, and that view's Edit still opens this dialog in edit mode
                  (`TeamDialog` in MembersPage.tsx, `team` set). Restoring the door is reverting
                  the commit that removed the item: the dialog's create mode, its
                  `team_new` / `team_create` labels and `api.teams.create` are all
                  still here and still covered (`TeamDialog.test.tsx`). */}
              {/* The whole menu is hidden while the empty roster's hero is the
                  create door: two doors to one dialog read as two different
                  actions. It returns with the first row, when the hero is gone.
                  Also hidden until the first read has answered: the names the
                  dialog checks a new one against are unknown until then. */}
              {loaded && !(!loadError && hasNoCrewmates(members)) && (
              <DropdownMenu guideScope="menu:members.add-menu">
                <DropdownMenuTrigger asChild>
                  <button
                    className="flex items-center justify-center w-7 h-7 rounded-md transition-colors bg-transparent border-none shrink-0 text-muted hover:text-text hover:bg-bg-hover cursor-pointer data-[state=open]:bg-bg-hover data-[state=open]:text-text"
                    aria-label={t('pages.membersPage.add_menu')}
                    title={t('pages.membersPage.add_menu')}
                    data-testid="member-add"
                    {...uiLocation('members.add-menu')}
                  >
                    <Plus size={15} />
                  </button>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="end" data-testid="member-add-menu">
                  <DropdownMenuItem
                    onSelect={openQuick}
                    disabled={createHeld !== null}
                    // A disabled Radix item takes no pointer events, so a `title`
                    // would never show; the hold reason is written under the label
                    // instead, where mouse, keyboard and touch users all read it.
                    className={createHeld !== null ? 'items-start' : undefined}
                    data-testid="member-add-crewmate"
                  >
                    <Plus size={13} className="lucide-inline text-muted shrink-0" aria-hidden="true" />
                    <span className="flex-1 min-w-0 flex flex-col">
                      <span {...uiLocation('members.add-menu.new')}>{t('pages.membersPage.add_member')}</span>
                      {createHeld !== null && (
                        <span className="text-[11.5px] text-muted whitespace-normal" data-testid="member-add-crewmate-held">
                          {createHeld}
                        </span>
                      )}
                    </span>
                  </DropdownMenuItem>
                  {/* Import from file, the same dialog the switcher opens, so the
                      phone roster (where the switcher is hidden) reaches it too. */}
                  {createHeld === null && (
                    <DropdownMenuItem onSelect={() => setImporting(true)} data-testid="member-add-import">
                      <FileUp size={13} className="lucide-inline text-muted shrink-0" aria-hidden="true" />
                      <span {...uiLocation('members.add-menu.import')}>{t('pages.membersPage.import_from_file')}</span>
                    </DropdownMenuItem>
                  )}
                </DropdownMenuContent>
              </DropdownMenu>
              )}
              {importing && <CrewmateImportDialog open onOpenChange={setImporting} />}
              {/* The pinned roster's own way out, beside a thread on desktop — a
                  second close for the pin the switcher's footer action also lifts.
                  Not in a team view: the column stands there on its own, so
                  lifting the pin would change nothing on screen. md+ only, like
                  the pin itself. */}
              {rosterPinned && activeName && (
                <button
                  type="button"
                  onClick={() => setRosterPin(false)}
                  className="hidden md:flex items-center justify-center w-7 h-7 rounded-md transition-colors bg-transparent border-none shrink-0 text-muted hover:text-text hover:bg-bg-hover cursor-pointer"
                  aria-label={t('pages.membersPage.roster_hide')}
                  title={t('pages.membersPage.roster_hide')}
                  data-testid="member-roster-hide"
                >
                  <X size={15} aria-hidden="true" />
                </button>
              )}
            </div>
            {/* The count reads the cached roster; after a create whose re-read
                failed that cache is a list without the new crewmate, and "0
                crewmates" under "Radar was created" contradicts the notice. The
                count reads as a dash until the retry refreshes the list — the SAME
                dash a failed arrival read shows: one treatment of "count unknown",
                not a dash in one case and a vanished line in the other. */}
            <div className={`px-4 pb-2 ${ROW_STATUS_CLS} text-muted`} data-testid="member-count">
              {/* "N of M" while any filter (not the search) narrows the list, so
                  the header never contradicts a 1-row or empty view below it.
                  With no roster to count (the read failed, or the post-create
                  re-read did) the line is a dash: "0 members" above "Could not
                  load the member roster" would state as fact what is only
                  unknown. The rows waiting on the team read (`rowsReady`) read
                  the same way: "8 crewmates" above an empty list contradicts it. */}
              {loadError || postCreateError?.kind === 'roster' || !rowsReady
                ? '\u2014'
                : narrowed
                  ? t('pages.membersPage.member_count_filtered', {
                      shown: sortedMembers.length,
                      count: shownMembers.length,
                    })
                  : t('pages.membersPage.member_count', { count: shownMembers.length })}
            </div>
            {/* A failed registry read blanks EVERY roster badge at once. That is
                not "no member has a patrol" — it is a page-level unknown, so it
                is said here, on the roster the badges live on, not only inside
                whichever drawer happens to be open. Same shared notice as the
                drawer block; a read failure on a page holding no draft is safe
                to hand to the agent. */}
            {patrol.failed && (
              <div className="px-4 pb-2">
                <ErrorNotice
                  message={t('pages.membersPage.patrol_error_roster')}
                  variant="inline"
                  askAgent
                  testId="member-roster-patrol-error"
                />
              </div>
            )}
            {/* A failed team read renders the roster FLAT, which is not "no teams"
                -- it is unknown, and said here on the roster the grouping lives
                on. No draft on this page, so the hand-off is safe. */}
            {teamsNoticeShown && (
              <div className="px-4 pb-2">
                <ErrorNotice
                  message={t('pages.membersPage.teams_load_failed')}
                  variant="inline"
                  askAgent
                  onDismiss={dismissTeamsNotice}
                  testId="member-roster-teams-error"
                />
              </div>
            )}
            {/* The floating dock (components/ListDock): the glass search capsule and
                the filter chip hover over the roster, which scrolls under them. */}
            <ListDock field={(
              // The Sessions sidebar's search row (components/SearchFilterBar): the
              // same field, clear button and inline sort/filter menu. The menu holds
              // what the sidebar's holds for sessions, in the roster's terms — a
              // star toggle, the member's live state, its origin, and the sort.
              // Menu rows keep the menu open (preventDefault) so several can be
              // toggled in one visit, as the sidebar's do.
            <SearchFilterBar
              className="px-2"
              placeholder={t('pages.membersPage.search_members')}
              clearLabel={t('pages.chatSidebar.clear_search')}
              value={filter}
              onChange={setFilter}
              inputTestId="member-search"
              trailing={(
                <DropdownMenu open={filterMenuOpen} onOpenChange={setFilterMenuOpen}>
                  <DropdownMenuTrigger asChild>
                    {/* Badge = unread count, the same meaning the sidebar's
                        trigger badge carries, so one learned reading serves both. */}
                    <FilterMenuButton
                      title={t('pages.membersPage.sort_filter_members')}
                      badge={filterCounts.status.unread}
                      testId="member-filter-menu"
                    />
                  </DropdownMenuTrigger>
                  <FilterMenuContent align="end" data-testid="member-filters">
                    <FilterMenuLabel>{t('pages.chatSidebar.filter')}</FilterMenuLabel>
                    <DropdownMenuItem
                      onSelect={(e) => { e.preventDefault(); toggleStarredOnly() }}
                      role="menuitemcheckbox"
                      aria-checked={starredOnly}
                      title={t('pages.membersPage.filter_starred_description')}
                      data-testid="member-filter-starred"
                    >
                      <Star size={12} className={starredOnly ? 'text-accent' : 'text-muted'} {...(starredOnly ? { fill: 'var(--accent)', stroke: 'none' } : {})} />
                      <span className="flex-1 truncate">{t('pages.membersPage.filter_starred')}</span>
                      {/* Every filter row shows its count the same way, 0 included:
                          a zero-count row is exactly the one that blanks the list
                          when chosen, whichever section it sits in. */}
                      <span className="text-muted text-[11px] shrink-0">{filterCounts.starred}</span>
                      {starredOnly && <Check size={14} className="text-accent shrink-0" />}
                    </DropdownMenuItem>
                    {STATUS_FILTERS.map((key) => {
                      const active = statusFilter.has(key)
                      return (
                        <DropdownMenuItem
                          key={key}
                          onSelect={(e) => { e.preventDefault(); toggleStatus(key) }}
                          role="menuitemcheckbox"
                          aria-checked={active}
                          title={t(STATUS_TITLE_KEY[key])}
                          data-testid={`member-filter-status-${key}`}
                        >
                          {STATUS_ICON[key](active)}
                          <span className="flex-1 truncate">{t(STATUS_LABEL_KEY[key])}</span>
                          <span className="text-muted text-[11px] shrink-0">{filterCounts.status[key]}</span>
                          {active && <Check size={14} className="text-accent shrink-0" />}
                        </DropdownMenuItem>
                      )
                    })}
                    <DropdownMenuSeparator />
                    <FilterMenuLabel>{t('pages.membersPage.filter_origin')}</FilterMenuLabel>
                    {SOURCE_FILTERS.map((key) => {
                      const active = sourceFilter === key
                      return (
                        <DropdownMenuItem
                          key={key}
                          onSelect={(e) => { e.preventDefault(); pickSource(key) }}
                          role="menuitemradio"
                          aria-checked={active}
                          title={t(SOURCE_TITLE_KEY[key])}
                          data-testid={`member-filter-source-${key}`}
                        >
                          <span className="flex-1 truncate">{t(SOURCE_LABEL_KEY[key])}</span>
                          {/* 0 is rendered, not omitted: a zero-count origin is
                              exactly the one that blanks the list when chosen. */}
                          <span className="text-muted text-[11px] shrink-0">{filterCounts.source[key]}</span>
                          {active && <Check size={14} className="text-accent shrink-0" />}
                        </DropdownMenuItem>
                      )
                    })}
                    <DropdownMenuSeparator />
                    <FilterMenuLabel>{t('pages.chatSidebar.sort_by')}</FilterMenuLabel>
                    {SORT_OPTIONS.map((key) => (
                      <DropdownMenuItem
                        key={key}
                        onSelect={() => setRawSort(key)}
                        role="menuitemradio"
                        aria-checked={sort === key}
                        data-testid={`member-sort-${key}`}
                      >
                        <span className="flex-1">{t(SORT_LABEL_KEY[key])}</span>
                        {sort === key && <Check size={14} className="text-accent shrink-0" />}
                      </DropdownMenuItem>
                    ))}
                  </FilterMenuContent>
                </DropdownMenu>
              )}
            />
            )} shelf={(
              <>
            {/* The at-rest marker that the list is narrowed: ONE aggregate chip in
                the sidebar's chip recipe (components/SearchFilterBar), naming every
                active filter, so a returning user sees WHY the roster is short and
                clears the lot with one click instead of reopening the menu. One
                control, not one per filter — up to six sibling buttons would break
                AUTOSDE max-two-buttons-per-row — and in the NEUTRAL aggregate shape
                the sidebar's tag filter uses for "clears several": a coloured pill
                means "clears this one filter" on both lists, a neutral pill means
                "clears them all". The search text is not in it: it is visible in
                the field, with its own clear button. */}
            {narrowed && (
              <div className={FILTER_CHIP_ROW_CLS} data-testid="member-filter-chips">
                {/* The visible text IS the outcome of the click — "Clear Starred (2),
                    Mine (6) filter" — the same sentence the accessible name carries,
                    so a reader never has to guess whether the one ✕ drops one filter
                    or all of them. One existing key, one {{var}}: no glued strings. */}
                <FilterChip
                  aggregate
                  label={t('pages.chatSidebar.clear_named_filter', { filter: fmtList(activeFilterLabels, { type: 'unit', style: 'short' }) })}
                  clearLabel={t('pages.chatSidebar.clear_named_filter', { filter: fmtList(activeFilterNames) })}
                  onClear={clearFilters}
                  testId="member-filter-chip"
                />
              </div>
            )}
            {/* Star-write failure. Falsy message renders nothing. askAgent is ON:
                the roster holds no unsaved draft, so the hand-off's navigation
                destroys nothing (AUTOSDE errors-use-error-notice). */}
            {/* Mounted only while there IS an error: the wrapper sits on the dock's
                shelf, and an empty wrapper would keep the shelf (and its 4px scrim)
                open under a bare field. */}
            {starError && (
              <div className="px-2">
                <ErrorNotice
                  message={starError.message}
                  report={starError.report}
                  title={t('pages.membersPage.star_failed_title')}
                  onDismiss={() => setStarError(null)}
                  askAgent
                  actionPlacement="below"
                  testId="member-star-error"
                />
              </div>
            )}
            {gone && gone.shown === '' && (
              /* The roster is the answer surface when there is no thread to stand
                 in the gone member's place: a gone `?member=` with nothing
                 remembered lands here (#11763) rather than mounting a stranger's
                 thread.
                 This is where the answer to "where did they go" has to live. Same
                 tone as the thread-side notice. */
              <div className="px-4 py-1.5 text-[13px] text-warn" role="status" data-testid="member-gone-roster-notice">
                {t('pages.membersPage.member_gone_roster', { name: gone.name })}
              </div>
            )}
              </>
            )}>
            <ul
              className={`${LIST_BODY_CLS} list-none m-0`}
              style={{ scrollbarWidth: 'none' }}
              aria-label={t('pages.membersPage.title')}
              {...uiLocation('members.roster-list')}
            >
              {loaded && !loadError && hasNoCrewmates(members) && (
                // Below md only: above md the thread column carries the hero
                // instead (see the DM thread section), so the two panes never
                // show the same hero twice on a wide viewport.
                <li className="md:hidden">
                  <CrewmateEmptyHero onCreate={openQuick} onAdvanced={openAdvanced} held={createHeld} onImport={() => setImporting(true)} />
                </li>
              )}
              {loadError && (
                /* The shared notice, not a bare alert: a read failure on a list
                   that holds no draft, so the agent hand-off is safe here. Below
                   md only: at md and up the chat column carries this same notice
                   (`member-column-load-error`), and the two side by side read as
                   one message doubled. */
                <li className="px-2 py-4 md:hidden flex flex-col items-start gap-2">
                  <ErrorNotice
                    message={t('pages.membersPage.roster_load_failed')}
                    variant="inline"
                    askAgent
                    // Same label as the chat-column notice for the same failure:
                    // two names for one helper on one screen reads as two helpers.
                    askAgentLabel={t('pages.membersPage.roster_load_failed_ask')}
                    // The roster column is narrow: let the link drop to its own
                    // line rather than squeeze the sentence to a word per line.
                    className="flex-wrap"
                    testId="member-roster-error"
                  />
                  {/* The plain retry first: a failed read is usually transient,
                      and "ask about it" alone reads as the only way out. */}
                  <Btn onClick={() => void rosterQuery.refetch()} data-testid="member-roster-retry">
                    {t('pages.membersPage.roster_load_retry')}
                  </Btn>
                </li>
              )}
              {allHidden && (
                <li className="px-4 py-6 text-xs text-muted" data-testid="member-all-hidden">
                  <p>{t('pages.membersPage.all_hidden_hint')}</p>
                </li>
              )}
              {filteredOut && (
                <li className="px-4 py-6 text-xs text-muted" data-testid="member-filtered-out">
                  <p>{t('pages.membersPage.filters_hide_all')}</p>
                  <button
                    type="button"
                    onClick={clearFilters}
                    className="mt-2 inline-flex items-center gap-1 text-[11.5px] px-2 py-1 rounded border border-border hover:bg-accent/40"
                    data-testid="member-filters-clear"
                  >
                    {t('pages.membersPage.filters_clear')}
                  </button>
                </li>
              )}
              {/* Grouped by team once any team exists: a header row per team, its
                  crewmates indented under it, the rows no team lists in a trailing
                  muted "No team" group. With no teams the list is flat, as it was
                  before teams -- one anonymous group and no header. A collapsed
                  group hides its rows; the fold persists per team. */}
              {rowsReady && rosterGroups.map((group) => {
                const collapsed = grouped && collapsedTeams.has(group.id)
                return (
                  <Fragment key={group.id}>
                    {grouped && (
                      <TeamGroupHeader
                        group={group}
                        selected={!!activeTeam && group.id === activeTeam.id}
                        collapsed={collapsed}
                        onOpen={() => {
                          if (!group.team) return
                          const id = group.team.id
                          if (rosterDrawerAvailable) closeRosterDrawer(() => openTeam(id))
                          else openTeam(id)
                        }}
                        onToggle={() => toggleTeamCollapsed(group.id)}
                      />
                    )}
                    {!collapsed &&
                      group.members.map((m) => (
                        <MemberRow
                          key={m.name}
                          m={m}
                          t={t}
                          activeName={activeName}
                          // In the drawer a pick closes it first, so the switch
                          // lands on the entry below the drawer's duplicate.
                          openMember={rosterDrawerAvailable ? pickFromRosterDrawer : openMember}
                          toggleStar={toggleStar}
                          starPending={starPending}
                          slotKeyOf={slotKeyOf}
                          isRunning={isRunning}
                          isUnread={isUnread}
                          isNeedsYou={isNeedsYou}
                          automationOf={automationOf}
                          // No layout projection under the drawer's compositor-
                          // driven slide (registerDrawerTargets' precondition).
                          reduceMotion={rosterDrawerAvailable ? true : reduceMotion}
                          scrollActiveRowIntoView={scrollActiveRowIntoView}
                          slugCollides={collidingSlugs.has(m.slug)}
                          indented={grouped}
                          faceOnStage={faceOnStageFor === m.name}
                        />
                      ))}
                  </Fragment>
                )
              })}
              {/* The crewmates the listing rule hides, counted at the list's end
                  (#21852): the count above covers the listed rows only, so
                  without this a crew of three read as "2 crewmates". */}
              {rowsReady && hiddenCount > 0 && (
                <li className="px-4 py-3 text-xs text-muted" data-testid="member-hidden-hint">
                  {hiddenCount <= hiddenNames.length
                    ? t('pages.membersPage.hidden_named_hint', {
                        count: hiddenNames.length,
                        names: fmtList(hiddenNames),
                      })
                    : t('pages.membersPage.hidden_more_hint', {
                        count: hiddenCount - hiddenNames.length,
                        names: fmtList(hiddenNames, { type: 'unit', style: 'short' }),
                      })}
                </li>
              )}
            </ul>
            </ListDock>
            {/* Window-splitter between roster and thread: the same component as the
                Sessions sidebar's grip, sitting on the card's right border the same
                way (absolute, 12px rounded-xl corner inset), so the two pages' edges
                read as one control. md+ only — below md the page is single-pane and
                there is nothing to resize. */}
            <div className={rosterShown ? 'hidden md:block' : 'hidden'} data-testid="member-roster-resize">
              <ResizeHandle
                handleProps={roster.handleProps}
                label={t('pages.membersPage.resize_roster')}
                onNudge={roster.nudge}
                value={roster.width}
                min={ROSTER_MIN}
                max={ROSTER_MAX}
                inset={12}
                // z-40: above the floating search dock (ListDock, z-30), whose
                // opaque shelf would otherwise take the inner half of the grip.
                className="absolute top-0 -right-[3px] h-full z-40"
              />
            </div>
          </aside>
  )
}
