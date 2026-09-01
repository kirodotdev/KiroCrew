import type React from 'react'
import type { Dispatch, MutableRefObject, RefObject, SetStateAction } from 'react'
import { createPortal } from 'react-dom'
import type { NavigateFunction } from 'react-router-dom'
import { AnimatePresence, motion, type MotionValue } from 'framer-motion'
import { Columns2, ExternalLink, EyeOff, Loader, MessageSquare, Pen, Sparkles, Undo2, VenetianMask, X } from 'lucide-react'

import { api } from '../../api/client'
import { EdgeFade, JumpToBottomButton } from '../../app-sdk/ChatScrollChrome'
import AgentDropdownList, { DefaultAgentRow, ManageAgentsFooter } from '../../components/AgentDropdownList'
import ChatDropOverlay from '../../components/ChatDropOverlay'
import ChatInput from '../../components/ChatInput'
import { ChatTranscriptSkeleton } from '../../components/ChatTranscriptSkeleton'
import Clickable from '../../components/Clickable'
import DetailPanel from '../../components/DetailPanel'
import ErrorNotice from '../../components/ErrorNotice'
import FlyingQuote from '../../components/FlyingQuote'
import FollowUpCard from '../../components/FollowUpCard'
import InboundLinkChip from '../../components/InboundLinkChip'
import InfoTip from '../../components/InfoTip'
import { PanelLeftLight, PanelLeftSolid, PanelRightSolid } from '../../components/icons/panels'
import ModelEffortDropdown from '../../components/ModelEffortDropdown'
import OverlayDrawer from '../../components/OverlayDrawer'
import PendingQuestionCard from '../../components/PendingQuestionCard'
import ProjectPicker from '../../components/ProjectPicker'
import QueueStack, { SubagentDeliveryProgress } from '../../components/QueueStack'
import ReasoningEffortDropdown from '../../components/ReasoningEffortDropdown'
import SearchBar from '../../components/SearchBar'
import SearchResultsList from '../../components/SearchResultsList'
import SessionGridView from '../../components/SessionGridView'
import SessionPulseSurveyCard from '../../components/SessionPulseSurveyCard'
import SessionTabStrip from '../../components/SessionTabStrip'
import { settingsPath } from '../../components/settingsPath'
import SlotTagPopover from '../../components/SlotTagPopover'
import SnipOverlay from '../../components/SnipOverlay'
import { TipCard } from '../../components/TipCard'
import TypewriterText from '../../components/TypewriterText'
import { Btn, EmptyState, Input } from '../../components/ui'
import VoiceDisabledModal from '../../components/VoiceDisabledModal'
import WelcomeView from '../../components/WelcomeView'
import SearchHighlightContext from '../../hooks/SearchHighlightContext'
import type { useImeGuard } from '../../hooks/useImeGuard'
import type { HoverIntent } from '../../hooks/useHoverIntent'
import { SETTINGS_DEFAULT_MODEL_ID } from '../../hooks/useSettingHighlight'
import type { SidePanelDock } from '../../hooks/useSidePanelDock'
import { TagPopoverProvider } from '../../hooks/useTagPopover'
import type { VisualViewport } from '../../hooks/useVisualViewport'
import { voiceInputSupported } from '../../hooks/useVoiceInput'
import { i18nT } from '../../i18n/t'
import { fmtDateFields } from '../../i18n/format'
import { modelSupportsEffort } from '../../lib/effort'
import { JiraHostsCtx } from '../../lib/jiraHosts'
import { pinIsWithheld } from '../../lib/model'
import type { ProviderAdapter } from '../../providers/types'
import type { AppDispatch } from '../../store'
import {
  clearPendingPermissions,
  clearUnresumableResume,
  createSlot,
  deleteSlot,
  requestSlotReveal,
  requestStop,
  switchSlot,
} from '../../store/chatSlice'
import { sseSlotTitle } from '../../store/dashboardSlice'
import { findSurfaceBySlotMode, surfaceLabel } from '../../surfaces/registry'
import type { ChatMessage, ChatSlot, SessionInfo } from '../../types'
import { slotChannelLabel } from '../../utils/channelOrigin'
import { addPendingFile, buildRelMap, normalizeWindowsPath } from '../../utils/fileTokens'
import { isHiddenInvisibleAssistantRow } from '../../utils/invisibleText'
import { MeasureFarm } from '../../hooks/virtualizer/MeasureFarm'
import type { SourceLinkKind } from '../../utils/pullRequestLinks'
import { handleStopPress, isEscalationState } from '../../utils/stopDebounce'
import ChatSidebar from '../ChatSidebar'
import { ChatFooter, PinnedPrompt, PinnedPromptPill } from '.'
import {
  ChatHeaderMenu,
  KnowledgeBubbleChip,
  msgIdentityKey,
} from './ChatPageMessageContent'
import { CONTENT_WIDTH } from './ChatSettings'
import CollapsibleToolGroup from './CollapsibleToolGroup'
import EarlierMessagesBar from './EarlierMessagesBar'
import TranscriptScrollShell from './TranscriptScrollShell'
import FolderSuggestionCard from './FolderSuggestionCard'
import { KnowledgePicker } from './KnowledgePicker'
import { searchScopeIsLimited } from './pagination'
import { pinHidesRow } from '../../utils/pinnedPrompt'
import { RowDisclosureProvider } from './rowDisclosure'
import SessionFlyout, { TOGGLE_RECT } from './SessionFlyout'
import SidePanel from './SidePanel'
import {
  isSidePanelHidden,
  shouldMountSidePanel,
  type SidePanelDockMotion,
} from './sidePanelMount'
import SubagentProgressBar from './SubagentProgressBar'
import TaskProgressBar from './TaskProgressBar'
import TurnBlock from './TurnBlock'
import {
  REFUSED_PRESS_TITLE_KEYS,
  type ChatPageActionsController,
} from './useChatPageActionsController'
import type { ChatPageComposerController } from './useChatPageComposerController'
import type { ChatPageResourcesController } from './useChatPageResourcesController'
import type { useChatPageSessionController } from './useChatPageSessionController'
import type {
  ChatPageTranscriptController,
  ChatPageTranscriptEarlyController,
  PendingJumpOrigin,
} from './useChatPageTranscriptController'
import type { useKnowledgeFetch } from './useKnowledgeFetch'
import WorkflowProgressBar from './WorkflowProgressBar'

/**
 * Sentence for the unresumable-resume notice, built from the raw facts the chat
 * slice records.
 *
 * The slice stores `{ key, title, surface, reason }` rather than a finished
 * string because a reducer cannot localize: the label for a session's origin is
 * derived from its KEY, and that derivation lives at the render site. Keyed on
 * the stored key alone, because the resume being narrated often came from
 * another surface entirely (the command palette, a notification) whose row is
 * nowhere in this page's lists.
 *
 * `reason: 'failed'` gets its own sentence: nothing was resumed, so there is no
 * surface to name, and telling the user it "belongs to" somewhere would be a
 * guess.
 *
 * For `reason: 'surface'` the label is resolved, never interpolated raw. The
 * wire `surface` is a MACHINE value (`member`, `subagent`), so dropping it into
 * localized copy renders lowercase machine vocabulary mid-sentence -- and its
 * empty case reads "it's a Session session". So: the localized dashboard label
 * for a dashboard key, the channel label for a channel key, the surface
 * registry's own label when the mode is a registered surface, and otherwise a
 * sentence that names no surface at all -- and does not say "surface" either,
 * which is vocabulary a user meets only in settings prose.
 *
 * The registry lookup depends on `surfaces/builtins` having been imported (it
 * registers by module side effect, from `App.tsx`), which always holds wherever
 * this page renders. A miss degrades to the surface-free sentence rather than to
 * a wrong label, so the coupling cannot produce a lie.
 *
 * The message keys live in this namespace rather than the sidebar's because the
 * older wording said "from the chat sidebar", which names a surface three of the
 * four resume entry points never touch. The two LABEL keys stay under
 * `pages.chatSidebar.*` because the sidebar's own row still renders them.
 */
function unresumableNoticeMessage(r: { key: string; title: string; surface: string; reason: 'surface' | 'failed' }): string {
  const title = r.title || r.key
  if (r.reason === 'failed') {
    return i18nT('pages.chatPage.could_not_open_this_session', { title })
  }
  const registered = findSurfaceBySlotMode(r.surface)
  const surface = r.key.startsWith('dashboard')
    ? i18nT('pages.chatSidebar.dashboard_source')
    : slotChannelLabel(r.key) || (registered ? surfaceLabel(registered) : '')
  if (!surface) {
    return i18nT('pages.chatPage.this_session_is_not_a_chat_session', { title })
  }
  return i18nT('pages.chatPage.this_session_cannot_be_opened_in_chat', { title, surface })
}

/**
 * Height of the transcript's tail spacer, in px.
 *
 * This plus the scroller's own `paddingBottom` is the clearance between the last
 * line of the transcript and the fade band below it, so it MUST stay >= that
 * band's height (`h-3`, 12px) or the fade slices the last line and the sliced
 * glyphs read as a hairline seam above the composer. It is a px value and not
 * `vh` for exactly that reason: as `2vh` the clearance tracked the viewport and
 * the margin was one pixel at 844px tall, so every shorter viewport — i.e. every
 * phone — landed inside the band.
 */
const TRANSCRIPT_TAIL_SPACER_PX = 16

/**
 * How far the transcript's bottom mask reaches ABOVE the scrollport's bottom edge,
 * in px. This is the part that does the actual feathering, because it is the only
 * part that overlaps readable content, so `TRANSCRIPT_TAIL_SPACER_PX` plus the
 * scroller's own `paddingBottom` must stay >= this or the mask slices the last line
 * when the user is at the bottom.
 */
const TRANSCRIPT_MASK_ABOVE_PX = 16

/**
 * How far that same mask reaches BELOW the scrollport's bottom edge, so it ends
 * flush against the composer box instead of stopping short and leaving a strip
 * where a hairline shows through.
 *
 * It is the exact distance from the scrollport's bottom edge to the top of the
 * composer box, which `ChatInput` owns as two pieces: the `input-area`'s own `pt-1`
 * (4px) plus the composer's top spacer (`h-[6px]`, the box that replaced the
 * pointer-only drag handle). Overshooting FURTHER is not harmless — the mask is
 * `z-[1]` and the composer sits in a later auto-z sibling, so any excess paints over
 * the box's own top border and dims it.
 *
 * That distance only holds while the composer status stack is EMPTY. When any status
 * bar renders, it occupies the strip, so every child of that stack must paint above
 * the mask's `z-[1]` tail. The status-stack contract test pins the child list and
 * their ordering rather than relying on a wrapper stacking context.
 */
const COMPOSER_MASK_OVERSHOOT_PX = 10

/** The mobile sessions drawer deliberately leaves a sliver of conversation visible.
 * Its rendered width and travel must use this one value, or easing continues after
 * the panel has already moved off-screen. */
export const DRAWER_UNCOVERED_PX = 40

export type ChatPageViewActions = Pick<
  ChatPageActionsController,
  | 'activeAgentName'
  | 'approve'
  | 'continuable'
  | 'continuing'
  | 'currentSlot'
  | 'dashCfg'
  | 'dismissApproval'
  | 'dismissFollowup'
  | 'flyingQuote'
  | 'folderSuggestion'
  | 'folderSuggestionAccept'
  | 'folderSuggestionDecline'
  | 'followupAddToSession'
  | 'followUpOptions'
  | 'followUpPicked'
  | 'followUpSourceKey'
  | 'followupStartInWorktree'
  | 'handleCancelQueued'
  | 'handleContinue'
  | 'handleEditQueued'
  | 'handleInterruptQueued'
  | 'handleFollowUpSend'
  | 'handleFollowUpSelect'
  | 'handleReorderQueued'
  | 'inputAreaRef'
  | 'interrupted'
  | 'pendingFollowup'
  | 'pendingQuestion'
  | 'queuedMessages'
  | 'queuePendingIds'
  | 'refusedPress'
  | 'regenerating'
  | 'send'
  | 'setFlyingQuote'
  | 'setProject'
  | 'setRefusedPress'
  | 'steer'
  | 'submitComments'
  | 'switchAgent'
  | 'switchModel'
  | 'systemDeliveryCount'
  | 'toApiDecision'
>

export type ChatPageViewComposer = Pick<
  ChatPageComposerController,
  | 'agentBtnRect'
  | 'agentDropdown'
  | 'agentDropdownRef'
  | 'agentFilter'
  | 'agentInputRef'
  | 'autoNudgeLoop'
  | 'autoNudgeOpen'
  | 'cancelVoice'
  | 'canStageSessionRef'
  | 'chatConfig'
  | 'chatPaneEl'
  | 'defaultAgent'
  | 'defaultAgentFailed'
  | 'effectiveAgents'
  | 'filteredAgents'
  | 'filteredModels'
  | 'historySuggestions'
  | 'input'
  | 'inputRef'
  | 'installedAgents'
  | 'isMac'
  | 'isWelcomeState'
  | 'modelBtnRect'
  | 'modelDropdown'
  | 'modelDropdownRef'
  | 'modelFilter'
  | 'modelInputRef'
  | 'newSessionRef'
  | 'onAgentListKeyDown'
  | 'onModelListKeyDown'
  | 'pasteBlocks'
  | 'pendingAgent'
  | 'pendingDirs'
  | 'pendingFiles'
  | 'pendingModel'
  | 'pendingSessions'
  | 'pickedFileTokens'
  | 'prefillHint'
  | 'projectBtnRect'
  | 'projectPickerOpen'
  | 'reasoningEffortBtnRect'
  | 'reasoningEffortDropdown'
  | 'reasoningEffortDropdownRef'
  | 'remoteCrew'
  | 'resizedInfo'
  | 'setAgentBtnRect'
  | 'setAgentDropdown'
  | 'setAgentFilter'
  | 'setAutoNudgeLoop'
  | 'setAutoNudgeOpen'
  | 'setChatPaneEl'
  | 'setInput'
  | 'setModelBtnRect'
  | 'setModelDropdown'
  | 'setModelFilter'
  | 'setPasteBlocks'
  | 'setPendingFiles'
  | 'setPrefillHint'
  | 'setProjectBtnRect'
  | 'setProjectPickerOpen'
  | 'setReasoningEffortBtnRect'
  | 'setReasoningEffortDropdown'
  | 'setSnipFrame'
  | 'setUploadError'
  | 'setVoiceSetupOpen'
  | 'showHistorySuggestions'
  | 'snipFrame'
  | 'snipSlotRef'
  | 'stageSessionRef'
  | 'startVoice'
  | 'stopVoice'
  | 'sttAvailable'
  | 'sttDictationPanel'
  | 'sttEnabled'
  | 'sttProvider'
  | 'toggleDefaultAgent'
  | 'toggleVoice'
  | 'unstageSessionRef'
  | 'uploadError'
  | 'uploading'
  | 'voice'
  | 'voiceCaretRef'
  | 'voiceOwned'
  | 'voicePendingCaretRef'
  | 'voiceSetupOpen'
>

export type ChatPageViewResources = Pick<
  ChatPageResourcesController,
  | 'addSourceCommentToChat'
  | 'dragOver'
  | 'dropTargetProps'
  | 'handleArtifactOpen'
  | 'handleCapture'
  | 'handleFileOpen'
  | 'handleFileSave'
  | 'handleOptimizeResult'
  | 'hasBrowserTab'
  | 'hasLiveAppTab'
  | 'panelIssues'
  | 'panelSources'
  | 'reconcileIssueUrl'
  | 'reconcileSourceUrl'
  | 'search'
  | 'selectedIssueUrl'
  | 'selectedSourceUrl'
  | 'selectIssueUrl'
  | 'selectSourceUrl'
  | 'tabsCtl'
  | 'uploadFiles'
>

export type ChatPageViewSession = Pick<
  ReturnType<typeof useChatPageSessionController>,
  | 'closeSessionTab'
  | 'handleResumeSession'
  | 'newSlotFailed'
  | 'newSlotMutation'
  | 'openSlotInNewTab'
  | 'ownsSessionTabs'
  | 'selectSessionTab'
  | 'sessionTabs'
  | 'setNewSlotFailed'
  | 'setSidError'
  | 'sidError'
>

export type ChatPageViewTranscriptEarly = Pick<
  ChatPageTranscriptEarlyController,
  | 'composerBandRef'
  | 'handleSurveyLayoutChange'
  | 'onPinCollapsedHeight'
  | 'onScrollPin'
  | 'pinCardRef'
  | 'pinExpanded'
  | 'pinFoldRef'
  | 'pinMinimized'
  | 'pinned'
  | 'scrollBottom'
  | 'scrollerRef'
  | 'scrollToPinnedPrompt'
  | 'setPinExpanded'
  | 'setPinMinimized'
>

export type ChatPageViewTranscript = Pick<
  ChatPageTranscriptController,
  | 'activeSlotTitle'
  | 'chatNav'
  | 'chatPins'
  | 'chatPinsLoading'
  | 'dismissPinStatus'
  | 'displayItems'
  | 'handleJumpToPin'
  | 'handleLoadEarlier'
  | 'handleUnpinById'
  | 'isAtBottom'
  | 'jumpToSearchResult'
  | 'lastRole'
  | 'pinStatus'
  | 'renderFarmItem'
  | 'renderMessage'
  | 'renderTurnItem'
  | 'renderedDisplayItems'
  | 'searchCtxValue'
  | 'spinnerNearTop'
  | 'stableMsgKey'
  | 'streamTick'
  | 'virt'
  | 'virtualKey'
  | 'visibleDisplayItems'
>

export interface ChatPageCoreViewModel {
  _modelPinActive: string
  _modelPinAgent: string
  _modelPinPinned: boolean
  activeSlot: string | null
  activeTip: React.ComponentProps<typeof TipCard>['tip'] | null
  appToolCallIds: ReadonlySet<string>
  completedTurnCount: number
  composerBusy: boolean
  connected: boolean
  contextPct: number
  contextTokens: { used?: number; window: number }
  /** Peer-crew context window for a remote-bound session before its first turn,
   *  0 when the session is local or the peer does not name the picked model. */
  remoteContextWindow: number
  creatingSlot: boolean
  cursorIsForActiveSlot: boolean
  defaultEffort: string
  displayMode: 'trust' | 'trust_reads' | 'yolo' | 'normal'
  effectiveEffort: string
  effectiveMode: string | undefined
  embedded: boolean | undefined
  embedMode: 'chat' | 'sessions' | undefined
  filteredSlots: ChatSlot[]
  generatingTitleSlots: Set<string>
  history: SessionInfo[]
  historyHasMore: boolean
  isMobile: boolean
  jiraSourceHosts: string[]
  kiroCrewVersion: string
  knowledgeFetch: ReturnType<typeof useKnowledgeFetch>
  loadingOlder: boolean
  messages: ChatMessage[]
  mode: string | undefined
  olderFailed: boolean
  popout: boolean | undefined
  projectBranch: string
  projectGit: {
    path: string
    repo: boolean
    repoRoot?: string
    branch?: string
    detached?: boolean
    head?: string
  } | undefined
  /** Working-tree counts for the composer footer badge, already reduced by the
   *  page: 0 stands for "nothing to report", never for "unknown". */
  projectGitAhead: number
  projectGitBehind: number
  projectGitDirty: number
  projectGitError: boolean
  provider: ProviderAdapter
  sentMessages: string[]
  shownModel: string
  slotHasMore: boolean
  slotLoading: boolean
  slotRunning: boolean
  slotState: 'stopping' | 'idle' | 'streaming' | 'tool_running' | 'compacting'
  slotStopping: boolean
  slotSwitchTarget: string | null
  surfaceUnreadSlots: string[]
  title: string
  titleDraft: string
  turnDisclosure: Record<string, boolean>
  unresumableResume: {
    key: string
    title: string
    surface: string
    reason: 'surface' | 'failed'
  } | null
}

export interface ChatPageLayoutViewModel {
  activeIsSplitAnchor: boolean
  activePoppedOut: boolean
  activityOpen: boolean
  activitySlot: HTMLElement | null
  chatContainerRef: RefObject<HTMLDivElement>
  closeSidebar: () => void
  clearSplitOnSelect: () => void
  containerH: number
  drawerDragging: boolean
  drawerMounted: boolean
  drawerPanelRef: RefObject<HTMLDivElement>
  drawerScrim: MotionValue<number>
  drawerScrimRef: RefObject<HTMLDivElement>
  drawerX: MotionValue<number>
  effectiveSidebarWidth: number
  enterSplit: (anchor: string | null) => void
  expandFrom: { x: number; y: number; w: number; h: number } | null
  expandSidebar: (fromFlyout: boolean) => void
  flyout: HoverIntent
  flyoutEligible: boolean
  flyoutNew: () => void
  flyoutSurfaceRef: RefObject<HTMLDivElement>
  flyoutSwitch: (key: string) => void
  flyoutTriggerRef: RefObject<HTMLButtonElement>
  focusActivePopout: (id: string) => void
  inlineSidePanelShowing: boolean
  /** Height of the band an interactive widget (a software keyboard) covers at the
   *  bottom of the fixed positioning viewport. 0 at rest, so the two consumers
   *  below are strict no-ops until a keyboard is up. */
  keyboardInset: number
  mobileSessions: boolean
  navigateToEmbeddedSlot: (key: string) => void
  openActivePopout: (id: string, title?: string) => void
  openSidebar: () => void
  panelFillWidth: number | undefined
  panelMaximized: boolean
  panelReserve: number | undefined
  returnSelfToMain: () => void
  setSidebarDragging: Dispatch<SetStateAction<boolean>>
  setSidebarPinned: Dispatch<SetStateAction<boolean>>
  setSidebarWidth: Dispatch<SetStateAction<number>>
  setSplitMode: Dispatch<SetStateAction<boolean>>
  sidebarAutoHidden: MutableRefObject<boolean | null>
  sidebarDragging: boolean
  sidebarOpen: boolean
  sidebarPinned: boolean
  sidePanelDock: SidePanelDock
  sidePanelDockAnim: SidePanelDockMotion
  sidePanelWantsMount: boolean
  sideOverlayPanelRef: RefObject<HTMLDivElement>
  sideOverlayPhase: 'closed' | 'open' | 'closing'
  sideOverlayX: MotionValue<number>
  splitAnchor: string | null
  splitAnchorForActive: string | null
  splitFeatureEnabled: boolean
  splitMode: boolean
  toggleAct: () => void
  /** The visible part of the window, so the mobile drawer and its scrim can be
   *  inset to it rather than to the layout viewport. See `useVisualViewport`. */
  vv: VisualViewport
  winW: number
}

export interface ChatPageViewPorts {
  cancelTitleRef: MutableRefObject<boolean>
  dismissTip: () => void
  dispatch: AppDispatch
  editingTitle: boolean
  handleAddToContext: (absPath: string, kind: 'file' | 'dir') => void
  navigate: NavigateFunction
  pinModelToAgentMut: {
    mutate: (variables: { agent: string; model: string }) => void
  }
  revealSourceLink: (
    slot: string,
    chip: { url: string; kind: SourceLinkKind },
  ) => boolean
  setEditingTitleSlot: Dispatch<SetStateAction<string | null>>
  setGeneratingTitleSlots: Dispatch<SetStateAction<Set<string>>>
  setPendingPinnedJump: Dispatch<SetStateAction<{
    slotKey: string
    messageTs: string
    mid?: string
    origin: PendingJumpOrigin
  } | null>>
  setTitleDraft: Dispatch<SetStateAction<string>>
  setTurnDisclosureFor: (key: string, expanded: boolean) => void
  softStopAtMapRef: MutableRefObject<Map<string, number>>
  titleIme: ReturnType<typeof useImeGuard>
}

export interface ChatPageViewProps {
  actions: ChatPageViewActions
  composer: ChatPageViewComposer
  layout: ChatPageLayoutViewModel
  page: ChatPageCoreViewModel
  ports: ChatPageViewPorts
  resources: ChatPageViewResources
  session: ChatPageViewSession
  transcript: ChatPageViewTranscript
  transcriptEarly: ChatPageViewTranscriptEarly
}

export default function ChatPageView({
  actions,
  composer,
  layout,
  page,
  ports,
  resources,
  session,
  transcript,
  transcriptEarly,
}: ChatPageViewProps) {
  const {
    activeAgentName,
    approve,
    continuable,
    continuing,
    currentSlot,
    dashCfg,
    dismissApproval,
    dismissFollowup,
    flyingQuote,
    folderSuggestion,
    folderSuggestionAccept,
    folderSuggestionDecline,
    followupAddToSession,
    followUpOptions,
    followUpPicked,
    followUpSourceKey,
    followupStartInWorktree,
    handleCancelQueued,
    handleContinue,
    handleEditQueued,
    handleInterruptQueued,
    handleFollowUpSend,
    handleFollowUpSelect,
    handleReorderQueued,
    inputAreaRef,
    interrupted,
    pendingFollowup,
    pendingQuestion,
    queuedMessages,
    queuePendingIds,
    refusedPress,
    regenerating,
    send,
    setFlyingQuote,
    setProject,
    setRefusedPress,
    steer,
    submitComments,
    switchAgent,
    switchModel,
    systemDeliveryCount,
    toApiDecision,
  } = actions
  const {
    agentBtnRect,
    agentDropdown,
    agentDropdownRef,
    agentFilter,
    agentInputRef,
    autoNudgeLoop,
    autoNudgeOpen,
    cancelVoice,
    canStageSessionRef,
    chatConfig,
    chatPaneEl,
    defaultAgent,
    defaultAgentFailed,
    filteredAgents,
    filteredModels,
    historySuggestions,
    input,
    inputRef,
    installedAgents,
    effectiveAgents,
    remoteCrew,
    isMac,
    isWelcomeState,
    modelBtnRect,
    modelDropdown,
    modelDropdownRef,
    modelFilter,
    modelInputRef,
    newSessionRef,
    onAgentListKeyDown,
    onModelListKeyDown,
    pasteBlocks,
    pendingAgent,
    pendingDirs,
    pendingFiles,
    pendingModel,
    pendingSessions,
    pickedFileTokens,
    prefillHint,
    projectBtnRect,
    projectPickerOpen,
    reasoningEffortBtnRect,
    reasoningEffortDropdown,
    reasoningEffortDropdownRef,
    resizedInfo,
    setAgentBtnRect,
    setAgentDropdown,
    setAgentFilter,
    setAutoNudgeLoop,
    setAutoNudgeOpen,
    setChatPaneEl,
    setInput,
    setModelBtnRect,
    setModelDropdown,
    setModelFilter,
    setPasteBlocks,
    setPendingFiles,
    setPrefillHint,
    setProjectBtnRect,
    setProjectPickerOpen,
    setReasoningEffortBtnRect,
    setReasoningEffortDropdown,
    setSnipFrame,
    setUploadError,
    setVoiceSetupOpen,
    showHistorySuggestions,
    snipFrame,
    snipSlotRef,
    stageSessionRef,
    startVoice,
    stopVoice,
    sttAvailable,
    sttDictationPanel,
    sttEnabled,
    sttProvider,
    toggleDefaultAgent,
    toggleVoice,
    unstageSessionRef,
    uploadError,
    uploading,
    voice,
    voiceCaretRef,
    voiceOwned,
    voicePendingCaretRef,
    voiceSetupOpen,
  } = composer
  const {
    activeIsSplitAnchor,
    activePoppedOut,
    activityOpen,
    activitySlot,
    chatContainerRef,
    closeSidebar,
    clearSplitOnSelect,
    containerH,
    drawerDragging,
    drawerMounted,
    drawerPanelRef,
    drawerScrim,
    drawerScrimRef,
    drawerX,
    effectiveSidebarWidth,
    enterSplit,
    expandFrom,
    expandSidebar,
    flyout,
    flyoutEligible,
    flyoutNew,
    flyoutSurfaceRef,
    flyoutSwitch,
    flyoutTriggerRef,
    focusActivePopout,
    inlineSidePanelShowing,
    keyboardInset,
    mobileSessions,
    navigateToEmbeddedSlot,
    openActivePopout,
    openSidebar,
    panelFillWidth,
    panelMaximized,
    panelReserve,
    returnSelfToMain,
    setSidebarDragging,
    setSidebarPinned,
    setSidebarWidth,
    setSplitMode,
    sidebarAutoHidden,
    sidebarDragging,
    sidebarOpen,
    sidebarPinned,
    sidePanelDock,
    sidePanelDockAnim,
    sidePanelWantsMount,
    sideOverlayPanelRef,
    sideOverlayPhase,
    sideOverlayX,
    splitAnchor,
    splitAnchorForActive,
    splitFeatureEnabled,
    splitMode,
    toggleAct,
    vv,
    winW,
  } = layout
  const {
    _modelPinActive,
    _modelPinAgent,
    _modelPinPinned,
    activeSlot,
    activeTip,
    appToolCallIds,
    completedTurnCount,
    composerBusy,
    connected,
    contextPct,
    contextTokens,
    remoteContextWindow,
    creatingSlot,
    cursorIsForActiveSlot,
    defaultEffort,
    displayMode,
    effectiveEffort,
    effectiveMode,
    embedded,
    embedMode,
    filteredSlots,
    generatingTitleSlots,
    history,
    historyHasMore,
    isMobile,
    jiraSourceHosts,
    kiroCrewVersion,
    knowledgeFetch,
    loadingOlder,
    messages,
    mode,
    olderFailed,
    popout,
    projectBranch,
    projectGit,
    projectGitAhead,
    projectGitBehind,
    projectGitDirty,
    projectGitError,
    provider,
    sentMessages,
    shownModel,
    slotHasMore,
    slotLoading,
    slotRunning,
    slotState,
    slotStopping,
    slotSwitchTarget,
    surfaceUnreadSlots,
    title,
    titleDraft,
    turnDisclosure,
    unresumableResume,
  } = page
  const {
    cancelTitleRef,
    dismissTip,
    dispatch,
    editingTitle,
    handleAddToContext,
    navigate,
    pinModelToAgentMut,
    revealSourceLink,
    setEditingTitleSlot,
    setGeneratingTitleSlots,
    setPendingPinnedJump,
    setTitleDraft,
    setTurnDisclosureFor,
    softStopAtMapRef,
    titleIme,
  } = ports
  const {
    addSourceCommentToChat,
    dragOver,
    dropTargetProps,
    handleArtifactOpen,
    handleCapture,
    handleFileOpen,
    handleFileSave,
    handleOptimizeResult,
    hasBrowserTab,
    hasLiveAppTab,
    panelIssues,
    panelSources,
    reconcileIssueUrl,
    reconcileSourceUrl,
    search,
    selectedIssueUrl,
    selectedSourceUrl,
    selectIssueUrl,
    selectSourceUrl,
    tabsCtl,
    uploadFiles,
  } = resources
  const {
    closeSessionTab,
    handleResumeSession,
    newSlotFailed,
    newSlotMutation,
    openSlotInNewTab,
    ownsSessionTabs,
    selectSessionTab,
    sessionTabs,
    setNewSlotFailed,
    setSidError,
    sidError,
  } = session
  const {
    activeSlotTitle,
    chatNav,
    chatPins,
    chatPinsLoading,
    dismissPinStatus,
    displayItems,
    renderedDisplayItems,
    spinnerNearTop,
    renderFarmItem,
    virtualKey,
    handleJumpToPin,
    handleLoadEarlier,
    handleUnpinById,
    isAtBottom,
    jumpToSearchResult,
    lastRole,
    pinStatus,
    renderMessage,
    renderTurnItem,
    searchCtxValue,
    stableMsgKey,
    streamTick,
    virt,
    visibleDisplayItems,
  } = transcript
  const {
    composerBandRef,
    handleSurveyLayoutChange,
    onPinCollapsedHeight,
    onScrollPin,
    pinCardRef,
    pinExpanded,
    pinFoldRef,
    pinMinimized,
    pinned,
    scrollBottom,
    scrollerRef,
    scrollToPinnedPrompt,
    setPinExpanded,
    setPinMinimized,
  } = transcriptEarly

  return (
    <RowDisclosureProvider resetKey={activeSlot}>
    <TagPopoverProvider>
    {/* Self-hosted Jira allowlist for every markdown anchor in the page --
        message bodies, previews, and panels alike -- so a pasted Jira URL
        chips identically wherever it renders. Cloud URLs need no provider. */}
    <JiraHostsCtx.Provider value={jiraSourceHosts}>
    <div
      ref={chatContainerRef}
      // The app-wide nav drawer also listens at this surface. Claim both sides
      // here so its gesture cannot arm alongside either ChatPage drawer.
      data-owns-swipe={embedded ? undefined : 'left right'}
      className="flex flex-1 min-h-0 h-full overflow-hidden relative"
    >
      <AnimatePresence>
        {isMobile && drawerMounted && (
          <motion.div
            key="sessions-backdrop"
            data-testid="sessions-backdrop"
            className="fixed inset-0 z-[46] bg-black/50 backdrop-blur-sm"
            // ^ Kept adjacent to `key`: the composer-chrome occlusion guard
            // anchors its z-order regex on that proximity, so anything inserted
            // between the two puts `z-[46]` out of its reach and silently turns
            // that file's ordering assertions into comparisons against
            // `undefined`.
            ref={drawerScrimRef}
            // The margins inset this fixed box to the VISIBLE band: inset-0 is
            // the whole layout viewport, which a keyboard shrinks on Chromium
            // but not on iOS Safari, so an unmodified scrim keeps its full
            // height there and the drawer's lower half sits behind the keyboard
            // with nothing dimmed under it. Insetting rather than restating the
            // edges is what lets every safe-area class keep owning its own edge,
            // here and on the panel below.
            style={{ opacity: drawerScrim, marginTop: vv.offsetTop, marginBottom: keyboardInset }}
            // Ignored while a drag owns the panel: the release that ends a
            // close gesture lands here as a click, and treating it as a
            // tap-to-dismiss would run a second close over the settle.
            onClick={() => { if (!drawerDragging) closeSidebar() }}
          />
        )}
      </AnimatePresence>
      {/* Sidebar toggle — absolute in the stable container in BOTH states
          (only the icon flips), so collapsing cannot drag it sideways with
          the reflowing content pane. The collapse/expand motion itself is the
          panel deforming into/out of this button's rect (OverlayDrawer morph
          mode, morphTarget below). Desktop, non-embed, with sessions only.
          While collapsed, hovering it opens the recents flyout below; clicking
          hands that flyout's rect to the drawer so the panel grows out of it. */}
      {!isMobile && embedMode !== 'chat' && embedMode !== 'sessions' && filteredSlots.length > 0 && (
        <button
          ref={flyoutTriggerRef}
          type="button"
          onClick={() => expandSidebar(flyout.open)}
          {...flyout.triggerProps}
          aria-haspopup={flyoutEligible ? 'menu' : undefined}
          aria-expanded={flyoutEligible ? flyout.open : undefined}
          // Geometry mirrored by TOGGLE_RECT (chat/SessionFlyout) — every
          // surface in this interaction grows out of and back into this rect.
          className="pi-morph absolute top-[9px] left-2 z-[61] w-7 h-7 rounded-md flex items-center justify-center cursor-pointer text-muted hover:text-text hover:bg-bg-hover transition-colors bg-transparent border-none"
          title={sidebarOpen ? i18nT('pages.chatPage.hide_sessions') : i18nT('pages.chatPage.show_sessions')}
          aria-label={sidebarOpen ? i18nT('pages.chatPage.hide_sessions_sidebar') : i18nT('pages.chatPage.show_sessions_sidebar')}
        >
          {sidebarOpen ? <PanelLeftLight size={16} /> : <PanelLeftSolid size={16} />}
        </button>
      )}
      <AnimatePresence>
        {flyoutEligible && flyout.open && (
          <SessionFlyout
            key="session-flyout"
            ref={flyoutSurfaceRef}
            slots={filteredSlots}
            activeSlot={activeSlot}
            unreadSlots={surfaceUnreadSlots}
            panelWidth={effectiveSidebarWidth}
            // The panel's own height (OverlayDrawer carries pb-2), so the
            // flyout can never be taller than the thing it grows into.
            maxHeight={Math.max(0, containerH - 8)}
            connected={connected}
            creating={creatingSlot}
            autoFocus={flyout.openedBy === 'keyboard'}
            onSwitch={flyoutSwitch}
            onNew={flyoutNew}
            onExpand={() => expandSidebar(true)}
            onDismiss={() => { flyout.close(); flyoutTriggerRef.current?.focus() }}
            onMouseEnter={flyout.surfaceProps.onMouseEnter}
            onMouseLeave={flyout.surfaceProps.onMouseLeave}
            onBlur={flyout.surfaceProps.onBlur}
          />
        )}
      </AnimatePresence>
      {embedMode === 'chat' ? null : embedMode === 'sessions' ? (
        <div className="flex-1 min-w-0 h-full overflow-hidden [&_.sidebar-inner]:!w-full [&_.sidebar-inner]:!border-0 [&_.sidebar-inner]:!rounded-none [&_.sidebar-inner]:!shrink [&_.sidebar-inner]:!bg-bg [&_.sidebar-resize-handle]:!hidden">
          <ChatSidebar
            slots={filteredSlots}
            activeSlot={null}
            unreadSlots={surfaceUnreadSlots}
            history={history}
            historyHasMore={historyHasMore}
            defaultAgent={defaultAgent}
            installedAgents={installedAgents}
            mode={mode}
            onWidthChange={setSidebarWidth}
            onDragChange={setSidebarDragging}
            onSelectSlot={navigateToEmbeddedSlot}
          />
        </div>
      ) : (
      <OverlayDrawer open={isMobile ? drawerMounted : sidebarOpen} width={isMobile ? Math.max(0, winW - DRAWER_UNCOVERED_PX) : effectiveSidebarWidth} dragging={sidebarDragging} slideX={isMobile ? drawerX : undefined} slideRef={drawerPanelRef}
        // Mobile only: the same visible-band inset the scrim above carries, so the
        // two surfaces move together. Margins rather than restated edges is what
        // matters here: top-safe-offset-[42px] and bottom-safe compose env() insets
        // no script can read, so the panel could not express its own top/height in
        // JS without a calc(env(…)) string. With both edges pinned and height auto
        // the box is over-constrained on the block axis, so the used height is the
        // viewport minus both safe insets AND both margins — the top edge lands at
        // the visible top and the bottom edge just above the keyboard. Both terms
        // are 0 at rest, so the resting panel is unchanged; and where the layout
        // viewport window.innerHeight reports disagrees with the fixed positioning
        // viewport, the bottom margin merely under-insets — today's behaviour.
        //
        // Horizontal channels are untouched: left-safe stays on the className and
        // OverlayDrawer applies width and the x slide AFTER this style, so neither
        // can be overridden from here.
        slideStyle={isMobile ? { marginTop: vv.offsetTop, marginBottom: keyboardInset } : undefined}
        morph={!isMobile} morphTarget={TOGGLE_RECT} expandFrom={expandFrom} contentH={Math.max(0, containerH - 8)} className={isMobile ? 'mobile-sessions-overlay fixed top-safe-offset-[42px] bottom-safe left-safe z-50 bg-bg-elevated !py-0 rounded-r-xl shadow-lg [&>*]:!rounded-none [&>*]:!border-0 [&>*]:!m-0' : ''}>
        <ChatSidebar
          slots={filteredSlots}
          activeSlot={activeSlot}
          unreadSlots={surfaceUnreadSlots}
          history={history}
          historyHasMore={historyHasMore}
          defaultAgent={defaultAgent}
          installedAgents={installedAgents}
          mode={mode}
          onWidthChange={setSidebarWidth}
          onDragChange={setSidebarDragging}
          collapsible={!isMobile}
          staticRows={isMobile}
          onSelectSlot={clearSplitOnSelect}
          onOpenSlotInNewTab={ownsSessionTabs ? openSlotInNewTab : undefined}
          onOpenSource={revealSourceLink}
          // Only offer the pane as a drop target when a composer exists to show
          // the chip — see canStageSessionRef for why this is a named predicate.
          chatDropTarget={canStageSessionRef ? chatPaneEl : null}
          onDropSessionRef={stageSessionRef}
        />
      </OverlayDrawer>
      )}

      {/* Per-slot tag picker — a single connected popover, opened from any session
          menu (sidebar row or header) via the ChatPage-scoped TagPopover context. */}
      <SlotTagPopover />

      {/* Chat pane */}
      {embedMode !== 'sessions' && (
      <div ref={setChatPaneEl} className={`relative flex flex-col bg-bg min-w-0 min-h-0 h-full overflow-hidden ${(activityOpen && !activitySlot) || search.isOpen ? 'flex-[1_1_60%]' : 'flex-1'}`} style={{ transition: 'flex 0.2s', ...(!sidebarOpen && !isMobile ? { marginLeft: '-0.5rem' } : {}), '--mc-content-width': CONTENT_WIDTH[chatConfig.contentWidth].messages, '--mc-input-width': CONTENT_WIDTH[chatConfig.contentWidth].input } as React.CSSProperties}>
        {snipFrame && (
          <SnipOverlay
            frame={snipFrame}
            onComplete={f => { uploadFiles([f], snipSlotRef.current); setSnipFrame(null) }}
            onCancel={() => setSnipFrame(null)}
            onError={setUploadError}
          />
        )}
        {uploadError && (
          <div className="mx-4 mt-2 mb-0 bg-bg-elevated border rounded-lg p-3 flex items-center gap-3 animate-rise" style={{ borderColor: 'color-mix(in srgb, var(--danger) 45%, transparent)' }}>
            <span className="text-sm text-text flex-1">{uploadError}</span>
            <button onClick={() => setUploadError('')} aria-label={i18nT('pages.chatPage.dismiss_upload_error')} className="text-muted hover:text-text text-lg leading-none">&times;</button>
          </div>
        )}
        {sidError && (
          <div className="mx-4 mt-2 mb-0 bg-bg-elevated border rounded-lg p-3 flex items-center gap-3 animate-rise" style={{ borderColor: 'color-mix(in srgb, var(--warn) 45%, transparent)' }}>
            <span className="text-sm text-text flex-1">{sidError}</span>
            <button onClick={() => setSidError('')} aria-label={i18nT('pages.chatPage.dismiss_error')} className="text-muted hover:text-text text-lg leading-none">&times;</button>
          </div>
        )}
        {pinStatus && (
          <div role="status" className="mx-4 mt-2 mb-0 bg-bg-elevated border rounded-lg p-3 flex items-center gap-3 animate-rise" style={{ borderColor: 'color-mix(in srgb, var(--warn) 45%, transparent)' }}>
            <span className="text-sm text-text flex-1">{pinStatus}</span>
            <button onClick={dismissPinStatus} aria-label={i18nT('app.dismiss')} className="text-muted hover:text-text leading-none p-0.5"><X className="w-4 h-4" /></button>
          </div>
        )}
        {/* Every resume entry point converges here: the sidebar row, this page's
            own "Continue a previous chat" list, the notification panel's Resume
            button and the two command-palette providers all end on /chat — and
            the two providers are plain modules with no component of their own,
            so one shared site is what lets them narrate at all.

            It sits with the pane-level banners above, OUTSIDE the
            split / no-slot / transcript ternary below, because a resume can land
            here with NO active slot at all (a palette or notification resume
            while no tab is open) — and that ternary's `!activeSlot` branch
            renders only the empty state, so a notice placed inside the transcript
            branch was silent in exactly that case.

            Deliberately NOT in the sidebar: that pane's Older Sessions section
            starts closed, so a notice inside it is invisible to anyone who had
            not already opened it, which is everyone arriving from the other
            three paths. */}
        {unresumableResume && (
          <div className="mx-4 mt-2 mb-0" data-testid="unresumable-resume-error">
            <ErrorNotice
              message={unresumableNoticeMessage(unresumableResume)}
              onDismiss={() => dispatch(clearUnresumableResume())}
              variant="block"
            />
          </div>
        )}
        {/* Floating sessions opener — mobile only, and only on a chat with
            nothing in it yet (a conversation gets the in-header control
            instead). Suppressed while the inline side panel is showing: it is
            `fixed` at the same top-left corner as the panel's own collapse
            button and, carrying z-10 against that button's auto z-index, paints
            OVER it — leaving no way to close a panel that covers the whole
            screen. It would also be pointing at a chat pane the panel has
            squeezed to zero width. Sessions stay reachable meanwhile via the
            left-edge drag (useDrawerSwipe above).

            Suppressed when EMBEDDED for the same reason it is suppressed
            behind the side panel: `fixed` anchors it to the VIEWPORT, not to
            the host's pane, so it lands on whatever the host put in that
            corner -- in Papyrus, on the toolbar's back button, giving two
            overlapping tap targets on the app's primary exit. A host that
            embeds one scoped conversation has no sessions list to open. */}
        {isMobile && !embedded && !sidebarOpen && !inlineSidePanelShowing && !(activeSlot && (messages.length > 0 || slotRunning)) && (
          <div className="fixed top-safe-offset-[42px] left-safe ml-2 z-10">
            <button className="p-2 rounded-lg text-muted hover:text-text bg-bg-elevated border border-border shadow-sm cursor-pointer" onClick={openSidebar} aria-label={i18nT('pages.chatPage.toggle_sessions')}>
              {/* Same glyph as the desktop toggle: a control is named by the SURFACE
                  it opens, and this opens the sessions panel. Solid rather than
                  `PanelLeftLight` because this form only renders while that panel is
                  closed. It carries no conversation-mode variant -- mode belongs to
                  the conversation, not to the drawer, and the header's own mode
                  control already shows it. */}
              <PanelLeftSolid size={18} />
            </button>
          </div>
        )}
        {/* Open-sessions strip. ABOVE the session title row, not inside the
            transcript column: the title row is an absolute overlay anchored to
            that column, so a strip inserted inside it would be painted over.
            Sitting here it pushes the whole column down instead, and the
            transcript (flex: 1) gives up exactly the strip's height.

            Renders nothing below two tabs (see SessionTabStrip), so a user who
            never opens a second tab sees the surface unchanged. Suppressed in
            split view, which does its own tiling and shows every open session
            at once, and on every EMBEDDED host (`ownsSessionTabs`) — the same
            predicate that stops those hosts owning the persisted set, so the
            strip and the set can never disagree about whose surface this is. */}
        {activeSlot && ownsSessionTabs && !(splitMode && splitFeatureEnabled) && (
          // no-drag: on the desktop shell the top strip of the window is the
          // titlebar drag region, and a tab you cannot click is worse than no tab.
          <div style={{ WebkitAppRegion: 'no-drag' } as React.CSSProperties}>
            <SessionTabStrip
              tabs={sessionTabs.tabs}
              activeKey={activeSlot}
              cue={sessionTabs.cue}
              connected={connected}
              onSelect={selectSessionTab}
              onClose={closeSessionTab}
            />
          </div>
        )}
        {splitMode && splitFeatureEnabled ? (
          <SessionGridView
            seedSlot={splitAnchor ?? activeSlot}
            onClose={() => setSplitMode(false)}
            onCollapse={(slot, anchorTs, anchorMid) => {
              dispatch(switchSlot(slot))
              setSplitMode(false)
              // switchSlot.pending sets activeSlot synchronously, so the pending-jump
              // effect pages back to the anchor instead of landing on the newest turn.
              if (anchorTs) setPendingPinnedJump({ slotKey: slot, messageTs: anchorTs, mid: anchorMid, origin: 'earlier' })
            }}
          />
        ) : !activeSlot ? (
          <div className="flex-1 flex flex-col items-center justify-center gap-4 px-8">
            <EmptyState icon={<MessageSquare className="lucide-inline" />} title={i18nT('pages.chatPage.what_can_i_do_for_you')} subtitle={i18nT('pages.chatPage.start_a_new_chat_to_begin')} />
            <Btn
              primary
              disabled={newSlotMutation.isPending}
              onClick={() => {
                if (newSlotFailed) {
                  // Re-arm before state updates can let auto-selection run.
                  newSessionRef.current = true
                  setNewSlotFailed(false)
                  setSidError('')
                  newSlotMutation.mutate()
                  return
                }
                dispatch(createSlot({ agent: pendingAgent || defaultAgent || undefined, model: pendingModel || undefined, mode }))
              }}
            >
              {i18nT('pages.chatPage.start_a_new_chat')}
            </Btn>
          </div>
        ) : (
          <SearchHighlightContext.Provider value={searchCtxValue}>
          <div className="relative flex flex-col flex-1 min-h-0" {...dropTargetProps}>
            {/* Claude-style title row — absolute overlay, solid top fading to transparent.
                Inset on the right by the 6px scrollbar width (see ::-webkit-scrollbar
                in index.css) so the overlay never paints over the scroller's scrollbar
                track — otherwise the thumb is hidden/un-grabbable when scrolled to top. */}
            {/* z-[45] at rest keeps this row BELOW the mobile drawer scrim
                (z-[46]) so an open sessions drawer dims it. While it hosts the
                rename editor it lifts to z-[47], above the composer status bars
                (z-[46]): those are flex-flow chrome, not overlays, and they rise
                into this band once the transcript scroller collapses to zero —
                what a phone keyboard does — where they painted over the caret.
                Scoping the lift to the edit is safe because opening the drawer
                blurs the input, which commits and closes the editor. */}
            <div className={`absolute top-0 left-0 right-1.5 ${editingTitle ? 'z-[47]' : 'z-[45]'} pointer-events-none`} style={{ WebkitAppRegion: 'no-drag' } as React.CSSProperties}>
              {/* The row's left padding GLIDES between its open (20px) and
                  collapsed (60px, clearing the stationary toggle + divider)
                  values on the same 320ms curve as the panel — an instant
                  class flip here reads as the title jumping sideways at the
                  start of the slide. */}
              <div className={`relative pr-1.5 pt-[9px] pb-2 flex items-center gap-2 bg-bg pointer-events-none transition-[padding-left] duration-[240ms] [transition-timing-function:cubic-bezier(.32,.72,0,1)] ${!isMobile && embedMode !== 'chat' && filteredSlots.length > 0 && !sidebarOpen ? 'pl-[60px]' : isMobile ? (embedMode === 'chat' ? 'pl-4' : 'pl-3') : 'pl-5'}`}>
                {/* Divider between toggle and title — ALWAYS mounted and
                    absolute (zero width, no flex-gap participation) so it can
                    never change the row's layout; it rides the row (title
                    side) and only fades. left-[52px] = the collapsed pane's
                    view of container x 44 (button 8+28 + 8px gap). */}
                {!isMobile && embedMode !== 'chat' && filteredSlots.length > 0 && (
                  <span aria-hidden="true" className={`absolute left-[52px] top-[13px] w-px h-5 bg-border transition-opacity ${sidebarOpen ? 'opacity-0 duration-100' : 'opacity-100 duration-150 delay-[90ms]'}`} />
                )}
                {embedMode !== 'chat' && isMobile && (
                  <button className="p-1 rounded-md text-muted hover:text-text cursor-pointer bg-transparent border-none pointer-events-auto" onClick={() => mobileSessions ? closeSidebar() : openSidebar()} aria-label={i18nT('pages.chatPage.toggle_sessions')}>
                    {/* Mirrors the desktop toggle exactly, state included: solid
                        while the panel is hidden, light while it is showing. */}
                    {mobileSessions ? <PanelLeftLight size={16} /> : <PanelLeftSolid size={16} />}
                  </button>
                )}
                <div className="group/header flex min-w-0 items-stretch gap-0.5 pointer-events-auto">
                <div className="flex items-center rounded-l-md rounded-r-[2px] px-1.5 py-0.5 group-hover/header:bg-bg-hover transition-colors">
                <ChatHeaderMenu
                  activeSlot={activeSlot}
                  agent={currentSlot?.agent}
                  onReveal={activeSlot && embedMode !== 'chat' ? () => {
                    // The request rides the store, not a window event: with the
                    // drawer collapsed ChatSidebar is unmounted, so an event
                    // dispatched here (before the mount that setSidebarPinned
                    // schedules commits) had no listener and was dropped —
                    // the store entry survives until the sidebar consumes it
                    // (#912). Mobile drives its own drawer state. Embed-chat
                    // never mounts a sidebar, so the item is not offered there:
                    // a stored request would outlive the view and fire on
                    // whichever sidebar mounts next.
                    sidebarAutoHidden.current = null
                    if (isMobile) openSidebar()
                    else if (!sidebarPinned) setSidebarPinned(true)
                    dispatch(requestSlotReveal(activeSlot))
                  } : undefined}
                  onRename={activeSlot ? () => { setEditingTitleSlot(activeSlot); setTitleDraft(title) } : undefined}
                  mode={effectiveMode}
                />
                </div>
              {editingTitle ? (
                <div className="flex min-w-0 flex-1 items-center gap-1 px-1.5 py-0.5 rounded-l-[2px] rounded-r-md bg-bg-hover">
                  {currentSlot?.memory_mode === 'incognito' && <span title={i18nT('pages.chatPage.incognito_memory_writes_disabled')}><EyeOff size={13} className="shrink-0 text-warn" /></span>}
                  {currentSlot?.memory_mode === 'temporary' && <span title={i18nT('pages.chatPage.temporary_no_memory_reads_or_writes')}><VenetianMask size={13} className="shrink-0 text-aim" /></span>}
                  <Input className="session-header-title text-sm font-semibold text-muted font-body bg-transparent border-0 rounded-none p-0 m-0 min-w-0 flex-1 outline-none focus-ring md:max-w-[50vw] focus:!shadow-none" size={Math.min(Math.max(titleDraft.length + 2, 6), 80)} autoFocus value={titleDraft} onChange={e => setTitleDraft(e.target.value)} {...titleIme.bindComposition<HTMLInputElement>({ onBlur: () => { if (!cancelTitleRef.current && titleDraft.trim() && activeSlot && titleDraft !== title) { dispatch(sseSlotTitle({ key: activeSlot, title: titleDraft.trim() })); api.renameSlot(activeSlot, titleDraft.trim()).catch(() => {}) } cancelTitleRef.current = false; setEditingTitleSlot(null) } })} onKeyDown={e => { if (e.key === 'Enter' && titleIme.claimEnter(e)) (e.target as HTMLInputElement).blur(); if (e.key === 'Escape') { titleIme.reset(); cancelTitleRef.current = true; setEditingTitleSlot(null) } }} />
                </div>
              ) : (
                <div className="cursor-text flex min-w-0 items-center gap-1 px-1.5 py-0.5 rounded-l-[2px] rounded-r-md group-hover/header:bg-bg-hover transition-colors">
                  <Clickable className="flex min-w-0 items-center gap-1" onClick={() => { if (activeSlot && generatingTitleSlots.has(activeSlot)) return; setEditingTitleSlot(activeSlot); setTitleDraft(title) }}>
                    {currentSlot?.memory_mode === 'incognito' && <span title={i18nT('pages.chatPage.incognito_memory_writes_disabled')}><EyeOff size={13} className="shrink-0 text-warn" /></span>}
                    {currentSlot?.memory_mode === 'temporary' && <span title={i18nT('pages.chatPage.temporary_no_memory_reads_or_writes')}><VenetianMask size={13} className="shrink-0 text-aim" /></span>}
                    <TypewriterText text={title} className="session-header-title text-sm font-semibold text-muted font-body truncate min-w-0 md:max-w-[50vw]" />
                    <Pen size={13} className="shrink-0 text-muted opacity-0 group-hover/header:opacity-60 transition-opacity" />
                  </Clickable>
                  {activeSlot && (generatingTitleSlots.has(activeSlot) ? <Loader size={16} className="shrink-0 text-accent animate-spin" /> : <Btn aria-label={i18nT('pages.chatPage.regenerate_title_with_llm')} className="shrink-0 text-muted opacity-0 group-hover/header:opacity-40 hover:!opacity-100 hover:text-accent transition-all cursor-pointer bg-transparent border-none p-0" title={i18nT('pages.chatPage.regenerate_title_with_llm')} onClick={e => { e.stopPropagation(); if (!activeSlot || generatingTitleSlots.has(activeSlot)) return; const slot = activeSlot; setGeneratingTitleSlots(prev => new Set(prev).add(slot)); api.generateTitle(slot).then(r => { /* title is redacted server-side via redact_exfiltration_urls + redact_credentials */ if (r.title) dispatch(sseSlotTitle({ key: slot, title: r.title })) }).catch(e => {
                    // eslint-disable-next-line no-console -- surface title-generation failures for debugging
                    console.warn('Failed to generate title:', e)
                  }).finally(() => setGeneratingTitleSlots(prev => { const next = new Set(prev); next.delete(slot); return next })) }}><Sparkles size={16} /></Btn>)}
                </div>
              )}
                </div>
              {effectiveMode === 'orchestrator' && <span className="pointer-events-auto"><InfoTip text={i18nT('pages.chatPage.autopilot_plans_before_executing_each_stage_need')} /></span>}
              <InboundLinkChip slotKey={activeSlot} />
              {/* Trailing controls grouped under a single ml-auto so multiple
                  right-aligned items don't each absorb free space (two ml-auto
                  siblings split the gap, parking the split icon mid-header). */}
              {/* focus-caption-reserve: this group owns the window's top-trailing
                  corner — where Windows and frameless Linux paint their caption
                  controls — whenever the side panel is not holding that edge, i.e.
                  while it is closed (the state that renders the reopen toggle
                  below) or docked at the bottom. Right-docked and showing, the
                  panel is at that edge instead and carries the reserve itself, so
                  reserving here too would indent these controls for nothing. */}
              <div className={`ml-auto flex shrink-0 items-center gap-1.5 pointer-events-none${!sidePanelWantsMount || sidePanelDock === 'bottom' ? ' focus-caption-reserve' : ''}`}>
              {/* Pop-out control, promoted to the title bar (menu items remain for
                  sidebar parity). Mirrors the split-view pattern to its left: a
                  dimmed icon to act, an accent chip when the state is active.
                  Inside the popout window itself the same spot carries Return. */}
              {popout ? (
                <Clickable className="flex items-center gap-1 text-muted hover:text-text transition-colors cursor-pointer pointer-events-auto text-[11px] font-medium px-1.5 py-0.5 rounded hover:bg-bg-hover" onClick={returnSelfToMain} title={i18nT('pages.chatPage.return_this_session_to_the_main_window')} aria-label={i18nT('pages.chatPage.return_to_main_window')}>
                  <Undo2 size={13} /> {i18nT('pages.chatPage.return')}
                </Clickable>
              ) : !embedMode && activeSlot && (activePoppedOut ? (
                <Clickable className="flex items-center gap-1 text-accent bg-accent/10 hover:bg-accent/20 transition-colors cursor-pointer pointer-events-auto text-[11px] font-medium px-1.5 py-0.5 rounded" onClick={() => focusActivePopout(activeSlot)} title={i18nT('pages.chatPage.this_session_is_open_in_its_own_window_focus_it')} aria-label={i18nT('pages.chatPage.focus_popped_out_window')}>
                  <ExternalLink size={13} /> {i18nT('pages.chatPage.popped_out')}
                </Clickable>
              ) : (
                <Clickable className="flex items-center justify-center w-7 h-7 rounded-md hover:bg-bg-hover transition-colors bg-transparent border-none cursor-pointer shrink-0 text-muted hover:text-text pointer-events-auto" onClick={() => openActivePopout(activeSlot, currentSlot?.title)} title={i18nT('pages.chatPage.pop_out_to_window')} aria-label={i18nT('pages.chatPage.pop_out_session_to_its_own_window')}>
                  <ExternalLink size={15} />
                </Clickable>
              ))}
              {/* Activity panel open toggle — relocated here from the top bar
                  (item 2.4) so opening the panel no longer narrows the now
                  full-width header. Shown only while the panel is closed; the
                  panel's own header carries the close button. Never disabled:
                  below the mobile breakpoint the panel opens full width, at or
                  above it opens beside the chat. There is no width at which
                  the button does nothing. */}
              {!embedMode && !popout && !activityOpen && (
                <Clickable
                  className="pi-morph flex items-center justify-center w-7 h-7 rounded-md transition-colors bg-transparent border-none shrink-0 pointer-events-auto text-muted hover:text-text hover:bg-bg-hover cursor-pointer"
                  onClick={toggleAct}
                  title={i18nT('pages.chatPage.open_activity_panel')}
                  aria-label={i18nT('pages.chatPage.open_activity_panel')}
                >
                  <PanelRightSolid size={15} />
                </Clickable>
              )}
              {!embedMode && splitFeatureEnabled && (splitAnchorForActive && !activeIsSplitAnchor ? (
                <Clickable className="flex items-center gap-1 text-accent bg-accent/10 hover:bg-accent/20 transition-colors cursor-pointer pointer-events-auto text-[11px] font-medium px-1.5 py-0.5 rounded" onClick={() => enterSplit(splitAnchorForActive)} title={i18nT('pages.chatPage.this_session_is_open_in_a_split_return_to_it')} aria-label={i18nT('pages.chatPage.return_to_split_view')}>
                <Columns2 size={13} /> {i18nT('pages.chatPage.in_split')}
              </Clickable>
              ) : (
                <Clickable className="opacity-40 hover:opacity-100 transition-opacity cursor-pointer pointer-events-auto" onClick={() => enterSplit(activeSlot)} title={i18nT('pages.chatPage.split_view_d')} aria-label={i18nT('pages.chatPage.enter_split_view')}>
                <Columns2 size={14} />
              </Clickable>
              ))}
              </div>
              {/* Header fade — softens content passing up into the opaque title
                  row, so it hangs off that row's bottom edge (anchor="below":
                  as an in-flow sibling its 24px consumed layout and pushed the
                  pinned card that far off the header; out of flow it overlays
                  the transcript and the pinned card paints above it). */}
              <EdgeFade side="top" anchor="below" />
              </div>
              {/* Fold sentinel — zero-height, always mounted. Its top edge is the
                  line the pinned prompt sticks to (see updatePinnedPrompt). */}
              <div ref={pinFoldRef} aria-hidden className="h-0" />
              {pinned && (pinMinimized ? (
                <PinnedPromptPill onRestore={() => setPinMinimized(false)} />
              ) : (
                <PinnedPrompt
                  text={pinned.text}
                  fullText={pinned.full}
                  images={pinned.images}
                  bodyBeyondPreview={pinned.bodyBeyondPreview}
                  pushUp={pinned.push}
                  bannerH={pinned.bannerH}
                  expanded={pinExpanded}
                  onToggleExpanded={() => setPinExpanded(p => !p)}
                  onJump={() => scrollToPinnedPrompt(pinned.idx)}
                  onMinimize={() => setPinMinimized(true)}
                  cardRef={pinCardRef}
                  onCollapsedHeight={onPinCollapsedHeight}
                />
              ))}
            </div>
            <ChatDropOverlay active={dragOver} />
            {isWelcomeState ? (
              <motion.div
                key="welcome-hero"
                layout
                className="flex-1 flex flex-col items-center justify-center gap-6 px-8 min-h-0 overflow-y-auto"
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                exit={{ opacity: 0 }}
                transition={{ duration: 0.18 }}
              >
                <WelcomeView
                  mode={currentSlot?.mode || mode}
                  setInput={setInput}
                  memoryMode={currentSlot?.memory_mode ?? 'persistent'}
                  cleanMode={currentSlot?.clean_mode}
                  onSwitchMode={async (newMode) => {
                    if (!activeSlot) return
                    // Create-first-then-delete: deleting the active slot first
                    // would make deleteSlot jump focus to a sibling. Creating
                    // first keeps the new slot active, so the delete skips the
                    // sibling navigation. Carry agent/project/folder/color so
                    // the recreated slot keeps its identity and placement.
                    const old = currentSlot
                    const opts = {
                      agent: old?.agent || defaultAgent || undefined,
                      model: old?.model || undefined,
                      mode,
                      memory_mode: newMode,
                      folder_id: old?.folder_id ?? null,
                      color_index: old?.color_index ?? null,
                      color_hex: old?.color_hex ?? null,
                      project: old?.project ?? null,
                    }
                    try { await dispatch(createSlot(opts)).unwrap() } catch { return }
                    try { await dispatch(deleteSlot(activeSlot)).unwrap() } catch { /* new slot already active */ }
                  }}
                  onToggleClean={async (clean) => {
                    if (!activeSlot) return
                    const old = currentSlot
                    const opts = {
                      agent: old?.agent || defaultAgent || undefined,
                      model: old?.model || undefined,
                      mode,
                      clean_mode: clean,
                      folder_id: old?.folder_id ?? null,
                      color_index: old?.color_index ?? null,
                      color_hex: old?.color_hex ?? null,
                      project: old?.project ?? null,
                    }
                    try { await dispatch(createSlot(opts)).unwrap() } catch { return }
                    try { await dispatch(deleteSlot(activeSlot)).unwrap() } catch { /* new slot already active */ }
                  }}
                />
              </motion.div>
            ) : (
            <TranscriptScrollShell
              scrollerRef={scrollerRef}
              onScroll={onScrollPin}
              virt={virt}
              loadingOlder={loadingOlder}
              spinnerNearTop={spinnerNearTop}
              // Second half of the fade-band clearance, alongside
              // TRANSCRIPT_TAIL_SPACER_PX. Unlike the tail spacer this one also
              // applies to a transcript short enough not to scroll, so both are
              // needed for the last line to clear the band in every state.
              // `visibility` is not one of the properties the shell claims, so
              // adding it here is inside its documented contract. Hiding rather
              // than unmounting keeps the scroller's geometry and the height
              // cache intact -- the restore needs to WRITE scrollTop while this
              // is up, which a display:none element cannot do.
              scrollerStyle={{ paddingBottom: 16, ...(virt.restoreGate ? { visibility: 'hidden' as const } : null) }}
              aboveRows={<>
              {/* Mid-switch `slotHasMore` still describes the outgoing chat, so the cursor
                  key gates the bar to match the paging thunk's own precondition. */}
              {slotHasMore && cursorIsForActiveSlot && (
                <EarlierMessagesBar loading={loadingOlder} failed={olderFailed} onLoad={handleLoadEarlier} onFocusRelease={() => scrollerRef.current?.focus()} />
              )}
              </>}
              belowRows={<>
              {/* Off-screen measurement farm. Kept FIRST in `belowRows` — i.e.
                  after the rows and before the footer, exactly where it sat
                  before the scroll shell was extracted: it renders into a
                  zero-height overflow-hidden box, so its position only has to
                  be inside the transcript's own column (the width is what
                  makes the measurements true). */}
              <MeasureFarm
                enabled={!!activeSlot && !slotLoading}
                // NOT paused on slotRunning: a session with an agent working
                // is "running" for minutes at a stretch (tool executions),
                // and gating the farm on it disabled measured geometry on
                // exactly the sessions people actively read — every row they
                // scrolled to was estimate-priced and corrected under their
                // finger. Streaming regroups are safe: farmRecord revalidates
                // row identity at write time and drops stale writes.
                paused={loadingOlder}
                count={renderedDisplayItems.length}
                originIndex={visibleDisplayItems[0]?.index ?? Math.max(0, renderedDisplayItems.length - 1)}
                isMeasured={virt.farmIsMeasured}
                isMounted={virt.farmRowMounted}
                keyAt={(i) => { const it = renderedDisplayItems[i]; return it ? virtualKey(it, i) : null }}
                record={virt.farmRecord}
                renderItem={renderFarmItem}
                scrollerEl={() => scrollerRef.current}
              />
              {/* Footer */}
              <ChatFooter running={slotRunning} stopping={slotStopping} state={slotState} lastRole={lastRole} streamTick={streamTick} regenerating={regenerating} stopState={currentSlot?.stop_state} />
              {activeSlot && !slotLoading && !embedded && !popout && slotSwitchTarget !== activeSlot && (
                <div className="px-4 mx-auto w-full" style={{ maxWidth: 'var(--mc-content-width, 900px)' }}>
                  <SessionPulseSurveyCard
                    // Remount on session switch: without this, React reuses
                    // the same component instance across sessions, so an
                    // in-progress rating/feedback/email from session A would
                    // still be sitting in state when the user switches to
                    // session B and hits Submit — attributing A's answers to
                    // B's sessionId prop, which had already updated.
                    //
                    // Gated on !slotLoading: the card captures its baseline
                    // turn count on FIRST MOUNT (see the component's own
                    // comment), so mounting before history finishes loading
                    // would baseline at 0 and then count every loaded
                    // historical turn as "live" once the fetch resolves —
                    // reintroducing the exact reopened-session bug the
                    // baseline exists to prevent, just via a race instead of
                    // a missing check.
                    key={activeSlot}
                    sessionId={activeSlot}
                    kiroCrewVersion={kiroCrewVersion}
                    turnCount={completedTurnCount}
                    slotOrigin={currentSlot?.origin}
                    onLayoutChange={handleSurveyLayoutChange}
                  />
                </div>
              )}
              {/* Tail spacer, in px rather than vh. It plus the scroller's own
                  bottom padding is the clearance between the last line of the
                  transcript and the FIXED-height fade band below, so expressing it
                  in `vh` made that clearance viewport-dependent: at 2vh + 8px it
                  cleared a 24px band by 1px at 844px tall and cut INTO the last
                  line on anything shorter (−1px at 740, −2px at 700, −5px at 560),
                  which is the sliced-glyph hairline reported from a phone and the
                  reason it looked mobile-only. */}
              <div style={{ height: TRANSCRIPT_TAIL_SPACER_PX }} />
              </>}
            >
              {/* Message items — only the mounted window renders; everything
                  else is represented by the top/bottom spacers. */}
              {visibleDisplayItems.map((vi) => {
                if (!vi.mounted) return null
                const item = vi.data
                const displayIdx = vi.index
                // A hidden invisible-only assistant row grouped as a loose
                // single (short quiet-cycle batches never wrap into a turn)
                // draws nothing in renderMessage; skip its measured py-1
                // wrapper too, or each quiet cycle leaves an empty spacer row.
                if (item.kind === 'single' && isHiddenInvisibleAssistantRow(item.msg)) return null
                if (item.kind === 'turn') {
                  return <div key={vi.key} ref={virt.measureRef(vi.index)} data-display-index={displayIdx}><TurnBlock turn={item} renderItem={renderTurnItem} collapseAll={chatConfig.collapseAllSteps} appToolCallIds={appToolCallIds} disclosure={turnDisclosure[vi.key]} disclosureKey={vi.key} onDisclosureChange={setTurnDisclosureFor} /></div>
                }
                return <div key={vi.key} ref={virt.measureRef(vi.index)} data-display-index={displayIdx} className={`px-4 mx-auto w-full py-1`} style={{
                  maxWidth: 'var(--mc-content-width, 900px)',
                  // The pinned banner is styled as this row's own bubble and sits
                  // at the exact position and width the bubble had when its bottom
                  // edge reached the band's bottom, so leaving both visible is what
                  // betrays them as two containers. Hide the real one (visibility,
                  // NOT display — the virtualizer must keep measuring its height or
                  // the transcript would reflow under the reader) and the bubble
                  // appears to simply stop travelling and stick. A row is only ever
                  // hidden once it is entirely behind the band, so a tall prompt
                  // never leaves a visible hole above the response.
                  //
                  // Match by message IDENTITY (ts), not display index. `pinned.idx`
                  // is computed in a scroll rAF against `displayItemsRef`, which is
                  // refreshed in a layout effect — but a streaming append or a turn
                  // regroup can still shift the list between that read and this
                  // render, leaving `pinned.idx` pointing one row off. When it did,
                  // the WRONG row was hidden and the real pinned bubble painted
                  // alongside the banner — the "two stacked boxes" bug. The ts is
                  // stable across any index shift, so it hides the right row every
                  // frame; fall back to the index only for a message with no ts.
                  // Hiding is safe only while the banner stands in for the row, so
                  // the minimized chip must not hide it — see pinHidesRow.
                  visibility: pinHidesRow(pinned, pinMinimized, {
                    ts: item.kind === 'single' ? item.msg.ts : undefined,
                    idx: displayIdx,
                  }) ? 'hidden' : undefined,
                }}>{item.kind === 'group' ? (() => {
                const unresolvedGroupPerms = item.msgs.filter(m => m.role === 'permission' && !m.meta?.resolved)
                if (item.msgs.every(m => m.role === 'permission')) return null
                return (
                <CollapsibleToolGroup
                  count={item.msgs.filter(m => m.role !== 'permission').length}
                  disclosureKey={`ctg-${vi.key}`}
                  hasPermission={false}
                  isRunning={slotRunning && displayIdx === displayItems.length - 1}
                  permissionMeta={unresolvedGroupPerms.at(-1)?.meta as Record<string, unknown> | undefined}
                  pendingPermCount={unresolvedGroupPerms.length}
                  onApprove={(() => {
                    const aid = unresolvedGroupPerms.at(-1)?.meta?.approval_id as string | undefined
                    if (!aid) return approve
                    return async (action: string) => {
                      await api.resolveApproval(aid, toApiDecision(action))
                      dismissApproval(aid)
                    }
                  })()}
                  onViewActivity={toggleAct}
                  activityOpen={activityOpen}
                >{item.msgs.map((m, j) => <div key={msgIdentityKey(m, stableMsgKey)}>{renderMessage(item.startIdx + j, m)}</div>)}</CollapsibleToolGroup>)
              })() : renderMessage(item.idx, item.msg)}</div>
              })}
            </TranscriptScrollShell>
            )}
            {/* Restore cover. A session left mid-history reopens on a transcript
                that hydrates in chunks and is only positioned once its anchored
                row lands, so the rows underneath are briefly partial and in the
                wrong place. Showing them means the reader watches the transcript
                assemble and then jump; covering that window turns it into one
                deliberate load. A session left AT the live end never raises this
                -- it is placed on the first commit, with nothing to wait for. */}
            {/* ONE placeholder for both waits. Fetching the slot used to show a
                centred spinner and restoring a reading position used to show
                bars, so two readings of the same fact -- the transcript is not
                ready -- looked like different events. A spinner also says only
                "wait", where a skeleton previews the shape that is coming. */}
            {(slotLoading || virt.restoreGate) && <ChatTranscriptSkeleton />}
            {/* Transcript bottom mask. Its box deliberately does NOT stop at the
                scrollport's bottom edge — it reaches DOWN to the composer box, and
                that overshoot is the point.

                The band used to end exactly on that boundary, which left the
                COMPOSER_MASK_OVERSHOOT_PX strip between it and the input box
                unmasked and a hairline showed through there. So the box now spans
                `above` px over the boundary — feathering the hard clip, since the
                transcript is cut at the scrollport edge whenever the user is
                scrolled up — PLUS that strip below it, kept opaque so the mask is
                flush against the input box with nothing between them.

                The three numbers are one arithmetic unit and must move together:
                height = above + overshoot, and the two negative margins cancel the
                whole box, so it paints over both regions while consuming ZERO
                layout. A positive residual would push the composer down instead.

                The solid stop runs from the bottom up through a few px ABOVE the
                boundary on purpose: a ramp that reaches full opacity only AT the
                clip edge leaves its topmost rows just shy of opaque, and the clipped
                glyphs bleed through (measured over a blank control at 390px:
                +7.6 / +5.2 / +1.9 mean channel at 3 / 2 / 1px above the edge, 0.00
                once the bottom is solid). TRANSCRIPT_TAIL_SPACER_PX plus the
                scroller's padding must stay >= `above`, the part that reaches up
                into readable content. ChatPage.fadeClearance.test.tsx pins all of
                it, including that the overshoot never covers the box's own top
                border. */}
            <div
              aria-hidden
              className="bg-gradient-to-t from-bg from-[62%] to-transparent pointer-events-none relative z-[1]"
              style={{
                height: TRANSCRIPT_MASK_ABOVE_PX + COMPOSER_MASK_OVERSHOOT_PX,
                marginTop: -TRANSCRIPT_MASK_ABOVE_PX,
                marginBottom: -COMPOSER_MASK_OVERSHOOT_PX,
              }}
            />
            <div className="relative">
              <JumpToBottomButton visible={!isAtBottom && messages.length > 0} onClick={() => scrollBottom(true)} />
              {/* Status chrome never claims more than half the pane. These bars
                  are flex-flow siblings of the transcript scroller, which has an
                  automatic minimum size of 0 and collapses under pressure — an
                  opening keyboard shrinks the layout viewport, so an uncapped
                  stack rises into the title band at the top of the pane and
                  covers the rename editor. Capping makes the stack yield first.
                  `svh` not `%` (a percentage resolves against this wrapper's own
                  content-derived height, so it computes to none) and not `vh`
                  (which over-measures a phone showing its URL bar). Scoped to
                  the bars: FlyingQuote, the composer and the `absolute -top-10`
                  scroll-to-bottom button must all stay outside the scroll box.
                  `pb-[11px] mb-[-11px]` cancels QueueStack's OVERLAP: its -11px
                  fuse margin is what pulls the queue card into the composer, and
                  a scroll container turns that overhang into permanent internal
                  overflow (measured: scrollHeight-clientHeight == 11 with a
                  collapsed queue at any height, so a thumb showed and the card's
                  bottom 11px clipped). The padding lands the child's margin edge
                  exactly on the padding box, and the equal negative margin keeps
                  the wrapper's contribution to the column unchanged, so the seam
                  still fuses. Layout-neutral when the queue is empty: the pair
                  cancels. `scrollbar-overlay` is what every other internal
                  scroller here uses (SkillDirectoryBrowser pairs it with the same
                  `overflow-y-auto overscroll-contain`): it replaces the global
                  always-visible `var(--border)` thumb with a hover-revealed
                  overlay one, so the capped box does not carry a permanent bar. */}
              <div ref={composerBandRef} className="max-h-[50svh] overflow-y-auto overscroll-contain scrollbar-overlay pb-[11px] mb-[-11px]" data-testid="composer-status-stack">
              {/* Not gated on activityOpen (unlike the two bars below): the
                  activity sidebar has no TODO view, so hiding it there would
                  lose the information rather than de-duplicate it. */}
              <TaskProgressBar slot={activeSlot} />
              {/* De-duplicate ONLY against the matching sidebar tab (#728): each
                  bar is redundant when the activity sidebar is actually SHOWING
                  its own view (Subagents / Workflows), but on any OTHER tab
                  (Files, Changes, Logs, Artifacts) hiding it would lose the live
                  roster entirely. The condition mirrors the SidePanel's own
                  render guard (`activityOpen && !search.isOpen`) — so opening the
                  find pane, which UNMOUNTS the panel, re-shows the bar — and
                  reads the live panel tab (`tabsCtl`), NOT the Redux
                  `activityTab`, which only tracks programmatic openActivityToTab
                  calls and goes stale when the user clicks a tab in the panel. */}
              {!(activityOpen && !search.isOpen && tabsCtl.tabs.find(t => t.id === tabsCtl.activeId)?.kind === 'subagents') && <SubagentProgressBar slot={activeSlot} />}
              {!(activityOpen && !search.isOpen && tabsCtl.tabs.find(t => t.id === tabsCtl.activeId)?.kind === 'workflows') && <WorkflowProgressBar slot={activeSlot} />}
              <SubagentDeliveryProgress count={systemDeliveryCount} />
              <QueueStack messages={queuedMessages} onCancel={handleCancelQueued} onInterrupt={handleInterruptQueued} onEdit={handleEditQueued} onReorder={handleReorderQueued} pendingIds={queuePendingIds} fuseBelow={followUpOptions.length === 0 && !knowledgeFetch.pendingKnowledge} />
              </div>
              {flyingQuote && <FlyingQuote text={flyingQuote.text} from={flyingQuote.from} targetRef={inputAreaRef} onComplete={() => setFlyingQuote(null)} />}
              <div ref={inputAreaRef} className="relative z-10">
              {/* The refused-press answer sits directly above the composer,
                  adjacent to the message-footer controls that raised it, so the
                  press cannot fail silently. Shares the chat column's own
                  container recipe (the page gutter + the theme content width)
                  rather than capping itself: a narrower centred box reads as
                  belonging to neither the transcript above nor the input below.

                  The title names the refused action. Without it the notice
                  reads as a generic error rather than "this is the answer to
                  the button you just pressed" — a first-time reader then
                  concludes the click did nothing and presses again. */}
              {refusedPress && (
                <div
                  className="px-4 mb-1.5 mx-auto w-full"
                  style={{ maxWidth: 'var(--mc-content-width, 900px)' }}
                  data-testid="refused-press-error"
                >
                  <ErrorNotice
                    title={i18nT(REFUSED_PRESS_TITLE_KEYS[refusedPress.action])}
                    message={refusedPress.message}
                    onDismiss={() => setRefusedPress(null)}
                  />
                </div>
              )}
              {showHistorySuggestions && (
                <div className="absolute left-0 right-0 bottom-full mb-1 mx-auto w-full max-w-[760px] border border-border rounded-lg bg-card overflow-hidden animate-scale-in z-50 shadow-lg flex flex-col max-h-[min(300px,40vh)]">
                  <div className="px-3.5 py-2.5 border-b border-border shrink-0">
                    <span className="text-[12px] font-semibold text-muted tracking-[.02em]">{i18nT('pages.chatPage.continue_a_previous_chat')}</span>
                  </div>
                  <div className="overflow-y-auto flex-1 min-h-0" role="listbox" aria-label={i18nT('pages.chatPage.previous_chats')}>
                    {historySuggestions.map((s) => (
                      <div
                        key={s.key}
                        role="option"
                        tabIndex={0}
                        aria-selected={false}
                        className="w-full text-left px-3.5 py-2.5 flex items-center gap-3 cursor-pointer transition-all border-b border-border last:border-0 hover:bg-bg-hover"
                        onMouseDown={(e) => { e.preventDefault(); handleResumeSession(s.key, s.title || s.key) }}
                        onKeyDown={(e) => { if (e.key === 'Enter') handleResumeSession(s.key, s.title || s.key) }}
                      >
                        <div className="flex-1 min-w-0">
                          <div className="font-mono text-[13px] text-text truncate">{s.title || s.key}</div>
                          {s.created && <div className="text-[11px] text-muted font-mono mt-0.5">{fmtDateFields(s.created, { year: 'numeric', month: 'short', day: 'numeric' })}</div>}
                        </div>
                        <Undo2 size={14} className="text-accent shrink-0" />
                      </div>
                    ))}
                  </div>
                  <div className="px-3.5 py-2 border-t border-border flex justify-end shrink-0">
                    <span className="text-[11px] text-muted-strong">{i18nT('pages.chatPage.esc_to_dismiss')}</span>
                  </div>
                </div>
              )}
              {knowledgeFetch.results.length > 0 || knowledgeFetch.loading ? (
                <KnowledgePicker
                  results={knowledgeFetch.results}
                  query={knowledgeFetch.query}
                  loading={knowledgeFetch.loading}
                  onInject={(selected) => {
                    knowledgeFetch.inject(selected)
                  }}
                  onSkip={() => knowledgeFetch.clearResults()}
                />
              ) : null}
              {pendingQuestion && (
                <div className="px-4 pb-2 mx-auto w-full" style={{ maxWidth: 'var(--mc-content-width, 900px)' }}>
                  <PendingQuestionCard
                    slotKey={activeSlot}
                    onFallbackSend={(text) => {
                      // A 404 means the blocked wait is gone and the card has
                      // already cleared. Keep the user's answer in the composer
                      // for an explicit retry instead of auto-sending: even with
                      // a live WS, /api/chat can resolve with an HTTP error (for
                      // example Kiro becoming unavailable), which would otherwise
                      // leave the answer only in a non-persisted optimistic bubble.
                      setInput((prev) => (prev.trim() ? `${prev}\n${text}` : text))
                    }}
                    onDirectSend={(text) => {
                      // No-ask_id card: the card IS the interaction, so answer
                      // and send in one click.
                      //
                      // Offline, send() bails at its own !connected guard and
                      // the card clears regardless — which would DROP the
                      // answer. Fall back to the composer so it survives, the
                      // same recovery the 404 path uses.
                      if (!connected) {
                        setInput((prev) => (prev.trim() ? `${prev}\n${text}` : text))
                        return
                      }
                      void send(text, activeSlot || undefined)
                    }}
                  />
                </div>
              )}
              {pendingFollowup && activeSlot && (
                <div className="px-4 pb-2 mx-auto w-full" style={{ maxWidth: 'var(--mc-content-width, 900px)' }}>
                  <FollowUpCard
                    items={pendingFollowup.items}
                    projectDir={currentSlot?.project || undefined}
                    onAddToSession={followupAddToSession}
                    onStartInWorktree={followupStartInWorktree}
                    onSkip={dismissFollowup}
                  />
                </div>
              )}
              <ChatInput
              aboveComposer={
                /* In-flow tip inside the composer's own width wrapper: shares
                   the composer's exact box geometry (Raymond 2026-07-21: tip
                   width must always match the input box) while still pushing
                   chat content up like QueueStack (team decision: never cover
                   thinking/output; queue and question card keep priority via
                   tipSuppressed). ChatInput renders this slot LAST in the
                   above-composer stack, so the card stays flush against the
                   input box and an options row sits above it. */
                <AnimatePresence>
                  {folderSuggestion && activeSlot ? (
                    <div className="pt-1.5" key="folder-suggestion">
                      <FolderSuggestionCard
                        folderName={folderSuggestion.folderName}
                        breadcrumb={folderSuggestion.breadcrumb}
                        onAccept={folderSuggestionAccept}
                        onDecline={folderSuggestionDecline}
                      />
                    </div>
                  ) : activeTip && (
                    <div className="pt-1.5" key="tip">
                      <TipCard tip={activeTip} onDismiss={dismissTip} />
                    </div>
                  )}
                </AnimatePresence>
              }
              value={input}
              onChange={setInput}
              onSend={() => send()}
              canSteer={composerBusy}
              onSteer={steer}
              onFollowUpSend={handleFollowUpSend}
              disabled={
                /* Streaming, compaction, and stopping all
                   keep the input interactive: api_chat queues on slot.running and
                   stop preserves the queue, so typing + Enter queues a
                   follow-up during the stop window instead of being silently blocked. */
                false
              }
              autoFocusKey={activeSlot}
              prefillHint={prefillHint}
              onDismissHint={() => setPrefillHint(false)}
              onScreenshot={handleCapture}
              onUploadFiles={uploadFiles}
              uploading={uploading}
              pendingFiles={pendingFiles}
              pendingDirs={pendingDirs}
              resizedInfo={resizedInfo}
              onRemoveFile={p => {
                setPendingFiles(prev => prev.filter(x => x !== p))
                // A picker-picked file also inserted an `@rel` token into the
                // composer, so its remove strips that token too — the same
                // contract folder chips have, so the two chip kinds cannot
                // disagree about what "remove" means. The exact token is
                // recorded at pick time, but the ref is in-memory only: a
                // restored draft or a failed-send restore re-stages the file
                // without it. Fall back to deriving the token from the path —
                // the shortest boundary-checked `@suffix` present in the text
                // (the same walk buildRelMap uses), which is exactly the form
                // the picker inserts. Uploaded/dropped files have no token in
                // the text, so the derivation finds nothing and their remove
                // stays state-only. On no match the text is left alone —
                // visible and editable is the safe fallback.
                const token = pickedFileTokens.current[p] ?? [...buildRelMap([p], inputRef.current).keys()].map(s => `@${s}`)[0]
                delete pickedFileTokens.current[p]
                if (!token) return
                const esc = token.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
                setInput(prev => prev.replace(new RegExp(`(^|\\s)${esc}(?: |(?=\\s)|$)`, 'g'), '$1'))
              }}
              onRemoveDir={rel => {
                // The chip derives from the `@rel/` token, so removing the
                // reference IS removing the token. Boundary-checked so
                // "@src/pages/" never eats a longer "@src/pages/sub/" token.
                const esc = `@${rel}`.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
                setInput(prev => prev.replace(new RegExp(`(^|\\s)${esc}(?: |(?=\\s)|$)`, 'g'), '$1'))
              }}
              pendingSessions={pendingSessions}
              onRemoveSessionRef={unstageSessionRef}
              // A folder pick is complete once ChatInput inserts its `@rel/`
              // token — the chip derives from the text, so there is no state
              // to stage here. Files stay list-backed (uploads have no token)
              // and additionally record their inserted token for remove.
              onFileSelect={(path, kind, token) => {
                if (kind === 'dir') return
                // Stage under the canonical (forward-slash Windows) identity —
                // the same form the tree context menu stages — so the SAME file
                // picked through both entry points dedupes instead of sending
                // twice. Token bookkeeping keys on the staged form so remove
                // finds it.
                const canon = normalizeWindowsPath(path)
                if (token) pickedFileTokens.current[canon] = token
                setPendingFiles(prev => addPendingFile(prev, canon))
              }}
              onFileOpen={handleFileOpen}
              project={currentSlot?.project || ''}
              projectBranch={projectBranch}
              projectDetached={!projectGitError && !!projectGit?.detached}
              projectGitDirty={projectGitDirty}
              projectGitAhead={projectGitAhead}
              projectGitBehind={projectGitBehind}
              isMac={isMac}
              onDrop={dropTargetProps.onDrop}
              onDragOver={dropTargetProps.onDragOver}
              onDragLeave={dropTargetProps.onDragLeave}
              voiceRecording={voiceOwned && voice.recording}
              voiceTranscribing={voiceOwned && voice.transcribing}
              /* Ungated: `startVoice` refuses on `voice.transcribing` outright,
                 so the voice controls have to read the same global fact. */
              voiceTranscribeActive={voice.transcribing}
              voiceError={voice.error}
              voiceLevel={voiceOwned ? voice.level : 0}
              voiceDeviceLabel={voiceOwned ? voice.deviceLabel : ''}
              voiceDeviceId={voiceOwned ? voice.deviceId : ''}
              onSelectVoiceDevice={voice.switchDevice}
              voiceDeviceSwitchIsLive={voiceOwned && voice.deviceSwitchIsLive}
              onClearVoiceError={voice.clearError}
              voiceDictationPanel={sttDictationPanel}
              voiceStreaming={voice.streamEnabled}
              voiceSampleRef={voice.sampleRef}
              voicePartial={voiceOwned ? voice.partial : ''}
              voiceDownload={voiceOwned ? voice.download : null}
              voiceCaretRef={voiceCaretRef}
              voicePendingCaretRef={voicePendingCaretRef}
              onVoiceToggle={voiceInputSupported ? toggleVoice : undefined}
              onVoiceCancel={voiceInputSupported ? cancelVoice : undefined}
              onVoicePrewarm={voiceInputSupported ? voice.prewarm : undefined}
              onVoiceStart={voiceInputSupported ? startVoice : undefined}
              onVoiceStop={voiceInputSupported ? stopVoice : undefined}
              voiceCaptureActive={voice.recording}
              agentName={activeAgentName}
              agentSource={effectiveAgents.find(a => a.name === activeAgentName)?.source}
              modelName={shownModel}
              onAgentClick={provider.capabilities.agentTemplates ? (rect) => { setAgentBtnRect(rect); setAgentDropdown(!agentDropdown) } : undefined}
              onModelClick={(rect) => { setModelBtnRect(rect); setModelDropdown(!modelDropdown) }}
              onProjectClick={(rect) => {
                setProjectBtnRect(rect)
                setProjectPickerOpen(o => !o)
              }}
              contextPct={contextPct}
              contextUsedTokens={contextTokens?.used}
              contextWindowTokens={contextTokens?.window || remoteContextWindow || provider.getContextWindow(shownModel)}
              showContextPct={chatConfig.showContextPct}
              showContextTokens={chatConfig.showContextTokens}
              isRunning={composerBusy}
              /* Composed with `interrupted`, matching the ErrorCard gate above.
                 Availability alone would put a filled primary button on the
                 composer of every idle chat that holds a conversation — an
                 accent-filled control reads as "this is your next move", so on
                 a slot that finished cleanly it advertises pending work that
                 does not exist and the only thing distinguishing it from Send
                 is a hover tooltip. `interrupted` is not merely the wording
                 now: it is the reason the control exists at all. When nothing
                 proves an interruption the composer falls back to the ordinary
                 Send button, disabled while empty, like every other chat.

                 The cost is a turn that died leaving no evidence — a hard kill
                 after a mid-turn assistant segment already flushed, which is
                 the one shape `_is_interrupted` cannot see. That slot loses its
                 one-click nudge; typing anything still resumes it. Closing that
                 hole needs a persisted turn-in-flight marker (backend), not a
                 louder button here. */
              continuable={continuable && interrupted}
              continueIsRecovery={interrupted}
              onContinue={handleContinue}
              continuing={continuing}
              onStop={() => {
                const slot = activeSlot
                if (!slot) return
                const isEscalation = isEscalationState(currentSlot?.stop_state)
                // Per-slot view over the map, satisfying SoftStopRef so the
                // arming window is measured against THIS slot's soft press.
                const map = softStopAtMapRef.current
                const slotRef = {
                  get current() { return map.get(slot) ?? 0 },
                  set current(v: number) { map.set(slot, v) },
                }
                const action = handleStopPress(
                  isEscalation,
                  Date.now(),
                  slotRef,
                  () => dispatch(requestStop({ slotId: slot, force: false })),
                  () => dispatch(requestStop({ slotId: slot, force: true })),
                )
                // 'ignore' = accidental rapid double-tap during the arming window
                if (action !== 'ignore') dispatch(clearPendingPermissions())
              }}
              isQueued={slotStopping}
              stopState={currentSlot?.stop_state}
              approvalMode={displayMode}
              providerId={provider.id}
              reasoningEffort={effectiveEffort}
              onReasoningEffortClick={provider.capabilities.reasoningEffort && modelSupportsEffort(shownModel === 'auto' ? '' : shownModel) ? (rect) => { setReasoningEffortBtnRect(rect); setReasoningEffortDropdown(!reasoningEffortDropdown) } : undefined}
              onAutoNudgeClick={setAutoNudgeOpen}
              autoNudgeLoop={autoNudgeLoop}
              autoNudgeOpen={autoNudgeOpen}
              onAutoNudgeChange={setAutoNudgeLoop}
              onOptimizeResult={handleOptimizeResult}
              memoryMode={currentSlot?.memory_mode ?? 'persistent'}
              cleanMode={currentSlot?.clean_mode}
              sentMessages={sentMessages}
              sendOnEnter={isMobile ? 'ctrl-enter' : chatConfig.sendOnEnter}
              followUpOptions={followUpOptions}
              followUpPicked={followUpPicked}
              quickSend={dashCfg?.quick_send}
              followUpLayout={chatConfig.followUpLayout}
              followUpSourceKey={followUpSourceKey}
              onFollowUpSelect={handleFollowUpSelect}
              pasteBlocks={pasteBlocks}
              onPasteBlocksChange={setPasteBlocks}
              knowledgeChip={knowledgeFetch.pendingKnowledge ? <div className="flex items-start gap-1"><KnowledgeBubbleChip knowledge={{ items: knowledgeFetch.pendingKnowledge.items.length, tokens: knowledgeFetch.pendingKnowledge.totalTokens, titles: knowledgeFetch.pendingKnowledge.items.map(i => i.title), content: knowledgeFetch.pendingKnowledge.items.map(i => ({ title: i.title, text: i.content.slice(0, 2000) })) }} /><button type="button" onClick={() => knowledgeFetch.clearPending()} className="shrink-0 mt-0.5 p-0.5 text-muted hover:text-danger bg-transparent border-none cursor-pointer rounded hover:bg-danger/10 transition-colors" aria-label={i18nT('pages.chatPage.remove_knowledge_context')} title={i18nT('pages.chatPage.remove_knowledge_context')}>&times;</button></div> : undefined}
              connected={connected}
            />
            </div>
            <VoiceDisabledModal
              open={voiceSetupOpen}
              reason={sttEnabled && !sttAvailable ? 'unavailable' : 'disabled'}
              provider={sttProvider}
              onClose={() => setVoiceSetupOpen(false)}
              onOpenSettings={() => {
                setVoiceSetupOpen(false)
                navigate(embedded ? '/embed/settings' : settingsPath({ tab: 'voice' }))
              }}
            />
            {/* Agent dropdown portal — triggered from input bar */}
            {agentDropdown && agentBtnRect && createPortal(
              // The keydown handler routes arrow/Enter navigation to the inner
              // role="listbox"; the dialog is a focus container (tabIndex={-1}),
              // not an interactive widget itself, so this delegation is intentional.
              // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions
              <div ref={agentDropdownRef} role="dialog" aria-label={i18nT('pages.chatPage.agent_selector')} tabIndex={-1} onKeyDown={onAgentListKeyDown} className="fixed z-[9999] bg-bg-elevated border border-border rounded-xl shadow-xl min-w-[260px] max-w-[340px] flex flex-col p-1 gap-0.5 animate-slide-up" style={(() => { const left = Math.max(8, Math.min(agentBtnRect.left, window.innerWidth - 348)); return { bottom: window.innerHeight - agentBtnRect.top + 4, left } })()}>
                <div className="px-1.5 pt-1.5 pb-1">
                  <Input ref={agentInputRef} type="text" aria-label={i18nT('pages.chatPage.filter_agents')} placeholder={i18nT('pages.chatPage.type_to_filter')} value={agentFilter} onChange={e => setAgentFilter(e.target.value)} className="w-full px-2 py-1 text-[13px]" />
                </div>
                <div role="listbox" aria-label={i18nT('pages.chatPage.agent_list')} className="overflow-y-auto max-h-[280px]">
                <AgentDropdownList agents={filteredAgents} activeAgent={activeAgentName} defaultAgent={defaultAgent} onSelect={(name) => { switchAgent(name); setAgentDropdown(false) }} filter={agentFilter} />
                </div>
                {/* Embedded chat gets neither half of the default-agent affordance: it has
                    no /capabilities route for the footer, and the footer is what carries the
                    failed-write alert — offering the write without its error path would make
                    a rejected request indistinguishable from a successful one. */}
                {!embedded && <DefaultAgentRow agentName={activeAgentName} isDefault={activeAgentName === defaultAgent} onSetDefault={() => toggleDefaultAgent(activeAgentName)} />}
                {!embedded && <ManageAgentsFooter error={defaultAgentFailed} onManage={() => { setAgentDropdown(false); navigate('/capabilities?tab=templates') }} />}
              </div>,
              document.body
            )}
            {/* Model dropdown portal — triggered from input bar */}
            {modelDropdown && modelBtnRect && createPortal(
              <ModelEffortDropdown
                anchorRect={modelBtnRect}
                dropdownRef={modelDropdownRef}
                inputRef={modelInputRef}
                onListKeyDown={onModelListKeyDown}
                models={filteredModels}
                activeModel={shownModel}
                onSelectModel={name => switchModel(name)}
                filter={modelFilter}
                setFilter={setModelFilter}
                onClose={() => setModelDropdown(false)}
                hasEffort={!!(activeSlot && provider.capabilities.reasoningEffort && modelSupportsEffort(shownModel === 'auto' ? '' : shownModel))}
                slot={activeSlot}
                currentEffort={currentSlot?.reasoning_effort || ''}
                defaultEffort={defaultEffort}
                effortLevelsOverride={remoteCrew.isRemote ? (remoteCrew.capabilities?.effort_levels ?? []) : undefined}
                onSetDefault={() => {
                  setModelDropdown(false)
                  navigate(settingsPath({ tab: 'chat', highlight: SETTINGS_DEFAULT_MODEL_ID }))
                }}
                agentName={_modelPinAgent}
                pinModelName={_modelPinActive || 'auto'}
                pinModelUnavailable={pinIsWithheld(_modelPinActive, shownModel)}
                pinnedToAgent={_modelPinPinned}
                onPinToAgent={() => {
                  setModelDropdown(false)
                  pinModelToAgentMut.mutate({
                    agent: _modelPinAgent,
                    // The slot's REAL model, never the display fallback: a
                    // stale/degraded list must not be able to persist 'auto'
                    // over a pin the account actually has.
                    model: _modelPinActive === 'auto' ? '' : _modelPinActive,
                  })
                }}
              />,
              document.body
            )}
            {/* Project picker — triggered from input bar */}
            <ProjectPicker
              open={projectPickerOpen}
              onOpenChange={setProjectPickerOpen}
              anchorRect={projectBtnRect}
              onSelect={path => { setProject(path); setProjectPickerOpen(false) }}
            />
            {/* Reasoning effort dropdown portal */}
            {reasoningEffortDropdown && reasoningEffortBtnRect && activeSlot && provider.capabilities.reasoningEffort && modelSupportsEffort(shownModel === 'auto' ? '' : shownModel) && createPortal(
              <div ref={reasoningEffortDropdownRef} className="fixed z-[9999] animate-slide-up" style={(() => { const left = Math.max(8, Math.min(reasoningEffortBtnRect.left, window.innerWidth - 220)); return { bottom: window.innerHeight - reasoningEffortBtnRect.top + 4, left: isMobile ? 8 : left, ...(isMobile ? { right: 8, maxWidth: 'calc(100vw - 16px)' } : {}) } })()}>
                <ReasoningEffortDropdown slot={activeSlot} currentEffort={currentSlot?.reasoning_effort || ''} defaultEffort={defaultEffort} levelsOverride={remoteCrew.isRemote ? (remoteCrew.capabilities?.effort_levels ?? []) : undefined} onClose={() => setReasoningEffortDropdown(false)} />
              </div>,
              document.body
            )}
            </div>
          </div>
          </SearchHighlightContext.Provider>
        )}
      </div>
      )}
      {search.isOpen && (
          <DetailPanel
            key="search-panel"
            title={<SearchBar docked term={search.term} setTerm={search.setTerm} matches={search.matches} currentIdx={search.currentIdx} next={search.next} prev={search.prev} close={search.close} caseSensitive={search.caseSensitive} toggleCaseSensitive={search.toggleCaseSensitive} focusNonce={search.focusNonce} goTo={search.goTo} scopeLimited={searchScopeIsLimited({ slotHasMore, cursorIsForActiveSlot })} />}
            onClose={search.close}
            initialWidth={400}
            minWidth={320}
            reserveWidth={panelReserve}
            storageKey="mc-search-width"
            noPadding
          >
            {search.matches.length > 0 ? (
              <SearchResultsList
                matches={search.matches}
                currentIdx={search.currentIdx}
                messages={messages}
                term={search.term}
                caseSensitive={search.caseSensitive}
                onJump={jumpToSearchResult}
              />
            ) : (
              <div className="px-4 py-3 text-[13px] text-muted">{search.term ? i18nT('pages.chatPage.no_results') : i18nT('pages.chatPage.type_to_search_this_conversation')}</div>
            )}
          </DetailPanel>
        )}
      <AnimatePresence initial={false}>
        {/* Inline side panel — mobile / embed frames where there's no actbar
            grid column. Desktop uses the actbar portal below. */}
        {(isMobile
          // A live app tab stays mounted while hidden so its iframe and drawing
          // survive a close; an in-flight close must also remain mounted until
          // its compositor transform reaches the parked position.
          ? (sideOverlayPhase !== 'closed'
              || (shouldMountSidePanel({ activityOpen, hasLiveAppTab, hasBrowserTab, searchOpen: search.isOpen })
                  && isSidePanelHidden({ activityOpen, hasLiveAppTab, hasBrowserTab, searchOpen: search.isOpen }))) && !activitySlot
          : shouldMountSidePanel({ activityOpen, hasLiveAppTab, hasBrowserTab, searchOpen: search.isOpen }) && !activitySlot) && (
          <motion.div
            key="side-panel-inline"
            ref={isMobile ? sideOverlayPanelRef : undefined}
            initial={isMobile ? false : { width: 0 }}
            animate={isMobile ? undefined : { width: 'auto' }}
            exit={isMobile ? undefined : { width: 0 }}
            transition={isMobile ? undefined : { duration: 0.4, ease: [0.32, 0.72, 0, 1] }}
            // Mobile owns the content area below the shell chrome, so it can
            // slide as a fixed overlay instead of reflowing the transcript.
            className={isMobile
              ? 'fixed top-safe-offset-[42px] bottom-safe left-safe right-safe z-[47] flex justify-end bg-bg'
              : 'h-full overflow-hidden flex justify-end shrink-0'}
            // Kept mounted for a live app tab: hide instead of unmounting so the
            // iframe (and the drawing inside it) survives a panel close. The
            // mobile offset stays bound to the MotionValue so drag frames paint.
            style={isMobile
              ? (sideOverlayPhase === 'closed' ? { display: 'none' } : { x: sideOverlayX })
              : (isSidePanelHidden({ activityOpen, hasLiveAppTab, hasBrowserTab, searchOpen: search.isOpen }) ? { display: 'none' } : undefined)}
          >
            <SidePanel
              tabsCtl={tabsCtl}
              slot={activeSlot || ''}
              panelHidden={isSidePanelHidden({ activityOpen, hasLiveAppTab, hasBrowserTab, searchOpen: search.isOpen })}
              onFileOpen={handleFileOpen}
              onArtifactOpen={handleArtifactOpen}
              onAddToContext={handleAddToContext}
              projectDir={currentSlot?.project || undefined} navLinks={chatNav.links} navResolving={chatNav.resolving}
              sources={panelSources} selectedSourceUrl={selectedSourceUrl} onSelectSource={selectSourceUrl} onReconcileSource={reconcileSourceUrl}
              issues={panelIssues} selectedIssueUrl={selectedIssueUrl} onSelectIssue={selectIssueUrl} onReconcileIssue={reconcileIssueUrl}
              onAddSourceToChat={addSourceCommentToChat}
              onSubmitComments={submitComments} connected={connected} onFileSave={handleFileSave} onClose={toggleAct}
              pins={chatPins} pinsLoading={chatPinsLoading} onJumpToPin={handleJumpToPin} onUnpin={handleUnpinById}
              slotTitle={activeSlotTitle} chatMode={mode}
              expanded={panelMaximized}
              fillWidth={panelFillWidth}
              canDockBottom={false}
            />
          </motion.div>
        )}
      </AnimatePresence>
      {/* Full-height tabbed side panel: portaled into the App shell's
          'actbar' grid column so it spans the window top-to-bottom; the header
          row ends at its left edge, shifting the top-bar buttons left.
          The motion wrapper animates the column width 0 -> auto: the actbar
          grid column tracks it frame-by-frame, so the chat pane slides left in
          sync while the panel (right-anchored via justify-end) slides out from
          the window edge — both sides move together instead of snapping. */}
      {activitySlot && createPortal(
        <AnimatePresence initial={false}>
          {shouldMountSidePanel({ activityOpen, hasLiveAppTab, hasBrowserTab, searchOpen: search.isOpen }) && (
            <motion.div
              key="side-panel"
              initial={sidePanelDockAnim.initial}
              animate={sidePanelDockAnim.animate}
              exit={sidePanelDockAnim.exit}
              transition={{ duration: 0.18, ease: [0.2, 0, 0, 1] }}
              className={sidePanelDock === 'bottom' ? 'w-full overflow-visible flex flex-col justify-end' : 'h-full overflow-visible flex justify-end'}
              style={isSidePanelHidden({ activityOpen, hasLiveAppTab, hasBrowserTab, searchOpen: search.isOpen }) ? { display: 'none' } : undefined}
            >
              <SidePanel
                tabsCtl={tabsCtl}
                slot={activeSlot || ''}
                panelHidden={isSidePanelHidden({ activityOpen, hasLiveAppTab, hasBrowserTab, searchOpen: search.isOpen })}
                onFileOpen={handleFileOpen}
                onArtifactOpen={handleArtifactOpen}
                onAddToContext={handleAddToContext}
                projectDir={currentSlot?.project || undefined} navLinks={chatNav.links} navResolving={chatNav.resolving}
                sources={panelSources} selectedSourceUrl={selectedSourceUrl} onSelectSource={selectSourceUrl} onReconcileSource={reconcileSourceUrl}
              issues={panelIssues} selectedIssueUrl={selectedIssueUrl} onSelectIssue={selectIssueUrl} onReconcileIssue={reconcileIssueUrl}
              onAddSourceToChat={addSourceCommentToChat}
                onSubmitComments={submitComments} connected={connected} onFileSave={handleFileSave} onClose={toggleAct}
                pins={chatPins} pinsLoading={chatPinsLoading} onJumpToPin={handleJumpToPin} onUnpin={handleUnpinById}
                slotTitle={activeSlotTitle} chatMode={mode}
                expanded={panelMaximized}
                fillWidth={panelFillWidth}
              />
            </motion.div>
          )}
        </AnimatePresence>,
        activitySlot
      )}
    </div>
    </JiraHostsCtx.Provider>
    </TagPopoverProvider>
    </RowDisclosureProvider>
  )
}
