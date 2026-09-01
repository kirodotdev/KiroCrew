import { useState, useRef, useCallback, useEffect, useMemo } from 'react'
import { useLocation, useNavigate, useNavigationType, useSearchParams } from 'react-router-dom'
import { useQuery, useQueries, useMutation, useQueryClient } from '@tanstack/react-query'
import { useModelsDegraded } from '../providers/modelListHealth'
import { useIsMobile } from '../hooks/useIsMobile'
import { useVisualViewport } from '../hooks/useVisualViewport'
import { useImeGuard } from '../hooks/useImeGuard'
import { useRailWidth } from '../hooks/useRailWidth'
import { isTouchDevice } from '../utils/isTouchDevice'
import { isHiddenInvisibleAssistantRow } from '../utils/invisibleText'
import { devLog, devWatchMessages, inspectorOn } from '../dev/scrollInspector'
import { isBrowseCommand } from '../utils/browseCommand'
// Re-exported so the symbol `ChatPage` exported before this extraction stays
// importable from here; the implementation lives in `utils/browseCommand` so a
// pure test need not pull ChatPage's module graph.
export { isBrowseCommand }
import { useDrawerSwipe, animateDrawer, registerDrawerTargets, takeOverDrawer, safeAreaLeft } from '../hooks/useDrawerSwipe'
import { useAppSelector, useAppDispatch, store } from '../store'
import { useConnected } from '../hooks/useConnected'
import { useChatPopouts } from '../hooks/useChatPopouts'
import { claimAppAutoOpen, openPanelView } from '../hooks/usePanelTabs'
import {
  switchSlot, createSlot,
  forkSlot,
  syncSlotRunningFromServer,
  selectComposerBusy,
  setVoiceAudio,
  toggleActivity, openActivityPanel, openActivityToTab,
  selectSubagent,
  pendingQuestionFor,
  mcpAppKey,
} from '../store/chatSlice'
import { addNotification } from '../store/notificationsSlice'
import { onTerminalReady, sendToTerminalSession } from '../utils/terminalRegistry'
import { addTab as addDockTerminal } from '../hooks/useBottomTerminal'
import { triggerRefresh } from '../store/dashboardSlice'
import { api } from '../api/client'
import type { PlanStepInput } from '../api/client'
import { useProvider } from '../providers'
import { fileReadUrl } from '../utils/fileReadUrl'
import { safeSetItem } from '../utils/safeStorage'
import { addPendingFile, hasExactRelMention, normalizeWindowsPath, parseDirTokens, spliceDirTokens } from '../utils/fileTokens'
import { makeRelative } from '../components/FilePickerMenu'
/** Delay (ms) before scrolling to bottom after a state update, giving React time to commit. */
const SCROLL_AFTER_RENDER_MS = 100

// Canonical home is utils/navIntent (shared with the popout nav-intent
// applier); re-exported here for this page's historical importers.
export { PREFILL_STORAGE_KEY } from '../utils/navIntent'
import { anchorForSlot, loadLayout, sessionSlots } from '../hooks/splitLayoutStore'
import { countCompletedTurns } from '../lib/completedTurns'
import { displayModel } from '../lib/model'
import { runBelongsToSlot } from '../apps/workflows/runModel'
import { useTipTrigger } from '../components/TipCard'
import { CHAT_PANE_MIN_W, sidePanelFillWidth } from './chat/SidePanel'
import { useSidePanelDock } from '../hooks/useSidePanelDock'
import { setSessionPreviewPending, normalizeUrl, PREVIEW_EXPAND_EVENT } from '../components/WebPreviewPanel'
import { detectPreviewUrl, previewFeedDecision } from '../utils/detectPreviewUrl'
import { SIDEBAR_MIN, SIDEBAR_MAX, clampSidebarWidth } from './chat/sidebarWidth'
import {
  commitRevealedSource,
  parseSourceLinkUrl,
  type SourceLinkKind,
} from '../utils/pullRequestLinks'
import { loadChatConfig } from './chat/ChatSettings'
import { focusComposerAfter, revealComposer } from './chat/composerFocus'
import {
  uniqueNotificationTs,
  useChatPageComposerController,
} from './chat/useChatPageComposerController'
import { useChatPageActionsController } from './chat/useChatPageActionsController'
import { useChatPageResourcesController } from './chat/useChatPageResourcesController'
import { useChatPageSessionController } from './chat/useChatPageSessionController'
import {
  useChatPageTranscriptController,
  useChatPageTranscriptEarlyController,
} from './chat/useChatPageTranscriptController'
import ChatPageView, {
  DRAWER_UNCOVERED_PX,
  type ChatPageCoreViewModel,
  type ChatPageLayoutViewModel,
  type ChatPageViewPorts,
} from './chat/ChatPageView'
import { useHoverIntent } from '../hooks/useHoverIntent'
import { useKnowledgeFetch } from './chat/useKnowledgeFetch'
import { useMotionValue, useTransform } from 'framer-motion'

import { shouldMountSidePanel, isSidePanelHidden, sidePanelDockMotion } from './chat/sidePanelMount'
import type { ParsedSubagentCompletion } from './chat/subagentCompletion'
import { useConnectionsUiEnabled } from '../hooks/useConnectionsUi'
import { isChatPageSurface } from '../utils/channelOrigin'
import { errMessage } from '../utils/thunkError'


import { i18nT } from '../i18n/t'


/** Stable empty set so the mcpApps-derived selector returns a referentially
 *  equal value when the slot has no app renders (avoids useless re-renders). */
const EMPTY_APP_ID_SET: ReadonlySet<string> = new Set()

export default function ChatPage({ mode, embedded, embedMode, popout, noUrlSync }: { mode?: string; embedded?: boolean; embedMode?: 'chat' | 'sessions'; popout?: boolean; noUrlSync?: boolean } = {}) {
  const dispatch = useAppDispatch()
  const navigate = useNavigate()
  const navigationType = useNavigationType()
  const location = useLocation()
  const queryClient = useQueryClient()
  const provider = useProvider()
  const [searchParams, setSearchParams] = useSearchParams()
  // Declared with the other top-of-component hooks because the ?sid= URL-sync
  // effect reads it (mobile replaces rather than pushes a session switch), and
  // that effect is defined well above where the layout hooks start.
  const isMobile = useIsMobile()
  // The mobile sessions drawer and its scrim are `fixed` overlays that autofocus
  // a search input, so a software keyboard is open whenever they are. iOS Safari
  // shrinks only the VISUAL viewport for the keyboard (`interactive-widget`
  // default `resizes-visual`), so `fixed inset-0` / `top-safe`/`bottom-safe`
  // insets keep measuring the full layout viewport and strand the drawer's lower
  // content behind the keyboard. Inset both boxes to the visible band instead —
  // only on mobile; desktop morph mode is untouched.
  //
  // Read on the page, not in the view: this is the drawer's geometry and the page
  // already owns the drawer (the mint/spend pair and the phase machine), while
  // ChatPageView declares no hooks of its own. The two reduced values travel to
  // the boxes as layout view-model fields.
  const vv = useVisualViewport()
  // Height of the band an interactive widget covers at the bottom of the fixed
  // positioning viewport — the same expression SidePanelLayout derives its own
  // keyboard inset from. 0 on desktop and whenever no keyboard is up, so every
  // consumer below is a strict no-op at rest.
  const keyboardInset =
    typeof window === 'undefined' ? 0 : Math.max(0, window.innerHeight - vv.offsetTop - vv.height)
  const slots = useAppSelector(s => s.dashboard.slots)
  // Unified chat view: show default, orchestrator and crew slots together.
  // App-owned worker slots (s.app) are excluded by the sidebar itself.
  const filteredSlots = useMemo(
    () => slots.filter(s => isChatPageSurface(s.surface ?? s.mode)),
    [slots],
  )
  const filteredSlotsRef = useRef(filteredSlots)
  filteredSlotsRef.current = filteredSlots
  const unreadSlots = useAppSelector(s => s.dashboard.unreadSlots)
  // Unified view: unread keys for all chat-like slots (both default and orchestrator).
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
  // The one post-resolve answer for every resume entry point; rendered with the
  // pane-level banners, which is the only place all of them can see.
  const unresumableResume = useAppSelector(s => s.chat.unresumableResume)
  const activeSlot = useAppSelector(s => s.chat.activeSlot)
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
    queryKey: ['dashboardConfig'], queryFn: () => api.dashboardConfig(), staleTime: 30_000,
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
      tabsCtlRef.current?.openApp(id, i18nT('pages.chatPage.mcp_app_tab_title'), activeSlot)
    }
  }, [mcpAppPanel, activeSlot, appToolCallIds, dispatch])

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
  // User-sent messages (oldest → newest) for ↑/↓ prompt history in the input.
  // Deduplicate consecutive identical prompts to match shell/REPL behavior.
  // `messages` gets a new reference on every streaming chunk; preserve the
  // previous array when user-message content is unchanged so `sentMessages`
  // stays referentially stable and doesn't re-run downstream effects.
  const sentMessagesRef = useRef<string[]>([])
  const sentMessagesSlotRef = useRef<string | null>(null)
  // Per-slot timestamp (ms) of the last soft-stop press, used to arm the
  // force-kill. A force press (second click while soft_pending) arriving
  // within FORCE_KILL_ARMING_MS of that slot's soft stop is treated as an
  // accidental rapid double-tap and ignored, so users can't hard-kill by
  // mashing Stop. Keyed by slot so switching slots can't measure one slot's
  // press against another slot's timestamp.
  const softStopAtMapRef = useRef<Map<string, number>>(new Map())
  const sentMessages = useMemo(() => {
    const out: string[] = []
    for (const m of messages) {
      if (m.role !== 'user') continue
      const text = m.rawText ?? m.content
      if (!text || text === out[out.length - 1]) continue
      out.push(text)
    }
    // Reset the cached reference when switching slots — otherwise two
    // conversations with matching length+tail would share the prior array.
    if (sentMessagesSlotRef.current !== activeSlot) {
      sentMessagesSlotRef.current = activeSlot ?? null
      sentMessagesRef.current = out
      return out
    }
    // Append-only within a slot — full element-wise compare (array is small).
    const prev = sentMessagesRef.current
    if (prev.length === out.length && prev.every((v, i) => v === out[i])) {
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
   * identity re-rendered the entire sidebar during its mobile drawer slide.
   * Keep every prop handed to ChatSidebar referentially stable.
   */
  const clearSplitOnSelect = useCallback(() => setSplitMode(false), [])
  /** Same contract as `clearSplitOnSelect`, for the sessions-only embed frame. */
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

  // Controller handoff invariant: composer allocates forward refs first; the
  // action controller below binds its send and picker callbacks in this same
  // render. Voice/dropdown events cannot fire during render, so their nullable
  // ports are bound before interaction. Do not reorder these two controllers.
  const composer = useChatPageComposerController({
    activeSlot,
    connected,
    dispatch,
    embedded,
    history,
    knowledgeFetch,
    messages,
    mode,
    pendingQuestion,
    refreshTrigger,
    searchParams,
    setSearchParams,
    slotLoading,
    slotRunning,
    slots,
    splitMode,
  })
  const {
    drafts,
    fileDrafts,
    pasteDrafts,
    sessionRefDrafts,
    saveDrafts,
    knowledgeFetchRef,
    prevSlot,
    activeSlotRef,
    composerSlotRef,
    input,
    setInput,
    inputRef,
    sendingRef,
    chatConfig,
    installedAgents,
    remoteCrew,
    effectiveDefaultAgent,
    defaultAgent,
    availableModels,
    pendingAgent,
    pendingAgentRef,
    setPendingAgent,
    pendingModelRef,
    setPendingModel,
    pendingProjectRef,
    setPendingProject,
    steerMutation,
    setAutoNudgeOpen,
    autoNudgeLoop,
    approvalMode,
    scrollerRef,
    scrollToDisplayIndex,
    vGetFollowRef,
    vScrollToBottomRef,
    mountIndexRef,
    setPrefillHint,
    autoSendRef,
    autoSendTick,
    setAutoSendTick,
    newSessionRef,
    tokenConsumingRef,
    widgetPrefillRef,
    setUploading,
    setPendingFiles,
    pickedFileTokens,
    setSnipFrame,
    snipSlotRef,
    pendingFilesRef,
    setPasteBlocks,
    pasteBlocksRef,
    setPendingSessions,
    pendingSessionsRef,
    setUploadError,
    setResizedInfo,
    frozenInputRef,
    voiceCaretRef,
    voicePendingCaretRef,
    sttDisarmedRef,
    lastDictationAnchorRef,
    lastDictationValueRef,
    postStopEditedRef,
    streamEnabledRef,
    sendRef,
    voiceRef,
    switchAgentRef,
    switchModelRef,
  } = composer
  // (Streaming-off teardown now lives in useVoiceInput — see its effect on
  // [streamEnabled, streamRecording, streamStop]. Routing through voice.toggle
  // here is racy because `useVoiceInput` flips its returned `recording` to the
  // batch value on the same render that `streamEnabled` goes false.)

  // The project ref is shared with later session actions. Resource ingress reads
  // it at event time so a folder drop always uses the project from this render.
  const currentProjectRef = useRef<string | undefined>(undefined)
  currentProjectRef.current = slots.find(slot => slot.key === activeSlot)?.project || undefined
  const resources = useChatPageResourcesController({
    activeSlot,
    activeSlotRef,
    messages,
    slotLoading,
    dispatch,
    queryClient,
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
    selectSource,
    setRevealedSources,
    colorThemeRef,
    handleFileOpen,
    handleFolderOpen,
    handleArtifactOpen,
    handleOpenDiff,
  } = resources
  // The identity-stable member, hoisted once: every dock-opening handler below
  // depends on `closeSearch` rather than on `search`, which useMessageSearch
  // rebuilds as a fresh literal on every search-state change. Same reason the
  // resources controller hoists it — see its handleFileOpen.
  const closeSearch = search.close

  // Open the Subagents panel from a completion card. A per-agent event
  // deep-links to the agent it reports on, so the panel lands on that
  // transcript rather than whatever was last selected; a wave digest names no
  // single agent and just opens the tab.
  const handleSubagentPanelOpen = useCallback((parsed: ParsedSubagentCompletion) => {
    if (parsed.kind === 'single') dispatch(selectSubagent(parsed.agentId))
    dispatch(openActivityToTab('subagents'))
  }, [dispatch])


  const { data: forkCfg } = useQuery<{ tail_fork_enabled?: boolean }>({ queryKey: ['dashboardConfig'], queryFn: () => api.dashboardConfig(), staleTime: 30_000 })
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
        alert(i18nT('pages.chatPage.fork_failed_error', { error: result.error || i18nT('pages.chatPage.unknown_error') }))
      }
    } catch (e) {
      alert(i18nT('pages.chatPage.fork_failed_error', { error: errMessage(e) || i18nT('pages.chatPage.unknown_error') }))
    }
  }, [activeSlot, dispatch, forkCfg])

  const handlePlanFromHere = useCallback(async (visibleIndex: number, messageId?: string) => {
    if (!activeSlot) return
    try {
      const result = await dispatch(forkSlot({ slot: activeSlot, atIndex: visibleIndex, messageId, mode: 'orchestrator' })).unwrap()
      if (result.ok) {
        await dispatch(switchSlot(result.key))
        // Unified view: the forked orchestrator slot lives in the same sidebar.
        if (!mode) navigate('/chat')
      } else {
        alert(i18nT('pages.chatPage.plan_from_here_failed_error', { error: result.error || i18nT('pages.chatPage.unknown_error') }))
      }
    } catch (e) {
      alert(i18nT('pages.chatPage.plan_from_here_failed_error', { error: errMessage(e) || i18nT('pages.chatPage.unknown_error') }))
    }
  }, [activeSlot, dispatch, mode, navigate])


  const transcriptEarly = useChatPageTranscriptEarlyController({
    activeTip,
    mountIndexRef,
    pinPromptMinimized: chatConfig.pinPromptMinimized,
    scrollerRef,
    scrollToDisplayIndex,
    slotRunningRef,
    vGetFollowRef,
    vScrollToBottomRef,
  })
  const {
    scrollBottom,
    autoFollowAllowed,
  } = transcriptEarly

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
    input,
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
    drawerPopRef,
    highlightTs,
    initialMidRef,
    initialMsgRef,
    initialSidRef,
    setHighlightTs,
    selectSessionTab,
  } = session

  // Reconcile the active slot's running state from WS slot updates. The reducer
  // guards against a stale snapshot overwriting an unconfirmed local turn.
  //
  // This MUST be registered before the actions controller below, because effects
  // run in registration order within a commit and the controller's auto-send
  // effect reads this reducer's output LIVE: `send` asks
  // `selectComposerBusy(store.getState(), slot)`, which reads
  // `state.chat.slotRunning`, to decide whether to append an optimistic user
  // bubble. Arriving with `?autoSend=1` on a slot the server already has running
  // is the racing commit — the store's `slotRunning` is still false until this
  // dispatch lands. Registered after the controller, auto-send would read the
  // stale false, draw an optimistic bubble, and then the backend's own `queued`
  // row would arrive for the same message, leaving the turn duplicated in the
  // transcript. Keep this above the controller call.
  useEffect(() => {
    if (!activeSlot) return
    const s = slots.find(s => s.key === activeSlot)
    if (!s) return
    dispatch(syncSlotRunningFromServer({ slot: s.key, running: s.running, stopping: s.stopping ?? false }))
  }, [slots, activeSlot, dispatch])

  // Auto-scroll during streaming — only when pinned to bottom
  const lastMsg = messages[messages.length - 1]
  const isStreaming = lastMsg?.role === 'streaming'
  const actions = useChatPageActionsController({
    activeSlot,
    connected,
    dispatch,
    mode,
    slotRunning,
    composer: {
      activeSlotRef,
      autoSendRef,
      autoSendTick,
      composerSlotRef,
      defaultAgent,
      drafts,
      effectiveDefaultAgent,
      fileDrafts,
      frozenInputRef,
      inputRef,
      knowledgeFetchRef,
      lastDictationAnchorRef,
      lastDictationValueRef,
      newSessionRef,
      pasteBlocksRef,
      pasteDrafts,
      pendingAgentRef,
      pendingFilesRef,
      pendingModelRef,
      pendingProjectRef,
      pendingSessionsRef,
      pickedFileTokens,
      postStopEditedRef,
      saveDrafts,
      sendRef,
      sendingRef,
      sessionRefDrafts,
      setAutoSendTick,
      setInput,
      setPasteBlocks,
      setPendingAgent,
      setPendingFiles,
      setPendingModel,
      setPendingProject,
      setPendingSessions,
      setPrefillHint,
      steerMutation,
      streamEnabledRef,
      sttDisarmedRef,
      switchAgentRef,
      switchModelRef,
      voiceRef,
      widgetPrefillRef,
    },
    resources: { colorThemeRef, tabsCtl },
    session: { messages, messagesRef, slots, currentProjectRef },
    scroll: { scrollBottom },
  })
  const {
    dashCfg,
    currentSlot,
    effectiveMode,
    approve,
    toApiDecision,
    dismissApproval,
    regenerating,
    showRefusedPress,
    handleRegenerate,
    continuable,
    interrupted,
    continuing,
    handleContinue,
    handleQuote,
    handleAsk,
    handleEditResend,
  } = actions
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

  // Refs so the "run in terminal" listener (registered once) always sees the
  // live panel controller + this chat's working directory.
  const tabsCtlRef = useRef(tabsCtl); tabsCtlRef.current = tabsCtl

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
   *  `close()` runs unconditionally, exactly as the resources controller's
   *  handleFileOpen / handleArtifactOpen / handleOpenDiff do. Guarding it on
   *  `search.isOpen` would pull that value into the closure, and the transcript
   *  controller's renderMessage holds this callback across renders where the find
   *  pane opens — so a captured `isOpen === false` would skip the close entirely and
   *  open the tab behind the hidden dock. `close()` is already safe with nothing
   *  open: it only hands focus back `if (wasOpen)`. */
  const revealAppInPanel = useCallback((toolCallId: string) => {
    closeSearch()
    dispatch(openActivityPanel())
    tabsCtlRef.current?.openApp(toolCallId, i18nT('pages.chatPage.mcp_app_tab_title'), activeSlot ?? null)
    // Depend on the stable member, not the whole hook object: `closeSearch` is a
    // useCallback([]) in useMessageSearch, while `search` is a fresh literal every
    // render — naming it would make this a new function every render and churn
    // renderMessage, defeating memo(TurnBlock) for settled turns.
  }, [dispatch, activeSlot, closeSearch])

  // "Add to context" from the file-browser rail's row context menu: insert the
  // SAME `@`-mention the file picker does, so a right-click is just a second
  // entry point to the existing mention plumbing. A file gets an `@rel` token
  // plus a staged upload (chip + `[attached_file N]` on send); a folder gets a
  // bare `@rel/` reference (the token IS the reference — no upload). The caret
  // is unknown from the tree, so both append. Idempotent: re-adding a path
  // already referenced in the composer is a no-op.
  const handleAddToContext = useCallback((absPath: string, kind: 'file' | 'dir') => {
    // `absPath` arrives from the tree with a forward-slash-normalized Windows
    // root; normalize the project root the same way (Windows-shaped roots
    // only — normalizeWindowsPath leaves POSIX paths, where `\` is a legal
    // name character, untouched) so makeRelative can relativize on native
    // Windows instead of keeping the absolute path.
    const rel = makeRelative(absPath, normalizeWindowsPath(currentProjectRef.current || ''))
    if (kind === 'dir') {
      // spliceDirTokens dedupes by exact string -- it only ever sees bare
      // RELATIVE tokens, with no platform context to prove a `\` is a
      // Windows separator rather than a literal POSIX filename character, so
      // it cannot safely widen the comparison itself. Widen HERE instead,
      // gated on the PROJECT being Windows-shaped (an absolute path DOES
      // carry a provable drive-letter/UNC prefix): only then can the Windows
      // @-picker's backslash-form dir token (`@src\utils\`) be recognized as
      // the SAME folder this handler's forward-slash `rel` (`src/utils/`)
      // refers to. On a POSIX project this widening never triggers, so two
      // genuinely different directories (`src/a\b/` vs `src/a/b/`) can never
      // be conflated.
      const relSlash = rel.endsWith('/') ? rel : `${rel}/`
      const project = currentProjectRef.current || ''
      const projectIsWindowsShaped = normalizeWindowsPath(project) !== project
      const dup = projectIsWindowsShaped && parseDirTokens(inputRef.current).some(
        t => t.rel.replace(/\\/g, '/') === relSlash,
      )
      if (!dup) {
        const spliced = spliceDirTokens(inputRef.current, null, [rel])
        if (spliced.changed) setInput(spliced.value)
      }
    } else {
      const token = `@${rel}`
      // hasExactRelMention checks EXACTLY this rel (either separator
      // rendition — the Windows @-picker inserts backslash rels), never a
      // shorter basename suffix: two staged files sharing a basename could
      // otherwise cross-match on a single `@util.ts` mention, and later
      // removing the SECOND file's chip (whose fallback derivation also
      // suffix-walks) would then strip the FIRST file's mention instead.
      // Checked against the live text (not inside the updater) because the
      // token BOOKKEEPING must follow the same branch: on the already-mentioned
      // no-op the token present in the text may be a different form than the
      // one derived here, and recording ours would make chip-remove strip a
      // token that is not there while leaving the real one behind.
      const alreadyMentioned = hasExactRelMention(inputRef.current, rel)
      if (!alreadyMentioned) {
        setInput(prev => {
          const lead = prev && !/\s$/.test(prev) ? ' ' : ''
          return `${prev}${lead}${token} `
        })
        pickedFileTokens.current[absPath] = token
      }
      // addPendingFile dedupes by canonical Windows identity: the @-picker may
      // have already staged this file in native `C:\…` form, and an exact check
      // would send it twice under two attachment markers.
      setPendingFiles(prev => addPendingFile(prev, absPath))
    }
    revealComposer()
  }, [inputRef, pickedFileTokens, setInput, setPendingFiles])

  // Feed the Web Preview tab from chat, by signal type (previewFeedDecision).
  // Neither path ever navigates the iframe: both hand the URL to the panel as a
  // "Load preview" card (setSessionPreviewPending) — the GET fires only on the
  // user's explicit Load click, so agent output can never drive the scripted
  // iframe to an arbitrary host without consent.
  //   • marker (`kirocrew:preview`, explicit agent intent) → also OPEN the tab,
  //     once per distinct URL. The applied URL is PERSISTED per slot so a route
  //     remount doesn't reopen a card the user dismissed; an in-memory ref
  //     backstops a failed localStorage write.
  //   • heuristic (a localhost URL merely mentioned in prose) → offer the card
  //     WITHOUT opening the tab, and only when no target is set yet.
  // Reuses the shared tabsCtlRef so the effect stays mount-stable as the strip churns.
  const appliedPreviewMemRef = useRef<Record<string, string>>({})
  useEffect(() => {
    const slot = activeSlot
    if (!slot) return
    let existing = ''
    try {
      existing = localStorage.getItem(`mc-webpreview-url:${slot}`)
        || localStorage.getItem(`mc-webpreview-pending:${slot}`) || ''
    } catch { /* ignore */ }
    const feed = previewFeedDecision(detectPreviewUrl(messages), !!existing)
    if (!feed) return
    const norm = normalizeUrl(feed.url)
    if (!norm) return
    if (feed.open) {
      // Marker → surface the Load-preview card + open the tab, deduped via a
      // PERSISTED applied key (survives remounts) plus an in-memory ref
      // (survives a failed localStorage write) so it never re-opens.
      let applied = ''
      try { applied = localStorage.getItem(`mc-webpreview-applied:${slot}`) || '' } catch { /* ignore */ }
      if (applied === norm || appliedPreviewMemRef.current[slot] === norm) return
      appliedPreviewMemRef.current[slot] = norm
      try { localStorage.setItem(`mc-webpreview-applied:${slot}`, norm) } catch { /* ignore */ }
      // Loopback-only (enforced inside setSessionPreviewPending): a rejected
      // (non-loopback) marker feeds nothing — and must not open the tab either.
      if (!setSessionPreviewPending(slot, norm)) return
      dispatch(openActivityPanel())
      tabsCtlRef.current.openView('browser')
    } else {
      setSessionPreviewPending(slot, norm)      // heuristic offer: card only, no open, no load
    }
  }, [messages, activeSlot, dispatch])
  // Auto-open the Browser panel when the agent starts browsing. The signal is the
  // agent's own shell call: browsing is `playwright-cli` commands, so a shell
  // tool_call whose preview invokes it is the start of a browse. Open/focus the tab
  // only at the START (new slot, or after a >90s gap), NOT on every command, so it
  // cannot steal focus from a tab the user switched to mid-browse.
  const browseOpenedRef = useRef<{ key: string | null; ts: number }>({ key: null, ts: 0 })
  useEffect(() => {
    const onTool = (e: Event) => {
      const d = (e as CustomEvent<{ slot?: string; is_shell?: boolean; input_preview?: string }>).detail
      if (!d?.is_shell) return
      if (!isBrowseCommand(d.input_preview)) return
      const key = d.slot ?? null
      // Only auto-open when the browsing session IS the one on screen. A background
      // session's commands must not open another session's panel.
      if (!key || key !== activeSlotRef.current) return
      const now = Date.now()
      const prev = browseOpenedRef.current
      if (prev.key !== key || now - prev.ts > 90_000) {
        dispatch(openActivityPanel())
        tabsCtlRef.current.openView('browser')
      }
      browseOpenedRef.current = { key, ts: now }
    }
    window.addEventListener('kirocrew-tool-call', onTool)
    return () => window.removeEventListener('kirocrew-tool-call', onTool)
  }, [dispatch, activeSlotRef])
  // Reachability: declare open chat slots to the Electron main process so the
  // agent command channel polls for them (see listPanelIds) even before the Browser
  // tab is ever opened — this is what makes the built-in browser the default for a
  // fresh chat. It is NOT a grant: authorization to drive the built-in browser is
  // Browser Mode (the Settings toggle), and the main-process gate is just the view
  // precondition. There is no separate per-session consent registration — the
  // command channel can only deliver an op for a session key it polls for, and it
  // must poll before any URL is known, so gating reachability on a per-session
  // grant would make the whole native path unreachable for a fresh chat.
  //
  // EVERY open chat is declared, not just the active one.
  //
  // The command channel can only deliver an op for a session key it polls for,
  // and it must poll BEFORE any URL is known. Declaring only `activeSlot` made
  // that a moving target, and both consequences were observed live in a diagnostic
  // run:
  //   * a chat created and messaged within seconds RACED the registration — the
  //     navigate reached the gateway first, which answered `no-native-panel` (503)
  //     because no poller held that key yet, so the proxy fell back to the
  //     Playwright mirror for the whole turn (observed: slot created at T+0, the
  //     navigate at T+15s, the key first reported 9 minutes later);
  //   * a BACKGROUND chat was never reachable at all, even when it was the session
  //     the agent was acting for.
  //
  // Declaring a key is NOT authorization — it grants nothing, and every op still
  // runs the same gate — so there is no reason to report one key instead of all of
  // them. Tracking is diffed rather than torn down per change: re-registering the
  // same keys on every slot-list edit would churn IPC for no reason, and dropping
  // them mid-turn is exactly the race above.
  const trackedSlotsRef = useRef<Set<string>>(new Set())
  const trackableSlotKeys = useMemo(
    () => slots.map(s => s.key).filter((k): k is string => !!k),
    [slots],
  )
  useEffect(() => {
    const api = (window as unknown as {
      browserAPI?: { trackSession?: (id: string, tracked: boolean) => Promise<unknown> }
    }).browserAPI
    if (!api?.trackSession) return      // plain browser (no bridge)
    const want = new Set(trackableSlotKeys)
    const tracked = trackedSlotsRef.current
    for (const key of want) {
      if (tracked.has(key)) continue
      tracked.add(key)
      void api.trackSession(key, true)
    }
    for (const key of [...tracked]) {
      if (want.has(key)) continue
      tracked.delete(key)
      void api.trackSession(key, false)
    }
  }, [trackableSlotKeys])
  // Native counterpart of the mirror auto-open above. When the agent opens a page
  // in the BUILT-IN browser, the WebContentsView is created in the Electron main
  // process but the dashboard owns layout — until the Browser panel mounts and
  // reports its rect, the page is composited nowhere and the user sees nothing.
  // So surface the panel on the main process's `browser:agent-opened` signal.
  //
  // Same active-slot guard as the mirror path: a background session's page must
  // not open another session's panel.
  useEffect(() => {
    const api = (window as unknown as {
      browserAPI?: { onAgentOpened?: (cb: (p: { panelId?: string }) => void) => () => void }
    }).browserAPI
    if (!api?.onAgentOpened) return      // plain browser (no preload bridge)
    return api.onAgentOpened(({ panelId }) => {
      if (!panelId || panelId !== activeSlotRef.current) return
      dispatch(openActivityPanel())
      tabsCtlRef.current.openView('browser')
    })
  }, [dispatch, activeSlotRef])
  // "Run in terminal" (from chat code blocks): open a terminal tab in the
  // app-wide dock panel and run the command in it, starting in the chat's
  // working dir. The dock panel persists across routes (unlike chat-scoped
  // terminal tabs) so the running shell survives navigation.
  useEffect(() => {
    const handler = (e: Event) => {
      const detail = (e as CustomEvent).detail || {}
      const code: string = detail.code
      const reqId: string = detail.reqId
      if (typeof code !== 'string' || !code) return
      const sessionId = addDockTerminal(currentProjectRef.current ?? undefined)
      let settled = false
      const emit = (ok: boolean) => {
        if (settled) return
        settled = true
        window.dispatchEvent(new CustomEvent('mc:run-in-terminal-result', { detail: { reqId, ok } }))
      }
      if (!sessionId) { emit(false); return }
      const unsub = onTerminalReady(sessionId, () => { emit(sendToTerminalSession(sessionId, code)) })
      // Give the PTY time to connect; if it never does, report failure.
      setTimeout(() => { unsub(); emit(false) }, 6000)
    }
    window.addEventListener('mc:run-in-terminal', handler)
    return () => window.removeEventListener('mc:run-in-terminal', handler)
  }, [])
  // Cold-tab hydration: after a reload (or when restoring a slot's strip from
  // the persisted panel-tabs store), file tabs come back as lightweight
  // references with their heavy content stripped (content === undefined). Read
  // it back declaratively with useQueries — one ['file-read', path] query per
  // cold file tab (same key/shape as handleFileOpen so the cache dedupes).
  // Once a tab's content is patched in it drops out of coldFileTabs and its
  // query unsubscribes. Diff tabs are transient (not persisted — a restored
  // diff can't reconstruct the original turn snapshot); artifact tabs
  // self-hydrate via ArtifactPanel's own ['artifact', slug] query.
  const coldFileTabs = useMemo(
    () => tabsCtl.tabs.filter(t => t.kind === 'file' && t.path && t.content === undefined),
    [tabsCtl.tabs],
  )
  const coldFileResults = useQueries({
    queries: coldFileTabs.map(t => ({
      queryKey: ['file-read', t.path!],
      queryFn: async () => {
        const res = await fetch(fileReadUrl(t.path!))
        const text = res.ok
          ? await res.text()
          : res.status === 404 ? i18nT('pages.chatPage.file_not_found_on_disk_it_may_have_been_moved_or')
          : i18nT('pages.chatPage.unable_to_read_file')
        return { text, ok: res.ok }
      },
      staleTime: 10_000,
    })),
  })
  // Mirror settled reads into the tab strip. useQueries owns the fetch
  // lifecycle (error/retry/dedupe); this effect only writes results back, and
  // the content===undefined guard keeps it idempotent (a hydrated tab leaves
  // coldFileTabs, so it isn't re-patched).
  useEffect(() => {
    coldFileResults.forEach((r, i) => {
      const t = coldFileTabs[i]
      if (!t || t.content !== undefined) return
      if (r.data) tabsCtl.patchTab(t.id, { content: r.data.text, savedContent: r.data.text })
      else if (r.isError) {
        // The placeholder is not user work: stamp it as its own baseline so
        // the tab counts clean and the next chip/tree click retries the read
        // instead of "protecting" the error text as unsaved edits.
        const errText = i18nT('pages.chatPage.error_reading_file')
        tabsCtl.patchTab(t.id, { content: errText, savedContent: errText })
      }
    })
  }, [coldFileResults, coldFileTabs, tabsCtl])
  // Session mode of the active slot. In the unified chat view the page-level
  // `mode` prop is always '' — the slot's own mode is the source of truth for
  // header identity (Autopilot icon + tooltip).
  const title = currentSlot?.title && currentSlot.title !== currentSlot.key ? currentSlot.title : activeSlot || ''
  const displayMode = approvalMode === 'yolo' ? 'yolo' : currentSlot?.trust ? 'trust' : currentSlot?.trust_reads ? 'trust_reads' : 'normal'
  // Resolve model for existing slots that don't have one stored
  const _slotAgentName = (currentSlot && !currentSlot.model) ? (currentSlot.agent || defaultAgent || 'default') : ''
  const { data: _slotResolvedModel } = useQuery({
    queryKey: ['resolved-model', _slotAgentName, provider.id],
    queryFn: () => provider.resolveModel(_slotAgentName),
    enabled: !!_slotAgentName,
  })
  // The agent the composer's "set as default" row acts on: the active slot's
  // agent, else whichever agent a new session would open on.
  const _modelPinAgent = currentSlot?.agent || pendingAgent || defaultAgent || 'default'
  const _modelPinCfg = installedAgents.find(a => a.name === _modelPinAgent)
  // Writes agents.<name>.model in config.json. Invalidates the resolved-model
  // queries so a slot showing an inherited value picks the new pin up without a
  // reload; open sessions keep the model they already resolved.
  const pinModelToAgentMut = useMutation({
    mutationFn: ({ agent, model }: { agent: string; model: string }) =>
      api.updateKirocrewAgent(agent, { model }),
    onSuccess: () => {
      dispatch(triggerRefresh())
      queryClient.invalidateQueries({ queryKey: ['resolved-model'] })
    },
    // The dropdown closes as soon as the row is clicked, so without this a
    // failed write left NOTHING on screen and the old default silently stood —
    // discoverable only by reopening the menu. Body is the agent name plus the
    // server's own message, so it carries no untranslated prose of its own.
    onError: (e: Error, vars) => {
      dispatch(addNotification({
        ts: uniqueNotificationTs(),
        kind: 'agent',
        priority: 'critical',
        title: i18nT('pages.chatPage.could_not_set_the_agent_default_model'),
        body: `${vars.agent}: ${e?.message || i18nT('components.errorBoundary.something_went_wrong')}`,
      }))
    },
  })
  // Derived, not mirrored into state via an effect: the effect form cost an extra
  // render pass every time the query settled, for a value that is a pure function
  // of the query result.
  const resolvedModel = _slotResolvedModel || ''
  // The model to DISPLAY for this slot. A slot can stay pinned to a model the
  // account can no longer run (a plan downgrade leaves the pin behind): the
  // backend withholds it at spawn and runs the session on its own default, so
  // showing the pin would name a model no turn will use. The slot carries the
  // backend's verdict for exactly that (`model_withheld`), and it is pinned
  // server-side to the model it was computed for, so it always describes the
  // first operand below whenever that operand is the slot's own pin. The
  // degraded flag gates the list-membership fallback used when there is no
  // verdict — a cached list served while /api/models fails is stale, not
  // authoritative — and is subscribed to rather than read, because it can flip
  // without the list changing.
  const _modelsDegraded = useModelsDegraded(provider.id)
  const shownModel = displayModel(
    currentSlot?.model || resolvedModel || '',
    availableModels,
    _modelsDegraded,
    currentSlot?.model_withheld,
  )
  // Context-window fallback for a peer-bound session BEFORE its first turn. Once a
  // turn has run the real number arrives with the relayed `context_usage` frame and
  // wins; until then `provider.getContextWindow` would answer from THIS machine's
  // model knowledge, which can differ from the peer's for the same model name.
  const remoteContextWindow = useMemo(() => {
    if (!remoteCrew.isRemote) return 0
    const picked = shownModel === 'auto' ? '' : shownModel
    return remoteCrew.capabilities?.models.find(m => m.model_name === picked)?.context_window || 0
  }, [remoteCrew.isRemote, remoteCrew.capabilities, shownModel])
  // True when the pin row would be a no-op: the agent already stores exactly
  // the model the composer is showing. 'auto' is the inherit spelling, never a
  // stored pin, so it never counts as pinned. Reads the slot's REAL model, not
  // `shownModel` — this pairs with the write below, and a display fallback must
  // never decide what gets persisted.
  const _modelPinActive = currentSlot?.model || resolvedModel || ''
  const _modelPinPinned =
    !!_modelPinCfg?.model && _modelPinCfg.model === _modelPinActive && _modelPinActive !== 'auto'
  // The configured default effort for new sessions. A slot that has never
  // touched the effort control carries '' (no override) but still RUNS at this
  // default — the backend applies `slot.reasoning_effort or agent.reasoning_effort`
  // — so the composer must show the inherited value rather than a bare
  // "Default", which read as "the model decides" and hid the real setting.
  const { data: _defaultEffort } = useQuery({
    queryKey: ['default-effort', provider.id],
    queryFn: () => provider.resolveDefaultEffort(),
    enabled: provider.capabilities.reasoningEffort,
  })
  const defaultEffort = _defaultEffort || ''
  // Effort actually in force for the active slot: per-slot override, else the
  // configured default. Display only — the slot's raw value still drives the
  // picker so "no override" stays distinguishable from an explicit pick.
  const effectiveEffort = currentSlot?.reasoning_effort || defaultEffort
  // Branch label for the active project chip. The user can check out a
  // different branch outside the dashboard at any time, so this refetches on a
  // slow interval and on window focus rather than being read once. A failure
  // (no git, path gone, not a repo) leaves the chip showing the folder name
  // alone, which is the pre-existing behaviour.
  const _slotProject = currentSlot?.project || ''
  const { data: projectGit, isError: projectGitError } = useQuery({
    queryKey: ['project-git', _slotProject],
    queryFn: () => api.projectGit(_slotProject),
    enabled: !!_slotProject,
    staleTime: 15_000,
    refetchInterval: 60_000,
    refetchOnWindowFocus: true,
    retry: false,
  })
  // React Query keeps the last successful data after a failed refetch, so a
  // project that was deleted or revoked would keep showing its old branch
  // indefinitely. Treat an errored query as "no branch" and fall back to the
  // folder name, which is the same degradation as a non-repo project.
  const projectBranch = projectGitError
    ? ''
    : projectGit?.branch || (projectGit?.detached ? projectGit.head || '' : '')
  // Working-tree summary for the composer footer badge. Shares the Git panel's
  // ['git-status', dir] key so panel and footer dedupe into one fetch — while
  // the panel is open its 5s interval drives the shared cache and this observer
  // just reads it. Gated on repo=true so a non-repo project never runs a git
  // subprocess on an interval; the cheap HEAD-file probe above answers that.
  const { data: projectGitStatus, isError: projectGitStatusError } = useQuery({
    queryKey: ['git-status', _slotProject],
    queryFn: () => api.projectGitStatus(_slotProject),
    enabled: !!_slotProject && !projectGitError && !!projectGit?.repo,
    staleTime: 15_000,
    refetchInterval: 60_000,
    refetchOnWindowFocus: true,
    retry: false,
  })
  const gitBadge = !projectGitStatusError && projectGitStatus?.repo
    ? {
        dirty: projectGitStatus.files.length,
        ahead: projectGitStatus.ahead ?? 0,
        behind: projectGitStatus.behind ?? 0,
      }
    : undefined
  // The badge asserts "clean" by ABSENCE, so a stale reading right after the
  // agent finishes editing files is misleading at exactly the decision moment
  // the badge exists for. Invalidate the shared key on the running→idle
  // transition; the 60s interval covers everything else.
  const prevGitRunningRef = useRef(false)
  useEffect(() => {
    if (prevGitRunningRef.current && !slotRunning && _slotProject) {
      queryClient.invalidateQueries({ queryKey: ['git-status', _slotProject] })
    }
    prevGitRunningRef.current = slotRunning
  }, [slotRunning, _slotProject, queryClient])

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
    // If the marker cannot be persisted (quota), skip the auto-open entirely:
    // opening changes tabsCtl, which re-runs this effect, and an absent marker
    // would make it open again forever.
    try { localStorage.setItem(key, '1') } catch { return }
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
  const [generatingTitleSlots, setGeneratingTitleSlots] = useState<Set<string>>(new Set())
  const [titleDraft, setTitleDraft] = useState('')
  const lastTextIdx = useMemo(() => {
    for (let i = messages.length - 1; i >= 0; i--) {
      // Agree with renderMessage's skip: a hidden invisible-only row draws
      // nothing, so anchoring Regenerate/variant-switching on it would make
      // those affordances unreachable for the rest of a quiet monitor run.
      if (messages[i].role === 'assistant' && !isHiddenInvisibleAssistantRow(messages[i])) return i
    }
    return -1
  }, [messages])

  const cancelTitleRef = useRef(false)
  // The session-title field is an Enter-to-commit input; the guard owns both the
  // composition latch and the keypress, so the rename cannot fire on the Enter that
  // commits an IME candidate.
  const titleIme = useImeGuard()
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
  const toggleAct = useCallback(() => {
    // Opening with no tabs shows the empty-state launcher grid (no seeded
    // default view) -- the user picks what to open.
    dispatch(toggleActivity())
  }, [dispatch])
  // Header-launched toggle: the top-bar Activity button (App.tsx) dispatches
  // this event so the panel-close coordination above stays in ChatPage.
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
    api.voiceSynthesize(activeSlotRef.current || '', content).catch(() => {})
  }, [dispatch, activeSlotRef])

  const handleApplyPlan = useCallback(async (steps: PlanStepInput[]) => {
    try {
      const r = await api.planFromChat(steps, planTaskId)
      if (r.ok) { navigate('/projects?applied=' + (r.task_id || planTaskId)); return true }
    } catch { /* API error */ }
    alert(i18nT('pages.chatPage.failed_to_apply_plan'))
    return false
  }, [planTaskId, navigate])

  const transcript = useChatPageTranscriptController({
    activeSlot,
    activeViewIsBoundedPage,
    activityOpen,
    appToolCallIds,
    approve,
    autoNudgeLoop,
    chatConfig,
    connected,
    connectionsUiOn,
    continuing,
    continuable,
    cursorIsForActiveSlot,
    dismissApproval,
    dispatch,
    early: transcriptEarly,
    embedded,
    filteredSlots,
    handleApplyPlan,
    handleArtifactOpen,
    handleAsk,
    handleContinue,
    handleEditResend,
    handleFileOpen,
    handleFolderOpen,
    handleFork,
    handleOpenDiff,
    handlePlanFromHere,
    handleQuote,
    handleRegenerate,
    handleSpeak,
    handleSubagentPanelOpen,
    highlightTs,
    initialMidRef,
    initialMsgRef,
    initialSidRef,
    interrupted,
    isMobile,
    isStreaming,
    lastTextIdx,
    linkPreviewsOn,
    loadingOlder,
    mcpAppPanel,
    messages,
    messagesRef,
    mode,
    planTaskId,
    popout,
    regenerating,
    revealAppInPanel,
    search,
    selectSessionTab,
    setAutoNudgeOpen,
    setHighlightTs,
    setToolDisclosureFor,
    showRefusedPress,
    slotHasMore,
    slotOldestIndex,
    slotRunning,
    slotState,
    socialShareOn,
    toApiDecision,
    toggleAct,
    toolDisclosure,
  })
  const {
    setPendingPinnedJump,
  } = transcript

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
  /** Panel offset in px: `-drawerTravel()` offscreen, `0` at rest. A MotionValue so
   *  the drag writes it at frame rate without re-rendering this component. */
  const drawerX = useMotionValue(0)
  /** The sliding panel and the scrim behind it. Registered through refs so every
   *  settle can target the current DOM nodes on the compositor rather than sampling
   *  a MotionValue on the main thread while a session streams. */
  const drawerPanelRef = useRef<HTMLDivElement | null>(null)
  const drawerScrimRef = useRef<HTMLDivElement | null>(null)
  /**
   * The drawer's travel is its OWN width, not the screen's: the mobile panel
   * deliberately leaves `DRAWER_UNCOVERED_PX` of chat visible, so a viewport-wide
   * travel makes the panel disappear before the easing ends. Safe-area left is part
   * of the span because the drawer starts inset from that edge.
   */
  const drawerTravel = useCallback(
    () => Math.max(0, (window.innerWidth || 0) - DRAWER_UNCOVERED_PX + safeAreaLeft()),
    [],
  )
  /** Mobile's right activity panel is a fixed compositor overlay. Desktop and
   * embeds retain the existing width animation because they genuinely share a row. */
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
   *  is half-dimmed and a cancelled drag un-dims with the finger. Dividing by the
   *  drawer's own travel reaches zero exactly as the panel clears the edge. */
  const drawerScrim = useTransform(drawerX, x =>
    Math.max(0, Math.min(1, 1 + x / Math.max(1, drawerTravel()))))
  // The drawer panel is mounted per open, so targets are read through refs at
  // settle time. `staticRows` below is the companion invariant: projection nodes
  // cannot live under this compositor-driven transform.
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
   * drawer instead of leaving `/chat`.
   *
   * ONE entry, and it exists exactly while the drawer is open: every open mints
   * it and every close that was not itself the Back spends it. The alternative --
   * leaving it behind -- is the twin-entry defect `SidePanelLayout`'s back control
   * documents: two entries with the same URL, so the next back-swipe visibly does
   * nothing.
   *
   * The entry is a bare DUPLICATE of the one below it. The drawer is view state,
   * not a location: a URL that moved would have to be unwound on the pop, and
   * unwinding a `?sid=` is exactly what the session controller's sid effect would
   * misread as the user retracing sessions.
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
  /** Spend the entry, if we still hold one. `drawerPopRef` is what tells the
   *  session controller's sid effect this POP is bookkeeping rather than a
   *  session the user asked for. */
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
  //
  // Seed the portal slot SYNCHRONOUSLY so the very first render after a
  // ChatPage remount (e.g. switching back to /chat) already targets the
  // full-height actbar grid column. An effect-only seed leaves activitySlot
  // null for render 1, which falls back to the inline panel (rendered below
  // the header) and then flashes: below-header -> disappear -> portal opens.
  // The App shell (and its #activity-bar-slot) lives outside the router, so on
  // route-nav back it's already in the DOM. The effect below stays as the
  // fallback for cold load / mobile->desktop crossings where it isn't yet.
  const [activitySlot, setActivitySlot] = useState<HTMLElement | null>(
    () => (isMobile || embedMode) ? null : document.getElementById('activity-bar-slot'),
  )
  useEffect(() => {
    if (isMobile || embedMode) { setActivitySlot(null); return }
    const el = document.getElementById('activity-bar-slot')
    if (el) { setActivitySlot(el); return }
    // Slot not in the DOM yet. On a mobile -> desktop crossing, this
    // component's media-query subscription can flush (and run this effect)
    // before the App shell re-renders the slot div -- a one-shot lookup here
    // would miss it forever and strand the panel on the inline fallback
    // (rendering below the header instead of in the full-height column).
    // Watch the DOM until the slot appears, then latch it and stop.
    setActivitySlot(null)
    const mo = new MutationObserver(() => {
      const found = document.getElementById('activity-bar-slot')
      if (found) { setActivitySlot(found); mo.disconnect() }
    })
    mo.observe(document.body, { childList: true, subtree: true })
    return () => mo.disconnect()
  }, [isMobile, embedMode])
  /** The inline panel's mount predicate is shared by its overlay phase effect and
   *  the view, so a store transition cannot leave a mobile close without a node to
   *  travel through. */
  const sidePanelWantsMount = shouldMountSidePanel({ activityOpen, hasLiveAppTab, hasBrowserTab, searchOpen: search.isOpen })
    && !isSidePanelHidden({ activityOpen, hasLiveAppTab, hasBrowserTab, searchOpen: search.isOpen })
  // Mobile right-panel overlay: slide in when the panel wants the screen and
  // retain the DOM through a closing travel. A drag itself owns the MotionValue;
  // this effect only reacts to the durable store state.
  useEffect(() => {
    if (!isMobile) { setSideOverlayPhase('closed'); takeOverDrawer(sideOverlayX); return }
    if (sidePanelWantsMount) {
      if (sideOverlayPhaseRef.current === 'open') return
      if (sideOverlayPhaseRef.current === 'closed') sideOverlayX.set(window.innerWidth || 0)
      sideOverlayPhaseRef.current = 'open'
      setSideOverlayPhase('open')
      takeOverDrawer(sideOverlayX)
      animateDrawer(sideOverlayX, 0)
      return
    }
    if (sideOverlayPhaseRef.current !== 'open') return
    sideOverlayPhaseRef.current = 'closing'
    setSideOverlayPhase('closing')
    takeOverDrawer(sideOverlayX)
    animateDrawer(sideOverlayX, window.innerWidth || 0, () => {
      sideOverlayPhaseRef.current = 'closed'
      setSideOverlayPhase('closed')
    })
  }, [isMobile, sidePanelWantsMount, sideOverlayX])
  /** The right overlay's release must read the current durable panel state without
   *  subscribing its gesture callback to every activity transition. */
  const activityOpenRef = useRef(activityOpen)
  activityOpenRef.current = activityOpen
  /** Mount the right overlay under the finger without persisting a panel open
   *  until the user actually commits the gesture. */
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
    && shouldMountSidePanel({ activityOpen, hasLiveAppTab, hasBrowserTab, searchOpen: search.isOpen })
    && !isSidePanelHidden({ activityOpen, hasLiveAppTab, hasBrowserTab, searchOpen: search.isOpen })
  // The two panels share the chat pane and are selected by DIRECTION, not by a
  // narrow starting edge. Each is disabled while the other is visible because a
  // closing drag for one is an opening drag for the other.
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
    // /chat, reachable by drag rather than by tap. Spending on arrival had the
    // mirror hazard: a Back during the closing slide popped the still-unspent
    // entry while the POP effect above ignored it (the phase is 'closing', not
    // 'open'), and the settle then spent a second, real entry.
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
      if (open) { dispatch(openActivityPanel()); return }
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
    closeSearch()
    dispatch(openActivityToTab(view))
    // The mobile session drawer covers the panel it would reveal into. The
    // activeSlot effect closes it on a real switch, but a chip on the session
    // already open does not change activeSlot.
    if (isMobile) closeSidebar()
    return true
  }, [dispatch, isMobile, selectSource, closeSidebar, closeSearch, jiraSourceHostsRef, setRevealedSources, sourceHostsRef])
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
    // Still a mount-once registration in practice: closeSidebar's own deps are
    // `runDrawerClose` (over `drawerX`, a useMotionValue stable for the component's
    // lifetime, and `drawerTravel`, a useCallback([]) that reads window.innerWidth at
    // call time) and `consumeDrawerEntry` (over `navigate` and a ref), so nothing in
    // that chain changes identity. Naming it rather than relying on that keeps the
    // listener from silently capturing a stale copy if any link ever gains a dep.
  }, [closeSidebar])
  // The no-sessions force-open yields to expand mode: with an empty list no
  // sessions toggle is rendered, so suppressing it makes nothing inert, and the
  // preview would otherwise stay covered by a list that cannot be dismissed.
  const sidebarOpen = isMobile
    ? mobileSessions
    : (sidebarPinned || (filteredSlots.length === 0 && !previewExpanded))

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
    dispatch(switchSlot(key))
    setSplitMode(false)
    flyout.close()
  }, [dispatch, flyout])
  const flyoutNew = useCallback(() => {
    const effectiveMode = loadChatConfig().defaultAutopilot ? 'orchestrator' : (mode || '')
    flyout.close()
    // `focusComposerAfter`, not a bare dispatch + rAF: there is one composer and
    // it is bound to the ACTIVE slot, so focusing before creation fulfils puts
    // the caret on the old session and loses whatever is typed. See the module.
    focusComposerAfter(dispatch(createSlot({ agent: defaultAgent || undefined, mode: effectiveMode })).unwrap())
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

  const page = {
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
    projectGitAhead: gitBadge?.ahead ?? 0,
    projectGitBehind: gitBadge?.behind ?? 0,
    projectGitDirty: gitBadge?.dirty ?? 0,
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
  } satisfies ChatPageCoreViewModel

  const layout = {
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
  } satisfies ChatPageLayoutViewModel

  const ports = {
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
  } satisfies ChatPageViewPorts

  // ChatPageView is this page's single rendering consumer, so it takes whole
  // controllers; its explicit page/layout/ports models are the view-only
  // contract. A second consumer must introduce a narrower behavior port instead
  // of copying this ownership.
  return (
    <ChatPageView
      actions={actions}
      composer={composer}
      layout={layout}
      page={page}
      ports={ports}
      resources={resources}
      session={session}
      transcript={transcript}
      transcriptEarly={transcriptEarly}
    />
  )
}
