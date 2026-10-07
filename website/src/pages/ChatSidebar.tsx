import { useState, useRef, useEffect, memo, useMemo, useCallback, useId, useContext, Fragment } from 'react'
import { createPortal } from 'react-dom'
import { LayoutGroup, AnimatePresence, motion } from 'framer-motion'
import { Plus, X, Pin, Monitor, ArrowUpDown, Eye, EyeOff, VenetianMask, Ghost, FolderPlus, FolderX, MessageSquare, MessageSquarePlus, Folder, ChevronRight, ChevronDown, ChevronUp, Clock, Pencil, BrushCleaning, Link2, Circle, MoreVertical, Tag as TagIcon, Columns3, CornerDownRight, GripVertical, Check, GitFork, List, ListTree, Loader, Loader2, Settings, RotateCcw, Bot, ExternalLink, Cpu, GitMerge, Workflow, CircleDot, Users, TriangleAlert, Goal, MessageCircleQuestionMark, ShieldCheck, Server, Pause, Play, Hourglass } from 'lucide-react'
import GithubLogo from '../components/icons/GithubLogo'
import GitlabLogo from '../components/icons/GitlabLogo'
import { FolderBody } from '../components/FolderBody'
import { FolderRail } from '../components/FolderRail'
import ErrorNotice, { ErrorNoticeMenuItem } from '../components/ErrorNotice'
import JiraLogo from '../components/icons/JiraLogo'
import { sourceProviderMeta } from '../utils/sourceProviderMeta'
import FolderGlyph from '../components/FolderGlyph'
import { DndContext, DragOverlay, MeasuringStrategy } from '@dnd-kit/core'
import { SortableContext, verticalListSortingStrategy } from '@dnd-kit/sortable'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { usePreviewFlag } from '../hooks/usePreviewFlag'
import { PREVIEW_DASHBOARD } from '../utils/previewFlags'
import { shallowEqual, useStore } from 'react-redux'
import { useAppDispatch, useAppSelector } from '../store'
import type { RootState } from '../store'
import { useConnected } from '../hooks/useConnected'
import { DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem, DropdownMenuLabel, DropdownMenuSeparator, DropdownMenuSub, DropdownMenuSubTrigger, DropdownMenuSubContent } from '../components/ui/dropdown-menu'
import { ContextMenu, ContextMenuTrigger, ContextMenuContent, ContextMenuItem, ContextMenuSeparator, ContextMenuSub, ContextMenuSubTrigger, ContextMenuSubContent } from '../components/ui/context-menu'
import { offlineProps } from '../utils/offline'
import { switchSlot, deleteSlot, fetchHistory, resumeFromHistory, deleteHistorySession, selectSidebarWorkflowActive, selectAutomationForSlot } from '../store/chatSlice'
import { slotIsRemoteBound } from '../store/dashboardSlice'
import { IS_MAC } from '../hooks/useKeyboardShortcuts'
import { api, SEARCH_MIN_CHARS } from '../api/client'
import { errMessage } from '../utils/thunkError'
import { computeRecentRank, recencyTintShadow, clampTintCount } from '../utils/recencyTint'
import { folderOffersHide } from '../utils/folderVisibility'
import { groupHistoryByFolder } from '../utils/groupHistoryByFolder'
import { highlightText } from '../utils/highlightText'
import { slotChannelLabel, slotChannelNamespace } from '../utils/channelOrigin'
import { toolStatusLabel, type ToolStatusDetail } from '../utils/toolStatusLabel'
import { sessionRefBlockReason } from '../utils/sessionRefs'
import { SearchInput, Input, Btn, IconButton, IconButtonGroup } from '../components/ui'
import SimpleSelect from '../components/SimpleSelect'
import FolderConfigModal from '../components/FolderConfigModal'
import ModelDropdownList from '../components/ModelDropdownList'
import { useAvailableModelsQuery } from '../hooks/useAvailableModels'
import { useListboxKeyboard } from '../hooks/useListboxKeyboard'
import { useDndSensors } from '../hooks/useDndSensors'
import { useSessionPalette } from '../hooks/useSessionPalette'
import { ancestorsOf, descendantsOf, orphanCitation } from '../lib/sessionLineage'
import { useReducedMotion } from '../hooks/useReducedMotion'
import { useSimplifiedToolNames } from '../hooks/useSimplifiedToolNames'
import { useLanguage } from '../i18n/LanguageProvider'
import { useSessionActions } from '../hooks/useSessionActions'
import { useCloseSessionTree } from '../hooks/useCloseSessionTree'
import { sidebarRowBackgroundOnly, sidebarRowRunning } from '../lib/sessionCloseTree'
import { useChatPopouts } from '../hooks/useChatPopouts'
import { platformShortcut } from '../utils/platform'
import { useImeGuard } from '../hooks/useImeGuard'
import { useIsMobile } from '../hooks/useIsMobile'
import { useIsTouchDevice } from '../hooks/useIsTouchDevice'
import ResizeHandle from '../components/ResizeHandle'
import { SearchFilterBar, FilterMenuButton, FilterChip, FILTER_CHIP_ROW_CLS, FilterMenuLabel, FilterMenuContent } from '../components/SearchFilterBar'
import { ListDock } from '../components/ListDock'
import { LIST_SHELL_CLS, LIST_HEADER_CLS, LIST_TITLE_CLS, LIST_BODY_CLS, ROW_BOX_CLS, ROW_IDLE_CLS, ROW_ACTIVE_CLS, ROW_META_CLS, ROW_TITLE_CLS, ROW_STATUS_CLS } from '../components/listShell'
import { safeSetItem } from '../utils/safeStorage'
import FolderMoveSubmenu from '../components/FolderMoveSubmenu'
import MoveUndoBar from '../components/MoveUndoBar'
import SessionActionsMenu from '../components/SessionActionsMenu'
import ImportSessionItem from '../components/ImportSessionItem'
import { usePendingSourceUnlinks } from '../components/SourceLinksSubmenu'
import { ChannelBrandIcon, hasChannelBrandIcon } from '../components/ChannelBrandIcon'
import { RemoteCrewChip } from '../components/RemoteCrewChip'
import TagManagerList from '../components/TagManagerList'
import { DndActiveProbe, DndDraggable, DndDroppable } from '../components/dnd'
import { FOLDER_SORT_MODES, collectFolderSubtreeIds, folderHasCreatedStamp, folderNameText, type FolderSortMode } from '../utils/folderTree'
import { normalizeRunSessionKey } from '../apps/workflows/runModel'
import { sanitizeLlmOutput } from '../utils/sanitize'
import type { ChatFolder, ChatTag, SessionLink } from '../types'
import { SESSION_LANES, hasLiveSessionWork } from './chat/sessionLane'
import { isMemoryWait, queuedWaitText } from './chat/subagentQueuedReason'
import {
  type RecentUnit,
  RECENT_WINDOW_PRESETS,
  formatRecentWindow,
} from './recentWindow'
import { loadChatConfig, saveChatConfig } from './chat/ChatSettings'
import { focusSiblingSessionRow } from './chat/sessionRowNav'
import { SessionRowWindowContext, SessionRowWindowScroller, WindowedSessionRow, useSessionRowWindowRoot } from './chat/sessionRowWindow'
import { compareBySort, fmtRelativeTime, readSessionSortKey, SESSION_SORT_STORAGE_KEY, slotActivityTs } from './chat/sessionOrder'
import { STALE_COLLAPSE_PRESETS_MS } from './staleCollapse'
import type { StaleSplit } from './staleCollapse'
import type { SortKey } from './chat/sessionOrder'
import { useLanguageGeneration } from '../i18n/useLanguageGeneration'
import { deriveAutomationStatus, MONITOR_STATUS_KEYS } from '../monitoring/automation'
import MonitorRadar from '../components/MonitorRadar'

import { i18nT } from '../i18n/t'
import { agentOrDefaultLabel } from '../utils/agentLabel'
import { useLaneScrollMemory } from '../hooks/useLaneScrollMemory'
import { useScrollEdges } from '../hooks/useScrollEdges'
import { compareText, fmtDateFields, fmtList } from '../i18n/format'
import { sidebarCollision } from './chat-sidebar/dnd/collision'
export { sidebarCollision, isFolderNestBand } from './chat-sidebar/dnd/collision'
export { boardSidebarWidth } from './chat-sidebar/board'
import { ChatPaneDropZone, RootDropHint, BoardUnfileDropStrip, SortableFolderBlock, SortableSubfolderBlock, SortableColumnFolder, FolderDragGhost, SessionDragGhost } from './chat-sidebar/dnd/targets'
import type { Slot, SourceLinkState, SidebarSourceLink, HistoryItem, AgentInfo, SessionFilterKey } from './chat-sidebar/types'
import { FOLDERS_SHELVED_LS_KEY, FLAT_VIEW_LS_KEY } from './chat-sidebar/persistence'
import { SESSION_FILTERS, useSessionFilterState, useSessionStatusFilters } from './chat-sidebar/filters'
import { useDebouncedSessionSearch, useSearchMatches } from './chat-sidebar/search'
import { openCrewWindow, useCrewWindow } from './chat/crew-window/crewWindowStore'
import { sessionRowIdentity, historyRowIdentity } from './chat-sidebar/rowIdentity'
import { buildSidebarRows, chipLabel, sameRowView, type ConductorRowView, type RowScene, type RowShell, type SessionRowView, type SidebarRows } from './chat-sidebar/rows'
import { useSessionSources } from './chat-sidebar/sessionSources'
import { CrewGroupSection, CrewOfflineContext, LocalGroupHeader, useCollapsedCrews } from './chat-sidebar/CrewGroups'
import { crewOf, type CrewGroup } from '../hooks/useInstanceSessions'
import { useSessionRename, useFolderRename } from './chat-sidebar/rename'
import { useSidebarLane, useLaneCycle, renderedLane } from './chat-sidebar/lanes'
import { useHistoryPane } from './chat-sidebar/history'
import { FolderClosedSection } from './chat-sidebar/FolderClosedSection'
import { usePinnedSessionOrder, usePinnedOrderAuthority, usePinnedKeyboardReorder } from './chat-sidebar/pinnedOrder'
import { useStaleCollapse, useStaleMoveWatcher, useStaleNarrowBridge } from './chat-sidebar/stale'
import { useFolderSort, useFolderVisibility, useFolderFilterReveal, useFolderFilterRows, useFolderMutations, useFolderTree, useRootFolderLanes } from './chat-sidebar/folders'
import FolderCleanupPanel from './chat-sidebar/FolderCleanupPanel'
import { useSidebarResize } from './chat-sidebar/resize'
import { useSidebarTags } from './chat-sidebar/tags'
import { useBoardColumns, useColumnPopover, useBoardColumnMutations, useColumnMatches, useBoardFolderCollapse } from './chat-sidebar/board'
import { useHoverHold, useHoverPinLiveness } from './chat-sidebar/hoverHold'
import { useLineageAvailable, useConductorLane, citedCreatorOf } from './chat-sidebar/conductor'
import { useShortcutOrder } from './chat-sidebar/shortcuts'
import { useFolderDropOps, useSidebarMoveUndo, useSidebarDragHandlers, useNativeSessionDrag } from './chat-sidebar/dnd/useSidebarDrag'
import { useSidebarReveal } from './chat-sidebar/reveal'
import { useFolderChatCreate, useSessionCreate } from './chat-sidebar/create'

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
/** Rows at or past this paint ordinal share ONE `orderStamp`, so an insertion
 *  or reorder above them does not re-render them: they snap into their new
 *  position instead of springing there.
 *
 *  `orderStamp` exists so a displaced row re-renders and framer measures it
 *  (see `RowPlacement.orderStamp` in ./chat-sidebar/rows). Stamped as a plain ordinal, a New Chat landing at the
 *  top of a 160-session sidebar shifts every ordinal by one and voids all 160
 *  memo boundaries in one commit — 160 row bodies, 160 layout measurements and
 *  a group-wide spring — for a change whose visible effect is a handful of rows
 *  sliding down by one slot. The rows below the fold are displaced too, but
 *  nobody sees them move, so their spring buys nothing.
 *
 *  48 rows is roughly two sidebar viewports of 56px rows: the visible
 *  displacement stays continuous (the persistent-element rule in
 *  website/AGENTS.md is about what the user can see move), while the per-insert
 *  cost is bounded by this constant instead of growing with the session count.
 *  The deliberate casualty: a user scrolled deep into the list sees rows beyond
 *  the window snap rather than slide when something above them moves. The
 *  ordinal-bump above the window still has its usual cost, so this is a bound,
 *  not a fix for rows inside it. Pinned by ChatSidebar.rowMemo.test.tsx. */
export const SIDEBAR_DISPLACEMENT_WINDOW = 48

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

/** Is this click the "open as a tab" modifier gesture? One predicate for every
 *  surface that offers it (session rows, the New button, the folder create
 *  entries), so the platform split cannot drift between them. The split is deliberate: Ctrl+click IS a
 *  right-click on macOS, so honouring it there would fire this and the context
 *  menu from one press; Cmd is the tab modifier there. Shift and Alt are
 *  excluded because both carry other meanings in the sidebar (range/reorder). */
function isOpenInTabModifierClick(e: React.MouseEvent): boolean {
  return (IS_MAC ? e.metaKey && !e.ctrlKey : e.ctrlKey && !e.metaKey) && !e.shiftKey && !e.altKey
}

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

/** Quiet boundary between manually ordered pins and automatically sorted rows.
 *  Starts 2px left of the row content column: `ml-2` 8 against the root-lane
 *  row pad 10, and inside a folder body `FOLDER_ROW_PAD_CLS` moves it to 7
 *  against the in-folder pad 9 (the `data-pinned-divider` hook). Main had the
 *  same 2px relation (`mx-3` 12 against a 14px pad). `mr-3` keeps the right
 *  edge on the rows' `pr-3`. */
function PinnedSessionDivider() {
  return (
    <div
      data-testid="pinned-session-divider"
      data-pinned-divider=""
      aria-hidden="true"
      className="ml-2 mr-3 my-1 h-[4px] shrink-0 border-y border-border-strong opacity-70"
    />
  )
}

/** The sidebar's ONE disclosure-chevron grammar (#2887): a ChevronRight that
 * rotates 90° when its section is open — animated at one shared duration —
 * and sits unrotated when closed. Every stateful disclosure in this pane
 * renders through here, which rules out the drift modes by construction:
 * Right/Down glyph swaps, counter-rotation when closed (the pre-#2884
 * defect), inline-style rotation, and divergent durations. Position is the
 * one deliberate asymmetry (the Older Sessions section header trails; row
 * disclosures lead) — see the comment at the header call site. */
function DisclosureChevron({ open, size, className = '' }: { open: boolean; size: number; className?: string }) {
  return <ChevronRight size={size} className={`shrink-0 transition-transform duration-200 ${open ? 'rotate-90' : ''} ${className}`.trimEnd()} />
}

/** Stable empty group list for the board, which groups nothing. */
const NO_CREW_GROUPS: CrewGroup[] = []

/** Lifecycle states after which a pull request can never merge, so its CI
 * rollup carries no actionable information and the lifecycle glyph is the only
 * meaningful signal. Named ONCE here because the vocabulary is shared by three
 * sibling conditionals; an inline literal per glyph is how `closed` came to be
 * covered by the badge but not by the CI gate.
 *
 * `closed` matters as much as `merged`: a closed pull request's check rollup can
 * stay pending FOREVER (GitHub parks fork-PR checks in PENDING /
 * ACTION_REQUIRED when the PR is closed before a maintainer approves the run),
 * so a chip gated only on `merged` spins its "checks running" spinner
 * indefinitely on a PR nobody is waiting for. Must stay in step with
 * `PullRequestPanel.tsx::SourceTabState`, which applies the same rule to the
 * source-strip tab — the chip and the tab describe one pull request and may not
 * disagree about its lifecycle. */
const TERMINAL_SOURCE_LINK_STATES: ReadonlySet<SourceLinkState> = new Set<SourceLinkState>([
  'merged',
  'closed',
])

/** Whether a chip should show its CI rollup or its merge state. Both are moot
 * once the pull request is terminal, so they share one gate. An ABSENT state
 * means the provider status has not been read yet (or the payload predates the
 * field), which is not terminal — such a chip keeps rendering CI exactly as it
 * always did. */
function showsChipCi(state: SourceLinkState | undefined): boolean {
  return state === undefined || !TERMINAL_SOURCE_LINK_STATES.has(state)
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

/** The single status glyph a change chip shows, or null for none.
 *
 * One function rather than sibling conditionals because the interesting part is
 * the PRECEDENCE, and precedence expressed as four independent `&&` guards is
 * how a chip comes to render two glyphs — or none — for a state nobody
 * enumerated.
 *
 * A conflict outranks a pending or passing rollup: green-check-on-unmergeable
 * is the reason this exists, since it reads as "ready" on a branch that cannot
 * land. It does NOT outrank a failed rollup — with both blockers live the worse
 * outcome is the one worth surfacing, and a red chip already says "do not
 * expect this to merge".
 *
 * `blocked` is deliberately not a conflict. On a repo with required reviews it
 * is the normal state of every open pull request, so treating it as a blocker
 * would decorate the whole session list and mean nothing.
 */
function chipStatusGlyph(
  link: SidebarSourceLink,
): 'failed' | 'conflict' | 'running' | 'passed' | null {
  if (!showsChipCi(link.state)) return null
  if (link.ci === 'failed') return 'failed'
  // GitHub can settle `mergeStateStatus: dirty` while `mergeable` is still
  // `unknown` (the two fields are recorded independently, each only once it is
  // real), and GitLab's `conflict` normalizes into both — so either field
  // alone is a real conflict answer.
  if (link.mergeable === 'conflicting' || link.mergeStateStatus === 'dirty') return 'conflict'
  if (link.ci === 'running') return 'running'
  if (link.ci === 'passed') return 'passed'
  return null
}

/** The provider's mark for a chip, or a neutral link glyph for a provider this
 * build does not recognize.
 *
 * One resolver rather than a ternary inlined at each chip: the change chip and
 * the issue chip render the identical mark, so the two copies were pure
 * duplication. Their LABEL branches were not duplication -- they encoded
 * genuinely different rules, since `!7` is a merge request and `#7` an issue --
 * which is exactly the knowledge that belongs with the parser rather than
 * spread across two render sites.
 *
 * The `default` is not dead code even though `provider` is typed as three
 * literals. That type describes what the serializer sends TODAY; the value
 * itself arrives over the wire from Python, where nothing enforces it. The
 * branch it replaced used GitLab as its implicit `else`, so an unrecognized
 * provider rendered GitLab's brand mark on someone else's review system --
 * a wrong attribution is worse than an anonymous one, and this is the one
 * failure mode a chip must not have.
 *
 * A REGISTERED provider sits between those two cases: it is not a built-in, so
 * it has no bundled mark here, but its descriptor may have supplied one. That
 * icon is consulted before the neutral glyph, which is what lets an edition
 * wear its own brand without any provider being able to wear another's. Mirrors
 * the fallback order in `MarkdownRenderer`'s forge chip and
 * `PullRequestPanel`'s tab strip.
 */
function SourceLinkIcon({ provider }: { provider: SidebarSourceLink['provider'] }) {
  switch (provider) {
    case 'github':
      return <GithubLogo size={10} className="shrink-0" />
    case 'gitlab':
      return <GitlabLogo size={10} className="shrink-0" />
    case 'jira':
      return <JiraLogo size={10} className="shrink-0" />
    default: {
      const Icon = sourceProviderMeta(provider).icon
      if (Icon) return <Icon size={10} className="shrink-0" />
      return <Link2 className="lucide-inline shrink-0" aria-hidden="true" />
    }
  }
}

/** A session row's pull-request / issue chip strip, including the expandable
 *  "+N" overflow chip.
 *
 *  A component rather than a block inside the row's render callback because the
 *  overflow chip is interactive and therefore needs per-row state. The slots
 *  payload deliberately serializes at most three links PER KIND (state.py's
 *  `_SERIALIZED_SOURCE_LINKS_PER_SLOT`) so a broadcast carrying dozens of rows
 *  stays small, which means the links behind "+N" are not on the client at all
 *  and expanding has to fetch them. */
function SessionSourceChips({ slotKey, links, total, connected, isActive, onOpenSource, onActivateSlot }: {
  slotKey: string
  /** The budgeted links from the slots payload — what the collapsed strip shows. */
  links: SidebarSourceLink[]
  /** `source_links_total`: how many the session actually has, budget aside. */
  total?: number
  connected: boolean
  isActive: boolean
  onOpenSource?: (slotKey: string, ref: { url: string; kind: 'change' | 'issue' }) => boolean
  /** Switch to this session — the chip reveals into ITS side panel, so the
   *  session has to be the active one first. */
  onActivateSlot: () => void
}) {
  const [wantsExpanded, setWantsExpanded] = useState(false)
  const pendingUnlinks = usePendingSourceUnlinks(slotKey)

  /** What the slots payload currently says this row's links are.
   *
   *  Part of the query key, so it is the LINK IDENTITY that decides whether a
   *  fetched list still applies — not the count. A session can drop one pull
   *  request as it gains another, leaving `total` unchanged, and a count-keyed
   *  cache would serve that superseded set forever. */
  const signature = `${total ?? ''}|${links.map(link => link.url).join(' ')}`
  // React Query rather than useState + fetch (website/AUTOSDE.yaml `use-react-query`):
  // the same session can be rendered by more than one column, and a shared cache
  // is what stops each copy issuing its own GET for the same slot. `enabled`
  // makes the read lazy — nothing is fetched until the row is actually expanded —
  // and `retry: false` keeps a failed expand immediate, because the user's next
  // click IS the retry.
  const { data: fetchedLinks, isFetching, isError, refetch } = useQuery<SidebarSourceLink[]>({
    queryKey: ['session-source-links', slotKey, signature],
    queryFn: async () => {
      const payload = await api.chatSlotSourceLinks(slotKey)
      // Shape-check rather than trust: a malformed 200 (a proxy, an older
      // gateway) would otherwise put `undefined` where the render filters an
      // array, and an exception in render unmounts the whole sidebar.
      if (!Array.isArray(payload?.links)) throw new Error('malformed source-links response')
      return payload.links
    },
    enabled: wantsExpanded,
    retry: false,
    // Owned by the query rather than inherited from the provider: collapsing and
    // re-expanding within the window must not re-issue the GET, and that
    // guarantee should not depend on a global default someone may retune.
    staleTime: 30_000,
  })

  // Expanded only while a list for THIS payload is in hand. Because the payload
  // is in the query key, a slots push that changes the links switches to a key
  // with no data yet: the row falls back to the live budgeted strip and re-offers
  // "+N" while the new list loads, instead of freezing on a snapshot that
  // silently omits the new link.
  const isExpanded = wantsExpanded && fetchedLinks !== undefined
  const failed = wantsExpanded && isError

  // Toggling REPLACES the button that was activated, so without this the
  // keyboard user is dropped to the top of the document mid-row. Armed only by
  // the two click handlers, so a re-render from a slots push never steals focus.
  const pendingFocus = useRef<'expand' | 'collapse' | null>(null)
  const expandRef = useRef<HTMLButtonElement>(null)
  const collapseRef = useRef<HTMLButtonElement>(null)
  useEffect(() => {
    const want = pendingFocus.current
    if (!want) return
    pendingFocus.current = null
    ;(want === 'collapse' ? collapseRef : expandRef).current?.focus()
  }, [isExpanded])

  const shownAll = isExpanded && fetchedLinks ? fetchedLinks : links
  // Pending mutations from any session menu hide chips; failure restores them.
  const shown = shownAll.filter(link => !link.identity || !pendingUnlinks.includes(link.identity))
  // Derived from the UNFILTERED set, so the optimistic-unlink filter only
  // removes visible chips and never inflates the overflow count: computing this
  // against `shown` would subtract an already-hidden chip from the stale server
  // `total` and render a phantom "+1 more" for the round-trip window until the
  // slots push refreshes `total`. Lands on 0 once expanded and self-corrects if
  // a payload ever reports a total below the links it carries.
  const hidden = typeof total === 'number' ? Math.max(0, total - shownAll.length) : 0
  const changeLinks = shown.filter(link => (link.kind ?? 'change') !== 'issue')
  const issueLinks = shown.filter(link => (link.kind ?? 'change') === 'issue')

  const expand = () => {
    if (isFetching) return
    pendingFocus.current = 'collapse'
    // Already enabled means this is a retry after a failure (or a re-expand of a
    // key whose fetch never landed): flipping the flag again would not re-issue
    // the query, so ask for it explicitly.
    if (wantsExpanded) void refetch()
    else setWantsExpanded(true)
  }

  const overflowLabel = issueLinks.length
    ? i18nT('pages.chatSidebar.more_pull_request_or_issue_in_this_session', { count: hidden })
    : i18nT('pages.chatSidebar.more_pull_request_in_this_session', { count: hidden })
  /** Chip tooltip. A plain click now reveals in-panel, so a bare "Open <url>"
   *  would promise the browser and mislead; naming the modifier is also the only
   *  way that escape hatch is discoverable rather than found by accident. */
  const chipTitle = (link: SidebarSourceLink) => i18nT('pages.chatSidebar.open_source_link_in_side_panel', {
    url: link.url,
    modifier: platformShortcut('Cmd+click'),
  })
  /** Chip click: switch to the session the chip belongs to and reveal its pull
   *  request / issue in that session's side panel, rather than sending the user
   *  out to the provider's website.
   *
   *  The chip stays a real anchor with a real href, so four cases deliberately
   *  fall through to plain link navigation instead:
   *    - `onOpenSource` unset — the surface has no side panel to reveal into
   *      (the `/embed/sessions` list).
   *    - a modifier click — the user asked for a new tab/window explicitly, and
   *      "Copy link address" still yields the PR url.
   *    - offline — the panel loads a PR through the LOCAL provider CLI, so with
   *      the gateway down the provider's own page is the only thing that can
   *      answer at all.
   *    - `onOpenSource` returning false — the panel could not resolve this url,
   *      so the provider's page is better than a dead click.
   *  Middle-click never reaches a click handler (it fires auxclick), so it opens
   *  a background tab natively without a case here.
   *
   *  `preventDefault` comes LAST on purpose: the default action runs only after
   *  every handler returns, so suppressing navigation after the reveal is still
   *  effective — and it means the reveal decides, rather than being assumed to
   *  succeed. */
  const revealInPanel = (link: SidebarSourceLink) => (e: React.MouseEvent<HTMLAnchorElement>) => {
    // The row is a click-to-switch button; never let a chip click reach it,
    // whichever branch we take below.
    e.stopPropagation()
    // A pointer click focuses the anchor, and the row's action strip is
    // revealed on focus-within, so a focused chip would pin that strip visible
    // after the pointer leaves the row. Drop focus on pointer clicks only
    // (`detail` counts clicks; keyboard activation reports 0) so Enter on a
    // focused chip leaves focus where a keyboard user expects it.
    if (e.detail > 0) e.currentTarget.blur()
    if (!onOpenSource || !connected || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return
    if (!isActive) onActivateSlot()
    if (!onOpenSource(slotKey, { url: link.url, kind: link.kind ?? 'change' })) return
    e.preventDefault()
  }

  return (
    <div className="flex flex-wrap gap-1.5 mt-1">
      {changeLinks.map(link => (
        // `link.url` is always an `https://` URL on an allowlisted host
        // (state.py scans for the literal "https://" then validates via
        // parse_source_url), so no scheme sanitising is needed for the href.
        //
        // The row is a dnd-kit draggable as well as a button, so the anchor also
        // disables its own native HTML5 drag — that would otherwise put the URL
        // on the dataTransfer instead of the slot key in the board/flat scopes
        // that use native drag.
        <a key={link.url} href={link.url} target="_blank" rel="noopener noreferrer"
          draggable={false}
          onClick={revealInPanel(link)}
          className="inline-flex items-center gap-1 px-1.5 py-[1px] rounded-[4px] text-[10px] leading-none font-medium text-muted no-underline border border-border bg-bg-elevated/60 hover:text-text hover:border-accent"
          title={chipTitle(link)}>
          <SourceLinkIcon provider={link.provider} />
          {chipLabel(link)}
          {link.state === 'merged' && (
            <span className="inline-flex shrink-0 text-aim" aria-label={i18nT('pages.chatSidebar.merged')} title={i18nT('pages.chatSidebar.merged')}>
              <GitMerge className="lucide-inline" aria-hidden="true" />
            </span>
          )}
          {/* Real text needs no aria-label/title of its own: the anchor's
              accessible name already includes it, and a child `title` would
              shadow the anchor's tooltip — the URL and the modifier escape
              hatch — for the region the word covers. The merged sibling
              carries both only because its span is icon-only. */}
          {link.state === 'closed' && (
            <span className="shrink-0 whitespace-nowrap text-danger">
              {i18nT('pages.chatSidebar.closed')}
            </span>
          )}
          {/* One status glyph, chosen by `chipStatusGlyph` — CI is moot
              once the PR is terminal (merged or closed), where the
              lifecycle glyph is the signal, and a merge conflict
              outranks a pending or passing rollup. */}
          {/* Pending CI is a STATIC amber dot (the provider's own pending
              convention), never a spinner: an animated glyph on a session
              card reads as "the agent is working on this session", which is
              a stronger claim than "this PR's checks haven't finished".
              Motion on the card stays reserved for session activity. */}
          {(() => {
            switch (chipStatusGlyph(link)) {
              case 'running':
                return <Circle className="lucide-inline shrink-0 text-warn scale-75" fill="currentColor" strokeWidth={0} aria-label={i18nT('pages.chatSidebar.checks_running')} />
              case 'passed':
                return <Check className="lucide-inline shrink-0 text-ok" aria-label={i18nT('pages.chatSidebar.checks_passed')} />
              case 'failed':
                return <X className="lucide-inline shrink-0 text-danger" aria-label={i18nT('pages.chatSidebar.checks_failed')} />
              case 'conflict':
                // The panel's own conflict-banner key, reused rather than
                // duplicated: the chip and the banner describe one pull
                // request, so they must not word it differently in any
                // locale.
                return <TriangleAlert className="lucide-inline shrink-0 text-danger" aria-label={i18nT('components.pullRequestPanel.merge_conflicts')} />
              default:
                return null
            }
          })()}
        </a>
      ))}
      {issueLinks.map(link => (
        // Issue chip: the same anchor discipline (reveal in panel, no native
        // drag) but deliberately NO ci / state / merge decoration — the
        // chip-status cache is pull-request-only in this phase, so an issue chip
        // has nothing truthful to colour and a borrowed glyph would assert state
        // we never fetched. How the number is written is the serializer's call
        // (`source_ref_label`), so nothing here branches on provider except the
        // issue dot, which Jira does not get: its label is already a whole
        // identifier (PROJ-123) rather than a bare number needing a marker.
        <a key={link.url} href={link.url} target="_blank" rel="noopener noreferrer"
          data-testid={`session-issue-chip-${link.number}`}
          draggable={false}
          onClick={revealInPanel(link)}
          className="inline-flex items-center gap-1 px-1.5 py-[1px] rounded-[4px] text-[10px] leading-none font-medium text-muted no-underline border border-border bg-bg-elevated/60 hover:text-text hover:border-accent"
          title={chipTitle(link)}>
          <SourceLinkIcon provider={link.provider} />
          {link.provider !== 'jira' && <CircleDot className="lucide-inline shrink-0" aria-hidden="true" />}
          {chipLabel(link)}
        </a>
      ))}
      {hidden > 0 && (
        // Gated on `hidden`, NOT on the expand intent: a row whose payload moved
        // under an open expansion renders the live budgeted strip again, and
        // must re-offer the overflow rather than hide it behind a stale state.
        //
        // `onMouseDown` stops the row's drag from claiming the press, matching
        // the row's other in-place controls; without it a click on the chip can
        // be swallowed as a drag activation. Deliberately NOT `disabled` while
        // loading — disabling the focused button blurs it to <body>, and the
        // `if (loading) return` guard in `expand` already prevents a double
        // fetch.
        <button type="button"
          ref={expandRef}
          data-testid="session-source-overflow"
          draggable={false}
          aria-expanded={false}
          onMouseDown={e => e.stopPropagation()}
          onClick={e => { e.stopPropagation(); expand() }}
          className="inline-flex items-center gap-1 px-1.5 py-[1px] rounded-[4px] text-[10px] leading-none font-medium text-muted border border-border bg-bg-elevated/60 cursor-pointer hover:text-text hover:border-accent"
          title={failed ? i18nT('pages.chatSidebar.source_links_expand_failed') : overflowLabel}
          // An aria-label OUTRANKS the title in the accessible-name computation,
          // so the failure has to be named here too or a screen reader still
          // announces "2 more pull requests…" on a button that just failed.
          aria-label={failed ? i18nT('pages.chatSidebar.source_links_expand_failed') : overflowLabel}>
          {/* This spinner is exempt from the "no motion on a session card" rule
              that governs the CI glyph above: it is transient feedback for the
              user's OWN click on this button, not an ambient status claim about
              the session. It exists only while their expand is in flight. */}
          {isFetching
            ? <Loader2 className="lucide-inline shrink-0 animate-spin" aria-hidden="true" />
            : failed && <RotateCcw className="lucide-inline shrink-0 text-warn" aria-hidden="true" />}
          +{hidden}
        </button>
      )}
      {isExpanded && (
        <button type="button"
          ref={collapseRef}
          data-testid="session-source-collapse"
          draggable={false}
          aria-expanded={true}
          onMouseDown={e => e.stopPropagation()}
          onClick={e => { e.stopPropagation(); pendingFocus.current = 'expand'; setWantsExpanded(false) }}
          className="inline-flex items-center px-1.5 py-[1px] rounded-[4px] text-[10px] leading-none font-medium text-muted border border-border bg-bg-elevated/60 cursor-pointer hover:text-text hover:border-accent"
          title={i18nT('pages.chatSidebar.collapse_source_links')}
          aria-label={i18nT('pages.chatSidebar.collapse_source_links')}>
          <ChevronUp className="lucide-inline shrink-0" aria-hidden="true" />
        </button>
      )}
    </div>
  )
}

/**
 * A pill for one duration choice, shared by the Recent window presets and — on a
 * phone, where those options render inline rather than in a flyout — the
 * dormant-collapse thresholds. One component so the two lists cannot drift, and
 * so the duplicated class/style pair does not read as copy-paste.
 */
function DurationChip({ label, selected, onSelect }: { label: string; selected: boolean; onSelect: () => void }) {
  return (
    <button
      type="button"
      aria-pressed={selected}
      className="px-2 py-0.5 rounded-full text-[11px] cursor-pointer border transition-colors"
      style={selected
        ? { background: 'color-mix(in srgb, var(--ok) 12%, transparent)', color: 'var(--ok)', borderColor: 'color-mix(in srgb, var(--ok) 35%, transparent)' }
        : { background: 'transparent', color: 'var(--muted)', borderColor: 'var(--border)' }}
      // A plain button is not a menu item, so clicking it leaves the host menu
      // open on its own — Radix dismisses on an item select or an outside
      // pointer-down, neither of which this is (verified in a browser).
      onClick={onSelect}
    >
      {label}
    </button>
  )
}

/**
 * Catalog keys for the filter rows, chips and tooltips.
 *
 * Keys, not copy: these tables are module-level, so an `i18nT()` call here would
 * resolve once at boot and never follow a language switch — the lookup happens
 * where each label renders. Shaped as flat `Record`s of full literal keys and
 * indexed inline at the `i18nT()` call, because that is the form
 * `scripts/check-i18n-keys.mjs` can resolve statically; a key it cannot resolve
 * is a key it cannot verify exists.
 */
export const FILTER_LABEL_KEY: Record<SessionFilterKey, string> = {
  unread: 'pages.chatSidebar.filter_unread',
  running: 'pages.chatSidebar.filter_running',
  pinned: 'pages.chatSidebar.filter_pinned',
  recent: 'pages.chatSidebar.filter_recent',
}
export const FILTER_DESCRIPTION_KEY: Record<SessionFilterKey, string> = {
  unread: 'pages.chatSidebar.filter_unread_description',
  running: 'pages.chatSidebar.filter_running_description',
  pinned: 'pages.chatSidebar.filter_pinned_description',
  recent: 'pages.chatSidebar.filter_recent_description',
}

/** Compute a date segment label for a session timestamp. Mirrors ChatGPT/Claude.
 *  Accepts either a Unix epoch (seconds) from backend `modified` or an ISO `created` string. */
function dateSegment(ts: number | string | undefined): string {
  if (ts == null) return i18nT('pages.chatSidebar.older')
  const d = typeof ts === 'number' ? new Date(ts * 1000) : new Date(ts)
  if (isNaN(d.getTime())) return i18nT('pages.chatSidebar.older')
  const now = new Date()
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate())
  const startOfYesterday = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1)
  const daysAgo7 = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 7)
  const daysAgo30 = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 30)
  if (d >= startOfToday) return i18nT('pages.chatSidebar.today')
  if (d >= startOfYesterday) return i18nT('pages.chatSidebar.yesterday')
  if (d >= daysAgo7) return i18nT('pages.chatSidebar.last_7_days')
  if (d >= daysAgo30) return i18nT('pages.chatSidebar.last_30_days')
  if (d.getFullYear() === now.getFullYear()) return fmtDateFields(d, { month: 'long' })
  return fmtDateFields(d, { year: 'numeric', month: 'long' })
}

/** Animated collapsible for unknown-height content (folder bodies).
 *  Uses CSS grid `1fr`/`0fr` trick so we can animate to intrinsic height
 *  without measuring. For fixed-height panels use Framer Motion instead. */
/** The nested folder body's own left inset, in px — and the `D` term in the
 *  sidebar's alignment algebra (see renderFolderHeader).
 *
 *  It exists for the collapse animation: the body animates through
 *  `grid-template-rows` with `overflow: hidden`, and without a little padding the
 *  children's focus rings and the connector's rounded corner clip against that
 *  edge. The LEFT component is the load-bearing one — it shifts the whole nested
 *  subtree right by this much relative to the folder header that sits above it,
 *  which is why the header's own pad has to be `D + ml-3` to keep the folder glyph
 *  on the connector line.
 *
 *  Named and exported rather than inlined because that offset is what has broken
 *  the sidebar's alignment guides four times: it is invisible in the class list, so
 *  every attempt to derive the geometry from Tailwind classes alone has been 2px
 *  out. ChatSidebar.folderAlignment.test.tsx imports THIS constant for its
 *  arithmetic and asserts the rendered padding against it, so a change here fails a
 *  test instead of silently moving three guides. */
export const FOLDER_BODY_INSET_PX = 2

/** Padding the folder body carries while open. The LEFT term is the alignment
 *  algebra's `D`; the vertical 2px keeps focus rings off the clip edge. */
const FOLDER_BODY_OPEN_PADDING = `2px 0 2px ${FOLDER_BODY_INSET_PX}px`

/** Stacking base for pinned folder headers. A header at depth d gets
 *  `FOLDER_ROW_STICKY_Z - d`, so a parent's header paints over its child's as the
 *  child's block scrolls out beneath it. Kept small: it only has to beat the
 *  session rows in the lane, and every menu and popover renders in a portal. */
const FOLDER_ROW_STICKY_Z = 20

/** The list-view folder body: the connector line (`border-l`) plus the gap after
 *  it, and a tighter left pad (9px, `R_in`) for every row filed inside a folder.
 *  The folder's own line already marks the grouping, so rows under it do not need
 *  the full 10px root-lane pad. 9 and not less: the recency tint paints an accent
 *  stripe up to 7px wide at the row's left edge (`recencyTintShadow`), and 9 keeps
 *  2px between that stripe and the text. The descendant selectors outrank the
 *  rows' own `pl-2.5` / `ml-[10px]` (two classes vs one), and nested bodies apply
 *  the same value, so depth does not compound it. That precedence is proven only
 *  by the measured playwright/sidebar-folder-alignment.spec.ts; the jsdom tests
 *  pin class strings and would stay green if the specificity flipped. The
 *  divider, the dormant
 *  toggle, the empty-folder "new chat" affordance, the pinned divider and the
 *  "N hidden folders" reveal row move with the row so all stay on (the pinned
 *  divider: 2px left of) its content column. Shared by the list-view and
 *  board-view folder bodies. */
export const FOLDER_ROW_PAD_CLS = '[&_.session-row]:pl-[9px] [&_[data-row-divider]]:ml-[9px] [&_[data-stale-toggle]]:pl-[9px] [&_[data-folder-new-chat]]:pl-[9px] [&_[data-pinned-divider]]:ml-[7px] [&_[data-folder-hidden-reveal]]:pl-[9px]'
export const FOLDER_BODY_CLS = `relative border-l border-border mb-1 ml-1 pl-[3px] rounded-bl-md ${FOLDER_ROW_PAD_CLS}`

/** The board-view folder body. Its header is not the list header: `paddingLeft`
 *  6, an 11px glyph and `gap-2` 8 put the folder name at 6 + 11 + 8 = 25 from the
 *  header box, against the list header's 3 + 12 + 4 = 19. So the board body keeps
 *  the same row pads (`FOLDER_ROW_PAD_CLS`, R_in 9) and takes a wider body pad:
 *  D 2 + `ml-2` 8 + border 1 + `pl-[5px]` 5 + R_in 9 = 25, rows on the name. The
 *  connector lands at D + 8 = 10, inside the glyph's 6..17 span. Each board
 *  nesting level costs 2 + 8 + 1 + 5 = 16px. */
export const BOARD_FOLDER_BODY_CLS = `relative border-l border-border ml-2 pl-[5px] ${FOLDER_ROW_PAD_CLS}`

/** What a session row can DO: every callback and ref it needs, built ONCE in the
 *  shell and handed to every row, so the row's memo compares one stable reference
 *  here and its data through `sameRowView`. */
interface SessionRowActions {
  renameInputRef: React.MutableRefObject<HTMLTextAreaElement | null>
  onRenameStart: (key: string, scope: string, title: string, fromMenu: boolean) => void
  onRenameChange: (value: string) => void
  onRenameCommit: (key: string, value: string) => void
  onRenameCancel: () => void
  onDuplicate: (key: string) => void
  /** The row's ✕. In the nesting conductor lane this is the tree close, so a card
   *  with sessions under it closes its subtree (see `useCloseSessionTree`). */
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
  /** Go where a row whose session belongs to another page lives
   *  (`view.opensElsewhere`): click and Enter land here instead of `switchSlot`. */
  openElsewhere: (path: string) => void
  /** The conductor lane's chevron: open or close the sessions this row opened. */
  toggleConductor: (identity: string) => void
}

interface SessionRowProps {
  /** Everything the row renders from, already masked for its origin by the row
   *  model (./chat-sidebar/rows): a peer row's view carries no local state. */
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
const SessionRow = memo(function SessionRow({ view, actions }: SessionRowProps) {
  const {
    slot: s, showDivider, scope, navScope, holdContainer, isActive, isOut, isPinned, isUnread, isRunning,
    recent, subagentCount, subagentApprovalCount, digitBadge,
    isRenaming, renamingHere, renameValue, revealFlash, pinnedOrderIndex, pinnedReorderEnabled, rowAnimEnabled,
    opensElsewhere, peerId, peerName,
  } = view
  const conductor = view.conductor
  // A card with sessions under it in the nesting conductor lane closes its whole
  // subtree, so its ✕ and row menu say how many: the row model's `subtreeCount`
  // is taken over the lane's kept rows, the same rows the close plans over.
  // When the press would refuse (this card or a session under it is running),
  // the ✕ and the menu say so first: muted, "Can't close: N running", and still
  // pressable, because the press opens the notice that lists who is running.
  const closeBlockedCount = conductor && conductor.subtreeCount > 0 && conductor.subtreeRunning > 0 ? conductor.subtreeRunning : undefined
  const closeSessionCount = !closeBlockedCount && conductor && conductor.subtreeCount > 0 ? conductor.subtreeCount + 1 : undefined
  const closeSessionLabel = closeBlockedCount
    ? i18nT('pages.chatSidebar.close_blocked', { count: closeBlockedCount })
    : conductor && conductor.subtreeCount > 0
      ? i18nT('pages.chatSidebar.close_session_tree', { count: conductor.subtreeCount, total: conductor.subtreeCount + 1 })
      : undefined
  // The row menu splits the same reach over two lines: the action, then a muted
  // line that wraps, so a phone's narrow menu never cuts the sentence off.
  const closeMenuLabel = closeBlockedCount
    ? closeSessionLabel
    : conductor && conductor.subtreeCount > 0
      ? i18nT('pages.chatSidebar.close_all', { count: conductor.subtreeCount + 1 })
      : undefined
  const closeMenuHint = !closeBlockedCount && conductor && conductor.subtreeCount > 0
    ? i18nT('pages.chatSidebar.close_session_tree_hint', { count: conductor.subtreeCount })
    : undefined
  const {
    connected, mode, isMobile, colorMode, defaultAgent, installedAgents, tagById, paletteColors, boost, boostFor,
    recentTintCount, dragInFlight, activeDraggedKey, activeDraggedPinnedIndex,
  } = view.shell
  const {
    onNativeDragStart, onNativeDragEnd, onPinnedKeyboardReorder,
    renameInputRef, onRenameStart, onRenameChange, onRenameCommit, onRenameCancel,
    onDuplicate, onCloseSession, onMenuCloseAutoFocus, onSelectSlot, onOpenSlotInNewTab, onOpenSource, onOpenPeerSession,
    openElsewhere, toggleConductor,
  } = actions
  // A LOCAL row whose surface this page does not render goes where it lives: click
  // and Enter land here instead of `switchSlot`, and every local-only affordance
  // (rename, close, fork, drag, the row menu) is withheld exactly as it is for a peer
  // row, because the slot's lifecycle is owned elsewhere.
  const onOpenElsewhere = opensElsewhere != null ? () => openElsewhere(opensElsewhere) : undefined
  // Peer ownership, present only on a row sourced from a connected remote
  // instance. Every local-only affordance below is gated on its ABSENCE rather
  // than disabled: a control that looks actionable and silently does nothing is
  // worse than no control, and none of close / duplicate / rename / reorder can
  // be honoured for a session whose slot lives on another machine.
  //
  // `peerId`, NOT the `instance_id` that `remoteCrewName` below reads: these two
  // sit in one scope and mean opposite things. This one says the session is not
  // ours; that one says the session IS ours and dispatches elsewhere. It and
  // `peerName` come from the row model's origin check, set on a peer view only.
  //
  // The affordance gate proper. A peer row and a row that opens elsewhere are
  // withheld the SAME set -- rename, close, fork, drag, the row menu -- for the
  // same reason: this sidebar does not own the slot's lifecycle. They differ only
  // in what a click does (open a window vs. navigate), which the handlers below decide.
  const foreignRow = !!peerId || onOpenElsewhere != null
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
  // The peer's display name for the runs-elsewhere chip. Read from the SHARED
  // ['instances'] cache and enabled only for a row that is actually bound, so a
  // peerless install never issues the query. Falls back to the instance id: it is
  // less friendly but it is true, and a blank chip would claim the session runs
  // somewhere unnamed.
  const remoteCrewQuery = useQuery({
    queryKey: ['instances'],
    queryFn: () => api.listInstances(),
    enabled: s.executor === 'remote' && !!s.instance_id,
  })
  const remoteCrewName =
    remoteCrewQuery.data?.instances?.find(i => i.id === s.instance_id)?.name || s.instance_id || ''
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
  const statusDetail = useAppSelector(st => st.chat.slotStatusDetail?.[localSlotKey])
  const automation = useAppSelector(st => selectAutomationForSlot(st, localSlotKey))
  const goalLoop = automation?.kind === 'legacy_goal_loop' ? automation : undefined
  const monitor = automation?.kind === 'structured_monitor' ? automation : null
  const queuedForSlot = useAppSelector(st => st.chat.subagentQueued?.[localSlotKey] || 0)
  const queuedReason = useAppSelector(st => st.chat.subagentQueuedReason?.[localSlotKey])
  // {count, name, phase} of this slot's running workflow fan-out, or undefined.
  // shallowEqual because the map is rebuilt per run event; the primitives only
  // change when THIS slot's runs do.
  const wf = useAppSelector(st => selectSidebarWorkflowActive(st)[normalizeRunSessionKey(localSlotKey)], shallowEqual)
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
            aria-label={i18nT('pages.chatSidebar.opened_by_closed_session', { slot: conductor.orphanOf })}
            title={i18nT('pages.chatSidebar.opened_by_closed_session', { slot: conductor.orphanOf })}
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
    const agentDisplay = agentName ? agentOrDefaultLabel(s.agent, defaultAgent) : ''
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
    const rowState = ([
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
            <span className="truncate"><span className="font-medium" style={{ color: 'var(--warn)' }}>{i18nT('pages.chatSidebar.loop_waiting_on_you')}</span><span className="text-muted"> · {goalLoop
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
          // `remote_action_unsupported` refusal), so the instruction would point
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
    ] as const).find(entry => entry.when)?.build() ?? null

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
    const unreadDot = !rowState && isUnread
      // A DOT, so it keeps its own size: `ROW_ICON_PX` sizes the lucide glyphs,
      // whose ink covers a fraction of their box, while a filled disc covers all
      // of it. At 10px it reads as heavier than every state that outranks it.
      // `--ok`, not `--accent`: this dot signals STATE (the agent finished and
      // the result is unread), so it reads the semantic status token that the
      // `recent` status chip (`SESSION_FILTERS`) and the connection-status dot (InstancesPanel's
      // `bg-ok`) already use, not the brand/interactive color. A theme where
      // the two hues differ can then keep the status cue distinct from
      // ordinary accent chrome (#10479).
      ? <span className="w-2 h-2 rounded-full shrink-0" style={{ background: 'var(--ok)' }}
        role="img" aria-label={i18nT('pages.chatSidebar.agent_finished_your_turn')}
        title={i18nT('pages.chatSidebar.agent_finished_your_turn')} />
      : null
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
      onOpenInNewTab: onOpenSlotInNewTab ? () => onOpenSlotInNewTab(rowKey) : undefined,
      // The same close the row's ✕ performs, also scoped to the conductor lane:
      // `treeClose` resolves to `closeSessionTree` there and `sessionActions.close`
      // everywhere else, so the menu never silently collapses a subtree that is
      // not visible in the current lane.
      onClose: onCloseSession,
      // When the conductor lane shows a card with nested sessions, the menu's
      // Close item does what the ✕ does: close the whole subtree. Give it the
      // same scoped label so the user knows what the press reaches.
      closeLabel: closeMenuLabel,
      closeHint: closeMenuHint,
      closeMuted: closeBlockedCount != null,
      // A row menu opens from inside this panel, where the folder-order banner
      // (when there is one) sits over the tree -- the menu need not repeat it.
      sidebarOnScreen: true,
    }), [rowKey, mode, onRenameStart, scope, rowTitle, onOpenSlotInNewTab, onCloseSession, closeMenuLabel, closeMenuHint, closeBlockedCount])
    const rowActions = useMemo(() => (void langGen, !renamingHere && !foreignRow ? (isMobile ? (
      <div className="absolute top-1/2 -translate-y-1/2 right-1.5 flex items-center gap-0.5">
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
      <IconButtonGroup reveal className="absolute top-1/2 -translate-y-1/2 right-1.5 has-[[data-state=open]]:opacity-100">
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <IconButton title={i18nT('pages.chatSidebar.more')} aria-label={i18nT('pages.chatSidebar.more_options')} onMouseDown={e => e.stopPropagation()} onClick={e => e.stopPropagation()}><MoreVertical size={12} /></IconButton>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="min-w-[160px]" onClick={e => e.stopPropagation()} onCloseAutoFocus={onMenuCloseAutoFocus}>
            <SessionActionsMenu variant="dropdown" {...rowMenuProps} />
          </DropdownMenuContent>
        </DropdownMenu>
        <IconButton variant="accent" title={i18nT('pages.chatSidebar.duplicate')} aria-label={i18nT('pages.chatSidebar.duplicate')} onMouseDown={e => e.stopPropagation()} onClick={e => { e.stopPropagation(); onDuplicate(rowKey) }}><GitFork size={12} /></IconButton>
        <IconButton variant={closeBlockedCount ? 'default' : 'danger'} title={closeSessionLabel ?? i18nT('pages.chatSidebar.close')} aria-label={closeSessionLabel ?? i18nT('pages.chatSidebar.close_session')} onMouseDown={e => e.stopPropagation()} onClick={e => { e.stopPropagation(); onCloseSession(rowKey) }} className={closeBlockedCount ? 'flex shrink-0 items-center gap-0.5 whitespace-nowrap opacity-60' : closeSessionCount ? 'flex shrink-0 items-center gap-0.5 whitespace-nowrap' : undefined} data-testid={closeBlockedCount ? 'row-close-blocked' : closeSessionCount ? 'row-close-tree' : undefined} aria-describedby={closeSessionCount ? `row-close-hint-${rowKey}` : undefined}>
          <X size={12} />
          {closeBlockedCount ? (
            <span aria-hidden="true" className="text-[11px] font-semibold leading-none tabular-nums" data-testid="row-close-blocked-label">
              {i18nT('pages.chatSidebar.close_blocked_short', { count: closeBlockedCount })}
            </span>
          ) : null}
          {/* The reach, on the button itself: it reads "Close 5" whenever it is
              shown. A bare count read as an unread or alert badge, and the word
              alone hid that the press reaches more than this one session.
              Decorative for a screen reader, which already hears the full label. */}
          {closeSessionCount ? (
            <span className="flex items-baseline gap-1 text-[11px] font-semibold leading-none tabular-nums">
              <span aria-hidden="true" data-testid="row-close-word">{i18nT('pages.chatSidebar.close')}</span>
              <span aria-hidden="true" data-testid="row-close-count">{closeSessionCount}</span>
              {/* What the number counts, for a screen reader: read as the
                  button's description after its full label. */}
              <span id={`row-close-hint-${rowKey}`} className="sr-only" data-testid="row-close-hint">
                {i18nT('pages.chatSidebar.close_count_hint', { count: closeSessionCount })}
              </span>
            </span>
          ) : null}
        </IconButton>
      </IconButtonGroup>
    )) : null
    // `langGen` (read with `void` above) because the labels are i18nT strings, which re-translate on a catalog load.
    ), [renamingHere, foreignRow, isMobile, rowMenuProps, onMenuCloseAutoFocus, onDuplicate, onCloseSession, closeSessionLabel, closeSessionCount, closeBlockedCount, rowKey, langGen])
    const rowContextMenuContent = useMemo(() => (void langGen, !foreignRow ? (
      <ContextMenuContent className="min-w-[160px]" onClick={e => e.stopPropagation()} onCloseAutoFocus={onMenuCloseAutoFocus}>
        <SessionActionsMenu variant="context" {...rowMenuProps} />
      </ContextMenuContent>
    ) : null), [foreignRow, onMenuCloseAutoFocus, rowMenuProps, langGen])
    return (
      <DndDroppable
        id={`pinned-session:${scope}:${rowIdentity}`}
        data={{ type: 'pinned-session', key: s.key, container: reorderContainer }}
        disabled={!dndRow || !pinnedReorderEnabled || !isPinned || isRenaming}
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
        initial={rowAnimEnabled ? { opacity: 0, x: -12 } : false}
        // The anchor's dimming belongs HERE and not in a class: Motion writes its
        // animation target to the element's own style, so an inline `opacity: 1` from
        // this target outranks any opacity utility on the same row and the anchor paints
        // at full strength. One source of truth, and the property a person actually sees.
        animate={{ opacity: conductor?.anchorOnly ? 0.55 : 1, x: 0 }}
        transition={{ layout: { type: 'spring', stiffness: 500, damping: 35 }, opacity: { duration: 0.2 }, x: { duration: 0.2 } }}>
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
        <ContextMenu>
          <ContextMenuTrigger asChild>
        <div ref={dndRow ? setNodeRef : undefined} {...(dndRow ? listeners : {})}
          data-draggable={(!isRenaming && !foreignRow).toString()}
          className={`session-row group relative flex items-start ${ROW_BOX_CLS} text-sm transition-all select-none ${isActive ? !connected ? `session-active ${ROW_ACTIVE_CLS} cursor-not-allowed` : `session-active ${ROW_ACTIVE_CLS} cursor-pointer` : !connected ? 'text-muted opacity-50 cursor-not-allowed' : `${ROW_IDLE_CLS} cursor-pointer`} ${goalLoopStalled ? 'session-loop-stalled' : ''} ${rowColor ? 'session-colored' : ''} ${rowColor && colorMode === 'gradient' ? 'session-gradient' : ''} ${isDragging ? 'opacity-40' : ''} ${revealFlash ? `session-reveal-flash${revealFlash === 'fade' ? ' session-reveal-flash-fade' : ''}` : ''}`}
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
          {...offlineProps(connected, 'switch sessions')}
          role="button"
          tabIndex={0}
          data-session-row={rowIdentity}
          data-session-scope={navScope}
          data-session-container={holdContainer}
          aria-current={isActive ? 'true' : undefined}
          aria-disabled={!connected}
          aria-keyshortcuts={dndRow && pinnedReorderEnabled && isPinned ? 'Alt+ArrowUp Alt+ArrowDown' : undefined}
          onKeyDown={e => {
            if (dndRow && pinnedReorderEnabled && isPinned && e.altKey && !e.metaKey && !e.ctrlKey && !e.shiftKey
              && (e.key === 'ArrowUp' || e.key === 'ArrowDown')
              && (e.target as HTMLElement) === e.currentTarget) {
              e.preventDefault()
              e.stopPropagation()
              onPinnedKeyboardReorder(s.key, reorderContainer, e.key === 'ArrowUp' ? -1 : 1, e.currentTarget)
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
            dispatch(switchSlot({ key: s.key, announceOnMissing: true }))
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
            dispatch(switchSlot({ key: s.key, announceOnMissing: true }))
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
              data-testid="pinned-session-insertion"
              aria-hidden="true"
              className={`absolute left-3 right-3 h-0.5 rounded-full bg-accent pointer-events-none z-20 ${activeDraggedPinnedIndex < pinnedOrderIndex ? '-bottom-[1px]' : '-top-[1px]'}`}
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
           *  row's `pl-2.5` is the content column's whole left offset. */}
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
            // Keeps a childless row's card aligned with its siblings' rather than
            // shifted left by the missing chevron.
            <span className="mt-2.5 mr-0.5 w-4 h-4 shrink-0" aria-hidden="true" />
          ))}
          <div className="flex-1 min-w-0 overflow-hidden">
            <div className={`session-agent-label ${ROW_META_CLS} font-semibold truncate flex items-center gap-1 ${agentColor}`}>
              {/* Plain keyed span, deliberately unanimated: 200+ per-row
                *  AnimatePresence trees each paid child-diffing bookkeeping on
                *  every sidebar commit for a crossfade that fires only on the
                *  rare agent switch, and the repo's animation invariant is
                *  framer-only (no new CSS @keyframes). */}
              <span key={agentName || 'empty'} title={agentDisplay || undefined} className={`truncate shrink-0 ${resolvedSlotTags.length > 0 || agentDiverged ? 'max-w-[50%]' : ''}`}>{agentDisplay || '\u00A0'}</span>
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
                *  crew's participants and read as "people are in it" on a cold read. */}
              {!peerId && onOpenElsewhere && (
                <span
                  className="shrink-0 inline-flex items-center gap-0.5 text-[10px] px-1 rounded bg-info-subtle text-info border border-info/40"
                  data-testid="members-page-chip">
                  <ExternalLink size={9} className="shrink-0" aria-hidden="true" />
                  {i18nT('pages.chatSidebar.members_page_chip')}
                </span>
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
                  // group is `ml-auto … shrink-0` (see :4013 below), so an
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
              {s.executor === 'remote' && !inCrewGroup && (
                <RemoteCrewChip
                  name={remoteCrewName}
                  label={i18nT('pages.chatSidebar.on_instance', { name: remoteCrewName })}
                  title={i18nT('pages.chatSidebar.runs_on_crew', { name: remoteCrewName })}
                />
              )}
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
              {slotActivityTs(s) || isPinned || conductorMeta ? (
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
                  {isPinned && <span className="shrink-0" title={i18nT('pages.chatSidebar.pinned')}><Pin size={10} className="text-accent" /></span>}
                </span>
              ) : null}
            </div>
            {/* NEVER wraps. `truncate` rather than a two-line clamp, so every row
                is the same height. A clamped title also moved the whole
                secondary line down by a full line box on some rows, which is what
                made the list read as ragged. The full string stays reachable
                through the `title` attribute, and the rename box below is the one
                place it is shown in full. On a touch screen the row's buttons stay
                shown, so a card with "Close 5" reserves room for it and the title
                truncates first, never the control. */}
            <div
              data-session-title
              className={`${ROW_TITLE_CLS} font-semibold text-text ${renamingHere ? '' : 'truncate'}${(closeSessionCount || closeBlockedCount) && !isMobile ? ' [@media(hover:none)]:pr-24' : ''}`}
              title={s.title && s.title !== s.key ? s.title : s.key}
            >
              {/* No separate fork glyph: forked titles already carry the
                  persisted "↳ " marker (chat_fork.py _FORK_TITLE_MARKER). Keeping
                  the arrow in the title text — rather than as a UI-only glyph —
                  means it pre-fills the rename box (setRenameValue at the
                  onRename handler) so users can edit or drop it when they rename.
                  A separate ↳ glyph also double-stacked into "↳↳ Fork of …". */}
              {renamingHere ? (
                <textarea ref={renameInputRef} rows={1} className={`w-full bg-bg-elevated border border-accent rounded px-1 py-0 ${ROW_TITLE_CLS} text-text-strong outline-hidden select-text resize-none block overflow-hidden focus-ring`} value={renameValue} onChange={e => onRenameChange(e.target.value)} {...ime.bindEnter<HTMLTextAreaElement>({ onEnter: () => { (document.activeElement as HTMLTextAreaElement)?.blur() }, onEscape: onRenameCancel, onBlur: () => onRenameCommit(s.key, renameValue) })} onMouseDown={e => e.stopPropagation()} />
              ) : (s.title && s.title !== s.key ? s.title : s.key)}
            </div>
            {/* Secondary line: one ordered resolver decides both the words and the
                marker leading them (#3830), so the two can no longer disagree.
                The tail is `last_message`, which is also where the `unread` dot
                lands — the one marker with no state branch of its own. A row that
                is unread with nothing said yet still renders the line, because the
                dot IS the content then. */}
            {rowState ?? ((s.last_message || unreadDot) ? (
              <div className={ROW_STATUS_LINE_MUTED_CLS}>
                {unreadDot}
                {/* `min-w-0` or the ellipsis never renders: this is a flex child, and
                    a flex item's `min-width: auto` floor keeps it at content width
                    instead of letting `truncate` clip it (i18n render gate,
                    layout/ellipsis-with-flex-parent). */}
                {s.last_message ? <span className="truncate min-w-0">{s.last_message}</span> : null}
              </div>
            ) : null)}
            {s.source_links && s.source_links.length > 0 && (
              <SessionSourceChips
                slotKey={s.key}
                links={s.source_links}
                total={s.source_links_total}
                connected={connected}
                isActive={isActive}
                onOpenSource={onOpenSource}
                onActivateSlot={() => { dispatch(switchSlot({ key: s.key, announceOnMissing: true })); onSelectSlot?.(s.key) }}
              />
            )}
            {/* No tag chips here: every tag renders in the meta line above as
             *  tinted `· name` text. A chip row would print each tag twice. */}
          </div>
          {/* Hide the hover action popup (⋯ / duplicate / close) while THIS slot
           *  is being renamed: it is absolute-positioned at right-1.5 and reveals
           *  on focus-within, so the focused rename input would otherwise make it
           *  pop up and overlap the input's right edge. Mirrors the folder-header
           *  guard below (!(editingId === folder.id && editScope === 'list')). */}
          {/* A PEER-OWNED row shows NO action group at all: every entry in it
           *  (⋯ menu, duplicate, close, rename, pin, move-to-folder) is a
           *  local-slot operation that cannot reach a session on another machine.
           *  Omitting beats disabling — the same call `historyRow` makes for its
           *  delete button. A remote-EXECUTED local slot keeps the whole group:
           *  its slot is local, so every one of those operations still applies. */}
          {rowActions}
        </div>
          </ContextMenuTrigger>
          {rowContextMenuContent}
        </ContextMenu>
          )}
        </DndDraggable>
        {/* The divider starts at the CONTENT x, not the row's edge, so it
         *  underlines the text block rather than boxing the whole row — the row's
         *  left pad reads as a margin, and a rule running through it would box
         *  the row instead. Matches the Figma, which carries this border on the
         *  `content` frame rather than on the row.
         *
         *  10px is the row's content offset: the row's whole `pl-2.5`, since
         *  nothing else lives in that pad. The right inset is the row's own
         *  padding. */}
        {/* `-mt-px` so the rule does NOT add a row of layout height. In flow it made
         *  the row-to-row pitch row-height + 1, and since the active row suppresses
         *  its neighbours' dividers the pitch also VARIED down the list (measured
         *  60 and 61 on one list), which no fixed row height can compensate for.
         *  Overlaying the row's last pixel keeps the pitch equal to the row height.
         *  The left inset is unchanged — it still starts at the content x. */}
        {showDivider && <div data-row-divider="" className="ml-[10px] mr-3 -mt-px border-b border-border" />}
      </motion.div>
        )}
      </DndDroppable>
    )
}, (a, b) => a.actions === b.actions && sameRowView(a.view, b.view))

interface ChatSidebarProps {
  slots: Slot[]
  activeSlot: string | null
  unreadSlots: string[]
  history: HistoryItem[]
  historyHasMore: boolean
  defaultAgent: string
  installedAgents: AgentInfo[]
  mode?: string
  onWidthChange?: (w: number) => void
  onDragChange?: (dragging: boolean) => void
  /** The host stretches the sidebar to its own width (the mobile drawer, the
   *  sessions embed), so its paint reserves no chat pane beside it. */
  fillsHost?: boolean
  /** Optional callback fired when the user explicitly clicks a slot.
   *  When provided, this fires AFTER the switchSlot dispatch so consumers
   *  can react to user-driven selection (e.g. to navigate the URL). */
  onSelectSlot?: (key: string) => void
  /**
   * Render session rows WITHOUT Framer layout projection (`layout`/`layoutId`).
   *
   * Set by the mobile sessions drawer, whose slide runs on the COMPOSITOR
   * (WAAPI — see `registerDrawerTargets` in useDrawerSwipe). Projection only
   * stays correct while framer owns every animated ancestor transform: under a
   * compositor-driven ancestor it attributes the panel's travel to the rows
   * themselves and compounds a corrective transform per re-measure (measured
   * >4,000px — the rows visibly flew in from the panel's right edge). The rows
   * are the sidebar's ONLY projection nodes, so this one switch is the whole
   * containment. Costs on mobile: reorders/pin moves snap instead of glide,
   * and the flat↔tree toggle loses its row-morph continuity.
   */
  staticRows?: boolean
  /** Open a session as a TAB on the host surface instead of switching to it,
   *  bound to middle-click, modifier-click and the row menu's "Open in a session
   *  tab".
   *
   *  `background` follows the pointer/menu split every browser and editor uses:
   *  a middle-click or modifier-click QUEUES the session without moving the user
   *  (that is what makes triaging three rows in a row useful), while the menu
   *  item is a deliberate "take me there" and opens in the foreground.
   *
   *  Omitted on surfaces with no tab strip (the embed sessions list, a popped-out
   *  window), and an omitted callback leaves the gestures unbound rather than
   *  falling back to a plain switch — a middle-click that quietly navigated
   *  would be indistinguishable from a misfire. */
  onOpenSlotInNewTab?: (key: string, opts?: { background?: boolean }) => void
  /** Reveal a session's pull request / issue in the side panel instead of
   *  leaving for the provider's website.
   *
   *  Fires AFTER the row's own switchSlot dispatch, so the consumer can address
   *  the panel of the session the chip belongs to. Returns whether the panel took
   *  the link: FALSE (or an omitted callback) falls back to plain link
   *  navigation, which is the correct behaviour both on a surface with no side
   *  panel (the `/embed/sessions` list) and for a url the panel cannot resolve. */
  onOpenSource?: (slotKey: string, link: { url: string; kind: 'change' | 'issue' }) => boolean
  /** When true, ChatPage floats a hide-sidebar button over this header's
   *  top-left (open state), so the header reserves left space for it.
   *  Omitted in embed/sessions mode where the sidebar is the whole view. */
  collapsible?: boolean
  /** Element to portal the "drag a session into the chat" drop zone into —
   *  ChatPage's chat-pane wrapper. The zone renders inside this component's
   *  DndContext (so dnd-kit sees it) but measures against the pane's rect, which
   *  is what makes the whole pane a valid target rather than just the composer.
   *  Omit to disable the gesture (embed/sessions mode has no chat pane). */
  chatDropTarget?: HTMLElement | null
  /** Called when a session is dropped on the chat pane. Receives a snapshot,
   *  not a live slot, because the composer stages it until send. Never fired for
   *  incognito/temporary sessions or for the already-active session. */
  onDropSessionRef?: (ref: { key: string; title: string; messages?: number }) => void
  /** Where a crew row's window opens, for a host with no chat pane of its
   *  own (embed/sessions). Omit to open it over this page's pane. */
  onOpenPeerSession?: (instanceId: string, key: string) => void
}

/** Sort options, in menu order. The label lives in `SORT_LABEL_KEY`. */
const SORT_OPTIONS: { value: SortKey }[] = [
  { value: 'date-desc' },
  { value: 'date-asc' },
  { value: 'created-desc' },
  { value: 'created-asc' },
  { value: 'name-asc' },
  { value: 'name-desc' },
]
/** Catalog key per sort option — same resolvable shape as `FILTER_LABEL_KEY`. */
export const SORT_LABEL_KEY: Record<SortKey, string> = {
  'date-desc': 'pages.chatSidebar.sort_newest',
  'date-asc': 'pages.chatSidebar.sort_oldest',
  'created-desc': 'pages.chatSidebar.sort_created_newest',
  'created-asc': 'pages.chatSidebar.sort_created_oldest',
  'name-asc': 'pages.chatSidebar.sort_name_asc',
  'name-desc': 'pages.chatSidebar.sort_name_desc',
}
/** Catalog key per folder sort mode -- the "Folder order" rows in the same menu.
 *  Three rows because there are three modes; the list itself is
 *  `FOLDER_SORT_MODES`, so a fourth mode fails typing here rather than rendering
 *  with no label. */
export const FOLDER_SORT_LABEL_KEY: Record<FolderSortMode, string> = {
  custom: 'pages.chatSidebar.folder_order_custom',
  name: 'pages.chatSidebar.folder_order_name',
  created: 'pages.chatSidebar.folder_order_created',
}

/** How many levels of conductor nesting still step the row to the right.
 *
 *  Lineage depth has no ceiling -- a conductor that opens a conductor nests as far as
 *  the work does -- and each level costs 14px of a sidebar that is 320px at its
 *  narrowest. Left uncapped, a deep chain walks the card off the right edge until the
 *  title is unreadable. Past this depth the rows stop stepping and the level is shown
 *  as a number instead, which keeps the information without the geometry. */
const CONDUCTOR_MAX_INDENT_DEPTH = 6

import { SIDEBAR_MIN } from './chat/sidebarWidth'
export { SIDEBAR_MIN, SIDEBAR_MAX } from './chat/sidebarWidth'

/**
 * The Sessions sidebar: composes the owners under ./chat-sidebar/ (state, projections,
 * drag and drop, reveal, search) and renders what they produce.
 *
 * Owner hooks are called where their block used to sit, so React runs their effects in
 * the order the sidebar has always run them; ChatSidebar.ownerComposition.test.ts pins
 * that call order. Which rows pass the filters, their order, their lane and folder, and
 * what each one shows come from the row model (./chat-sidebar/rows), which reads local
 * key-indexed state for the rows it builds only after its one origin check. SessionRow,
 * the row and folder render closures, the idle-session cleanup, the bulk model switch
 * and the JSX stay in this file: source pins read them here (the switchSlot call-site
 * count, list-shell parity, the bulk switcher, the restyle ratchet), and the render
 * closures stamp rows in paint order. The owner table and the pin list are in
 * docs/system-specs/modules/history.md.
 */
function ChatSidebar({
  // Bound as `localSlots`, NOT `slots`. This component now holds TWO
  // collections — the caller's local tabs and `allRows`, which also carries
  // live peer rows — and a site that reads the wrong one fails silently: a
  // local-only read under-reports the rendered set, and a merged read leaks
  // local key-indexed state onto a colliding peer key. Neither shows up as a
  // crash, so the names are the guard. The prop itself keeps its public name;
  // only the binding is scoped, which forces every call site inside this file
  // to say which collection it means.
  slots: localSlots, activeSlot, unreadSlots, history, historyHasMore,
  defaultAgent, installedAgents, mode, onWidthChange, onDragChange, fillsHost, onSelectSlot, onOpenSlotInNewTab, onOpenSource, onOpenPeerSession, collapsible,
  chatDropTarget, onDropSessionRef, staticRows,
}: ChatSidebarProps) {
  useLanguageGeneration() // memo() bails out of the provider-level repaint; subscribe directly
  const dispatch = useAppDispatch()
  const queryClient = useQueryClient()
  // Read-only store handle for point-in-time reads inside async callbacks (the
  // rename-recovery compare-and-set in ./chat-sidebar/rename). useAppSelector subscribes and would
  // re-render; useStore().getState() reads the live value without subscribing.
  const store = useStore<RootState>()
  const ime = useImeGuard()
  const isMobile = useIsMobile()
  const isTouchDevice = useIsTouchDevice()

  // Sidebar-only state
  const [seedError, setSeedError] = useState('')
  // The board/list toggle also persists a chat-config field (`tagColumnsEnabled`)
  // via `saveChatConfig`, which returns false and rolls back when storage cannot
  // hold the write (GPT 6.1, errors-use-error-notice). That reuses the seedError
  // notice slot below, but with a save-specific title — this flag picks it.
  const [toggleSaveFailed, setToggleSaveFailed] = useState(false)
  // Shared failure line for the board's column mutations (delete / reorder /
  // add-after / card drop) — one state, because they all edit the same strip and
  // a second banner per verb would stack. Server-side inputs only, so a failed
  // write leaves nothing to re-enter; the caches are re-synced alongside.
  const [boardError, setBoardError] = useState('')
  // Same shape for the folder mutations (create / delete / update): the optimistic
  // update already rolls the cache back, but a rolled-back rename with no message
  // reads as a dead click.
  const [folderActionError, setFolderActionError] = useState('')
  // A failed "New chat" (any local variant) used to be a silent no-op: the
  // react-query rejection was swallowed and nothing rendered. Mirrors
  // remoteCrewError below, but lives above the list rather than in the menu,
  // because the plain entries close the menu on select.
  const [newChatError, setNewChatError] = useState('')
  // Inline failure reason for "New chat on crew" — a crew create can 502 and
  // leave nothing behind, so its reason is shown in the submenu rather than lost.
  const remoteCrewErrorId = useId()
  const [remoteCrewError, setRemoteCrewError] = useState('')
  // Controlled open for the New-chat menu, so a successful crew create can close
  // it (the crew rows preventDefault to stay open on failure) and closing clears
  // any stale remoteCrewError.
  const [newChatMenuOpen, setNewChatMenuOpen] = useState(false)
  const [slotFilter, setSlotFilter] = useState('')
  const [historyFilter, setHistoryFilter] = useState('')
  // A resumed history row whose surface ChatPage cannot display used to succeed
  // on the wire and then silently bounce the user back to whatever slot was
  // already open, indistinguishable from a dead click (#3624). Neither the
  // check nor the notice lives here any more: `resumeFromHistory` records the
  // outcome on the chat slice and ChatPage renders it above the composer, so
  // the four sibling resume entry points get the same feedback (#5925).
  // Digest of session keys + titles (NOT status), fed to both searches as their
  // revalidate signal. Sorted+joined so reordering `slots` alone cannot refetch.
  const slotTitleDigest = useMemo(
    () => localSlots.map(s => s.key + '\u0000' + (s.title || '')).sort().join('\u0001'),
    [localSlots],
  )
  const {
    historySearchResults, instancesList, instanceSessions, remoteSessionsError, allRows, allLiveSlots,
    selectInstance, crewGroups,
  } = useSessionSources({ historyFilter, slotTitleDigest, localSlots })
  // Opening a crew row is local and instant: the window reads the peer's slot
  // itself, so nothing is created here and there is nothing to wait on.
  const crewWindow = useCrewWindow()
  const onOpenPeerSessionRef = useRef(onOpenPeerSession)
  onOpenPeerSessionRef.current = onOpenPeerSession
  const openPeerSession = useCallback((instanceId: string, remoteSlot: string) => {
    // A host with no chat pane of its own (the embedded Sessions list) says
    // where the window opens; otherwise it opens over this page's pane.
    if (onOpenPeerSessionRef.current) onOpenPeerSessionRef.current(instanceId, remoteSlot)
    else openCrewWindow({ instanceId, key: remoteSlot })
  }, [])
  // Connected crews, for the "New chat on crew" entry. `warm` is the authority
  // on which peers hold a live tunnel (it holds the loopback port + minted
  // token); `instancesList` only supplies the display name, so a crew missing
  // from the query still offers its id rather than vanishing from the menu.
  //
  // Select the `warm` OBJECT, never a derived array. react-redux compares a
  // selector's result by reference, so returning `Object.keys(...)` allocates a
  // fresh array on every call, never equals the previous one, and re-renders the
  // sidebar in a loop until the heap dies. That is why `hasWarmInstances`
  // (./chat-sidebar/sessionSources) selects a primitive (`.length > 0`) instead. Deriving happens in the memo.
  const warmMap = useAppSelector(s => s.instances?.warm)
  const warmCrews = useMemo(
    () => Object.keys(warmMap ?? {})
      .map(id => ({ id, name: instancesList.find(i => i.id === id)?.name || id }))
      // compareText, not `localeCompare`: a bare localeCompare collates in the
      // HOST locale and ignores the app language entirely, so the crew list
      // would order itself differently from every other list on the page.
      .sort((a, b) => compareText(a.name, b.name)),
    [warmMap, instancesList],
  )
  // Which folder groups are collapsed in the grouped search-results view.
  // Ephemeral: reset on every query change so a fresh search shows all groups.
  const [collapsedHistoryGroups, setCollapsedHistoryGroups] = useState<Set<string>>(() => new Set())
  useEffect(() => { setCollapsedHistoryGroups(new Set()) }, [historyFilter])
  // Backend relevance rank per slot key (0 = best). A Map instead of a Set so
  // `filteredSlots` can ORDER matches by the backend's ranking (title matches
  // carry a strong field boost server-side) rather than re-sorting them by
  // date, which buries a title match below every fresher session that merely
  // mentions the query in its body. First-wins on canonical-key collisions so
  // a duplicate file cannot demote the better-ranked entry.
  const slotSearchRanks = useDebouncedSessionSearch(
    slotFilter,
    sessions => {
      const ranks = new Map<string, number>()
      sessions.forEach((s, i) => {
        const key = s.key.replace(/^dashboard_/, '')
        if (!ranks.has(key)) ranks.set(key, i)
      })
      return ranks
    },
    slotTitleDigest,
  )
  const {
    renamingSlot, renameScope, renameValue, renameError, setRenameError, renameInputRef,
    suppressMenuRestoreRef, onRenameStart, onRenameChange, onRenameCancel, onRenameCommit,
    onMenuCloseAutoFocus,
  } = useSessionRename({ dispatch, store, queryClient })
  // Folder create / settings modal target. One modal instance is rendered at the
  // sidebar root, so — unlike the inline inputs it replaced — it needs no column
  // scope: a folder rendered in several board columns can only have one modal.
  // `parentId` is the fixed destination for 'create' ('' = top level).
  const [folderModal, setFolderModal] = useState<
    { mode: 'create'; parentId: string } | { mode: 'edit'; folderId: string } | null
  >(null)
  const [sortKey, setSortKey] = useState<SortKey>(readSessionSortKey)
  const { lane, setLanePersisted, setFlatView } = useSidebarLane()
  /** Derived, so every existing `flatView` read site keeps its exact meaning. */
  const flatView = lane === 'flat'
  const conductorView = lane === 'conductor'
  const {
    activeFilters, filtersPaused, setAllFiltersPaused, clearAllFilters,
    filterHiddenFolders, setFilterHiddenFolders, toggleFolderFilter, unhideFolderChain,
    showAllFolders, filterTagIds, toggleTagFilter, clearTagFilter, foldersShelved, setFoldersShelved,
    toggleFoldersShelved, toggleFilter, disableFilter, enableFilter,
  } = useSessionFilterState()
  const {
    slotsLoaded, workflowActiveSet, automationRunningSet, subagentCounts, subagentStartedCounts, subagentApprovalCounts, unreadSet,
    recentWindowMs, recentAmountDraft, setRecentAmountDraft, recentUnitDraft, selectRecentPreset,
    commitRecentAmount, changeRecentUnit, runningSet, recentSet,
  } = useSessionStatusFilters({ unreadSlots, activeFilters, filtersPaused, enableFilter, localSlots, disableFilter })
  const creatingSlot = useAppSelector(s => s.chat.creatingSlot)
  const connected = useConnected()
  const {
    historyOpen, setHistoryOpen, openHistoryPane, historyHeight, historyDragging, historyResize,
  } = useHistoryPane({ setHistoryFilter, slotFilter, dispatch })
  const [cleanupOpen, setCleanupOpen] = useState(false)
  const [folderCleanupOpen, setFolderCleanupOpen] = useState(false)  // header ⋮ → "Clean up empty folders" panel
  const [manageTagsOpen, setManageTagsOpen] = useState(false)  // header ⋮ → "Manage tags…" panel (list-view tag CRUD)
  const [filterSortOpen, setFilterSortOpen] = useState(false)
  const [cleanupDays, setCleanupDays] = useState(3)
  const [cleanupExpanded, setCleanupExpanded] = useState(false)
  const [cleanupError, setCleanupError] = useState('')
  const { data: cleanupPreviewData, isLoading: cleanupPreviewLoading, isError: cleanupPreviewError } = useQuery({
    queryKey: ['cleanup-preview', cleanupDays, activeSlot],
    queryFn: () => api.cleanupSessions(cleanupDays, activeSlot || '', true),
    enabled: cleanupOpen,
    gcTime: 0,
  })
  const cleanupPreview = cleanupPreviewData?.keys ?? null
  const activeIsStale = cleanupPreviewData?.active_is_stale ?? false
  const cleanupMutation = useMutation({
    mutationFn: () => api.cleanupSessions(cleanupDays, activeSlot || ''),
    onSuccess: (res) => {
      if (res.keys?.length) {
        for (const key of res.keys) dispatch(deleteSlot(key))
        dispatch(fetchHistory(false))
      }
      if (res.failed?.length) {
        setCleanupError(`${res.failed.length} session(s) failed to archive`)
      } else {
        setCleanupOpen(false)
      }
      queryClient.invalidateQueries({ queryKey: ['cleanup-preview'] })
    },
    onError: (e) => setCleanupError(e instanceof Error ? e.message : i18nT('pages.chatSidebar.archive_failed')),
  })

  // Bulk model switch — apply one model to every live session at once.
  const [bulkModelOpen, setBulkModelOpen] = useState(false)
  const [bulkModel, setBulkModel] = useState('')        // pending pick ('auto' = provider default)
  const [bulkSkipRunning, setBulkSkipRunning] = useState(true)
  const [bulkModelError, setBulkModelError] = useState('')
  // Per-instance id: ChatPage mounts a mobile-drawer sidebar and a desktop one, so a
  // literal id would collide and point one panel's checkbox at the other's label.
  const bulkSkipRunningLabelId = useId()
  const bulkModelsQuery = useAvailableModelsQuery({ enabled: bulkModelOpen })
  const bulkModelOptions = bulkModelsQuery.data
  // The roster failed to load when EITHER flag is up. The ACP adapter never
  // rejects: a 503 / network error / empty response resolves with the last-good
  // cached list or Auto alone and marks the provider degraded, so `isError`
  // alone would stay false through every real failure and the panel would show
  // a one-entry list as if that were the whole catalog. Same pair Settings >
  // Chat reads for its model selects.
  const bulkModelsFailed = bulkModelsQuery.isError || bulkModelsQuery.isDegraded
  // The pick counts only while the roster still lists it. A degraded roster is
  // the last-good CACHED list, so a model can be picked from it, Retry can then
  // succeed with a roster that no longer carries that model, and nothing else
  // would unpick it: the backend accepts any non-registry id, so Switch would
  // reset every session onto a model kiro-cli then refuses. Derived, not
  // stored, so there is no window between the roster changing and the pick
  // being cleared in which Switch could still fire with the stale id.
  const bulkModelPick = useMemo(
    () => (bulkModelOptions.some(m => m.name === bulkModel) ? bulkModel : ''),
    [bulkModelOptions, bulkModel],
  )
  const bulkRunningCount = useMemo(() => localSlots.filter(s => s.running).length, [localSlots])
  // Count only slots that would actually change: model differs from the target
  // (the backend leaves already-on-target slots as `unchanged`), minus running
  // slots when skipping. Keeps the "Switch N" label + disable guard honest.
  const bulkAffectedCount = useMemo(() => {
    return localSlots.filter(s => (s.model ?? '') !== bulkModelPick && (!bulkSkipRunning || !s.running)).length
  }, [localSlots, bulkModelPick, bulkSkipRunning])
  const bulkModelMutation = useMutation({
    // 'auto' goes on the wire verbatim (not collapsed to ''): '' doubles as the
    // "never chosen" state that every reader re-resolves to the agent template's
    // model, so it cannot express an explicit Auto pick.
    mutationFn: ({ model, skipRunning }: { model: string; skipRunning: boolean }) =>
      api.chatSlotsModel(model, skipRunning),
    onSuccess: (res) => {
      // The switched models refresh on the next authoritative sseSlots push;
      // this handler does not eagerly reflect them. The previous dead-key
      // `invalidateQueries(['chat-slots'])` was a no-op (no query is registered
      // on that key; slot.model lives in the Redux dashboard slice), and an
      // eager client-side patch here would need a per-field reconciliation
      // contract to avoid overwriting a reordered authoritative frame -- that
      // belongs to the whole-list applySlots reducer-contract work in #11149,
      // not this rename-recovery fix. Removing the no-op keeps the pre-existing
      // behaviour without carrying that contract into this PR.
      // Partial failure: the endpoint returns 200 with a non-empty `failed`
      // list when some slots' resets raised. Surface it and keep the panel
      // open instead of silently closing on a partial success.
      if (res.failed?.length) {
        setBulkModelError(i18nT('pages.chatSidebar.session_failed_to_switch', { count: res.failed.length }))
      } else {
        setBulkModelOpen(false)
        setBulkModel('')
        setBulkModelError('')
      }
    },
    onError: (e) => setBulkModelError(e instanceof Error ? e.message : i18nT('pages.chatSidebar.switch_failed')),
  })
  // Roving-focus keyboard nav for the model list (WAI-ARIA listbox). No filter
  // input here, so the hook moves focus into the list on open; Escape/Tab close.
  const bulkListRef = useRef<HTMLDivElement>(null)
  const bulkInputRef = useRef<HTMLInputElement>(null)
  const { onListKeyDown: bulkOnListKeyDown } = useListboxKeyboard({
    open: bulkModelOpen,
    dropdownRef: bulkListRef,
    inputRef: bulkInputRef,
    hasFilterInput: false,
    filteredCount: bulkModelOptions.length,
    onEnterSingleMatch: () => {},
    closeToTrigger: () => { setBulkModelOpen(false); setBulkModel(''); setBulkModelError('') },
  })

  const {
    pinned, pinnedOrder, pinnedRank, reorderPinned, orderState: pinnedOrderState,
  } = usePinnedSessionOrder({ localSlots, sortKey })

  const {
    staleCollapseMs, setStaleCollapseMs, staleExpanded, setStaleExpanded, setStaleRecentlyMoved,
    isStaleExempt,
  } = useStaleCollapse({ pinned, activeSlot, runningSet, subagentCounts, unreadSet })

  // The two halves render either side of the expander, so a held row crossing it
  // leaves the sub-list the anchor was measured against. Freeze the side instead.
  const holdStaleSide = (split: StaleSplit<Slot>): StaleSplit<Slot> => {
    const pin = hoverPinRef.current
    if (!pin || pin.scope !== 'list') return split
    const from = pin.staleSide ? split.fresh : split.stale
    // `pin.key` and `seenOrder` are origin-qualified (captured from
    // `data-session-row`), so every slot must be matched through
    // `sessionRowIdentity` — a raw `s.key` compare would miss a peer row and
    // collide two rows that share a raw key.
    const at = from.findIndex(s => sessionRowIdentity(s) === pin.key)
    // Absent from the side it does not belong on is the normal case, in every
    // container that does not hold the row as well as before it migrates.
    if (at < 0) return split
    const rank = new Map(pin.seenOrder.map((k, i) => [k, i]))
    const mine = rank.get(pin.key)
    if (mine == null) return split
    const to = pin.staleSide ? split.stale : split.fresh
    // Reseat by the CAPTURED order, so it returns to the position it was read at
    // rather than to the end of the half it is going back to.
    const seat = to.reduce((n, s) => {
      const r = rank.get(sessionRowIdentity(s))
      return n + (r != null && r < mine ? 1 : 0)
    }, 0)
    const seated = to.slice()
    seated.splice(Math.min(seat, seated.length), 0, from[at])
    const rest = from.filter(s => sessionRowIdentity(s) !== pin.key)
    return pin.staleSide ? { fresh: rest, stale: seated } : { fresh: seated, stale: rest }
  }

  // The row model's dormant split (inert while narrowed or under a non-date sort),
  // then the hover hold on top of it.
  const splitStale = (list: Slot[]): StaleSplit<Slot> =>
    holdStaleSide(sidebarRows.staleSplit(list, { collapseMs: staleCollapseMs, isExempt: isStaleExempt }))
  const renderStaleSection = (containerId: string, staleSlots: Slot[], depth: number, containerName?: string): React.ReactNode => {
    if (staleSlots.length === 0) return null
    const open = staleExpanded.has(containerId)
    const regionId = `stale-rows-${containerId}`
    const lblId = `stale-lbl-${containerId}`
    const ctxId = `stale-ctx-${containerId}`
    // One pluralised sentence carries the count AND says where the rows went,
    // so a folder badge of "2" over one visible row plus "1 dormant session
    // hidden" visibly adds up. A bare noun + count pill read as a category,
    // not as "the rest are in here" (gui-user-test friction on this row).
    const count = staleSlots.length
    const label = open
      ? i18nT('pages.chatSidebar.stale_collapse_row_shown', { count })
      : i18nT('pages.chatSidebar.stale_collapse_row_hidden', { count })
    // The threshold is otherwise only named in the sort/filter menu; the same
    // compact window label ("7d") ties the row back to that setting.
    const windowLabel = formatRecentWindow(staleCollapseMs)
    const hint = open
      ? i18nT('pages.chatSidebar.stale_collapse_row_hint_open', { window: windowLabel })
      : i18nT('pages.chatSidebar.stale_collapse_row_hint', { window: windowLabel })
    return (
      <Fragment key={`stale-${containerId}`}>
        {/* aria-labelledby composes the visible sentence + a visually-hidden
            container name, so AT announces "1 dormant session hidden <folder>"
            — an aria-label would override the button contents and re-composing
            it per locale is exactly the concatenation trap the i18n rules ban. */}
        <button type="button"
          aria-expanded={open}
          aria-controls={regionId}
          aria-labelledby={`${lblId} ${ctxId}`}
          title={hint}
          data-testid={`stale-expander-${containerId}`}
          onClick={() => setStaleExpanded(prev => {
            const next = new Set(prev)
            if (next.has(containerId)) next.delete(containerId); else next.add(containerId)
            return next
          })}
          data-stale-toggle="" className="w-full flex items-center gap-1.5 pl-2.5 pr-3 py-0.5 rounded-md text-[11px] leading-4 text-muted hover:text-accent hover:bg-bg-hover transition-all bg-transparent border-none cursor-pointer text-left">
          <DisclosureChevron open={open} size={11} />
          <span id={lblId} className="tabular-nums">{label}</span>
          <span id={ctxId} className="sr-only">{containerName
            ? i18nT('pages.chatSidebar.stale_collapse_ctx_in_name', { name: containerName })
            : i18nT('pages.chatSidebar.stale_collapse_row_ungrouped')}</span>
        </button>
        {/* The controlled region always exists so aria-controls never dangles
            in the collapsed state; only the rows are conditionally mounted. */}
        <div id={regionId} data-stale-region={containerId} hidden={!open}>{open && staleSlots.map(s => renderSessionRow(s, depth, false))}</div>
      </Fragment>
    )
  }
  // ── end stale-session collapse ─────────────────────────────────────────────

  // In-flow discovery affordance for the Older Sessions pane: a text row that
  // follows the LAST session of a lane. It triggers the same action as the
  // persistent footer, but answers a different question. The footer is a
  // structural control pinned under the scroll area for a user who already
  // knows the pane exists; this row sits where a user scanning the list runs
  // out of rows without finding their chat — which is exactly where a session
  // evicted from the open-tab list (idle eviction, restart, cleanup, a closed
  // tab) has gone. A new user has no other cue that sessions move anywhere, so
  // the row is what connects "my chat is gone" to "it is one click below".
  // Hidden while the pane is open: the pane itself is then the continuation.
  const renderOlderSessionsHint = (lane: string): React.ReactNode => {
    if (historyOpen) return null
    return (
      <button
        type="button"
        data-testid={`older-sessions-hint-${lane}`}
        onClick={openHistoryPane}
        className="mt-1 mx-1 px-2 py-1.5 text-left text-[12px] text-muted hover:text-accent hover:bg-accent-subtle rounded-md cursor-pointer bg-transparent border-none transition-colors"
      >
        {i18nT('pages.chatSidebar.show_all_older_sessions')}
      </button>
    )
  }

  // Ranks up to the configured count of sessions by settled recency for the sidebar tint —
  // see ../utils/recencyTint. Count = server-side dashboard.recent_tint_count (shared
  // kirocrewConfig query); recomputes when the slots or the configured count change.
  const { data: mcCfg, status: mcCfgStatus, error: mcCfgError, errorUpdatedAt: mcCfgErrorUpdatedAt } = useQuery({ queryKey: ['kirocrewConfig'], queryFn: () => api.kirocrewConfig() })
  const recentTintCount = clampTintCount(mcCfg?.dashboard?.recent_tint_count)
  const recentRank = useMemo(() => computeRecentRank(localSlots, recentTintCount), [localSlots, recentTintCount])

  const {
    folderSortRead, folderSortMode, folderCompare, folderReorderable, folderDragWithheld, folderReorderHint,
    folderReorderHintRef, showFolderReorderHint, hideFolderReorderHint, folderSortMut,
  } = useFolderSort({ queryClient, mcCfg, mcCfgStatus, mcCfgError, mcCfgErrorUpdatedAt, setFolderActionError })

  const {
    folderEditInputRef, editingId, setEditingId, editScope, setEditScope, editName, setEditName,
  } = useFolderRename({ renamingSlot, suppressMenuRestoreRef })

  // Folders via React Query. `isSuccess` gates the stale-collapse move
  // watcher below: before folder data has actually ARRIVED `folders` is the
  // [] default, so every filed slot would read as "just moved" the moment
  // real data lands — hydration is not user movement. isSuccess (not
  // isFetched, which is also true after a FAILED first fetch) stays false
  // through an error window until the websocket seed or a retry backfills.
  const { data: folders = [], isSuccess: foldersLoaded, isError: foldersFailed, error: foldersError, refetch: refetchFolders } = useQuery<ChatFolder[]>({ queryKey: ['chat-folders'], queryFn: () => api.chatFolders() })

  const {
    tagsData, tagsQueryFailed, refetchTags, tagById, activeTagIds, tagFilterRows, activeTagNames,
  } = useSidebarTags({ filterTagIds, localSlots })
  const {
    rawColumns, tagColumnsSettled, columnsFailed, columnsError, refetchColumns, tagColumnsEnabled,
    hideEmptyFolderBody, orderedColumns,
  } = useBoardColumns()
  // After the columns: only board view may widen the sidebar past SIDEBAR_MAX.
  const {
    paintedSidebarWidth, rootStyle, sidebarMax, paintedWidthRef, sidebarResize, nudgeSidebar, widenForBoard, restorePreBoardWidth,
  } = useSidebarResize({ onWidthChange, onDragChange, fillsHost, boardActive: orderedColumns.length > 0 })
  // Which edges of the board's lane strip hide lanes. A window too narrow for
  // every lane clips the strip at the sidebar's edge, and an overlay scrollbar
  // leaves no standing sign that more lanes sit past it, so the strip paints an
  // edge cue from these (the session tab strip's cue width). The box
  // observer covers a resize; a lane added or removed changes only the
  // content, so the column count re-measures it.
  const [attachStripEdges, stripEdges, remeasureStripEdges] = useScrollEdges<HTMLDivElement>()
  useEffect(() => { remeasureStripEdges() }, [orderedColumns.length, remeasureStripEdges])
  usePinnedOrderAuthority({ orderState: pinnedOrderState, slotsLoaded, tagColumnsSettled, orderedColumns })
  const {
    columnEditId, setColumnEditId, popoverPos, columnPopoverRef, columnPopoverImeLatch, closeColumnPopover,
  } = useColumnPopover()

  const {
    updateColumnMutation, deleteColumnMutation, reorderColumnsMutation, addColumnAfterMutation,
    dropSlotMutation, missingLanes, seedStateLanesMutation,
  } = useBoardColumnMutations({ queryClient, setBoardError, orderedColumns, rawColumns, paintedWidthRef, widenForBoard, setSeedError })
  const {
    columnMatches,
  } = useColumnMatches({ subagentStartedCounts, subagentApprovalCounts, workflowActiveSet, automationRunningSet })

  const {
    slotFolders, foldersWithActiveSubtree, setRevealForcedVisible, isFolderHidden, filterHiddenSubtree,
  } = useFolderVisibility({ folders, localSlots, filterHiddenFolders })

  useStaleMoveWatcher({ foldersLoaded, localSlots, slotFolders, setStaleRecentlyMoved })
  // Which folders' closed-sessions rows are open. Held here, not in the row:
  // the tree re-creates folder body nodes every render, which would reset it.
  const [closedOpen, setClosedOpen] = useState<ReadonlySet<string>>(() => new Set())
  // Refetch the folder counts when a filed tab opens or closes. A folder's
  // `closed_count` moves exactly then, and no folder-store write follows to
  // trigger the generation refetch, so without this the closed-sessions row
  // would keep the count it had at page load.
  // Read off the slots themselves, not `slotFolders`: that map also moves when
  // the folder list itself loads, and refetching then would answer a load with
  // a second load.
  const filedTabsSignature = useMemo(
    () => localSlots.filter(s => s.folder_id).map(s => `${s.key}=${s.folder_id}`).sort().join('|'),
    [localSlots],
  )
  const filedTabsSeen = useRef<string | null>(null)
  useEffect(() => {
    if (filedTabsSeen.current !== null && filedTabsSeen.current !== filedTabsSignature) {
      void queryClient.invalidateQueries({ queryKey: ['chat-folders'] })
    }
    filedTabsSeen.current = filedTabsSignature
  }, [filedTabsSignature, queryClient])

  const {
    searchRanked, folderNameMatchIds,
  } = useSearchMatches({ slotFilter, slotSearchRanks, folders, isFolderHidden })

  // The folder filter goes inert while searching, in BOTH views: a query must
  // reach every match, so an unchecked folder can never become a search dead
  // end. Everything that consults the filter routes through this flag.
  const folderFilterActive = slotFilter.trim() === '' && filterHiddenFolders.size > 0

  // While a drag is in flight the row model keeps the filtered list it had when the
  // drag started, so the lanes hold still under the pointer.
  const [dragFrozen, setDragFrozen] = useState(false)
  // Layout-projection budget: every enrolled session row belongs to one
  // LayoutGroup, and Framer measures getBoundingClientRect for each enrolled
  // node on a commit. renderSessionRow therefore enrolls only the first
  // SIDEBAR_DISPLACEMENT_WINDOW paint positions; later rows stay ordinary
  // motion divs and snap. Reduced motion disables even that bounded window.
  // The shared live reader (not framer's useReducedMotion): the sidebar test
  // files mock framer-motion per-file, and the hook reads the media query
  // directly and re-renders on change.
  const reduceMotion = useReducedMotion()

  // The reveal's ways to drop the search and a row's folder hide (the hidden ancestor
  // chain, walked over the current folders). The tag and status clears are stable
  // owner callbacks.
  const clearSearch = useCallback(() => setSlotFilter(''), [])
  const clearFolderHide = useCallback((folderId: string | undefined) => unhideFolderChain(folderId, folders),
    [unhideFolderChain, folders])
  // Per-machine groups (preview): a row a crew owns, or a local slot whose turns
  // run on it, leaves the Local lanes and renders under that crew's group, in the
  // same filtered order. The board keeps every row in its columns, so it groups
  // nothing.
  const shownCrewGroups = orderedColumns.length > 0 ? NO_CREW_GROUPS : crewGroups
  // The row model (./chat-sidebar/rows): which rows pass the filters, the one lane
  // order, the lanes' and folders' populations, the reveal registry and the chip
  // counts. Live sessions from connected remote instances are merged in ahead of the
  // filter and the sort, so a remote row is narrowed and ordered by exactly the same
  // rules as a local one, while local key-indexed state is read only after the
  // model's origin check. Rebuilt every render from the previous result: each
  // memoized output keeps its identity while the inputs it reads hold still; the
  // result, its `lanes`, `staleSplit` and `views` are fresh per build.
  const previousRowsRef = useRef<SidebarRows | null>(null)
  const sidebarRows = buildSidebarRows({
    rows: allRows,
    local: {
      folderOf: slotFolders, pinned, pinnedRank, unread: unreadSet, running: runningSet, recent: recentSet,
      searchRanks: searchRanked,
    },
    filters: {
      tags: { resolved: activeTagIds, raw: filterTagIds },
      search: { text: slotFilter, folderMatches: folderNameMatchIds },
      status: { active: activeFilters, paused: filtersPaused, recentWindowMs },
      folders: { hiddenSubtree: filterHiddenSubtree, active: folderFilterActive },
    },
    clears: { tags: clearTagFilter, search: clearSearch, status: clearAllFilters, folder: clearFolderHide },
    crewGroups: shownCrewGroups,
    sortKey,
    clock: Date.now,
    frozen: dragFrozen,
  }, previousRowsRef.current)
  previousRowsRef.current = sidebarRows
  const { listNarrowed, revealBlockingFilters, laneOrder, isRowFolderHidden } = sidebarRows
  // `laneSlots` is the Local lanes' rows: `filteredSlots` minus every row a crew
  // group holds. `shownCrewRows` is each crew group's rows, the folder hide applied.
  const {
    main: laneSlots, crew: shownCrewRows, crewRowCount: shownCrewRowCount,
    flat: flatSlots, ungrouped: ungroupedSlots, boardPeerCount,
  } = sidebarRows.lanes
  const filterCounts = sidebarRows.statusCounts
  const [collapsedCrews, toggleCrewCollapsed, expandCrew] = useCollapsedCrews()

  const { hoverPinRef, heldDisplacedRef, releaseHoverPin, heldLane, onRootPointerOver } = useHoverHold()

  const { lineageAvailable } = useLineageAvailable({ allRows })
  // Which lane the sidebar is actually rendering (see `renderedLane`). The tag-column
  // board wins when columns exist — flat view does not replace it, it applies INSIDE
  // each column (folders skipped, the column's rows render flat). The conductor lane
  // is gated on `lineageAvailable` as well as the board, and the reason is the toggle:
  // it renders only when more than one lane is available, so with a persisted
  // conductor preference, no edges and no folders the cycle holds `tree` alone, the
  // button is not drawn at all, and an ungated lane would render a layout the user has
  // no control to leave. The folder filter applies to the flat lane and the tree, NOT
  // to the board.
  const shownLane = renderedLane({ lane, boardColumns: orderedColumns.length, folders: folders.length, lineageAvailable })
  const boardLaneActive = shownLane === 'board' || shownLane === 'board-flat'
  const flatLaneActive = shownLane === 'flat'
  const conductorLaneActive = shownLane === 'conductor'
  // Whether any folder ROW is on screen to drag: the tree, and the board unless
  // flat view empties its columns of folders (`relevantFolders`). The flat lane
  // explodes chats out of their folders and the conductor lane nests by lineage.
  // Gates only the copy about DRAGGING (the menu's reorder note) -- the folder
  // order itself is read and offered in every lane.
  const folderRowsDrawn = shownLane === 'tree' || shownLane === 'board'

  // Scroll memory for the session lane. Collapsing the sessions sidebar (or
  // closing the mobile drawer) UNMOUNTS ChatSidebar — OverlayDrawer gates its
  // children on `open` — so the lane remounted at the top and a user who had
  // scrolled deep into a long list was thrown back on every reopen. Anchored
  // on the top visible ROW rather than a pixel offset: rows are
  // `content-visibility: auto` with a 60px intrinsic placeholder, so a fresh
  // mount lays never-rendered rows out taller than rendered ones and the same
  // scrollTop lands on a different session. One entry per lane kind (flat and
  // tree keep independent positions); board columns are their own scrollers
  // and out of scope here. See useLaneScrollMemory.
  const laneScrollRef = useRef<HTMLDivElement | null>(null)
  // Row windowing (pages/chat/sessionRowWindow): the lane scroller is the
  // observer root, so the one callback ref feeds both the scroll memory's ref
  // object and the window. Stable, so React does not detach and re-attach it on
  // every commit.
  const laneRowWindow = useSessionRowWindowRoot()
  const setLaneRowWindowRoot = laneRowWindow.setRoot
  const setLaneScrollEl = useCallback((el: HTMLDivElement | null) => {
    laneScrollRef.current = el
    setLaneRowWindowRoot(el)
  }, [setLaneRowWindowRoot])
  const laneScrollMemory = useLaneScrollMemory(
    boardLaneActive ? null : `chat-sidebar-lane:${conductorLaneActive ? 'conductor' : flatLaneActive ? 'flat' : 'tree'}`,
    laneScrollRef,
  )

  useHoverPinLiveness({ hoverPinRef, releaseHoverPin, filteredSlots: laneSlots, boardLaneActive, flatLaneActive, conductorLaneActive, orderedColumns })

  useStaleNarrowBridge({ listNarrowed, filteredSlots: laneSlots, staleCollapseMs, sortKey, isStaleExempt, setStaleExpanded, slotFolders })
  // Reduced motion disables every row. Otherwise renderSessionRow enrolls only
  // the first SIDEBAR_DISPLACEMENT_WINDOW paint positions in layout projection,
  // bounding Framer's measurement set without a total-list-size cliff.
  const rowAnimEnabled = !reduceMotion

  const {
    isFolderFilteredOut, revealedContainers, toggleReveal, hiddenByContainer, allHiddenFolders,
    hiddenFolderCount,
  } = useFolderFilterReveal({ folderFilterActive, filterHiddenFolders, folders, isFolderHidden, folderCompare, boardLaneActive })
  const {
    citedCreatorExists, conductorRows, conductorMatching, lineage, conductorExpanded, toggleConductorExpanded,
    expandConductorAncestors,
  } = useConductorLane({ conductorLaneActive, allRows, allLiveSlots, isRowFolderHidden, laneOrder, flatSlots })
  // The rows the conductor lane may show, by identity: every match, plus each
  // ancestor a match needs to hang from. The lane draws exactly these, the ✕
  // label counts exactly these, and the tree close plans over exactly these, so
  // a press can never reach a worker the filter or a hidden folder kept off screen.
  const conductorKept = useMemo(() => {
    const kept = new Set<string>()
    if (!lineage) return kept
    const onLane = new Set(conductorRows.map(s => sessionRowIdentity(s)))
    for (const id of conductorMatching) {
      if (!onLane.has(id)) continue
      kept.add(id)
      for (const up of ancestorsOf(id, lineage.parentOf)) kept.add(up)
    }
    return kept
  }, [lineage, conductorRows, conductorMatching])
  const conductorSearching = slotFilter.trim() !== ''
  const treeCloseRows = useMemo(
    () => localSlots.filter(s => conductorKept.has(sessionRowIdentity(s))),
    [localSlots, conductorKept],
  )
  // Reveal opens the crew group holding the target, then the lane's own ancestors.
  // Looked up in `allRows`: the reveal may be clearing the filter that hides it.
  const expandRevealAncestors = useCallback((identity: string) => {
    const row = allRows.find(s => sessionRowIdentity(s) === identity)
    const crew = row ? crewOf(row) : undefined
    if (crew && shownCrewRows.has(crew)) expandCrew(crew)
    expandConductorAncestors(identity)
  }, [allRows, shownCrewRows, expandCrew, expandConductorAncestors])

  const {
    availableLanes, nextLane, laneSwitchLabel, cycleLane,
  } = useLaneCycle({ lineageAvailable, boardLaneActive, folders, lane, setLanePersisted })

  const sidebarRootRef = useRef<HTMLDivElement>(null)
  const { digitModifierHeld, shortcutDigitByKey } = useShortcutOrder({ sidebarRootRef, dispatch, localSlots })

  const {
    folderFilterRows,
  } = useFolderFilterRows({ folderTree: sidebarRows.folderTree, folders, folderCompare, filterHiddenFolders, filterHiddenSubtree })

  const {
    createFolderMutation, deleteFolderMutation, updateFolderMutation, toggleCollapse,
  } = useFolderMutations({ queryClient, setFolderActionError, folders })

  const { clearBoardCollapse, boardFolderCollapsed, toggleColumnCollapse } = useBoardFolderCollapse()

  // ── Folder drag-to-reorder ──
  // Mouse and touch are split on purpose; the split and its WebKit reasoning
  // live in the shared hook. 5px of mouse travel is this list's own choice -
  // rows are tightly packed and a click only selects, so the threshold can sit
  // lower than the Apps nav rail's. `keyboard` is on because this IS a sortable
  // ring, so the sortable coordinate getter has somewhere to move.
  const dndSensors = useDndSensors({ distance: 5, keyboard: true })
  // Tracks the item currently being dragged, for the DragOverlay preview.
  const [activeDrag, setActiveDrag] = useState<{ type: string; id: string } | null>(null)
  // Its native counterpart: the board card in flight, which no DndContext sees.
  const { nativeSessionDrag, startNativeSessionDrag, endNativeSessionDrag } = useNativeSessionDrag()
  // The derived lane a session card hovers right now. Such a lane refuses the
  // drop, and its header's hover title is out of sight mid-drag, so the lane
  // says why in a visible line instead. It shows only while `nativeSessionDrag`
  // is set, so the card drag's own end hides it; a new drag starts it clean.
  const [derivedHintCol, setDerivedHintCol] = useState<string | null>(null)
  const startBoardCardDrag = useCallback((key: string) => {
    setDerivedHintCol(null)
    startNativeSessionDrag(key)
  }, [startNativeSessionDrag])
  const {
    reorderFolders, moveFolderTo,
  } = useFolderDropOps({ folderReorderable, queryClient, setFolderActionError, updateFolderMutation })
  const { folderSubtrees, expandFolderAncestors } = useFolderTree({ folders, updateFolderMutation, clearBoardCollapse })

  const {
    revealFlash,
  } = useSidebarReveal({ sidebarRootRef, dispatch, localSlots, revealBlockingFilters, staleCollapseMs, sortKey, isStaleExempt, slotFolders, setStaleExpanded, expandFolderAncestors, expandConductorAncestors: expandRevealAncestors, folders, setSlotFilter, setFilterHiddenFolders, setRevealForcedVisible, setFlatView })
  const renameCommit = useCallback((id: string, name: string) => {
    if (name.trim()) updateFolderMutation.mutate({ id, body: { name: name.trim() } })
    setEditingId(null)
  }, [updateFolderMutation, setEditingId])
  const {
    dragMove, undoDragMove, undoBar, folderMove, undoFolderMove, folderUndoBar, moveByDrag, moveFolderByDrag,
  } = useSidebarMoveUndo({ localSlots, folders, moveFolderTo, queryClient })
  // Surface-agnostic session actions (duplicate/read/pin/copy/move/close) shared
  // by all three row menus AND the row's non-menu buttons (Duplicate/Close) so
  // each behaviour has one definition. Rename + Tags stay local (they drive this
  // component's inline-edit + tag-popover state).
  const sessionActions = useSessionActions(mode)
  // The row ✕ closes the card AND the sessions nested under it, so on a lead it can
  // end several workers' turns at once. `useCloseSessionTree` owns that: it plans the
  // subtree from the same lineage edges this lane nests by, asks before destroying
  // work still in flight, and falls back to `sessionActions.close` verbatim for a card
  // with nothing under it. `runningSet` is passed as the predicate so the
  // dialog marks exactly the rows the lane draws as running.
  // Passed plain, not memoized: the hook reads its inputs through a ref so that
  // `closeSessionTree` keeps one identity for the sidebar's life, which is what
  // lets it ride into each row's memoized menu props without busting the row memo.
  // `subagentCounts[s.key] > 0`: a worker whose own turn has ended while its
  // background subagent is still working looks idle to `runningSet`, but it has
  // active work that the tree close would silently cancel. Include it in the
  // guard so those workers block the cascade the same way a running turn does.
  const { closeSessionTree, closeTreeDialog, closeTreeError, closeTreeNotice } = useCloseSessionTree({
    rows: treeCloseRows,
    allRows: localSlots,
    isRunning: (s: Slot) => sidebarRowRunning(s, runningSet, subagentCounts),
    isBackgroundOnly: (s: Slot) => sidebarRowBackgroundOnly(s, runningSet, subagentCounts),
    closeOne: sessionActions.close,
    // The still-running notice links each running session to itself, opened the
    // same way a press on its row opens it, so the person can stop it there.
    openOne: (key: string) => { dispatch(switchSlot({ key, announceOnMissing: true })); onSelectSlot?.(key) },
  })
  // The tree-close guard is scoped to the conductor lane while it nests: tree
  // nesting is only visible there. In the flat, folder and board lanes, and in the
  // conductor lane while a search flattens it, the user sees no nested structure,
  // so pressing ✕ on a session closes exactly that one session.
  const treeClose = conductorLaneActive && !conductorSearching ? closeSessionTree : sessionActions.close
  // Which sessions are currently open in a popped-out window (shared singleton).
  const { poppedOut } = useChatPopouts()
  const {
    handleSidebarDragStart, reportDndActive, handleSidebarDragEnd, handleSidebarDragCancel,
    handleSidebarDragOver,
  } = useSidebarDragHandlers({ releaseHoverPin, setDragFrozen, hideFolderReorderHint, setActiveDrag, activeDrag, dragFrozen, folderReorderable, folderSortRead, showFolderReorderHint, moveFolderByDrag, reorderFolders, searchRanked, pinned, reorderPinned, localSlots, activeSlot, onDropSessionRef, moveByDrag, folders, boardFolderCollapsed, updateFolderMutation, clearBoardCollapse })
  const {
    folderCreateError, setFolderCreateError, createChatInFolder,
  } = useFolderChatCreate({ folders, defaultAgent, mode, dispatch, dropSlotMutation, onOpenSlotInNewTab, updateFolderMutation, clearBoardCollapse })

  const {
    crewPreview, openCrewMembers, remoteCrewChatPreview,
    createChatMutation, createRemoteChatMutation, createEphemeralChatMutation,
  } = useSessionCreate({ setNewChatError, dispatch, defaultAgent, mode, onOpenSlotInNewTab, setRemoteCrewError, setNewChatMenuOpen, onOpenPeerSession })
  // A conductor-lane member anchor opens on the Members page (see renderSessionRow).
  const navigate = useNavigate()
  // The Dynamic Dashboard is a Feature Preview (Settings > Developer): the
  // kebab's "All dashboards" door is offered only once it is on. The page it
  // opens stays routable either way, like every preview's route.
  const dashboardPreview = usePreviewFlag(PREVIEW_DASHBOARD)

  // Session colors
  const { paletteColors, boost, boostFor, colorMode } = useSessionPalette()

  // ── Session row (reference-style: color palette, memory_mode, rename on right-click) ──
  // Does any descendant (direct or nested) of `folderId` contain a slot from `slots`?
  function descendantMatch(fs: ChatFolder[], folderId: string, slots: Slot[], visited = new Set<string>()): boolean {
    if (visited.has(folderId)) return false // cycle guard
    visited.add(folderId)
    for (const child of fs) {
      if (child.parent_id !== folderId) continue
      if (slots.some(s => sidebarRows.folderOf(s) === child.id)) return true
      if (descendantMatch(fs, child.id, slots, visited)) return true
    }
    return false
  }

  // Render a folder block scoped to a single column: only slots matching the column predicate.
  // Always render the folder header (even with 0 matches) so users can see + drop into it.
  const renderColumnFolder = (folder: ChatFolder, columnId: string, colSlotKeys: Set<string>, dragHandleProps?: React.HTMLAttributes<HTMLElement>, forceCollapsed?: boolean): React.ReactNode => {
    const childFolders = folders.filter(f => f.parent_id === folder.id).sort(folderCompare)
    const { rows: childSlots, navScope: folderLaneScope, container: folderHoldContainer } = heldLane(sidebarRows.folderTree.rowsIn(folder.id).filter(s => colSlotKeys.has(sessionRowIdentity(s))), columnId, `board:${columnId}:folder:${folder.id}`)
    // A nested folder the person unchecked drops out of the recursion, so neither its
    // header nor anything under it renders. Checking the folder's OWN id is enough:
    // dropping it here takes its descendants with it, the same way the tree's block
    // removal does. Its sessions are already gone from `colSlotKeys`; without this the
    // column would still draw the header of a folder the person asked not to see.
    const deepChildren = childFolders.filter(f => !isFolderFilteredOut(f))
    // Same opt-in as the tree (see the note in renderFolderBlock): only when the
    // setting is on does a column copy holding nothing lose its body, and with it
    // the collapse state it no longer has anything to remember.
    const emptyBody = hideEmptyFolderBody && deepChildren.length === 0 && childSlots.length === 0
    const collapsed = boardFolderCollapsed(columnId, folder)
    // Valid "Move folder to" destinations: everything outside this folder's
    // own subtree (cycle guard). One O(1) lookup, computed once per row.
    const subtreeIds = folderSubtrees.get(folder.id) ?? collectFolderSubtreeIds(folders, folder.id)
    const reparentTargets = folders.filter(f => !subtreeIds.has(f.id))
    // The board lane's answer to a reveal. A column has no folder HEADER row, so the
    // tree's `folderFlash` has nothing to attach to here and a board reveal used to
    // scroll to the column and then sit there unmarked -- the scroll alone is not the
    // confirmation, since the target is often already on screen and nothing moves.
    // Same state, same classes, attached to the box the reveal actually found.
    const boardFolderFlash = revealFlash?.kind === 'folder' && revealFlash.key === folder.id
      ? (revealFlash.fading ? 'fade' : 'flash')
      : null
    const count = childSlots.length + deepChildren.filter(cf => {
      const cfSlots = sidebarRows.folderTree.rowsIn(cf.id).filter(s => colSlotKeys.has(sessionRowIdentity(s)))
      return cfSlots.length > 0 || descendantMatch(
        folders,
        cf.id,
        laneSlots.filter(s => colSlotKeys.has(sessionRowIdentity(s))),
      )
    }).length
    // Board-view folders become sortable only when a drag handle is supplied
    // (root folders wrapped in SortableColumnFolder). Subfolders render without
    // one, so a board subfolder is not directly draggable at all — neither
    // reordered nor re-parented. That is NOT parity with the list view, which
    // has always given a nested row a drag (re-parent before #10428, reorder as
    // well after it); giving the board lane the same gesture needs a handle this
    // path does not pass down, so it stays a separate piece of work. Disabled
    // while renaming in THIS column (rename is per-column via editScope) so
    // the inline input stays usable.
    const draggable = !!dragHandleProps && !(editingId === folder.id && editScope === columnId)
    return (
      // Two drop mechanisms coexist on this block, one per drag SOURCE:
      //  • Native HTML5 onDrop (below) — SESSION cards drag natively (they set
      //    dataTransfer text/plain), so a session dropped here is assigned to
      //    this folder via assignToFolder.
      //  • dnd-kit DndDroppable (this wrapper) — FOLDERS drag via the pointer
      //    sensor (SortableColumnFolder), never via native DnD, so their active
      //    data lives in active.data.current, unreadable by onDrop. The
      //    folder-drop droppable is what lets handleSidebarDragEnd re-parent a
      //    folder dropped here (moveFolderTo). The two never collide: a native
      //    drag never fires dnd-kit's onDragEnd and a dnd-kit drag never fires
      //    the DOM drop event. Id is column-scoped because a root folder renders
      //    once per board column and dnd-kit droppable ids must be unique.
      <DndDroppable key={`col-${columnId}-folder-drop-${folder.id}`} id={`col-${columnId}-folder-drop:${folder.id}`} data={{ type: 'folder-drop', folderId: folder.id }}>
        {({ setNodeRef, isOver }) => (
      // The drag handlers below make this a mouse-only drop target with no
      // keyboard analogue, so scope-disable the static-interaction rule.
      // eslint-disable-next-line jsx-a11y/no-static-element-interactions
      <div ref={setNodeRef}
        data-testid={`col-${columnId}-folder-${folder.id}`}
        data-folder-drop={folder.id}
        // `folder-col` is the board lane's counterpart to the tree's `folder-row`,
        // and it exists for the flash: the reveal outline in index.css is declared
        // COMPOUND (`.folder-row.session-reveal-flash`), so adding the flash class to
        // a box carrying neither row class attaches a class that no rule matches and
        // paints nothing. Naming this box gives the same declaration something to
        // key on here.
        className={`folder-col rounded-md transition-all mb-0.5${isOver ? ' ring-1 ring-accent' : ''}${boardFolderFlash ? ` session-reveal-flash${boardFolderFlash === 'fade' ? ' session-reveal-flash-fade' : ''}` : ''}`}
        onDragOver={e => { e.preventDefault(); e.stopPropagation(); e.currentTarget.classList.add('ring-1', 'ring-accent') }}
        onDragLeave={e => { e.stopPropagation(); e.currentTarget.classList.remove('ring-1', 'ring-accent') }}
        onDrop={e => {
          e.preventDefault(); e.stopPropagation()
          e.currentTarget.classList.remove('ring-1', 'ring-accent')
          const k = e.dataTransfer.getData('text/plain')
          if (k) moveByDrag(k, folder.id)
        }}
      >
        {/* Same rule as the tree row: a column copy with no body has nothing to
         *  toggle, so it is not a control - no button role, no tab stop, no
         *  pointer cursor, no expanded state and no handler. It stays draggable,
         *  because reordering a folder is an action an empty folder can honour. */}
        <div
          className={`group relative flex items-center gap-2 pr-2 py-1 rounded-md ${draggable ? 'cursor-grab active:cursor-grabbing' : emptyBody ? 'cursor-default' : 'cursor-pointer'} text-[12px] text-muted transition-all${emptyBody ? '' : ' hover:text-text hover:bg-bg-hover'}`}
          style={{ paddingLeft: '6px' }}
          {...(draggable ? dragHandleProps : {})}
          // The collapse props go AFTER the drag spread, and the order is
          // load-bearing: `dragHandleProps` are dnd-kit's sortable listeners and
          // they carry the keyboard sensor's own `onKeyDown`, so a later drag
          // spread would replace the collapse handler and Enter would start a
          // drag instead of toggling the body.
          {...(emptyBody
            // No body to disclose, but the row is still a DRAG HANDLE while it is
            // draggable: `useSortable` here hands down only `listeners`, never its
            // `attributes`, so this hand-written `tabIndex` is the only thing that
            // lets the keyboard sensor reach the row - drop it and an empty folder
            // can be reordered with a mouse but not with a keyboard. So it keeps a
            // tab stop and says what it is, and drops only the collapse-specific
            // props (`aria-expanded`, the expand/collapse label, both handlers).
            ? (draggable ? {
              role: 'button',
              tabIndex: 0,
              'aria-label': i18nT('pages.chatSidebar.folder_2', { name: folder.name }),
              'aria-roledescription': i18nT('pages.chatSidebar.drag_to_reorder'),
            } : {})
            : {
              role: 'button',
              tabIndex: 0,
              'aria-expanded': !collapsed,
              'aria-label': collapsed ? i18nT('pages.chatSidebar.expand_folder_name', { name: folder.name }) : i18nT('pages.chatSidebar.collapse_folder_name', { name: folder.name }),
              onClick: () => toggleColumnCollapse(columnId, folder),
              // `e.target === e.currentTarget` restricts the Space/Enter toggle to
              // the row itself. Without it the row swallows every Space typed in a
              // focused DESCENDANT - the inline rename input below - because
              // preventDefault() drops the character and the folder collapses
              // instead. Same guard as Clickable and UpdateModal.
              onKeyDown: (e: React.KeyboardEvent) => { if (e.target === e.currentTarget && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); toggleColumnCollapse(columnId, folder) } },
            })}
        >
          {/* Dimmer and hover-inert on an empty row - same rule as the tree. */}
          {/* Always open on an inert row - same reason as the tree. */}
          <FolderGlyph color={folder.color} icon={folder.icon} size={11} open={!collapsed || emptyBody}
            className={emptyBody ? 'shrink-0 text-muted/40 transition-colors' : undefined} />
          {editingId === folder.id && editScope === columnId ? (
            /* Inline rename input — board-view parity with renderFolderHeader.
             *  Without this branch the ⋯-menu "Rename" set editingId but no
             *  field ever appeared, so rename silently did nothing here. The
             *  collapse handler is on the OUTER div, so the input's onClick +
             *  onMouseDown stopPropagation are load-bearing (they keep clicking
             *  the field from bubbling to toggleColumnCollapse). Keys are
             *  handled the other way round — the row's onKeyDown ignores events
             *  whose target is not the row — so Space types a space here rather
             *  than collapsing the folder. */
            <Input ref={folderEditInputRef} className="flex-1 py-0.5 text-[12px] min-w-0" value={editName} onChange={e => setEditName(e.target.value)} onClick={e => e.stopPropagation()} onMouseDown={e => e.stopPropagation()} {...ime.bindEnter<HTMLInputElement>({ onEnter: () => renameCommit(folder.id, editName), onEscape: () => setEditingId(null), onBlur: () => renameCommit(folder.id, editName) })} />
          ) : (
            // Double-click rename is a mouse-only power shortcut; the accessible
            // path is the ⋯-menu Rename item, so scope-disable the interaction rule.
            // eslint-disable-next-line jsx-a11y/no-static-element-interactions
            <span className="flex-1 truncate" title={i18nT('pages.chatSidebar.double_click_to_rename')} onDoubleClick={e => { e.stopPropagation(); setEditingId(folder.id); setEditScope(columnId); setEditName(folder.name) }}>{folder.name}</span>
          )}
          <span className="text-[10px] text-muted shrink-0">{count}</span>
          {/* List-view parity: an empty folder's row keeps its action cluster
            *  visible (see the note in renderFolderHeader). */}
          {!(editingId === folder.id && editScope === columnId) && (
          <span className={`${emptyBody ? '' : 'opacity-0 '}group-hover:opacity-100 [@media(hover:none)]:opacity-100 group-focus-within:opacity-100 focus-within:opacity-100 has-[[data-state=open]]:opacity-100 transition-opacity flex items-center gap-0.5`}>
            {/* ⋯ menu + a primary "new chat in folder" action, mirroring the
             *  list-view folder header (renderFolderHeader) so board view has
             *  the same one-click way to start a session inside a folder. */}
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <button type="button" data-testid={`col-${columnId}-folder-${folder.id}-menu`} className="text-muted hover:text-text bg-transparent border-none cursor-pointer p-[2px]" title={i18nT('pages.chatSidebar.more')} aria-label={i18nT('pages.chatSidebar.folder_options_for', { name: folder.name })} aria-haspopup="menu" onMouseDown={e => { e.stopPropagation() }} onClick={e => { e.stopPropagation() }} onKeyDown={e => { e.stopPropagation() }}>
                  <MoreVertical size={11} />
                </button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="start" className="min-w-[180px]" onClick={e => e.stopPropagation()} onCloseAutoFocus={onMenuCloseAutoFocus}>
                <DropdownMenuItem onClick={() => { suppressMenuRestoreRef.current = true; setEditingId(folder.id); setEditScope(columnId); setEditName(folder.name) }}><Pencil size={13} /> {i18nT('pages.chatSidebar.rename')}</DropdownMenuItem>
                <DropdownMenuItem data-testid={`col-${columnId}-folder-${folder.id}-new-sub`} onClick={() => { setFolderModal({ mode: 'create', parentId: folder.id }) }}><FolderPlus size={13} /> {i18nT('pages.chatSidebar.new_subfolder')}</DropdownMenuItem>
                {(() => {
                  const rows = (
                    <>
                      {/* Menu create entries take NO open-in-tab gesture (#10575,
                       *  scoped out): a menu closes on select, and Radix keyboard
                       *  activation synthesizes a modifier-free click, so the
                       *  gesture would be mouse-only and undiscoverable. */}
                      <DropdownMenuItem data-testid={`col-${columnId}-folder-${folder.id}-new-incognito`} onClick={() => { createChatInFolder(folder.id, { columnId, memoryMode: 'incognito' }) }}><EyeOff size={13} className="text-warn" /> {i18nT('components.welcomeView.incognito')}</DropdownMenuItem>
                      <DropdownMenuItem data-testid={`col-${columnId}-folder-${folder.id}-new-temporary`} onClick={() => { createChatInFolder(folder.id, { columnId, memoryMode: 'temporary' }) }}><VenetianMask size={13} className="text-aim" /> {i18nT('components.welcomeView.temporary')}</DropdownMenuItem>
                    </>
                  )
                  // A flyout has nowhere to open at phone width, so inline the rows
                  // under a caption there instead (parity with the + New menu).
                  if (isMobile) {
                    return (
                      <>
                        <DropdownMenuLabel className="text-[11px] uppercase tracking-[.04em] flex items-center gap-2"><Ghost size={13} className="text-muted" /> {i18nT('pages.chatSidebar.new_ephemeral_chat')}</DropdownMenuLabel>
                        {rows}
                      </>
                    )
                  }
                  return (
                    <DropdownMenuSub>
                      <DropdownMenuSubTrigger data-testid={`col-${columnId}-folder-${folder.id}-new-ephemeral`}>
                        <Ghost size={13} className="text-muted" /> {i18nT('pages.chatSidebar.new_ephemeral_chat')}
                        <ChevronRight size={13} className="ml-auto text-muted" />
                      </DropdownMenuSubTrigger>
                      <DropdownMenuSubContent>{rows}</DropdownMenuSubContent>
                    </DropdownMenuSub>
                  )
                })()}
                {/* Re-parent: board-view parity with the list-view folder menu. */}
                <FolderMoveSubmenu variant="dropdown" label={i18nT('pages.chatSidebar.move_folder_to')} sortMode={folderSortMode}
                  folders={reparentTargets}
                  currentFolderId={folder.parent_id || null}
                  onPick={pid => moveFolderTo(folder.id, pid)} />
                <DropdownMenuItem data-testid={`col-${columnId}-folder-${folder.id}-settings`} onClick={() => { setFolderModal({ mode: 'edit', folderId: folder.id }) }}><Settings size={13} /> {i18nT('components.folderConfigModal.folder_settings')}</DropdownMenuItem>
                <DropdownMenuSeparator />
                <DropdownMenuItem className="text-danger focus:text-danger" onClick={() => { if (confirm(i18nT('pages.chatSidebar.delete_folder_confirm', { name: folder.name }))) deleteFolderMutation.mutate(folder.id) }}><X size={13} /> {i18nT('pages.chatSidebar.delete_folder')}</DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
            {/* Same three-gesture contract as the header New button; the
             *  existing stopPropagation stays so the header click/drag
             *  handlers never see the press. */}
            <button type="button" data-testid={`col-${columnId}-folder-${folder.id}-new-chat`} className="text-muted hover:text-accent bg-transparent border-none cursor-pointer p-[2px]" title={i18nT('pages.chatSidebar.new_chat_in_name', { name: folder.name })} aria-label={i18nT('pages.chatSidebar.new_chat_in_name', { name: folder.name })}
              onClick={e => { e.stopPropagation(); createChatInFolder(folder.id, { columnId, inNewTab: !!onOpenSlotInNewTab && isOpenInTabModifierClick(e) }) }}
              onMouseDown={e => { e.stopPropagation(); if (e.button === 1 && onOpenSlotInNewTab) e.preventDefault() }}
              onAuxClick={onOpenSlotInNewTab ? (e => {
                if (e.button !== 1) return
                e.preventDefault()
                e.stopPropagation()
                createChatInFolder(folder.id, { columnId, inNewTab: true })
              }) : undefined}
              onKeyDown={e => { e.stopPropagation() }}>
              <MessageSquarePlus size={11} />
            </button>
          </span>
          )}
        </div>
        {renderFolderCreateError(folder.id, columnId)}
        {!emptyBody && (
        <FolderBody padding={FOLDER_BODY_OPEN_PADDING} open={!collapsed && !forceCollapsed}>
          {/* `BOARD_FOLDER_BODY_CLS`: the same row pads as the list-view body
           *  (`FOLDER_ROW_PAD_CLS`), with a body pad sized to this header, so in
           *  both views a folder's rows land on that folder's name column. */}
          <div className={BOARD_FOLDER_BODY_CLS}>
            {(deepChildren.length > 0 || childSlots.length > 0) && (
              <FolderRail name={folder.name} id={`${columnId}-${folder.id}`} onToggle={() => toggleColumnCollapse(columnId, folder)} />
            )}
            {/* Default: the empty-folder affordance stays exactly as it was, in
             *  list-view parity (see renderFolderBlock). Reached only when the
             *  setting is OFF - with it on there is no body to put this in. */}
            {deepChildren.length === 0 && childSlots.length === 0 && (
              <button key={`col-${columnId}-newchat-${folder.id}`} type="button" data-testid={`col-${columnId}-folder-${folder.id}-empty-new-chat`} data-folder-new-chat=""
                // Same three-gesture contract as the folder header's "+".
                onMouseDownCapture={onOpenSlotInNewTab ? (e => { if (e.button === 1) e.preventDefault() }) : undefined}
                onAuxClick={onOpenSlotInNewTab ? (e => {
                  if (e.button !== 1) return
                  e.preventDefault()
                  createChatInFolder(folder.id, { columnId, inNewTab: true })
                }) : undefined}
                onClick={e => createChatInFolder(folder.id, { columnId, inNewTab: !!onOpenSlotInNewTab && isOpenInTabModifierClick(e) })}
                title={i18nT('pages.chatSidebar.new_chat_in_name', { name: folder.name })} aria-label={i18nT('pages.chatSidebar.new_chat_in_name', { name: folder.name })}
                className="w-full flex items-center gap-2.5 px-4 py-2 rounded-md text-[11px] text-muted hover:text-accent hover:bg-bg-hover transition-all bg-transparent border-none cursor-pointer text-left">
                <span>{i18nT('pages.chatSidebar.new_chat_in_name', { name: folder.name })}</span><MessageSquarePlus size={11} className="shrink-0 ml-auto" />
              </button>
            )}
            {deepChildren.map(cf => renderColumnFolder(cf, columnId, colSlotKeys))}
            {childSlots.map((s, i) => {
              const isActive = isActiveRow(s)
              const nextIsActive = isActiveRow(childSlots[i + 1])
              const showDivider = i < childSlots.length - 1 && !isActive && !nextIsActive
                && !startsAutomaticSection(childSlots, i + 1)
              // `scope` stays per-folder so the Framer layoutId and the inline
              // rename target remain unique, but the arrow rove is scoped to the
              // COLUMN: a board column's foldered and ungrouped rows are one
              // visible list, so ArrowDown has to cross the folder boundary.
              return (
                <Fragment key={sessionRowIdentity(s)}>
                  {startsAutomaticSection(childSlots, i) && <PinnedSessionDivider />}
                  {renderSessionRow(s, 1, showDivider, `${folderLaneScope}:${folder.id}`, folderLaneScope, folderHoldContainer)}
                </Fragment>
              )
            })}
          </div>
        </FolderBody>
        )}
      </div>
        )}
      </DndDroppable>
    )
  }

  const {
    startsAutomaticSection, reorderPinnedByKeyboard,
  } = usePinnedKeyboardReorder({ searchRanked, pinned, pinnedOrder, slotFolders, reorderPinned })

  // A row whose session belongs to another page (a crew member's own DM thread,
  // admitted to the conductor lane as a creator anchor) opens where it lives. Read
  // through a ref so the row actions stay ONE stable object across route changes.
  const navigateRef = useRef(navigate)
  navigateRef.current = navigate
  const openElsewhere = useCallback((path: string) => { navigateRef.current(path) }, [])
  // What every session row can do, built once: the row's memo compares this one
  // reference, so none of these may take a new identity per render.
  const rowActions = useMemo(() => ({
    renameInputRef, onRenameStart, onRenameChange, onRenameCommit, onRenameCancel,
    onDuplicate: sessionActions.duplicate, onCloseSession: treeClose,
    onMenuCloseAutoFocus, onSelectSlot, onOpenSlotInNewTab, onOpenSource,
    onOpenPeerSession: openPeerSession,
    onNativeDragStart: startBoardCardDrag, onNativeDragEnd: endNativeSessionDrag,
    onPinnedKeyboardReorder: reorderPinnedByKeyboard,
    openElsewhere, toggleConductor: toggleConductorExpanded,
  }), [
    renameInputRef, onRenameStart, onRenameChange, onRenameCommit, onRenameCancel,
    sessionActions.duplicate, treeClose, onMenuCloseAutoFocus, onSelectSlot, onOpenSlotInNewTab,
    onOpenSource, openPeerSession, startBoardCardDrag, endNativeSessionDrag, reorderPinnedByKeyboard,
    openElsewhere, toggleConductorExpanded,
  ])
  // The values every row shows the same way, ONE object per change of any of them:
  // each view holds it by reference, so it re-renders every row exactly when one of
  // these moves. The drag members are reduced to primitives first, so a drag-over
  // that rebuilds `activeDrag` with the same session leaves the rows alone.
  const dragInFlight = !!activeDrag
  const activeDraggedKey = activeDrag?.type === 'session' ? activeDrag.id : null
  const activeDraggedPinnedIndex = activeDraggedKey !== null ? (pinnedRank.get(activeDraggedKey) ?? -1) : -1
  const rowShell: RowShell = useMemo(() => ({
    connected, mode, defaultAgent, installedAgents, tagById, paletteColors, boost, boostFor, colorMode, recentTintCount,
    // A session row on any touch screen (an iPad too) gets the single ⋯ menu,
    // not the overlay cluster.
    isMobile: isMobile || isTouchDevice,
    dragInFlight, activeDraggedKey, activeDraggedPinnedIndex,
  }), [
    connected, mode, defaultAgent, installedAgents, tagById, paletteColors, boost, boostFor, colorMode, recentTintCount,
    isMobile, isTouchDevice, dragInFlight, activeDraggedKey, activeDraggedPinnedIndex,
  ])
  // What this render shows rows with. The row model reads the local-only members
  // for a local row alone.
  const rowScene: RowScene = {
    activeSlot, crewWindow, shell: rowShell,
    local: {
      recentRank, subagentCounts, subagentApprovalCounts, poppedOut,
      digits: digitModifierHeld ? shortcutDigitByKey : null,
      renaming: { key: renamingSlot, scope: renameScope, value: renameValue },
      revealFlash, nativeDragKey: nativeSessionDrag,
    },
    drag: activeDrag,
  }
  const rowViews = sidebarRows.views(rowScene)
  // The active-row highlight. `activeSlot` names a LOCAL session and a peer row can
  // carry a byte-identical key, so the row model lights a peer row only for the crew
  // window open on its own (crew, key) pair, and a local row only while no crew
  // window is open; five lanes ask it (list folders, fresh children, flat, board
  // root, board columns), each also of the NEXT row to decide dividers.
  const isActiveRow = rowViews.isActive

  // scope namespaces the Framer layoutId per render location. A multi-tag slot
  // can render in several columns at once; same layoutId in one LayoutGroup
  // collides (Framer paints one, hides the rest). Distinct scope = distinct id.
  // Paint-order stamp threaded through every row this render — see
  // RowPlacement.orderStamp for why the memo boundary needs it.
  let sessionRowOrderStamp = 0
  const renderSessionRow = (s: Slot, _indent: number, showDivider: boolean, scope = 'list', navScope = scope, holdContainer = navScope, conductor?: ConductorRowView) => {
    // Clamped, not raw: rows past the window share a stamp and bail out of a
    // displacement above them (see SIDEBAR_DISPLACEMENT_WINDOW).
    const orderStamp = Math.min(sessionRowOrderStamp++, SIDEBAR_DISPLACEMENT_WINDOW)
    const view = rowViews.view(s, {
      scope, navScope, holdContainer, showDivider, orderStamp, conductor,
      // staticRows (the compositor drawer) folds into the one row-animation
      // gate: projection under a WAAPI-driven ancestor mis-attributes the
      // panel's motion to the rows, so the drawer disables row animation
      // wholesale. Outside it, enroll only the first two-viewport paint
      // window: every later row shares the clamped stamp and snaps, keeping
      // Framer's projection registry bounded at every total list size.
      rowAnimEnabled: rowAnimEnabled && orderStamp < SIDEBAR_DISPLACEMENT_WINDOW && !staticRows,
    })
    // Windowed: far from the viewport the row renders as a cheap stub (see
    // pages/chat/sessionRowWindow). Each window root (the lane, or one board
    // column) mounts its own first rows at once so the initial paint shows real
    // rows; the row model's `windowOf` names the rows that never stub.
    const { title, keepMounted } = rowViews.windowOf(s)
    return (
      <WindowedSessionRow key={view.identity} rowId={view.identity} slotKey={s.key} navScope={navScope} holdContainer={holdContainer}
        title={title} keepMounted={keepMounted}>
        <SessionRow view={view} actions={rowActions} />
      </WindowedSessionRow>
    )
  }

  // The per-machine group chrome each list lane draws: `Local` above the lane's
  // own rows, the crew groups below them. Both are null with no crew group.
  const localGroupHeader = shownCrewGroups.length > 0 ? <LocalGroupHeader /> : null
  const renderCrewGroups = (): React.ReactNode => shownCrewGroups.map(g => (
    <CrewGroupSection key={g.id} group={g} rows={shownCrewRows.get(g.id) ?? []}
      collapsed={collapsedCrews.has(g.id)} onToggle={toggleCrewCollapsed} hideWhenEmpty={listNarrowed}
      chevron={<DisclosureChevron open={!collapsedCrews.has(g.id)} size={11} />}
      renderRows={(rows, scope) => rows.map((s, i) => renderSessionRow(
        s, 0, i < rows.length - 1 && !isActiveRow(s) && !isActiveRow(rows[i + 1]), scope,
      ))} />
  ))

  // ── Folder row: matches session-row width (full width minus drawer padding) ──
  // Recursively check if a folder or any descendant contains an unread slot.
  const folderTreeHasUnread = (folderId: string, visited = new Set<string>()): boolean => {
    if (visited.has(folderId)) return false
    visited.add(folderId)
    for (const k of unreadSet) { if (slotFolders[k] === folderId) return true }
    return folders.some(f => f.parent_id === folderId && folderTreeHasUnread(f.id, visited))
  }

  // Inline failure notice for a folder-scoped create, rendered directly under
  // the folder's header row through the shared ErrorNotice surface (AUTOSDE
  // errors-use-error-notice): it carries the role="alert", the design tokens,
  // the dismiss affordance, and the agent hand-off. askAgent is on because the
  // hand-off destroys nothing here — the sidebar holds no unsaved draft (the
  // rename Input commits on blur) and survives the navigation. `columnId`
  // scopes board-view rendering to the column the create was issued from, so
  // a root folder repeated across columns announces ONE alert, under a
  // column-unique test id.
  // True when the tree lane will NOT render the folder's header (and so its
  // per-folder notice mount): the folder or an ancestor is hidden or filtered
  // out, and not currently revealed via the "N hidden folders" peek (whose
  // container key is the parent id, or 'root' at top level). Mirrors the
  // exclusion applied at visibleRootFolders / renderFolderBlock's child
  // filter, so the tree-lane fallback below renders exactly when the scoped
  // mount cannot.
  const folderCreateMountAbsent = (folderId: string): boolean => {
    let cur = folders.find(f => f.id === folderId)
    // A folder that no longer exists (deleted while its create was in flight)
    // has no header anywhere by definition — the strongest mount-absent case.
    if (!cur) return true
    const seen = new Set<string>()
    while (cur) {
      if (seen.has(cur.id)) break
      seen.add(cur.id)
      const revealed = revealedContainers.has(cur.parent_id || 'root')
      if ((isFolderHidden(cur) && !revealed) || isFolderFilteredOut(cur)) return true
      cur = cur.parent_id ? folders.find(f => f.id === cur!.parent_id) : undefined
    }
    return false
  }

  const renderFolderCreateError = (folderId: string, columnId?: string): React.ReactNode => {
    if (!folderCreateError || folderCreateError.folderId !== folderId) return null
    // Ownership: an exact columnId match wins (board columns each render the
    // folder, so scoping prevents N duplicate alerts). Outside board view the
    // single tree mount owns EVERY error for its folder — including one whose
    // columnId outlived its column or its view (user switched back to tree).
    const owns = folderCreateError.columnId === columnId || (columnId === undefined && !boardLaneActive)
    if (!owns) return null
    return (
      <div className="px-2 py-1">
        {/* inline variant with flex-wrap: the sidebar drawer is ~250px wide,
         *  and both stock single-row layouts squeeze the message to a sliver
         *  beside the Ask-agent / dismiss controls. Wrapping lets the message
         *  take the full line and the controls fold under it. */}
        <ErrorNotice
          message={folderCreateError.message}
          title={folderCreateError.title}
          report={folderCreateError.report}
          variant="inline"
          askAgent
          onDismiss={() => setFolderCreateError(null)}
          testId={columnId ? `col-${columnId}-folder-create-error-${folderId}` : `folder-create-error-${folderId}`}
          className="flex-wrap w-full"
        />
        {/* Direct remedy for the stale-directory case: open Folder settings
         *  right here instead of describing a hover-only menu glyph. On its
         *  own line, never as a row peer of the notice's Ask-agent/dismiss
         *  pair (the two-buttons-per-row cap) — same pattern as the
         *  PullRequestPanel remedy link. */}
        {folderCreateError.offerSettings && (
          <div className="mt-0.5">
            <button type="button"
              className="text-[11px] font-medium text-danger/80 hover:text-danger bg-transparent border-none p-0 cursor-pointer underline decoration-danger/30 hover:decoration-danger underline-offset-2"
              data-testid={`folder-create-error-settings-${folderId}`}
              onClick={() => { setFolderModal({ mode: 'edit', folderId }); setFolderCreateError(null) }}>
              {i18nT('components.folderConfigModal.folder_settings')}
            </button>
          </div>
        )}
      </div>
    )
  }

  /**
   * While the search box narrows the list, does this folder's subtree still put
   * ANYTHING on screen? Answered without rendering, because the render cannot
   * answer it: a nested block's `[]` is returned from inside the subfolder
   * wrapper's own render, which runs after `childNodes.push` has already committed
   * the wrapper — so `childNodes.length > 0` reads true for a subtree that draws
   * nothing, and both the drop gate and the header count believed it.
   *
   * That is not cosmetic. Searching "archive" kept `Sydney Property` on screen
   * wearing the count `1` — the `1` being a subfolder that did not render —
   * directly above the note saying no sessions matched. The row named nothing the
   * query asked for and the number contradicted the sentence beneath it.
   *
   * Each clause mirrors one thing `renderFolderBlock` actually draws, so the
   * predicate cannot drift from the render: own surviving sessions, the query
   * having named this folder (`folderNameMatchIds` already covers a matched
   * folder's whole subtree), the create-failure notice this folder owns, the
   * "N hidden folders" peek row filed in this container, and recursively any
   * child folder the tree is willing to draw. `visited` is the same cycle guard
   * `renderFolderBlock` carries, for the same reason: `parent_id` comes off disk.
   */
  const narrowedSubtreeShowsSomething = (folder: ChatFolder, visited = new Set<string>()): boolean => {
    if (visited.has(folder.id)) return false
    visited.add(folder.id)
    if (sidebarRows.folderTree.rowsIn(folder.id).length > 0) return true
    if (folderNameMatchIds?.has(folder.id)) return true
    if (folderCreateError?.folderId === folder.id) return true
    if (hiddenByContainer.get(folder.id)?.length) return true
    return folders.some(f => f.parent_id === folder.id
      && !isFolderHidden(f) && !isFolderFilteredOut(f)
      && narrowedSubtreeShowsSomething(f, visited))
  }

  /**
   * The child folders this container will draw, in the order it draws them — the
   * narrow's verdict included. Sorted, not raw array order: a subfolder's `order`
   * is set by a drag AND by chat_folder_move's before/after, and the cache order
   * reflects neither.
   */
  const drawableChildFolders = (folder: ChatFolder): ChatFolder[] =>
    folders.filter(f => f.parent_id === folder.id
      && !isFolderHidden(f) && !isFolderFilteredOut(f)
      && (!listNarrowed || narrowedSubtreeShowsSomething(f)))
      .sort(folderCompare)

  // The folder's action items, rendered once for BOTH surfaces that open them:
  // the row's ⋯ button (a DropdownMenu) and a right-click on the row (a Radix
  // ContextMenu, which positions itself at the pointer). Radix menu items only
  // work inside their own primitive family, so the family is picked from
  // `variant` -- the same shape SessionActionsMenu and FolderMoveSubmenu use.
  // `data-testid`s carry a `-ctx` suffix in the context variant so a test can
  // tell the two copies apart when both are mounted for one folder.
  const renderFolderMenuItems = (folder: ChatFolder, reparentTargets: readonly ChatFolder[], variant: 'dropdown' | 'context') => {
    const ctx = variant === 'context'
    const Item = ctx ? ContextMenuItem : DropdownMenuItem
    const Separator = ctx ? ContextMenuSeparator : DropdownMenuSeparator
    const Sub = ctx ? ContextMenuSub : DropdownMenuSub
    const SubTrigger = ctx ? ContextMenuSubTrigger : DropdownMenuSubTrigger
    const SubContent = ctx ? ContextMenuSubContent : DropdownMenuSubContent
    const tid = (name: string) => `folder-${name}-${folder.id}${ctx ? '-ctx' : ''}`
    const ephemeralRows = (
      <>
        {/* Menu create entries take NO open-in-tab gesture (#10575,
         *  scoped out): a menu closes on select, and Radix keyboard
         *  activation synthesizes a modifier-free click, so the
         *  gesture would be mouse-only and undiscoverable. */}
        <Item data-testid={tid('new-incognito')} onClick={() => { createChatInFolder(folder.id, { memoryMode: 'incognito' }) }}><EyeOff size={13} className="text-warn" /> {i18nT('components.welcomeView.incognito')}</Item>
        <Item data-testid={tid('new-temporary')} onClick={() => { createChatInFolder(folder.id, { memoryMode: 'temporary' }) }}><VenetianMask size={13} className="text-aim" /> {i18nT('components.welcomeView.temporary')}</Item>
      </>
    )
    return (
      <>
        <Item data-testid={tid('rename')} onClick={() => { suppressMenuRestoreRef.current = true; setEditingId(folder.id); setEditScope('list'); setEditName(folder.name) }}><Pencil size={13} /> {i18nT('pages.chatSidebar.rename')}</Item>
        <Item data-testid={tid('new-subfolder')} onClick={() => { setFolderModal({ mode: 'create', parentId: folder.id }) }}><FolderPlus size={13} /> {i18nT('pages.chatSidebar.new_subfolder')}</Item>
        {/* A flyout has nowhere to open at phone width, so inline the rows
         *  under a caption there instead (parity with the + New menu). The
         *  context family has no Label primitive, so the caption is a plain
         *  div styled like DropdownMenuLabel. */}
        {isMobile ? (
          <>
            {ctx
              ? <div className="px-3 py-1.5 text-[11px] font-semibold text-muted uppercase tracking-[.04em] flex items-center gap-2"><Ghost size={13} className="text-muted" /> {i18nT('pages.chatSidebar.new_ephemeral_chat')}</div>
              : <DropdownMenuLabel className="text-[11px] uppercase tracking-[.04em] flex items-center gap-2"><Ghost size={13} className="text-muted" /> {i18nT('pages.chatSidebar.new_ephemeral_chat')}</DropdownMenuLabel>}
            {ephemeralRows}
          </>
        ) : (
          <Sub>
            <SubTrigger data-testid={tid('new-ephemeral')}>
              <Ghost size={13} className="text-muted" /> {i18nT('pages.chatSidebar.new_ephemeral_chat')}
              <ChevronRight size={13} className="ml-auto text-muted" />
            </SubTrigger>
            <SubContent>{ephemeralRows}</SubContent>
          </Sub>
        )}
        {/* Re-parent: move this folder under another folder or back to the
         *  top level. Self + descendants are excluded (cycle guard). */}
        <FolderMoveSubmenu variant={variant} label={i18nT('pages.chatSidebar.move_folder_to')} sortMode={folderSortMode}
          folders={reparentTargets}
          currentFolderId={folder.parent_id || null}
          onPick={pid => moveFolderTo(folder.id, pid)} />
        <Item data-testid={tid('settings')} onClick={() => { setFolderModal({ mode: 'edit', folderId: folder.id }) }}><Settings size={13} /> {i18nT('components.folderConfigModal.folder_settings')}</Item>
        {/* Hide this folder from the session lists (flat lane + tree).
         *  Same state the filter menu's checkboxes drive, reached from the
         *  folder itself — which is where the user is looking when they
         *  decide a folder is noise. Distinct from "Hide when empty"
         *  below, which is a server-persisted archive affordance. */}
        <Item data-testid={tid('visibility')} onClick={() => { toggleFolderFilter(folder.id) }}>
          {filterHiddenFolders.has(folder.id)
            ? <><Eye size={13} /> {i18nT('pages.chatSidebar.show_folder')}</>
            : <><EyeOff size={13} /> {i18nT('pages.chatSidebar.hide_folder')}</>}
        </Item>
        {folderOffersHide(folder, foldersWithActiveSubtree) && (
          <Item data-testid={tid('hide')} onClick={() => { updateFolderMutation.mutate({ id: folder.id, body: { hidden: true } }) }}><EyeOff size={13} /> {i18nT('pages.chatSidebar.hide_when_empty')}</Item>
        )}
        <Separator />
        <Item className="text-danger focus:text-danger" data-testid={tid('delete')} onClick={() => { if (confirm(i18nT('pages.chatSidebar.delete_folder_confirm', { name: folder.name }))) deleteFolderMutation.mutate(folder.id) }}><X size={13} /> {i18nT('pages.chatSidebar.delete_folder')}</Item>
      </>
    )
  }

  const renderFolderHeader = (folder: ChatFolder, dragHandleProps?: React.HTMLAttributes<HTMLElement>, emptyBody = false, depth = 0) => {
    // Same predicate `renderFolderBlock` renders by, so the number describes what
    // the row can actually show. Counting a hidden-when-empty child made the count
    // and the body disagree: the body skipped it, so no body rendered, while the
    // count still said 1 - and the row then presented as a toggle with nothing to
    // toggle. A folder the user hid is a folder they asked not to see, so it is
    // not part of what this row holds.
    //
    // `drawableChildFolders` carries the narrow's verdict for the same reason: with
    // a search active, a child whose whole subtree draws nothing is not part of
    // what this row holds either, and counting it printed a number the body below
    // could not account for.
    const childFolders = drawableChildFolders(folder)
    // The row model's folder index: a peer row is never in a folder, so a peer key
    // colliding with a local one cannot count a session this machine does not own
    // toward the folder it does.
    const childSlots = sidebarRows.folderTree.rowsIn(folder.id)
    const count = childSlots.length + childFolders.length
    const collapsed = !!folder.collapsed
    // One derivation, not two: `renderFolderBlock` decides the body from the nodes
    // it renders, and this row follows that decision. With the count now built
    // from the same predicate, `count === 0` agrees with it by construction rather
    // than by a guard that had to pick which way to fail.
    const emptyRow = emptyBody
    // `button` when the row toggles something, a plain `span` when it does not.
    const HeaderShell = (emptyRow ? 'span' : 'button') as 'button'
    const hasUnread = folderTreeHasUnread(folder.id)
    const draggable = !!dragHandleProps && editingId !== folder.id
    // Valid "Move folder to" destinations: everything outside this folder's
    // own subtree (cycle guard). One O(1) lookup, computed once per row.
    const subtreeIds = folderSubtrees.get(folder.id) ?? collectFolderSubtreeIds(folders, folder.id)
    const reparentTargets = folders.filter(f => !subtreeIds.has(f.id))
    // Reveal confirmation for THIS folder. The `kind` check is what keeps a
    // session reveal from lighting up a folder whose id equals that slot key.
    const folderFlash = revealFlash?.kind === 'folder' && revealFlash.key === folder.id
      ? (revealFlash.fading ? 'fade' : 'flash')
      : null
    return (
      // Right-click (or long-press) anywhere on the row opens the SAME menu the
      // ⋯ button does, positioned at the pointer. The ⋯ button stays: it is the
      // keyboard-reachable path, and the only one on a device with no secondary
      // button. Rename mode opts out so a right-click in the name input keeps the
      // browser's own edit menu (cut/paste).
      <ContextMenu key={`folder-header-${folder.id}`}>
        <ContextMenuTrigger asChild disabled={editingId === folder.id && editScope === 'list'}>
      <div
        // The reveal target for this folder (command palette Folders tab), and the
        // only marker that identifies a folder ROW. Deliberately not the existing
        // `data-folder-drop`: that one is a drop zone and is rendered once per
        // BOARD COLUMN as well as here, so `querySelector` would return whichever
        // copy sorts first in the DOM — the same ambiguity the session reveal
        // avoids by targeting `data-session-row` instead of `data-slot-key`.
        data-folder-row={folder.id}
        // Non-interactive container (role="group"): the row holds a collapse
        // toggle button + action buttons, so it must NOT itself be a button —
        // an interactive element can't legally contain other interactive
        // elements (invalid ARIA), and a folder row is a grouping, not an action.
        role="group"
        aria-label={i18nT('pages.chatSidebar.folder_2', { name: folder.name })}
        // The whole header is the drag-to-reorder handle (pointer listeners only,
        // no role override). 8px activation distance keeps the collapse toggle
        // and action buttons clickable; drag is off while renaming.
        {...(draggable ? dragHandleProps : {})}
        // `pl-[3px]` (3px), with no inline left-pad override. Deliberately LESS than
        // the session rows' pad, so the glyph outdents into the row gutter while the
        // folder name lands on its sessions' text. Historically this equalled the
        // row pad so a nested folder read as a peer of the sessions filed beside it
        // rather than sitting a couple of px to their
        // left. The pad is therefore NOT free: #3903 raised it to 18px to open a
        // gutter for an absolutely-positioned unread dot, which broke guide 3. That
        // dot is back inline on the right, where it does not compete for the pad.
        //
        // With H = this header's box left, D = `FOLDER_BODY_INSET_PX` 2 — the
        // nested body's own left inset, applied by `FolderBody` so its collapse
        // animation does not clip. It is invisible in the class list, which is
        // exactly why four revisions derived this geometry from Tailwind classes
        // and each landed 2px out. It is now a named, exported constant that the
        // alignment test imports and asserts against the rendered padding, so it
        // is no longer a free empirical term.
        // P = this `pl-[3px]` 3,
        // G = glyph 12, g = `gap-[4px]`, M = body `ml-1` 4, B = 1px border,
        // p = body `pl-[3px]` 3, R = root-lane row `pl-2.5` 10, R_in = in-folder
        // row pad 9 (`FOLDER_BODY_CLS` overrides the row's pad inside a body):
        //
        //   GUIDE 1  connector line runs under the glyph     P <= D + M < P + G
        //   GUIDE 2  name == agent / title / tool-call sub   P + G + g = D+M+B+p+R_in
        //   GUIDE 3  glyph hangs left of sibling content     R_sib - P > 0
        //
        //   3 <= 6 < 15      3 + 12 + 4 = 2 + 4 + 1 + 3 + 9   root 10-3 = 7, nested 9-3 = 6
        //
        // Each nesting level costs D + M + B + p = 10px (19 before). The glyph used
        // to sit ON the sibling content column (P = R), which pinned the per-level
        // cost at glyph + gap and wasted the width session titles need; it now
        // outdents into the row gutter like a tree view, and the NAME carries the
        // alignment instead.
        //
        // All three hold at EVERY depth and in the root lane: the algebra has no
        // per-depth term, so depth 3 nests exactly as depth 2 does.
        //
        // Measured on the built SPA (x in CSS px), NOT derived — a paper estimate
        // of these same numbers was 3px out: root glyph 248, root-lane session
        // content 255; depth 1 glyph 258, name and all three text lines 264;
        // depth 2 name/content 274; depth 3 content 284.
        //
        // Four revisions have broken these guides by computing from class names
        // without D: #1211 (changed 9/17/7 at once), #3766 (status gutter in flow,
        // +18px to the content column), #3903 (name 1px past content, nested glyph
        // 2px short), and a `px-2` attempt during this fix. Re-measure with
        // `website/scripts/capture-folder-glyph.mjs` under MEASURE=1 — never
        // re-derive on paper.
        // A row with no body does not light up on hover. The highlight is this
        // sidebar's "this row is pressable" signal, and a row that toggles nothing
        // wearing the same one is the whole reason the previous round's dead click
        // read as broken. Its cluster is already visible at rest, so hover has
        // nothing left to reveal here either.
        // Pinned to the top of the lane while any of its folder is on screen: the
        // header is `sticky` inside its own folder block (the drop container that
        // holds header + body), so it rides the top edge until the block's end
        // pushes it off, and the next folder's header takes over. A nested header
        // pins one row lower per depth so the whole ancestor path stays readable,
        // and a shallower header paints above a deeper one as it is pushed out.
        // The opaque surface and the row height live in index.css
        // (`.folder-row-sticky`); `sticky` also serves as the containing block
        // the old `relative` provided for the absolutely-positioned children.
        style={{ top: `calc(var(--folder-row-sticky-h) * ${depth} - var(--folder-row-sticky-inset))`, zIndex: FOLDER_ROW_STICKY_Z - depth }}
        className={`folder-row folder-row-sticky group sticky flex items-center gap-2 pl-[3px] pr-2.5 py-1.5 rounded-md text-sm text-muted transition-all${emptyRow ? '' : ' hover:text-text hover:bg-bg-hover'} ${draggable ? 'cursor-grab active:cursor-grabbing' : ''}${folderFlash ? ` session-reveal-flash${folderFlash === 'fade' ? ' session-reveal-flash-fade' : ''}` : ''}`}>
        {editingId === folder.id && editScope === 'list' ? (
          <>
            <FolderGlyph color={folder.color} icon={folder.icon} size={12} open={!collapsed} />
            <Input ref={folderEditInputRef} className="flex-1 py-0.5 text-[13px] min-w-0" value={editName} onChange={e => setEditName(e.target.value)} onClick={e => e.stopPropagation()} onMouseDown={e => e.stopPropagation()} {...ime.bindEnter<HTMLInputElement>({ onEnter: () => renameCommit(folder.id, editName), onEscape: () => setEditingId(null), onBlur: () => renameCommit(folder.id, editName) })} />
            <span className="text-[11px] text-muted tabular-nums shrink-0">{count}</span>
          </>
        ) : (
          <>
            {/* The collapse toggle is the real interactive control — a native
             *  <button> (keyboard-operable for free), filling the row so clicking
             *  the folder glyph/name still toggles.  Double-click the name renames. */}
            {/* A folder with no body has nothing to toggle, so on an empty row this
             *  is not a control at all: no button role, no tab stop, no pointer
             *  cursor, no expanded state to announce and no handler. Keeping the
             *  <button> and neutering its handler is the worst of the options - a
             *  focusable control that looks clickable and does nothing. The name
             *  still double-click renames and the row's own cluster still creates
             *  and opens the menu, so the row keeps every action it can honour. */}
            <HeaderShell
              className={`flex items-center gap-[4px] flex-1 min-w-0 bg-transparent border-none text-left text-inherit p-0${emptyRow ? '' : ' cursor-pointer'}`}
              {...(emptyRow
                // Nothing to disclose, but the row is still a DRAG HANDLE while it
                // is draggable, and this shell is the only focusable thing inside
                // it: the sortable `listeners` sit on the row div, which has no tab
                // stop of its own, so keyboard activation reaches them by bubbling
                // from here. Swapping the <button> for a <span> without this took
                // keyboard reordering away from empty folders in the list view -
                // the same hole the board row had, through a different door.
                ? (draggable ? {
                  role: 'button',
                  tabIndex: 0,
                  'aria-label': i18nT('pages.chatSidebar.folder_2', { name: folder.name }),
                  'aria-roledescription': i18nT('pages.chatSidebar.drag_to_reorder'),
                } : {})
                : {
                type: 'button' as const,
                'aria-expanded': !collapsed,
                'aria-label': collapsed ? i18nT('pages.chatSidebar.expand_folder_name', { name: folder.name }) : i18nT('pages.chatSidebar.collapse_folder_name', { name: folder.name }),
                onClick: () => toggleCollapse(folder.id),
              })}>
              {/* An inert row's glyph says "inactive" by WEIGHT, not by shape. The
               *  closed shape is this product's "collapsed, click to expand"
               *  affordance, so drawing it on a row that toggles nothing invites
               *  exactly the dead click it was meant to prevent; the open shape
               *  invites no click, and "contents shown below" is not a lie when
               *  there are none. So the shape stays open and the glyph instead goes
               *  dimmer and stops brightening on hover, which every pressable
               *  sibling does. Same icon, same box: the alignment guides that key
               *  off this glyph's geometry are untouched. */}
              {/* `|| emptyRow` is the point, not a tidy-up: a folder that was
               *  collapsed BEFORE it emptied still carries `collapsed: true`, and
               *  binding the glyph to that alone would draw the closed shape on an
               *  inert row - this product's "click to expand" affordance on a row
               *  that cannot expand. An inert row is always drawn open. */}
              <FolderGlyph color={folder.color} icon={folder.icon} size={12} open={!collapsed || emptyRow}
                className={emptyRow ? 'shrink-0 text-muted/40 transition-colors' : undefined}
                testId={`folder-collapse-${folder.id}`} />
              {/* Double-click rename is a mouse-only power shortcut; the accessible
               *  path is the ⋯-menu Rename item, so scope-disable the interaction rule. */}
              {/* The matched letters are marked while the search box narrows the list.
               *  Without it a folder row surfaced by a NAME match carries no cue at
               *  all: the row for "Sydney Property" on a search for "archive" (its
               *  subfolder) is indistinguishable from one whose own name matched, so
               *  the lane reads as arbitrary. The launcher already marks its matched
               *  letters, and this is the same signal in the surface the user was
               *  looking at. `highlightText` returns the plain string when the term
               *  is empty or absent, so ancestor and subtree rows — which have
               *  nothing to mark — are untouched, and that difference is itself the
               *  cue: the marked row is the one that explains the result. */}
              {/* eslint-disable-next-line jsx-a11y/no-static-element-interactions */}
              <span className="flex-1 text-[13px] font-medium text-text truncate text-left" title={i18nT('pages.chatSidebar.double_click_to_rename')} onDoubleClick={e => { e.stopPropagation(); setEditingId(folder.id); setEditScope('list'); setEditName(folder.name) }}>{highlightText(folderNameText(folder), slotFilter.trim(), false, -1)}</span>
              {/* Channel-owned folder (created by per-channel session filing):
               *  show the channel's brand mark so the folder reads as "these are
               *  the Discord conversations" at a glance. Guarded the same way the
               *  session rows are — a channel with no brand asset shows nothing
               *  rather than ChannelBrandIcon's generic Link2 fallback, which
               *  means "live mirroring" elsewhere in this sidebar. */}
              {folder.channel && hasChannelBrandIcon(folder.channel) && (
                <span className="shrink-0 opacity-80" aria-hidden><ChannelBrandIcon channel={folder.channel} size={11} /></span>
              )}
              {folder.project_dir && <span className="text-[10px] text-accent/60 shrink-0" title={folder.project_dir}><Link2 size={9} /></span>}
              {/* Unread dot on the RIGHT, inline before the count — a state marker
               *  reading after the text, not a gutter marker. #3903 moved it into an
               *  absolute LEFT gutter, which forced the header's pad to 18px; that
               *  pad is load-bearing for the alignment guides (it must equal the
               *  session row's), so the dot goes back where it does not compete with
               *  it. Only when collapsed: an expanded folder's child rows carry
               *  their own markers. */}
              {hasUnread && collapsed && (
                // Carries the same accessible name as a session row's unread
                // marker, and the SAME i18n key: a colour-only dot is invisible to
                // a screen reader and indistinguishable from decoration, and this
                // one sits beside a count where that reads as styling. The session
                // row's gutter marker has had `role="img"` + a label since #3766;
                // this one had neither.
                // `--ok` for the same reason as the session row's dot: it is the
                // SAME unread state rolled up, so it reads the same semantic
                // status token rather than the brand accent, matching the
                // `recent` filter and the connection-status dot (#10479).
                <span className="w-2 h-2 rounded-full shrink-0" style={{ background: 'var(--ok)' }}
                  role="img"
                  aria-label={i18nT('pages.chatSidebar.agent_finished_your_turn')}
                  title={i18nT('pages.chatSidebar.agent_finished_your_turn')} />
              )}
              <span className="text-[11px] text-muted tabular-nums shrink-0">{count}</span>
            </HeaderShell>
            {folder.default_agent && <span className="text-[10px] text-accent bg-accent/10 px-1.5 py-0.5 rounded-full shrink-0 truncate max-w-[60px]" title={i18nT('pages.chatSidebar.default_agent', { name: folder.default_agent })}>{folder.default_agent}</span>}
          </>
        )}
        {/* An empty folder's row is otherwise a dead end: hiding the body took
          *  away the only control it had, and the closed glyph alone does not say
          *  the row can be opened or created in. So the row's own action cluster
          *  stops hiding on an empty folder — it already holds exactly the two
          *  controls that row needs (create, and the ⋯ menu whose rename/delete
          *  is what an empty folder usually wants), so nothing is ADDED to the
          *  row and the two-buttons-per-row cap is untouched. */}
        {!(editingId === folder.id && editScope === 'list') && (
        <div className={`transition-all flex items-center gap-0.5 rounded-md group-focus-within:opacity-100 focus-within:opacity-100 has-[[data-state=open]]:opacity-100${emptyRow ? ' shrink-0 -my-1' : ' absolute top-1/2 -translate-y-1/2 right-1.5 p-1 bg-card border border-border shadow-sm opacity-0 group-hover:opacity-100 [@media(hover:none)]:opacity-100 [@media(hover:none)]:static [@media(hover:none)]:translate-y-0 [@media(hover:none)]:shrink-0 [@media(hover:none)]:-my-1 [@media(hover:none)]:p-0 [@media(hover:none)]:bg-transparent [@media(hover:none)]:border-transparent [@media(hover:none)]:shadow-none'}`}>
          {/* ⋯ menu first, then the primary "new chat" action.  Sibling
           *  <button>s of the collapse toggle (valid ARIA — no nesting). */}
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <button type="button" className="cursor-pointer p-[4px] rounded text-muted hover:text-text hover:bg-bg-hover transition-all bg-transparent border-none" title={i18nT('pages.chatSidebar.more')} aria-label={i18nT('pages.chatSidebar.folder_options_for', { name: folder.name })} aria-haspopup="menu" data-testid={`folder-menu-${folder.id}`} onMouseDown={e => { e.stopPropagation() }}><MoreVertical size={12} /></button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="start" className="min-w-[180px]" onClick={e => e.stopPropagation()} onCloseAutoFocus={onMenuCloseAutoFocus}>
              {renderFolderMenuItems(folder, reparentTargets, 'dropdown')}
            </DropdownMenuContent>
          </DropdownMenu>
          {/* Same three-gesture contract as the header New button: plain click
           *  creates and switches; Cmd/Ctrl-click and middle-click create the
           *  session as a background TAB. Gated on `onOpenSlotInNewTab` --
           *  embedded hosts have no tab strip, so the modifier is ignored. */}
          <button type="button" data-testid={`folder-new-chat-${folder.id}`} className="cursor-pointer p-[4px] rounded text-muted hover:text-accent hover:bg-bg-hover transition-all bg-transparent border-none" title={i18nT('pages.chatSidebar.new_chat_in_name', { name: folder.name })} aria-label={i18nT('pages.chatSidebar.new_chat_in_name', { name: folder.name })}
            onMouseDownCapture={onOpenSlotInNewTab ? (e => { if (e.button === 1) e.preventDefault() }) : undefined}
            onAuxClick={onOpenSlotInNewTab ? (e => {
              if (e.button !== 1) return
              e.preventDefault()
              e.stopPropagation()
              createChatInFolder(folder.id, { inNewTab: true })
            }) : undefined}
            onClick={e => { e.stopPropagation(); createChatInFolder(folder.id, { inNewTab: !!onOpenSlotInNewTab && isOpenInTabModifierClick(e) }) }}><MessageSquarePlus size={12} /></button>
        </div>
        )}
      </div>
        </ContextMenuTrigger>
        <ContextMenuContent data-testid={`folder-context-menu-${folder.id}`} className="min-w-[180px]" onClick={e => e.stopPropagation()} onCloseAutoFocus={onMenuCloseAutoFocus}>
          {renderFolderMenuItems(folder, reparentTargets, 'context')}
        </ContextMenuContent>
      </ContextMenu>
    )
  }

  // One row announcing the folders this container is hiding, rendered at the
  // BOTTOM of that container's folder list and indented to its depth. Peeking it
  // open renders those folders' real blocks (dimmed), so every normal
  // affordance — including ⋯ → Show folder, the durable undo — still works.
  // `containerKey` is 'root' | 'flat' | parent folder id.
  const renderHiddenReveal = (containerKey: string, hidden: readonly ChatFolder[], depth: number): React.ReactNode => {
    if (hidden.length === 0) return null
    const open = revealedContainers.has(containerKey)
    const n = hidden.length
    return (
      <div key={`hidden-reveal-${containerKey}`} data-testid={`hidden-reveal-${containerKey}`}>
        <button
          type="button"
          onClick={() => toggleReveal(containerKey)}
          aria-expanded={open}
          title={open ? i18nT('pages.chatSidebar.collapse_hidden_folders') : i18nT('pages.chatSidebar.show_hidden_folder', { count: n })}
          data-folder-hidden-reveal=""
          className="w-full flex items-center gap-1.5 py-1 pl-2.5 pr-2 text-left text-[11px] text-muted hover:text-text hover:bg-accent-subtle rounded-md cursor-pointer bg-transparent border-none transition-colors"
        >
          <DisclosureChevron open={open} size={11} />
          <span>{i18nT('pages.chatSidebar.hidden_folder_count', { count: n })}</span>
        </button>
        {open && (
          <div className="opacity-70">
            {hidden.map(f => (
              <Fragment key={`revealed-${f.id}`}>{renderFolderBlock(f, depth)}</Fragment>
            ))}
          </div>
        )}
      </div>
    )
  }

  const renderFolderBlock = (folder: ChatFolder, depth: number, visited = new Set<string>(), dragHandleProps?: React.HTMLAttributes<HTMLElement>, forceCollapsed = false): React.ReactNode[] => {
    if (depth > 10 || visited.has(folder.id)) return []
    visited.add(folder.id)
    const childSlots = sidebarRows.folderTree.rowsIn(folder.id)
    const childNodes: React.ReactNode[] = []
    // Nested subfolders are sortables, exactly as root folders are: dragging one
    // either re-orders it among its siblings (drop on a sibling's edges or body)
    // or re-parents it (drop on the middle band of another folder's header, or on
    // the root lane to move it to the top level). Both gestures are the ones the
    // root lane already has -- see SortableSubfolderBlock for why a nested row
    // could previously only re-parent. The subtree ids ride along in the drag data
    // so collision detection can exclude self/descendants as targets, and the
    // sibling ids so a reorder cannot resolve into another container.
    //
    // `drawableChildFolders`, not a raw parent_id filter: while the list is
    // narrowed a child whose subtree draws nothing must be skipped HERE, before
    // the push. The nested render returns `[]` from inside the wrapper's own
    // render, which runs long after this push, so a wrapper committed now can
    // never be taken back -- and `childNodes.length` is what the drop gate below
    // and the header count both read.
    //
    // One SortableContext per PARENT, holding exactly that parent's drawable
    // children: `order` is a per-container index, so the ring a drag may move
    // within is one container's children and nothing else. It renders no DOM of
    // its own, so the row geometry the alignment guides pin is untouched.
    const childFolderRows = drawableChildFolders(folder)
    if (childFolderRows.length) {
      const siblingIds = childFolderRows.map(f => f.id)
      childNodes.push(
        <SortableContext key={`subfolder-ring-${folder.id}`} items={siblingIds} strategy={verticalListSortingStrategy}>
          {childFolderRows.map(cf => (
            <SortableSubfolderBlock key={`subfolder-drag-${cf.id}`} folder={cf} reorderable={folderReorderable} dragWithheld={folderDragWithheld}
              depth={depth + 1} visited={visited}
              subtree={[...(folderSubtrees.get(cf.id) ?? collectFolderSubtreeIds(folders, cf.id))]}
              siblings={siblingIds}
              disabled={editingId === cf.id}
              renderFolderBlock={renderFolderBlock} />
          ))}
        </SortableContext>
      )
    }
    // Bottom of THIS container's folder list: announce what the filter is
    // hiding here, at this depth. Sits after the sibling folders and before the
    // new-subfolder input, so it reads as part of the folder list.
    const hiddenHere = hiddenByContainer.get(folder.id)
    if (hiddenHere?.length) childNodes.push(renderHiddenReveal(folder.id, hiddenHere, depth + 1))
    const { fresh: freshChildSlotsRaw, stale: staleChildSlots } = splitStale(childSlots)
    // Stale rows are collapsed into their own section and are stale precisely
    // because nothing is bumping them, so only the live list needs the hold.
    const { rows: freshChildSlots, navScope: treeChildScope, container: treeChildContainer } = heldLane(freshChildSlotsRaw, 'list', `tree:folder:${folder.id}`)
    freshChildSlots.forEach((s, i) => {
      const isActive = isActiveRow(s)
      const nextIsActive = isActiveRow(freshChildSlots[i + 1])
      const showDivider = i < freshChildSlots.length - 1 && !isActive && !nextIsActive
        && !startsAutomaticSection(freshChildSlots, i + 1)
      if (startsAutomaticSection(freshChildSlots, i)) {
        childNodes.push(<PinnedSessionDivider key={`pinned-divider-${folder.id}`} />)
      }
      childNodes.push(renderSessionRow(s, depth + 1, showDivider, treeChildScope, treeChildScope, treeChildContainer))
    })
    if (!searchRanked && staleExpanded.has(folder.id)
      && staleChildSlots.length > 0 && freshChildSlots.length > 0
      && pinned.has(freshChildSlots[freshChildSlots.length - 1].key)) {
      childNodes.push(<PinnedSessionDivider key={`pinned-divider-stale-${folder.id}`} />)
    }
    const staleSection = renderStaleSection(folder.id, staleChildSlots, depth + 1, folder.name)
    if (staleSection) childNodes.push(staleSection)
    // Closed sessions filed here, behind a collapsed row at the foot of the body.
    // Built apart from `childNodes` on purpose: every emptiness test
    // below (the narrowed-empty drop, the opt-in empty-body hide, the header's
    // count) is about what is OPEN in the folder, and must keep reading that.
    // Not while the list is narrowed: a search or filter answers "which open
    // rows match", which this unfiltered list does not.
    const closedCount = folder.closed_count ?? 0
    const closedNode = !listNarrowed && closedCount > 0 ? (
      <FolderClosedSection key={`closed-${folder.id}`} folderId={folder.id} folderName={folder.name}
        count={closedCount} open={closedOpen.has(folder.id)}
        onToggle={() => setClosedOpen(prev => {
          const next = new Set(prev)
          if (next.has(folder.id)) next.delete(folder.id); else next.add(folder.id)
          return next
        })}
        connected={connected}
        onResume={({ key, title }) => { dispatch(resumeFromHistory({ key, title })) }}
        onOpenOlderSessions={openHistoryPane}
        renderChevron={open => <DisclosureChevron open={open} size={11} />} />
    ) : null
    // Hide folders with no matching children while the list is narrowed —
    // unless this folder owns the active create-failure notice: a create fired
    // from the folder-picker menu can target a folder the narrow is hiding,
    // and eliding it would make the failure exactly as silent as before #8229.
    //
    // Nor when the folder ITSELF is what the query named. An empty folder whose
    // name matches is still the answer to "where is that folder" — dropping it
    // would mean the one search guaranteed to name it is also the one search that
    // cannot show it. `folderNameMatchIds` covers the matched folder's subtree, so
    // a matched parent keeps its empty children too: they are part of what the
    // query asked to see.
    if (listNarrowed && childNodes.length === 0
      && folderCreateError?.folderId !== folder.id
      && !folderNameMatchIds?.has(folder.id)) return []
    // Wrap children in a bordered container so the folder's extent is visually
    // clear when multiple folders are open. Only wrap when there's content,
    // otherwise the FolderBody would render an empty 1px-tall strip with a line.
    // Opt-in (Settings > Chat > "Hide the body of an empty folder"): a folder with
    // nothing in it renders NO body - not a collapsed one, not an empty one - so
    // it costs one row instead of two and a tree of area folders stops spending
    // most of the sidebar's height on rows holding nothing. Dropping the body
    // rather than collapsing it is why there is no per-folder expansion state:
    // nothing is hidden, so nothing needs re-reaching.
    // A folder holding only closed sessions keeps a body for their row, or the
    // row would be unreachable in exactly the case it exists for.
    const emptyBody = hideEmptyFolderBody && childNodes.length === 0 && !closedNode
    const wrapped = childNodes.length > 0 || (hideEmptyFolderBody && closedNode) ? (
      <div key={`folder-children-${folder.id}`} className={FOLDER_BODY_CLS}>
        <FolderRail name={folder.name} id={folder.id} onToggle={() => toggleCollapse(folder.id)} />
        {childNodes}
        {closedNode}
      </div>
    ) : emptyBody || listNarrowed ? null : (
      // Default: the empty-folder affordance stays exactly as it was. A newly
      // created (or emptied) expanded folder would otherwise render nothing,
      // leaving the hover-only create control on the header as the only
      // (invisible-at-rest) way to start a session in it.
      <div key={`folder-children-${folder.id}`} className={FOLDER_BODY_CLS}>
        <button key={`folder-newchat-${folder.id}`} type="button" data-testid={`folder-empty-new-chat-${folder.id}`} data-folder-new-chat=""
          // Same three-gesture contract as the folder header's "+" above.
          onMouseDownCapture={onOpenSlotInNewTab ? (e => { if (e.button === 1) e.preventDefault() }) : undefined}
          onAuxClick={onOpenSlotInNewTab ? (e => {
            if (e.button !== 1) return
            e.preventDefault()
            createChatInFolder(folder.id, { inNewTab: true })
          }) : undefined}
          onClick={e => createChatInFolder(folder.id, { inNewTab: !!onOpenSlotInNewTab && isOpenInTabModifierClick(e) })}
          title={i18nT('pages.chatSidebar.new_chat_in_name', { name: folder.name })} aria-label={i18nT('pages.chatSidebar.new_chat_in_name', { name: folder.name })}
          className="w-full flex items-center gap-2.5 pl-2.5 pr-3 py-2 rounded-md text-[12px] text-muted hover:text-accent hover:bg-bg-hover transition-all bg-transparent border-none cursor-pointer text-left">
          <span>{i18nT('pages.chatSidebar.new_chat_in_name', { name: folder.name })}</span><MessageSquarePlus size={13} className="shrink-0 ml-auto" />
        </button>
        {closedNode}
      </div>
    )
    // Outer container wraps header + body so the entire folder block is a
    // single drag-drop target. Dropping anywhere inside (header, children,
    // empty space) assigns the dragged session to this folder.
    // Uses a dragEnter counter instead of contains() checks — nested child
    // folders fire enter/leave pairs that balance to zero when the drag
    // moves into a subfolder, so the parent highlight clears correctly.
    return [
      <DndDroppable key={`folder-drop-${folder.id}`} id={`folder-drop:${folder.id}`} data={{ type: 'folder-drop', folderId: folder.id }}>
        {({ setNodeRef, isOver }) => (
          // `--folder-pin-stack` is how far the pinned headers above this block's
          // rows reach down the lane (this header plus every ancestor's), so a
          // row that keyboard roving or a reveal scrolls into view lands below
          // them instead of behind them (`scroll-margin-top` in index.css).
          <div ref={setNodeRef} data-folder-drop={folder.id} style={{ '--folder-pin-stack': `calc(var(--folder-row-sticky-h) * ${depth + 1})` } as React.CSSProperties} className={`rounded-md transition-all mb-0.5${isOver ? ' ring-1 ring-accent' : ''}`}>
            {renderFolderHeader(folder, dragHandleProps, emptyBody, depth)}
            {renderFolderCreateError(folder.id)}
            {wrapped && <FolderBody key={`folder-body-${folder.id}`} padding={FOLDER_BODY_OPEN_PADDING} open={!folder.collapsed && !forceCollapsed}>{wrapped}</FolderBody>}
          </div>
        )}
      </DndDroppable>,
    ]
  }

  const {
    rootFolders, visibleRootFolders, rootFolderIds,
  } = useRootFolderLanes({ folders, folderCompare, isFolderHidden, isFolderFilteredOut })
  // True while actively dragging a session that currently lives in a folder.
  // Used to reveal the empty-state drop placeholder inside the "No folder"
  // group so there's always a reachable ungroup target.
  const draggingFolderedSession = activeDrag?.type === 'session' && !!slotFolders[activeDrag.id]
  // The board's reading of the same question, from the native mirror: a card
  // drags with HTML5 DnD there, so `activeDrag` never carries it. Reveals the
  // unfile strip at the foot of every column.
  const boardDraggingFiledSession = nativeSessionDrag !== null && !!slotFolders[nativeSessionDrag]
  // WHY the session being dragged may not be referenced into the open chat, or
  // null when it may be. Carries the reason rather than a boolean because the two
  // refusals read differently to the user (a privacy guard vs a self-drop no-op).
  // Drives the drop zone's refusal state; the drop handler re-decides with the
  // same function.
  const draggingRefRefusal = activeDrag?.type === 'session'
    ? sessionRefBlockReason({
      key: activeDrag.id,
      activeSlot,
      memoryMode: localSlots.find(x => x.key === activeDrag.id)?.memory_mode,
    })
    : null
  // True while dragging a folder that currently has a parent — the only case
  // where "drop on the root lane to move to top level" applies.
  const draggingNestedFolder = activeDrag?.type === 'folder' && !!folders.find(f => f.id === activeDrag.id)?.parent_id

  // Droppable rects are normally snapshotted once at drag-start, but these
  // lanes ANIMATE during drags (the dragged folder's body collapses over 150ms;
  // hovered collapsed folders auto-expand; the chat-pane zone mounts mid-drag),
  // so the snapshot goes stale and drop targets diverge from the cursor. While a
  // drag is live, poll re-measurement (dnd-kit's numeric `frequency`
  // self-reschedules a measure loop) so rects track the animating layout. Idle
  // sessions keep the plain strategy — no background measuring.
  const dndMeasuring = activeDrag
    ? { droppable: { strategy: MeasuringStrategy.Always, frequency: 100 } }
    : { droppable: { strategy: MeasuringStrategy.Always } }
  /** The follow-the-cursor preview for whatever is being dragged. */
  const dragGhost = activeDrag
    ? activeDrag.type === 'folder'
      ? <FolderDragGhost folder={folders.find(x => x.id === activeDrag.id)} />
      : <SessionDragGhost slot={localSlots.find(x => x.key === activeDrag.id)} fallbackLabel={activeDrag.id} />
    : null
  /**
   * The drag preview is PORTALED to `document.body`.
   *
   * dnd-kit positions the overlay `fixed`, which normally escapes ancestor
   * overflow — but the sidebar rides inside OverlayDrawer's morph `clip-path`,
   * and a clip-path clips every descendant including fixed ones. Rendered in
   * place, the ghost therefore vanished the instant the cursor crossed out of
   * the sidebar and into the chat pane, i.e. for the whole second half of the
   * one gesture that aims there. Portaling keeps it visible until release; it
   * stays inside the DndContext because React portals preserve context.
   */
  const dragOverlay = createPortal(
    <DragOverlay dropAnimation={null}>{dragGhost}</DragOverlay>,
    document.body,
  )

  // Narrow-sidebar header responsiveness: below ~256px the full "New chat"
  // label no longer fits next to the label + kebab, so collapse the create
  // button to icon-only; below ~200px also drop the "Sessions" label.
  const compactHeader = paintedSidebarWidth < 256
  const tinyHeader = paintedSidebarWidth < 200

  return (
    // stable theming hook 'sidebar' — see website/docs/theming-contract.md
    <div ref={sidebarRootRef} onPointerOver={onRootPointerOver} onPointerLeave={releaseHoverPin} className={`${LIST_SHELL_CLS} flex flex-col shrink-0 relative h-full`} style={rootStyle}>
      {/* Drag handle — the shared column grip (components/ResizeHandle), so
          this edge looks and behaves exactly like the Crew Members roster's and
          the app workspaces'. Positioned absolutely on the card's right border
          (the default is an in-flow flex sibling); `inset` is the card's
          rounded-xl radius so the accent bar spans exactly the straight
          segment of the border. `sidebar-resize-handle` stays as the hook the
          mobile overlay and the split-pane host use to hide it. */}
      <ResizeHandle
        handleProps={sidebarResize}
        label={i18nT('pages.chatSidebar.resize_sidebar')}
        onNudge={nudgeSidebar}
        value={paintedSidebarWidth}
        min={SIDEBAR_MIN}
        max={sidebarMax}
        inset={12}
        // z-40: above the floating search dock (ListDock, z-30). Once a filter
        // chip or a notice mounts, the dock's opaque shelf spans the card's full
        // width, and at z-10 it took the inner half of the grip's 6px strip.
        className="sidebar-resize-handle absolute top-0 -right-[3px] h-full z-40"
      />

      {/* Header — all elements ("Sessions" title, kebab, New button) centered
          on one line 23px from the panel top (1px card border + mt-0.5, then
          centered in a 40px row) — the shared control baseline: the nav rail
          header, chat title row, and activity strip icons center on the same
          line.
          px-2 is symmetric so the New button ends 9px from the card's right
          edge (8 + 1px border) — the same as its 9px gap to the top edge
          (1px border + mt-0.5 + 6px of the h-10 row around the h-7 button). */}
      <div className={LIST_HEADER_CLS}>
        <div className={`flex items-center gap-1.5 min-w-0 flex-1 ${collapsible && !isMobile ? 'pl-9' : 'pl-1.5'}`}>
          {!tinyHeader && <span className={LIST_TITLE_CLS}>{i18nT('pages.chatSidebar.sessions')}</span>}
        </div>
        <div className="flex items-center gap-1.5 shrink-0">
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <button className="mc-touch-hit w-7 h-7 rounded-md border border-border bg-transparent text-muted cursor-pointer flex items-center justify-center hover:border-border-strong hover:text-text transition-all" title={i18nT('pages.chatSidebar.more_options')} aria-label={i18nT('pages.chatSidebar.more_options')}><MoreVertical size={14} /></button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="min-w-[180px]">
              {dashboardPreview && <DropdownMenuItem onSelect={() => navigate('/session-dashboards')}>
                <Monitor size={14} className="text-muted" />
                {i18nT('commandCenter.all_title')}
              </DropdownMenuItem>}
              <DropdownMenuItem disabled={seedStateLanesMutation.isPending} onClick={() => {
                if (seedStateLanesMutation.isPending) return
                const isActive = tagColumnsEnabled && rawColumns.length > 0
                const next = !isActive
                const cfg = loadChatConfig()
                // The toggle persists `tagColumnsEnabled` through saveChatConfig,
                // which returns false and rolls back when the write (or its dirty
                // marker) cannot be stored. Surface that failure and STOP before
                // clearing the error, seeding lanes or restoring widths — none of
                // which should happen for a preference that was not saved (GPT 6.1
                // F1, errors-use-error-notice).
                if (!saveChatConfig({ ...cfg, tagColumnsEnabled: next })) {
                  setToggleSaveFailed(true)
                  setSeedError(i18nT('pages.chatSidebar.failed_to_save_board_toggle'))
                  return
                }
                setToggleSaveFailed(false)
                setSeedError('')
                if (!next) {
                  // Leaving board view: give back the width the user chose before
                  // the lanes were auto-widened, rather than stranding a ~900px
                  // sidebar in list view.
                  restorePreBoardWidth()
                }
                // Seed when the board has no lanes and nothing configured worth
                // keeping. Seeding is additive and idempotent, so a repeat click
                // cannot duplicate lanes; the pending guard above only stops a
                // second request racing the first before the cache refreshes.
                if (next && !rawColumns.some(c => c.source === 'state' || c.name || (c.tag_ids || []).length || c.include_untagged)) {
                  seedStateLanesMutation.mutate()
                }
              }}>
                <Columns3 size={14} className={tagColumnsEnabled && rawColumns.length > 0 ? 'text-accent' : 'text-muted'} />
                {tagColumnsEnabled && rawColumns.length > 0 ? i18nT('pages.chatSidebar.switch_to_list_view') : i18nT('pages.chatSidebar.switch_to_board_view')}
              </DropdownMenuItem>
              {tagColumnsEnabled && rawColumns.length > 0 && missingLanes.length > 0 && (
                <DropdownMenuItem
                  data-testid="add-state-lanes"
                  disabled={seedStateLanesMutation.isPending}
                  onClick={() => { if (!seedStateLanesMutation.isPending) seedStateLanesMutation.mutate() }}
                >
                  <Columns3 size={14} className="text-muted" />
                  {i18nT('pages.chatSidebar.add_state_lanes')}
                </DropdownMenuItem>
              )}
              <DropdownMenuItem onClick={() => { setCleanupOpen(!cleanupOpen); setCleanupExpanded(false); setCleanupError('') }}>
                <BrushCleaning size={14} className="text-muted" />
                {i18nT('pages.chatSidebar.clean_up_sessions')}
              </DropdownMenuItem>
              <DropdownMenuItem data-testid="clean-up-empty-folders" onClick={() => setFolderCleanupOpen(!folderCleanupOpen)}>
                <FolderX size={14} className="text-muted" />
                {i18nT('pages.chatSidebar.clean_up_empty_folders_menu')}
              </DropdownMenuItem>
              <DropdownMenuItem onClick={() => { setBulkModelOpen(true); setBulkModel(''); setBulkSkipRunning(true); setBulkModelError('') }}>
                <Cpu size={14} className="text-muted" />
                {i18nT('pages.chatSidebar.switch_all_to_model')}
              </DropdownMenuItem>
              <DropdownMenuItem onClick={() => setManageTagsOpen(o => !o)}>
                <TagIcon size={14} className="text-muted" />
                {i18nT('pages.chatSidebar.manage_tags')}
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
          {/* Split create-button: main segment = one-click New chat; caret
           *  opens a menu grouping New folder + New chat in folder (flat
           *  folder flyout). Replaces the old standalone New-folder + New-chat
           *  header buttons. Menu is portaled to <body> so the right-side
           *  folder flyout escapes the sidebar's overflow clip. */}
          <div className="relative flex items-center rounded-md bg-accent text-accent-fg overflow-hidden [@media(pointer:coarse)]:overflow-visible shrink-0" data-create-menu>
            <button
              disabled={creatingSlot}
              className={`mc-touch-hit-y flex items-center h-7 rounded-s-md cursor-pointer bg-transparent border-none text-accent-fg hover:bg-accent-hover active:scale-95 transition-all disabled:opacity-70 disabled:cursor-wait disabled:active:scale-100 ${compactHeader ? 'justify-center w-7' : 'gap-1.5 pl-2 pr-2.5 text-[12px] font-semibold'}`}
              // Same three-gesture contract as a session row: plain click
              // creates and switches; Cmd/Ctrl-click and middle-click create the
              // session as a background TAB and leave the user where they are.
              // Both tab gestures are gated on `onOpenSlotInNewTab` — without a
              // tab strip (embedded hosts) there is nothing to open into, so the
              // modifier is ignored and the click stays an ordinary create.
              // Middle-press autoscroll is cancelled on mousedown, as on rows.
              onMouseDownCapture={onOpenSlotInNewTab ? (e => { if (e.button === 1) e.preventDefault() }) : undefined}
              onAuxClick={onOpenSlotInNewTab ? (e => {
                if (e.button !== 1 || creatingSlot) return
                e.preventDefault()
                createChatMutation.mutate({ inNewTab: true })
              }) : undefined}
              onClick={e => { createChatMutation.mutate({ inNewTab: !!onOpenSlotInNewTab && isOpenInTabModifierClick(e) }) }}
              title={i18nT('pages.chatSidebar.new_chat')}
              aria-label={i18nT('pages.chatSidebar.new_chat_session')}
              aria-busy={creatingSlot}
            >{creatingSlot ? <Loader2 size={15} className="animate-spin" /> : <Plus size={15} />}{!compactHeader && <span className="whitespace-nowrap">{creatingSlot ? i18nT('pages.chatSidebar.creating') : i18nT('pages.chatSidebar.new')}</span>}</button>
            <span className="w-px h-4 bg-accent-fg opacity-30" aria-hidden="true" />
            <DropdownMenu open={newChatMenuOpen} onOpenChange={o => { setNewChatMenuOpen(o); if (!o) setRemoteCrewError('') }}>
              <DropdownMenuTrigger asChild>
                <button
                  className="mc-touch-hit-end flex items-center justify-center w-6 h-7 rounded-e-md cursor-pointer bg-transparent border-none text-accent-fg hover:bg-black/10 active:scale-95 transition-all"
                  title={i18nT('pages.chatSidebar.create')} aria-label={i18nT('pages.chatSidebar.more_create_options')}><ChevronDown size={13} /></button>
              </DropdownMenuTrigger>
              {/* max-w bounds the menu: the mode descriptions below are full
               *  sentences, and without an upper bound a flex item's automatic
               *  min-width lets the longest one stretch the menu across the
               *  session list instead of wrapping. */}
              <DropdownMenuContent align="end" className="min-w-[200px] max-w-[264px]" onCloseAutoFocus={onMenuCloseAutoFocus}>
                {/* The plain chat is what the button's main segment does, but a
                 *  menu that lists every OTHER way to create and omits the
                 *  ordinary one reads as if the other kinds were the only ones
                 *  the caret can make. Listed first so the default stays the
                 *  default. */}
                <DropdownMenuItem disabled={creatingSlot} onClick={() => { createChatMutation.mutate({ inNewTab: false }) }}>
                  <MessageSquarePlus size={14} className="text-muted" /> {i18nT('pages.chatSidebar.new_chat')}
                </DropdownMenuItem>
                {/* Ephemeral session types are grouped one level down: they are two
                 *  spellings of one choice (a session that leaves no lasting memory),
                 *  so listing both at the top level would double the session-type rows
                 *  a user reads before picking an ordinary chat. max-w bounds the
                 *  submenu for the same reason the parent content is bounded — the
                 *  glosses are full sentences and would otherwise stretch it across
                 *  the session list instead of wrapping. */}
                {(() => {
                  const ephemeralRows = (
                    <>
                      <DropdownMenuItem className="items-start" data-testid="new-incognito-chat" disabled={creatingSlot} onClick={() => { createEphemeralChatMutation.mutate('incognito') }}>
                        <EyeOff size={14} className="text-muted mt-[3px] shrink-0" />
                        <span className="flex min-w-0 flex-col gap-px">
                          <span>{i18nT('components.welcomeView.incognito')}</span>
                          <span className="whitespace-normal text-[11px] leading-snug text-muted">{i18nT('components.welcomeView.incognito_desc')}</span>
                        </span>
                      </DropdownMenuItem>
                      <DropdownMenuItem className="items-start" data-testid="new-temporary-chat" disabled={creatingSlot} onClick={() => { createEphemeralChatMutation.mutate('temporary') }}>
                        <VenetianMask size={14} className="text-muted mt-[3px] shrink-0" />
                        <span className="flex min-w-0 flex-col gap-px">
                          <span>{i18nT('components.welcomeView.temporary')}</span>
                          <span className="whitespace-normal text-[11px] leading-snug text-muted">{i18nT('components.welcomeView.temporary_desc')}</span>
                        </span>
                      </DropdownMenuItem>
                    </>
                  )
                  // A flyout has nowhere to open at phone width (Radix pins a
                  // submenu to the trigger's side and only shifts it vertically),
                  // so on a phone the two modes are listed inline under a caption.
                  if (isMobile) {
                    return (
                      <>
                        <DropdownMenuLabel className="text-[11px] uppercase tracking-[.04em] flex items-center gap-2">
                          <Ghost size={13} className="text-muted" /> {i18nT('pages.chatSidebar.new_ephemeral_chat')}
                        </DropdownMenuLabel>
                        {ephemeralRows}
                      </>
                    )
                  }
                  return (
                  <DropdownMenuSub>
                    <DropdownMenuSubTrigger>
                      <Ghost size={14} className="text-muted" /> {i18nT('pages.chatSidebar.new_ephemeral_chat')}
                      <ChevronRight size={13} className="ml-auto text-muted" />
                    </DropdownMenuSubTrigger>
                    <DropdownMenuSubContent className="max-w-[264px]">
                      {ephemeralRows}
                    </DropdownMenuSubContent>
                  </DropdownMenuSub>
                  )
                })()}
                {/* Import creates a session too, so it sits with the create rows
                 *  rather than only in a per-session ⋯ menu that has to be opened
                 *  on some unrelated session first. */}
                <ImportSessionItem Item={DropdownMenuItem} />
                {/* Crew Members is a DOOR, not a create action: it navigates to the
                 *  Members page (or, while that page is preview-gated, to the
                 *  Settings card that turns it on — see `openCrewMembers`). It sits
                 *  among the create entries because this menu is where "crew" was
                 *  offered until Crew Mode retired, so it is where a returning user
                 *  looks. Not disabled by `creatingSlot`: it creates nothing.
                 *
                 *  CAPTURED: the Feature Previews "See what it looks like" dialog
                 *  shows the Members page this entry opens. A visible change to
                 *  that page makes the picture stale — re-shoot with
                 *  `scripts/capture-feature-previews.mjs`.
                 *
                 *  Separators on BOTH sides: every other row here creates something and is
                 *  named "New …"; this one navigates and is not. Without the rule a
                 *  reader parsed it as an unnamed create action on every menu open
                 *  (UX review on #9519). It sits between the session rows and the
                 *  folder rows, in a group of its own. */}
                <DropdownMenuSeparator />
                <DropdownMenuItem className="items-start" data-testid="open-crew-members" onClick={openCrewMembers}>
                  <Users size={14} className="text-muted mt-[3px] shrink-0" />
                  <span className="flex min-w-0 flex-col gap-px">
                    <span>{i18nT('pages.chatSidebar.open_crew_members')}</span>
                    {/* The gloss tells the truth about where the click lands. While
                     *  the page is preview-gated the entry detours to the Settings
                     *  card that turns it on, and a gloss that still promised the
                     *  page read as "offered and hidden at once" (UX review on
                     *  #9519) — so it discloses the detour instead. */}
                    <span className="whitespace-normal text-[11px] leading-snug text-muted">{crewPreview ? i18nT('pages.chatSidebar.open_crew_members_desc') : i18nT('pages.chatSidebar.open_crew_members_gated_desc')}</span>
                  </span>
                </DropdownMenuItem>
                <DropdownMenuSeparator />
                <DropdownMenuItem onClick={() => { setFolderModal({ mode: 'create', parentId: '' }) }}>
                  <FolderPlus size={14} className="text-muted" /> {i18nT('pages.chatSidebar.new_folder')}
                </DropdownMenuItem>
                {folders.length > 0 && (() => {
                  const folderRows = (() => {
                    const roots = folders.filter(f => !f.parent_id).sort(folderCompare)
                    const childrenOf = (pid: string) => folders.filter(f => f.parent_id === pid).sort(folderCompare)
                    const items: { f: ChatFolder; depth: number }[] = []
                    const walk = (list: ChatFolder[], depth: number) => { for (const f of list) { items.push({ f, depth }); walk(childrenOf(f.id), depth + 1) } }
                    walk(roots, 0)
                    return items.map(({ f, depth }) => (
                      <DropdownMenuItem key={f.id} style={{ paddingLeft: `${12 + depth * 16}px` }} onClick={() => createChatInFolder(f.id, { focus: true })}>
                        <Folder size={14} className={depth === 0 ? 'text-muted' : 'text-muted/60'} /> {f.name}
                      </DropdownMenuItem>
                    ))
                  })()
                  // A flyout has nowhere to open at phone width (Radix pins a
                  // submenu to the trigger's side and only shifts it vertically),
                  // so on a phone the folders are listed inline under a caption.
                  if (isMobile) {
                    return (
                      <>
                        <DropdownMenuLabel className="text-[11px] uppercase tracking-[.04em] flex items-center gap-2">
                          <Folder size={13} className="text-muted" /> {i18nT('pages.chatSidebar.new_chat_in_folder')}
                        </DropdownMenuLabel>
                        <div className="max-h-[240px] overflow-y-auto">{folderRows}</div>
                      </>
                    )
                  }
                  return (
                  <DropdownMenuSub>
                    <DropdownMenuSubTrigger className="data-[disabled]:pointer-events-none data-[disabled]:opacity-50">
                      <Folder size={14} className="text-muted" /> {i18nT('pages.chatSidebar.new_chat_in_folder')}
                      <ChevronRight size={13} className="ml-auto text-muted" />
                    </DropdownMenuSubTrigger>
                    {/* Intentional tighter cap composed via min() with the
                        primitive's available-height var: 300px keeps the folder
                        list submenu compact while preserving the viewport
                        never-clip floor (a bare max-h would override the
                        primitive, since cn()'s tailwind-merge dedupes max-h-*).
                        overflow is left to the primitive. */}
                    <DropdownMenuSubContent className="max-h-[min(300px,var(--radix-dropdown-menu-content-available-height))]">
                      {folderRows}
                    </DropdownMenuSubContent>
                  </DropdownMenuSub>
                  )
                })()}
                {/* "New chat on crew" — the same shape as "New chat in folder"
                 *  above (dynamic rows behind one submenu, listed inline at phone
                 *  width where a Radix flyout has nowhere to open), because it
                 *  answers the same kind of question. The row is absent, not
                 *  disabled, when no crew holds a live tunnel: a disabled row
                 *  would advertise a capability the install may never have. It
                 *  sits AFTER the folder rows so the local ways to create keep
                 *  their position.
                 *
                 *  Preview-gated on its OWN flag (`utils/previewFlags.ts`), not
                 *  the Crew Members page's: the landing is what is unfinished, since the
                 *  created session opens in that crew's pane and the local list
                 *  does not yet show live remote sessions. Toggle lives in
                 *  Settings > Remote Crew. */}
                {remoteCrewChatPreview && warmCrews.length > 0 && (() => {
                  // The picked row swaps its icon for a spinner and its label for
                  // "Starting chat on <crew>…": the create opens a session on the peer
                  // before the local one exists, so it takes seconds, and the menu
                  // stays open (onSelect preventDefault) the whole time, so the row
                  // is where the eye already is.
                  const crewRows = warmCrews.map(c => {
                    const pendingHere = createRemoteChatMutation.isPending && createRemoteChatMutation.variables === c.id
                    return (
                      <DropdownMenuItem key={c.id} data-testid={`new-chat-on-crew-${c.id}`}
                        // The picked row is the progress cue, so it is not disabled
                        // (a disabled row is drawn faded); the guard in onSelect is
                        // what refuses a second pick while the create runs.
                        disabled={createRemoteChatMutation.isPending && !pendingHere}
                        aria-busy={pendingHere || undefined}
                        onSelect={e => {
                          e.preventDefault()
                          if (createRemoteChatMutation.isPending) return
                          createRemoteChatMutation.mutate(c.id)
                        }}>
                        {pendingHere
                          ? <Loader2 size={14} className="text-info animate-spin" aria-hidden="true" data-testid={`new-chat-on-crew-spinner-${c.id}`} />
                          : <Server size={14} className="text-info" />}{' '}
                        {pendingHere ? i18nT('pages.chatSidebar.creating_on_crew', { name: c.name }) : c.name}
                      </DropdownMenuItem>
                    )
                  })
                  // Inline failure reason (version mismatch, tunnel down), shown
                  // through the shared ErrorNotice (website AGENTS.md forbids a
                  // hand-written text-danger div for a rejected mutation). Kept in
                  // the menu because the create leaves nothing behind on failure —
                  // closing would erase the only signal; `onSelect preventDefault`
                  // on the rows keeps a failed create from auto-closing over it.
                  // The sibling menu item is the keyboard-reachable hand-off in
                  // both the mobile inline list and the desktop submenu.
                  const errRow = remoteCrewError
                    ? (
                      <>
                        <div className="px-2 py-1.5">
                          <ErrorNotice
                            id={remoteCrewErrorId}
                            message={remoteCrewError}
                            variant="inline"
                            testId="new-chat-on-crew-error"
                          />
                        </div>
                        <ErrorNoticeMenuItem
                          Item={DropdownMenuItem}
                          message={remoteCrewError}
                          describedBy={remoteCrewErrorId}
                        />
                      </>
                    )
                    : null
                  if (isMobile) {
                    return (
                      <>
                        <DropdownMenuLabel className="text-[11px] uppercase tracking-[.04em] flex items-center gap-2">
                          <Server size={13} className="text-info" /> {i18nT('pages.chatSidebar.new_chat_on_crew')}
                        </DropdownMenuLabel>
                        <div className="max-h-[240px] overflow-y-auto">{crewRows}{errRow}</div>
                      </>
                    )
                  }
                  return (
                    <DropdownMenuSub>
                      <DropdownMenuSubTrigger data-testid="new-chat-on-crew" className="data-[disabled]:pointer-events-none data-[disabled]:opacity-50">
                        <Server size={14} className="text-info" /> {i18nT('pages.chatSidebar.new_chat_on_crew')}
                        <ChevronRight size={13} className="ml-auto text-muted" />
                      </DropdownMenuSubTrigger>
                      {/* Intentional tighter cap composed via min() with the
                          primitive's available-height var: 300px keeps the crew
                          list submenu compact while preserving the viewport
                          never-clip floor (a bare max-h would override the
                          primitive, since cn()'s tailwind-merge dedupes max-h-*).
                          overflow is left to the primitive. */}
                      <DropdownMenuSubContent className="max-h-[min(300px,var(--radix-dropdown-menu-content-available-height))]">
                        {crewRows}{errRow}
                      </DropdownMenuSubContent>
                    </DropdownMenuSub>
                  )
                })()}
              </DropdownMenuContent>
            </DropdownMenu>
          </div>
        </div>
      </div>

      {/* Split View (session grid) has no entry here on purpose: this sidebar is a
       *  navigation surface, and the grid's own affordances live next to the
       *  transcript they act on — the chat header's Columns2 button (⌘D) opens it,
       *  and the header's "in split" badge is the way back into a live split. */}

      {/* Clean Up dialog */}
      {folderCleanupOpen && <FolderCleanupPanel folders={folders} onClose={() => setFolderCleanupOpen(false)} />}
      {cleanupOpen && (() => {
        const archivable = cleanupPreview ? cleanupPreview.map(k => localSlots.find(s => s.key === k)).filter(Boolean) as Slot[] : []
        const noStale = cleanupPreview != null && cleanupPreview.length === 0 && !activeIsStale
        return (
          <div className="mx-2 mb-2 p-3 rounded-lg bg-bg border border-border shadow-md text-sm animate-rise">
            <div className="font-medium text-text-strong mb-2"><BrushCleaning size={14} className="lucide-inline" /> {i18nT('pages.chatSidebar.clean_up_sessions_2')}</div>
            <div className="text-muted text-[12px] mb-2">{i18nT('pages.chatSidebar.archive_sessions_with_no_activity_in_the_last')}</div>
            <div className="flex items-center gap-2 mb-3">
              {[1, 3, 7].map(d => (
                <button key={d} className={`px-2.5 py-1 rounded-md text-[12px] border transition-all cursor-pointer ${
                  cleanupDays === d ? 'bg-accent text-accent-fg border-accent' : 'bg-transparent text-muted border-border hover:border-border-strong hover:text-text'
                }`} onClick={() => setCleanupDays(d)}>{i18nT('pages.chatSidebar.day', { count: d })}</button>
              ))}
            </div>
            <div className="text-[12px] text-muted mb-3">
              {cleanupPreviewLoading
                ? i18nT('pages.chatSidebar.checking')
                : cleanupPreviewError
                  ? (
                    // Read failure inside a confirm dialog with no draft: nothing to
                    // lose, so the hand-off is on. Retry stays a separate button
                    // rather than being the error surface itself.
                    <span className="inline-flex items-center gap-2 flex-wrap">
                      <ErrorNotice message={i18nT('pages.chatSidebar.failed_to_load_preview')} variant="inline" askAgent testId="cleanup-preview-error" />
                      <Btn className="text-[12px] px-2 py-0.5" onClick={() => queryClient.invalidateQueries({ queryKey: ['cleanup-preview'] })}>{i18nT('pages.chatSidebar.retry')}</Btn>
                    </span>
                  )
                  : noStale
                    ? i18nT('pages.chatSidebar.no_inactive_sessions_to_archive')
                    : cleanupPreview != null && <>
                      {i18nT('pages.chatSidebar.session', { count: archivable.length })} {i18nT('pages.chatSidebar.will_be_moved_to_older_sessions')}{activeIsStale ? ` ${i18nT('pages.chatSidebar.1_skipped_currently_selected')}` : ''} {i18nT('pages.chatSidebar.pinned_sessions_are_kept')}
                      {archivable.length > 0 && (
                        <button className="ml-1 text-accent hover:underline cursor-pointer bg-transparent border-none p-0 text-[12px]" onClick={() => setCleanupExpanded(!cleanupExpanded)}>
                          {cleanupExpanded ? i18nT('pages.chatSidebar.hide') : i18nT('pages.chatSidebar.show')} {i18nT('pages.chatSidebar.session', { count: archivable.length })} ▸
                        </button>
                      )}
                      {cleanupExpanded && archivable.length > 0 && (
                        <div className="mt-2 max-h-32 overflow-y-auto rounded-md border border-border bg-bg-elevated p-1.5">
                          {archivable.map(s => (
                            <div key={s.key} className="text-[12px] text-muted truncate py-0.5 px-1">
                              {s.title && s.title !== s.key ? s.title : s.key}
                              {slotActivityTs(s) && <span className="ml-1 text-[11px] opacity-60">{fmtRelativeTime(slotActivityTs(s))}</span>}
                            </div>
                          ))}
                        </div>
                      )}
                      </>
              }
            </div>
            {/* Archive inputs are server-side (the days window is a persisted
                pick, not a draft), so nothing is lost by handing off. Its own
                line, above the Cancel/Archive pair: a third control in that row
                would break max-two-buttons-per-row. */}
            <ErrorNotice message={cleanupError} askAgent className="mb-2" testId="cleanup-error" />
            <div className="flex items-center gap-2 justify-end">
              <Btn className="text-[12px] px-3 py-1" onClick={() => setCleanupOpen(false)}>{i18nT('pages.chatSidebar.cancel')}</Btn>
              <Btn className="text-[12px] px-3 py-1 bg-accent text-accent-fg hover:bg-accent-hover" disabled={archivable.length === 0 || cleanupMutation.isPending || cleanupPreviewLoading} onClick={() => {
                setCleanupError('')
                cleanupMutation.mutate()
              }}>{cleanupMutation.isPending ? i18nT('pages.chatSidebar.archiving') : i18nT('pages.chatSidebar.archive_session', { count: archivable.length })}</Btn>
            </div>
          </div>
        )
      })()}

      {/* Switch-all-to-model dialog — mirrors the Clean Up panel. Picking a
       *  model applies it to every live session (each switch resets that
       *  session); running sessions are skipped by default. */}
      {bulkModelOpen && (
        <div className="mx-2 mb-2 p-3 rounded-lg bg-bg border border-border shadow-md text-sm animate-rise">
          <div className="font-medium text-text-strong mb-2"><Cpu size={14} className="lucide-inline" /> {i18nT('pages.chatSidebar.switch_all_sessions')}</div>
          <div className="text-muted text-[12px] mb-2">{i18nT('pages.chatSidebar.pick_a_model_for_every_session_switching_a_sessi')} <span className="text-danger">{i18nT('pages.chatSidebar.resets_its_conversation')}</span>.</div>
          {bulkModelsFailed && (
            <div className="flex flex-wrap items-center gap-2 mb-2">
              {/* No hand-off: the chosen bulkModel/skipRunning selection is unsaved,
                  and the navigation would discard it. Retry stays in the panel.
                  Its own row above the listbox, wrapping the Retry button under
                  the notice when the sidebar is too narrow for both: an inline
                  notice sharing a fixed row collapses to one character per line
                  at sidebar width, and the Cancel/Switch row below is already at
                  the two-button limit. */}
              <ErrorNotice
                className="flex-1 min-w-[12rem]"
                message={i18nT('pages.chatSidebar.model_list_failed')}
                testId="bulk-model-roster-error"
              />
              <Btn
                className="text-[12px] px-3 py-1 shrink-0"
                disabled={bulkModelsQuery.isFetching}
                onClick={() => bulkModelsQuery.refetch()}
              >{i18nT('pages.chatSidebar.retry')}</Btn>
            </div>
          )}
          <div ref={bulkListRef} role="listbox" aria-label={i18nT('pages.chatSidebar.model_list')} tabIndex={-1} onKeyDown={bulkOnListKeyDown} className="max-h-[220px] overflow-y-auto rounded-md border border-border bg-bg-elevated p-1 mb-2 outline-hidden">
            <ModelDropdownList models={bulkModelOptions} activeModel={bulkModelPick} onSelect={setBulkModel} />
          </div>
          {bulkRunningCount > 0 && (
            <label className="flex items-center gap-2 text-[12px] text-muted mb-2 cursor-pointer">
              {/* aria-labelledby, not aria-label: the name is the visible
                  "Skip N running sessions" text, which is two catalog keys plus a
                  live count. Binding it by reference keeps the announced name and
                  the rendered name the same string, so the count cannot drift. */}
              <input type="checkbox" aria-labelledby={bulkSkipRunningLabelId} checked={bulkSkipRunning} onChange={e => setBulkSkipRunning(e.target.checked)} />
              <span id={bulkSkipRunningLabelId}>{i18nT('pages.chatSidebar.skip')} {i18nT('pages.chatSidebar.running_session', { count: bulkRunningCount })}</span>
            </label>
          )}
          {/* No hand-off: the chosen bulkModel/skipRunning selection is unsaved,
              and the navigation would discard it. Its own line, above the
              Cancel/Switch pair: a third control in that row would break
              max-two-buttons-per-row, and an inline notice sharing the row
              collapses to one character per line at sidebar width. */}
          <ErrorNotice message={bulkModelError} className="mb-2" testId="bulk-model-error" />
          <div className="flex items-center gap-2 justify-end">
            <Btn className="text-[12px] px-3 py-1" onClick={() => { setBulkModelOpen(false); setBulkModel(''); setBulkModelError('') }}>{i18nT('pages.chatSidebar.cancel')}</Btn>
            <Btn className="text-[12px] px-3 py-1 bg-accent text-accent-fg hover:bg-accent-hover" disabled={!bulkModelPick || bulkAffectedCount === 0 || bulkModelMutation.isPending} onClick={() => { setBulkModelError(''); bulkModelMutation.mutate({ model: bulkModelPick, skipRunning: bulkSkipRunning }) }}>{bulkModelMutation.isPending ? i18nT('pages.chatSidebar.switching') : i18nT('pages.chatSidebar.switch_session', { count: bulkAffectedCount })}</Btn>
          </div>
        </div>
      )}

      {/* Manage-tags panel — mirrors the Clean Up / Switch All panels. Renders
       *  the shared TagManagerList in 'manage' mode (no column context), so tag
       *  CRUD is reachable in list view too, not only from a board column. */}
      {manageTagsOpen && (
        <div data-testid="manage-tags-panel" className="mx-2 mb-2 p-3 rounded-lg bg-bg border border-border shadow-md text-sm animate-rise">
          <div className="flex items-center justify-between mb-2">
            <div className="font-medium text-text-strong"><TagIcon size={14} className="lucide-inline" /> {i18nT('pages.chatSidebar.manage_tags_2')}</div>
            <button type="button" className="text-muted hover:text-text bg-transparent border-none cursor-pointer p-0 leading-none" onClick={() => setManageTagsOpen(false)} aria-label={i18nT('pages.chatSidebar.close')}><X size={13} /></button>
          </div>
          <div className="text-muted text-[12px] mb-2">{i18nT('pages.chatSidebar.rename_flag_as_status_or_delete_tags_changes_app')}</div>
          <TagManagerList mode="manage" />
        </div>
      )}

      {/* The floating dock (components/ListDock): the glass search capsule, the
          filter chips and the list-level notices hover over the lanes, and
          every lane's scroller pads its top by the dock's live height. */}
      <ListDock field={(
        // Search with inline sort/filter control — the shared list-panel
        // search row (components/SearchFilterBar), also mounted by the Crew
        // Members roster.
      <SearchFilterBar
        placeholder={i18nT('pages.chatSidebar.search_sessions')}
        clearLabel={i18nT('pages.chatSidebar.clear_search')}
        value={slotFilter}
        onChange={setSlotFilter}
        trailingCount={availableLanes.length > 1 ? 2 : 1}
        trailing={(
          <>
            {/* ONE button cycling tree -> conductor -> flat, skipping any lane that
             *  cannot render (see `availableLanes`). Deliberately not a segmented
             *  control: the sidebar's chrome stays Raycast-plain, so the icon shows
             *  the lane you are IN and the copy names where the next press goes.
             *  Hidden entirely when only the tree is available, which is the
             *  pre-existing "no folders, nothing to flatten" case. */}
            {availableLanes.length > 1 && (
            <button
              type="button"
              className={`relative w-6 h-6 rounded flex items-center justify-center cursor-pointer transition-colors border-none ${lane !== 'tree' ? 'text-accent bg-accent-subtle' : 'text-muted hover:text-text hover:bg-bg-hover bg-transparent'}`}
              onClick={cycleLane}
              /* Both strings describe the ACTION and are derived from `nextLane`, not
               * from the lane in view: this is the feature's only entry point, so a
               * label naming the current lane tells every user -- and every screen
               * reader -- that the press goes somewhere it does not.
               *
               * With a board configured the toggle flattens INSIDE each column rather
               * than producing the single flat lane, so the copy must not promise
               * "all chats without folders" (one combined list). */
              title={laneSwitchLabel}
              aria-label={laneSwitchLabel}
              /* NOT `aria-pressed`. This cycles three positions, and a boolean would
               * announce the same "pressed" for conductor and for flat -- two different
               * states told apart by nothing a screen reader hears. The lane in view is
               * named instead, which is the fact a reader actually wants. */
              data-lane={lane}
              data-next-lane={nextLane}
              data-testid="flat-view-toggle"
            >
              {/* Crossfaded rather than hard-swapped. One persistent button showing two
                * different drawings on press reads as two different buttons -- a blind
                * read of this control reported exactly that confusion -- and a short
                * dissolve is what says "the same button changed" instead. */}
              <AnimatePresence mode="wait" initial={false}>
                <motion.span
                  key={conductorView ? 'conductor' : 'list'}
                  initial={{ opacity: 0 }}
                  animate={{ opacity: 1 }}
                  exit={{ opacity: 0 }}
                  transition={{ duration: 0.12 }}
                  className="flex items-center justify-center"
                >
                  {conductorView ? <ListTree size={14} /> : <List size={14} />}
                </motion.span>
              </AnimatePresence>
            </button>
            )}
            <DropdownMenu open={filterSortOpen} onOpenChange={setFilterSortOpen}>
              <DropdownMenuTrigger asChild>
                {/* The funnel holds the way back, so while a hide withholds rows its
                    title says both that something is withheld and how much.

                    Warn, not accent: the view toggle immediately beside it tints accent
                    to mean "this lane is active", so one accent doing both jobs reads as
                    the toggle's own state rather than as a population kept off screen.

                    The count does NOT go in the accessible name. A button's name names
                    the button; a count that changes under the reader belongs in content,
                    and every lane draws it as content — a reveal row where there are
                    folder headers to hang one from, the lane notice in a board. That also
                    keeps the name stable for a reader navigating by control name. Both
                    numbers read `hiddenFolderCount`, so they cannot disagree. */}
                <FilterMenuButton
                  title={hiddenFolderCount > 0
                    ? i18nT('pages.chatSidebar.sort_filter_sessions_hidden', { count: hiddenFolderCount })
                    : i18nT('pages.chatSidebar.sort_filter_sessions')}
                  aria-label={i18nT('pages.chatSidebar.sort_and_filter_sessions')}
                  badge={filterCounts['unread']}
                  className={hiddenFolderCount > 0 ? 'text-warn' : undefined}
                  data-folder-hide-active={hiddenFolderCount > 0 ? String(hiddenFolderCount) : undefined}
                />
              </DropdownMenuTrigger>
              <FilterMenuContent align="end">
                <FilterMenuLabel>{i18nT('pages.chatSidebar.filter')}</FilterMenuLabel>
                {SESSION_FILTERS.map(filterDef => {
                  const active = activeFilters.has(filterDef.key)
                  const slotCount = filterCounts[filterDef.key] ?? 0
                  const isRecent = filterDef.key === 'recent'
                  if (isRecent) {
                    // The window picker is a NESTED FLYOUT on a wide viewport and
                    // renders INLINE on a phone. Radix hardcodes a submenu to
                    // side="right" and only lets its popper shift on the cross
                    // axis, so at phone width neither side fits: the flyout lands
                    // on whichever side overflows less and is cut off by the
                    // viewport (measured at 390px: 249px wide, 192px of it past
                    // the right edge, --radix-popper-available-width: 57px). No
                    // width or padding tuning can recover that — the flyout has
                    // nowhere to go beside a menu that already spans most of the
                    // screen, so on a phone the options come inline instead.
                    const picker = (
                      // Non-menu-item controls: stop click/keydown from reaching
                      // Radix so choosing a window doesn't dismiss the menu
                      // (mirrors the folder-rename input pattern).
                      // eslint-disable-next-line jsx-a11y/no-static-element-interactions -- the three handlers only stopPropagation, so this wrapper has no action of its own for a keyboard to reach; the chips and the number input inside are the real controls and each is separately focusable
                      <div
                        onClick={e => e.stopPropagation()}
                        onMouseDown={e => e.stopPropagation()}
                        onKeyDown={e => e.stopPropagation()}
                      >
                        <div className="px-1 pb-1 text-[11px] text-muted">{i18nT('pages.chatSidebar.within')}</div>
                        <div className="flex flex-wrap gap-1 px-1 mb-2">
                          {RECENT_WINDOW_PRESETS.map(preset => (
                            <DurationChip
                              key={preset.ms}
                              label={preset.label}
                              selected={recentWindowMs === preset.ms}
                              onSelect={() => selectRecentPreset(preset.ms)}
                            />
                          ))}
                        </div>
                        <div className="px-1 text-[12px] text-muted">
                          <div className="mb-1">{i18nT('pages.chatSidebar.custom')}</div>
                          <div className="flex items-center gap-1.5">
                            {/* Draft-string value so the field can be cleared
                                / partially typed; commit + clamp on blur or
                                Enter. Unit changes commit immediately but keep
                                the amount as-typed (no re-derivation flip). */}
                            <input
                              type="number"
                              min={1}
                              max={9999}
                              value={recentAmountDraft}
                              onChange={e => setRecentAmountDraft(e.target.value)}
                              onBlur={commitRecentAmount}
                              onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); commitRecentAmount() } }}
                              aria-label={i18nT('pages.chatSidebar.custom_recency_amount')}
                              className="w-12 shrink-0 px-1.5 py-0.5 rounded border border-border bg-bg-elevated text-text text-[12px]"
                            />
                            <SimpleSelect
                              value={recentUnitDraft}
                              onChange={v => changeRecentUnit(v as RecentUnit)}
                              className="px-1.5 py-0.5 text-[12px] rounded"
                              options={['minutes', 'hours', 'days']}
                              optionLabels={[i18nT('pages.chatSidebar.min'), i18nT('pages.chatSidebar.hours'), i18nT('pages.chatSidebar.days')]}
                              aria-label={i18nT('pages.chatSidebar.custom_recency_unit')}
                              // Was `flex-1 min-w-0` on the old <select>; the
                              // trigger's chrome is fixed inside ui/select.tsx,
                              // but the flex sizing has to survive on the
                              // wrapper div that replaces it as the flex item.
                              style={{ flex: '1 1 0%', minWidth: 0 }}
                            />
                          </div>
                        </div>
                      </div>
                    )
                    const rowBody = (
                      <>
                        {filterDef.icon(active)}
                        <span className="flex-1 truncate">
                          {i18nT(FILTER_LABEL_KEY[filterDef.key])}
                          <span className="text-muted"> · {formatRecentWindow(recentWindowMs)}</span>
                          {slotCount > 0 ? ` (${slotCount})` : ''}
                        </span>
                        {active && <Check size={14} className="text-accent shrink-0" />}
                      </>
                    )
                    if (isMobile) {
                      // Inline: the row keeps its only job (toggle the filter) and
                      // the window options sit under it, one tap each. No chevron
                      // — there is nothing left to open.
                      //
                      // The picker is NOT gated on the filter being active. It was,
                      // on the reasoning that picking a window did not enable the
                      // filter so an always-visible picker reported an effect it
                      // was not having — and picking now DOES enable it, at the
                      // commit seam, for every viewport. Keeping the gate would be
                      // the per-modality thinking that caused this defect.
                      return (
                        <Fragment key={filterDef.key}>
                          <DropdownMenuItem
                            title={i18nT(FILTER_DESCRIPTION_KEY[filterDef.key])}
                            onSelect={e => { e.preventDefault(); toggleFilter('recent') }}
                          >
                            {rowBody}
                          </DropdownMenuItem>
                          <div className="px-2 pb-1">{picker}</div>
                        </Fragment>
                      )
                    }
                    // Flyout. The whole row is a single SubTrigger (one focusable
                    // menu item with correct roving-tabindex). Toggling the
                    // filter must be reachable by every input modality:
                    //  - pointer: onClick toggles; we deliberately do NOT
                    //    preventDefault so Radix's own click-to-open still fires.
                    //  - keyboard: Radix routes Enter/Space/ArrowRight to open the
                    //    submenu and the SubTrigger is a div (no synthetic click),
                    //    so onClick never fires for keys. onKeyDown toggles on
                    //    Enter/Space (preventDefault suppresses Radix's open for
                    //    just those keys); ArrowRight falls through and opens.
                    return (
                      <DropdownMenuSub key={filterDef.key}>
                        <DropdownMenuSubTrigger
                          title={i18nT(FILTER_DESCRIPTION_KEY[filterDef.key])}
                          onClick={() => toggleFilter('recent')}
                          onKeyDown={e => {
                            if (e.key === 'Enter' || e.key === ' ') {
                              e.preventDefault()
                              toggleFilter('recent')
                            }
                          }}
                        >
                          {rowBody}
                          <ChevronRight size={13} className="text-muted shrink-0" />
                        </DropdownMenuSubTrigger>
                        <DropdownMenuSubContent className="min-w-[190px] p-2">
                          {picker}
                        </DropdownMenuSubContent>
                      </DropdownMenuSub>
                    )
                  }
                  return (
                    <DropdownMenuItem
                      key={filterDef.key}
                      title={i18nT(FILTER_DESCRIPTION_KEY[filterDef.key])}
                      // Keep the menu open so multiple filters can be toggled.
                      onSelect={e => { e.preventDefault(); toggleFilter(filterDef.key) }}
                    >
                      {filterDef.icon(active)}
                      <span className="flex-1 truncate">{i18nT(FILTER_LABEL_KEY[filterDef.key])}{slotCount > 0 ? ` (${slotCount})` : ''}</span>
                      {active && <Check size={14} className="text-accent shrink-0" />}
                    </DropdownMenuItem>
                  )
                })}
                {/* Lift every active status filter at once, for a look at the
                    whole list without losing the filter setup, then put them
                    back. A menu row, not a third control in the chip row:
                    AUTOSDE max-two-buttons-per-row grandfathers that row but
                    forbids growing it, and the chip click keeps its one meaning
                    (clear this filter). Hidden with no active filter, when
                    there is nothing to pause. */}
                {activeFilters.size > 0 && (
                  <DropdownMenuItem
                    data-testid="filter-pause-all"
                    aria-pressed={filtersPaused}
                    onSelect={e => { e.preventDefault(); setAllFiltersPaused(!filtersPaused) }}
                  >
                    {filtersPaused ? <Play size={12} className="text-muted" aria-hidden="true" /> : <Pause size={12} className="text-muted" aria-hidden="true" />}
                    <span className="flex-1 truncate">{filtersPaused ? i18nT('pages.chatSidebar.resume_all_filters') : i18nT('pages.chatSidebar.pause_all_filters')}</span>
                  </DropdownMenuItem>
                )}
                <DropdownMenuSeparator />
                {/* Names its object: this menu also carries "Folder order" two
                    sections down, and a bare "Sort by" over one list beside an
                    order over another read as sorting twice, with a guess about
                    which list each one changes. The members page keeps the bare
                    key -- it has no second list. */}
                <FilterMenuLabel>{i18nT('pages.chatSidebar.sort_sessions_by')}</FilterMenuLabel>
                {SORT_OPTIONS.map(o => (
                  <DropdownMenuItem
                    key={o.value}
                    onSelect={() => { setSortKey(o.value); safeSetItem(SESSION_SORT_STORAGE_KEY, o.value) }}
                  >
                    <span className="flex-1">{i18nT(SORT_LABEL_KEY[o.value])}</span>
                    {sortKey === o.value && <Check size={14} className="text-accent shrink-0" />}
                  </DropdownMenuItem>
                ))}
                {/* Folder order: the same row grammar as Sort by, one section down,
                    because it answers the same kind of question about this list.
                    Custom is the person's own arrangement (drag, or an agent's
                    chat_folder_move); Name and Created are views over it that
                    never rewrite a stored position. Offered in EVERY lane, unlike
                    the stale control below: the flat lane explodes chats out of
                    their folders and the conductor lane nests by lineage, but the
                    mode is not idle there -- every row menu's "Move to folder"
                    picker, the history search's folder groups, the Command Bar,
                    the job form and the MCP tree all list in it -- and this is the
                    only control that writes it. A mode a person cannot change from
                    the lane they are in is the trap. */}
                <DropdownMenuSeparator />
                <FilterMenuLabel>{i18nT('pages.chatSidebar.folder_order')}</FilterMenuLabel>
                {FOLDER_SORT_MODES.map(mode => (
                  <DropdownMenuItem
                    key={mode}
                    data-testid={`folder-order-${mode}`}
                    // "Already the mode" is the value in flight while a save is
                    // pending (the newest pick, queued or on the wire), not the
                    // cache (which moves only when a save lands): pick Name,
                    // reopen, pick Custom -- Custom must go out, not be read as
                    // a no-op against the still-Custom cache. A pick that is a
                    // change goes into `folderSortMut`'s one-at-a-time queue.
                    onSelect={() => {
                      const current = folderSortMut.isPending ? folderSortMut.variables : folderSortMode
                      if (mode !== current) folderSortMut.mutate(mode)
                    }}
                  >
                    <span className="flex-1">{i18nT(FOLDER_SORT_LABEL_KEY[mode])}</span>
                    {folderSortMode === mode && <Check size={14} className="text-accent shrink-0" />}
                  </DropdownMenuItem>
                ))}
                {/* Outside Custom a folder drag can re-parent but not reorder
                    (the rows' droppable side is off). Said here, where the mode
                    is chosen, the same caption grammar as the stale control's
                    paused hint below -- and as a FACT about the modes, not the
                    sidebar hint's "Switch to Custom" sentence: that one sits
                    beside a button that does the switching, and the same words
                    here, two rows under the Custom row itself, read as an action
                    that does nothing. The drop itself speaks again when a drag
                    ends with nothing moved (`folderReorderHint`). Only where a
                    folder row is drawn to drag (`folderRowsDrawn`) -- a note
                    promising a reorder the flat and conductor lanes cannot offer
                    would mislead. */}
                {folderSortMode !== 'custom' && folderRowsDrawn && (
                  <div className="px-2 pt-0.5 pb-1.5 text-[11px] text-muted italic whitespace-normal" data-testid="folder-order-reorder-note">
                    {i18nT('pages.chatSidebar.folder_order_reorder_note')}
                  </div>
                )}
                {/* A folder from before the `created_at` stamp existed has no date, so
                    the created comparator puts it after every stamped row, in the
                    stored order -- on an existing tree that is the order the person
                    already had, and picking the mode looks like nothing happened.
                    The same fact-line pattern as the drag note, shown only while the
                    mode is active and such a folder is in the list (a fresh install
                    never sees it); every folder creator has stamped since the mode
                    shipped, so the line retires as those folders go. */}
                {folderSortMode === 'created' && folders.some(f => !folderHasCreatedStamp(f)) && (
                  // Bounded: the menu sizes to its widest child, and a one-line
                  // sentence this long would widen every row to it. Wrapped at the
                  // width the rows already have, it reads as the caption it is.
                  <div className="px-2 pt-0.5 pb-1.5 text-[11px] text-muted italic whitespace-normal max-w-[300px]" data-testid="folder-order-unstamped-note">
                    {i18nT('pages.chatSidebar.folder_order_unstamped_note')}
                  </div>
                )}
                <DropdownMenuSeparator />
                {/* Stale-session collapse threshold. Lives beside Sort rather than
                    in Settings: it shapes how this list reads, exactly like the
                    sort order, and the Recent filter's window picker set the
                    precedent for a duration control in this menu. Hidden in the
                    flat lane and on the board — those views render rows through
                    paths the collapse does not touch, and a control that
                    displays an active setting while doing nothing is a lie. */}
                {!flatLaneActive && !boardLaneActive && (() => {
                  // The trigger must not advertise "· 2d" while the collapse
                  // is inert (narrowed list / non-date sort): a control that
                  // displays an active setting while doing nothing is a lie.
                  const stalePaused = staleCollapseMs > 0 && (listNarrowed || sortKey !== 'date-desc')
                  const staleRowBody = (
                    <>
                      <Clock size={14} className="text-muted shrink-0" />
                      <span className="flex-1 truncate">
                        {i18nT('pages.chatSidebar.stale_collapse_menu')}
                        <span className="text-muted"> · {staleCollapseMs > 0
                          ? (stalePaused
                            ? i18nT('pages.chatSidebar.stale_collapse_paused')
                            : formatRecentWindow(staleCollapseMs))
                          : i18nT('pages.chatSidebar.stale_collapse_off')}</span>
                      </span>
                    </>
                  )
                  const staleLabel = (ms: number) => (ms > 0 ? formatRecentWindow(ms) : i18nT('pages.chatSidebar.stale_collapse_off'))
                  // Caption saying what the durations mean, mirroring the Recent
                  // submenu's "Within" caption above its presets. While paused the
                  // WHY must be readable without hover — the trigger's title
                  // tooltip is invisible to keyboard and touch users, so the hint
                  // renders in the picker too.
                  const staleCaption = (
                    <>
                      <div className="px-2 pt-1 pb-1 text-[11px] text-muted whitespace-normal">{i18nT('pages.chatSidebar.stale_collapse_caption')}</div>
                      {stalePaused && (
                        <div className="px-2 pb-1.5 text-[11px] text-muted italic whitespace-normal">{i18nT('pages.chatSidebar.stale_collapse_paused_hint')}</div>
                      )}
                    </>
                  )
                  if (isMobile) {
                    // Same reason as the Recent picker above: a flyout cannot fit
                    // beside a phone-width menu. The thresholds render inline as
                    // chips rather than as seven more menu rows, so the menu stays
                    // scannable and every option is one tap away.
                    return (
                      <>
                        {/* A section caption, NOT a menu row: inline, there is
                            nothing to tap here (the chips below carry the action),
                            so styling it like the tappable rows above would invite
                            a tap that does nothing. Matches FILTER / SORT BY. */}
                        <DropdownMenuLabel
                          className="text-[11px] uppercase tracking-[.04em] flex items-center gap-2"
                          data-testid="stale-collapse-menu"
                        >
                          {staleRowBody}
                        </DropdownMenuLabel>
                        {staleCaption}
                        <div className="flex flex-wrap gap-1 px-2 pb-1.5">
                          {STALE_COLLAPSE_PRESETS_MS.map(ms => (
                            <DurationChip
                              key={ms}
                              label={staleLabel(ms)}
                              selected={staleCollapseMs === ms}
                              onSelect={() => setStaleCollapseMs(ms)}
                            />
                          ))}
                        </div>
                      </>
                    )
                  }
                  return (
                <DropdownMenuSub>
                  <DropdownMenuSubTrigger data-testid="stale-collapse-menu"
                    title={stalePaused ? i18nT('pages.chatSidebar.stale_collapse_paused_hint') : undefined}>
                    {staleRowBody}
                    <ChevronRight size={13} className="text-muted shrink-0" />
                  </DropdownMenuSubTrigger>
                  <DropdownMenuSubContent className="min-w-[150px] max-w-[240px]">
                    {staleCaption}
                    {STALE_COLLAPSE_PRESETS_MS.map(ms => (
                      <DropdownMenuItem
                        key={ms}
                        onSelect={() => setStaleCollapseMs(ms)}
                      >
                        <span className="flex-1">{staleLabel(ms)}</span>
                        {staleCollapseMs === ms && <Check size={14} className="text-accent shrink-0" />}
                      </DropdownMenuItem>
                    ))}
                  </DropdownMenuSubContent>
                </DropdownMenuSub>
                  )
                })()}
                {/* Tags. Placed above Folders and NOT gated on the lane: tags are
                    a property of the session, so they mean the same thing in the
                    flat list, the folder tree and the board — and the board is
                    exactly where a phone user is most likely to want this, since
                    the columns scroll sideways there. Folders, by contrast, are a
                    list-view structure and stay hidden on the board. */}
                {tagFilterRows.length > 0 && (
                  <>
                    <DropdownMenuSeparator />
                    <FilterMenuLabel>
                      {i18nT('pages.chatSidebar.tags')}
                    </FilterMenuLabel>
                    {tagFilterRows.map(({ tag: t, count, selected }) => (
                      <DropdownMenuItem
                        key={t.id}
                        title={selected
                          ? i18nT('pages.chatSidebar.stop_filtering_by_tag', { name: t.name })
                          : i18nT('pages.chatSidebar.show_only_sessions_tagged', { name: t.name })}
                        // Keep the menu open so several tags can be selected.
                        onSelect={e => { e.preventDefault(); toggleTagFilter(t.id) }}
                        data-testid={`tag-filter-${t.id}`}
                        role="menuitemcheckbox"
                        aria-checked={selected}
                      >
                        <span
                          aria-hidden="true"
                          className="w-3.5 h-3.5 shrink-0 rounded-[3px] border flex items-center justify-center"
                          style={selected
                            ? { borderColor: t.color, background: t.color }
                            : { borderColor: 'var(--border)', background: 'transparent' }}
                        >
                          {selected && <Check size={10} strokeWidth={3} style={{ color: t.color === '#ffffff' ? '#000' : '#fff' }} />}
                        </span>
                        <span className="flex-1 truncate">{t.name}</span>
                        {/* 0 is rendered, not omitted: a zero-count tag is exactly
                            the one that blanks the list when selected. */}
                        <span className="text-muted text-[11px] shrink-0">{count}</span>
                      </DropdownMenuItem>
                    ))}
                  </>
                )}
                {/* Folders sit LAST on purpose: the list grows with the user's
                    folder count, so anything below it would get pushed out of
                    easy reach. Being last, it can simply overflow into the
                    menu's own scroll (the DropdownMenuContent primitive caps to
                    the available viewport height and scrolls) with no inner
                    scroll region of its own. */}
                {/* Reachable in EVERY view, the board included. The folder hide applies
                    to the board's own population, so a person who hides a folder and
                    then switches to a board needs the control that reverses it where
                    they are standing: a board column has no folder header, so it carries
                    no reveal row either, and without this section the hide would have no
                    way back short of leaving board view. */}
                {folderFilterRows.length > 0 && (
                  <>
                    <DropdownMenuSeparator />
                    {/* The heading doubles as the shelve control: activating it
                        rolls the folder list up or down. It stays a menu item so
                        keyboard users reach it with the same arrow keys as every
                        other row, and preventDefault keeps the menu open. */}
                    <DropdownMenuItem
                      onSelect={e => { e.preventDefault(); toggleFoldersShelved() }}
                      data-testid="folder-filter-shelve"
                      aria-expanded={!foldersShelved}
                      title={foldersShelved ? i18nT('pages.chatSidebar.show_the_folder_list') : i18nT('pages.chatSidebar.roll_the_folder_list_up')}
                      className="text-[11px] uppercase tracking-[.04em] text-muted"
                    >
                      <DisclosureChevron open={!foldersShelved} size={12} />
                      <span className="flex-1">
                        {i18nT('pages.chatSidebar.folders')}
                        {hiddenFolderCount > 0 && (
                          <span className="normal-case tracking-normal"> &middot; {i18nT('pages.chatSidebar.hidden_folder_count', { count: hiddenFolderCount })}</span>
                        )}
                      </span>
                    </DropdownMenuItem>
                    {!foldersShelved && (
                      <>
                    {filterHiddenFolders.size > 0 && (
                      <DropdownMenuItem onSelect={e => { e.preventDefault(); showAllFolders() }} data-testid="folder-filter-show-all">
                        <RotateCcw size={12} className="text-muted shrink-0" />
                        <span className="flex-1">{i18nT('pages.chatSidebar.show_all_folders')}</span>
                      </DropdownMenuItem>
                    )}
                    {folderFilterRows.map(({ folder: f, depth, count, hidden, hiddenByAncestor }) => (
                      <DropdownMenuItem
                        key={f.id}
                        style={{ paddingLeft: `${8 + depth * 14}px` }}
                        title={hiddenByAncestor
                          ? i18nT('pages.chatSidebar.hidden_because_parent_hidden', { name: f.name })
                          : hidden ? i18nT('pages.chatSidebar.show_folder') : i18nT('pages.chatSidebar.hide_folder')}
                        // Keep the menu open so several folders can be toggled.
                        onSelect={e => { e.preventDefault(); toggleFolderFilter(f.id) }}
                        data-testid={`folder-filter-${f.id}`}
                        role="menuitemcheckbox"
                        aria-checked={!hidden && !hiddenByAncestor}
                      >
                        <span
                          aria-hidden="true"
                          className="w-3.5 h-3.5 shrink-0 rounded-[3px] border flex items-center justify-center"
                          style={hidden || hiddenByAncestor
                            ? { borderColor: 'var(--border)', background: 'transparent' }
                            : { borderColor: 'var(--accent)', background: 'var(--accent)' }}
                        >
                          {!hidden && !hiddenByAncestor && <Check size={10} className="text-accent-fg" strokeWidth={3} />}
                        </span>
                        <FolderGlyph color={f.color} icon={f.icon} size={12} className="shrink-0 text-muted" />
                        <span className={`flex-1 truncate${hiddenByAncestor ? ' opacity-50' : ''}`}>{f.name}</span>
                        {count > 0 && <span className="text-muted text-[11px] shrink-0">{count}</span>}
                      </DropdownMenuItem>
                    ))}
                      </>
                    )}
                  </>
                )}
              </FilterMenuContent>
            </DropdownMenu>
          </>
        )}
      />
      )} shelf={(
        <>
      {/* One aggregate chip in its OWN row, never per-tag chips in the row below.
          AUTOSDE max-two-buttons-per-row grandfathers that row's existing filter
          chips but forbids growing it, and per-tag chips grow it without bound.
          Tag colours survive as spans inside this single control. */}
      {activeTagIds.size > 0 && (
        <div className="px-3 pb-1">
          <button
            type="button"
            data-testid="tag-filter-chip"
            className="inline-flex items-center gap-1 max-w-full pl-2 pr-1 py-0.5 rounded-full text-[11px] cursor-pointer transition-colors bg-bg-elevated/60 border border-border text-muted hover:text-text"
            onClick={clearTagFilter}
            title={i18nT('pages.chatSidebar.clear_named_filter', { filter: fmtList(activeTagNames, { type: 'disjunction' }) })}
            aria-label={i18nT('pages.chatSidebar.clear_named_filter', { filter: fmtList(activeTagNames, { type: 'disjunction' }) })}
          >
            {/* Swatch carries the colour, the name stays in body text: a pale
                tag on this surface can fall near 2:1 contrast at 11px. */}
            <span className="truncate inline-flex items-center gap-1.5">
              {tagFilterRows.filter(({ tag: t }) => activeTagIds.has(t.id)).map(({ tag: t }) => (
                <span key={t.id} className="inline-flex items-center gap-1">
                  <span
                    aria-hidden="true"
                    className="w-2 h-2 shrink-0 rounded-full border border-border"
                    style={{ background: t.color }}
                  />
                  {t.name}
                </span>
              ))}
            </span>
            <X size={11} className="shrink-0" />
          </button>
        </div>
      )}
      {activeFilters.size > 0 && (
        <div className={FILTER_CHIP_ROW_CLS}>
          {SESSION_FILTERS.filter(filterDef => activeFilters.has(filterDef.key)).map(filterDef => {
            const slotCount = filterCounts[filterDef.key] ?? 0
            const filterLabel = i18nT(FILTER_LABEL_KEY[filterDef.key])
            // The label goes in as-is. It used to be `.toLowerCase()`d to read as
            // mid-sentence English, which does not survive translation: German
            // nouns are capitalised, CJK has no case, and Turkish lowercases `I`
            // to a dotless `ı`.
            const clearLabel = i18nT('pages.chatSidebar.clear_named_filter', { filter: filterLabel })
            return (
              <FilterChip
                key={filterDef.key}
                testId={`filter-chip-${filterDef.key}`}
                label={`${filterLabel}${filterDef.key === 'recent' ? ` · ${formatRecentWindow(recentWindowMs)}` : ''}${slotCount > 0 ? ` (${slotCount})` : ''}`}
                color={filterDef.color}
                paused={filtersPaused}
                clearLabel={clearLabel}
                onClear={() => toggleFilter(filterDef.key)}
              />
            )
          })}
        </div>
      )}
      {seedError && (
        /* Outside the layout branches on purpose. A TOTAL seed failure leaves
         * zero columns, so the board branch never renders — a banner inside it
         * would be invisible in exactly the case it exists for, while the
         * toggle has already flipped and the user is looking at a list.
         * askAgent: the seed writes derived lanes, no draft in the sidebar.
         * Retry is a separate button, not the notice itself. */
        <div className="mx-2 mt-2 flex flex-col gap-1 shrink-0">
          <ErrorNotice
            title={toggleSaveFailed ? undefined : i18nT('pages.chatSidebar.lane_seed_failed')}
            message={seedError}
            askAgent
            onDismiss={() => { setSeedError(''); setToggleSaveFailed(false) }}
            testId="lane-seed-error"
          />
          {/* The Retry button seeds state lanes — the recovery for a SEED
           * failure. It must not show under a toggle-SAVE failure: there its
           * "Try again" label would promise to retry the save but instead seed
           * lanes into a board the user never switched to (UX Review). The
           * save-failure copy already carries its own next step. */}
          {!toggleSaveFailed && (
            <div>
              <Btn
                type="button"
                className="text-[12px] px-2 py-0.5"
                onClick={() => { setSeedError(''); seedStateLanesMutation.mutate() }}
              >
                {i18nT('pages.chatSidebar.lane_seed_retry')}
              </Btn>
            </div>
          )}
        </div>
      )}
      {/* Read failures for the two lists this pane is built from. Same placement
       *  rationale as the seed banner: a failed folders query means no folder
       *  tree, a failed columns query means no board, so neither branch can host
       *  its own notice. Both are pure reads — askAgent on, Retry via refetch. */}
      {foldersFailed && (
        <div className="mx-2 mt-2 flex flex-col gap-1 shrink-0">
          <ErrorNotice
            title={i18nT('pages.chatSidebar.folders_load_failed')}
            message={(errMessage(foldersError) || i18nT('components.errorBoundary.something_went_wrong'))}
            askAgent
            testId="chat-folders-error"
          />
          <div>
            <Btn type="button" className="text-[12px] px-2 py-0.5" onClick={() => void refetchFolders()}>
              {i18nT('pages.chatSidebar.retry')}
            </Btn>
          </div>
        </div>
      )}
      {columnsFailed && (
        <div className="mx-2 mt-2 flex flex-col gap-1 shrink-0">
          <ErrorNotice
            title={i18nT('pages.chatSidebar.columns_load_failed')}
            message={(errMessage(columnsError) || i18nT('components.errorBoundary.something_went_wrong'))}
            askAgent
            testId="tag-columns-error"
          />
          <div>
            <Btn type="button" className="text-[12px] px-2 py-0.5" onClick={() => void refetchColumns()}>
              {i18nT('pages.chatSidebar.retry')}
            </Btn>
          </div>
        </div>
      )}
      {/* Folder writes (create / delete / update) and local "New chat" creates.
       *  Inputs are already persisted or were never typed (a create menu pick),
       *  so the hand-off loses nothing. Dismissable: the failure is a moment, not
       *  a state — the caches have already been re-synced. */}
      <ErrorNotice
        title={i18nT('pages.chatSidebar.folder_update_failed')}
        message={folderActionError}
        askAgent
        onDismiss={() => setFolderActionError('')}
        className="mx-2 mt-2 shrink-0"
        testId="folder-action-error"
      />
      {/* A confirmed subtree close that the server refused per session. It sits in
       *  this same band because it is the same kind of fact: a write on the rows
       *  below failed, the list already agrees with what did happen, and the row
       *  staying put is the only other trace the person would get. */}
      {closeTreeError}
      {closeTreeNotice}
      {/* The folder order (dashboard.folder_sort) could not be read AND there is
       *  no body to fall back on: the tree is drawn in the stored order meanwhile
       *  and sibling drags are withdrawn, so the person is told why the order they
       *  chose is not the one they see -- and that nothing is asked of them (the
       *  read retries on its own). Not dismissable: it is a state, not a moment.
       *  Keyed on the LATCHED failure, not the query status, so a retry's pending
       *  phase does not unmount it; it clears when a body arrives. A failed
       *  background refetch with a body on hand says nothing here. */}
      {folderSortRead.error !== null && (
        <ErrorNotice
          title={i18nT('pages.chatSidebar.folder_order_unavailable')}
          message={folderSortRead.error}
          // The server's own words ("config store", "gateway") stay the message --
          // they are the journal key the hand-off reads -- but under the title as
          // a smaller line, not as the lead: the title says what happened.
          messagePlacement="below"
          footer={i18nT('pages.chatSidebar.folder_order_unavailable_detail')}
          askAgent
          // Under the text, not beside it: a sibling column in this ~300px
          // panel left the title and server string one or two words a line.
          actionPlacement="below"
          className="mx-2 mt-2 shrink-0"
          testId="folder-order-unavailable"
        />
      )}
      {/* A folder drag just ended with nothing moved because the drawn order is
       *  not the stored one (see `folderReorderHint`). Status, not error: no
       *  request failed and nothing is broken -- the gesture is withdrawn in this
       *  mode, and this line says where it comes back and offers the way there:
       *  the action writes Custom through the same path the menu row does, so the
       *  person who just tried to reorder does not have to find the menu. It stays
       *  until the person does something else (no clock takes the action away).
       *  Announced live so a keyboard drag gets the same answer a pointer drag does. */}
      {folderReorderHint && (
        <div
          ref={folderReorderHintRef}
          role="status"
          className="mx-2 mt-2 shrink-0 rounded-lg border border-border bg-bg-elevated px-3 py-2 text-[12px] text-muted"
          data-testid="folder-reorder-hint"
        >
          <div className="flex items-center gap-2">
            <ArrowUpDown size={14} className="shrink-0" aria-hidden="true" />
            <span className="min-w-0 flex-1">{i18nT('pages.chatSidebar.folder_order_reorder_hint')}</span>
          </div>
          {/* On its own line, aligned with the sentence: beside it, the two fight
              for the sidebar's width and the sentence wraps to a word a line.
              The shared primitive, restyled to a link: its disabled state (the
              PATCH in flight) then reads disabled instead of keeping the accent
              and the pointer cursor. */}
          <Btn
            className="mt-1 ml-[22px] p-0 border-none bg-transparent rounded-none text-accent hover:bg-transparent hover:underline text-[12px] font-medium active:scale-100"
            data-testid="folder-reorder-hint-switch"
            disabled={folderSortMut.isPending}
            onClick={() => { if (folderSortMode !== 'custom') folderSortMut.mutate('custom') }}
          >
            {i18nT('pages.chatSidebar.folder_order_switch_custom')}
          </Btn>
        </div>
      )}
      <ErrorNotice
        message={newChatError}
        askAgent
        onDismiss={() => setNewChatError('')}
        className="mx-2 mt-2 shrink-0"
        testId="new-chat-error"
      />
      {/* A refused rename: the editor is already closed and the title has been
       *  reverted to the server value by the recovery refetch, so there is no
       *  unsaved draft left to lose and the hand-off is safe. Dismissable: the
       *  failure is a moment, not a state. */}
      <ErrorNotice
        title={i18nT('pages.chatPage.could_not_rename_session')}
        message={renameError}
        askAgent
        onDismiss={() => setRenameError('')}
        className="mx-2 mt-2 shrink-0"
        testId="rename-error"
      />
        {/* An instance that is CONNECTED but did not answer contributes no rows.
          *  Saying so is the difference between "that instance has nothing open" and
          *  "we could not ask": without this line the list silently claims a
          *  completeness it does not have, which is worse than showing fewer rows.
          *  Placed ABOVE the view branches so it appears in the flat lane, the board
          *  and the folder tree alike — an unreachable peer is not a property of one
          *  layout. Non-blocking by design: local rows are unaffected. */}
        {instanceSessions.loading && (
          /* The same honesty rule as the notice below, for the window BEFORE any
           *  peer has answered: remote rows land seconds after mount, so a list
           *  that stays silent until then reads as complete while it is not, and
           *  the arriving rows shift the list under a scan already in progress.
           *  Muted single line in the same slot, so the two states cannot stack
           *  into competing banners. */
          <div className="mx-2 mt-2 px-2 py-1.5 text-[11px] text-muted flex items-center gap-1.5">
            <Server size={11} aria-hidden="true" className="shrink-0" />
            <span className="min-w-0 truncate">
              {i18nT('pages.chatSidebar.checking_remote_instances')}
            </span>
          </div>
        )}
        {remoteSessionsError && (
          /* A peer read that failed, through the one shared error surface — see
           *  `remoteSessionsError` for how the two failing reads collapse into it.
           *  `askAgent` is ON: this is a LIST read, so the hand-off's navigation
           *  destroys no unsaved state, and a tunnel that stopped answering is
           *  exactly the class of failure the user cannot fix by hand but the
           *  agent often can. Not dismissible: the condition is live, so a
           *  dismissed banner would reappear on the next 15s refetch.
           *  `shrink-0` matches the new-chat notice above it — the rail is a
           *  flex column and a growable banner would eat the list's height. */
          <ErrorNotice
            title={remoteSessionsError.title}
            message={remoteSessionsError.message}
            askAgent
            actionPlacement="below"
            className="mx-2 mt-2 shrink-0"
            testId="instance-sessions-error"
          />
        )}
        </>
      )}>
      <LayoutGroup id="chat-slots">
        {conductorLaneActive ? (
          // Conductor lane: every session nested under the session that OPENED it.
          //
          // The row is the EXISTING session card, unchanged — agent label, time,
          // title, preview, PR chips, loop status, tags, needs-you. The lane adds
          // three things and nothing else: indentation per depth, a chevron with a
          // child count on a card that has children, and that subtree's aggregated
          // badges while the card is collapsed. No compact row design: a nested
          // session is the same object as a top-level one and reads the same.
          //
          // Search flattens it, the way the flat lane does: a query must reach every
          // match, so a match three levels down inside a collapsed conductor cannot be
          // hidden behind a chevron the user would have to guess at.
          //
          // DnD is off, like the flat lane's row order: position here is a function of
          // who opened whom, so there is nothing a drop inside the lane could land on.
          <SessionRowWindowContext.Provider value={laneRowWindow.rowWindow}>
          <motion.div ref={setLaneScrollEl} onScroll={laneScrollMemory.onScroll} layoutScroll={rowAnimEnabled} className={`${LIST_BODY_CLS} flex flex-col`} style={{ scrollbarWidth: 'none' }} data-testid="conductor-view-lane">
            {folderCreateError && renderFolderCreateError(folderCreateError.folderId, folderCreateError.columnId)}
            {localGroupHeader}
            {(() => {
              const tree = lineage
              if (!tree) return null
              const searching = conductorSearching
              // Keyed by identity, exactly as `lineage` is: a raw-key map would let a
              // federated peer row overwrite the local row it collides with, so one
              // session would vanish and the other would render twice.
              const byKey = new Map(conductorRows.map(s => [sessionRowIdentity(s), s] as const))
              // The rows this lane may show: every match, plus each ancestor a match
              // needs to hang from. An ancestor is on screen as CONTEXT -- the filter
              // did not admit it -- so it renders dimmed and still carries its chevron.
              // Without it a filtered-out conductor's workers each resolved no parent
              // and scattered to the top level, which is what switching on Unread did.
              const kept = conductorKept
              const keptKids = (key: string) =>
                (tree.children.get(key) ?? []).filter(k => kept.has(k))
              const rows: Array<{ id: string; slot: Slot; depth: number; childCount: number; expanded: boolean; orphanOf: string | null; citesParent?: string | null; anchorOnly: boolean; aggregate: { needsYou: number; running: number } | null }> = []

              /** Does this row want the user? The same two signals the row itself
               *  renders as a dot or a subtitle, so a collapsed conductor's badge and
               *  its children's badges can never disagree. */
              // Both predicates are derived from the state the CHILD ROWS themselves
              // render from, because the collapsed aggregate claims to be the same fact
              // at a coarser zoom: a count that disagrees with the glyphs it stands for
              // is worse than no count. `runningSet` is the widened signal -- own turn,
              // a live workflow, or a loop -- so a workflow-active child is counted the
              // way its own row is drawn, and a subagent awaiting approval is an ask
              // even though the slot itself carries no `pending_approval`.
              const isRunning = sidebarRows.isRunning
              const wantsUser = (s: Slot) =>
                !!(
                  s.pending_approval
                  || s.needs_input
                  || (subagentApprovalCounts[s.key] || 0) > 0
                  || (unreadSet.has(s.key) && !isRunning(s))
                )

              const emit = (key: string, depth: number) => {
                const slot = byKey.get(key)
                if (!slot) return
                const kids = keptKids(key)
                const expanded = conductorExpanded.has(key)
                const subtree = kids.length > 0 && !expanded
                  ? descendantsOf(key, tree.children).filter(k => kept.has(k))
                  : []
                // Which of the two citation glyphs this row earns. `orphanCitation` only
                // knows the row was placed under nothing; whether that is because the
                // creator closed or because the folder filter conceals it is decided
                // against the unfiltered population. A creator that is still there is
                // open and running, so saying it closed would be false.
                const cited = orphanCitation(slot, tree.parentOf.get(key) ?? null)
                const creator = citedCreatorOf(slot)
                const creatorStillOpen = cited != null && creator !== null
                  && (citedCreatorExists.get(creator.origin)?.has(creator.key) ?? false)
                rows.push({
                  id: key,
                  slot,
                  depth,
                  childCount: kids.length,
                  expanded,
                  orphanOf: creatorStillOpen ? null : cited,
                  citesParent: creatorStillOpen ? cited : null,
                  anchorOnly: !conductorMatching.has(key),
                  // Only a COLLAPSED conductor aggregates: while it is open its
                  // children show their own badges, and showing both would count the
                  // same session twice on one screen.
                  aggregate: subtree.length > 0
                    ? {
                      needsYou: subtree.filter(k => { const c = byKey.get(k); return c ? wantsUser(c) : false }).length,
                      running: subtree.filter(k => { const c = byKey.get(k); return c ? isRunning(c) : false }).length,
                    }
                    : null,
                })
                if (!expanded) return
                for (const kid of kids) emit(kid, depth + 1)
              }

              if (searching) {
                // Flattened: every match at depth 0, in the lane's order, with no
                // chevrons. Matches the flat lane's answer to the same question.
                for (const s of flatSlots) {
                  // The cited creator rides along even though the lane is not nesting:
                  // flattened, a child is otherwise indistinguishable from a root.
                  rows.push({ id: sessionRowIdentity(s), slot: s, depth: 0, childCount: 0, expanded: false, orphanOf: null, citesParent: s.parent?.slot ?? null, anchorOnly: false, aggregate: null })
                }
              } else {
                for (const key of tree.roots) if (kept.has(key)) emit(key, 0)
              }

              return rows.map((row, i) => {
                const next = i < rows.length - 1 ? rows[i + 1] : null
                const isActive = isActiveRow(row.slot)
                const showDivider = next != null && !isActive && !isActiveRow(next.slot)
                return (
                  <Fragment key={row.id}>
                    {/* NO WRAPPER. The row is rendered exactly as the flat lane renders
                     *  it, and the lane's three additions are handed INTO it (see
                     *  `ConductorRowView`, and the chevron's `toggleConductor` action).
                     *  Wrapping the card in a flex column with the chevron and counts
                     *  beside it narrowed the card: its title truncated early, its own
                     *  divider stopped short of the row, and the counts took the column
                     *  the top line keeps for the time. */}
                    {renderSessionRow(row.slot, row.depth, showDivider, 'conductor', 'conductor', 'conductor', {
                      depth: row.depth,
                      childCount: row.childCount,
                      // Every descendant under this card -- what the ✕ label quotes.
                      // Zero while searching: the lane is flat then and the ✕ closes one session.
                      subtreeCount: searching ? 0 : descendantsOf(row.id, tree.children).filter(k => kept.has(k)).length,
                      // What the press would find running, by the close's own predicate,
                      // so the ✕ says "Can't close" before the press instead of after.
                      // Only a card with sessions under it: a leaf keeps the plain single close.
                      subtreeRunning: (() => {
                        if (searching) return 0
                        const under = descendantsOf(row.id, tree.children).filter(k => kept.has(k))
                        if (under.length === 0) return 0
                        return [row.id, ...under].filter(k => { const s = byKey.get(k); return s != null && sidebarRowRunning(s, runningSet, subagentCounts) }).length
                      })(),
                      expanded: row.expanded,
                      aggregate: row.aggregate,
                      orphanOf: row.orphanOf,
                      citesParent: row.citesParent ?? null,
                      anchorOnly: row.anchorOnly,
                    })}
                  </Fragment>
                )
              })
            })()}
            {flatSlots.length === 0 && shownCrewRowCount === 0 && (
              <div className="px-3 py-4 text-[12px] text-muted">{i18nT('pages.chatSidebar.no_sessions_match')}</div>
            )}
            {flatSlots.length > 0 && lineage != null && lineage.children.size === 0 && allHiddenFolders.length === 0 && (
              // Not an error state: the crew log may be off, or nothing has opened
              // anything yet. The lane still shows every session -- it just has no
              // nesting to show, and says so instead of looking broken.
              //
              // Withheld while this lane is concealing a folder, because then the note
              // cannot be read as intended: the rows above it are live sessions, and the
              // reveal row immediately below already says how many folders are hidden,
              // which is the actual reason there is no nesting left to draw.
              <div className="px-3 py-2 text-[11px] text-muted select-none" data-testid="conductor-lane-empty-note">
                {i18nT('pages.chatSidebar.no_conductor_sessions_yet')}
              </div>
            )}
            {renderHiddenReveal('conductor', allHiddenFolders, 0)}
            {renderOlderSessionsHint('conductor')}
            {renderCrewGroups()}
          </motion.div>
          </SessionRowWindowContext.Provider>
        ) : flatLaneActive ? (
          // Flat view: every chat exploded out of its folder into one lane.
          // Removes only the folder rendering hierarchy — sort, pin priority,
          // filters, and search all apply as usual (filteredSlots). No folder
          // tree. This lane only renders when NO tag columns exist: with a
          // board configured, flat view applies inside each column instead
          // (see the column body), so the board never silently disappears.
          // Inactive without folders (the toggle is hidden then too), so a
          // persisted flat preference can never strand the user.
          //
          // Its DndContext carries EXACTLY ONE target: the chat pane. No
          // SortableContext and no folder droppables are registered, so
          // dragging a session into the open chat works here just as it does in
          // the tree, while row order stays a pure function of the sort key —
          // there is nothing for a drop inside the lane to land on. (Order is
          // the reason: a flat lane spans every folder, so a manual position
          // would have no place to be stored.) `sidebarCollision` also keeps the
          // pane out of its closest-edge fallback, so a release inside the
          // sidebar resolves to no target rather than snapping to the pane.
          <DndContext sensors={dndSensors} collisionDetection={sidebarCollision}
            measuring={dndMeasuring}
            onDragStart={handleSidebarDragStart} onDragEnd={handleSidebarDragEnd} onDragCancel={handleSidebarDragCancel}>
            <DndActiveProbe report={reportDndActive} />
            {chatDropTarget && onDropSessionRef && activeDrag?.type === 'session'
              && createPortal(
                <ChatPaneDropZone refusal={draggingRefRefusal} />,
                chatDropTarget,
              )}
            <SessionRowWindowContext.Provider value={laneRowWindow.rowWindow}>
            <motion.div ref={setLaneScrollEl} onScroll={laneScrollMemory.onScroll} layoutScroll={rowAnimEnabled} className={`${LIST_BODY_CLS} flex flex-col`} style={{ scrollbarWidth: 'none' }} data-testid="flat-view-lane">
              {/* Flat view renders no folder headers, so the per-folder mount
               *  points for the create-failure notice never exist here — yet
               *  the New menu still offers "New chat in folder". Render the
               *  notice at the top of the lane so a failed folder create is
               *  never console-only in this layout. */}
              {folderCreateError && renderFolderCreateError(folderCreateError.folderId, folderCreateError.columnId)}
              {localGroupHeader}
              {(() => {
                // Date segments (Today / Yesterday / Last 7 Days / …) between
                // rows — resurrects the 9bb0f71 active-list pattern: only for
                // date sorts (segments mislead on name/created order, same
                // guard as the history pane), and pinned rows render first
                // without segments since pinning overrides date order.
                const isDateSort = sortKey === 'date-desc' || sortKey === 'date-asc'
                // Hoisted above the hold so the pixel anchor can count header heights.
                const baseSeg = (s: Slot) => (isDateSort && !sidebarRows.isPinned(s) ? dateSegment(slotActivityTs(s)) : '')
                const { rows: flatRows, navScope: flatLaneScope, container: flatHoldContainer } = heldLane(flatSlots, 'flat', 'flat', baseSeg)
                // Reads the flag holdHovered just set for THIS lane, so the header
                // rule and the hold cannot disagree about the row being displaced.
                const heldKey = heldDisplacedRef.current ? hoverPinRef.current?.key : undefined
                // Unconditional exclusion would drop the header of a bucket whose
                // sole row — or the lane's top row — is merely being hovered.
                const segOf = (s: Slot) => {
                  const seg = baseSeg(s)
                  return seg && s.key === heldKey ? '' : seg
                }
                let prevSeg = ''
                return flatRows.map((s, i) => {
                  const seg = segOf(s)
                  const showHeader = seg !== '' && seg !== prevSeg
                  if (seg) prevSeg = seg
                  const next = i < flatRows.length - 1 ? flatRows[i + 1] : null
                  const nextIsActive = isActiveRow(next)
                  const isActive = isActiveRow(s)
                  // No divider before a segment header — the header separates.
                  const nextSeg = next ? segOf(next) : seg
                  const showDivider = next != null && !isActive && !nextIsActive && nextSeg === seg
                    && !startsAutomaticSection(flatSlots, i + 1)
                  return (
                    <Fragment key={sessionRowIdentity(s)}>
                      {startsAutomaticSection(flatSlots, i) && <PinnedSessionDivider />}
                      {showHeader && (
                        <div data-date-header data-testid="date-segment-header" className="pl-2 pr-3 pt-3 pb-1 text-[11px] font-semibold text-muted uppercase tracking-[.06em] select-none first:pt-1">{seg}</div>
                      )}
                      {renderSessionRow(s, 0, showDivider, flatLaneScope, flatLaneScope, flatHoldContainer)}
                    </Fragment>
                  )
                })
              })()}
              {flatSlots.length === 0 && shownCrewRowCount === 0 && (
                <div className="px-3 py-4 text-[12px] text-muted">{i18nT('pages.chatSidebar.no_sessions_match')}</div>
              )}
              {/* Flat view has no containers to anchor to — every hide, top-level
               *  or nested, collapses into this one row at the bottom of the lane. */}
              {renderHiddenReveal('flat', allHiddenFolders, 0)}
              {renderOlderSessionsHint('flat')}
              {renderCrewGroups()}
            </motion.div>
            </SessionRowWindowContext.Provider>
            {dragOverlay}
          </DndContext>
        ) : orderedColumns.length === 0 ? (
          // Legacy single-lane layout (identical to pre-columns behavior)
          // Scrollbar hidden (scrollbar-none + inline scrollbarWidth covers
          // Firefox, modern WebKit, and Safari <16) to match the app rail in
          // App.tsx: on macOS with "always show scrollbars" this lane is
          // permanently scrollable, so the 6px track was a fixed stripe down
          // the sidebar rather than a transient hint. Scrolling itself is
          // untouched — wheel, trackpad, keyboard, and drag-autoscroll all
          // still work, and the list's own overflow is still the affordance.
          <SessionRowWindowContext.Provider value={laneRowWindow.rowWindow}>
          <motion.div ref={setLaneScrollEl} onScroll={laneScrollMemory.onScroll} layoutScroll={rowAnimEnabled} className={`${LIST_BODY_CLS} flex flex-col`} style={{ scrollbarWidth: 'none' }} data-testid="tree-view-lane">
            {/* Tree-lane fallback, completing the set (flat and board lanes
             *  carry the same): a create into a folder the folder-filter or
             *  hide feature excludes never renders that folder's header, so
             *  its scoped notice mount does not exist. Renders exactly when
             *  the scoped mount cannot (folderCreateMountAbsent). */}
            {folderCreateError && folderCreateMountAbsent(folderCreateError.folderId) && renderFolderCreateError(folderCreateError.folderId, folderCreateError.columnId)}
            {/* One DndContext owns folder reorder (sortable) + session drag-to-
             *  assign (draggable rows + droppable folder/root targets). */}
            <DndContext sensors={dndSensors} collisionDetection={sidebarCollision}
              measuring={dndMeasuring}
              onDragStart={handleSidebarDragStart} onDragOver={handleSidebarDragOver} onDragEnd={handleSidebarDragEnd} onDragCancel={handleSidebarDragCancel}>
              <DndActiveProbe report={reportDndActive} />
              {/* "Drag a session into the open chat" target. Portaled into
               *  ChatPage's pane so it covers the WHOLE conversation area (not
               *  just the composer), while staying inside this DndContext —
               *  React portals preserve context, and useDroppable measures the
               *  node where it actually renders. Mounted only during a session
               *  drag, and only when a pane and a handler exist. */}
              {chatDropTarget && onDropSessionRef && activeDrag?.type === 'session'
                && createPortal(
                  <ChatPaneDropZone refusal={draggingRefRefusal} />,
                  chatDropTarget,
                )}
              {/* Root lane is the fallback drop target: dropping a session on
               *  empty space (not over a folder) ungroups it (folderId: null). */}
              <DndDroppable id="root-lane" data={{ type: 'folder-drop', folderId: null }}>
                {({ setNodeRef }) => (
                  <div ref={setNodeRef} className="flex flex-col flex-1 min-h-0">
                    {localGroupHeader}
                    <SortableContext items={rootFolderIds} strategy={verticalListSortingStrategy}>
                      {visibleRootFolders.map(f => <SortableFolderBlock key={f.id} folder={f} subtree={[...(folderSubtrees.get(f.id) ?? collectFolderSubtreeIds(folders, f.id))]} siblings={rootFolderIds} reorderable={folderReorderable} dragWithheld={folderDragWithheld} renderFolderBlock={renderFolderBlock} />)}
                    </SortableContext>
                    {/* Bottom of the ROOT folder list. For a top-level hide this
                     *  is the sidebar's own bottom, which is exactly the "single
                     *  footer row" shape — the nested case is what needs depth. */}
                    {renderHiddenReveal('root', hiddenByContainer.get('root') ?? [], 0)}
                    {/* Every folder block and the ungrouped bucket read
                        laneSlots, so an empty one means nothing can render
                        below — say so rather than leaving a blank lane.
                        A folder-NAME search is the case where the plain wording
                        lies: the matched folder rows are rendered directly above
                        this line, so "No sessions match" alone reads as a
                        contradiction ("no conversations matched, even though two
                        folders did"). `folderNameMatchIds` is non-null only when
                        at least one folder name matched, which is exactly when
                        those rows are on screen, so it is the condition — not a
                        proxy for it.
                        The wording deliberately does NOT point at "the folders
                        above": the tree keeps a folder that merely holds a
                        subfolder, so a row that matched nothing can sit in that
                        list, and a sentence claiming it matched is the same
                        contradiction with the roles reversed. It claims only that
                        a folder name matched; the marks say which row. */}
                    {laneSlots.length === 0 && shownCrewRowCount === 0 && listNarrowed && (
                      <div className="px-3 py-4 text-[12px] text-muted">{i18nT(folderNameMatchIds ? 'pages.chatSidebar.no_sessions_match_folders' : 'pages.chatSidebar.no_sessions_match')}</div>
                    )}
                    {/* Ungrouped sessions live in a headerless droppable bucket
                     *  (folderId: null) that fills the remaining height below the
                     *  folders, so the whole empty lower area is a drop target —
                     *  dropping a session here ungroups it. The ring only lights up
                     *  while dragging a foldered session (when ungrouping applies). */}
                    {(rootFolders.length > 0 || ungroupedSlots.length > 0 || shownCrewGroups.length > 0) && (
                      <DndDroppable id="root-group" data={{ type: 'folder-drop', folderId: null }}>
                        {({ setNodeRef: setRootGroupRef, isOver }) => (
                          <div ref={setRootGroupRef} className={`flex flex-col flex-1 min-h-0 rounded-md transition-all ${isOver && (draggingFolderedSession || draggingNestedFolder) ? 'ring-1 ring-accent' : ''}`}>
                            {/* Explicit un-nest target while dragging a subfolder —
                             *  same escape hatch (and wording) as the session zone
                             *  below, always reachable even when the root lane has
                             *  no empty space. */}
                            {draggingNestedFolder && <RootDropHint />}
                            {(() => {
                              const { fresh: freshRootRaw, stale: staleRoot } = splitStale(ungroupedSlots)
                              const { rows: freshRoot, navScope: treeRootScope, container: treeRootContainer } = heldLane(freshRootRaw, 'list', 'tree:root')
                              return (
                                <>
                                  {freshRoot.map((s, i) => {
                                    const nextIsActive = isActiveRow(freshRoot[i + 1])
                                    const isActive = isActiveRow(s)
                                    const showDivider = i < freshRoot.length - 1 && !isActive && !nextIsActive
                                      && !startsAutomaticSection(freshRoot, i + 1)
                                    return (
                                      <Fragment key={sessionRowIdentity(s)}>
                                        {startsAutomaticSection(freshRoot, i) && <PinnedSessionDivider />}
                                        {renderSessionRow(s, 0, showDivider, treeRootScope, treeRootScope, treeRootContainer)}
                                      </Fragment>
                                    )
                                  })}
                                  {!searchRanked && staleExpanded.has('root')
                                    && staleRoot.length > 0 && freshRoot.length > 0
                                    && pinned.has(freshRoot[freshRoot.length - 1].key) && <PinnedSessionDivider />}
                                  {renderStaleSection('root', staleRoot, 0)}
                                  {/* After the dormant expander, so it stays the
                                   *  lane's last line even when rows are folded. */}
                                  {renderOlderSessionsHint('root')}
                                  {renderCrewGroups()}
                                </>
                              )
                            })()}
                            {ungroupedSlots.length === 0 && draggingFolderedSession && <RootDropHint />}
                          </div>
                        )}
                      </DndDroppable>
                    )}
                  </div>
                )}
              </DndDroppable>
              {dragOverlay}
            </DndContext>
          </motion.div>
          </SessionRowWindowContext.Provider>
        ) : (
          // Trello-style horizontal column strip. The columns scroll on their own
          // inside the strip, so the lane itself steps below the floating dock
          // (ListDock) rather than scrolling under it; the hidden-folders line and
          // the board error above the strip stay reachable that way.
          <div className="flex-1 min-h-0 flex flex-col pt-[var(--list-dock-h,0.5rem)]">
          {/* Lane-level fallback ownership (exactly one mount ever renders):
           *  - no columnId (New-menu create): no per-column mount exists;
           *  - board-flat: the columnId-scoped mounts are hidden with folders;
           *  - the error's column was deleted: its mount is gone for good;
           *  - the target FOLDER was deleted mid-flight: no mount anywhere.
           *  With folders shown, the column alive and the folder present, the
           *  column's own mount wins and this line is false. */}
          {folderCreateError && (flatView || !folderCreateError.columnId || !orderedColumns.some(c => c.id === folderCreateError.columnId) || folderCreateMountAbsent(folderCreateError.folderId)) && renderFolderCreateError(folderCreateError.folderId, folderCreateError.columnId)}
          {/* Board writes (delete / reorder / add-after / card drop) report here,
           *  above the strip. Column payloads are server-side, so nothing can be
           *  lost by handing off; the caches were re-synced in the onError. */}
          <ErrorNotice
            title={i18nT('pages.chatSidebar.board_update_failed')}
            message={boardError}
            askAgent
            onDismiss={() => setBoardError('')}
            className="mx-2 mt-2 shrink-0"
            testId="board-error"
          />
          {/* Below the two error notices above, not beside them: this is a
            *  standing fact about the layout, not something that just went
            *  wrong, so it must never push a failure the user has to act on
            *  further down the lane.
            *
            *  The experimental peer-session merge is a FLAT/LIST affordance.
            *  Board columns remain local-slot containers because their tags,
            *  lanes and drop actions are local mutations. Every filtered peer
            *  row is therefore omitted here and represented by this exact count. */}
          {boardPeerCount > 0 && (
            <div className="mx-2 mt-2 px-2 py-1.5 rounded-md bg-info-subtle border border-info/40 text-info text-[11px] flex items-center gap-1.5">
              <Server size={11} aria-hidden="true" className="shrink-0" />
              <span className="min-w-0">
                {i18nT('pages.chatSidebar.remote_sessions_not_shown_in_board_view', { count: boardPeerCount })}
              </span>
            </div>
          )}
          {/* The hide's trace in the board lane, as a row rather than as a tint.
            *
            * The other three lanes end a container with a reveal row, which a board
            * cannot copy: a column draws no folder header for such a row to hang from,
            * and a hidden folder is not a property of any one column anyway — its
            * sessions scatter across all of them, so a per-column row would print the
            * same count once per column. So it sits at the LANE level, beside the notice
            * above that reports the other population a board declines to draw.
            *
            * A row and not just the funnel's tint, because the hide is persistent: it
            * lives in localStorage and survives a reload, so a tint and a hover count are
            * all a returning reader has to account for sessions that are simply fewer
            * than they were. The honest reading of that is deletion.
            *
            * It is a button, and it opens the filter menu, because that menu holds the
            * undo. It also UNSHELVES the menu's folder list on the way: that list is the
            * way back and it is gated behind the shelf, so opening the menu over a
            * rolled-up shelf lands the reader on a dense panel with no folders in it and
            * the word on the button promises something that did not happen. The other
            * lanes' row peeks the folders open in place; this is the same gesture as far
            * as a board can carry it. */}
          {hiddenFolderCount > 0 && (
            <button
              type="button"
              onClick={() => {
                setFoldersShelved(false)
                safeSetItem(FOLDERS_SHELVED_LS_KEY, '0')
                setFilterSortOpen(true)
              }}
              title={i18nT('pages.chatSidebar.show_hidden_folders_from_board', { count: hiddenFolderCount })}
              aria-label={i18nT('pages.chatSidebar.show_hidden_folders_from_board', { count: hiddenFolderCount })}
              data-testid="board-hidden-folders"
              data-hidden-folder-count={String(hiddenFolderCount)}
              className="mx-2 mt-2 px-2 py-1.5 rounded-md bg-warn-subtle border border-warn/40 text-warn text-[11px] flex items-center gap-1.5 text-left cursor-pointer hover:bg-warn/20 transition-colors"
            >
              <EyeOff size={11} aria-hidden="true" className="shrink-0" />
              <span className="min-w-0 truncate" data-testid="board-hidden-folders-count">
                {i18nT('pages.chatSidebar.hidden_folder_count', { count: hiddenFolderCount })}
              </span>
              {/* The action, in VISIBLE text and not only in the name. A count plus a
                *  glyph tells a sighted pointer-less reader that rows are withheld and
                *  leaves them to guess the row is tappable, which is the hover-only
                *  failure this row exists to end. `show` is the catalog's own word for
                *  this affordance, so the 13 locales already carry it. The chevron is
                *  decorative: the word beside it already says what happens. */}
              <span className="ml-auto shrink-0 inline-flex items-center gap-0.5 underline decoration-dotted underline-offset-2"
                data-testid="board-hidden-folders-action">
                {i18nT('pages.chatSidebar.show')}
                <ChevronRight size={11} aria-hidden="true" className="shrink-0" />
              </span>
            </button>
          )}
          <div className="relative flex-1 min-h-0 flex flex-col">
          <div ref={attachStripEdges} className="flex-1 overflow-x-auto overflow-y-hidden flex gap-2 p-2" data-testid="column-strip">
            {orderedColumns.map((col, colIdx) => {
              // The board pool already leaves out every row the folder filter conceals,
              // and this one population feeds every render site below: the flat-board
              // rows, every folder block's body through `colSlotKeys`, each block's
              // aggregate count, and the "no sessions" notice. The board lane has no
              // reveal row (a column has no folder header for one to hang from), so the
              // way back is re-checking the folder in the filter menu.
              const colSlots = sidebarRows.lanes.board.filter(s => columnMatches(col, s))
              const colTags = col.tag_ids.map(tid => tagById[tid]).filter(Boolean) as ChatTag[]
              const laneDef = col.source === 'state' ? SESSION_LANES.find(l => l.key === col.state_key) : undefined
              // Only a single-status-tag column can accept a card: dropping onto a
              // derived lane has nothing to write (the backend refuses it too).
              const isStatusLane = !laneDef && colTags.length === 1 && !!colTags[0].status
              return (
                // Board column is a drag-and-drop drop zone (column reorder + session
                // card drop); mouse-only drag handlers, so scope-disable the rule.
                // eslint-disable-next-line jsx-a11y/no-static-element-interactions
                <div key={col.id} data-testid={`column-${col.id}`} className="flex flex-col flex-1 min-w-0 bg-card border border-border rounded-md overflow-hidden" style={{ minWidth: orderedColumns.length > 1 ? '220px' : undefined }}
                  // Clear first, on the way down: a folder block or unfile strip
                  // inside the lane takes the card and stops the dragover, so only
                  // a dragover that reaches the lane itself shows the hint again.
                  onDragOverCapture={laneDef ? () => setDerivedHintCol(c => (c === col.id ? null : c)) : undefined}
                  onDragOver={e => {
                    const types = e.dataTransfer.types
                    // Accept column reorder on the entire column surface
                    if (types.includes('application/mc-column')) {
                      e.preventDefault()
                      return
                    }
                    // A derived lane leaves the drop refused (no preventDefault)
                    // and shows why.
                    if (laneDef && types.includes('text/plain')) {
                      setDerivedHintCol(col.id)
                      return
                    }
                    // Accept session-card drop only on status lanes
                    if (isStatusLane && types.includes('text/plain')) {
                      e.preventDefault()
                      e.currentTarget.classList.add('ring-1', 'ring-accent')
                    }
                  }}
                  onDragLeave={e => {
                    e.currentTarget.classList.remove('ring-1', 'ring-accent')
                    // Moving onto a child also fires dragleave; only leaving the lane clears.
                    if (!e.currentTarget.contains(e.relatedTarget as Node | null)) setDerivedHintCol(c => (c === col.id ? null : c))
                  }}
                  onDrop={e => {
                    e.currentTarget.classList.remove('ring-1', 'ring-accent')
                    setDerivedHintCol(null)
                    // Column reorder takes priority
                    const draggedCol = e.dataTransfer.getData('application/mc-column')
                    if (draggedCol && draggedCol !== col.id) {
                      e.preventDefault()
                      const ids = orderedColumns.map(c => c.id).filter(id => id !== draggedCol)
                      ids.splice(colIdx, 0, draggedCol)
                      reorderColumnsMutation.mutate(ids)
                      return
                    }
                    if (!isStatusLane) return
                    e.preventDefault()
                    const k = e.dataTransfer.getData('text/plain')
                    if (k) dropSlotMutation.mutate({ slot: k, columnId: col.id })
                  }}>
                  <div className="relative flex items-center gap-1 p-2 border-b border-border bg-bg-elevated">
                    {/* Reorder handle: mouse-only drag source for column reordering. */}
                    {/* eslint-disable-next-line jsx-a11y/no-static-element-interactions */}
                    <span draggable
                      className="cursor-grab text-muted hover:text-text shrink-0"
                      onDragStart={e => { e.dataTransfer.setData('application/mc-column', col.id); e.dataTransfer.effectAllowed = 'move' }}
                      title={i18nT('pages.chatSidebar.drag_to_reorder')}>
                      <GripVertical size={12} />
                    </span>
                    <div className="flex flex-wrap gap-1 items-center flex-1 min-w-0">
                      {laneDef ? (
                        // A lane's identity is its runtime state, so it shows a
                        // fixed name and accent rather than tag chips — there is
                        // no filter behind it for the user to edit.
                        <span className="inline-flex items-center gap-1.5 min-w-0" title={i18nT('pages.chatSidebar.lane_derived_hint')}>
                          <span className="w-2 h-2 rounded-full shrink-0" style={{ background: laneDef.color }} aria-hidden />
                          <span className="text-[11px] font-semibold uppercase tracking-wider truncate" style={{ color: laneDef.color }}>{i18nT(laneDef.labelKey)}</span>
                        </span>
                      ) : colTags.length === 0 ? (
                        <span className="text-[11px] text-muted font-semibold uppercase tracking-wider">{col.name || (col.include_untagged ? i18nT('pages.chatSidebar.untagged_2') : i18nT('pages.chatSidebar.all_sessions'))}</span>
                      ) : (
                        <>
                          {colTags.map(t => (
                            <span key={t.id} className="inline-flex items-center gap-1 px-1.5 py-[1px] rounded-[4px] text-[10px] leading-none font-medium border" style={{ borderColor: t.color, color: t.color, background: t.color + '1a' }}>{t.name}</span>
                          ))}
                          {col.include_untagged && <span className="inline-flex items-center gap-1 px-1.5 py-[1px] rounded-[4px] text-[10px] leading-none font-medium border border-dashed border-muted text-muted" title={i18nT('pages.chatSidebar.also_shows_untagged_sessions')}>{i18nT('pages.chatSidebar.untagged')}</span>}
                        </>
                      )}
                      {col.name && !laneDef && colTags.length > 0 && <span className="text-[11px] text-muted ml-1">· {col.name}</span>}
                      {/* A bare match-all column beside the lanes shows every
                        * session again, so the counts stop summing and cards
                        * appear twice. Seeding deliberately does not delete it
                        * (it is indistinguishable from a column the user added),
                        * so say what it is and let them decide. */}
                      {!laneDef && colTags.length === 0 && !col.name && !col.include_untagged
                        && orderedColumns.some(c => c.source === 'state') && (
                        <span data-testid={`column-duplicates-hint-${col.id}`} className="text-[10px] text-muted ml-1 truncate">
                          · {i18nT('pages.chatSidebar.lane_legacy_column_hint')}
                        </span>
                      )}
                    </div>
                    <span data-testid={`column-count-${col.id}`} className="text-[11px] text-muted shrink-0">{colSlots.length}</span>
                    <button type="button" data-testid={`column-new-folder-${col.id}`} className="text-muted hover:text-accent bg-transparent border-none cursor-pointer shrink-0 p-[2px]" title={i18nT('pages.chatSidebar.new_folder')} aria-label={i18nT('pages.chatSidebar.new_folder')} onClick={() => { setFolderModal({ mode: 'create', parentId: '' }) }}><FolderPlus size={12} /></button>
                    {!laneDef && <button type="button" data-testid={`column-edit-${col.id}`} className="text-muted hover:text-accent bg-transparent border-none cursor-pointer shrink-0 p-[2px]" title={i18nT('pages.chatSidebar.filter_manage_tags')} aria-label={i18nT('pages.chatSidebar.filter_manage_tags')} onClick={() => setColumnEditId(columnEditId === col.id ? null : col.id)}><TagIcon size={12} /></button>}
                    <button
                      type="button"
                      data-testid={`column-add-after-${col.id}`}
                      className="text-muted hover:text-accent bg-transparent border-none cursor-pointer shrink-0 p-[2px] disabled:cursor-wait disabled:opacity-50"
                      title={i18nT('pages.chatSidebar.add_column_after_this_one')}
                      aria-label={i18nT('pages.chatSidebar.add_column_after_this_one')}
                      disabled={addColumnAfterMutation.isPending}
                      onClick={() => addColumnAfterMutation.mutate(col.id)}
                    ><Plus size={12} /></button>
                    <button
                      type="button"
                      data-testid={`column-delete-${col.id}`}
                      className="text-muted hover:text-danger bg-transparent border-none cursor-pointer shrink-0 p-[2px]"
                      title={i18nT('pages.chatSidebar.delete_column')}
                      aria-label={i18nT('pages.chatSidebar.delete_column')}
                      onClick={() => { if (confirm(i18nT('pages.chatSidebar.delete_this_column'))) deleteColumnMutation.mutate(col.id) }}
                    ><X size={12} /></button>
                    {/* Overlays the top of the card list, so the cards under the
                      * pointer do not move while it shows. */}
                    {nativeSessionDrag !== null && derivedHintCol === col.id && (
                      <span role="status" aria-live="polite" data-testid={`column-derived-hint-${col.id}`}
                        className="absolute top-full left-0 right-0 z-10 pointer-events-none px-2 py-1 text-[11px] text-muted border-b border-border bg-bg-elevated">
                        {i18nT('pages.chatSidebar.lane_derived_hint')}
                      </span>
                    )}
                  </div>
                  {/* Column filter popover — portaled to <body> so the column's
                      overflow-hidden ancestor cannot clip it; viewport-anchored
                      to the edit button via popoverPos. */}
                  {columnEditId === col.id && popoverPos && createPortal(
                    /* Non-modal disclosure: role=dialog + a Tab-trap contains keyboard
                       focus, but we deliberately omit aria-modal — the popover has no
                       backdrop and is outside-click-dismissible, so claiming the rest of
                       the page is inert would mislead screen readers. */
                    // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions -- Escape-dismiss and the Tab trap ARE a dialog's documented keyboard operation, and they have to live on the dialog root because the trap reasons about first/last focusable inside it; the onClick only stopPropagation
                    <div ref={columnPopoverRef} role="dialog" aria-label={i18nT('pages.chatSidebar.filter_tags', { name: col.name || 'column' })} tabIndex={-1} data-column-popover={col.id}
                      className="fixed z-[9100] bg-bg-elevated border border-border rounded-lg shadow-lg p-2 min-w-[240px] text-[13px] outline-hidden"
                      style={{ top: popoverPos.top, left: popoverPos.left }}
                      onClick={e => e.stopPropagation()}
                      onKeyDown={e => {
                        if (e.key === 'Escape') { e.stopPropagation(); closeColumnPopover(col.id); return }
                        if (e.key !== 'Tab') return
                        // Trap Tab within the dialog — portal content sits at the end of
                        // <body>, so without this Tab would jump into unrelated page chrome.
                        const root = columnPopoverRef.current
                        if (!root) return
                        const f = Array.from(root.querySelectorAll<HTMLElement>('a[href],button:not([disabled]),input:not([disabled]),[tabindex]:not([tabindex="-1"])'))
                        if (f.length === 0) return
                        const first = f[0], last = f[f.length - 1]
                        const wrapsBackward = e.shiftKey && document.activeElement === first
                        const wrapsForward = !e.shiftKey && document.activeElement === last
                        // A mid-popover Tab is the browser's to move, and not the trap's
                        // to claim. A boundary Tab the IME owns must not cycle focus —
                        // the user is choosing a candidate, not leaving the field —
                        // so `claimKey` (native-event contract in useImeGuard.ts) runs
                        // before the preventDefault() and focus move.
                        if (!wrapsBackward && !wrapsForward) return
                        // `claimSyntheticKey` owns BOTH halves of a decline:
                        // the native event (which document/window listeners
                        // see) and React's own propagation flag (which it
                        // walks when dispatching to component ancestors), so a
                        // declined Tab cannot trigger an ancestor's keyboard
                        // handling.
                        if (!columnPopoverImeLatch.claimSyntheticKey(e)) return
                        e.preventDefault()
                        ;(wrapsBackward ? last : first).focus()
                      }}>
                      <div className="flex items-center justify-between mb-1">
                        <span className="text-[11px] font-semibold text-muted uppercase tracking-wider">{i18nT('pages.chatSidebar.column_filter')}</span>
                        <button className="text-muted hover:text-text bg-transparent border-none cursor-pointer p-0" onClick={() => closeColumnPopover(col.id)} aria-label={i18nT('pages.chatSidebar.close')}><X size={13} /></button>
                      </div>
                      <Input className="w-full py-1 text-[12px] mb-2" placeholder={i18nT('pages.chatSidebar.column_name_optional')} defaultValue={col.name} onBlur={e => { const v = e.target.value.trim(); if (v !== col.name) updateColumnMutation.mutate({ id: col.id, body: { name: v } }) }} />
                      <div className="flex items-center gap-1 mb-2" role="radiogroup" aria-label={i18nT('pages.chatSidebar.match_mode')}>
                        {(['any', 'all', 'none'] as const).map(m => (
                          <button key={m} role="radio" aria-checked={col.mode === m} className={`text-[11px] px-2 py-0.5 rounded cursor-pointer border transition-all ${col.mode === m ? 'border-accent text-accent bg-accent-subtle' : 'border-border text-muted hover:text-text'}`} onClick={() => updateColumnMutation.mutate({ id: col.id, body: { mode: m } })}>{m}</button>
                        ))}
                      </div>
                      <label htmlFor={`column-include-untagged-${col.id}`} className="flex items-center gap-2 px-1 py-1 mb-2 text-[11px] text-muted cursor-pointer select-none hover:text-text" title={i18nT('pages.chatSidebar.also_show_sessions_that_have_no_tags_at_all')}>
                        <input
                          type="checkbox"
                          id={`column-include-untagged-${col.id}`}
                          data-testid={`column-include-untagged-${col.id}`}
                          aria-label={i18nT('pages.chatSidebar.include_untagged_sessions')}
                          checked={!!col.include_untagged}
                          onChange={e => updateColumnMutation.mutate({ id: col.id, body: { include_untagged: e.target.checked } })}
                          className="cursor-pointer"
                        />
                        {i18nT('pages.chatSidebar.include_untagged_sessions')}
                      </label>
                      <TagManagerList
                        mode="column-filter"
                        selectedIds={col.tag_ids}
                        onToggleTag={(_tagId, nextIds) => updateColumnMutation.mutate({ id: col.id, body: { tag_ids: nextIds } })}
                        createTestId={`tag-create-${col.id}`}
                      />
                      <div className="mt-2 flex justify-end">
                        <button className="text-[11px] text-muted hover:text-text bg-transparent border-none cursor-pointer" onClick={() => { updateColumnMutation.mutate({ id: col.id, body: { tag_ids: [] } }) }}>{i18nT('pages.chatSidebar.clear_filter')}</button>
                      </div>
                    </div>,
                    document.body
                  )}
                  <SessionRowWindowScroller className="flex-1 overflow-y-auto scrollbar-none p-1.5 flex flex-col" style={{ scrollbarWidth: 'none' }}>
                    {/* No onDrop here: folder assignment only changes via folder-header drop
                        (into a folder) and the unfile strip below this scroller (out of one).
                        Cross-column drops are handled by the OUTER column onDrop
                        (which only mutates status tags, keeping folder_id intact). */}
                    {(() => {
                      const colSlotKeys = new Set(colSlots.map(sessionRowIdentity))
                      // Show ALL root folders as drop targets, not only those with matching slots.
                      // Empty folders render with "0" count so users see the structure they built.
                      // Root folders in explicit `order`-field order (the sorted
                      // rootFolders memo, same source as list view). Rendering the
                      // raw cache array here made drops appear to revert: a reorder
                      // only rewrites `order` values (array positions are
                      // unchanged), so an unsorted render ignored the new order.
                      //
                      // Flat view inside the board: the same view-only toggle as
                      // the list — folders stop rendering and every matching
                      // session sits directly in the lane, in filteredSlots
                      // order. Cross-lane card drag (the column onDrop above) is
                      // untouched; only folder rendering (and with it folder
                      // reorder/drop, which need folder headers) goes away.
                      // A folder the person unchecked in the filter menu drops out here
                      // for the same reason the tree drops it: the hide is a statement
                      // about the folder, not about one lane, so every lane that renders
                      // folder blocks answers to it. `isFolderHidden` is deliberately NOT
                      // applied -- a board column renders an empty folder header on
                      // purpose, as something to drop onto.
                      const relevantFolders = flatView ? [] : rootFolders.filter(f => !isFolderFilteredOut(f))
                      const { rows: ungrouped, navScope: colLaneScope, container: colHoldContainer } = heldLane(flatView
                        ? colSlots
                        : colSlots.filter(s => {
                            const folderId = sidebarRows.folderOf(s)
                            return !folderId || !folders.find(f => f.id === folderId)
                          }), col.id, `board:${col.id}:ungrouped`)
                      // In flat view folders never render, so an empty lane is
                      // empty — folder structure alone must not suppress the
                      // "no sessions" notice.
                      const hasAny = colSlots.length > 0 || (!flatView && folders.length > 0)
                      return (
                        <>
                          {/* Folder reorder in board view: one DndContext per
                           *  column (folder ids stay unique within it) + the
                           *  header as drag handle. Reorders flow through the
                           *  same global reorderFolders() as list view, so order
                           *  is consistent across columns. Native session-card
                           *  drop (HTML5 DnD) is untouched — it uses drag events,
                           *  not the pointer sensor. Skipped entirely in flat
                           *  view: no folder headers means nothing to drag, and
                           *  an empty context would still mount sensors and a
                           *  body portal per column for nothing. */}
                          {!flatView && (
                          <DndContext sensors={dndSensors} collisionDetection={sidebarCollision} measuring={{ droppable: { strategy: MeasuringStrategy.Always } }} onDragStart={handleSidebarDragStart} onDragEnd={handleSidebarDragEnd} onDragCancel={handleSidebarDragCancel}>
                            <DndActiveProbe report={reportDndActive} />
                            <SortableContext items={relevantFolders.map(f => f.id)} strategy={verticalListSortingStrategy}>
                              {relevantFolders.map(f => <SortableColumnFolder key={f.id} folder={f} columnId={col.id} colSlotKeys={colSlotKeys} subtree={[...(folderSubtrees.get(f.id) ?? collectFolderSubtreeIds(folders, f.id))]} reorderable={folderReorderable} dragWithheld={folderDragWithheld} renderColumnFolder={renderColumnFolder} />)}
                            </SortableContext>
                            {/* Compact ghost follows the pointer while a folder drags —
                             *  same visual as the list-view overlay. DragOverlay renders
                             *  null unless THIS column's DndContext has an active drag,
                             *  so per-column overlays never stack. Portaled to
                             *  document.body: the sidebar rides inside OverlayDrawer's
                             *  morph clip-path, and a clip-path clips fixed-position
                             *  descendants too, so an in-place overlay is erased the
                             *  moment the ghost strays past the drawer edge. React
                             *  portals preserve context, so the overlay still reads
                             *  THIS column's active drag. */}
                            {createPortal(
                              <DragOverlay dropAnimation={null}>
                                {activeDrag?.type === 'folder' ? <FolderDragGhost folder={folders.find(x => x.id === activeDrag.id)} /> : null}
                              </DragOverlay>,
                              document.body,
                            )}
                          </DndContext>
                          )}
                          {ungrouped.map((s, i) => {
                            const isActive = isActiveRow(s)
                            const nextIsActive = isActiveRow(ungrouped[i + 1])
                            const showDivider = i < ungrouped.length - 1 && !isActive && !nextIsActive
                              && !startsAutomaticSection(ungrouped, i + 1)
                            return (
                              <Fragment key={sessionRowIdentity(s)}>
                                {startsAutomaticSection(ungrouped, i) && <PinnedSessionDivider />}
                                {renderSessionRow(s, 0, showDivider, colLaneScope, colLaneScope, colHoldContainer)}
                              </Fragment>
                            )
                          })}
                          {!hasAny && <div className="text-muted text-[12px] text-center py-4">{i18nT('pages.chatSidebar.no_sessions')}</div>}
                        </>
                      )
                    })()}
                  </SessionRowWindowScroller>
                  {/* The way back out of a folder, at the foot of the column and
                    * outside the scroller so it stays in reach however far the
                    * lane is scrolled. Only while a FILED card is in flight: an
                    * unfiled card has nothing to leave, and a column reorder is
                    * not a card. The strip's handlers stop propagation, so the
                    * column onDrop above never sees this release -- the retag
                    * and the unfile stay two gestures. */}
                  {boardDraggingFiledSession && <BoardUnfileDropStrip columnId={col.id} onUnfile={k => moveByDrag(k, null)} />}
                </div>
              )
            })}
          </div>
          {/* Edge cues: lanes continue past a clipped edge of the strip. Each is
            *  a 24 px band, the session tab strip's width, so the lane controls it
            *  overlaps stay legible and clickable. It fades from the foreground
            *  colour at low alpha, not from the page background as the tab
            *  strip's does: the lanes are cards, and in light theme the card and
            *  the page background are both near white, so a fade from bg over a
            *  card shows nothing. The foreground contrasts with the card in every
            *  theme. It is inset to the strip's padding, the cards' own vertical
            *  extent, so it shades the clipped lane and not the page gutter above
            *  and below it. It is fade-only, as the other scroll strips' cues are: the
            *  strip scrolls natively, and an arrow here would look like the
            *  follow-up bar's scroll buttons while a click on it fell through to the
            *  row underneath. */}
          {stripEdges.left && (
            <div aria-hidden="true" data-testid="column-strip-cue-left" className="pointer-events-none absolute left-0 top-2 bottom-2 w-6 z-10 bg-gradient-to-r from-text-strong/15 to-transparent" />
          )}
          {stripEdges.right && (
            <div aria-hidden="true" data-testid="column-strip-cue-right" className="pointer-events-none absolute right-0 top-2 bottom-2 w-6 z-10 bg-gradient-to-l from-text-strong/15 to-transparent" />
          )}
          </div>
          </div>
        )}
      </LayoutGroup>
      </ListDock>

      {/* Drag-move confirmation + undo. Deliberately a SIBLING of the lanes and
          a sibling ABOVE the separator, so it never covers the row that just
          moved and never covers the persistent "Older Sessions" control — the
          footer shifts down by its height while it is up. Session moves and
          folder re-parents share the slot: arming either dismisses the other,
          so at most one offer (and one ⌘Z listener) exists at a time. */}
      <AnimatePresence initial={false}>
        {dragMove?.live && (
          <MoveUndoBar key={dragMove.id} moved={dragMove}
            onUndo={() => undoDragMove(dragMove.id)}
            onHoldChange={undoBar.onHoldChange}
            remainingMs={undoBar.remainingMs}
            paused={undoBar.paused}
            /* Same width ladder as the header's compact/tiny steps: below this the
               prefix + shortcut would eat the row and truncate the destination. */
            compact={paintedSidebarWidth < 220} />
        )}
        {folderMove?.live && (
          <MoveUndoBar key={folderMove.id} moved={folderMove}
            onUndo={() => undoFolderMove(folderMove.id)}
            onHoldChange={folderUndoBar.onHoldChange}
            remainingMs={folderUndoBar.remainingMs}
            paused={folderUndoBar.paused}
            compact={paintedSidebarWidth < 220} />
        )}
      </AnimatePresence>

      {/* When expanded: doubles as the resize handle (accent on hover, drag to resize, dbl-click to collapse).
          When collapsed: just a static 1px divider between sessions and the Older Sessions footer. */}
      {historyOpen ? (
        // Separator that doubles as a Pointer-Events resize handle (drag,
        // mouse/touch/pen) / collapse (double-click); neither gesture is driven
        // from the keyboard on this element.
        // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions -- the handler the rule sees is onDoubleClick, and the collapse it performs is duplicated on the "Older Sessions" row below (role=button/tabIndex=0, Enter+Space), so the COLLAPSE is keyboard-reachable. The RESIZE is not: usePointerDrag exposes pointer handlers only and this pane has no arrow-key resize anywhere. Giving it one is the ARIA window-splitter keyboard contract — a feature, not a lint fix
        <div
          role="separator"
          aria-orientation="horizontal"
          aria-label={i18nT('pages.chatSidebar.resize_history_pane')}
          {...historyResize}
          onDoubleClick={() => setHistoryOpen(false)}
          className="relative h-[6px] cursor-ns-resize z-10 group/drag flex items-center justify-center select-none"
          style={{ touchAction: 'none' }}
        >
          <div className={`w-full transition-all duration-200 ${historyDragging ? 'h-[2px] bg-accent-hover' : 'h-px bg-border group-hover/drag:h-[2px] group-hover/drag:bg-accent'}`} />
        </div>
      ) : (
        <div className="border-t border-border" />
      )}
      {/* Older Sessions footer — the persistent collapse/expand header for the
          history pane. Whole row is the click target; the Clear button stops
          propagation. */}
      <div
        role="button"
        tabIndex={0}
        onClick={() => { if (historyOpen) setHistoryOpen(false); else openHistoryPane() }}
        onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); if (historyOpen) setHistoryOpen(false); else openHistoryPane() } }}
        /* pt/pb are 14px, not py-3, so this row's top border lands on the same
           baseline as the nav rail's community row ("Star us · Report issue"):
           both cards sit 8px off the shell floor, the rail spends 8+2+24+10 =
           44px below its own hairline, and 14+16+14 matches that exactly. The
           symmetric padding is what keeps the clock and label optically centred
           in the band. */
        className="flex justify-between items-center px-3 pt-[14px] pb-[14px] cursor-pointer select-none"
        aria-expanded={historyOpen}
        aria-controls="history-pane"
        aria-label={i18nT('pages.chatSidebar.older_sessions')}
      >
        <span className="flex items-center gap-1.5 text-[13px] font-semibold text-text-strong leading-none">
          <Clock size={14} className="shrink-0" />
          <span className="leading-none">{i18nT('pages.chatSidebar.older_sessions_2')}</span>
        </span>
        {/* Chevron trails the Clear button so the disclosure glyph is the
            rightmost control, and Clear shifts left by the gap rather than
            being pushed off the row's 12px right inset. The gap is 12px, wider
            than the row's other spacing: Clear is destructive (it wipes closed
            sessions behind a single confirm), so a pointer aimed at the collapse
            glyph must not land on it. This trailing position is the pane's ONE
            deliberate exception to the sidebar's leading-chevron grammar
            (#2887): a section header ends with its own disclosure glyph, while
            row-level disclosures (group headers, hidden-folders reveal, the
            folders filter row) lead with theirs like tree rows everywhere else.
            All four share the same mechanic: a ChevronRight that rotates 90°
            when open — never a Right/Down glyph swap, never a counter-rotation
            when closed. */}
        <span className="flex items-center gap-3 shrink-0">
          {historyOpen && history.length > 0 && (
            <button
              className="px-2 py-0.5 rounded-md border border-border bg-transparent text-muted text-[12px] cursor-pointer hover:text-danger hover:border-danger transition-all"
              onClick={async e => { e.stopPropagation(); if (confirm(i18nT('pages.chatSidebar.clear_closed_sessions_active_tabs_and_pinned_ses'))) { await api.clearSessions(); dispatch(fetchHistory(false)) } }}
            >{i18nT('pages.chatSidebar.clear')}</button>
          )}
          <DisclosureChevron open={historyOpen} size={16} className="text-text-strong" />
        </span>
      </div>
      <AnimatePresence initial={false}>
        {historyOpen && (
          <motion.div
            id="history-pane"
            key="history-pane"
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.15, ease: [0.16, 1, 0.3, 1] }}
            className="overflow-hidden"
          >
            <div className="px-2 pb-1">
              <div className="relative">
                <SearchInput className="w-full" placeholder={i18nT('pages.chatSidebar.search_older_sessions')} value={historyFilter} onChange={e => setHistoryFilter(e.target.value)} />
                {historyFilter && (
                  <button type="button" className="absolute right-2 top-1/2 -translate-y-1/2 text-muted hover:text-text cursor-pointer bg-transparent border-none p-0 leading-none transition-colors" onClick={() => setHistoryFilter('')} aria-label={i18nT('pages.chatSidebar.clear_search')}><X size={13} /></button>
                )}
              </div>
              {/* The unresumable-surface notice used to live here. It moved to
                  ChatPage's shared notice slot above the composer (#5925): this
                  pane starts CLOSED (`historyOpen` defaults false), so a notice
                  inside it can only ever be seen by someone who had already
                  opened it -- which is nobody arriving from the command palette,
                  a notification, or ChatPage's own "Continue a previous chat"
                  list. One always-visible site serves all of them. */}
            </div>
            {/* scroll-shadow already fades the top/bottom edge as its
             *  scrollability cue, so the bar itself is redundant here. */}
            <div className="overflow-y-auto scrollbar-none p-2 scroll-shadow" style={{ height: `${historyHeight}px`, scrollbarWidth: 'none' }}>
              {(() => {
                const historyLocalMatch = (s: { title?: string; key: string }) =>
                  ((s.title || '') + s.key).toLowerCase().includes(historyFilter.toLowerCase())
                // Additive rather than a boolean OR: here the backend result IS the
                // source list, so filtering `history` instead would drop backend-only hits.
                // Remote crew sessions are NOT merged here: they are the peer's
                // LIVE slots and join the live sessions list above. Merging them into
                // history as well would render each remote row twice.
                const filteredHistory = (() => {
                  if (!historyFilter) return history
                  if (historyFilter.trim().length >= SEARCH_MIN_CHARS && historySearchResults) {
                    const seen = new Set(historySearchResults.map(s => s.key))
                    return [...historySearchResults,
                            ...history.filter(s => !seen.has(s.key) && historyLocalMatch(s))]
                  }
                  return (historySearchResults ?? history).filter(historyLocalMatch)
                })()
                // One definition of "search active" for every site below: results
                // are present AND the query is still at/above the search threshold.
                // The compound check matters on the clear-X frame: historyFilter
                // empties synchronously but useDebouncedSessionSearch nulls its
                // result in a passive effect (one render later), so a bare
                // `historySearchResults` test would treat that stale frame as an
                // active search and paint date segment headers over a
                // relevance-ordered list.
                const searchActive = historyFilter.trim().length >= SEARCH_MIN_CHARS && !!historySearchResults
                // Hide date segments when the user has an active search — results are
                // Segments only make sense when the list is date-ordered. For name/created
                // sorts (or active search, which is relevance-ranked) they'd interleave.
                const showSegments = !searchActive
                  && (sortKey === 'date-desc' || sortKey === 'date-asc')
                // Active search: keep the backend's relevance ranking (title-boosted;
                // see search_sessions in history.py). Re-sorting search results by the
                // sidebar sort key buried an exact title match under fresher sessions
                // that merely mention the query in their content — and defeated
                // groupHistoryByFolder's documented order-preserving contract. The
                // command palette's Sessions tab already preserves backend order.
                // No search: skip the sort only when the backend already returns
                // date-desc order.
                const sortedHistory = (searchActive || sortKey === 'date-desc') ? filteredHistory : [...filteredHistory].sort((a, b) => compareBySort(a, b, sortKey))
                // An empty pane is reachable whenever every session on disk is
                // already open as a tab (the common case for a light user), so it
                // needs to say so rather than render a search box over blank space.
                // A filtered-to-nothing list is a different statement and reuses the
                // wording the two sibling panes already use for it.
                if (sortedHistory.length === 0) {
                  return (
                    <div className="px-3 py-4 text-[12px] text-muted text-center">
                      {historyFilter
                        ? i18nT('pages.chatSidebar.no_sessions_match')
                        : i18nT('pages.chatSidebar.no_older_sessions')}
                    </div>
                  )
                }
                let prevSeg = ''
                // Derive agent color the same way renderSessionRow does so history rows
                // match the session-row visual language (agent name tinted by source).
                const agentColorFor = (agentName: string): string => {
                  const meta = installedAgents.find(a => a.name === agentName)
                  if (meta?.source === 'package') return 'text-[var(--aim)]'
                  if (meta?.source === 'builtin') return 'text-muted'
                  return 'text-muted'
                }
                const historyRow = (s: (typeof sortedHistory)[number]) => {
                  const displayDate = fmtRelativeTime(s.modified ?? s.created)
                  const agentName = s.agent || defaultAgent || ''
                  // Display vs resolution key, same split as renderSessionRow:
                  // `agentColorFor` must receive the bare name. An archived
                  // session whose JSONL metadata never recorded an agent falls
                  // back to the CURRENT default, which is a different fact from
                  // a session pinned to that same alias — the marker is what
                  // tells them apart (#6529).
                  const agentDisplay = agentName ? agentOrDefaultLabel(s.agent, defaultAgent) : ''
                  const agentColor = agentColorFor(agentName)
                  const isDashboard = s.key.startsWith('dashboard')
                  const channel = slotChannelNamespace(s.key)
                  const surfaceLabel = isDashboard
                    ? i18nT('pages.chatSidebar.dashboard_source')
                    : slotChannelLabel(s.key) || i18nT('pages.chatSidebar.session_source')
                  // Federated-search row from a connected remote instance: its
                  // transcript lives on the other gateway, so activation switches
                  // to that instance's pane instead of resuming a (same-keyed but
                  // unrelated) local session, and the local delete action is
                  // hidden — deleteHistorySession would target the LOCAL file.
                  const remoteInstanceId = (s as { instance_id?: string }).instance_id
                  const remoteInstanceName = (s as { instance_name?: string }).instance_name
                  const activateRow = () => {
                    // A remote row never resumes here, so it can never produce the
                    // unresumable notice above — the pane switch IS its outcome.
                    if (remoteInstanceId) { selectInstance(remoteInstanceId); return }
                    // No post-resolve check here: `resumeFromHistory` itself
                    // records an undisplayable-surface answer on the slice
                    // (#5925), which is what the notice above renders. Keeping
                    // a second copy of that predicate per call site is how the
                    // four sibling entry points ended up giving no feedback at
                    // all while this one did.
                    dispatch(resumeFromHistory({ key: s.key, title: s.title || s.key }))
                  }
                  return (
                    <div className={`group relative flex items-start gap-2.5 pr-4 py-2 rounded-md text-sm transition-all select-none ${remoteInstanceId ? '' : '[@media(hover:none)]:pr-10 '}${!connected ? 'text-muted opacity-50 cursor-not-allowed' : 'text-muted hover:text-text hover:bg-bg-hover cursor-pointer'}`} style={{ paddingLeft: '10px' }} title={s.title || s.key} {...offlineProps(connected, 'resume sessions')} role="button" tabIndex={0} aria-disabled={!connected} onKeyDown={e => {
                      // WCAG 2.1.1: history rows must be resumable via keyboard.
                      if (e.key !== 'Enter' && e.key !== ' ') return
                      if ((e.target as HTMLElement) !== e.currentTarget) return
                      e.preventDefault()
                      if (!connected) return
                      activateRow()
                    }} onMouseDown={e => {
                      // NOTE: pointer activation lives on onMouseDown (not onClick). For a
                      // div[role="button"], browsers do NOT synthesize a click from Enter
                      // (that only happens for native buttons/links — hence the onKeyDown
                      // handler above), and AT activation (e.g. VoiceOver VO+Space)
                      // synthesizes a click INSTEAD of key events. So each path activates
                      // exactly once. Do NOT add an e.detail === 0 guard here or in any
                      // future onClick: AT-synthesized clicks have detail 0 and would be
                      // silently dropped, breaking screen-reader activation.
                      e.preventDefault()
                      if ((e.target as HTMLElement).closest?.('[data-close]')) { if (!remoteInstanceId && confirm(i18nT('pages.chatSidebar.are_you_sure_you_want_to_delete_this_history_ses'))) dispatch(deleteHistorySession(s.key)); return }
                      if (!connected) return
                      activateRow()
                    }}>
                      {/* Platform glyph — fills the left column that session rows reserve for the unread dot */}
                      <span role="img" className="shrink-0 flex items-center justify-center self-center text-muted" title={surfaceLabel} aria-label={surfaceLabel}>
                        {isDashboard
                          ? <Monitor size={12} />
                          : channel === 'unified'
                            ? <MessageSquare size={12} />
                            : <ChannelBrandIcon channel={channel ?? ''} size={12} />
                        }
                      </span>
                      <div className="flex-1 min-w-0 overflow-hidden">
                        <div className={`session-agent-label text-[11px] font-semibold truncate leading-tight flex items-center gap-1 ${agentColor}`}>
                          <span className="truncate" title={agentDisplay || undefined}>{agentDisplay || '\u00A0'}</span>
                          {/* Remote-crew marker. Tinted `info` + a server glyph rather
                              than the neutral chip styling every other meta chip uses:
                              this row's transcript lives on ANOTHER MACHINE, which is a
                              different claim from "has this tag" and the one the user
                              must not misread. The glyph is the non-colour half of the
                              cue, so the distinction survives a colour-vision
                              deficiency; it is aria-hidden because the crew name beside
                              it already names the target.

                              The tooltip names the OUTCOME, and it is the opposite of a
                              live peer row's. Activating this row calls
                              `selectInstance` — the pane switch IS its outcome — whereas
                              a live peer row opens the session HERE. The two carry the
                              same pill, so without this the identical marker would mean
                              two different clicks. */}
                          {remoteInstanceName && (
                            <RemoteCrewChip
                              name={remoteInstanceName}
                              label={i18nT('pages.chatSidebar.on_instance', { name: remoteInstanceName })}
                              title={i18nT('pages.chatSidebar.opens_on_crew_switches_there', { name: remoteInstanceName })}
                            />
                          )}
                          {s.memory_mode === 'incognito' && <span className="text-muted" title={i18nT('pages.chatSidebar.incognito_no_memory_writes')}><EyeOff size={10} /></span>}
                          {s.memory_mode === 'temporary' && <span className="text-aim" title={i18nT('pages.chatSidebar.temporary_no_memory_reads_or_writes')}><VenetianMask size={10} /></span>}
                          {displayDate && <span className="ml-auto text-[11px] text-muted font-normal shrink-0">{displayDate}</span>}
                        </div>
                        <div className="text-[13px] leading-snug line-clamp-2 break-words">{s.title || s.key}</div>
                      </div>
                      {/* Floating hover button group — matches session-row pattern.
                          Hidden for remote rows: deleteHistorySession targets the
                          LOCAL session file, which for a remote row is at best a
                          same-keyed unrelated conversation. */}
                      {!remoteInstanceId && <div className="absolute top-1/2 -translate-y-1/2 right-1.5 opacity-0 group-hover:opacity-100 [@media(hover:none)]:opacity-100 group-focus-within:opacity-100 focus-within:opacity-100 transition-all flex items-center gap-0.5 rounded-md p-1 bg-card border border-border shadow-sm">
                        <button type="button" title={i18nT('pages.chatSidebar.delete_history_session')} aria-label={i18nT('pages.chatSidebar.delete_history_session')} className="text-[12px] text-muted cursor-pointer p-[4px] rounded hover:text-danger hover:bg-danger-subtle transition-all bg-transparent border-none" onMouseDown={e => e.stopPropagation()} onClick={e => { e.stopPropagation(); if (confirm(i18nT('pages.chatSidebar.are_you_sure_you_want_to_delete_this_history_ses'))) dispatch(deleteHistorySession(s.key)) }}><X size={12} /></button>
                      </div>}
                    </div>
                  )
                }
                // Folder-grouped view: during an active content search, regroup the
                // relevance-ranked results under collapsible folder headers (+ Unfiled)
                // by the folder each session was filed in, instead of date segments.
                if (searchActive) {
                  return groupHistoryByFolder(sortedHistory, folders, folderSortMode).map(({ key: gid, folder, rows }) => {
                    const collapsed = collapsedHistoryGroups.has(gid)
                    const groupName = folder ? folder.name : i18nT('pages.chatSidebar.unfiled')
                    return (
                      <Fragment key={gid}>
                        <button type="button" aria-expanded={!collapsed} aria-label={collapsed ? i18nT('pages.chatSidebar.expand_group_results', { group: groupName }) : i18nT('pages.chatSidebar.collapse_group_results', { group: groupName })} className="w-full flex items-center gap-1.5 px-2 pt-3 pb-1 text-[11px] font-semibold text-muted select-none bg-transparent border-none cursor-pointer hover:text-text first:pt-1" onClick={() => setCollapsedHistoryGroups(prev => { const next = new Set(prev); if (next.has(gid)) next.delete(gid); else next.add(gid); return next })}>
                          <DisclosureChevron open={!collapsed} size={12} />
                          {folder ? <FolderGlyph color={folder.color} icon={folder.icon} size={12} open={!collapsed} /> : <Folder size={12} className="text-muted shrink-0" />}
                          <span className="truncate">{folder ? folder.name : i18nT('pages.chatSidebar.unfiled')}</span>
                          <span className="ml-0.5 text-muted font-normal tabular-nums">· {rows.length}</span>
                        </button>
                        {!collapsed && rows.map((s, i) => (
                          <Fragment key={historyRowIdentity(s)}>
                            {historyRow(s)}
                            {i < rows.length - 1 && <div className="mx-3 border-b border-border" />}
                          </Fragment>
                        ))}
                      </Fragment>
                    )
                  })
                }
                return sortedHistory.map((s, idx) => {
                  const tsForSegment = s.modified ?? s.created
                  const seg = dateSegment(tsForSegment)
                  const showHeader = showSegments && seg !== prevSeg
                  prevSeg = seg
                  // Divider between consecutive rows — but not before a segment header
                  // (the header itself separates), and not after the last row.
                  const isLast = idx === sortedHistory.length - 1
                  const nextSeg = !isLast ? dateSegment(sortedHistory[idx + 1].modified ?? sortedHistory[idx + 1].created) : seg
                  const showDivider = !isLast && (!showSegments || nextSeg === seg)
                  return (
                    <Fragment key={historyRowIdentity(s)}>
                      {showHeader && (
                        <div className="px-2 pt-3 pb-1 text-[11px] font-semibold text-muted uppercase tracking-[.06em] select-none first:pt-1">{seg}</div>
                      )}
                      {historyRow(s)}
                      {showDivider && <div className="mx-3 border-b border-border" />}
                    </Fragment>
                  )
                })
              })()}
              {/* Load-more uses onMouseDown+preventDefault to trigger without stealing
                  focus from the transcript; scope-disable the static-interaction rule. */}
              {/* eslint-disable-next-line jsx-a11y/no-static-element-interactions */}
              {historyHasMore && <div className="flex justify-center py-2 text-accent text-[13px] font-medium cursor-pointer hover:bg-accent-subtle rounded-md" onMouseDown={e => { e.preventDefault(); dispatch(fetchHistory(true)) }}>{i18nT('pages.chatSidebar.load_more')}</div>}
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      {/* The close-tree confirm, one instance for the whole sidebar: a press lands
       *  on one row at a time, and the hook already answers an earlier ask "no"
       *  when a second arrives. */}
      {closeTreeDialog}

      {/* One folder create/settings modal for the whole sidebar. Rendered here
       *  rather than per-row so a folder shown in several board columns can only
       *  ever open one, and so the ProjectPicker it hosts has a single owner. */}
      {folderModal && (
        <FolderConfigModal
          open={true}
          mode={folderModal.mode}
          parentId={folderModal.mode === 'create' ? folderModal.parentId : undefined}
          folder={folderModal.mode === 'edit' ? folders.find(f => f.id === folderModal.folderId) : undefined}
          folders={folders}
          installedAgents={installedAgents}
          globalDefaultAgent={defaultAgent}
          availableTags={tagsData}
          availableTagsFailed={tagsQueryFailed}
          onRetryTags={() => { void refetchTags() }}
          onClose={() => setFolderModal(null)}
          onSubmit={async draft => {
            // AWAIT the mutation and only close on success. The backend rejects a
            // free-typed project_dir (not absolute / not an existing directory /
            // sensitive) and a multi-emoji icon with a 400; closing optimistically
            // discarded the whole draft with no feedback. Rethrowing lets the modal
            // stay open and render the reason.
            if (folderModal.mode === 'create') {
              await createFolderMutation.mutateAsync({
                name: draft.name,
                parentId: folderModal.parentId || undefined,
                projectDir: draft.projectDir,
                defaultAgent: draft.defaultAgent,
                color: draft.color,
                icon: draft.icon,
                tags: draft.tags,
                steeringDirs: draft.steeringDirs,
              })
              // Creating a folder while flat view is on would otherwise appear
              // to do nothing (flat rendering skips folder blocks in both the
              // list and the board columns). Exit flat view so the new folder
              // is visible, whichever entry point created it.
              if (flatView) {
                setFlatView(false)
                safeSetItem(FLAT_VIEW_LS_KEY, '0')
              }
            } else {
              // Build the PATCH from what the USER edited (draft.touched, measured
              // against what the modal opened with) — NOT from a diff against live
              // cache, whose shape would revert any field another client changed
              // mid-edit.
              const touched = new Set(draft.touched)
              const body: Record<string, unknown> = {}
              if (touched.has('name')) body.name = draft.name
              if (touched.has('projectDir')) body.project_dir = draft.projectDir
              if (touched.has('defaultAgent')) body.default_agent = draft.defaultAgent
              // '' is a legitimate color instruction: it clears back to gray.
              if (touched.has('color')) body.color = draft.color
              // regenerate_icon and a manual icon are mutually exclusive on the
              // backend; the modal keeps them exclusive in the draft, and this
              // branch keeps them exclusive on the wire.
              if (draft.regenerateIcon) {
                body.regenerate_icon = true
              } else if (touched.has('icon')) {
                // '' clears back to the default glyph.
                body.icon = draft.icon
              }
              // An empty array is a legitimate instruction too: it clears the
              // folder's tags.
              if (touched.has('tags')) body.tags = draft.tags
              // '' / [] clears here as well: PATCH steering_dirs:[] removes the
              // folder's extra steering directories (server resolves effective
              // dirs from folder_id, so nothing resolved is sent).
              if (touched.has('steeringDirs')) body.steering_dirs = draft.steeringDirs
              if (Object.keys(body).length > 0) {
                await updateFolderMutation.mutateAsync({ id: folderModal.folderId, body })
              }
            }
            setFolderModal(null)
          }}
        />
      )}
    </div>
  )
}

export default memo(ChatSidebar)
