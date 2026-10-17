import type { NavigateFunction } from 'react-router-dom'
import type { RefObject, Dispatch, SetStateAction, MutableRefObject } from 'react'
import { createPortal } from 'react-dom'
import { Undo2 } from 'lucide-react'
import { Composer, type ComposerHandle, type ComposerVoiceOptions } from '../../../chat-core/composer/Composer'
import type { ComposerPasteSlice } from '../../../chat-core/composer/composerPastes'
import type { ComposerDraftStore } from '../../../chat-core/composer/draftStore'
import type { QuoteFlight } from '../../../chat-core/composer/selectionActions'
import type { UseMessageQuote } from '../../../chat-core/composer/useMessageQuote'
import AgentDropdownList, { isCrewmateChat, DefaultAgentRow, defaultWriteKind, ManageAgentsFooter, type DefaultWriteFailure } from '../../../components/AgentDropdownList'
import type { KiroCrewAgent } from '../../../components/AgentSelector'
import { MemoryModeChip, type MemoryMode } from '../../../components/MemoryModeChip'
import type { PromptHistoryItem } from '../../../components/composerPromptHistory'
import type { ResolvedSessionControl } from '../../../hooks/useSessionControls'
import type { ProviderAdapter, ModelInfo } from '../../../providers/types'
import type { AppDispatch } from '../../../store'
import type { ChatMessage, SessionInfo, ChatSlot } from '../../../types'
import type { ResizeInfo } from '../../../utils/resizeImage'
import type { SessionRef } from '../../../utils/sessionRefs'
import type { SlotApprovalMode } from '../../../utils/slotApprovalMode'
import { SETTINGS_DEFAULT_MODEL_ID } from '../../../hooks/useSettingHighlight'
import { settingsPath } from '../../../components/settingsPath'
import { agentOrDefaultLabel } from '../../../utils/agentLabel'
import { sessionMemoryTag } from '../../../lib/agentMemorySource'
import { dismissFollowupItem, type FollowupItem } from '../../../store/chatSlice'
import { Input } from '../../../components/ui'
import { KnowledgeBubbleChip } from '../ChatPageMessageContent'
import type { useChatPageResourcesController } from '../useChatPageResourcesController'
import type { useFileMentionActions } from './composerFileMentions'
import type { usePageFlags } from './pageEnvironment'
import type { useActiveSlotState } from './activeSlotState'
import type { useModelPickerModels } from './sessionPicks'
import { REFUSED_PRESS_TITLE_KEYS, type useRefusedPress } from './turnActions'
import type { useComposerSend } from './composerSend'
import type { useSessionControlChips } from './sessionControls'
import type { useComposerChips } from './composerChips'
import type { useSessionAutomation } from './sessionAutomation'
import { SELECTION_INERT_ATTR } from '../useSelectionInertOverlays'
import { legacyCodexEffort } from '../../../hooks/useInteractiveModels'
import { isUnpinnedModel, jevRouteShownModel } from '../../../lib/jevRoute'
import ProjectPicker from '../../../components/ProjectPicker'
import { ProjectAwsProfileSection } from '../../../components/ProjectAwsProfile'
import ModelEffortDropdown from '../../../components/ModelEffortDropdown'
import ChatInput from '../../../components/ChatInput'
import { useStableCallbackProps } from '../useStableCallbackProps'
import SessionControlHost from '../../../components/SessionControlHost'
import ErrorNotice from '../../../components/ErrorNotice'
import { pinIsWithheld } from '../../../lib/model'
import FollowUpCard from '../../../components/FollowUpCard'
import PendingQuestionCard from '../../../components/PendingQuestionCard'
import FlyingQuote from '../../../components/FlyingQuote'
import QueueStack, { SubagentDeliveryProgress } from '../../../components/QueueStack'
import SubagentProgressBar from '../SubagentProgressBar'
import TaskProgressBar from '../TaskProgressBar'
import type { ChatConfig } from '../ChatSettings'
import { KnowledgePicker } from '../KnowledgePicker'
import { JumpToBottomButton } from '../../../app-sdk/ChatScrollChrome'
import WorkflowProgressBar from '../WorkflowProgressBar'
import { tryQuickSend } from '../../../lib/quickSend'
import { i18nT } from '../../../i18n/t'
import { fmtDateFields } from '../../../i18n/format'

/**
 * The main composer, with its callback props held at stable identities. This
 * page builds most of them inline on every render; the forwarders let
 * `memo(ChatInput)` skip a page render (a pin, a streamed frame) that changes
 * nothing the composer shows.
 */
function StableChatInput(props: React.ComponentProps<typeof ChatInput>) {
  return <ChatInput {...useStableCallbackProps(props)} />
}

interface ComposerDockViewProps {
  embedded?: boolean
  mode?: string
  activeSlot: string
  connected: boolean
  dispatch: AppDispatch
  isMobile: boolean
  isTouch: boolean
  navigate: NavigateFunction
  provider: ProviderAdapter
  activityOpen: boolean
  composerBusy: boolean
  contextPct: number
  contextTokens: { used?: number; window: number; }
  knowledgeFetch: ReturnType<typeof useActiveSlotState>['knowledgeFetch']
  messages: ChatMessage[]
  pendingFollowup: { items: FollowupItem[]; ts: number; } | undefined
  pendingQuestion: ReturnType<typeof useActiveSlotState>['pendingQuestion']
  projectLocked: boolean
  restoredQuestionNotice: ReturnType<typeof useActiveSlotState>['restoredQuestionNotice']
  sentMessages: PromptHistoryItem[]
  slotRunning: boolean
  slotStopping: boolean
  composerDraft: ComposerDraftStore
  historySuggestions: SessionInfo[]
  showHistorySuggestions: boolean
  chatConfig: ChatConfig
  agentDropdown: boolean
  agentDropdownRef: RefObject<HTMLDivElement>
  agentFilter: string
  agentInputRef: RefObject<HTMLInputElement>
  defaultAgent: string
  defaultAgentFailed: false | DefaultWriteFailure
  effectiveAgents: KiroCrewAgent[]
  filteredAgents: KiroCrewAgent[]
  installedAgents: KiroCrewAgent[]
  setAgentDropdown: Dispatch<SetStateAction<boolean>>
  setAgentFilter: Dispatch<SetStateAction<string>>
  toggleDefaultAgent: (name: string, kind?: 'member' | 'template') => void
  codexPairModels: boolean
  filteredModels: ModelInfo[]
  hiddenModelsQ: ReturnType<typeof useModelPickerModels>['hiddenModelsQ']
  jevRouteOn: boolean
  modelDropdown: boolean
  modelDropdownRef: RefObject<HTMLDivElement>
  modelFilter: string
  modelInputRef: RefObject<HTMLInputElement>
  modelPickerConfigured: boolean
  modelPickerReturnsFocusRef: MutableRefObject<boolean>
  setModelDropdown: Dispatch<SetStateAction<boolean>>
  setModelFilter: Dispatch<SetStateAction<string>>
  onAgentListKeyDown: (e: React.KeyboardEvent) => void
  onModelListKeyDown: (e: React.KeyboardEvent) => void
  anchorModelBtn: (rect: DOMRect, trigger?: HTMLElement | null) => void
  modelBtnRect: DOMRect | null
  messageQuote: UseMessageQuote
  applyAutomationChange: ReturnType<typeof useSessionAutomation>['applyAutomationChange']
  automation: ReturnType<typeof useSessionAutomation>['automation']
  automationCreationReady: boolean
  automationOpen: boolean
  automationSnapshotFailed: boolean
  setAutomationOpen: Dispatch<SetStateAction<boolean>>
  prefillHint: boolean
  setPrefillHint: Dispatch<SetStateAction<boolean>>
  pasteSlice: ComposerPasteSlice
  pendingFiles: string[]
  pendingSessions: SessionRef[]
  resizedInfo: Record<string, ResizeInfo>
  unstageSessionRef: (key: string) => void
  uploading: boolean
  agentBtnRect: DOMRect | null
  anchorAgentBtn: (rect: DOMRect, trigger?: HTMLElement | null) => void
  projectPickerOpen: boolean
  setProjectPickerOpen: Dispatch<SetStateAction<boolean>>
  anchorProjectBtn: (rect: DOMRect, trigger?: HTMLElement | null) => void
  projectBtnRect: DOMRect | null
  pendingDirs: string[]
  composerRef: RefObject<ComposerHandle>
  composerVoiceOptions: ComposerVoiceOptions
  isMac: boolean
  cancelUpload: () => void
  composerUploadFiles: (files: File[]) => Promise<void> | undefined
  dropTargetProps: ReturnType<typeof useChatPageResourcesController>['dropTargetProps']
  handleCapture: () => Promise<void>
  handleFileOpen: ReturnType<typeof useChatPageResourcesController>['handleFileOpen']
  handleOptimizeResult: (slot: string | null, optimized: string) => void
  search: ReturnType<typeof useChatPageResourcesController>['search']
  tabsCtl: ReturnType<typeof useChatPageResourcesController>['tabsCtl']
  /** Live uploads `cancelUpload` aborts: above zero the control is offered,
   *  above one its label names the count. */
  uploadCount: number
  sendHeld: boolean
  composerBandRef: (el: HTMLDivElement | null) => void
  scrollBottom: (instant?: boolean) => void
  handleResumeSession: (key: string, title: string) => Promise<void>
  composerRootChange: (v: string) => void
  composerUserEdit: (v: string) => void
  followUpMulti: boolean
  followUpOptions: string[]
  followUpPicked: Set<string>
  followUpPickedRef: MutableRefObject<Set<string>>
  followUpSourceKey: string | null
  toggleFollowUpOption: (o: string) => void
  dashCfg: ReturnType<typeof usePageFlags>['dashCfg']
  jevAutoConsented: boolean
  refusedPress: ReturnType<typeof useRefusedPress>['refusedPress']
  setRefusedPress: ReturnType<typeof useRefusedPress>['setRefusedPress']
  send: ReturnType<typeof useComposerSend>['send']
  pickModel: (modelName: string) => void
  setProject: (path: string) => Promise<void>
  switchAgent: (agentName: string, kind?: 'member' | 'template') => Promise<void>
  currentSlot: ChatSlot | undefined
  activeFolderName: string
  anchorSessionControl: (rect: DOMRect, trigger?: HTMLElement | null) => void
  openSessionControl: { key: string; slot: string; } | null
  refreshSessionControlStatuses: () => void
  sessionControlKey: string
  sessionControlRect: DOMRect | null
  sessionControls: ResolvedSessionControl[]
  setOpenSessionControl: Dispatch<SetStateAction<{ key: string; slot: string; } | null>>
  activeAgentName: string
  attachmentAnnouncement: { text: string; nonce: number; }
  clampOutOfTokens: (text: string, at: number) => number
  handleAddToContext: (absPath: string, kind: 'file' | 'dir', dropAt?: number | null) => void
  mentionKeyEdit: ReturnType<typeof useFileMentionActions>['mentionKeyEdit']
  optimizerMentionGuard: (draft: string) => (rewrite: string) => boolean
  removeDirChip: (rel: string) => void
  removeFileChip: (p: string) => void
  selectPickedFile: ReturnType<typeof useFileMentionActions>['selectPickedFile']
  followupAddToSession: (item: FollowupItem) => void
  followupStartInNewChat: (item: FollowupItem) => Promise<void>
  followupStartInWorktree: (item: FollowupItem) => Promise<void>
  displayMode: SlotApprovalMode
  _modelPinActive: string
  _modelPinAgent: string
  _modelPinPinned: boolean
  _pinShownModel: string
  _slotProject: string
  defaultEffort: string
  effectiveEffort: string
  effortLevelsOverride: string[] | undefined
  effortSupported: boolean
  modelMarker: 'auto' | 'default' | null
  pinModelToAgentMut: ReturnType<typeof useComposerChips>['pinModelToAgentMut']
  projectBranch: string
  projectGit: ReturnType<typeof useComposerChips>['projectGit']
  projectGitError: boolean
  shownModel: string
  cardOwnsRecovery: boolean
  continuable: boolean
  continuing: boolean
  handleContinue: () => void
  interrupted: boolean
  dockGutter: number
  dockRef: (el: HTMLDivElement | null) => void
  endQuoteFlight: () => void
  flyingQuote: QuoteFlight | null
  inputAreaRef: RefObject<HTMLDivElement>
  isAtBottom: boolean
  answerQuestionCard: (text: string) => void
  handleCancelQueued: (queueId: string) => void
  handleEditQueued: (queueId: string, content: string) => void
  handleInterruptQueued: (queueId: string) => void
  handleReorderQueued: (queueId: string, direction: 'next' | 'later') => void
  keepQuestionAnswer: (text: string, questions?: { question: string; }[]) => void
  queuedMessages: ChatMessage[]
  queuePendingIds: ReadonlySet<string>
  steer: (opts?: { auto?: boolean; text?: string; }) => void
  stopTurn: () => void
  systemDeliveryCount: number
  handleEditLastRequest: () => void
  composerOptimizing: boolean
  handleOptimizingChange: (optimizing: boolean) => void
  memorySwitching: boolean
  showComposerMemoryChip: boolean
  switchMemoryMode: (newMode: MemoryMode) => Promise<void>
  composerAbove: JSX.Element
  composerSessionControls: ReturnType<typeof useSessionControlChips>
}

/**
 * The floating composer dock, the next positioned sibling after the scroller (that DOM order is what paints it over the transcript), and its contents: the jump-to-bottom pill, the status stack (task, sub-agent and workflow bars, queued messages), the flying quote, and the input area (refused-press notice, history suggestions, knowledge picker, question and follow-up cards, memory-mode chip, the composer), and the composer toolbar's popovers (the agent and model dropdowns, the project picker, the open app session control).
 *
 * Its body is not re-indented: `ChatPage.statusStackLayering.test.tsx` finds the status stack's end by its closing tag's depth (14 spaces).
 */
export default function ComposerDockView({
  embedded, mode, activeSlot, connected, dispatch, isMobile, isTouch, navigate, provider, activityOpen, composerBusy, contextPct, contextTokens, knowledgeFetch, messages, pendingFollowup,
  pendingQuestion, projectLocked, restoredQuestionNotice, sentMessages, slotRunning, slotStopping, composerDraft, historySuggestions, showHistorySuggestions, chatConfig, agentDropdown,
  agentDropdownRef, agentFilter, agentInputRef, defaultAgent, defaultAgentFailed, effectiveAgents, filteredAgents, installedAgents, setAgentDropdown, setAgentFilter, toggleDefaultAgent,
  codexPairModels, filteredModels, hiddenModelsQ, jevRouteOn, modelDropdown, modelDropdownRef, modelFilter, modelInputRef, modelPickerConfigured, modelPickerReturnsFocusRef, setModelDropdown,
  setModelFilter, onAgentListKeyDown, onModelListKeyDown, anchorModelBtn, modelBtnRect, messageQuote, applyAutomationChange, automation, automationCreationReady, automationOpen,
  automationSnapshotFailed, setAutomationOpen, prefillHint, setPrefillHint, pasteSlice, pendingFiles, pendingSessions, resizedInfo, unstageSessionRef, uploading, agentBtnRect, anchorAgentBtn,
  projectPickerOpen, setProjectPickerOpen, anchorProjectBtn, projectBtnRect, pendingDirs, composerRef, composerVoiceOptions, isMac, cancelUpload, composerUploadFiles, dropTargetProps, handleCapture,
  handleFileOpen, handleOptimizeResult, search, tabsCtl, uploadCount, sendHeld, composerBandRef, scrollBottom, handleResumeSession, composerRootChange, composerUserEdit, followUpMulti,
  followUpOptions, followUpPicked, followUpPickedRef, followUpSourceKey, toggleFollowUpOption, dashCfg, jevAutoConsented, refusedPress, setRefusedPress, send, pickModel, setProject, switchAgent,
  currentSlot, activeFolderName, anchorSessionControl, openSessionControl, refreshSessionControlStatuses, sessionControlKey, sessionControlRect, sessionControls, setOpenSessionControl,
  activeAgentName, attachmentAnnouncement, clampOutOfTokens, handleAddToContext, mentionKeyEdit, optimizerMentionGuard, removeDirChip, removeFileChip, selectPickedFile, followupAddToSession,
  followupStartInNewChat, followupStartInWorktree, displayMode, _modelPinActive, _modelPinAgent, _modelPinPinned, _pinShownModel, _slotProject, defaultEffort, effectiveEffort, effortLevelsOverride,
  effortSupported, modelMarker, pinModelToAgentMut, projectBranch, projectGit, projectGitError, shownModel, cardOwnsRecovery, continuable, continuing, handleContinue, interrupted, dockGutter, dockRef,
  endQuoteFlight, flyingQuote, inputAreaRef, isAtBottom, answerQuestionCard, handleCancelQueued, handleEditQueued, handleInterruptQueued, handleReorderQueued, keepQuestionAnswer, queuedMessages,
  queuePendingIds, steer, stopTurn, systemDeliveryCount, handleEditLastRequest, composerOptimizing, handleOptimizingChange, memorySwitching, showComposerMemoryChip, switchMemoryMode, composerAbove,
  composerSessionControls,
}: ComposerDockViewProps) {
  return (
    <>
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
            <div ref={dockRef} className="absolute left-0 bottom-0 pointer-events-none" style={{ right: dockGutter }} data-testid="composer-dock-root" {...{ [SELECTION_INERT_ATTR]: '' }}>
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
              {(pendingQuestion || restoredQuestionNotice) && (
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
                    projectIsRepo={projectGitError ? undefined : projectGit?.repo}
                    onAddToSession={followupAddToSession}
                    onStartInWorktree={followupStartInWorktree}
                    onStartInNewChat={followupStartInNewChat}
                    onSkip={(index) => dispatch(dismissFollowupItem({ slot: activeSlot, index, ts: pendingFollowup.ts }))}
                  />
                </div>
              )}
              {showComposerMemoryChip && (
                // No backdrop: the chip is glass and whatever scrolls under it is meant to show.
                // `w-fit`, not a full-width flex row: the box is a `dock-inert` child, so
                // it catches input, and it should be no wider than the chip it holds.
                <div className="relative z-10 mx-auto w-fit px-4 pt-2 pb-2" data-testid="composer-memory-chip">
                  <MemoryModeChip memoryMode={currentSlot?.memory_mode ?? 'persistent'} onSwitchMode={switchMemoryMode} switching={memorySwitching} disabled={composerOptimizing} />
                </div>
              )}
              {/* Announces why the composer is locked; in every session, as the user may move on mid-switch. */}
              <span role="status" className="sr-only" data-testid="memory-switch-status">{memorySwitching ? i18nT('components.welcomeView.switching_memory_mode') : ''}</span>
              <Composer
                ref={composerRef}
                slotKey={activeSlot}
                draft={composerDraft}
                onChange={composerRootChange}
                voice={composerVoiceOptions}
                pastes={pasteSlice}
              >
              <StableChatInput
              aboveComposer={composerAbove}
              // No `value`: ChatInput reads the text from the root's `draft` store.
              // `composerUserEdit` is what ChatInput calls for the user's own edits
              // (typing, paste, undo, picker inserts), never for a parent-driven
              // seed -- so it is the signal that arms the prefill hint's expiry.
              onChange={composerUserEdit}
              onSend={() => send()}
              terminalCommands={activeSlot && !currentSlot ? 'pending' : currentSlot?.executor === 'remote' ? 'remote' : 'local'}
              canSteer={composerBusy}
              onSteer={steer}
              // AND a turn actually running. `composerBusy` is also true when only
              // background sub-agents are working, and there is no turn to decide
              // ABOUT in that state: the send starts a fresh turn and the point
              // never runs, so offering the mode there would promise a decision
              // nothing makes.
              jevAutoAvailable={jevAutoConsented && !!slotRunning}
              onFollowUpSend={(text?: string) => send(text)}
              // Streaming, compaction and stopping keep the input interactive (api_chat queues on slot.running and stop preserves the
              // queue, so Enter queues a follow-up during the stop window). Only a memory-mode switch locks it, the reason as placeholder.
              disabled={memorySwitching} lockedPlaceholder={memorySwitching ? i18nT('components.welcomeView.switching_memory_mode') : undefined}
              autoFocusKey={activeSlot}
              prefillHint={prefillHint}
              onDismissHint={() => setPrefillHint(false)}
              onScreenshot={handleCapture}
              onUploadFiles={composerUploadFiles}
              /* Only while the active slot has a live upload request, whichever
                 host started it. `uploading` is shared with the screenshot
                 path, which has no request to cancel. */
              onCancelUpload={uploadCount > 0 ? cancelUpload : undefined}
              uploadCount={uploadCount}
              /* The one collapsible composer. Opt-in rather than default so the
                 shared preference key and the window-level expand event stay
                 correct by construction -- see ChatInput's `collapsible` prop. */
              collapsible
              /* `uploading` clears when the FIRST of several producers settles; the counted hold keeps the spinner and cancel control up until the last one does. */
              uploading={uploading || sendHeld}
              holdSend={sendHeld}
              pendingFiles={pendingFiles}
              pendingDirs={pendingDirs}
              resizedInfo={resizedInfo}
              onRemoveFile={removeFileChip}
              onRemoveDir={removeDirChip}
              attachmentAnnouncement={attachmentAnnouncement}
              pendingSessions={pendingSessions}
              pendingQuote={messageQuote.pendingQuote}
              onRemoveQuote={messageQuote.clearQuote}
              onRemoveSessionRef={unstageSessionRef}
              onFileSelect={selectPickedFile}
              onFileOpen={handleFileOpen}
              // A tree row dropped on the composer is "Add to chat" by drag.
              onTreeEntryDrop={handleAddToContext}
              clampDropOffset={clampOutOfTokens}
              onMentionKey={mentionKeyEdit}
              optimizerMentionGuard={optimizerMentionGuard}
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
              agentLabel={agentOrDefaultLabel(currentSlot?.agent, defaultAgent)}
              agentIsInheritedDefault={!currentSlot?.agent && !!defaultAgent}
              // Read from the same rows the picker lists, so the chip marks a
              // session exactly when the picker shows a same-name pair.
              agentMemory={sessionMemoryTag(currentSlot?.agent, currentSlot?.agent_kind, defaultAgent, effectiveAgents)}
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
              contextWindowTokens={contextTokens?.window || provider.getContextWindow(shownModel)}
              showContextPct={chatConfig.showContextPct}
              showContextTokens={chatConfig.showContextTokens}
              isRunning={composerBusy}
              projectLocked={projectLocked}
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

                 `cardOwnsRecovery` is the case where the card and the composer
                 would otherwise disagree (#13342): the newest row is one whose
                 card has withheld Resume because a retry replays the
                 rejection, and offers a remedy of its own instead -- the issue
                 form for a refused feature request, the restart hint for a
                 session start that failed twice, a fresh session for a
                 conversation too big for the model, or a refused app-owned
                 agent turn. A composer beneath it saying "press Resume" would
                 argue with the card, so it falls back to the ordinary Send
                 button; typing still works. */
              continuable={continuable && interrupted && !cardOwnsRecovery}
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
              onOptimizingChange={handleOptimizingChange}
              memoryMode={currentSlot?.memory_mode ?? 'persistent'}
              sentMessages={sentMessages}
              onEditLastRequest={handleEditLastRequest}
              sendOnEnter={isMobile && isTouch ? 'ctrl-enter' : chatConfig.sendOnEnter}
              followUpOptions={followUpOptions}
              followUpPicked={followUpPicked}
              quickSend={dashCfg?.quick_send}
              followUpLayout={chatConfig.followUpLayout}
              followUpSourceKey={followUpSourceKey}
              followUpMulti={followUpMulti}
              onFollowUpSelect={(o: string, e: React.MouseEvent, _key: string | null | undefined, sendNow: (text: string) => void) => {
                // One-click: enabled + no shift + not busy + not already in multi-select.
                // `sendNow` is the composer's chip send: it steers or queues per the slot's busy-send mode.
                if (tryQuickSend(o, dashCfg?.quick_send, e.shiftKey, slotRunning, followUpPickedRef.current.size, sendNow)) return
                toggleFollowUpOption(o)
              }}
              showFullPastes={chatConfig.showFullPastes}
              knowledgeChip={knowledgeFetch.pendingKnowledge ? <div className="flex items-start gap-1"><KnowledgeBubbleChip knowledge={{ items: knowledgeFetch.pendingKnowledge.items.length, tokens: knowledgeFetch.pendingKnowledge.totalTokens, titles: knowledgeFetch.pendingKnowledge.items.map(i => i.title), content: knowledgeFetch.pendingKnowledge.items.map(i => ({ title: i.title, text: i.content.slice(0, 2000) })) }} /><button type="button" onClick={() => knowledgeFetch.clearPending()} className="shrink-0 mt-0.5 p-0.5 text-muted hover:text-danger-text bg-transparent border-none cursor-pointer rounded hover:bg-danger/10 transition-colors" aria-label={i18nT('pages.chatPage.remove_knowledge_context')} title={i18nT('pages.chatPage.remove_knowledge_context')}>&times;</button></div> : undefined}
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
                {/* The default for new sessions is a template; a crewmate's chat offers no such write. */}
                {!embedded && !isCrewmateChat(currentSlot?.agent_kind, activeAgentName, installedAgents) && <DefaultAgentRow agentName={activeAgentName} isDefault={activeAgentName === defaultAgent} memoryTag={sessionMemoryTag(currentSlot?.agent, currentSlot?.agent_kind, defaultAgent, effectiveAgents)} onSetDefault={() => toggleDefaultAgent(activeAgentName, defaultWriteKind(currentSlot?.agent_kind))} />}
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
              startPath={_slotProject}
              errorHandoff
              footer={_slotProject
                ? <ProjectAwsProfileSection project={_slotProject} errorHandoff onHandoff={() => setProjectPickerOpen(false)} />
                : undefined}
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
    </>
  )
}
