/**
 * Crew Members — one durable, pinned DM thread per crew member.
 *
 * CAPTURED: Settings > Developer > Feature Previews shows a screenshot of this
 * page in its "See what it looks like" dialog. A visible change here makes that
 * picture stale — re-shoot with `scripts/capture-feature-previews.mjs`.
 *
 * While no thread is open the page shows the full roster. Once a thread opens,
 * the roster folds into `CrewmateSwitcher` in the header and the conversation
 * receives that width. The centred identity pill opens `CrewProfilePanel`: an
 * in-flow 34% column while SidePanel is closed, or a rounded hover card centred
 * over the remaining thread width while SidePanel is open or on a phone. The
 * shared avatar moves between pill and card.
 *
 * The chat SidePanel has two standing entries here: the crewmate's generated
 * Dashboard and the workspace-scoped Files browser. Side Chat remains dynamic.
 * Profile owns Notes, Schedules, driven Sessions and browser-local Goals.
 * Configuration still writes through CrewEditorDialog.
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
 * Which crewmate is open rides the URL (`?member=<name>`), and the last one
 * opened is remembered per browser: a visit that names no one lands on the
 * remembered crewmate if it is still on the roster, else on the most recently
 * USED chat (greatest `last_active_ts`), else on the built-in Assistant
 * (`kirocrew-captain` on its own template, see
 * lib/assistantMember), which is a crewmate like any other: a first visit
 * with no usage lands on its welcome. The reserved `default` member is never
 * landed on or remembered. That is the conversation the
 * user most plausibly came back for, and it is a property of the user's own
 * history, not of the list order: #11763 rejected priming the user on
 * whichever row the SORT floated to the top, and that still holds — the
 * default follows use, never the sort. Only a roster with no crewmate beyond
 * `default` opens nothing;
 * it shows the New crewmate hero instead. Below md the same landing applies
 * to a bare visit; the roster is the explicit `?view=roster`, which the
 * header Back writes and which never auto-opens.
 *
 * Creating a crewmate happens in place of the chat, on the New crewmate card,
 * with the chat and side panel hidden but still MOUNTED. See `openCreate`.
 */
import { Fragment, lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useLocation, useNavigate, useSearchParams } from 'react-router-dom'
import { ArrowLeft, Check, ChevronRight, Circle, Goal, LayoutDashboard, Loader2, MessageCircleQuestionMark, Plus, RotateCw, Square, Star, Users, X, Zap } from 'lucide-react'
import { usePreviewFlag } from '../../hooks/usePreviewFlag'
import { PREVIEW_DASHBOARD } from '../../utils/previewFlags'
import { PanelRightSolid } from '../../components/icons/panels'
import { Btn } from '../../components/ui'
import { CrewMemberMark } from '../../components/CrewMemberMark'
import NewCrewmateDialog, { type CreatedCrewmate, type CrewmateDraft } from './NewCrewmateDialog'
import { useTranslation } from 'react-i18next'
import { api, type CrewTeam, type MemberRosterRow } from '../../api/client'
import { crewDisplayName, type KiroCrewAgent } from '../../components/AgentSelector'
import { isAssistantMember } from '../../lib/assistantMember'
import { useMemberFirstGreeting } from './useMemberFirstGreeting'
import {
  MEMBERS_ROSTER_QUERY_KEY,
  memberThreadQueryKey,
  membersRosterQuery,
  type MemberThreadOutcome,
} from '../../api/membersQuery'
import { teamsQuery } from '../../api/teamsQuery'
// Lazy so the editor's code (this dialog + the field components it re-exports)
// splits out of the main App chunk — it is only needed once the identity pill
// opens the modal, and keeping it eager pushed App over its bundle budget. The
// hook stays eager: it is called every render to drive the pill's open state.
const CrewEditorDialog = lazy(() => import('../../components/crew/CrewEditorDialog'))
// A tiny NON-lazy fallback shown while the CrewEditorDialog chunk downloads on a
// cold-cache first open, so the pill click gives immediate feedback instead of
// rendering nothing until the chunk lands. Deliberately plain (no Radix dialog)
// so it stays out of the lazy split's purpose — keeping the editor's code off the
// main App chunk. The hook's own `crew-editor-loading` dialog takes over once the
// chunk is in and the roster read is still pending.
function CrewEditorChunkLoading() {
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-bg/60"
      role="dialog"
      aria-busy="true"
      aria-label={i18nT('pages.chatPage.loading')}
      data-testid="crew-editor-chunk-loading"
    >
      <div className="flex items-center gap-2 rounded-lg border border-border bg-bg-elevated px-5 py-4 text-[13px] text-muted shadow-lg">
        <Loader2 size={16} className="lucide-inline animate-spin" />
        {i18nT('pages.chatPage.loading')}
      </div>
    </div>
  )
}
import { useCrewEditor } from '../../components/crew/useCrewEditor'
import { AUTONUDGE_LOOPS_QUERY_KEY, type AutoNudgeLoop } from '../../components/autoNudgeLoop'
import { skipToken, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { timeAgo } from '../../utils/timeAgo'
import { fmtList } from '../../i18n/format'
import { i18nT } from '../../i18n/t'
import { usePersistedBool } from '../../hooks/usePersistedBool'
import { usePersistedString } from '../../hooks/usePersistedString'
import { findReport, subscribeChatHandoff, type ErrorReport } from '../../utils/errorReport'
import { drainCaptainHandoffs, wantsShownChat } from '../../lib/captainHandoff'
import { useAppDispatch, useAppSelector } from '../../store'
import { selectSlotStreamState, selectSlotToolLog } from '../../store/chatSlice'
import { toolStatusLabel, type ToolStatusDetail } from '../../utils/toolStatusLabel'
import { useSimplifiedToolNames } from '../../hooks/useSimplifiedToolNames'
import { useLanguage } from '../../i18n/LanguageProvider'
import { markSlotRead } from '../../store/dashboardSlice'
import { emitSlotRead, flushSlotRead } from '../../lib/slotReadRelay'
import { setViewedThreadSlot, clearViewedThreadSlot } from '../../lib/viewedThread'
import CrewAvatar from '../../components/CrewAvatar'
import CrewStateAvatar from '../../components/CrewStateAvatar'
import Glass from '../../components/Glass'
import { resolvePillActivity, type PillActivityKind } from './pillActivity'
import ChatPane from '../../components/ChatPane'
import type { ThreadHooks } from '../../app-sdk/messageRenderers'
import { threadsApi, threadsQueryKey } from '../../api/threads'
import ThreadPanel from './ThreadPanel'
import CrewmateSwitcher from './CrewmateSwitcher'
import CrewProfilePanel, { type ProfileTab } from './CrewProfilePanel'
import { createPortal } from 'react-dom'
import { useCrewmateThreadsFlag } from '../../hooks/useCrewmateThreadsFlag'
import { CrewDashboardFrame } from './CrewWebview'
import ErrorBoundary from '../../components/ErrorBoundary'
import ErrorNotice from '../../components/ErrorNotice'
import { useConfirm } from '../../components/ConfirmDialog'
import { useGuideOpenedSurface } from '../../guide/useGuideOpenedSurface'
import { useGuideRevealScope } from '../../guide/GuideRevealScope'
import { useGuideSelection } from '../../guide/guidePredicates'
import { useGuardedLeave, useMayLeaveForNavigation, usePublishNavigationStake, useRegisterNavigationLeaveGuard } from '../../components/NavigationLeaveGuard'
import CrewNotesTab from './CrewNotesTab'
import { useIsMobile } from '../../hooks/useIsMobile'
import { useConnected } from '../../hooks/useConnected'
import { sessionTitleRoster } from '../../utils/sessionRoster'
import { SearchFilterBar, FilterMenuButton, FilterChip, FILTER_CHIP_ROW_CLS, FilterMenuLabel, FilterMenuContent } from '../../components/SearchFilterBar'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator, DropdownMenuTrigger } from '../../components/ui/dropdown-menu'
import {
  countByFilter, hasNoCrewmates, listedByDefault, narrowRoster, parseSort, parseSourceFilter, parseStatusFilters, queryNarrows,
  rosterPopulation, sortRoster,
  SORT_OPTIONS, SOURCE_FILTERS, STATUS_FILTERS,
  type MemberSignals, type MemberSort, type MemberSourceFilter, type MemberStatusFilter, type RosterQuery,
} from './rosterFilter'
import { defaultAgentQuery } from '../../api/defaultAgentQuery'
import { AnimatePresence, motion, useReducedMotion } from 'framer-motion'
import { isSidePanelHidden, shouldMountSidePanel, sidePanelDockMotion } from '../chat/sidePanelMount'
import SidePanel, { SIDE_PANEL_MIN_W, SIDE_PANEL_RESERVED_W, type SidePanelLeadingTab, type SidePanelWithholdable } from '../chat/SidePanel'
import { useRailWidth } from '../../hooks/useRailWidth'
import { CHAT_TRANSCRIPT_VIEWS, VIEW_DATA_SOURCE, useAnyLiveAppTab, usePanelTabs, type ViewKind } from '../../hooks/usePanelTabs'
import { usePanelTabDescriptors } from '../../hooks/panelTabRegistry'
import CrewWakeSection from '../../components/CrewWakeSection'
import { crewWakeQueryKey, wakesCrew } from '../../components/crew/wakesCrew'
import { usePanelDocumentActions } from '../../hooks/usePanelDocumentActions'
import ResizeHandle from '../../components/ResizeHandle'
import { cn } from '../../lib/utils'
import { LIST_SHELL_CLS, LIST_HEADER_CLS, LIST_TITLE_CLS, LIST_BODY_CLS, ROW_BOX_CLS, ROW_IDLE_CLS, ROW_ACTIVE_CLS, ROW_TITLE_CLS, ROW_STATUS_CLS } from '../../components/listShell'
import { ListDock } from '../../components/ListDock'
import { useColumnResize } from '../../hooks/useColumnResize'
import { loadColumnWidth } from '../../lib/columnWidth'
import { tabStatus, type TabStatus } from '../../lib/sessionTabs'
import { lastActivityEpoch } from '../chat/sessionOrder'
import TeamGroupHeader from './TeamGroupHeader'
import TeamView, { type TeamMemberInput } from './TeamView'
import TeamDialog from './TeamDialog'
import { TEAM_COLLAPSED_KEY, TEAM_PARAM, groupRosterByTeam, parseCollapsedTeams, serializeCollapsedTeams } from './teamGroups'
import { safeGetItem, safeSetItem } from '../../utils/safeStorage'
import { useMemberProjection, useMemberRosterViews } from '../../state/useMemberProjection'
import type { RosterView } from '../../state/memberProjectionTypes'
import type { CrewmateIdentity } from '../chat/CrewmateMessage'
import { SidePanelDockHost } from '../../components/SidePanelGlyph'
import { uiLocation } from '../../uiLocations/uiLocation'

/** Creating a crewmate happens IN this page, in place of the chat: the header
 *  "+" New crewmate, the empty-state hero and the switcher's New crewmate open
 *  `NewCrewmateDialog` (embedded) with its Advanced settings folded; the "+"
 *  menu's Advanced row and the hero's Advanced link open the same form with
 *  Advanced settings already unfolded. A drafted proposal (a Captain
 *  create link carrying a name and goal, a guide) opens the same card with
 *  that name and goal filled in. All perform the same `POST /api/agents`
 *  write as the crew manager's create form (one write path, several front
 *  doors); this page's form also asks for the crewmate's first greeting.
 *  Editing an existing crewmate also happens IN this page now (CREW-18688): the
 *  Profile card's edit controls open `CrewEditorDialog` as a modal, driven by the
 *  shared `useCrewEditor` state machine — the SAME editor the crew manager
 *  mounts, so there is still one write path, reached from two surfaces. */

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

/** Which crewmate to RESTORE when the URL names none, or to fall back to when
 *  it names one that is gone (deleted or renamed since the link/memory was
 *  written): the remembered crewmate if it is still on the roster, else the
 *  most recently USED one — the greatest `last_active_ts`, strict `>` so a tie
 *  keeps the first in `ordered`. Product decision (CrewMates launch review):
 *  when crewmates exist and none is selected, the most recently used chat
 *  opens by default; the "pick one" landing is gone. This deliberately keys on
 *  use, not on `ordered`'s position — #11763 rejected priming the user on
 *  whichever row the SORT floated to the top, and a recency the user produced
 *  themselves is a different thing from a sort they may not have chosen.
 *  With no recorded use at all, the built-in Assistant (`isAssistantMember`)
 *  when it is on the roster -- a first visit lands on its welcome -- else the
 *  first non-`default` row. The reserved `default` member is never landed on
 *  and never remembered. `undefined` when no crewmate exists. Pure, so the
 *  cases — restore, most-recently-used, tie, stale, empty — are tested
 *  directly. */
export function resolveDefaultMember(
  remembered: string | null,
  ordered: readonly MemberRosterRow[],
): MemberRosterRow | undefined {
  if (remembered && remembered !== 'default') {
    const hit = ordered.find((m) => m.name === remembered)
    if (hit) return hit
  }
  let best: MemberRosterRow | undefined
  for (const m of ordered) {
    if (m.name === 'default') continue
    if (!best || (m.last_active_ts ?? 0) > (best.last_active_ts ?? 0)) best = m
  }
  if (best && (best.last_active_ts ?? 0) > 0) return best
  return ordered.find(isAssistantMember) ?? best
}

type MemberMemoryDisplay = 'global' | 'legacy' | 'private' | 'ownership_mismatch' | 'unavailable'

/** Full catalog keys for the Profile Memory tile, one per `memberMemoryDisplay` answer. */
const PROFILE_MEMORY_KEYS: Record<MemberMemoryDisplay, string> = {
  private: 'pages.membersPage.profile_memory_private',
  global: 'pages.membersPage.profile_memory_global',
  legacy: 'pages.membersPage.profile_memory_legacy',
  ownership_mismatch: 'pages.membersPage.profile_memory_ownership_mismatch',
  unavailable: 'pages.membersPage.profile_memory_unavailable',
}

export function memberMemoryDisplay(row: MemberRosterRow): MemberMemoryDisplay {
  if (row.name === 'default') return row.memory_store === 'default' ? 'global' : 'unavailable'
  if (isAssistantMember(row) && row.memory_store === 'default' && !row.memory_owner) return 'global'
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
export const CREW_DASHBOARD_TAB_ID = 'crew-dashboard'
/** Standing host tabs in SidePanel. Files comes from the pinned view block;
 * Dashboard is the one host-owned leading tab. Profile is a sibling surface. */
export const CREW_PANEL_TAB_IDS: readonly string[] = [CREW_DASHBOARD_TAB_ID]
/** Chat-panel views this page withholds from the strip and the + menu
 *  (`SidePanel.hiddenViews`). The unfed half is DERIVED, not enumerated: every
 *  view `VIEW_DATA_SOURCE` classifies as `chat-transcript` (Changes / Issues /
 *  Links / Pins today) reads indexes ChatPage builds over the transcript, none
 *  of which runs here, so each would render an affirmative "none" — and a new
 *  transcript-fed view must be classified where kinds are defined before it can
 *  exist, so it cannot arrive here unwithheld. `summary` is withheld by choice:
 *  Profile Sessions and the generated Dashboard are the member-level views; the
 *  generic chat session summary would be a third, unrelated summary. Exported so the test pins the set. */
export const MEMBERS_UNFED_VIEWS: readonly ViewKind[] = [...CHAT_TRANSCRIPT_VIEWS, 'summary']
/** Everything this page withholds once the thread is confirmed. The rule: the
 *  STANDING tabs here are Dashboard (the host-owned leading tab) and Files (the
 *  one pinned view this page keeps); everything the chat page reaches through
 *  the + menu stays reachable through the + menu here too (Side Chat, Terminal,
 *  Browser, Git, Subagents, Workflows, the Developer-Mode views and any
 *  app-contributed tab), bound to the member's own slot. Withheld, then: the
 *  unfed views above, the chat page's Command Center (the task dashboard lives
 *  in the permanent Dashboard tab, not a second chat view) and Artifacts — a
 *  PINNED standing tab on the chat page, and this page's standing set is
 *  Dashboard + Files, so it is the one withdrawal versus the plain chat panel.
 *  Side chat IS offered — its composer draft lives in the chat-core store
 *  (`sideChatDrafts`, per slot, persisted), so `SidePanel` unmounting the body
 *  on a tab or member switch loses nothing, and the selection toolbar's "Ask
 *  about this" needs the tab as its landing (`openMemberSideChat`); it stays
 *  dynamic, never standing. Exported so the test pins the set. */
export const MEMBERS_WITHHELD_VIEWS: readonly SidePanelWithholdable[] = [...MEMBERS_UNFED_VIEWS, 'command-center', 'artifacts']
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
/** Whether the panel is on screen, and whether the header shows its opener —
 *  the two answers the placement and the two visibility flags decide together.
 *
 *  Each placement reads its OWN flag: the docked column reads the persisted
 *  `dockedOpen`, the overlay reads the per-visit `overlayOpen`. The opener
 *  disappears while the docked column is open, because the open panel's own
 *  strip carries the close control there — the chat page's split of the gesture
 *  across the two halves. As an overlay the opener always stays, since a
 *  dismissed drawer has no strip left to reopen it from.
 *
 *  Pure, so both placements and all four flag combinations are tested directly. */
export function panelChrome({ beside, dockedOpen, overlayOpen }: {
  beside: boolean
  dockedOpen: boolean
  overlayOpen: boolean
}): { panelVisible: boolean; showOpener: boolean } {
  return {
    panelVisible: beside ? dockedOpen : overlayOpen,
    showOpener: !beside || !dockedOpen,
  }
}
/** Punctuation, not prose: joins an activity label to its project name, and a
 *  driving row's title to its status word in the hover title. */
const PROJECT_SEPARATOR = ' \u00b7 '
/** Driving-sessions rows shown before the list folds behind "Show all". */
const DRIVING_VISIBLE = 5
/** Roster filter persistence — same `mc-` localStorage family as the rest of
 *  the dashboard's view preferences (ChatSidebar's session filters use the
 *  same idiom). Only the TOGGLES live here; the star mark itself is a crew
 *  field on the server. */
const STARRED_ONLY_KEY = 'mc-members-starred-only'
const SOURCE_FILTER_KEY = 'mc-members-source'
const STATUS_FILTER_KEY = 'mc-members-status'
const SORT_KEY = 'mc-members-sort'
/** Whether the DOCKED side panel is shown. Same `mc-` family and the same one
 *  key per origin as the filters above: the panel is the page's furniture, not
 *  a per-member state, so the choice follows the user across members and
 *  reloads exactly as the chat page's own panel flag does. */
const PANEL_OPEN_KEY = 'mc-members-panel-open'
/** The Assistant's docked panel choice, hidden until the user opens it. The
 *  Assistant is the landing chat for a first-time user, so its page opens on
 *  the conversation alone; opening the panel there is remembered here without
 *  moving the choice every other crewmate reads from `PANEL_OPEN_KEY`. */
const ASSISTANT_PANEL_OPEN_KEY = 'mc-members-assistant-panel-open'
/** Shared-layout id of the crewmate's face: the pill's face and the docked
 *  card's head face are the SAME element, so only one is ever on screen and the
 *  one slides into the other (framer `layoutId`). */
const CREW_FACE_LAYOUT_ID = 'crew-identity-face'
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
  // unread state whose roster dot reads `var(--ok)` below, and for the sidebar
  // chip that was rebound in #10488 (#10479).
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
/** The identity pill's second line, per activity kind (`pillActivity.ts`),
 *  for the kinds the page owns the copy of. `tool` and `thinking` are absent
 *  on purpose: their text comes from the shared status seam
 *  (`toolStatusLabel`), the same string the sessions sidebar paints for that
 *  moment. `writing` has its own word — the seam's "Streaming" is transport
 *  jargon, and this line says what the crewmate is doing. `idle` has two
 *  spellings — with the time since the thread last moved when one is known,
 *  bare when it is not. File-scope and indexed in place so the key checker
 *  resolves every entry. */
const PILL_ACTIVITY_KEY: Record<Exclude<PillActivityKind, 'tool' | 'thinking'>, string> = {
  writing: 'pages.membersPage.pill_writing',
  compacting: 'pages.membersPage.pill_compacting',
  stopping: 'pages.membersPage.pill_stopping',
  working: 'pages.membersPage.drawer_working',
  delegated: 'pages.membersPage.drawer_delegated_working',
  idle: 'pages.membersPage.pill_idle',
}
/** How often the "next wake in …" countdown in the drawer re-reads the clock.
 *  Coarser than the popover's per-second tick on purpose: the drawer line is
 *  an at-a-glance status, and a per-second re-render of the whole drawer for
 *  a readout that already drops seconds above a minute buys nothing. */
const PATROL_TICK_MS = 15_000
/** Stable empty roster for the not-yet-answered read, so the memos keyed on
 *  `members` do not recompute on every render while the first fetch is out. */
const EMPTY_ROSTER: readonly MemberRosterRow[] = []
const EMPTY_TEAMS: readonly CrewTeam[] = []

/** i18n translate function, taken from the hook so the row need not re-derive
 *  its type. */
type TFn = ReturnType<typeof useTranslation>['t']

/** One roster row. Extracted so `useMemberProjection` is called once PER ROW
 *  (a hook cannot run inside the parent's `.map`), letting a `member_projection`
 *  frame re-render just this row. The projected roster view overrides the
 *  server row field-by-field when present; a field the projection omits (or a
 *  gateway with no projections block) falls back to the row. Presence
 *  (`running`) and the driving list stay on live slots in the parent and are
 *  not projected here. */
function MemberRow({
  m,
  t,
  activeName,
  openMember,
  toggleStar,
  starPending,
  slotKeyOf,
  isRunning,
  isUnread,
  isNeedsYou,
  activePatrolOf,
  reduceMotion,
  scrollActiveRowIntoView,
  slugCollides,
  indented,
}: {
  m: MemberRosterRow
  t: TFn
  activeName: string
  openMember: (m: MemberRosterRow) => void
  toggleStar: (m: MemberRosterRow) => void
  starPending: Set<string>
  slotKeyOf: (m: MemberRosterRow) => string
  isRunning: (m: MemberRosterRow) => boolean | undefined
  isUnread: (m: MemberRosterRow) => boolean
  /** The crewmate's turn is parked on an approval or a question — the same
   *  reading the status filter and the switcher row take (`signalsOf`). */
  isNeedsYou: (m: MemberRosterRow) => boolean
  activePatrolOf: (m: MemberRosterRow) => AutoNudgeLoop | undefined
  reduceMotion: boolean | null
  scrollActiveRowIntoView: (el: HTMLButtonElement | null) => void
  slugCollides: boolean
  /** The row sits under a team header: stepped in so the header reads as its group. */
  indented: boolean
}) {
  // Withheld for a colliding slug, exactly as the page's merged list and drawer
  // do: this row shares its slug with another member, so a slug-keyed frame
  // cannot say which of the two it describes.
  const roster = useMemberProjection<RosterView>(slugCollides ? null : m.slug, 'roster')
  // The projected view over the server row: projection wins field-by-field
  // when present, so slotKeyOf / isRunning / isUnread and the display read the
  // pushed value. `running` is intentionally NOT overridden — it is live
  // presence, resolved from slots in the parent.
  //
  // `last_message` goes the other way, and the direction is the point.
  // Every other field here is config-derived, so the event log is where it is
  // written and the projection IS the record. A message preview is not: the
  // server row carries it from the conversation transcript, which is the store
  // the message was persisted through, and the member/message event is a second
  // copy appended afterwards on a best-effort hook. When that append is refused
  // the projection keeps the PREVIOUS message, so giving it precedence renders a
  // stale preview over the fresh transcript value sitting beside it in the same
  // payload -- and nothing on the card says which of the two it is showing. The
  // projection still fills in when the row has no transcript value at all, which
  // is what a pushed frame is for.
  //
  // `last_active_ts` takes the GREATER of the two instead, matching the merged
  // list above: both are readings of the one folded value (the server row reads
  // it off the same roster projection), so the newer reading is simply the
  // right one, and max keeps a frozen baseline from walking a row backwards.
  const view: MemberRosterRow = roster
    ? {
        ...m,
        kiro_agent: roster.kiro_agent ?? m.kiro_agent,
        workspace: roster.workspace ?? m.workspace,
        memory_store: roster.memory_store ?? m.memory_store,
        model: roster.model ?? m.model,
        source: roster.source ?? m.source,
        starred: roster.starred ?? m.starred,
        avatar: roster.avatar ?? m.avatar,
        slot_key: roster.slot_key ?? m.slot_key,
        last_active_ts: Math.max(m.last_active_ts ?? 0, roster.last_active_ts ?? 0) || undefined,
        last_message: m.last_message || roster.last_message,
      }
    : m
  // `layout` on the row, so the ONE move the recency re-sort makes is
  // followable: a row relocating to the top on the user's own send is a
  // persistent, already-identified element changing place, and an instant
  // teleport makes the reader re-find it. Keyed by the crew NAME (the row's
  // identity), which is what lets framer measure the same element across the
  // re-order. Off under reduced motion — the row still lands in its new place,
  // it just does not travel — and `layout="position"` so only the offset is
  // animated, never the row's size, which would fight the star and the preview
  // reflowing in the same frame.
  return (
      <motion.li
        key={view.name}
        layout={reduceMotion ? false : 'position'}
        transition={{ type: 'spring', stiffness: 520, damping: 42 }}
        className="group/row relative"
      >
        {/* ChatSidebar's own row recipe (components/listShell), so the
            two conversation lists read as one family; pr-8 widens the
            right padding over ROW_BOX_CLS's pr-3 to hold the star. The
            star is a SIBLING of the row button, not a child: a button
            inside a button is invalid HTML and breaks keyboard
            activation. It is absolutely placed over the row's right
            padding so the row keeps its single click target and the
            label its width. */}
        <button
          onClick={() => openMember(view)}
          // The open row keeps itself in view: a member opened by URL
          // (a deep link, the crew manager's post-create landing) can sit
          // below the fold of a long roster, and a thread with no visible
          // row looks like a member that was never added (#9513).
          ref={view.name === activeName ? scrollActiveRowIntoView : undefined}
          className={cn(
            'w-full flex items-center gap-2.5 text-sm text-left transition-all select-none',
            ROW_BOX_CLS, 'pr-8',
            // Grouped-row indent = ROW_BOX_CLS left pad (10) + 14: `pl-6` 24.
            indented && 'pl-6',
            view.name === activeName ? ROW_ACTIVE_CLS : ROW_IDLE_CLS,
          )}
          aria-current={view.name === activeName ? 'true' : undefined}
        >
          <span className="relative shrink-0">
            {/* The face reacts: it animates while the member works and
                flashes its finished / failed expression on the turn's
                trailing edge. The dot below stays presence-only — a
                finished turn is not presence. */}
            <CrewStateAvatar
              seed={view.name}
              avatar={view.avatar}
              slotKey={slotKeyOf(view)}
              running={!!isRunning(view)}
              size={36}
              working="subtle"
            />
            {/* Presence dot renders only while the member is working —
                an idle member shows nothing rather than a gray dot,
                which read as a broken/disabled state. */}
            {isRunning(view) && (
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
                const lp = activePatrolOf(view)
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
            <span className={`block ${ROW_TITLE_CLS} font-semibold text-text truncate`}>{crewDisplayName(view)}</span>
            {/* Last-message preview, like a session row — presence
                already rides the avatar dot, so a textual Idle/Working
                label says nothing the dot does not. A "Stopped" chip
                leads the preview when the thread's NEWEST event is a
                Stop press: the server skips the stop card's JSON, so the
                preview is the last conversational line, which reads as
                ongoing work on a thread the user has stopped — the chip
                is the honest marker over it. It is localized HERE, not
                sent as a word from the server, whose preview is computed
                without the client's locale. The chip is `shrink-0` so
                the preview, not the label, is what truncates. The server
                flag is false once a newer real message lands, so the chip
                cannot outlive the stop. */}
            <span className={`flex items-center gap-1 ${ROW_STATUS_CLS} text-muted min-w-0`}>
              {view.last_message_stopped && (
                <span
                  className="inline-flex items-center gap-0.5 shrink-0 font-medium text-danger"
                  data-testid="member-stopped-indicator"
                >
                  <Square size={9} fill="currentColor" className="lucide-inline" aria-hidden="true" />
                  {t('pages.membersPage.stopped_indicator')}
                </span>
              )}
              <span className="block truncate min-w-0">{view.last_message || '\u00a0'}</span>
            </span>
          </span>
          {/* Needs-you marker on the row's right edge, leading the unread
              dot: the switcher's own cue (CrewmateSwitcher's row), so the
              full roster and the folded one agree about who is parked on
              a question. A `warn` dot that is a named image plus the same
              muted word, `aria-hidden` because the dot's name already says
              the fuller sentence; the two labels are the existing
              `filter_status_needs_you` / `team_needs_you` keys. */}
          {isNeedsYou(view) && (
            <span className="flex items-center gap-1 shrink-0" data-testid="member-needs-you">
              <span
                className="w-2 h-2 rounded-full shrink-0 bg-warn"
                role="img"
                aria-label={t('pages.membersPage.filter_status_needs_you')}
                title={t('pages.membersPage.filter_status_needs_you')}
                data-testid="member-needs-you-dot"
              />
              <span className="text-[11px] text-muted whitespace-nowrap" aria-hidden="true">{t('pages.membersPage.team_needs_you')}</span>
            </span>
          )}
          {/* Unread marker on the row's right edge — the IM convention
              (and where the rail badge sits), vertically centered by the
              row's items-center. w-2 h-2 filled with the STATUS token
              like ChatSidebar's unread dot (#10479/#10488) — not brand
              accent, which a theme also spends on links and chips — with
              a real accessible name: nothing else on the row says
              "unread". The left side is taken — presence rides the
              avatar. */}
          {isUnread(view) && (
            <span
              className="w-2 h-2 rounded-full shrink-0"
              style={{ background: 'var(--ok)' }}
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
            toggleStar(view)
          }}
          aria-pressed={!!view.starred}
          disabled={starPending.has(view.name)}
          aria-label={t(view.starred ? 'pages.membersPage.unstar' : 'pages.membersPage.star', { name: view.name })}
          title={t(view.starred ? 'pages.membersPage.unstar' : 'pages.membersPage.star', { name: view.name })}
          // 24x24 minimum target (the icon is 13px): a touch that lands beside
          // the glyph must hit the star, not the row button underneath.
          className={`absolute right-1 top-1/2 -translate-y-1/2 flex items-center justify-center w-6 h-6 rounded hover:bg-bg-hover transition-opacity ${
            view.starred
              ? 'opacity-100 text-accent'
              : 'md:opacity-0 md:group-hover/row:opacity-100 [@media(hover:none)]:opacity-100 md:focus-visible:opacity-100 text-muted'
          }`}
          data-testid={`member-star-${view.slug}`}
        >
          <Star
            size={13}
            {...(view.starred ? { fill: 'var(--accent)', stroke: 'none' } : {})}
          />
        </button>
      </motion.li>
  )
}

/** The intentionally-empty roster: one hero, one call to action. Type scale and
 *  tokens follow `EmptyState` in components/ui.tsx; the face is the real ghost at
 *  full strength rather than EmptyState's 12%-opacity icon, because here the
 *  avatar IS the subject (what a crewmate looks like), not decoration. */
function CrewmateEmptyHero({ onCreate, onAdvanced, held }: {
  onCreate: () => void
  /** Opens the full New crewmate form (the header menu's Advanced row). */
  onAdvanced: () => void
  /**
   * Why the CTA is held, or `null` when it is live. Set while a just-created
   * crewmate's follow-up is still in flight: the roster re-read has not yet
   * replaced this hero, and a second create through it would clear the first
   * one's recovery record exactly as the header "+" would — so every door to
   * the dialog reads the same hold.
   */
  held: string | null
}) {
  const { t } = useTranslation()
  return (
    <div className="flex-1 flex flex-col items-center justify-center gap-3 px-6 py-10 text-center animate-rise" data-testid="crewmate-empty-hero">
      <div className="mb-1 opacity-90"><CrewAvatar seed="crewmate" size={72} /></div>
      <div className="text-[17px] font-semibold text-text-strong" data-testid="crewmate-empty-title">{t('pages.membersPage.empty_title')}</div>
      <p className="m-0 max-w-[400px] text-[13.5px] leading-relaxed text-muted">{t('pages.membersPage.empty_body')}</p>
      <Btn
        primary
        onClick={onCreate}
        disabled={held !== null}
        title={held ?? undefined}
        aria-label={held ?? undefined}
        className="mt-2 h-9 px-4 text-[13.5px]"
        data-testid="crewmate-empty-cta"
        {...uiLocation('members.new')}
      >
        <Plus size={15} className="lucide-inline" aria-hidden="true" />
        {t('pages.membersPage.add_member')}
      </Btn>
      {/* Every setting stays one click away on an empty roster, where the header
          "+" menu (and its Advanced row) is hidden behind this hero. */}
      <button
        type="button"
        onClick={onAdvanced}
        disabled={held !== null}
        className="text-[12.5px] text-muted underline-offset-2 hover:underline bg-transparent border-none cursor-pointer disabled:cursor-default disabled:opacity-60"
        data-testid="crewmate-empty-advanced"
        {...uiLocation('members.new-advanced')}
      >
        {t('pages.membersPage.create_advanced')}
      </button>
    </div>
  )
}

/** A same-origin `/members?create=1` link's draft, or null for anything else
 *  -- including an href the URL parser refuses, which must never throw. */
export function createDraftFromHref(href: string | null, base: string): CrewmateDraft | null {
  if (!href) return null
  let url: URL
  try {
    url = new URL(href, base)
  } catch {
    return null
  }
  let origin: string
  try {
    origin = new URL(base).origin
  } catch {
    return null
  }
  if (url.origin !== origin || url.pathname !== '/members' || url.searchParams.get('create') !== '1') return null
  return { name: url.searchParams.get('name') ?? '', goal: url.searchParams.get('goal') ?? '' }
}

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

  // CREW-18688: editing a bot opens the crew editor as a MODAL on this page,
  // instead of navigating to /capabilities?tab=crews. The editor's whole
  // state machine lives in useCrewEditor; CrewEditorDialog renders it. The
  // identity pill in the thread header sets `editingCrew`, which drives the hook.
  const [editingCrew, setEditingCrew] = useState('')
  // The profile card (crewmate-panel IA): a floating hover card centred over the
  // thread, which tab it opened on. Every explicit open gets a nonce so asking
  // for the same initial tab again still resets a card the user navigated within.
  // The header pill hides while the card is up and the side panel is not (the
  // card's head names the crewmate then).
  const [profile, setProfile] = useState<{
    tab: ProfileTab
    placement: 'column' | 'floating'
    openNonce: number
  } | null>(null)
  // The profile card's description. The member roster carries no description, so
  // this reads the crew registry under the SAME key the editor and the crew
  // manager share — a save anywhere reaches the card. Display-only: the card
  // never writes, so a retained copy here is fine (the editor keeps its own
  // staleTime:0 observer below).
  const profileRegistryQuery = useQuery<{ agents: KiroCrewAgent[]; default_agent?: string }>({
    queryKey: ['kirocrew-agents'],
    queryFn: () => api.kirocrewAgents(),
    enabled: !!activeName,
  })
  // The crew editor needs the KiroCrewAgent roster (not the member roster):
  // same query key KiroCrewAgentsPage uses, so the cache is shared and a write
  // here reaches both. Only fetched while the editor is open.
  const crewAgentsQuery = useQuery({
    queryKey: ['kirocrew-agents'],
    queryFn: () => api.kirocrewAgents(),
    enabled: !!editingCrew,
    // Always refetch when the editor opens — never serve a cached roster as
    // "fresh" without a network read. The editor saves the WHOLE snapshot, so a
    // cached copy that went stale while nothing invalidated it (an external edit
    // the WebSocket missed while disconnected) would be admitted by the latch
    // below and let a save stomp newer config. staleTime:0 forces the fetch; the
    // rosterAdmitted latch then waits for IT to settle before opening.
    staleTime: 0,
  })
  // The editor saves the WHOLE agent snapshot, so it must OPEN from a roster
  // read that is current — never React Query's RETAINED copy. After this key is
  // invalidated `.data` keeps the pre-edit snapshot while the refetch is in
  // flight; opening the editor from that would let a full-snapshot save stomp
  // newer workspace/model/config values the retained copy predates.
  //
  // So gate the OPENING, not every render: `rosterAdmitted` latches true once a
  // fetch has settled successfully (isSuccess && !isFetching) for the CURRENT
  // editingCrew, and resets when the editor closes/switches. Before admission
  // the editor sees `[]` (→ editingAgent undefined → the hook holds the dialog
  // in its loading state) so a stale snapshot never opens it. AFTER admission we
  // keep showing live `.data`: the editor's own save invalidates this key and
  // the brief in-flight window then carries the just-written snapshot, not a
  // stale one — latching avoids tearing the open dialog down on every save. The
  // error path is handled separately (the roster-load notice), so it is not
  // treated as admission here.
  const rosterAdmittedRef = useRef('')
  const crewRosterSettledFresh = crewAgentsQuery.isSuccess && !crewAgentsQuery.isFetching
  if (!editingCrew) {
    rosterAdmittedRef.current = ''
  } else if (crewRosterSettledFresh && rosterAdmittedRef.current !== editingCrew) {
    rosterAdmittedRef.current = editingCrew
  }
  const rosterAdmitted = !!editingCrew && rosterAdmittedRef.current === editingCrew
  const crewAgents = useMemo<KiroCrewAgent[]>(
    () => (rosterAdmitted ? crewAgentsQuery.data?.agents || [] : []),
    [rosterAdmitted, crewAgentsQuery.data],
  )
  const crewDefaultAgent = rosterAdmitted ? crewAgentsQuery.data?.default_agent || '' : ''
  // The same prefix invalidation KiroCrewAgentsPage uses: it refreshes the
  // member roster (a projection under the same prefix) AND the config the
  // memory row reads, so an edit here updates the open thread's row without a
  // manual refetch.
  const refetchCrewAgents = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: ['kirocrew-agents'] })
    void queryClient.invalidateQueries({ queryKey: ['kirocrewConfig'] })
  }, [queryClient])
  const closeCrewEditor = useCallback(() => setEditingCrew(''), [])
  // Points at `releaseSchedRetention` (defined further down with the schedule
  // guard machinery) so `onCrewDeleted` above can stand down the retention when
  // a confirmed crew delete clears the URL. A ref because the fn is declared
  // after this; no-op until assigned, which is before any delete can fire.
  const releaseSchedRef = useRef<() => void>(() => {})
  // A delete of the OPEN member's crew retires the thread: the record is gone,
  // so clear `?member=` back to the roster rather than leave a thread mounted
  // on a crew that no longer exists. (The editor cannot rename the identity —
  // only display_name — so a save never needs a URL repoint.)
  const onCrewDeleted = useCallback((name: string) => {
    // Deleting the crew is already a confirmed, deliberate action (the editor's
    // own delete-confirm), and the Schedules draft in the panel behind the modal
    // is a schedule FOR this now-deleted crew — so it is intentionally discarded
    // with the crew, not silently lost. Release the schedule retention first so
    // clearing `?member=` unmounts the panel as an acknowledged discard rather
    // than bypassing the draft guard. (The release fn is defined later in this
    // component, so it is reached through a ref.)
    if (name === urlMember) {
      releaseSchedRef.current()
      setSearchParams({}, { replace: true })
    }
  }, [urlMember, setSearchParams])
  const crewEditor = useCrewEditor({
    editingName: editingCrew,
    // The canonical slug for `member_id` matching in the Schedules pane. The
    // pill sets `editingCrew` to a crew's display NAME; the roster row's `slug`
    // is the immutable id a private schedule's `member_id` actually holds, so a
    // renamed crewmate's schedules still resolve. Falls back to the name (the
    // hook then defaults memberId to editingName) when no row matches.
    memberId: rows.find((r) => r.name === editingCrew)?.slug,
    agents: crewAgents,
    // `|| isError` so a FAILED roster read also counts as "settled": otherwise
    // the editor's `loading` flag never clears on error, leaving a permanent
    // spinner dialog whose overlay hides the roster-load error notice behind it.
    // `rosterAdmitted` (not `.data !== undefined`) so a RETAINED stale snapshot
    // during the initial post-invalidation refetch keeps `loading` true rather
    // than opening the editor on data a full-snapshot save would stomp.
    agentsLoaded: rosterAdmitted || crewAgentsQuery.isError,
    defaultAgent: crewDefaultAgent,
    refetchAgents: refetchCrewAgents,
    onClose: closeCrewEditor,
    onDeleted: onCrewDeleted,
    // The editor's Chat and Manage-memory buttons leave `/members`; route them
    // through the page's guard so a typed Schedules draft behind the modal is
    // not silently discarded.
    leave,
  })

  // The crew-editor MODAL (opened by the identity pill) holds its own unsaved
  // pane edits (template, model, routing, …) in `crewEditor.dirtyPanes`. The
  // modal intercepts its OWN in-app close, but the page-level exits below
  // (route leave, browser Back, reload) are outside its reach — so without
  // folding its dirty state in here, editing a field then pressing Back or
  // reloading discarded the edits silently, exactly as an unguarded Schedules
  // draft would. Mirrored to a ref for the synchronous guards and to state for
  // the published stake / `beforeunload` arm. Gated on the modal being open so
  // a stale set never arms a guard over a closed editor. The nested
  // create-workspace modal is a second typed draft the editor can hold open, so
  // `wsModalOpen && wsDirty` is folded in too — the same parity NewCrewmateDialog
  // keeps for its own workspace sub-form. The avatar builder is a third: it
  // composes an override across many controls and commits only on Apply (which
  // then surfaces in `dirtyPanes`), so while it is OPEN the in-progress edit is
  // in no pane yet — the open state itself is the at-stake signal, since the
  // builder exposes no dirty callback to narrow it. The template pane reports
  // its OWN nested-dialog drafts (publish-copy name) through `templateDirty`, a
  // single signal so a future nested template input is covered without adding
  // another term here.
  const crewModalAtStake = !!editingCrew && (crewEditor.dirtyPanes.size > 0 || (crewEditor.wsModalOpen && crewEditor.wsDirty) || crewEditor.avatarBuilderOpen || crewEditor.templateDirty)
  const crewModalAtStakeRef = useRef(crewModalAtStake)
  crewModalAtStakeRef.current = crewModalAtStake
  // In-flight editor WRITE (an update/delete/avatar-upload/capability save still
  // pending), distinct from a dirty draft: navigating away + confirming discard
  // would unmount the modal while the request is in the air, and it could still
  // land — persisting a change the user believes was discarded. So the page
  // guards REFUSE (not merely prompt) while this holds, matching `requestClose`
  // which refuses a close mid-commit. Folded into the stake too so the browser's
  // own beforeunload arms during a write. `sheetBusy` covers the editor's direct
  // mutations (update / delete / avatar upload / capability save); `schedSaving`
  // is the NESTED Schedules-create POST (CrewWakeSection's in-flight create),
  // which lives outside sheetBusy and would otherwise persist a schedule after a
  // confirmed discard unmounts the modal.
  const crewModalBusy = !!editingCrew && (crewEditor.sheetBusy || crewEditor.schedSaving)
  const crewModalBusyRef = useRef(crewModalBusy)
  crewModalBusyRef.current = crewModalBusy

  // Teams: the roster's grouping and the main pane's OTHER occupant. The open
  // team rides the URL like the open member (`?team=<id>`); the two parameters
  // are exclusive -- opening one writes the URL without the other. The list is
  // small and this page's own dialog is its only writer, so a failed read is
  // the only state worth a notice. EVERY failed read, not only the first: a
  // refetch that fails after a save or a return to the page leaves the cached
  // list on screen, and that list is said to be stale rather than shown as
  // current. With no answer at all the roster renders flat.
  const teamsQ = useQuery(teamsQuery)
  const teams = teamsQ.data ?? EMPTY_TEAMS
  const teamsFailed = teamsQ.isError
  const urlTeam = searchParams.get(TEAM_PARAM) ?? ''
  const activeTeam = useMemo(() => teams.find((tm) => tm.id === urlTeam), [teams, urlTeam])
  // Collapsed groups persist per browser, keyed by team id (the "No team" group
  // by its fixed id), the same localStorage idiom as the roster filters.
  const [rawCollapsedTeams, setRawCollapsedTeams] = usePersistedString(TEAM_COLLAPSED_KEY, '[]')
  const collapsedTeams = useMemo(() => parseCollapsedTeams(rawCollapsedTeams), [rawCollapsedTeams])
  const toggleTeamCollapsed = useCallback(
    (id: string) =>
      setRawCollapsedTeams((prev) => {
        const next = parseCollapsedTeams(prev)
        if (next.has(id)) next.delete(id)
        else next.add(id)
        return serializeCollapsedTeams(next)
      }),
    [setRawCollapsedTeams],
  )
  // The New team / Edit team dialog. `team` set = edit. Mounted only while
  // open, so its fields start from the team it was opened for.
  const [teamDialog, setTeamDialog] = useState<{ team?: CrewTeam } | null>(null)
  // Set when a URL NAMED a member that is gone: the user asked for someone
  // specific, so the outcome is said out loud — above the fallback thread on
  // md+ (`shown` = who opened instead), above the roster below md (`shown` is
  // '' — no thread opened). The remembered-member fallback never sets it —
  // there the user named nobody. Cleared once a different member opens.
  const [gone, setGone] = useState<{ name: string; shown: string } | null>(null)
  // The New crewmate card, shown in place of the chat.
  const [createOpen, setCreateOpen] = useState(false)
  // Whether the card opens with Advanced settings unfolded: folded for New
  // crewmate, unfolded for Advanced (the "+" menu row and the hero's link).
  // A proposal carrying a goal unfolds it too (the card's own rule), so the
  // goal is in view.
  const [createExpanded, setCreateExpanded] = useState(false)
  // The proposal an opening starts from (a create link's name and goal), or
  // none. A NEW object per opening: the card re-fills from it by identity.
  const [createDraft, setCreateDraft] = useState<CrewmateDraft>()
  const mayLeave = useMayLeaveForNavigation()
  // The card's own report of whether the user changed what it opened with,
  // and whether a create is in flight.
  const createEditedRef = useRef(false)
  const createBusyRef = useRef(false)
  const onCreateDraftState = useCallback(({ edited, busy }: { edited: boolean; busy: boolean }) => {
    createEditedRef.current = edited
    createBusyRef.current = busy
  }, [])
  const createOpenRef = useRef(false)
  createOpenRef.current = createOpen
  /** Every door to the New crewmate card: the "+" menu, the hero and the
   *  switcher (no draft), and a create link or guide (a proposed name and
   *  goal). A card already open is left only if its guard agrees, which asks
   *  only when the user changed it (a guide that brought us here already
   *  asked: `leaveGranted`); refused, it stays as it is. The chat behind the card
   *  stays mounted, so its unsent draft is there again on the way back. */
  const openCreate = useCallback((expanded: boolean, draft?: CrewmateDraft, leaveGranted = false) => {
    // A create in flight keeps its card as sent: nothing re-fills it.
    if (createOpenRef.current && createBusyRef.current) return
    if (createOpenRef.current && !leaveGranted && !mayLeave()) return
    createEditedRef.current = false
    setCreateDraft(draft ? { ...draft } : undefined)
    setCreateExpanded(expanded)
    setCreateOpen(true)
  }, [mayLeave])
  const draftRequest = searchParams.get('create') === '1'
  // Set below, once the card's close exists: records the guide that opened it.
  const markGuideOpenedRef = useRef<() => void>(() => {})
  useEffect(() => {
    if (!draftRequest) return
    const granted = (location.state as { leaveGranted?: boolean } | null)?.leaveGranted === true
    markGuideOpenedRef.current()
    openCreate(false, { name: searchParams.get('name') ?? '', goal: searchParams.get('goal') ?? '' }, granted)
    const next = new URLSearchParams(searchParams)
    next.delete('create'); next.delete('name'); next.delete('goal')
    setSearchParams(next, { replace: true })
  }, [draftRequest, searchParams, setSearchParams, openCreate, location.state])
  // A guide that opened the card and was then called off steps it aside so the
  // chat (with the guide's result line) shows again -- only while the user has
  // not touched the draft. An edited draft is theirs and stays open.
  markGuideOpenedRef.current = useGuideOpenedSurface('crewmate.create', {
    open: createOpen,
    isPristine: () => !createEditedRef.current && !createBusyRef.current,
    close: () => setCreateOpen(false),
  })
  // Choosing a chat or a team while the card is up means "show me that": the
  // card is left only if its own guard agrees (a create in flight asks too).
  // A card whose create just succeeded has already been stood down by
  // `handleCreated`, so opening the new crewmate passes straight through.
  const leaveCreate = useCallback((): boolean => {
    if (createOpenRef.current) {
      if (!mayLeave()) return false
      setCreateOpen(false)
    }
    return true
  }, [mayLeave])
  // A way back to a chat (the guide's "Back to Captain") asked for the chat:
  // the card standing in its place steps aside, exactly as when the chat is
  // chosen in the roster. The state is spent once.
  const showChatRequested = wantsShownChat(location.state)
  useEffect(() => {
    if (!showChatRequested) return
    leaveCreate()
    navigate({ search: location.search }, { replace: true, state: null })
  }, [showChatRequested, leaveCreate, navigate, location.search])
  const openAdvanced = useCallback(() => openCreate(true), [openCreate])
  const openQuick = useCallback(() => openCreate(false), [openCreate])
  /** The DM column, the containing block of the modal profile card. */
  const threadColumnRef = useRef<HTMLElement>(null)
  // Crewmates just created here whose first chat open has not answered yet,
  // keyed by NAME: two creates can race inside one thread round trip. An
  // entry lives until its own thread answers (confirmed, collision or failed
  // open) and is what lets that answer release the create hold. The first
  // greeting itself is not the page's: the server owes it to the crewmate
  // (`first_greeting` on the create) and the greeting request every member
  // thread open makes (`useMemberFirstGreeting`) starts it, hidden.
  const pendingOpens = useRef(new Set<string>())
  // The post-create follow-up (`openCreated` → roster re-read → thread open)
  // spans several awaits, and nothing above cancels them when the user leaves
  // the page mid-way: a route change unmounts this component, but the roster
  // refetch still resolves and `openThread`'s `onSuccess` still runs.
  // Continuing would `setSearchParams` the page they navigated to back to
  // `/members?member=<new name>`. Every post-await step checks this ref and,
  // when the page is gone, drops the follow-up.
  const pageMounted = useRef(true)
  useEffect(() => {
    pageMounted.current = true
    return () => {
      pageMounted.current = false
    }
  }, [])
  // A step AFTER a successful create that failed: the roster re-read. The
  // record is what the retry needs and the notice above the chat column says
  // so; the create itself is never in doubt here (the server has the
  // crewmate), so the dialog does not reopen.
  const [postCreateError, setPostCreateError] = useState<
    | { kind: 'roster'; created: CreatedCrewmate }
    | null
  >(null)
  // Mirror for the thread mutation's callbacks, which must read the CURRENT
  // record, not the one of the render that armed them.
  const postCreateErrorRef = useRef(postCreateError)
  postCreateErrorRef.current = postCreateError
  // Whether the post-create notice may be closed without its retry: only
  // when the CACHED roster is non-empty — below md the
  // notice column replaces the roster, so an undismissable notice over a
  // roster that has other crewmates would lock every existing chat behind a
  // server that keeps failing (Opus, round 36). Closing it leaves the old
  // list, missing the new name until the next read; that is the honest
  // trade against a page with no way back. Over an EMPTY cached roster it
  // stays undismissable: "No crewmates yet" under "Radar was created" would
  // be two contradicting statements in one column. "Empty" is the hero's own
  // predicate (`hasNoCrewmates`), so the two can never disagree about what
  // counts as empty.
  const postCreateDismissable =
    postCreateError !== null && !hasNoCrewmates(members)
  // The crewmate whose follow-up (roster re-read, chat open) is still in
  // flight. One at a time, by design: `postCreateError` holds ONE record, so a
  // second create started inside the first one's window could overwrite the
  // first one's retry. While this is set the header "+" is held (the hero is
  // already gone once a crewmate exists), so the window cannot be entered; it
  // clears at every terminal point of the follow-up, a failed chat open
  // included.
  const [followUp, setFollowUp] = useState<CreatedCrewmate | null>(null)
  // Mirror for the thread mutation's callbacks (same reason as
  // `postCreateErrorRef`): an open's answer must read the follow-up that is
  // in flight NOW, not the one of the render that armed the callback.
  const followUpRef = useRef(followUp)
  followUpRef.current = followUp
  // The one reason every create door (header "+", both heroes) is held, or
  // `null` when creating is open. The failed-step record wins the wording:
  // it is the one with a retry the user can act on.
  const createHeld: string | null = postCreateError
    ? !postCreateDismissable
      // A roster notice over an EMPTY cached roster has no dismiss (see its
      // `onDismiss`), so its hold names only the retry; "retry or dismiss"
      // is every dismissable notice's.
      ? t('pages.membersPage.add_member_pending_roster', { name: postCreateError.created.name })
      : t('pages.membersPage.add_member_pending', { name: postCreateError.created.name })
    : followUp
      ? t('pages.membersPage.add_member_settling', { name: followUp.name })
      : loadError
        // A failed arrival read leaves the dialog's exact-name check with
        // nothing to check against (`existingNames` is empty), and the
        // post-failure reconcile in the dialog is premised on the roster
        // having been read. The door is held until a read lands; the
        // notice's Try again is the way back.
        ? t('pages.membersPage.roster_load_failed')
        : null
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
  // column beside the thread, hidden and shown by the header toggle and by the
  // panel's own close control — the chat page's panel, docked. Narrower, it is
  // an overlay the same toggle opens, because a column that narrow would fold
  // the thread to nothing. The window width is tracked live (not sampled at
  // mount) so crossing the boundary re-docks.
  const isMobile = useIsMobile()
  const [winW, setWinW] = useState(() => (typeof window !== 'undefined' ? window.innerWidth : 0))
  useEffect(() => {
    const onResize = () => setWinW(window.innerWidth)
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [])
  // The roster column is folded into the header's crewmate switcher while a
  // thread is open (crewmate-panel IA), so it reserves no width then — unless
  // the user pinned it back beside the thread from the switcher's "Show the
  // full roster" action. The switcher lists crewmates only; team headers, New
  // team, the star, the filters and the sort live on the roster column, and on
  // desktop (where the thread's back control is hidden and a bare /members
  // reopens a crewmate) this pin is the one way to reach them. It is page
  // state, not persisted: the folded roster is the desktop norm and a pin is a
  // detour from it. Picking a row keeps the pin; the roster header's close
  // control or the switcher action (now "Hide the roster") lifts it. A TEAM
  // view is not folded: it draws no switcher and its Back is `md:hidden`, so on
  // desktop the roster column is the only way out of `/members?team=<id>` and
  // stays beside the pane there, pinned or not.
  const [rosterPinned, setRosterPinned] = useState(false)
  const rosterShown = !activeName || rosterPinned
  const beside = panelSitsBeside({ winW, rosterW: rosterShown ? roster.width : 0, isMobile })
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
  // Docked visibility, persisted and cross-window synced, shown by default so
  // the page opens the way it always has. Kept SEPARATE from `overlayOpen`
  // because the two placements answer different questions — whether the
  // permanent column is wanted at all, and whether the drawer is up right now
  // — so dismissing the drawer must not also hide the column the next time the
  // window widens.
  const [memberDockedOpen, setMemberDockedOpen] = usePersistedBool(PANEL_OPEN_KEY, true)
  const [assistantDockedOpen, setAssistantDockedOpen] = usePersistedBool(ASSISTANT_PANEL_OPEN_KEY, false)
  const onAssistant = useMemo(() => {
    const open = members.find((m) => m.name === activeName)
    return !!open && isAssistantMember(open)
  }, [members, activeName])
  const dockedOpen = onAssistant ? assistantDockedOpen : memberDockedOpen
  const setDockedOpen = onAssistant ? setAssistantDockedOpen : setMemberDockedOpen
  const { panelVisible, showOpener } = panelChrome({ beside, dockedOpen, overlayOpen })
  const openProfile = useCallback((tab: ProfileTab = 'profile') => {
    setProfile((current) => ({
      tab,
      placement: isMobile || panelVisible ? 'floating' : 'column',
      openNonce: (current?.openNonce ?? 0) + 1,
    }))
  }, [isMobile, panelVisible])
  // The Schedules tab's unsaved-draft question, reachable from the exits declared
  // ABOVE where the tab itself is built. There is exactly one guard for exits that
  // unmount Profile; a ref lets those early callbacks reach the late definition
  // (`mayLeaveSchedules`) without hoisting the whole tab up here. Answers true while
  // no draft is at stake, so this is inert on every other tab.
  const schedGuardRef = useRef<() => Promise<boolean>>(async () => true)
  // Whether an unmounting exit needs to ask, answerable SYNCHRONOUSLY. Those exits
  // check this first and keep their old synchronous path when nothing is at stake:
  // routing every crewmate switch and panel reveal through a promise would make the
  // whole page's navigation async to protect a form that is usually not open.
  const schedAtStakeRef = useRef<() => boolean>(() => false)
  // Closing the card asks the Schedules draft question like every other exit. Hoisted
  // above the header because the identity pill is one of those exits: with Profile
  // floating, the pill reads `aria-expanded` and a press on it CLOSES the card rather
  // than re-opening it. Re-opening set `profile.tab` back to `profile`, and the tab
  // is part of the card's React key, so a card opened on another tab (the quiet-chat
  // Sessions link) was remounted by that press and a New schedule draft inside it
  // destroyed — with no question asked. Pointer users reach the pill rarely (the
  // floating card's scrim covers the header), keyboard users reach it every time.
  const requestCloseProfile = useCallback(() => {
    if (!schedAtStakeRef.current()) { setProfile(null); return }
    void schedGuardRef.current().then((ok) => { if (ok) setProfile(null) })
  }, [])
  // A local panel reveal goes through `togglePanel`, which can ask before it drops the
  // Profile column. A cross-tab persisted-state update cannot be declined: keep a dirty
  // Profile mounted rather than discarding its pushed schedule form. With no draft, the
  // remote reveal keeps the ordinary fold-to-pill behavior.
  const panelWasVisibleRef = useRef(panelVisible)
  useEffect(() => {
    const was = panelWasVisibleRef.current
    panelWasVisibleRef.current = panelVisible
    if (!was && panelVisible && profile?.placement === 'column') {
      if (schedAtStakeRef.current()) return
      setProfile(null)
    }
  }, [panelVisible, profile])
  // The card is ABOUT the open crewmate, so it goes when the crewmate does: a
  // switch to another crewmate, a team open, the phone's Back or a bare
  // `/members` all change `activeName`, and a card kept across that change popped
  // open by itself over the NEXT crewmate — on the old tab, since the card is
  // keyed on name + tab + accepted-open nonce (`profilePanel`). No guard here: every one of those exits
  // already asked the Schedules draft question BEFORE changing the crewmate, so
  // by the time the name moves there is nothing left to ask about. Keyed on the
  // name, not the row object: a re-POST of the same thread is not a switch.
  useEffect(() => { setProfile(null) }, [activeName])
  // Placement is decided when the card opens. A window that crosses below `md`
  // while the card holds its in-flow column would keep a fixed-width aside on a
  // phone-width viewport, so the card is re-placed as the floating one — kept,
  // not dropped. The two placements are different subtrees, so the move remounts
  // the card body; with a typed Schedules draft at stake the column is kept
  // instead, the same call the cross-tab panel reveal above makes: a misplaced
  // column beats a draft discarded with no question asked.
  useEffect(() => {
    if (!isMobile) return
    setProfile((p) => (p && p.placement === 'column' && !schedAtStakeRef.current() ? { ...p, placement: 'floating' } : p))
  }, [isMobile])
  const closeDocked = useCallback(() => setDockedOpen(false), [setDockedOpen])
  // One gesture drives whichever placement is live, so the header button and
  // the dashboard's side-panel chord share it. The chord reaches this page the
  // way it reaches the chat page: App dispatches `toggle-activity-panel` on the
  // window and the page that owns a panel listens, which keeps one binding
  // across both surfaces instead of each inventing its own.
  //
  // Both the panel and its opener live behind an open member, so the gesture
  // is gated on one too: the roster with no member open draws no panel, and a
  // toggle there would move the persisted choice with nothing on screen
  // changing, so the next member opened would come up hidden for no reason the
  // user can see. `openMemberRef` is read rather than closed over so the
  // listener binds once instead of re-binding per selection.
  const openMemberRef = useRef(false)
  const togglePanel = useCallback(() => {
    if (!openMemberRef.current) return
    // Hiding the panel leaves the Profile card mounted, so it cannot discard
    // the schedule draft now hosted there. Revealing still guards the Profile
    // fold: that path closes the card and does unmount its pushed form.
    const hiding = beside ? dockedOpen : overlayOpen
    const flip = () => {
      if (beside) setDockedOpen((v) => !v)
      else setOverlayOpen((v) => !v)
    }
    const revealAndFoldProfile = () => {
      flip()
      setProfile(null)
    }
    if (!hiding && profile) {
      if (!schedAtStakeRef.current()) { revealAndFoldProfile(); return }
      void schedGuardRef.current().then((ok) => { if (ok) revealAndFoldProfile() })
      return
    }
    flip()
  }, [beside, dockedOpen, overlayOpen, setDockedOpen, profile])
  useEffect(() => {
    const onToggle = () => togglePanel()
    window.addEventListener('toggle-activity-panel', onToggle)
    return () => window.removeEventListener('toggle-activity-panel', onToggle)
  }, [togglePanel])
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

  const active = useMemo(
    () => members.find((m) => m.name === activeName),
    [members, activeName],
  )
  // What the panel gesture reads: the panel and its opener are both drawn only
  // while a member is open, so the toggle is inert otherwise.
  useEffect(() => { openMemberRef.current = !!active }, [active])
  // Guide facts: the roster picker's selection (whether a crewmate is open and
  // whether any exists, never which), and whether the roster is shown, per
  // layout: folded into the switcher on a wide screen (`members.roster`),
  // behind the open chat on a phone (`members.roster-phone`).
  useGuideSelection('crewmate_selected', { selected: !!active, available: loaded && !hasNoCrewmates(members) })
  useGuideRevealScope(isMobile ? undefined : 'members.roster', rosterShown)
  useGuideRevealScope(isMobile ? 'members.roster-phone' : undefined, !activeName)
  // The profile card (opened from the header pill) holds the Permissions row a
  // "crewmate tools" guide points at (`members.permissions`).
  useGuideRevealScope('members.profile', !!profile)
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
      // Transcript-first, for the reason the row above states: the projection's
      // message copy is a second copy and a refused append leaves it behind.
      last_active_ts: active.last_active_ts || activeRoster.last_active_ts,
      last_message: active.last_message || activeRoster.last_message,
    }
  }, [active, activeRoster])
  // The identity the DM pane draws the crewmate's messages under. Memoised on
  // the two fields so the pane's renderer memo does not rebuild per render.
  const crewmateName = activeView?.name
  const crewmateAvatar = activeView?.avatar
  const crewmateLabel = activeView ? crewDisplayName(activeView) : undefined
  const crewmateCaptain = isAssistantMember(activeView)
  const crewmateIdentity = useMemo<CrewmateIdentity | undefined>(
    () => (crewmateName ? { name: crewmateName, avatar: crewmateAvatar, label: crewmateLabel, captain: crewmateCaptain } : undefined),
    [crewmateName, crewmateAvatar, crewmateLabel, crewmateCaptain],
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
  // render from and the list `resolveDefaultMember` searches for a remembered
  // member, so a typed filter never changes the order or which member a
  // return visit restores (that lookup is by NAME, so a re-sort cannot change
  // which member it finds).
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
  // The default crew, through the shared ['default-agent'] query (the crew
  // manager's promotion write and every `refresh` frame invalidate it). The
  // hide rule lists the default crew whatever its record says. `''` while
  // unknown: the hide rule then treats an uncreated default row like any other
  // until the answer lands. A FAILED read is `null`, even when an older value
  // is still cached (that value may name a crew that is no longer the
  // default): the hide rule then lists every row and the roster says why
  // through ErrorNotice below.
  const defaultAgentRead = useQuery(defaultAgentQuery)
  const defaultAgentFailed = defaultAgentRead.isError
  // Settled = an answer or a terminal failure (retries keep it pending).
  const defaultAgentSettled = defaultAgentRead.data !== undefined || defaultAgentRead.isError
  const defaultAgent: string | null = defaultAgentFailed ? null : defaultAgentRead.data ?? ''
  // Named apart from `rosterQuery` above: that one is the React Query READ of
  // the roster, this one is the user's filter/sort question asked of it.
  const rosterFilterQuery = useMemo<RosterQuery>(
    () => ({ search: filter, starredOnly, source: sourceFilter, status: statusFilter, sort, defaultAgent, chosen: activeName }),
    [filter, starredOnly, sourceFilter, statusFilter, sort, defaultAgent, activeName],
  )
  // The rows the roster is about right now: created crewmates and the default
  // crew, or -- with a search typed -- whatever the search reaches, hidden rows
  // included. The header count, the "N of M" and the filter menu's tallies read
  // THIS list, never `members`, so no count includes a row the user cannot see.
  const shownMembers = useMemo(() => rosterPopulation(members, rosterFilterQuery), [members, rosterFilterQuery])
  // The auto-open's candidates: only rows the roster lists unasked. A hidden
  // row can still be opened by name (a deep link, the search), but must not be
  // what the page opens on its own -- a thread standing over a roster that does
  // not show its row reads as a misroute.
  const listedMembers = useMemo(
    () => orderedMembers.filter((m) => listedByDefault(m, defaultAgent)),
    [orderedMembers, defaultAgent],
  )
  const activeSlot = active ? threadOutcome?.slot_key ?? '' : ''
  // Captain, and a crewmate just created here, speak first: once the confirmed
  // thread is open, ask the gateway for the first greeting. The server greets
  // only an empty, never-greeted thread it owes a greeting to (Captain's, or a
  // crewmate created with `first_greeting`), so this is a no-op on every later
  // open. Asked only for Captain and dashboard-created crewmates: no other row
  // can be owed one.
  const memberGreeting = useMemberFirstGreeting(
    active?.slug,
    activeSlot,
    !!active && (isAssistantMember(active) || active.dashboard_created === true),
  )
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
  // The "+" hold (`followUp`) is released by an open's outcome ONLY when the
  // hold belongs to the crewmate that open was for: re-clicking crewmate A
  // while crewmate B's follow-up is still in flight lands A's answer here, and
  // clearing unconditionally would drop B's hold mid-follow-up. A's own hold
  // (if any) is the one this open may release.
  const releaseFollowUp = useCallback((name: string) => {
    if (followUpRef.current?.name === name) setFollowUp(null)
  }, [])
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
        if (pendingOpens.current.delete(m.name)) releaseFollowUp(m.name)
        return
      }
      setThreadOutcome(m.name, () => ({ slot_key: r.slot_key }))
      // The crewmate created moments ago has its chat: the follow-up is over.
      if (pendingOpens.current.delete(m.name)) releaseFollowUp(m.name)
      if (m.slot_key !== r.slot_key) {
        queryClient.setQueryData<MemberRosterRow[]>(MEMBERS_ROSTER_QUERY_KEY, (rows) =>
          rows?.map((row) => (row.name === m.name ? { ...row, slot_key: r.slot_key, bound: true } : row)),
        )
      }
    },
    onError: (error, m, seq) => {
      if (seq !== threadReqSeq.current[m.name]) return
      // The hold comes down: the failure notice under this line is the
      // user's surface now. The greeting the server owes this crewmate is
      // still owed, so its next successful open (the re-click repair gesture,
      // the reconnect re-POST) asks for it then.
      if (pendingOpens.current.delete(m.name)) releaseFollowUp(m.name)
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
  // Files…) aimed at a foreign session. The panel falls back to its slot-free
  // Dashboard tab; Profile is separate. The thread keeps rendering the cached key under its
  // own failure notice (its pre-existing contract, see activeThreadFailed).
  const confirmedSlot =
    active && (pendingThreadFor === active.name || activeThreadFailed) ? '' : activeSlot

  // Error hand-offs addressed to Captain ("Ask <Captain>" on an error surface)
  // land in Captain's composer, appended to any unsent draft and never sent.
  // Only on the CONFIRMED thread, so a refused/foreign key never receives one.
  // Two triggers, like ChatPage's drain: arriving here (route change or a hard
  // reload), and the subscription, for a hand-off raised while this page is
  // already open on Captain (no remount happens then).
  const captainSlot = isAssistantMember(active) ? confirmedSlot : ''
  useEffect(() => {
    if (!captainSlot) return
    const drain = () => { drainCaptainHandoffs(captainSlot) }
    drain()
    return subscribeChatHandoff(drain)
  }, [captainSlot])
  // Crewmate-panel IA: the right column rests at 60% of the content row (the
  // window minus the live nav rail), clamped so the thread keeps its minimum.
  // The same number sizes the docked profile card, so card and panel swap in
  // place; a width the user dragged (persisted by SidePanel) wins over it.
  const railW = useRailWidth()
  const panelDefaultW = Math.max(SIDE_PANEL_MIN_W, Math.round((winW - railW) * 0.6))
  // The profile card: a hover card a third of the row wide (clamped to its
  // column by the style below), never the side panel's width.
  const profileCardW = Math.max(360, Math.round((winW - railW) * 0.34))
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
  const tabsCtl = usePanelTabs(activeSlot || null, panelTabDescriptors, { leadingIds: CREW_PANEL_TAB_IDS })
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
  // The DM slot's RESOLVED project directory. Not `active.workspace`: that is
  // the workspace NAME the crew record carries (a key into the configured
  // workspaces, "default" for most), and the backend resolves it to a directory
  // when it opens the member slot (`default_project_dir`), which is what the
  // slot frame's `project` holds. Handing the name to Files / Terminal rooted
  // both in a folder literally called "default".
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
  // member is editing. `summary` is withheld too: Profile Sessions and the generated
  // Dashboard already own the member-level views. So is the pinned Artifacts tab:
  // this page's standing tabs are Dashboard + Files only, while every + menu view
  // stays offered (see MEMBERS_WITHHELD_VIEWS). Until the thread is confirmed, EVERY slot-bound view is
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
  // so opening it means focusing that tab — and revealing the panel if it is
  // hidden, in either placement, since a tab behind a hidden panel is not "on
  // screen". Only the endpoint-confirmed slot may host it (the pane is keyed on
  // that same slot, so the two agree); `false` tells the selection seam the Ask
  // did NOT happen, so it never seeds a quote into a Side Chat that never
  // opened. Switching the side-panel tab leaves the Profile card mounted, so a
  // schedule draft there is not an exit and needs no discard guard.
  const openMemberSideChat = useCallback((slot: string): boolean => {
    if (!confirmedSlot || slot !== confirmedSlot) return false
    tabsCtl.openView('side')
    if (beside) setDockedOpen(true)
    else setOverlayOpen(true)
    return true
  }, [confirmedSlot, tabsCtl, beside, setDockedOpen])
  // Legacy-named ChatPane seam: the quiet-chat link opens Profile on Sessions,
  // where the sessions this crewmate is driving now live. Changing the card tab
  // remounts its pushed page, so a schedule draft takes the same guard as every
  // other Profile exit.
  const openCrewWorkLog = useCallback(() => {
    const open = () => openProfile('sessions')
    if (!schedAtStakeRef.current()) { open(); return }
    void schedGuardRef.current().then((ok) => { if (ok) open() })
  }, [openProfile])
  // The card's Workspace tile opens the panel's Files view. A docked Profile
  // column occupies the space the revealed panel needs, so this accepted exit
  // closes that card exactly like togglePanel's reveal path. A floating Profile
  // stays mounted beside the panel — and so is never asked: the draft question
  // belongs only to an exit that unmounts the card, and a "keep my draft" here
  // would otherwise block Files for a draft that was never at risk.
  const openCrewView = useCallback((view: ViewKind) => {
    const unmountsCard = profile?.placement === 'column'
    const focus = () => {
      if (unmountsCard) setProfile(null)
      tabsCtl.openView(view)
      if (beside) setDockedOpen(true)
      else setOverlayOpen(true)
    }
    if (!unmountsCard || !schedAtStakeRef.current()) { focus(); return }
    void schedGuardRef.current().then((ok) => { if (ok) focus() })
  }, [tabsCtl, beside, setDockedOpen, profile])
  // Session routing inside the DM transcript. A crewmate's prose names sessions
  // constantly -- "picked this up in `chat-2235-…`", a `/chat?sid=…` link to the
  // worker it dispatched -- and until now every one of those was inert here
  // while the same text on the chat page resolved.
  //
  // The roster is the WS `slots` frame this page already subscribes to, narrowed
  // by the shared builder to the slots the DESTINATION can render: the handler
  // navigates to the unified chat view, so a chip to anything that view drops
  // would clear itself on arrival. Withheld -- not emptied -- until a real
  // snapshot has arrived and the socket is up: absent means "this surface does
  // not know which sessions exist", which is the honest answer then, and it
  // leaves the link plain rather than live-looking and dead.
  const sessionRoster = useMemo(() => sessionTitleRoster(liveSlots), [liveSlots])
  // A foreign slot is NOT hosted in this page's own pane -- it belongs to the
  // chat page, with its sidebar, its history paging and its composer. Same
  // primitive the Driving-sessions rows use.
  const openSessionOnChatPage = useCallback(
    (key: string) => { navigate(`/chat?sid=${encodeURIComponent(key)}`) },
    [navigate],
  )
  // Reply threads (screen 07). The footer data per message is one small read
  // beside the transcript; the open thread takes over the side panel while it
  // is on screen, and closing it hands the panel's tabs back. Keyed on the
  // CONFIRMED slot only, like every other slot-bound view here.
  // Behind `dashboard.crewmate_threads` (off by default): off, no footer read
  // is made, no Reply in thread control is offered and no panel is mounted --
  // the routes answer 404 then, so a control drawn anyway would only reach a
  // refusal. Flipping the flag off closes an open thread. A config read that
  // FAILED is not "off": the flag keeps its last known value and the failure
  // is said below (ErrorNotice + Retry), so threads that are on do not vanish
  // as if the switch had been flipped.
  const {
    on: threadsOn,
    failed: threadsFlagFailed,
    retrying: threadsFlagRetrying,
    retry: retryThreadsFlag,
  } = useCrewmateThreadsFlag()
  const [openThreadMid, setOpenThreadMid] = useState<string | null>(null)
  useEffect(() => { setOpenThreadMid(null) }, [confirmedSlot, threadsOn])
  const threadsQuery = useQuery({
    queryKey: threadsQueryKey(confirmedSlot || ''),
    queryFn: () => threadsApi.summary(confirmedSlot),
    enabled: threadsOn && !!confirmedSlot,
    staleTime: 30_000,
  })
  const threadSummaries = threadsQuery.data?.threads
  const openReplyThread = useCallback((mid: string) => {
    setOpenThreadMid(mid)
    if (beside) setDockedOpen(true)
    else setOverlayOpen(true)
  }, [beside, setDockedOpen])
  const closeReplyThread = useCallback(() => setOpenThreadMid(null), [])
  const threadHooks = useMemo<ThreadHooks | undefined>(
    () => (threadsOn && confirmedSlot
      ? {
          summaryOf: (mid: string) => threadSummaries?.[mid],
          onOpen: openReplyThread,
          crewmateName: activeName,
        }
      : undefined),
    [threadsOn, confirmedSlot, threadSummaries, openReplyThread, activeName],
  )
  // Whether each leading tab's body is on screen — the gate for its data reads.
  // Read from what the panel SHOWS (`onActiveTabChange`), not from the stored
  // focus: a stored focus on a withheld view falls back to the first leading tab
  // in the strip without moving the store, and that tab must load when it is the
  // one on screen.
  const [shownTabId, setShownTabId] = useState<string | null>(null)
  // Whether the Schedules tab is holding unsaved work. Declared here rather than beside
  // its own guards because the mount decision below reads it: see `keepMountedForDraft`.
  const [schedAtStake, setSchedAtStake] = useState(false)
  // Has the user just accepted a discard? Then retention stands down for that one exit.
  // A ref, not state: the exit that sets it re-renders the page by itself (it hides the
  // panel), and the flag must already be true at that render.
  const schedReleased = useRef(false)
  // Whether the Schedules section is mounted, readable from the effect below without
  // making it depend on a value computed further down this render.
  const schedulesMountedRef = useRef(false)
  const activeTabId = shownTabId ?? tabsCtl.activeId
  // The in-chat Command Center dock is the Dynamic Dashboard, a Feature Preview
  // (Settings > Developer); off, the dock has no opener. The Dashboard TAB is the
  // crewmate's own published page (CrewDashboardFrame), not that surface, so it
  // stays a standing entry either way.
  const dashboardPreview = usePreviewFlag(PREVIEW_DASHBOARD)
  const dashboardVisible = panelVisible && activeTabId === CREW_DASHBOARD_TAB_ID
  const [dashboardVisitedFor, setDashboardVisitedFor] = useState<string | null>(null)
  useEffect(() => {
    // Once the Dashboard has been on screen for this crewmate its body stays
    // mounted across tab switches in the strip (Dashboard <-> Files), so the
    // report keeps its scroll and its minted document instead of re-minting.
    if (dashboardVisible) setDashboardVisitedFor(activeMemberKey)
  }, [dashboardVisible, activeMemberKey])
  const closeOverlay = useCallback(() => setOverlayOpen(false), [])
  // Mount continuity — the chat page's rule, verbatim: a live Browser tab (its
  // WebContentsView) or a body-owning app tab (any slot's) cannot survive a
  // remount, so while one exists a closed overlay is kept mounted and hidden
  // rather than unmounted. There is no find pane on this page.
  const hasLiveAppTab = useAnyLiveAppTab()
  const hasBrowserTab = tabsCtl.tabs.some((tab) => tab.kind === 'browser')
  // A Command Center tab is the Dynamic Dashboard preview's; off, nothing of it
  // may hold the panel.
  const hasTaskDashboard = dashboardPreview && tabsCtl.tabs.some(tab => tab.kind === 'command-center')
  const mountInput = { activityOpen: panelVisible, hasLiveAppTab, hasBrowserTab, hasTaskDashboard, searchOpen: false }
  const panelMounted = shouldMountSidePanel(mountInput)
  const panelHidden = isSidePanelHidden(mountInput)
  // File / artifact / save for the panel's Files, Artifacts and document tabs —
  // the chat page's own implementation, not a copy. A failed read is reported
  // above the thread; an open reveals whichever placement is live, since the
  // tab it focused is otherwise behind a hidden panel. Each placement arms its
  // OWN flag: arming `overlayOpen` from the docked column would leave the
  // overlay already open over the thread the next time the window narrows.
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
  const revealPanelAfterOpen = useCallback(() => {
    if (besideRef.current) setDockedOpen(true)
    else setOverlayOpen(true)
  }, [setDockedOpen])
  const { openFile, openArtifact, saveFile } = usePanelDocumentActions({
    tabsCtl,
    slotRef: activeSlotRef,
    queryClient,
    showActionError,
    onOpened: revealPanelAfterOpen,
  })
  // Transcript file links focus a side-panel tab, but the schedule draft now
  // lives in the separate Profile card. Opening a file leaves that card mounted,
  // so this compatibility wrapper deliberately does not invoke the draft guard.
  const openFileGuarded = useCallback((...args: Parameters<typeof openFile>) => {
    void openFile(...args)
  }, [openFile])
  // Opening one of the crewmate's sessions leaves `/members` for `/chat` outright, so it
  // destroys the Schedules form as surely as the identity pill does. It is a raw
  // `navigate`, which the leave channel never sees -- only callers that ask reach it --
  // so it asks here, like every other exit.
  const openSessionGuarded = useCallback((key: string) => {
    if (!schedAtStakeRef.current()) { openSessionOnChatPage(key); return }
    void schedGuardRef.current().then((ok) => { if (ok) openSessionOnChatPage(key) })
  }, [openSessionOnChatPage])
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

  const activeSlug = active?.slug ?? ''
  const activeMemberName = active?.name ?? ''
  // What a schedule created from the Schedules tab must carry in its `agent` field --
  // which is the provider template only for a crewmate whose identity the server will
  // KEEP. `wakesCrew` matches a job on `member_id` when there is one, and otherwise
  // compares `agent` against the crewmate's DISPLAY NAME. A crewmate with no persisted
  // identity has its `member_id` cleared as the job is created
  // (`cron_service/identity.py`), so writing the template into `agent` there left the new
  // schedule matching neither field and invisible on the very tab that made it. Submit
  // whatever the matcher will actually read.
  const schedAgentField = activeView && memberMemoryDisplay(activeView) === 'private'
    ? activeView.kiro_agent
    : activeMemberName
  // Schedules count behind the Schedules tab's chip. Read whenever the STRIP is
  // on screen, not when the tab is — the whole job of the badge is to answer
  // "does anything wake this mate" without opening it, and a read gated on the
  // tab being open could only ever report a count the user is already looking at.
  // Same key and same queryFn as the pane inside (`crewWakeQueryKey` +
  // `api.crons()`), so opening the tab spends no second request and the chip can
  // never disagree with the list it summarizes.
  const schedulesCountQuery = useQuery({
    queryKey: crewWakeQueryKey(activeMemberName),
    queryFn: () => api.crons(),
    enabled: (panelVisible || !!profile) && !!activeMemberName,
  })
  // This crewmate's own schedules. The `false` is `wakesCrew`'s `isDefaultCrew`
  // argument, not a claim about this crewmate: it withholds the unowned-job fallback,
  // so a job with no `member_id` and no bound agent is never claimed here even when
  // this crewmate IS the default crew — that one lives on `/schedule`, the
  // cross-crewmate view, which is also why this page never reads which crew is the
  // default. Everything `wakesCrew` attributes earlier (durable `member_id`, a bound
  // `agent`, a multi-entry `agent_sequence`) still counts, same as in the editor.
  const schedulesForActive = (schedulesCountQuery.data?.jobs || [])
    .filter((j) => wakesCrew(j, activeMemberName, false, activeSlug))
  // Unsaved work inside the Schedules tab. Refs, not state: only the guard below reads
  // them and a re-render per keystroke would remount nothing but cost the panel a pass.
  // (`schedAtStake`, the state mirror, is declared above with the mount decision that
  // also reads it.)
  const schedDraftDirty = useRef(false)
  const schedSaving = useRef(false)
  // The refs are what the synchronous guards read; the state mirror is what the published
  // navigation stake and the `beforeunload` listener need, since both live in effects.
  const setSchedDraftDirty = useCallback((d: boolean) => {
    schedDraftDirty.current = d
    setSchedAtStake(d || schedSaving.current)
  }, [])
  const setSchedSaving = useCallback((s: boolean) => {
    schedSaving.current = s
    setSchedAtStake(s || schedDraftDirty.current)
  }, [])
  const { confirm: confirmSched, confirmDialog: schedConfirmDialog } = useConfirm()
  /** The section's own collapse toggle, which it cannot guard itself. */
  const requestCancelSchedDraft = useCallback((proceed: () => void) => {
    void (async () => {
      const ok = await confirmSched({
        title: t('pages.kiroCrewAgentsPage.discard_new_schedule'),
        body: schedSaving.current ? t('pages.kiroCrewAgentsPage.discard_anyway_note') : undefined,
        confirmLabel: t('pages.kiroCrewAgentsPage.discard_schedule_confirm'),
      })
      if (ok) proceed()
    })()
  }, [confirmSched, t])
  /** The user accepted a discard, so RETENTION stands down for the exit that asked --
   *  and only retention. The draft's own flags are left alone, so it stays guarded until
   *  the form actually unmounts and reports itself clean.
   *
   *  Retention has to stand down before the exit runs, not after: `keepMountedForDraft`
   *  is what would hold the panel mounted, and waiting for the unmount to clear the flag
   *  that is preventing that unmount keeps the panel hidden and mounted for good.
   *  Clearing the draft flags instead was the first version of this, and it disarmed the
   *  page: with two dirty surfaces on the leave channel, accepting the schedule's
   *  question and then vetoing the other one left a visible, unguarded draft to be lost
   *  on some later exit. A release is a statement about one exit; the draft is still a
   *  draft until it is gone. */
  const releaseSchedRetention = useCallback(() => { schedReleased.current = true }, [])
  // Expose it to `onCrewDeleted` (declared above): a confirmed crew delete stands
  // down the schedule retention so the panel unmount is an acknowledged discard.
  releaseSchedRef.current = releaseSchedRetention
  // The release is good for the exit that asked and no further. If the form is still
  // mounted on the next render, that exit did not happen -- a guard after this one
  // vetoed it -- so retention arms again for whatever comes next. Runs after every
  // render on purpose: it is answering "did the exit land", which only the next render
  // knows.
  useEffect(() => {
    if (schedReleased.current && schedulesMountedRef.current) schedReleased.current = false
  })
  /** Asked by the strip before it switches away from Schedules; see `onBeforeLeave`. */
  const mayLeaveSchedules = useCallback(async () => {
    // A create request in flight is never discardable: unmounting the form does not
    // cancel the POST, so the "discarded" schedule would persist. The section disables
    // its own cancel for exactly this window.
    if (schedSaving.current) return false
    if (!schedDraftDirty.current) return true
    const ok = await confirmSched({
      title: t('pages.kiroCrewAgentsPage.discard_new_schedule'),
      confirmLabel: t('pages.kiroCrewAgentsPage.discard_schedule_confirm'),
    })
    if (ok) releaseSchedRetention()
    return ok
  }, [confirmSched, t, releaseSchedRetention])
  // The overlay scrim hides only the side panel. Profile is a sibling and stays
  // mounted, so closing the overlay is not a schedule-draft exit.
  const requestCloseOverlay = useCallback(() => {
    closeOverlay()
  }, [closeOverlay])
  // ONE guard for exits that unmount Profile. The ref is what lets callbacks declared
  // earlier in this component (the header's panel reveal, a roster crewmate switch)
  // reach it.
  //
  // The gate is the section being MOUNTED, not visible. The dirty flag is only
  // meaningful while the form exists, and a stale true would confirm-prompt over a tab
  // with no form -- but draft-destroying exits must stay armed while a hidden Profile
  // is being retained: hidden is exactly when the draft has nowhere else to live.
  const schedulesMounted = !!profile
  schedulesMountedRef.current = schedulesMounted
  schedGuardRef.current = useCallback(async () => {
    // An in-flight editor write must not be abandoned: refuse the leave outright
    // until it settles (the request could still land and persist a "discarded"
    // change), matching requestClose's mid-commit refusal.
    if (crewModalBusyRef.current) return false
    // The modal's unsaved panes are a second draft this page can lose on an
    // exit it does not own. The async guard backs the in-app exits (sidebar
    // clicks and session opens); confirm before discarding modal edits, the
    // same question the Schedules draft asks.
    if (crewModalAtStakeRef.current && !window.confirm(t('pages.membersPage.edit_leave_draft'))) return false
    if (!schedulesMounted) return true
    return mayLeaveSchedules()
  }, [schedulesMounted, mayLeaveSchedules, t])
  schedAtStakeRef.current = useCallback(
    () => schedulesMounted && (schedSaving.current || schedDraftDirty.current),
    [schedulesMounted],
  )
  // Leaving the ROUTE is the last exit, and the registry that owns it is synchronous, so
  // it cannot use the app's confirm dialog. `window.confirm`, exactly as the New crewmate
  // dialog's own guard does on this page for the same reason. A create in flight refuses
  // outright here too: the POST would land with the page gone.
  useRegisterNavigationLeaveGuard(() => {
    // Refuse a route leave while an editor write is in flight (see the async
    // guard): the request could still persist a change the user discarded.
    if (crewModalBusyRef.current) return false
    // Leaving the route also abandons an open editor modal's unsaved panes.
    // Same synchronous `window.confirm` the Schedules draft uses here, since
    // the registry cannot await the app's confirm dialog.
    if (crewModalAtStakeRef.current && !window.confirm(t('pages.membersPage.edit_leave_draft'))) return false
    if (!schedulesMounted) return true
    if (schedSaving.current) return false
    if (!schedDraftDirty.current) return true
    const ok = window.confirm(t('pages.membersPage.schedules_leave_draft'))
    if (ok) releaseSchedRetention()
    return ok
  })
  // Registering a guard is not enough on its own: `NavigationBackGuard` arms off the
  // published STAKE, not off the guard, so without this the browser's own Back button
  // discarded the draft silently while every wired in-app exit asked. The New crewmate
  // dialog on this same page already publishes, which is what made the gap uneven rather
  // than merely absent.
  const anyDraftAtStake = schedAtStake || crewModalAtStake || crewModalBusy
  usePublishNavigationStake(anyDraftAtStake)
  // A reload or a tab close is not a route change, so the guard above never sees it; the
  // browser's own prompt is the only thing that can. Registered while a Schedules draft,
  // the editor modal's unsaved panes, or an in-flight editor write is at stake, since an always-on `beforeunload` nags on every ordinary close.
  useEffect(() => {
    if (!anyDraftAtStake) return
    const onUnload = (e: BeforeUnloadEvent) => { e.preventDefault(); e.returnValue = '' }
    window.addEventListener('beforeunload', onUnload)
    return () => window.removeEventListener('beforeunload', onUnload)
  }, [anyDraftAtStake])
  // Mounting a member thread IS reading it, but nothing on this page moves
  // `chat.activeSlot` (that transition belongs to the Sessions page's
  // switchSlot, the only other markSlotRead caller). Two things follow:
  //
  // 1. The websocket unread-marker must learn about the open thread another
  //    way, or it flags every message that lands in it. It reads
  //    `viewedThread` beside `chat.activeSlot`; the visible-view effect below
  //    registers the mounted thread there. Before this, each arrival was
  //    flagged and drained a render later, and both writes relayed to the
  //    parent dashboard's crew tab -- a badge that lit and vanished on every
  //    message.
  // 2. A flag that was set while the thread was NOT on screen (closed, or
  //    this window hidden) still has to be drained when it opens or is
  //    revealed. Without this the rail badge is permanent -- no code path
  //    clears a live member slot's unread until the slot itself is deleted.
  const dispatch = useAppDispatch()
  const activeSlotUnread = useAppSelector(
    (s) => !!activeSlot && s.dashboard.unreadSlots.includes(activeSlot),
  )
  const activeSlotLastTs = useAppSelector(
    (s) => (activeSlot ? s.dashboard.slots.find(sl => sl.key === activeSlot)?.last_ts : undefined),
  )
  // The identity pill's second line — what the crewmate is doing now. Its
  // busy readings are the slot's live status line (`slotStatusDetail`), the
  // SAME record the sessions sidebar and the command palette render through
  // `toolStatusLabel`, so the pill names a moment the way the sidebar row
  // does and honours the `simplifiedToolNames` preference. Each read is
  // memo-safe on its own (a string, a boolean or a stable entry ref), so the
  // header does not re-render on every WS frame; `resolvePillActivity` folds
  // them at render time. The key falls back to the roster's slot_key the same
  // way the avatar does, so a thread whose confirmed slot has not resolved yet
  // still reads live.
  const pillSlotKey = activeSlot || active?.slot_key || ''
  const pillStreamState = useAppSelector((s) => (pillSlotKey ? selectSlotStreamState(s, pillSlotKey) : 'idle'))
  const pillDetail = useAppSelector((s) => (pillSlotKey ? s.chat.slotStatusDetail[pillSlotKey] : undefined))
  // Whether the tool call the status describes has RETURNED: the status seam
  // keeps the call's label until the next status frame, but once its output
  // is in the tool log the model is reading it, and the pill says so. Matched
  // by the call's own id, so parallel calls cannot be confused, and tested
  // with `!== undefined`: an empty output is still a return.
  const pillToolReturned = useAppSelector((s) => {
    const d = pillSlotKey ? s.chat.slotStatusDetail[pillSlotKey] : undefined
    if (d?.kind !== 'tool' || !d.toolCallId) return false
    const entry = selectSlotToolLog(s, pillSlotKey).findLast((e) => e.type === 'tool' && e.tool_call_id === d.toolCallId)
    return entry !== undefined && entry.output !== undefined
  })
  const pillLiveSlot = useAppSelector((s) => (pillSlotKey ? s.dashboard.slots.find((sl) => sl.key === pillSlotKey) : undefined))
  const simplifiedToolNames = useSimplifiedToolNames()
  const uiLang = useLanguage().resolved
  const pillLabelOf = useCallback(
    (detail: ToolStatusDetail) => toolStatusLabel(detail, simplifiedToolNames, uiLang),
    [simplifiedToolNames, uiLang],
  )
  // The resting line's age ("Idle · 6m ago") is on screen for as long as the
  // thread rests, so it must move on its own: re-read the clock on the
  // drawer's coarse tick while the pill is resting, and not at all while it is
  // busy (the busy line carries no age). A separate clock from the Schedules card, because it runs under a different
  // condition.
  const pillResting = !!active && !isRunning(active) && pillStreamState === 'idle'
  const pillLastActive = (activeView ?? active)?.last_active_ts
  const [pillIdleAge, setPillIdleAge] = useState('')
  useEffect(() => {
    if (!pillResting || !pillLastActive) { setPillIdleAge(''); return }
    const read = () => setPillIdleAge(timeAgo(pillLastActive))
    read()
    const timer = setInterval(read, PATROL_TICK_MS)
    return () => clearInterval(timer)
    // `uiLang` is a deliberate extra dep: the age is a formatted string in the
    // UI language, so a language switch must re-read it at once rather than on
    // the next tick.
  }, [pillResting, pillLastActive, uiLang])
  const pillActivity = useMemo(() => {
    const act = resolvePillActivity({
      streamState: pillStreamState,
      detail: pillDetail,
      toolReturned: pillToolReturned,
      running: !!active && !!isRunning(active),
      delegatedOnly: !!pillLiveSlot?.subagents_running && !pillLiveSlot?.running,
      labelOf: pillLabelOf,
    })
    const label = act.text !== undefined
      ? act.text
      : act.kind === 'idle' && pillIdleAge
        ? t('pages.membersPage.pill_idle_since', { when: pillIdleAge })
        : t(PILL_ACTIVITY_KEY[act.kind as Exclude<PillActivityKind, 'tool' | 'thinking'>])
    return { kind: act.kind, label }
  }, [pillStreamState, pillDetail, pillToolReturned, active, isRunning, pillLiveSlot, pillLabelOf, t, pillIdleAge])
  // Reactive document visibility AND focus, so the read effect below re-runs
  // when the user returns to a hidden tab or focuses the window — a plain
  // document.hidden read would leave the effect settled and the reveal
  // unnoticed, and Page Visibility alone calls occluded/unfocused windows
  // "visible", which would let a parked window mark threads read.
  const [pageVisible, setPageVisible] = useState(() => !document.hidden && document.hasFocus())
  useEffect(() => {
    const onVis = () => setPageVisible(!document.hidden && document.hasFocus())
    document.addEventListener('visibilitychange', onVis)
    window.addEventListener('focus', onVis)
    window.addEventListener('blur', onVis)
    return () => {
      document.removeEventListener('visibilitychange', onVis)
      window.removeEventListener('focus', onVis)
      window.removeEventListener('blur', onVis)
    }
  }, [])
  // A creation surface (guided flow or Advanced form) hides the chat while it
  // stays mounted: hidden is not viewed, so it neither reads nor suppresses.
  const threadShown = pageVisible && !createOpen
  useEffect(() => {
    // Viewing the thread is the read — but only a VISIBLE view is a view. A
    // hidden member tab neither clears its own badge nor relays one; on
    // reveal this effect re-runs via pageVisible and does both. The relay is
    // NOT gated on this window's own badge: this window may have read the
    // thread earlier (locally clean) while another window's badge is still
    // lit, and a visible view here retires that too. Throttled per slot by
    // the relay module; watermarked at the slot's newest known message ts.
    if (activeSlot && threadShown) {
      if (activeSlotUnread) dispatch(markSlotRead(activeSlot))
      emitSlotRead(activeSlot, activeSlotLastTs)
    }
  }, [activeSlot, activeSlotUnread, threadShown, activeSlotLastTs, dispatch])
  // Tell the unread-marker which thread is on screen, for exactly as long as
  // it is: registered while the thread is mounted AND this window is visible
  // and focused, retired on switch, hide, blur and unmount. A hidden window's
  // open thread therefore badges like any other slot, and the read effect
  // above drains it on reveal -- same visibility bar for both directions.
  useEffect(() => {
    if (!activeSlot || !threadShown) return
    setViewedThreadSlot(activeSlot)
    return () => {
      // Like switchSlot, flush before retiring the view so its trailing read
      // timer cannot outlive it and clear a later, unseen message's badge.
      flushSlotRead(activeSlot)
      clearViewedThreadSlot(activeSlot)
    }
  }, [activeSlot, threadShown])

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
  // copy would be wrong then, since the roster is not empty. Judged against
  // the shown population: hidden rows are not "filtered out", they are unlisted.
  const filteredOut =
    loaded && !loadError && shownMembers.length > 0 && sortedMembers.length === 0 && !filter.trim()
  // Every crewmate exists but the listing rule hides them all (none chatted,
  // created here, starred or the default crew): say so and name the search as
  // the way in, rather than an empty list under "0 crewmates".
  const allHidden =
    loaded && !loadError && !filter.trim() && shownMembers.length === 0 && !hasNoCrewmates(members)
  // Relative times in the Profile Schedules tab move while the card is open.
  const [nowTs, setNowTs] = useState(() => Date.now() / 1000)
  useEffect(() => {
    if (!profile) return
    setNowTs(Date.now() / 1000)
    const timer = setInterval(() => setNowTs(Date.now() / 1000), 60_000)
    return () => clearInterval(timer)
  }, [profile])
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
      if (remember && m.name !== 'default') safeSetItem(LAST_MEMBER_KEY, m.name)
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

  /** Opens `m`, or answers `false` when the Schedules guard refuses to let go of a
   *  dirty draft. A caller that parked state on this open HAPPENING must read that
   *  answer: `openCreated` holds the create button on the open releasing it, and a
   *  refused guard opens no thread to do it. Everything but the
   *  guard's own await runs synchronously, so a switch with nothing at stake still
   *  lands in one tick. */
  const openMember = useCallback(
    async (m: MemberRosterRow): Promise<boolean> => {
      if (!leaveCreate()) return false
      // Re-clicking the open member is the repair gesture (re-POST); the URL
      // is unchanged so the sync effect would not fire — call through. It is
      // also an explicit choice of that member, so a swap notice still
      // standing over it (the user was routed here from a dead link) has
      // been acknowledged: retire it.
      if (m.name === activeName) {
        activate(m)
        setGone(null)
        return true
      }
      const go = () => {
        if (urlMember || !isMobile) {
          // Switching between members while one is open REPLACES the entry, and
          // so does opening one above md, where the roster and the thread sit
          // side by side and an open is not a navigation step. Either way the
          // page holds one history entry however many members are visited and
          // Back leaves it in one press — the Sessions sidebar's rule. The
          // breakpoint is named directly because the desktop half used to ride
          // on `urlMember` always being set by the arrival auto-open: an EMPTY
          // roster leaves the URL bare (nothing to open), and the open that
          // follows the first create must still replace.
          setSearchParams({ [MEMBER_PARAM]: m.name }, { replace: true })
          return
        }
        // Entering a thread from the roster below md — the one place where the
        // roster IS the page and no member is open — is a step in a two-level
        // navigation, so it is PUSHED. The state marks the entry as pushed from
        // this page's roster, which is what lets the below-md back button pop
        // instead of replace.
        setSearchParams({ [MEMBER_PARAM]: m.name }, { state: { fromRoster: true } })
      }
      // A switch to ANOTHER crewmate remounts the Schedules section (it is keyed on the
      // crewmate), so it destroys an open create form just as leaving the tab does. It
      // used to do that silently, which is what made "every exit asks" untrue. Nothing
      // at stake keeps the switch synchronous, exactly as before.
      if (!schedAtStakeRef.current()) { go(); return true }
      const ok = await schedGuardRef.current()
      if (ok) go()
      return ok
    },
    [activeName, urlMember, isMobile, activate, setSearchParams, leaveCreate],
  )

  // A crewmate was just created in this page's dialog: close it and open its
  // chat; the hold rides that open (openThread's outcome releases it). The roster is re-read BEFORE the URL names the new crewmate,
  // so the URL sync effect finds it — a name not yet on the roster would read
  // as "gone" and open someone else in its place. A FAILED re-read must not
  // take that path either: react-query keeps the stale roster as `res.data`
  // on error, so the name would be missing for the wrong reason. It is
  // reported instead, with a retry that repeats exactly this step.
  const openCreated = useCallback(async (created: CreatedCrewmate) => {
    setPostCreateError(null)
    setFollowUp(created)
    // A re-read that did not actually land is a failed re-read. The star
    // mutation's `cancelQueries` (above) can cut this refetch short, and a
    // cancelled query REVERTS to its previous successful state: `refetch()`
    // then resolves without `isError`, carrying the pre-create roster, and
    // the new name would read as absent and someone else's chat opened. Only a response newer than what we had counts as fresh.
    const before = queryClient.getQueryState(MEMBERS_ROSTER_QUERY_KEY)?.dataUpdatedAt ?? 0
    const res = await rosterQuery.refetch()
    // The user left while the roster was re-reading: nothing below may run.
    // The URL write would drag the page they chose back here.
    if (!pageMounted.current) return
    const fresh = !res.isError && res.dataUpdatedAt > before
    if (!fresh) {
      setFollowUp(null)
      setPostCreateError({ kind: 'roster', created })
      return
    }
    const hit = res.data?.find((r) => r.name === created.name)
    if (hit) {
      // The hold now rides the thread open: released by openThread's
      // onSuccess or onError.
      pendingOpens.current.add(created.name)
      if (await openMember(hit)) return
      pendingOpens.current.delete(created.name)
      // The Schedules guard refused: the user kept a dirty draft, so no thread
      // opens and nothing is left to release the hold — the create button would
      // stay disabled for the rest of the page's life. Drop the hold here; the
      // crewmate IS on the roster now, and its first open asks for the
      // greeting the server owes it.
      setFollowUp(null)
      return
    }
    // The re-read landed without the name (the server accepted a name the
    // roster's read does not list). The URL write below lets the URL sync
    // effect say "gone"; the hold is dropped rather than kept for a row that
    // is not coming.
    setFollowUp(null)
    setSearchParams({ [MEMBER_PARAM]: created.name }, { replace: true })
  }, [rosterQuery, openMember, setSearchParams, queryClient])

  const handleCreated = useCallback((created: CreatedCrewmate) => {
    // The form's work is done: the open that follows must not ask to leave it.
    createOpenRef.current = false
    setCreateOpen(false)
    void openCreated(created)
  }, [openCreated])

  const retryPostCreate = useCallback(() => {
    const failed = postCreateError
    if (!failed) return
    setPostCreateError(null)
    void openCreated(failed.created)
  }, [postCreateError, openCreated])

  // URL -> open crewmate. Once the roster is in: a URL that names a crewmate
  // opens it; a URL that names none (a fresh visit, the sidebar entry, a
  // reload) is REPLACED with the remembered crewmate if one is still on the
  // roster, so returning users land back on the conversation they left, else
  // with the most recently USED one, else the built-in Assistant
  // (`resolveDefaultMember`) — the default follows the user's own history,
  // never the sort order (#11763). "Nothing
  // to open" therefore means an EMPTY roster, and only that. A URL naming a
  // crewmate that is gone (deleted or renamed) falls back the same way, with
  // a one-line notice above the chat naming the swap — the user asked for
  // someone specific, and a silently mounted other chat is the misroute this
  // page exists to prevent. Below md the page is a two-level list->detail
  // navigation: a bare visit lands like md+, the roster is the EXPLICIT
  // `?view=roster` (what the header Back writes) and never auto-opens, and a
  // gone crewmate in the URL returns to that roster instead of bouncing the
  // phone user into a different crewmate's chat.
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
    if (urlTeam) {
      // A team is open in the main pane: no thread stands beside it, and the
      // remembered-member fallback below must not overrule an explicit team
      // link. A link naming a team that is gone (deleted elsewhere) returns to
      // the bare roster once the team list has answered.
      if (teamsQ.data !== undefined && !teamsQ.data.some((tm) => tm.id === urlTeam)) {
        setSearchParams(isMobile ? { view: 'roster' } : {}, { replace: true })
        return
      }
      if (activeName) {
        activeNameRef.current = ''
        setActiveName('')
      }
      return
    }
    if (isMobile && (urlMember || searchParams.get('view') === 'roster')) {
      if (urlMember) {
        // No thread to fall back to below md — the roster is the answer, so
        // say where the member went above the list (shown: '' marks the
        // roster variant of the notice).
        setGone((prev) =>
          prev && prev.name === urlMember && prev.shown === '' ? prev : { name: urlMember, shown: '' },
        )
        setSearchParams({ view: 'roster' }, { replace: true })
      } else if (activeName) {
        activeNameRef.current = ''
        setActiveName('')
      }
      return
    }
    // URL names no crewmate (or names a gone one; below md only a bare visit
    // gets here): restore the
    // remembered crewmate if it is still listed, else open the most recently
    // used listed one. `undefined` here means nothing is listed — the chat
    // column shows the New crewmate hero instead (or, with only hidden rows,
    // stays empty until a search or a link names one).
    // The fallback reads the LISTED rows, and the listing rule exempts the
    // default crew by name: until the default-crew read has settled (data or
    // a terminal error) the remembered crewmate may be that unlisted default,
    // and resolving now would open a substitute and overwrite the memory.
    if (!defaultAgentSettled) return
    // A remembered crewmate the user opened themselves is restored even when the
    // listing rule hides it (it was reached through the search): it is listed
    // again while open (`chosen`), and resolving among listed rows only would
    // open a substitute and overwrite the memory. Only with no usable memory
    // does the fallback pick among the listed rows -- and when nothing but the
    // default crew is listed while hidden crewmates exist, it opens the most
    // recently used of those (listed while open), so the page never lands on an
    // empty pane or claims there are no crewmates.
    const remembered = safeGetItem(LAST_MEMBER_KEY)
    const rememberedRow =
      remembered && remembered !== 'default' ? members.find((m) => m.name === remembered) : undefined
    const target =
      rememberedRow ?? resolveDefaultMember(null, listedMembers) ?? resolveDefaultMember(null, orderedMembers)
    if (!target) {
      // Named a gone crewmate on an empty roster: say where they went above
      // the roster (shown: '' marks the roster variant of the notice, as
      // below md) and clear the URL back to the bare list.
      if (urlMember) {
        setGone((prev) =>
          prev && prev.name === urlMember && prev.shown === '' ? prev : { name: urlMember, shown: '' },
        )
        setSearchParams(isMobile ? { view: 'roster' } : {}, { replace: true })
      }
      // Nothing to open means nothing may STAY open — the same clear the
      // below-md branch does: a chat can still be mounted for a crewmate the
      // roster no longer lists (deleted while open), and returning to a bare
      // `/members` from there (the crew editor's exit, the rail's Crewmates
      // row) would otherwise leave that chat standing over a URL that names
      // no one, next to the empty roster's hero.
      if (activeName) {
        activeNameRef.current = ''
        setActiveName('')
      }
      return
    }
    if (urlMember) {
      setGone((prev) =>
        prev && prev.name === urlMember && prev.shown === target.name
          ? prev
          : { name: urlMember, shown: target.name },
      )
      goneStandInRef.current = target.name
    }
    setSearchParams({ [MEMBER_PARAM]: target.name }, { replace: true })
  }, [loaded, loadError, urlMember, urlTeam, teamsQ.data, members, listedMembers, activeName, isMobile, activate, setSearchParams, rosterQuery.isFetching, defaultAgentSettled, orderedMembers, searchParams])

  // Team open: the header row's click. Same history rule as openMember -- one
  // entry above md or while something is already open, a PUSHED step from the
  // bare roster below md so the back button pops.
  const openTeam = useCallback(
    (id: string) => {
      if (!leaveCreate()) return
      const go = () => {
        if (urlMember || urlTeam || !isMobile) {
          // The replace keeps the entry's own state: below md this branch is
          // reached from the team view itself (Edit team -> Save re-opens the
          // saved id over `?team=`), and dropping `fromRoster` there would send
          // the next Back to a second copy of the roster instead of off the
          // page -- closeTeamView reads that flag to choose between the two.
          setSearchParams({ [TEAM_PARAM]: id }, { replace: true, state: location.state })
          return
        }
        setSearchParams({ [TEAM_PARAM]: id }, { state: { fromRoster: true } })
      }
      // Opening a team clears the open crewmate, which unmounts the whole panel subtree
      // and with it any create form on the Schedules tab. A team header row is a click
      // away from that tab, so it asks first.
      if (!schedAtStakeRef.current()) { go(); return }
      void schedGuardRef.current().then((ok) => { if (ok) go() })
    },
    [urlMember, urlTeam, isMobile, setSearchParams, location.state, leaveCreate],
  )
  const closeTeamView = useCallback(() => {
    if ((location.state as { fromRoster?: boolean } | null)?.fromRoster) navigate(-1)
    else setSearchParams({ view: 'roster' }, { replace: true })
  }, [location.state, navigate, setSearchParams])
  // The roster, grouped: the DISPLAYED rows (after search / filter / sort) under
  // their team headers, in the teams' stored order, unlisted rows last.
  // Team headers are judged against the rows the roster is ABOUT (`shownMembers`),
  // not the whole roster: a team whose every crewmate is hidden still gets its
  // header (empty), so the team view, its Needs-you inbox and its editor stay
  // reachable.
  const rosterGroups = useMemo(
    () => groupRosterByTeam(teams, sortedMembers, shownMembers),
    [teams, sortedMembers, shownMembers],
  )
  const grouped = teams.length > 0
  // The open team's crewmates for the team view -- the WHOLE team in roster
  // order, not the filtered rows: a search typed into the roster narrows the
  // list, not the team. Each carries the live readings the roster rows resolve.
  const teamMembers = useMemo<TeamMemberInput[]>(() => {
    if (!activeTeam) return []
    const onTeam = new Set(activeTeam.members)
    return orderedMembers
      .filter((m) => onTeam.has(m.name))
      .map((m) => {
        const slotKey = slotKeyOf(m)
        return {
          row: m,
          slotKey,
          running: !!isRunning(m),
          needsInput: !!(slotKey && liveNeedsYou[slotKey]),
          slugCollides: collidingSlugs.has(m.slug),
        }
      })
  }, [activeTeam, orderedMembers, slotKeyOf, isRunning, liveNeedsYou, collidingSlugs])

  // The post-create notice (a failed roster re-read) with its retry, rendered
  // ONCE, in the chat column: below md a roster notice brings that column on
  // screen (see the two `className`s).
  const postCreateNotice = postCreateError && (
    /* No hand-off: a chat may already be mounted under this line with
       a draft in its composer (ChatPane keeps it in local state), and
       the hand-off navigates away, unmounting it. The retry repeats the
       one step that failed; the create itself already succeeded. */
    <div
      // With no chat open the notice is the whole column's content: centred
      // like the hero it replaced, so the empty pane reads as intended, not
      // broken.
      className={!active
        ? 'flex flex-1 flex-col items-center justify-center gap-3 px-6 py-10 text-center animate-rise'
        : 'flex flex-wrap items-center gap-2 px-4 py-2'}
      data-testid="member-post-create-error"
    >
      {/* The retry sits right after the text, not at the far edge of a
          stretched notice: the two read as one sentence, and its label
          names the one step it repeats ("Refresh your crewmates") so it
          cannot read as "create Radar again". The dismiss follows
          `postCreateDismissable`: over an empty cached roster there is
          none — closing it would put "No crewmates yet" under a crewmate
          that exists, and the retry is the only honest way off it; over a
          roster with other rows it can be closed, so the existing chats
          stay reachable below md. */}
      <ErrorNotice
        message={t('pages.membersPage.create_roster_failed', { name: postCreateError.created.name })}
        variant="inline"
        onDismiss={postCreateDismissable ? () => setPostCreateError(null) : undefined}
        className="min-w-0"
      />
      <Btn onClick={retryPostCreate} className="shrink-0" data-testid="member-post-create-retry">
        {t('pages.membersPage.create_retry_roster')}
      </Btn>
    </div>
  )

  return (
    // No bottom inset on the root: the card columns carry their own pb-2 and
    // the side panel brings the chat SidePanel's mb-2, so all three end 8px
    // above the window edge without stacking two insets. No right padding
    // either — the panel docks FLUSH to the window's right edge, exactly as it
    // does in the chat page's actbar column; the card columns' pr-2 lives on
    // the inner wrapper below.
    // This page has no bottom row for the side panel, so every panel glyph
    // in its transcripts draws the right-dock pane.
    <SidePanelDockHost value={false}>
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
        // Below md the roster is the whole width, so it yields whenever the
        // chat column must be seen: an open chat, or a ROSTER post-create
        // notice (a failed re-read opens no chat, and a full-width `shrink-0`
        // roster would push the notice and its retry off-screen). A GREETING
        // notice does not imply an open chat — the header back can close the
        // chat under it — so it alone must not hide the roster, or a phone
        // shows a notice bar over a blank column until it is dismissed. An
        // open team view takes the column the same way an open chat does.
        // Pinned from the switcher, the column returns beside the thread at
        // md and up only: below md the back control is the way to the roster.
        // A team view keeps the column at md and up whether or not it is
        // pinned: the team header draws no switcher and its own Back is
        // `md:hidden`, so without the column a desktop `/members?team=<id>`
        // had no way back to the roster.
        className={`${
          activeName && !activeTeam && !rosterPinned
            ? 'hidden'
            : activeName || activeTeam || createOpen || postCreateError
              ? 'hidden md:flex'
              : 'flex'
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
              reads bare, because this header names a page, not a pane.
              The icon is the same two-ghost brand mark the nav rail draws
              for this page (`components/CrewMemberMark.tsx`), so the rail
              row and the page it opens name the thing with one glyph. */}
          <div className="flex items-center gap-1.5 min-w-0 flex-1 pl-1.5">
            <CrewMemberMark size={15} className="inline-block text-muted shrink-0" />
            <h1 className={LIST_TITLE_CLS}>{t('pages.membersPage.title')}</h1>
          </div>
          {/* Two things can be added here, so the "+" opens a menu: a crewmate
              or a team (the dialogs below). The trigger keeps the bare Plus
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
              not the menu's: New team has no part in the follow-up. */}
          {/* The whole menu is hidden while the empty roster's hero is the
              create door: two doors to one dialog read as two different
              actions (a team of no one has nothing to group yet either). It
              returns with the first row, when the hero is gone. Also hidden
              until the first read has answered: the names the dialog checks
              a new one against are unknown until then. */}
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
                  <span>{t('pages.membersPage.add_member')}</span>
                  {createHeld !== null && (
                    <span className="text-[11.5px] text-muted whitespace-normal" data-testid="member-add-crewmate-held">
                      {createHeld}
                    </span>
                  )}
                </span>
              </DropdownMenuItem>
              <DropdownMenuItem onSelect={() => setTeamDialog({})} data-testid="member-add-team" {...uiLocation('members.add-menu.new-team')}>
                <Users size={13} className="lucide-inline text-muted" aria-hidden="true" />
                <span className="flex-1">{t('pages.membersPage.team_new')}</span>
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
          )}
          {/* The pinned roster's own way out, beside a thread on desktop — a
              second close for the pin the switcher's footer action also lifts.
              Not in a team view: the column stands there on its own, so
              lifting the pin would change nothing on screen. md+ only, like
              the pin itself. */}
          {rosterPinned && activeName && (
            <button
              type="button"
              onClick={() => setRosterPinned(false)}
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
              unknown. */}
          {loadError || postCreateError?.kind === 'roster'
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
        {teamsFailed && (
          <div className="px-4 pb-2">
            <ErrorNotice
              message={t('pages.membersPage.teams_load_failed')}
              variant="inline"
              askAgent
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
        {/* Default-crew lookup failed: the hide rule is off (every row listed),
            and this says why in localized copy, never the raw server text.
            Mounted only while failing, like the star error. */}
        {defaultAgentFailed && (
          <div className="px-2">
            <ErrorNotice
              message={t('pages.membersPage.default_agent_failed_title')}
              report={findReport(defaultAgentRead.error instanceof Error ? defaultAgentRead.error.message : undefined)}
              askAgent
              actionPlacement="below"
              testId="member-default-agent-error"
            />
          </div>
        )}
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
             in the gone member's place: below md a stale link always lands
             here, and on desktop a gone `?member=` with nothing remembered
             now does too (#11763) rather than mounting a stranger's thread.
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
              <CrewmateEmptyHero onCreate={openQuick} onAdvanced={openAdvanced} held={createHeld} />
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
          {rosterGroups.map((group) => {
            const collapsed = grouped && collapsedTeams.has(group.id)
            return (
              <Fragment key={group.id}>
                {grouped && (
                  <TeamGroupHeader
                    group={group}
                    selected={!!activeTeam && group.id === activeTeam.id}
                    collapsed={collapsed}
                    onOpen={() => group.team && openTeam(group.team.id)}
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
                      openMember={openMember}
                      toggleStar={toggleStar}
                      starPending={starPending}
                      slotKeyOf={slotKeyOf}
                      isRunning={isRunning}
                      isUnread={isUnread}
                      isNeedsYou={isNeedsYou}
                      activePatrolOf={activePatrolOf}
                      reduceMotion={reduceMotion}
                      scrollActiveRowIntoView={scrollActiveRowIntoView}
                      slugCollides={collidingSlugs.has(m.slug)}
                      indented={grouped}
                    />
                  ))}
              </Fragment>
            )
          })}
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

      {/* DM thread */}
      <section
        // Below md the column shows only while a chat or a team view is open —
        // except while a ROSTER post-create notice is up: a failed re-read
        // opens no chat, and hiding the column would hide the one place the
        // failure and its retry are said.
        className={`${activeName || activeTeam || postCreateError ? 'flex' : 'hidden md:flex'} flex-1 min-w-0 flex-col min-h-0 relative`}
        ref={threadColumnRef}
        // The page's main column: the thread (or the team view, or an empty
        // state), and never the side panel, which is a sibling of this section.
        // Named so a case can ask what the THREAD says without matching the
        // panel's own copy — the two surfaces share several sentences, "Opening
        // the conversation…" among them, and each says it about itself.
        data-testid="member-main-column"
        style={createOpen ? { display: 'none' } : undefined}
        onClickCapture={(event) => {
          if (!isAssistantMember(active) || event.button !== 0 || event.metaKey || event.ctrlKey || event.altKey || event.shiftKey) return
          const anchor = event.target instanceof Element ? event.target.closest('a[href]') : null
          if (!anchor) return
          const draft = createDraftFromHref(anchor.getAttribute('href'), window.location.href)
          if (!draft) return
          event.preventDefault()
          event.stopPropagation()
          openCreate(false, draft)
        }}
      >
        {postCreateNotice}
        {/* The hero yields to a post-create notice: after the FIRST create a
            failed re-read leaves the cached roster at [] while the crewmate
            exists, and "No crewmates yet" under "Radar was created" would be
            two contradicting statements in one column. It yields to an open
            team view too: a team can exist before its first crewmate does,
            and the pane is the team's then. */}
        {!active && !activeTeam && !postCreateError && loaded && !loadError && hasNoCrewmates(members) && (
          <CrewmateEmptyHero onCreate={openQuick} onAdvanced={openAdvanced} held={createHeld} />
        )}
        {/* A failed roster read at md and up: the roster column shows its own
            notice, but this column would otherwise be blank — no chat can
            open (the URL sync waits on the roster) and the hero is gated on a
            successful read. Say the same failure here, so a wide window is
            never half empty. Hidden below md, where this column is not shown
            and the roster's notice is the screen. An open team view carries
            its own copy of this notice (below), so it steps aside then. */}
        {!active && !activeTeam && !postCreateError && loadError && (
          <div className="hidden md:flex flex-1 flex-col items-center justify-center gap-3 px-6 py-10 text-center" data-testid="member-column-load-error">
            <ErrorNotice
              message={t('pages.membersPage.roster_load_failed')}
              variant="inline"
              askAgent
              // Named, not "the agent": on a page of crewmates a bare "agent"
              // reads as a third party, and the hand-off goes unused.
              askAgentLabel={t('pages.membersPage.roster_load_failed_ask')}
            />
            {/* Same shape as the post-create roster notice: the plain retry
                under the sentence, the hand-off link beside it as the second
                option, never the only one. */}
            <Btn onClick={() => void rosterQuery.refetch()} data-testid="member-column-load-retry">
              {t('pages.membersPage.roster_load_retry')}
            </Btn>
          </div>
        )}
        {/* The team view takes the pane a chat would: the manager's desk for
            the open team. Keyed by team so switching teams remounts its reads. */}
        {!active && activeTeam && (
          <>
            {/* The roster column -- and the notice it carries -- is hidden below
                md while a team is open, so a failed read is said HERE too: the
                team on screen may be stale. The pane's copy steps out of the way
                where the roster's own copy is on screen: the roster stands beside
                the pane at md and up in a team view, so the pane's notice hides
                there (`md:hidden`), exactly the column's own hidden range. */}
            {loadError && (
              <div className="px-4 pt-3 md:hidden">
                <ErrorNotice
                  message={t('pages.membersPage.roster_load_failed')}
                  variant="inline"
                  askAgent
                  testId="team-view-roster-error"
                />
              </div>
            )}
            {teamsFailed && (
              <div className="px-4 pt-3 md:hidden">
                <ErrorNotice
                  message={t('pages.membersPage.teams_load_failed')}
                  variant="inline"
                  askAgent
                  testId="team-view-teams-error"
                />
              </div>
            )}
            <TeamView
              key={activeTeam.id}
              team={activeTeam}
              members={teamMembers}
              rosterFailed={loadError}
              onOpenMember={openMember}
              onEdit={() => setTeamDialog({ team: activeTeam })}
              onBack={closeTeamView}
            />
          </>
        )}
        {active && (
          <>
            {/* No rule under the header: it shares the transcript's background
                and is set off by spacing alone, the way ChatPage's session
                header sits over its transcript (bg-bg, no border-b). A hairline
                here read as a second frame inside the pane (issue #9425).
                Three columns, the outer two equal, so the identity pill in the
                middle is centred on the pane whether or not the back button
                (narrow) or the panel opener (docked, panel hidden) is present:
                a flex row with `flex-1` around the pill would shift it by the
                width of whichever side control is missing. */}
            <header className="grid grid-cols-[1fr_minmax(0,auto)_1fr] items-center gap-2 px-3 py-2" data-testid="member-thread-header">
              <div className="flex items-center justify-start min-w-0 gap-1">
                {/* The roster, folded into one chip (crewmate-panel IA): stacked
                    faces and the count; open, a searchable list that switches
                    the thread. Hidden below md, where the phone's back button
                    returns to the full-width roster instead. It carries the
                    same per-row signals the roster's status filters read
                    (`signalsOf`), so a crewmate parked on a question stays
                    visible while the roster column is folded away. */}
                <CrewmateSwitcher
                  className="hidden md:flex"
                  members={orderedMembers}
                  activeName={active.name}
                  signals={signalsOf}
                  onPick={(name) => {
                    const m = orderedMembers.find((row) => row.name === name)
                    if (m) void openMember(m)
                  }}
                  onCreate={createHeld ? undefined : openQuick}
                  rosterShown={rosterPinned}
                  onToggleRoster={() => setRosterPinned((v) => !v)}
                />
                <button
                  // Back to the roster. When this entry was pushed from the
                  // roster on this page, pop it — the browser's own Back then
                  // lands on whatever preceded the roster, with no duplicate
                  // roster entry. A deep link (no such state) has no roster
                  // entry behind it, so drop the param in place instead.
                  onClick={() => {
                    const go = () => {
                      if ((location.state as { fromRoster?: boolean } | null)?.fromRoster) navigate(-1)
                      else setSearchParams({ view: 'roster' }, { replace: true })
                    }
                    // Clearing the member param unmounts the panel subtree with the
                    // Schedules form in it, so this asks like every other exit. The
                    // replace branch is the one that needed it most: a replace raises no
                    // `popstate`, so neither the published stake nor `NavigationBackGuard`
                    // can see it, and a deep-linked crewmate on a narrow window reaches
                    // it with an ordinary tap.
                    if (!schedAtStakeRef.current()) { go(); return }
                    void schedGuardRef.current().then((ok) => { if (ok) go() })
                  }}
                  className="md:hidden inline-flex items-center p-1 -ml-1 rounded hover:bg-accent/40"
                  aria-label={t('pages.membersPage.back_to_roster')}
                  data-testid="member-back"
                  {...uiLocation('members.back')}
                >
                  <ArrowLeft size={16} className="lucide-inline" />
                </button>
              </div>
              {/* The identity pill is one centred Glass chip holding the face,
                  name and activity line. It opens Profile, not the editor; the
                  card's pencil opens the shared CrewEditorDialog. While Profile
                  owns an in-flow column the pill unmounts and the face travels to
                  the card through its shared layoutId. With SidePanel open, Profile
                  floats and the pill remains. The accessible name stays the
                  crewmate; the tooltip describes the Profile action. */}
              {profile?.placement === 'column' ? (
                /* The card holds its own column and names the crewmate itself; the
                   pill steps out and its face travels to the card's head (shared
                   layoutId). An empty middle cell keeps the header's grid. */
                <span aria-hidden="true" data-testid="member-identity-pill-placeholder" />
              ) : (
              <Glass
                as="button"
                type="button"
                variant="chip"
                radius={999}
                // Profile is the read surface; its pencil owns the editor door.
                // A toggle, as `aria-expanded` says: with the card already open the
                // press closes it (through the draft guard — see requestCloseProfile)
                // instead of re-opening on the Profile tab, which remounted the card.
                onClick={() => { if (profile) requestCloseProfile(); else openProfile('profile') }}
                className="glass-shadow flex items-center gap-2.5 pl-2.5 pr-3 py-1.5 min-w-0 max-w-full justify-self-center cursor-pointer text-left focus-ring"
                title={t('pages.membersPage.profile_card')}
                aria-expanded={!!profile}
                data-testid="member-identity-pill"
                {...uiLocation('members.edit')}
              >
                {/* The same reactive CrewStateAvatar as before — a plain face,
                    no scrim, no badge (issue #9425). */}
                {/* The face is a shared layout element: when the card docks and the
                    pill steps out, the same face slides into the card's head. */}
                <motion.span layoutId={CREW_FACE_LAYOUT_ID} className="flex shrink-0 rounded-full">
                  <CrewStateAvatar
                    seed={active.name}
                    avatar={active.avatar}
                    slotKey={activeSlot || active.slot_key}
                    running={!!isRunning(active)}
                    size={30}
                    working="full"
                  />
                </motion.span>
                <div className="min-w-0 leading-tight">
                  {/* Title row = name (+ the ID when a label covers it). */}
                  <div className="min-w-0 flex items-center gap-1.5" data-testid="member-title-row">
                    {/* The ID stays visible when a label covers it — routes, crons
                        and spawn params address the ID, never the label. A label
                        that differs from the ID only in case ("Kiro" over "kiro")
                        covers nothing, so the line is withheld and the ID rides
                        the name's tooltip instead. */}
                    {(() => {
                      const label = crewDisplayName(active)
                      // The built-in Assistant's ID beside its label reads as
                      // noise; it is still shown in Settings.
                      const idShown = !isAssistantMember(active) && label.toLowerCase() !== active.name.toLowerCase()
                      const idTip = t('components.agentSelector.agent_id_tooltip', { name: active.name })
                      return (
                        <>
                          <div
                            className="text-[13.5px] font-semibold truncate max-w-[24rem]"
                            title={!idShown && label !== active.name ? idTip : undefined}
                            data-testid="member-pill-name"
                          >{label}</div>
                          {idShown && (
                            <div className="text-[11px] font-mono text-muted truncate max-w-[11rem]" title={idTip} data-testid="member-pill-id">{active.name}</div>
                          )}
                        </>
                      )
                    })()}
                  </div>
                  {/* Activity line — what the crewmate is doing right now, text
                      only (the face above already carries presence, so no dot
                      here). Always rendered, so the pill keeps one height
                      whether the crewmate is busy or resting: a resting line
                      says how long ago the thread last moved. A busy line is
                      the shared status label, clamped in `pillActivity.ts`;
                      `truncate` is the belt to that cap's braces. Out of the
                      button's accessible name: the name is WHO the thread is
                      with, and this line changes several times a turn — the
                      screen-reader copy sits outside the button, below. */}
                  <div
                    className="text-[11px] text-muted truncate max-w-[24rem]"
                    data-testid="member-pill-activity"
                    data-activity={pillActivity.kind}
                    aria-hidden="true"
                  >{pillActivity.label}</div>
                </div>
                {/* The one visible sign that this chip OPENS something. Without
                    it the pill and the switcher chip beside it are two glass
                    chips with faces in them, and only one of them is a door to
                    the Profile. Decorative: the tooltip and `aria-expanded`
                    already say it for assistive tech. */}
                <ChevronRight size={14} className="lucide-inline shrink-0 text-muted" aria-hidden="true" data-testid="member-identity-pill-chevron" />
              </Glass>
              )}
              {/* The panel's opener. Same icon and hit-target as the chat
                  page's side-panel toggle, so the two surfaces teach one
                  gesture, and the dashboard's side-panel chord fires it too.
                  Docked, it appears only while the panel is hidden — the open
                  panel's own strip carries the close control, which is exactly
                  how the chat page splits the two halves of the gesture. As an
                  overlay it stays put and reads pressed while the drawer is up.
                  The pin chip was removed: every member thread is pinned by
                  construction (a server invariant, not a per-thread state), so
                  announcing it taught the user a term for a thing that can
                  never be otherwise. The member's edit entry is not a peer of
                  this toggle: it is the identity pill in the middle. */}
              <div className="flex items-center justify-end min-w-0">
                {/* The activity line for assistive tech: the same text, outside
                    the button so it never joins the crewmate's name, and NOT a
                    live region — a line that changes several times a turn
                    would otherwise be announced on every change. It is in the
                    reading order for a reader who asks. */}
                <span className="sr-only" data-testid="member-pill-activity-sr">{pillActivity.label}</span>
                {showOpener && (
                  <button
                    onClick={togglePanel}
                    className="flex items-center justify-center w-7 h-7 rounded-md transition-colors bg-transparent border-none shrink-0 text-muted hover:text-text hover:bg-bg-hover cursor-pointer"
                    aria-pressed={panelVisible}
                    aria-controls="member-side-panel"
                    aria-label={t('pages.membersPage.panel_toggle')}
                    title={t('pages.membersPage.panel_toggle')}
                    data-testid="member-panel-toggle"
                    {...uiLocation('members.details')}
                  >
                    <PanelRightSolid size={15} />
                  </button>
                )}
              </div>
            </header>
            {/* The roster is folded into the switcher while a DM is open, so
                every roster-only failure also needs a visible copy above the
                thread. When the roster is pinned back on desktop its own copy
                is visible and this wrapper steps aside; below md the roster is
                hidden regardless of the pin. No hand-off: the Profile card may
                hold an unsaved schedule draft, and Ask the agent navigates away. */}
            {(patrol.failed || teamsFailed || defaultAgentFailed || starError) && (
              <div
                className={`${rosterPinned ? 'md:hidden' : ''} flex flex-col gap-2 px-4 pb-2`}
                data-testid="member-main-roster-errors"
              >
                {patrol.failed && (
                  <ErrorNotice
                    message={t('pages.membersPage.patrol_error_roster')}
                    variant="inline"
                    askAgent={false}
                    testId="member-main-patrol-error"
                  />
                )}
                {teamsFailed && (
                  <ErrorNotice
                    message={t('pages.membersPage.teams_load_failed')}
                    variant="inline"
                    askAgent={false}
                    testId="member-main-teams-error"
                  />
                )}
                {defaultAgentFailed && (
                  <ErrorNotice
                    message={t('pages.membersPage.default_agent_failed_title')}
                    report={findReport(defaultAgentRead.error instanceof Error ? defaultAgentRead.error.message : undefined)}
                    askAgent={false}
                    actionPlacement="below"
                    testId="member-main-default-agent-error"
                  />
                )}
                {starError && (
                  <ErrorNotice
                    message={starError.message}
                    report={starError.report}
                    title={t('pages.membersPage.star_failed_title')}
                    onDismiss={() => setStarError(null)}
                    askAgent={false}
                    actionPlacement="below"
                    testId="member-main-star-error"
                  />
                )}
              </div>
            )}
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
            {/* The editor's roster read (crewAgentsQuery, enabled only once the
                identity pill sets editingCrew) failed: without this the pill would
                be a silent dead click — editingAgent stays undefined, the hook's
                `open` never flips, and CrewEditorDialog returns null forever with no
                report (F1). No hand-off: this notice renders ONLY when the roster
                read failed, so editingAgent is undefined and dirtyPanes is always
                empty here — a `dirtyPanes.size === 0` guard is therefore inert and
                would make the hand-off unconditionally on, silently discarding the
                ChatPane DM composer draft and the panel's Schedules create draft via
                the raw `/chat` navigate that skips NavigationLeaveGuard. Like the two
                neighbouring notices, this one does not hand off. */}
            {editingCrew && crewAgentsQuery.isError && (
              <ErrorNotice
                title={t('pages.membersPage.roster_load_failed')}
                message={crewAgentsQuery.error instanceof Error ? crewAgentsQuery.error.message : String(crewAgentsQuery.error)}
                askAgent={false}
                onDismiss={closeCrewEditor}
                testId="member-crew-roster-load-error"
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
            {threadsFlagFailed && (
              /* The config read behind the reply-threads flag failed. Said
                 here, not rendered as "threads are off": with nothing cached
                 the Reply control is withheld (the routes would refuse), with
                 a cached value the last reading stands; either way the user
                 can ask for the read again in place. No hand-off: the DM
                 composer below holds an unsaved draft. */
              <div className="px-4 py-2 flex items-start gap-2" data-testid="member-threads-flag-error-row">
                <ErrorNotice
                  message={t('pages.chat.thread.err_flag_failed')}
                  variant="inline"
                  className="flex-1 min-w-0"
                  testId="member-threads-flag-error"
                />
                <Btn
                  disabled={threadsFlagRetrying}
                  onClick={retryThreadsFlag}
                  className="shrink-0"
                  data-testid="member-threads-flag-retry"
                >
                  <RotateCw className="lucide-inline" aria-hidden />
                  {t('pages.chat.thread.retry')}
                </Btn>
              </div>
            )}
            {threadsOn && threadsQuery.isError && (
              /* The per-message reply counts failed to load: the chat itself is
                 fine and stays mounted below, so this says only what is
                 missing (the footers) and offers the read again in place. No
                 hand-off, for the reason the notices around it give: the DM
                 composer below holds an unsaved draft. */
              <div className="px-4 py-2 flex items-start gap-2" data-testid="member-threads-error-row">
                <ErrorNotice
                  message={t('pages.chat.thread.err_summary_failed')}
                  variant="inline"
                  className="flex-1 min-w-0"
                  testId="member-threads-error"
                />
                <Btn
                  disabled={threadsQuery.isFetching}
                  onClick={() => { void threadsQuery.refetch() }}
                  className="shrink-0"
                  data-testid="member-threads-retry"
                >
                  <RotateCw className="lucide-inline" aria-hidden />
                  {t('pages.chat.thread.retry')}
                </Btn>
              </div>
            )}
            {memberGreeting.failed && (
              /* No hand-off: the Captain composer below stays mounted and can
                 already hold what the user typed (ChatPane keeps that draft in
                 local state); the hand-off navigates away and would unmount
                 it. The chat itself still works, so this says only that the
                 greeting did not start and offers it again in place. */
              <div className="px-4 py-2 flex items-start gap-2" data-testid="member-greeting-error-row">
                <ErrorNotice
                  message={t('pages.membersPage.greeting_failed')}
                  variant="inline"
                  className="flex-1 min-w-0"
                  testId="member-greeting-error"
                />
                <Btn
                  disabled={memberGreeting.retrying}
                  onClick={memberGreeting.retry}
                  className="shrink-0"
                  data-testid="member-greeting-retry"
                >
                  <RotateCw className="lucide-inline" aria-hidden />
                  {t('pages.chat.thread.retry')}
                </Btn>
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

                      Default (split) busy mode: a send while the member is
                      working offers the same Steer / Queue / Jev auto choice
                      as the main chat, so the user can queue a follow-up
                      instead of always interrupting the running turn. */}
                  <ChatPane
                    slotKey={activeSlot}
                    agentLocked
                    frameless
                    followContentWidth
                    // The failure notice above owns the verdict on this thread
                    // while a repair has failed; the pane's own "Session
                    // ready" would contradict it one line down.
                    hideEmptyHint={activeThreadFailed}
                    crewmate={crewmateIdentity}
                    onOpenCrewWorkLog={openCrewWorkLog}
                    openSideChat={openMemberSideChat}
                    threads={threadHooks}
                    onFileOpen={openFileGuarded}
                    onSessionOpen={openSessionGuarded}
                    sessions={connected && slotsLoaded ? sessionRoster : undefined}
                    activeSession={activeSlot}
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
      <div className={createOpen ? 'flex flex-1 min-w-0 min-h-0 flex-col overflow-y-auto' : 'hidden'} data-testid="member-create-pane">
        {/* The New crewmate card, in place of the chat. */}
        <div className={createOpen ? 'flex flex-1 min-w-0 min-h-0 justify-center p-4' : 'hidden'} data-testid="member-create-advanced">
          <NewCrewmateDialog
            embedded
            open={createOpen}
            startExpanded={createExpanded}
            initialDraft={createDraft}
            guided
            onDraftStateChange={onCreateDraftState}
            firstGreeting
            onClose={() => setCreateOpen(false)}
            onCreated={handleCreated}
            existingNames={existingNames}
          />
        </div>
      </div>
      </div>

      {/* Side panel — the chat page's tabbed SidePanel, docked to this page.
          Read-only observation lives in its permanent first tab (Crew
          summary); writes live in the crew manager. The + menu is the chat
          panel's own (Files / Artifacts / Terminal / Browser / Side chat …),
          all against the member's DM slot, and the strip is bucketed per
          member so it follows the roster selection. Wide windows dock it as a
          column the strip's own close control hides, with the header opener
          taking the gesture back while it is hidden; narrow ones make it an
          overlay the header button opens, with the same close control, on the
          chat page's dock motion. */}
      <div className={createOpen ? 'hidden' : 'contents'}>
      {active && (() => {
          // Notes — the crewmate's own standing notes, read-only (no editor:
          // see CrewNotesTab). Its briefing read is gated on the tab being on
          // screen, like the work log's reads.
          const notesBody = activeSlug && activeMemberName ? (
            <CrewNotesTab
              slug={activeSlug}
              member={activeMemberName}
              displayName={crewDisplayName(active)}
              header={null}
              visible
            />
          ) : null
          // One Dashboard: the existing crew publication is one task view.
          // Preserve its renderer and exact member identity, while the host
          // owns live task summaries, questions and approval controls.
          // The Dashboard tab IS the crewmate's published view (crewmate-panel IA):
          // the HTML report the crewmate writes itself, rendered straight into the
          // panel. No card, Contained bar or Expand around it (CrewDashboardFrame,
          // not the CrewWebview drawer) and no Command Center above it: each read
          // as one more container stacked over the one page that matters.
          const dashboardBody = (
            <div className="h-full min-h-0 flex flex-col" data-testid="member-dashboard" aria-label={t('pages.membersPage.dashboard_tab')}>
              {/* Said here only when the MAIN COLUMN is not already saying it:
                  that column renders the same sentence while it has no pane
                  (`activeSlot` unset), and two live regions saying it at once
                  on every cold open is one too many. */}
              {!confirmedSlot && !activeThreadFailed && activeSlot
                ? <p role="status" className="px-4 pt-3 text-sm text-muted">{t('pages.membersPage.opening_thread')}</p>
                : null}
              {activeSlug && activeMemberName && (
                <CrewDashboardFrame slug={activeSlug} member={activeMemberName} displayName={crewDisplayName(activeView ?? active)} />
              )}
            </div>
          )
          // Schedules — what wakes THIS crewmate without anyone asking, and nothing
          // else. The body is the crew editor's own pane (`CrewWakeSection`), scoped
          // with `ownedOnly`. Not a copy: one schedules editor exists in the product
          // and this is a second mount of it, so the create form, the
          // member-immutability rule and the Global Memory V1 attribution behave
          // identically on both surfaces. A schedule belonging to no crewmate is not
          // this crewmate's business and stays on `/schedule`.
          const schedulesBody = activeMemberName ? (
            <div className="px-3 py-3" data-testid="member-schedules" aria-label={t('pages.membersPage.schedules_tab')}>
              {/* Keyed per crewmate so a member switch REMOUNTS the section. Without
                  it the open create form survives the switch and keeps whatever was
                  typed, while its `memberId` becomes the new crewmate -- submitting
                  one crewmate's draft as another's schedule. */}
              <CrewWakeSection
                key={activeMemberName}
                crew={activeMemberName}
                // The crew's IMMUTABLE id, which is what a private schedule's
                // `member_id` holds. Not the display name: they differ for any
                // crewmate whose name is not already its own slug, and passing the
                // name made every such crewmate read as having no schedules and hid
                // a job the moment it was created here. See `wakesCrew`.
                memberId={activeSlug}
                // The job's `agent` field, which has to be passed: `JobForm` falls back
                // to `''`, so omitting it persisted a created schedule with no agent at
                // all and `/schedule` labelled this crewmate's own job as the default
                // crew's. The prop is named for the editor's case, where this IS always
                // the provider template; here it is the template only for a crewmate
                // whose identity persists. See `schedAgentField`.
                agentTemplate={schedAgentField}
                // This crewmate's own schedules only: `ownedOnly` drops `wakesCrew`'s
                // unowned-job fallback and nothing else, so a job attributed by
                // `member_id`, by `agent`, or by an `agent_sequence` still lists here
                // exactly as it does in the editor. See `WakeScope`.
                ownedOnly
                initialAdding
                // The pushed page's bar already says "New schedule" and holds the
                // one Back control, which runs the draft guard below. The
                // section's own heading and its Cancel X would be a second title
                // and a second exit on the same short page.
                chromeless
                // The panel column is ~460px on a wide screen, where every `sm:` /
                // `md:` promotion in the section fires and truncates a job name to
                // about ten characters. The host knows its own width; the section
                // cannot ask about it with viewport breakpoints.
                dense
                heading={t('pages.membersPage.schedules_heading')}
                blurb={t('pages.membersPage.schedules_blurb')}
                emptyLine={t('pages.membersPage.schedules_empty')}
                // The create form's pinned-crew hint names the crew EDITOR; this
                // page is the crewmate's Profile, so it says so in its own noun.
                pinnedHint={t('pages.membersPage.schedules_pinned_hint')}
                onDraftChange={setSchedDraftDirty}
                onSavingChange={setSchedSaving}
                onRequestCancel={requestCancelSchedDraft}
              />
            </div>
          ) : null
          // The card's Sessions tab: the sessions this crewmate is driving — the
          // worker sessions it opened and steers — and nothing else. Rows use live slots, the sidebar's status vocabulary, and a jump into
          // the session, in the card's row idiom.
          const sessionsBody = (
            <div className="flex flex-col gap-3" data-testid="crew-profile-sessions">
              {drivingSessions.length === 0 && !slotsLoaded ? (
                <div className="space-y-2" aria-hidden>
                  <div className="h-12 rounded-2xl bg-bg-hover animate-pulse" />
                  <div className="h-12 rounded-2xl bg-bg-hover animate-pulse" />
                </div>
              ) : drivingSessions.length === 0 ? (
                <div className="rounded-2xl border border-dashed border-border-strong bg-bg px-4 py-8 text-center text-[13px] text-muted" data-testid="crew-profile-sessions-empty">
                  {t('pages.membersPage.driving_none')}
                </div>
              ) : (
                <ul className="list-none m-0 p-0 rounded-2xl border border-border bg-bg overflow-hidden" data-testid="crew-profile-driving">
                  {visibleDriving.map((s) => {
                    const kind = tabStatus(s, [], s.key)
                    const status = DRIVING_STATUS[kind]
                    const label = t(status.label)
                    const activityTs = lastActivityEpoch(s)
                    const title = s.title || s.key
                    return (
                      <li key={s.key} className="border-t border-border first:border-t-0">
                        <button
                          type="button"
                          onClick={() => openSessionGuarded(s.key)}
                          className="w-full flex items-center gap-3 px-3.5 py-3 text-left hover:bg-bg-hover transition-colors cursor-pointer"
                          title={title + PROJECT_SEPARATOR + label}
                          data-testid="crew-profile-driving-row"
                          data-status={kind}
                        >
                          <Circle size={9} className={`shrink-0 ${status.cls}`} aria-hidden />
                          <span className="flex-1 min-w-0 leading-tight">
                            <span className="block text-[13px] font-semibold truncate">{title}</span>
                            <span className="block text-[11.5px] text-muted truncate">
                              {label}{activityTs > 0 ? `${PROJECT_SEPARATOR}${timeAgo(activityTs)}` : ''}
                            </span>
                          </span>
                          <ChevronRight size={15} className="text-muted shrink-0" aria-hidden="true" />
                        </button>
                      </li>
                    )
                  })}
                </ul>
              )}
              {drivingSessions.length > DRIVING_VISIBLE && (
                <button
                  type="button"
                  onClick={() => setDrivingExpandedFor(drivingExpanded ? '' : activeMemberKey)}
                  className="self-start text-[12.5px] font-semibold text-accent cursor-pointer"
                  aria-expanded={drivingExpanded}
                >
                  {drivingExpanded
                    ? t('pages.membersPage.driving_show_less')
                    : t('pages.membersPage.driving_show_all', { count: drivingSessions.length })}
                </button>
              )}
            </div>
          )
          const requestProfileBack = (proceed: () => void) => {
            if (!schedAtStakeRef.current()) { proceed(); return }
            void schedGuardRef.current().then((ok) => { if (ok) proceed() })
          }
          // The profile card (crewmate-panel IA): the pill's surface. Notes,
          // Schedules and the work log are ITS tabs/pages now, so the three
          // bodies above mount here instead of on the side panel's strip.
          const profilePanel = activeView && profile ? (
            <CrewProfilePanel
              key={`${active.name}:${profile.tab}:${profile.openNonce}`}
              member={activeView}
              running={!!isRunning(active)}
              slotKey={activeSlot || active.slot_key}
              description={profileRegistryQuery.data?.agents.find((a) => a.name === active.name)?.description ?? ''}
              descriptionError={profileRegistryQuery.isError}
              memoryLabel={t(PROFILE_MEMORY_KEYS[memberMemoryDisplay(activeView)])}
              initialTab={profile.tab}
              schedules={schedulesForActive}
              schedulesLoading={schedulesCountQuery.isPending}
              schedulesError={schedulesCountQuery.isError}
              nowTs={nowTs}
              onOpenSchedule={() => { const destination = '/schedule'; leave(() => navigate(destination), destination) }}
              onOpenScheduleJob={(id) => { const destination = `/schedule?job=${encodeURIComponent(id)}`; leave(() => navigate(destination), destination) }}
              newScheduleBody={schedulesBody}
              sessionsBody={sessionsBody}
              notesBody={notesBody}
              faceLayoutId={profile.placement === 'column' ? CREW_FACE_LAYOUT_ID : undefined}
              onClose={requestCloseProfile}
              onRequestBack={requestProfileBack}
              onEdit={() => setEditingCrew(active.name)}
              onOpenFiles={() => openCrewView('files')}
            />
          ) : null
          // The card is a hover card either way — rounded on every corner, a third
          // of the row wide, the side panel's height. WHERE it sits depends on the
          // side panel and viewport: with the panel hidden on a non-phone viewport,
          // the card takes its own column and the thread gives up the width (the
          // pill is hidden then, the card's head names the crewmate); with the panel
          // open or on a phone, the card floats centred over the DM column (its
          // containing block, which is exactly the chat width the side panel leaves),
          // and a click outside it closes it. No scrim: the thread stays readable
          // beside it.
          const profileDocked = !!profilePanel && profile?.placement === 'column'
          // The column reveals on the chat page's width axis (so the thread
          // narrows instead of jumping), while the face slides over from the pill.
          const dockedSurface = (
            <AnimatePresence initial={false}>
              {profileDocked && (
                <motion.aside
                  key="crew-profile-docked"
                  initial={{ width: 0, opacity: 0 }}
                  animate={{ width: profileCardW + 8, opacity: 1 }}
                  exit={{ width: 0, opacity: 0 }}
                  transition={{ duration: 0.26, ease: [0.2, 0.8, 0.2, 1] }}
                  className="h-full shrink-0 flex flex-col min-h-0 pb-2 pr-2 overflow-hidden"
                  data-testid="crew-profile-docked"
                >
                  <div className="flex-1 min-h-0 rounded-2xl border border-border bg-bg-elevated shadow-lg overflow-hidden flex flex-col" style={{ width: profileCardW }} data-testid="crew-profile-card">{profilePanel}</div>
                </motion.aside>
              )}
            </AnimatePresence>
          )
          const profileSurface = profileDocked ? dockedSurface : profilePanel && threadColumnRef.current ? createPortal(
            <div
              className="absolute inset-0 z-30 flex justify-center px-2"
              role="presentation"
              onClick={(e) => { if (e.target === e.currentTarget) requestCloseProfile() }}
              data-testid="crew-profile-modal"
            >
              <div
                className="h-full rounded-2xl border border-border bg-bg-elevated shadow-2xl overflow-hidden flex flex-col animate-in zoom-in-95 fade-in-0 duration-200"
                style={{ width: `min(${profileCardW}px, calc(100% - 16px))` }}
                data-testid="crew-profile-card"
              >{profilePanel}</div>
            </div>,
            threadColumnRef.current,
          ) : null
          // The side panel's host tabs: the dynamic Dashboard only. The Workspace
          // file browser is the one chat view left unwithheld (see
          // MEMBERS_WITHHELD_VIEWS); together they are the whole strip.
          const leadingTabs: SidePanelLeadingTab[] = [
            {
              id: CREW_DASHBOARD_TAB_ID,
              title: t('pages.membersPage.dashboard_tab'),
              icon: <LayoutDashboard className="lucide-inline" aria-hidden="true" />,
              keepMounted: dashboardVisitedFor === activeMemberKey,
              render: () => dashboardBody,
            },
          ]
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
            // The member is the chat's identity while its slot is still being
            // confirmed, so a resize started before the POST answers lands on
            // the confirmed key, and one that spans a member switch does not.
            slotOwner: active?.name,
            // Sizes are SAVED only under the confirmed key: `activeSlot` holds
            // its last good key through a refusal, and that key now belongs to
            // another session, whose remembered size a drag here must not take.
            persistSlot: confirmedSlot,
            hiddenViews,
            onActiveTabChange: setShownTabId,
            projectDir,
            onFileOpen: openFile,
            onArtifactOpen: openArtifact,
            onFileSave: saveFile,
            leadingTabs,
            slotTitle: crewDisplayName(activeView ?? active),
            canDockBottom: false,
            defaultWidth: panelDefaultW,
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
          return (<>
            {profileDocked ? profileSurface : <>{dockedSurface}{profileSurface}</>}
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
                    /* Both placements are dismissable, and the overlay carries a
                       second dismiss on top of the strip's close: the scrim,
                       the drawer convention. On a phone the panel is
                       handed the window width (`fillWidth`) so it fills the
                       scrim; on a tablet-width window the panel keeps its own
                       (resizable, persisted) width against the dimmed chat. */
                    : 'fixed top-safe-offset-[42px] bottom-safe left-safe right-safe z-40 flex justify-end bg-bg/60 backdrop-blur-xs'}
                  style={panelHidden ? { display: 'none' } : undefined}
                  // The scrim dismisses only the side panel. The Profile card is a
                  // sibling and stays mounted, including any schedule draft it holds.
                  onClick={beside ? undefined : (e) => { if (e.target === e.currentTarget) requestCloseOverlay() }}
                  data-testid="member-side-panel"
                  data-placement={beside ? 'docked' : 'overlay'}
                >
                  <motion.div
                    initial={innerMotion.initial}
                    animate={innerMotion.animate}
                    exit={innerMotion.exit}
                    transition={{ duration: 0.18, ease: [0.2, 0, 0, 1] }}
                    className={beside ? 'h-full flex justify-end relative' : 'h-full flex justify-end max-w-full relative'}
                  >
                    {/* The open reply thread covers the panel's tabs while it is on
                        screen and slides away on close, so the tabs the user had are
                        where they left them. `mb-2` + `rounded-l-xl` match the
                        panel's own frame (SidePanel's root) so the thread reads as
                        the panel showing something else, not a second panel. */}
                    <AnimatePresence initial={false}>
                      {threadsOn && openThreadMid && confirmedSlot && (
                        <motion.div
                          key={`thread-${openThreadMid}`}
                          initial={reduceMotion ? { opacity: 1 } : { x: 24, opacity: 0 }}
                          animate={{ x: 0, opacity: 1 }}
                          exit={reduceMotion ? { opacity: 0 } : { x: 24, opacity: 0 }}
                          transition={{ duration: 0.18, ease: [0.2, 0, 0, 1] }}
                          className={`absolute inset-0 z-20 overflow-hidden ${beside ? 'mb-2 rounded-l-xl border-l border-t border-b border-border' : ''}`}
                          style={beside ? { inset: 0, bottom: 8 } : undefined}
                        >
                          <ThreadPanel
                            slot={confirmedSlot}
                            mid={openThreadMid}
                            crewmateName={activeName}
                            crewmateLabel={crewmateLabel}
                            onClose={closeReplyThread}
                          />
                        </motion.div>
                      )}
                    </AnimatePresence>
                    <SidePanel
                      {...panelProps}
                      panelHidden={panelHidden}
                      /* Both placements are dismissable, so both hand the panel
                         an onClose: the strip renders its close control and
                         Escape inside a view answers. Docked it hides the
                         column and the thread takes the full width; as an
                         overlay the scrim is a second dismiss, the drawer
                         convention. `extraReserveW` keeps the live roster width
                         plus the page's gaps clear on top of the shell reserve,
                         so a drag can never fold the thread to nothing (the
                         contract the old drawer's reserveWidth carried).
                         Overlay: the panel covers the thread, so nothing to
                         reserve. */
                      onClose={beside ? closeDocked : closeOverlay}
                      extraReserveW={beside && rosterShown ? roster.width + PANEL_GAPS_W : 0}
                      /* Phone only (see panelFillWidth): the overlay fills the
                         window. Off the phone this is undefined and the panel
                         sizes itself. */
                      fillWidth={panelFillWidth}
                    />
                  </motion.div>
                </motion.div>
              )}
            </AnimatePresence>
          </>)
        })()}
      </div>
      {/* New team / Edit team. A saved team opens its team view; a deleted one
          that was open drops `?team=` and the bare URL falls to the page's
          default (the remembered or most recently used crewmate, or the hero
          on an empty roster). */}
      {teamDialog && (
        <TeamDialog
          open
          team={teamDialog.team}
          teams={teams}
          members={orderedMembers}
          onClose={() => setTeamDialog(null)}
          onSaved={(saved) => {
            setTeamDialog(null)
            openTeam(saved.id)
          }}
          onDeleted={(removed) => {
            setTeamDialog(null)
            if (urlTeam === removed.id) setSearchParams(isMobile ? { view: 'roster' } : {}, { replace: true })
          }}
        />
      )}
      {/* The Schedules tab's discard prompt. Raised from the strip's own guard and from
          the section's collapse toggle, so it must sit outside the panel subtree the
          answer may unmount. */}
      {schedConfirmDialog}
      {/* CREW-18688: the bot-edit modal, opened in place by the thread header's
          identity pill (member-identity-pill). Renders nothing until editingCrew is
          set; the hook returns open=false until its roster read resolves the
          record. Lazy + Suspense so its code is not in the main App chunk. The
          fallback is a small NON-lazy loading dialog shown only while a pill click
          is pending and the chunk is still downloading (first open on a cold cache,
          worst over remote access): without it that first click renders nothing,
          because the hook's own loading dialog lives inside the lazy chunk. When no
          edit is requested the fallback is null, so a closed dialog renders nothing. */}
      <Suspense fallback={editingCrew ? <CrewEditorChunkLoading /> : null}>
        <CrewEditorDialog ctl={crewEditor} />
      </Suspense>
    </div>
    </SidePanelDockHost>
  )
}
