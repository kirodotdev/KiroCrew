import { useState, useRef, useEffect, memo, useMemo, useCallback, useId } from 'react'
import { LayoutGroup, AnimatePresence } from 'framer-motion'
import { X, Tag as TagIcon } from 'lucide-react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { usePreviewFlag } from '../hooks/usePreviewFlag'
import { PREVIEW_FOLDER_CONDUCTOR } from '../utils/previewFlags'
import { useStore } from 'react-redux'
import { useAppDispatch, useAppSelector } from '../store'
import type { RootState } from '../store'
import { useConnected } from '../hooks/useConnected'
import { fetchHistory, deleteHistorySession, switchSlot, clearHistorySessions } from '../store/chatSlice'
import { api } from '../api/client'
import { computeRecentRank, clampTintCount } from '../utils/recencyTint'
import { useDndSensors } from '../hooks/useDndSensors'
import { useSessionPalette } from '../hooks/useSessionPalette'
import { setCrewEnabled } from '../lib/crewEnabled'
import { useReducedMotion } from '../hooks/useReducedMotion'
import { useSessionActions } from '../hooks/useSessionActions'
import { useChatPopouts } from '../hooks/useChatPopouts'
import { useImeGuard } from '../hooks/useImeGuard'
import { useIsMobile } from '../hooks/useIsMobile'
import { useIsTouchDevice } from '../hooks/useIsTouchDevice'
import ResizeHandle from '../components/ResizeHandle'
import { ListDock } from '../components/ListDock'
import { LIST_SHELL_CLS } from '../components/listShell'
import MoveUndoBar from '../components/MoveUndoBar'
import TagManagerList from '../components/TagManagerList'
import type { ChatFolder } from '../types'
import { useSessionRowWindowRoot } from './chat/sessionRowWindow'
import { useSessionSort } from './chat-sidebar/sessionSort'
import { useLanguageGeneration } from '../i18n/useLanguageGeneration'

import { i18nT } from '../i18n/t'
import { uiLocation } from '../uiLocations/uiLocation'
import { useGuideDisclosureScope } from '../guide/GuideRevealScope'
import { useGuide } from '../guide/GuideContext'
import { useLaneScrollMemory } from '../hooks/useLaneScrollMemory'
import { useScrollEdges } from '../hooks/useScrollEdges'
import { compareText } from '../i18n/format'
export { sidebarCollision, isFolderNestBand } from './chat-sidebar/dnd/collision'
export { boardSidebarWidth } from './chat-sidebar/board'
import { useSessionFilterState, useSessionStatusFilters } from './chat-sidebar/filters'
import { useDebouncedSessionSearch, useSearchMatches } from './chat-sidebar/search'
import { openCrewWindow, useCrewWindow } from './chat/crew-window/crewWindowStore'
import { useStableCallbackProps } from './chat/useStableCallbackProps'
import { sessionRowIdentity } from './chat-sidebar/rowIdentity'
import { buildSidebarRows, guideStepOnRowControl, type RowScene, type RowShell, type SidebarRows } from './chat-sidebar/rows'
import { useSessionSources } from './chat-sidebar/sessionSources'
import { crewSpaceChain, useCollapsedCrews, useCollapsedCrewSpaces, useCrewEnable } from './chat-sidebar/CrewGroups'
import { crewOf, type CrewGroup } from '../hooks/useInstanceSessions'
import { useSessionRename, useSessionAutoTitle, useFolderRename } from './chat-sidebar/rename'
import { useSidebarLane, useLaneCycle, renderedLane } from './chat-sidebar/lanes'
import { useHistoryPane } from './chat-sidebar/history'
import { usePinnedSessionOrder, usePinnedOrderAuthority, usePinnedKeyboardReorder } from './chat-sidebar/pinnedOrder'
import { useRowAnimSettled } from './chat-sidebar/rowAnimSettle'
import { useStaleCollapse, useStaleMoveWatcher, useStaleNarrowBridge } from './chat-sidebar/stale'
import { useFolderSort, useFolderVisibility, useFolderFilterReveal, useFolderFilterRows, useFolderMutations, useFolderTree, useRootFolderLanes } from './chat-sidebar/folders'
import FolderCleanupPanel from './chat-sidebar/FolderCleanupPanel'
import { useSidebarResize } from './chat-sidebar/resize'
import { useSidebarScrollPeek, useFullTitlesOnScrollSetting } from './chat-sidebar/scrollPeek'
import ScrollPeekLayer from './chat-sidebar/ScrollPeekLayer'
import { useSidebarTags } from './chat-sidebar/tags'
import { useSidebarModels } from './chat-sidebar/models'
import { useBoardColumns, useColumnPopover, useBoardColumnMutations, useColumnMatches, useBoardFolderCollapse } from './chat-sidebar/board'
import { useHoverHold, useHoverPinLiveness } from './chat-sidebar/hoverHold'
import { useLineageAvailable, useConductorLane } from './chat-sidebar/conductor'
import { useShortcutOrder } from './chat-sidebar/shortcuts'
import { useFolderDropOps, useSidebarMoveUndo, useSidebarDragHandlers, useNativeSessionDrag } from './chat-sidebar/dnd/useSidebarDrag'
import { useSidebarReveal } from './chat-sidebar/reveal'
import { useHoldPinnedHeaderOnCollapse } from './chat-sidebar/stickyCollapse'
import { useFolderChatCreate, useSessionCreate } from './chat-sidebar/create'
export { FOLDER_BODY_INSET_PX, FOLDER_BODY_CLS, BOARD_FOLDER_BODY_CLS, SIDEBAR_TEXT_CONTROL_CLS, STALE_TOGGLE_CLS } from './chat-sidebar/chrome'
export { connectedChannelLinks } from './chat-sidebar/SessionRow'
import type { ChatSidebarProps, SidebarScope } from './chat-sidebar/scope'
import { createLaneRows } from './chat-sidebar/laneRows'
export { SIDEBAR_DISPLACEMENT_WINDOW } from './chat-sidebar/laneRows'
import { createFolderBlocks } from './chat-sidebar/folderBlocks'
import { createListLanes } from './chat-sidebar/listLanes'
import { createBoardLane } from './chat-sidebar/boardLane'
import ErrorNotice from '../components/ErrorNotice'
import { renderSidebarHeader } from './chat-sidebar/SidebarHeader'
import { renderSearchDock } from './chat-sidebar/SearchDock'
export { FILTER_LABEL_KEY, FILTER_DESCRIPTION_KEY, SORT_LABEL_KEY, FOLDER_SORT_LABEL_KEY } from './chat-sidebar/SearchDock'
import { renderHistoryPane } from './chat-sidebar/HistoryPane'
import { renderFolderModal } from './chat-sidebar/folderModal'
import { renderSessionCleanupPanel, useSessionCleanup } from './chat-sidebar/sessionCleanup'
import { renderBulkModelPanel, useBulkModelSwitch } from './chat-sidebar/bulkModelSwitch'
import BroadcastPanel from './chat-sidebar/BroadcastPanel'

/** Stable empty group list for the board, which groups nothing. */
const NO_CREW_GROUPS: CrewGroup[] = []

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
 * key-indexed state for the rows it builds only after its one origin check. After its
 * last hook call the shell builds one scope (./chat-sidebar/scope) and hands it to the
 * render owners, which call no hooks: the lane-row and folder-block factories (built once
 * per render, so the row stamps belong to that pass), the lanes, the header, the dock and
 * the Older Sessions pane. What stays here: every hook call, the row actions and row shell
 * the row memo compares, the native confirm() dialogs (eslint's native-dialog allowlist
 * names this file) and the root region. The owner table and the pin list are in
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
  defaultAgent, installedAgents, rowAgents, mode, onWidthChange, onDragChange, fillsHost, onSelectSlot, onOpenSlotInNewTab, onOpenSource, onOpenPeerSession, collapsible,
  chatDropTarget, onDropSessionRef, staticRows, scrollPeek,
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
  // Kept apart from the error above because nothing was sent: an offline refusal
  // is status text, and routing it through the danger surface would claim a failure.
  const [folderOfflineNotice, setFolderOfflineNotice] = useState('')
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
  // Whether any row renders a runs-elsewhere chip, i.e. whether this sidebar
  // issues the ['instances'] read that RemoteCrewNamesError reports on.
  const hasRemoteExecutedRow = useMemo(
    () => allRows.some(r => r.executor === 'remote' && !!r.instance_id),
    [allRows],
  )
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
  const { autoTitleError, setAutoTitleError, onAutoTitle } = useSessionAutoTitle({ dispatch, store })
  // Folder create / settings modal target. One modal instance is rendered at the
  // sidebar root, so — unlike the inline inputs it replaced — it needs no column
  // scope: a folder rendered in several board columns can only have one modal.
  // `parentId` is the fixed destination for 'create' ('' = top level).
  const [folderModal, setFolderModal] = useState<
    { mode: 'create'; parentId: string } | { mode: 'edit'; folderId: string } | null
  >(null)
  // The session sort lives in the gateway config (dashboard.session_sort), so a
  // pick follows the person to every browser.
  const { sortKey, setSortKey, sortError, dismissSortError } = useSessionSort({ queryClient })
  const { lane, setLanePersisted, setFlatView } = useSidebarLane()
  /** Derived, so every existing `flatView` read site keeps its exact meaning. */
  const flatView = lane === 'flat'
  const conductorView = lane === 'conductor'
  const {
    activeFilters, filtersPaused, setAllFiltersPaused, clearAllFilters,
    filterHiddenFolders, setFilterHiddenFolders, toggleFolderFilter, unhideFolderChain,
    showAllFolders, filterTagIds, toggleTagFilter, clearTagFilter, foldersShelved, setFoldersShelved,
    toggleFoldersShelved, toggleFilter, disableFilter, enableFilter,
    hiddenFilters, toggleHideFilter, clearHiddenFilters,
    filterModelKeys, filterModelsExcluded, toggleModelFilter, setModelFilterExcluded, clearModelFilter,
  } = useSessionFilterState()
  const {
    slotsLoaded, workflowActiveSet, automationRunningSet, subagentCounts, subagentStartedCounts, subagentApprovalCounts, unreadSet,
    recentWindowMs, recentAmountDraft, setRecentAmountDraft, recentUnitDraft, selectRecentPreset,
    commitRecentAmount, changeRecentUnit, runningSet, recentSet,
  } = useSessionStatusFilters({ unreadSlots, activeFilters, filtersPaused, enableFilter, localSlots, disableFilter })
  const creatingSlot = useAppSelector(s => s.chat.creatingSlot)
  const connected = useConnected()
  // An offline refusal stops being true once the gateway is back, so retire it on
  // the disconnected -> connected transition only.
  const wasConnected = useRef(connected)
  useEffect(() => {
    if (connected && !wasConnected.current) setFolderOfflineNotice('')
    wasConnected.current = connected
  }, [connected])
  const {
    historyOpen, setHistoryOpen, openHistoryPane, historyHeight, historyDragging, historyResize,
  } = useHistoryPane({ setHistoryFilter, slotFilter, dispatch })
  const [cleanupOpen, setCleanupOpen] = useState(false)
  const [folderCleanupOpen, setFolderCleanupOpen] = useState(false)  // header ⋮ → "Clean up empty folders" panel
  const [manageTagsOpen, setManageTagsOpen] = useState(false)  // header ⋮ → "Manage tags…" panel (list-view tag CRUD)
  const [broadcastOpen, setBroadcastOpen] = useState(false)  // header ⋮ → "Broadcast message…" panel
  const [filterSortOpen, setFilterSortOpen] = useState(false)
  const sessionCleanup = useSessionCleanup({ cleanupOpen, setCleanupOpen, activeSlot, dispatch, queryClient })

  const bulkModel = useBulkModelSwitch({ localSlots })

  const {
    pinned, pinnedOrder, pinnedRank, reorderPinned, orderState: pinnedOrderState,
  } = usePinnedSessionOrder({ localSlots, sortKey })

  const {
    staleCollapseMs, setStaleCollapseMs, staleExpanded, setStaleExpanded, setStaleRecentlyMoved,
    isStaleExempt,
  } = useStaleCollapse({ pinned, activeSlot, runningSet, subagentCounts, unreadSet })

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
  const { modelFilterRows, activeModelKeys, activeModelRows, modelFilterPasses } = useSidebarModels({ filterModelKeys, filterModelsExcluded, localSlots })
  // The direction is only ON when it has something to invert: a stored exclude
  // whose keys no session uses any more must not draw a ticked invert row (or
  // mark rows as hidden) while the next tick would narrow to "only X".
  const modelFilterInverted = filterModelsExcluded && activeModelKeys.size > 0
  const {
    rawColumns, tagColumnsSettled, columnsFailed, columnsError, refetchColumns, tagColumnsEnabled,
    hideEmptyFolderBody, orderedColumns,
  } = useBoardColumns()
  // After the columns: only board view may widen the sidebar past SIDEBAR_MAX.
  const {
    paintedSidebarWidth, rootStyle, sidebarMax, paintedWidthRef, sidebarResize, nudgeSidebar, widenForBoard, restorePreBoardWidth,
  } = useSidebarResize({ onWidthChange, onDragChange, fillsHost, boardActive: orderedColumns.length > 0 })
  const fullTitlesOnScroll = useFullTitlesOnScrollSetting()
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
      models: { passes: modelFilterPasses, active: activeModelKeys.size > 0 },
      search: { text: slotFilter, folderMatches: folderNameMatchIds },
      status: { active: activeFilters, hidden: hiddenFilters, paused: filtersPaused, recentWindowMs },
      folders: { hiddenSubtree: filterHiddenSubtree, active: folderFilterActive },
    },
    clears: { tags: clearTagFilter, models: clearModelFilter, search: clearSearch, status: clearAllFilters, folder: clearFolderHide, hides: clearHiddenFilters },
    crewGroups: shownCrewGroups,
    sortKey,
    clock: Date.now,
    frozen: dragFrozen,
  }, previousRowsRef.current)
  previousRowsRef.current = sidebarRows
  const { listNarrowed, revealBlockingFilters, laneOrder, isRowFolderHidden, isRowStatusHidden } = sidebarRows
  // `laneSlots` is the Local lanes' rows: `filteredSlots` minus every row a crew
  // group holds. `shownCrewRows` is each crew group's rows, the folder hide applied.
  const {
    main: laneSlots, crew: shownCrewRows, crewRowCount: shownCrewRowCount,
    flat: flatSlots, ungrouped: ungroupedSlots, boardPeerCount,
  } = sidebarRows.lanes
  const filterCounts = sidebarRows.statusCounts
  const [collapsedCrews, toggleCrewCollapsed, expandCrew] = useCollapsedCrews()
  const [collapsedSpaces, toggleSpaceCollapsed, expandSpaces] = useCollapsedCrewSpaces()
  const enableCrew = useCallback((id: string) => setCrewEnabled(dispatch, id, true)
    .finally(() => { void queryClient.invalidateQueries({ queryKey: ['instances'] }) }), [dispatch, queryClient])
  const crewEnable = useCrewEnable(enableCrew, id => instancesList.find(i => i.id === id)?.name || id)

  const { hoverPinRef, heldDisplacedRef, releaseHoverPin, heldLane, onRootPointerOver } = useHoverHold()

  /**
   * Is the conductor PIN on? Settings > Developer > Feature Previews, default
   * off (see `utils/previewFlags.ts`).
   *
   * It gates the pinned card, its accent bar and its tag -- the three things
   * that change the resting list -- and nothing else. Neither way to REACH the
   * conductor is gated on it: not the row's chat button, and not the same action
   * in the folder's two menus. Both are ingresses that move nothing on the list,
   * and holding them would leave a conducted folder with no way to the
   * conversation it is drawn around.
   */
  const folderConductorPin = usePreviewFlag(PREVIEW_FOLDER_CONDUCTOR)

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
  // `rowAnimEnabled` itself is derived below the pinned-header hold, which
  // also turns it off (see `instantCloseId`).

  const {
    isFolderFilteredOut, revealedContainers, toggleReveal, hiddenByContainer, allHiddenFolders,
    hiddenFolderCount,
  } = useFolderFilterReveal({ folderFilterActive, filterHiddenFolders, folders, isFolderHidden, folderCompare, boardLaneActive })
  const {
    citedCreatorExists, conductorRows, conductorMatching, lineage, conductorExpanded, toggleConductorExpanded,
    expandConductorAncestors,
  } = useConductorLane({ conductorLaneActive, allRows, allLiveSlots, isRowFolderHidden, isRowStatusHidden, laneOrder, flatSlots })
  // Reveal opens the crew group holding the target, then the lane's own ancestors.
  // Looked up in `allRows`: the reveal may be clearing the filter that hides it.
  const expandRevealAncestors = useCallback((identity: string) => {
    const row = allRows.find(s => sessionRowIdentity(s) === identity)
    const crew = row ? crewOf(row) : undefined
    if (crew && shownCrewRows.has(crew)) {
      expandCrew(crew)
      // And the crew's own spaces above the row, so the reveal lands on it.
      const folders = shownCrewGroups.find(g => g.id === crew)?.folders ?? []
      expandSpaces(...crewSpaceChain(crew, folders, row?.peer_folder_id))
    }
    expandConductorAncestors(identity)
  }, [allRows, shownCrewRows, shownCrewGroups, expandCrew, expandSpaces, expandConductorAncestors])

  const {
    availableLanes, nextLane, laneSwitchLabel, cycleLane,
  } = useLaneCycle({ lineageAvailable, boardLaneActive, folders, lane, setLanePersisted })

  const sidebarRootRef = useRef<HTMLDivElement>(null)
  // Scroll peek (desktop list lanes only); see chat-sidebar/scrollPeek.
  // Desktop-only by design (the narrow-viewport-required exemption): the peek
  // draws a clipped title in full past the sidebar's edge, over the chat
  // beside it. The phone sessions pane is a near-full-width drawer that leaves
  // no room right of the pane for an overflowing title, and nothing beside it
  // to draw over; there the row's `title` attribute and the rename box stay
  // the path to the full string. The board lane has no list rows to peek.
  const peekTitles = useSidebarScrollPeek({
    enabled: fullTitlesOnScroll && !!scrollPeek && !isMobile && !boardLaneActive,
    rootRef: sidebarRootRef,
    laneRef: laneScrollRef,
  })
  const { digitModifierHeld, shortcutDigitByKey } = useShortcutOrder({ sidebarRootRef, dispatch, localSlots })

  const {
    folderFilterRows,
  } = useFolderFilterRows({ folderTree: sidebarRows.folderTree, folders, folderCompare, filterHiddenFolders, filterHiddenSubtree })

  const {
    createFolderMutation, deleteFolderMutation, updateFolderMutation, toggleCollapse,
  } = useFolderMutations({ queryClient, setFolderActionError, setFolderOfflineNotice, folders, connected })
  // The list view's toggle. Its folder headers are sticky, so a collapse also
  // moves the lane to keep a pinned header where it is painted, in the commit
  // that hides the body (see stickyCollapse.ts); `from` is the pressed control,
  // inside the folder block.
  const { armHold, disarm: disarmHold, instantCloseId } = useHoldPinnedHeaderOnCollapse(laneScrollRef, folders)
  // A collapse from a pinned header takes the reduced-motion path for its one
  // commit (`instantCloseId`, see stickyCollapse.ts).
  const rowAnimEnabled = !reduceMotion && instantCloseId === null
  // The lane's first population moves rows without animating them, until they
  // have painted (see rowAnimSettle.ts). A separate flag, not a term of
  // `rowAnimEnabled`: Framer reads a row's layout props once, when it mounts.
  // `slotsGeneration` counts slot lists applied since page load and, unlike
  // `slotsLoaded`, a socket (re)connect does not reset it, so an empty gateway
  // whose list already landed is not waited on forever.
  const slotListLanded = useAppSelector(s => (s.dashboard.slotsGeneration ?? 0) > 0)
  const rowAnimSettled = useRowAnimSettled((slotsLoaded || slotListLanded) && (foldersLoaded || foldersFailed) && tagColumnsSettled)

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
  } = useFolderDropOps({ folderReorderable, queryClient, setFolderActionError, setFolderOfflineNotice, updateFolderMutation, connected })
  const { folderSubtrees, expandFolderAncestors } = useFolderTree({ folders, updateFolderMutation, clearBoardCollapse })

  const {
    revealFlash,
  } = useSidebarReveal({ sidebarRootRef, dispatch, localSlots, revealBlockingFilters, staleCollapseMs, sortKey, isStaleExempt, slotFolders, setStaleExpanded, expandFolderAncestors, expandConductorAncestors: expandRevealAncestors, folders, setSlotFilter, setFilterHiddenFolders, setRevealForcedVisible, setFlatView })
  const renameCommit = useCallback((id: string, name: string) => {
    // Re-checked here, not only where the editor opens: the gateway can drop
    // while an editor is open. Keep the editor and its draft, and say why,
    // instead of firing a PATCH whose rollback would discard the typed name.
    if (!connected) {
      setFolderOfflineNotice(i18nT('utils.offline.gateway_offline_reconnect', { action: i18nT('utils.offline.rename_folders') }))
      return
    }
    if (name.trim()) updateFolderMutation.mutate({ id, body: { name: name.trim() } })
    setEditingId(null)
  }, [updateFolderMutation, setEditingId, connected])
  const {
    dragMove, undoDragMove, undoBar, folderMove, undoFolderMove, folderUndoBar, moveByDrag, moveFolderByDrag,
  } = useSidebarMoveUndo({ localSlots, folders, moveFolderTo, queryClient })
  // Surface-agnostic session actions (duplicate/read/pin/copy/move/close) shared
  // by all three row menus AND the row's non-menu buttons (Duplicate/Close) so
  // each behaviour has one definition. Rename + Tags stay local (they drive this
  // component's inline-edit + tag-popover state).
  const sessionActions = useSessionActions(mode)
  const closeRefused = useAppSelector(st => st.chat.closeRefused)
  const { forkError: duplicateError, clearForkError: clearDuplicateError } = sessionActions
  // Which sessions are currently open in a popped-out window (shared singleton).
  const { poppedOut } = useChatPopouts()
  const {
    handleSidebarDragStart, reportDndActive, handleSidebarDragEnd, handleSidebarDragCancel,
    handleSidebarDragOver,
  } = useSidebarDragHandlers({ releaseHoverPin, setDragFrozen, hideFolderReorderHint, setActiveDrag, activeDrag, dragFrozen, folderReorderable, folderSortRead, showFolderReorderHint, moveFolderByDrag, reorderFolders, searchRanked, pinned, reorderPinned, localSlots, activeSlot, onDropSessionRef, moveByDrag, folders, boardFolderCollapsed, updateFolderMutation, clearBoardCollapse })
  const {
    folderCreateError, setFolderCreateError, createChatInFolder, refuseCreateOffline,
  } = useFolderChatCreate({ folders, mode, dispatch, dropSlotMutation, onOpenSlotInNewTab, updateFolderMutation, clearBoardCollapse, connected, setFolderOfflineNotice })

  const {
    crewPreview, openCrewMembers, remoteCrewChatPreview,
    createChatMutation, createRemoteChatMutation, createEphemeralChatMutation,
  } = useSessionCreate({ setNewChatError, dispatch, mode, onOpenSlotInNewTab, setRemoteCrewError, setNewChatMenuOpen, onOpenPeerSession })
  // A conductor-lane member anchor opens on the Members page (see renderSessionRow).
  const navigate = useNavigate()

  // Session colors
  const { paletteColors, boost, boostFor, colorMode } = useSessionPalette()

  const {
    startsAutomaticSection, reorderPinnedByKeyboard,
  } = usePinnedKeyboardReorder({ searchRanked, pinned, pinnedOrder, slotFolders, reorderPinned })

  // A row whose session belongs to another page (a crew member's own DM thread,
  // admitted to the conductor lane as a creator anchor) opens where it lives. Read
  // through a ref so the row actions stay ONE stable object across route changes.
  const navigateRef = useRef(navigate)
  navigateRef.current = navigate
  const openElsewhere = useCallback((path: string) => { navigateRef.current(path) }, [])
  // The caller's source-reveal handler is rebuilt whenever its own inputs move,
  // and a new identity here would rebuild `rowActions` and re-render every row.
  // Rows only ever CALL it, so they get a stable forwarder to the latest one;
  // an absent handler stays undefined, which the chips read as "cannot reveal".
  const { onOpenSource: stableOpenSource } = useStableCallbackProps({ onOpenSource })
  // What every session row can do, built once: the row's memo compares this one
  // reference, so none of these may take a new identity per render.
  const rowActions = useMemo(() => ({
    renameInputRef, onRenameStart, onRenameChange, onRenameCommit, onRenameCancel, onAutoTitle,
    onDuplicate: sessionActions.duplicate, onCloseSession: sessionActions.close,
    onMenuCloseAutoFocus, onSelectSlot, onOpenSlotInNewTab, onOpenSource: stableOpenSource,
    onOpenPeerSession: openPeerSession,
    onNativeDragStart: startBoardCardDrag, onNativeDragEnd: endNativeSessionDrag,
    onPinnedKeyboardReorder: reorderPinnedByKeyboard,
    openElsewhere, toggleConductor: toggleConductorExpanded,
  }), [
    renameInputRef, onRenameStart, onRenameChange, onRenameCommit, onRenameCancel, onAutoTitle,
    sessionActions.duplicate, sessionActions.close, onMenuCloseAutoFocus, onSelectSlot, onOpenSlotInNewTab,
    stableOpenSource, openPeerSession, startBoardCardDrag, endNativeSessionDrag, reorderPinnedByKeyboard,
    openElsewhere, toggleConductorExpanded,
  ])
  // The values every row shows the same way, ONE object per change of any of them:
  // each view holds it by reference, so it re-renders every row exactly when one of
  // these moves. The drag members are reduced to primitives first, so a drag-over
  // that rebuilds `activeDrag` with the same session leaves the rows alone.
  const dragInFlight = !!activeDrag
  const activeDraggedKey = activeDrag?.type === 'session' ? activeDrag.id : null
  const activeDraggedPinnedIndex = activeDraggedKey !== null ? (pinnedRank.get(activeDraggedKey) ?? -1) : -1
  const tintAgents = rowAgents ?? installedAgents
  // A live guide step pointing at a row control (`sessions.row-menu`, or a
  // `ui.find` whose opener is it) needs the open row's actions shown with no
  // pointer on the row. Only a step GuideLayer draws here counts (owned by this
  // tab, entered). Read once here so rows re-render only when it flips.
  const guideView = useGuide()?.view
  const guideOnRowControl = guideStepOnRowControl(guideView?.ownedHere && !guideView.needsEnter ? guideView.step : null)
  const rowShell: RowShell = useMemo(() => ({
    connected, mode, defaultAgent, installedAgents: tintAgents, tagById, paletteColors, boost, boostFor, colorMode, recentTintCount,
    // A session row on any touch screen (an iPad too) gets the single ⋯ menu,
    // not the overlay cluster.
    isMobile: isMobile || isTouchDevice,
    dragInFlight, activeDraggedKey, activeDraggedPinnedIndex, guideOnRowControl,
  }), [
    connected, mode, defaultAgent, tintAgents, tagById, paletteColors, boost, boostFor, colorMode, recentTintCount,
    isMobile, isTouchDevice, dragInFlight, activeDraggedKey, activeDraggedPinnedIndex, guideOnRowControl,
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

  const {
    rootFolders, visibleRootFolders, rootFolderIds,
  } = useRootFolderLanes({ folders, folderCompare, isFolderHidden, isFolderFilteredOut })

  // The Older Sessions pane's guide scope (`open:chat.older-sessions`).
  useGuideDisclosureScope('chat.older-sessions', historyOpen)

  // The native confirm() dialogs stay in this file, the one eslint's native-dialog
  // allowlist names; the render owners call these.
  const deleteFolderWithConfirm = (folder: ChatFolder) => { if (confirm(i18nT('pages.chatSidebar.delete_folder_confirm', { name: folder.name }))) deleteFolderMutation.mutate(folder.id) }
  const deleteColumnWithConfirm = (columnId: string) => { if (confirm(i18nT('pages.chatSidebar.delete_this_column'))) deleteColumnMutation.mutate(columnId) }
  const clearClosedSessionsWithConfirm = async () => { if (confirm(i18nT('pages.chatSidebar.clear_closed_sessions_active_tabs_and_pinned_ses'))) { await dispatch(clearHistorySessions()); dispatch(fetchHistory(false)) } }
  const deleteHistorySessionWithConfirm = (key: string) => { if (confirm(i18nT('pages.chatSidebar.are_you_sure_you_want_to_delete_this_history_ses'))) dispatch(deleteHistorySession(key)) }

  // Everything the render owners draw from, built after the last hook call. The
  // factories run once per render, so the row stamps belong to this pass.
  const scope: SidebarScope = { activeDrag, activeFilters, activeModelKeys, activeModelRows, activeSlot,
    activeTagIds, activeTagNames, addColumnAfterMutation, allHiddenFolders, armHold, attachStripEdges,
    autoTitleError, availableLanes, boardError, boardFolderCollapsed, boardLaneActive, boardPeerCount,
    broadcastOpen, bulkModel, changeRecentUnit, chatDropTarget, citedCreatorExists, cleanupOpen,
    clearClosedSessionsWithConfirm, clearHiddenFilters, clearModelFilter, clearTagFilter, closeColumnPopover,
    closeRefused, collapsedCrews, collapsedHistoryGroups, collapsedSpaces, collapsible, columnEditId,
    columnMatches, columnPopoverImeLatch, columnPopoverRef, columnsError, columnsFailed, commitRecentAmount,
    conductorExpanded, conductorMatching, conductorRows, conductorView, connected, createChatInFolder,
    createChatMutation, createEphemeralChatMutation, createFolderMutation, createRemoteChatMutation,
    creatingSlot, crewEnable, crewPreview, cycleLane, defaultAgent, deleteColumnWithConfirm,
    deleteFolderWithConfirm, deleteHistorySessionWithConfirm, derivedHintCol, disarmHold, dismissSortError, dispatch,
    dndSensors, dropSlotMutation, editingId, editName, editScope, filterCounts, filterHiddenFolders,
    filterSortOpen, filtersPaused, flatLaneActive, flatSlots, flatView, folderActionError, folderCleanupOpen, folderOfflineNotice,
    folderCompare, folderConductorPin, folderCreateError, folderDragWithheld, folderEditInputRef,
    folderFilterRows, folderNameMatchIds, folderReorderable, folderReorderHint, folderReorderHintRef,
    folderRowsDrawn, folders, foldersError, foldersFailed, folderSortMode, folderSortMut, folderSortRead,
    foldersShelved, folderSubtrees, foldersWithActiveSubtree, handleSidebarDragCancel, handleSidebarDragEnd,
    handleSidebarDragOver, handleSidebarDragStart, hasRemoteExecutedRow, heldDisplacedRef, heldLane,
    hiddenByContainer, hiddenFilters, hiddenFolderCount, hideEmptyFolderBody, history, historyDragging,
    historyFilter, historyHasMore, historyHeight, historyOpen, historyResize, historySearchResults,
    hoverPinRef, ime, installedAgents, instanceSessions, instantCloseId, isActiveRow, isFolderFilteredOut,
    isFolderHidden, isMobile, isStaleExempt, lane, laneRowWindow, laneScrollMemory, laneSlots,
    laneSwitchLabel, lineage, listNarrowed, localSlots, missingLanes, modelFilterInverted, modelFilterRows,
    moveByDrag, moveFolderTo, nativeSessionDrag, navigate, newChatError, newChatMenuOpen, nextLane,
    onDropSessionRef, onMenuCloseAutoFocus, onOpenSlotInNewTab, onSelectSlot, openCrewMembers,
    openHistoryPane, orderedColumns, paintedSidebarWidth, pinned, popoverPos, queryClient, rawColumns,
    recentAmountDraft, recentUnitDraft, recentWindowMs, refetchColumns, refetchFolders, refetchTags,
    remoteCrewChatPreview, remoteCrewError, remoteCrewErrorId, remoteSessionsError, renameCommit,
    renameError, reorderColumnsMutation, reportDndActive, restorePreBoardWidth, revealedContainers,
    revealFlash, rootFolderIds, rootFolders, rowActions, rowAnimEnabled, rowMotionHeld: !rowAnimSettled, rowViews, searchRanked, seedError,
    seedStateLanesMutation, selectInstance, selectRecentPreset, sessionCleanup, setAllFiltersPaused,
    setAutoTitleError, setBoardError, setCleanupOpen, setCollapsedHistoryGroups, setColumnEditId,
    setDerivedHintCol, setEditingId, setEditName, setEditScope, setFilterSortOpen, setFlatView,
    setFolderActionError, setFolderCleanupOpen, setFolderOfflineNotice, refuseCreateOffline, setFolderCreateError, setFolderModal, setFoldersShelved,
    setHistoryFilter, setHistoryOpen, setLaneScrollEl, setBroadcastOpen, setManageTagsOpen, setModelFilterExcluded,
    setNewChatError, setNewChatMenuOpen, setRecentAmountDraft, setRemoteCrewError, setRenameError,
    setSeedError, setSlotFilter, setSortKey, setStaleCollapseMs, setStaleExpanded, setToggleSaveFailed,
    showAllFolders, shownCrewGroups, shownCrewRowCount, shownCrewRows, sidebarRows, slotFilter, slotFolders,
    sortError, sortKey, staleCollapseMs, staleExpanded, startsAutomaticSection, staticRows, stripEdges,
    subagentApprovalCounts, suppressMenuRestoreRef, tagById, tagColumnsEnabled, tagFilterRows, tagsData,
    tagsQueryFailed, tintAgents, toggleCollapse, toggleColumnCollapse, toggleCrewCollapsed, toggleFilter,
    toggleFolderFilter, toggleFoldersShelved, toggleHideFilter, toggleModelFilter, toggleReveal,
    toggleSaveFailed, toggleSpaceCollapsed, toggleTagFilter, ungroupedSlots, unreadSet, updateColumnMutation,
    updateFolderMutation, visibleRootFolders, warmCrews }
  const laneRows = createLaneRows(scope)
  const folderBlocks = createFolderBlocks({ ...scope, ...laneRows })
  const lanes = createListLanes({ ...scope, ...laneRows, ...folderBlocks })
  const board = createBoardLane({ ...scope, ...laneRows, ...folderBlocks })

  return (
    // stable theming hook 'sidebar' — see website/docs/theming-contract.md
    // The region is the session picker a guide's "choose the session" step
    // points at (`sessions.list`): both layouts draw their rows inside it.
    <div role="region" aria-label={i18nT('pages.chatSidebar.sessions')} {...uiLocation('sessions.list', sidebarRootRef)} data-row-anim={rowAnimSettled ? 'settled' : 'holding'} onPointerOver={onRootPointerOver} onPointerLeave={releaseHoverPin} className={`${LIST_SHELL_CLS} flex flex-col shrink-0 relative h-full`} style={rootStyle}>
      {/* Scroll peek: full titles in a floating, click-through layer. The
          card itself keeps `sidebarWidth`. */}
      <ScrollPeekLayer titles={peekTitles} />
      {/* Outside the row list on purpose: a notice under a row adds a line every session
          pays for in above-fold density (AUTOSDE session-row-fixed-height). */}
      {duplicateError ? (
        <ErrorNotice
          message={duplicateError.message}
          report={duplicateError.report}
          variant="inline"
          askAgent
          onDismiss={clearDuplicateError}
          testId={`sidebar-duplicate-error-${duplicateError.slotKey}`}
          className="mx-1 mt-1 mb-0 min-w-0 flex-wrap shrink-0"
          messageClassName="min-w-0 break-words"
        />
      ) : null}
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
      {renderSidebarHeader(scope)}

      {/* Split View (session grid) has no entry here on purpose: this sidebar is a
       *  navigation surface, and the grid's own affordances live next to the
       *  transcript they act on — the chat header's Columns2 button (⌘D) opens it,
       *  and the header's "in split" badge is the way back into a live split. */}

      {/* Clean Up dialog */}
      {folderCleanupOpen && <FolderCleanupPanel folders={folders} onClose={() => setFolderCleanupOpen(false)} />}
      {cleanupOpen && renderSessionCleanupPanel(scope)}

      {/* Switch-all-to-model dialog — mirrors the Clean Up panel. Picking a
       *  model applies it to every live session; running sessions are skipped
       *  by default. A switch restarts each session's agent process and keeps
       *  its conversation: the next turn resumes it on the new model. The one
       *  thing a switch can destroy is a reply in flight, and only for a
       *  running session left unskipped, so the red caveat renders only once
       *  the skip box is cleared with some session running. */}
      {bulkModel.bulkModelOpen && renderBulkModelPanel(scope)}

      {/* Broadcast dialog — one message steered into every session the pick
       *  reaches: a running turn takes it after its current tool call, an idle
       *  session starts a new turn. Crewmate chats are left out by default. */}
      {broadcastOpen && (
        <BroadcastPanel slots={localSlots} folders={folders} tags={tagsData ?? []} agents={installedAgents} signals={{ subagentStartedCounts, subagentApprovalCounts, workflowActiveSet, automationRunningSet }} onClose={() => setBroadcastOpen(false)} onOpenSession={key => { dispatch(switchSlot({ key, announceOnMissing: true })); onSelectSlot?.(key) }} />
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
      <ListDock {...renderSearchDock(scope)}>
      <LayoutGroup id="chat-slots">
        {conductorLaneActive ? (
          lanes.renderConductorLane()
        ) : flatLaneActive ? (
          lanes.renderFlatLane()
        ) : orderedColumns.length === 0 ? (
          lanes.renderTreeLane()
        ) : (
          board.renderBoardLane()
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
      {renderHistoryPane(scope)}

      {/* One folder create/settings modal for the whole sidebar. Rendered here
       *  rather than per-row so a folder shown in several board columns can only
       *  ever open one, and so the ProjectPicker it hosts has a single owner. */}
      {folderModal && renderFolderModal(scope, folderModal)}
    </div>
  )
}

export default memo(ChatSidebar)
