/**
 * Direct characterization coverage for ChatPageView's composition boundary.
 *
 * The child components are prop recorders: these tests exercise the routing and
 * state decisions owned by the page view without re-testing every child widget.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'
import type { ReactNode } from 'react'

import ChatPageView, {
  type ChatPageViewProps,
} from '../pages/chat/ChatPageView'

type AnyProps = Record<string, unknown>

const { apiMocks } = vi.hoisted(() => ({
  apiMocks: {
    generateTitle: vi.fn().mockResolvedValue({}),
    renameSlot: vi.fn().mockResolvedValue({}),
    resolveApproval: vi.fn().mockResolvedValue({}),
  },
}))

let snipProps: AnyProps | null = null
let pinnedProps: AnyProps | null = null
let pinnedPillProps: AnyProps | null = null
let knowledgeProps: AnyProps | null = null
let pendingQuestionProps: AnyProps | null = null
let inputProps: AnyProps | null = null
let headerProps: AnyProps | null = null
let gridProps: AnyProps | null = null
let agentListProps: AnyProps | null = null
let defaultAgentProps: AnyProps | null = null
let manageAgentsProps: AnyProps | null = null
let modelProps: AnyProps | null = null
let effortProps: AnyProps | null = null
let voiceDisabledProps: AnyProps | null = null

vi.mock('../api/client', () => ({ api: apiMocks }))

vi.mock('react-dom', async (importOriginal) => ({
  ...(await importOriginal<typeof import('react-dom')>()),
  createPortal: (node: ReactNode) => node,
}))

vi.mock('framer-motion', async (importOriginal) => {
  const React = await import('react')
  const actual = await importOriginal<typeof import('framer-motion')>()
  const Div = ({ children, className, style }: { children?: ReactNode; className?: string; style?: object }) =>
    React.createElement('div', { className, style }, children)
  return { ...actual, AnimatePresence: ({ children }: { children?: ReactNode }) => children, motion: { div: Div } }
})

vi.mock('../components/AgentDropdownList', () => ({
  default: (props: AnyProps) => { agentListProps = props; return null },
  DefaultAgentRow: (props: AnyProps) => { defaultAgentProps = props; return null },
  ManageAgentsFooter: (props: AnyProps) => { manageAgentsProps = props; return null },
}))
vi.mock('../components/ChatDropOverlay', () => ({ default: () => null }))
vi.mock('../components/ChatInput', () => ({
  default: (props: AnyProps) => { inputProps = props; return null },
}))
vi.mock('../components/Clickable', async () => {
  const React = await import('react')
  return { default: ({ children, onClick, ...props }: { children?: ReactNode; onClick?: () => void }) => React.createElement('button', { ...props, onClick }, children) }
})
vi.mock('../components/DetailPanel', () => ({ default: ({ children }: { children?: ReactNode }) => children }))
vi.mock('../components/ErrorNotice', () => ({ default: () => null }))
vi.mock('../components/FlyingQuote', () => ({ default: (props: AnyProps) => <button data-testid="flying-quote" aria-label="flying quote" onClick={() => (props.onComplete as () => void)()} /> }))
vi.mock('../components/FollowUpCard', () => ({ default: () => null }))
vi.mock('../components/InboundLinkChip', () => ({ default: () => null }))
vi.mock('../components/InfoTip', () => ({ default: () => null }))
vi.mock('../components/ModelEffortDropdown', () => ({ default: (props: AnyProps) => { modelProps = props; return null } }))
vi.mock('../components/OverlayDrawer', () => ({ default: ({ children }: { children?: ReactNode }) => children }))
vi.mock('../components/PendingQuestionCard', () => ({ default: (props: AnyProps) => { pendingQuestionProps = props; return null } }))
vi.mock('../components/ProjectPicker', () => ({ default: () => null }))
vi.mock('../components/QueueStack', () => ({ default: () => null, SubagentDeliveryProgress: () => null }))
vi.mock('../components/ReasoningEffortDropdown', () => ({ default: (props: AnyProps) => { effortProps = props; return null } }))
vi.mock('../components/SearchBar', () => ({ default: () => null }))
vi.mock('../components/SearchResultsList', () => ({ default: () => null }))
vi.mock('../components/SessionGridView', () => ({ default: (props: AnyProps) => { gridProps = props; return null } }))
vi.mock('../components/SessionPulseSurveyCard', () => ({ default: () => null }))
vi.mock('../components/SessionTabStrip', () => ({ default: () => null }))
vi.mock('../components/SlotTagPopover', () => ({ default: () => null }))
vi.mock('../components/SnipOverlay', () => ({ default: (props: AnyProps) => { snipProps = props; return null } }))
vi.mock('../components/TipCard', () => ({ TipCard: () => null }))
vi.mock('../components/TypewriterText', () => ({ default: ({ text }: { text: string }) => <>{text}</> }))
vi.mock('../components/ui', async () => {
  const React = await import('react')
  return {
    Btn: ({ children, onClick, ...props }: { children?: ReactNode; onClick?: () => void }) => React.createElement('button', { ...props, onClick }, children),
    EmptyState: () => null,
    Input: React.forwardRef<HTMLInputElement, React.ComponentProps<'input'>>((props, ref) => React.createElement('input', { ...props, ref })),
  }
})
vi.mock('../components/VoiceDisabledModal', () => ({ default: (props: AnyProps) => { voiceDisabledProps = props; return null } }))
vi.mock('../components/WelcomeView', () => ({ default: () => null }))
vi.mock('../pages/ChatSidebar', () => ({ default: () => null }))
vi.mock('../pages/chat', () => ({
  ChatFooter: () => null,
  PinnedPrompt: (props: AnyProps) => { pinnedProps = props; return null },
  PinnedPromptPill: (props: AnyProps) => { pinnedPillProps = props; return null },
}))
vi.mock('../pages/chat/ChatPageMessageContent', () => ({
  ChatHeaderMenu: (props: AnyProps) => { headerProps = props; return null },
  KnowledgeBubbleChip: () => null,
  msgIdentityKey: () => 'message-key',
}))
vi.mock('../pages/chat/CollapsibleToolGroup', () => ({ default: (props: AnyProps) => <>{props.children as ReactNode}</> }))
vi.mock('../pages/chat/EarlierMessagesBar', () => ({ default: () => null }))
vi.mock('../pages/chat/FolderSuggestionCard', () => ({ default: () => null }))
vi.mock('../pages/chat/KnowledgePicker', () => ({ KnowledgePicker: (props: AnyProps) => { knowledgeProps = props; return null } }))
vi.mock('../pages/chat/rowDisclosure', () => ({ RowDisclosureProvider: ({ children }: { children?: ReactNode }) => <>{children}</> }))
vi.mock('../pages/chat/SessionFlyout', () => ({ default: () => null, TOGGLE_RECT: { x: 0, y: 0, w: 1, h: 1 } }))
vi.mock('../pages/chat/SidePanel', () => ({ default: () => null }))
vi.mock('../pages/chat/SubagentProgressBar', () => ({ default: () => null }))
vi.mock('../pages/chat/TaskProgressBar', () => ({ default: () => null }))
vi.mock('../pages/chat/TurnBlock', () => ({ default: () => null }))
vi.mock('../pages/chat/WorkflowProgressBar', () => ({ default: () => null }))
vi.mock('../hooks/useTagPopover', () => ({ TagPopoverProvider: ({ children }: { children?: ReactNode }) => <>{children}</> }))

const fn = () => vi.fn()
const ref = <T,>(current: T) => ({ current })
const rect = { top: 40, left: 20, right: 60, bottom: 80, width: 40, height: 40, x: 20, y: 40, toJSON: () => ({}) } as DOMRect

function makeProps(): ChatPageViewProps {
  const dispatch = vi.fn(() => ({ unwrap: vi.fn().mockResolvedValue({}) }))
  return {
    actions: {
      activeAgentName: 'planner', approve: fn(), continuable: false, continuing: false,
      currentSlot: { key: 'slot-1', title: 'A chat', agent: 'planner', project: '/repo' },
      dashCfg: undefined, dismissApproval: fn(), dismissFollowup: fn(), flyingQuote: null,
      folderSuggestion: null, folderSuggestionAccept: fn(), folderSuggestionDecline: fn(),
      followupAddToSession: fn(), followUpOptions: [], followUpPicked: undefined,
      followUpSourceKey: undefined, followupStartInWorktree: fn(), handleCancelQueued: fn(),
      handleContinue: fn(), handleEditQueued: fn(), handleInterruptQueued: fn(),
      handleFollowUpSend: fn(), handleFollowUpSelect: fn(), handleReorderQueued: fn(), inputAreaRef: ref(null),
      interrupted: false, pendingFollowup: null, pendingQuestion: null, queuedMessages: [],
      queuePendingIds: new Set<string>(),
      refusedPress: null, regenerating: false, send: fn(), setFlyingQuote: fn(), setProject: fn(),
      setRefusedPress: fn(), steer: fn(), submitComments: fn(), switchAgent: fn(),
      switchModel: fn(), systemDeliveryCount: 0, toApiDecision: (action: string) => `api:${action}`,
    },
    composer: {
      agentBtnRect: null, agentDropdown: false, agentDropdownRef: ref(null), agentFilter: '', agentInputRef: ref(null),
      autoNudgeLoop: false, autoNudgeOpen: false, cancelVoice: fn(), canStageSessionRef: ref(null),
      chatConfig: { contentWidth: 'compact', collapseAllSteps: false, showContextPct: false, showContextTokens: false, sendOnEnter: 'enter', followUpLayout: 'chips' },
      chatPaneEl: null, defaultAgent: 'planner', defaultAgentFailed: false, filteredAgents: [{ name: 'planner' }],
      filteredModels: [{ name: 'gpt-5.4' }], historySuggestions: [], input: '', inputRef: ref(''),
      installedAgents: [{ name: 'planner', source: 'builtin' }], isMac: false, isWelcomeState: false,
      // Remote-crew ports (#7693): the shelf reads the peer-aware roster and the
      // peer's effort levels, so a local-only stub leaves both undefined.
      effectiveAgents: [{ name: 'planner', source: 'builtin' }], remoteCrew: { isRemote: false, capabilities: undefined },
      modelBtnRect: null, modelDropdown: false, modelDropdownRef: ref(null), modelFilter: '', modelInputRef: ref(null),
      newSessionRef: ref(false), onAgentListKeyDown: fn(), onModelListKeyDown: fn(), pasteBlocks: [],
      pendingAgent: null, pendingDirs: [], pendingFiles: [], pendingModel: null, pendingSessions: [],
      pickedFileTokens: ref({}), prefillHint: false, projectBtnRect: null, projectPickerOpen: false,
      reasoningEffortBtnRect: null, reasoningEffortDropdown: false, reasoningEffortDropdownRef: ref(null),
      resizedInfo: null, setAgentBtnRect: fn(), setAgentDropdown: fn(), setAgentFilter: fn(),
      setAutoNudgeLoop: fn(), setAutoNudgeOpen: fn(), setChatPaneEl: fn(), setInput: fn(),
      setModelBtnRect: fn(), setModelDropdown: fn(), setModelFilter: fn(), setPasteBlocks: fn(),
      setPendingFiles: fn(), setPrefillHint: fn(), setProjectBtnRect: fn(), setProjectPickerOpen: fn(),
      setReasoningEffortBtnRect: fn(), setReasoningEffortDropdown: fn(), setSnipFrame: fn(),
      setUploadError: fn(), setVoiceSetupOpen: fn(), showHistorySuggestions: false, snipFrame: null,
      snipSlotRef: ref(null), stageSessionRef: ref(null), startVoice: fn(), stopVoice: fn(),
      sttAvailable: true, sttDictationPanel: false, sttEnabled: false, sttProvider: 'native', toggleDefaultAgent: fn(), toggleVoice: fn(),
      unstageSessionRef: fn(), uploadError: '', uploading: false,
      voice: { recording: false, transcribing: false, error: null, level: 0, deviceLabel: '', deviceId: '', switchDevice: fn(), deviceSwitchIsLive: false, clearError: fn(), streamEnabled: false, sampleRef: ref(null), partial: '', download: null, prewarm: fn() },
      voiceCaretRef: ref(null), voiceOwned: false, voicePendingCaretRef: ref(null), voiceSetupOpen: false,
    },
    layout: {
      activeIsSplitAnchor: false, activePoppedOut: false, activityOpen: false, activitySlot: null,
      chatContainerRef: ref(null), closeSidebar: fn(), clearSplitOnSelect: fn(), containerH: 700,
      drawerDragging: false, drawerMounted: false, drawerPanelRef: ref(null), drawerScrim: 0 as never,
      drawerScrimRef: ref(null), drawerX: 0 as never, effectiveSidebarWidth: 300, enterSplit: fn(),
      expandFrom: null, expandSidebar: fn(), flyout: { open: false, openedBy: 'pointer', triggerProps: {}, surfaceProps: {}, close: fn() },
      flyoutEligible: false, flyoutNew: fn(), flyoutSurfaceRef: ref(null), flyoutSwitch: fn(), flyoutTriggerRef: ref(null),
      focusActivePopout: fn(), inlineSidePanelShowing: false, keyboardInset: 0, mobileSessions: false, navigateToEmbeddedSlot: fn(),
      openActivePopout: fn(), openSidebar: fn(), panelFillWidth: undefined, panelMaximized: false, panelReserve: undefined,
      returnSelfToMain: fn(), setSidebarDragging: fn(), setSidebarPinned: fn(), setSidebarWidth: fn(), setSplitMode: fn(),
      sidebarAutoHidden: ref(null), sidebarDragging: false, sidebarOpen: false, sidebarPinned: false,
      sidePanelDock: {} as never, sidePanelDockAnim: {} as never, sideOverlayPanelRef: ref(null), sideOverlayPhase: 'closed', sideOverlayX: 0 as never,
      splitAnchor: null, splitAnchorForActive: null, splitFeatureEnabled: false, splitMode: false, toggleAct: fn(),
      // At rest the two viewports agree, so the mobile drawer's inset is 0 — the
      // shape every non-keyboard render sees.
      vv: { height: 800, offsetTop: 0 }, winW: 1280,
    },
    page: {
      _modelPinActive: 'gpt-5.4', _modelPinAgent: 'planner', _modelPinPinned: false,
      activeSlot: 'slot-1', activeTip: null, appToolCallIds: new Set(), completedTurnCount: 0,
      composerBusy: false, connected: true, contextPct: 0, contextTokens: { window: 100_000 }, remoteContextWindow: 0, creatingSlot: false,
      cursorIsForActiveSlot: true, defaultEffort: '', displayMode: 'normal', effectiveEffort: '', effectiveMode: undefined,
      embedded: false, embedMode: undefined, filteredSlots: [{ key: 'slot-1', title: 'A chat' }], generatingTitleSlots: new Set(),
      history: [], historyHasMore: false, isMobile: false, jiraSourceHosts: [], kiroCrewVersion: 'test',
      knowledgeFetch: { results: [], query: '', loading: false, inject: fn(), clearResults: fn(), pendingKnowledge: null, clearPending: fn() },
      loadingOlder: false, messages: [{ role: 'user', content: 'hello' }], mode: 'normal', olderFailed: false,
      popout: false, projectBranch: 'main', projectGit: { path: '/repo', repo: true }, projectGitError: false,
      provider: { id: 'acp', capabilities: { agentTemplates: true, reasoningEffort: true }, getContextWindow: () => 100_000 },
      sentMessages: [], shownModel: 'gpt-5.4', slotHasMore: false, slotLoading: false, slotRunning: false,
      slotState: 'idle', slotStopping: false, slotSwitchTarget: null, surfaceUnreadSlots: [], title: 'A chat', titleDraft: 'A chat', turnDisclosure: {},
    },
    ports: {
      cancelTitleRef: ref(false), dismissTip: fn(), dispatch: dispatch as never, editingTitle: false, handleAddToContext: fn(),
      navigate: fn(), pinModelToAgentMut: { mutate: fn() }, revealSourceLink: fn(), setEditingTitleSlot: fn(),
      setGeneratingTitleSlots: fn(), setPendingPinnedJump: fn(), setTitleDraft: fn(), setTurnDisclosureFor: fn(),
      softStopAtMapRef: ref(new Map()), titleIme: { bindComposition: () => ({}), claimEnter: () => true, reset: fn() } as never,
    },
    resources: {
      addSourceCommentToChat: fn(), dragOver: false, dropTargetProps: {}, handleArtifactOpen: fn(), handleCapture: fn(), handleFileOpen: fn(),
      handleFileSave: fn(), handleOptimizeResult: fn(), hasBrowserTab: false, hasLiveAppTab: false, panelIssues: [], panelSources: [],
      reconcileIssueUrl: fn(), reconcileSourceUrl: fn(), search: { isOpen: false, term: '', setTerm: fn(), matches: [], currentIdx: 0, next: fn(), prev: fn(), close: fn(), caseSensitive: false, toggleCaseSensitive: fn(), focusNonce: 0, goTo: fn() },
      selectedIssueUrl: null, selectedSourceUrl: null, selectIssueUrl: fn(), selectSourceUrl: fn(), tabsCtl: { tabs: [], activeId: '' }, uploadFiles: fn(),
    },
    session: {
      closeSessionTab: fn(), handleResumeSession: fn(), newSlotFailed: false, newSlotMutation: { isPending: false, mutate: fn() },
      openSlotInNewTab: fn(), ownsSessionTabs: false, selectSessionTab: fn(), sessionTabs: { tabs: [], cue: null },
      setNewSlotFailed: fn(), setSidError: fn(), sidError: '',
    },
    transcript: {
      activeSlotTitle: 'A chat', chatNav: { links: [], resolving: false }, chatPins: [], chatPinsLoading: false,
      dismissPinStatus: fn(), displayItems: [], handleJumpToPin: fn(), handleLoadEarlier: fn(), handleUnpinById: fn(),
      isAtBottom: true, jumpToSearchResult: fn(), lastRole: 'user', pinStatus: '', renderMessage: () => <div data-testid="message" />,
      renderTurnItem: () => null, searchCtxValue: {} as never, stableMsgKey: () => 'stable', streamTick: 0,
      // The measure farm renders inside the scroll shell, so the view needs the
      // deferred list it measures, the row-key fn it identifies rows by, its
      // off-screen renderer, and the farm ports on `virt` (#7916).
      renderedDisplayItems: [], spinnerNearTop: true, renderFarmItem: () => null, virtualKey: () => 'stable',
      virt: {
        topSentinelRef: ref(null), bottomSentinelRef: ref(null), offsetBefore: 0, offsetAfter: 0, measureRef: () => () => {},
        farmIsMeasured: () => true, farmRowMounted: () => false, farmRecord: fn(),
      }, visibleDisplayItems: [],
    },
    transcriptEarly: {
      handleSurveyLayoutChange: fn(), onPinCollapsedHeight: fn(), onScrollPin: fn(), pinCardRef: ref(null),
      pinExpanded: false, pinFoldRef: ref(null), pinMinimized: false, pinned: null, scrollBottom: fn(), scrollerRef: ref(null), scrollToPinnedPrompt: fn(), setPinExpanded: fn(), setPinMinimized: fn(),
    },
  } as unknown as ChatPageViewProps
}

function call<T extends (...args: never[]) => unknown>(value: unknown, ...args: Parameters<T>) {
  return (value as T)(...args)
}

beforeEach(() => {
  snipProps = pinnedProps = pinnedPillProps = knowledgeProps = pendingQuestionProps = inputProps = headerProps = gridProps = null
  agentListProps = defaultAgentProps = manageAgentsProps = modelProps = effortProps = voiceDisabledProps = null
  vi.clearAllMocks()
})

afterEach(() => vi.restoreAllMocks())

describe('ChatPageView composition boundary', () => {
  it('routes transient mobile and composer surfaces without altering child behavior', async () => {
    const props = makeProps()
    props.page.isMobile = true
    props.layout.drawerMounted = true
    props.layout.mobileSessions = true
    props.composer.snipFrame = { x: 0, y: 0, width: 10, height: 10 } as never
    props.composer.uploadError = 'upload exploded'
    props.session.sidError = 'session error'
    props.transcript.pinStatus = 'pin error'
    props.transcriptEarly.pinned = { idx: 0, text: 'Pinned', full: 'Pinned', images: [], bodyBeyondPreview: false, push: 0, bannerH: 0 } as never
    props.actions.flyingQuote = { text: 'quote', from: 'assistant' } as never
    props.composer.historySuggestions = [{ key: 'history-1', title: 'Earlier chat' }] as never
    props.composer.showHistorySuggestions = true
    props.page.knowledgeFetch = { ...props.page.knowledgeFetch, results: [{ id: 'k1' }] } as never
    props.actions.pendingQuestion = { prompt: 'answer?' } as never
    props.page.connected = false
    props.composer.voiceSetupOpen = true

    render(<ChatPageView {...props} />)

    fireEvent.click(screen.getByText('upload exploded').parentElement!.querySelector('button')!)
    fireEvent.click(screen.getByText('session error').parentElement!.querySelector('button')!)
    fireEvent.click(screen.getByText('pin error').parentElement!.querySelector('button')!)
    fireEvent.keyDown(screen.getByRole('option'), { key: 'Enter' })
    fireEvent.click(screen.getByTestId('flying-quote'))

    await act(async () => {
      call<(file: File, slot: string | null) => void>(snipProps!.onComplete, new File(['x'], 'shot.png'))
      call<() => void>(snipProps!.onCancel)
      call<(selected: unknown[]) => void>(knowledgeProps!.onInject, [{ id: 'k1' }])
      call<() => void>(knowledgeProps!.onSkip)
      call<(text: string) => void>(pendingQuestionProps!.onDirectSend, 'offline answer')
      call<(text: string) => void>(inputProps!.onFollowUpSend, 'follow-up')
      call<() => void>(inputProps!.onDismissHint)
      call<() => void>(inputProps!.onStop)
      call<(rect: DOMRect) => void>(inputProps!.onReasoningEffortClick, rect)
      call<() => void>(pinnedProps!.onToggleExpanded)
      call<() => void>(pinnedProps!.onJump)
      call<() => void>(pinnedProps!.onMinimize)
      call<() => void>(voiceDisabledProps!.onClose)
      call<() => void>(voiceDisabledProps!.onOpenSettings)
    })

    expect(props.composer.setUploadError).toHaveBeenCalledWith('')
    expect(props.session.setSidError).toHaveBeenCalledWith('')
    expect(props.transcript.dismissPinStatus).toHaveBeenCalledOnce()
    expect(props.session.handleResumeSession).toHaveBeenCalledWith('history-1', 'Earlier chat')
    expect(props.actions.setFlyingQuote).toHaveBeenCalledWith(null)
    expect(props.resources.uploadFiles).toHaveBeenCalledWith(expect.any(Array), null)
    expect(props.composer.setSnipFrame).toHaveBeenCalledWith(null)
    expect(props.page.knowledgeFetch.inject).toHaveBeenCalledWith([{ id: 'k1' }])
    expect(props.page.knowledgeFetch.clearResults).toHaveBeenCalledOnce()
    expect(props.actions.handleFollowUpSend).toHaveBeenCalledWith('follow-up')
    expect(props.actions.send).not.toHaveBeenCalledWith('offline answer', 'slot-1')
    expect(props.composer.setInput).toHaveBeenCalledOnce()
    expect(props.composer.setPrefillHint).toHaveBeenCalledWith(false)
    expect(props.composer.setReasoningEffortBtnRect).toHaveBeenCalledWith(rect)
    expect(props.composer.setReasoningEffortDropdown).toHaveBeenCalledWith(true)
    expect(props.transcriptEarly.setPinExpanded).toHaveBeenCalledOnce()
    expect(props.transcriptEarly.scrollToPinnedPrompt).toHaveBeenCalledWith(0)
    expect(props.transcriptEarly.setPinMinimized).toHaveBeenCalledWith(true)
    expect(props.composer.setVoiceSetupOpen).toHaveBeenCalledWith(false)
    expect(props.ports.navigate).toHaveBeenCalledWith('/settings/voice')
  })

  it('swaps the pinned card for its restorable chip when minimized', () => {
    const props = makeProps()
    props.transcriptEarly.pinned = { idx: 0, text: 'Pinned', full: 'Pinned', images: [], bodyBeyondPreview: false, push: 0, bannerH: 0 } as never
    props.transcriptEarly.pinMinimized = true

    render(<ChatPageView {...props} />)

    expect(pinnedProps).toBeNull()
    expect(pinnedPillProps).not.toBeNull()
    call<() => void>(pinnedPillProps!.onRestore)
    expect(props.transcriptEarly.setPinMinimized).toHaveBeenCalledWith(false)
  })

  it('routes desktop header, grouped approvals, and composer dropdown ports', async () => {
    const props = makeProps()
    props.layout.flyoutEligible = true
    props.layout.flyout = { ...props.layout.flyout, open: true } as never
    props.composer.agentDropdown = true
    props.composer.agentBtnRect = rect
    props.composer.modelDropdown = true
    props.composer.modelBtnRect = rect
    props.composer.reasoningEffortDropdown = true
    props.composer.reasoningEffortBtnRect = rect
    const group = {
      kind: 'group', startIdx: 0,
      msgs: [
        { role: 'tool', content: 'tool result' },
        { role: 'permission', content: 'permission', meta: { approval_id: 'approval-1' } },
      ],
    }
    props.transcript.displayItems = [group] as never
    props.transcript.visibleDisplayItems = [{ mounted: true, index: 0, key: 'group-1', data: group }] as never

    render(<ChatPageView {...props} />)

    await act(async () => {
      call<() => void>(headerProps!.onReveal)
      call<(name: string) => void>(agentListProps!.onSelect, 'reviewer')
      call<() => void>(defaultAgentProps!.onSetDefault)
      call<() => void>(manageAgentsProps!.onManage)
      call<() => void>(modelProps!.onClose)
      call<() => void>(modelProps!.onSetDefault)
      call<() => void>(modelProps!.onPinToAgent)
      call<() => void>(effortProps!.onClose)
    })

    expect(props.layout.setSidebarPinned).toHaveBeenCalledWith(true)
    expect(props.ports.dispatch).toHaveBeenCalled()
    expect(props.actions.switchAgent).toHaveBeenCalledWith('reviewer')
    expect(props.composer.setAgentDropdown).toHaveBeenCalledWith(false)
    expect(props.composer.toggleDefaultAgent).toHaveBeenCalledWith('planner')
    expect(props.ports.navigate).toHaveBeenCalledWith('/capabilities?tab=templates')
    expect(props.composer.setModelDropdown).toHaveBeenCalledWith(false)
    expect(props.ports.navigate).toHaveBeenCalledWith(expect.stringContaining('/settings/chat?highlight='))
    expect(props.ports.pinModelToAgentMut.mutate).toHaveBeenCalledWith({ agent: 'planner', model: 'gpt-5.4' })
    expect(props.composer.setReasoningEffortDropdown).toHaveBeenCalledWith(false)
  })

  it('preserves split collapse ordering at the page boundary', () => {
    const props = makeProps()
    props.layout.splitMode = true
    props.layout.splitFeatureEnabled = true
    props.layout.splitAnchor = 'slot-1'

    render(<ChatPageView {...props} />)

    call<() => void>(gridProps!.onClose)
    call<(slot: string, ts: string, mid: string) => void>(gridProps!.onCollapse, 'slot-2', '100', 'mid-1')

    expect(props.layout.setSplitMode).toHaveBeenNthCalledWith(1, false)
    expect(props.ports.dispatch).toHaveBeenCalled()
    expect(props.layout.setSplitMode).toHaveBeenNthCalledWith(2, false)
    expect(props.ports.setPendingPinnedJump).toHaveBeenCalledWith({ slotKey: 'slot-2', messageTs: '100', mid: 'mid-1', origin: 'earlier' })
  })
})
