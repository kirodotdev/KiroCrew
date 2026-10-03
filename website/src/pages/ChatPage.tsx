import { useContext, Fragment, useState, useRef, useCallback, useEffect, useLayoutEffect, useMemo } from 'react'
import { WorkspacePanelContext, WorkspaceFullscreenContext } from '../components/WorkspacePanelContext'
import { createPortal } from 'react-dom'
import { useLocation, useNavigate, useNavigationType, useSearchParams } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { useIsMobile } from '../hooks/useIsMobile'
import { useShellSlot } from '../hooks/useShellSlot'
import { useMobileNavRail } from '../components/MobileNavRailContext'
import { useVisualViewport } from '../hooks/useVisualViewport'
import { useAnchoredTriggerRect } from '../hooks/useAnchoredTriggerRect'
import { useRailWidth } from '../hooks/useRailWidth'
import { SETTINGS_DEFAULT_MODEL_ID } from '../hooks/useSettingHighlight'
import { settingsPath } from '../components/settingsPath'
import { isTouchDevice } from '../utils/isTouchDevice'
import { agentOrDefaultLabel } from '../utils/agentLabel'
import { toApiDecision } from '../utils/approvalDecision'
import { isHiddenInvisibleAssistantRow } from '../utils/invisibleText'
import { mergeRenderers, resolveRenderer, type MessageRenderer, type MessageRenderContext } from '../app-sdk/messageRenderers'
import { createTranscriptRenderers } from './chat/transcriptRenderers'
import { useDrawerSwipe, animateDrawer, registerDrawerTargets, takeOverDrawer, safeAreaLeft } from '../hooks/useDrawerSwipe'
import { useAppSelector, useAppDispatch, useAppStore, store, type AppDispatch } from '../store'
import { useConnected } from '../hooks/useConnected'
import { queuedSendStash } from '../hooks/useQueuedMessageActions'
import { useChatPopouts } from '../hooks/useChatPopouts'
import {
  switchSlot, createSlot, deleteSlot, loadOlderMessages, abortActiveOlderFetch, isSupersededPagingRejection, appendMessage, appendSlotMessage, endLocalTurn, forkSlot,
  setSlotRunning, startLocalTurn, syncSlotRunningFromServer, setPendingInput, setAgentSwitchNotice, resolveByApprovalId, selectComposerBusy, selectSendConfirmed,
  setVoiceAudio,
  toggleActivity, openActivityPanel, openActivityToTab,
  selectSubagent,
  truncateAfterIndex, replaceMessages,
  pendingQuestionFor, clearFollowupCard, dismissFollowupItem, clearFolderSuggestion, ageFolderSuggestion,
  capturePendingAskId, confirmOptimisticSend, markSendUnconfirmed, resolveOptimisticSteer,
  requestSlotReveal,
  requestFolderReveal,
  mcpAppKey,
  } from '../store/chatSlice'
import { confirmedDelivered } from '../utils/sendDelivery'
import { sendTurn } from '../chat-core/transport/sendTurn'
import { applySteerReceipt } from '../chat-core/transport/steerReceipt'
import { useSelectionQuoteAsk } from '../chat-core/composer/selectionActions'
import { addNotification, removeNotificationByTs } from '../store/notificationsSlice'
import { useDeleteTerminalSession } from '../components/CliPanel'
import { interceptSlashCommand, isInterceptedSlashCommand } from './chat/ChatInput'
import { updateSlot, slotIsRemoteBound } from '../store/dashboardSlice'
import { inFlightSlotSwitchOutcome, performSlotSwitch, stagedSlotSwitchTarget } from '../lib/slotSwitch'
import { performAgentSlotSwitch } from '../lib/agentSwitch'
import { api } from '../api/client'
import { resolveAskAfterSend } from '../lib/resolveAskAfterSend'
import type { PlanStepInput } from '../api/client'
import { useProvider } from '../providers'
import { safeSetItem, safeSetSessionItem } from '../utils/safeStorage'
import { EmptyState, Btn, Input } from '../components/ui'
import { type FileChangeEntry } from '../components/FileChangeChips'
import { ChatTranscriptSkeleton } from '../components/ChatTranscriptSkeleton'
import SnipOverlay from '../components/SnipOverlay'
import CollapsibleToolGroup from './chat/CollapsibleToolGroup'
import { isSystemNoticeRow } from './chat/CompactionCard'
import { decisionStripFieldOf } from './chat/decisionRecord'
import { RowDisclosureProvider } from './chat/rowDisclosure'
import { useJevAutoSend } from './chat/useJevAutoSend'
import type { DisplayItem, TurnItem } from './chat/types'
import { MeasureFarm } from '../hooks/virtualizer/MeasureFarm'
import { useScrollManager } from './chat/useScrollManager'
import { shouldPaginateOlder, shouldContinueOlderWalk, canForkAtWindow, searchScopeIsLimited, earlierAffordanceInView, EARLIER_BAR_SELECTOR, OLDER_WALK_MAX_PAGES_PER_INPUT } from './chat/pagination'
import {
  ChatHeaderMenu,
  KnowledgeBubbleChip,
  messageRowKey,
  mintSendId,
  msgIdentityKey,
  renderUserContent,
  turnLeadKey,
  } from './chat/ChatPageMessageContent'
import { useChatPageTranscriptEarlyController } from './chat/useChatPageTranscriptController'
import { useChatPageSessionController } from './chat/useChatPageSessionController'
import { useChatPageResourcesController } from './chat/useChatPageResourcesController'
import { useComposerDraftLifecycle, useComposerDraftStores, useStagedDraftPersistence } from './chat/page/composerDrafts'
import { useComposerStaging, useStagedFolderRefs } from './chat/page/composerStaging'
import { useFileMentionActions } from './chat/page/composerFileMentions'
import { useChatEventBridges, useColdFileTabHydration } from './chat/page/eventBridges'
import { uniqueNotificationTs } from './chat/page/notificationTs'
import { useWelcomeState } from './chat/page/welcomeState'
import { useSessionRosters } from './chat/page/sessionRosters'
import { useAppLaunchIntake, useAutoSendIntake, useErrorHandoffIntake, usePendingInputIntake } from './chat/page/launchIntake'
import { useFollowUpChips } from './chat/page/followUpChips'
import { useComposerSessionControls, useSessionControlChips } from './chat/page/sessionControls'
import { useComposerChips } from './chat/page/composerChips'
import { useTurnRecovery } from './chat/page/turnRecovery'
import { useComposerAboveBand, useComposerDockMetrics } from './chat/page/composerDock'
import { useStableRowKeys, useTranscriptRows } from './chat/page/transcriptRows'
import { useBusyTurnControls } from './chat/page/busyTurnControls'
import { useSessionAutomation } from './chat/page/sessionAutomation'
import { useTranscriptJumps } from './chat/page/transcriptJumps'
import ChatPaneNotices from './chat/page/ChatPaneNotices'
import MobileTopBar from './chat/page/MobileTopBar'
import EarlierMessagesBar from './chat/EarlierMessagesBar'
import TranscriptScrollShell, { useTranscriptWidth } from './chat/TranscriptScrollShell'
import { devLog, devWatchMessages, inspectorOn } from '../dev/scrollInspector'
import TurnNavigationMinimap from './chat/TurnNavigationMinimap'
import { useVirtualChat } from '../hooks/virtualizer/useVirtualChat'
import { prepareSendPayload, serializeDirTokens } from '../utils/fileTokens'
import { carryPastes, expandAll as expandPasteTokens, mergeCarriedDraft, pruneBlocks as pruneBlocksUtil, saveStoredPaste } from '../utils/pasteTokens'
import { extractPromptFromToken, extractSlackContextFromToken } from '../utils/tokenPrompt'
/** Map message index → displayItems index, for scroll-to-match and the turn minimap. */
function buildMessageToDisplayIdx(items: DisplayItem[]): Map<number, number> {
  const map = new Map<number, number>()
  items.forEach((item, di) => {
    if (item.kind === 'turn') {
      for (const ti of item.items) {
        if (ti.kind === 'single') map.set(ti.idx, di)
        else if (ti.kind === 'group') ti.msgs.forEach((_, mi) => map.set(ti.startIdx + mi, di))
      }
    } else if (item.kind === 'single') map.set(item.idx, di)
    else if (item.kind === 'group') item.msgs.forEach((_, mi) => map.set(item.startIdx + mi, di))
  })
  return map
}
/** Delay (ms) before scrolling to bottom after a state update, giving React time to commit. */
const SCROLL_AFTER_RENDER_MS = 100
/** Min gap between scroll-gesture-driven retries of a failed older-history
 * page. One request per gesture on a dead link, not one per scroll event. */
const OLDER_RETRY_COOLDOWN_MS = 1500
/** Cadence of the level-triggered top-parked pagination poll. Slow on purpose:
 * it is the backstop that guarantees progress while the reader holds the top,
 * not the fast path (the sentinel/crossing triggers still fire first). */
const OLDER_TOP_POLL_MS = 700
/**
 * How long after the reader's last scroll the top-of-transcript walk keeps
 * paging. Past this they have stopped climbing, and a page landing then is
 * movement they did not ask for.
 */
const OLDER_WALK_ACTIVE_MS = 1500
// Idle prefetch: quiet time required before background pages load, and the
// poll cadence. Quiet > the farm's deep-idle threshold is deliberate — the
// farm gets first claim on idle time; prefetch only runs once it is caught up.
// A page may LAND only after the scroller has been still this long: landing
// compensation writes scrollTop, and mid-momentum writes fight the fling.
const OLDER_LANDING_SETTLE_MS = 400
/** A transcript at least this much taller than its viewport is scrollable
 *  in earnest: the reader can climb to ask for history, so nothing fetches
 *  it for them. Below it, auto-fill is load-bearing (no scrollbar exists). */
const OLDER_FILL_SLACK_PX = 120

/** Whether a top-sentinel fire may auto-fetch history for a reader who did
 *  not climb. Exported for tests: this single predicate is what separates
 *  the load-bearing short-transcript fill (no scrollbar exists, so nothing
 *  else CAN load history) from the boot-transient page chain that walked
 *  megabytes over a parked phone after every refresh.
 *
 *  Keyed on INPUT, not on follow: a boot that restores a saved scroll
 *  anchor releases follow before the reader has touched anything, and a
 *  follow-based gate read that as a climbing reader -- the replica probe
 *  showed the walk running at one page per ~8s through that door with
 *  zero input events. Real wheel/touch input is the only signal that a
 *  fetch is reader-initiated; the sole inputless exception is a transcript
 *  too short to scroll, where the fill is load-bearing (no scrollbar
 *  exists, so nothing else could ever load its history). */
export function shouldAutoFillOlder(g: { scrollHeight: number; clientHeight: number; sawInput: boolean }): boolean {
  if (g.scrollHeight <= g.clientHeight + OLDER_FILL_SLACK_PX) return true
  return g.sawInput
}
/** How long one real gesture authorizes an automatic older-history fetch.
 *
 *  It is a WINDOW and not a latch because the latch was the defect. A landing's
 *  own compensation writes scrollTop, which fires a `scroll` event, so with
 *  permanent authorization the automatic doors ran land -> quiet -> land at a
 *  steady beat over a reader who was not asking for any of it. Only `wheel` and
 *  `touchmove` refresh this stamp, and our own writes produce neither, so the
 *  window ages out on its own: a gesture burst buys a bounded run of pages, not
 *  the rest of the session. */
const REAL_GESTURE_AUTH_MS = 20000

/**
 * Height of the transcript's tail spacer, in px.
 *
 * This plus the scroller's own dock clearance is the gap between the last line of
 * the transcript and the top of the composer dock when the reader is at the
 * bottom. It is a px value and not `vh` on purpose: as `2vh` the clearance tracked
 * the viewport and the margin was one pixel at 844px tall, so every shorter
 * viewport — i.e. every phone — landed the last line under the dock.
 */
const TRANSCRIPT_TAIL_SPACER_PX = 16

/**
 * Breathing room, in px, between the composer dock's top edge and the last line of
 * the transcript, on top of the dock's own measured height.
 *
 * The transcript scroller runs the full height of the pane and the dock floats
 * over its bottom edge (iOS toolbar layout), so the scroller's `paddingBottom` is
 * `dockH + this`: exactly the strip the dock covers, plus this margin, so the last
 * line stops clear of the glass instead of under it. There is no opaque fade band
 * between the two any more — the transcript scrolls under the glass and the
 * material's own blur and tint are what keep the dock legible over it.
 * ChatPage.dockClearance.test.tsx pins the wiring.
 */
const DOCK_CLEARANCE_PX = 16
/**
 * Strip of screen the mobile sessions drawer deliberately leaves uncovered, so a
 * sliver of the conversation behind it stays visible.
 *
 * ONE number, because it has to be two things at once: the panel's rendered
 * width (`viewport - this`) and the offset its slide travels. Spelling the width
 * as a `max-w-[calc(100vw-2.5rem)]` class while the travel read `innerWidth` is
 * exactly how they drifted apart — the panel finished sliding 40px before its
 * animation did, so the ease-out's tail moved an already-offscreen panel.
 */
const DRAWER_UNCOVERED_PX = 40
// No arbitrary cap on pinned-jump page loads: the loop terminates when the
// target message is found OR history is exhausted (!slotHasMore / null result).
// The `cancelled` flag in the useEffect cleanup and the loadOlderMessages null
// sentinel prevent infinite loops.  A ref tracks loads for diagnostics only.
// Canonical home is utils/navIntent (shared with the popout nav-intent
// applier); re-exported here for this page's historical importers.
export { PREFILL_STORAGE_KEY } from '../utils/navIntent'
import { PREFILL_STORAGE_KEY, writePrefill } from '../utils/navIntent'
import WelcomeView from '../components/WelcomeView'
import { MemoryModeChip, type MemoryMode } from '../components/MemoryModeChip'
import { openPanelView, claimAppAutoOpen } from '../hooks/usePanelTabs'
import { useFilteredDropdown } from '../hooks/useFilteredDropdown'
import { effortToCarry, filterInteractiveModels, legacyCodexEffort, shouldSeparateModelEffort, switchGroupedModel, useModelPickerConfigured, useModelPickerHiddenModelsQuery } from '../hooks/useInteractiveModels'
import { isUnpinnedModel, JEV_ROUTE_MODEL, jevRouteOffered, jevRouteShownModel, withJevRoute } from '../lib/jevRoute'
import { useListboxKeyboard } from '../hooks/useListboxKeyboard'
import AgentDropdownList, { DefaultAgentRow, ManageAgentsFooter } from '../components/AgentDropdownList'
import { agentSwitchFailureMessage } from '../utils/agentSwitchFeedback'
import ProjectPicker from '../components/ProjectPicker'
import InboundLinkChip from '../components/InboundLinkChip'
import ModelEffortDropdown from '../components/ModelEffortDropdown'

import ChatInput from '../components/ChatInput'
import { useStableCallbackProps } from './chat/useStableCallbackProps'
import { useLanguageGeneration } from '../i18n/useLanguageGeneration'
import { promptHistoryFromMessages, samePromptHistory, type PromptHistoryItem } from '../components/composerPromptHistory'
import SessionControlHost from '../components/SessionControlHost'
import ErrorNotice from '../components/ErrorNotice'
import ChatDropOverlay from '../components/ChatDropOverlay'
import SessionGridView from '../components/SessionGridView'
import SessionTabStrip from '../components/SessionTabStrip'
import { anchorForSlot, loadLayout, sessionSlots } from '../hooks/splitLayoutStore'
import { mcpAppTabTitle } from '../lib/mcpAppSrcdoc'
import { countCompletedTurns } from '../lib/completedTurns'
import { pinIsWithheld } from '../lib/model'
import { slotApprovalMode } from '../utils/slotApprovalMode'
import FollowUpCard from '../components/FollowUpCard'
import { useMoveSlotToFolder } from '../hooks/useMoveSlotToFolder'
import PendingQuestionCard from '../components/PendingQuestionCard'
import SessionPulseSurveyCard from '../components/SessionPulseSurveyCard'
import type { FollowupItem } from '../store/chatSlice'

// Stable identity for the "no follow-up cards" case: returning a fresh {} from
// the selector would make it a new reference on every store update.
const EMPTY_FOLLOWUPS: Record<string, { items: FollowupItem[]; ts: number }> = {}
import FlyingQuote from '../components/FlyingQuote'
import SearchHighlightContext, { MessageSearchScope } from '../hooks/SearchHighlightContext'
import SearchBar from '../components/SearchBar'
import SearchResultsList from '../components/SearchResultsList'
import QueueStack, { SubagentDeliveryProgress } from '../components/QueueStack'
import { runBelongsToSlot } from '../apps/workflows/runModel'
import { useTipTrigger } from '../components/TipCard'
import { Composer, type ComposerHandle, type ComposerVoiceOptions } from '../chat-core/composer/Composer'
import { createComposerDraftStore, useComposerDraft, useComposerDraftSelector, type ComposerDraftStore } from '../chat-core/composer/draftStore'
import { ChatFooter, AssistantMessage, UserMessage, PinnedPrompt } from './chat'
import { pinCandidateKey } from './chat/usePinnedPrompt'
import { useStreamIdle } from './chat/ChatFooter'
import type { TurnStats } from './chat/AssistantMessage'
import { prevUserTextFor } from './chat/share/shareSupport'
import { turnHadPolicyBlock } from '../app-sdk/turnPolicyBlock'
import MarkdownRenderer from '../components/MarkdownRenderer'
import { JiraHostsCtx } from '../lib/jiraHosts'
import { SidebarFolderCtx, type SidebarFolderActions } from '../components/markdown/contexts'
import MessageErrorBoundary from '../components/MessageErrorBoundary'
import SessionTitleControl from './chat/SessionTitleControl'
import { useChatNavigation } from '../hooks/useChatNavigation'
import { useChatPins } from '../hooks/useChatPins'
import SubagentProgressBar from './chat/SubagentProgressBar'
import CommandCenterDock from './chat/command-center/CommandCenterDock'
import { usePreviewFlag } from '../hooks/usePreviewFlag'
import { PREVIEW_DASHBOARD } from '../utils/previewFlags'
import TaskProgressBar from './chat/TaskProgressBar'
import SidePanel, { CHAT_PANE_MIN_W, sidePanelFillWidth } from './chat/SidePanel'
import { useSidePanelDock } from '../hooks/useSidePanelDock'
import { REASONING_ROLES, stripAppEnvelope } from './chat/groupDisplayItems'
import { PREVIEW_EXPAND_EVENT } from '../components/WebPreviewPanel'
import ChatSidebar from './ChatSidebar'
import { SIDEBAR_MIN, SIDEBAR_MAX, clampSidebarWidth } from './chat/sidebarWidth'
import { mergeIntoDraft, mergeRecoveredDraft, setDraft } from '../utils/chatDrafts'
import { setFileDraft } from '../utils/chatFileDrafts'
import { setPasteDraft } from '../utils/chatPasteDrafts'
import { setSessionRefDraft } from '../utils/chatSessionRefDrafts'
import { mergeSessionRefs, appendSessionRefLinks } from '../utils/sessionRefs'
import { commitRevealedSource, parseSourceLinkUrl, type SourceLinkKind } from '../utils/pullRequestLinks'
import { parseOptions } from '../app-sdk/protocol'
import { isNoteRow } from '../lib/noteContract'
import OverlayDrawer from '../components/OverlayDrawer'
import { loadChatConfig, CONTENT_WIDTH, type ChatConfig } from './chat/ChatSettings'
import { scaleContentWidth } from './chat/contentWidth'
import SessionFlyout, { TOGGLE_RECT } from './chat/SessionFlyout'
import { focusComposer, focusComposerAfter, revealComposer } from './chat/composerFocus'
import { isStaleProjectDirError, resolveFolderAgent, resolveFolderProjectDir } from '../utils/folderAgent'
import { useHoverIntent } from '../hooks/useHoverIntent'
import { useKnowledgeFetch, extractKnowledgeQuery, expandKnowledgeBlock } from './chat/useKnowledgeFetch'
import { KnowledgePicker } from './chat/KnowledgePicker'
import { MessageSquare, Clock, AppWindow, Undo2, Columns2, ExternalLink } from 'lucide-react'
import { EdgeFade, JumpToBottomButton } from '../app-sdk/ChatScrollChrome'
import { PanelLeftSolid, PanelLeftLight } from '../components/icons/panels'
import PanelToggles, { PANEL_HEADER_ACTION_CLS, PANEL_HEADER_ACTIONS_CLS } from '../components/PanelToggles'

import SlotTagPopover from '../components/SlotTagPopover'
import { TagPopoverProvider } from '../hooks/useTagPopover'

import { AnimatePresence, motion, useMotionValue, useTransform } from 'framer-motion'
import DetailPanel from '../components/DetailPanel'

import type { ChatMessage } from '../types'

import { shouldMountSidePanel, isSidePanelHidden, sidePanelDockMotion, SIDE_PANEL_DOCK_TRANSITION } from './chat/sidePanelMount'
import type { ParsedSubagentCompletion } from './chat/subagentCompletion'
import { useConnectionsUiEnabled } from '../hooks/useConnectionsUi'
import TurnBlock from './chat/TurnBlock'
import Clickable from '../components/Clickable'
import WorkflowProgressBar from './chat/WorkflowProgressBar'
import { tryQuickSend } from '../lib/quickSend'
import { rewindWithRollback } from '../lib/rewindCall'
import { isChatPageSurface } from '../utils/channelOrigin'
import { sessionTitleRoster } from '../utils/sessionRoster'
import { errMessage } from '../utils/thunkError'


import { i18nT } from '../i18n/t'
import { fmtDateFields } from '../i18n/format'
import { fmtMessageTime, fmtMessageTimeFull } from './chat/messageTime'
import { fetchDashboardConfig } from '../api/dashboardConfigQuery'

/**
 * Horizontal room the chat pane reclaims (negative margin) while the desktop
 * sessions sidebar is collapsed: the gap the open panel kept between itself and
 * the pane. Every pane-space offset that clears the stationary toggle adds it.
 */
const COLLAPSED_PANE_RECLAIM_PX = 8
/**
 * Leading clearance for the open-session strip while the desktop sessions
 * sidebar is collapsed. The pane's reclaimed margin moves the toggle's
 * container-space right edge (x + size) that much farther into the strip; the
 * final 4px keeps the first tab visibly separate from the control.
 */
const COLLAPSED_SESSION_TABS_INSET = TOGGLE_RECT.x + TOGGLE_RECT.size + COLLAPSED_PANE_RECLAIM_PX + 4
/**
 * Human-readable reason from a rejected thunk. `unwrap()` rejects with RTK's
 * SERIALIZED error — a plain object, never an `Error` instance — so an
 * `instanceof Error` test always fails and every user would read the developer
 * fallback. Read `message` structurally instead, with a plain-language fallback.
 */


const createFailReason = (e: unknown): string => {
  const msg = typeof e === 'object' && e !== null ? (e as { message?: unknown }).message : undefined
  return typeof msg === 'string' && msg.trim() ? msg : 'the server did not respond'
}

/** The error hand-off's activation of the slot it just created
 *  (useErrorHandoffIntake). `keepTargetOnMissing`: that slot was JUST created,
 *  so a 404 from its detail fetch is a create/fetch race on a slot that exists
 *  -- the reducer keeps it selected instead of unwinding to the previous chat
 *  (#6309). Defined here because this page owns the classified call site. */
const activateHandoffSlot = (dispatch: AppDispatch, key: string) => dispatch(switchSlot({ key, keepTargetOnMissing: true })).unwrap()

/** ChatPage's tool rows derive their auto-denied state inside ToolCallLine;
 *  the registry default that reads this set is never reached here. Frozen and
 *  shared so the per-row context does not allocate. */
const NO_AUTO_DENIED = new Set<string>()

// `mentionBoundary` / `strictMentionBoundary` / `leadingMentionBoundary`
// moved to utils/fileTokens.ts: the reconciliation staleness check, the
// remove-chip strip and send serialization share one definition there and
// can never disagree about what counts as a boundary.

/** Stable empty set so the mcpApps-derived selector returns a referentially
 *  equal value when the slot has no app renders (avoids useless re-renders). */
const EMPTY_APP_ID_SET: ReadonlySet<string> = new Set()

// Per-action titles for the refused-press notice above the composer. A press
// added later gets its refusal surfaced by adding one entry here and calling
// `showRefusedPress` from its catch — the `as const` map keeps every key
// statically resolvable for the catalog-key gate.
const REFUSED_PRESS_TITLE_KEYS = {
  continue: 'pages.chatPage.could_not_continue',
  regenerate: 'pages.chatPage.could_not_regenerate',
  switch_variant: 'pages.chatPage.could_not_switch_variant',
  // Two keys because the two /side failures leave different visible states: a
  // refused open leaves no panel, a refused turn leaves the panel open beside
  // the notice — "couldn't open" over an open panel contradicts what the user sees.
  side_open: 'pages.chatPage.could_not_open_side_chat',
  side_turn: 'pages.chatPage.could_not_send_to_side_chat',
} as const
type RefusedPressAction = keyof typeof REFUSED_PRESS_TITLE_KEYS


/**
 * Where a jump-to-message came from, because the three entry points owe the
 * reader different copy when the target cannot be found.
 *
 *  - `pin`     the pins list, so pin wording is accurate;
 *  - `earlier` the earlier-messages control, which has its own paging copy;
 *  - `link`    a `?msg=` share link, minted by copy-link-to-message for ANY
 *              message. That reader may never have pinned anything, so naming a
 *              pin would report an action they did not take.
 */
type PendingJumpOrigin = 'pin' | 'earlier' | 'link'

/** SINGLE writer for the not-found copy, so a new origin cannot reach the reader
 *  wearing another origin's wording. */
const jumpUnavailableNotice = (origin: PendingJumpOrigin): string =>
  origin === 'earlier' ? i18nT('components.chatPane.earlier_messages_unavailable')
    : origin === 'link' ? i18nT('pages.chat.deepLink.message_unavailable')
      : i18nT('pages.chat.pins.message_unavailable')


/**
 * The main composer, with its callback props held at stable identities. This
 * page builds most of them inline on every render; the forwarders let
 * `memo(ChatInput)` skip a page render (a pin, a streamed frame) that changes
 * nothing the composer shows.
 */
function StableChatInput(props: React.ComponentProps<typeof ChatInput>) {
  return <ChatInput {...useStableCallbackProps(props)} />
}


/** Draft facts ChatPage renders from; each re-renders the page only when it flips. */
const isNonBlank = (text: string) => text.trim() !== ''

/**
 * The per-commit side of the composer draft: mirrors the committed text into
 * ChatPage's `inputRef` during render and
 * hands each committed change to `onCommit` (persist the slot draft, feed the
 * history suggestions). A child of ChatPage, so its effect runs before the
 * page's own effects in the same commit. Renders nothing, and it is the only
 * part of the page that re-renders on a keystroke.
 */
function ComposerDraftSync({ store, inputRef, onCommit }: {
  store: ComposerDraftStore
  inputRef: React.MutableRefObject<string>
  onCommit: (text: string) => void
}) {
  const text = useComposerDraft(store)
  inputRef.current = text
  useEffect(() => { onCommit(text) }, [text, onCommit])
  return null
}

export default function ChatPage({ mode, embedded, embedMode, popout, noUrlSync }: { mode?: string; embedded?: boolean; embedMode?: 'chat' | 'sessions'; popout?: boolean; noUrlSync?: boolean } = {}) {
  const dispatch = useAppDispatch()
  const moveSlotToFolder = useMoveSlotToFolder()
  const navigate = useNavigate()
  const navigationType = useNavigationType()
  const location = useLocation()
  const queryClient = useQueryClient()
  const provider = useProvider()
  // Kills a Run-in-terminal tab's backend PTY when the dispatch rolls back
  // (see useChatEventBridges' mc:run-in-terminal handler). Routed through the
  // same mutation the tab-close paths use (per the use-react-query guideline);
  // read through a ref because that handler's effect deliberately registers once.
  const deleteTerminalSession = useDeleteTerminalSession()
  const deleteTerminalSessionRef = useRef(deleteTerminalSession)
  deleteTerminalSessionRef.current = deleteTerminalSession
  const [searchParams, setSearchParams] = useSearchParams()
  // Declared with the other top-of-component hooks because the ?sid= URL-sync
  // effect reads it (mobile replaces rather than pushes a session switch), and
  // that effect is defined well above where the layout hooks start.
  const isMobile = useIsMobile()
  /**
   * Phone: the App shell's top bar is the ONE bar, and this page's title row
   * lives in it. The shell renders `#mobile-topbar-slot` (centre cell) and
   * `#mobile-topbar-trail-slot` (after the bell) only on the chat route below
   * the breakpoint; this page fills them through `createPortal` — the same
   * hand-off `#activity-bar-slot` uses on desktop. Both resolve to `null`
   * anywhere the shell does not render them (the popout window, an embedded
   * host, a desktop width), and the page then keeps its own inline title row
   * — so a missing slot degrades to today's layout rather than to no title.
   *
   * `mobileNavRail` is the shell's main navigation as an icon rail, rendered
   * beside the sessions pane inside this page's ONE drawer (see the
   * OverlayDrawer below). `null` off the phone chat route.
   */
  const singleBar = isMobile && !embedded && !popout
  const topbarSlot = useShellSlot('mobile-topbar-slot', singleBar)
  const topbarTrailSlot = useShellSlot('mobile-topbar-trail-slot', singleBar)
  const mobileNavRail = useMobileNavRail()
  /**
   * Whether the title row is in the shell's bar rather than inline. Derived
   * from the slot ELEMENT, not from `isMobile`: the two can disagree for a
   * render on a breakpoint crossing, and the row must be in exactly one place
   * — the inline copy stands down only once the portal target exists, and the
   * transcript's header spacer, the fixed sessions button and the split view's
   * inline toggle all key off this same flag so none of them doubles up.
   */
  const titleInTopbar = topbarSlot !== null
  // The memoized composer props below hold i18nT labels; their memo re-keys on a catalog load.
  const langGen = useLanguageGeneration()
  // The mobile sessions drawer and its scrim are `fixed` overlays that autofocus
  // a search input, so a software keyboard is open whenever they are. iOS Safari
  // shrinks only the VISUAL viewport for the keyboard (`interactive-widget`
  // default `resizes-visual`), so `fixed inset-0` / `top-safe`/`bottom-safe`
  // insets keep measuring the full layout viewport and strand the drawer's lower
  // content behind the keyboard. Inset both boxes to the visible band instead —
  // only on mobile; desktop morph mode is untouched.
  const vv = useVisualViewport()
  // Height of the band an interactive widget covers at the bottom of the fixed
  // positioning viewport — the same expression SidePanelLayout derives its own
  // keyboard inset from. 0 on desktop and whenever no keyboard is up, so every
  // consumer below is a strict no-op at rest.
  const keyboardInset =
    typeof window === 'undefined' ? 0 : Math.max(0, window.innerHeight - vv.offsetTop - vv.height)
  const slots = useAppSelector(s => s.dashboard.slots)
  // A user-facing switch gesture hit a session the server no longer has
  // (#6372); rendered through the pane ErrorNotice below (errors-use-error-notice).
  const switchSlotGone = useAppSelector(s => s.chat.switchSlotGone)
  // Unified chat view: show default and crew slots together.
  // App-owned worker slots (s.app) are excluded by the sidebar itself.
  const filteredSlots = useMemo(
    () => slots.filter(s => isChatPageSurface(s.surface ?? s.mode)),
    [slots],
  )
  const filteredSlotsRef = useRef(filteredSlots)
  filteredSlotsRef.current = filteredSlots
  const unreadSlots = useAppSelector(s => s.dashboard.unreadSlots)
  // Unified view: unread keys for all chat-like slots.
  const surfaceUnreadSlots = useMemo(
    () => {
      if (unreadSlots.length === 0) return []
      const visibleKeys = new Set(filteredSlots.map(s => s.key))
      return unreadSlots.filter(k => visibleKeys.has(k))
    },
    [unreadSlots, filteredSlots],
  )
  const refreshTrigger = useAppSelector(s => s.dashboard.refreshTrigger)
  const connected = useConnected()
  // Create-in-flight, so the flyout's New button can go inert exactly like the
  // sidebar's does instead of accepting a second click.
  const creatingSlot = useAppSelector(s => s.chat.creatingSlot)
  // Read synchronously by the slot-change effect, so held in a ref updated every
  // render. The same reducer case sets it and `activeSlot`, so the render that
  // sees the new active slot sees this too.
  const foregroundCreateId = useAppSelector(s => s.chat.foregroundCreateId)
  const foregroundCreateIdRef = useRef(foregroundCreateId); foregroundCreateIdRef.current = foregroundCreateId
  const lastCreatedActivation = useAppSelector(s => s.chat.lastCreatedActivation)
  const lastCreatedActivationRef = useRef(lastCreatedActivation); lastCreatedActivationRef.current = lastCreatedActivation
  // The one post-resolve answer for every resume entry point (#5925); rendered
  // above the composer, which is the only place all of them can see.
  const unresumableResume = useAppSelector(s => s.chat.unresumableResume)
  const undeletableHistory = useAppSelector(s => s.chat.undeletableHistory)
  const activeSlot = useAppSelector(s => s.chat.activeSlot)
  // The store this page is rendered under (not the module singleton): the
  // opener reads live state after an await, and it must be the same store
  // its dispatches went to. Also read by the MCP-app openers below, so it is
  // declared ahead of the auto-open effect.
  const boundStore = useAppStore()
  // Reveal eligible completed replies while recovery is offered, including an
  // older reply the user chose to read aloud. Slot identity prevents bleed-over.
  const [voiceRecoverySlot, setVoiceRecoverySlot] = useState<string | null>(null)
  // tool_call_ids in THIS slot that have a live MCP App render payload. Passed
  // to TurnBlock so app-bearing rows (which mount an interactive iframe) never
  // fold into a collapsible pane — collapsing hides the app, and re-expanding
  // remounts the iframe and loses in-canvas state. Kept here rather than inside
  // TurnBlock because that component is also rendered by app-sdk/ChatEmbed with
  // no Redux Provider mounted. The custom equality fn keeps the derived Set
  // referentially stable across unrelated chat-state updates.
  const appToolCallIds = useAppSelector(s => {
    const apps = s.chat.mcpApps
    if (!activeSlot || !apps) return EMPTY_APP_ID_SET
    const prefix = mcpAppKey(activeSlot, '')
    const ids = Object.keys(apps).filter(k => k.startsWith(prefix)).map(k => k.slice(prefix.length))
    return ids.length ? new Set(ids) : EMPTY_APP_ID_SET
  }, (a, b) => a.size === b.size && [...a].every(id => b.has(id)))
  // MCP Apps in the side panel (dashboard.mcp_app_panel, opt-in). When on, a new
  // render opens the panel to its own `app` tab instead of drawing inline in the
  // bubble — same auto-open path the web-preview marker uses.
  const { data: appPanelCfg, isError: appPanelCfgError } = useQuery<{ mcp_app_panel?: boolean; auto_open_git_panel?: boolean }>({
    queryKey: ['dashboardConfig'], queryFn: fetchDashboardConfig, staleTime: 30_000,
  })
  const mcpAppPanel = appPanelCfg?.mcp_app_panel === true
  // Opt-in: expand the side panel to the Git tab on sight of a git project
  // (dashboard.auto_open_git_panel). See the git-panel effect for why it is off
  // by default.
  const autoOpenGitPanel = appPanelCfg?.auto_open_git_panel === true
  // Whether that value is KNOWN yet. The git effect consumes a one-shot
  // localStorage marker, so acting while this query is still in flight would
  // burn the marker with the flag reading false and an opted-in user would never
  // get the panel. A FAILED query counts as known and resolves to the documented
  // default (off) — otherwise a config endpoint that is down would withhold the
  // Git tab itself, which the flag does not govern.
  const autoOpenGitPanelKnown = appPanelCfg !== undefined || appPanelCfgError
  // Tool-call ids already routed to a tab, so re-renders of the same app don't
  // yank focus back to the panel on every streaming update.
  useEffect(() => {
    if (!mcpAppPanel || !activeSlot) return
    for (const id of appToolCallIds) {
      // The claim lives at module scope, NOT in a ref: a ref is recreated on every
      // ChatPage mount, so a trip to Settings and back re-opened (and re-focused)
      // a tab the user had deliberately closed.
      if (!claimAppAutoOpen(activeSlot, id)) continue
      dispatch(openActivityPanel())
      // Chip title comes from the render payload already in the store -- the
      // payload IS what created this id (appToolCallIds keys off chat.mcpApps).
      // Read at effect time from the Provider-bound store (never the module
      // singleton, which a test harness does not mount) so unrelated chat
      // updates do not re-run the effect.
      const payload = boundStore.getState().chat.mcpApps?.[mcpAppKey(activeSlot, id)]
      tabsCtlRef.current?.openApp(id, mcpAppTabTitle(payload, i18nT('pages.chatPage.mcp_app_tab_title')), activeSlot)
    }
  }, [mcpAppPanel, activeSlot, appToolCallIds, dispatch, boundStore])

  const messages = useAppSelector(s => s.chat.messages)
    const probeServerTotal = useAppSelector(s => (activeSlot ? (s.chat.slotServerTotal?.[activeSlot] ?? -1) : -1))
  const probePrevMsgsRef = useRef(-1)
  useEffect(() => {
    devWatchMessages(messages.length, probeServerTotal)
    const prev = probePrevMsgsRef.current
    probePrevMsgsRef.current = messages.length
    if (prev >= 0 && messages.length !== prev && inspectorOn()) {
      const d = messages.length - prev
      devLog(d > 0 ? 'MSGS+' : 'MSGS-', `${prev}->${messages.length} (${d > 0 ? '+' : ''}${d}) total=${probeServerTotal}`)
    }
  }, [messages.length, probeServerTotal])
  const messagesRef = useRef(messages)
  messagesRef.current = messages
  const kiroCrewVersion = useAppSelector(s => s.dashboard.status?.version) || ''
  // Count COMPLETED back-and-forths (one user message answered by an assistant
  // reply), not raw assistant-role messages — see countCompletedTurns for why a
  // plain assistant-message tally over-counts. Extracted to a pure helper so the
  // counting rule is unit-tested directly (completedTurns.test.ts).
  const completedTurnCount = useMemo(() => countCompletedTurns(messages), [messages])
  const knowledgeFetch = useKnowledgeFetch(activeSlot)
  const knowledgeFetchRef = useRef(knowledgeFetch)
  knowledgeFetchRef.current = knowledgeFetch
  // User-sent messages (oldest → newest) for ↑/↓ prompt history in the input.
  // Deduplicate consecutive identical prompts to match shell/REPL behavior.
  // `messages` gets a new reference on every streaming chunk; preserve the
  // previous array when user-message content is unchanged so `sentMessages`
  // stays referentially stable and doesn't re-run downstream effects.
  const sentMessagesRef = useRef<PromptHistoryItem[]>([])
  const sentMessagesSlotRef = useRef<string | null>(null)
  // Per-slot timestamp (ms) of the last soft-stop press, used to arm the
  // force-kill. A force press (second click while soft_pending) arriving
  // within FORCE_KILL_ARMING_MS of that slot's soft stop is treated as an
  // accidental rapid double-tap and ignored, so users can't hard-kill by
  // mashing Stop. Keyed by slot so switching slots can't measure one slot's
  // press against another slot's timestamp.
  const softStopAtMapRef = useRef<Map<string, number>>(new Map())
  const sentMessages = useMemo(() => {
    const out = promptHistoryFromMessages(messages)
    // Reset the cached reference when switching slots — otherwise two
    // conversations with matching length+tail would share the prior array.
    if (sentMessagesSlotRef.current !== activeSlot) {
      sentMessagesSlotRef.current = activeSlot ?? null
      sentMessagesRef.current = out
      return out
    }
    // Append-only within a slot — full element-wise compare (array is small).
    const prev = sentMessagesRef.current
    if (samePromptHistory(prev, out)) {
      return prev
    }
    sentMessagesRef.current = out
    return out
  }, [messages, activeSlot])
  const slotRunning = useAppSelector(s => s.chat.slotRunning)
  // Live mirror for `autoFollowAllowed`, a stable callback several effects
  // depend on: taking `slotRunning` as a dependency would re-attach those
  // observers on every turn boundary.
  const slotRunningRef = useRef(!!slotRunning)
  slotRunningRef.current = !!slotRunning
  // Turn disclosure ("N tool calls" / "Worked through N steps"), keyed by the
  // virtualizer's stable row key. This lives HERE rather than in TurnBlock
  // because the transcript is virtualised: a row is unmounted once it leaves
  // the mounted window, which streaming does routinely as it scrolls content
  // past, and row-local state would be destroyed every time. An entry exists
  // only for a turn the user has explicitly toggled; absent means "use the
  // default", so the automatic collapse-on-completion is untouched.
  const [turnDisclosure, setTurnDisclosure] = useState<Record<string, boolean>>({})
  const setTurnDisclosureFor = useCallback((key: string, expanded: boolean) => {
    setTurnDisclosure(prev => (prev[key] === expanded ? prev : { ...prev, [key]: expanded }))
  }, [])
  // Same problem, same shape, for the per-tool-call pill (ToolCallLine): its
  // expanded panel is also row-local and also dies when the virtualizer
  // recycles the row. Keyed by the pill's own message key.
  const [toolDisclosure, setToolDisclosure] = useState<Record<string, boolean>>({})
  const setToolDisclosureFor = useCallback((key: string, expanded: boolean) => {
    setToolDisclosure(prev => (prev[key] === expanded ? prev : { ...prev, [key]: expanded }))
  }, [])
  // Row keys are only unique within a slot, so carrying them across a slot
  // switch would apply one session's choices to another's turns.
  useEffect(() => { setTurnDisclosure({}); setToolDisclosure({}) }, [activeSlot])
  // Shared composer-busy rule (chatSlice.selectComposerBusy). Drives the
  // composer's busy/queue affordance so a message sent during a sub-agent run
  // reads as "will queue".
  const composerBusy = useAppSelector(s => selectComposerBusy(s, s.chat.activeSlot))
  const slotStopping = useAppSelector(s => s.chat.slotStopping)
  const slotLoading = useAppSelector(s => s.chat.slotLoading)
  // While a session-switch history fetch is still in flight for the active
  // slot, this equals activeSlot (even during the cached-provisional window
  // where slotLoading is already false). Used to defer the session-pulse
  // survey's baseline capture until the real transcript has settled.
  const slotSwitchTarget = useAppSelector(s => s.chat.slotSwitchTarget)
  const pendingQuestion = useAppSelector(s => pendingQuestionFor(s.chat.pendingQuestions, s.chat.activeSlot))
  const pendingFollowup = useAppSelector(s => (s.chat.activeSlot ? s.chat.followups?.[s.chat.activeSlot] : undefined))
  const folderSuggestion = useAppSelector(s => (s.chat.activeSlot ? s.chat.folderSuggestions?.[s.chat.activeSlot] : undefined))
  const followupTsBySlot = useAppSelector(s => s.chat.followups) ?? EMPTY_FOLLOWUPS
  // The ambient tip yields to functional surfaces that own the above-composer band
  const tipSuppressed = useAppSelector(s =>
    s.chat.messages.some(m => m.role === 'queued') ||
    // Question card only renders for its OWNING slot (see the render-site
    // slot check below) -- suppression must match, or a question pending in
    // another running slot suppresses tips here forever.
    !!pendingQuestionFor(s.chat.pendingQuestions, s.chat.activeSlot) ||
    // The follow-up card occupies the same above-composer band. Cards are
    // slot-keyed, so read only the ACTIVE slot's entry — a card parked in
    // another session must not suppress tips here.
    (!!s.chat.activeSlot && !!s.chat.followups?.[s.chat.activeSlot]) ||
    // The folder-suggestion card takes the same slot inside the composer box the
    // tip does, and it can land on the FIRST turn — exactly when a tip is most
    // likely to be offered. It is actionable and one-shot where the tip is
    // ambient and re-offered, so the tip yields. Slot-keyed like the follow-up
    // card, so a card parked in another session must not suppress tips here.
    (!!s.chat.activeSlot && !!s.chat.folderSuggestions?.[s.chat.activeSlot]) ||
    // Active subagents render the progress bar in the same above-composer
    // zone the floating tip occupies — the tip always yields: never crowd
    // the queue/subagent surfaces.
    Object.values(s.chat.subagents).some(a => a.status === 'running' || a.status === 'tool' || a.status === 'pending') ||
    // Workflow runs render WorkflowProgressBar in the same band — but only
    // runs belonging to THIS slot show a bar here, so filter by ownership or
    // a terminal run parked in another slot would suppress tips everywhere
    // forever.
    Object.values(s.chat.workflowRuns ?? {}).some(r => runBelongsToSlot(r.sessionKey, s.chat.activeSlot) && (r.status === 'running' || r.status === 'finished' || r.status === 'failed' || r.status === 'cancelled'))
  ) || knowledgeFetch.loading || knowledgeFetch.results.length > 0
  // Split View state is declared up here (not at its usage site) because the
  // tip hook below must know about it: in split mode SessionGridView replaces
  // the composer, TipCard never renders, and an unblocked hook would fetch a
  // tip + record it as shown, silently burning the 6h cadence.
  const [splitMode, setSplitMode] = useState(false)
  /**
   * Passed to ChatSidebar as `onSelectSlot`. Stable BY CONTRACT, not by
   * convenience: `ChatSidebar` is wrapped in `memo`, and an inline arrow here
   * makes that memo bail on EVERY ChatPage render. ChatPage re-renders once per
   * frame while anything is streaming (`useWebSocket` batches chunks per rAF
   * and this page subscribes to the whole `chat.messages`), so an unstable
   * identity re-rendered the entire sidebar ~60 times across the 0.32s mobile
   * drawer slide — on the same main thread that drives the slide's transform.
   * Keep every prop handed to ChatSidebar referentially stable.
   */
  const clearSplitOnSelect = useCallback(() => setSplitMode(false), [])
  /** Same contract as `clearSplitOnSelect`, for the `embedMode === 'sessions'`
   *  frame where the sidebar IS the whole page. */
  const navigateToEmbeddedSlot = useCallback((key: string) => navigate(`/embed/chat/${key}`), [navigate])
  const [splitAnchor, setSplitAnchor] = useState<string | null>(null)
  // Temporary sessions ("no memory reads or writes") must never show
  // memory-personalized tips.
  const tipTemporary = useAppSelector(s => s.dashboard.slots.find(sl => sl.key === s.chat.activeSlot)?.memory_mode === 'temporary')
  const tipBlocked = tipTemporary || splitMode || embedMode === 'sessions'
  const { tip: activeTip, dismiss: dismissTip } = useTipTrigger(!!slotRunning, tipSuppressed, activeSlot, tipBlocked)
  const slotState = useAppSelector(s => s.chat.slotState)
  const contextPct = useAppSelector(s => s.chat.slotContextPct[s.chat.activeSlot ?? ''] ?? 0)
  const contextTokens = useAppSelector(s => s.chat.slotContextTokens?.[s.chat.activeSlot ?? ''])
  // Length only. The two arrays themselves are mutated per streamed sub-agent /
  // tool chunk, and their only consumer is the Activity panel (SidePanel), which
  // is closed by default and now subscribes to them itself. Subscribing to the
  // arrays here re-rendered this whole component per chunk for data it never
  // read.
  const activityOpen = useAppSelector(s => s.chat.activityOpen)
  const slotHasMore = useAppSelector(s => s.chat.slotHasMore)
  const slotOldestIndex = useAppSelector(s => s.chat.slotOldestIndex)
  const cursorIsForActiveSlot = useAppSelector(s => s.chat.slotCursorKey === s.chat.activeSlot)
  const loadingOlder = useAppSelector(s => s.chat.loadingOlder)
  const olderFailed = useAppSelector(s => s.chat.slotOlderError)
  // switchSlot.pending seeds the active view from the pane cache, which for a
  // background pane is a BOUNDED page; the record is present only while it is.
  const activeViewIsBoundedPage = useAppSelector(s => activeSlot ? s.chat.slotPaneBounded?.[activeSlot] !== undefined : false)
  const history = useAppSelector(s => s.chat.history)
  const historyHasMore = useAppSelector(s => s.chat.historyHasMore)

  const draftStores = useComposerDraftStores()
  const { drafts, fileDrafts, pasteDrafts, sessionRefDrafts, saveDrafts, saveDraftsDebounced } = draftStores
  // Outgoing-slot flush key, advanced inside the slot-change effect after it
  // flushes that slot's draft. Distinct from composerSlotRef (the live persist
  // key); both must trail their writes or the draft smear returns.
  const prevSlot = useRef<string | null>(null)
  // Latest-value ref for `activeSlot`, updated every render. Used by async
  // upload callbacks (takeScreenshot, uploadFiles) to detect when the user
  // has switched slots between the initial click and the promise resolving,
  // so the uploaded file lands in the original slot's draft instead of
  // silently appearing in whatever slot is now active.
  const activeSlotRef = useRef(activeSlot); activeSlotRef.current = activeSlot
  // The slot the live composer state belongs to; the per-composer persist
  // effects key off this, not `activeSlot`. Advanced by a dedicated effect
  // declared AFTER those effects so a batched keystroke+switch can't smear one
  // slot's draft onto another. See that advance effect for the full rationale.
  const composerSlotRef = useRef(activeSlot)
  // The composer text lives in a store, not in this component's state: a
  // keystroke re-renders the composer that subscribes to it (`ChatInput` under
  // `<Composer draft>`), not this page. `setInput` keeps the `useState` setter
  // shape; code that acts on the text reads `inputRef`, and render-time facts
  // (blank or not, staged `@dir/` tokens) are selected below so the page
  // re-renders only when that fact changes.
  const composerDraft = useState(() => createComposerDraftStore(activeSlot ? drafts.current[activeSlot] ?? '' : ''))[0]
  const setInput = composerDraft.set
  const inputNonBlank = useComposerDraftSelector(composerDraft, isNonBlank)

  const sendingRef = useRef(false)
  // The empty-transcript welcome screen and its "Continue a previous chat?" suggestions.
  const { updateHistoryQuery, historySuggestions, isWelcomeState, showHistorySuggestions } = useWelcomeState({
    messages, slotRunning, slotLoading, sendingRef, knowledgeFetch, pendingQuestion, history,
  })
  const pendingInput = useAppSelector(s => s.chat.pendingInput)

  const [chatConfig, setChatConfig] = useState<ChatConfig>(loadChatConfig)
  useEffect(() => {
    const reload = () => { const next = loadChatConfig(); setChatConfig(prev => JSON.stringify(prev) === JSON.stringify(next) ? prev : next) }
    window.addEventListener('focus', reload)
    window.addEventListener('mc-config-changed', reload)
    return () => { window.removeEventListener('focus', reload); window.removeEventListener('mc-config-changed', reload) }
  }, [])

  // Project is part of the roster's identity: re-pointing this slot at another
  // project changes which project-scoped agents exist. Derived here rather than
  // from `currentSlot`, which is computed further down the render body.
  const activeSlotProject = slots.find(s => s.key === activeSlot)?.project || undefined
  // Crew-bound (remote-executor) sessions refuse every local turn-starting
  // action server-side (`remote_bound_refusal`): regenerate, edit-resend, rewind
  // and continue would run the crew's turn on THIS machine and diverge the
  // transcripts. So the client must not OFFER them here either — same predicate
  // and same `executor` keying `selectContinuable` already uses for Resume.
  const activeSlotRemoteBound = slotIsRemoteBound(slots.find(s => s.key === activeSlot))
  // The agent and model rosters the pickers offer: this machine's, or a peer crew's.
  const {
    installedAgents, defaultAgent, remoteCrew, effectiveAgents,
    defaultAgentFailed, toggleDefaultAgent,
    agentDropdown, setAgentDropdown, agentFilter, setAgentFilter, agentDropdownRef, agentInputRef, filteredAgents,
    effectiveModels,
  } = useSessionRosters({ activeSlot, activeSlotProject, refreshTrigger, slots, dispatch })
  const selectionCapabilitiesQ = useQuery({
    queryKey: ['slot-selection-capabilities', activeSlot],
    queryFn: () => api.chatSlotSelectionCapabilities(activeSlot!),
    enabled: !!activeSlot && typeof api.chatSlotSelectionCapabilities === 'function',
    // A new ACP session may not exist when its slot first appears. Recheck
    // until the agent reports its config options, then refresh less often.
    // A missing/non-ACP peer can remain unknown indefinitely. Probe quickly
    // during session startup, then back off instead of proxying every 2s.
    refetchInterval: query => query.state.data?.known || query.state.dataUpdateCount + query.state.errorUpdateCount >= 5 ? 30_000 : 2_000,
  })
  const selectionCapabilities = selectionCapabilitiesQ.data?.known
    ? selectionCapabilitiesQ.data
    : undefined
  const hiddenModelsQ = useModelPickerHiddenModelsQuery()
  const hiddenModelIds = hiddenModelsQ.data
  const modelPickerConfigured = useModelPickerConfigured()
  const availableModels = effectiveModels
  // The server owns the backend-specific model ID convention, including while
  // the ACP session is still starting. Never infer it from bracketed IDs alone.
  const codexPairModels = shouldSeparateModelEffort(
    selectionCapabilitiesQ.data?.model_effort_pair_ids, effectiveModels,
  )
  // Whether the picker may offer `Auto (Jev)` (see `lib/jevRoute.ts`): the fleet's
  // answer AND the owner's keystone consent, both required. Two reads the page
  // already makes for other reasons, so the row costs no new request.
  const jevDashCfgQ = useQuery<{ decisions_enabled?: boolean }>({
    queryKey: ['dashboardConfig'],
    queryFn: fetchDashboardConfig,
    staleTime: 30_000,
  })
  const jevConsentQ = useQuery({
    queryKey: ['decisionsConsent'],
    queryFn: () => api.getDecisionsConsent(),
    // A 404 is "this gateway predates the keystone", not a transient failure: the
    // row is simply not offered.
    retry: false,
  })
  // NOT offered for a remote-bound session. Its turns run on the peer through
  // `relay_remote_turn`, which never reaches the routing hook, and the slot-model
  // route forwards the resolved `auto` to the peer without the flag — so the entry
  // would be a control that silently does nothing.
  // The slot answer is the third gate: without one the entry has nowhere to land.
  // It returns as soon as a slot exists.
  const jevRouteOn =
    jevRouteOffered(jevDashCfgQ.data, jevConsentQ.data, !!activeSlot) && !remoteCrew.isRemote
  const jevRouteLabel = i18nT('pages.chatPage.model_auto_jev_description')
  const modelPickerModels = useMemo(
    () => {
      const pickerSlot = slots.find(slot => slot.key === activeSlot)
      return withJevRoute(
        filterInteractiveModels(effectiveModels, hiddenModelIds, [
          pickerSlot?.model || '',
          pickerSlot?.served_model || '',
        ], codexPairModels),
        jevRouteOn,
        jevRouteLabel,
      )
    },
    [effectiveModels, hiddenModelIds, slots, activeSlot, jevRouteOn, jevRouteLabel, codexPairModels],
  )
  const { open: modelDropdown, setOpen: setModelDropdown, filter: modelFilter, setFilter: setModelFilter, dropdownRef: modelDropdownRef, inputRef: modelInputRef, filtered: filteredModels } = useFilteredDropdown(modelPickerModels)
  // Whether the composer held focus when the picker was opened from its chip
  // (ChatInput reads this before the press moves focus). A pick closes the
  // picker, which unmounts the focused row and would otherwise drop focus on
  // <body>; when the user was typing, the pick hands focus back to the
  // composer so they can carry on. A user who was not typing is left alone —
  // focusing the composer under them would be a surprise, and on touch it
  // would raise the keyboard (`focusComposer` already skips touch).
  const modelPickerReturnsFocusRef = useRef(false)
  // Roving-focus keyboard nav for the agent + model dropdowns (shared with StyledSelect/AgentSelector).
  const { onListKeyDown: onAgentListKeyDown } = useListboxKeyboard({
    open: agentDropdown,
    dropdownRef: agentDropdownRef,
    inputRef: agentInputRef,
    hasFilterInput: true,
    filteredCount: filteredAgents.length,
    onEnterSingleMatch: () => {
      const a = filteredAgents[0]
      if (a) { switchAgent(a.name, a.selection_kind); setAgentDropdown(false) }
    },
    closeToTrigger: () => setAgentDropdown(false),
  })
  const { onListKeyDown: onModelListKeyDown } = useListboxKeyboard({
    open: modelDropdown,
    dropdownRef: modelDropdownRef,
    inputRef: modelInputRef,
    hasFilterInput: true,
    filteredCount: filteredModels.length,
    onEnterSingleMatch: () => { pickModel(filteredModels[0].name) },
    closeToTrigger: () => setModelDropdown(false),
  })
  const [pendingAgent, _setPendingAgent] = useState('')  // agent for next new slot
  const pendingAgentRef = useRef('')
  // The namespace the pending agent was picked from, kept beside the name so
  // the create carries both: a pending "reviewer" template must not become the
  // "reviewer" member on send. Cleared with the name.
  const pendingAgentKindRef = useRef<'member' | 'template' | undefined>(undefined)
  const setPendingAgent = useCallback((v: string, kind?: 'member' | 'template') => {
    pendingAgentRef.current = v
    pendingAgentKindRef.current = v ? kind : undefined
    _setPendingAgent(v)
  }, [])
  const [pendingModel, _setPendingModel] = useState('')  // model for next new slot
  const pendingModelRef = useRef('')
  const setPendingModel = useCallback((v: string) => { pendingModelRef.current = v; _setPendingModel(v) }, [])
  const pendingProjectRef = useRef('')
  const setPendingProject = useCallback((v: string) => { pendingProjectRef.current = v }, [])

  // pendingModel is the model for the NEXT new slot, and it is deliberately
  // left EMPTY unless the user explicitly picks one (switchModel below).
  //
  // It used to be seeded at mount from the backend resolver. That resolver
  // answers "what would run", which is right for the composer chip but wrong as
  // a session-create value: a session's model is a permanent pin (the runtime
  // reads `slot.model or agent_model`, so a set slot.model wins for every later
  // turn). Seeding it pinned every new chat to whatever the four-tier chain
  // happened to resolve at page load, so an agent left on Auto never
  // re-resolved and later changes to the agent or the global default never
  // reached the session (#2035).
  //
  // Sending nothing is what preserves the chain. `SessionManager.get_or_create`
  // documents that a `None` model "falls back to the global agent.model config
  // -- but only when the named agent does not pin its own model ... and the
  // global is not a sentinel value like 'auto', in which case it stays None to
  // let the backend resolve from the agent's own JSON config". So omitting it
  // honours the crew pin, the template pin, the global default and Auto, in that
  // order, at session-create time.
  //
  // Sending the literal 'auto' would NOT be equivalent: it is truthy, so it
  // short-circuits `slot.model or agent_model` and would override a template or
  // global pin the user did configure.
  // Composer-toolbar picker anchors: each hook keeps its portaled menu glued
  // to the ChatInput chip that opened it while the menu is open (#10616).
  const { rect: modelBtnRect, anchorTo: anchorModelBtn } = useAnchoredTriggerRect(modelDropdown)
  // One in-page slot for every failed action whose only report used to be a
  // notification-centre toast, a native alert() or a swallowed catch (fork,
  // apply-plan, steer, rename, title generation, the agent
  // default-model pin, a session create that failed while sending, a file the
  // panel could not read). Rendered once, above the composer, through
  // ErrorNotice; the newest failure wins, the same shape as `refusedPress`.
  // `title` is optional because several sites already own a whole-sentence
  // message ("Fork failed: …") that must stay intact for the error-journal match.
  const [actionError, setActionError] = useState<{ title?: string; message: string; preserveOnSwitch?: boolean } | null>(null)
  const showActionError = useCallback((message: string, title?: string) => {
    // Same failure re-reported (an effect re-run, a retry that fails the same
    // way) keeps the stored object, so React bails out instead of re-rendering.
    setActionError(prev => (prev && prev.message === message && prev.title === title) ? prev : { title, message })
  }, [])
  // NOT fire-and-forget: the receipt is the only thing that knows whether the
  // text reached the running turn, and the optimistic bubble asserts that it did.
  // The same `/api/chat` POST as `send()` with the `steer` flag, through the
  // same transport -- `sendTurn` never rejects, so the outcome is read from the
  // receipt, not from an error callback.
  const steerMutation = useMutation({
    // `auto` sends the same POST with `steer: 'auto'`: the gateway then chooses
    // between injecting into the running turn and queueing for the next one
    // (`decisions/points/message_steer.py`). The receipt policy below is unchanged,
    // because the answer arrives as the `dispatched` of a steer or the `queued` of
    // a queue -- both rulings `applySteerReceipt` already owns.
    mutationFn: ({ text, sendId, slot, auto }: { text: string; sendId?: string; slot: string; auto?: boolean }) =>
      sendTurn({ message: text, slot, steer: auto ? 'auto' : true, ...(sendId ? { meta: { sendId } } : {}) }),
    onSuccess: (receipt, { text, sendId, slot }) => {
      // Receipt policy for a steer, owned once in chat-core (issue #9457):
      // applySteerReceipt decides WHICH ruling applies; the adapter below is
      // ChatPage's HOW. The composer was cleared at submit and the optimistic
      // bubble is NOT persisted -- the next transcript rebuild drops it -- so a
      // steer that did not provably reach the gateway hands its text back.
      // Everything here is addressed to the SENDING slot, not the active one:
      // the user can switch sessions inside the deadline window, and this text
      // and its rows belong to the transcript they were typed into (the same
      // rule send()'s restore and steer-echo append follow).
      //
      // "On screen" means the LIVE composer state belongs to this slot --
      // composerSlotRef, not activeSlotRef: during a slot switch the active
      // slot has already flipped while the composer still holds (and is about
      // to flush) the outgoing slot's text. Writing only the persisted draft in
      // that window would be overwritten by that flush from the stale input;
      // updating the live input instead is what the flush then persists.
      const onScreenNow = composerSlotRef.current === slot
      const handBack = () => {
        const kept = onScreenNow ? inputRef.current : (drafts.current[slot] ?? '')
        const back = mergeRecoveredDraft(kept, text)
        setDraft(drafts.current, slot, back)
        saveDrafts()
        if (onScreenNow) setInput(back)
      }
      const row = (message: ChatMessage) => dispatch(appendSlotMessage({ slot, message }))
      applySteerReceipt(receipt, {
        // A confirmed echo is stronger evidence than a missing HTTP response,
        // including when a steer raced onto a new turn and lost its steer flag.
        echoReconciled: () => !!sendId && selectSendConfirmed(store.getState(), slot, sendId),
        restore: handBack,
        // The reducer drops only an optimistic bubble; left standing it would be
        // a third, false representation of the same text next to the error row
        // and the refilled composer. 'queued' is the reducer's DROP arm, 'turn'
        // demotes. Guarded on sendId/slot exactly as before: with no sendId the
        // reducer has no key and there is nothing to resolve (the old code's
        // `if (sendId)` on the failure arms and its `if (!sendId || !slot)
        // return` before the accepted arms both collapse to this guard).
        resolveBubble: (outcome) => {
          if (!sendId || !slot) return
          dispatch(resolveOptimisticSteer({ slot, sendId, outcome: outcome === 'turn' ? 'turn' : 'queued' }))
        },
        reportFailure: (reason, status) => row({
          role: 'error',
          content: reason
            ? i18nT('pages.chatPage.send_failed_with_error', { error: reason })
            : i18nT(status === 'transport-error' ? 'pages.chatPage.send_failed_connection' : 'pages.chatPage.send_failed'),
          cls: '',
        }),
        // The lead glyph is NoticeCard's tone selector (parseNotice): \u26A0 =
        // warn, which also gives the row its "Warning" screen-reader label.
        // Kept out of the catalog string so the copy stays shared with the
        // surfaces that render it in their own strip.
        warnUnconfirmed: () => row({ role: 'notice', content: '\u26A0\uFE0F ' + i18nT('pages.chatPage.delivery_unconfirmed'), cls: '' }),
        // ChatPage's steer is TEXT-ONLY and carries no raw/files split into the
        // mutation (`text` is already the wire text; attachments are excluded
        // from ChatPage steer by design, see steer()). It never stashed on a
        // queued demotion and structurally cannot do so losslessly, so this arm
        // stays a no-op -- the queue card falls to the parser fallback exactly
        // as it did before #9457. resolveBubble('drop') still fires for the
        // demotion via the helper's queued path.
        stashDemoted: () => undefined,
      })
    },
  })
  const {
    automationOpen, setAutomationOpen, automation, automationId, automationCreationReady, automationSnapshotFailed,
    applyAutomationChange,
  } = useSessionAutomation({ activeSlot, queryClient, dispatch })
  const approvalMode = useAppSelector(s => s.dashboard.approvalMode)

  // Close the slot-scoped automation surface on navigation. Its record comes
  // from the same Redux collection the sidebar reads; the WebSocket hook owns
  // both the cold REST snapshot and live updates.
  useEffect(() => {
    setAutomationOpen(false)
  }, [activeSlot, setAutomationOpen])
  const {
    scrollerRef,
    scrollToDisplayIndex,
  } = useScrollManager()

  // Share the settled cache scope and live-layout measurement gate with every
  // transcript host. Layout itself still follows the pane without a debounce.
  const { widthBucket: scrollerWidthBucket, canMeasure, bindScroller } = useTranscriptWidth()

  // Single scroll controller: the virtualizer (`virt`, created below) owns
  // follow + scroll-to-bottom. These refs bridge the early effects/handlers
  // (declared before `virt` in source order) to the virtualizer's API without
  // a temporal-dead-zone hazard — they are populated right after `virt` is
  // created and only read inside callbacks/effects that run post-render.
  // `vGetFollowRef` defaults to "following" so a gate that fires on the very
  // first commit (before the mirror populates it) behaves like the fresh-slot
  // bottom pin it accompanies.
  const vGetFollowRef = useRef<() => boolean>(() => true)
  // Live scroller element for gates that run before `virt` exists in scope
  // (handleTopReached is a dependency of the useVirtualChat call itself).
  const vScrollerElRef = useRef<HTMLElement | null>(null)
  // Whether THIS session has seen real reader input (wheel/touch) on the
  // scroller. Programmatic scrolls (landing compensation, bottom pins) fire
  // scroll events but neither of these, so the flag genuinely means "the
  // reader drove". Read by every self-issued history-fetch gate.
  // Timestamp of the last REAL gesture (wheel/touchmove). Separate from the
  // latch above because the idle prefetch's authorization must expire.
  const lastRealInputAtRef = useRef(0)
  // Pages the SENTINEL door has issued since the last real gesture. The walk
  // poll bounds itself the same way (OLDER_WALK_MAX_PAGES_PER_INPUT) because an
  // unbounded authorization once walked a whole multi-megabyte transcript over a
  // parked reader; this door needs its own counter because the poll's lives
  // inside its interval effect, out of reach here.
  const sentinelPagesSinceInputRef = useRef(0)
  // The walk poll's own budget and last-gesture stamp. Refs so neither survives
  // only as long as the effect that reads them -- see the note at their use.
  const walkPagesSinceInputRef = useRef(0)
  const walkLastInputAtRef = useRef(Number.NEGATIVE_INFINITY)
  // Entering a session is not a request for history, so a session must never
  // INHERIT authorization. Every one of these is written in exactly one place --
  // `noteInput`, on a real wheel/touchmove over the transcript -- and that
  // listener's effect is not keyed on the slot, so without this clear a gesture
  // in the session you just left still authorizes the automatic doors in the one
  // you just opened. Reader intent belongs to the session it happened in.
  //
  // A one-way "has this session ever seen input" latch used to sit here too. It
  // is gone rather than reset: an authorization that can only ever turn ON is not
  // an authorization, and both automatic doors now read the same EXPIRING window,
  // so leaving the slot simply lets it age out.
  useEffect(() => {
    lastRealInputAtRef.current = 0
    walkPagesSinceInputRef.current = 0
    walkLastInputAtRef.current = Number.NEGATIVE_INFINITY
    sentinelPagesSinceInputRef.current = 0
  }, [activeSlot])
  const vScrollToBottomRef = useRef<(behavior?: ScrollBehavior) => void>(() => {})
  // Mirrored so the early handlers (declared above `virt`) can refuse to page on
  // unsettled geometry, the same gate the walk poll and idle prefetch apply.
  const vFarmIsMeasuredRef = useRef<((i: number) => boolean) | null>(null)
  // Mirrored for the interval bodies, which must not re-arm per render.
  const earlierBarInViewRef = useRef<() => boolean>(() => false)
  const mountIndexRef = useRef<(index: number, opts?: { unionOnly?: boolean }) => boolean>(() => false)
  const estimateRowTopRef = useRef<(index: number) => number | null>(() => null)

  const [prefillHint, setPrefillHint] = useState(false)
  // Whether the user has edited the seeded composer. The hint's expiry is armed
  // by that first edit, not by the seed's arrival: a hand-off's error report is
  // a dozen lines the user reads before touching anything, and a clock started
  // at the seed collapsed the box from the prefill cap to the six-line typing
  // cap under them mid-read, taking the "pre-filled" explanation with it.
  const [prefillEdited, setPrefillEdited] = useState(false)
  const raisePrefillHint = useCallback(() => { setPrefillHint(true); setPrefillEdited(false) }, [])
  const autoSendRef = useRef<string | null>(null)
  const appLaunchSendRef = useRef<{ slotKey?: string } | null>(null)
  const [autoSendTick, setAutoSendTick] = useState(0)
  const newSessionRef = useRef(false)
  // True while the challenge-redirect token effect is creating/linking its
  // session. Blocks the auto-select effect from switching to a different slot
  // (which would orphan the freshly slack-linked session and break mirroring).
  const tokenConsumingRef = useRef(
    typeof window !== 'undefined' && new URLSearchParams(window.location.search).has('token'),
  )
  // The composer text as of the last committed draft render. `ComposerDraftSync`
  // (rendered below) advances it; the mid-switch guards below write it
  // directly, and the next draft commit overwrites them.
  const inputRef = useRef(composerDraft.get())
  // Holds the exact text a widget action pre-filled into the composer, so the
  // eventual user-initiated send can be tagged meta.origin='widget' for
 // forensic attribution. Set on widget pre-fill, consumed
  // and cleared in send(). A genuine from-scratch turn never sets this.
  const widgetPrefillRef = useRef<string | null>(null)
  // Token (`${slotKey}:${ts}`) of the most recently consumed composer prefill.
  // Guards the per-slot draft-restore effect against React.StrictMode's mount
  // double-invoke: the first invoke consumes+removes PREFILL_STORAGE_KEY and
  // seeds the composer, so without this the second invoke would find no stored
  // prefill and reset the composer to the (empty) incoming draft — the artifact
  // companion panel mounts ChatPage fresh, so it hits this double-invoke every
  // first open. See the draft-restore effect below.
  const consumedPrefillRef = useRef<string | null>(null)

  // Auto-dismiss the prefill hint 10 seconds after the user starts editing the
  // seed. Until then it holds: the band and the taller cap are what let the
  // seeded text be read, and reading has no deadline.
  useEffect(() => {
    if (!prefillHint || !prefillEdited) return
    const t = setTimeout(() => setPrefillHint(false), 10000)
    return () => clearTimeout(t)
  }, [prefillHint, prefillEdited])

  // Error hand-offs ("Ask the agent"): claimed into a FIFO, one fresh session at a time.
  useErrorHandoffIntake({
    embedded,
    connected,
    mode,
    dispatch,
    drafts,
    inputRef,
    activeSlotRef,
    showActionError,
    activateCreatedSlot: activateHandoffSlot,
    createFailReason,
  })

  // Text that reaches the composer from outside a keystroke (see launchIntake).
  usePendingInputIntake({
    pendingInput,
    activeSlot,
    embedded,
    searchParams,
    setSearchParams,
    dispatch,
    autoSendRef,
    newSessionRef,
    setAutoSendTick,
    drafts,
    saveDraftsDebounced,
    setInput,
    raisePrefillHint,
  })

  // Consume prompt from token payload (channel challenge-and-redirect flow).
  // The prompt is HMAC-signed in the token — server validates the signature
  // and sets the session cookie before the SPA loads. No auto-send — the user
  // must press Enter to confirm.
  //
  // Three cases, driven by signed claims in the token:
  //  1. session_key present → the originating Slack thread is already linked to
  //     a dashboard session; reconnect to THAT session instead of making a new
  //     one (fixes "thread reply spawns a disconnected session").
  //  2. channel + thread_ts present (no session_key) → fresh thread; create a
  //     new session and auto-link it back to that Slack thread so agent
  //     responses flow into the thread.
  //  3. neither → plain new session (e.g. a top-level channel message).
  // In all cases the prompt is seeded via PREFILL_STORAGE_KEY (the channel the
  // slot-restore effect honors) AND set directly once the target slot is
  // active, so the previous slot's draft can't clobber it.
  useEffect(() => {
    // tokenConsumingRef is initialized true when a token is in the URL; every
    // early return below MUST clear it, or the auto-select guard stays engaged
    // for the whole session and blocks slot selection.
    if (embedded) { tokenConsumingRef.current = false; return }
    const token = new URLSearchParams(window.location.search).get('token')
    if (!token) { tokenConsumingRef.current = false; return }
    // Always strip token from URL to prevent leakage via referrer/history.
    // Two layers, both required (PR #11112 review):
    //  1. Raw replaceState scrubs the visible URL immediately — but ONLY the
    //     `token` param, preserving `?sid` so a session deep link
    //     (`/chat?sid=…&token=…`) still names its slot after the strip.
    //     Preserves history.state for the same reason as the prefill strip.
    //  2. setSearchParams scrubs the ROUTER's own searchParams state, which
    //     raw replaceState never updates — the `?sid` sync effects
    //     (useChatPageSessionController) copy router params back into the URL
    //     on slot activation, and would otherwise restore the live credential
    //     into browser history.
    const scrubbed = new URLSearchParams(window.location.search)
    scrubbed.delete('token')
    const scrubbedQs = scrubbed.toString()
    window.history.replaceState(
      window.history.state, '', window.location.pathname + (scrubbedQs ? `?${scrubbedQs}` : ''))
    setSearchParams(prev => {
      const next = new URLSearchParams(prev)
      next.delete('token')
      return next
    }, { replace: true })
    const prompt = extractPromptFromToken(token)
    if (!prompt) { tokenConsumingRef.current = false; return }
    const { sessionKey, channel, threadTs } = extractSlackContextFromToken(token)
    // Backend session keys are history keys (dashboard:chat-…); the frontend
    // slot key is the bare form.
    const targetSlot = sessionKey ? sessionKey.replace(/^dashboard:/, '') : null
    tokenConsumingRef.current = true
    ;(async () => {
     try {
      let slotKey: string | null = null
      if (targetSlot) {
        // Case 1: reconnect to the existing linked session.
        try {
          await dispatch(switchSlot(targetSlot)).unwrap()
          slotKey = targetSlot
        } catch {
          // Session vanished (deleted/expired) — fall back to a new one.
        }
      }
      if (!slotKey) {
        // No targetSlot (or reconnect failed): create the session HERE and,
        // for a fresh thread, slack-link it so responses mirror to Slack.
        try {
          const create = dispatch(createSlot({ mode }))
          noCarryCreateIdsRef.current.add(create.requestId)
          const slot = await create.unwrap()
          slotKey = slot?.key ?? null
        } catch {
          // ignore — fall back to prefilling the current slot
        }
        // Case 2: auto-link the new session back to the originating thread so
        // responses flow into Slack. Best-effort; failure just leaves it
        // unlinked.
        if (slotKey && channel && threadTs) {
          try { await api.slackLink(slotKey, channel, threadTs) } catch { /* non-fatal */ }
        }
      }
      // We have created/reconnected AND made the target slot active. Critically,
      // clear newSessionRef and pin activeSlot to this slot so send() reuses it
      // on Enter — otherwise send()'s forceNew path would spawn a SECOND,
      // unlinked slot and break Slack mirroring.
      if (slotKey) {
        newSessionRef.current = false
        dispatch(switchSlot(slotKey))
        safeSetSessionItem(
          PREFILL_STORAGE_KEY,
          JSON.stringify({ slotKey, prompt, ts: Date.now() }),
        )
      }
      // While the switch to `slotKey` has not committed yet, the composer still
      // belongs to the old session and a write here would persist the prompt
      // into ITS draft. The slot-change effect restores the prompt from the
      // prefill hand-off written above, so only write when the composer is
      // already on the target (or there is no target to switch to).
      if (!slotKey || composerSlotRef.current === slotKey) setInput(prompt)
      raisePrefillHint()
      autoSendRef.current = prompt
      setAutoSendTick(t => t + 1)
     } finally {
      // Release the auto-select guard once the session is created/linked (or
      // failed), so normal slot selection resumes.
      tokenConsumingRef.current = false
     }
    })()
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  // What the composer holds besides its text, then the per-commit / per-switch
  // draft bookkeeping over it. The draft commit runs from `ComposerDraftSync`'s
  // effect, a child of this page, so it lands before this page's own effects in
  // the same commit: ahead of the slot-change restore and the composer-key
  // advance, which is what keeps a keystroke batched with a switch on the slot
  // it was typed in.
  const staging = useComposerStaging({ activeSlot, splitMode, composerSlotRef, pickedFileTokens: draftStores.pickedFileTokens })
  const {
    uploading, setUploading,
    pendingFiles, setPendingFiles, pendingFilesRef,
    pickedFileTokens, mergeSlotTokens,
    snipFrame, setSnipFrame, snipSlotRef,
    pasteBlocks, setPasteBlocks, pasteBlocksRef,
    pendingSessions, setPendingSessions, pendingSessionsRef,
    stageSessionRef, unstageSessionRef, canStageSessionRef,
    chatPaneEl, setChatPaneEl,
    uploadError, setUploadError, uploadHint, setUploadHint,
    resizedInfo, setResizedInfo,
  } = staging
  const { reconcileFileChipsRef, onComposerDraftCommit, noCarryCreateIdsRef } = useComposerDraftLifecycle({
    activeSlot,
    stores: draftStores,
    staging,
    prevSlot,
    composerSlotRef,
    inputRef,
    setInput,
    consumedPrefillRef,
    foregroundCreateId,
    foregroundCreateIdRef,
    lastCreatedActivationRef,
    knowledgeFetchRef,
    raisePrefillHint,
    setPrefillHint,
    setActionError,
    updateHistoryQuery,
  })
  const { rect: agentBtnRect, anchorTo: anchorAgentBtn } = useAnchoredTriggerRect(agentDropdown)
  const [projectPickerOpen, setProjectPickerOpen] = useState(false)
  const { rect: projectBtnRect, anchorTo: anchorProjectBtn } = useAnchoredTriggerRect(projectPickerOpen)

  // Prevent Chrome from navigating to dropped files.
  // Must be on document to catch drops anywhere on the page.
  useEffect(() => {
    const preventNav = (e: DragEvent) => {
      if (e.dataTransfer?.types?.includes('Files')) {
        e.preventDefault()
        e.dataTransfer.dropEffect = 'copy'
      }
    }
    document.addEventListener('dragover', preventNav)
    document.addEventListener('drop', preventNav)
    return () => {
      document.removeEventListener('dragover', preventNav)
      document.removeEventListener('drop', preventNav)
    }
  }, [])

  const pendingDirs = useStagedFolderRefs(composerDraft)
  // Live persistence of every staged resource, then the composer-key advance,
  // in that order (see useStagedDraftPersistence).
  useStagedDraftPersistence({ activeSlot, composerSlotRef, stores: draftStores, staging })
  const isMac = useAppSelector(s => s.dashboard.status?.platform) === 'darwin'
  // Voice dictation is the Composer's Voice atom (chat-core P3-b): ChatPage no
  // longer runs the hook or wires 23 props. It supplies, through the `Composer`
  // root below, only what the atom cannot know on its own: which slot the
  // composer currently shows (the draft-settlement predicate), where an
  // off-screen batch transcript goes (that slot's persisted draft), the
  // endpointer's auto-submit, and that this is the surface owning the
  // document-wide push-to-talk key. `composerRef` reaches the atom's controls
  // from send().
  //
  // Forward ref to send() (defined far below) so the streaming endpointer's
  // auto-submit callback — handed to the atom here, above send — can fire it.
  // Kept fresh by an effect after send is declared.
  const sendRef = useRef<((optionText?: string, targetSlot?: string) => void) | null>(null)
  const composerRef = useRef<ComposerHandle>(null)
  // Live composer caret, kept current by ChatInput; the resources controller
  // splices a picked file token at it, and dictation splices the transcript at
  // it — so ChatPage owns the refs and hands them to the atom.
  const voiceCaretRef = useRef<{ start: number; end: number } | null>(null)
  const voicePendingCaretRef = useRef<number | null>(null)
  // Splice into the LIVE composer only when the target slot is both the active
  // slot AND the slot the composer's `input` currently belongs to. On a slot
  // switch, activeSlotRef updates synchronously in render, but the composer's
  // draft-restore + composerSlotRef advance run in LATER effects — splicing in
  // that unsettled window would let the pending draft restore overwrite the
  // transcript.
  const voiceIsComposerFor = useCallback((target: string | null) => target === activeSlotRef.current && composerSlotRef.current === target, [])
  // Off-screen batch transcript: append to the target slot's persisted draft
  // (recoverable, shown on return). Mirrors handleOptimizeResult's cross-slot
  // routing.
  const voiceDeliverOffScreen = useCallback((target: string, append: (base: string) => string) => {
    const next = append(drafts.current[target] ?? '')
    setDraft(drafts.current, target, next)
    // Mid-switch guard: if the composer still belongs to `target` (activeSlot
    // has advanced in render but the outgoing-slot persist effect hasn't run
    // yet), that effect will flush inputRef.current into drafts[target] and
    // would overwrite this transcript with the pre-transcript input. Carry the
    // appended value into inputRef too so the flush preserves the transcript.
    if (composerSlotRef.current === target) inputRef.current = next
    saveDrafts()
  }, [saveDrafts, drafts])
  const voiceAutoSubmit = useCallback(() => { sendRef.current?.() }, [])
  const composerVoiceOptions = useMemo<ComposerVoiceOptions>(() => ({
    isComposerFor: voiceIsComposerFor,
    deliverOffScreen: voiceDeliverOffScreen,
    onAutoSubmit: voiceAutoSubmit,
    pushToTalk: true,
    caretRef: voiceCaretRef,
    pendingCaretRef: voicePendingCaretRef,
    settingsRoute: embedded ? '/embed/settings' : settingsPath({ tab: 'voice' }),
  }), [voiceIsComposerFor, voiceDeliverOffScreen, voiceAutoSubmit, embedded])

  // The project ref is read by the resources controller's drop/paste handlers at
  // event time, so it is declared before the controller and refreshed every render.
  const currentProjectRef = useRef<string | undefined>(undefined)
  currentProjectRef.current = slots.find(s => s.key === activeSlot)?.project || undefined

  // Opt-in "reuse the current terminal" for Run-in-terminal
  // (dashboard.terminal.reuse_current, default off — the fresh-shell default is
  // unchanged). Read from the shared kirocrewConfig query and mirrored onto a
  // ref because the run-in-terminal handler below installs once ([]-deps) and
  // reads it at event time, not at mount. Only a literal `true` turns it on —
  // the same "one literal decides it" rule the completion toggle uses, so a
  // hand-edited non-boolean cannot half-enable it.
  const { data: terminalCfg, isError: terminalCfgIsError, isPending: terminalCfgIsPending } = useQuery<{ dashboard?: { terminal?: { reuse_current?: boolean } } }>({
    queryKey: ['kirocrewConfig'],
    queryFn: () => api.kirocrewConfig(),
    staleTime: 30_000,
  })
  const terminalReuseRef = useRef(false)
  terminalReuseRef.current = terminalCfg?.dashboard?.terminal?.reuse_current === true
  // The handler installs once ([]-deps) and reads at event time, so the query's
  // UNSETTLED state is mirrored onto a ref too. Until the initial read settles
  // we cannot know whether reuse was saved on, and collapsing an unknown to the
  // fresh-tab EXECUTE path would run a command a reuse-on user expected only to
  // be copied — the exact harm this setting removes. So an unsettled read takes
  // the copy-never-run path (benign if reuse was actually off: the user just
  // pastes it themselves) rather than executing on a guess. `isPending` is true
  // only before the first result (data or error) arrives.
  const terminalCfgUnsettledRef = useRef(false)
  terminalCfgUnsettledRef.current = terminalCfgIsPending === true
  // The run-in-terminal handler installs once ([]-deps) and reads at event
  // time, so the query's FAILED state is mirrored onto a ref too. A read
  // failure is distinct from a legitimately-off setting: when the config could
  // not be read we cannot know whether reuse was on, so the handler surfaces
  // that read failure through ErrorNotice (errors-use-error-notice) rather than
  // silently collapsing a saved reuse-on into the fresh-tab path with nothing
  // on screen. `isError` only after the query has actually errored, so a
  // pending first load still takes the silent-default branch, not a false alarm.
  const terminalCfgReadFailedRef = useRef(false)
  terminalCfgReadFailedRef.current = terminalCfgIsError === true

  const resources = useChatPageResourcesController({
    activeSlot,
    activeSlotRef,
    messages,
    slotLoading,
    dispatch,
    queryClient,
    showActionError,
    composer: {
      inputRef,
      setInput,
      drafts,
      fileDrafts,
      setPendingFiles,
      currentProjectRef,
      voiceCaretRef,
      voicePendingCaretRef,
      saveDrafts,
    },
    capture: {
      setUploading,
      setUploadError,
      setUploadHint,
      setResizedInfo,
      snipSlotRef,
      setSnipFrame,
    },
  })
  const {
    tabsCtl,
    hasLiveAppTab,
    hasBrowserTab,
    search,
    sourceHostsRef,
    jiraSourceHosts,
    jiraSourceHostsRef,
    panelSources,
    panelIssues,
    selectedSourceUrl,
    selectedIssueUrl,
    selectSource,
    selectSourceUrl,
    selectIssueUrl,
    reconcileSourceUrl,
    reconcileIssueUrl,
    setRevealedSources,
    addSourceCommentToChat,
    colorThemeRef,
    handleFileOpen,
    handleFolderOpen,
    handleArtifactOpen,
    handleOpenDiff,
    handleFileSave,
    handleCapture,
    uploadFiles,
    cancelUpload,
    uploadCancellable,
    handleOptimizeResult,
    dragOver,
    dropTargetProps,
  } = resources

  // Open the Subagents panel from a completion card. A per-agent event
  // deep-links to the agent it reports on, so the panel lands on that
  // transcript rather than whatever was last selected; a wave digest names no
  // single agent and just opens the tab.
  const handleSubagentPanelOpen = useCallback((parsed: ParsedSubagentCompletion) => {
    if (parsed.kind === 'single') dispatch(selectSubagent(parsed.agentId))
    dispatch(openActivityToTab('subagents'))
  }, [dispatch])

  // `filteredSlots`, not `slots`: a surface this page cannot render would chip to a
  // destination the switch clears. That narrowing IS the shared builder's rule
  // (`utils/sessionRoster`), which the Members page's crewmate DM also uses --
  // its chips navigate here, so the two must agree on which slots may chip.
  // Signature because heartbeats remint slot objects.
  const sessionTitleSig = JSON.stringify(filteredSlots.map(s => [s.key, s.title || s.key]))
  const sessionTitles = useMemo(
    () => sessionTitleRoster(filteredSlots),
    // eslint-disable-next-line react-hooks/exhaustive-deps -- keyed on the value-equal pair signature, not the slot objects (see above)
    [sessionTitleSig],
  )

  const { data: forkCfg } = useQuery<{ tail_fork_enabled?: boolean }>({ queryKey: ['dashboardConfig'], queryFn: fetchDashboardConfig, staleTime: 30_000 })
  const handleFork = useCallback(async (visibleIndex: number, messageId?: string) => {
    if (!activeSlot) return
    try {
      // Fork WITHOUT a prompt: an unsent composer draft must never be
      // auto-submitted into the freshly forked session. The
      // per-slot draft mechanism saves the source slot's composer text on
      // slot-switch, so the user's parked draft stays safe in the original
      // session and the fork opens with an empty composer.
      //
      // forkCfg is undefined until the dashboardConfig query resolves for the
      // first time. Use the cache when warm; otherwise fetch a fresh value
      // directly so direction never silently falls back to an undefined config
      // — which would downgrade an intended tail-fork to a head-fork whenever
      // the query has errored or settled with no data, not just while loading.
      const resolvedCfg = forkCfg ?? await api.dashboardConfig()
      const direction = resolvedCfg?.tail_fork_enabled ? 'tail' : 'head'
      const result = await dispatch(forkSlot({ slot: activeSlot, atIndex: visibleIndex, messageId, direction })).unwrap()
      if (result.ok) {
        await dispatch(switchSlot(result.key))
      } else {
        showActionError(i18nT('pages.chatPage.fork_failed_error', { error: result.error || i18nT('pages.chatPage.unknown_error') }))
      }
    } catch (e) {
      showActionError(i18nT('pages.chatPage.fork_failed_error', { error: errMessage(e) || i18nT('pages.chatPage.unknown_error') }))
    }
  }, [activeSlot, dispatch, forkCfg, showActionError])

  const transcriptEarly = useChatPageTranscriptEarlyController({
    activeTip,
    mountIndexRef,
    estimateRowTopRef,
    scrollerRef,
    scrollToDisplayIndex,
    slotRunningRef,
    vGetFollowRef,
    vScrollToBottomRef,
  })
  const {
    scrollBottom,
    autoFollowAllowed,
    handleSurveyLayoutChange,
    composerBandRef,
    navToDisplayIndex,
    displayItemsRef,
    pinFoldRef,
    pinCardRef,
    pinEnabledRef,
    pinned,
    setPinned,
    pinExpanded,
    setPinExpanded,
    onPinCollapsedHeight,
    scrollTranscriptBy,
    updatePinnedPrompt,
    onScrollPin,
    scrollToPinnedPrompt,
  } = transcriptEarly

  // Sticky-bottom scroll state is owned by the virtualizer (`virt.isAtBottom`,
  // wired below). No local mirror — a single source of truth avoids
  // dual-controller drift.

  // New content while following is handled inside the virtualizer (RO re-pin
  // for in-place growth + append layout-effect pin for new items), so ChatPage
  // does not run its own message-length scroll effect.
  const session = useChatPageSessionController({
    activeSlot,
    activeSlotRef,
    connected,
    defaultAgent,
    dispatch,
    drafts,
    embedMode,
    embedded,
    fileDrafts,
    filteredSlots,
    filteredSlotsRef,
    history,
    inputNonBlank,
    isMobile,
    locationKey: location.key,
    locationPathname: location.pathname,
    locationHash: location.hash,
    mode,
    navigate,
    navigationType,
    newSessionRef,
    noUrlSync,
    pasteDrafts,
    popout,
    prevSlot,
    saveDrafts,
    searchParams,
    slots,
    tokenConsumingRef,
  })
  const {
    appSlotLaunch,
    setAppSlotLaunch,
    closeSessionTab,
    drawerPopRef,
    handleResumeSession,
    highlightTs,
    initialMidRef,
    initialMsgRef,
    initialSidRef,
    newSlotFailed,
    newSlotMutation,
    openSlotInNewTab,
    ownsSessionTabs,
    selectSessionTab,
    sessionTabs,
    setHighlightTs,
    setNewSlotFailed,
    setSidError,
    sidError,
  } = session

  // Only the routed chat consumes app launch intents; see useAppLaunchIntake.
  useAppLaunchIntake({
    embedded,
    connected,
    activeSlot,
    slotLoading,
    locationKey: location.key,
    appSlotLaunch,
    setAppSlotLaunch,
    autoSendRef,
    appLaunchSendRef,
    newSessionRef,
    setAutoSendTick,
    drafts,
    saveDraftsDebounced,
    setInput,
    raisePrefillHint,
    setPendingAgent,
    setActionError,
  })

  // Auto-scroll during streaming — only when pinned to bottom
  const lastMsg = messages[messages.length - 1]
  const isStreaming = lastMsg?.role === 'streaming'
  // Follow-up option chips and the ownership of the text they append.
  const {
    followUpOptions, followUpSourceKey,
    followUpPicked, followUpPickedRef, toggleFollowUpOption,
    composerRootChange, composerUserEdit,
  } = useFollowUpChips({
    messages, isStreaming, pendingQuestion, activeSlot, inputRef, setInput, prefillEdited, setPrefillEdited,
  })
  const { data: dashCfg } = useQuery<{ quick_send?: boolean; session_grid?: boolean; link_previews?: boolean; social_share_enabled?: boolean }>({ queryKey: ['dashboardConfig'], queryFn: fetchDashboardConfig, staleTime: 30_000 })
  // Session grid (split view) is an opt-in feature flag (Settings › Chat › Split View). Gates ⌘D, the Columns2 button, and the grid render.
  const splitFeatureEnabled = dashCfg?.session_grid === true
  // Link previews are opt-in too (Settings › Chat › Link Previews): enabling them
  // lets this machine fetch every http(s) link the model emits. Hoisted to a
  // stable primitive so it can sit in the transcript renderer's dep list — flipping
  // the toggle has to re-render already-rendered messages, not just the next one.
  const linkPreviewsOn = dashCfg?.link_previews === true
  // "Share as image" is a governance-gated entry (`capabilities.social_share`),
  // not a preference: the server resolves the ceiling and reports it here, and the
  // entry stays hidden until it says true — the endpoint is the authority, the
  // frontend never guesses (same posture as the mobile-connect rail row).
  const socialShareOn = dashCfg?.social_share_enabled === true
  // Whether the split send button may offer `Auto (Jev)`: the fleet ceiling and
  // the owner's consent, both the gateway's answers (see useJevAutoSend).
  const jevAutoConsented = useJevAutoSend()
  // Connections cards own consent for the providers they render, so chat drops
  // the duplicate OAuth banner — but only while that gallery is reachable.
  const connectionsUiOn = useConnectionsUiEnabled()
  // Pop-out state for the title-bar control (shared singleton — same channel the menus use).
  const { isPoppedOut: isSlotPoppedOut, open: openActivePopout, focus: focusActivePopout, returnSelfToMain } = useChatPopouts()
  const activePoppedOut = !!activeSlot && isSlotPoppedOut(activeSlot)
  const planTaskId = useMemo(() => {
    for (const m of messages) {
      const match = m.content?.match(/<!-- plan_task_id:(\S+) -->/)
      if (match) return match[1]
    }
    return ''
  }, [messages])

  // Scroll to show Footer when agent starts running (loading indicator appears)
  const prevRunningRef = useRef(false)
  useEffect(() => {
    // LIVE geometry, not just the follow flag. A turn can start without the
    // reader having asked for anything -- a subagent completion, a cron
    // notification and an auto-nudge cycle all flip `slotRunning` on their own --
    // and a stale armed flag then teleports a reader who is deep in history to
    // the bottom. Requiring them to actually BE near the bottom makes that
    // impossible: the flag can be wrong, the distance cannot.
    if (slotRunning && !prevRunningRef.current && autoFollowAllowed()) {
      setTimeout(() => scrollBottom(), SCROLL_AFTER_RENDER_MS)
    }
    prevRunningRef.current = slotRunning
  }, [slotRunning, scrollBottom, autoFollowAllowed])

  // Reconcile the active slot's running state from WS slot updates. The reducer
  // guards against a stale snapshot overwriting an unconfirmed local turn.
  useEffect(() => {
    if (!activeSlot) return
    const s = slots.find(s => s.key === activeSlot)
    if (!s) return
    dispatch(syncSlotRunningFromServer({ slot: s.key, running: s.running, stopping: s.stopping ?? false }))
  }, [slots, activeSlot, dispatch])

  // Raw send — sends pre-built text directly to the server
  const modeRef = useRef(mode)
  modeRef.current = mode

  // Resolves true when the server accepted the message (dispatched, queued,
  // or received-but-late), false when nothing was delivered (offline, empty,
  // intercepted locally, transport error, refused). UI reactions all stay
  // inside send(); the verdict exists for callers that persist state only on
  // delivery (ArtifactPanel's submit-to-chat batch marks comments sent on it).
  const send = useCallback(async (optionText?: string, targetSlot?: string, steerNow?: boolean, isolated = false): Promise<boolean> => {
    // Defense-in-depth: ChatInput already gates Send/Optimize buttons and
    // the keyboard Enter shortcut on `connected`, but a future caller (a
    // programmatic dispatch from a hotkey, a follow-up option click, an
    // intent handler) could call send() while offline. Bail before we
    // clear the draft via setInput('') below — losing the user's typed
    // message with no recovery path is the offline-UX regression we're
    // guarding against. Cheap belt-and-braces.
    if (!connected) return false
    const raw = (isolated ? optionText ?? '' : optionText || inputRef.current).trim()
    // App launches own only their explicit text, not the composer's staged data.
    const widgetOrigin = !isolated && !!widgetPrefillRef.current && raw.includes(widgetPrefillRef.current)
    if (!isolated) widgetPrefillRef.current = null
    if (!raw && (isolated || (!pendingFilesRef.current.length && !pendingSessionsRef.current.length))) return false

    // Sending while STREAMING dictation is live ends the dictation (see
    // `useComposerVoice.disarmForSend` for the full rationale — streaming only,
    // batch keeps capturing and lands its transcript when the user stops).
    if (!isolated) composerRef.current?.voice()?.disarmForSend()

    // The session actually on screen at send time. Read from the ref (fresh
    // every render), not the closure `activeSlot` (stale until send() is
    // re-memoized). Under lag a reducer-driven activeSlot change can move the
    // active slot before ChatPage re-renders, so the closure would route into
    // the slot the user just left. Used for slash routing, the composer draft
    // clear, and (below) the send target.
    const uiSlot = activeSlotRef.current

    // Capture a pending BLOCKING card at ENTRY — before the first await below.
    // This send consumes the answer channel of the card the user saw when they
    // hit send; captured after an await, the card-submit flow can resolve the
    // card (or a newer one can land) in the gap, and the capture would compare
    // against the wrong baseline. Its staleness is
    // resolved over the network, not in the store. A STATELESS card needs no
    // capture: the server retires it when this send's user row lands and
    // announces it with `question_card_resolved`.
    const entrySendSlot = targetSlot ?? uiSlot
    // An app's supplied text is not the human's answer to a pending card.
    // Null captures keep all composer-owned completion effects inert.
    const askAtSend = isolated ? null : capturePendingAskId(store.getState().chat.pendingQuestions, entrySendSlot)
    // Entry-time capture of the folder-suggestion card, ONLY when it was
    // actually on screen for this send: the card renders solely in this page's
    // composer band for the ACTIVE slot, so a targeted send into another slot —
    // and any send from a surface that never renders the card (ChatPane) — must
    // not age it. The captured `ts` pins the card GENERATION the user saw; the
    // aging dispatch below is ts-guarded so a replacement card arriving while
    // the POST is in flight does not inherit this send's age.
    const folderCardAtSend =
      !isolated && entrySendSlot && entrySendSlot === uiSlot ? store.getState().chat.folderSuggestions?.[entrySendSlot] : undefined

    // Slash command interception (e.g. /side): runs before knowledge so a
    // bare prefix like /side returns immediately without touching input parse.
    // Gate on the RAW composer text first — a pasted block whose content
    // happens to start with "/side " must stay main-chat content, never
    // become a command. Only a command the user actually typed is expanded
    // (so a paste after "/side " reaches the side chat as content) and
    // delegated. On failure keep the composer intact so the question stays
    // recoverable — same rules as steer()'s guard.
    // An option answer (optionText — a question-card, follow-up or decision-
    // card choice) is an answer payload for the agent, never a typed UI
    // command: a choice that happens to look like "/side …" must reach the
    // turn as text rather than open Side Chat and strand the card. Same
    // carve-out the knowledge-fetch branch below applies.
    if (!optionText && isInterceptedSlashCommand(raw)) {
      const slashPastes = pasteBlocksRef.current
      const slashTxt = slashPastes.length ? expandPasteTokens(raw, slashPastes) : raw
      const slashResult = await interceptSlashCommand(slashTxt, uiSlot, dispatch)
      if (slashResult.intercepted) {
        if (!optionText && !slashResult.failed) { setInput(''); setPasteBlocks([]) }
        // Keeping the composer intact is the recovery; this is the report.
        // Same surface as a refused footer press, so the reason sits above the
        // draft it left in place instead of only in the console.
        if (slashResult.failed) {
          setRefusedPress({
            action: slashResult.stage === 'turn' ? 'side_turn' : 'side_open',
            message: slashResult.error || i18nT('pages.chatPage.side_command_not_run'),
          })
        }
        return false
      }
    }

    // Knowledge fetch: intercept @knowledge prefix, show picker instead of sending
    const kq = extractKnowledgeQuery(raw)
    if (kq && !optionText) {
      knowledgeFetchRef.current.searchKnowledge(kq)
      setInput('')
      return false
    }

    // Snapshot the staged attachments BEFORE the composer is cleared below, so a
    // failed send can put them back (prepareSendPayload's `filePaths` drops
    // images, which would silently lose them on restore).
    const sentFiles = isolated ? [] : pendingFilesRef.current.slice()
    // Explicit text already excludes staged session refs. App launches also
    // exclude files, paste expansion and knowledge, without changing legacy
    // option-click or composer-send behavior.
    const sentSessionRefs = isolated || optionText ? [] : pendingSessionsRef.current.slice()
    const stagedFilesAtSend = [...new Set(sentFiles)]
    const { txt: typedTxt, displayTxt: typedDisplayTxt, filePaths } = isolated
      ? { txt: raw, displayTxt: raw, filePaths: [] }
      : prepareSendPayload(raw, sentFiles)
    // Folder references serialize like files but from the text alone: each
    // `@rel/` token becomes `[attached_dir N] /abs/path` in the LLM-facing
    // text (absolute, so the reference survives a cwd/project mismatch and
    // history replay), while the display text keeps the `@rel/` token for the
    // bubble chip — the same fresh-vs-wire split files use. Runs AFTER the
    // file pass: file tokens never end in `/`, so the two rewrites are
    // disjoint. `dirPaths` rides `meta.dirs`, ordered so marker N indexes
    // dirPaths[N-1] losslessly.
    const { llm: typedTxtDirs, dirPaths } = isolated
      ? { llm: typedTxt, dirPaths: [] }
      : serializeDirTokens(typedTxt, currentProjectRef.current || '')
    // Staged session references become plain markdown links appended to the
    // message — deliberately a POINTER, not the referenced transcript. Inlining
    // another session's content would spend a large share of THIS session's
    // context window in one turn and can trip autocompact, compacting away the
    // conversation the reference was meant to enrich. The agent follows the link
    // on demand instead, through a read path that is already bounded, redacted,
    // and incognito-refusing server-side.
    //
    // The link is built by the SAME helper the session menu's "Copy link" uses,
    // so a referenced session and a hand-copied one are the same string.
    //
    // Appended to the sent and displayed text alike: unlike a paste token there
    // is no collapsed form to preserve in the bubble, so what the user sees is
    // exactly what was sent. Appending (never splicing) also means paste-token
    // ranges found earlier in the string are untouched.
    const txt = appendSessionRefLinks(typedTxtDirs, sentSessionRefs)
    const displayTxt = appendSessionRefLinks(typedDisplayTxt, sentSessionRefs)
    // Expand paste tokens for the LLM; UI-facing displayTxt keeps the tokens
    // intact so the user bubble can render them as clickable chips.
    const activePastes = isolated ? [] : pasteBlocksRef.current
    let llmTxt = activePastes.length ? expandPasteTokens(txt, activePastes) : txt
    // Prepend knowledge context if pending
    let knowledgeBlock: import('./chat/useKnowledgeFetch').KnowledgeBlock | null = null
    if (!isolated && knowledgeFetchRef.current.pendingKnowledge) {
      knowledgeBlock = knowledgeFetchRef.current.pendingKnowledge
      llmTxt = expandKnowledgeBlock(knowledgeBlock) + '\n' + llmTxt
    }
    if (!isolated) knowledgeFetchRef.current.clearPending()
    const bubblePastes = pruneBlocksUtil(displayTxt, activePastes)
    if (bubblePastes.length) saveStoredPaste(llmTxt, displayTxt, bubblePastes, filePaths)

    if (!isolated) setPrefillHint(false)
    // The alias sub-map this send is about to drop, kept as a frozen snapshot
    // (deleting the key unhooks it; the object survives) so EVERY composer-
    // recovery path can put the bookkeeping back beside the text and files it
    // restores (fork GPT review): a queued send cancelled after this clear
    // restored text + chips whose mentions reconciliation could no longer
    // see -- hand-deleting one left a stale chip that the next send silently
    // re-attached.
    const sentSlotTokens = !isolated && !optionText && uiSlot ? pickedFileTokens.current[uiSlot] : undefined
    if (!isolated && !optionText) {
      // Drops only THIS slot's own token sub-map (`pickedFileTokens` is
      // slot-scoped -- see useFileMentionTokens), never another slot's: a
      // DIFFERENT, still-open slot's attachment is untouched regardless of
      // what this send just carried.
      setInput(''); setPendingFiles([]); if (uiSlot) delete pickedFileTokens.current[uiSlot]; setPasteBlocks([]); setPendingSessions([]); if (uiSlot) { delete drafts.current[uiSlot]; delete fileDrafts.current[uiSlot]; delete pasteDrafts.current[uiSlot]; delete sessionRefDrafts.current[uiSlot]; saveDrafts() }
      // The challenge-handoff prompt is seeded into PREFILL_STORAGE_KEY and the
      // slot-restore effect re-applies it on slot changes. Once that prompt is
      // sent, clear the seed so a later slot-restore can't re-fill the (now
      // empty) composer with the already-sent text.
      try { sessionStorage.removeItem(PREFILL_STORAGE_KEY) } catch { /* sessionStorage unavailable */ }
    }
    // Target the slot the user is actually looking at (uiSlot, from the ref),
    // not the stale closure `activeSlot`. See the uiSlot note above.
    let slot = targetSlot ?? uiSlot
    // Only a normal (non-targeted) send consumes the one-shot "new session"
    // intent. A targeted send — e.g. submitting document comments to the
    // document's origin slot — must leave it intact for the user's next send.
    let forceNew = false
    if (!targetSlot) {
      forceNew = newSessionRef.current
      newSessionRef.current = false
    }
    if (!slot || forceNew) {
      sendingRef.current = true;
      // The composer was cleared above, so a create failure here would destroy
      // the user's text: `.unwrap()` rejects, send() unwinds, and nothing is
      // ever sent — no error bubble, no draft to recover, and sendingRef stuck
      // true (which suppresses the welcome state). Restore the composer, its
      // paste blocks and attachments, surface the failure, and bail.
      let created: { key: string } | null = null
      try {
        created = await dispatch(createSlot({ agent: pendingAgentRef.current || defaultAgent || undefined, agent_kind: pendingAgentRef.current ? pendingAgentKindRef.current : undefined, model: pendingModelRef.current || undefined, mode: modeRef.current })).unwrap()
      } catch (e: unknown) {
        sendingRef.current = false
        if (isolated) {
          // The app never consumed the composer. Keep its payload in the
          // page-level copyable notice, which survives slot switches, instead
          // of a draft or a user row that would age pending approvals.
          const failure = i18nT('pages.chatPage.send_failed_with_error', { error: createFailReason(e) })
          setActionError({ message: i18nT('appChatLaunch.unsent', { error: failure, message: raw }), title: i18nT('pages.chatPage.could_not_start_a_new_session'), preserveOnSwitch: true })
          return false
        }
        // Recover the payload WITHOUT clobbering anything newer. Two traps make a
        // plain assignment lossy here:
        //  - The composer is only cleared above when `!optionText`, and the
        //    reachable forceNew path IS the optionText path (Projects / Dev Fleet /
        //    Prompts navigate to ?autoSend=1&newSession=1), so the composer still
        //    holds the user's own draft — overwriting it would destroy exactly the
        //    kind of text this guard exists to protect.
        //  - The create is awaited, so meanwhile the user may have typed, attached
        //    files, or switched sessions.
        // So MERGE into whatever the target slot holds now, and only touch live
        // composer state while that slot is still the one on screen.
        // Restore in place ONLY when the composer still belongs to the slot that
        // issued the send. A no-slot send (auto-send that fires before the slot list
        // resolves) must NOT fall back to whatever session auto-selection has since
        // activated: that would splice a new-session payload into an unrelated
        // session and send it there on retry. Those cases get a notification.
        const sameSlot = activeSlotRef.current === uiSlot
        const onScreen = sameSlot
        // Un-consume the one-shot new-session intent while the user is still on the
        // slot that issued the send — re-arming after they switched away would make
        // THAT session's next message spawn an unintended new session. Also re-arm
        // whenever there was no origin slot: the queued retry below MUST still create
        // its own session, and `sameSlot` is false there as soon as auto-selection
        // activates one mid-await, which would otherwise send the payload into an
        // unrelated existing session.
        // `|| !uiSlot` on the VALUE too, not just the condition: a slotless send also
        // reaches the create branch via `!slot` with `forceNew === false` (the
        // challenge-token flow, whose own createSlot failed), and arming `false` there
        // would let the queued retry deliver the payload as a user turn in whatever
        // unrelated session auto-selection activates. A send that had no origin slot
        // must always create its own session on retry.
        if (sameSlot || !uiSlot) newSessionRef.current = forceNew || !uiSlot
        const keepFiles = onScreen ? pendingFilesRef.current : (uiSlot ? fileDrafts.current[uiSlot] ?? [] : [])
        const restoredFiles = [...new Set([...keepFiles, ...sentFiles])]
        // Session refs merge by key (they carry no sequence to collide on, unlike
        // pastes), keeping whatever the user staged since the failed send.
        const keepRefs = onScreen ? pendingSessionsRef.current : (uiSlot ? sessionRefDrafts.current[uiSlot] ?? [] : [])
        const restoredRefs = mergeSessionRefs(keepRefs, sentSessionRefs)
        const keepPastes = onScreen ? pasteBlocksRef.current : (uiSlot ? pasteDrafts.current[uiSlot] ?? [] : [])
        // Collapsed pastes resolve by `seq`, not id, and a paste made while the
        // composer was empty restarts at #1 — so a naive id-merge can leave two
        // blocks sharing #1, with both markers resolving to one of them and
        // silently swapping the user's content on retry. `carryPastes` owns the
        // rule: re-sequence the carried blocks past the kept ones and rewrite
        // their markers in the payload text.
        const carried = carryPastes(raw, activePastes, keepPastes)
        // `full` keeps every token: it is the payload a retry re-sends whole.
        const { full: payload, pastes: restoredPastes } = carried
        const keepText = onScreen ? inputRef.current : (uiSlot ? drafts.current[uiSlot] ?? '' : '')
        // Keep whatever the user typed while the create was in flight and append
        // the payload after it, without duplicating one the composer already
        // holds — a synchronously rejected create can land before React flushes
        // the clear. `mergeRecoveredDraft` owns that rule for every recovery
        // site, including the send-failure path further down.
        const restoredText = mergeCarriedDraft(keepText, carried)
        if (onScreen && uiSlot) {
          setInput(restoredText); setPasteBlocks(restoredPastes); setPendingFiles(restoredFiles); setPendingSessions(restoredRefs)
          // clearPending() above already consumed the knowledge selection, so a
          // retry would otherwise go out WITHOUT the context the user picked. Slot-
          // gated: selection is per-slot, so re-injecting while the user views another
          // session would smear it there. MERGE rather than skip-or-replace — `inject`
          // replaces, so skipping when a newer selection exists would drop the failed
          // turn's context, and replacing would drop what the user picked since. Newer
          // items win on an id collision.
          if (knowledgeBlock) {
            const newer = knowledgeFetchRef.current.pendingKnowledge?.items ?? []
            const newerIds = new Set(newer.map(i => i.id))
            knowledgeFetchRef.current.inject([...knowledgeBlock.items.filter(i => !newerIds.has(i.id)), ...newer])
          }
          dispatch(appendMessage({ role: 'error', content: i18nT('pages.chatPage.could_not_start_session_message_restored', { error: createFailReason(e) }), cls: '' }))
        }
        // Announce the failure wherever the in-chat bubble could not. Two shapes:
        //  - No origin slot at all: nothing durable can hold the text (a draft under
        //    the session auto-selection just activated would splice this payload into
        //    an unrelated conversation, and a composer restore lives in state the
        //    next slot switch wipes). So the notification CARRIES the message —
        //    expanded pastes and attachment paths included.
        //  - Origin slot exists but the user moved on: the draft is parked there, so
        //    point at it. An error bubble would land in the wrong session.
        if (!uiSlot) {
          // No session to restore into or persist to (a draft under the session
          // auto-selection just activated would splice this into an unrelated
          // conversation, and a notification body reaches the OS notification centre
          // — `useNativeNotification` publishes the latest unacked body, and any entry
          // can be re-marked unread, so `acked` is no barrier). Hand the payload back
          // to the mechanism that produced it instead: re-arming `autoSendRef` makes
          // the auto-send effect resend it. Text only — paste blocks and attachments
          // cannot exist on this path (no composer renders without a slot).
          //
          // If a slot is ALREADY active, the effect's deps
          // (`[send, connected, autoSendTick]`) will not change again on their own, so
          // bump the tick to drive the retry now — and stay silent, because that
          // retry reports its own outcome (it runs with a slot, so a second failure
          // produces the error bubble or the moved-on notification below). Telling the
          // user to retype while a retry is in flight invites a duplicate turn.
          // Otherwise nothing can drive it until a real `connected`/slot change, so
          // report it and be honest that the queue is tab-local.
          const retryNow = !!activeSlotRef.current
          autoSendRef.current = payload
          if (retryNow) {
            setAutoSendTick(t => t + 1)
          } else {
            // In-page as well as the toast: with no slot there is no composer
            // restore and no error bubble, so the notice is the only thing on
            // the page that says the send did not happen.
            const queuedBody = i18nT('pages.chatPage.message_queued_until_session_ready', { error: createFailReason(e) })
            showActionError(queuedBody, i18nT('pages.chatPage.could_not_start_a_new_session'))
            dispatch(addNotification({
              ts: uniqueNotificationTs(),
              kind: 'agent',
              priority: 'critical',
              title: i18nT('pages.chatPage.could_not_start_a_new_session'),
              body: queuedBody,
            }))
          }
        } else if (!onScreen) {
          // The knowledge selection is NOT restored here: `inject` writes to the slot
          // the user is now viewing, so restoring it off-screen would attach the failed
          // turn's context to an unrelated session. Re-selecting is a two-click library
          // action (unlike typed text, which is unrecoverable), so this reports the gap
          // instead of routing knowledge per-slot — but it must not be silent.
          const lostContext = knowledgeBlock
            ? ' Its knowledge context was not kept — re-pick it before you resend.'
            : ''
          // The restored draft lives in a session that is not on screen, so the
          // page the user is looking at shows nothing without this notice.
          const draftBody = i18nT('pages.chatPage.message_saved_as_draft', { error: createFailReason(e), extra: lostContext })
          showActionError(draftBody, i18nT('pages.chatPage.could_not_start_a_new_session'))
          dispatch(addNotification({
            ts: uniqueNotificationTs(),
            kind: 'agent',
            priority: 'critical',
            title: i18nT('pages.chatPage.could_not_start_a_new_session'),
            body: draftBody,
            slot: uiSlot,
          }))
        }
        // The failed create's alias snapshot goes back beside the text and
        // chips it restores (fork GPT review): without it a restored mention
        // is invisible to reconciliation and its chip sticks. Slot-keyed, so
        // this one merge serves both the on-screen and parked-draft arms.
        if (uiSlot && sentSlotTokens) mergeSlotTokens(uiSlot, sentSlotTokens)
        if (uiSlot) {
          setDraft(drafts.current, uiSlot, restoredText)
          setPasteDraft(pasteDrafts.current, uiSlot, restoredPastes)
          setFileDraft(fileDrafts.current, uiSlot, restoredFiles)
          setSessionRefDraft(sessionRefDrafts.current, uiSlot, restoredRefs)
          saveDrafts()
        }
        return false
      }
      const result = created
      slot = result.key;
      if (pendingProjectRef.current) {
        await api.chatSlotProject(result.key, pendingProjectRef.current).catch(e => {
          // eslint-disable-next-line no-console -- surface project-assign failures for debugging
          console.error('chatSlotProject failed', e)
        })
      }
    }
    setPendingAgent(''); setPendingModel(''); setPendingProject('')
    // Build meta for persistence (knowledge, files, pastes)
    const meta: Record<string, unknown> = {}
    if (filePaths.length) meta.files = filePaths
    if (dirPaths.length) meta.dirs = dirPaths
    if (bubblePastes.length) meta.pastes = bubblePastes
    if (knowledgeBlock) meta.knowledge = { items: knowledgeBlock.items.length, tokens: knowledgeBlock.totalTokens, titles: knowledgeBlock.items.map(i => i.title), content: knowledgeBlock.items.map(i => ({ title: i.title, text: i.content.slice(0, 2000) })) }
    if (widgetOrigin) meta.origin = 'widget'
    // A client-generated correlation ID so the server echo can be matched
    // to this exact optimistic bubble without relying on content equality.
    // The server preserves meta fields on the user row it appends, so the
    // echo carries both this sendId AND the server-minted `mid` (#2845).
    const sendId = mintSendId()
    meta.sendId = sendId
    const metaPayload = meta
    // A busy snapshot may be stale. The server's user event supplies the
    // bubble for an immediate dispatch; a real queue has its own card.
    const _busy = selectComposerBusy(store.getState(), slot ?? null)
    // Whether THIS send drew its own bubble. A plain send on a busy slot does
    // not: the server's `queue_push` card represents it. The receipt arms
    // below read this so none of them describes a row that was never rendered.
    const bubbleMinted = !_busy || forceNew
    if (bubbleMinted) {
      dispatch(appendMessage({ role: 'user', content: displayTxt, cls: '', ts: new Date().toISOString(), meta: metaPayload }))
    }
    if (!isolated) window.dispatchEvent(new Event('voice-stop'))
    sendingRef.current = false
    setTimeout(() => scrollBottom(), SCROLL_AFTER_RENDER_MS)
    if (slot) dispatch(startLocalTurn(slot))
    /**
     * Put the composer back the way it was before this send.
     *
     * Called from BOTH failure shapes: a transport error (fetch rejected) and a
     * REJECTED RESPONSE (`!body.queued && !body.ok` — e.g. an expired cookie
     * answering 403). Both mean the message did not go out, so both must recover
     * identically; previously only the transport branch restored, so a dropped
     * connection kept the user's message while a 403 discarded it.
     *
     * Persist for `slot` unconditionally (recoverable on disk), but only touch
     * the live input/blocks when `slot` is the one on screen. Compare against
     * activeSlotRef.current, NOT the closure's `activeSlot`: a new-session /
     * forceNew send creates a fresh slot and switches the UI to it, so the
     * closure value is stale — using it would leave the user's just-typed message
     * empty on the very session they are now viewing. The ref reflects what is
     * actually on screen, so it restores visibly for a new-session failure while
     * still not splicing a targeted send's text into an unrelated slot.
     *
     * Restores `typedTxt` — what the user actually TYPED — and brings the staged
     * references back as chips, rather than restoring the link-appended `txt`.
     * Restoring `txt` preserved the reference (the link is in the text) but left
     * it as a raw URL, and re-staging the chips ON TOP of that text would make
     * the retry append each link a SECOND time. Splitting them puts the composer
     * back in exactly its pre-send state: chip visible, link appended once on
     * retry. Paste blocks come back too, or the restored text would show a dead
     * `[ Paste #N · M lines ]` literal. Shares the create-failure path's merge
     * rule so a reference staged while the send was in flight is not clobbered.
     */
    const restoreComposerAfterFailedSend = () => {
      // App payloads remain in the page-level error notice for copying; the
      // composer was never consumed and must not gain the app's text or chips.
      if (!slot || isolated) return
      // Ownership of the live composer state, not the active tab: see the
      // steer receipt's `onScreenNow` for the mid-switch window this closes.
      const onScreenNow = composerSlotRef.current === slot
      const liveRefs = onScreenNow ? pendingSessionsRef.current : (sessionRefDrafts.current[slot] ?? [])
      const refsBack = mergeSessionRefs(liveRefs, sentSessionRefs)
      // MERGE, never overwrite. The send is in flight for up to 10s, and the user
      // can type a fresh message in that window — clobbering it with the failed
      // payload would lose newer work to recover older. Mirrors the create-failure
      // path above: keep what is there, append the failed payload unless it is
      // already the same text, and re-sequence the carried paste blocks so two
      // blocks cannot claim one `[ Paste #N ]` marker.
      const keepText = onScreenNow ? inputRef.current : (drafts.current[slot] ?? '')
      const keepPastes = onScreenNow ? pasteBlocksRef.current : (pasteDrafts.current[slot] ?? [])
      const carried = carryPastes(typedTxt, activePastes, keepPastes)
      const pastesBack = carried.pastes
      // Same merge rule as the create-failure path above, and the separator lives
      // in `mergeRecoveredDraft` rather than in a template literal here: the blank
      // line between the kept draft and the recovered payload is message
      // structure, not copy, so it stays off the i18n gate honestly rather than by
      // exemption (same treatment as appendSessionRefLinks).
      const textBack = mergeCarriedDraft(keepText, carried)
      setDraft(drafts.current, slot, textBack)
      setPasteDraft(pasteDrafts.current, slot, pastesBack)
      setSessionRefDraft(sessionRefDrafts.current, slot, refsBack)
      saveDrafts()
      if (onScreenNow) {
        setInput(textBack); setPasteBlocks(pastesBack); setPendingSessions(refsBack)
      }
      // Aliases come back with the text they describe -- MERGE, never
      // overwrite, so a file picked while the send was in flight keeps its
      // fresh bookkeeping (fork GPT review; same rule as the text above).
      if (sentSlotTokens) mergeSlotTokens(slot, sentSlotTokens)
    }
    // The POST, its 10 s deadline, the resolves-not-rejects trap and the body
    // classification all live in the chat-core transport now; this surface
    // only decides how to REACT to the receipt. `sendTurn` never rejects.
    const receipt = await sendTurn({
      message: llmTxt,
      slot: slot ?? undefined,
      meta: metaPayload,
      steer: steerNow,
      colorTheme: colorThemeRef.current,
    })
    const { body } = receipt
    // - `transport-error`: the fetch rejected. Restore and report only when
    //   no correlated server echo has already proved delivery.
    // - `response-late`: the deadline fired; the request may have arrived.
    //   The optimistic bubble stays pending -- `markSendUnconfirmed` stamps
    //   it, which draws its delivery-pending line, and `selectTurnInterrupted`
    //   never offers Resume for a row still carrying `meta.optimistic` -- and a
    //   WARN notice under it says so. No restore: the text may have been
    //   delivered late, and a duplicate turn is worse than a visible pending
    //   row (the same ruling the steer path applies to a minted bubble).
    // - `unknown`: a 2xx whose body would not parse. The request was accepted
    //   and only its answer is mangled, so it may have started a turn that is
    //   streaming right now. Reporting a refusal would hand the payload back
    //   and invite a retry that duplicates a delivered turn, so an unknown
    //   takes no action rather than asserting a refusal it cannot prove. It
    //   still falls through to the body-driven steps below, which all read
    //   `ok` / `queued` and are no-ops on an empty body.
    // Both failure branches are addressed to the SENDING slot: the user can
    // switch sessions while the POST is in flight, and a failure that lands
    // then must neither clear the new session's running state nor put its
    // error row in the new session's transcript (`endLocalTurn` is the
    // slot-keyed inverse of the `startLocalTurn` above; `appendSlotMessage`
    // routes to the slot's own list, the active one included).
    const failLocalTurn = (message: ChatMessage) => {
      if (isolated) {
        if (slot) dispatch(endLocalTurn(slot))
        setActionError({ message: i18nT('appChatLaunch.unsent', { error: message.content, message: raw }), preserveOnSwitch: true })
        return
      }
      if (slot) {
        dispatch(endLocalTurn(slot))
        dispatch(appendSlotMessage({ slot, message }))
      } else {
        dispatch(setSlotRunning(false))
        dispatch(appendMessage(message))
      }
    }
    if (receipt.status === 'transport-error') {
      if (slot && selectSendConfirmed(store.getState(), slot, sendId)) return true
      // Cause-stating and naming the restore ("...and try again"), the shared
      // core copy the other surfaces use, instead of a bare "Connection error".
      failLocalTurn({ role: 'error', content: i18nT('pages.chatPage.send_failed_connection'), cls: '' })
      restoreComposerAfterFailedSend()
      return false
    }
    if (receipt.status === 'response-late') {
      // Indeterminate, not failed: the local turn stays pending (a late WS
      // frame or the next slot refresh settles it) and the bubble keeps its
      // `optimistic` flag. A correlated echo is stronger evidence than the
      // missing response -- a channel-linked slot can deliver one -- and then
      // there is nothing to warn about.
      if (slot && selectSendConfirmed(store.getState(), slot, sendId)) return true
      // A busy-slot Queue send minted no bubble, so there is no row to mark and
      // the notice below would point at nothing. It keeps the bare pending
      // verdict; what becomes of its text is the restore decision this arm
      // does not take.
      if (!bubbleMinted || !slot) return true
      // The mark is what draws the bubble's pending line (the `optimistic` flag
      // alone cannot, see `markSendUnconfirmed`), and the row under the bubble
      // says what the line cannot: that the deadline passed and what to do.
      // WARN tone via NoticeCard's lead-glyph selector (parseNotice), exactly
      // as the steer receipt's notice above. Both addressed to the SENDING
      // slot like the failure rows: the user can switch sessions inside the
      // deadline window.
      dispatch(markSendUnconfirmed({ slot, sendId }))
      dispatch(appendSlotMessage({ slot, message: { role: 'notice', content: '\u26A0\uFE0F ' + i18nT('pages.chatPage.delivery_unconfirmed_pending'), cls: '' } }))
      return true
    }
    if (body.queued && llmTxt === typedTxtDirs) {
      // The server queued this send and its receipt names the entry:
      // `queue_id` is the same id `queue_push` broadcasts and the card's
      // cancel button carries, so the pre-send composer state binds to
      // exactly this card — content plays no part in the key, which is what
      // makes duplicate texts, serialization-colliding captions, and other
      // tabs' cards structurally unable to consume someone else's record.
      // A receipt without `queue_id` (an older gateway, a requeued steer)
      // simply doesn't stash — the parser fallback covers those cards.
      //
      // Eligibility is DERIVED, not enumerated: stash only when the POSTed
      // text is exactly what {raw, staged files} alone explain
      // (`typedTxtDirs` — prepareSendPayload + dir-token serialization).
      // Expanded paste blocks, appended session-ref links, a prepended
      // knowledge block, and ANY FUTURE feature that diverges `llmTxt`
      // from the composer state all fail this equality and fall to the
      // parser — a stash hit for such a send would restore `raw` WITHOUT
      // the context the user staged, silently dropping it, so the failure
      // mode of forgetting is a conservative fallback, not silent loss.
      //
      // No size bound on purpose: an entry is deleted on the cancel that
      // consumes it, and evicting a live entry would degrade that queued
      // card's cancel to the parser fallback — for a spaced attachment path
      // that is exactly the marker-in-composer data loss this PR exists to
      // fix. Entries orphaned by normal delivery are three small strings
      // and are bounded by how many sends a single tab queues in one
      // session.
      if (typeof body.queue_id === 'string' && body.queue_id) {
        queuedSendStash.set(body.queue_id, { raw, files: stagedFilesAtSend, sent: llmTxt, aliases: sentSlotTokens })
      }
    }
    if (receipt.status === 'refused') {
      // FRAMED like the steer's refusal (and ChatEmbed's): a raw backend reason
      // ("slot agent mismatch") reads as the agent erroring mid-work, not as
      // "your request never went out".
      failLocalTurn({
        role: 'error',
        content: receipt.reason
          ? i18nT('pages.chatPage.send_failed_with_error', { error: receipt.reason })
          : i18nT('pages.chatPage.send_failed'),
        cls: '',
      })
      // The server explicitly accepted neither (`ok` nor `queued`), so nothing
      // was sent — recovering the composer cannot duplicate a delivered turn.
      restoreComposerAfterFailedSend()
    }
    if (slot && confirmedDelivered(body)) {
      // The response remains a delivery receipt (#4131), even if the correlated
      // user echo is missed. The echo owns insertion before streaming, so the
      // receipt must never append another row.
      // Addressed to the SENDING slot because the user can switch sessions
      // while the POST is in flight. A queued acceptance is not delivery.
      // The receipt carries the server-minted user-row `mid` (when the send
      // dispatched immediately); handing it to the reconcile stamps it onto
      // this optimistic bubble so message-pinning works this turn instead of
      // only after the chat_done refresh.
      dispatch(confirmOptimisticSend({ slot, sendId, mid: typeof body.mid === 'string' ? body.mid : undefined }))
    }
    // No stateless-card retirement here: the server retires the card when the
    // user row lands (immediate dispatch) or when the queued entry pops, and
    // announces it with `question_card_resolved` to every window, this one
    // included. A failed send appends no row, so the card stays; a queued send
    // stays cancellable with the card intact until its pop.
    if (body.ok && !body.queued && folderCardAtSend && slot === entrySendSlot) {
      // Delivery bar and slot-identity guard for the folder-suggestion card's
      // turn-aging: the card was on screen when the user hit send (captured at
      // entry, active slot only) and the server confirmed the send was
      // delivered. Failed sends never reach here; queued sends are still
      // cancellable; forceNew reroutes (the send lands in a freshly created
      // session) answer nothing in the entry slot. ts pins the card
      // generation, so a replacement that landed mid-flight is not aged.
      dispatch(ageFolderSuggestion({ slot, ts: folderCardAtSend.ts }))
    }
    // The user answered in the composer instead of the card; a blocking card
    // is resolved over the network, so this cannot be a store-only retirement.
    void resolveAskAfterSend(body, slot === entrySendSlot ? askAtSend : null, dispatch)
    // The delivery verdict (see the callback's doc above). Only an explicit
    // `refused` reads as not-delivered here; `unknown` (a 2xx whose body did
    // not parse) may have started a turn, so it counts as delivered for the
    // same reason the composer above does not restore on it — a retry it
    // invited could duplicate a delivered turn.
    return receipt.status !== 'refused'
    // `send` is deliberately kept stable: it reads volatile values (agent,
    // model, project, mode, colorTheme, activeSlot) through refs so it does not
    // re-create on every keystroke/theme/agent change (it is passed to children
    // and consumed by the auto-send effect). setPending*/saveDrafts/scrollBottom
    // are stable, and defaultAgent is only a creation-time fallback — pulling
    // them into the dep array would defeat that stability without changing
    // outcomes.
    // send() no longer reads the closure `activeSlot` for its target. It reads
    // uiSlot = activeSlotRef.current, so it routes to the on-screen slot even
    // between the reducer flip and this callback's re-memoization.
    // activeSlot is left in deps as a harmless no-op: dropping it churns the
    // array for no behavior change (the ref is always current regardless).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeSlot, dispatch, connected])

  // Submit inline document comments to the session the file was opened from,
  // not the currently-active one. If the user switched sessions while the
  // panel was open, switch back to the origin session so the prompt + reply
  // land where the document belongs. switchSlot.pending sets activeSlot
  // synchronously, but send()'s closure activeSlot is stale until re-render,
  // so the origin slot is passed to send() explicitly.
  // Keep sendRef current so the streaming endpointer's auto-submit callback
  // (wired into the voice hook above, before send is declared) always invokes
  // the latest send(). Assigned in render like inputRef.current = input above.
  sendRef.current = send
  const submitComments = useCallback((message: string) => {
    // Defense-in-depth: the panels' submit buttons are gated on `connected`,
    // but bail here too so an offline call can't switch the active session
    // and then have send() silently drop the message.
    if (!connected) return false
    const target = tabsCtl.activeTab?.slot ?? null
    if (target && target !== activeSlot) dispatch(switchSlot(target))
    // The delivery verdict flows back to the panel: ArtifactPanel marks a
    // comment batch as sent only when this resolves true.
    return send(message, target ?? undefined)
  }, [connected, tabsCtl.activeTab, activeSlot, dispatch, send])

  // The armed auto-send (?autoSend=1, a signed token, an app launch) and a
  // widget action's prefill.
  useAutoSendIntake({ connected, send, autoSendTick, autoSendRef, appLaunchSendRef, widgetPrefillRef, setInput, raisePrefillHint })

  const approve = useCallback(async (action: string) => { if (activeSlot) await api.approveChatSlot(activeSlot, action) }, [activeSlot])
  // Approvals dismissed through the CollapsibleToolGroup mounts resolve via the
  // ONE-SHOT `api.resolveApproval` endpoint, which has no trust verb. The shared
  // `toApiDecision` (utils/approvalDecision.ts) is fail-closed and is the only
  // place that mapping is spelled — a Trust affordance on this path would claim
  // a standing grant the backend never records (#5400, #5434).
  const dismissApproval = useCallback((aid: string, decision?: string) => {
    dispatch(resolveByApprovalId({ id: aid, slot: activeSlot || undefined, decision }))
    const n = store.getState().notifications.items.find(x => x.approval_id === aid)
    if (n) dispatch(removeNotificationByTs(n.ts))
  }, [activeSlot, dispatch])
  const switchAgent = useCallback(async (agentName: string, kind?: 'member' | 'template') => {
    if (!activeSlot) {
      setPendingAgent(agentName, kind)
      // Clear any explicit pick made for the PREVIOUS agent rather than
      // re-seeding a resolved model: an empty pendingModel makes createSlot omit
      // `model`, which lets the backend resolve the new agent's own chain at
      // create time. Seeding the resolved id here pinned it instead (#2035).
      setPendingModel('')
      return
    }
    dispatch(setAgentSwitchNotice(null))
    try {
      // Same protocol as switchModel below (#4523): the acting tab must not
      // depend on the coalesced slots rebroadcast to see its own pick.
      // performAgentSlotSwitch mirrors exactly what the response names.
      await performAgentSlotSwitch(activeSlot, agentName, dispatch, kind)
    } catch (error) {
      // Closing the picker is the call sites' job and already happens
      // synchronously alongside this call, so a failure surfaces as the shared
      // notice rather than by holding the dropdown open.
      dispatch(setAgentSwitchNotice(agentSwitchFailureMessage(error)))
    }
    // The setPending* setters are useState setters, so they are stable and cost
    // nothing to list. `installedAgents`, `provider` and `queryClient` are
    // deliberately absent: this body reads none of them, and `installedAgents` is
    // a fresh array on every agents refetch, so naming it would rebuild the
    // callback — and every picker holding it — for no behavioral gain.
  }, [activeSlot, dispatch, setPendingAgent, setPendingModel])
  const switchModel = useCallback(async (modelName: string) => {
    // 'auto' is stored VERBATIM, not collapsed to ''. Both resolve to the same
    // provider behaviour server-side, but '' is also the "never chosen" state,
    // and every reader of an empty model re-resolves it to the agent template's
    // model (the `resolvedModel` / `_initResolvedModel` queries below, and the
    // backend's slot.model backfill). Writing '' therefore made an explicit Auto
    // pick snap straight back to e.g. claude-opus-5 — Auto was unselectable.
    // kiro-cli advertises `auto` as a real model id (and its default_model), and
    // the ChatPane + Alt+Shift model-cycle paths already send it verbatim.
    // `pendingModel` is forwarded into slot creation verbatim, and the sentinel is
    // not a model any provider serves. So it is held as NOTHING: creation omits the
    // model and the backend resolves the agent's own chain, which is what a held
    // `auto` would have resolved to anyway. Routing is armed by a pick made once the
    // slot exists, where the flag is set with it.
    if (!activeSlot) {
      if (modelName === JEV_ROUTE_MODEL) { setPendingModel(''); return }
      setPendingModel(modelName)
      return
    }
    try {
      const slotState = store.getState().dashboard.slots.find(s => s.key === activeSlot)
      // A staged slider pick or a legacy pair level is carried; an effort
      // write already on the wire is waited for, not re-sent, and its
      // presence blocks the legacy migration -- see effortToCarry.
      const inFlightEffort = inFlightSlotSwitchOutcome('reasoning_effort', activeSlot)
      const carriedEffort = effortToCarry(
        slotState?.model || '', slotState?.reasoning_effort || '',
        stagedSlotSwitchTarget('reasoning_effort', activeSlot),
        inFlightEffort !== null,
        codexPairModels,
      )
      await switchGroupedModel(carriedEffort, async level => {
        // Either the effort the user just picked in the embedded slider (still
        // inside its debounce, so not yet on the wire) or, on a Codex slot
        // that still pins model[max], that old pair level moving into the
        // separate effort field before a grouped row sends the bare model.
        // A failed effort write aborts the model pick instead of silently
        // resetting an owner's previous selection.
        let normalizedModel: string | undefined
        await performSlotSwitch('reasoning_effort', activeSlot, level,
          async () => {
            const r = await api.chatSlotReasoningEffort(activeSlot, level)
            normalizedModel = r?.model
            return r?.reasoning_effort ?? level
          },
          (value) => dispatch(updateSlot({
            key: activeSlot,
            reasoning_effort: value,
            ...(normalizedModel ? { model: normalizedModel } : {}),
          })))
      }, async afterEffort => {
        // performSlotSwitch owns the whole protocol: per-slot+field serialized
        // dispatch, latest-request-wins adjudication, hung-request timeout, and
        // exactly-one store write on the authoritative value (#4523). The store
        // write is deliberately NOT awaited on the server's slots rebroadcast:
        // that push is coalesced and never arrives with the websocket down.
        await performSlotSwitch('model', activeSlot, modelName,
          async () => {
            // The response's `model` is the stored value (deprecated ids are
            // remapped server-side), so prefer it over the requested name.
            // Effort first on the wire (see switchGroupedModel); a refused
            // effort aborts this pick before anything is sent.
            await afterEffort
            const r = await api.chatSlotModel(activeSlot, modelName)
            return r?.model ?? modelName
          },
          // The routing flag is written from the REQUEST, not from the response's
          // `model`: the gateway resolves the sentinel to `auto`, so the stored
          // model cannot tell a routed pick from a plain Auto one. Written on
          // every pick, because picking a concrete model is what clears it.
          (value) => dispatch(updateSlot({
            key: activeSlot,
            model: value,
            jev_route: modelName === JEV_ROUTE_MODEL,
          })))
      }, () => inFlightSlotSwitchOutcome('reasoning_effort', activeSlot))
      queryClient.invalidateQueries({ queryKey: ['slot-selection-capabilities', activeSlot] })
    } catch (e) {
      // Same failure surface as the agent switch beside this: the shared
      // notice toast, preferring the server's own message. The chip keeps
      // showing what is actually running either way.
      dispatch(setAgentSwitchNotice(agentSwitchFailureMessage(e)))
      // eslint-disable-next-line no-console -- surface switchModel failures for debugging
      console.error('switchModel failed', e)
    }
    // Dismissal is the picker's job, not this callback's: a row click closes
    // the menu at the call site (the same shape as the agent picker and the
    // split-pane ChatPane picker), so a rejected switch is reported by the
    // notice toast above, never by a menu left open. Reasoning-effort edits
    // live on the drill-in page and keep the menu open on their own.
    // setPendingModel is a stable useState setter.
  }, [activeSlot, codexPairModels, dispatch, queryClient, setPendingModel])
  // A pick from the picker: a row click or Enter on the sole filtered match.
  // Closes the menu and, when the composer held focus at open time, hands
  // focus back to it (see `modelPickerReturnsFocusRef`). The picker's other
  // exits — Escape, outside click, the drill-in page's own links — are not
  // picks and keep their existing focus behaviour.
  const pickModel = useCallback((modelName: string) => {
    switchModel(modelName)
    setModelDropdown(false)
    if (modelPickerReturnsFocusRef.current) focusComposer()
  }, [switchModel, setModelDropdown])
  const setProject = useCallback(async (path: string) => {
    if (!activeSlot) { setPendingProject(path); return }
    try {
      // Same protocol as switchModel above; the server realpath-normalizes
      // the directory, so the response's spelling is what gets written.
      await performSlotSwitch('project', activeSlot, path,
        async () => {
          const r = await api.chatSlotProject(activeSlot, path)
          return r?.project ?? path
        },
        (value) => dispatch(updateSlot({ key: activeSlot, project: value })))
    } catch (e) {
      dispatch(setAgentSwitchNotice(agentSwitchFailureMessage(e)))
      // eslint-disable-next-line no-console -- surface setProject failures for debugging
      console.error('setProject failed', e)
    }
    // setPendingProject is a stable ref-backed setter.
  }, [activeSlot, dispatch, setPendingProject])

  const currentSlot = slots.find(s => s.key === activeSlot)
  // App-contributed session controls and the folder context they are handed.
  const sessionControlState = useComposerSessionControls({ activeSlot, currentSlot, queryClient })
  const {
    sessionControls, openSessionControl, setOpenSessionControl, sessionControlRect, anchorSessionControl,
    refreshSessionControlStatuses, activeFolderName, sessionControlKey, sessionControlStatuses, chatFolders,
    chatFoldersError, chatFoldersLoaded, mobileNewSessionInFlightRef, mobileNewSessionBusy, setMobileNewSessionBusy,
  } = sessionControlState
  // One source for both same-meaning markers in the agent pop-up: the row's check and
  // the default-agent row's label. Reading the slot twice let them disagree.
  // A peer-bound session falls back to the PEER's default, never this machine's:
  // the backend deliberately stores no agent for such a slot (the peer picks), so
  // `defaultAgent` here would advertise a crew from the wrong roster while the
  // peer answered with its own.
  const effectiveDefaultAgent = remoteCrew.isRemote ? (remoteCrew.capabilities?.default_agent || '') : defaultAgent
  const activeAgentName = currentSlot?.agent || effectiveDefaultAgent || 'default'
  // Refs so the "run in terminal" listener (registered once) always sees the
  // live panel controller + this chat's working directory.
  const tabsCtlRef = useRef(tabsCtl); tabsCtlRef.current = tabsCtl
  // Stable, so the memoized dock does not re-render on every streamed chunk.
  // Close the find pane FIRST, as revealAppInPanel / handleFileOpen /
  // handleOpenDiff do: the find pane owns the right-hand dock exclusively
  // (shouldMountSidePanel returns false while it is open), so without the close
  // the dashboard would open behind a pane the user cannot see past. `close()`
  // is safe with nothing open.
  // The Dynamic Dashboard is a Feature Preview (Settings > Developer). While it
  // is off there is no dock to open from, no menu entry to open with, and a
  // persisted `command-center` tab is withheld from the strip (SidePanel), so
  // the opener is absent rather than a path onto a view nothing else offers.
  const dashboardPreview = usePreviewFlag(PREVIEW_DASHBOARD)
  const openCommandCenterOn = useCallback(() => {
    search.close(); dispatch(openActivityPanel()); tabsCtlRef.current.openView('command-center')
    // eslint-disable-next-line react-hooks/exhaustive-deps -- `search.close` is a useCallback([]) in useMessageSearch; the object around it is rebuilt every render
  }, [dispatch, search.close])
  const openCommandCenter = dashboardPreview ? openCommandCenterOn : undefined

  /** Bring an app's panel tab back — focusing it if open, re-creating it if the
   *  user closed it (`openApp` upserts).
   *
   *  The auto-open effect above deliberately does not re-open a tab the user
   *  closed, which is why the bubble placeholder has to be a real control rather
   *  than static text. Note the effect's once-per-tool-call guard holds only
   *  PER CHATPAGE MOUNT: `openedAppTabsRef` is not persisted, so navigating away
   *  and back re-arms it. Closing the find pane is part of the action: `isSidePanelHidden`
   *  keeps the panel hidden while search owns the dock, so without this the click
   *  would open a tab the user cannot see and look broken.
   *
   *  `close()` runs unconditionally, exactly as handleFileOpen / handleArtifactOpen /
   *  handleOpenDiff do. Guarding it on `search.isOpen` would pull that value into the
   *  closure, and `renderMessage` below holds this callback across renders where the
   *  find pane opens — so a captured `isOpen === false` would skip the close entirely
   *  and open the tab behind the hidden dock. `close()` is already safe with nothing
   *  open: it only hands focus back `if (wasOpen)`. */
  const revealAppInPanel = useCallback((toolCallId: string) => {
    search.close()
    dispatch(openActivityPanel())
    // Same title derivation as the auto-open effect: an event-time read from
    // the Provider-bound store keeps the payload out of this callback's deps.
    const payload = activeSlot ? boundStore.getState().chat.mcpApps?.[mcpAppKey(activeSlot, toolCallId)] : undefined
    tabsCtlRef.current?.openApp(toolCallId, mcpAppTabTitle(payload, i18nT('pages.chatPage.mcp_app_tab_title')), activeSlot ?? null)
    // As at handleFileOpen: the rule asks for the whole `search` object only because
    // `close` is INVOKED and a called member is attributed to its receiver, not
    // because this body reads `search` itself.
    // eslint-disable-next-line react-hooks/exhaustive-deps -- `search.close` is a useCallback([]) in useMessageSearch, so the listed member already pins everything this body calls; naming the enclosing object would make this a new function every render and churn renderMessage below
  }, [dispatch, activeSlot, boundStore, search.close])
  // `@`-mentions of files and folders and the file chips they stage.
  const { clampOutOfTokens, handleAddToContext, removeFileChip, removeDirChip, selectPickedFile } = useFileMentionActions({
    staging,
    inputRef,
    setInput,
    currentProjectRef,
    voiceCaretRef,
    voicePendingCaretRef,
    reconcileFileChipsRef,
  })

  // ── Follow-up card actions (suggest_followup MCP tool) ───────────────────
  // Both routes PRE-FILL a composer and stop; neither sends. `setPendingInput`
  // is consumed by usePendingInputIntake above, which drops the text into the composer and
  // flags the prefill hint — the same path the Projects page and command
  // palette use, so there is one prefill mechanism, not a parallel one.
  //
  // Live per-slot card timestamps, read inside async actions without making them
  // depend on (and re-create on) every card change.
  const followupTsRef = useRef<Record<string, { items: FollowupItem[]; ts: number }>>({})
  followupTsRef.current = followupTsBySlot
  const followupAddToSession = useCallback((item: FollowupItem) => {
    if (!activeSlot) return
    // APPEND when the composer already holds unsent text: the pending-input path
    // replaces the draft and persists it, so a plain set would silently destroy
    // whatever the user was mid-way through typing. `inputRef` is the live
    // composer value; `mergeIntoDraft` is shared with the error → agent hand-off
    // drain so the two paths cannot drift.
    dispatch(setPendingInput(mergeIntoDraft(inputRef.current, item.prompt)))
    // Clear by the RENDERED card's ts, as the worktree action does: a newer card
    // for this slot can land between render and click, and an unqualified clear
    // would delete suggestions the user never saw.
    dispatch(clearFollowupCard({ slot: activeSlot, ts: followupTsRef.current[activeSlot]?.ts }))
  }, [dispatch, activeSlot])

  // Folder suggestion: accepting reuses the ONE move path every other surface
  // (row menu, drag-to-folder, new-chat-in-folder) already funnels through, so
  // the optimistic update and its guarded rollback are inherited rather than
  // re-implemented here. Both answers clear the card by the ts it rendered with,
  // for the same reason the follow-up actions do. The card passes the folder id
  // its dropdown currently shows — the suggestion by default, or whatever the
  // user picked instead.
  const folderSuggestionAccept = useCallback((folderId: string) => {
    if (!activeSlot || !folderSuggestion) return
    moveSlotToFolder(activeSlot, folderId)
    dispatch(clearFolderSuggestion({ slot: activeSlot, ts: folderSuggestion.ts }))
  }, [activeSlot, folderSuggestion, moveSlotToFolder, dispatch])

  const folderSuggestionDecline = useCallback(() => {
    if (!activeSlot || !folderSuggestion) return
    // Nothing to tell the backend: it already spent its one offer for this slot,
    // so declining is purely "take the card away".
    dispatch(clearFolderSuggestion({ slot: activeSlot, ts: folderSuggestion.ts }))
  }, [activeSlot, folderSuggestion, dispatch])

  // Fallback branch name when the agent did not supply one: slugify the title
  // under FOLLOWUP_BRANCH_RE's grammar (the server re-validates, so a slug that
  // degenerates to empty is replaced rather than sent and rejected).
  const followupBranchFor = useCallback((item: FollowupItem) => {
    if (item.branch) return item.branch
    const slug = item.title
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, '-')
      .replace(/^-+|-+$/g, '')
      .slice(0, 40)
    return `followup/${slug || 'suggestion'}`
  }, [])

  const followupStartInWorktree = useCallback(async (item: FollowupItem) => {
    const repo = currentSlot?.project
    if (!repo) throw new Error(i18nT('pages.chatPage.this_session_has_no_project_directory_to_branch'))
    const originSlot = activeSlot
    // Inherit the spawning session's sidebar folder, so a worktree started from
    // a session filed under a project folder lands in that same folder instead
    // of at the sidebar's top level (#6347). `currentSlot` IS the spawning
    // session (it already supplies `.project` above), and its `folder_id` is
    // undefined when that session is itself unfiled — in which case the new
    // session lands top-level, exactly today's behaviour. No default folder is
    // invented; unfiled stays unfiled.
    const originFolderId = currentSlot?.folder_id
    // Capture the card's ts up front so completion clears only THIS card. A
    // newer card can arrive for the same slot while the request is in flight;
    // without the guard the older action's completion would clobber it.
    const originTs = originSlot ? followupTsRef.current[originSlot]?.ts : undefined
    // Create the worktree FIRST: if git refuses (branch exists, not a repo),
    // we must not have already spawned an empty session the user has to clean
    // up. The card surfaces the thrown message inline.
    const res = await api.createWorktree(repo, followupBranchFor(item))
    const path = res?.path
    if (!path) throw new Error(res?.error || i18nT('pages.chatPage.worktree_creation_returned_no_path'))
    let slotKey = ''
    try {
      // `activate: false` on purpose: the slot must be SCOPED to the worktree
      // before the user can type into it. Activating first (the default) leaves a
      // window where the composer is live but `chatSlotProject` is still pending,
      // so a turn sent in that window would run in the default directory — agent
      // tools writing to the wrong checkout. It also means a scoping failure can
      // render its error on the still-mounted card instead of unmounting it.
      const slot = await dispatch(createSlot({ mode, project: path, folder_id: originFolderId, activate: false })).unwrap()
      slotKey = slot?.key || ''
    } catch {
      // The worktree exists but the session does not. Say so, and name the path:
      // the create endpoint is idempotent for its own destination, so pressing
      // the button again reuses this worktree instead of 409-ing on it.
      throw new Error(
        `Worktree created at ${path}, but its session could not be opened and scoped. ` +
        'Press the button again to retry — the existing worktree will be reused.',
      )
    }
    // A fulfilled thunk with no key would skip every guard below (scoping,
    // activation, focus verification) and prefill whatever session is on screen
    // — the exact fail-open the docs promise not to do. Fail closed instead.
    if (!slotKey) {
      throw new Error(
        `Worktree created at ${path}, but no session was returned. ` +
        'Press the button again to retry — the existing worktree will be reused.',
      )
    }
    // Scoping is NOT done here: `createSlot({ activate: false })` awaits the
    // project assignment before it publishes the slot, and deletes the session if
    // that fails, so the slot is never reachable in an unscoped state. A failure
    // therefore rejects the thunk and is reported by the catch above.
    // createSlot's fulfilled reducer deliberately does NOT activate its result
    // if the user switched sessions while the create was in flight. The
    // prefill below writes to the *active* composer, so without this the
    // prompt would land in whatever unrelated session is on screen and the new
    // worktree session would open empty. The user asked for this worktree by
    // clicking; take them to it — and if that fails, surface the error and
    // keep the card rather than prefilling the wrong conversation.
    // Read the store directly, NOT activeSlotRef: the ref is refreshed by a
    // render, and `unwrap()` resolves as soon as the reducer ran — so a stale
    // ref would report a failure (and skip the prefill) on a switch that in
    // fact succeeded. store.getState() sees the committed value immediately.
    // Hand the prompt over through PREFILL_STORAGE_KEY *before* the switch — the
    // same channel the ?sid / popout paths use. `setPendingInput` alone loses the
    // race: its consuming effect is declared BEFORE the per-slot draft-restore
    // effect, so when the switch and the prefill land in one React commit the
    // restore runs last and overwrites the composer with the incoming slot's
    // (empty) draft, and the prompt vanishes. Seeding the prefill makes the
    // restore itself apply the prompt, so there is nothing left to race.
    writePrefill(slotKey, item.prompt)
    if (store.getState().chat.activeSlot !== slotKey) {
      try {
        await dispatch(switchSlot(slotKey)).unwrap()
      } catch {
        throw new Error(
          `Worktree ready at ${path}, but its session could not be opened. ` +
          'Switch to it in the sidebar, or press the button again.',
        )
      }
    }
    if (store.getState().chat.activeSlot !== slotKey) {
      throw new Error(
        `Worktree ready at ${path}, but its session is not in focus. ` +
        'Switch to it in the sidebar, or press the button again.',
      )
    }
    dispatch(setPendingInput(item.prompt))
    if (originSlot) dispatch(clearFollowupCard({ slot: originSlot, ts: originTs }))
  }, [currentSlot?.project, currentSlot?.folder_id, followupBranchFor, dispatch, mode, activeSlot])

  // Outside events feeding the side panel, the dock terminal and the composer,
  // then the cold file tabs' reads.
  useChatEventBridges({
    activeSlot,
    activeSlotRef,
    messages,
    slots,
    dispatch,
    queryClient,
    showActionError,
    tabsCtlRef,
    currentProjectRef,
    deleteTerminalSessionRef,
    terminalReuseRef,
    terminalCfgUnsettledRef,
    terminalCfgReadFailedRef,
    inputRef,
  })
  useColdFileTabHydration({ tabsCtl, showActionError })
  // Session mode of the active slot. In the unified chat view the page-level
  // `mode` prop is always '' — the slot's own mode is the source of truth.
  const effectiveMode = currentSlot?.mode || mode
  const title = currentSlot?.title && currentSlot.title !== currentSlot.key ? currentSlot.title : activeSlot || ''
  const displayMode = slotApprovalMode(approvalMode, currentSlot)
  // What the composer's model, effort and project chips show.
  const {
    shownModel, _pinShownModel, chipDefault, modelMarker, effortSupported, effortLevelsOverride, remoteContextWindow,
    _modelPinAgent, _modelPinActive, _modelPinPinned, pinModelToAgentMut,
    defaultEffort, effectiveEffort,
    _slotProject, projectGit, projectGitError, projectBranch,
  } = useComposerChips({
    currentSlot,
    defaultAgent,
    pendingAgent,
    installedAgents,
    provider,
    availableModels,
    codexPairModels,
    selectionCapabilities,
    selectionCapabilitiesQ,
    remoteCrew,
    dispatch,
    queryClient,
    showActionError,
  })

  // Auto-open the Git panel when the slot has a project dir that is a git repo.
  // OPT-IN (dashboard.auto_open_git_panel, default off) because the marker below
  // cannot make this the once-per-project nudge it reads like: a new slot inherits
  // `dashboard.default_project`, so keying on slot+path re-fires for every new
  // chat in the same repo — forever. The Git TAB is still created unconditionally
  // (same as the folder tab below), so the panel is one click away when off.
  useEffect(() => {
    if (!activeSlot || !_slotProject || projectGitError) return
    if (!projectGit?.repo) return
    // Do not consume the marker before the opt-in's value is known — see
    // `autoOpenGitPanelKnown`.
    if (!autoOpenGitPanelKnown) return
    const key = `mc-git-panel-opened:${activeSlot}:${_slotProject}`
    if (localStorage.getItem(key)) return
    // If the marker cannot be persisted, skip the auto-open entirely: opening
    // changes tabsCtl, which re-runs this effect, and an absent marker would
    // make it open again forever. safeSetItem reports whether the write landed
    // (after reclaiming a disposable tier if the quota was full), so the guard
    // is the return value rather than a caught throw.
    if (!safeSetItem(key, '1')) return
    tabsCtl.openView('git')
    if (autoOpenGitPanel) dispatch(openActivityPanel())
  }, [activeSlot, _slotProject, projectGit?.repo, projectGitError, tabsCtl, dispatch, autoOpenGitPanel, autoOpenGitPanelKnown])

  const [sidebarPinned, setSidebarPinned] = useState(() => localStorage.getItem('mc-sidebar-pinned') !== 'false')
  const sidebarPinnedRef = useRef(sidebarPinned)
  sidebarPinnedRef.current = sidebarPinned
  // Pre-focus session-list state while the Web Preview expand mode auto-hides
  // it, so exiting focus mode restores what the user had. null = focus mode is
  // not the reason the list is hidden (the user owns the state).
  const sidebarAutoHidden = useRef<boolean | null>(null)
  // One LLM title generation at a time from the phone bar's menu item: the
  // menu closes on select, so nothing else stops a second tap from starting a
  // concurrent call whose last response would win (SessionTitleControl's
  // button has the same guard in its `generating` state).
  const menuAutoTitleInFlight = useRef(false)
  const [sidePanelDock] = useSidePanelDock()
  // Recomputed on every dock flip: the wrapper keeps one React key across the
  // flip, so both axes have to stay named or the flipped-away one gets driven
  // back to its base (see sidePanelDockMotion).
  const sidePanelDockAnim = useMemo(() => sidePanelDockMotion(sidePanelDock), [sidePanelDock])
  const [sidebarWidth, setSidebarWidth] = useState(() => {
    const v = parseInt(localStorage.getItem('mc-sidebar-width') || '', 10)
    return !isNaN(v) && v >= SIDEBAR_MIN && v <= SIDEBAR_MAX ? v : 260
  })
  const [sidebarDragging, setSidebarDragging] = useState(false)
  // Pinned to the slot the rename opened on: activeSlot moves the instant the user
  // switches sessions, and a live-resolved commit would rename the wrong session.
  const [editingTitleSlot, setEditingTitleSlot] = useState<string | null>(null)
  const editingTitle = editingTitleSlot !== null && editingTitleSlot === activeSlot
  // Leaving abandons the draft. The pin alone closes the editor but keeps it, so a
  // return would revive stale text and a blur could overwrite a newer title.
  useEffect(() => { setEditingTitleSlot(null) }, [activeSlot])
  // Native session grid "split mode": an in-place tiling of the chat surface (NOT an
  // overlay). The flag is EPHEMERAL per mount — nav/refresh lands on single chat —
  // but the LAYOUT persists per anchor slot (splitLayoutStore). So a split is
  // preserved across navigation, and a member session opened on its own shows single
  // chat plus an "in split" badge that re-enters it (β model). `splitAnchor` is the
  // slot whose split we're showing (the one ⌘D'd from, or the badge's target).
  // enterSplit opens Split View for `anchor`: SessionGridView restores anchor's saved
  // layout if one exists, else seeds [anchor | placeholder]. Closing back down to a
  // single session dissolves the layout and collapses to native chat (onCollapse).
  const enterSplit = useCallback((anchor: string | null) => { setSplitAnchor(anchor); setSplitMode(true) }, [])
  // Anchor of the persisted split the active session belongs to (>= 2 live sessions),
  // or null — drives the "in split" badge in single chat. Validated against live
  // slots so a stale layout (a member was deleted) never shows a dead badge.
  const splitAnchorForActive = useMemo(() => {
    if (!splitFeatureEnabled || splitMode || !activeSlot) return null
    const anchor = anchorForSlot(activeSlot)
    if (!anchor) return null
    const liveKeys = new Set(slots.map((s) => s.key))
    return sessionSlots(loadLayout(anchor)).filter((k) => liveKeys.has(k)).length >= 2 ? anchor : null
  }, [splitFeatureEnabled, splitMode, activeSlot, slots])
  // True when the active session IS the anchor of its live persisted split (the slot
  // ⌘D was originally pressed from). The anchor's natural view IS its split, so we
  // auto-open it (no badge, no extra click); non-anchor members stay single chat + badge.
  const activeIsSplitAnchor = splitAnchorForActive !== null && splitAnchorForActive === activeSlot
  // Auto-enter split when you land on its anchor. Gated on splitMode being off (so we
  // don't fight an in-progress exit) and on a resolved activeSlot + real >=2-member live
  // layout (so a fresh refresh never seeds an orphan pane).
  // Members never auto-enter; closing a split to 1 dissolves the layout so there's no loop.
  useEffect(() => {
    if (embedMode || splitMode || !activeIsSplitAnchor) return
    enterSplit(splitAnchorForActive)
  }, [embedMode, splitMode, activeIsSplitAnchor, splitAnchorForActive, enterSplit])
  // ⌘D / Ctrl+D enters split mode from single chat (splitting the current session).
  // Inside split mode the grid (SessionGridView) owns ⌘D = split the focused pane.
  useEffect(() => {
    if (embedMode) return
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && !e.shiftKey && !e.altKey && e.key.toLowerCase() === 'd') {
        if (!splitFeatureEnabled || splitMode || !activeSlot) return
        e.preventDefault()
        enterSplit(activeSlot)
      }
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [embedMode, splitMode, enterSplit, splitFeatureEnabled, activeSlot])
  const lastTextIdx = useMemo(() => {
    for (let i = messages.length - 1; i >= 0; i--) {
      // Agree with renderMessage's skip: a hidden invisible-only row draws
      // nothing, so anchoring Regenerate/variant-switching on it would make
      // those affordances unreachable for the rest of a quiet monitor run.
      // System-notice rows (compaction / session reload) are passed over too:
      // they draw a system card, not a reply, so hosting Regenerate/variant
      // switching on them would target the wrong row.
      if (messages[i].role === 'assistant' && !isHiddenInvisibleAssistantRow(messages[i]) && !isSystemNoticeRow(messages[i])) return i
    }
    return -1
  }, [messages])
  const [regenerating, setRegenerating] = useState(false)
  useEffect(() => { setRegenerating(false) }, [activeSlot])
  // Clear typing dots as soon as streaming starts
  useEffect(() => {
    if (regenerating && isStreaming) setRegenerating(false)
  }, [regenerating, isStreaming])
  // Safety timeout
  useEffect(() => {
    if (!regenerating) return
    const t = setTimeout(() => { setRegenerating(false) }, 30_000)
    return () => clearTimeout(t)
  }, [regenerating])
  // ---- Refused-press notice ---------------------------------------------------
  // One surface for any press the server refuses. These endpoints re-check under
  // the slot lock and can refuse a press the client believed was available (a
  // turn already running, a stop in progress, a pending approval, a readiness
  // probe that timed out). Left in the console, that refusal reaches the user as
  // the button flicking to disabled and straight back — a control that promises
  // action and then says nothing. The server names the reason; this shows it
  // above the composer with a per-action title. One state slot serves every
  // refusable press (the newest refusal wins), so a press added later inherits
  // the surface by calling `showRefusedPress` instead of re-discovering
  // console.warn. The title map is `as const` so the key gate resolves every
  // member from the single render-site call.
  const [refusedPress, setRefusedPress] = useState<{ action: RefusedPressAction; message: string } | null>(null)
  const showRefusedPress = useCallback((action: RefusedPressAction, e: unknown) => {
    setRefusedPress({ action, message: e instanceof Error && e.message ? e.message : String(e) })
  }, [])
  useEffect(() => { setRefusedPress(null) }, [activeSlot])
  // A turn that actually starts retires the refusal: whatever the slot was busy
  // with is over, so the old reason would now describe a state that passed.
  useEffect(() => { if (slotRunning) setRefusedPress(null) }, [slotRunning])
  const handleRegenerate = useCallback(() => {
    if (!activeSlot || regenerating || slotRunning || activeSlotRemoteBound) return
    // Mirror the server's scan exactly (chat_regenerate.py): the turn being
    // regenerated ends at the last assistant row BY ROLE — hidden
    // invisible-only rows included — so this optimistic truncation cannot
    // diverge from the history rewrite the server persists. System-notice
    // rows (compaction / session reload) ARE skipped, on both sides: they are
    // status rows, not the reply, and capturing one as the variant would drop
    // the real reply from variant history. The skip-aware lastTextIdx only
    // decides which drawn row HOSTS the affordance; its extra invisible-row
    // skip would truncate after an earlier user row than the server does.
    let aiIdx = -1
    for (let i = messages.length - 1; i >= 0; i--) {
      // Never cross a real user turn (mirror the server): a reply found past
      // a newer user row (e.g. a /compact row awaiting only its notice) would
      // be regenerated by deleting that newer turn, irreversibly.
      if (messages[i].role === 'user') break
      if (messages[i].role !== 'assistant') continue
      if (isSystemNoticeRow(messages[i])) continue
      aiIdx = i
      break
    }
    if (aiIdx < 0) return
    const uIdx = messages.slice(0, aiIdx).map(mm => mm.role).lastIndexOf('user')
    if (uIdx < 0) return
    const snapshot = [...messages]
    dispatch(truncateAfterIndex(uIdx + 1))
    setRegenerating(true)
    api.regenerateSlot(activeSlot).catch((e: unknown) => {
      showRefusedPress('regenerate', e)
      dispatch(replaceMessages(snapshot))
      setRegenerating(false)
    })
  }, [activeSlot, regenerating, slotRunning, activeSlotRemoteBound, messages, dispatch, showRefusedPress])

  // ---- Continue the thread ---------------------------------------------------
  const {
    continuable, interrupted, sendUnconfirmed, continuing, handleContinue,
    openModelPickerFromError, openDefaultModelSetting, openKiroSignIn, openMemberCapabilities,
    featureRequestRefused, sessionStartRepeated,
  } = useTurnRecovery({
    activeSlot,
    slotRunning,
    messages,
    navigate,
    anchorModelBtn,
    setModelDropdown,
    modelPickerReturnsFocusRef,
    showRefusedPress,
  })

  // The floating composer dock's measured clearance (see composerDock).
  const { inputAreaRef, dockH, dockGutter, dockRef } = useComposerDockMetrics(scrollerRef)

  // Quote / Ask on selected assistant text — the shared chat-core seam
  // (chat-core/composer/selectionActions): Quote lands in this composer with
  // the transit animation, Ask seeds the Side Chat. This page's Side Chat
  // surface is the activity panel's `side` tab; the /side slash command opens
  // it through the same `openActivityToTab('side')` bridge.
  const openSideChat = useCallback(() => { dispatch(openActivityToTab('side')) }, [dispatch])
  const { onQuote: handleQuote, onAsk: handleAsk, quoteFlight: flyingQuote, endQuoteFlight } = useSelectionQuoteAsk({
    slot: activeSlot,
    setInput,
    revealComposer,
    openSideChat,
  })
  // Split view's panes ask about THEIR slot, but the activity panel — and the
  // Side Chat inside it — is bound to the active slot. Re-bind it first (the
  // same switchSlot the grid's collapse path uses; split mode itself is not
  // left), then open the tab. The seed names the slot, so it waits for the
  // re-bound panel's composer rather than landing on the old slot's.
  //
  // Offline, the re-bind is withheld like every other switchSlot in this file
  // (the tab strip, the sidebar row, the ?sid deep link): a rejected switch
  // clears the pane's active messages and the transcript the reader just
  // selected from disappears until reconnect. The grid is handed no
  // `openSideChat` at all while disconnected, so the panes' toolbars offer
  // Copy / Quote only (capability by omission, never an Ask into the void);
  // the guard here covers the frame between the drop and the re-render, and
  // rather than open a Side Chat bound to some OTHER slot it does nothing.
  // Read at click time, not captured: the callback is memoized and the
  // gateway can drop between renders (the tab strip's own gate, now inside
  // useChatPageSessionController, keeps its ref the same way).
  const connectedRef = useRef(connected)
  connectedRef.current = connected
  const openSideChatForPane = useCallback((slot: string): boolean | Promise<boolean> => {
    if (slot === activeSlot) {
      dispatch(openActivityToTab('side'))
      return true
    }
    // `false` tells the selection seam the Ask did NOT happen, so it does
    // not seed a quote into a Side Chat that never opened.
    if (!connectedRef.current) return false
    // The re-bind is a request the server can reject (the pane's session was
    // deleted under it); `switchSlot.rejected` then falls back to the slot the
    // page was on. Report the verdict only once it is known: the seam seeds
    // on `true`, and a rejection is surfaced through the page's ErrorNotice
    // instead of leaving a silent, invisible seed behind.
    return dispatch(switchSlot(slot)).unwrap().then(
      () => {
        // A later switch (the user clicked another pane, or Asked from it)
        // may have landed while this one was in flight; the panel is bound to
        // whatever is active NOW, so opening the Side tab here would show the
        // other pane's Side Chat with this quote hidden in this slot's draft.
        // Report the Ask as not happened instead of seeding a stale slot.
        if (boundStore.getState().chat.activeSlot !== slot) return false
        dispatch(openActivityToTab('side'))
        return true
      },
      (e: unknown) => {
        showActionError(i18nT('pages.chatPage.side_chat_pane_gone', { error: errMessage(e) || i18nT('pages.chatPage.unknown_error') }))
        return false
      },
    )
  }, [activeSlot, boundStore, dispatch, showActionError])

  const handleEditResend = useCallback((index: number, ts: string, newContent: string) => {
    if (!activeSlot || slotRunning || activeSlotRemoteBound) return
    const snapshot = [...messages]
    dispatch(truncateAfterIndex(index))
    dispatch(appendMessage({ role: 'user', content: newContent, cls: '', ts: new Date().toISOString() }))
    setRegenerating(true)
    // Use /rewind (fork-and-swap) — discards the orphan kiro-cli session so
    // truncated forward turns can't resurface on resume. Mirrors kiro-cli's
    // native /rewind slash command, but swaps the session under the same
    // slot identity so the UI stays in place (no new tab, no title change).
    rewindWithRollback(activeSlot, ts, newContent, () => {
      dispatch(replaceMessages(snapshot))
      setRegenerating(false)
    })
  }, [activeSlot, slotRunning, activeSlotRemoteBound, messages, dispatch])

  // ⌘↑ / Ctrl+Up: open the LAST user message in the existing
  // Edit & resend editor. The chord itself is claimed by the composer
  // (`ChatInput` / `LexicalComposerInput`), which only fires from an empty
  // composer, so this handler's job is eligibility (the same gates the row's
  // `canEdit` affordance uses) and finding the target row: the newest
  // `role === 'user'` message — agent turns, tool cards and system/steering
  // rows are skipped by the role filter alone. `editLast` carries a rising
  // sequence number plus the target's identity (index AND ts) so only the
  // matching `UserMessage` row reacts and a re-render never re-opens the
  // editor over an in-progress draft. The row ACKNOWLEDGES consumption via
  // `onEditConsumed` (which nulls this state): a persisted request must never
  // survive a row remount, or the rising-edge mount would replay the editor
  // after Escape + session-switch and invite an accidental resend.
  const [editLast, setEditLast] = useState<{ seq: number; ts: string; index: number } | null>(null)
  // Backs `editLast.seq` with a counter that survives consumption — deriving
  // it from `editLast` itself (nulled by `onEditConsumed`) would restart at 1
  // after every consumed request, colliding with a mounted row's already-seen
  // `lastEditRequestRef` and silently dropping the next press.
  const editSeqRef = useRef(0)
  // True only while `renderFarmItem` builds an off-screen measurement row. The
  // farm draws through the same `renderMessage` as the live transcript, so
  // without this a farm-rendered `UserMessage` would receive the pending edit
  // request, consume it (nulling `editLast`) and measure its editor instead of
  // the bubble — the live row never opens and a wrong height gets cached.
  // Read synchronously while the elements are created, so a ref is enough.
  const farmPassRef = useRef(false)
  const handleEditConsumed = useCallback(() => setEditLast(null), [])
  // Same for a session switch: the request is slot-scoped intent, so leaving
  // the slot discards any unconsumed request instead of letting a later,
  // unrelated remount treat it as fresh.
  useEffect(() => { setEditLast(null) }, [activeSlot])

  const searchCtxValue = useMemo(() => ({
    term: search.term,
    caseSensitive: search.caseSensitive,
    currentMessageIdx: search.currentMessageIdx,
    currentOccurrenceIdx: search.currentOccurrenceIdx,
  }), [search.term, search.caseSensitive, search.currentMessageIdx, search.currentOccurrenceIdx])

  const renderUserContentCb = useCallback(
    // The session triple matches what the assistant / note rows hand
    // MarkdownRenderer (see the AssistantMessage and inject branches below),
    // so a `/chat?sid=…` link behaves identically across row kinds (#8253).
    (c: string, mt: Record<string, unknown> | undefined, ts?: string) => renderUserContent({
      content: c,
      meta: mt,
      onFileOpen: handleFileOpen,
      onFolderOpen: handleFolderOpen,
      linkPreviews: linkPreviewsOn,
      onSessionOpen: selectSessionTab,
      sessions: connected ? sessionTitles : undefined,
      activeSession: activeSlot || undefined,
      // The short-name chip needs the row's write time; without it no short name
      // resolves here, because slot numbers are reused.
      messageTs: ts,
    }),
    [handleFileOpen, handleFolderOpen, linkPreviewsOn, selectSessionTab, connected, sessionTitles, activeSlot]
  )

  useEffect(() => {
    const togglePin = () => {
      // Always-available collapse. Only guard is no-sessions (the sidebar is
      // force-open then anyway, so there is nothing to collapse).
      if (filteredSlotsRef.current.length === 0) return
      // Explicit user intent outranks the preview-expand auto-hide, so exiting
      // expand mode leaves this choice alone.
      sidebarAutoHidden.current = null
      setSidebarPinned(p => {
        const next = !p
        safeSetItem('mc-sidebar-pinned', String(next))
        return next
      })
    }
    window.addEventListener('toggle-pin-chat-sidebar', togglePin)
    return () => window.removeEventListener('toggle-pin-chat-sidebar', togglePin)
  }, [])

  const lastRole = messages[messages.length - 1]?.role ?? ''
  // Advances with every streamed chunk, so ChatFooter can tell "text is arriving"
  // apart from "the stream went quiet mid-turn" (the model generating a tool call,
  // or a tool group holding the trailing 'streaming' message open). 0 whenever no
  // streaming message is in flight.
  const streamTick = lastRole === 'streaming' ? (messages[messages.length - 1]?.content.length ?? 0) : 0
  // Transcript heat: advances on ANY transcript mutation (streamed chunk, tool
  // row, thinking burst), so useStreamIdle can tell a high-frequency burst from
  // a quiet running turn. Render-phase ref bump, guarded on the identity change
  // — the same pattern as a lazy initializer, so it is StrictMode-safe.
  const heatMessagesRef = useRef<ChatMessage[] | null>(null)
  const heatTickRef = useRef(0)
  if (heatMessagesRef.current !== messages) { heatMessagesRef.current = messages; heatTickRef.current++ }
  // Hot while the slot runs and mutations landed within the idle window. The
  // state update inside useStreamIdle commits AFTER the render that delivered a
  // mutation, so a row mounting on the first mutation after a quiet spell still
  // reads idle=true (hot=false) and keeps its entrance ease; only rows mounting
  // inside a burst (a second mutation within 700ms) snap. Passed down to
  // ToolCallLine to gate its height animations — see `transcriptHot` there.
  const transcriptIdle = useStreamIdle(heatTickRef.current, slotRunning)
  const transcriptHot = slotRunning && !transcriptIdle
  // Precompute: index of last finalized assistant message (tools after this are "trailing")
  // The activity panel has exactly two modes, and the question that picks one
  // is NOT "how wide is the window" — it is "how much width is left for the
  // chat". Subtract the shell's nav rail and the session sidebar (both of which
  // the user can hide) from the viewport: if what remains still seats the panel
  // at its minimum PLUS a usable chat pane, the panel sits BESIDE the chat.
  // Otherwise it FILLS the chat column, with the sidebar and rail untouched.
  //
  // Consequences worth stating:
  //  - Hiding the rail (162px) or the sidebar (~260px) can promote fill -> beside
  //    at a viewport width that could not seat both a moment earlier.
  //  - Mobile needs no special case: rail 0 + sidebar 0 (its drawer is fixed,
  //    not a flex sibling) always lands under the threshold. isMobile is still
  //    forced to fill so a 700px phone-class viewport cannot go beside.
  //  - The measurement is loop-free ON PURPOSE. It reads the rail TRACK and the
  //    sidebar's own state, never the chat container's painted width — that
  //    shrinks when the panel opens, which would oscillate beside <-> fill.
  const railWidth = useRailWidth()
  const [winW, setWinW] = useState(() => window.innerWidth)
  useEffect(() => {
    const onResize = () => setWinW(window.innerWidth)
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [])
  // Stored width is validated against SIDEBAR_MIN..SIDEBAR_MAX only, never the
  // window; clamp for render but leave the preference for the wide viewport.
  const effectiveSidebarWidth = clampSidebarWidth({ stored: sidebarWidth, winW, railW: railWidth })
  const workspaceFullscreen = useContext(WorkspaceFullscreenContext)?.fullscreen ?? false
  const reportWorkspaceSearch = useContext(WorkspacePanelContext)
  useEffect(() => {
    reportWorkspaceSearch(search.isOpen)
    return () => reportWorkspaceSearch(false)
  }, [search.isOpen, reportWorkspaceSearch])
  const { isOpen: workspaceSearchIsOpen, close: closeWorkspaceSearch } = search
  const toggleAct = useCallback(() => {
    if (workspaceSearchIsOpen) {
      closeWorkspaceSearch()
      dispatch(openActivityPanel())
      return
    }
    // Opening with no tabs shows the empty-state launcher grid (no seeded
    // default view) -- the user picks what to open.
    dispatch(toggleActivity())
  }, [dispatch, workspaceSearchIsOpen, closeWorkspaceSearch])
  // Shell-launched toggle: the panel-toggle shortcut (`onToggleSidePanel`,
  // shell/shortcuts/shellKeyboard.ts) dispatches this event so the panel-close
  // coordination above stays in ChatPage.
  useEffect(() => {
    const h = () => toggleAct()
    window.addEventListener('toggle-activity-panel', h)
    return () => window.removeEventListener('toggle-activity-panel', h)
  }, [toggleAct])
  // Bridge explicit view requests (e.g. the /side slash command dispatches
  // openActivityToTab('side')) into the tab model.
  const activityTab = useAppSelector(s => s.chat.activityTab)
  // Keyed on the REQUEST counter, never on the tab's value. `activityTab` also
  // changes when a chat switch restores the incoming chat's cached tab (Files
  // when it has none), and bridging that would force-focus Files — or whatever
  // view was last requested in that chat — over the tab the tab strip has
  // remembered and the user actually left the chat on. Only openActivityToTab
  // bumps the counter, so only a deliberate request moves focus.
  const activityTabRequest = useAppSelector(s => s.chat.activityTabRequest)
  // Skip the mount invocation: the counter is already non-zero after any earlier
  // request this page load, so firing on mount would re-open that view on top of
  // the now-persisted strip every time ChatPage remounts after a route change.
  const activityTabBridged = useRef(false)
  useEffect(() => {
    if (!activityTabBridged.current) { activityTabBridged.current = true; return }
    if (activityOpen) tabsCtl.openView(activityTab === ('nav' as string) ? 'files' : activityTab)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activityTabRequest])
  // Stable row callbacks. Inline lambdas in the row renderer would hand
  // AssistantMessage a fresh function identity every render, so its memo()
  // could never bail out — the boundary would break at the call site, not in the
  // renderer. Both read live state from a ref / the store rather than closing over
  // it, so neither needs a dependency that churns while a turn streams.
  const handleSpeak = useCallback((content: string) => {
    if (store.getState().chat.voicePlaying) {
      window.dispatchEvent(new Event('voice-stop'))
      dispatch(setVoiceAudio(null))
      return
    }
    dispatch(setVoiceAudio(null))
    // A synthesis still loading its model has no playing audio yet, but owns
    // the same output channel. Replace it before starting a manual request.
    window.dispatchEvent(new Event('voice-stop'))
    api.voiceSynthesize(activeSlotRef.current || '', content).catch(() => {})
  }, [dispatch])

  const handleApplyPlan = useCallback(async (steps: PlanStepInput[]) => {
    try {
      const r = await api.planFromChat(steps, planTaskId)
      if (r.ok) { navigate('/projects?applied=' + (r.task_id || planTaskId)); return true }
    } catch { /* API error */ }
    showActionError(i18nT('pages.chatPage.failed_to_apply_plan'))
    return false
  }, [planTaskId, navigate, showActionError])

  // The rows the virtualizer draws (see transcriptRows).
  const { renderedTranscript, renderedDisplayItems } = useTranscriptRows({
    messages, slotRunning, activeSlot, appToolCallIds, displayItemsRef, scrollerRef,
  })

  // Pinned prompt: keep the enablement ref in sync (updatePinnedPrompt is declared
  // above chatConfig and reads it through a ref), and recompute after the list
  // changes — a new turn shifts geometry with no scroll event of its own.
  useEffect(() => {
    pinEnabledRef.current = chatConfig.pinLastPrompt
    if (!chatConfig.pinLastPrompt) setPinned(null)
  }, [chatConfig.pinLastPrompt, pinEnabledRef, setPinned])
  useEffect(() => { updatePinnedPrompt() }, [renderedDisplayItems, updatePinnedPrompt])
  // Expanded state PERSISTS as the pinned prompt is replaced by the next one
  // while scrolling — the user asked for a sticky "keep it open" behaviour, so we
  // do NOT collapse on `pinned.idx` change. It still resets on slot switch below
  // (a different session should start collapsed).

  // Virtualized display — only mounts items in the viewport window. The
  // virtualizer shares `scrollerRef` with useScrollManager so the legacy
  // scroll APIs (scrollToDisplayIndex, scrollToBottom) operate on the
  // same DOM element. Its own follow-output handles streaming auto-pin
  // and append-pin, so the legacy useStreamingScroll/useFollowOutput
  // calls below are no-ops in this configuration but are kept invoked
  // for hook-call stability.
  const { stableMsgKey, stableAnchorId, anchorAltId, prefetchStartIndex, virtualKey } = useStableRowKeys({
    renderedDisplayItems, appToolCallIds, activeSlot,
  })

  // (Sticky widget detection removed — widgets now unmount with the
  // window like any other item. See useVirtualChat call below for the
  // memory-vs-flicker trade-off rationale.)

  // THE admission rule for every AUTOMATIC older fetch: the control that offers
  // history must be on the reader's screen. See earlierAffordanceInView for why
  // this replaces the per-trigger geometry/latch proxies, each of which had a
  // second cause that was not the reader.
  const earlierBarInView = useCallback(() => {
    const el = vScrollerElRef.current
    if (!el) return false
    const bar = el.querySelector(EARLIER_BAR_SELECTOR)
    const vr = el.getBoundingClientRect()
    const br = bar?.getBoundingClientRect()
    return earlierAffordanceInView(
      br ? { top: br.top, bottom: br.bottom } : null,
      { top: vr.top, bottom: vr.bottom },
    )
  }, [])

  // Reaching the top of a resumed transcript fetches the history behind the loaded slice.
  /**
   * Has the active session finished ARRIVING? Every automatic history fetch is
   * shut until it has.
   *
   * This gates the door itself rather than feeding shouldAutoFillOlder, because
   * an empty transcript satisfies that predicate's "too short to scroll" branch
   * on GEOMETRY alone -- the one path no gesture requirement can close, since
   * the branch returns before it ever reads `sawInput`. A switch installs an
   * empty list, restores cursor ownership on fulfilment, and leaves the earlier
   * bar sitting in view with nothing above it: three conditions that together
   * read as "the reader is at the top asking for history" while the reader has
   * done nothing at all.
   */
  const handleTopReached = useCallback(() => {
    const chat = store.getState().chat
    if (!shouldPaginateOlder({ loadingOlder: chat.loadingOlder, slotHasMore: chat.slotHasMore })) return
    // A bottom-FOLLOWED reader on a SCROLLABLE transcript did not ask for
    // this: the top sentinel only reaches them through boot/measurement
    // transients (estimate-priced rows keep total height under a viewport
    // for a beat; a spacer collapse pulls the top within reach), and each
    // self-issued landing re-fires the transition -- a page chain over a
    // parked reader, felt as 'it starts loading previous the moment I
    // refresh, then jumps'. When the transcript cannot scroll at all the
    // fill is load-bearing (a short resumed session has no scrollbar to
    // climb), so it stays; the moment it is scrollable, further history
    // is reader-initiated (climb releases follow, sentinel then serves).
    if (!earlierBarInView()) return
    const el = vScrollerElRef.current
    // The "too short to scroll" branch of shouldAutoFillOlder fires on GEOMETRY,
    // so it also fires on a geometry TRANSIENT — and the comment above names the
    // two that produce one. What it did not account for is that the composer's
    // text is ChatPage state, so every keystroke re-renders this tree and gives
    // the virtualizer another chance to be caught mid-measurement. Reported from
    // a phone as history loading while TYPING. Both the walk poll and the idle
    // prefetch already refuse to page on unsettled geometry; this door did not,
    // and it is the one the sentinel comes through.
    // An empty transcript is NOT "too short to scroll" -- it is "not loaded yet".
    // shouldAutoFillOlder cannot tell the two apart: both satisfy its geometry
    // branch, which returns before it ever reads `sawInput`, so no authorization
    // requirement can close that path. Separating the two states is what stops a
    // session ENTRY from reading as a reader parked at the top asking for history.
    // The measured-rows loop below cannot do it either: over zero rows it checks
    // nothing and falls straight through.
    if (displayItemsRef.current.length === 0) return
    const nRows = displayItemsRef.current.length
    for (let i = 0; i < nRows; i++) { if (!vFarmIsMeasuredRef.current?.(i)) return }
    // Neither of the two obvious signals can key this. A one-way input latch is
    // one-way latch, so on a scrollable transcript one touch leaves it open for
    // the rest of the mount — it cannot mean "this session". And `!follow` is the
    // design shouldAutoFillOlder's own contract names as falsified: follow is
    // released with no reader input at all, both by an anchor restore and at slot
    // entry, where `lastWriteTop` resets to -1 so the idle branch's self-check
    // cannot rescue it. Either one turns an entry geometry transient into a fetch
    // nobody asked for.
    // What survives both is the EXPIRING form of the real-gesture record, which
    // the automatic doors already apply: a timestamp cannot latch open, and it is
    // silent on a slot the reader has not touched.
    if (el && !shouldAutoFillOlder({
      scrollHeight: el.scrollHeight,
      clientHeight: el.clientHeight,
      sawInput: Date.now() - lastRealInputAtRef.current <= REAL_GESTURE_AUTH_MS,
    })) return
    // A gesture authorizes a BOUNDED run of pages, not the whole authorization
    // window. Authorization alone is what let one flick chain prepends until the
    // transcript ran out and left the reader at the very start of history.
    // The short-transcript fill is exempt because it bounds itself: every page
    // makes the transcript taller, so the geometry branch stops admitting once it
    // outgrows the viewport, and bounding it would strand a transcript that is
    // still too short to offer a scrollbar.
    if (el && el.scrollHeight > el.clientHeight + OLDER_FILL_SLACK_PX) {
      if (sentinelPagesSinceInputRef.current >= OLDER_WALK_MAX_PAGES_PER_INPUT) return
      sentinelPagesSinceInputRef.current += 1
    }
    if (inspectorOn()) devLog('OLDER', 'sentinel')
    void dispatch(loadOlderMessages())
  }, [dispatch, earlierBarInView, displayItemsRef])
  /**
   * The click path needs no gate beyond the in-flight check the thunk already makes.
   *
   * Also the remedy for affordances NOT adjacent to it: the unavailable fork/plan items
   * and the partial-scope search count both name "load earlier history" as the fix while
   * that control sits at the top of the transcript. Those callers page from where the
   * statement is read, and this deliberately does NOT scroll or move focus -- the reader
   * is mid-transcript at the message they mean to fork, or typing in the search field,
   * and satisfying the condition takes many pages, so relocating them on each one costs
   * more than the hunt it saves. Their in-flight cue is a spinner on the item instead.
   */
  const handleLoadEarlier = useCallback(() => {
    if (store.getState().chat.loadingOlder) return
    if (inspectorOn()) devLog('OLDER', 'manual-bar')
    void dispatch(loadOlderMessages())
  }, [dispatch])

  const virt = useVirtualChat<DisplayItem>({
    items: renderedDisplayItems,
    getKey: virtualKey,
    // Anchor resolution survives the per-landing key reshuffle by identifying
    // rows by their TAIL message — see stableAnchorIdFor.
    getStableId: stableAnchorId,
    getAltId: anchorAltId,
    prefetchStartIndex,
    sessionId: activeSlot ?? '__no_slot__',
    // Width-bucketed height scope: measured row heights are only valid for
    // the width they were measured at. Breakout tables use the full pane, so
    // the uncapped 16px buckets re-measure desktop resizes as well as phones.
    // Colon-delimited, because the persisted key is `vc_heights_<scope>` and
    // storageGc reads the owning session as the segment before the first ':'
    // (and deletes a session's keys at that delimiter): a scope joined with
    // any other separator is orphaned on every boot and missed on delete.
    heightScopeKey: `${activeSlot ?? '__no_slot__'}:tables1:w${scrollerWidthBucket}`,
    canMeasure,
    estimatedHeight: 100,
    // Overscan tradeoff (experimental):
    //   smaller (3)   → least memory, frequent widget remounts on small scrolls
    //   medium  (12)  → screenful of buffer, ~290MB baseline / 450MB while scrolling
    //   larger  (25)  → fewer remounts but inflated RAM from warm iframe pool
    // Currently testing 6 — middle ground between memory and remount frequency.
    overscan: 6,
    // A first measurement lands in the offset tree immediately instead of
    // waiting out the height-sync debounce. Without this, a fast scroll or a
    // FAR jump mounts a streak of rows whose real heights sit outside the
    // spacer math for up to the debounce window; when they reconcile, content
    // shifts under the viewport. Chrome's native scroll anchoring absorbs
    // that shift, iOS Safari has none — measured 13-25px of post-jump drift
    // with anchoring disabled (the "jump lands off by a bit" report). First
    // measurements happen once per row, so they cannot be the oscillation the
    // debounce exists to smother.
    eagerFirstMeasure: true,
    // No isSticky: widget messages unmount along with everything else
    // when they leave the viewport window. Trade-off: scrolling back to
    // an old widget causes its iframe to reload (1-2 frames of flicker).
    // Memory benefit: only widgets in the active window are kept alive,
    // ~290MB baseline instead of 500MB+ with all-widgets-sticky.
    externalScrollerRef: scrollerRef,
    // The currently-streaming message is always the LAST message and
    // therefore always ends up in the LAST displayItems entry — whether
    // that entry is itself the streaming `single`, or a `turn`/`group`
    // that the streaming message got folded into (turns only close when a
    // new user/nudge message opens the next one, by which point the prior
    // streaming message has already finished). Passing its index lets the
    // virtualizer track that one row's growth every RO tick instead of
    // debouncing it into a stale-then-jump spacer (see the `streamingIndex`
    // option's doc and useVirtualChat.spacerLurch.test.tsx).
    streamingIndex: isStreaming && renderedDisplayItems.length > 0 ? renderedDisplayItems.length - 1 : undefined,
    // `slotRunning`, not `isStreaming`: a turn spends much of its life in tool
    // calls with no streaming row named, and follow has to keep working there.
    runActive: !!slotRunning,
    onTopReached: handleTopReached,
  })

  // Single scroll controller wiring: expose the virtualizer's follow API to
  // the early effects/handlers (declared above) via refs, and derive the
  // at-bottom state for the jump-to-bottom pill. The virtualizer owns slot
  // entry, streaming follow, and append-pin; ChatPage only triggers explicit
  // jumps (send, jump-to-latest pill) through these.
  const isAtBottom = virt.isAtBottom
  // Mirror the virtualizer's follow API into the refs the early effects/handlers
  // (declared above) read. Done in a layout effect rather than the render body
  // so a concurrent render React throws away can't write stale callbacks into
  // the refs. Layout effects run before passive effects, so the gating effects
  // that call vGetFollowRef.current() still see this commit's callback.
  useLayoutEffect(() => {
    vGetFollowRef.current = virt.getFollow
    vScrollerElRef.current = virt.scrollerRef?.current ?? vScrollerElRef.current
    vScrollToBottomRef.current = virt.scrollToBottom
    vFarmIsMeasuredRef.current = virt.farmIsMeasured
    earlierBarInViewRef.current = earlierBarInView
    mountIndexRef.current = virt.mountIndex
    estimateRowTopRef.current = virt.estimateRowTop
  })

  // Legacy aliases so the JSX below keeps reading the same names.
  const visibleDisplayItems = virt.virtualItems
  // A window replacement can commit after the scroll frame that requested it.
  // Re-read geometry from the committed rows so an incomplete old window cannot
  // leave its banner at rest over a different part of the transcript.
  useLayoutEffect(() => { updatePinnedPrompt() }, [visibleDisplayItems, updatePinnedPrompt])



  // A reader parked within one viewport of the absolute top while older
  // history remains is a STANDING request for more. Every edge-triggered
  // fire in this path has a death mode (measured on a pod rig — runs stalled
  // after 3/5/9/10 pages, nondeterministically): the top sentinel only fires
  // on intersection TRANSITIONS and a small landing never pushes it back out
  // of view; the window-start crossing needs a >lead→≤lead transition that a
  // top-pinned recompute skips; and a landing-edge chain races the scroll
  // compensation it reads. So this is deliberately LEVEL-triggered: a slow
  // poll, gated to near-top + hasMore + idle + no error. It cannot stack
  // requests (loadingOlder gates), cannot spin on a dead link (slotOlderError
  // gates; the scroll-retry below owns that path), and stops the moment the
  // reader leaves the top or history is exhausted.
  useEffect(() => {
    if (!slotHasMore) return
    const el = virt.scrollerRef?.current
    // The reader's own INPUT, not scroll position: a landing's compensation
    // moves scrollTop thousands of px without the reader touching anything,
    // which would otherwise end the walk after every single page — on desktop
    // (no rubber-band to hold the top) that reduced "load to the beginning"
    // to one page per manual climb. While the last fetch this poll issued is
    // newer than the last wheel/touch, the reader is still waiting on the
    // walk it started: keep going. Any input hands control back to the
    // near-top gate.
    // Both of these live in REFS, not in this effect's scope. As locals they were
    // re-created with the effect -- whose deps include `slotHasMore`, a flag that
    // history loading itself moves -- and each re-creation handed out a fresh full
    // budget plus a `lastInput` of "now", i.e. an authorization nobody gestured
    // for. A budget that a re-render can reissue is not a budget.
    // The walk needs the reader to have ACTUALLY climbed: a phone refresh
    // parks at the bottom with zero wheel/touch input ever, and boot-phase
    // transients (pre-layout scrollTop near 0, the giant-turn grouped list
    // briefly shorter than a viewport) can slip one self-issued page past
    // the near-top gate. With lastInput frozen at mount, that single
    // landing made `walking` TRUE FOREVER and the poll walked the entire
    // multi-megabyte transcript over a parked reader -- the field report
    // 'it just keeps loading previous after a refresh'. Requiring one real
    // input event this session before any poll-issued fetch turns the walk
    // back into what its own comment promises: reader-initiated.
    const noteInput = () => {
      walkLastInputAtRef.current = Date.now()
      walkPagesSinceInputRef.current = 0
      lastRealInputAtRef.current = Date.now()
      sentinelPagesSinceInputRef.current = 0
    }
    el?.addEventListener('wheel', noteInput, { passive: true })
    el?.addEventListener('touchmove', noteInput, { passive: true })
    // A wheel and a touch are not the only ways a human scrolls. PgUp/Home/space
    // and a scrollbar-thumb drag reach the top just as deliberately, and gating on
    // wheel/touchmove alone silences automatic older history for exactly the
    // readers who use them -- keyboard navigation being the accessibility path.
    // Both events are still unforgeable by us, which is the whole point of the
    // window: writing `scrollTop` fires neither, so an automatic scroll cannot
    // authorize itself. `keydown` is bound to the SCROLLER, not the document, so
    // typing in the composer is not mistaken for an intent to read history.
    el?.addEventListener('keydown', noteInput, { passive: true })
    el?.addEventListener('pointerdown', noteInput, { passive: true })
    // The walk is "in progress" when the newest older-page LANDING postdates
    // the newest user input — regardless of which trigger fired the fetch.
    // (An earlier draft keyed this on the poll's own fires and never engaged:
    // the sentinel/crossing triggers always win the race for the first page,
    // and its landing throws the reader off the near-top gate before the next
    // tick, so the poll never got the first fire it required.)
    let lastLanding = 0
    let prevLoading = false
    // Any scroll event — momentum coasting included, which fires no
    // wheel/touchmove — marks the transcript as still MOVING. A landing's
    // prepend compensation writes scrollTop, and on iOS a programmatic write
    // mid-momentum fights the fling's own curve: the reader sees the view
    // snap. Pages land only when the scroller is fully settled.
    let lastScrollEvt = 0
    const noteScroll = () => {
      lastScrollEvt = Date.now()
      // Motion kills any in-flight page: landings commit only while still.
      abortActiveOlderFetch()
    }
    el?.addEventListener('scroll', noteScroll, { passive: true })
    const t = setInterval(() => {
      const el2 = virt.scrollerRef?.current
      if (!el2 || el2.clientHeight <= 0) return
      const chat = store.getState().chat
      if (prevLoading && !chat.loadingOlder) lastLanding = Date.now()
      prevLoading = chat.loadingOlder
      const walking = lastLanding > walkLastInputAtRef.current
      // Authorization is a WINDOW, the same one the sentinel door uses -- not the
      // "has this session ever seen input" latch that used to gate this poll. The
      // latch could only ever turn ON: one flick authorized the walk for the rest
      // of the mount, `lastInput` froze at that flick, and since every landing
      // postdates it `walking` was true forever after. The page counter was then
      // the only brake, and it was reissued whenever this effect re-created. With
      // an expiring window the door is simply SHUT while nobody is scrolling,
      // which is what "reader-initiated" has to mean.
      if (!shouldContinueOlderWalk({
        sawRealInput: Date.now() - lastRealInputAtRef.current <= REAL_GESTURE_AUTH_MS,
        nearTop: el2.scrollTop <= el2.clientHeight,
        walking,
        pagesSinceInput: walkPagesSinceInputRef.current,
      })) return
      // A bottom-followed reader is reading the LIVE end: the top-of-
      // transcript walk has nothing for them, and its landings are pure
      // disturbance budget. The sentinel/crossing triggers (reader-scroll
      // driven) still page history the moment they actually climb.
      if (vGetFollowRef.current()) return
      if (!earlierBarInViewRef.current()) return
      if (Date.now() - lastScrollEvt < OLDER_LANDING_SETTLE_MS) return
      // ...and stops entirely once they are no longer climbing. The settle gate
      // above only says "not mid-gesture", so on its own it made a reader who
      // STOPPED near the top the ideal candidate: they would sit still and watch
      // several pages land under them, one prepend each, which is felt as the
      // transcript starting to move on its own a second after they let go.
      // History still reaches back as far as they like -- it loads while they
      // climb, which is when they are asking for it.
      if (Date.now() - lastScrollEvt > OLDER_WALK_ACTIVE_MS) return
      if (!chat.slotHasMore || chat.loadingOlder || chat.slotOlderError) return
      if (chat.slotCursorKey !== chat.activeSlot) return
      // Same contract as the idle prefetch: a page lands only on FULLY
      // MEASURED geometry. Without this the walk outran the farm and piled
      // unmeasured rows over the parked reader -- every measurement landing
      // was an estimate correction under their eyes (reproduced on the rig
      // as per-landing twitches at the top). Turn-grouped pages measure in
      // ~1-2s, so the walk's pace barely changes.
      const nRows = displayItemsRef.current.length
      for (let i = 0; i < nRows; i++) { if (!virt.farmIsMeasured(i)) return }
      walkPagesSinceInputRef.current += 1
      if (inspectorOn()) devLog('OLDER', `walk p${walkPagesSinceInputRef.current}`)
      void dispatch(loadOlderMessages())
    }, OLDER_TOP_POLL_MS)
    return () => {
      clearInterval(t)
      el?.removeEventListener('scroll', noteScroll)
      el?.removeEventListener('wheel', noteInput)
      el?.removeEventListener('touchmove', noteInput)
      el?.removeEventListener('keydown', noteInput)
      el?.removeEventListener('pointerdown', noteInput)
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps -- listeners re-arm on these triggers only; handlers read refs
  }, [slotHasMore, dispatch, virt.scrollerRef])

  // The sticky in-flight spinner is only meaningful where pages LAND — at the
  // top of the loaded transcript. `loadingOlder` is now true for the whole
  // automatic walk (a dozen pages back-to-back), so gating the spinner on the
  // fetch alone kept it pinned over the reader even mid-transcript. Track
  // "near the top" cheaply: the setState is value-stable away from the
  // threshold, so mid-scroll updates bail before rendering.
  const [spinnerNearTop, setSpinnerNearTop] = useState(true)
  useEffect(() => {
    const el = virt.scrollerRef?.current
    if (!el) return
    let raf = 0
    const onScroll = () => {
      // Cancel-and-reschedule, never latch-on-pending (frameSchedulerLatch
      // guard): a dropped frame handle must not wedge the near-top signal.
      if (raf) cancelAnimationFrame(raf)
      raf = requestAnimationFrame(() => {
        raf = 0
        setSpinnerNearTop(el.scrollTop < el.clientHeight * 1.5)
      })
    }
    onScroll()
    el.addEventListener('scroll', onScroll, { passive: true })
    return () => { el.removeEventListener('scroll', onScroll); if (raf) cancelAnimationFrame(raf) }
  }, [virt.scrollerRef, activeSlot])

  // A failed older-page fetch PARKS pagination: the top sentinel is already
  // inside the viewport, so no new crossing ever fires and automatic paging
  // never resumes — the only way forward is the retry bar, which on a phone
  // is easy to miss, so the transcript reads as "history ends here"
  // (reproduced: fail page 3 of 7 once, scrolling never fetches again).
  // Treat a FURTHER upward scroll as retry intent — the reader is still
  // asking for older content. Cooldown-gated so a dead link costs one
  // request per gesture, not one per scroll event; the thunk's own
  // loadingOlder gate covers the in-flight window.
  const olderRetryAtRef = useRef(0)
  useEffect(() => {
    const el = virt.scrollerRef?.current
    if (!el) return
    let prevTop = el.scrollTop
    const onScroll = () => {
      const st = el.scrollTop
      const up = st < prevTop
      prevTop = st
      if (!up) return
      const chat = store.getState().chat
      if (!chat.slotOlderError || chat.loadingOlder || !chat.slotHasMore) return
      const now = Date.now()
      if (now - olderRetryAtRef.current < OLDER_RETRY_COOLDOWN_MS) return
      olderRetryAtRef.current = now
      if (inspectorOn()) devLog('OLDER', 'error-retry')
      void dispatch(loadOlderMessages())
    }
    el.addEventListener('scroll', onScroll, { passive: true })
    return () => el.removeEventListener('scroll', onScroll)
  }, [dispatch, activeSlot, virt.scrollerRef])
  // No "load more" pagination indicator with virtualization — the
  // windowing engine swaps mounted/placeholder automatically.

  // Reset scroll-navigation state on slot switch.
  useEffect(() => {
    setPinned(null)
    setPinExpanded(false)
  }, [activeSlot, setPinExpanded, setPinned])

  // A busy turn: steer into it, answer its question card, stop it, and the queued-message cards.
  const {
    queuedMessages, systemDeliveryCount, steer, stopTurn, keepQuestionAnswer, answerQuestionCard,
    handleCancelQueued, handleInterruptQueued, handleEditQueued, handleReorderQueued, queuePendingIds,
  } = useBusyTurnControls({
    activeSlot,
    currentSlot,
    connected,
    activeSlotRef,
    slotRunning,
    messages,
    send,
    steerMutation,
    composerRef,
    composerSlotRef,
    inputRef,
    setInput,
    stores: draftStores,
    staging,
    pendingQuestion,
    softStopAtMapRef,
    dispatch,
  })


  // Search, pins, tool focus, and deep links navigate the rows the virtualizer
  // actually renders, including post-defer MCP App coalescing.
  const messageToDisplayIdx = useMemo(
    () => buildMessageToDisplayIdx(renderedDisplayItems),
    [renderedDisplayItems],
  )

  const navigateToTurn = useCallback((displayIndex: number, opts?: { instant?: boolean }) => {
    // instant: used by the minimap's drag-scrub — a smooth glide would lag the
    // pointer and queue easings on every marker crossing.
    navToDisplayIndex(displayIndex, { behavior: opts?.instant ? 'auto' : 'smooth', align: 'start', offset: -24 })
  }, [navToDisplayIndex])

  // The transcript renders the deferred `renderedTranscript` snapshot; while a
  // history page lands, live indexes lead the rows on screen. The minimap's
  // messages and `messageToDisplayIdx` map come from that same rendered frame,
  // so a marker's display index always names the virtualizer row on screen.
  // One per-turn derivation: `sections` feeds the turn minimap; `links` feeds the
  // Navigation tab. Both read the deferred snapshot, so the link list trails a
  // landing history page by one deferred commit -- deliberate, and harmless for
  // a side panel.
  const chatNav = useChatNavigation(renderedTranscript.messages, messageToDisplayIdx)

  // ── Chat Pins ──────────────────────────────────────────────────────────────
  const {
    pins: chatPins,
    loading: chatPinsLoading,
    error: chatPinsError,
    clearError: clearChatPinsError,
    isPinned,
    pinMessage,
    unpinMessage,
    unpinById,
  } = useChatPins(activeSlot ?? undefined)
  const [pinNotice, setPinNotice] = useState<string | null>(null)
  // A pinned-message page load that was REFUSED — kept apart from `pinNotice`
  // (which carries the not-found / unavailable answers) so it renders as an
  // error rather than as status text.
  const [pinLoadError, setPinLoadError] = useState<string | null>(null)
  const [pendingPinnedJump, setPendingPinnedJump] = useState<{
    slotKey: string
    messageTs: string
    mid?: string
    // Required, not optional: the entry points render different copy, and a new
    // caller that omitted it would silently show pin wording.
    origin: PendingJumpOrigin
  } | null>(null)
  const pinnedJumpPageLoadsRef = useRef(0)
  const jumpToLoadedPinnedMessage = useCallback((messageTs: string, mid?: string): boolean => {
    // Mid-based resolution when a mid is known; ts ONLY for legacy pins that carry none.
    // Falling through to ts with a mid in hand takes a same-tick twin, which is the wrong row.
    const msgIdx = mid
      ? messages.findIndex(m => (m.meta as Record<string, unknown> | undefined)?.mid === mid)
      : messages.findIndex(m => m.ts === messageTs)
    if (msgIdx < 0) return false
    const di = messageToDisplayIdxRef.current.get(msgIdx)
    if (di === undefined) return false
    setPinNotice(null)
    setPinLoadError(null)
    navToDisplayIndex(di, { behavior: 'smooth', align: 'center' })
    setHighlightTs(messageTs)
    setTimeout(() => setHighlightTs(null), 3000)
    return true
  }, [messages, navToDisplayIndex, setHighlightTs])
  // ⌘↑ / Ctrl+Up target resolution, placed next to the virtualizer seam it
  // depends on: the transcript is virtualized, so the newest user message can
  // sit OUTSIDE the mounted window (a long agent reply just streamed past it)
  // and no `UserMessage` row would exist to see the rising edge. Scroll the
  // target into view FIRST and raise the request in the same pass — the row
  // then receives the request at mount and acknowledges it on consumption.
  const handleEditLastRequest = useCallback(() => {
    if (!activeSlot || slotRunning || regenerating || activeSlotRemoteBound) return
    const msgs = messagesRef.current
    for (let i = msgs.length - 1; i >= 0; i--) {
      if (msgs[i].role !== 'user') continue
      const di = messageToDisplayIdxRef.current.get(i)
      if (di === undefined) return
      navToDisplayIndex(di, { behavior: 'auto', align: 'center' })
      setEditLast({ seq: ++editSeqRef.current, ts: msgs[i].ts || '', index: i })
      return
    }
  }, [activeSlot, slotRunning, regenerating, activeSlotRemoteBound, navToDisplayIndex])
  const handleJumpToPinnedMessage = useCallback((messageTs: string, mid: string | undefined, { origin }: { origin: PendingJumpOrigin }) => {
    if (jumpToLoadedPinnedMessage(messageTs, mid)) return
    if (activeSlot && (!cursorIsForActiveSlot || (slotHasMore && slotOldestIndex > 0))) {
      pinnedJumpPageLoadsRef.current = 0
      setPinNotice(null)
      setPinLoadError(null)
      setPendingPinnedJump({ slotKey: activeSlot, messageTs, mid, origin })
      return
    }
    // Same writer as the async branch below, so the synchronous dead-link case
    // cannot drift into pin wording while the paging case reports the truth.
    setPinNotice(jumpUnavailableNotice(origin))
  }, [activeSlot, cursorIsForActiveSlot, jumpToLoadedPinnedMessage, slotHasMore, slotOldestIndex])
  // The pins list's own entry point, so pin copy is claimed HERE by a caller that
  // means it rather than inherited by one that passed nothing.
  const handleJumpToPin = useCallback((messageTs: string, mid?: string) => {
    handleJumpToPinnedMessage(messageTs, mid, { origin: 'pin' })
  }, [handleJumpToPinnedMessage])
  useEffect(() => {
    if (!pendingPinnedJump) return
    if (pendingPinnedJump.slotKey !== activeSlot) {
      pinnedJumpPageLoadsRef.current = 0
      setPendingPinnedJump(null)
      return
    }
    // Captured per effect run so the async branches below report the entry point
    // this jump came from, not whichever one ran last.
    const notFoundNotice = jumpUnavailableNotice(pendingPinnedJump.origin)
    // A fetch that errored is transient, so the not-found copy would tell the reader
    // their history is gone. `link` shares the retry copy: it makes no origin claim.
    const loadFailedNotice = pendingPinnedJump.origin === 'earlier' || pendingPinnedJump.origin === 'link'
      ? i18nT('components.chatPane.earlier_messages_load_failed')
      : notFoundNotice
    if (jumpToLoadedPinnedMessage(pendingPinnedJump.messageTs, pendingPinnedJump.mid)) {
      pinnedJumpPageLoadsRef.current = 0
      // A jump resolved against the bounded page is provisional: the full
      // transcript prepends older rows, so re-resolve once it has replaced it.
      if (!activeViewIsBoundedPage) setPendingPinnedJump(null)
      return
    }
    // The cursor still describes the chat we left; wait for the switch to settle
    // rather than read its has-more as this chat's.
    if (!cursorIsForActiveSlot) return
    if (!slotHasMore || slotOldestIndex <= 0) {
      pinnedJumpPageLoadsRef.current = 0
      setPinNotice(notFoundNotice)
      setPendingPinnedJump(null)
      return
    }
    if (loadingOlder) return

    // Counted for diagnostics only, deliberately NOT compared against a ceiling:
    // an arbitrary page cap is what made a distant pin in a resumed session report
    // itself "unavailable" when it simply needed more pages, and it was removed for
    // that reason (chatPins.test.ts, 'no arbitrary page-load cap'). The loop is
    // bounded by the history itself -- the target is found, `slotHasMore` goes
    // false, or `slotOldestIndex` reaches 0 -- and it is walking history the reader
    // ASKED for by tapping the pin, which is not the unasked loading this branch is
    // about. The real fix for a very distant pin is a server query that fetches
    // AROUND a message id; until that exists, paging is the honest behaviour.
    pinnedJumpPageLoadsRef.current += 1
    if (inspectorOn()) devLog('OLDER', `jump p${pinnedJumpPageLoadsRef.current}`)
    let cancelled = false
    void dispatch(loadOlderMessages()).unwrap().then(result => {
      if (!cancelled && result === null) {
        pinnedJumpPageLoadsRef.current = 0
        setPinNotice(notFoundNotice)
        setPendingPinnedJump(null)
      }
    }).catch(err => {
      // Cancelled or refused means the user switched chat, not that the pin is
      // unreachable.
      if (isSupersededPagingRejection(err)) return
      if (!cancelled) {
        pinnedJumpPageLoadsRef.current = 0
        setPinLoadError(loadFailedNotice)
        setPendingPinnedJump(null)
      }
    })
    return () => { cancelled = true }
  }, [
    activeSlot,
    activeViewIsBoundedPage,
    cursorIsForActiveSlot,
    dispatch,
    jumpToLoadedPinnedMessage,
    loadingOlder,
    pendingPinnedJump,
    slotHasMore,
    slotOldestIndex,
  ])
  const handleTogglePinForMessage = useCallback((mid: string, messageTs: string, role: 'user' | 'assistant', content: string) => {
    if (isPinned(mid)) {
      void unpinMessage(mid).catch(() => {}) // useChatPins exposes the localized error state.
      return
    }
    // A session's FIRST pin opens the Pins tab, so the pin has a visible
    // destination -- the same shape as the Issues reveal, and for the same
    // reason: Pins is an on-demand view, so nothing would surface it otherwise.
    // A session pinned earlier reaches it through the + menu (Issues' zero
    // option for pre-existing links), which is what keeps this free of a
    // persisted reveal claim.
    // Read before the mutation so the optimistic insert has not landed yet.
    const isFirstPin = chatPins.length === 0
    void pinMessage({ mid, message_ts: messageTs, role, preview: content }).catch(() => {})
    if (isFirstPin && activeSlot) {
      // Addressed by slot, not through tabsCtl, for the same reason as the
      // source-reveal path: that binding can be a chat being left.
      openPanelView(activeSlot, 'pins')
      // Pinning is NOT a navigation request, so it must not cost the user state
      // they are mid-way through. Unlike the source-reveal path this does not
      // close the find pane: someone who searched the transcript to FIND the
      // message they are pinning would lose the pane and its results on the very
      // click that acts on a result. Below the mobile breakpoint the panel opens
      // full width, so opening it would navigate them off the chat entirely.
      // The tab is still created above -- it is revealed quietly instead.
      if (!search.isOpen && !isMobile) dispatch(openActivityPanel())
    }
  }, [activeSlot, chatPins.length, dispatch, isMobile, isPinned, pinMessage, search.isOpen, unpinMessage])
  const handleUnpinById = useCallback((id: string) => {
    void unpinById(id).catch(() => {})
  }, [unpinById])
  // Split by kind. `pinError` is something that FAILED — a refused page load,
  // a pin/unpin request the server rejected (useChatPins keeps that state, so
  // the `.catch(() => {})` at the call sites is not a swallow). `pinStatus` is
  // an answer — the message is not in this history, the pin limit is reached.
  const pinError = pinLoadError ?? (chatPinsError && chatPinsError !== 'pin_limit'
    ? i18nT(chatPinsError === 'pin' ? 'pages.chat.pins.pin_failed' : 'pages.chat.pins.unpin_failed')
    : null)
  const pinStatus = pinNotice ?? (chatPinsError === 'pin_limit' ? i18nT('pages.chat.pins.pin_limit_reached') : null)
  const dismissPinStatus = useCallback(() => {
    setPinNotice(null)
    setPinLoadError(null)
    clearChatPinsError()
  }, [clearChatPinsError])
  // Only the answer times out. A failure stays until the user dismisses it or
  // hands it to the agent — an error that vanishes after eight seconds is the
  // toast shape this sweep is removing.
  useEffect(() => {
    if (!pinStatus) return
    const timeout = window.setTimeout(() => {
      setPinNotice(null)
      if (chatPinsError === 'pin_limit') clearChatPinsError()
    }, 8000)
    return () => window.clearTimeout(timeout)
  }, [pinStatus, chatPinsError, clearChatPinsError])

  // Read the display-index map via a ref so the search scroll effect
  // (useTranscriptJumps, below) does NOT
  // re-fire when the map is rebuilt (every new message / stream chunk rebuilds
  // it). Otherwise an open search pane would yank the chat back to the current
  // match each time the agent emits output. The effect should scroll only on
  // deliberate search navigation (currentIdx / currentMessageIdx change).
  const messageToDisplayIdxRef = useRef(messageToDisplayIdx)
  messageToDisplayIdxRef.current = messageToDisplayIdx
  // Search steps and clicks, the approval bar's "Show in chat", and ?msg= deep links.
  const { jumpToSearchResult } = useTranscriptJumps({
    search,
    messages,
    messageToDisplayIdx,
    messageToDisplayIdxRef,
    navToDisplayIndex,
    activeSlot,
    cursorIsForActiveSlot,
    slotHasMore,
    slotOldestIndex,
    initialMsgRef,
    initialMidRef,
    initialSidRef,
    setHighlightTs,
    handleJumpToPinnedMessage,
  })

  // Precomputed O(n) map from message index → visible (user/assistant) index,
  // used by the fork button. Avoids a per-row O(i) filter that would make the
  // renderer O(n²) overall.
  const visibleIndexMap = useMemo(() => {
    const map = new Map<number, number>()
    let count = 0
    for (let idx = 0; idx < messages.length; idx++) {
      const r = messages[idx].role
      if (r === 'user' || r === 'assistant') {
        map.set(idx, count)
        count++
      }
    }
    return map
  }, [messages])

  const activeSlotTitle = filteredSlots.find(s => s.key === activeSlot)?.title

  // Session documents (in-session artifacts) for the active slot. Used only to
  // badge file-change rows that are tracked docs/artifacts (e.g. a generated
  // PR body) rather than source-file edits. Shares the ['session-artifacts',
  // slot] query key with the Artifacts tab so it's a single deduped fetch; the
  // memoized Set keeps AssistantMessage's memo stable across renders.
  const { data: sessionDocs } = useQuery({
    queryKey: ['session-artifacts', activeSlot],
    queryFn: () => api.artifactSessionDocs(activeSlot || undefined),
    enabled: !!activeSlot,
    staleTime: 15_000,
  })
  const artifactPaths = useMemo(
    () => new Set((sessionDocs?.docs || []).map(d => d.path)),
    [sessionDocs],
  )

  // Flush-volatile positional state is read through refs so a streaming flush
  // (which replaces `messages` and rebuilds the derived index/tail values)
  // does not mint a new renderMessage -> renderTurnItem identity and defeat
  // memo(TurnBlock) for every settled turn. The refs are synced per render, so
  // a callback invoked during THIS render's children sees current values.
  // UI-state deps (chatConfig, linkPreviewsOn, disclosure, pin state, ...)
  // deliberately STAY in the dep array: when they change, settled turns must
  // re-render with the new behavior, and the changed identity is what breaks
  // through the memo.
  const visibleIndexMapRef = useRef(visibleIndexMap); visibleIndexMapRef.current = visibleIndexMap
  const lastTextIdxRef = useRef(lastTextIdx); lastTextIdxRef.current = lastTextIdx
  // The first reply in this session with a removed credential: the one-time
  // redaction coach renders after it and nowhere else.
  const redactionCoachTs = useMemo(() => {
    for (const m of messages) {
      const r = (m.meta as Record<string, unknown> | undefined)?.redactions
      if (m.role === 'assistant' && Array.isArray(r) && r.length > 0) return m.ts ?? null
    }
    return null
  }, [messages])
  const slotStateRef2 = useRef(slotState); slotStateRef2.current = slotState

  // ── Registry-driven row dispatch (chat-core P5-a) ──
  // Every transcript row on this page resolves through the SAME renderer
  // registry the other surfaces consume (app-sdk/messageRenderers), so a role
  // registered once renders everywhere -- the double-wiring defect class
  // (`mcp_oauth` shipped wired in app-sdk and raw in the main chat) is closed
  // structurally rather than by the parity test alone. ChatPage's chrome (tool
  // disclosure state, fork/pin/footer, the error card's Continue, the nudge
  // card's Loop button, ...) rides as HOST ENTRIES that reuse the default ids
  // they replace, plus a few page-only shape entries. Order inside this array
  // is the page's precedence order, unchanged from the if-chain it replaces:
  // the shared dashboard set (sub-agent completion, launch cards, tool,
  // thinking, file, nudge, recovery inject, workflow completion, error), then
  // permission, undrawn, hidden invisible assistant, and the conversational
  // bubble. Roles none of these claim fall to the registry defaults (`undrawn`
  // for queued/system/done and the reasoning roles; `tool_lifecycle` for raw
  // wire shapes the store normalizes away; the stop-event card, the notice
  // card and the MCP OAuth banner -- P5-c deleted the page's copies of those
  // three, which drew the same component from the same inputs), and a role
  // NOBODY claims renders as the bubble, which is what the if-chain's
  // fall-through did.
  //
  // Memoized with the deps the old renderMessage carried: UI-state deps
  // (chatConfig, linkPreviewsOn, disclosure, pin state, ...) deliberately STAY
  // in the array so settled turns re-render with the new behavior, and the
  // changed identity is what breaks through memo(TurnBlock).
  const { renderers: chatPageRenderers, fallback: bubbleRenderer } = useMemo<{ renderers: readonly MessageRenderer[]; fallback: MessageRenderer }>(() => {
    /** The conversational row: user / inject (cron & recovery prose) / assistant. */
    const bubble: MessageRenderer = {
      id: 'bubble',
      roles: ['user', 'assistant', 'streaming', 'inject'],
      render: (m, ctx) => {
        const i = ctx.index
        const key = ctx.key
    const isUser = m.role === 'user'
    const isStreaming = m.role === 'streaming'
    const isInject = m.role === 'inject'
    // Pass a stable handleFork (useCallback) + primitive index so memo()
    // on AssistantMessage can short-circuit when only unrelated state changes.
    // visibleIndexMap is O(1) per row.
    const messageId = typeof m.meta?.mid === 'string' && m.meta.mid ? m.meta.mid : undefined
    const canResolveOnServer = !!messageId && !isStreaming && !isInject
    const canFork = canResolveOnServer || canForkAtWindow({ isStreaming, isInject, slotHasMore, cursorIsForActiveSlot })
    const forkIndex = canFork ? visibleIndexMapRef.current.get(i) : undefined
    const msgTime = fmtMessageTime(m.ts)
    const msgTimeFull = fmtMessageTimeFull(m.ts)
    return (
      <MessageSearchScope key={key} messageIdx={i}>
      <div className={`group flex flex-col min-w-0 ${isUser ? 'items-end' : ''} ${m.ts && m.ts === highlightTs ? 'animate-msg-highlight rounded-lg' : ''}`}>
        <div className={`chat-message-body flex flex-col gap-0.5 min-w-0 overflow-hidden max-w-full ${isUser ? 'items-end' : ''}`}>
          {isUser ? (
            <UserMessage
              content={m.content}
              meta={m.meta}
              timestamp={chatConfig.showTimestamps ? msgTime : undefined}
              timestampTitle={msgTimeFull}
              renderContent={renderUserContentCb}
              canEdit={!slotRunning && !regenerating && !!activeSlot && !activeSlotRemoteBound}
              slotRunning={slotRunning}
              messageIndex={i}
              messageTs={m.ts || ''}
              onEditResend={handleEditResend}
              doubleClickToEdit={chatConfig.doubleClickToEdit}
              editRequest={!farmPassRef.current && editLast && editLast.index === i && editLast.ts === (m.ts || '') ? editLast.seq : undefined}
              onEditConsumed={farmPassRef.current ? undefined : handleEditConsumed}
              slotKey={activeSlot || undefined}
              slotTitle={activeSlotTitle}
              mode={mode}
              pinned={m.ts && (m.meta as Record<string, unknown> | undefined)?.mid ? isPinned((m.meta as Record<string, unknown>).mid as string) : false}
              onTogglePin={m.ts && (m.meta as Record<string, unknown> | undefined)?.mid ? () => handleTogglePinForMessage((m.meta as Record<string, unknown>).mid as string, m.ts!, 'user', m.content) : undefined}
            />
          ) : isInject ? (
            (() => {
              const cronLabel = (m.meta?.cronLabel as string) || ''
              const appLabel = (m.meta?.appLabel as string) || ''
              // Strip wrapper tags — LLM needs them for context but user sees clean content
              const stripped = cronLabel
                ? m.content.replace(/^\[Cron notification from ".*"\]\n/, '').replace(/\n\[End of cron notification\]$/, '')
                : appLabel
                  ? stripAppEnvelope(m.content)
                  : m.content
              // A note's marker is consumed into the pill row, so rendering it too would show
              // the same choices twice. Non-note inject rows keep it: there it is prose.
              const cleanContent = isNoteRow(m) ? parseOptions(stripped).text : stripped
              return <>
                {cronLabel && <span className="text-muted text-[11px] leading-4 font-medium px-1 mb-1"><Clock className="lucide-inline" /> {cronLabel}</span>}
                {!cronLabel && appLabel && <span className="text-muted text-[11px] leading-4 font-medium px-1 mb-1 cursor-help" title={i18nT('components.mcpApp.from_app_tooltip')}><AppWindow className="lucide-inline" /> {i18nT('components.mcpApp.from_app', { app: appLabel.split('/')[0] })}</span>}
                {/* Same session wiring as the assistant branch. Without it `resolveSessionChip`
                    refuses at its first guard and a `/chat?sid=` link gains `target="_blank"`. */}
                <div className="mc-message-font-scope msg-content px-4 py-3 leading-relaxed rounded-lg bg-warn-subtle text-text ring-1 ring-inset forced-colors:border ring-warn/30 rounded-bl-[4px] overflow-hidden min-w-0" style={{ overflowWrap: 'anywhere', wordBreak: 'break-word', fontSize: 'var(--mc-message-font-size, 14px)' }}><MessageErrorBoundary rawContent={cleanContent}><MarkdownRenderer content={cleanContent} onSessionOpen={selectSessionTab} sessions={connected ? sessionTitles : undefined} activeSession={activeSlot || undefined} messageTs={m.ts} softBreaks /></MessageErrorBoundary></div>
                {/* No `font-mono`: a formatted date is prose, and Tailwind's
                    `font-mono` pins `var(--mono)` — a token the Font Family
                    setting never writes, so it overrode the user's choice and
                    put JetBrains Mono (no CJK coverage) under a date that a
                    zh/ja dashboard renders WITH CJK characters. `tabular-nums`
                    keeps the digits fixed-width, which is the alignment the
                    mono was actually there for. */}
                {chatConfig.showTimestamps && msgTime && <span className="text-muted text-[12px] leading-4 tabular-nums px-1" title={msgTimeFull}>{msgTime}</span>}
              </>
            })()
          ) : (
            <div className="flex flex-col gap-0">
              <AssistantMessage revealActions={!!activeSlot && voiceRecoverySlot === activeSlot} suppressSteerAck={turnHadPolicyBlock(messagesRef.current, i)} prevUserText={prevUserTextFor(messagesRef.current, i)} shareEnabled={socialShareOn} linkPreviews={linkPreviewsOn} content={m.content} isStreaming={isStreaming} isRegenerating={regenerating && i === lastTextIdxRef.current} onFileOpen={handleFileOpen} onFolderOpen={handleFolderOpen} onArtifactOpen={handleArtifactOpen} onSessionOpen={selectSessionTab} sessions={connected ? sessionTitles : undefined} activeSession={activeSlot || undefined} onQuote={handleQuote} onAsk={handleAsk} slotRunning={slotRunning} planTaskId={planTaskId} timestamp={chatConfig.showTimestamps ? msgTime : undefined} timestampTitle={msgTimeFull} messageTs={m.ts} slotKey={activeSlot || undefined} slotTitle={activeSlotTitle} mode={mode} fileChanges={(m.meta as Record<string, unknown> | undefined)?.file_changes as FileChangeEntry[] | undefined} fileChangesOmittedFiles={(m.meta as Record<string, unknown> | undefined)?.file_changes_omitted_files} blockedLinks={(m.meta as Record<string, unknown> | undefined)?.blocked_links} redactions={(m.meta as Record<string, unknown> | undefined)?.redactions} showRedactionCoach={!!m.ts && m.ts === redactionCoachTs} turnStats={chatConfig.showTurnStats ? (m.meta as Record<string, unknown> | undefined)?.turn_stats as TurnStats | undefined : undefined} decisionsStrip={decisionStripFieldOf(m)} onOpenDiff={handleOpenDiff} fileChipStyle={chatConfig.fileChipStyle} artifactPaths={artifactPaths} pinned={m.ts && (m.meta as Record<string, unknown> | undefined)?.mid ? isPinned((m.meta as Record<string, unknown>).mid as string) : false} onTogglePin={m.ts && (m.meta as Record<string, unknown> | undefined)?.mid ? () => handleTogglePinForMessage((m.meta as Record<string, unknown>).mid as string, m.ts!, 'assistant', m.content) : undefined} showFooter={(() => {
                // Show footer on the last assistant message of each completed turn
                if (isStreaming) return false
                // Find next message after this one that's assistant, user, or streaming
                for (let j = i + 1; j < messagesRef.current.length; j++) {
                  const later = messagesRef.current[j]
                  if (later.role === 'user') return true // end of turn — show footer
                  // A hidden invisible-only row draws nothing, so it cannot
                  // host the footer; pass over it to the row that renders.
                  if (isHiddenInvisibleAssistantRow(later)) continue
                  // A system-notice row (compaction / session reload) draws a
                  // system card, not a reply, so it cannot end the turn either.
                  if (isSystemNoticeRow(later)) continue
                  if (later.role === 'assistant' || later.role === 'streaming') return false // not last assistant in turn
                }
                // End of messages. A run still in progress has not produced this
                // turn's footer yet — but a message that already CARRIES turn
                // stats is a turn that finished, and a LATER run (a cron, a
                // monitor cycle, another tab, a background job) must not retract
                // the footer of a turn it has nothing to do with.
                //
                // Retracting it removed the stats line, the timestamp row and the
                // overflow trigger — measured frame by frame from a phone
                // recording: ~108px, gone for 3 frames at 60fps, at the very
                // bottom edge of the transcript. Content shrinking there makes
                // the engine clamp a bottom-parked reader down, and nothing ever
                // pushes them back up when the footer returns, so each flicker
                // cost the reader their position permanently.
                const stats = (m.meta as Record<string, unknown> | undefined)?.turn_stats as TurnStats | undefined
                if (stats && (stats.elapsed_ms ?? 0) > 0) return true
                return !slotRunning
              })()} onSpeak={handleSpeak} onRegenerate={i === lastTextIdxRef.current && !slotRunning && !regenerating && activeSlot && !activeSlotRemoteBound ? handleRegenerate : undefined} variants={m.variants} variantIdx={m.variant_idx} onSwitchVariant={i === lastTextIdxRef.current && m.variants && m.variants.length > 1 && activeSlot ? (idx: number) => { api.switchVariant(activeSlot, idx).catch((e: unknown) => {
                showRefusedPress('switch_variant', e)
              }) } : undefined} onFork={embedded && !popout ? undefined : handleFork} forkIndex={forkIndex} forkMessageId={canResolveOnServer ? messageId : undefined} onLoadEarlier={cursorIsForActiveSlot ? handleLoadEarlier : undefined} loadingOlder={loadingOlder} earlierRemaining={slotOldestIndex} onApplyPlan={handleApplyPlan} />
            </div>
          )}
        </div>
      </div>
      </MessageSearchScope>
    )
      },
    }
    // The dashboard's shared row set (pages/chat/transcriptRenderers.tsx --
    // tool lines and launch cards, thinking block, nudge, recovery inject, the
    // two completion cards, the error card with Continue), wired with this
    // page's behaviours through its options; ChatPane calls the same factory
    // with fewer. Only rows that are genuinely page-specific follow it. The
    // stop-event card, the notice card and the MCP OAuth banner are NOT among
    // them: the SDK defaults draw each from the same component and the same
    // inputs (`ctx.hideCardOwnedOAuth` is this page's `connectionsUiOn`), and
    // this page's `ctx.row` is a keyed passthrough, so the page reads those
    // three rows from the registry exactly as every pane does (P5-c).
    const shared = createTranscriptRenderers({
      slot: activeSlot || undefined,
      // An unparseable file row has always fallen through to the bubble on
      // this page (a pane draws nothing for it); P5-b changes no row's output.
      renderUnparsedFile: (m, ctx) => bubble.render(m, ctx),
      onFileOpen: handleFileOpen,
      onFolderOpen: handleFolderOpen,
      onOpenSubagentPanel: handleSubagentPanelOpen,
      toolDisclosure,
      onToolDisclosureChange: setToolDisclosureFor,
      // Animate tools in the trailing group (after last assistant/streaming text).
      toolRunning: (_m, ctx) => slotStateRef2.current === 'tool_running' && ctx.index > lastTextIdxRef.current,
      transcriptHot,
      appInPanel: mcpAppPanel,
      onOpenApp: revealAppInPanel,
      // The Loop button is offered only when this row's own loop is the one
      // still bound to the slot, so a historical card never opens a successor
      // loop's controls (the match rule lives in the factory).
      activeNudgeLoopId: automationId,
      onOpenNudgeLoop: () => setAutomationOpen(true),
      continuable,
      interrupted,
      continuing,
      onContinue: handleContinue,
      onPickModel: openModelPickerFromError,
      onOpenDefaultModel: embedded || popout ? undefined : openDefaultModelSetting,
      onOpenSignIn: embedded || popout ? undefined : openKiroSignIn,
      onOpenCapabilities: embedded || popout ? undefined : openMemberCapabilities,
      onSessionOpen: selectSessionTab,
      sessions: connected ? sessionTitles : undefined,
      activeSession: activeSlot || undefined,
    })
    const renderers = mergeRenderers([
      ...shared,
      {
        // Approval flow: the permission cards own it; grouped, never a standalone row.
        id: 'permission',
        roles: ['permission'],
        render: () => null,
      },
      {
        // The page's undrawn set is NARROWER than the SDK default's: reasoning
        // roles without reasoning content (the old `isReasoningRole -> null`
        // arm) and the queue rail's rows draw nothing here, but `system` /
        // `done` -- lifecycle markers this store never carries -- are left
        // unclaimed on purpose, so they take the bubble fallback exactly as the
        // if-chain's fall-through did rather than vanishing.
        id: 'undrawn',
        roles: [...REASONING_ROLES, 'queued'],
        render: () => null,
      },
      {
        // A quiet monitor-loop cycle replies with a bare zero-width space
        // (U+200B): the content is truthy but renders as nothing, so the row
        // would draw as an empty bubble -- one per quiet cycle, historical
        // transcripts included. Skip it; rows carrying file-change chips still
        // render (the chips are the content). Same skip as the app-sdk registry.
        id: 'hidden_invisible_assistant',
        roles: ['*'],
        match: isHiddenInvisibleAssistantRow,
        render: () => null,
      },
      bubble,
    ])
    return { renderers, fallback: bubble }
  }, [slotRunning, handleFileOpen, handleArtifactOpen, selectSessionTab, sessionTitles, connected, handleFork, handleQuote, handleAsk, chatConfig, activeSlot, regenerating, activeSlotRemoteBound, handleRegenerate, handleEditResend, editLast, handleEditConsumed, slotHasMore, loadingOlder, cursorIsForActiveSlot, slotOldestIndex, handleLoadEarlier, renderUserContentCb, highlightTs, activeSlotTitle, mode, embedded, popout, handleOpenDiff, planTaskId, artifactPaths, automationId, toolDisclosure, setToolDisclosureFor, linkPreviewsOn, socialShareOn, voiceRecoverySlot, handleSubagentPanelOpen, isPinned, handleTogglePinForMessage, showRefusedPress, transcriptHot, revealAppInPanel, continuable, interrupted, continuing, handleContinue, openModelPickerFromError, openDefaultModelSetting, openKiroSignIn, openMemberCapabilities, handleFolderOpen, handleSpeak, handleApplyPlan, mcpAppPanel, redactionCoachTs, setAutomationOpen])

  const renderMessage = useCallback((i: number, m: ChatMessage) => {
    // Key identity rules (clientTs preference + streaming->assistant role
    // normalization) live in messageRowKey -- see its doc comment.
    const key = messageRowKey(m, i)
    const ctx: MessageRenderContext = {
      index: i,
      messages: messagesRef.current,
      running: slotRunning,
      key,
      onFileOpen: handleFileOpen,
      hideCardOwnedOAuth: connectionsUiOn,
      autoDeniedIds: NO_AUTO_DENIED,
      // The shared row set returns `ctx.row(...)`; the row must be a KEYED
      // passthrough, not an element, so a tool line lands in the DOM exactly as
      // this page's own entry used to render it (the virtualizer measures the
      // row's component root). `wrapper` is reached only by a registry default
      // this page does not override (raw wire-shape tool rows).
      wrapper: (children) => <Fragment key={key}>{children}</Fragment>,
      row: (children) => <Fragment key={key}>{children}</Fragment>,
    }
    const entry = resolveRenderer(m, chatPageRenderers)
    // A role nobody claims renders as the conversational bubble -- what the
    // if-chain's fall-through did, so an unknown role is visible, never lost.
    // By reference: the merged list's tail is an SDK default, not the bubble.
    return (entry ?? bubbleRenderer).render(m, ctx)
  }, [chatPageRenderers, bubbleRenderer, slotRunning, handleFileOpen, connectionsUiOn])

  // Hoisted out of the row map so every TurnBlock receives the SAME function
  // identity per render — an inline closure there re-created it per row per
  // render and defeated memo(TurnBlock) even when the turn identity was stable
  // (see createTurnGrouper). It depends on nothing row-specific.
  const renderTurnItem = useCallback((it: TurnItem, _j: number) => {
    // Skip hidden tool messages (✅/🚫 completions) to avoid empty py-1 wrappers
    if (it.kind === 'single' && it.msg.role === 'tool' && !it.msg.content.startsWith('🔧')) return null
    // Same for hidden invisible-only assistant rows: renderMessage draws
    // nothing for them, and the bare wrapper would still stack py-1 spacers,
    // one per quiet monitor cycle.
    if (it.kind === 'single' && isHiddenInvisibleAssistantRow(it.msg)) return null
    return <div key={turnLeadKey(it, stableMsgKey)} data-content-column="" className={`px-4 mx-auto w-full py-1`} style={{ maxWidth: 'var(--mc-content-width, 900px)' }}>
      {it.kind === 'group' ? (() => {
        const unresolvedPerms = it.msgs.filter(m => m.role === 'permission' && !m.meta?.resolved)
        // Skip group entirely if it only contains unresolved permissions (handled by ApprovalBar)
        if (it.msgs.every(m => m.role === 'permission')) return null
        return (
        <CollapsibleToolGroup
          count={it.msgs.filter(m => m.role !== 'permission').length}
          disclosureKey={`ctg-${turnLeadKey(it, stableMsgKey)}`}
          hasPermission={false}
          isRunning={false}
          permissionMeta={unresolvedPerms.at(-1)?.meta as Record<string, unknown> | undefined}
          pendingPermCount={unresolvedPerms.length}
          onApprove={(() => {
            const aid = unresolvedPerms.at(-1)?.meta?.approval_id as string | undefined
            if (!aid) return approve
            return async (action: string) => { await api.resolveApproval(aid, toApiDecision(action)); dismissApproval(aid) }
          })()}
          onViewActivity={toggleAct}
          activityOpen={activityOpen}
        >{it.msgs.map((m, j) => <div key={msgIdentityKey(m, stableMsgKey)}>{renderMessage(it.startIdx + j, m)}</div>)}</CollapsibleToolGroup>)
      })() : renderMessage(it.idx, it.msg)}
    </div>
  }, [stableMsgKey, renderMessage, approve, dismissApproval, toggleAct, activityOpen])

  // ---- Measure-farm wiring ----
  // The farm's renderItem must reproduce the transcript row wrappers EXACTLY
  // (same classes, same maxWidth, default disclosure) so an off-screen
  // measurement equals the height the row will really mount at. Group rows
  // whose members are all permissions render null in the transcript; the farm
  // mirrors that so their measured height is the wrapper's own (near-zero).
  const buildFarmItem = useCallback((i: number): React.ReactNode => {
    const item = renderedDisplayItems[i]
    if (!item) return null
    if (item.kind === 'turn') {
      return <TurnBlock turn={item} renderItem={renderTurnItem} collapseAll={chatConfig.collapseAllSteps} appToolCallIds={appToolCallIds} disclosure={undefined} disclosureKey={`farm-${i}`} onDisclosureChange={() => {}} />
    }
    return (
      <div data-content-column="" className={`px-4 mx-auto w-full py-1`} style={{ maxWidth: 'var(--mc-content-width, 900px)' }}>
        {item.kind === 'group' ? (() => {
          if (item.msgs.every(m => m.role === 'permission')) return null
          return (
            <CollapsibleToolGroup
              count={item.msgs.filter(m => m.role !== 'permission').length}
              disclosureKey={`farm-ctg-${i}`}
              hasPermission={false}
              isRunning={false}
              permissionMeta={undefined}
              pendingPermCount={0}
              onApprove={approve}
              onViewActivity={toggleAct}
              activityOpen={false}
            >{item.msgs.map((m, j) => <div key={msgIdentityKey(m, stableMsgKey)}>{renderMessage(item.startIdx + j, m)}</div>)}</CollapsibleToolGroup>
          )
        })() : renderMessage(item.idx, item.msg)}
      </div>
    )
  }, [renderedDisplayItems, renderTurnItem, chatConfig.collapseAllSteps, appToolCallIds, approve, toggleAct, stableMsgKey, renderMessage])
  const renderFarmItem = useCallback((i: number): React.ReactNode => {
    farmPassRef.current = true
    try { return buildFarmItem(i) } finally { farmPassRef.current = false }
  }, [buildFarmItem])

  /**
   * Mobile sessions drawer, as ONE value rather than an open flag plus a
   * mounted flag. `closing` exists because the panel must stay in the DOM while
   * it slides out — with two booleans that window is exactly where they drift
   * apart, and the panel either unmounts mid-slide or is left mounted after it.
   *
   * `open` is the intent (the toggle reads it, aria reads it); mount is
   * `phase !== 'closed'`. There is one writer per transition below, and the
   * gesture reports through its `onCommit` / `onSettle` callbacks rather than
   * writing the phase itself.
   */
  const [drawerPhase, setDrawerPhase] = useState<'closed' | 'open' | 'closing'>('closed')
  const mobileSessions = drawerPhase === 'open'
  const drawerMounted = drawerPhase !== 'closed'
  /** Panel offset in px: `-innerWidth` offscreen, `0` at rest. A MotionValue so
   *  the drag writes it at frame rate without re-rendering this component. */
  const drawerX = useMotionValue(0)
  /** The sliding panel and the scrim behind it. Handed to
   *  `registerDrawerTargets` so the settle animates THESE ELEMENTS (compositor)
   *  instead of sampling `drawerX` on the main thread — the only way the slide
   *  holds its frame rate while sessions stream. Refs rather than state:
   *  nothing renders off them. */
  const drawerPanelRef = useRef<HTMLDivElement | null>(null)
  const drawerScrimRef = useRef<HTMLDivElement | null>(null)
  /**
   * The drawer's travel: its OWN width, not the screen's.
   *
   * The panel is deliberately narrower than the viewport (`DRAWER_UNCOVERED_PX`
   * of chat stays visible beside it), so a travel of `innerWidth` puts it fully
   * offscreen at ~90% of the slide — and the settle's whole deceleration tail
   * then plays with nothing on screen. Measured on a 390px phone: the panel
   * vanished at 160ms of a 300ms dismissal, leaving 140ms of invisible motion
   * plus a scrim still 10% dark, fading alone. Matching the travel to the width
   * lands the panel's edge and the scrim's zero on the same frame, which is what
   * makes the ease-out readable.
   *
   * Live, not captured: rotation and resize change it, and a settle registered
   * earlier must not animate against a stale span. The safe-area inset is part
   * of it — the panel is pinned at `left-safe`, so it starts that far in and has
   * to cross it too.
   */
  const drawerTravel = useCallback(
    () => Math.max(0, (window.innerWidth || 0) - DRAWER_UNCOVERED_PX + safeAreaLeft()),
    [],
  )
  /**
   * RIGHT-side panel slide (mobile only). The inline side panel used to enter
   * by animating `width: 0 → auto` — a LAYOUT animation the compositor cannot
   * take, which re-laid-out the squeezed chat pane on every frame of the
   * 400ms open. On mobile it covers the full window anyway, so it is rendered
   * as a fixed overlay instead and slides in from the RIGHT on the compositor,
   * mirroring the sessions drawer. Offsets run [0, +width] (closed = +width).
   * Desktop keeps the width reveal: there the chat pane genuinely shares the
   * row and the reflow is the point.
   */
  const sideOverlayX = useMotionValue(0)
  const sideOverlayPanelRef = useRef<HTMLDivElement | null>(null)
  const [sideOverlayPhase, setSideOverlayPhase] = useState<'closed' | 'open' | 'closing'>('closed')
  const sideOverlayPhaseRef = useRef(sideOverlayPhase)
  sideOverlayPhaseRef.current = sideOverlayPhase
  useEffect(() => registerDrawerTargets(sideOverlayX, {
    panel: () => sideOverlayPanelRef.current,
    scrim: () => null,
    travel: () => window.innerWidth || 0,
  }), [sideOverlayX])
  /** The scrim tracks the panel instead of running its own fade, so a half-drag
   *  is half-dimmed and a cancelled drag un-dims with the finger. Divided by the
   *  drawer's OWN travel, so it reaches 0 exactly as the panel clears the edge. */
  const drawerScrim = useTransform(drawerX, x =>
    Math.max(0, Math.min(1, 1 + x / Math.max(1, drawerTravel()))))
  // Point every settle on `drawerX` at real DOM. Registered once for the page's
  // lifetime, reading the elements through the refs at animation time — the
  // panel is mounted and unmounted per open, so binding the nodes themselves
  // here would go stale on the first close. Safe ONLY because the drawer's
  // ChatSidebar renders staticRows (no projection nodes under a compositor-
  // driven transform — see registerDrawerTargets' precondition).
  //
  // `travel` is recomputed per call rather than captured, so a rotation or a
  // resize between registration and the next settle cannot animate against a
  // stale span.
  useEffect(() => registerDrawerTargets(drawerX, {
    panel: () => drawerPanelRef.current,
    scrim: () => drawerScrimRef.current,
    travel: drawerTravel,
  }), [drawerX, drawerTravel])
  // Read for the transition guards below. The animation each transition starts
  // is a side effect, so it must not live inside a setState updater — React may
  // invoke an updater more than once, which would start the settle twice.
  const drawerPhaseRef = useRef(drawerPhase)
  drawerPhaseRef.current = drawerPhase
  /**
   * The drawer's history entry, so the platform back gesture dismisses the
   * drawer instead of leaving `/chat` (#5795).
   *
   * ONE entry, and it exists exactly while the drawer is open: every open mints
   * it and every close that was not itself the Back spends it. The alternative --
   * leaving it behind -- is the twin-entry defect `SidePanelLayout`'s back control
   * documents: two entries with the same URL, so the next back-swipe visibly does
   * nothing.
   *
   * The entry is a bare DUPLICATE of the one below it. The drawer is view state,
   * not a location: a URL that moved would have to be unwound on the pop, and
   * unwinding a `?sid=` is exactly what the sid effect would misread as the user
   * retracing sessions.
   *
   * Deliberately NOT marked in `history.state`, unlike `SUBNAV_PUSH_STATE`. That
   * marker earns its place because a SubNav drill-in CHANGES the url, so a cold
   * deep link can land on the drilled-in entry and the marker is the only way to
   * tell "we pushed this" from "the user arrived here". Nothing can deep-link a
   * drawer open, so there is no such question to answer and a marker would be
   * write-only state. Ownership is this ref, which is also the only form that is
   * correct: a marked entry can outlive the mount that pushed it -- a reload
   * restores `history.state`, and Forward can walk back INTO one -- so reading a
   * marker would have the page consume an entry it never pushed.
   */
  const drawerEntryRef = useRef(false)
  const locationRef = useRef(location)
  locationRef.current = location
  const pushDrawerEntry = useCallback(() => {
    // Desktop's sidebar is a persistent column with its own toggle, not a layer
    // over the content, and Back there already means "leave the route".
    if (!isMobile || drawerEntryRef.current) return
    const loc = locationRef.current
    drawerEntryRef.current = true
    navigate({ pathname: loc.pathname, search: loc.search, hash: loc.hash })
  }, [isMobile, navigate])
  /** Spend the entry, if we still hold one. `drawerPopRef` is what tells the sid
   *  effect this POP is bookkeeping rather than a session the user asked for. */
  const consumeDrawerEntry = useCallback(() => {
    if (!drawerEntryRef.current) return
    drawerEntryRef.current = false
    drawerPopRef.current = true
    navigate(-1)
  }, [navigate, drawerPopRef])
  const openSidebar = useCallback(() => {
    if (drawerPhaseRef.current === 'open') return
    // Seat it offscreen before the mount so the first painted frame is the
    // closed offset, then let the shared settle carry it in.
    if (drawerPhaseRef.current === 'closed') drawerX.set(-drawerTravel())
    drawerPhaseRef.current = 'open'
    setDrawerPhase('open')
    animateDrawer(drawerX, 0)
    pushDrawerEntry()
  }, [drawerX, drawerTravel, pushDrawerEntry])
  // The sidebar-folder chip's roster and handler, for every markdown span on this
  // page (`InlineCode` -> `FolderChip`). A folder path in prose (`goal/worker`)
  // becomes a click that reveals that row in the sidebar -- the same store
  // request the command bar's folder scope issues, so the sidebar answers it the
  // same way (expand ancestors, leave the flat lane, scroll, flash). The click
  // first puts a sidebar ON SCREEN, exactly as the header menu's "Reveal in
  // sidebar" does above: the phone drives its drawer, the desktop pins the pane,
  // and only then is the request dispatched -- the store holds it until the
  // sidebar mounts and consumes it. Embed-chat never mounts a sidebar, so no
  // handler is wired there and `resolveFolderChip` offers no chip: a stored
  // request would outlive the view and replay on whichever sidebar mounts next.
  // Offered while OFFLINE, unlike the session chip: revealing a row is
  // client-side work on a list already cached, where switching a session needs
  // the gateway. Provided once here rather than threaded through
  // MarkdownRenderer props, so subagent and workflow cards and system notices
  // offer the same chip as a message body.
  const sidebarFolderActions = useMemo<SidebarFolderActions>(() => ({
    folders: chatFolders,
    onFolderReveal: embedMode === 'chat' ? undefined : (folderId: string) => {
      sidebarAutoHidden.current = null
      if (isMobile) openSidebar()
      else if (!sidebarPinned) setSidebarPinned(true)
      dispatch(requestFolderReveal(folderId))
    },
  }), [chatFolders, dispatch, embedMode, isMobile, openSidebar, sidebarPinned])
  /** Mount the panel for a drag in progress. Deliberately NOT `openSidebar`:
   *  that one runs the settle to the rest position, which would race the finger
   *  for the same value and pull the panel out from under it. The gesture has
   *  already seated the offset and owns it until release.
   *
   *  No history entry here either — the drag has not committed to anything yet,
   *  and one the user drags back would push and immediately pop. The entry is
   *  minted where the gesture COMMITS, in `onCommit`. */
  const beginDrawerDrag = useCallback(() => {
    drawerPhaseRef.current = 'open'
    setDrawerPhase('open')
  }, [])
  /** Run the close animation and nothing else. Split out because the Back that
   *  closes the drawer must NOT consume an entry — that pop already spent it. */
  const runDrawerClose = useCallback(() => {
    if (drawerPhaseRef.current !== 'open') return false
    drawerPhaseRef.current = 'closing'
    setDrawerPhase('closing')
    animateDrawer(drawerX, -drawerTravel(), () => {
      drawerPhaseRef.current = 'closed'
      setDrawerPhase('closed')
    })
    return true
  }, [drawerX, drawerTravel])
  const closeSidebar = useCallback(() => {
    // Phase first, then the pop: the POP effect below skips a drawer that is no
    // longer 'open', which is what keeps this close from being counted twice.
    if (runDrawerClose()) consumeDrawerEntry()
  }, [runDrawerClose, consumeDrawerEntry])
  /**
   * The Back that lands on the entry BELOW the drawer's: close the drawer and
   * stay put.
   *
   * While the drawer is open and we hold an entry, that entry is the one on top,
   * so any POP is a pop off it. Closing WITHOUT `consumeDrawerEntry` is the
   * point — the pop is the consumption. `location.key` in the deps rather than
   * `location`, so this runs once per history entry and not on every search-param
   * rewrite the sid effect makes.
   */
  useEffect(() => {
    if (navigationType !== 'POP') return
    if (!drawerEntryRef.current || drawerPhaseRef.current !== 'open') return
    drawerEntryRef.current = false
    runDrawerClose()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [location.key, navigationType])
  // Close the drawer when a session is selected. Routed through closeSidebar so
  // it slides out — flipping straight to 'closed' would unmount it on the spot.
  useEffect(() => { if (isMobile) closeSidebar() }, [activeSlot]) // eslint-disable-line react-hooks/exhaustive-deps
  // Leaving the mobile viewport: drop the panel with no slide. There is no
  // mobile drawer to animate on the other side of that crossing, and the
  // desktop sidebar owns its own open state. The history entry goes with it —
  // the desktop sidebar is a column, not a layer, so an entry standing for
  // "a drawer is open" would be left with nothing to dismiss.
  useEffect(() => { if (!isMobile) { setDrawerPhase('closed'); consumeDrawerEntry() } }, [isMobile]) // eslint-disable-line react-hooks/exhaustive-deps
  const chatContainerRef = useRef<HTMLDivElement>(null)
  // Measured container height — sizes the sidebar border-box morph (the panel
  // rect the box shrinks from on collapse and grows back to on expand).
  const [containerH, setContainerH] = useState(0)
  useEffect(() => {
    const el = chatContainerRef.current
    if (!el) return
    const measure = () => setContainerH(el.clientHeight)
    measure()
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    return () => ro.disconnect()
  }, [])
  // Full-height activity bar slot in the App shell grid (desktop dashboard
  // only): the Activity panel portals into it so it spans the window
  // top-to-bottom. The header row ends at the slot's left edge,
  // so the top-bar right cluster (capsule, terminal, bell, gear) shifts left
  // when the panel opens. Null on mobile / embed frames -> inline fallback.
  // `useShellSlot` seeds synchronously (no first-render flash into the inline
  // fallback) and watches the DOM across a mobile -> desktop crossing; the
  // phone top-bar slots below resolve through the same hook.
  const activitySlot = useShellSlot('activity-bar-slot', !isMobile && !embedMode)
  /** The inline panel's mount predicate, shared by the overlay phase effect
   *  and the render below so the two cannot disagree. */
  // A persisted dashboard tab holds the panel mounted only while the preview
  // offers it; off, the tab is withheld and must not keep the panel on screen.
  const hasTaskDashboard = dashboardPreview && tabsCtl.tabs.some(tab => tab.kind === 'command-center')
  const sidePanelWantsMount = shouldMountSidePanel({ activityOpen, hasLiveAppTab, hasBrowserTab, hasTaskDashboard, searchOpen: search.isOpen })
    && !isSidePanelHidden({ activityOpen, hasLiveAppTab, hasBrowserTab, hasTaskDashboard, searchOpen: search.isOpen })
  // Mobile right-panel overlay: slide in when the panel wants the screen,
  // slide out (keeping it mounted for the travel) when it stops wanting it.
  useEffect(() => {
    if (!isMobile) { setSideOverlayPhase('closed'); takeOverDrawer(sideOverlayX); return }
    if (sidePanelWantsMount) {
      if (sideOverlayPhaseRef.current === 'open') return
      if (sideOverlayPhaseRef.current === 'closed') sideOverlayX.set(window.innerWidth || 0)
      sideOverlayPhaseRef.current = 'open'
      setSideOverlayPhase('open')
      takeOverDrawer(sideOverlayX)
      animateDrawer(sideOverlayX, 0)
    } else {
      if (sideOverlayPhaseRef.current !== 'open') return
      sideOverlayPhaseRef.current = 'closing'
      setSideOverlayPhase('closing')
      takeOverDrawer(sideOverlayX)
      animateDrawer(sideOverlayX, window.innerWidth || 0, () => {
        sideOverlayPhaseRef.current = 'closed'
        setSideOverlayPhase('closed')
      })
    }
  }, [isMobile, sidePanelWantsMount, sideOverlayX])
  /** Read by the right overlay's release handler, which must know whether the
   *  store currently has the panel open without depending on it. */
  const activityOpenRef = useRef(activityOpen)
  activityOpenRef.current = activityOpen
  /**
   * Mount the right overlay for a drag in progress — the mirror of
   * `beginDrawerDrag`, and deliberately NOT the effect above: that one runs a
   * settle to the rest position, which would race the finger for the same
   * value. The gesture has already seated the offset and owns it until release.
   *
   * The STORE is left alone until the release commits. Writing `activityOpen`
   * here would re-run the effect above mid-gesture, and a drag the user then
   * reconsiders would still have flipped (and persisted) the panel's open flag.
   * The phase alone is enough to mount, because the effect keys on the store's
   * predicate rather than on the phase.
   */
  const beginSideOverlayDrag = useCallback(() => {
    sideOverlayPhaseRef.current = 'open'
    setSideOverlayPhase('open')
  }, [])

  /** True while the INLINE side panel (mobile / embed, no actbar column) is
   *  mounted AND visible.
   *
   *  Mobile has no actbar grid column, so the panel renders as a flex sibling of
   *  the chat pane at the full window width — it covers the content area
   *  outright. Anything the chat pane floats over that area (the sessions FAB
   *  below) would land on top of the panel's own controls, so it is gated on
   *  this. Reuses the panel's own mount/visibility predicates rather than
   *  re-deriving them from `activityOpen`, which is only one of their inputs (a
   *  live app or browser tab keeps the panel mounted through a close, and the
   *  find pane hides it while owning the dock). */
  const inlineSidePanelShowing = !activitySlot
    && shouldMountSidePanel({ activityOpen, hasLiveAppTab, hasBrowserTab, hasTaskDashboard, searchOpen: search.isOpen })
    && !isSidePanelHidden({ activityOpen, hasLiveAppTab, hasBrowserTab, hasTaskDashboard, searchOpen: search.isOpen })
  // ONE binding per panel, each covering both of ITS directions: a rightward
  // drag opens the sessions drawer, a leftward drag on the open drawer closes
  // it. Which rule applies is read live from `open` inside the hook, so the
  // opening drag does not tear its own listeners down when the panel mounts
  // mid-gesture.
  //
  // The two instances share this element and are told apart by DIRECTION, not by
  // where the touch began — so an opening drag works from anywhere in the pane
  // rather than out of a narrow edge band. The one thing direction cannot
  // separate is a panel that is already OPEN: its closing drag is the other
  // panel's opening drag, so each instance is disabled while the other's panel
  // is on screen.
  const drawerDragging = useDrawerSwipe(chatContainerRef, {
    // Gated on the sibling not being OPEN, not on it having finished closing.
    // The two panels exclude each other because one's closing direction is the
    // other's opening direction — a hazard that lasts only while the sibling is
    // actually open. Requiring `'closed'` also held the gate shut for the whole
    // slide out, so a swipe dismissing one panel could not be followed straight
    // away by a swipe revealing the other.
    enabled: isMobile && !embedded && sideOverlayPhase !== 'open',
    travel: drawerTravel,
    open: mobileSessions,
    x: drawerX,
    onGestureOpen: beginDrawerDrag,
    // The gesture's COMMIT points, which is where its history entry is minted and
    // spent — `beginDrawerDrag` only mounts the panel, and a drag the user
    // reconsiders commits to closed without ever having minted one (both helpers
    // are guarded on `drawerEntryRef`, so the call is a no-op then).
    //
    // Deliberately NOT `onSettle`. That one waits for the 120-450ms slide so a
    // consumer cannot unmount the panel mid-animation, which is exactly what
    // makes it the wrong signal here: the entry stands for "a drawer is open, so
    // Back dismisses it", an INTENT, and the hook reports intent at release.
    // Minting on arrival left the whole opening slide with the panel covering the
    // screen and no entry to pop, so a Back there leaked past the drawer and left
    // /chat — #5795 again, reachable by drag rather than by tap. Spending on
    // arrival had the mirror hazard: a Back during the closing slide popped the
    // still-unspent entry while the POP effect above ignored it (the phase is
    // 'closing', not 'open'), and the settle then spent a second, real entry.
    onCommit: open => {
      if (open) { pushDrawerEntry(); return }
      // Committed to closing: mark it now so the sibling's gate opens immediately.
      // 'closing' rather than 'closed' because the panel is still on screen and
      // `drawerMounted` keys on that — unmounting here would cut the slide short.
      drawerPhaseRef.current = 'closing'
      setDrawerPhase('closing')
      consumeDrawerEntry()
    },
    // Arrival, which is bookkeeping only: the phase flip that unmounts the panel.
    onSettle: open => {
      if (open) return
      drawerPhaseRef.current = 'closed'
      setDrawerPhase('closed')
    },
  })
  // Right-hand side panel, same gesture mirrored. Not bound when the actbar
  // column owns the panel (desktop) or while the find pane holds the dock —
  // there the store's mount predicate refuses to keep the panel open, so a
  // committed drag would be undone by the effect above on the next render.
  useDrawerSwipe(chatContainerRef, {
    enabled: isMobile && !embedded && !activitySlot && !search.isOpen && drawerPhase !== 'open',
    side: 'right',
    open: sideOverlayPhase === 'open',
    x: sideOverlayX,
    onGestureOpen: beginSideOverlayDrag,
    // See the left instance: the sibling's gate has to open at commit time, and
    // 'closing' keeps this panel mounted for the rest of its slide.
    onCommit: open => {
      if (open) return
      sideOverlayPhaseRef.current = 'closing'
      setSideOverlayPhase('closing')
    },
    onSettle: open => {
      // Committed: publish to the store, which is what keeps the panel open
      // across a re-render and remembers it for this chat. The effect above sees
      // a phase already at 'open' and does not re-animate.
      if (open) { dispatch(openActivityPanel()); return }
      // Parked: write 'closed' BEFORE touching the store, so the effect sees a
      // panel that has already arrived and does not run a second slide-out over
      // this one. The store is only flipped when it actually had the panel open
      // — an opening drag that was reconsidered never wrote it in the first
      // place, and toggling here would OPEN the panel it just dismissed.
      sideOverlayPhaseRef.current = 'closed'
      setSideOverlayPhase('closed')
      if (activityOpenRef.current) toggleAct()
    },
  })
  /** Reveal a session's pull request / issue in that session's side panel.
   *
   *  Fires from a sidebar chip AFTER ChatSidebar has dispatched the slot switch,
   *  so `switchSlot.pending` has already published the target slot to the store —
   *  but activeSlotRef is assigned during RENDER and still names the chat being
   *  left, so `slot` is threaded explicitly through every write below.
   *
   *  The url is re-parsed rather than trusted: the chip payload comes from the
   *  BACKEND's scan, and running it through the panel's own parser is what
   *  guarantees the injected link matches the shape (and the host allowlist) the
   *  panels already work with.
   *
   *  Returns whether the panel took the link. FALSE hands the click back to the
   *  chip's own anchor, so a url this parser rejects opens the provider instead
   *  of doing nothing at all. That is reachable rather than theoretical: the two
   *  parsers read the self-managed GitLab allowlist from different places, and
   *  `sourceHosts` is empty until the dashboard-config query resolves (and stays
   *  empty if it fails), so every self-hosted chip parses to null in that window
   *  even though the backend scan accepted it. */
  const revealSourceLink = useCallback((slot: string, chip: { url: string; kind: SourceLinkKind }): boolean => {
    const link = parseSourceLinkUrl(chip.url, sourceHostsRef.current, jiraSourceHostsRef.current)
    if (!link) return false
    const view = link.kind === 'issue' ? 'issues' : 'changes'
    // Durable BEFORE the state update, and one key at a time. Writing inside the
    // updater would both make it impure (React may invoke an updater more than
    // once) and publish this window's whole map, deleting a sibling window's
    // reveals — see `commitRevealedSource`.
    commitRevealedSource(slot, link.kind, link.url)
    setRevealedSources(previous => ({
      ...previous,
      [slot]: { ...previous[slot], [link.kind]: link },
    }))
    selectSource(link.kind, link.url, slot)
    // Addressed by slot, not through tabsCtl: that binding is still the chat
    // being left, so the tab would open on the wrong strip.
    openPanelView(slot, view)
    // The find pane owns the right-hand dock exclusively (shouldMountSidePanel
    // returns false while it is open), so revealing into a session with search
    // open would suppress the chip's navigation and then mount nothing at all.
    // Same reason handleFileOpen / handleOpenDiff close it before opening a dock
    // panel.
    search.close()
    dispatch(openActivityToTab(view))
    // The mobile session drawer covers the panel it would reveal into. The
    // activeSlot effect closes it on a real switch, but a chip on the session
    // already open does not change activeSlot.
    if (isMobile) closeSidebar()
    return true
    // eslint-disable-next-line react-hooks/exhaustive-deps -- only `search.close()` is read off `search`, and that member is a useCallback([]) in useMessageSearch; the object around it is rebuilt every render, so depending on it would recreate this reveal handler continuously
  }, [dispatch, isMobile, selectSource, closeSidebar])
  // Web Preview expand mode — broadcast by the Web Preview tab's
  // expand toggle. When on, hide the session list and maximize the side panel
  // (passed to SidePanel), so the preview gets max room and chat shrinks to its
  // minimum. App collapses the left nav off the same event.
  //
  // Hiding the list drives `sidebarPinned` directly instead of overriding
  // `sidebarOpen`: an override leaves the sessions toggle visibly present but
  // inert. Driving the real state keeps that toggle working normally inside
  // expand mode. `sidebarAutoHidden` holds the pre-expand state to restore on
  // exit, and is cleared once the user toggles the list themselves. Neither
  // transition persists `mc-sidebar-pinned` — only a user toggle does.
  //
  // The ref is read and cleared HERE, in the handler, and only plain values
  // reach the setter: a state updater must be pure, and React invokes one twice
  // under StrictMode, which would make the second pass read an already-cleared
  // ref and lose the restore value.
  //
  // The mobile drawer is a separate state, so it is closed outright rather than
  // suppressed — a swipe or a tap still reopens it, which an override would not
  // allow.
  const [previewExpanded, setPreviewExpanded] = useState(false)
  useEffect(() => {
    const onPreviewExpand = (e: Event) => {
      const expanded = !!(e as CustomEvent<{ expanded?: boolean }>).detail?.expanded
      setPreviewExpanded(expanded)
      if (expanded) {
        closeSidebar()
        if (sidebarAutoHidden.current === null) sidebarAutoHidden.current = sidebarPinnedRef.current
        setSidebarPinned(false)
        return
      }
      const prior = sidebarAutoHidden.current
      sidebarAutoHidden.current = null
      if (prior !== null) setSidebarPinned(prior)
    }
    window.addEventListener(PREVIEW_EXPAND_EVENT, onPreviewExpand)
    return () => window.removeEventListener(PREVIEW_EXPAND_EVENT, onPreviewExpand)
    // Still a mount-once registration in practice: closeSidebar closes over only
    // `drawerX` (a useMotionValue, stable for the component's lifetime) and
    // `drawerTravel` (a useCallback([]) that reads window.innerWidth at call time),
    // so its identity never changes. Naming it rather than relying on that keeps the
    // listener from silently capturing a stale copy if closeSidebar ever gains a dep.
  }, [closeSidebar])
  // The no-sessions force-open yields to expand mode: with an empty list no
  // sessions toggle is rendered, so suppressing it makes nothing inert, and the
  // preview would otherwise stay covered by a list that cannot be dismissed.
  const sidebarOpen = isMobile
    ? mobileSessions
    : (sidebarPinned || (filteredSlots.length === 0 && !previewExpanded))
  // Whether the sidebar -- and with it the banner that says a failed
  // folder-order read -- is on this screen right now. The same facts that mount
  // it below: embed-chat never mounts one, embed-sessions always does, and the
  // dashboard mounts it in the drawer while the drawer is (mobile) or the panel
  // is open (desktop). The header menu and the folder-suggestion card say the
  // failure themselves only when this is false: one screen, one notice.
  const sidebarOnScreen = embedMode === 'chat'
    ? false
    : embedMode === 'sessions'
      ? true
      : (isMobile ? drawerMounted : sidebarOpen)

  // ── Collapsed-sidebar hover flyout ──────────────────────────────────────
  // Hovering the toggle while collapsed opens a recents list over the chat, so
  // switching sessions stops being expand → switch → collapse. It is purely an
  // overlay: it never touches `sidebarPinned`, because `panelReserve` and
  // `panelFillWidth` below both read `sidebarOpen`, and flipping it to show a
  // transient popover would re-run the side panel's width maths and visibly
  // resize the chat every time the pointer rested on a 28px button.
  const flyoutTriggerRef = useRef<HTMLButtonElement>(null)
  const flyoutSurfaceRef = useRef<HTMLDivElement>(null)
  // Touch is a second gate beyond isMobile: a desktop-width touch device has no
  // hover, so the flyout would only ever appear as a tap artefact.
  const flyoutEligible = !isMobile && !isTouchDevice() && !splitMode
    && embedMode !== 'chat' && embedMode !== 'sessions'
    && !sidebarOpen && filteredSlots.length > 0
  const flyout = useHoverIntent({
    enabled: flyoutEligible,
    triggerRef: flyoutTriggerRef,
    surfaceRef: flyoutSurfaceRef,
  })
  // Rect the sidebar's clip window should expand FROM, captured at click time
  // from the live flyout element. Null when the expand came from the button
  // alone, which keeps the stock button-rect morph for that path.
  const [expandFrom, setExpandFrom] = useState<{ x: number; y: number; w: number; h: number } | null>(null)
  const expandSidebar = useCallback((fromFlyout: boolean) => {
    const surface = flyoutSurfaceRef.current
    const container = chatContainerRef.current
    if (fromFlyout && surface && container) {
      const s = surface.getBoundingClientRect()
      const c = container.getBoundingClientRect()
      setExpandFrom({ x: s.left - c.left, y: s.top - c.top, w: s.width, h: s.height })
    } else {
      setExpandFrom(null)
    }
    flyout.close()
    window.dispatchEvent(new CustomEvent('toggle-pin-chat-sidebar'))
  }, [flyout])
  // The rect is only valid for the mount it was captured for. Clearing it on
  // collapse means a later button-only expand cannot inherit a stale flyout
  // rect and appear to grow out of nothing.
  useEffect(() => { if (!sidebarOpen) setExpandFrom(null) }, [sidebarOpen])
  const flyoutSwitch = useCallback((key: string) => {
    // User gesture on a listed session row (collapsed-sidebar flyout): the
    // announced class, same as the expanded sidebar's own rows.
    dispatch(switchSlot({ key, announceOnMissing: true }))
    setSplitMode(false)
    flyout.close()
  }, [dispatch, flyout])
  const flyoutNew = useCallback(() => {
    flyout.close()
    // `focusComposerAfter`, not a bare dispatch + rAF: there is one composer and
    // it is bound to the ACTIVE slot, so focusing before creation fulfils puts
    // the caret on the old session and loses whatever is typed. See the module.
    focusComposerAfter(dispatch(createSlot({ agent: defaultAgent || undefined, mode: mode || '' })).unwrap())
  }, [dispatch, defaultAgent, mode, flyout])

  // Force the list open when there is nothing in it, so a user with no sessions
  // still has the surface that creates one. Skipped while expand mode owns the
  // hidden state: re-pinning there would fight the auto-hide and, worse, persist
  // 'true' over the user's stored preference, which the restore on exit then
  // contradicts in the live state.
  useEffect(() => {
    if (filteredSlots.length === 0 && !sidebarPinned && !previewExpanded) {
      setSidebarPinned(true)
      safeSetItem('mc-sidebar-pinned', 'true')
    }
  }, [filteredSlots.length, sidebarPinned, previewExpanded])

  // Horizontal space (px) the detail panel must keep clear so it never grows
  // past its flex row and collapses the chat pane: the open sidebar's width
  // plus a usable chat-pane minimum. On mobile the panel is full-screen (no
  // shared row), so no reserve applies.
  const CHAT_PANE_MIN = CHAT_PANE_MIN_W
  const panelReserve = isMobile ? undefined : (sidebarOpen ? effectiveSidebarWidth : 0) + CHAT_PANE_MIN
  // The panel takes its maximum only while the session list is actually hidden.
  // That maximum is measured against the header's reserve, which knows nothing
  // about the session list's width — so keeping it while the user reopens the
  // list inside expand mode pushes the chat pane below CHAT_PANE_MIN and clips
  // its content. Reverting to the normal width maths there costs the preview a
  // few hundred px in a state the user asked for by reopening the list.
  const panelMaximized = previewExpanded && !sidebarOpen

  // FILL vs BESIDE for the activity panel, decided from the width left for the
  // CHAT once the shell's hideable chrome is subtracted — the nav rail track and
  // the session sidebar (a shrink-0 flex sibling of exactly sidebarWidth; on
  // mobile its drawer is fixed-position and consumes no row width). Undefined =
  // beside. A px width = fill the chat column, squeezing the chat pane to zero
  // while the rail and sidebar stay exactly where they are.
  //
  // The panel's render PATH is unchanged either way, so crossing the threshold
  // never remounts it (no terminal re-attach, no Virtuoso churn) — only its
  // width changes. See sidePanelFillWidth for why this is loop-free.
  const panelFillWidth = sidePanelFillWidth({
    winW,
    railW: railWidth,
    sidebarW: !isMobile && sidebarOpen ? effectiveSidebarWidth : 0,
    isMobile,
  })

  // The mobile sessions toggle, rendered inline by whichever header owns the
  // surface's top-left: the single-chat title row, or in split view the grid's
  // top-left pane (SessionGridView `leading`). Mirrors the desktop toggle
  // exactly, state included: solid while the panel is hidden, light while it
  // is showing.
  const mobileSessionsToggle = (
    <button className="mc-touch-hit p-1 rounded-md text-muted hover:text-text cursor-pointer bg-transparent border-none pointer-events-auto shrink-0" onClick={() => mobileSessions ? closeSidebar() : openSidebar()} aria-label={i18nT('pages.chatPage.toggle_sessions')}>
      {mobileSessions ? <PanelLeftLight size={16} /> : <PanelLeftSolid size={16} />}
    </button>
  )
  /**
   * The same toggle for the shell's bar. `p-2` puts its 32px box at the bar's
   * 8px inset, so the glyph's ink lands on the 16px page gutter the nav logo
   * held before it (page-layout.md, "The title belongs to the content column").
   */
  const topbarSessionsToggle = (
    <button className="mc-touch-hit p-2 rounded-md text-text hover:text-text-strong hover:bg-bg-hover cursor-pointer bg-transparent border-none shrink-0" onClick={() => mobileSessions ? closeSidebar() : openSidebar()} aria-label={i18nT('pages.chatPage.toggle_sessions')} aria-expanded={mobileSessions} data-testid="mobile-topbar-sessions-toggle">
      {mobileSessions ? <PanelLeftLight size={18} /> : <PanelLeftSolid size={18} />}
    </button>
  )

  // Mobile "new session here" (session menu item): creates a session in the SAME folder as
  // the one on screen, so starting a sibling chat does not require opening the
  // sessions drawer. Resolves agent and project like the drawer's folder "+"
  // (createChatInFolderMutation): nearest folder default_agent, then the global
  // default; nearest folder project_dir; a stale folder project dir gets the drawer's own message
  // (isStaleProjectDirError). The create itself stays here rather than in
  // createChatInFolderMutation on purpose: that mutation lives inside
  // ChatSidebar with board-column, tab and attempt state this button has none
  // of. An unfiled session creates an unfiled sibling, like the flyout's
  // New. A filed session waits for the folder list: resolving against a
  // pending or failed query would silently drop the folder's agent and project.
  const mobileNewFolderId = currentSlot?.folder_id || null
  const mobileNewFolder = mobileNewFolderId ? chatFolders.find(f => f.id === mobileNewFolderId) : undefined
  // Disabled only while the folder list is still loading, so a tap cannot
  // create the session before the folder's agent and project are known. Once
  // the list has settled the button is live: a failed load is reported on tap
  // (instead of leaving a dead button a touch user gets no tooltip for), and a
  // folder id that no longer names a folder is treated as unfiled, the way the
  // drawer files such a session.
  const mobileNewSessionReady = !mobileNewFolderId || chatFoldersLoaded || !!chatFoldersError
  const mobileNewSessionHere = async () => {
    if (!mobileNewSessionReady || mobileNewSessionInFlightRef.current) return
    // One notice for both failures below. The body stays the raw error text,
    // which is the key the error journal matches for the notice's ask-agent
    // hand-off; the title names what failed.
    const fail = (error: unknown, title = i18nT('pages.chatPage.could_not_start_a_new_session')) =>
      showActionError(errMessage(error) || i18nT('pages.chatPage.unknown_error'), title)
    if (mobileNewFolderId && !chatFoldersLoaded) return fail(chatFoldersError)
    const folderId = mobileNewFolder ? mobileNewFolderId : ''
    const agent = folderId
      ? resolveFolderAgent(chatFolders, folderId, defaultAgent)
      : (defaultAgent || undefined)
    const project = folderId ? resolveFolderProjectDir(chatFolders, folderId) : undefined
    mobileNewSessionInFlightRef.current = true
    setMobileNewSessionBusy(true)
    try {
      await dispatch(createSlot({
        agent,
        mode: mode || '',
        ...(folderId ? { folder_id: folderId } : {}),
        ...(project ? { project } : {}),
      })).unwrap()
    } catch (error) {
      // Same stale-project-dir sentence as the drawer's folder `+`, as the
      // notice title: it is the one create failure the user can fix, so name
      // the missing path.
      return fail(error, folderId && isStaleProjectDirError(error)
        ? i18nT('pages.chatSidebar.folder_project_dir_missing', { path: project ?? '' })
        : undefined)
    } finally {
      mobileNewSessionInFlightRef.current = false
      setMobileNewSessionBusy(false)
    }
    // Touch devices skip focus by design (no keyboard pop); this only matters
    // for a narrow desktop window, where the caret should follow the create.
    focusComposer()
  }
  const mobileNewSessionLabel = mobileNewFolder
    ? i18nT('pages.chatSidebar.new_chat_in_name', { name: mobileNewFolder.name })
    : i18nT('pages.chat.sessionFlyout.new_chat')
  // The item leads the session (chevron) menu on the phone. It is not a third
  // control beside the sessions toggle: the bar's centre cell holds two
  // (AUTOSDE `max-two-buttons-per-row`, narrow-viewport.md).
  const mobileNewSessionItem = {
    label: mobileNewSessionLabel,
    disabled: !mobileNewSessionReady || mobileNewSessionBusy,
    onSelect: () => { void mobileNewSessionHere() },
  }

  // Unchanged from the inline WelcomeView handler; shared with the composer memory chip.
  const switchMemoryMode = async (newMode: MemoryMode) => {
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
      instanceId: old?.instance_id || undefined,
    }
    try { await dispatch(createSlot(opts)).unwrap() } catch (error) {
      showActionError(errMessage(error) || i18nT('pages.chatPage.unknown_error'))
      return
    }
    try { await dispatch(deleteSlot(activeSlot)).unwrap() } catch (error) {
      showActionError(errMessage(error) || i18nT('pages.chatPage.unknown_error'))
    }
  }
  // The welcome screen puts its memory chip directly above the composer.
  const showComposerMemoryChip = isWelcomeState

  // The band ChatInput renders above the composer, and the session-control chips.
  const composerAbove = useComposerAboveBand({
    langGen,
    controls: sessionControlState,
    folderSuggestion,
    activeSlot,
    sidebarOnScreen,
    folderSuggestionAccept,
    folderSuggestionDecline,
    activeTip,
    dismissTip,
  })
  const composerSessionControls = useSessionControlChips({ sessionControls, openSessionControl, activeSlot, sessionControlStatuses })

  return (
    <RowDisclosureProvider resetKey={activeSlot}>
    <TagPopoverProvider>
    {/* Self-hosted Jira allowlist for every markdown anchor in the page --
        message bodies, previews, and panels alike -- so a pasted Jira URL
        chips identically wherever it renders. Cloud URLs need no provider. */}
    <JiraHostsCtx.Provider value={jiraSourceHosts}>
    <SidebarFolderCtx.Provider value={sidebarFolderActions}>
    <div
      ref={chatContainerRef}
      /* Both sides are this page's own: a rightward drag opens the sessions
         drawer and a leftward one the activity panel, so the app-wide nav
         gesture bound on the shell must not also arm here. Declared on the SAME
         element the two instances below bind, which is what lets them read the
         claim as their own and proceed.

         Withheld when `embedded`, because that is exactly when this page binds
         NOTHING (both instances are gated on `!embedded`) — and an embedded chat
         renders INSIDE the shell (the artifact companion, the Papyrus co-author
         panel, an app SDK panel), at full width on mobile. A claim that outlives
         its ownership there suppressed the nav swipe while serving nothing: a
         dead gesture across the whole screen, on the chat-shaped surface where
         the gesture was most likely to be tried. The claim has to track what is
         actually bound, or the fail-open default is defeated by the one page
         that declares.

         Not also gated on `isMobile`: the app-wide instance is mobile-only, so a
         desktop claim suppresses nothing, and adding the term would imply this
         attribute carries a guarantee about a case it cannot affect. */
      data-owns-swipe={embedded ? undefined : 'left right'}
      className="flex flex-1 min-h-0 h-full overflow-hidden relative"
      /* Published here, not on the chat pane, because the side panel (Activity,
         Browser, SideChat's composer and follow-up chips) is this element's
         child and the chat pane's SIBLING. Anything that reads the setting via
         `var(--mc-message-font-size, 14px)` from inside the panel would
         otherwise silently take the fallback and look right at the default
         only. Content width stays on the pane: it is a column geometry, and the
         panel has its own. */
      style={{ '--mc-message-font-size': `${chatConfig.messageFontSize}px` } as React.CSSProperties}
    >
      <AnimatePresence>
        {isMobile && drawerMounted && (
          <motion.div
            key="sessions-backdrop"
            data-testid="sessions-backdrop"
            className="fixed inset-0 z-[46] bg-black/50 backdrop-blur-xs"
            // ^ Frosted, matching every other scrim in the app (App.tsx's own
            // mobile nav backdrop is the same three classes). Kept adjacent to
            // `key` — the composer-chrome occlusion guard anchors its z-order
            // regex on that proximity. A full-viewport `backdrop-filter` does
            // re-sample its backdrop on every repaint behind it — which under
            // a streaming message list is every frame — and both alternatives
            // were tried and rejected on how they LOOK: dropping it entirely,
            // and deferring it to the settled state (the blur arriving after
            // the panel had stopped read as a second event).
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
          // In split view the grid's top-left pane reserves this column
          // (SessionGridView `leading`); its title row is the same height as
          // the single-chat row, so the toggle keeps this rect there too.
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
        <div className="flex-1 min-w-0 h-full overflow-hidden [&_.sidebar-inner]:!w-full [&_.sidebar-inner]:!border-0 [&_.sidebar-inner]:!rounded-none [&_.sidebar-inner]:!shrink [&_.sidebar-inner]:!bg-bg [--folder-row-sticky-bg:var(--bg)] [&_.sidebar-resize-handle]:!hidden">
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
        // Top edge: below the shell's bar while this page still draws its own
        // title row under that bar, so the two bars stay visible above the
        // panel; with the row IN the shell's bar (`titleInTopbar`) the panel
        // covers the whole safe-area height like the App nav drawer it
        // replaces on this route -- the rail's brand mark then sits where
        // that drawer's did. Close: scrim tap, swipe, Back -- all unchanged.
        morph={!isMobile} morphTarget={TOGGLE_RECT} expandFrom={expandFrom} contentH={Math.max(0, containerH - 8)} className={isMobile ? `mobile-sessions-overlay fixed ${titleInTopbar ? 'top-safe' : 'top-safe-offset-[42px]'} bottom-safe left-safe z-50 bg-bg-elevated !py-0 rounded-r-xl shadow-lg [&>*]:!rounded-none [&>*]:!border-0 [&>*]:!m-0` : ''}>
        {/* Phone chat page: ONE drawer holds the shell's navigation rail (72px,
            from MobileNavRailContext) beside the sessions pane, Discord-style, so
            the bar above needs no second drawer trigger. The pane keeps its
            `staticRows` content and takes the drawer's remaining width; the
            drawer's own width, slide, scrim and keyboard inset are unchanged.
            Row taps that stay on this page close the drawer (`closeSidebar`);
            rows that leave it `replace` the duplicate history entry
            `pushDrawerEntry` minted, so Back lands on the chat. */}
        {(() => {
        const sessionsPane = (
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
        )
        return isMobile && mobileNavRail ? (
          <div className="flex h-full min-h-0 w-full" data-testid="mobile-split-drawer">
            {mobileNavRail({ onActivate: closeSidebar })}
            {/* The pane sits on the drawer's elevated surface so it reads apart
                from the rail's `bg-bg-accent` (the sidebar's own shell paints
                `bg-bg`, one step too close to the rail on the light themes). */}
            {/* The drawer's `[&>*]` resets (no card border/radius/margin on the
                sidebar root) reach only its direct child, which is now this
                wrapper -- so the wrapper carries the same resets one level down. */}
            <div className="flex-1 min-w-0 h-full min-h-0 flex flex-col overflow-hidden bg-bg-elevated [&_.sidebar-inner]:!bg-bg-elevated [&_.sidebar-inner]:!w-full [&_.sidebar-inner]:!min-w-0 [&_.sidebar-inner]:!shrink [&>*]:!rounded-none [&>*]:!border-0 [&>*]:!m-0">{sessionsPane}</div>
          </div>
        ) : sessionsPane
        })()}
      </OverlayDrawer>
      )}
      <MobileTopBar
        topbarSlot={topbarSlot}
        topbarTrailSlot={topbarTrailSlot}
        embedMode={embedMode}
        topbarSessionsToggle={topbarSessionsToggle}
        activeSlot={activeSlot}
        splitMode={splitMode}
        splitFeatureEnabled={splitFeatureEnabled}
        title={title}
        editingTitle={editingTitle}
        setEditingTitleSlot={setEditingTitleSlot}
        showActionError={showActionError}
        setActionError={setActionError}
        currentSlot={currentSlot}
        sidebarAutoHidden={sidebarAutoHidden}
        openSidebar={openSidebar}
        menuAutoTitleInFlight={menuAutoTitleInFlight}
        effectiveMode={effectiveMode}
        sidebarOnScreen={sidebarOnScreen}
        activePoppedOut={activePoppedOut}
        focusActivePopout={focusActivePopout}
        openActivePopout={openActivePopout}
        activityOpen={activityOpen}
        toggleAct={toggleAct}
        splitAnchorForActive={splitAnchorForActive}
        activeIsSplitAnchor={activeIsSplitAnchor}
        enterSplit={enterSplit}
        mobileNewSessionItem={mobileNewSessionItem}
        dispatch={dispatch}
      />

      {/* Per-slot tag picker — a single connected popover, opened from any session
          menu (sidebar row or header) via the ChatPage-scoped TagPopover context. */}
      <SlotTagPopover />
      <ComposerDraftSync store={composerDraft} inputRef={inputRef} onCommit={onComposerDraftCommit} />

      {/* Chat pane */}
      {embedMode !== 'sessions' && (
      <div ref={setChatPaneEl} className={`relative flex flex-col bg-bg min-w-0 min-h-0 h-full overflow-hidden ${(activityOpen && !activitySlot) || search.isOpen ? 'flex-[1_1_60%]' : 'flex-1'}`} style={{ transition: 'flex 0.2s', ...(!sidebarOpen && !isMobile ? { marginLeft: -COLLAPSED_PANE_RECLAIM_PX } : {}), '--mc-content-width': scaleContentWidth(CONTENT_WIDTH[chatConfig.contentWidth], chatConfig.contentWidth, chatConfig.messageFontSize).messages, '--mc-input-width': scaleContentWidth(CONTENT_WIDTH[chatConfig.contentWidth], chatConfig.contentWidth, chatConfig.messageFontSize).input } as React.CSSProperties}>
        {snipFrame && (
          <SnipOverlay
            frame={snipFrame}
            onComplete={f => { uploadFiles([f], snipSlotRef.current); setSnipFrame(null) }}
            onCancel={() => setSnipFrame(null)}
            onError={setUploadError}
          />
        )}
        <ChatPaneNotices
          uploadHint={uploadHint}
          setUploadHint={setUploadHint}
          uploadError={uploadError}
          setUploadError={setUploadError}
          sidError={sidError}
          setSidError={setSidError}
          activeSlot={activeSlot}
          provider={provider}
          selectionCapabilitiesQ={selectionCapabilitiesQ}
          chipDefault={chipDefault}
          actionError={actionError}
          setActionError={setActionError}
          switchSlotGone={switchSlotGone}
          setVoiceRecoverySlot={setVoiceRecoverySlot}
          pinError={pinError}
          pinStatus={pinStatus}
          dismissPinStatus={dismissPinStatus}
          unresumableResume={unresumableResume}
          undeletableHistory={undeletableHistory}
          dispatch={dispatch}
        />
        {/* Floating sessions opener — mobile only, and only on a chat with
            nothing in it yet (a conversation gets the in-header control
            instead). Suppressed while the inline side panel is showing: it is
            `fixed` at the same top-left corner as the panel's own collapse
            button and, carrying z-10 against that button's auto z-index, paints
            OVER it — leaving no way to close a panel that covers the whole
            screen. It would also be pointing at a chat pane the panel has
            squeezed to zero width. Sessions stay reachable meanwhile via the
            rightward drag (useDrawerSwipe above).

            Suppressed when EMBEDDED for the same reason it is suppressed
            behind the side panel: `fixed` anchors it to the VIEWPORT, not to
            the host's pane, so it lands on whatever the host put in that
            corner -- in Papyrus, on the toolbar's back button, giving two
            overlapping tap targets on the app's primary exit. A host that
            embeds one scoped conversation has no sessions list to open. */}
        {isMobile && !embedded && !sidebarOpen && !inlineSidePanelShowing && !titleInTopbar && !(activeSlot && (messages.length > 0 || slotRunning)) && (
          <div className="fixed top-safe-offset-[42px] left-safe ml-2 z-10">
            <button className="mc-touch-hit p-2 rounded-lg text-muted hover:text-text bg-bg-elevated border border-border shadow-sm cursor-pointer" onClick={openSidebar} aria-label={i18nT('pages.chatPage.toggle_sessions')}>
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
          //
          // While the desktop sidebar is collapsed the shell insets the strip
          // past the stationary toggle, gliding on the same 240ms curve as the
          // panel morph and the title row so the tabs do not jump at the start
          // of the slide. The shell owns the strip's bottom divider (the strip
          // draws none of its own): an inset border on the strip's root would
          // stop short of the gutter and leave a notch under the toggle, so
          // the shell draws one continuous hairline across the full width.
          // `empty:hidden` keeps the shell out of the layout when the strip
          // renders nothing (fewer than two tabs), so the divider it owns
          // appears only when the strip does.
          <div
            className="empty:hidden border-b border-border transition-[padding-left] duration-[240ms] [transition-timing-function:cubic-bezier(.32,.72,0,1)]"
            style={{
              WebkitAppRegion: 'no-drag',
              paddingLeft: !sidebarOpen && !isMobile ? COLLAPSED_SESSION_TABS_INSET : 0,
            } as React.CSSProperties}
          >
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
            openSideChat={connected ? openSideChatForPane : undefined}
            // The single-chat title row is not rendered in split view, so the
            // grid's top-left pane stands in for it: on desktop it clears the
            // shell's stationary toggle while the sidebar is collapsed (the
            // same columns the title row's 'pl-[60px]' rule reserves, measured
            // from the pane's own edge); on mobile it carries the sessions
            // toggle inline.
            leading={
              isMobile
                ? (embedMode !== 'chat' && !titleInTopbar ? { control: mobileSessionsToggle } : undefined)
                : (embedMode !== 'chat' && embedMode !== 'sessions' && filteredSlots.length > 0 && !sidebarOpen ? { inset: true } : undefined)
            }
            onFileOpen={handleFileOpen}
            onClose={() => setSplitMode(false)}
            onCollapse={(slot, anchorTs, anchorMid) => {
              // User gesture on a session reference (split-pane collapse): the
              // announced class.
              dispatch(switchSlot({ key: slot, announceOnMissing: true }))
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
                dispatch(createSlot({ agent: pendingAgent || defaultAgent || undefined, agent_kind: pendingAgent ? pendingAgentKindRef.current : undefined, model: pendingModel || undefined, mode }))
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
              {/* Phone: the shell's single bar hosts this row (the portal above),
                  so the inline copy stands down. The fold sentinel, the pinned
                  prompt and the header fade stay: they belong to the transcript
                  column, not to the bar. */}
              {!titleInTopbar && (
              <div className={`relative pr-[3px] mt-[3px] h-10 flex items-center gap-2 bg-bg pointer-events-none transition-[padding-left] duration-[240ms] [transition-timing-function:cubic-bezier(.32,.72,0,1)] ${!isMobile && embedMode !== 'chat' && filteredSlots.length > 0 && !sidebarOpen ? 'pl-[60px]' : isMobile ? (embedMode === 'chat' ? 'pl-4' : 'pl-3') : 'pl-5'}`}>
                {/* Divider between toggle and title — ALWAYS mounted and
                    absolute (zero width, no flex-gap participation) so it can
                    never change the row's layout; it rides the row (title
                    side) and only fades. left-[52px] = the collapsed pane's
                    view of container x 44 (button 8+28 + 8px gap). */}
                {!isMobile && embedMode !== 'chat' && filteredSlots.length > 0 && (
                  <span aria-hidden="true" className={`absolute left-[52px] top-[13px] w-px h-5 bg-border transition-opacity ${sidebarOpen ? 'opacity-0 duration-100' : 'opacity-100 duration-150 delay-[90ms]'}`} />
                )}
                {embedMode !== 'chat' && isMobile && mobileSessionsToggle}
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
                  onRename={activeSlot ? () => setEditingTitleSlot(activeSlot) : undefined}
                  mode={effectiveMode}
                  sidebarOnScreen={sidebarOnScreen}
                  newSessionHere={embedMode !== 'chat' && isMobile ? mobileNewSessionItem : undefined}
                />
                </div>
                {/* Shared with every split-view pane header (#9727). The editor
                    flag stays here, pinned to the slot it opened on. */}
                {activeSlot && (
                  <SessionTitleControl
                    slotKey={activeSlot}
                    title={title}
                    editing={editingTitle}
                    onEditingChange={open => setEditingTitleSlot(open ? activeSlot : null)}
                    onError={showActionError}
                    onAttempt={() => setActionError(null)}
                  />
                )}
                </div>
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
              <div data-testid="panel-controls-chat" className={`${PANEL_HEADER_ACTIONS_CLS} ml-auto pointer-events-none${!sidePanelWantsMount || sidePanelDock === 'bottom' ? ' focus-caption-reserve' : ''}`}>
              <div data-testid="panel-controls-session" className={PANEL_HEADER_ACTIONS_CLS}>
              {/* Pop-out control, promoted to the title bar (menu items remain for
                  sidebar parity). Mirrors the split-view pattern to its left: a
                  dimmed icon to act, an accent chip when the state is active.
                  Inside the popout window itself the same spot carries Return. */}
              {popout ? (
                <Clickable className="flex items-center gap-1 text-muted hover:text-text transition-colors cursor-pointer pointer-events-auto text-[11px] font-medium px-1.5 py-0.5 rounded hover:bg-bg-hover" onClick={returnSelfToMain} title={i18nT('pages.chatPage.return_this_session_to_the_main_window')} aria-label={i18nT('pages.chatPage.return_to_main_window')}>
                  <Undo2 size={13} /> {i18nT('pages.chatPage.return')}
                </Clickable>
              ) : !embedMode && activeSlot && (activePoppedOut ? (
                // State chip, not an action cell: keeps its visible label so the
                // session's state reads without hover; height matches the 28px row.
                <Clickable className="flex items-center gap-1 h-7 px-1.5 rounded-md text-[11px] font-medium pointer-events-auto text-accent bg-accent/10 hover:bg-accent/20 transition-colors cursor-pointer" onClick={() => focusActivePopout(activeSlot)} title={i18nT('pages.chatPage.this_session_is_open_in_its_own_window_focus_it')} aria-label={i18nT('pages.chatPage.focus_popped_out_window')}>
                  <ExternalLink className="lucide-inline w-3.5 h-3.5" /> {i18nT('pages.chatPage.popped_out')}
                </Clickable>
              ) : (
                <Clickable className={`${PANEL_HEADER_ACTION_CLS} pointer-events-auto text-muted hover:text-text hover:bg-bg-hover cursor-pointer`} onClick={() => openActivePopout(activeSlot, currentSlot?.title)} title={i18nT('pages.chatPage.pop_out_to_window')} aria-label={i18nT('pages.chatPage.pop_out_session_to_its_own_window')}>
                  <ExternalLink className="lucide-inline w-3.5 h-3.5" />
                </Clickable>
              ))}
              {!embedMode && splitFeatureEnabled && (splitAnchorForActive && !activeIsSplitAnchor ? (
                <Clickable className="flex items-center gap-1 h-7 px-1.5 rounded-md text-[11px] font-medium pointer-events-auto text-accent bg-accent/10 hover:bg-accent/20 transition-colors cursor-pointer" onClick={() => enterSplit(splitAnchorForActive)} title={i18nT('pages.chatPage.this_session_is_open_in_a_split_return_to_it')} aria-label={i18nT('pages.chatPage.return_to_split_view')}>
                  <Columns2 className="lucide-inline w-3.5 h-3.5" /> {i18nT('pages.chatPage.in_split')}
                </Clickable>
              ) : (
                <Clickable className={`${PANEL_HEADER_ACTION_CLS} pointer-events-auto text-muted opacity-40 hover:opacity-100 hover:text-text hover:bg-bg-hover cursor-pointer`} onClick={() => enterSplit(activeSlot)} title={i18nT('pages.chatPage.split_view_d')} aria-label={i18nT('pages.chatPage.enter_split_view')}>
                  <Columns2 className="lucide-inline w-3.5 h-3.5" />
                </Clickable>
              ))}
              </div>
              {!embedMode && !popout && (!activityOpen || search.isOpen) && (
                // Session-window actions and workspace visibility are separate
                // two-action regions. The inset hairline preserves that category
                // boundary without moving the trailing Side-panel toggle.
                <div data-testid="panel-controls-workspace-visibility" className={`${PANEL_HEADER_ACTIONS_CLS} border-l border-border pl-2`}>
                  <PanelToggles workspaceOpen={false} />
                </div>
              )}
              </div>
              {/* Header fade — softens content passing up into the opaque title
                  row, so it hangs off that row's bottom edge (anchor="below":
                  as an in-flow sibling its 24px consumed layout and pushed the
                  pinned card that far off the header; out of flow it overlays
                  the transcript and the pinned card paints above it). */}
              <EdgeFade side="top" anchor="below" />
              </div>
              )}
              {titleInTopbar && <EdgeFade side="top" anchor="below" />}
              {/* Fold sentinel — zero-height, always mounted. Its top edge is the
                  line the pinned prompt sticks to (see updatePinnedPrompt). */}
              <div ref={pinFoldRef} aria-hidden className="h-0" />
              {pinned && (
                <PinnedPrompt
                  text={pinned.text}
                  fullText={pinned.full}
                  images={pinned.images}
                  bodyBeyondPreview={pinned.bodyBeyondPreview}
                  pushUp={pinned.push}
                  liveH={pinned.liveH}
                  maxH={pinned.maxH}
                  bannerH={pinned.bannerH}
                  expanded={pinExpanded}
                  onToggleExpanded={() => setPinExpanded(p => !p)}
                  onJump={() => scrollToPinnedPrompt(pinned.idx)}
                  cardRef={pinCardRef}
                  onCollapsedHeight={onPinCollapsedHeight}
                  promptKey={pinCandidateKey(pinned.idx, pinned.ts)}
                  scrollTranscriptBy={scrollTranscriptBy}
                />
              )}
            </div>
            <ChatDropOverlay active={dragOver} />
            {isWelcomeState ? (
              <motion.div
                key="welcome-hero"
                // `position`, not a full `layout`: the box now changes SIZE on
                // every dock resize (`marginBottom: dockH` below), and a full
                // layout animation would scale the greeting and cards through
                // each change -- the 0 -> dockH first measurement included --
                // instead of moving them. Position still animates.
                layout="position"
                // `isolate`: the hero is its own stacking context. WelcomeView
                // layers its cards with z-indexes of its own (a hovered card
                // grows over the next row at z-10, the Refresh link sits above
                // that at z-20). The dock root deliberately has no z-index, and
                // the composer inside it is z-10, so without a context here those
                // values compared against the composer's and the Refresh link
                // painted ON TOP of the input box. Inside its own context the
                // hero's z-indexes order only the cards against each other.
                // `safe center`: when the welcome column is taller than this
                // box (a phone, a short window), plain centring pushes its top
                // out of scroll reach; safe centring falls back to the start.
                className="isolate flex-1 flex flex-col items-center [justify-content:safe_center] gap-6 px-8 min-h-0 overflow-y-auto"
                // The hero's BOX ends above the dock (a margin, not padding
                // under it): the cards and the Refresh link are controls with
                // labels, and a label blurred under the glass reads as a glitch
                // and a tap there is ambiguous (maintainer ruling on #15820). A
                // column taller than the box scrolls inside it, never under the
                // dock; the memory-mode chip is part of the dock and so sits
                // below the box too.
                style={{ marginBottom: dockH }}
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                exit={{ opacity: 0 }}
                transition={{ duration: 0.18 }}
              >
                <WelcomeView setInput={setInput} />
              </motion.div>
            ) : (
            <>
            <TurnNavigationMinimap
              items={chatNav.sections}
              scrollerRef={scrollerRef}
              onNavigate={navigateToTurn}
              // The rail maps loaded turns only; while the server holds older
              // rows its labels say "of N loaded" (#8221's disclosure).
              windowed={slotHasMore && cursorIsForActiveSlot}
              side={chatConfig.minimapSide}
            />
            <TranscriptScrollShell
              // The 64px spacer clears the inline title row; with the row in
              // the shell's bar (phone) the transcript starts at the top of the
              // column and the spacer would be a blank band under the bar.
              headerSpacer={!titleInTopbar}
              scrollerRef={scrollerRef}
              onScrollerElement={bindScroller}
              onScroll={onScrollPin}
              virt={virt}
              loadingOlder={loadingOlder}
              spinnerNearTop={spinnerNearTop}
              // The strip of the scroller the floating dock covers, plus
              // DOCK_CLEARANCE_PX, alongside TRANSCRIPT_TAIL_SPACER_PX. Unlike
              // the tail spacer this one also applies to a transcript short
              // enough not to scroll, so both are needed for the last line to
              // clear the dock in every state.
              // `visibility` is not one of the properties the shell claims, so
              // adding it here is inside its documented contract. Hiding rather
              // than unmounting keeps the scroller's geometry and the height
              // cache intact -- the restore needs to WRITE scrollTop while this
              // is up, which a display:none element cannot do.
              scrollerStyle={{ paddingBottom: dockH + DOCK_CLEARANCE_PX, ...(virt.restoreGate ? { visibility: 'hidden' as const } : null) }}
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
              <ChatFooter running={slotRunning} stopping={slotStopping} state={slotState} lastRole={lastRole} streamTick={streamTick} regenerating={regenerating} stopState={currentSlot?.stop_state} sendUnconfirmed={sendUnconfirmed} />
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
                  transcript and the top of the floating composer dock, so
                  expressing it in `vh` made that clearance viewport-dependent: at
                  2vh + 8px it cleared by 1px at 844px tall and cut INTO the last
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
                // This row's bubble is the one the pinned banner is standing in for
                // (identity rule below): hide the row and mark it for index.css. The
                // marker reads `folding` while the hook reports the row's re-shown
                // action strip still UNCOVERED — some of it below the card's resting
                // bottom, on screen. That is the whole fold and the strip's own
                // height of scroll after it: the card rests at its clamp while the
                // strip, hanging under the bubble, is still sliding under it, so a
                // marker keyed on the fold alone dropped the strip out from under
                // the pointer for that last stretch. Once the strip is under the card
                // or behind the header the marker is empty and the strip hides with
                // its row again, or Tab would stop on controls nobody can see.
                const pinnedStandin = !!(pinned && (pinned.ts != null
                  ? (item.kind === 'single' && item.msg.ts === pinned.ts)
                  : pinned.idx === displayIdx))
                return <div key={vi.key} ref={virt.measureRef(vi.index)} data-display-index={displayIdx} className={`px-4 mx-auto w-full py-1`} data-pinned-standin={pinnedStandin ? (pinned?.stripUncovered ? 'folding' : '') : undefined} style={{
                  maxWidth: 'var(--mc-content-width, 900px)',
                  // The pinned banner is styled as this row's own bubble and sits
                  // at the exact position and width the bubble had when its top
                  // edge reached the fold, so leaving both visible is what betrays
                  // them as two containers. Hide the real one (visibility, NOT
                  // display — the virtualizer must keep measuring its height or
                  // the transcript would reflow under the reader) and the bubble
                  // appears to simply stop travelling and stick. The row hides the
                  // moment its top crosses the fold (the top-edge hand-off), and
                  // the card then folds down the BUBBLE's remaining height, so the
                  // row's action strip beneath the bubble is never behind the card
                  // — `data-pinned-standin` lets index.css re-show that strip
                  // (visibility is inherited, so a `visible` descendant of a hidden
                  // row is drawn and clickable). Without it a prompt taller than
                  // the viewport never shows its copy / copy-link / pin row at all:
                  // its bottom is only on screen once its top is above the fold.
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
                  visibility: pinnedStandin ? 'hidden' : undefined,
                }}>{item.kind === 'group' ? (() => {
                const unresolvedGroupPerms = item.msgs.filter(m => m.role === 'permission' && !m.meta?.resolved)
                if (item.msgs.every(m => m.role === 'permission')) return null
                return (
                <CollapsibleToolGroup
                  count={item.msgs.filter(m => m.role !== 'permission').length}
                  disclosureKey={`ctg-${vi.key}`}
                  hasPermission={false}
                  isRunning={slotRunning && displayIdx === renderedDisplayItems.length - 1}
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
            </>
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
            {/* Composer dock. Floats over the bottom of the transcript scroller
                instead of sitting under it in the flex column, so the scroller
                runs the full height of the pane and the conversation scrolls
                UNDER the glass (iOS toolbar layout). The scroller pays for the
                covered strip with `paddingBottom: dockH + DOCK_CLEARANCE_PX`,
                measured from this box by `dockRef`. No z-index here on purpose:
                a positioned box with `z-index: auto` forms no stacking context,
                so SubagentProgressBar's wave chip keeps its `z-[46]` against
                the theme-experience overlays it was lifted to clear, and the
                dock still paints over the scroller because it follows it in
                DOM order. `pointer-events-none` on the root, restored on the
                CHILDREN of the two wrapper boxes (`dock-inert`, index.css) —
                each child is its own 900px column, so the two wrappers, which
                span the pane, never catch a wheel of their own — then the empty
                width either side of the column lets wheel and touch reach the
                transcript underneath, as the strip beside an iOS toolbar does.
                `right: dockGutter` keeps the scrollbar column clear (above). */}
            <div ref={dockRef} className="absolute left-0 bottom-0 pointer-events-none" style={{ right: dockGutter }} data-testid="composer-dock-root">
              <JumpToBottomButton visible={!isAtBottom && messages.length > 0} onClick={() => scrollBottom(true)} />
              {/* Status chrome never claims more than half the pane. The dock
                  is anchored to the pane's bottom edge and grows upward, so an
                  opening keyboard — which shrinks the layout viewport — would
                  let an uncapped stack rise into the title band at the top of
                  the pane and cover the rename editor. Capping makes the stack
                  yield first.
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
              <div ref={composerBandRef} className="max-h-[50svh] overflow-y-auto overscroll-contain scrollbar-overlay pb-[11px] mb-[-11px] dock-inert" data-testid="composer-status-stack">
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
              {openCommandCenter && <CommandCenterDock slot={activeSlot} onOpen={openCommandCenter} />}
              <SubagentDeliveryProgress count={systemDeliveryCount} />
              <QueueStack messages={queuedMessages} onCancel={handleCancelQueued} onInterrupt={handleInterruptQueued} onEdit={handleEditQueued} onReorder={handleReorderQueued} pendingIds={queuePendingIds} fuseBelow={followUpOptions.length === 0 && !knowledgeFetch.pendingKnowledge} />
              </div>
              {flyingQuote && <FlyingQuote text={flyingQuote.text} from={flyingQuote.from} targetRef={inputAreaRef} onComplete={endQuoteFlight} />}
              <div ref={inputAreaRef} className="relative z-10 dock-inert">
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
                    // Hand-off on. The composer beneath holds a live draft, but it
                    // is persisted per slot on every keystroke and on slot switch
                    // (the setDraft effects above), and an in-chat hand-off opens
                    // a FRESH slot without navigating away — so the draft survives.
                    askAgent
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
              {knowledgeFetch.results.length > 0 || knowledgeFetch.loading || knowledgeFetch.error ? (
                <KnowledgePicker
                  results={knowledgeFetch.results}
                  query={knowledgeFetch.query}
                  loading={knowledgeFetch.loading}
                  error={knowledgeFetch.error}
                  onRetry={knowledgeFetch.retrySearch}
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
                    onFallbackSend={keepQuestionAnswer}
                    onDirectSend={answerQuestionCard}
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
                    onSkip={(index) => dispatch(dismissFollowupItem({ slot: activeSlot, index, ts: pendingFollowup.ts }))}
                  />
                </div>
              )}
              {showComposerMemoryChip && (
                // No backdrop: the chip is glass and whatever scrolls under it is meant to show.
                // `w-fit`, not a full-width flex row: the box is a `dock-inert` child, so
                // it catches input, and it should be no wider than the chip it holds.
                <div className="relative z-10 mx-auto w-fit px-4 pt-2 pb-2" data-testid="composer-memory-chip">
                  <MemoryModeChip memoryMode={currentSlot?.memory_mode ?? 'persistent'} onSwitchMode={switchMemoryMode} />
                </div>
              )}
              <Composer
                ref={composerRef}
                slotKey={activeSlot}
                draft={composerDraft}
                onChange={composerRootChange}
                voice={composerVoiceOptions}
              >
              <StableChatInput
              aboveComposer={composerAbove}
              // No `value`: ChatInput reads the text from the root's `draft` store.
              // `composerUserEdit` is what ChatInput calls for the user's own edits
              // (typing, paste, undo, picker inserts), never for a parent-driven
              // seed -- so it is the signal that arms the prefill hint's expiry.
              onChange={composerUserEdit}
              onSend={() => send()}
              canSteer={composerBusy}
              onSteer={steer}
              // AND a turn actually running. `composerBusy` is also true when only
              // background sub-agents are working, and there is no turn to decide
              // ABOUT in that state: the send starts a fresh turn and the point
              // never runs, so offering the mode there would promise a decision
              // nothing makes.
              jevAutoAvailable={jevAutoConsented && !!slotRunning}
              onFollowUpSend={(text?: string) => send(text)}
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
              /* Only while a real upload is abortable. `uploading` is shared
                 with the screenshot path, which has no request to cancel. */
              onCancelUpload={uploadCancellable ? cancelUpload : undefined}
              /* The one collapsible composer. Opt-in rather than default so the
                 shared preference key and the window-level expand event stay
                 correct by construction -- see ChatInput's `collapsible` prop. */
              collapsible
              uploading={uploading}
              pendingFiles={pendingFiles}
              pendingDirs={pendingDirs}
              resizedInfo={resizedInfo}
              onRemoveFile={removeFileChip}
              onRemoveDir={removeDirChip}
              pendingSessions={pendingSessions}
              onRemoveSessionRef={unstageSessionRef}
              onFileSelect={selectPickedFile}
              onFileOpen={handleFileOpen}
              // A tree row dropped on the composer is "Add to chat" by drag.
              onTreeEntryDrop={handleAddToContext}
              clampDropOffset={clampOutOfTokens}
              project={currentSlot?.project || ''}
              projectBranch={projectBranch}
              projectDetached={!projectGitError && !!projectGit?.detached}
              isMac={isMac}
              onDrop={dropTargetProps.onDrop}
              onDragOver={dropTargetProps.onDragOver}
              onDragLeave={dropTargetProps.onDragLeave}
              agentName={activeAgentName}
              // The chip shows the inherited-default marker; `agentName` stays
              // the raw resolved alias for the skills query and switch title.
              // Uses the SLOT's stored agent (not `activeAgentName`, which has
              // already collapsed empty->default) so an agent-less slot reads
              // `<default> · default` and a pinned one reads the bare alias (#8770).
              agentLabel={agentOrDefaultLabel(currentSlot?.agent, effectiveDefaultAgent)}
              agentIsInheritedDefault={!currentSlot?.agent && !!effectiveDefaultAgent}
              agentSource={effectiveAgents.find(a => a.name === activeAgentName)?.source}
              modelName={shownModel}
              modelIsInheritedDefault={modelMarker === 'default'}
              modelIsAutoChosen={modelMarker === 'auto'}
              // The turn's model is Jev's to pick exactly when the routing gate
              // says so: the slot names no model, and the preview is on. Reads the
              // slot's RAW model, not `shownModel` -- that one substitutes the
              // served id for an inheriting slot, so it is almost never `auto` and
              // would hide every routed turn. Same `jevRouteOn` the picker's row is
              // drawn from, so chip, menu and gate cannot disagree.
              modelIsJevRouted={jevRouteOn && isUnpinnedModel(currentSlot?.model)}
              onAgentClick={provider.capabilities.agentTemplates ? (rect, trigger) => { anchorAgentBtn(rect, trigger); setAgentDropdown(!agentDropdown) } : undefined}
              onModelClick={(rect, trigger, composerHadFocus) => {
                modelPickerReturnsFocusRef.current = !!composerHadFocus
                anchorModelBtn(rect, trigger); setModelDropdown(!modelDropdown)
              }}
              onProjectClick={(rect, trigger) => {
                anchorProjectBtn(rect, trigger)
                setProjectPickerOpen(o => !o)
              }}
              sessionControls={composerSessionControls}
              onSessionControlClick={(key, rect, trigger) => {
                anchorSessionControl(rect, trigger)
                // Two independent setState calls, not one updater with a side
                // effect: React may run an updater twice (StrictMode does in
                // dev), which would bump the refresh token twice per toggle and
                // fire a redundant status poll.
                if (openSessionControl?.key === key) {
                  setOpenSessionControl(null)
                  refreshSessionControlStatuses()
                } else {
                  // Capture the slot the control is opened in: the host render
                  // is gated on it still matching activeSlot, so a chat switch
                  // can never mount the control against the next session.
                  setOpenSessionControl({ key, slot: activeSlot })
                }
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
                 louder button here.

                 `featureRequestRefused` is the one case where the card and the
                 composer would otherwise disagree (#13342): the newest row is
                 the plan's refusal of a feature request, the card has withheld
                 Resume because a retry replays that rejection and offered the
                 issue form instead, and a composer beneath it saying "press
                 Resume" would argue with the card. The composer falls back to
                 the ordinary Send button; typing still works.
                 `sessionStartRepeated` is the same rule for a session start
                 that failed twice in a row: the card names the remedy and
                 withholds Resume, so the composer falls back to Send. */
              continuable={continuable && interrupted && !featureRequestRefused && !sessionStartRepeated}
              continueIsRecovery={interrupted}
              onContinue={handleContinue}
              continuing={continuing}
              onStop={stopTurn}
              isQueued={slotStopping}
              stopState={currentSlot?.stop_state}
              compacting={!!currentSlot?.compacting}
              stopDeclined={!!currentSlot?.stop_declined}
              approvalMode={displayMode}
              providerId={provider.id}
              reasoningEffort={effectiveEffort}
              effortIsDefault={!currentSlot?.reasoning_effort && !legacyCodexEffort(currentSlot?.model || '', '', codexPairModels) && !!defaultEffort}
              // Effort is edited INSIDE the model picker (one control, see
              // docs/decisions/2026-06-14-chat-composer-model-and-effort-are-one-control.md);
              // the chip only names the level in force.
              hasEffort={effortSupported}
              onAutomationClick={setAutomationOpen}
              automation={automation}
              automationOpen={automationOpen}
              automationCreationReady={automationCreationReady}
              automationSnapshotFailed={automationSnapshotFailed}
              sessionMode={currentSlot?.mode || mode}
              onAutomationChange={applyAutomationChange}
              onOptimizeResult={handleOptimizeResult}
              memoryMode={currentSlot?.memory_mode ?? 'persistent'}
              sentMessages={sentMessages}
              onEditLastRequest={handleEditLastRequest}
              sendOnEnter={isMobile ? 'ctrl-enter' : chatConfig.sendOnEnter}
              followUpOptions={followUpOptions}
              followUpPicked={followUpPicked}
              quickSend={dashCfg?.quick_send}
              followUpLayout={chatConfig.followUpLayout}
              followUpSourceKey={followUpSourceKey}
              onFollowUpSelect={(o: string, e: React.MouseEvent) => {
                // One-click: enabled + no shift + not busy + not already in multi-select
                if (tryQuickSend(o, dashCfg?.quick_send, e.shiftKey, slotRunning, followUpPickedRef.current.size, send)) return
                toggleFollowUpOption(o)
              }}
              pasteBlocks={pasteBlocks}
              onPasteBlocksChange={setPasteBlocks}
              showFullPastes={chatConfig.showFullPastes}
              knowledgeChip={knowledgeFetch.pendingKnowledge ? <div className="flex items-start gap-1"><KnowledgeBubbleChip knowledge={{ items: knowledgeFetch.pendingKnowledge.items.length, tokens: knowledgeFetch.pendingKnowledge.totalTokens, titles: knowledgeFetch.pendingKnowledge.items.map(i => i.title), content: knowledgeFetch.pendingKnowledge.items.map(i => ({ title: i.title, text: i.content.slice(0, 2000) })) }} /><button type="button" onClick={() => knowledgeFetch.clearPending()} className="shrink-0 mt-0.5 p-0.5 text-muted hover:text-danger bg-transparent border-none cursor-pointer rounded hover:bg-danger/10 transition-colors" aria-label={i18nT('pages.chatPage.remove_knowledge_context')} title={i18nT('pages.chatPage.remove_knowledge_context')}>&times;</button></div> : undefined}
              connected={connected}
            />
              </Composer>
            </div>
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
                <AgentDropdownList agents={filteredAgents} activeAgent={activeAgentName} activeKind={currentSlot?.agent_kind} defaultAgent={defaultAgent} onSelect={(name, kind) => { switchAgent(name, kind); setAgentDropdown(false) }} filter={agentFilter} />
                </div>
                {/* Embedded chat gets neither half of the default-agent affordance: it has
                    no /capabilities route for the footer, and the footer is what carries the
                    failed-write alert — offering the write without its error path would make
                    a rejected request indistinguishable from a successful one. */}
                {!embedded && <DefaultAgentRow agentName={activeAgentName} isDefault={activeAgentName === defaultAgent} onSetDefault={() => toggleDefaultAgent(activeAgentName)} />}
                {!embedded && <ManageAgentsFooter error={defaultAgentFailed} onManage={() => { setAgentDropdown(false); navigate('/capabilities?tab=crews') }} />}
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
                activeModel={jevRouteShownModel(shownModel, currentSlot)}
                onSelectModel={pickModel}
                modelsLoading={remoteCrew.modelsPending}
                modelsFailed={remoteCrew.failed}
                retryingModels={remoteCrew.retrying}
                onRetryModels={() => remoteCrew.refetch()}
                filter={modelFilter}
                setFilter={setModelFilter}
                onClose={() => setModelDropdown(false)}
                modelVisibilityError={hiddenModelsQ.isError}
                onRetryModelVisibility={() => hiddenModelsQ.refetch()}
                // The embedded effort slider is fed by the slot's ACP
                // capability read (#13637): the agent's own answer wins over
                // the static model-name heuristic, and its level list replaces
                // this machine's when it reports one.
                hasEffort={!!(activeSlot && effortSupported)}
                slot={activeSlot}
                currentEffort={currentSlot?.reasoning_effort || legacyCodexEffort(currentSlot?.model || '', '', codexPairModels)}
                defaultEffort={defaultEffort}
                effortLevelsOverride={effortLevelsOverride}
                onManageModels={modelPickerConfigured ? undefined : () => {
                  setModelDropdown(false)
                  navigate(settingsPath({ tab: 'chat', sub: 'models', highlight: 'key:dashboard.model_picker_hidden_models' }))
                }}
                onSetDefault={() => {
                  setModelDropdown(false)
                  navigate(settingsPath({ tab: 'chat', sub: 'models', highlight: SETTINGS_DEFAULT_MODEL_ID }))
                }}
                agentName={_modelPinAgent}
                pinModelName={_modelPinActive || 'auto'}
                pinModelUnavailable={pinIsWithheld(_modelPinActive, _pinShownModel)}
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
              errorHandoff
            />
            {/* App-contributed session control popover — triggered from input bar.
                Only the open one is mounted, so an app's control costs nothing
                while closed. Render is gated on the slot the control was opened
                in: on a chat switch the committed render where activeSlot has
                already moved would otherwise mount a fresh control (the host
                keys on session) bound to the NEW chat, and a control that
                writes per-session state on mount would write to the wrong
                chat. The clearing effect above runs too late to prevent that
                render — this gate is the correctness guard. */}
            {(() => {
              const sc =
                openSessionControl && openSessionControl.slot === activeSlot
                  ? sessionControls.find(c => c.key === openSessionControl.key)
                  : null
              if (!sc) return null
              return (
                <SessionControlHost
                  /* Keyed on the control, and load-bearing: the host's error
                     boundary holds `state.error`, and nothing clears it on a
                     prop change. Unkeyed, React reuses the one instance across
                     controls, so opening B after A crashed would show B the
                     stale error and never mount it. The key makes switching
                     controls a remount, which is the only thing that resets the
                     boundary. Do not remove it. */
                  key={sc.key}
                  control={sc}
                  anchorRect={sessionControlRect}
                  onClose={() => {
                    setOpenSessionControl(null)
                    refreshSessionControlStatuses()
                  }}
                  session={{
                    sessionKey: sessionControlKey,
                    folderId: currentSlot?.folder_id || '',
                    folderName: activeFolderName,
                    cwd: currentSlot?.project || '',
                  }}
                />
              )
            })()}
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
            grid column. Desktop uses the actbar portal below.

            MOBILE is a fixed overlay sliding in from the RIGHT on the
            compositor (sideOverlayX / animateDrawer), mirroring the sessions
            drawer: the old `width: 0 → auto` reveal is a LAYOUT animation, so
            it re-laid-out the panel AND the squeezed chat pane every frame.
            Mount is held through 'closing' so the slide-out is not cut short.
            Embed frames keep the width reveal — they have no overlay chrome. */}
        {(isMobile
          // `hasLiveAppTab` keeps a closed-but-alive panel MOUNTED (display:none
          // below) so its iframe — and the drawing inside it — survives the
          // close, exactly as the width-reveal branch always did.
          ? (sideOverlayPhase !== 'closed'
              || (shouldMountSidePanel({ activityOpen, hasLiveAppTab, hasBrowserTab, hasTaskDashboard, searchOpen: search.isOpen })
                  && isSidePanelHidden({ activityOpen, hasLiveAppTab, hasBrowserTab, hasTaskDashboard, searchOpen: search.isOpen }))) && !activitySlot
          : shouldMountSidePanel({ activityOpen, hasLiveAppTab, hasBrowserTab, hasTaskDashboard, searchOpen: search.isOpen }) && !activitySlot) && (
          <motion.div
            key="side-panel-inline"
            layout="position"
            layoutDependency={workspaceFullscreen}
            data-workspace-panel-host
            ref={isMobile ? sideOverlayPanelRef : undefined}
            initial={isMobile ? false : { width: 0 }}
            animate={isMobile ? undefined : { width: 'auto' }}
            exit={isMobile ? undefined : { width: 0 }}
            transition={isMobile ? undefined : { duration: 0.4, ease: [0.32, 0.72, 0, 1] }}
            // Below the 42px app topbar, like the sessions drawer — the panel
            // takes over the CONTENT area, not the shell chrome.
            className={isMobile
              ? 'fixed top-safe-offset-[42px] bottom-safe left-safe right-safe z-[47] flex justify-end bg-bg'
              : 'h-full overflow-hidden flex justify-end shrink-0'}
            // Kept mounted for a live app tab: hide instead of unmounting so the
            // iframe (and the drawing inside it) survives a panel close. On
            // mobile the overlay phase already owns mount/hide timing, and the
            // offset is BOUND (not a one-shot read): a drag writes the value
            // directly, so only a live binding paints those frames — the
            // compositor settle still runs through the registered element above.
            style={isMobile
              ? (sideOverlayPhase === 'closed'
                  ? { display: 'none' } // keep-alive: mounted for the iframe, invisible
                  : { x: sideOverlayX })
              : (isSidePanelHidden({ activityOpen, hasLiveAppTab, hasBrowserTab, hasTaskDashboard, searchOpen: search.isOpen }) ? { display: 'none' } : undefined)}
          >
            <SidePanel
              tabsCtl={tabsCtl}
              slot={activeSlot || ''}
              panelHidden={isSidePanelHidden({ activityOpen, hasLiveAppTab, hasBrowserTab, hasTaskDashboard, searchOpen: search.isOpen })}
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
              extraReserveW={!isMobile && sidebarOpen ? effectiveSidebarWidth : 0}
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
          {shouldMountSidePanel({ activityOpen, hasLiveAppTab, hasBrowserTab, hasTaskDashboard, searchOpen: search.isOpen }) && (
            <motion.div
              key="side-panel"
              data-workspace-panel-host
              initial={sidePanelDockAnim.initial}
              animate={workspaceFullscreen
                ? { ...sidePanelDockAnim.animate, width: '100%', height: '100%' }
                : sidePanelDockAnim.animate}
              exit={sidePanelDockAnim.exit}
              transition={SIDE_PANEL_DOCK_TRANSITION}
              className={sidePanelDock === 'bottom' ? 'w-full overflow-visible flex flex-col justify-end' : 'h-full overflow-visible flex justify-end'}
              style={isSidePanelHidden({ activityOpen, hasLiveAppTab, hasBrowserTab, hasTaskDashboard, searchOpen: search.isOpen }) ? { display: 'none' } : undefined}
            >
              <SidePanel
                tabsCtl={tabsCtl}
                slot={activeSlot || ''}
                panelHidden={isSidePanelHidden({ activityOpen, hasLiveAppTab, hasBrowserTab, hasTaskDashboard, searchOpen: search.isOpen })}
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
                extraReserveW={!isMobile && sidebarOpen ? effectiveSidebarWidth : 0}
              />
            </motion.div>
          )}
        </AnimatePresence>,
        activitySlot
      )}
    </div>
    </SidebarFolderCtx.Provider>
    </JiraHostsCtx.Provider>
    </TagPopoverProvider>
    </RowDisclosureProvider>
  )
}
