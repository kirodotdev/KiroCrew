/** One session row of the Sessions sidebar (`SessionRow`): its type scale, its
 *  ordered state resolver, the slot-scoped live state it subscribes to, its
 *  menus and its drag and keyboard gestures. The shell builds the row's `view`
 *  (./rows) and its one `actions` object; the row's memo compares both. */
import { useRef, useEffect, memo, useMemo, useContext, type ReactNode } from 'react'
import { motion } from 'framer-motion'
import { X, Pin, VolumeX, EyeOff, VenetianMask, MoreVertical, CornerDownRight, GitFork, Loader, Loader2, Bot, ExternalLink, Workflow, TriangleAlert, Goal, MessageCircleQuestionMark, ShieldCheck, Hourglass, GripVertical, Moon } from 'lucide-react'
import { useQuery } from '@tanstack/react-query'
import { shallowEqual } from 'react-redux'
import { useAppDispatch, useAppSelector, type RootState } from '../../store'
import { DropdownMenu, DropdownMenuTrigger, DropdownMenuContent } from '../../components/ui/dropdown-menu'
import { ContextMenuStableTrigger, ContextMenuContent } from '../../components/ui/context-menu'
import { offlineProps } from '../../utils/offline'
import { sessionRefBlockReason } from '../../utils/sessionRefs'
import { crewOf } from '../../hooks/useInstanceSessions'
import { switchSlot, selectSidebarWorkflowActive, selectAutomationForSlot } from '../../store/chatSlice'
import { slotIsRemoteBound } from '../../store/dashboardSlice'
import { api } from '../../api/client'
import { recencyTintShadow } from '../../utils/recencyTint'
import { toolStatusLabel, type ToolStatusDetail } from '../../utils/toolStatusLabel'
import { IconButton, IconButtonGroup } from '../../components/ui'
import { useSimplifiedToolNames } from '../../hooks/useSimplifiedToolNames'
import { useLanguage } from '../../i18n/LanguageProvider'
import { useImeGuard } from '../../hooks/useImeGuard'
import { ROW_BOX_CLS, ROW_IDLE_CLS, ROW_ACTIVE_CLS, ROW_META_CLS, ROW_TITLE_CLS, ROW_STATUS_CLS } from '../../components/listShell'
import SessionActionsMenu from '../../components/SessionActionsMenu'
import { ChannelBrandIcon } from '../../components/ChannelBrandIcon'
import { RemoteCrewChip } from '../../components/RemoteCrewChip'
import { DndDraggable, DndDroppable } from '../../components/dnd'
import { normalizeRunSessionKey } from '../../apps/workflows/runModel'
import { sanitizeLlmOutput } from '../../utils/sanitize'
import type { ChatTag, SessionLink } from '../../types'
import { hasLiveSessionWork } from '../chat/sessionLane'
import { isMemoryWait, queuedWaitText } from '../chat/subagentQueuedReason'
import { focusSiblingSessionRow } from '../chat/sessionRowNav'
import { fmtRelativeTime, slotActivityTs } from '../chat/sessionOrder'
import { useLanguageGeneration } from '../../i18n/useLanguageGeneration'
import { deriveAutomationStatus, MONITOR_STATUS_KEYS } from '../../monitoring/automation'
import MonitorRadar from '../../components/MonitorRadar'
import { i18nT } from '../../i18n/t'
import { uiLocation } from '../../uiLocations/uiLocation'
import { agentOrDefaultLabel, withMemorySource } from '../../utils/agentLabel'
import { isUntitledSession, sessionDisplayTitle } from '../../utils/sessionDisplayTitle'
import { sessionMemoryTag } from '../../lib/agentMemorySource'
import { sameRowView, type SessionRowView } from './rows'
import { memberThreadTitle } from '../../lib/memberThreadTitle'
import { CrewOfflineContext } from './CrewGroups'
import { useSlotTitleGenerating } from '../../hooks/slotTitleGeneration'
import { DisclosureChevron, isOpenInTabModifierClick } from './chrome'
import { SessionSourceChips } from './SessionSourceChips'
import { CrewEyebrow, CrewGhostBadge, CrewWorkerStack } from '../../components/crew/CrewModeMarks'
import { summarizeCrewWorkers } from '../../lib/crewWorkers'
import { useCrewWorkerSummary } from '../chat/command-center/CrewWorkerList'
import ErrorNotice from '../../components/ErrorNotice'
import { MemberRowHead } from './MemberRowFace'

/** A row's slot-scoped live facts, read in ONE subscription. Flat, so the
 *  caller's shallowEqual compares each fact by identity: the workflow entry
 *  (rebuilt per run event) is spread into primitives for the same reason the
 *  old per-field subscription compared it shallowly. */
function selectRowLive(st: RootState, localSlotKey: string) {
  const wf = selectSidebarWorkflowActive(st)[normalizeRunSessionKey(localSlotKey)]
  return {
    statusDetail: st.chat.slotStatusDetail?.[localSlotKey],
    automation: selectAutomationForSlot(st, localSlotKey),
    queuedForSlot: st.chat.subagentQueued?.[localSlotKey] || 0,
    queuedReason: st.chat.subagentQueuedReason?.[localSlotKey],
    wfCount: wf?.count,
    wfName: wf?.name,
    wfPhase: wf?.phase,
  }
}

/**
 * Session-row type scale, quantized to a 4px baseline grid.
 *
 * Every line box is a multiple of 4 and the inter-line gaps are zero — the
 * leading carries the breathing room — so a row is a whole number of grid
 * units (12px padding + 12 + 20 + 16 = 60) and consecutive rows stack on the
 * grid instead of drifting. The previous scale mixed three RATIOS
 * (`leading-tight` / `leading-snug`) over 11/13/12px text, which produced
 * 13.75 / 17.875 / 16.5px boxes: no line landed on the grid and the row height
 * was an arbitrary 62.125px.
 *
 * The three sizes are also spread far enough apart to READ as a hierarchy.
 * 11/13/12 sat within 2px of each other, and CJK glyphs fill their em box, so
 * the secondary line competed with the headline instead of yielding to it.
 *
 * The three boxes need NOT be equal to each other. Row-centring the status
 * marker did require the first and last to match — it is the only way
 * headline-centre can coincide with row-centre — and that constraint is gone
 * because the marker now leads the secondary line and centres on IT.
 * Which is what buys the meta line its 12px box: the tightest of the three,
 * spent on the least important line.
 */

/* ROW_META_CLS / ROW_TITLE_CLS / ROW_STATUS_CLS come from components/listShell,
 * shared with the Crew Members roster so the two lists sit on one type scale. */
/* A SECOND surface tracks the three listShell row sizes: the Notes app's left rail
 * (`apps/md-notebook/constants.ts`, `RAIL_TYPE`) mirrors them so the two
 * sidebars read as one scale. The agreement is by copied value, not a shared
 * token — nothing goes red if these move. Change a size in listShell and update
 * `RAIL_TYPE` in the same commit, or the rail silently diverges. */

/** The secondary line's three shapes, as whole class strings. The eight status
 *  branches that render this line each used to spell the type classes out, so a
 *  ninth state was one copy-paste away from re-introducing a size the grid does
 *  not contain — which is how the line ended up at 12px against an 11px meta
 *  line in the first place. Colour is what actually differs between them.
 *
 *  All three are FLEX rows, because all three lead with the row's status marker
 *  (the muted one carries the `unread` dot). A `w-2 h-2` dot only gets its box as
 *  a flex item — as an inline child both dimensions are dropped and it vanishes. */
const ROW_STATUS_LINE_CLS = `${ROW_STATUS_CLS} flex items-center gap-1.5 min-w-0`
const ROW_STATUS_LINE_ACCENT_CLS = `${ROW_STATUS_CLS} text-accent truncate flex items-center gap-1`
const ROW_STATUS_LINE_MUTED_CLS = `${ROW_STATUS_CLS} text-muted flex items-center gap-1.5 min-w-0`

/** Every glyph in a session row is drawn at ONE size — the status marker, the
 *  meta line's mode/channel markers, and the pin. Three sizes (9 / 10 / 12) read
 *  as accidental variation rather than as a hierarchy, since none of these
 *  glyphs outranks another. */
const ROW_ICON_PX = 10

/** The row-state branches that mean "this session is working right now". The
 *  title line's live dot lights only when one of these wins the row's state
 *  resolver, so the dot and the secondary line always name the same state. */
const LIVE_ROW_STATE_KEYS: ReadonlySet<string> = new Set(['running', 'subagents', 'snapshot_live_work'])

/** A slot's running-status line. Every phase resolves through toolStatusLabel:
 *  the fixed phases (`thinking`/`streaming`) carry no copy in the store and map
 *  to catalog keys at render time, a server-supplied status (also
 *  `kind: 'thinking'`, with its own `label`) is passed through, and a `tool`
 *  phase honors the user's `simplifiedToolNames` preference (purpose vs raw tool
 *  title), so the row agrees with the inline tool pill in the transcript rather
 *  than always showing the purpose. The generic copy covers whatever resolves to
 *  nothing (an `idle` phase caught before the slot list refreshes). */
function slotStatusText(detail: ToolStatusDetail | undefined, simplifiedToolNames: boolean, uiLang: string): string {
  return toolStatusLabel(detail, simplifiedToolNames, uiLang) || i18nT('pages.chatSidebar.thinking')
}

/** The channels a session row wears a brand mark for: one per channel that is
 * currently connected, in first-seen order.
 *
 * Connected means at least one delivery on that channel is not paused — the
 * same rule the session menu's Connect/Disconnect row uses to pick its verb, so
 * the mark on the row and the verb in the menu can never disagree. `direction`
 * plays no part: a channel the session was born in and a channel it was later
 * connected to are the same fact to the reader of the list, and the one thing a
 * disconnect changes on either is `paused`.
 *
 * One per channel, not one per link. A session born in Discord and mirrored to
 * Discord carries two links for it, and a reader should see one Discord mark,
 * not two.
 *
 * Exported for its own test; the row is the only production caller. */
export function connectedChannelLinks(links: readonly SessionLink[] | undefined): SessionLink[] {
  const byChannel = new Map<string, SessionLink>()
  for (const link of links ?? []) {
    if (link.paused || byChannel.has(link.channel)) continue
    byChannel.set(link.channel, link)
  }
  return [...byChannel.values()]
}

/** How many levels of conductor nesting still step the row to the right.
 *
 *  Lineage depth has no ceiling -- a conductor that opens a conductor nests as far as
 *  the work does -- and each level costs 14px of a sidebar that is 320px at its
 *  narrowest. Left uncapped, a deep chain walks the card off the right edge until the
 *  title is unreadable. Past this depth the rows stop stepping and the level is shown
 *  as a number instead, which keeps the information without the geometry. */
const CONDUCTOR_MAX_INDENT_DEPTH = 6

/** The runs-elsewhere chip of a remote-EXECUTED local row, with the peer's
 *  display name read from the SHARED ['instances'] cache. Its own component so
 *  the query observer exists only on rows that are actually bound: an observer
 *  in every row, even a disabled one, re-registers its options on every row
 *  render, and each registration is a query-cache event every cache listener
 *  answers, a per-row cost a full folder pays on each sidebar commit. Falls
 *  back to the instance id: it is less friendly but it is true, and a blank chip
 *  would claim the session runs somewhere unnamed. */
function ExecutorCrewChip({ instanceId }: { instanceId: string }) {
  const { data } = useQuery({
    queryKey: ['instances'],
    queryFn: () => api.listInstances(),
    enabled: !!instanceId,
  })
  const name = data?.instances?.find(i => i.id === instanceId)?.name || instanceId
  return (
    <RemoteCrewChip
      name={name}
      label={i18nT('pages.chatSidebar.on_instance', { name })}
      title={i18nT('pages.chatSidebar.runs_on_crew', { name })}
    />
  )
}

/** What a session row can DO: every callback and ref it needs, built ONCE in the
 *  shell and handed to every row, so the row's memo compares one stable reference
 *  here and its data through `sameRowView`. */
interface SessionRowActions {
  renameInputRef: React.MutableRefObject<HTMLTextAreaElement | null>
  onRenameStart: (key: string, scope: string, title: string, fromMenu: boolean) => void
  onRenameChange: (value: string) => void
  onRenameCommit: (key: string, value: string) => void
  slotRenameEnterRef: React.MutableRefObject<boolean>
  onRenameCancel: () => void
  /** The row menu's "Regenerate title": re-derives the title from the
   *  conversation without making the session active. */
  onAutoTitle: (key: string) => void
  onDuplicate: (key: string) => void
  onCloseSession: (key: string) => void
  onMenuCloseAutoFocus: (e: Event) => void
  onSelectSlot?: (key: string) => void
  onOpenSlotInNewTab?: (key: string, opts?: { background?: boolean }) => void
  onOpenSource?: (slotKey: string, link: { url: string; kind: 'change' | 'issue' }) => boolean
  /** Open a row whose session lives on a remote instance as a window onto the
   *  peer's own slot. Distinct from `onSelectSlot` because the session has no
   *  local slot: the peer owns it. */
  onOpenPeerSession: (instanceId: string, remoteSlot: string) => void
  /** A board card drags with native HTML5 DnD, outside every DndContext, so the
   *  shell learns of that drag only from the row: called with the slot key at
   *  `dragstart`. The end is read at the window (see `useNativeSessionDrag`). */
  onNativeDragStart: (key: string) => void
  /** The one end the window cannot hear: the row unmounting while its own
   *  native drag is still in flight (its lane changed under it), after which
   *  the browser's `dragend` lands on a detached node. */
  onNativeDragEnd: () => void
  onPinnedKeyboardReorder: (key: string, container: string, delta: -1 | 1, row: HTMLElement) => void
  /** Alt+Arrow on an UNPINNED row: the manual order's move, as a drop makes it. */
  onManualKeyboardReorder: (key: string, container: string, delta: -1 | 1, row: HTMLElement) => void
  /** Go where a row whose session belongs to another page lives
   *  (`view.opensElsewhere`): click and Enter land here instead of `switchSlot`. */
  openElsewhere: (path: string) => void
  /** The conductor lane's chevron: open or close the sessions this row opened. */
  toggleConductor: (identity: string) => void
  /** Stage a reference to a session in the active chat's composer — the same
   *  callback the drag-into-the-composer drop fires. The row menu's "Reference
   *  in chat" item calls it for THIS row's session after the row has checked
   *  `sessionRefBlockReason`; `canStageSessionRef` gates whether a composer
   *  exists to stage into at all. Undefined on a surface with no chat pane
   *  (the embed sessions list), which hides the item. */
  onDropSessionRef?: (ref: { key: string; title: string; messages?: number }) => void
  /** Whether a composer exists to stage a session reference into (not split
   *  view, an active slot present). The row combines it with the per-row block
   *  reason to decide whether to offer "Reference in chat". */
  canStageSessionRef?: boolean
}

interface SessionRowProps {
  /** Everything the row renders from, already masked for its origin by the row
   *  model (./rows): a peer row's view carries no local state. */
  view: SessionRowView
  actions: SessionRowActions
}

/** One sidebar session row behind a memo boundary, so the 200+ row bodies do
 *  not re-execute when unrelated sidebar state moves. The row takes its whole
 *  data as one `view` the row model builds per placement, compared field by
 *  field (`sameRowView`), and its callbacks as one `actions` object the shell
 *  builds once; ChatSidebar.rowMemo.test.tsx pins the boundary. The slot's LIVE
 *  per-slot state (status line, goal loop, queued sub-agents, workflow runs)
 *  is subscribed to HERE, slot-scoped, so a background event re-renders only
 *  the row it belongs to. */

/** A Crew Mode root's live workers on its row: a ghost per worker coloured by
 *  state, then "N needs you" or "done x/y". It IS the row's status line, not a
 *  line of its own: the row's running and approval states outrank it (the caller
 *  passes them first), and with no workers yet it shows *fallback*, the last
 *  message line, so a crew card is as tall as any other row. */
function CrewRootSummaryLine({ root, fallback }: { root: string; fallback: ReactNode }) {
  // The server's groups (the worker list API), so the row, the panel and the
  // breadcrumb agree; the fallback until the list has answered.
  // A failed background poll keeps the last summary's ghosts and says the read
  // failed in the row's own status line (the panel's wording, with the agent
  // hand-off); the next poll clears it. With no data at all the row says so too.
  const { lastWorkers, failed } = useCrewWorkerSummary(root)
  // Only tool approvals reach the person; a worker's question is its conductor's.
  const { groups, approval, done, total } = summarizeCrewWorkers(lastWorkers ?? [])
  if (total === 0 && !failed) return <>{fallback}</>
  return (
    <div className={ROW_STATUS_LINE_CLS} data-testid="crew-root-summary">
      {total > 0 && <CrewWorkerStack groups={groups} />}
      {failed ? (
        <ErrorNotice variant="inline" askAgent className="ml-auto min-w-0" messageClassName="truncate" message={i18nT('commandCenter.workers_error')} />
      ) : (
        <span className={`ml-auto text-[12px] truncate ${approval > 0 ? 'crew-tone-warn font-medium' : 'crew-tone-muted'}`}>
          {approval > 0
            ? i18nT('components.crewMode.summary_approval', { n: approval })
            : i18nT('components.crewMode.summary_done', { done, total })}
        </span>
      )}
    </div>
  )
}

export const SessionRow = memo(function SessionRow({ view, actions }: SessionRowProps) {
  const {
    slot: s, showDivider, scope, navScope, holdContainer, isActive, isSidebarActiveSlot, crewWindowCovers, isOut, isPinned, isUnread, isRunning,
    recent, subagentCount, subagentApprovalCount, digitBadge,
    isRenaming, renamingHere, renameValue, revealFlash, pinnedOrderIndex, drawnOrderIndex, pinnedReorderEnabled, rowAnimEnabled,
    rowMotionHeld, opensElsewhere, peerId, peerName,
  } = view
  // A Crew Mode root (stamped by the gateway, `crew_mode.stamp_rows`) is drawn as
  // the crew card: ghost badge, gradient eyebrow and its workers' summary. Only a
  // local row: a peer row's stamps describe another gateway's tree.
  const crewRoot = !!s.crew_mode_root && !s.peer_id
  const conductor = view.conductor
  // An ADOPTED remote-crew row (a local slot that mirrors a peer session) has a ✕
  // that removes only this machine's copy — the session stays live on its peer,
  // and this hub cannot close it across the link. Relabel the ✕ "Remove from this
  // machine" and name the crew it keeps running on, instead of promising a durable
  // "Close" the row never performs, which is what made the removed row reappear on
  // the next refresh (#19958).
  // `!s.peer_id && crewOf(s)` is the repo's existing spelling of "adopted here": a
  // peer row is owned elsewhere (no ✕ to relabel), and crewOf returns the crew a
  // local slot's turns run on (executor === 'remote' with an instance_id).
  const releaseLocally = !s.peer_id && !!crewOf(s)
  // The crew's display name for the hint, resolved from the SAME shared
  // ['instances'] cache the row's "On <name>" chip reads, so the hint names the
  // crew the chip already shows. Gated on `releaseLocally` so an ordinary row
  // registers no observer (the per-row cost ExecutorCrewChip was split out to
  // avoid). Resolves to the crew's display NAME only; a raw instance id is not a
  // name a reader recognises, so when the lookup misses (cache not loaded, or no
  // match) the hint falls back to the generic line rather than showing the id.
  const { data: instancesForHint } = useQuery({
    queryKey: ['instances'],
    queryFn: () => api.listInstances(),
    enabled: releaseLocally,
  })
  const releaseCrewName = releaseLocally
    ? (instancesForHint?.instances?.find(i => i.id === s.instance_id)?.name || '')
    : ''
  // A card with sessions under it says how far its ✕ reaches: this one session,
  // never the ones it opened, which stay open and keep running. The release label
  // wins when both apply: a reappearing-after-close surprise is the sharper one.
  const closeReachLabel = releaseLocally
    ? i18nT('pages.chatSidebar.release_locally')
    : conductor && conductor.childCount > 0 ? i18nT('pages.chatSidebar.close_this_session_only') : undefined
  const closeReachHint = releaseLocally
    ? (releaseCrewName
      ? i18nT('pages.chatSidebar.release_locally_hint', { peer: releaseCrewName })
      : i18nT('pages.chatSidebar.release_locally_hint_generic'))
    : conductor && conductor.childCount > 0 ? i18nT('pages.chatSidebar.close_keeps_children_hint') : undefined
  const {
    connected, mode, isMobile, colorMode, defaultAgent, installedAgents, memoryRows, tagById, paletteColors, boost, boostFor,
    recentTintCount, dragInFlight, activeDraggedKey, activeDraggedPinnedIndex, activeDraggedDrawnIndex, manualSort,
    guideOnRowControl, showUnreadDot,
  } = view.shell
  const {
    onNativeDragStart, onNativeDragEnd, onPinnedKeyboardReorder, onManualKeyboardReorder,
    renameInputRef, onRenameStart, onRenameChange, onRenameCommit, onRenameCancel, onAutoTitle,
    slotRenameEnterRef,
    onDuplicate, onCloseSession, onMenuCloseAutoFocus, onSelectSlot, onOpenSlotInNewTab, onOpenSource, onOpenPeerSession,
    openElsewhere, toggleConductor, onDropSessionRef, canStageSessionRef,
  } = actions
  // A LOCAL row whose surface this page does not render goes where it lives: click
  // and Enter land here instead of `switchSlot`, and every local-only affordance
  // (rename, close, fork, drag, the row menu) is withheld exactly as it is for a peer
  // row, because the slot's lifecycle is owned elsewhere.
  const onOpenElsewhere = opensElsewhere != null ? () => openElsewhere(opensElsewhere) : undefined
  const titleGenerating = useSlotTitleGenerating(s.key)
  // Peer ownership, present only on a row sourced from a connected remote
  // instance. Every local-only affordance below is gated on its ABSENCE rather
  // than disabled: a control that looks actionable and silently does nothing is
  // worse than no control, and none of close / duplicate / rename / reorder can
  // be honoured for a session whose slot lives on another machine.
  //
  // `peerId`, NOT the `instance_id` that `ExecutorCrewChip` below reads: these two
  // sit in one scope and mean opposite things. This one says the session is not
  // ours; that one says the session IS ours and dispatches elsewhere. It and
  // `peerName` come from the row model's origin check, set on a peer view only.
  //
  // The affordance gate proper. A peer row and a row that opens elsewhere are
  // withheld the SAME set -- rename, close, fork, drag, the row menu -- for the
  // same reason: this sidebar does not own the slot's lifecycle. They differ only
  // in what a click does (open a window vs. navigate), which the handlers below decide.
  const foreignRow = !!peerId || onOpenElsewhere != null
  // A crewmate's own DM thread is named after the crewmate, never after a generated
  // topic (`memberThreadTitle`). Local rows only: a peer's roster is not ours.
  const memberTitle = peerId ? null : memberThreadTitle(s, installedAgents)
  // An untitled row reads "Untitled session", never its key (#22049); the key
  // stays in the tooltip, where it tells untitled rows apart.
  const shownTitle = memberTitle ?? sessionDisplayTitle(s)
  // The tooltip keeps a topic stored before the thread was named after the crewmate.
  const tooltipTitle = memberTitle !== null && s.title && s.title !== s.key && s.title !== memberTitle
    ? `${memberTitle}: ${s.title}`
    : memberTitle ?? (isUntitledSession(s) ? `${shownTitle} · ${s.key}` : shownTitle)
  // Inside a crew group the header already names the machine, so the row
  // drops its own crew chip.
  const inCrewGroup = scope.startsWith('crew:')
  const inOfflineCrewGroup = useContext(CrewOfflineContext) && inCrewGroup
  const rowIdentity = view.identity
  // The row model resolved the origin once: `localKey` is '' for a peer row, so a
  // peer row never reads slot-scoped LOCAL state under its raw key (the gateways do
  // not share a key namespace, and deterministic keys can be byte-identical).
  const localSlotKey = view.localKey
  // memo() bails out of the provider-level repaint, so the row subscribes to
  // catalog loads directly (same contract as the ChatSidebar shell) — its
  // i18nT strings must re-translate even when no prop moves.
  const langGen = useLanguageGeneration()
  const dispatch = useAppDispatch()
  const ime = useImeGuard()
  const simplifiedToolNames = useSimplifiedToolNames()
  const uiLang = useLanguage().resolved
  // ── Slot-scoped store reads ──────────────────────────────────────────────
  // Each subscription selects THIS slot's entry, so a write to another slot's
  // status/loop/queue/run leaves this row's subscription value untouched and
  // the row does not re-render. Hoisting any of these to the shell as a
  // whole-map read re-renders every row per event — the regression the memo
  // test's per-row render count exists to catch.
  // `localSlotKey`, not `s.key`: a peer-owned row must not read slot-scoped LOCAL
  // state under its own key, because the two gateways do not share a key
  // namespace and a collision would show another session's status here.
  // `selectAutomationForSlot` does its own `isUnsafeKey`/`safeKey` normalization,
  // so this needs no own-property guard of its own.
  //
  // ONE subscription for all of them (`selectRowLive`): every mounted row runs
  // its selectors on every store notification, so five per row was five times
  // the sidebar's share of each dispatch. shallowEqual over the flat result
  // keeps each field's own identity rule: an entry object repaints only when
  // THIS slot's entry is replaced, and the workflow primitives only when this
  // slot's runs change.
  const { statusDetail, automation, queuedForSlot, queuedReason, wfCount, wfName: wfRawName, wfPhase: wfRawPhase } =
    useAppSelector(st => selectRowLive(st, localSlotKey), shallowEqual)
  const goalLoop = automation?.kind === 'legacy_goal_loop' ? automation : undefined
  const monitor = automation?.kind === 'structured_monitor' ? automation : null
  // {count, name, phase} of this slot's running workflow fan-out, or undefined.
  const wf = useMemo(
    () => (wfCount === undefined ? undefined : { count: wfCount, name: wfRawName ?? '', phase: wfRawPhase ?? '' }),
    [wfCount, wfRawName, wfRawPhase],
  )
    // The conductor lane's count cluster, built HERE so it can sit inside the card's
    // own meta group. The lane hands over numbers and renders none of this itself,
    // which is what keeps one card implementation across every lane.
    const conductorMeta = conductor && (
      conductor.childCount > 0
      || conductor.aggregate != null
      || conductor.orphanOf != null
      || conductor.citesParent != null
      || conductor.depth > CONDUCTOR_MAX_INDENT_DEPTH
    ) ? (
      <>
        {conductor.depth > CONDUCTOR_MAX_INDENT_DEPTH && (
          // Past the indent cap the rows stop stepping right, so the level is carried
          // as a number rather than lost. Prefixed with a middot so it is not read as
          // one more count: it sits in the same cluster as the child and aggregate
          // numbers, and a bare digit there says nothing about which kind it is.
          <span className="text-muted tabular-nums shrink-0"
            title={i18nT('pages.chatSidebar.nesting_depth', { depth: conductor.depth })}
            data-testid={`conductor-depth-${rowIdentity}`}>&middot;{conductor.depth}</span>
        )}
        {/* Both citation states name the SAME bent arrow, so both carry their name the
         *  same way: `role="img"` with `aria-label` on the wrapper, and the glyph inside
         *  marked decorative. An `aria-label` on the bare `<svg>` is not dependably
         *  exposed — an `svg` element carries no image role of its own — so the name has
         *  to sit on an element whose role admits one. Without it the row offers a reader
         *  a shape and no fact: the arrow says a creator exists and never says which. */}
        {conductor.orphanOf != null && (
          <span className="inline-flex items-center text-muted shrink-0"
            role="img"
            aria-label={i18nT('pages.chatSidebar.opened_by_closed_session')}
            title={i18nT('pages.chatSidebar.opened_by_closed_session')}
            data-orphan-of={conductor.orphanOf}
            data-testid={`conductor-orphan-${rowIdentity}`}>
            <CornerDownRight size={11} className="lucide-inline" aria-hidden="true" />
          </span>
        )}
        {conductor.orphanOf == null && conductor.citesParent != null && (
          // Same glyph, different fact: this creator is open, the lane just is not
          // nesting right now (search flattens every match to one level). Without it a
          // flattened child looks exactly like a session nobody opened.
          <span className="inline-flex items-center text-muted shrink-0"
            role="img"
            aria-label={i18nT('pages.chatSidebar.opened_by_session', { slot: conductor.citesParent })}
            title={i18nT('pages.chatSidebar.opened_by_session', { slot: conductor.citesParent })}
            data-cites-parent={conductor.citesParent}
            data-testid={`conductor-cites-parent-${rowIdentity}`}>
            <CornerDownRight size={11} className="lucide-inline" aria-hidden="true" />
          </span>
        )}
        {conductor.childCount > 0 && (
          <span className="text-muted tabular-nums shrink-0"
            role="img"
            aria-label={i18nT('pages.chatSidebar.opened_sessions_count', { count: conductor.childCount })}
            title={i18nT('pages.chatSidebar.opened_sessions_count', { count: conductor.childCount })}
            data-testid={`conductor-child-count-${rowIdentity}`}>{conductor.childCount}</span>
        )}
        {/* Each aggregate count carries the SAME glyph its children show on their own
         *  rows, so the two read as the same fact at two zoom levels. Tint alone
         *  distinguished them before, which is invisible to a colour-blind reader and
         *  gone entirely in a high-contrast theme. The child count above stays plain:
         *  it has no per-child glyph to echo. */}
        {conductor.aggregate != null && conductor.aggregate.needsYou > 0 && (
          <span className="inline-flex items-center gap-0.5 px-1 rounded bg-accent-subtle text-accent tabular-nums shrink-0"
            title={i18nT('pages.chatSidebar.needs_your_answer')}
            data-testid={`conductor-needs-you-${rowIdentity}`}>
            <MessageCircleQuestionMark size={10} className="lucide-inline shrink-0" aria-hidden />
            {conductor.aggregate.needsYou}
          </span>
        )}
        {conductor.aggregate != null && conductor.aggregate.running > 0 && (
          <span className="inline-flex items-center gap-0.5 px-1 rounded bg-bg-hover text-muted tabular-nums shrink-0"
            title={i18nT('pages.chatSidebar.running_session', { count: conductor.aggregate.running })}
            data-testid={`conductor-running-${rowIdentity}`}>
            <Loader2 size={10} className="lucide-inline shrink-0 animate-spin" aria-hidden />
            {conductor.aggregate.running}
          </span>
        )}
      </>
    ) : null

    // Flat view shares the tree's layoutId namespace so Framer Motion treats a
    // row as the SAME element across the view toggle and animates it from its
    // tree position into the flat lane (and back). Safe: the two views are
    // ternary branches — never mounted simultaneously — so IDs can't collide.
    // Behavior stays keyed on the real scope.
    const layoutScope = scope === 'flat' || scope === 'conductor' ? 'list' : scope
    // List and flat rows use dnd-kit. Board columns retain native HTML5 drag
    // because a card drop there changes status-column membership.
    //
    // Drag is a LOCAL-slot gesture: dnd-kit's drop handlers reorder local slots,
    // move them between folders and drop them onto the chat pane, all keyed by a
    // slot key that exists in the local store. A PEER-OWNED row has no local slot,
    // so a drag could only resolve to nothing or — if a peer key ever coincided
    // with a local one — to the WRONG session. Excluded by construction rather
    // than handled per drop target. A remote-EXECUTED local slot is not excluded:
    // its slot is right here, and reordering it is as meaningful as any other.
    const dndRow = (scope === 'list' || scope === 'flat') && !foreignRow
    // Whether THIS row's native drag is in flight, so the unmount cleanup below
    // reports the drag's end only for the row that started it: every other row
    // unmounts freely under the windowing, and must not end a drag it never began.
    const nativeDragInFlight = useRef(false)
    useEffect(() => () => { if (nativeDragInFlight.current) onNativeDragEnd?.() }, [onNativeDragEnd])
    const reorderContainer = scope === 'flat' ? 'flat' : (s.folder_id || 'root')
    // Shown on the rows a drag can place in the manual order.
    const manualHandle = manualSort && !isPinned && dndRow && pinnedReorderEnabled
    // Alt+ArrowUp/Down moves a pinned row in every sort, and an unpinned row
    // only under Manual: in another sort the chord stays the page's (the
    // transcript's turn step), and a keystroke never switches the sort.
    const keyboardMovable = dndRow && pinnedReorderEnabled && (isPinned || manualSort)
    const agentName = s.agent || defaultAgent || ''
    // What the row SHOWS, kept separate from `agentName` on purpose. That value
    // is a resolution KEY — it feeds the source tint lookup, the divergence
    // comparison and the span's React key — so a decorated string in it would
    // tint the wrong agent and compare a label against a name.
    //
    // An empty `s.agent` means this session resolves the CURRENT default at run
    // time; it is NOT a pin that happens to name the default. Both states put
    // the same alias in `agentName`, and `effective_agent` is "" for both (an
    // alias resolving to itself reports nothing), so without the marker the two
    // are indistinguishable in every value the row holds (#6529). One shared
    // spelling with the agents rail and the Schedule page.
    //
    // The row's own empty-state placeholder is preserved: with no agent AND no
    // default there is nothing to mark, and the literal 'default' the helper
    // degrades to would be a boot-window claim rather than a label.
    //
    // A crewmate and a custom agent of one name read alike, so a row whose agent
    // has such a pair in the picker also says which memory it runs on (#19472).
    // Local rows only: `memoryRows` is THIS machine's catalog, and a peer's row
    // or a row whose turns run on another crew resolves its agent there.
    const memoryTag = agentName && !peerId && !crewOf(s)
      ? sessionMemoryTag(s.agent, s.agent_kind, defaultAgent, memoryRows)
      : null
    // A crewmate's own thread is named by the title below, so the chip does not
    // print the same name a second time; the line keeps its height and the
    // Members-page chip.
    const agentDisplay = agentName && memberTitle === null ? withMemorySource(agentOrDefaultLabel(s.agent, defaultAgent, s.agent_kind), memoryTag) : ''
    // A DIVERGENCE, not a status: the row is advertising `agentName` while a
    // different agent answers the session — usually an app agent that was
    // removed, or one whose registration has not landed yet. Shown because the
    // stored binding is deliberately left verbatim, so without this the sidebar
    // names an agent that is not running, and the user only finds out turns
    // later when none of its tools are there.
    //
    // Empty is the common case and means "nothing to report", so the marker is
    // gated on a non-empty value that actually differs from what is displayed —
    // never on inequality alone, which would fire during the boot window on a
    // healthy install. The `?? ''` is load-bearing: rows arrive from persisted
    // and optimistically-added state that predates this field.
    const effectiveAgent = s.effective_agent ?? ''
    const agentDiverged = effectiveAgent !== '' && effectiveAgent !== agentName
    const agentMeta = installedAgents.find(a => a.name === agentName)
    const isPackageAgent = agentMeta?.source === 'package'
    const isBuiltin = agentMeta?.source === 'builtin'
    const agentColor = isPackageAgent ? 'text-[var(--aim)]' : isBuiltin ? 'text-muted' : 'text-muted'
    // The meta line's second slot shows the session's TAGS, not a value derived
    // from the project path. The auto-tagger already labels each session with its
    // project, so those tags ARE the context the row needs; deriving a label
    // would just print the same word again. ALL tags render, each as tinted plain
    // text after a "·", in tag `order` so the sequence is stable.
    const resolvedSlotTags = (s.tags ?? [])
      .map(tid => tagById[tid])
      .filter((t): t is ChatTag => !!t)
      .sort((a, b) => (a.order ?? 0) - (b.order ?? 0))
    // Sub-agents held at the spawn gate. Excluded from the running/queued
    // arithmetic below: "4 agents running" while 2 of them are blocked on your
    // click is both wrong and the reason the owed approval went unnoticed.
    const subagentAwaiting = Math.min(subagentApprovalCount, subagentCount)
    const subagentActive = subagentCount - subagentAwaiting
    // Distinguish started from queued: "3 agents running" is wrong for a wave
    // that is still entirely behind the concurrency cap.
    const subagentQueuedCount = Math.min(queuedForSlot, subagentActive)
    const subagentStarted = subagentActive - subagentQueuedCount
    const subagentLabel = subagentStarted === 0
      // Reuses the subagentRunCard keys: same meaning, same grammatical role
      // (counted agents queued/running), so a second namespace would be a
      // byte-identical duplicate across all 12 catalogs.
      ? i18nT('pages.chat.subagentRunCard.agent_queued', { count: subagentQueuedCount })
      : subagentQueuedCount > 0
        ? i18nT('pages.chatSidebar.running_queued', { started: subagentStarted, queued: subagentQueuedCount })
        : i18nT('pages.chat.subagentRunCard.agent_running', { count: subagentStarted })
    const subagentApprovalLabel = i18nT('pages.chatSidebar.sub_agent_needs_approval', { count: subagentAwaiting })
    // Children accepted but not started, with none started: the session is
    // WAITING on them, not working, so the row says so without the running
    // pulse. A memory wait names memory; a cap wait keeps the queued count.
    const subagentsOnlyQueued = subagentQueuedCount > 0 && subagentStarted === 0 && subagentAwaiting === 0
    const subagentWaitingLabel = isMemoryWait(queuedReason)
      ? i18nT('pages.chatSidebar.waiting_for_memory')
      : subagentLabel
    const subagentWaitingTitle = queuedWaitText(queuedReason) ?? subagentLabel
    // Live dynamic-workflow activity for THIS slot (slot-scoped subscription
    // above). The label mirrors what the sidebar-wide map used to precompute:
    // one run shows its sanitized name · phase, a fan-out shows a count.
    const wfName = wf ? sanitizeLlmOutput(wf.name).slice(0, 60) : ''
    const wfPhase = wf?.phase ? sanitizeLlmOutput(wf.phase).slice(0, 40) : ''
    const wfActive = wf
      ? {
        count: wf.count,
        label: wf.count > 1
          ? i18nT('pages.chatSidebar.workflow_running', { count: wf.count })
          : `${wfName}${wfPhase ? ` · ${wfPhase}` : ''}`,
      }
      : undefined
    // The agent's own ask: a question card the user has not answered yet. The
    // turn is parked on it, so this replaces a "Thinking…" that would otherwise
    // never change rather than annotating a finished turn.
    const needsInputLabel = i18nT('pages.chatSidebar.needs_your_answer')
    const monitorStatus = monitor ? deriveAutomationStatus(monitor) : null
    const monitorOwnsRunning = !!monitor && monitor.active && !monitor.terminal
    const monitorLabel = monitorStatus
      ? i18nT('components.sessionAutomationPopover.sidebar_status', {
        status: i18nT(MONITOR_STATUS_KEYS[monitorStatus]),
      })
      : ''
    // Goal loop (auto-nudge). A loop is a MODE, not a turn state, so it is not
    // gated on `s.running` — a looping session spends most of its life mid-turn,
    // and hiding the indicator then would hide it almost always.
    // `maxCycles === 0` means unlimited (autonudge.py NudgeLoop default), so
    // there is no denominator to show — fall back to a bare count.
    const goalLoopLabel = !goalLoop
      ? ''
      : goalLoop.maxCycles > 0
        ? i18nT('pages.chatSidebar.loop', { count: goalLoop.cycleCount, total: goalLoop.maxCycles })
        : i18nT('pages.chatSidebar.loop_2', { count: goalLoop.cycleCount })
    // The loop is armed but its session's last turn died — a trailing error row
    // or an unanswered user row, the state behind the composer's Resume button —
    // and nothing is executing on its behalf. The pulsing dot below would claim
    // active work for the whole gap until the user resumes or the next
    // idle-timer cycle fires (up to idle_secs away), so this renders as a static
    // warn dot with an explicit "interrupted" instead. Guarded on the raw turn
    // flag plus workflow/subagent activity: while any of those run, the loop IS
    // working and `s.interrupted` only describes a superseded turn.
    const snapshotOnlyLiveWork = !s.running && hasLiveSessionWork(s)
    const snapshotLiveWorkLabel = i18nT('pages.chatSidebar.filter_running')
    const liveWorkSupersedesInterruption = hasLiveSessionWork(s, {
      workflowActive: !!wfActive,
      // Started children only: a queued child has not superseded anything.
      detailedSubagentsRunning: subagentCount - subagentQueuedCount > 0,
    })
    const goalLoopStalled = !!goalLoop && !!s.interrupted && !liveWorkSupersedesInterruption
    // An armed loop whose NEWEST reply is an explicit `[OPTIONS:]` ask. The
    // loop cannot advance that decision itself — the user owes the answer — so
    // it must not read as unattended progress (the goal-loop branch below).
    // Gated on the newest reply only: `s.has_options` drops the moment any
    // later turn talks over the marker, so a superseded ask can never be
    // resurrected — the staleness that reverted the buried-[OPTIONS:] scan
    // (#10615). Idle only: a running turn IS the loop working, and a stalled
    // loop's danger row (below) outranks an ask its dead turn cannot collect.
    const loopWaiting = (!!goalLoop || monitorOwnsRunning)
      && !!s.has_options && !s.running && !s.interrupted
    // Ordinary sessions need the same reboot/error visibility as goal loops,
    // without claiming that an older interrupted parent turn has stopped live
    // child work. A goal loop keeps its richer cycle-specific treatment below;
    // active workflows, subagents, turns, and queued work keep
    // their progress indicators.
    const turnNeedsAttention = !goalLoop && !!s.interrupted && !liveWorkSupersedesInterruption
    // Whatever this row would have said if no loop were running, reused as the
    // loop line's trailing detail. This is why the loop branch can outrank the
    // working signals below without swallowing them: live workflow/subagent/tool
    // status still shows, and between cycles it falls back to the last message.
    // Reads the RAW `s.running`, not `runningSet`: the widened flag includes this
    // very loop, and an idle-between-cycles row must show its last message.
    const goalLoopDetail = wfActive
      ? wfActive.label
      : subagentCount > 0
        ? subagentLabel
        : s.running
          ? slotStatusText(statusDetail, simplifiedToolNames, uiLang)
          : snapshotOnlyLiveWork
            ? snapshotLiveWorkLabel
            : (s.last_message || '')
    const ci = s.color_index != null && s.color_index >= 0 && s.color_index < paletteColors.length ? s.color_index : null
    // The row's ONE status marker and the words beside it, resolved together: the
    // glyph is built INSIDE the branch's `subtitle`, immediately in front of the
    // label that names it, so a branch cannot ship a glyph without its phrase or a
    // phrase without its glyph.
    //
    // ── One ordered state resolver (#3830) ────────────────────────────────
    //
    // The marker and the subtitle line encode the SAME precedence. They used to
    // be two independent ternary chains a few hundred lines apart, with comments
    // asserting they "can never disagree" and nothing enforcing it: editing a
    // branch in one silently desynchronised the glyph from the subtitle. They are
    // now ONE node per branch, so the ordering exists once and a new state is
    // added in one place.
    //
    // Order is the contract. Owed decisions outrank every "working" signal —
    // a blocking card keeps `s.running` true, so without that ranking the row
    // would read "Thinking…" while nothing can advance until the user acts.
    //
    // `when` is a plain boolean, evaluated in order; the first truthy entry
    // wins. Everything else is behind `build()` and is called ONLY for that
    // winner. That laziness is load-bearing, not a style choice: the chain runs
    // for every row, and `slotStatusDetail` is only meaningful for a running
    // one — eagerly resolving the running label threw on rows where it is
    // absent. The ternary chain this replaces got the same property for free by
    // being a ternary; here it has to be explicit.
    //
    // The tail is `last_message`, and the `unread` dot rides on it (below).
    const rowStateEntry = ([
      {
        // Pending approval outranks running (mirrors the Board's inferLane,
        // which returns its approval lane before the running check), so an owed
        // approval is never hidden behind a "Thinking…" spinner.
        key: 'pending_approval',
        when: !!s.pending_approval,
        build: () => (
          <div className={ROW_STATUS_LINE_CLS}>
            <ShieldCheck size={ROW_ICON_PX} className="shrink-0" style={{ color: 'var(--warn)' }} aria-hidden />
            <span className="truncate"><span className="font-medium" style={{ color: 'var(--warn)' }}>{i18nT('pages.chatSidebar.needs_approval')}</span>{s.last_message ? <span className="text-muted"> · {s.last_message}</span> : null}</span>
          </div>
        ),
      },
      {
        // Sub-agents blocked on a spawn approval. Directly below the slot's own
        // pending approval and above every "working" signal, for the same
        // reason: an owed decision must not read as work in progress. The bot
        // glyph is static, not pulsing — nothing is running — and warn-coloured
        // to match the row above.
        key: 'subagent_awaiting',
        when: subagentAwaiting > 0,
        build: () => (
          <div className={ROW_STATUS_LINE_CLS} title={subagentApprovalLabel}>
            <Bot size={ROW_ICON_PX} className="shrink-0" style={{ color: 'var(--warn)' }} aria-hidden />
            <span className="truncate font-medium" style={{ color: 'var(--warn)' }}>{subagentApprovalLabel}</span>
          </div>
        ),
      },
      {
        // An unanswered question card. Above every "working" signal for the
        // same reason as the approval branches — and a blocking card keeps
        // `s.running` true, so without this the row would show "Thinking…"
        // while nothing can advance. Info-coloured and static-glyphed to stay
        // distinct from the warn-coloured approval rows above.
        //
        // A card is a websocket broadcast with no transcript row, so
        // `last_message` is whatever the agent last said BEFORE the ask — not
        // the question. Trailing it after "Needs your answer ·" would read as
        // the question itself, so the label stands alone.
        key: 'needs_input',
        when: !!s.needs_input,
        build: () => (
          <div className={ROW_STATUS_LINE_CLS} title={needsInputLabel}>
            <MessageCircleQuestionMark size={ROW_ICON_PX} className="shrink-0" style={{ color: 'var(--info)' }} aria-hidden />
            <span className="truncate font-medium" style={{ color: 'var(--info)' }}>{needsInputLabel}</span>
          </div>
        ),
      },
      {
        // An armed loop (goal loop or structured monitor) whose newest reply is
        // an `[OPTIONS:]` ask. Ranked with the owed-decision cluster, above
        // every "working" signal: the loop is holding for the user, and the
        // pulsing goal-loop row below would read as unattended progress —
        // the exact confusion this branch exists to remove. Static glyph,
        // warn ink: nothing is running. The trailing detail keeps the loop's
        // identity (cycle count / monitor status) so the row still says WHICH
        // automation is waiting, per the goalLoopDetail pattern.
        key: 'loop_waiting',
        when: loopWaiting,
        build: () => (
          // The tooltip names the cycle count so the trailing fraction is
          // glossed, not orphaned: the UX blind-reader could not tell the
          // waiting row's trailing "Loop 18/80" and the progress row's leading
          // "Loop 7/24" were the same counter. Monitors have no fraction, so
          // they keep the generic title.
          <div
            className={ROW_STATUS_LINE_CLS}
            title={goalLoop
              ? (goalLoop.maxCycles > 0
                ? i18nT('pages.chatSidebar.loop_waiting_title_cycle', { count: goalLoop.cycleCount, total: goalLoop.maxCycles })
                : i18nT('pages.chatSidebar.loop_waiting_title_cycle_2', { count: goalLoop.cycleCount }))
              : i18nT('pages.chatSidebar.loop_waiting_title')}
          >
            {goalLoop
              ? <Goal size={ROW_ICON_PX} className="shrink-0" style={{ color: 'var(--warn)' }} aria-hidden />
              : <MonitorRadar actionRunning={false} className="text-warn" />}
            {/* Monitors get a visible "paused" gloss instead of the live
                status string: "Waiting on you · Monitor · active" read as a
                contradiction (UX span 4a221cc48433). Goal loops keep the
                cycle fraction but PREFIX it with "Paused at" — reusing the
                working row's "Loop N/M" verbatim made the two rows read as
                the same state (UX span 03f52eaca7f7); the tooltip above
                carries the full sentence. */}
            <span className="truncate"><span className="font-medium crew-tone-warn">{i18nT('pages.chatSidebar.loop_waiting_on_you')}</span><span className="text-muted"> · {goalLoop
              ? (goalLoop.maxCycles > 0
                ? i18nT('pages.chatSidebar.loop_waiting_cycle', { count: goalLoop.cycleCount, total: goalLoop.maxCycles })
                : i18nT('pages.chatSidebar.loop_waiting_cycle_2', { count: goalLoop.cycleCount }))
              : i18nT('pages.chatSidebar.loop_waiting_monitor')}</span></span>
          </div>
        ),
      },
      {
        // An actionable wake is executing agent work now, so it outranks the
        // ordinary work signals below while remaining under decisions the user
        // owes. Scheduled and terminal monitors resolve at the tail.
        key: 'structured_monitor_action',
        when: !!monitor && monitorStatus === 'action_running',
        build: () => (
          <div className={ROW_STATUS_LINE_CLS} title={monitorLabel}>
            <MonitorRadar actionRunning className="text-accent" />
            <span className="truncate font-medium">{monitorLabel}</span>
          </div>
        ),
      },
      {
        // An active goal loop outranks every "working" signal below it but
        // stays under both approval branches: an owed decision must never read
        // as unattended progress. Nothing is lost by ranking it high —
        // `goalLoopDetail` carries whatever the lower branch would have shown,
        // so this reads "Loop 7/24 · 3 agents running". Stalled (see
        // `goalLoopStalled`): the whole row flashes danger-red (the
        // `session-loop-stalled` class on the row container, index.css) and
        // the label reads "interrupted" in static danger text — a stalled loop
        // is a failure that needs attention, not a calm in-progress state.
        key: 'goal_loop',
        when: !!goalLoop,
        build: () => (
          <div className={ROW_STATUS_LINE_CLS} title={goalLoopStalled ? i18nT('pages.chatSidebar.goal_loop_interrupted_title') : goalLoop && goalLoop.maxCycles > 0 ? i18nT('pages.chatSidebar.goal_loop_cycle', { count: goalLoop.cycleCount, total: goalLoop.maxCycles }) : i18nT('pages.chatSidebar.goal_loop_cycle_no_cap', { count: goalLoop?.cycleCount ?? 0 })}>
            <Goal size={ROW_ICON_PX} className={`shrink-0 ${goalLoopStalled ? 'text-danger' : 'text-accent animate-pulse'}`} aria-hidden />
            <span className="truncate"><span className={`font-medium ${goalLoopStalled ? 'text-danger' : 'text-accent'}`}>{goalLoopLabel}{goalLoopStalled ? ` — ${i18nT('pages.chatSidebar.loop_interrupted')}` : ''}</span>{goalLoopDetail ? <span className="text-muted"> · {goalLoopDetail}</span> : null}</span>
          </div>
        ),
      },
      {
        // An ordinary session whose last turn ended without a reply needs a
        // visible handoff after a gateway restart or terminal error. Static
        // danger ink distinguishes "manual action required" from every pulsing
        // or spinning progress state. Live child work suppresses this branch via
        // `turnNeedsAttention`, and goal loops retain their cycle-specific row.
        key: 'interrupted',
        when: turnNeedsAttention,
        build: () => {
          // A crew-bound row must not name Resume: the composer offers no such
          // control there (`selectContinuable` mirrors the server's
          // `relay_archive_read_only` refusal), so the instruction would point
          // at a button that is not on screen. The interruption is still real and
          // still needs the marker — only the instruction is dropped.
          //
          // Shares `slotIsRemoteBound` with the composer deliberately: this row
          // and that gate answer the SAME question, so one spelling keeps the
          // label from drifting if the server's refusal is ever keyed elsewhere.
          // The crew chip below stays inline because it answers a different
          // question — which crew a row runs on, not whether an action is refused.
          const label = slotIsRemoteBound(s)
            ? i18nT('pages.chat.recoveryCard.turn_interrupted')
            : `${i18nT('pages.chat.recoveryCard.turn_interrupted')} · ${i18nT('components.chatInput.resume')}`
          return (
            <div className={ROW_STATUS_LINE_CLS} title={label}>
              <TriangleAlert size={ROW_ICON_PX} className="shrink-0 text-danger" aria-hidden />
              <span className="truncate font-medium text-danger">{label}</span>
            </div>
          )
        },
      },
      {
        // A dynamic-workflow run launched from this session is still executing
        // — surface it even though the parent turn has ended (`s.running` is
        // false while the run executes in the background). Outranks the
        // subagent count: workflow track agents may also register as
        // subagents, and "which workflow / phase" is the stronger signal.
        key: 'workflow',
        when: !!wfActive,
        build: () => (
          <div className={ROW_STATUS_LINE_ACCENT_CLS} title={i18nT('pages.chatSidebar.workflow_running', { count: wfActive?.count ?? 0 })}>
            <Workflow size={ROW_ICON_PX} className="shrink-0 text-accent animate-pulse" aria-hidden />
            <span className="truncate">{wfActive?.label}</span>
          </div>
        ),
      },
      {
        // Every child is still queued: a static badge, never the running pulse,
        // because nothing has started. The tooltip carries the wait's own
        // sentence (how much memory is short, or the concurrency limit).
        key: 'subagents_waiting',
        when: subagentsOnlyQueued,
        build: () => (
          <div className={ROW_STATUS_LINE_MUTED_CLS} title={subagentWaitingTitle} data-testid="session-subagents-waiting">
            <Hourglass size={ROW_ICON_PX} className="shrink-0" aria-hidden />
            <span className="truncate">{subagentWaitingLabel}</span>
          </div>
        ),
      },
      {
        // A spawned subagent is still running (with or without more queued
        // behind it) — surface it even if the parent turn has ended
        // (`s.running` is false while it waits for completion events), so the
        // sidebar shows live activity instead of a stale last message.
        key: 'subagents',
        when: subagentCount > 0,
        build: () => (
          <div className={ROW_STATUS_LINE_ACCENT_CLS} title={subagentLabel}>
            <Bot size={ROW_ICON_PX} className="shrink-0 text-accent animate-pulse" aria-hidden />
            <span className="truncate">{subagentLabel}</span>
          </div>
        ),
      },
      {
        // Reconnect snapshots can report queued work or running
        // children before their detailed activity records arrive. The shared
        // predicate suppresses stale Resume; this branch replaces the equally
        // stale last-message fallback with an honest localized working state.
        key: 'snapshot_live_work',
        when: snapshotOnlyLiveWork,
        build: () => (
          <div className={ROW_STATUS_LINE_ACCENT_CLS} title={snapshotLiveWorkLabel}>
            <Loader size={ROW_ICON_PX} className="shrink-0 text-accent animate-spin" aria-hidden />
            <span className="truncate">{snapshotLiveWorkLabel}</span>
          </div>
        ),
      },
      {
        // A spinner, not a pulsing dot: "actively working" is the one state
        // with a definite direction, and rotation reads as progress where a
        // fading dot reads as a mere marker.
        key: 'running',
        when: isRunning && (!monitorOwnsRunning || s.running),
        build: () => {
          const text = slotStatusText(statusDetail, simplifiedToolNames, uiLang)
          // `title` because this is the one status text that is unbounded — a tool
          // phase can name a long command — and the line truncates. The gutter
          // glyph used to carry that tooltip, so it has to move with it, or a
          // truncated tool status becomes unreadable rather than abbreviated.
          return (
            <div className={ROW_STATUS_LINE_ACCENT_CLS} title={text}>
              <Loader size={ROW_ICON_PX} className="shrink-0 text-accent animate-spin" aria-hidden />{text}
            </div>
          )
        },
      },
      {
        // Passive monitor state is useful only after stronger row signals have
        // had their turn. An unread completion wins over retained terminal state.
        key: 'structured_monitor_passive',
        when: !!monitor && monitor.active && !monitor.terminal
          && monitorStatus !== 'action_running' && !isUnread,
        build: () => (
          <div className={ROW_STATUS_LINE_CLS} title={monitorLabel}>
            <MonitorRadar
              actionRunning={false}
              className={monitorStatus === 'success'
                ? 'text-ok'
                : monitorStatus === 'blocked' || monitorStatus === 'budget_stopped'
                  ? 'text-warn'
                  : 'text-muted'}
            />
            <span className="truncate font-medium">{monitorLabel}</span>
          </div>
        ),
      },
      {
        // LAST on purpose. Naming where the session is and what clicking does is
        // true of every unlinked peer row and therefore the weakest thing this
        // slot can say — any live state the crew reported (running, needs
        // approval, a monitor) is more useful, so each of those claims the slot
        // first and this only lights when none did.
        //
        // It says what the click DOES rather than what the row is not: the promise
        // was otherwise hover-only, and a row that merely reports its own absence
        // gives a first-time reader nothing to act on. The pill cannot carry it —
        // the pill says WHERE the session runs, which stays equally true after the
        // row is opened here.
        //
        // This is a LIFECYCLE state with exactly one transition: once the row is
        // opened locally the local slot wins the identity dedupe, `peerId` is
        // gone, and the line goes with it.
        key: 'peer_not_open_here',
        when: !!peerId,
        build: () => (
          <div className={ROW_STATUS_LINE_MUTED_CLS} data-testid="session-peer-not-open-here">
            {/* Inside a crew group the header already names the machine. */}
            <span className="truncate min-w-0">{inOfflineCrewGroup
              ? i18nT('pages.chatSidebar.crew_row_offline')
              : inCrewGroup
              ? i18nT('pages.chatSidebar.crew_row_open_here')
              : i18nT('pages.chatSidebar.not_open_here_yet', { name: peerName || '' })}</span>
          </div>
        ),
      },
    ] as const).find(entry => entry.when)
    const rowState = rowStateEntry?.build() ?? null
    // The title-line live dot reads the WINNING branch of the resolver above, so
    // it can never disagree with the secondary line: it lights exactly when that
    // line says the session is working (a turn, started sub-agents, or reconnect
    // live work). A branch that outranks those wins the line and leaves the title
    // bare: an owed approval or answer, a loop, a monitor wake, a workflow run or
    // an interrupted turn each carry their own stronger cue. A queued-only row
    // has no dot either, because its own branch says nothing has started.
    const showLiveDot = !!rowStateEntry && LIVE_ROW_STATE_KEYS.has(rowStateEntry.key)

    // `unread` sits LAST, so it lights only when nothing else claims the slot.
    // That is stricter than the dot it replaces, which coexisted with the
    // workflow and sub-agent states; with one marker, showing two for one row is
    // not available and the more specific state is the useful one.
    //
    // It is the ONE state whose marker is not accompanied by its own words: the
    // secondary line it leads is `last_message`, which says what the agent said,
    // not that you have not read it. So unlike every glyph above — each of which
    // sits directly in front of the label naming it, and is therefore
    // `aria-hidden` — this dot keeps a real accessible name and a tooltip.
    const unreadDot = !rowState && isUnread && showUnreadDot !== false
      // A DOT, so it keeps its own size: `ROW_ICON_PX` sizes the lucide glyphs,
      // whose ink covers a fraction of their box, while a filled disc covers all
      // of it. At 10px it reads as heavier than every state that outranks it.
      // `--ok`, not `--accent`: this dot signals STATE (the agent finished and
      // the result is unread), so it reads the semantic status token that the
      // `recent` status chip (`SESSION_FILTERS`) and the connection-status dot (InstancesPanel's
      // `bg-ok`) already use, not the brand/interactive color. A theme where
      // the two hues differ can then keep the status cue distinct from
      // ordinary accent chrome.
      ? <span className="w-2 h-2 rounded-full shrink-0" style={{ background: 'var(--ok)' }}
        role="img" aria-label={i18nT('pages.chatSidebar.agent_finished_your_turn')}
        title={i18nT('pages.chatSidebar.agent_finished_your_turn')} />
      : null
    // The row's tail line when no state claims it: the last message, led by the
    // unread dot. A crew card shows it too until its workers summary has an answer.
    const lastMessageLine = (s.last_message || unreadDot) ? (
      <div className={ROW_STATUS_LINE_MUTED_CLS}>
        {unreadDot}
        {/* `min-w-0` or the ellipsis never renders: this is a flex child, and
            a flex item's `min-width: auto` floor keeps it at content width
            instead of letting `truncate` clip it (i18n render gate,
            layout/ellipsis-with-flex-parent). */}
        {s.last_message ? <span className="truncate min-w-0">{s.last_message}</span> : null}
      </div>
    ) : null
    // Custom hex (color_hex) wins over the palette index. It is deliberately
    // theme-independent: palette swatches re-derive from the theme accent,
    // a custom color is frozen. Muted-text legibility still goes through the
    // same APCA boost via boostFor.
    const customHex = typeof s.color_hex === 'string' && s.color_hex ? s.color_hex : null
    // Agent default color, resolved at RENDER time (not creation): a session
    // with no explicit per-session color inherits its agent's session_color.
    // Deriving it here rather than persisting at creation means it applies to
    // EVERY origin (dashboard, channel, cron, subagent — anything with s.agent),
    // needs no event-loop config I/O, and re-tints live when the agent's color
    // is edited. An explicit per-session color_hex/color_index still wins.
    const agentHex = (!customHex && ci == null && s.agent)
      ? (() => {
          const c = installedAgents.find(a => a.name === s.agent)?.session_color
          return typeof c === 'string' && /^#[0-9a-f]{6}$/i.test(c) ? c : null
        })()
      : null
    const frozenHex = customHex ?? agentHex
    const rowColor = frozenHex ?? (ci != null ? paletteColors[ci] : null)
    const boostStyle: Record<string, string> = {}
    if (frozenHex) {
      boostStyle['--session-color'] = frozenHex
      const cb = boostFor(frozenHex)
      if (cb.mutedColors[0]) boostStyle['--session-muted'] = cb.mutedColors[0]
    } else if (rowColor && ci != null) {
      boostStyle['--session-color'] = rowColor
      if (boost.mutedColors[ci]) boostStyle['--session-muted'] = boost.mutedColors[ci]
    }
    if (recent) boostStyle.boxShadow = recencyTintShadow(recent, recentTintCount)
    // A session that's open in its own window is dimmed here so the main
    // sidebar reads as "handed off" (skipped while active — you may be viewing it).
    if (isOut && !isActive) boostStyle.opacity = '0.6'
    // The shared menu is connected: it pulls read/pin/move/copy/colour/close/tags
    // straight from the store keyed on slotKey (Tags opens the shared popover via
    // the TagPopover context). This row only supplies the one genuinely
    // surface-specific bit — Rename drives this component's inline row-edit state.
    //
    // The menus are built once per change of what they read, not per render. A
    // row re-renders every time its paint position moves (`orderStamp`, which
    // drives the layout spring), so a pin or a new chat re-renders the ~45 rows
    // below it. Building the ⋯ dropdown and the context-menu content on each of
    // those renders costs ~200 Radix fibers per row, about 90% of a pin's
    // render work, and none of it depends on position. An unchanged element is
    // skipped by React, and each menu still re-renders from its own store and
    // context subscriptions.
    const rowKey = s.key
    const rowTitle = s.title
    const rowMenuProps = useMemo(() => ({
      slotKey: rowKey,
      mode,
      onRename: () => onRenameStart(rowKey, scope, rowTitle && rowTitle !== rowKey ? rowTitle : '', true),
      onAutoTitle: () => onAutoTitle(rowKey),
      onOpenInNewTab: onOpenSlotInNewTab ? () => onOpenSlotInNewTab(rowKey) : undefined,
      // A row menu opens from inside this panel, where the folder-order banner
      // (when there is one) sits over the tree -- the menu need not repeat it.
      sidebarOnScreen: true,
      // On a card with sessions under it, the menu's Close says it reaches this
      // one session only, as visible text: a phone has no hover tooltip. On an
      // adopted remote-crew row the same slot carries the "Release locally" label.
      closeHint: closeReachHint,
      closeLabel: releaseLocally ? closeReachLabel : undefined,
      // Removing an adopted remote-crew copy is reversible (the session stays on
      // its peer), so its menu item renders neutral, not destructive-red.
      closeNeutral: releaseLocally,
      // "Reference in chat": stage a link to THIS session in the active chat's
      // composer, the discoverable form of the drag gesture. Offered only when a
      // composer exists (`canStageSessionRef`) and this session may be referenced
      // into it — `sessionRefBlockReason` refuses an incognito/temporary target
      // and the already-active session (a self-reference). A foreign (peer) row's
      // slot is owned elsewhere and stages nothing, so it is withheld there too.
      // `crewWindowCovers` withholds it on EVERY row while a crew window covers
      // the pane: the active composer is then hidden under an opaque cover, so a
      // staged ref would land where the user cannot see it (the no-visible-
      // composer case `canStageSessionRef` guards, which stays true here because
      // activeSlot is still set). Re-decided at click time from the SAME pure
      // function the drop zone uses, so the offer and the handler cannot drift.
      // `isSidebarActiveSlot` is the raw key===activeSlot test (a per-row view
      // field), NOT `isActive`: a crew window over the pane makes `isActive`
      // false while the session is still active, which would otherwise offer a
      // self-reference the drop zone refuses. Carried in the view, not
      // `rowActions`, so a chat switch re-renders only the rows whose answer
      // flips. Fires the same callback the drop does, with the row's snapshot.
      onReferenceInChat: onDropSessionRef && canStageSessionRef && !foreignRow && !crewWindowCovers
        && !sessionRefBlockReason({ key: rowKey, activeSlot: isSidebarActiveSlot ? rowKey : null, memoryMode: s.memory_mode })
        ? () => onDropSessionRef({
            key: rowKey,
            title: rowTitle && rowTitle !== rowKey ? rowTitle : rowKey,
            messages: s.messages,
          })
        : undefined,
    }), [rowKey, mode, onRenameStart, onAutoTitle, scope, rowTitle, onOpenSlotInNewTab, closeReachHint, releaseLocally, closeReachLabel, onDropSessionRef, canStageSessionRef, foreignRow, crewWindowCovers, isSidebarActiveSlot, s.memory_mode, s.messages])
    const rowActions = useMemo(() => (void langGen, !renamingHere && !foreignRow ? (isMobile ? (
      // In flow, not absolute: the phone ⋮ is always drawn, so the content
      // column must end before it. Absolutely placed, the column ran to the
      // row's own pr-3 and a long title painted under the button and its 44px
      // `mc-touch-hit` area. `-mr-1.5` keeps the button 6px from the row's
      // edge, where `right-1.5` put it; `ml-3` (12px) covers the 11px the hit
      // area grows past the 22px button toward the text.
      <div className="shrink-0 self-center ml-3 -mr-1.5 flex items-center gap-0.5">
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <button type="button" className="mc-touch-hit text-muted/50 active:text-text p-1 cursor-pointer bg-transparent border-none" aria-label={i18nT('pages.chatSidebar.more_options')} onMouseDown={e => e.stopPropagation()} onClick={e => e.stopPropagation()}><MoreVertical size={14} /></button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="min-w-[160px]" onClick={e => e.stopPropagation()} onCloseAutoFocus={onMenuCloseAutoFocus}>
            <SessionActionsMenu variant="dropdown" {...rowMenuProps} onDuplicate={() => onDuplicate(rowKey)} />
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
    ) : (
      // The open session's row shows its actions only while a guide step
      // points at one (sessions.row-menu resolves to the open row's copy).
      <IconButtonGroup className={`absolute top-1/2 -translate-y-1/2 right-1.5 opacity-0 group-hover:opacity-100 [@media(hover:none)]:opacity-100 group-focus-visible:opacity-100 group-has-[:focus-visible]:opacity-100 has-[[data-state=open]]:opacity-100 ${isActive && guideOnRowControl ? '!opacity-100' : ''}`}>
        <DropdownMenu guideScope="menu:sessions.row-menu">
          <DropdownMenuTrigger asChild>
            <IconButton title={i18nT('pages.chatSidebar.more')} aria-label={i18nT('pages.chatSidebar.more_options')} {...uiLocation('sessions.row-menu')} onMouseDown={e => e.stopPropagation()} onClick={e => e.stopPropagation()}><MoreVertical size={12} /></IconButton>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="min-w-[160px]" onClick={e => e.stopPropagation()} onCloseAutoFocus={onMenuCloseAutoFocus}>
            <SessionActionsMenu variant="dropdown" {...rowMenuProps} />
          </DropdownMenuContent>
        </DropdownMenu>
        <IconButton variant="accent" title={i18nT('pages.chatSidebar.duplicate')} aria-label={i18nT('pages.chatSidebar.duplicate')} {...uiLocation('sessions.row-duplicate')} onMouseDown={e => e.stopPropagation()} onClick={e => { e.stopPropagation(); onDuplicate(rowKey) }}><GitFork size={12} /></IconButton>
        {/* No hover close control on an ADOPTED remote-crew row (#19958, ruling 4):
            a bare icon with no visible label read as a destructive "cut the chat
            off" and the reader would not click it (UX). Removal for an adopted row
            is only through the labelled row-menu item "Remove from this machine",
            where the plain label and the hint are always visible. Ordinary local
            rows keep their red ✕ close. */}
        {!releaseLocally && (
          <IconButton variant="danger" title={closeReachLabel ?? i18nT('pages.chatSidebar.close')} aria-label={closeReachLabel !== undefined ? i18nT('pages.chatSidebar.close_this_session_only') : i18nT('pages.chatSidebar.close_session')} {...uiLocation('sessions.row-close')} onMouseDown={e => e.stopPropagation()} onClick={e => { e.stopPropagation(); onCloseSession(rowKey) }}><X size={12} /></IconButton>
        )}
      </IconButtonGroup>
    )) : null
    // `langGen` (read with `void` above) because the labels are i18nT strings, which re-translate on a catalog load.
    ), [renamingHere, foreignRow, isMobile, isActive, guideOnRowControl, rowMenuProps, onMenuCloseAutoFocus, onDuplicate, onCloseSession, closeReachLabel, releaseLocally, rowKey, langGen])
    const rowContextMenuContent = useMemo(() => (void langGen, !foreignRow ? (
      <ContextMenuContent className="min-w-[160px]" onClick={e => e.stopPropagation()} onCloseAutoFocus={onMenuCloseAutoFocus}>
        <SessionActionsMenu variant="context" {...rowMenuProps} />
      </ContextMenuContent>
    ) : null), [foreignRow, onMenuCloseAutoFocus, rowMenuProps, langGen])
    return (
      // A drop target in its own order: a pinned row takes pinned drags (the
      // pinned order), an unpinned row unpinned ones (the manual order, in any
      // sort). The collision layer keeps each to its own container.
      <DndDroppable
        id={`${isPinned ? 'pinned-session' : 'manual-session'}:${scope}:${rowIdentity}`}
        data={{ type: isPinned ? 'pinned-session' : 'manual-session', key: s.key, container: reorderContainer }}
        disabled={!dndRow || !pinnedReorderEnabled || isRenaming}
      >
        {({ setNodeRef: setPinnedDropRef, isOver: isPinnedDropOver }) => (
      <motion.div ref={setPinnedDropRef} layout={rowAnimEnabled ? 'position' : false} layoutId={rowAnimEnabled ? `slot-${layoutScope}-${rowIdentity}` : undefined}
        data-slot-key={s.key}
        {...(conductor ? {
          // On the OUTERMOST row element, which is the card plus its divider. Same
          // marker and same meaning as before the lane stopped wrapping the card:
          // this row is nested under the session that opened it.
          'data-conductor-depth': conductor.depth,
          ...(conductor.depth > 0 ? { 'data-testid': 'conductor-nested-row' } : {}),
          ...(conductor.anchorOnly ? { 'data-conductor-anchor': 'true' } : {}),
        } : {})}
        initial={rowAnimEnabled && !rowMotionHeld ? { opacity: 0, x: -12 } : false}
        // The anchor's dimming belongs HERE and not in a class: Motion writes its
        // animation target to the element's own style, so an inline `opacity: 1` from
        // this target outranks any opacity utility on the same row and the anchor paints
        // at full strength. One source of truth, and the property a person actually sees.
        animate={{ opacity: conductor?.anchorOnly ? 0.55 : 1, x: 0 }}
        // While the first population settles the layout transition is instant, not
        // the projection off: Framer reads `layout`/`layoutId` once, at mount, but
        // reads `transition` on every layout change, so the same rows spring later.
        transition={{ layout: rowMotionHeld ? { type: false } : { type: 'spring', stiffness: 500, damping: 35 }, opacity: { duration: 0.2 }, x: { duration: 0.2 } }}>
        {/* Both dnd ids are ORIGIN-QUALIFIED (`rowIdentity`, not `s.key`): a peer
            row's disabled droppable/draggable still registers its id with dnd-kit,
            so a peer key that collided with a local one would register twice and
            the sortable would resolve the wrong node. `data.key` stays the RAW
            key, because the drop handlers look slots up in the local store. */}
        <DndDraggable
          id={`session:${rowIdentity}`}
          data={{ type: 'session', key: s.key, pinned: isPinned, container: reorderContainer }}
          disabled={!dndRow || isRenaming}
        >
          {({ setNodeRef, listeners, isDragging }) => (
        <ContextMenuStableTrigger content={rowContextMenuContent}>
        <div ref={dndRow ? setNodeRef : undefined} {...(dndRow ? listeners : {})}
          data-draggable={(!isRenaming && !foreignRow).toString()}
          className={`session-row focus-ring-accent-inset group relative flex items-start ${ROW_BOX_CLS} text-sm transition-all select-none ${isActive ? !connected ? `session-active ${ROW_ACTIVE_CLS} cursor-not-allowed` : `session-active ${ROW_ACTIVE_CLS} cursor-pointer` : !connected ? 'text-muted opacity-50 cursor-not-allowed' : `${ROW_IDLE_CLS} cursor-pointer`} ${goalLoopStalled ? 'session-loop-stalled' : ''} ${rowColor ? 'session-colored' : ''} ${rowColor && colorMode === 'gradient' ? 'session-gradient' : ''} ${isDragging ? 'opacity-40' : ''} ${revealFlash ? `session-reveal-flash${revealFlash === 'fade' ? ' session-reveal-flash-fade' : ''}` : ''}`}
          data-crew-root={crewRoot ? 'true' : undefined}
          style={boostStyle as React.CSSProperties}
          draggable={
            // Both drag paths are off for a peer-owned row and for a row that
            // opens elsewhere. Note the polarity: native HTML5 drag is enabled
            // precisely when dnd-kit is NOT, so gating `dndRow` alone would have
            // SWITCHED THIS ON rather than off.
            (!dndRow && !isRenaming && !foreignRow) && (connected || isActive)
          }
          title={
            // A peer-owned row OPENS THE SESSION, here, in the local pane: the
            // click creates a local slot bound to that peer session and switches
            // to it, so this row now makes the same promise every sibling row
            // makes. What still differs is where the TURNS run, which is the one
            // thing worth saying on hover — the `RemoteCrewChip` beside the agent
            // name carries the same fact as visible text, so nothing meaningful is
            // hover-only. Declared ahead of `offlineProps` so the gateway-offline
            // tooltip still wins while disconnected (last prop wins).
            //
            // A row that opens elsewhere makes a DIFFERENT promise -- the click
            // leaves this page -- and says so, since nothing else on the row does.
            peerId
              ? i18nT('pages.chatSidebar.opens_here_runs_on_instance', { name: peerName })
              : onOpenElsewhere
                ? i18nT('pages.chatSidebar.opens_on_members_page')
                : undefined
          }
          {...offlineProps(connected, i18nT('utils.offline.switch_sessions'))}
          role="button"
          tabIndex={0}
          data-session-row={rowIdentity}
          data-session-scope={navScope}
          data-session-container={holdContainer}
          aria-current={isActive ? 'true' : undefined}
          aria-disabled={!connected}
          aria-keyshortcuts={keyboardMovable ? 'Alt+ArrowUp Alt+ArrowDown' : undefined}
          onKeyDown={e => {
            if (keyboardMovable && e.altKey && !e.metaKey && !e.ctrlKey && !e.shiftKey
              && (e.key === 'ArrowUp' || e.key === 'ArrowDown')
              && (e.target as HTMLElement) === e.currentTarget) {
              e.preventDefault()
              e.stopPropagation()
              // A pinned row moves in the pinned order; an unpinned one in the
              // manual order, exactly as dropping it one row along would.
              const reorder = isPinned ? onPinnedKeyboardReorder : onManualKeyboardReorder
              reorder(s.key, reorderContainer, e.key === 'ArrowUp' ? -1 : 1, e.currentTarget)
              return
            }
            // ArrowUp/ArrowDown rove focus through the rows of THIS list (see
            // chat/sessionRowNav for why the rove is scope-bounded and clamped).
            // Focus-only, so walking the list doesn't load every session on the
            // way — Enter/Space below still switches. Bare arrows only: the
            // modified forms belong to other gestures (Alt+←/→ cycles sessions,
            // ⌘/Ctrl+arrow is OS text/scroll movement), and Shift is left free.
            // Skipped while a drag is in flight so dnd-kit keeps the arrows for
            // moving the dragged row, and skipped for a keystroke aimed at an
            // inner control so the rename input keeps its own caret keys.
            const roveStep = e.key === 'ArrowDown' ? 1 : e.key === 'ArrowUp' ? -1 : 0
            if (roveStep !== 0 && !dragInFlight && !e.altKey && !e.metaKey && !e.ctrlKey && !e.shiftKey
                && (e.target as HTMLElement) === e.currentTarget) {
              // Only claim the keystroke when focus actually moved; at the list
              // edge it falls through and still scrolls the list.
              if (focusSiblingSessionRow(e.currentTarget as HTMLElement, roveStep)) {
                e.preventDefault()
                e.stopPropagation()
              }
              return
            }
            // WCAG 2.1.1: session rows must be operable via keyboard.
            // Enter/Space activates the row (same as click). Other keys are
            // forwarded to dnd-kit's listener (this prop appears after the
            // {...listeners} spread, so last-prop-wins would otherwise clobber
            // it) — useful for continuing a pointer-initiated drag via arrow
            // keys. Note: keyboard-initiated drag pickup was never functional
            // for these rows (plain useDraggable without SortableContext), so
            // consuming Enter/Space here does not regress it.
            if (e.key !== 'Enter' && e.key !== ' ') {
              if (dndRow) (listeners as Record<string, (e: React.KeyboardEvent) => void> | undefined)?.onKeyDown?.(e)
              return
            }
            if ((e.target as HTMLElement) !== e.currentTarget) return // don't hijack inner buttons
            e.preventDefault()
            if (!connected) return
            if (peerId) { onOpenPeerSession?.(peerId, s.key); return }
            if (onOpenElsewhere) { onOpenElsewhere(); return }
            dispatch(switchSlot({ key: s.key, announceOnMissing: true, gesture: true }))
            onSelectSlot?.(s.key)
          }}
          onDragStart={!dndRow ? (e => { e.dataTransfer.setData('text/plain', s.key); e.dataTransfer.effectAllowed = 'move'; nativeDragInFlight.current = true; onNativeDragStart?.(s.key) }) : undefined}
          // Reaches the row only while it is still attached; the detached case is
          // the unmount cleanup above.
          onDragEnd={!dndRow ? (() => { nativeDragInFlight.current = false }) : undefined}
          // Chrome and Edge on Windows enter autoscroll on middle-button
          // MOUSEDOWN, before `auxclick` fires — so cancelling it in the
          // auxclick handler alone opens the tab AND leaves the pointer in
          // autoscroll mode on a scrollable sidebar. This is the only place that
          // can stop it. Middle button only: the primary button's mousedown
          // belongs to dnd-kit's drag listeners, spread above.
          onMouseDownCapture={onOpenSlotInNewTab ? (e => { if (e.button === 1) e.preventDefault() }) : undefined}
          // Middle-click opens the session as a tab in the BACKGROUND, the way
          // every browser and editor treats it — a user triaging by
          // middle-clicking three rows means "queue these up", and yanking them
          // to each one in turn defeats the gesture. Bound separately from
          // onClick because a middle press produces no click event.
          onAuxClick={onOpenSlotInNewTab ? (e => {
            if (e.button !== 1 || !connected) return
            e.preventDefault()
            if (foreignRow) return
            onOpenSlotInNewTab(s.key, { background: true })
          }) : undefined}
          onClick={e => {
            // A browser emits two click events before dblclick. Let the first
            // select an inactive session, but do not fetch it a second time
            // before the title's double-click handler opens rename.
            if (e.detail > 1 && (e.target as HTMLElement).closest?.('[data-session-title]')) return
            if ((e.target as HTMLElement).closest?.('[data-fork]')) { onDuplicate(s.key); return }
            if ((e.target as HTMLElement).closest?.('[data-close]')) { onCloseSession(s.key); return }
            // When the gateway is offline, switching sessions silently fails
            // (the HTTP fetch never returns) and the user is stuck staring at
            // the previous session's transcript. Block ALL session clicks so
            // the banner + cursor-not-allowed cue make the offline state obvious.
            // Previously only non-active rows were blocked, but re-clicking the
            // already-active row also dispatches switchSlot → fetchSlotDetail
            // fails offline → switchSlot.rejected clears messages to [] → the
            // ChatPage falls into its WelcomeView branch (activeSlot truthy +
            // messages empty) showing "What can I do for you?". Closing/deleting
            // /forking still works — those are local ops (or short-circuit) that
            // don't depend on gateway state.
            if (!connected) return
            // A peer-owned row has no local slot, so `switchSlot` would resolve
            // nothing and clear the transcript. It opens as a window onto the
            // peer's own slot instead (CrewChatWindow). (The federated
            // Older-Sessions rows still switch panes; a history row has no live
            // peer slot.) A remote-EXECUTED local slot falls through to
            // `switchSlot` below, because its transcript IS here.
            if (peerId) { onOpenPeerSession?.(peerId, s.key); return }
            // A row whose session belongs to another page: the pane cannot show
            // it, so `switchSlot` would land on a transcript the surface filter
            // hides and leave the user on the previous one. Go where it lives.
            if (onOpenElsewhere) { onOpenElsewhere(); return }
            // Modifier-click = open as a background tab, matching the
            // editor/browser convention. Platform split lives in the predicate.
            if (onOpenSlotInNewTab && isOpenInTabModifierClick(e)) {
              e.preventDefault()
              onOpenSlotInNewTab(s.key, { background: true })
              return
            }
            dispatch(switchSlot({ key: s.key, announceOnMissing: true, gesture: true }))
            onSelectSlot?.(s.key)
          }}
          onDoubleClick={e => {
            if (foreignRow) return
            if (!(e.target as HTMLElement).closest?.('[data-session-title]')) return
            if (renamingHere) return
            e.preventDefault()
            e.stopPropagation()
            onRenameStart(s.key, scope, s.title && s.title !== s.key ? s.title : '', false)
          }}>
          {isPinnedDropOver && activeDraggedKey !== null && activeDraggedKey !== s.key && (
            <span
              data-testid={isPinned ? 'pinned-session-insertion' : 'manual-session-insertion'}
              aria-hidden="true"
              className={`absolute left-3 right-3 h-0.5 rounded-full bg-accent pointer-events-none z-20 ${(isPinned ? activeDraggedPinnedIndex < pinnedOrderIndex : activeDraggedDrawnIndex < drawnOrderIndex) ? '-bottom-[1px]' : '-top-[1px]'}`}
            />
          )}
          {/* Held-modifier digit badge: while the chat-jump modifier is down,
           *  the first nine sessions in shortcut order show the digit that
           *  jumps to them. Overlays the row's right edge; pointer-events-none
           *  so it never intercepts the click it is describing, aria-hidden
           *  because the shortcuts modal is the accessible reference. */}
          {digitBadge != null && (
            <span aria-hidden="true" data-testid="digit-jump-badge"
              className="absolute right-1.5 top-1/2 -translate-y-1/2 z-10 min-w-[18px] h-[18px] px-1 rounded flex items-center justify-center text-[11px] font-semibold tabular-nums bg-bg-elevated border border-border text-text shadow-sm pointer-events-none">
              {digitBadge}
            </span>
          )}
          {/* NO STATUS GUTTER. The row's one status marker — spinner, bot, shield,
           *  loop, question, unread dot — leads the SECONDARY LINE, immediately in
           *  front of the words it marks ("Thinking…", "3 agents running", "Needs
           *  approval"), and it is built inside each branch's `subtitle` above so a
           *  branch cannot supply one without the other.
           *
           *  It used to sit in an absolutely-positioned gutter inside the row's
           *  then-`pl-3.5`, occupying x 1..13 with the content column starting at 14.
           *  That band is not free: the recency tint paints an opaque accent stripe
           *  up to 7px wide at this same left edge (`recencyTintShadow`), and the
           *  session-colour bar takes the first 2px (`.session-colored::before`).
           *  An accent spinner drawn over an accent stripe is a 1:1 contrast, so on
           *  a recent session the glyph lost its left half and read as clipped and
           *  mis-placed rather than tinted.
           *
           *  Inline, the glyph starts at the content column (10px) — clear of both
           *  markers by construction, at every tint rank, with no coordination
           *  between the two features. It also drops the gutter's `role="img"` +
           *  `aria-label` for every state except `unread`: a glyph sitting in front
           *  of its own visible label is decorative, so it is `aria-hidden` and the
           *  label is read once instead of twice.
           *
           *  The alignment guides are untouched: the gutter was out of flow and
           *  contributed nothing to the content column, so removing it moves no x —
           *  see ChatSidebar.folderAlignment.test.tsx, which still asserts the
           *  row's `pl-3.5` is the content column's whole left offset. */}
          {/* The conductor lane's indent and chevron, INSIDE the row. Two reasons they
           *  are here rather than in a wrapper around the card: the divider is a
           *  sibling of this row, so it keeps spanning the full width at every depth;
           *  and the card keeps the row's whole width, so its title truncates and its
           *  hover controls sit exactly where they do in every other lane. */}
          {conductor && conductor.depth > 0 && (
            <span aria-hidden="true" className="shrink-0"
              data-conductor-indent={Math.min(conductor.depth, CONDUCTOR_MAX_INDENT_DEPTH)}
              style={{ width: `${Math.min(conductor.depth, CONDUCTOR_MAX_INDENT_DEPTH) * 14}px` }} />
          )}
          {conductor && (conductor.childCount > 0 ? (
            <button
              type="button"
              className="mt-2.5 mr-0.5 w-4 h-4 shrink-0 rounded flex items-center justify-center border-none bg-transparent text-muted hover:text-text cursor-pointer"
              // Both stopped, like the row's other inner controls: this button owns the
              // press, and the row's own click would otherwise switch session as well.
              onMouseDown={e => e.stopPropagation()}
              onClick={e => { e.stopPropagation(); toggleConductor(rowIdentity) }}
              title={conductor.expanded
                ? i18nT('pages.chatSidebar.collapse_sessions_this_one_opened')
                : i18nT('pages.chatSidebar.expand_sessions_this_one_opened')}
              aria-label={conductor.expanded
                ? i18nT('pages.chatSidebar.collapse_sessions_this_one_opened')
                : i18nT('pages.chatSidebar.expand_sessions_this_one_opened')}
              aria-expanded={conductor.expanded}
              data-testid={`conductor-chevron-${rowIdentity}`}
            >
              <DisclosureChevron open={conductor.expanded} size={12} />
            </button>
          ) : (
            // Keeps a childless row aligned with its siblings' rather than
            // shifted left by the missing chevron.
            <span className="mt-2.5 mr-0.5 w-4 h-4 shrink-0" aria-hidden="true" />
          ))}
          {crewRoot && <span className="mt-2 mr-2"><CrewGhostBadge size={26} /></span>}
          {/* The `:has` rule hides the title line below while a crewmate head (`MemberRowHead`),
              which already carries the name, is drawn on the first line. Not while
              renaming: the rename box lives in that title line. */}
          <div className={`flex-1 min-w-0 overflow-hidden ${renamingHere ? '' : '[&:has([data-member-head])>[data-session-title]]:hidden'}`}>
            <div className={`session-agent-label ${ROW_META_CLS} font-semibold truncate flex items-center gap-1 ${agentColor}`}>
              {/* Plain keyed span, deliberately unanimated: 200+ per-row
                *  AnimatePresence trees each paid child-diffing bookkeeping on
                *  every sidebar commit for a crossfade that fires only on the
                *  rare agent switch, and the repo's animation invariant is
                *  framer-only (no new CSS @keyframes). */}
              {/* A crewmate's own thread has no agent chip (its title is the name), so the
                *  line starts at the Members-page chip with no placeholder gap before it. */}
              {/* A Crew Mode root names the mode, not the conductor behind it: the
                *  conductor is Crew Mode's to choose and may be swapped. */}
              {crewRoot && <CrewEyebrow />}
              {memberTitle === null && !crewRoot && (
                <span key={agentName || 'empty'} title={agentDisplay || undefined} className={`truncate shrink-0 ${resolvedSlotTags.length > 0 || agentDiverged ? 'max-w-[50%]' : ''}`}>{agentDisplay || '\u00A0'}</span>
              )}
              {/* WHY this card is sitting above its folder's other rows, in a
                *  word, beside the agent name that is the card's own first line.
                *  The accent bar down its left edge groups the card with the
                *  folder row; on its own that bar is a decoration a reader has to
                *  guess at, and the agent name says which AGENT is running, never
                *  what the session is to the folder.
                *
                *  Same shape and the same accent tokens as every other marker on
                *  this line (the peer-crew chip, the Members-page chip): nothing
                *  here is a new colour, and the tag is the accent the bar already
                *  is. Not a title attribute -- the menu item one row up carries
                *  the sentence, and a hover-only gloss is unreachable on a touch
                *  screen.
                *
                *  No size, line-height or weight of its own: it INHERITS the
                *  agent-name line's `ROW_META_CLS` and `font-semibold`, which is
                *  the one place that type scale is spelled. Restating those two
                *  utilities here instead is the drift
                *  `test/listShellParity.test.ts` exists to stop -- that pin
                *  reddened on exactly this span, and it reads the whole FILE as
                *  text, so the literal must not appear in a comment either. */}
              {view.folderConductor && (
                <span
                  className="shrink-0 inline-flex items-center px-1 rounded bg-accent-subtle text-accent"
                  data-testid={`folder-conductor-tag-${rowIdentity}`}>
                  {i18nT('pages.chatSidebar.conductor_tag')}
                </span>
              )}
              {/* Peer-OWNERSHIP badge: this session belongs to another machine.
                *  The SAME component the `RemoteCrewChip` further down this row
                *  uses, which says a LOCAL session dispatches its turns to a peer.
                *  Internally those are opposite directions of travel, but the chip
                *  exists precisely because they make one claim to the person
                *  reading the rail \u2014 "this is not on my machine" \u2014 and that
                *  component's own docstring already counts a peer-OWNED federated
                *  search row among its consumers. A live peer row is the same fact
                *  in the live list, so it gets the same marker rather than a
                *  second span with the same classes.
                *
                *  A row can never render BOTH: this one reads `peer_id`, the chip
                *  below reads `executor === 'remote'` + `instance_id`, and keeping
                *  those two fields apart is what makes them exclusive. Before the
                *  split they were one field, so a remote-EXECUTED local slot
                *  rendered two identical chips. */}
              {/* The badge carries a tooltip because it is the ONLY always-visible
                *  marker that this row's session lives on another machine — and a
                *  bare crew name does not say that. A reader who has not met the
                *  feature sees a pill with a word in it and cannot tell what a row
                *  WITHOUT one means either, so the text names both halves: whose
                *  session it is, and that clicking opens it here. */}
              {peerId && !inCrewGroup && (
                <RemoteCrewChip
                  name={peerName || ''}
                  label={i18nT('pages.chatSidebar.on_instance', { name: peerName || '' })}
                  title={i18nT('pages.chatSidebar.opens_here_runs_on_instance', { name: peerName || '' })}
                />
              )}
              {/* A row that opens on the Members page says so in TEXT, the way a
                *  peer row wears its peer name above. The hover title alone would
                *  leave a touch reader with an unexplained page jump. The text names
                *  the DESTINATION ("Members page", with the leave-this-page glyph),
                *  not the bare noun "Members", which in this product also means the
                *  crew's participants and read as "people are in it" on a cold read.
                *  A crewmate the roster lists instead wears its face and name on this
                *  line (`MemberRowHead`), and the title line below is hidden; this chip
                *  is the fallback for every other case. */}
              {!peerId && onOpenElsewhere && (
                <MemberRowHead agent={memberTitle !== null ? s.agent ?? '' : ''} name={shownTitle} tooltip={tooltipTitle} running={isRunning} />
              )}
              {/* NO destination marker beside the agent name any more. It read
                *  "· opens the astro dashboard", and it was there because the click
                *  LEFT this session behind — it went to that crew's pane instead.
                *  The click now ADOPTS the session into the local pane (see the
                *  row's `onClick`), so the sentence would be false, and a row that
                *  behaves like every other row needs no caveat. The chip above
                *  still says where the TURNS run, which is the fact that survived,
                *  and the row's `title` says the same in a sentence. */}
              {agentDiverged && (
                // Plain secondary TEXT, deliberately not a badge, a colour or an
                // icon. It is informational — the session works, it is simply
                // answered by someone else — so it must not read as an error, and
                // it must not be the row's loudest element.
                //
                // Accessibility follows from being real text: it is in the
                // accessible name of the meta line, read in document order by a
                // screen reader, and legible with colour vision ignored (it
                // inherits the line's muted tone rather than encoding meaning in
                // a hue). Nothing here is hover-only — the `title` merely repeats
                // the visible string so a truncated row can still be read in
                // full, which is why it is not the only carrier of the meaning.
                //
                // `font-normal` because the line is `font-semibold` for the agent
                // name; `shrink-0` because only the tag group owns the truncate
                // budget on this flex row.
                <span
                  data-testid="session-effective-agent"
                  // Shrinkable and ellipsizing, NOT `shrink-0`. The trailing meta
                  // group is `ml-auto … shrink-0` (further down this row), so an
                  // unbounded marker here squeezes the timestamp and channel
                  // glyphs off a minimum-width sidebar. This is the row's least
                  // important fact, so it is the one that yields: `min-w-0` lets
                  // flexbox shrink it, `max-w-[45%]` stops it from claiming the
                  // line before shrinking starts, and `truncate` ellipsizes what
                  // is left — the same shape as the tag group below, and the
                  // reason the `title` is worth keeping.
                  className="min-w-0 max-w-[45%] truncate font-normal text-muted"
                  title={i18nT('pages.chatSidebar.answered_by', { agent: effectiveAgent })}
                >
                  <span aria-hidden>{'\u00A0·\u00A0'}</span>
                  {i18nT('pages.chatSidebar.answered_by', { agent: effectiveAgent })}
                </span>
              )}
              {resolvedSlotTags.length > 0 && (
                // Every tag, each as `· <name>` tinted with the tag's own colour
                // and NO border — plain text sitting as context beside the agent
                // name, not an actionable pill. The group is the only node here
                // allowed to truncate (min-w-0), so a long tag run clips before it
                // pushes the timestamp off the row; the agent name and trailing
                // group stay shrink-0.
                //
                // It is a plain inline block (`truncate` = whitespace-nowrap +
                // overflow-hidden + text-overflow-ellipsis), NOT a flex row:
                // ellipsis does not render across flex children, so an inline-flex
                // group hard-clipped mid-word ("KiroC", "kc-them") instead of
                // showing "…". The children stay inline `<span>`s so a multi-tag
                // run ellipsizes as one line while each tag keeps its own colour
                // (applied inline, since it is per-tag data, not a theme token).
                <span className="truncate min-w-0 font-normal" title={resolvedSlotTags.map(t => t.name).join(' · ')}>
                  {resolvedSlotTags.map(t => (
                    <span key={t.id} data-testid={`slot-tag-${t.id}`}>
                      <span aria-hidden>{'\u00A0·\u00A0'}</span>
                      <span style={{ color: t.color }}>{t.name}</span>
                    </span>
                  ))}
                </span>
              )}
              {isOut && <span className="text-accent" title={i18nT('pages.chatSidebar.popped_out_to_a_separate_window')}><ExternalLink size={10} /></span>}
              {/* One brand mark per channel this session is CONNECTED to, read
               *  from `s.links` and nothing else. A second glyph used to be drawn
               *  here from the slot KEY for the channel the session was born in.
               *  That is a prefix read of the identity, and the property it
               *  rendered is not one the session address model has — its §5.3
               *  names capability, attachment and ingress, and "where did this
               *  start?" is the question it retires (docs/request-for-change/
               *  rfc-session-address-model.md). It also could not react to a
               *  disconnect: a Slack-born row kept its mark after the user chose
               *  "Disconnect from Slack", while the identical mark on a
               *  dashboard-born row one line down vanished. So the strip reads
               *  the one state the menu row toggles — `paused` — and nothing
               *  about where the session came from.
               *
               *  It replaces a `linked_to_slack` Link glyph that fired for ANY
               *  channel, because every non-Slack transport writes its id into
               *  slack_channel_id. */}
              {connectedChannelLinks(s.links).map(link => (
                <span
                  key={link.channel}
                  className="inline-flex text-[10px]"
                  role="img"
                  aria-label={i18nT('pages.chatSidebar.connected_to', { label: link.label })}
                  title={i18nT('pages.chatSidebar.connected_to', { label: link.label })}
                >
                  <ChannelBrandIcon channel={link.channel} size={10} />
                </span>
              ))}
              {/* Runs-elsewhere marker, first in the strip for the same reason it
               *  is first on a federated search row: it qualifies the whole row,
               *  so a user scanning the list should meet it before the per-session
               *  flags that only make sense once you know where the session is. */}
              {s.executor === 'remote' && !inCrewGroup && <ExecutorCrewChip instanceId={s.instance_id || ''} />}
              {s.memory_mode === 'incognito' && <span className="text-muted" title={i18nT('pages.chatSidebar.incognito_no_memory_writes')}><EyeOff size={10} /></span>}
              {s.memory_mode === 'temporary' && <span className="text-aim" title={i18nT('pages.chatSidebar.temporary_no_memory_reads_or_writes')}><VenetianMask size={10} /></span>}
              {/* Trailing meta grouped under ONE ml-auto: two sibling auto
               *  margins would split the free space and strand the timestamp
               *  mid-row.
               *
               *  No folder chip here. The meta line already names the session's
               *  REPO, which is the more precise of the two facts — a folder is a
               *  grouping the user chose, a repo is where the work actually is —
               *  and in practice the two names coincide often enough that showing
               *  both read as a stutter. Folder membership is carried by the tree
               *  itself in folder view; in flat view the row's own context menu
               *  still names it. */}
              {slotActivityTs(s) || isPinned || s.muted || s.process === 'released' || conductorMeta ? (
                <span className="ml-auto inline-flex items-center gap-1 shrink-0">
                  {/* FIRST in the group, so it sits immediately left of the timestamp
                   *  and the timestamp keeps the position it has in every other lane.
                   *  Inside the card's own meta group rather than beside the card:
                   *  outside it, this cluster occupied the column the time uses. */}
                  {conductorMeta}
                  {slotActivityTs(s) && <span data-testid="session-row-time" className="text-muted font-normal shrink-0">{fmtRelativeTime(slotActivityTs(s))}</span>}
                  {/* Last in the row: the pin is a state marker, not a label, so
                   *  it sits after the text that reads left-to-right rather than
                   *  pushing the agent name off its own start edge. */}
                  {/* At rest, not hover-only: a muted session must read differently
                   *  from a quiet one. Beside the pin, the other state marker. */}
                  {s.muted && <span className="shrink-0" data-testid="session-row-muted" role="img" aria-label={i18nT('pages.chatSidebar.muted')} title={i18nT('pages.chatSidebar.muted')}><VolumeX size={10} className="text-muted" /></span>}
                  {s.process === 'released' && <span className="shrink-0" data-testid="session-row-released" role="img" aria-label={i18nT('pages.chatSidebar.process_released')} title={i18nT('pages.chatSidebar.process_released')}><Moon size={10} className="text-muted" /></span>}
                  {isPinned && <span className="shrink-0" title={i18nT('pages.chatSidebar.pinned')}><Pin size={10} className="text-accent" /></span>}
                </span>
              ) : null}
            </div>
            {/* NEVER wraps. `truncate` rather than a two-line clamp, so every row
                is the same height. A clamped title also moved the whole
                secondary line down by a full line box on some rows, which is what
                made the list read as ragged. The full string stays reachable
                through the `title` attribute, and the rename box below is the one
                place it is shown in full. */}
            <div
              data-session-title
              className={`${ROW_TITLE_CLS} font-semibold text-text ${renamingHere ? '' : 'truncate'}`}
              title={tooltipTitle}
            >
              {/* No separate fork glyph: forked titles already carry the
                  persisted "↳ " marker (chat_fork.py _FORK_TITLE_MARKER). Keeping
                  the arrow in the title text — rather than as a UI-only glyph —
                  means it pre-fills the rename box (setRenameValue at the
                  onRename handler) so users can edit or drop it when they rename.
                  A separate ↳ glyph also double-stacked into "↳↳ Fork of …". */}
              {renamingHere ? (
                <textarea ref={renameInputRef} rows={1} className={`w-full bg-bg-elevated border border-accent rounded px-1 py-0 ${ROW_TITLE_CLS} text-text-strong outline-hidden select-text resize-none block overflow-hidden focus-ring`} value={renameValue} onChange={e => onRenameChange(e.target.value)} {...ime.bindEnter<HTMLTextAreaElement>({ onEnter: () => { slotRenameEnterRef.current = true; (document.activeElement as HTMLTextAreaElement)?.blur() }, onEscape: onRenameCancel, onBlur: () => onRenameCommit(s.key, renameValue) })} onMouseDown={e => e.stopPropagation()} />
              ) : (
                <>
                  {/* The live dot leads the name, where the eye lands when
                      scanning a long list. It carries the row's working state
                      on its own (the status line below may be truncated or
                      read separately), so it keeps a real accessible name. */}
                  {showLiveDot && (
                    <span className="session-live-dot animate-pulse" role="img" aria-label={i18nT('pages.chatSidebar.running_marker')} title={i18nT('pages.chatSidebar.running_marker')} data-testid="session-live-dot" />
                  )}
                  {/* A menu-started Regenerate title closes its menu at once, so
                      the row itself says the model call is running. */}
                  {titleGenerating && (
                    <Loader size={ROW_ICON_PX} className="lucide-inline mr-1 shrink-0 text-accent animate-spin" role="img" aria-label={i18nT('pages.chatPage.regenerating_title')} data-testid="row-title-generating" />
                  )}
                  {shownTitle}
                </>
              )}
            </div>
            {/* Secondary line: one ordered resolver decides both the words and the
                marker leading them (#3830), so the two can no longer disagree.
                The tail is `last_message`, which is also where the `unread` dot
                lands — the one marker with no state branch of its own. A row that
                is unread with nothing said yet still renders the line, because the
                dot IS the content then. A crew card's workers summary takes the
                tail's place: still ONE line, below the row's own state. */}
            {rowState ?? (crewRoot ? <CrewRootSummaryLine root={s.key} fallback={lastMessageLine} /> : lastMessageLine)}
            {s.source_links && s.source_links.length > 0 && (
              <SessionSourceChips
                slotKey={s.key}
                links={s.source_links}
                total={s.source_links_total}
                connected={connected}
                isActive={isActive}
                onOpenSource={onOpenSource}
                onActivateSlot={() => { dispatch(switchSlot({ key: s.key, announceOnMissing: true, gesture: true })); onSelectSlot?.(s.key) }}
              />
            )}
            {/* No tag chips here: every tag renders in the meta line above as
             *  tinted `· name` text. A chip row would print each tag twice. */}
          </div>
          {/* Under Manual an unpinned row wears a drag handle in its LEFT PAD,
           *  out of flow, so the agent name, title and status line stay on the
           *  content column and keep the folder name's left edge
           *  (docs/decisions/2026-10-09-folder-glyph-sits-on-its-connector-line.md).
           *  The box runs from 4px to 14px, so it ends where the content column
           *  starts, and the glyph's dots start at 7px, where the recency tint's
           *  widest stripe ends (`recencyTintShadow`); the colour bar takes only
           *  the first 2px. Not at the right end, which belongs to the ⋮ button's
           *  touch area and the hover toolbar: they would take a press aimed at
           *  it. The whole row still drags, so the handle only says it can.
           *  Decorative to assistive tech, which reads the row's Alt+Arrow
           *  shortcut instead. */}
          {manualHandle && (
            <span
              className="absolute left-1 w-2.5 top-1/2 -translate-y-1/2 flex"
              data-testid="session-row-drag-handle"
              aria-hidden="true"
              title={i18nT('pages.chatSidebar.drag_to_reorder')}>
              <GripVertical size={10} className="shrink-0 text-muted" />
            </span>
          )}
          {/* Hide the hover action popup (⋯ / duplicate / close) while THIS slot
           *  is being renamed: it is absolute-positioned at right-1.5 and reveals
           *  on `:focus-visible` inside the row, which a focused text input
           *  always matches, so the rename input would otherwise make it
           *  pop up and overlap the input's right edge. Mirrors the folder-header
           *  guard (!(editingId === folder.id && editScope === 'list')). */}
          {/* A PEER-OWNED row shows NO action group at all: every entry in it
           *  (⋯ menu, duplicate, close, rename, pin, move-to-folder) is a
           *  local-slot operation that cannot reach a session on another machine.
           *  Omitting beats disabling — the same call `historyRow` makes for its
           *  delete button. A remote-EXECUTED local slot keeps the whole group:
           *  its slot is local, so every one of those operations still applies. */}
          {rowActions}
        </div>
        </ContextMenuStableTrigger>
          )}
        </DndDraggable>
        {/* The divider starts at the CONTENT x, not the row's edge, so it
         *  underlines the text block rather than boxing the whole row — the row's
         *  left pad reads as a margin, and a rule running through it would box
         *  the row instead. Matches the Figma, which carries this border on the
         *  `content` frame rather than on the row.
         *
         *  14px is the row's content offset: the row's whole `pl-3.5`, since
         *  nothing else lives in that pad. The right inset is the row's own
         *  padding. */}
        {/* `-mt-px` so the rule does NOT add a row of layout height. In flow it made
         *  the row-to-row pitch row-height + 1, and since the active row suppresses
         *  its neighbours' dividers the pitch also VARIED down the list (measured
         *  60 and 61 on one list), which no fixed row height can compensate for.
         *  Overlaying the row's last pixel keeps the pitch equal to the row height.
         *  The left inset is unchanged — it still starts at the content x. */}
        {showDivider && <div data-row-divider="" className="ml-[14px] mr-3 -mt-px border-b border-border" />}
      </motion.div>
        )}
      </DndDroppable>
    )
}, (a, b) => a.actions === b.actions && sameRowView(a.view, b.view))
