/** The sidebar shell's interface: the props `ChatSidebar` takes, and the scope it
 *  hands the render owners it composes. */
import type { useConnected } from '../../hooks/useConnected'
import type { useDndSensors } from '../../hooks/useDndSensors'
import type { useImeGuard } from '../../hooks/useImeGuard'
import type { CrewGroup } from '../../hooks/useInstanceSessions'
import type { useIsMobile } from '../../hooks/useIsMobile'
import type { useLaneScrollMemory } from '../../hooks/useLaneScrollMemory'
import type { usePreviewFlag } from '../../hooks/usePreviewFlag'
import type { ScrollEdges } from '../../hooks/useScrollEdges'
import type { RootState, useAppDispatch } from '../../store'
import type { ChatFolder } from '../../types'
import type { SortKey } from '../chat/sessionOrder'
import type { useSessionRowWindowRoot } from '../chat/sessionRowWindow'
import type { useCollapsedCrewSpaces, useCollapsedCrews, useCrewEnable } from './CrewGroups'
import type { SessionRow } from './SessionRow'
import type { useBoardColumnMutations, useBoardColumns, useBoardFolderCollapse, useColumnMatches, useColumnPopover } from './board'
import type { useBulkModelSwitch } from './bulkModelSwitch'
import type { useConductorLane } from './conductor'
import type { useFolderChatCreate, useSessionCreate } from './create'
import type { useFolderDropOps, useNativeSessionDrag, useSidebarDragHandlers, useSidebarMoveUndo } from './dnd/useSidebarDrag'
import type { useSessionFilterState, useSessionStatusFilters } from './filters'
import type { useFolderFilterReveal, useFolderFilterRows, useFolderMutations, useFolderSort, useFolderTree, useFolderVisibility, useRootFolderLanes } from './folders'
import type { useHistoryPane } from './history'
import type { useHoverHold } from './hoverHold'
import type { useLaneCycle, useSidebarLane } from './lanes'
import type { useSidebarModels } from './models'
import type { usePinnedKeyboardReorder, usePinnedSessionOrder } from './pinnedOrder'
import type { useFolderRename, useSessionAutoTitle, useSessionRename } from './rename'
import type { useSidebarResize } from './resize'
import type { useSidebarReveal } from './reveal'
import type { SidebarRows } from './rows'
import type { useSearchMatches } from './search'
import type { useSessionCleanup } from './sessionCleanup'
import type { SessionSortError } from './sessionSort'
import type { useSessionSources } from './sessionSources'
import type { useStaleCollapse } from './stale'
import type { useHoldPinnedHeaderOnCollapse } from './stickyCollapse'
import type { useSidebarTags } from './tags'
import type { AgentInfo, HistoryItem, Slot } from './types'
import type { MemorySourceRow } from '../../lib/agentMemorySource'
import type { UseQueryResult, useQueryClient } from '@tanstack/react-query'
import type { useNavigate } from 'react-router-dom'

export interface ChatSidebarProps {
  slots: Slot[]
  activeSlot: string | null
  unreadSlots: string[]
  history: HistoryItem[]
  historyHasMore: boolean
  defaultAgent: string
  installedAgents: AgentInfo[]
  /** What row and history labels are TINTED from (agent colour, inherited
   *  session colour). The host passes the last loaded roster so a session
   *  switch, which empties `installedAgents` until the new fetch lands, does
   *  not flash every label to muted and back. Display only: pickers such as
   *  FolderConfigModal keep reading the scoped `installedAgents`. Defaults to it. */
  rowAgents?: AgentInfo[]
  /** The agent picker's rows, unfolded (a crewmate and a custom agent of one
   *  name are two rows), as last loaded. A row label whose agent has such a
   *  pair says which memory the session runs on (#19472). Display only.
   *  Absent: no row is marked. */
  rowChoices?: MemorySourceRow[]
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
  onOpenSlotInNewTab?: (key: string, opts?: { background?: boolean; requestSeq?: number }) => void
  /** Reveal a session's pull request / issue in the side panel instead of
   *  leaving for the provider's website.
   *
   *  Fires AFTER the row's own switchSlot dispatch, so the consumer can address
   *  the panel of the session the chip belongs to. Returns whether the panel took
   *  the link: FALSE (or an omitted callback) falls back to plain link
   *  navigation, which is the correct behaviour both on a surface with no side
   *  panel (the `/embed/sessions` list) and for a url the panel cannot resolve. */
  onOpenSource?: (slotKey: string, link: { url: string; kind: 'change' | 'issue' }) => boolean
  /** When true, SessionsSidebarChrome floats a hide-sidebar button over this
   *  header's top-left (open state), so the header reserves left space for it.
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

/** A setter from `useState`. */
type SetState<T> = React.Dispatch<React.SetStateAction<T>>

/** The folder the create / settings modal is open for. */
export type FolderModalTarget = { mode: 'create'; parentId: string } | { mode: 'edit'; folderId: string }

/** What the sidebar shell hands its render owners (./laneRows, ./folderBlocks,
 *  ./listLanes, ./boardLane, ./SidebarHeader, ./SearchDock,
 *  ./HistoryPane, ./folderModal, ./sessionCleanup, ./bulkModelSwitch) for one
 *  render: its props, its own state, what its owner hooks returned and the
 *  callbacks that keep the native confirm dialogs in ChatSidebar.tsx. The shell
 *  builds it after its last hook call; owners read it and call no hooks. */
export interface SidebarScope {
  // Its props
  localSlots: ChatSidebarProps['slots']
  activeSlot: ChatSidebarProps['activeSlot']
  history: ChatSidebarProps['history']
  historyHasMore: ChatSidebarProps['historyHasMore']
  defaultAgent: ChatSidebarProps['defaultAgent']
  installedAgents: ChatSidebarProps['installedAgents']
  onSelectSlot: ChatSidebarProps['onSelectSlot']
  onOpenSlotInNewTab: ChatSidebarProps['onOpenSlotInNewTab']
  collapsible: ChatSidebarProps['collapsible']
  chatDropTarget: ChatSidebarProps['chatDropTarget']
  onDropSessionRef: ChatSidebarProps['onDropSessionRef']
  staticRows: ChatSidebarProps['staticRows']

  // Its own state and what it derives
  seedError: string
  setSeedError: SetState<string>
  toggleSaveFailed: boolean
  setToggleSaveFailed: SetState<boolean>
  boardError: string
  setBoardError: SetState<string>
  folderActionError: string
  setFolderActionError: SetState<string>
  /** An offline refusal: neutral status text, never the failure surface. */
  folderOfflineNotice: string
  setFolderOfflineNotice: SetState<string>
  newChatError: string
  setNewChatError: SetState<string>
  remoteCrewErrorId: string
  remoteCrewError: string
  setRemoteCrewError: SetState<string>
  newChatMenuOpen: boolean
  setNewChatMenuOpen: SetState<boolean>
  slotFilter: string
  setSlotFilter: SetState<string>
  historyFilter: string
  setHistoryFilter: SetState<string>
  hasRemoteExecutedRow: boolean
  warmCrews: { id: string; name: string }[]
  collapsedHistoryGroups: Set<string>
  setCollapsedHistoryGroups: SetState<Set<string>>
  setFolderModal: SetState<FolderModalTarget | null>
  sortKey: SortKey
  /** The sort menu's pick: Manual with nothing saved seeds the order on screen first. */
  setSortKey: (key: SortKey) => void
  /** The last sort pick that did not save (null for none), and its dismiss. */
  sortError: SessionSortError | null
  dismissSortError: () => void
  /** Why the last manual-order write did not take ('' for none), and its setter. */
  manualOrderError: string
  setManualOrderError: (message: string) => void
  flatView: boolean
  conductorView: boolean
  creatingSlot: RootState['chat']['creatingSlot']
  cleanupOpen: boolean
  setCleanupOpen: SetState<boolean>
  folderCleanupOpen: boolean
  setFolderCleanupOpen: SetState<boolean>
  setManageTagsOpen: SetState<boolean>
  broadcastOpen: boolean
  setBroadcastOpen: SetState<boolean>
  filterSortOpen: boolean
  setFilterSortOpen: SetState<boolean>
  folders: ChatFolder[]
  foldersFailed: boolean
  foldersError: Error | null
  refetchFolders: UseQueryResult<ChatFolder[]>['refetch']
  modelFilterInverted: boolean
  shownCrewGroups: CrewGroup[]
  sidebarRows: SidebarRows
  listNarrowed: SidebarRows['listNarrowed']
  laneSlots: SidebarRows['lanes']['main']
  shownCrewRows: SidebarRows['lanes']['crew']
  shownCrewRowCount: SidebarRows['lanes']['crewRowCount']
  flatSlots: SidebarRows['lanes']['flat']
  ungroupedSlots: SidebarRows['lanes']['ungrouped']
  boardPeerCount: SidebarRows['lanes']['boardPeerCount']
  filterCounts: SidebarRows['statusCounts']
  boardLaneActive: boolean
  flatLaneActive: boolean
  folderRowsDrawn: boolean
  setLaneScrollEl: (el: HTMLDivElement | null) => void
  rowAnimEnabled: boolean
  /** True until the lane's first population has painted (see rowAnimSettle.ts). */
  rowMotionHeld: boolean
  activeDrag: { type: string; id: string } | null
  derivedHintCol: string | null
  setDerivedHintCol: SetState<string | null>
  renameCommit: (id: string, name: string) => void
  closeRefused: RootState['chat']['closeRefused']
  rowActions: React.ComponentProps<typeof SessionRow>['actions']
  tintAgents: AgentInfo[]
  rowViews: ReturnType<SidebarRows['views']>
  isActiveRow: ReturnType<SidebarRows['views']>['isActive']

  // The confirming callbacks (the native dialogs stay in ChatSidebar.tsx)
  clearClosedSessionsWithConfirm: () => Promise<void>
  deleteColumnWithConfirm: (columnId: string) => void
  deleteFolderWithConfirm: (folder: ChatFolder) => void
  deleteHistorySessionWithConfirm: (key: string) => void

  // From `useAppDispatch`
  dispatch: ReturnType<typeof useAppDispatch>

  // From `useQueryClient`
  queryClient: ReturnType<typeof useQueryClient>

  // From `useImeGuard`
  ime: ReturnType<typeof useImeGuard>

  // From `useIsMobile`
  isMobile: ReturnType<typeof useIsMobile>

  // From `useSessionSources`
  historySearchResults: ReturnType<typeof useSessionSources>['historySearchResults']
  instanceSessions: ReturnType<typeof useSessionSources>['instanceSessions']
  remoteSessionsError: ReturnType<typeof useSessionSources>['remoteSessionsError']
  selectInstance: ReturnType<typeof useSessionSources>['selectInstance']

  // From `useSessionRename`
  renameError: ReturnType<typeof useSessionRename>['renameError']
  setRenameError: ReturnType<typeof useSessionRename>['setRenameError']
  suppressMenuRestoreRef: ReturnType<typeof useSessionRename>['suppressMenuRestoreRef']
  onMenuCloseAutoFocus: ReturnType<typeof useSessionRename>['onMenuCloseAutoFocus']

  // From `useSessionAutoTitle`
  autoTitleError: ReturnType<typeof useSessionAutoTitle>['autoTitleError']
  setAutoTitleError: ReturnType<typeof useSessionAutoTitle>['setAutoTitleError']

  // From `useSidebarLane`
  lane: ReturnType<typeof useSidebarLane>['lane']
  setFlatView: ReturnType<typeof useSidebarLane>['setFlatView']

  // From `useSessionFilterState`
  activeFilters: ReturnType<typeof useSessionFilterState>['activeFilters']
  filtersPaused: ReturnType<typeof useSessionFilterState>['filtersPaused']
  setAllFiltersPaused: ReturnType<typeof useSessionFilterState>['setAllFiltersPaused']
  filterHiddenFolders: ReturnType<typeof useSessionFilterState>['filterHiddenFolders']
  toggleFolderFilter: ReturnType<typeof useSessionFilterState>['toggleFolderFilter']
  showAllFolders: ReturnType<typeof useSessionFilterState>['showAllFolders']
  toggleTagFilter: ReturnType<typeof useSessionFilterState>['toggleTagFilter']
  clearTagFilter: ReturnType<typeof useSessionFilterState>['clearTagFilter']
  foldersShelved: ReturnType<typeof useSessionFilterState>['foldersShelved']
  setFoldersShelved: ReturnType<typeof useSessionFilterState>['setFoldersShelved']
  toggleFoldersShelved: ReturnType<typeof useSessionFilterState>['toggleFoldersShelved']
  toggleFilter: ReturnType<typeof useSessionFilterState>['toggleFilter']
  hiddenFilters: ReturnType<typeof useSessionFilterState>['hiddenFilters']
  toggleHideFilter: ReturnType<typeof useSessionFilterState>['toggleHideFilter']
  clearHiddenFilters: ReturnType<typeof useSessionFilterState>['clearHiddenFilters']
  toggleModelFilter: ReturnType<typeof useSessionFilterState>['toggleModelFilter']
  setModelFilterExcluded: ReturnType<typeof useSessionFilterState>['setModelFilterExcluded']
  clearModelFilter: ReturnType<typeof useSessionFilterState>['clearModelFilter']

  // From `useSessionStatusFilters`
  subagentApprovalCounts: ReturnType<typeof useSessionStatusFilters>['subagentApprovalCounts']
  unreadSet: ReturnType<typeof useSessionStatusFilters>['unreadSet']
  recentWindowMs: ReturnType<typeof useSessionStatusFilters>['recentWindowMs']
  recentAmountDraft: ReturnType<typeof useSessionStatusFilters>['recentAmountDraft']
  setRecentAmountDraft: ReturnType<typeof useSessionStatusFilters>['setRecentAmountDraft']
  recentUnitDraft: ReturnType<typeof useSessionStatusFilters>['recentUnitDraft']
  selectRecentPreset: ReturnType<typeof useSessionStatusFilters>['selectRecentPreset']
  commitRecentAmount: ReturnType<typeof useSessionStatusFilters>['commitRecentAmount']
  changeRecentUnit: ReturnType<typeof useSessionStatusFilters>['changeRecentUnit']

  // From `useConnected`
  connected: ReturnType<typeof useConnected>

  // From `useHistoryPane`
  historyOpen: ReturnType<typeof useHistoryPane>['historyOpen']
  setHistoryOpen: ReturnType<typeof useHistoryPane>['setHistoryOpen']
  openHistoryPane: ReturnType<typeof useHistoryPane>['openHistoryPane']
  historyHeight: ReturnType<typeof useHistoryPane>['historyHeight']
  historyDragging: ReturnType<typeof useHistoryPane>['historyDragging']
  historyResize: ReturnType<typeof useHistoryPane>['historyResize']

  // From `useSessionCleanup`
  sessionCleanup: ReturnType<typeof useSessionCleanup>

  // From `useBulkModelSwitch`
  bulkModel: ReturnType<typeof useBulkModelSwitch>

  // From `usePinnedSessionOrder`
  pinned: ReturnType<typeof usePinnedSessionOrder>['pinned']
  pinnedOrderError: ReturnType<typeof usePinnedSessionOrder>['pinnedOrderError']
  setPinnedOrderError: ReturnType<typeof usePinnedSessionOrder>['setPinnedOrderError']
  pinnedBandCaption: string | undefined

  // From `useStaleCollapse`
  staleCollapseMs: ReturnType<typeof useStaleCollapse>['staleCollapseMs']
  setStaleCollapseMs: ReturnType<typeof useStaleCollapse>['setStaleCollapseMs']
  staleExpanded: ReturnType<typeof useStaleCollapse>['staleExpanded']
  setStaleExpanded: ReturnType<typeof useStaleCollapse>['setStaleExpanded']
  isStaleExempt: ReturnType<typeof useStaleCollapse>['isStaleExempt']

  // From `useFolderSort`
  folderSortRead: ReturnType<typeof useFolderSort>['folderSortRead']
  folderSortMode: ReturnType<typeof useFolderSort>['folderSortMode']
  folderCompare: ReturnType<typeof useFolderSort>['folderCompare']
  folderReorderable: ReturnType<typeof useFolderSort>['folderReorderable']
  folderDragWithheld: ReturnType<typeof useFolderSort>['folderDragWithheld']
  folderReorderHint: ReturnType<typeof useFolderSort>['folderReorderHint']
  folderReorderHintRef: ReturnType<typeof useFolderSort>['folderReorderHintRef']
  folderSortMut: ReturnType<typeof useFolderSort>['folderSortMut']

  // From `useFolderRename`
  folderEditInputRef: ReturnType<typeof useFolderRename>['folderEditInputRef']
  editingId: ReturnType<typeof useFolderRename>['editingId']
  setEditingId: ReturnType<typeof useFolderRename>['setEditingId']
  editScope: ReturnType<typeof useFolderRename>['editScope']
  setEditScope: ReturnType<typeof useFolderRename>['setEditScope']
  editName: ReturnType<typeof useFolderRename>['editName']
  setEditName: ReturnType<typeof useFolderRename>['setEditName']

  // From `useSidebarTags`
  tagsData: ReturnType<typeof useSidebarTags>['tagsData']
  tagsQueryFailed: ReturnType<typeof useSidebarTags>['tagsQueryFailed']
  refetchTags: ReturnType<typeof useSidebarTags>['refetchTags']
  tagById: ReturnType<typeof useSidebarTags>['tagById']
  activeTagIds: ReturnType<typeof useSidebarTags>['activeTagIds']
  tagFilterRows: ReturnType<typeof useSidebarTags>['tagFilterRows']
  activeTagNames: ReturnType<typeof useSidebarTags>['activeTagNames']

  // From `useSidebarModels`
  modelFilterRows: ReturnType<typeof useSidebarModels>['modelFilterRows']
  activeModelKeys: ReturnType<typeof useSidebarModels>['activeModelKeys']
  activeModelRows: ReturnType<typeof useSidebarModels>['activeModelRows']

  // From `useBoardColumns`
  rawColumns: ReturnType<typeof useBoardColumns>['rawColumns']
  columnsFailed: ReturnType<typeof useBoardColumns>['columnsFailed']
  columnsError: ReturnType<typeof useBoardColumns>['columnsError']
  refetchColumns: ReturnType<typeof useBoardColumns>['refetchColumns']
  tagColumnsEnabled: ReturnType<typeof useBoardColumns>['tagColumnsEnabled']
  hideEmptyFolderBody: ReturnType<typeof useBoardColumns>['hideEmptyFolderBody']
  orderedColumns: ReturnType<typeof useBoardColumns>['orderedColumns']

  // From `useSidebarResize`
  paintedSidebarWidth: ReturnType<typeof useSidebarResize>['paintedSidebarWidth']
  restorePreBoardWidth: ReturnType<typeof useSidebarResize>['restorePreBoardWidth']

  // From `useScrollEdges`
  attachStripEdges: (node: HTMLDivElement | null) => void
  stripEdges: ScrollEdges

  // From `useColumnPopover`
  columnEditId: ReturnType<typeof useColumnPopover>['columnEditId']
  setColumnEditId: ReturnType<typeof useColumnPopover>['setColumnEditId']
  popoverPos: ReturnType<typeof useColumnPopover>['popoverPos']
  columnPopoverRef: ReturnType<typeof useColumnPopover>['columnPopoverRef']
  columnPopoverImeLatch: ReturnType<typeof useColumnPopover>['columnPopoverImeLatch']
  closeColumnPopover: ReturnType<typeof useColumnPopover>['closeColumnPopover']

  // From `useBoardColumnMutations`
  updateColumnMutation: ReturnType<typeof useBoardColumnMutations>['updateColumnMutation']
  reorderColumnsMutation: ReturnType<typeof useBoardColumnMutations>['reorderColumnsMutation']
  addColumnAfterMutation: ReturnType<typeof useBoardColumnMutations>['addColumnAfterMutation']
  dropSlotMutation: ReturnType<typeof useBoardColumnMutations>['dropSlotMutation']
  missingLanes: ReturnType<typeof useBoardColumnMutations>['missingLanes']
  seedStateLanesMutation: ReturnType<typeof useBoardColumnMutations>['seedStateLanesMutation']

  // From `useColumnMatches`
  columnMatches: ReturnType<typeof useColumnMatches>['columnMatches']

  // From `useFolderVisibility`
  slotFolders: ReturnType<typeof useFolderVisibility>['slotFolders']
  foldersWithActiveSubtree: ReturnType<typeof useFolderVisibility>['foldersWithActiveSubtree']
  isFolderHidden: ReturnType<typeof useFolderVisibility>['isFolderHidden']

  // From `useSearchMatches`
  searchRanked: ReturnType<typeof useSearchMatches>['searchRanked']
  folderNameMatchIds: ReturnType<typeof useSearchMatches>['folderNameMatchIds']

  // From `useCollapsedCrews`
  collapsedCrews: ReturnType<typeof useCollapsedCrews>[0]
  toggleCrewCollapsed: ReturnType<typeof useCollapsedCrews>[1]

  // From `useCollapsedCrewSpaces`
  collapsedSpaces: ReturnType<typeof useCollapsedCrewSpaces>[0]
  toggleSpaceCollapsed: ReturnType<typeof useCollapsedCrewSpaces>[1]

  // From `useCrewEnable`
  crewEnable: ReturnType<typeof useCrewEnable>

  // From `useHoverHold`
  hoverPinRef: ReturnType<typeof useHoverHold>['hoverPinRef']
  heldDisplacedRef: ReturnType<typeof useHoverHold>['heldDisplacedRef']
  heldLane: ReturnType<typeof useHoverHold>['heldLane']

  // From `usePreviewFlag`
  folderConductorPin: ReturnType<typeof usePreviewFlag>

  // From `useSessionRowWindowRoot`
  laneRowWindow: ReturnType<typeof useSessionRowWindowRoot>

  // From `useLaneScrollMemory`
  laneScrollMemory: ReturnType<typeof useLaneScrollMemory>

  // From `useFolderFilterReveal`
  isFolderFilteredOut: ReturnType<typeof useFolderFilterReveal>['isFolderFilteredOut']
  revealedContainers: ReturnType<typeof useFolderFilterReveal>['revealedContainers']
  toggleReveal: ReturnType<typeof useFolderFilterReveal>['toggleReveal']
  hiddenByContainer: ReturnType<typeof useFolderFilterReveal>['hiddenByContainer']
  allHiddenFolders: ReturnType<typeof useFolderFilterReveal>['allHiddenFolders']
  hiddenFolderCount: ReturnType<typeof useFolderFilterReveal>['hiddenFolderCount']

  // From `useConductorLane`
  citedCreatorExists: ReturnType<typeof useConductorLane>['citedCreatorExists']
  conductorRows: ReturnType<typeof useConductorLane>['conductorRows']
  conductorMatching: ReturnType<typeof useConductorLane>['conductorMatching']
  lineage: ReturnType<typeof useConductorLane>['lineage']
  conductorExpanded: ReturnType<typeof useConductorLane>['conductorExpanded']

  // From `useLaneCycle`
  availableLanes: ReturnType<typeof useLaneCycle>['availableLanes']
  nextLane: ReturnType<typeof useLaneCycle>['nextLane']
  laneSwitchLabel: ReturnType<typeof useLaneCycle>['laneSwitchLabel']
  cycleLane: ReturnType<typeof useLaneCycle>['cycleLane']

  // From `useFolderFilterRows`
  folderFilterRows: ReturnType<typeof useFolderFilterRows>['folderFilterRows']

  // From `useFolderMutations`
  createFolderMutation: ReturnType<typeof useFolderMutations>['createFolderMutation']
  updateFolderMutation: ReturnType<typeof useFolderMutations>['updateFolderMutation']
  toggleCollapse: ReturnType<typeof useFolderMutations>['toggleCollapse']

  // From `useHoldPinnedHeaderOnCollapse`
  armHold: ReturnType<typeof useHoldPinnedHeaderOnCollapse>['armHold']
  disarmHold: ReturnType<typeof useHoldPinnedHeaderOnCollapse>['disarm']
  instantCloseId: ReturnType<typeof useHoldPinnedHeaderOnCollapse>['instantCloseId']

  // From `useBoardFolderCollapse`
  boardFolderCollapsed: ReturnType<typeof useBoardFolderCollapse>['boardFolderCollapsed']
  toggleColumnCollapse: ReturnType<typeof useBoardFolderCollapse>['toggleColumnCollapse']

  // From `useDndSensors`
  dndSensors: ReturnType<typeof useDndSensors>

  // From `useNativeSessionDrag`
  nativeSessionDrag: ReturnType<typeof useNativeSessionDrag>['nativeSessionDrag']

  // From `useFolderDropOps`
  moveFolderTo: ReturnType<typeof useFolderDropOps>['moveFolderTo']

  // From `useFolderTree`
  folderSubtrees: ReturnType<typeof useFolderTree>['folderSubtrees']

  // From `useSidebarReveal`
  revealFlash: ReturnType<typeof useSidebarReveal>['revealFlash']

  // From `useSidebarMoveUndo`
  moveByDrag: ReturnType<typeof useSidebarMoveUndo>['moveByDrag']

  // From `useSidebarDragHandlers`
  handleSidebarDragStart: ReturnType<typeof useSidebarDragHandlers>['handleSidebarDragStart']
  reportDndActive: ReturnType<typeof useSidebarDragHandlers>['reportDndActive']
  handleSidebarDragEnd: ReturnType<typeof useSidebarDragHandlers>['handleSidebarDragEnd']
  handleSidebarDragCancel: ReturnType<typeof useSidebarDragHandlers>['handleSidebarDragCancel']
  handleSidebarDragOver: ReturnType<typeof useSidebarDragHandlers>['handleSidebarDragOver']

  // From `useFolderChatCreate`
  folderCreateError: ReturnType<typeof useFolderChatCreate>['folderCreateError']
  setFolderCreateError: ReturnType<typeof useFolderChatCreate>['setFolderCreateError']
  createChatInFolder: ReturnType<typeof useFolderChatCreate>['createChatInFolder']
  refuseCreateOffline: ReturnType<typeof useFolderChatCreate>['refuseCreateOffline']

  // From `useSessionCreate`
  crewPreview: ReturnType<typeof useSessionCreate>['crewPreview']
  openCrewMembers: ReturnType<typeof useSessionCreate>['openCrewMembers']
  remoteCrewChatPreview: ReturnType<typeof useSessionCreate>['remoteCrewChatPreview']
  createChatMutation: ReturnType<typeof useSessionCreate>['createChatMutation']
  createRemoteChatMutation: ReturnType<typeof useSessionCreate>['createRemoteChatMutation']
  createEphemeralChatMutation: ReturnType<typeof useSessionCreate>['createEphemeralChatMutation']

  // From `useNavigate`
  navigate: ReturnType<typeof useNavigate>

  // From `usePinnedKeyboardReorder`
  startsAutomaticSection: ReturnType<typeof usePinnedKeyboardReorder>['startsAutomaticSection']

  // From `useRootFolderLanes`
  rootFolders: ReturnType<typeof useRootFolderLanes>['rootFolders']
  visibleRootFolders: ReturnType<typeof useRootFolderLanes>['visibleRootFolders']
  rootFolderIds: ReturnType<typeof useRootFolderLanes>['rootFolderIds']
}
