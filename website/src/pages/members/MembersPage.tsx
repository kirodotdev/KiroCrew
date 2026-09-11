/**
 * Crew Members — one durable, pinned DM thread per crew member.
 *
 * CAPTURED: Settings > Developer > Feature Previews shows a screenshot of this
 * page in its "See what it looks like" dialog. A visible change here makes that
 * picture stale — re-shoot with `scripts/capture-feature-previews.mjs`.
 *
 * The page realizes the B+C merged design: a member list on the left, the
 * selected member's pinned DM thread in the center (the real chat stack,
 * hosted the way split-view panes host it), and on the right the SAME tabbed
 * side panel the chat page docks — permanent, no close control — whose first
 * tab is the member's Crew summary (read-only observation) and whose + menu
 * offers the chat panel's own views (Files, Artifacts, Terminal, Browser…)
 * against the member's DM slot, because a member thread IS a chat slot.
 * Configuration WRITES are deliberately absent — both Edit affordances
 * navigate to the existing crew manager (/capabilities?tab=crews), so this
 * page never becomes a second editor.
 *
 * Identity is the exact CREW NAME, never the slug: slugification is lossy
 * (`Oncall` and `oncall` share a slug and therefore one thread directory),
 * so rows are keyed and selected by name, and a thread-open response whose
 * `member` is a DIFFERENT name is surfaced as a collision instead of being
 * silently mounted (first-bound-wins is the backend contract).
 *
 * The pin is a server-side property of member slots (born only through
 * POST /api/members/{slug}/thread). It is an invariant of every member
 * thread, so the UI does not announce it — there is no unpinned state to
 * contrast against.
 *
 * Which member is open rides the URL (`?member=<name>`), and the last one
 * opened is remembered per browser: a visit that names no member lands on
 * the remembered one (else the first row), never on the empty column.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useLocation, useNavigate, useSearchParams } from 'react-router-dom'
import { ArrowLeft, Check, ChevronRight, Circle, Clock, ExternalLink, Goal, MessageCircleQuestionMark, Pencil, Route, Star, UserPlus, Users, Webhook, Zap } from 'lucide-react'
import { PanelRightSolid } from '../../components/icons/panels'
import { useTranslation } from 'react-i18next'
import { api, type MemberRosterRow, type WebhookTokenEntry } from '../../api/client'
import {
  MEMBERS_ROSTER_QUERY_KEY,
  memberActivityQueryKey,
  memberThreadQueryKey,
  membersRosterQuery,
  type MemberThreadOutcome,
} from '../../api/membersQuery'
import { defaultAgentQuery } from '../../api/defaultAgentQuery'
import { cronJobsQuery } from '../../api/cronJobsQuery'
import { crewWebhooksQueryKey, wakesCrew, webhookBoundToCrew } from '../../components/crew/wakesCrew'
import {
  AUTONUDGE_LOOPS_QUERY_KEY,
  type AutoNudgeLoop,
  intervalText,
  nextCycle,
} from '../../components/autoNudgeLoop'
import { skipToken, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { timeAgo } from '../../utils/timeAgo'
import { fmtDateTimeNumeric, fmtList, fmtTime } from '../../i18n/format'
import { usePersistedBool } from '../../hooks/usePersistedBool'
import { usePersistedString } from '../../hooks/usePersistedString'
import { findReport, type ErrorReport } from '../../utils/errorReport'
import { useAppDispatch, useAppSelector } from '../../store'
import { markSlotRead } from '../../store/dashboardSlice'
import CrewAvatar from '../../components/CrewAvatar'
import CrewStateAvatar from '../../components/CrewStateAvatar'
import ChatPane from '../../components/ChatPane'
import ErrorBoundary from '../../components/ErrorBoundary'
import ErrorNotice from '../../components/ErrorNotice'
import { useGuardedLeave } from '../../components/NavigationLeaveGuard'
import { useIsMobile } from '../../hooks/useIsMobile'
import { useConnected } from '../../hooks/useConnected'
import { SearchFilterBar, FilterMenuButton, FilterChip, FILTER_CHIP_ROW_CLS, FILTER_MENU_LABEL_CLS, FILTER_MENU_CONTENT_CLS } from '../../components/SearchFilterBar'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuLabel, DropdownMenuSeparator, DropdownMenuTrigger } from '../../components/ui/dropdown-menu'
import {
  countByFilter, narrowRoster, parseSort, parseSourceFilter, parseStatusFilters, queryNarrows, sortRoster,
  SORT_OPTIONS, SOURCE_FILTERS, STATUS_FILTERS,
  type MemberSignals, type MemberSort, type MemberSourceFilter, type MemberStatusFilter, type RosterQuery,
} from './rosterFilter'
import { Btn } from '../../components/ui'
import { AnimatePresence, motion, useReducedMotion } from 'framer-motion'
import { isSidePanelHidden, shouldMountSidePanel, sidePanelDockMotion } from '../chat/sidePanelMount'
import SidePanel, { SIDE_PANEL_MIN_W, SIDE_PANEL_RESERVED_W, type SidePanelLeadingTab, type SidePanelWithholdable } from '../chat/SidePanel'
import { CHAT_TRANSCRIPT_VIEWS, VIEW_DATA_SOURCE, useAnyLiveAppTab, usePanelTabs, type ViewKind } from '../../hooks/usePanelTabs'
import { usePanelTabDescriptors } from '../../hooks/panelTabRegistry'
import { usePanelDocumentActions } from '../../hooks/usePanelDocumentActions'
import ResizeHandle from '../../components/ResizeHandle'
import { cn } from '../../lib/utils'
import { LIST_SHELL_CLS, LIST_HEADER_CLS, LIST_TITLE_CLS, LIST_BODY_CLS, ROW_BOX_CLS, ROW_IDLE_CLS, ROW_ACTIVE_CLS, ROW_TITLE_CLS, ROW_STATUS_CLS } from '../../components/listShell'
import { useColumnResize } from '../../hooks/useColumnResize'
import { loadColumnWidth } from '../../lib/columnWidth'
import { tabStatus, type TabStatus } from '../../lib/sessionTabs'
import { lastActivityEpoch } from '../chat/sessionOrder'
import { activityDayLabel, floorCountText, groupActivityDays, projectLabel } from './activityDays'
import { safeGetItem, safeSetItem } from '../../utils/safeStorage'

/** The crew manager surface — the ONLY write path for member configuration.
 *  The explicit tab wins over CapabilitiesPage's remembered last tab. */
const CREW_MANAGER_PATH = '/capabilities?tab=crews'

/** Adding a member IS creating a crew, so "add" is a navigation into the crew
 *  manager — but straight into its create form (`?new=1`), not onto the list
 *  the form sits behind: the user pressed "+", and a second "New crew" click
 *  was the whole complaint (#9513). `from=members` tells the manager where the
 *  user came from, so a successful create lands on the new member's thread
 *  here instead of back on the crew list. Spelled out in full (not built from
 *  CREW_MANAGER_PATH) so the i18n lint reads it as the route it is. */
const CREW_CREATE_PATH = '/capabilities?tab=crews&new=1&from=members'

/** One member's editor, reached THROUGH the crew manager: the deep link opens
 *  that crew's full editor — name, template, model, workspace, triggers, and
 *  the avatar row that leads on to the builder (see KiroCrewAgentsPage's
 *  `?crew=` latch). This page stays read-only — the face is clickable here,
 *  but every write still happens in the one editor. It deliberately does NOT
 *  add `&avatar=1`: from a chat surface the user asked for "edit this member",
 *  and landing straight in the builder answered a narrower question. */
const crewEditPath = (name: string) =>
  `${CREW_MANAGER_PATH}&crew=${encodeURIComponent(name)}`
/** The open member rides the URL (`?member=<name>`) so a reload keeps it
 *  and a link lands on one. Switching members REPLACES the entry — the page
 *  holds one history entry, so Back leaves it in one press (the Sessions
 *  sidebar's rule); only the below-md roster->thread step pushes. The value
 *  is the exact crew NAME, not the slug: the slug is lossy (see the header
 *  comment), and a link that resolved `Oncall` to `oncall`'s thread would be
 *  the silent misroute this page exists to prevent. */
const MEMBER_PARAM = 'member'
/** The last member opened, so returning to the page (or reloading) lands on
 *  the conversation the user left rather than the empty column. Stored by
 *  exact name for the same reason as the URL param. One key per origin is
 *  the right scope: the roster is the gateway's global crew list, and
 *  localStorage is already per-gateway. */
const LAST_MEMBER_KEY = 'mc-members-last-member'

/** Which member to open when the URL names none, or names one that is gone
 *  (deleted or renamed since the link/memory was written): the remembered
 *  member if it is still on the roster, else the first row in display order.
 *  `undefined` only for an empty roster. Pure, so the three cases — default,
 *  restore, stale fallback — are tested directly. */
export function resolveDefaultMember(
  remembered: string | null,
  ordered: readonly MemberRosterRow[],
): MemberRosterRow | undefined {
  if (remembered) {
    const hit = ordered.find((m) => m.name === remembered)
    if (hit) return hit
  }
  return ordered[0]
}

type MemberMemoryDisplay = 'global' | 'legacy' | 'private' | 'ownership_mismatch' | 'unavailable'

export function memberMemoryDisplay(row: MemberRosterRow): MemberMemoryDisplay {
  if (row.name === 'default') return row.memory_store === 'default' ? 'global' : 'unavailable'
  if (row.memory_owner && row.memory_owner !== row.name) return 'ownership_mismatch'
  if (row.memory_version === 2) return row.memory_owner === row.name ? 'private' : 'unavailable'
  if (row.memory_version === 1 && !row.memory_owner) return 'legacy'
  return 'unavailable'
}

/** Roster width bounds, persisted like the chat sidebar's (mc-sidebar-width). */
const ROSTER_MIN = 200
const ROSTER_MAX = 420
const ROSTER_DEFAULT = 264
const ROSTER_WIDTH_KEY = 'mc-members-roster-width'
/** The permanent first tab of the member's side panel. Its id is what
 *  `usePanelTabs` stores as the strip's focus while it is selected, so it must
 *  not collide with a chat `TabKind` — `'summary'` is the chat page's
 *  session-summary view, a different thing (that one summarises a transcript;
 *  this one describes a member). */
export const CREW_SUMMARY_TAB_ID = 'crew-summary'
/** Chat-panel views this page withholds from the strip and the + menu
 *  (`SidePanel.hiddenViews`). The unfed half is DERIVED, not enumerated: every
 *  view `VIEW_DATA_SOURCE` classifies as `chat-transcript` (Changes / Issues /
 *  Links / Pins today) reads indexes ChatPage builds over the transcript, none
 *  of which runs here, so each would render an affirmative "none" — and a new
 *  transcript-fed view must be classified where kinds are defined before it can
 *  exist, so it cannot arrive here unwithheld. `summary` is the one addition
 *  by choice: the chat page's SESSION summary has data, but next to the "Crew
 *  summary" chip it is an indistinguishable sibling label. Exported so the
 *  test pins the set. */
export const MEMBERS_UNFED_VIEWS: readonly ViewKind[] = [...CHAT_TRANSCRIPT_VIEWS, 'summary']
/** Everything this page withholds once the thread is confirmed. Today that is
 *  exactly the unfed set: Side chat IS offered — its composer draft lives in
 *  the chat-core store (`sideChatDrafts`, per slot, persisted), so `SidePanel`
 *  unmounting the body on a tab or member switch loses nothing, and the
 *  selection toolbar's "Ask about this" needs the tab as its landing
 *  (`openMemberSideChat`). Kept as its own name so the "withheld" and "unfed"
 *  reasons stay separable if they diverge again. Exported so the test pins
 *  the set. */
export const MEMBERS_WITHHELD_VIEWS: readonly SidePanelWithholdable[] = [...MEMBERS_UNFED_VIEWS]
/** Everything the panel withholds while the thread is UNCONFIRMED: every
 *  classified view, plus Terminal and app tabs. Derived from
 *  `VIEW_DATA_SOURCE` (the exhaustive `Record<ViewKind, …>`) rather than
 *  enumerated, so the same guarantee the unfed set has holds here too — a new
 *  `ViewKind` cannot arrive in this window offered; it is withheld by
 *  construction until the slot it would bind to exists. Exported so the test
 *  pins the set against the classification. */
export const MEMBERS_UNCONFIRMED_WITHHELD_VIEWS: readonly SidePanelWithholdable[] = [
  ...(Object.keys(VIEW_DATA_SOURCE) as ViewKind[]), 'terminal', 'app',
]
/** This page's three inter-column gap-2s (24px) — space the side panel must
 *  keep clear beside the roster so a drag can never fold the thread to zero.
 *  The thread's own minimum is already inside the panel's shell reserve
 *  (`SIDE_PANEL_RESERVED_W` budgets the nav rail plus a chat-pane minimum). */
const PANEL_GAPS_W = 24
/** Whether the side panel can sit BESIDE the thread as a permanent column,
 *  or must become an overlay the user opens. Beside needs the shell reserve
 *  (nav rail + a usable thread) plus the live roster width plus the panel's
 *  own minimum — the same arithmetic the chat page's `sidePanelFillWidth` does
 *  for its two columns, with the roster added. Pure, so the boundary is
 *  tested directly. Mobile always overlays (its viewport seats neither). */
export function panelSitsBeside({ winW, rosterW, isMobile }: { winW: number; rosterW: number; isMobile: boolean }): boolean {
  if (isMobile) return false
  return winW - rosterW - PANEL_GAPS_W >= SIDE_PANEL_RESERVED_W + SIDE_PANEL_MIN_W
}
/** Punctuation, not prose: joins an activity label to its project name, and a
 *  driving row's title to its status word in the hover title. */
const PROJECT_SEPARATOR = ' \u00b7 '
/** Driving-sessions rows shown before the list folds behind "Show all". */
const DRIVING_VISIBLE = 5
/** Activity days shown before the list folds behind "Show N more days". */
const ACTIVITY_DAYS_VISIBLE = 3
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
  unread: (active) => <Circle size={12} className={active ? 'text-accent' : 'text-muted'} {...(active ? { strokeWidth: 0, fill: 'var(--accent)' } : {})} />,
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
/** How each shared tab status renders on a driving row. The ORDER lives in
 *  `tabStatus` (lib/sessionTabs.ts) — this only maps its verdict to a dot
 *  class, an i18n label, and whether the label is spoken aloud in the row.
 *  `unread` cannot occur here (no unread set is passed) and reads as idle. */
const DRIVING_STATUS: Record<TabStatus, { cls: string; text: string; label: string; spoken: boolean }> = {
  permission: { cls: 'fill-warn text-warn', text: 'text-warn', label: 'pages.chatSidebar.needs_approval', spoken: true },
  question: { cls: 'fill-info text-info', text: 'text-info', label: 'pages.chatSidebar.needs_your_answer', spoken: true },
  running: { cls: 'fill-ok text-ok', text: 'text-ok', label: 'pages.membersPage.drawer_working', spoken: false },
  unread: { cls: 'fill-muted text-muted', text: 'text-muted', label: 'pages.membersPage.driving_idle', spoken: false },
  idle: { cls: 'fill-muted text-muted', text: 'text-muted', label: 'pages.membersPage.driving_idle', spoken: false },
}
// Module-level so the resize hook's memoised resolver isn't invalidated every render.
const loadRosterWidth = () => loadColumnWidth(ROSTER_WIDTH_KEY, ROSTER_MIN, ROSTER_MAX, ROSTER_DEFAULT)
/** The chat side panel's right-dock mount preset — module-pure, so one
 *  constant serves every render. */
const dockMotion = sidePanelDockMotion('right')
/** The auto-nudge service's terminal codes (`NudgeLoop.stopped_reason`) a
 *  member slot can actually receive, each mapped to the sentence the patrol
 *  block shows for a stopped loop. A code not listed here — a future terminal
 *  condition, or `autonudge_stop`, which today only research loops are
 *  stamped with — falls back to the code itself rather than to a sentence
 *  nothing produces. */
const PATROL_STOPPED_REASON: Record<string, string> = {
  manual: 'pages.membersPage.patrol_stopped_manual',
  cycle_cap: 'pages.membersPage.patrol_stopped_cycle_cap',
  runtime_budget: 'pages.membersPage.patrol_stopped_runtime_budget',
  approval_stalled: 'pages.membersPage.patrol_stopped_approval_stalled',
}
/** Floor under the websocket-driven invalidation of the loop registry: frames
 *  fire only on change, so a frame lost to a dropped socket would otherwise
 *  leave a stale verdict on screen indefinitely. One minute bounds that. */
const PATROL_REFRESH_MS = 60_000
/** How often the "next wake in …" countdown in the drawer re-reads the clock.
 *  Coarser than the popover's per-second tick on purpose: the drawer line is
 *  an at-a-glance status, and a per-second re-render of the whole drawer for
 *  a readout that already drops seconds above a minute buys nothing. */
const PATROL_TICK_MS = 15_000
/** Stable empty roster for the not-yet-answered read, so the memos keyed on
 *  `members` do not recompute on every render while the first fetch is out. */
const EMPTY_ROSTER: readonly MemberRosterRow[] = []

export default function MembersPage() {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const leave = useGuardedLeave()
  const location = useLocation()
  const queryClient = useQueryClient()
  // The roster is a React Query read (issue #9418), not page state: a return
  // to the page renders the cached list at once and refreshes it in the
  // background, and a crew written anywhere else reaches it through the
  // `['kirocrew-agents']` prefix invalidation (see membersQuery.ts). Three
  // states, kept apart the way every block on this page keeps them: not yet
  // answered, answered, failed with no answer to fall back on — a refetch
  // error after a good read keeps showing the last roster.
  const rosterQuery = useQuery(membersRosterQuery)
  const members = rosterQuery.data ?? EMPTY_ROSTER
  const loaded = rosterQuery.data !== undefined || rosterQuery.isError
  const loadError = rosterQuery.data === undefined && rosterQuery.isError
  // Identity is the exact crew name (unique in the registry); the slug is not.
  const [activeName, setActiveName] = useState<string>('')
  // The member the LAST open asked for, written synchronously by `activate`.
  // The URL sync effect below guards on this, not on `activeName`: the roster
  // is a React Query read, so a store update (the thread endpoint confirming a
  // key patches the row) can re-render this component on React's sync lane
  // BEFORE the default-lane `setActiveName` from the open has committed —
  // and an effect re-run in that window would see the old name, open the same
  // member twice and, for a stand-in open, overwrite the remembered member.
  const activeNameRef = useRef('')
  // Callback ref on the OPEN row only: React calls it as a row becomes the
  // open one (the prop flips from undefined to this), so no effect has to
  // re-find the element. `nearest` scrolls only when the row is actually
  // out of view — a click on a visible row must not shift the list. Guarded:
  // happy-dom has no scrollIntoView.
  const scrollActiveRowIntoView = useCallback((el: HTMLButtonElement | null) => {
    el?.scrollIntoView?.({ block: 'nearest' })
  }, [])
  // The URL is the one source of WHICH member is open; activeName follows it
  // (sync effect below). Clicks write the URL, never activeName directly, so
  // the phone's back gesture, a reload and a shallow link go through the same
  // path as a click.
  const [searchParams, setSearchParams] = useSearchParams()
  const urlMember = searchParams.get(MEMBER_PARAM) ?? ''
  // Set when a URL NAMED a member that is gone: the user asked for someone
  // specific, so the outcome is said out loud — above the fallback thread on
  // md+ (`shown` = who opened instead), above the roster below md (`shown` is
  // '' — no thread opened). The remembered-member fallback never sets it —
  // there the user named nobody. Cleared once a different member opens.
  const [gone, setGone] = useState<{ name: string; shown: string } | null>(null)
  // The member the fallback is about to open in place of a gone one a link
  // named. Set right before the fallback's URL write, read (and cleared) by
  // the open that write triggers, so that open can skip the memory write. A
  // ref, not state: it is a note between two runs of one effect, and must
  // not re-arm it.
  const goneStandInRef = useRef('')
  // The open member's thread, as the thread endpoint last answered it. The
  // roster's `bound`/`slot_key` are never trusted as mountable: dm.json
  // outlives the live slot (a restart drops an unmessaged slot while the
  // binding survives), and mounting an unconfirmed key would let the first
  // message auto-create an ordinary UNPINNED slot on the member key.
  // POST /api/members/{slug}/thread is idempotent and is the only creator/
  // repairer of member slots — so every open goes through it (the mutation
  // below), and its answer is cached per member NAME (memberThreadQueryKey)
  // so a return to a member mounts the cached thread at once while the
  // re-POST repairs in the background. Keying by the member the answer was
  // requested FOR makes a late completion of a previously selected member
  // harmless. `skipToken`: this entry is written by the mutation, never
  // fetched — the read only subscribes to it.
  const threadQuery = useQuery<MemberThreadOutcome>({
    queryKey: memberThreadQueryKey(activeName),
    queryFn: skipToken,
  })
  const threadOutcome = threadQuery.data
  // The slot a row's live readings (presence, unread, patrol) resolve to: the
  // thread endpoint's confirmed key for the open member, the roster binding
  // for everyone else. The roster row is patched with the confirmed key the
  // moment an open confirms one (see openThread), so a member opened earlier
  // in this visit keeps resolving after the selection moves on.
  const slotKeyOf = useCallback(
    (m: MemberRosterRow) =>
      (m.name === activeName ? threadOutcome?.slot_key : '') || m.slot_key,
    [activeName, threadOutcome],
  )
  // Roster width is user-adjustable on md+ (drag handle on the right edge),
  // mirroring the chat sidebar. Below md the roster is full-width single-pane
  // and the stored width is simply unused. Clamp + persist live in the shared
  // useColumnResize hook — the same primitive every resizable column uses.
  const roster = useColumnResize(ROSTER_WIDTH_KEY, loadRosterWidth, ROSTER_MIN, ROSTER_MAX)
  // Where the side panel lives. Wide enough (see panelSitsBeside) it is a
  // permanent column beside the thread with no close control — the chat page's
  // panel, docked. Narrower, it is an overlay the header button opens and the
  // panel's own close control dismisses, because a column that cannot be
  // dismissed would otherwise fold the thread to nothing. The window width is
  // tracked live (not sampled at mount) so crossing the boundary re-docks.
  const isMobile = useIsMobile()
  const [winW, setWinW] = useState(() => (typeof window !== 'undefined' ? window.innerWidth : 0))
  useEffect(() => {
    const onResize = () => setWinW(window.innerWidth)
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [])
  const beside = panelSitsBeside({ winW, rosterW: roster.width, isMobile })
  // On a phone the overlay must FILL its scrim. SidePanel's own mobile
  // fallback is `width: 100%`, which cannot resolve here: the overlay's inner
  // wrapper is a shrink-to-fit flex item, so a percentage child falls back to
  // the panel's max-content width and the panel lands at SIDE_PANEL_MIN_W
  // with a dimmed sliver of the thread showing beside it (issue #9979). The
  // chat page hands its panel an explicit px width for exactly this reason
  // (sidePanelFillWidth's mobile branch); this is that branch. `undefined` off
  // the phone, where the panel keeps its own resizable width in both
  // placements — the docked/overlay split is panelSitsBeside's, not this.
  const panelFillWidth = isMobile ? Math.max(SIDE_PANEL_MIN_W, winW) : undefined
  const [overlayOpen, setOverlayOpen] = useState(false)
  // `overlayOpen` is overlay-mode state only. Reset it whenever the panel docks
  // (a widening window, a narrower roster), so an open overlay does not lie in
  // wait and pop back over the thread the moment the window narrows again.
  useEffect(() => { if (beside) setOverlayOpen(false) }, [beside])
  const panelVisible = beside || overlayOpen
  // Live presence rides the already-subscribed WS `slots` frames — the roster
  // endpoint only fills the cold-start gap (its `running` is a snapshot).
  const liveSlots = useAppSelector((s) => s.dashboard.slots)
  // Whether a real slots snapshot has arrived. Before it, an empty `slots` is
  // ambiguous (the store itself refuses to treat a pre-first-frame empty frame
  // as authoritative), so the driving-sessions block must not assert "not
  // driving" on a cold open or a WS reconnect — it shows a skeleton instead,
  // the same three-state discipline the Recent-activity section keeps.
  const slotsLoaded = useAppSelector((s) => s.dashboard.slotsLoaded)
  const liveRunning = useMemo(() => {
    const byKey: Record<string, boolean> = {}
    for (const s of liveSlots) if (s.mode === 'member') byKey[s.key] = !!s.running
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

  const active = useMemo(
    () => members.find((m) => m.name === activeName),
    [members, activeName],
  )
  const activeMemory = active ? memberMemoryDisplay(active) : 'unavailable'
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
  // Display order before the search filter — this is what "the first member"
  // means for the default-open below, so a typed filter never changes which
  // member a fresh visit lands on. The ORDER is committed per MEMBERSHIP and
  // per chosen SORT, not per refetch: the roster query refetches on every
  // server refresh frame, on window focus and on staleness, and re-sorting
  // when a last_active_ts advances would move rows under the cursor mid-click
  // — opening a different member's durable pinned thread. Row CONTENT (star,
  // last-message preview, presence) still updates live from every refetch;
  // only the ordering is held until a member is added, removed or renamed, or
  // the user picks the other sort, which re-sorts from scratch.
  const committedOrderRef = useRef<{ sort: MemberSort; names: string[] }>({ sort, names: [] })
  const orderedMembers = useMemo(() => {
    const byName = new Map(members.map((m) => [m.name, m]))
    const prev = committedOrderRef.current
    const sameMembership =
      prev.sort === sort && prev.names.length === byName.size && prev.names.every((n) => byName.has(n))
    const names = sameMembership ? prev.names : sortRoster(members, sort).map((m) => m.name)
    committedOrderRef.current = { sort, names }
    return names.map((n) => byName.get(n)).filter((m): m is MemberRosterRow => !!m)
  }, [members, sort])
  // Named apart from `rosterQuery` above: that one is the React Query READ of
  // the roster, this one is the user's filter/sort question asked of it.
  const rosterFilterQuery = useMemo<RosterQuery>(
    () => ({ search: filter, starredOnly, source: sourceFilter, status: statusFilter, sort }),
    [filter, starredOnly, sourceFilter, statusFilter, sort],
  )
  const activeSlot = active ? threadOutcome?.slot_key ?? '' : ''
  // Two distinct verdicts with two different sentences: a collision is a
  // fact about the roster (the slug's thread belongs to another crew), a
  // failed POST is a transport error. Both render through ErrorNotice so
  // the structured report and the agent hand-off survive.
  const activeCollision = active ? threadOutcome?.collision ?? '' : ''
  const activeThreadFailed = !!active && !!threadOutcome?.failed
  // The thread open. A mutation, not a query: the endpoint is a write (the
  // idempotent creator/repairer of member slots), so it is issued on EVERY
  // open — never served from cache — and its answer is what the cache holds.
  // Outcomes are keyed by the member the POST was FOR, so a late answer for a
  // member the user has already left lands in that member's entry, not the
  // open one's. The roster row is patched with a freshly confirmed key so the
  // row's live readings (presence dot, unread, patrol badge) resolve to the
  // same slot the thread mounted on, without a roster refetch that would
  // re-sort the list under the cursor.
  const setThreadOutcome = useCallback(
    (name: string, update: (prev: MemberThreadOutcome | undefined) => MemberThreadOutcome) =>
      queryClient.setQueryData<MemberThreadOutcome>(memberThreadQueryKey(name), update),
    [queryClient],
  )
  // Per-member sequence of thread POSTs. Within ONE member only the LATEST
  // request may write its verdict back: an older answer arriving after a newer
  // one (a re-click on a slow link) is dropped whole, because letting a stale
  // success overwrite the refusal the newest POST just recorded would re-bind
  // the side panel to a key the endpoint has since refused — aiming Side chat
  // / Artifacts / Terminal at a foreign session. Dropping it loses nothing:
  // every open re-POSTs. Across members the rule above stands untouched (a
  // late answer for another member is that member's newest, so it lands).
  const threadReqSeq = useRef<Record<string, number>>({})
  const openThread = useMutation({
    mutationFn: (m: MemberRosterRow) => api.memberThread(m.slug),
    onMutate: (m) => {
      // A previous verdict for this member is retired while the re-POST is
      // out: a confirmed key keeps rendering (the cached thread stays up), a
      // collision or failure line comes down until the new answer is in.
      setThreadOutcome(m.name, (prev) => ({ slot_key: prev?.slot_key ?? '' }))
      // The sequence number rides the mutation context to onSuccess/onError.
      const seq = (threadReqSeq.current[m.name] ?? 0) + 1
      threadReqSeq.current[m.name] = seq
      return seq
    },
    onSuccess: (r, m, seq) => {
      if (seq !== threadReqSeq.current[m.name]) return
      if (r.member !== m.name) {
        // The slug's thread belongs to another crew (lossy-slug collision,
        // first-bound-wins). Mounting it would be a silent misroute — the
        // defining failure for a page whose premise is identity.
        setThreadOutcome(m.name, () => ({ slot_key: '', collision: r.member }))
        return
      }
      setThreadOutcome(m.name, () => ({ slot_key: r.slot_key }))
      if (m.slot_key !== r.slot_key) {
        queryClient.setQueryData<MemberRosterRow[]>(MEMBERS_ROSTER_QUERY_KEY, (rows) =>
          rows?.map((row) => (row.name === m.name ? { ...row, slot_key: r.slot_key, bound: true } : row)),
        )
      }
    },
    onError: (error, m, seq) => {
      if (seq !== threadReqSeq.current[m.name]) return
      setThreadOutcome(m.name, (prev) => ({
        slot_key: prev?.slot_key ?? '',
        failed: true,
        errorReport: findReport(error instanceof Error ? error.message : undefined),
      }))
    },
  })
  const { mutate: postThread } = openThread
  // The cached key is trusted only for as long as the gateway is known not to
  // have restarted. A dropped-then-restored socket is the one client-visible
  // sign that it may have (a restart drops an unmessaged member slot while
  // its binding survives), so on a RECONNECT the open member's thread is
  // re-confirmed by the same idempotent POST every open makes — the mounted
  // pane stays up meanwhile, exactly as during any other repair. The
  // websocket hook forgets the entries nobody is looking at at the same
  // moment. Only a true->false->true sequence seen by THIS mounted page
  // counts: the first connect after a reload is not a reconnect, and the
  // roster-driven open already confirms the thread then.
  const connected = useConnected()
  const hadConnectionRef = useRef(false)
  const activeRowRef = useRef<MemberRosterRow | undefined>(undefined)
  activeRowRef.current = active
  useEffect(() => {
    if (!connected) return
    if (hadConnectionRef.current && activeRowRef.current) postThread(activeRowRef.current)
    hadConnectionRef.current = true
  }, [connected, postThread])

  // The member whose thread POST is IN FLIGHT — the mutation's own pending
  // reading, which follows the LATEST call: a fast re-click (two POSTs out)
  // stays pending until the second answers, so the first one's completion
  // cannot re-bind the panel while the second is still unanswered. While it
  // is in flight the cached key is only a render hint for the thread column:
  // the side panel must not bind to it, because the POST may come back
  // refusing that very key (renamed / deleted member, a stale binding another
  // session now occupies) and a panel action dispatched in the window — an
  // artifact involvement write, a Side chat turn — cannot be recalled by the
  // unbind that follows.
  const pendingThreadFor = openThread.isPending ? openThread.variables?.name ?? '' : ''
  // The slot the SIDE PANEL may bind: the cached key only once the CURRENT
  // open's POST has confirmed it. Empty for the whole in-flight window, so no
  // slot-bound view is offered and no document action can record against a
  // key the endpoint is about to refuse — and empty again after a REFUSAL: a
  // 409 means the canonical key is occupied by a session that is not this
  // member's (or the endpoint could not repair it), so a cached key kept
  // through it would leave every slot-bound panel view (Side chat, Artifacts,
  // Files…) aimed at a foreign session. The panel falls back to the slot-free
  // Crew summary; the thread column keeps rendering the cached key under its
  // own failure notice (its pre-existing contract, see activeThreadFailed).
  const confirmedSlot =
    active && (pendingThreadFor === active.name || activeThreadFailed) ? '' : activeSlot

  // Sessions this member is driving: every live slot whose `created_by` is the
  // member's DM slot key. A member dispatches its real work into worker
  // sessions it opens via session_create and steers via session_send, and the
  // backend fences a member caller to the slots it created — so "created by"
  // IS "driven by", and the durable birth attribution is the whole source of
  // truth (no transcript scraping for the `[sent by session …]` prefix). Rides
  // the already-subscribed WS `slots` frames, which is also what gives each row
  // its live status — the same running / needs-approval / needs-input signals
  // the sidebar dot reads. Newest activity first; a closed worker leaves the
  // live slots and therefore this list, which is the honest reading of
  // "driving right now".
  const activeMemberKey = activeSlot || active?.slot_key || ''
  // The ORDER is committed per DRIVEN SET, not per frame. `liveSlots` refreshes
  // on every WS slots frame and `lastActivityEpoch` advances whenever a worker
  // does anything, so sorting per render moves rows under the cursor mid-click —
  // and each row is a jump into a session, so a shifted row navigates into the
  // WRONG one. It also decides which rows sit behind the DRIVING_VISIBLE fold,
  // so a live re-sort can pull a row out from under the pointer entirely.
  // Row CONTENT still updates from every frame (status dot, title, timestamp);
  // only the positions hold, until the driven set changes (a worker opens or
  // closes) or the member does, either of which re-sorts from scratch by
  // recency. Same rule as the roster order above, and the same reason.
  const committedDrivingRef = useRef<{ member: string; keys: string[] }>({ member: '', keys: [] })
  // The side panel's tab strip, bucketed by the member's slot key exactly as
  // the chat page buckets by chat slot: switching members swaps the whole strip
  // and switching back restores it. Keyed on the POST-CONFIRMED `activeSlot`
  // ONLY — never the roster's derived key. That key is a stale binding until
  // the thread endpoint confirms it (see the `slots` comment above): an
  // ordinary slot can occupy the canonical key after a restart, the POST then
  // answers 409 and `activeSlot` stays empty, and a panel bound to the derived
  // key would aim Side chat / Terminal / Summary at that unrelated session.
  // While no confirmed slot exists the strip lives in the shared no-slot bucket
  // and every slot-bound view is withheld (`hiddenViews` below); the Crew
  // summary needs no slot and stays.
  const panelTabDescriptors = usePanelTabDescriptors()
  const tabsCtl = usePanelTabs(activeSlot || null, panelTabDescriptors, { leadingId: CREW_SUMMARY_TAB_ID })
  // The member slot's project directory (the WS slots frame carries it) roots
  // the Files tab and is the cwd a Terminal tab spawns in. Only a record from
  // the CURRENT snapshot counts: a reconnect drops `slotsLoaded` but keeps the
  // pre-disconnect `slots` until the fresh frame lands, and the thread POST
  // can confirm inside that window — binding to the stale record would root
  // Files / Terminal in whatever project the key had BEFORE the restart, and a
  // save or command dispatched then would land in the wrong workspace. The
  // record must also be the member's own (`mode === 'member'`): a 200 confirm
  // names the member slot, never an ordinary session that happens to hold
  // the canonical key (that case answers 409 and never confirms).
  const activeLiveSlot = useMemo(
    () => (confirmedSlot && slotsLoaded
      ? liveSlots.find((s) => s.key === confirmedSlot && s.mode === 'member')
      : undefined),
    [liveSlots, slotsLoaded, confirmedSlot],
  )
  const projectDir = activeLiveSlot?.project || undefined
  // Terminal needs the slot RECORD, not just the confirmed key: the thread
  // POST answers before the WS `slots` frame that carries the slot's project,
  // and a shell spawned in that window would take `cwd: undefined` — the
  // backend's HOME fallback — with no re-rooting once the frame lands. Gate on
  // the record being present, not on `project` being set: a member with no
  // project legitimately opens its shell in the fallback cwd, exactly as a
  // project-less chat does.
  const slotRecordPresent = !!activeLiveSlot
  // Views this host withdraws from the strip and the + menu. Always: the views
  // fed by ChatPage-owned transcript indexes (pull-request / issue / link
  // extraction, the pins query) — this page has none of those, and an empty
  // Changes chip on the monitoring page would assert "nothing changed" while a
  // member is editing. `summary` (the chat page's SESSION summary) is withheld
  // too: one click from the "Crew summary" chip, two sibling labels a reader
  // cannot tell apart. Until the thread is confirmed, EVERY slot-bound view is
  // withheld as well, per the binding rule above — and so is Terminal: while
  // unconfirmed the strip sits in the shared no-slot bucket, so a PTY opened
  // then would be orphaned (live shell, unreachable tab) the moment the
  // confirmation re-keys the strip to the member's slot; app-contributed tabs
  // (`'app'`) likewise, since the re-key would remount their `AppHost` and
  // discard the app's own unsaved state. Terminal stays withheld a moment
  // longer — until the WS slots frame carries the confirmed slot, so its cwd
  // is known (see slotRecordPresent).
  const hiddenViews = useMemo<ReadonlySet<SidePanelWithholdable>>(
    () =>
      new Set<SidePanelWithholdable>(
        !confirmedSlot
          ? MEMBERS_UNCONFIRMED_WITHHELD_VIEWS
          : !slotRecordPresent
            ? [...MEMBERS_WITHHELD_VIEWS, 'terminal']
            : MEMBERS_WITHHELD_VIEWS,
      ),
    [confirmedSlot, slotRecordPresent],
  )
  // The selection toolbar's "Ask about this" on the thread: the Side Chat for
  // this member lives in the panel's Side tab (the chat page's home for it),
  // so opening it means focusing that tab — and, in overlay mode, revealing
  // the panel, since a hidden tab is not "on screen". Only the endpoint-
  // confirmed slot may host it (the pane is keyed on that same slot, so the
  // two agree); `false` tells the selection seam the Ask did NOT happen, so
  // it never seeds a quote into a Side Chat that never opened.
  const openMemberSideChat = useCallback((slot: string): boolean => {
    if (!confirmedSlot || slot !== confirmedSlot) return false
    tabsCtl.openView('side')
    if (!beside) setOverlayOpen(true)
    return true
  }, [confirmedSlot, tabsCtl, beside])
  // Whether the Crew summary body is on screen — the gate for its data reads
  // and its countdown tick, so a member whose panel shows a terminal does not
  // pay for a summary nobody is looking at. Read from what the panel SHOWS
  // (`onActiveTabChange`), not from the stored focus: a stored focus on a
  // withheld view falls back to the summary in the strip without moving the
  // store, and the summary must load when it is the one on screen.
  const [shownTabId, setShownTabId] = useState<string | null>(null)
  const summaryVisible = panelVisible && (shownTabId ?? tabsCtl.activeId) === CREW_SUMMARY_TAB_ID
  const closeOverlay = useCallback(() => setOverlayOpen(false), [])
  // Mount continuity — the chat page's rule, verbatim: a live Browser tab (its
  // WebContentsView) or a body-owning app tab (any slot's) cannot survive a
  // remount, so while one exists a closed overlay is kept mounted and hidden
  // rather than unmounted. There is no find pane on this page.
  const hasLiveAppTab = useAnyLiveAppTab()
  const hasBrowserTab = tabsCtl.tabs.some((tab) => tab.kind === 'browser')
  const mountInput = { activityOpen: panelVisible, hasLiveAppTab, hasBrowserTab, searchOpen: false }
  const panelMounted = shouldMountSidePanel(mountInput)
  const panelHidden = isSidePanelHidden(mountInput)
  // File / artifact / save for the panel's Files, Artifacts and document tabs —
  // the chat page's own implementation, not a copy. A failed read is reported
  // above the thread; an open while the panel is an OVERLAY reveals it, since
  // the tab it focused is otherwise hidden behind a closed panel. Docked, the
  // open needs nothing — and must not arm `overlayOpen`, or a later narrowing
  // of the window would find the overlay already open over the thread.
  const activeSlotRef = useRef<string | null>(confirmedSlot || null)
  activeSlotRef.current = confirmedSlot || null
  const besideRef = useRef(beside)
  besideRef.current = beside
  const [actionError, setActionError] = useState('')
  // A failed document read is reported above the thread. In overlay mode the
  // open panel covers exactly that spot — the click that failed happened inside
  // it — so the overlay closes as the notice appears; otherwise the failure is
  // silent to the person who caused it.
  const showActionError = useCallback((message: string) => {
    setActionError(message)
    if (!besideRef.current) setOverlayOpen(false)
  }, [])
  const revealPanelAfterOpen = useCallback(() => { if (!besideRef.current) setOverlayOpen(true) }, [])
  const { openFile, openArtifact, saveFile } = usePanelDocumentActions({
    tabsCtl,
    slotRef: activeSlotRef,
    queryClient,
    showActionError,
    onOpened: revealPanelAfterOpen,
  })
  const drivingSessions = useMemo(() => {
    if (!activeMemberKey) return []
    const mine = liveSlots.filter((s) => !!s.created_by && s.created_by === activeMemberKey)
    const byKey = new Map(mine.map((s) => [s.key, s]))
    const prev = committedDrivingRef.current
    const sameSet =
      prev.member === activeMemberKey &&
      prev.keys.length === byKey.size &&
      prev.keys.every((k) => byKey.has(k))
    const keys = sameSet
      ? prev.keys
      : [...mine].sort((a, b) => lastActivityEpoch(b) - lastActivityEpoch(a)).map((s) => s.key)
    committedDrivingRef.current = { member: activeMemberKey, keys }
    return keys.map((k) => byKey.get(k)).filter((s): s is (typeof mine)[number] => !!s)
  }, [liveSlots, activeMemberKey])
  // Collapsed past DRIVING_VISIBLE rows. Keyed to the member: the fold is a
  // reading position in ONE member's list, so switching members starts the
  // next list folded rather than inheriting the previous member's expansion.
  const [drivingExpandedFor, setDrivingExpandedFor] = useState('')
  const drivingExpanded = drivingExpandedFor === activeMemberKey
  const visibleDriving = drivingExpanded ? drivingSessions : drivingSessions.slice(0, DRIVING_VISIBLE)

  // Recent-activity pointers for the Crew summary tab, read when it is on
  // screen for a member and cached per exact member NAME, not slug — slugs are
  // lossy, and the whole point of the backend's member filter is that two
  // names sharing a slug have distinct histories. Real recorded signal only —
  // the summary derives its counts from these instead of fabricating stats.
  // Three states per member: no answer yet = still loading, failed with no
  // answer = error, answered = loaded. A pending or failed read must not
  // render the affirmative "no activity"; a refetch error after a good read
  // keeps the last entries. The finite staleTime is the roster's: a return to
  // the summary shows the cached pointers and refreshes them behind.
  const activeSlug = active?.slug ?? ''
  const activeMemberName = active?.name ?? ''
  const activityQuery = useQuery({
    queryKey: memberActivityQueryKey(activeSlug, activeMemberName),
    queryFn: () => api.memberActivity(activeSlug, activeMemberName),
    enabled: !!activeSlug && !!activeMemberName && summaryVisible,
    staleTime: membersRosterQuery.staleTime,
  })
  const activityLoading = activityQuery.data === undefined && !activityQuery.isError
  const activityError = activityQuery.data === undefined && activityQuery.isError
  const activeEntries = useMemo(
    () => activityQuery.data?.entries ?? [],
    [activityQuery.data],
  )
  const activityCapped = !!activityQuery.data?.capped

  // Wake sources — global lists (crons, webhook tokens, the default crew),
  // shared with the crew editor and the Schedule page through the same query
  // keys (one fetch serves all of them; a mint / revoke / save anywhere
  // reaches this summary through their invalidations), read once the Crew
  // summary is on screen and filtered per member at render. `failed` is kept
  // distinct from empty: absence of an answer and an answer of "none" must not
  // render the same (a failed fetch would otherwise show the affirmative
  // "nothing wakes this member", a false statement). Every source must have
  // answered before the block asserts anything; one failing with nothing
  // cached is the error.
  const cronsQuery = useQuery({ ...cronJobsQuery, enabled: summaryVisible })
  const hooksQuery = useQuery<{ tokens?: WebhookTokenEntry[] }>({
    queryKey: crewWebhooksQueryKey,
    queryFn: () => api.webhooks(),
    enabled: summaryVisible,
  })
  const defaultAgentQ = useQuery({ ...defaultAgentQuery, enabled: summaryVisible })
  const wakeSources = [cronsQuery, hooksQuery, defaultAgentQ]
  const wakeFailed = wakeSources.some((q) => q.data === undefined && q.isError)
  const wakeLoaded = wakeFailed || wakeSources.every((q) => q.data !== undefined)
  const wakeJobsAll = cronsQuery.data
  const wakeTokens = hooksQuery.data?.tokens
  const wakeDefaultAgent = defaultAgentQ.data ?? ''
  const wakeJobs = useMemo(
    () =>
      active && wakeJobsAll
        ? wakeJobsAll.filter((j) => wakesCrew(j, active.name, active.name === wakeDefaultAgent))
        : [],
    [active, wakeJobsAll, wakeDefaultAgent],
  )
  const wakeHooks = useMemo(
    () =>
      active && wakeTokens
        ? wakeTokens.filter((t) => webhookBoundToCrew(t, active.name))
        : [],
    [active, wakeTokens],
  )
  const { todayCount, weekCount, todayFloorTs, weekFloorTs } = useMemo(() => {
    const midnight = new Date()
    midnight.setHours(0, 0, 0, 0)
    const todayFloor = midnight.getTime() / 1000
    const weekFloor = Date.now() / 1000 - 7 * 86400
    let today = 0
    let week = 0
    for (const e of activeEntries) {
      if (e.ts >= todayFloor) today += 1
      if (e.ts >= weekFloor) week += 1
    }
    return { todayCount: today, weekCount: week, todayFloorTs: todayFloor, weekFloorTs: weekFloor }
  }, [activeEntries])
  // When the display window is saturated (server capped the entries) AND the
  // oldest returned entry still falls inside a counting window, more in-window
  // events exist beyond the cap — the count is a floor, rendered as "N+"
  // rather than asserted as exact.
  const oldestTs = activeEntries.length ? activeEntries[activeEntries.length - 1].ts : 0
  const todayIsFloor = activityCapped && oldestTs >= todayFloorTs
  const weekIsFloor = activityCapped && oldestTs >= weekFloorTs

  // Recent activity folded by calendar day: the log's rows are all alike
  // ("conversation · <project>"), so eight of them say nothing that one
  // "8 conversations" row does not. Each day carries how the member was
  // reached (picked by a human vs routed by the orchestrator) and the projects
  // it worked in; the rows themselves stay behind the day, as a time strip, for
  // whoever wants the rhythm of the day. Local midnight is the boundary — the
  // same "today" the stat card counts against.
  const activityDays = useMemo(
    () => groupActivityDays(activeEntries, activityCapped),
    [activeEntries, activityCapped],
  )
  // Both folds are reading positions in ONE member's list (same idiom as the
  // driving list): switching members starts the next list folded.
  const [activityDaysExpandedFor, setActivityDaysExpandedFor] = useState('')
  const activityDaysExpanded = activityDaysExpandedFor === activeMemberKey
  const visibleActivityDays = activityDaysExpanded
    ? activityDays
    : activityDays.slice(0, ACTIVITY_DAYS_VISIBLE)
  const [openActivityDay, setOpenActivityDay] = useState('')
  // A day's count phrase; on a floor day the phrase is rendered for n+1 so it
  // takes the plural, and the number is shown as `n+` (see floorCountText).
  const countPhrase = (key: string, n: number, isFloor: boolean) =>
    isFloor ? floorCountText(t(key, { count: n + 1 }), n + 1, n) : t(key, { count: n })

  // Mounting a member thread IS reading it, but nothing on this page moves
  // `chat.activeSlot` (that transition belongs to the Sessions page's
  // switchSlot, the only other markSlotRead caller), so the websocket
  // unread-marker keeps flagging this slot even while the user is looking at
  // it. Drain it here instead: once when the thread opens, and again every
  // time a live message re-flags the mounted thread. Without this the rail
  // badge is permanent — no code path clears a live member slot's unread
  // until the slot itself is deleted.
  const dispatch = useAppDispatch()
  const activeSlotUnread = useAppSelector(
    (s) => !!activeSlot && s.dashboard.unreadSlots.includes(activeSlot),
  )
  useEffect(() => {
    if (activeSlot && activeSlotUnread) dispatch(markSlotRead(activeSlot))
  }, [activeSlot, activeSlotUnread, dispatch])

  // Per-row unread marker: the rail badge says "1", this says WHICH member.
  // Keyed the same way isRunning resolves a member's slot (thread-endpoint
  // cache first, roster binding as the cold-start fallback), and read straight
  // from unreadSlots so the drain effect above clears the dot the moment the
  // thread is opened.
  const unreadSlots = useAppSelector((s) => s.dashboard.unreadSlots)
  const isUnread = useCallback(
    (m: MemberRosterRow) => {
      const key = slotKeyOf(m)
      return !!key && unreadSlots.includes(key)
    },
    [slotKeyOf, unreadSlots],
  )

  // Auto patrol: the auto-nudge loop (monitor / goal loop) bound to a member's
  // own DM slot. This is the thing that wakes a standing member without anyone
  // asking — so a member whose loop has silently stopped, or never armed, is a
  // member that will not act again until someone notices. The roster badge
  // and the drawer block both read from here, so the whole registry is read
  // (the badge needs every member, not just the open drawer's) and filtered
  // per member at render by slot key — the member's derived slot is
  // `member-<slug>`, resolved the same way isRunning resolves it.
  //
  // One React Query read, not a private fetch + frame merge: the websocket
  // hook invalidates AUTONUDGE_LOOPS_QUERY_KEY on every `autonudge_state`
  // frame AND on every (re)connect, so a stop that landed while the socket was
  // down is re-read the moment it comes back, and a transient mount-time
  // failure is retried on the next signal rather than freezing the block in
  // its failed state. The interval is a floor under that: frames fire only on
  // change, and the one reading this block must never give is a stale
  // "Patrolling" for a dead patrol.
  const patrolQuery = useQuery({
    queryKey: AUTONUDGE_LOOPS_QUERY_KEY,
    queryFn: () => api.autonudgeList(),
    refetchInterval: PATROL_REFRESH_MS,
    refetchOnReconnect: true,
  })
  // `failed` is kept distinct from empty for the same reason the wake-sources
  // block keeps it: a failed read must never render the affirmative "no patrol
  // scheduled", which is precisely the false statement this block exists to
  // prevent. A refetch error after a good read keeps showing the last data.
  const patrol = useMemo(() => {
    const data = patrolQuery.data
    const loops: Record<string, AutoNudgeLoop> = {}
    for (const lp of data?.loops || []) if (lp?.slot_key) loops[lp.slot_key] = lp
    return {
      loaded: data !== undefined || patrolQuery.isError,
      failed: data === undefined && patrolQuery.isError,
      loops,
    }
  }, [patrolQuery.data, patrolQuery.isError])
  const patrolLoopOf = useCallback(
    (m: MemberRosterRow) => {
      const key = slotKeyOf(m)
      return key ? patrol.loops[key] : undefined
    },
    [slotKeyOf, patrol.loops],
  )
  /** Roster-level reading of a member's loop record: the loop while it is
   *  ACTIVE, nothing otherwise. A stopped record and a member that never
   *  armed one look the same at the roster — "not patrolling" is the resting
   *  state of a member, not an incident that needs a placeholder mark; the
   *  drawer's block is where a stopped loop keeps its reason. The badge's
   *  presence IS the signal, the way the presence dot and unread dot work. */
  const activePatrolOf = useCallback(
    (m: MemberRosterRow): AutoNudgeLoop | undefined => {
      const lp = patrolLoopOf(m)
      return lp?.active ? lp : undefined
    },
    [patrolLoopOf],
  )
  const activePatrol = activeMemberKey ? patrol.loops[activeMemberKey] : undefined
  // The live facts the status filters read, resolved per row the same way the
  // row's own markers are (isRunning / isUnread / activePatrolOf), so a filter
  // can never disagree with the dot it filters on.
  const signalsOf = useCallback(
    (m: MemberRosterRow): MemberSignals => {
      const key = slotKeyOf(m)
      return {
        running: !!isRunning(m),
        needsYou: !!key && !!liveNeedsYou[key],
        unread: isUnread(m),
        patrolling: !!activePatrolOf(m),
      }
    },
    [slotKeyOf, isRunning, liveNeedsYou, isUnread, activePatrolOf],
  )
  // Narrow the COMMITTED order (never re-sort here): a refetch that advances
  // a last_active_ts must not move rows under the cursor — see orderedMembers.
  const sortedMembers = useMemo(
    () => narrowRoster(orderedMembers, rosterFilterQuery, signalsOf),
    [orderedMembers, rosterFilterQuery, signalsOf],
  )
  // Per-row counts in the filter menu: the one-word labels do not explain
  // themselves and a zero-count row is exactly the one that blanks the list.
  const filterCounts = useMemo(() => countByFilter(members, signalsOf), [members, signalsOf])
  const narrowed = queryNarrows(rosterFilterQuery)
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
  // True when the filters (not the search) hid everything — the empty-roster
  // copy would be wrong then, since the roster is not empty.
  const filteredOut =
    loaded && !loadError && members.length > 0 && sortedMembers.length === 0 && !filter.trim()
  // Which of the block's three verdicts to render. An active loop wins; a
  // stopped loop keeps its reason visible rather than collapsing into
  // "nothing scheduled" — that collapse is exactly how a dead patrol goes
  // unnoticed. (A refused arm is a reserved fourth verdict: the registry
  // contract names the field, but no backend emits it yet, so nothing here
  // renders one.)
  const patrolState: 'active' | 'stopped' | 'none' = activePatrol?.active
    ? 'active'
    : activePatrol
      ? 'stopped'
      : 'none'
  // Clock for the "next wake" countdown, ticking only while the summary shows
  // an active loop — the same deadline-preserving reading the composer's goal
  // chip renders (see nextCycleText), on a coarser tick.
  const [nowTs, setNowTs] = useState(() => Date.now() / 1000)
  const patrolTicking = summaryVisible && patrolState === 'active'
  useEffect(() => {
    if (!patrolTicking) return
    setNowTs(Date.now() / 1000)
    const timer = setInterval(() => setNowTs(Date.now() / 1000), PATROL_TICK_MS)
    return () => clearInterval(timer)
  }, [patrolTicking])
  // The roster badge's mount/unmount tween honours the OS motion preference:
  // the state change still happens, it just cuts instead of fading.
  const reduceMotion = useReducedMotion()

  // Open a member's thread and remember it as the last one opened. Called by
  // the URL sync effect only (plus the same-member re-click below), so every
  // way of arriving at a member — click, back/forward, shallow link, restore
  // on return — runs one code path. `remember` is false only for the member
  // opened IN PLACE OF one a link named that is gone: that open is the page's
  // choice, not the user's, so one stale link must not overwrite the member
  // they had actually chosen.
  const activate = useCallback(
    (m: MemberRosterRow, remember = true) => {
      activeNameRef.current = m.name
      setActiveName(m.name)
      if (remember) safeSetItem(LAST_MEMBER_KEY, m.name)
      // A Side Chat belongs to the member it was asked about; nothing to reset
      // here — the panel's strip is bucketed per member slot, so switching
      // members swaps the whole strip and a Side tab stays with its member.
      // ALWAYS post, even when a slot key is already cached: the endpoint is
      // the idempotent creator/repairer, and the backend can lose the live
      // slot between opens (archive, restart with a stale binding) — a cached
      // key mounted without the POST would point at nothing. The cache only
      // decides what to render while the POST is in flight — and, for the side
      // panel, not even that (see confirmedSlot).
      postThread(m)
    },
    [postThread],
  )

  const openMember = useCallback(
    (m: MemberRosterRow) => {
      // Re-clicking the open member is the repair gesture (re-POST); the URL
      // is unchanged so the sync effect would not fire — call through. It is
      // also an explicit choice of that member, so a swap notice still
      // standing over it (the user was routed here from a dead link) has
      // been acknowledged: retire it.
      if (m.name === activeName) {
        activate(m)
        setGone(null)
        return
      }
      if (urlMember) {
        // Switching between members while one is open REPLACES the entry, so
        // the page holds one history entry however many members are visited
        // and Back leaves it in one press — the Sessions sidebar's rule.
        setSearchParams({ [MEMBER_PARAM]: m.name }, { replace: true })
        return
      }
      // Entering a thread from the roster (below md, where no member is open)
      // is a step in a two-level navigation, so it is PUSHED. The state marks
      // the entry as pushed from this page's roster, which is what lets the
      // below-md back button pop instead of replace.
      setSearchParams({ [MEMBER_PARAM]: m.name }, { state: { fromRoster: true } })
    },
    [activeName, urlMember, activate, setSearchParams],
  )

  // URL -> open member. Once the roster is in: a URL that names a member
  // opens it; a URL that names none (a fresh visit, the sidebar entry, a
  // reload) is REPLACED with the remembered member, else the first row — so
  // the page never lands on the empty column, and the URL always says what
  // is on screen. A URL naming a member that is gone (deleted or renamed)
  // takes the same fallback, with a one-line notice above the thread naming
  // the swap — the user asked for someone specific, and a silently mounted
  // other thread is the misroute this page exists to prevent. Below md the
  // page is a two-level list->detail navigation: no `?member=` IS the
  // roster, so no auto-open there (same rule as SidePanelLayout's remembered
  // tab), and a gone member in the URL returns to the roster instead of
  // bouncing the phone user into a different member's thread.
  useEffect(() => {
    if (!loaded || loadError) return
    if (urlMember) {
      const hit = members.find((m) => m.name === urlMember)
      if (hit) {
        // Opened in place of a gone member a link named? Then it is not the
        // user's choice and must not become the memory (see `activate`).
        const standIn = goneStandInRef.current === hit.name
        goneStandInRef.current = ''
        if (hit.name !== activeNameRef.current) activate(hit, !standIn)
        // The notice belongs to the member shown in place of the gone one;
        // opening anyone else retires it. Functional updates throughout, and
        // `gone` is NOT a dependency: the URL write below is a router
        // transition, and a plain state write that re-armed this effect
        // before the transition committed would re-issue both writes and
        // keep interrupting the transition — the thread would never open.
        setGone((prev) => (prev && prev.shown !== hit.name ? null : prev))
        return
      }
      // Named but not (yet) on the roster while a refetch is in flight: a link
      // may simply have outrun the cache — the crew manager's create lands
      // here with the just-made member's name before the invalidated roster
      // has re-read (#9513). Hold the "gone" verdict until the fetch answers;
      // a member the fresh roster still lacks takes the fallback then.
      if (rosterQuery.isFetching) return
    }
    if (isMobile) {
      if (urlMember) {
        // No thread to fall back to below md — the roster is the answer, so
        // say where the member went above the list (shown: '' marks the
        // roster variant of the notice).
        setGone((prev) =>
          prev && prev.name === urlMember && prev.shown === '' ? prev : { name: urlMember, shown: '' },
        )
        setSearchParams({}, { replace: true })
      } else if (activeName) {
        activeNameRef.current = ''
        setActiveName('')
      }
      return
    }
    const target = resolveDefaultMember(safeGetItem(LAST_MEMBER_KEY), orderedMembers)
    if (!target) return
    if (urlMember) {
      setGone((prev) =>
        prev && prev.name === urlMember && prev.shown === target.name
          ? prev
          : { name: urlMember, shown: target.name },
      )
      goneStandInRef.current = target.name
    }
    setSearchParams({ [MEMBER_PARAM]: target.name }, { replace: true })
  }, [loaded, loadError, urlMember, members, orderedMembers, activeName, isMobile, activate, setSearchParams, rosterQuery.isFetching])

  return (
    // No bottom inset on the root: the card columns carry their own pb-2 and
    // the side panel brings the chat SidePanel's mb-2, so all three end 8px
    // above the window edge without stacking two insets. No right padding
    // either — the panel docks FLUSH to the window's right edge, exactly as it
    // does in the chat page's actbar column; the card columns' pr-2 lives on
    // the inner wrapper below.
    <div className="flex h-full min-h-0" data-testid="members-page">
      {/* Card columns (roster + thread) keep the page's original insets. */}
      <div className="flex flex-1 min-w-0 gap-2 pr-2 pb-2">
      {/* Member list. Below md the page is single-pane: the roster IS the
          page until a member is picked, then the thread takes over and the
          header's back button returns here. Two fixed rails (264+300px)
          otherwise crush the flex-1 thread to zero at narrow widths.
          The card, header line, list body and rows are the Sessions sidebar's
          own recipes (components/listShell) so the two conversation lists read
          as one surface — including the kiro-light shell hook that steps the
          card back from the white canvas. */}
      <aside
        className={`${
          activeName ? 'hidden md:flex' : 'flex'
        } ${LIST_SHELL_CLS} relative w-full md:w-[var(--roster-w)] shrink-0 flex-col min-h-0`}
        // CSS owns the breakpoint: the var is set unconditionally and only the
        // md: class consumes it, so resizing the window across 768px reacts
        // without any JS media-query snapshot going stale.
        style={{ '--roster-w': `${roster.width}px` } as React.CSSProperties}
        data-testid="member-roster"
      >
        <div className={LIST_HEADER_CLS}>
          {/* pl-1.5 is the sidebar's title inset when no rail toggle sits
              before it; the page icon leads the title where the sidebar's
              reads bare, because this header names a page, not a pane. */}
          <div className="flex items-center gap-1.5 min-w-0 flex-1 pl-1.5">
            <Users size={15} className="lucide-inline text-muted shrink-0" />
            <h1 className={LIST_TITLE_CLS}>{t('pages.membersPage.title')}</h1>
          </div>
          {/* Adding a member IS creating a crew, and the crew manager is the
              only write path — so this is a navigation, not an inline form.
              It lands ON the create form, not on the crew list (#9513). */}
          <button
            onClick={() => navigate(CREW_CREATE_PATH)}
            className="flex items-center justify-center w-7 h-7 rounded-md transition-colors bg-transparent border-none shrink-0 text-muted hover:text-text hover:bg-bg-hover cursor-pointer"
            aria-label={t('pages.membersPage.add_member')}
            title={t('pages.membersPage.add_member')}
            data-testid="member-add"
          >
            <UserPlus size={15} />
          </button>
        </div>
        <div className={`px-4 pb-2 ${ROW_STATUS_CLS} text-muted`} data-testid="member-count">
          {/* "N of M" while any filter (not the search) narrows the list, so
              the header never contradicts a 1-row or empty view below it.
              With no roster to count (the read failed) the line is a dash:
              "0 members" above "Could not load the member roster" would
              state as fact what is only unknown. */}
          {loadError
            ? '\u2014'
            : narrowed
              ? t('pages.membersPage.member_count_filtered', {
                  shown: sortedMembers.length,
                  count: members.length,
                })
              : t('pages.membersPage.member_count', { count: members.length })}
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
        {/* The Sessions sidebar's search row (components/SearchFilterBar): the
            same field, clear button and inline sort/filter menu. The menu holds
            what the sidebar's holds for sessions, in the roster's terms — a
            star toggle, the member's live state, its origin, and the sort.
            Menu rows keep the menu open (preventDefault) so several can be
            toggled in one visit, as the sidebar's do. */}
        <SearchFilterBar
          className="px-2 pb-1"
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
              <DropdownMenuContent align="end" className={FILTER_MENU_CONTENT_CLS} data-testid="member-filters">
                <DropdownMenuLabel className={FILTER_MENU_LABEL_CLS}>{t('pages.chatSidebar.filter')}</DropdownMenuLabel>
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
                <DropdownMenuLabel className={FILTER_MENU_LABEL_CLS}>{t('pages.membersPage.filter_origin')}</DropdownMenuLabel>
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
                <DropdownMenuLabel className={FILTER_MENU_LABEL_CLS}>{t('pages.chatSidebar.sort_by')}</DropdownMenuLabel>
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
              </DropdownMenuContent>
            </DropdownMenu>
          )}
        />
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
        <div className="px-2">
          <ErrorNotice
            message={starError?.message}
            report={starError?.report}
            title={t('pages.membersPage.star_failed_title')}
            onDismiss={() => setStarError(null)}
            askAgent
            testId="member-star-error"
          />
        </div>
        {gone && gone.shown === '' && (
          /* Below md a stale link lands on the roster; this is where the
             answer to "where did they go" has to live. Same tone as the
             thread-side notice. */
          <div className="px-4 py-1.5 text-[13px] text-warn" role="status" data-testid="member-gone-roster-notice">
            {t('pages.membersPage.member_gone_roster', { name: gone.name })}
          </div>
        )}
        <ul
          className={`${LIST_BODY_CLS} list-none m-0`}
          style={{ scrollbarWidth: 'none' }}
          aria-label={t('pages.membersPage.title')}
        >
          {loaded && !loadError && members.length === 0 && (
            <li className="px-4 py-6 text-xs text-muted">
              <p>{t('pages.membersPage.empty_roster')}</p>
              {/* The copy only says there is no one yet; this button IS the
                  way to change that — the create form, same destination as
                  the header "+". */}
              <button
                onClick={() => navigate(CREW_CREATE_PATH)}
                className="mt-2 inline-flex items-center gap-1 text-[11.5px] px-2 py-1 rounded border border-border hover:bg-accent/40"
                data-testid="member-empty-cta"
              >
                <UserPlus size={12} className="lucide-inline" />
                {t('pages.membersPage.add_member')}
              </button>
            </li>
          )}
          {loadError && (
            /* The shared notice, not a bare alert: a read failure on a list
               that holds no draft, so the agent hand-off is safe here. */
            <li className="px-2 py-4">
              <ErrorNotice
                message={t('pages.membersPage.roster_load_failed')}
                variant="inline"
                askAgent
                testId="member-roster-error"
              />
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
          {sortedMembers.map((m) => (
            <li key={m.name} className="group/row relative">
              {/* ChatSidebar's own row recipe (components/listShell), so the
                  two conversation lists read as one family; pr-8 widens the
                  right padding over ROW_BOX_CLS's pr-3 to hold the star. The
                  star is a SIBLING of the row button, not a child: a button
                  inside a button is invalid HTML and breaks keyboard
                  activation. It is absolutely placed over the row's right
                  padding so the row keeps its single click target and the
                  label its width. */}
              <button
                onClick={() => openMember(m)}
                // The open row keeps itself in view: a member opened by URL
                // (a deep link, the crew manager's post-create landing) can sit
                // below the fold of a long roster, and a thread with no visible
                // row looks like a member that was never added (#9513).
                ref={m.name === activeName ? scrollActiveRowIntoView : undefined}
                className={cn(
                  'w-full flex items-center gap-2.5 text-sm text-left transition-all select-none',
                  ROW_BOX_CLS, 'pr-8',
                  m.name === activeName ? ROW_ACTIVE_CLS : ROW_IDLE_CLS,
                )}
                aria-current={m.name === activeName ? 'true' : undefined}
              >
                <span className="relative shrink-0">
                  {/* The face reacts: it animates while the member works and
                      flashes its finished / failed expression on the turn's
                      trailing edge. The dot below stays presence-only — a
                      finished turn is not presence. */}
                  <CrewStateAvatar
                    seed={m.name}
                    avatar={m.avatar}
                    slotKey={slotKeyOf(m)}
                    running={!!isRunning(m)}
                    size={36}
                    working="subtle"
                  />
                  {/* Presence dot renders only while the member is working —
                      an idle member shows nothing rather than a gray dot,
                      which read as a broken/disabled state. */}
                  {isRunning(m) && (
                    <span
                      className="absolute -right-0.5 -bottom-0.5 w-2.5 h-2.5 rounded-full border-2 border-bg bg-ok"
                      aria-hidden="true"
                      data-testid="member-presence-dot"
                    />
                  )}
                  {/* Patrol badge — the member has an ACTIVE auto-nudge loop
                      on its own thread. Rendered only while the loop patrols:
                      a stopped loop and a never-armed member both show
                      nothing, because "not patrolling" is a member's resting
                      state, not an incident — a standing warn mark on an
                      idle avatar read as "something is broken", and the
                      drawer's block already spells a stopped loop's reason.
                      Top-right corner of the avatar, the composer's goal-chip
                      glyph on a solid accent fill (the presence dot's own
                      idiom — an outline read as nothing at a glance): a
                      different corner from the presence dot (bottom-right,
                      ok-green, "working now") and a different edge from the
                      row's right-side markers, so all of them can show at
                      once without covering each other. Mount/unmount is
                      animated (the badge fades out when the loop ends rather
                      than vanishing): a badge that pops in or out mid-glance
                      is what a state change looks like when it is not a
                      glitch. Under prefers-reduced-motion the tween is
                      skipped and the badge cuts straight to its new state. */}
                  <AnimatePresence initial={false}>
                    {(() => {
                      const lp = activePatrolOf(m)
                      if (!lp) return null
                      // The tooltip spells the count the drawer's way ("3 of 24"
                      // / "61 · no limit"): the compact "3/24" alone read as a date.
                      const cycle =
                        lp.max_cycles > 0
                          ? t('pages.membersPage.patrol_cycles_of', { n: lp.cycle_count, max: lp.max_cycles })
                          : t('pages.membersPage.patrol_cycles_unlimited', { n: lp.cycle_count })
                      const label = t('pages.membersPage.patrol_badge', { cycle })
                      return (
                        <motion.span
                          key="patrol"
                          initial={reduceMotion ? false : { opacity: 0, scale: 0.6 }}
                          animate={{ opacity: 1, scale: 1 }}
                          exit={reduceMotion ? { opacity: 0 } : { opacity: 0, scale: 0.6 }}
                          transition={reduceMotion ? { duration: 0 } : { duration: 0.15, ease: [0.2, 0, 0, 1] }}
                          className="absolute -right-1 -top-1 w-4 h-4 rounded-full border-2 border-bg flex items-center justify-center bg-accent text-accent-fg"
                          role="img"
                          aria-label={label}
                          title={label}
                          data-testid="member-patrol-dot"
                          data-state="active"
                        >
                          <Goal size={10} aria-hidden="true" />
                        </motion.span>
                      )
                    })()}
                  </AnimatePresence>
                </span>
                <span className="min-w-0 flex-1">
                  <span className={`block ${ROW_TITLE_CLS} font-semibold text-text truncate`}>{m.name}</span>
                  {/* Last-message preview, like a session row — presence
                      already rides the avatar dot, so a textual Idle/Working
                      label said nothing the dot did not. */}
                  <span className={`block ${ROW_STATUS_CLS} text-muted truncate`}>
                    {m.last_message || '\u00a0'}
                  </span>
                </span>
                {/* Unread marker on the row's right edge — the IM convention
                    (and where the rail badge sits), vertically centered by the
                    row's items-center. Accent-filled w-2 h-2 like ChatSidebar's
                    unread dot, with a real accessible name: nothing else on
                    the row says "unread". The left side is taken — presence
                    rides the avatar. */}
                {isUnread(m) && (
                  <span
                    className="w-2 h-2 rounded-full shrink-0"
                    style={{ background: 'var(--accent)' }}
                    role="img"
                    aria-label={t('pages.membersPage.unread_message')}
                    title={t('pages.membersPage.unread_message')}
                    data-testid="member-unread-dot"
                  />
                )}
              </button>
              {/* Star: always rendered when starred. Unstarred: visible below md
                  (touch has no hover or keyboard focus to reveal it), hover /
                  focus-revealed at md+ so a desktop roster stays quiet. Never
                  hidden from AT — opacity, not display. */}
              <button
                type="button"
                onClick={(e) => {
                  e.stopPropagation()
                  toggleStar(m)
                }}
                aria-pressed={!!m.starred}
                disabled={starPending.has(m.name)}
                aria-label={t(m.starred ? 'pages.membersPage.unstar' : 'pages.membersPage.star', { name: m.name })}
                title={t(m.starred ? 'pages.membersPage.unstar' : 'pages.membersPage.star', { name: m.name })}
                // 24x24 minimum target (the icon is 13px): a touch that lands beside
                // the glyph must hit the star, not the row button underneath.
                className={`absolute right-1 top-1/2 -translate-y-1/2 flex items-center justify-center w-6 h-6 rounded hover:bg-bg-hover transition-opacity ${
                  m.starred
                    ? 'opacity-100 text-accent'
                    : 'md:opacity-0 md:group-hover/row:opacity-100 md:focus-visible:opacity-100 text-muted'
                }`}
                data-testid={`member-star-${m.slug}`}
              >
                <Star
                  size={13}
                  {...(m.starred ? { fill: 'var(--accent)', stroke: 'none' } : {})}
                />
              </button>
            </li>
          ))}
        </ul>
        {/* Window-splitter between roster and thread: the same component as the
            Sessions sidebar's grip, sitting on the card's right border the same
            way (absolute, 12px rounded-xl corner inset), so the two pages' edges
            read as one control. md+ only — below md the page is single-pane and
            there is nothing to resize. */}
        <div className="hidden md:block" data-testid="member-roster-resize">
          <ResizeHandle
            handleProps={roster.handleProps}
            label={t('pages.membersPage.resize_roster')}
            onNudge={roster.nudge}
            value={roster.width}
            min={ROSTER_MIN}
            max={ROSTER_MAX}
            inset={12}
            className="absolute top-0 -right-[3px] h-full z-10"
          />
        </div>
      </aside>

      {/* DM thread */}
      <section
        className={`${activeName ? 'flex' : 'hidden md:flex'} flex-1 min-w-0 flex-col min-h-0`}
      >
        {!active && (
          <div className="flex-1 flex items-center justify-center text-sm text-muted px-6 text-center">
            {t('pages.membersPage.pick_a_member')}
          </div>
        )}
        {active && (
          <>
            {/* No rule under the header: it shares the transcript's background
                and is set off by spacing alone, the way ChatPage's session
                header sits over its transcript (bg-bg, no border-b). A hairline
                here read as a second frame inside the pane (issue #9425). */}
            <header className="flex items-center gap-2.5 px-4 py-2" data-testid="member-thread-header">
              <button
                // Back to the roster. When this entry was pushed from the
                // roster on this page, pop it — the browser's own Back then
                // lands on whatever preceded the roster, with no duplicate
                // roster entry. A deep link (no such state) has no roster
                // entry behind it, so drop the param in place instead.
                onClick={() => {
                  if ((location.state as { fromRoster?: boolean } | null)?.fromRoster) navigate(-1)
                  else setSearchParams({}, { replace: true })
                }}
                className="md:hidden inline-flex items-center p-1 -ml-1 rounded hover:bg-accent/40"
                aria-label={t('pages.membersPage.title')}
                data-testid="member-back"
              >
                <ArrowLeft size={16} className="lucide-inline" />
              </button>
              {/* The face is just the face on a chat surface — no hover
                  scrim, no pencil badge: #9116 tried making the avatar the
                  edit entry here and it read as an oversized "Edit avatar"
                  control sitting in the conversation (issue #9425). It is the
                  same reactive CrewStateAvatar as before. */}
              <CrewStateAvatar
                seed={active.name}
                avatar={active.avatar}
                slotKey={activeSlot || active.slot_key}
                running={!!isRunning(active)}
                size={30}
                working="full"
              />
              {/* Title row = name + a small pencil to its RIGHT. That pencil is
                  the member's edit entry: invisible at rest, it fades in when
                  the pointer is over the title row (or the button has focus),
                  and under (hover: none) it sits at low contrast permanently
                  — a touch user can never hover it into view. The click opens
                  the member's WHOLE editor in the crew manager — name,
                  template, model, workspace, triggers, avatar — not just the
                  avatar builder, so the label says "Edit member". It navigates
                  rather than editing here: this page never becomes a second
                  writer (issue #9103). `group/title` is scoped to this row so
                  the drawer toggle to the right does not reveal it. */}
              <div className="group/title min-w-0 flex-1 flex items-center gap-1.5" data-testid="member-title-row">
                <div className="text-[13.5px] font-semibold truncate">{active.name}</div>
                <button
                  type="button"
                  onClick={() => navigate(crewEditPath(active.name))}
                  className="inline-flex shrink-0 items-center justify-center w-6 h-6 rounded-md text-muted hover:text-text hover:bg-bg-hover cursor-pointer focus-ring opacity-0 transition-opacity duration-150 motion-reduce:transition-none group-hover/title:opacity-100 focus-visible:opacity-100 [@media(hover:none)]:opacity-60"
                  aria-label={t('pages.membersPage.edit_member')}
                  title={t('pages.membersPage.edit_member')}
                  data-testid="member-edit-name-button"
                >
                  <Pencil size={13} className="lucide-inline" />
                </button>
              </div>
              {/* The header carries NO panel control while the panel sits
                  beside the thread: that panel is permanent, so a toggle would
                  promise a close the strip does not offer. Only when the window
                  is too narrow for a column (see panelSitsBeside) does the
                  panel become an overlay, and then this is its opener — same
                  icon and hit-target as the chat page's side-panel toggle, so
                  the two surfaces teach one gesture. The pin chip was removed:
                  every member thread is pinned by construction (a server
                  invariant, not a per-thread state), so announcing it taught
                  the user a term for a thing that can never be otherwise.
                  The member's edit entry is not a peer of this toggle: it is
                  the pencil inside the title row, revealed on hover. */}
              {!beside && (
                <button
                  onClick={() => setOverlayOpen((v) => !v)}
                  className="flex items-center justify-center w-7 h-7 rounded-md transition-colors bg-transparent border-none shrink-0 text-muted hover:text-text hover:bg-bg-hover cursor-pointer"
                  aria-pressed={overlayOpen}
                  aria-controls="member-side-panel"
                  aria-label={t('pages.membersPage.details')}
                  title={t('pages.membersPage.details')}
                  data-testid="member-panel-toggle"
                >
                  <PanelRightSolid size={15} />
                </button>
              )}
            </header>
            {/* A failed document read from the panel's Files / Artifacts tabs.
                Reported here, above the thread, rather than inside the tab
                that failed to open — there is no such tab. Dismissable. No
                hand-off: the ChatPane below holds the DM composer draft as
                unsaved local state (its own notices say the same), and the
                agent hand-off navigates away, which would unmount it. */}
            {actionError && (
              <ErrorNotice
                message={actionError}
                onDismiss={() => setActionError('')}
                testId="member-panel-action-error"
              />
            )}
            {gone && gone.shown === active.name && (
              /* Decision-critical (the user is about to type into a thread they
                 did not ask for), so it wears the warn tone at body size, not
                 the drawer's muted timestamp style. A status, not an alert:
                 the fallback did open something. */
              <div className="px-4 py-2 text-[13px] text-warn" role="status" data-testid="member-gone-notice">
                {t('pages.membersPage.member_gone', { name: gone.name, shown: gone.shown })}
              </div>
            )}
            {activeCollision && (
              /* Nothing is mounted under a collision (the slot is cleared), so
                 there is no draft to lose: the hand-off is safe. */
              <div className="px-4 py-2">
                <ErrorNotice
                  message={t('pages.membersPage.slug_collision', { name: activeCollision })}
                  variant="inline"
                  askAgent
                  testId="member-thread-collision"
                />
              </div>
            )}
            {activeThreadFailed && (
              /* No hand-off while a cached thread is mounted under this line:
                 its DM composer still holds whatever the user typed (ChatPane
                 keeps that draft in local state), and the hand-off navigates
                 away, unmounting it. With nothing mounted (a cold open that
                 failed) there is no draft to lose, so the hand-off is on —
                 otherwise the column is a dead end. */
              <div className="px-4 py-2">
                {/* Two sentences for two situations: with no cached thread
                    the column is empty and the open failed outright; with one
                    still mounted below, "could not open" would contradict the
                    conversation the user is looking at — it is the REPAIR
                    that failed, and the copy says so. */}
                <ErrorNotice
                  message={t(
                    activeSlot
                      ? 'pages.membersPage.thread_repair_failed'
                      : 'pages.membersPage.thread_open_failed',
                  )}
                  report={threadOutcome?.errorReport}
                  variant="inline"
                  askAgent={!activeSlot}
                  testId="member-thread-error"
                />
                {threadOutcome?.errorReport && (
                  <details className="mt-1.5 text-[12px] text-muted" data-testid="member-thread-error-details">
                    <summary className="w-fit cursor-pointer rounded-sm focus-ring">{t('pages.membersPage.details')}</summary>
                    {/* No hand-off here: the notice above owns the sole action
                        and disables it while the cached conversation holds a draft. */}
                    <ErrorNotice
                      message={threadOutcome.errorReport.message}
                      report={threadOutcome.errorReport}
                      variant="inline"
                      askAgent={false}
                      className="mt-1"
                      messageClassName="whitespace-pre-wrap"
                    />
                  </details>
                )}
              </div>
            )}
            {activeSlot ? (
              <div className="flex-1 min-h-0">
                <ErrorBoundary>
                  {/* Same reading measure as the main chat transcript — the
                      pane resolves the user's Content width setting itself
                      (transcript and composer both). The DM column is the
                      page's widest region, and an uncapped line length is
                      unreadable on wide screens.

                      steer-only: a DM is a conversation with one named
                      member, not an operator console. Talking to a person has
                      no "queue this until they finish" step, so a send while
                      the member is working goes straight into its running
                      turn — no Steer/Queue split, no queue stack. The main
                      chat and split view keep the split button. */}
                  <ChatPane
                    slotKey={activeSlot}
                    agentLocked
                    frameless
                    followContentWidth
                    busyMode="steer-only"
                    // The failure notice above owns the verdict on this thread
                    // while a repair has failed; the pane's own "Session
                    // ready" would contradict it one line down.
                    hideEmptyHint={activeThreadFailed}
                    openSideChat={openMemberSideChat}
                  />
                </ErrorBoundary>
              </div>
            ) : (
              !activeCollision && !activeThreadFailed && (
                <div className="flex-1 flex items-center justify-center text-xs text-muted">
                  {t('pages.membersPage.opening_thread')}
                </div>
              )
            )}
          </>
        )}
      </section>
      </div>

      {/* Side panel — the chat page's tabbed SidePanel, docked to this page.
          Read-only observation lives in its permanent first tab (Crew
          summary); writes live in the crew manager. The + menu is the chat
          panel's own (Files / Artifacts / Terminal / Browser / Side chat …),
          all against the member's DM slot, and the strip is bucketed per
          member so it follows the roster selection. Wide windows dock it as a
          column with NO close control (it is part of the page, like the roster);
          narrow ones make it an overlay the header button opens, with the
          panel's own close control, on the chat page's dock motion. */}
      {active && (() => {
          const summaryBody = (
            <div className="px-3 py-3" data-testid="member-crew-summary" aria-label={t('pages.membersPage.crew_summary')}>
          {/* Identity + live status line — working now, or the last time
              anything happened on the thread. The chip above names the tab
              (Crew summary) and wears the face; this row names the member. */}
          <div className="flex items-center gap-2 mb-3 min-w-0">
            <CrewAvatar seed={active.name} avatar={active.avatar} size={22} />
            <span className="text-[13px] font-semibold truncate">{active.name}</span>
            <span className="text-[11px] truncate ml-auto shrink-0" data-testid="member-summary-status">
              {isRunning(active) ? (
                <span className="text-ok">{t('pages.membersPage.drawer_working')}</span>
              ) : active.last_active_ts ? (
                <span className="text-muted">{timeAgo(active.last_active_ts)}</span>
              ) : null}
            </span>
          </div>
          {/* Honest counters only — both derive from the recorded activity
              log. Semantic stats the backend cannot attest (PRs, triages,
              spend) are deliberately absent rather than fabricated. */}
          <div className="grid grid-cols-2 gap-2 mb-4" data-testid="member-stats">
            <div className="border border-border rounded-lg px-3 py-2">
              <div className="text-lg font-semibold leading-tight">
                {activityLoading || activityError ? '\u2013' : `${todayCount}${todayIsFloor ? '+' : ''}`}
              </div>
              <div className="text-[11px] text-muted">{t('pages.membersPage.stat_today')}</div>
            </div>
            <div className="border border-border rounded-lg px-3 py-2">
              <div className="text-lg font-semibold leading-tight">
                {activityLoading || activityError ? '\u2013' : `${weekCount}${weekIsFloor ? '+' : ''}`}
              </div>
              <div className="text-[11px] text-muted">{t('pages.membersPage.stat_week')}</div>
            </div>
          </div>
          {/* Sessions this member is driving — the worker sessions it opened
              and steers. Live rows off the WS slots frames (see the
              drivingSessions memo); each row is a jump into that session.
              The status dot is the sidebar's vocabulary: approval (warn) >
              needs input (info) > running (ok) > idle (muted). */}
          <div className="text-[11px] font-semibold tracking-wide text-muted mb-1.5">
            {t('pages.membersPage.driving_sessions')}
          </div>
          {drivingSessions.length === 0 && !slotsLoaded ? (
            <div className="mb-4 space-y-1.5" data-testid="member-driving-loading" aria-hidden>
              <div className="h-3 rounded bg-accent/40 animate-pulse" />
              <div className="h-3 w-3/4 rounded bg-accent/40 animate-pulse" />
            </div>
          ) : drivingSessions.length === 0 ? (
            <div className="text-[11px] text-muted mb-4" data-testid="member-driving-empty">
              {t('pages.membersPage.driving_none')}
            </div>
          ) : (
            <div className="mb-4">
              <ul className="list-none m-0 p-0 space-y-0.5" data-testid="member-driving-sessions">
                {visibleDriving.map((s) => {
                  // Precedence is the shared tab-status contract (approval and
                  // question outrank running); no unread set here, so the
                  // fourth state is plain idle.
                  const kind = tabStatus(s, [], s.key)
                  const status = DRIVING_STATUS[kind]
                  const label = t(status.label)
                  // Slot timestamps are ISO strings; timeAgo wants epoch seconds.
                  const activityTs = lastActivityEpoch(s)
                  const title = s.title || s.key
                  return (
                    <li key={s.key}>
                      <button
                        type="button"
                        onClick={() => navigate(`/chat?sid=${encodeURIComponent(s.key)}`)}
                        className="w-full text-left flex items-center gap-2 text-[11px] px-1.5 py-1 -mx-1.5 rounded hover:bg-accent/40"
                        title={title + PROJECT_SEPARATOR + label}
                        data-testid="member-driving-row"
                        data-status={kind}
                      >
                        <Circle size={8} className={`shrink-0 ${status.cls}`} aria-hidden />
                        <span className="min-w-0 truncate flex-1">{title}</span>
                        {/* The two states parked on the user get words, not
                            just a colour — the sidebar's own idiom for the
                            same signals; running/idle stay dot-only (the
                            label is in the hover title and for AT). */}
                        {status.spoken ? (
                          <span className={`shrink-0 font-medium ${status.text}`}>{label}</span>
                        ) : (
                          <span className="sr-only">{label}</span>
                        )}
                        {activityTs > 0 && (
                          <span className="text-muted shrink-0 whitespace-nowrap">{timeAgo(activityTs)}</span>
                        )}
                      </button>
                    </li>
                  )
                })}
              </ul>
              {drivingSessions.length > DRIVING_VISIBLE && (
                <button
                  type="button"
                  onClick={() => setDrivingExpandedFor(drivingExpanded ? '' : activeMemberKey)}
                  className="mt-1 text-[11px] text-muted hover:text-text"
                  aria-expanded={drivingExpanded}
                  data-testid="member-driving-toggle"
                >
                  {drivingExpanded
                    ? t('pages.membersPage.driving_show_less')
                    : t('pages.membersPage.driving_show_all', { count: drivingSessions.length })}
                </button>
              )}
            </div>
          )}
          {/* Auto patrol — the auto-nudge loop on this member's own thread,
              beside the sessions it drives: together they answer "is this
              member alive, and what is it doing". Three verdicts, never
              conflated (see patrolState), plus the loading / failed states
              every block in this drawer keeps. The readouts are the composer's
              goal chip's: same cycle spelling, same deadline-preserving
              countdown, same "last fire" wording — so a person who has read
              one has read the other. The block cross-fades on a verdict
              change; a stop that lands while the drawer is open must read as
              a change, not a flicker. */}
          <div className="text-[11px] font-semibold tracking-wide text-muted mb-1.5 flex items-center gap-1.5">
            <Goal
              size={12}
              className={`lucide-inline shrink-0 ${patrolState === 'active' ? 'text-accent' : 'text-muted'}`}
              aria-hidden="true"
            />
            <span className="flex-1">{t('pages.membersPage.patrol_title')}</span>
          </div>
          {!patrol.loaded ? (
            <div className="mb-4 space-y-1.5" data-testid="member-patrol-loading" aria-hidden>
              <div className="h-3 rounded bg-bg-hover animate-pulse" />
              <div className="h-3 w-3/4 rounded bg-bg-hover animate-pulse" />
            </div>
          ) : patrol.failed ? (
            /* The shared notice, not a hand-rolled alert: it keeps the
               structured error context and the agent hand-off. askAgent is
               safe here — a read failure on a drawer that holds no draft. */
            <div className="mb-4">
              <ErrorNotice
                message={t('pages.membersPage.patrol_error')}
                variant="inline"
                askAgent
                testId="member-patrol-error"
              />
            </div>
          ) : (
            <motion.div
              key={patrolState}
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              transition={{ duration: 0.18, ease: [0.2, 0, 0, 1] }}
              className="mb-4"
              data-testid="member-patrol"
              data-state={patrolState}
            >
              {patrolState === 'active' && activePatrol ? (
                <>
                  <div className="text-[11px] font-medium text-accent mb-1.5" data-testid="member-patrol-status">
                    {t('pages.membersPage.patrol_active')}
                  </div>
                  {/* Same label/value idiom as the Configuration list below. */}
                  <dl className="text-[11px] space-y-1 m-0">
                    <div className="flex gap-2">
                      <dt className="w-24 shrink-0 text-muted">{t('pages.membersPage.patrol_interval')}</dt>
                      <dd className="min-w-0 truncate m-0" data-testid="member-patrol-interval">
                        {intervalText(activePatrol.idle_secs)}
                      </dd>
                    </div>
                    <div className="flex gap-2">
                      <dt className="w-24 shrink-0 text-muted">{t('pages.membersPage.patrol_cycles')}</dt>
                      <dd className="min-w-0 truncate m-0" data-testid="member-patrol-cycles">
                        {/* Self-describing here ("3 of 24"); the chip keeps its
                            compact "3/24", which alone read as a date. */}
                        {activePatrol.max_cycles > 0
                          ? t('pages.membersPage.patrol_cycles_of', { n: activePatrol.cycle_count, max: activePatrol.max_cycles })
                          : t('pages.membersPage.patrol_cycles_unlimited', { n: activePatrol.cycle_count })}
                      </dd>
                    </div>
                    <div className="flex gap-2">
                      <dt className="w-24 shrink-0 text-muted">{t('pages.membersPage.patrol_last_wake')}</dt>
                      <dd
                        className="min-w-0 truncate m-0"
                        title={activePatrol.last_fire_ts ? fmtDateTimeNumeric(activePatrol.last_fire_ts) : undefined}
                      >
                        {activePatrol.last_fire_ts
                          ? timeAgo(activePatrol.last_fire_ts)
                          : t('components.autoNudgePopover.never')}
                      </dd>
                    </div>
                    <div className="flex gap-2">
                      <dt className="w-24 shrink-0 text-muted">{t('pages.membersPage.patrol_next_wake')}</dt>
                      <dd
                        className="min-w-0 truncate m-0"
                        title={activePatrol.next_due_ts > 0 ? fmtDateTimeNumeric(activePatrol.next_due_ts) : undefined}
                        data-testid="member-patrol-next"
                      >
                        {(() => {
                          // The row already says "Next wake", so the value is
                          // the bare remainder; the due / unscheduled readings
                          // are the composer chip's own sentences.
                          const next = nextCycle(activePatrol, nowTs)
                          switch (next.kind) {
                            case 'in':
                              return t('pages.membersPage.patrol_next_in', { time: next.time })
                            case 'due':
                              return t('components.autoNudgePopover.next_cycle_due')
                            default:
                              return t('components.autoNudgePopover.next_cycle_unscheduled')
                          }
                        })()}
                      </dd>
                    </div>
                    {(activePatrol.banner || activePatrol.message) && (
                      <div className="flex gap-2">
                        <dt className="w-24 shrink-0 text-muted">{t('pages.membersPage.patrol_instruction')}</dt>
                        {/* The banner is the SHORT stand-in the transcript row
                            shows; without one, the instruction's first line.
                            The full text sits in the hover title. */}
                        <dd
                          className="min-w-0 truncate m-0"
                          title={activePatrol.banner || activePatrol.message}
                          data-testid="member-patrol-instruction"
                        >
                          {(activePatrol.banner || activePatrol.message).split('\n')[0]}
                        </dd>
                      </div>
                    )}
                  </dl>
                </>
              ) : patrolState === 'stopped' && activePatrol ? (
                <div className="text-[11px] text-muted" data-testid="member-patrol-status">
                  <span className="text-text">{t('pages.membersPage.patrol_stopped')}</span>
                  {activePatrol.stopped_reason && (
                    <span className="block mt-0.5" data-testid="member-patrol-reason">
                      {PATROL_STOPPED_REASON[activePatrol.stopped_reason]
                        ? t(PATROL_STOPPED_REASON[activePatrol.stopped_reason])
                        : activePatrol.stopped_reason}
                    </span>
                  )}
                  {activePatrol.last_fire_ts > 0 && (
                    <span className="block mt-0.5" title={fmtDateTimeNumeric(activePatrol.last_fire_ts)}>
                      {t('pages.membersPage.patrol_last_wake_ago', { when: timeAgo(activePatrol.last_fire_ts) })}
                    </span>
                  )}
                </div>
              ) : (
                <div className="text-[11px] text-muted" data-testid="member-patrol-status">
                  {t('pages.membersPage.patrol_none')}
                </div>
              )}
            </motion.div>
          )}
          <div className="text-[11px] font-semibold tracking-wide text-muted mb-1.5">
            {t('pages.membersPage.recent_activity')}
          </div>
          {/* Three states, never conflated: a pending or failed read must not
              render the affirmative "no recorded activity". */}
          {activityLoading ? (
            <div className="mb-4 space-y-1.5" data-testid="member-activity-loading" aria-hidden>
              <div className="h-3 rounded bg-accent/40 animate-pulse" />
              <div className="h-3 w-3/4 rounded bg-accent/40 animate-pulse" />
            </div>
          ) : activityError ? (
            <div className="mb-4">
              <ErrorNotice
                message={t('pages.membersPage.activity_error')}
                variant="inline"
                askAgent
                testId="member-activity-error"
              />
            </div>
          ) : activeEntries.length === 0 ? (
            <div className="text-[11px] text-muted mb-4">
              {t('pages.membersPage.activity_empty')}
            </div>
          ) : (
            /* One row per calendar day, newest first, three days before the
               list folds. A row opens into the day's time strip — the same
               rows the old list showed, reduced to the one thing that varied
               between them (the clock), with routed picks marked in accent. */
            <div className="mb-4" data-testid="member-activity-days">
              {/* A grid, not flex rows: the day column sizes to its widest
                  label ("yesterday" in English, 「前天」 in Chinese) instead of
                  a fixed width that gapes in one locale and clips the other. */}
              <ul className="list-none m-0 p-0 -mx-1.5 grid grid-cols-[max-content_minmax(0,1fr)_auto] gap-y-0.5">
                {visibleActivityDays.map((day) => {
                  const dayKey = `${activeMemberKey}:${day.dayStart}`
                  const open = openActivityDay === dayKey
                  const first = day.projects[0] ? projectLabel(day.projects[0]) : ''
                  const more = day.projects.length - 1
                  return (
                    <li key={day.dayStart} className="contents">
                      <button
                        type="button"
                        onClick={() => setOpenActivityDay(open ? '' : dayKey)}
                        className="col-span-3 grid grid-cols-subgrid items-center gap-x-2 text-left text-[11px] px-1.5 py-1 rounded hover:bg-accent/40"
                        aria-expanded={open}
                        data-testid="member-activity-day"
                      >
                        <span className="text-muted whitespace-nowrap">
                          {activityDayLabel(day.dayStart)}
                        </span>
                        {/* Wraps rather than truncates: the project is the value
                            the row exists to show, and it sits last — the first
                            thing an ellipsis ate on a dense day. */}
                        <span className="min-w-0 break-words">
                          {/* `isFloor`: the server capped the log and this day holds
                              its oldest returned entry, so older events may be
                              missing — the count is "at least N", shown as N+.
                              The footer under the list says so in words. */}
                          {day.chats > 0 && countPhrase('pages.membersPage.activity_chat_count', day.chats, day.isFloor)}
                          {day.chats > 0 && day.routed > 0 && PROJECT_SEPARATOR}
                          {day.routed > 0 && (
                            <>
                              {/* The same glyph the time strip uses, introduced here
                                  beside its name so the strip's bare icon is
                                  already taught by the time a day is opened. */}
                              <Route size={10} className="inline-block align-[-1px] mr-0.5" aria-hidden />
                              {countPhrase('pages.membersPage.activity_routed_count', day.routed, day.isFloor)}
                            </>
                          )}
                          {first && (
                            <span className="text-muted" title={day.projects.join('\n')}>
                              {PROJECT_SEPARATOR}
                              {/* Spelled out, not "+1": the bare plus already
                                  means "at least" on a floor count in this row. */}
                              {more > 0
                                ? t('pages.membersPage.activity_projects_more', { name: first, count: more })
                                : first}
                            </span>
                          )}
                        </span>
                        <ChevronRight
                          size={12}
                          className={`shrink-0 text-muted transition-transform duration-150 ${open ? 'rotate-90' : ''}`}
                          aria-hidden
                        />
                      </button>
                      {/* Plain muted text, not pills and not accent: these open
                          nothing, and both a border and the accent colour read as
                          something to click. The Route icon alone marks an
                          orchestrator pick; the tooltip spells it out. */}
                      {open && (
                        <ul
                          className="list-none m-0 p-0 col-start-2 col-span-2 flex flex-wrap gap-x-2.5 gap-y-0.5 pr-1.5 pt-0.5 pb-1.5"
                          data-testid="member-activity-times"
                        >
                          {day.entries.map((e, i) => {
                            const routed = e.via === 'select_crew'
                            return (
                              <li
                                key={`${e.ts}-${i}`}
                                className="inline-flex items-center gap-0.5 font-mono text-[10px] leading-4 text-muted"
                                title={
                                  (routed
                                    ? t('pages.membersPage.activity_routed')
                                    : t('pages.membersPage.activity_chat')) +
                                  (e.project ? PROJECT_SEPARATOR + e.project : '')
                                }
                                data-routed={routed || undefined}
                              >
                                {routed && <Route size={10} className="shrink-0" aria-hidden />}
                                {fmtTime(e.ts)}
                              </li>
                            )
                          })}
                        </ul>
                      )}
                    </li>
                  )
                })}
              </ul>
              {activityDays.length > ACTIVITY_DAYS_VISIBLE && (
                <button
                  type="button"
                  onClick={() => setActivityDaysExpandedFor(activityDaysExpanded ? '' : activeMemberKey)}
                  className="text-[11px] text-muted hover:text-text px-1.5 py-1 -mx-1.5 rounded hover:bg-accent/40"
                  data-testid="member-activity-more"
                >
                  {activityDaysExpanded
                    ? t('pages.membersPage.driving_show_less')
                    : t('pages.membersPage.activity_more_days', {
                        count: activityDays.length - ACTIVITY_DAYS_VISIBLE,
                      })}
                </button>
              )}
              {/* Says in words what the `N+` on the oldest day means, so the
                  floor is explained where it is seen rather than on hover. */}
              {activityCapped && (
                <div className="text-[11px] text-muted mt-1" data-testid="member-activity-capped">
                  {t('pages.membersPage.activity_capped')}
                </div>
              )}
            </div>
          )}
          <div className="text-[11px] font-semibold tracking-wide text-muted mb-1.5 flex items-center">
            <span className="flex-1">{t('pages.membersPage.wake_sources')}</span>
            {/* Read-only view; managing schedules stays on the Schedule page
                (same jump idiom as the crew editor's wake pane). */}
            <button
              onClick={() => navigate('/schedule')}
              className="inline-flex items-center p-0.5 rounded hover:bg-accent/40 text-muted hover:text-text"
              aria-label={t('pages.membersPage.open_schedule')}
              title={t('pages.membersPage.open_schedule')}
              data-testid="member-wake-jump"
            >
              <ExternalLink size={12} className="lucide-inline" />
            </button>
          </div>
          {!wakeLoaded ? (
            <div className="mb-4 space-y-1.5" data-testid="member-wake-loading" aria-hidden>
              <div className="h-3 rounded bg-accent/40 animate-pulse" />
            </div>
          ) : wakeFailed ? (
            <div className="mb-4">
              <ErrorNotice
                message={t('pages.membersPage.wake_error')}
                variant="inline"
                askAgent
                testId="member-wake-error"
              />
            </div>
          ) : wakeJobs.length === 0 && wakeHooks.length === 0 && patrolState !== 'active' ? (
            <div className="text-[11px] text-muted mb-4">{t('pages.membersPage.wake_none')}</div>
          ) : (
            <ul className="list-none m-0 p-0 mb-4 space-y-1.5" data-testid="member-wake-sources">
              {/* An active patrol IS a wake source — the one this member set
                  for itself. Listing it here keeps the card from saying
                  "Last wake 6m ago" above "Nothing wakes this member". */}
              {patrolState === 'active' && activePatrol && (
                <li className="flex items-center gap-2 text-[11px]" data-testid="member-wake-patrol">
                  <Goal size={12} className="lucide-inline text-accent shrink-0" aria-hidden="true" />
                  <span className="min-w-0 truncate flex-1">{t('pages.membersPage.patrol_title')}</span>
                  <span className="text-muted shrink-0">
                    {t('pages.membersPage.wake_patrol_every', { every: intervalText(activePatrol.idle_secs) })}
                  </span>
                </li>
              )}
              {wakeJobs.map((jb) => (
                <li key={jb.id} className="flex items-center gap-2 text-[11px]">
                  <Clock size={12} className="lucide-inline text-muted shrink-0" />
                  <span className={`min-w-0 truncate flex-1 ${jb.enabled ? '' : 'text-muted'}`}>
                    {jb.name}
                    {!jb.enabled && ` (${t('pages.membersPage.wake_paused')})`}
                  </span>
                  <span className="font-mono text-muted shrink-0 max-w-[45%] truncate" title={jb.schedule}>
                    {jb.schedule}
                  </span>
                </li>
              ))}
              {wakeHooks.map((tk) => (
                <li key={tk.id} className="flex items-center gap-2 text-[11px]">
                  <Webhook size={12} className="lucide-inline text-muted shrink-0" />
                  <span className={`min-w-0 truncate flex-1 ${tk.enabled === false ? 'text-muted' : ''}`}>
                    {tk.label}
                    {tk.enabled === false && ` (${t('pages.membersPage.wake_paused')})`}
                  </span>
                  <span className="text-muted shrink-0">{t('pages.membersPage.wake_webhook')}</span>
                </li>
              ))}
            </ul>
          )}
          <div className="text-[11px] font-semibold tracking-wide text-muted mb-2">
            {t('pages.membersPage.configuration')}
          </div>
          <dl className="text-xs space-y-2">
            <div className="flex gap-2">
              <dt className="w-24 shrink-0 text-muted">
                {t('pages.membersPage.agent_template')}
              </dt>
              <dd className="min-w-0 truncate">{active.kiro_agent || t('pages.membersPage.inherited')}</dd>
            </div>
            <div className="flex gap-2">
              <dt className="w-24 shrink-0 text-muted">{t('pages.membersPage.model')}</dt>
              <dd className="min-w-0 truncate">{active.model || t('pages.membersPage.inherited')}</dd>
            </div>
            <div className="flex gap-2">
              <dt className="w-24 shrink-0 text-muted">
                {t('pages.membersPage.workspace')}
              </dt>
              <dd className="min-w-0 truncate">{String(active.workspace ?? '')}</dd>
            </div>
            <div className="flex gap-2">
              <dt className="w-24 shrink-0 text-muted">
                {t('pages.membersPage.memory_store')}
              </dt>
              <dd className="min-w-0 truncate">{String(active.memory_store ?? '')}</dd>
            </div>
          </dl>
          <div className="mt-3 flex flex-col gap-2 text-[11px] text-muted border border-border rounded-md px-2.5 py-2">
            <span>
              {activeMemory === 'global'
                ? t('pages.kiroCrewAgentsPage.global_memory_v1')
                : activeMemory === 'private'
                  ? t('pages.kiroCrewAgentsPage.private_memory_owned')
                  : activeMemory === 'legacy'
                    ? t('pages.kiroCrewAgentsPage.private_memory_legacy')
                    : activeMemory === 'ownership_mismatch'
                      ? t('pages.kiroCrewAgentsPage.memory_binding_mismatch')
                      : t('pages.kiroCrewAgentsPage.memory_binding_unavailable')}
            </span>
            <Btn onClick={() => {
              const destination = activeMemory === 'global' || activeMemory === 'private'
                ? `/settings/overview?view=memory&store=${encodeURIComponent(active.name === 'default' ? 'default' : String(active.memory_store))}`
                : `${CREW_MANAGER_PATH}&crew=${encodeURIComponent(active.name)}`
              leave(() => navigate(destination), destination)
            }}>
              {activeMemory === 'global' || activeMemory === 'private'
                ? t('pages.kiroCrewAgentsPage.manage_private_memory')
                : activeMemory === 'legacy'
                  ? t('pages.membersPage.setup_in_crew_manager')
                  : t('pages.kiroCrewAgentsPage.open_crew_manager')}
            </Btn>
          </div>
          {/* One exit, into the crew manager (the only writer), landing on
              THIS member's editor — the same destination as the header face,
              so the drawer's text route and the face never disagree. The
              #9116 "Edit avatar" text button is gone: it duplicated the face,
              and the avatar row inside the editor is where the builder opens
              from now. */}
          <button
            onClick={() => navigate(crewEditPath(active.name))}
            className="mt-4 w-full inline-flex items-center justify-center gap-1.5 text-xs px-3 py-2 rounded-md border border-border hover:bg-accent/40"
            data-testid="member-edit-in-manager"
          >
            <Pencil size={12} className="lucide-inline" />
            {t('pages.membersPage.edit_in_crew_manager')}
          </button>
            </div>
          )
          const leadingTab: SidePanelLeadingTab = {
            id: CREW_SUMMARY_TAB_ID,
            title: t('pages.membersPage.crew_summary'),
            // The member's face, not a kind glyph: the chip is the one place
            // the strip says WHOSE panel this is, and it changes with the
            // roster selection — unlike the chat page's ListTree Summary tab,
            // which summarises a transcript.
            icon: <CrewAvatar seed={active.name} avatar={active.avatar} size={16} />,
            render: () => summaryBody,
          }
          // Everything both placements share. Two different keys do two
          // different jobs here. `slot` is the IDENTITY of the panel's bodies —
          // the key a Browser tab's native WebContentsView, an app frame and
          // every document body are keyed by — so it is the same key the strip
          // is bucketed on (`activeSlot`, the key the LAST successful open
          // confirmed) and it holds steady through a re-POST: the transient
          // withdrawal while a thread is being revalidated (a routine WS
          // reconnect re-POSTs) must hide the slot-bound views, not re-key them
          // — a Browser body re-keyed to '' and back would `close()` its live
          // WebContentsView and lose history and form state. WHICH views may be
          // offered is `hiddenViews`' job, gated on `confirmedSlot`: until the
          // CURRENT open's POST confirms the key (and after a refusal) every
          // slot-bound view is withheld, so nothing can be dispatched against a
          // key the endpoint may refuse; the document actions above bind to
          // `confirmedSlot` for the same reason. No bottom dock: this page has
          // no bottom grid row for it to move into.
          const panelProps = {
            tabsCtl,
            slot: activeSlot,
            hiddenViews,
            onActiveTabChange: setShownTabId,
            projectDir,
            onFileOpen: openFile,
            onArtifactOpen: openArtifact,
            onFileSave: saveFile,
            leadingTab,
            slotTitle: active.name,
            canDockBottom: false,
          }
          // ONE SidePanel instance for both placements. Docked and overlay differ
          // only in the wrapper (an in-flow column vs a fixed sheet below the
          // 42px app topbar) and in the motion axis (the chat page's width
          // reveal vs a slide from the right edge), so they share one keyed
          // element and the panel is never remounted by a placement flip — a
          // live Browser tab's WebContentsView, like an app tab's frame, does
          // not survive a remount. For the same reason a CLOSED overlay stays
          // MOUNTED and hidden while such a tab exists (`shouldMountSidePanel`
          // / `isSidePanelHidden`, the chat page's exact rule); with no live
          // tab it unmounts on close, which preserves the exit motion. Both
          // axes are named in every target — see sidePanelDockMotion for why an
          // axis left out of `animate` freezes at its last value.
          // Two nested motion elements in BOTH placements so the SidePanel
          // instance is the same React subtree whichever way it is shown. Docked:
          // the outer is the chat page's width reveal and the inner is inert.
          // Overlay: the outer is a full-bleed SCRIM below the 42px app topbar
          // (fades in; a click on it closes the overlay — the whole chat column
          // is dimmed rather than left peeking out as a sliver beside the panel,
          // which read as a rendering fault) and the inner slides the panel in
          // from the right edge. Both axes are named in every target — see
          // sidePanelDockMotion for why an axis left out of `animate` freezes.
          const outerMotion = beside
            ? dockMotion
            : {
              initial: { opacity: 0, width: 'auto', height: '100%' },
              animate: { opacity: 1, width: 'auto', height: '100%' },
              exit: { opacity: 0, width: 'auto', height: '100%' },
            }
          const innerMotion = beside
            ? { initial: { x: 0 }, animate: { x: 0 }, exit: { x: 0 } }
            : { initial: { x: '100%' }, animate: { x: 0 }, exit: { x: '100%' } }
          return (
            <AnimatePresence initial={false}>
              {panelMounted && (
                <motion.div
                  key="member-side-panel"
                  id="member-side-panel"
                  initial={outerMotion.initial}
                  animate={outerMotion.animate}
                  exit={outerMotion.exit}
                  transition={{ duration: 0.18, ease: [0.2, 0, 0, 1] }}
                  className={beside
                    ? 'h-full overflow-visible flex justify-end shrink-0'
                    /* The overlay MUST be dismissable, so it is the one placement
                       that hands the panel an onClose — and the scrim is a second
                       dismiss, the drawer convention. On a phone the panel is
                       handed the window width (`fillWidth`) so it fills the
                       scrim; on a tablet-width window the panel keeps its own
                       (resizable, persisted) width against the dimmed chat. */
                    : 'fixed top-safe-offset-[42px] bottom-safe left-safe right-safe z-40 flex justify-end bg-bg/60 backdrop-blur-sm'}
                  style={panelHidden ? { display: 'none' } : undefined}
                  onClick={beside ? undefined : (e) => { if (e.target === e.currentTarget) closeOverlay() }}
                  data-testid="member-side-panel"
                  data-placement={beside ? 'docked' : 'overlay'}
                >
                  <motion.div
                    initial={innerMotion.initial}
                    animate={innerMotion.animate}
                    exit={innerMotion.exit}
                    transition={{ duration: 0.18, ease: [0.2, 0, 0, 1] }}
                    className={beside ? 'h-full flex justify-end' : 'h-full flex justify-end max-w-full'}
                  >
                    <SidePanel
                      {...panelProps}
                      panelHidden={panelHidden}
                      /* Docked: permanent — no onClose, so the strip renders no
                         close control and Escape inside a view does nothing.
                         `extraReserveW` keeps the live roster width plus the
                         page's gaps clear on top of the shell reserve, so a drag
                         can never fold the thread to nothing (the contract the
                         old drawer's reserveWidth carried). Overlay: the panel
                         covers the thread, so nothing to reserve. */
                      onClose={beside ? undefined : closeOverlay}
                      extraReserveW={beside ? roster.width + PANEL_GAPS_W : 0}
                      /* Phone only (see panelFillWidth): the overlay fills the
                         window. Off the phone this is undefined and the panel
                         sizes itself. */
                      fillWidth={panelFillWidth}
                    />
                  </motion.div>
                </motion.div>
              )}
            </AnimatePresence>
          )
        })()}
    </div>
  )
}
