import { useState, useRef, useCallback, useEffect, useMemo } from 'react'
import { useAnchoredTriggerRect } from '../hooks/useAnchoredTriggerRect'
import { useAppSelector, store, type AppDispatch } from '../store'
import {
  switchSlot, openActivityPanel, openActivityToTab, selectSubagent, mcpAppKey,
} from '../store/chatSlice'
import { slotIsRemoteBound } from '../store/dashboardSlice'
import { useScrollManager } from './chat/useScrollManager'
import { useChatPageTranscriptEarlyController } from './chat/useChatPageTranscriptController'
import { useChatPageSessionController } from './chat/useChatPageSessionController'
import { useChatPageResourcesController } from './chat/useChatPageResourcesController'
import { useComposerDraftLifecycle, useComposerDraftStores, useStagedDraftPersistence } from './chat/page/composerDrafts'
import { useComposerStaging, useStagedFolderRefs } from './chat/page/composerStaging'
import { useFileMentionActions } from './chat/page/composerFileMentions'
import { useChatEventBridges, useColdFileTabHydration } from './chat/page/eventBridges'
import { useChatPageEnvironment, usePageFlags } from './chat/page/pageEnvironment'
import { useActiveSlotState } from './chat/page/activeSlotState'
import { useComposerDraftState, ComposerDraftSync, useComposerIntakeRefs, useMainComposerHandoff, useComposerVoiceHost } from './chat/page/composerHost'
import { useModelPickerModels, useNewSessionPicks, useSessionChoices } from './chat/page/sessionPicks'
import { useSteerDelivery, useRefusedPress, useRegenerate } from './chat/page/turnActions'
import { useTranscriptScrollRefs, useTranscriptPaging } from './chat/page/transcriptPaging'
import { useRunInTerminalConfig, useAppPanelReveal } from './chat/page/panelTabs'
import { useComposerSend } from './chat/page/composerSend'
import { useFollowupCardActions, useFollowupStarts } from './chat/page/followupCardActions'
import { usePaneLayoutState, usePanelLayout } from './chat/page/paneLayout'
import { usePageSelectionActions, useMessageActions } from './chat/page/messageActions'
import { useTranscriptPins } from './chat/page/transcriptPins'
import { useTranscriptRendering } from './chat/page/transcriptRendering'
import { useSessionsDrawer } from './chat/page/sessionsDrawer'
import { useWelcomeState } from './chat/page/welcomeState'
import { useSessionRosters } from './chat/page/sessionRosters'
import { useAppLaunchIntake, useAutoSendIntake, useErrorHandoffIntake, usePendingInputIntake } from './chat/page/launchIntake'
import { useFollowUpChips } from './chat/page/followUpChips'
import { useComposerSessionControls, useSessionControlChips } from './chat/page/sessionControls'
import { useComposerChips } from './chat/page/composerChips'
import { useTurnRecovery } from './chat/page/turnRecovery'
import { useComposerAboveBand } from './chat/page/composerDock'
import { useStableRowKeys, useTranscriptRows } from './chat/page/transcriptRows'
import { useBusyTurnControls } from './chat/page/busyTurnControls'
import { useSignedLinkAnchor, useForkSession, useOpenFollowupSession, useOpenSideChatForPane } from './useChatPageSlotSwitches'
import ChatPageFrame from './chat/page/ChatPageFrame'
import SessionsSidebarChrome from './chat/page/SessionsSidebarChrome'
import ChatPaneChrome from './chat/page/ChatPaneChrome'
import ChatEmptyState from './chat/page/ChatEmptyState'
import ChatTitleOverlay from './chat/page/ChatTitleOverlay'
import ChatTranscriptView from './chat/page/ChatTranscriptView'
import ComposerDockView from './chat/page/ComposerDockView'
import ChatSidePanels from './chat/page/ChatSidePanels'
import { slotBusySteer } from '../components/chat-input/busySend'
import { useSessionAutomation } from './chat/page/sessionAutomation'
import { useTranscriptJumps } from './chat/page/transcriptJumps'
import { useTranscriptWidth } from './chat/TranscriptScrollShell'


// No arbitrary cap on pinned-jump page loads: the loop terminates when the
// target message is found OR history is exhausted (!slotHasMore / null result).
// The `cancelled` flag in the useEffect cleanup and the loadOlderMessages null
// sentinel prevent infinite loops.  A ref tracks loads for diagnostics only.
// Canonical home is utils/navIntent (shared with the popout nav-intent
// applier); re-exported here for this page's historical importers.
export { PREFILL_STORAGE_KEY } from '../utils/navIntent'
export { shouldAutoFillOlder } from './chat/page/transcriptPaging'
import { claimAppAutoOpen } from '../hooks/usePanelTabs'
import { useListboxKeyboard } from '../hooks/useListboxKeyboard'

import ChatDropOverlay from '../components/ChatDropOverlay'
import SessionGridView from '../components/SessionGridView'
import { mcpAppTabTitle } from '../lib/mcpAppSrcdoc'
import { slotApprovalMode } from '../utils/slotApprovalMode'
import { isComposerSendHeld, useComposerSendHeld } from '../utils/composerSendHolds'

import SearchHighlightContext from '../hooks/SearchHighlightContext'
import { loadChatConfig, CONTENT_WIDTH, type ChatConfig } from './chat/ChatSettings'
import { scaleContentWidth } from './chat/contentWidth'
import { TOGGLE_RECT } from './chat/SessionFlyout'


import type { ParsedSubagentCompletion } from './chat/subagentCompletion'
import { useLabelTip } from '../components/InstantTip'
import { sessionTitleRoster } from '../utils/sessionRoster'
import { useLanguageGeneration } from '../i18n/useLanguageGeneration'


import { i18nT } from '../i18n/t'

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


// `mentionBoundary` / `strictMentionBoundary` / `leadingMentionBoundary`
// moved to utils/fileTokens.ts: the reconciliation staleness check, the
// remove-chip strip and send serialization share one definition there and
// can never disagree about what counts as a boundary.


export default function ChatPage({ mode, embedded, embedMode, popout, noUrlSync }: { mode?: string; embedded?: boolean; embedMode?: 'chat' | 'sessions'; popout?: boolean; noUrlSync?: boolean } = {}) {
  const {
    dispatch, moveSlotToFolder, navigate, navigationType, location, queryClient, provider, deleteTerminalSessionRef, searchParams, setSearchParams, isMobile, isTouch, topbarSlot, topbarTrailSlot,
    mobileNavRail, titleInTopbar, langGen, vv, keyboardInset, slots, switchSlotGone, filteredSlots, filteredSlotsRef, surfaceUnreadSlots, refreshTrigger, connected, creatingSlot, foregroundCreateId,
    foregroundCreateIdRef, lastCreatedActivationRef, unresumableResume, undeletableHistory, activeSlot, boundStore, voiceRecoverySlot, setVoiceRecoverySlot, appToolCallIds, mcpAppPanel,
    autoOpenGitPanel, autoOpenGitPanelKnown, sidePanelAlwaysOpen, sidePanelAlwaysOpenKnown, sidePanelPinSummary,
  } = useChatPageEnvironment({
    embedded, embedMode, popout,
  })
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
  const {
    messages, messagesRef, kiroCrewVersion, completedTurnCount, knowledgeFetch, knowledgeFetchRef, softStopAtMapRef, sentMessages, slotRunning, slotRunningRef, turnDisclosure, setTurnDisclosureFor,
    toolDisclosure, setToolDisclosureFor, turnRowMayBeLive, composerBusy, projectLocked, slotStopping, slotLoading, slotSwitchTarget, pendingQuestion, restoredQuestionNotice, pendingFollowup,
    folderSuggestion, followupTsBySlot, splitMode, setSplitMode, clearSplitOnSelect, crewWindow, openEmbeddedCrewWindow, navigateToEmbeddedSlot, splitAnchor, setSplitAnchor, activeTip, dismissTip,
    slotState, contextPct, contextTokens, activityOpen, slotHasMore, slotOldestIndex, cursorIsForActiveSlot, loadingOlder, olderFailed, activeViewIsBoundedPage, history, historyHasMore,
  } = useActiveSlotState({
    embedded, embedMode, popout, navigate, searchParams, activeSlot,
  })

  const draftStores = useComposerDraftStores()
  const { drafts, fileDrafts, pasteDrafts, sessionRefDrafts, saveDrafts, saveDraftsDebounced } = draftStores
  const {
    prevSlot, activeSlotRef, composerSlotRef, composerMountedRef, composerDraft, setInput, insertDashboardReply, inputNonBlank, inputRef, sendingRef,
  } = useComposerDraftState({
    activeSlot, drafts,
  })
  // The empty-transcript welcome screen and its "Continue a previous chat?" suggestions.
  const { updateHistoryQuery, historySuggestions, isWelcomeState, showHistorySuggestions } = useWelcomeState({
    messages, slotRunning, slotLoading, sendingRef, knowledgeFetch, pendingQuestion, history,
  })
  const pendingInput = useAppSelector(s => s.chat.pendingInput)
  const mainComposerAppend = useAppSelector(s => s.chat.mainComposerAppend)

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
  // Relay archives (old remote-executor sessions) are read-only: the server
  // refuses every turn-starting action on them (`relay_archive_refusal`), so the
  // client must not OFFER regenerate, edit-resend, rewind or continue either —
  // same predicate and same `executor` keying `selectContinuable` uses for Resume.
  const activeSlotRemoteBound = slotIsRemoteBound(slots.find(s => s.key === activeSlot))
  // The agent and model rosters the pickers offer.
  const {
    installedAgents, sidebarAgents, sidebarChoices, defaultAgent, effectiveAgents,
    defaultAgentFailed, toggleDefaultAgent,
    agentDropdown, setAgentDropdown, agentFilter, setAgentFilter, agentDropdownRef, agentInputRef, filteredAgents,
    effectiveModels,
  } = useSessionRosters({ activeSlot, activeSlotProject, refreshTrigger, dispatch })
  const {
    selectionCapabilitiesQ, selectionCapabilities, hiddenModelsQ, modelPickerConfigured, availableModels, codexPairModels, jevRouteOn, modelDropdown, setModelDropdown, modelFilter, setModelFilter,
    modelDropdownRef, modelInputRef, filteredModels, modelPickerReturnsFocusRef,
  } = useModelPickerModels({
    slots, activeSlot, effectiveModels,
  })
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
  const {
    pendingAgent, pendingAgentRef, pendingAgentKindRef, setPendingAgent, pendingModel, pendingModelRef, setPendingModel, pendingProjectRef, setPendingProject, modelBtnRect, anchorModelBtn,
  } = useNewSessionPicks({
    modelDropdown,
  })
  const {
    actionError, setActionError, showActionError, messageQuote, steerMutation,
  } = useSteerDelivery({
    dispatch, activeSlot, drafts, saveDrafts, composerSlotRef, composerMountedRef, setInput, inputRef,
  })
  const {
    automationOpen, setAutomationOpen, automation, automationId, automationCreationReady, automationSnapshotFailed,
    applyAutomationChange,
  } = useSessionAutomation({ activeSlot, queryClient, dispatch })
  const approvalMode = useAppSelector(s => s.dashboard.approvalMode)
  const slotsLoaded = useAppSelector(s => s.dashboard.slotsLoaded)

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
  const {
    vGetFollowRef, vScrollerElRef, lastRealInputAtRef, sentinelPagesSinceInputRef, walkPagesSinceInputRef, walkLastInputAtRef, vScrollToBottomRef, vFarmIsMeasuredRef, earlierBarInViewRef,
    mountIndexRef, estimateRowTopRef,
  } = useTranscriptScrollRefs({
    activeSlot,
  })
  const {
    prefillHint, setPrefillHint, prefillEdited, setPrefillEdited, raisePrefillHint, autoSendRef, appLaunchSendRef, autoSendTick, setAutoSendTick, newSessionRef, tokenConsumingRef, widgetPrefillRef,
    consumedPrefillRef,
  } = useComposerIntakeRefs()

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
  useMainComposerHandoff({
    dispatch, activeSlot, splitMode, drafts, saveDraftsDebounced, composerSlotRef, setInput, inputRef, mainComposerAppend, raisePrefillHint,
  })
  useSignedLinkAnchor({
    embedded, mode, dispatch, setSearchParams, composerSlotRef, setInput, autoSendRef, newSessionRef, raisePrefillHint, setAutoSendTick, tokenConsumingRef,
    getNoCarryCreateIdsRef: () => noCarryCreateIdsRef,
  })

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
    setPasteBlocks, pasteBlocksRef, pasteSlice,
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
  const {
    isMac, voiceSubmitRef, composerRef, voiceCaretRef, voicePendingCaretRef, composerVoiceOptions,
  } = useComposerVoiceHost({
    embedded, drafts, saveDrafts, activeSlotRef, composerSlotRef, inputRef,
  })
  const {
    currentProjectRef, terminalReuseRef, terminalCfgUnsettledRef, terminalCfgReadFailedRef,
  } = useRunInTerminalConfig({
    slots, activeSlot,
  })

  const memorySwitchingRef = useRef(false) // Held true while a memory-mode switch runs (see usePanelLayout).
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
      currentProjectRef,
      voiceCaretRef,
      voicePendingCaretRef,
      saveDrafts, composerLockedRef: memorySwitchingRef,
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
    githubSourceHostsRef,
    bitbucketServerSourceHostsRef,
    gerritSourceHostsRef,
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
    handleOpenWorkingTreeDiff,
    handleFolderOpen,
    handleArtifactOpen,
    handleOpenDiff,
    handleFileSave,
    handleCapture,
    handleSnipComplete,
    handleSnipCancel,
    composerUploadFiles,
    cancelUpload,
    uploadCount,
    handleOptimizeResult,
    dragOver,
    dropTargetProps,
  } = resources
  const sendHeld = useComposerSendHeld(activeSlot)

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
  // The roster's "Untitled session" label is localized, so a language switch
  // must rebuild it even when no slot changed (#23059).
  const languageGeneration = useLanguageGeneration()
  const sessionTitles = useMemo(
    () => sessionTitleRoster(filteredSlots),
    // eslint-disable-next-line react-hooks/exhaustive-deps -- keyed on the value-equal pair signature, not the slot objects (see above)
    [sessionTitleSig, languageGeneration],
  )
  const {
    handleFork, forkDirectionChoice,
  } = useForkSession({
    activeSlot, dispatch, showActionError,
  })

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
    pinnedJumpChrome,
    scrollToPinnedPrompt,
  } = transcriptEarly

  // Sticky-bottom scroll state is owned by the virtualizer (`virt.isAtBottom`,
  // wired by useTranscriptPaging in transcriptPaging.ts). No local mirror — a single source of truth avoids
  // dual-controller drift.

  // New content while following is handled inside the virtualizer (RO re-pin
  // for in-place growth + append layout-effect pin for new items), so ChatPage
  // does not run its own message-length scroll effect.
  const session = useChatPageSessionController({
    activeSlot,
    activeSlotRef,
    connected,
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
    ensureSlotTab,
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
    sidMatches,
    setSidMatches,
    openShortNameMatch,
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
    followUpOptions, followUpSourceKey, followUpMulti,
    followUpPicked, followUpPickedRef, toggleFollowUpOption,
    composerRootChange, composerUserEdit,
  } = useFollowUpChips({
    messages, isStreaming, pendingQuestion, activeSlot, inputRef, setInput, prefillEdited, setPrefillEdited,
  })
  const {
    dashCfg, splitFeatureEnabled, linkPreviewsOn, socialShareOn, jevAutoConsented, connectionsUiOn, openActivePopout, focusActivePopout, returnSelfToMain, activePoppedOut, planTaskId,
  } = usePageFlags({
    activeSlot, messages, splitMode, fileDrafts, saveDrafts, prevSlot, activeSlotRef, composerSlotRef, pendingFilesRef, setPendingFiles,
  })
  const {
    refusedPress, setRefusedPress, showRefusedPress,
  } = useRefusedPress()
  const {
    send,
  } = useComposerSend({
    mode, dispatch, slots, connected, activeSlot, knowledgeFetchRef, slotRunning, drafts, fileDrafts, pasteDrafts, saveDrafts, sessionRefDrafts, activeSlotRef, composerSlotRef, composerMountedRef,
    setInput, inputRef, sendingRef, pendingAgentRef, pendingAgentKindRef, setPendingAgent, pendingModelRef, setPendingModel, pendingProjectRef, setPendingProject, setActionError, showActionError,
    messageQuote, setPrefillHint, autoSendRef, setAutoSendTick, newSessionRef, widgetPrefillRef, mergeSlotTokens, pasteBlocksRef, pendingFilesRef, pendingSessionsRef, pickedFileTokens, setPasteBlocks,
    setPendingFiles, setPendingSessions, composerRef, currentProjectRef, colorThemeRef, autoFollowAllowed, scrollBottom, setRefusedPress, createFailReason,
  })

  // Submit inline document comments to the session the file was opened from,
  // not the currently-active one. If the user switched sessions while the
  // panel was open, switch back to the origin session so the prompt + reply
  // land where the document belongs. switchSlot.pending sets activeSlot
  // synchronously, but send()'s closure activeSlot is stale until re-render,
  // so the origin slot is passed to send() explicitly.
  const submitComments = useCallback((message: string) => {
    // Defense-in-depth: the panels' submit buttons are gated on `connected`,
    // but bail here too so an offline call can't switch the active session
    // and then have send() silently drop the message.
    if (!connected) return false
    const target = tabsCtl.activeTab?.slot ?? null
    // The batch is a Send press for the TARGET slot: it steers or queues per
    // that slot's busy-send mode, read before a switch can change what the
    // store says about it.
    const sendSlot = target ?? activeSlotRef.current
    const steerFlag = sendSlot ? slotBusySteer(store.getState(), sendSlot, jevAutoConsented) : undefined
    if (target && target !== activeSlot) dispatch(switchSlot(target))
    // The delivery verdict flows back to the panel: ArtifactPanel marks a
    // comment batch as sent only when this resolves true.
    return send(message, target ?? undefined, steerFlag)
  }, [connected, tabsCtl.activeTab, activeSlot, dispatch, send, jevAutoConsented, activeSlotRef])

  // The armed auto-send (?autoSend=1, a signed token, an app launch) and a
  // widget action's prefill.
  useAutoSendIntake({ connected, send, autoSendTick, autoSendRef, appLaunchSendRef, widgetPrefillRef, setInput, raisePrefillHint })
  const {
    approve, dismissApproval, switchAgent, pickModel, setProject,
  } = useSessionChoices({
    dispatch, queryClient, activeSlot, codexPairModels, setModelDropdown, modelPickerReturnsFocusRef, setPendingAgent, setPendingModel, setPendingProject,
  })

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
  const activeAgentName = currentSlot?.agent || defaultAgent || 'default'
  // Refs so the "run in terminal" listener (registered once) always sees the
  // live panel controller + this chat's working directory.
  const tabsCtlRef = useRef(tabsCtl); tabsCtlRef.current = tabsCtl
  const {
    revealAppInPanel,
  } = useAppPanelReveal({
    dispatch, activeSlot, boundStore, search, tabsCtlRef,
  })
  // `@`-mentions of files and folders and the file chips they stage.
  // The composer's polite status line for an attachment the reconciliation
  // changed on its own (#14597). `nonce` advances on every announcement so the
  // same file removed, re-added and removed again re-announces each time: a
  // screen reader fires only when the live region's text CHANGES, and a repeated
  // message is the same text — the nonce lets ChatInput force the change.
  const [attachmentAnnouncement, setAttachmentAnnouncement] = useState<{ text: string; nonce: number }>({ text: '', nonce: 0 })
  const announceAttachmentChange = useCallback((text: string) => {
    setAttachmentAnnouncement(prev => ({ text, nonce: prev.nonce + 1 }))
  }, [])
  const { clampOutOfTokens, handleAddToContext, removeFileChip, removeDirChip, selectPickedFile, mentionKeyEdit, optimizerMentionGuard, mentionEditRange } = useFileMentionActions({
    staging,
    inputRef,
    setInput,
    currentProjectRef,
    voiceCaretRef,
    voicePendingCaretRef,
    reconcileFileChipsRef,
    announceAttachmentChange,
  })
  const {
    followupTsRef, followupAddToSession, folderSuggestionAccept, folderSuggestionDecline, followupBranchFor,
  } = useFollowupCardActions({
    dispatch, moveSlotToFolder, activeSlot, folderSuggestion, followupTsBySlot, inputRef,
  })
  const {
    openFollowupSession,
  } = useOpenFollowupSession({
    mode, dispatch,
  })
  const {
    followupStartInWorktree, followupStartInNewChat,
  } = useFollowupStarts({
    activeSlot, currentSlot, followupTsRef, followupBranchFor, openFollowupSession,
  })

  // Outside events feeding the side panel, the dock terminal and the composer,
  // then the cold file tabs' reads.
  const { runInTerminalScope } = useChatEventBridges({
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
    shownModel, _pinShownModel, chipDefault, modelMarker, effortSupported, effortLevelsOverride,
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
    dispatch,
    queryClient,
    showActionError,
  })
  const {
    sidebarPinned, setSidebarPinned, sidebarPinnedRef, sidebarAutoHidden, sidePanelDock, sidePanelDockAnim, sidebarWidth, setSidebarWidth, sidebarDragging, setSidebarDragging, setEditingTitleSlot,
    editingTitle, enterSplit, splitAnchorForActive, activeIsSplitAnchor,
  } = usePaneLayoutState({
    embedMode, dispatch, slots, activeSlot, autoOpenGitPanel, autoOpenGitPanelKnown, setSplitMode, splitMode, setSplitAnchor, tabsCtl, splitFeatureEnabled, _slotProject, projectGit, projectGitError, embedded, popout, isMobile, sidePanelAlwaysOpen, sidePanelAlwaysOpenKnown, activityOpen,
  })
  const {
    lastTextIdx, regenerating, setRegenerating, handleRegenerate,
  } = useRegenerate({
    dispatch, activeSlot, messages, messagesRef, slotRunning, activeSlotRemoteBound, isStreaming, setRefusedPress, showRefusedPress,
  })

  // ---- Continue the thread ---------------------------------------------------
  const {
    continuable, interrupted, sendUnconfirmed, continuing, handleContinue,
    openModelPickerFromError, openDefaultModelSetting, openKiroSignIn, openMemberCapabilities, openTurnLimitSetting,
    cardOwnsRecovery,
    freshSession, handleFreshSession,
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
  const {
    inputAreaRef, dockH, dockGutter, dockRef, quoteWholeMessage, handleQuote, handleAsk, flyingQuote, endQuoteFlight,
  } = usePageSelectionActions({
    dispatch, activeSlot, setInput, messageQuote, scrollerRef,
  })
  const {
    openSideChatForPane,
  } = useOpenSideChatForPane({
    activeSlot, boundStore, connected, dispatch, showActionError,
  })
  const {
    handleEditResend, editLast, setEditLast, editSeqRef, farmPassRef, handleEditConsumed, searchCtxValue, renderUserContentCb, lastRole, streamTick, transcriptHot, railWidth, winW,
    effectiveSidebarWidth, toggleAct, handleSpeak, handleApplyPlan,
  } = useMessageActions({
    dispatch, navigate, filteredSlotsRef, connected, activeSlot, messages, messagesRef, slotRunning, activityOpen, activeSlotRef, activeSlotRemoteBound, showActionError, handleFileOpen, handleFolderOpen, search,
    tabsCtl, sessionTitles, selectSessionTab, linkPreviewsOn, planTaskId, setSidebarPinned, sidebarAutoHidden, sidebarWidth, setRegenerating,
  })

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
  // do NOT collapse on `pinned.idx` change. It still resets on slot switch (useTranscriptPaging)
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
  const {
    handleLoadEarlier, virt, isAtBottom, visibleDisplayItems, spinnerNearTop, reportSlashRefusal,
  } = useTranscriptPaging({
    dispatch, activeSlot, slotRunning, slotHasMore, scrollerRef, canMeasure, scrollerWidthBucket, vGetFollowRef, vScrollerElRef, lastRealInputAtRef, sentinelPagesSinceInputRef, walkPagesSinceInputRef,
    walkLastInputAtRef, vScrollToBottomRef, vFarmIsMeasuredRef, earlierBarInViewRef, mountIndexRef, estimateRowTopRef, displayItemsRef, setPinExpanded, setPinned, updatePinnedPrompt, isStreaming,
    setRefusedPress, renderedDisplayItems, anchorAltId, prefetchStartIndex, stableAnchorId, virtualKey,
  })
  // A busy turn: steer into it, answer its question card, stop it, and the queued-message cards.
  const {
    queuedMessages, systemDeliveryCount, steer, stopTurn, keepQuestionAnswer, answerQuestionCard,
    handleCancelQueued, handleInterruptQueued, handleEditQueued, handleReorderQueued, queuePendingIds,
  } = useBusyTurnControls({
    activeSlot,
    messageQuote,
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
    onSlashRefused: reportSlashRefusal,
  })
  // The endpointer's auto-submit is an Enter press: the composer's default busy
  // decision for the active slot (never the flipped chord), so it steers where
  // Send would steer. Assigned in render so the voice callback (composerHost.ts) always
  // reaches the latest send() and steer().
  voiceSubmitRef.current = () => {
    const slot = activeSlotRef.current
    // send() owns the disconnected guard and upload hold, so route either state there before steer() can consume the composer.
    if (!connected || isComposerSendHeld(slot) || isComposerSendHeld(composerSlotRef.current)) { void send(); return }
    const flag = slot ? slotBusySteer(store.getState(), slot, jevAutoConsented) : undefined
    if (flag) steer(flag === 'auto' ? { auto: true } : undefined)
    else void send()
  }
  const {
    messageToDisplayIdx, navigateToTurn, chatNav, turnStepAnnouncement, chatPins, chatPinsLoading, isPinned, setPendingPinnedJump, handleEditLastRequest, handleJumpToPinnedMessage, handleJumpToQuote,
    handleJumpToPin, handleTogglePinForMessage, handleUnpinById, pinError, pinStatus, dismissPinStatus, messageToDisplayIdxRef,
  } = useTranscriptPins({
    embedded, dispatch, isMobile, activeSlot, messages, messagesRef, slotRunning, splitMode, crewWindow, slotHasMore, slotOldestIndex, cursorIsForActiveSlot, loadingOlder, activeViewIsBoundedPage,
    isWelcomeState, activeSlotRemoteBound, scrollerRef, search, navToDisplayIndex, pinnedJumpChrome, scrollToPinnedPrompt, setHighlightTs, splitFeatureEnabled, regenerating, setEditLast, editSeqRef,
    renderedDisplayItems, renderedTranscript,
  })
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
  const {
    activeSlotTitle, renderMessage, renderTurnItem, renderFarmItem,
  } = useTranscriptRendering({
    embedded, mode, popout, filteredSlots, connected, activeSlot, voiceRecoverySlot, appToolCallIds, mcpAppPanel, messages, messagesRef, slotRunning, toolDisclosure, setToolDisclosureFor, slotState,
    activityOpen, slotHasMore, slotOldestIndex, cursorIsForActiveSlot, loadingOlder, chatConfig, activeSlotRemoteBound, automationId, setAutomationOpen, handleArtifactOpen, handleFileOpen,
    handleFolderOpen, handleOpenDiff, handleSubagentPanelOpen, sessionTitles, handleFork, forkDirectionChoice, highlightTs, selectSessionTab, linkPreviewsOn, socialShareOn, connectionsUiOn, planTaskId, showRefusedPress,
    approve, dismissApproval, revealAppInPanel, lastTextIdx, regenerating, handleRegenerate, continuable, continuing, freshSession, handleContinue, handleFreshSession, interrupted,
    openDefaultModelSetting, openKiroSignIn, openMemberCapabilities, openModelPickerFromError, openTurnLimitSetting, quoteWholeMessage, handleAsk, handleQuote, handleEditResend, editLast, farmPassRef,
    handleEditConsumed, renderUserContentCb, transcriptHot, toggleAct, handleSpeak, handleApplyPlan, renderedDisplayItems, stableMsgKey, handleLoadEarlier, isPinned, handleJumpToQuote,
    handleTogglePinForMessage,
  })
  const {
    mobileSessions, drawerMounted, drawerX, drawerPanelRef, drawerScrimRef, sideOverlayX, sideOverlayPanelRef, sideOverlayPhase, drawerScrim, openSidebar, closedSessionActions, sidebarFolderActions,
    closeSidebar, chatContainerRef, containerH, activitySlot, hasTaskDashboard, sidePanelWantsMount, inlineSidePanelShowing, drawerDragging, revealSourceLink, previewExpanded, sidebarOpen,
    sidebarOnScreen, flyoutTriggerRef, flyoutSurfaceRef, flyoutEligible, flyout, expandFrom, expandSidebar,
  } = useSessionsDrawer({
    embedded, embedMode, dispatch, navigate, navigationType, location, isMobile, filteredSlots, slotsLoaded, activeSlot, splitMode, crewWindow, activityOpen, showActionError, githubSourceHostsRef, hasBrowserTab,
    hasLiveAppTab, jiraSourceHostsRef, bitbucketServerSourceHostsRef, gerritSourceHostsRef, search, selectSource, setRevealedSources, sourceHostsRef, tabsCtl, drawerPopRef, chatFolders, setSidebarPinned, sidebarPinned, sidebarPinnedRef,
    sidebarAutoHidden, toggleAct,
  })
  // "Open Sidebar Sessions in a Session Tab" (Settings > Chat > Sessions). A
  // sidebar row's click, Enter/Space and source chip all call `onSelectSlot`
  // after their `switchSlot`, so one handler covers each; the flyout below is
  // the same list while the sidebar is collapsed.
  const sidebarClickOpensTab = chatConfig.sidebarClickOpensTab
  // On a phone a row pick is the user asking for that conversation, so it
  // dismisses the drawer here as well as through the drawer's `activeSlot`
  // effect. That effect cannot see a pick of the session already active,
  // which is what a close leaves behind the list: its neighbour, or the
  // blank session made for an empty list.
  const selectFromSidebar = useCallback((key: string) => {
    clearSplitOnSelect()
    if (sidebarClickOpensTab) ensureSlotTab(key)
    if (isMobile) closeSidebar()
  }, [clearSplitOnSelect, sidebarClickOpensTab, ensureSlotTab, isMobile, closeSidebar])
  const flyoutSwitch = useCallback((key: string) => {
    // User gesture on a listed session row (collapsed-sidebar flyout): the
    // announced class, same as the expanded sidebar's own rows.
    dispatch(switchSlot({ key, announceOnMissing: true }))
    setSplitMode(false)
    if (sidebarClickOpensTab) ensureSlotTab(key)
    flyout.close()
  }, [dispatch, flyout, setSplitMode, sidebarClickOpensTab, ensureSlotTab])
  const {
    flyoutNew, panelReserve, panelMaximized, panelFillWidth, mobileSessionsToggle, topbarSessionsToggle, mobileNewSessionItem, switchMemoryMode, showComposerMemoryChip, memorySwitching, composerOptimizing, handleOptimizingChange,
  } = usePanelLayout({
    mode, dispatch, isMobile, filteredSlots, slotsLoaded, activeSlot, isWelcomeState, showActionError, currentSlot, chatFolders, chatFoldersError, chatFoldersLoaded, mobileNewSessionBusy,
    mobileNewSessionInFlightRef, setMobileNewSessionBusy, setSidebarPinned, sidebarPinned, railWidth, winW, effectiveSidebarWidth, mobileSessions, openSidebar, closeSidebar, previewExpanded,
    sidebarOpen, flyout, memorySwitch: { store: boundStore, composerSlotRef, inputRef, pendingFilesRef, pasteBlocksRef, pendingSessionsRef, drafts, fileDrafts, pasteDrafts, sessionRefDrafts, pickedFileTokens, saveDrafts, setActionError, composerMountedRef, lockRef: memorySwitchingRef },
  })

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
  // The two icon-only controls at the header bar's right end look alike, so
  // each shows its own aria-label in an InstantTip on hover or focus, at once,
  // instead of a native title the browser holds back for about a second.
  const popOutTip = useLabelTip(i18nT('pages.chatPage.pop_out_session_to_its_own_window'))
  const activityTip = useLabelTip(i18nT('pages.chatPage.open_activity_panel'))

  return (
    <ChatPageFrame
      embedded={embedded} activeSlot={activeSlot} chatConfig={chatConfig} jiraSourceHosts={jiraSourceHosts} runInTerminalScope={runInTerminalScope} activitySlot={activitySlot}
      chatContainerRef={chatContainerRef} closedSessionActions={closedSessionActions} sidebarFolderActions={sidebarFolderActions}
    >
      <SessionsSidebarChrome
        embedMode={embedMode} mode={mode} activeSlot={activeSlot} connected={connected} creatingSlot={creatingSlot} dispatch={dispatch} filteredSlots={filteredSlots} isMobile={isMobile}
        keyboardInset={keyboardInset} mobileNavRail={mobileNavRail} surfaceUnreadSlots={surfaceUnreadSlots} titleInTopbar={titleInTopbar} topbarSlot={topbarSlot} topbarTrailSlot={topbarTrailSlot}
        vv={vv} activityOpen={activityOpen} selectFromSidebar={selectFromSidebar} history={history} historyHasMore={historyHasMore} navigateToEmbeddedSlot={navigateToEmbeddedSlot}
        openEmbeddedCrewWindow={openEmbeddedCrewWindow} splitMode={splitMode} defaultAgent={defaultAgent} installedAgents={installedAgents} sidebarAgents={sidebarAgents} sidebarChoices={sidebarChoices}
        setActionError={setActionError} showActionError={showActionError} canStageSessionRef={canStageSessionRef} chatPaneEl={chatPaneEl} stageSessionRef={stageSessionRef}
        openSlotInNewTab={openSlotInNewTab} ownsSessionTabs={ownsSessionTabs} activePoppedOut={activePoppedOut} focusActivePopout={focusActivePopout} openActivePopout={openActivePopout}
        splitFeatureEnabled={splitFeatureEnabled} currentSlot={currentSlot} effectiveMode={effectiveMode} title={title} activeIsSplitAnchor={activeIsSplitAnchor} editingTitle={editingTitle}
        enterSplit={enterSplit} setEditingTitleSlot={setEditingTitleSlot} setSidebarDragging={setSidebarDragging} setSidebarWidth={setSidebarWidth} sidebarAutoHidden={sidebarAutoHidden}
        sidebarDragging={sidebarDragging} splitAnchorForActive={splitAnchorForActive} effectiveSidebarWidth={effectiveSidebarWidth} toggleAct={toggleAct} winW={winW} closeSidebar={closeSidebar}
        containerH={containerH} drawerDragging={drawerDragging} drawerMounted={drawerMounted} drawerPanelRef={drawerPanelRef} drawerScrim={drawerScrim} drawerScrimRef={drawerScrimRef}
        drawerX={drawerX} expandFrom={expandFrom} expandSidebar={expandSidebar} flyout={flyout} flyoutEligible={flyoutEligible} flyoutSurfaceRef={flyoutSurfaceRef} flyoutTriggerRef={flyoutTriggerRef}
        openSidebar={openSidebar} revealSourceLink={revealSourceLink} sidebarOnScreen={sidebarOnScreen} sidebarOpen={sidebarOpen} flyoutSwitch={flyoutSwitch} flyoutNew={flyoutNew}
        mobileNewSessionItem={mobileNewSessionItem} topbarSessionsToggle={topbarSessionsToggle}
      />
      <ComposerDraftSync store={composerDraft} inputRef={inputRef} onCommit={onComposerDraftCommit} />

      {/* Chat pane */}
      {embedMode !== 'sessions' && (
      <div ref={setChatPaneEl} className={`relative flex flex-col ${embedded || popout ? 'bg-bg ' : ''}min-w-0 min-h-0 h-full overflow-hidden ${(activityOpen && !activitySlot) || search.isOpen ? 'flex-[1_1_60%]' : 'flex-1'}`} style={{ transition: 'flex 0.2s', ...(!sidebarOpen && !isMobile ? { marginLeft: -COLLAPSED_PANE_RECLAIM_PX } : {}), '--mc-content-width': scaleContentWidth(CONTENT_WIDTH[chatConfig.contentWidth], chatConfig.contentWidth, chatConfig.messageFontSize).messages, '--mc-input-width': scaleContentWidth(CONTENT_WIDTH[chatConfig.contentWidth], chatConfig.contentWidth, chatConfig.messageFontSize).input } as React.CSSProperties}>
        <ChatPaneChrome
          embedded={embedded} activeSlot={activeSlot} connected={connected} dispatch={dispatch} isMobile={isMobile} navigate={navigate} provider={provider} setVoiceRecoverySlot={setVoiceRecoverySlot}
          switchSlotGone={switchSlotGone} titleInTopbar={titleInTopbar} undeletableHistory={undeletableHistory} unresumableResume={unresumableResume} crewWindow={crewWindow} messages={messages}
          slotRunning={slotRunning} splitMode={splitMode} selectionCapabilitiesQ={selectionCapabilitiesQ} actionError={actionError} setActionError={setActionError} setUploadError={setUploadError}
          setUploadHint={setUploadHint} snipFrame={snipFrame} uploadError={uploadError} uploadHint={uploadHint} handleSnipCancel={handleSnipCancel} handleSnipComplete={handleSnipComplete}
          closeSessionTab={closeSessionTab} openShortNameMatch={openShortNameMatch} ownsSessionTabs={ownsSessionTabs} selectSessionTab={selectSessionTab} sessionTabs={sessionTabs}
          setSidError={setSidError} setSidMatches={setSidMatches} sidError={sidError} sidMatches={sidMatches} splitFeatureEnabled={splitFeatureEnabled} chipDefault={chipDefault}
          dismissPinStatus={dismissPinStatus} pinError={pinError} pinStatus={pinStatus} inlineSidePanelShowing={inlineSidePanelShowing} openSidebar={openSidebar} sidebarOpen={sidebarOpen}
          COLLAPSED_SESSION_TABS_INSET={COLLAPSED_SESSION_TABS_INSET}
        />
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
          <ChatEmptyState
            mode={mode} dispatch={dispatch} pendingAgent={pendingAgent} pendingAgentKindRef={pendingAgentKindRef} pendingModel={pendingModel} newSessionRef={newSessionRef}
            newSlotFailed={newSlotFailed} newSlotMutation={newSlotMutation} setNewSlotFailed={setNewSlotFailed} setSidError={setSidError}
          />
        ) : (
          <SearchHighlightContext.Provider value={searchCtxValue}>
          <div className="relative flex flex-col flex-1 min-h-0" {...dropTargetProps}>
            <ChatTitleOverlay
              embedMode={embedMode} popout={popout} activeSlot={activeSlot} dispatch={dispatch} filteredSlots={filteredSlots} isMobile={isMobile} titleInTopbar={titleInTopbar}
              activityOpen={activityOpen} setActionError={setActionError} showActionError={showActionError} onPinCollapsedHeight={onPinCollapsedHeight} pinCardRef={pinCardRef}
              pinExpanded={pinExpanded} pinFoldRef={pinFoldRef} pinned={pinned} scrollToPinnedPrompt={scrollToPinnedPrompt} scrollTranscriptBy={scrollTranscriptBy} setPinExpanded={setPinExpanded}
              activePoppedOut={activePoppedOut} focusActivePopout={focusActivePopout} openActivePopout={openActivePopout} returnSelfToMain={returnSelfToMain} splitFeatureEnabled={splitFeatureEnabled}
              currentSlot={currentSlot} effectiveMode={effectiveMode} title={title} activeIsSplitAnchor={activeIsSplitAnchor} editingTitle={editingTitle} enterSplit={enterSplit}
              setEditingTitleSlot={setEditingTitleSlot} setSidebarPinned={setSidebarPinned} sidebarAutoHidden={sidebarAutoHidden} sidebarPinned={sidebarPinned} sidePanelDock={sidePanelDock}
              splitAnchorForActive={splitAnchorForActive} toggleAct={toggleAct} openSidebar={openSidebar} sidebarOnScreen={sidebarOnScreen} sidebarOpen={sidebarOpen}
              sidePanelWantsMount={sidePanelWantsMount} mobileNewSessionItem={mobileNewSessionItem} mobileSessionsToggle={mobileSessionsToggle} popOutTip={popOutTip} activityTip={activityTip}
            />
            <ChatDropOverlay active={dragOver} />
            <ChatTranscriptView
              embedded={embedded} popout={popout} activeSlot={activeSlot} appToolCallIds={appToolCallIds} titleInTopbar={titleInTopbar} activityOpen={activityOpen}
              completedTurnCount={completedTurnCount} cursorIsForActiveSlot={cursorIsForActiveSlot} kiroCrewVersion={kiroCrewVersion} loadingOlder={loadingOlder} olderFailed={olderFailed}
              setTurnDisclosureFor={setTurnDisclosureFor} slotHasMore={slotHasMore} slotLoading={slotLoading} slotRunning={slotRunning} slotState={slotState} slotStopping={slotStopping}
              slotSwitchTarget={slotSwitchTarget} turnDisclosure={turnDisclosure} turnRowMayBeLive={turnRowMayBeLive} setInput={setInput} isWelcomeState={isWelcomeState} chatConfig={chatConfig}
              scrollerRef={scrollerRef} bindScroller={bindScroller} handleSurveyLayoutChange={handleSurveyLayoutChange} onScrollPin={onScrollPin} pinned={pinned} approve={approve}
              dismissApproval={dismissApproval} currentSlot={currentSlot} regenerating={regenerating} sendUnconfirmed={sendUnconfirmed} dockH={dockH} lastRole={lastRole} streamTick={streamTick}
              toggleAct={toggleAct} renderedDisplayItems={renderedDisplayItems} stableMsgKey={stableMsgKey} virtualKey={virtualKey} handleLoadEarlier={handleLoadEarlier}
              spinnerNearTop={spinnerNearTop} virt={virt} visibleDisplayItems={visibleDisplayItems} chatNav={chatNav} navigateToTurn={navigateToTurn} turnStepAnnouncement={turnStepAnnouncement}
              renderFarmItem={renderFarmItem} renderMessage={renderMessage} renderTurnItem={renderTurnItem}
            />
            <ComposerDockView
              embedded={embedded} mode={mode} activeSlot={activeSlot} connected={connected} dispatch={dispatch} isMobile={isMobile} isTouch={isTouch} navigate={navigate} provider={provider}
              activityOpen={activityOpen} composerBusy={composerBusy} contextPct={contextPct} contextTokens={contextTokens} knowledgeFetch={knowledgeFetch} messages={messages}
              pendingFollowup={pendingFollowup} pendingQuestion={pendingQuestion} projectLocked={projectLocked} restoredQuestionNotice={restoredQuestionNotice} sentMessages={sentMessages}
              slotRunning={slotRunning} slotStopping={slotStopping} composerDraft={composerDraft} historySuggestions={historySuggestions} showHistorySuggestions={showHistorySuggestions}
              chatConfig={chatConfig} agentDropdown={agentDropdown} agentDropdownRef={agentDropdownRef} agentFilter={agentFilter} agentInputRef={agentInputRef} defaultAgent={defaultAgent}
              defaultAgentFailed={defaultAgentFailed} effectiveAgents={effectiveAgents} filteredAgents={filteredAgents} installedAgents={installedAgents} setAgentDropdown={setAgentDropdown}
              setAgentFilter={setAgentFilter} toggleDefaultAgent={toggleDefaultAgent} codexPairModels={codexPairModels} filteredModels={filteredModels} hiddenModelsQ={hiddenModelsQ}
              jevRouteOn={jevRouteOn} modelDropdown={modelDropdown} modelDropdownRef={modelDropdownRef} modelFilter={modelFilter} modelInputRef={modelInputRef}
              modelPickerConfigured={modelPickerConfigured} modelPickerReturnsFocusRef={modelPickerReturnsFocusRef} setModelDropdown={setModelDropdown} setModelFilter={setModelFilter}
              onAgentListKeyDown={onAgentListKeyDown} onModelListKeyDown={onModelListKeyDown} anchorModelBtn={anchorModelBtn} modelBtnRect={modelBtnRect} messageQuote={messageQuote}
              applyAutomationChange={applyAutomationChange} automation={automation} automationCreationReady={automationCreationReady} automationOpen={automationOpen}
              automationSnapshotFailed={automationSnapshotFailed} setAutomationOpen={setAutomationOpen} prefillHint={prefillHint} setPrefillHint={setPrefillHint} pasteSlice={pasteSlice}
              pendingFiles={pendingFiles} pendingSessions={pendingSessions} resizedInfo={resizedInfo} unstageSessionRef={unstageSessionRef} uploading={uploading} agentBtnRect={agentBtnRect}
              anchorAgentBtn={anchorAgentBtn} projectPickerOpen={projectPickerOpen} setProjectPickerOpen={setProjectPickerOpen} anchorProjectBtn={anchorProjectBtn} projectBtnRect={projectBtnRect}
              pendingDirs={pendingDirs} composerRef={composerRef} composerVoiceOptions={composerVoiceOptions} isMac={isMac} cancelUpload={cancelUpload} composerUploadFiles={composerUploadFiles}
              dropTargetProps={dropTargetProps} handleCapture={handleCapture} handleFileOpen={handleFileOpen} handleOptimizeResult={handleOptimizeResult} search={search} tabsCtl={tabsCtl}
              uploadCount={uploadCount} sendHeld={sendHeld} composerBandRef={composerBandRef} scrollBottom={scrollBottom} handleResumeSession={handleResumeSession}
              composerRootChange={composerRootChange} composerUserEdit={composerUserEdit} followUpMulti={followUpMulti} followUpOptions={followUpOptions} followUpPicked={followUpPicked}
              followUpPickedRef={followUpPickedRef} followUpSourceKey={followUpSourceKey} toggleFollowUpOption={toggleFollowUpOption} dashCfg={dashCfg} jevAutoConsented={jevAutoConsented}
              refusedPress={refusedPress} setRefusedPress={setRefusedPress} send={send} pickModel={pickModel} setProject={setProject} switchAgent={switchAgent} currentSlot={currentSlot}
              activeFolderName={activeFolderName} anchorSessionControl={anchorSessionControl} openSessionControl={openSessionControl} refreshSessionControlStatuses={refreshSessionControlStatuses}
              sessionControlKey={sessionControlKey} sessionControlRect={sessionControlRect} sessionControls={sessionControls} setOpenSessionControl={setOpenSessionControl}
              activeAgentName={activeAgentName} attachmentAnnouncement={attachmentAnnouncement} clampOutOfTokens={clampOutOfTokens} handleAddToContext={handleAddToContext}
              mentionKeyEdit={mentionKeyEdit} optimizerMentionGuard={optimizerMentionGuard} mentionEditRange={mentionEditRange} removeDirChip={removeDirChip} removeFileChip={removeFileChip} selectPickedFile={selectPickedFile}
              followupAddToSession={followupAddToSession} followupStartInNewChat={followupStartInNewChat} followupStartInWorktree={followupStartInWorktree} displayMode={displayMode}
              _modelPinActive={_modelPinActive} _modelPinAgent={_modelPinAgent} _modelPinPinned={_modelPinPinned} _pinShownModel={_pinShownModel} _slotProject={_slotProject}
              defaultEffort={defaultEffort} effectiveEffort={effectiveEffort} effortLevelsOverride={effortLevelsOverride} effortSupported={effortSupported} modelMarker={modelMarker}
              pinModelToAgentMut={pinModelToAgentMut} projectBranch={projectBranch} projectGit={projectGit} projectGitError={projectGitError} shownModel={shownModel}
              cardOwnsRecovery={cardOwnsRecovery} continuable={continuable} continuing={continuing} handleContinue={handleContinue} interrupted={interrupted} dockGutter={dockGutter} dockRef={dockRef}
              endQuoteFlight={endQuoteFlight} flyingQuote={flyingQuote} inputAreaRef={inputAreaRef} isAtBottom={isAtBottom} answerQuestionCard={answerQuestionCard}
              handleCancelQueued={handleCancelQueued} handleEditQueued={handleEditQueued} handleInterruptQueued={handleInterruptQueued} handleReorderQueued={handleReorderQueued}
              keepQuestionAnswer={keepQuestionAnswer} queuedMessages={queuedMessages} queuePendingIds={queuePendingIds} steer={steer} stopTurn={stopTurn} systemDeliveryCount={systemDeliveryCount}
              handleEditLastRequest={handleEditLastRequest} composerOptimizing={composerOptimizing} handleOptimizingChange={handleOptimizingChange} memorySwitching={memorySwitching}
              showComposerMemoryChip={showComposerMemoryChip} switchMemoryMode={switchMemoryMode} composerAbove={composerAbove} composerSessionControls={composerSessionControls}
            />
          </div>
          </SearchHighlightContext.Provider>
        )}
      </div>
      )}
      <ChatSidePanels
        mode={mode} activeSlot={activeSlot} connected={connected} isMobile={isMobile} activityOpen={activityOpen} cursorIsForActiveSlot={cursorIsForActiveSlot} messages={messages}
        slotHasMore={slotHasMore} insertDashboardReply={insertDashboardReply} addSourceCommentToChat={addSourceCommentToChat} handleArtifactOpen={handleArtifactOpen} handleFileOpen={handleFileOpen}
        handleFileSave={handleFileSave} handleOpenWorkingTreeDiff={handleOpenWorkingTreeDiff} hasBrowserTab={hasBrowserTab} hasLiveAppTab={hasLiveAppTab} panelIssues={panelIssues}
        panelSources={panelSources} reconcileIssueUrl={reconcileIssueUrl} reconcileSourceUrl={reconcileSourceUrl} search={search} selectedIssueUrl={selectedIssueUrl}
        selectedSourceUrl={selectedSourceUrl} selectIssueUrl={selectIssueUrl} selectSourceUrl={selectSourceUrl} tabsCtl={tabsCtl} submitComments={submitComments} currentSlot={currentSlot}
        handleAddToContext={handleAddToContext} sidePanelDock={sidePanelDock} sidePanelDockAnim={sidePanelDockAnim} effectiveSidebarWidth={effectiveSidebarWidth} toggleAct={toggleAct}
        chatNav={chatNav} chatPins={chatPins} chatPinsLoading={chatPinsLoading} handleJumpToPin={handleJumpToPin} handleUnpinById={handleUnpinById} jumpToSearchResult={jumpToSearchResult}
        activeSlotTitle={activeSlotTitle} activitySlot={activitySlot} hasTaskDashboard={hasTaskDashboard} sidebarOpen={sidebarOpen} sideOverlayPanelRef={sideOverlayPanelRef}
        sideOverlayPhase={sideOverlayPhase} sideOverlayX={sideOverlayX} panelFillWidth={panelFillWidth} panelMaximized={panelMaximized} panelReserve={panelReserve}
        pinSummary={sidePanelPinSummary}
      />
    </ChatPageFrame>
  )
}
