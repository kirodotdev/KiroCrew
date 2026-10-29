import { useMemo, useRef, useCallback, Fragment, type MutableRefObject, type Dispatch, type SetStateAction } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Clock, AppWindow } from 'lucide-react'
import type { MessageQuoteRole, MessageQuote } from '../../../chat-core/composer/messageQuote'
import type { ChatMessage, ChatSlot } from '../../../types'
import type { CapabilityFix, FreshSessionState, RepairImagesState } from '../ErrorCard'
import { toApiDecision } from '../../../utils/approvalDecision'
import { isHiddenInvisibleAssistantRow } from '../../../utils/invisibleText'
import { type MessageRenderer, mergeRenderers, type MessageRenderContext, resolveRenderer } from '../../../app-sdk/messageRenderers'
import { createTranscriptRenderers } from '../transcriptRenderers'
import { useStableCallbackProps } from '../useStableCallbackProps'
import { api, type PlanStepInput } from '../../../api/client'
import type { FileChangeEntry } from '../../../components/FileChangeChips'
import CollapsibleToolGroup from '../CollapsibleToolGroup'
import { isSystemNoticeRow } from '../CompactionCard'
import { decisionStripFieldOf } from '../decisionRecord'
import type { TurnItem, DisplayItem } from '../types'
import { canForkAtWindow } from '../pagination'
import { messageRowKey, turnLeadKey, msgIdentityKey } from '../ChatPageMessageContent'
import type { useChatPageResourcesController } from '../useChatPageResourcesController'
import { MessageSearchScope } from '../../../hooks/SearchHighlightContext'
import { UserMessage, AssistantMessage } from '..'
import type { ReactionError } from '../../../hooks/useChatReactions'
import type { TurnStats } from '../AssistantMessage'
import { warmBriefBodyOf } from '../../../components/chat/WarmBriefCard'
import { prevUserTextFor } from '../share/shareSupport'
import { turnHadPolicyBlock } from '../../../app-sdk/turnPolicyBlock'
import MarkdownRenderer from '../../../components/MarkdownRenderer'
import MessageErrorBoundary from '../../../components/MessageErrorBoundary'
import { stripAppEnvelope, REASONING_ROLES } from '../groupDisplayItems'
import { parseOptions } from '../../../app-sdk/protocol'
import { isNoteRow } from '../../../lib/noteContract'
import type { ChatConfig } from '../ChatSettings'
import type { ParsedSubagentCompletion } from '../subagentCompletion'
import TurnBlock from '../TurnBlock'
import { i18nT } from '../../../i18n/t'
import { fmtMessageTime, fmtMessageTimeFull } from '../messageTime'
import type { useActiveSlotState } from './activeSlotState'
import type { RefusedPressAction } from './turnActions'
import { usePersistedBool } from '../../../hooks/usePersistedBool'
import type { SteerRetractAction, SteerRetractRefusalNotice } from '../../../hooks/useSteerRetract'

/** ChatPage's tool rows derive their auto-denied state inside ToolCallLine;
 *  the registry default that reads this set is never reached here. Frozen and
 *  shared so the per-row context does not allocate. */
const NO_AUTO_DENIED = new Set<string>()

/** A message row's own pin and whole-message quote handlers. */
interface MessageRowHandlers {
  role: 'user' | 'assistant'
  togglePin: () => void
  quote: (shown: string) => void
  toggleReaction: (emoji: string) => void
}

interface TranscriptRenderingOptions {
  embedded?: boolean
  mode?: string
  popout?: boolean
  filteredSlots: ChatSlot[]
  connected: boolean
  activeSlot: string | null
  voiceRecoverySlot: string | null
  appToolCallIds: ReadonlySet<string>
  mcpAppPanel: boolean
  messages: ChatMessage[]
  messagesRef: MutableRefObject<ChatMessage[]>
  slotRunning: boolean
  toolDisclosure: Record<string, boolean>
  setToolDisclosureFor: (key: string, expanded: boolean) => void
  slotState: ReturnType<typeof useActiveSlotState>['slotState']
  activityOpen: boolean
  slotHasMore: boolean
  slotOldestIndex: number
  cursorIsForActiveSlot: boolean
  loadingOlder: boolean
  chatConfig: ChatConfig
  activeSlotRemoteBound: boolean
  automationId: string | undefined
  setAutomationOpen: Dispatch<SetStateAction<boolean>>
  handleArtifactOpen: (slug: string) => Promise<void>
  handleFileOpen: ReturnType<typeof useChatPageResourcesController>['handleFileOpen']
  handleFolderOpen: (dirPath: string) => void
  handleOpenDiff: (filePath: string, modified: string, original: string) => void
  handleSubagentPanelOpen: (parsed: ParsedSubagentCompletion) => void
  /** A Crew Mode dispatch hint's button: open the side panel on the Crew tab. */
  handleCrewWorkersOpen: () => void
  sessionTitles: Map<string, string>
  handleFork: (visibleIndex: number, messageId?: string, explicitDirection?: 'head') => Promise<void>
  forkDirectionChoice?: boolean
  highlightTs: string | null
  selectSessionTab: (key: string) => void
  linkPreviewsOn: boolean
  socialShareOn: boolean
  connectionsUiOn: boolean
  planTaskId: string
  showRefusedPress: (action: RefusedPressAction, e: unknown) => void
  approve: (action: string) => Promise<void>
  dismissApproval: (aid: string, decision?: string) => void
  revealAppInPanel: (toolCallId: string) => void
  lastTextIdx: number
  regenerating: boolean
  handleRegenerate: () => void
  /** Ids of the previous reply's rows while a Regenerate turn may replace them; drawn as "previous reply". */
  replacingMids: ReadonlySet<string>
  continuable: boolean
  continuing: boolean
  freshSession: FreshSessionState
  handleContinue: () => void
  handleFreshSession: () => void
  repairImages: RepairImagesState
  handleRepairImages: () => void
  interrupted: boolean
  openDefaultModelSetting: () => void
  openKiroSignIn: () => void
  openMemberCapabilities: (member: string, fix?: CapabilityFix) => void
  openModelPickerFromError: () => void
  openTurnLimitSetting: () => void
  quoteWholeMessage: (role: MessageQuoteRole, content: string, ts?: string, mid?: string) => void
  handleAsk: ((text: string) => void) | undefined
  handleQuote: (text: string, rect: DOMRect) => void
  handleEditResend: (index: number, ts: string, newContent: string) => void
  editLast: { seq: number; ts: string; index: number; } | null
  farmPassRef: MutableRefObject<boolean>
  handleEditConsumed: () => void
  renderUserContentCb: (c: string, mt: Record<string, unknown> | undefined, ts?: string) => JSX.Element
  transcriptHot: boolean
  toggleAct: () => void
  handleSpeak: (content: string) => void
  handleApplyPlan: (steps: PlanStepInput[]) => Promise<boolean>
  renderedDisplayItems: DisplayItem[]
  stableMsgKey: (m: ChatMessage) => string
  handleLoadEarlier: () => void
  isPinned: (mid: string) => boolean
  handleJumpToQuote: (q: MessageQuote) => void
  handleTogglePinForMessage: (mid: string, messageTs: string, role: 'user' | 'assistant', content: string) => void
  /** Unsend, Edit and Queue instead on a steer the running turn has not read yet. */
  handleSteerAction: (mid: string, action: SteerRetractAction) => void
  steerActionPending: Readonly<Record<string, SteerRetractAction>>
  steerActionRefusals: Readonly<Record<string, SteerRetractRefusalNotice>>
  steerActionRequestFailures: Readonly<Record<string, true>>
  reactionsFor: (mid: string) => ReadonlySet<string>
  handleToggleReactionForMessage: (mid: string, emoji: string) => void
  reactionErrorFor: (mid: string) => ReactionError | null
  clearReactionError: () => void
}

/**
 * How a transcript row renders on this page: the renderer registry (the bubble and
 * the page-only entries over the shared row set), renderMessage, turn items, and the
 * off-screen measurement farm's rows.
 *
 * Called from ChatPage at the position its statements held, so React registers its effects in the same order.
 */
/** The repeat-session-start card's restart (#13791). Module scope so the
 *  transcript renderers' memo never sees a new identity. */
const restartGatewayFromError = (): Promise<unknown> => api.restartGateway()

export function useTranscriptRendering({
  embedded, mode, popout, filteredSlots, connected, activeSlot, voiceRecoverySlot, appToolCallIds, mcpAppPanel, messages, messagesRef, slotRunning, toolDisclosure, setToolDisclosureFor, slotState,
  activityOpen, slotHasMore, slotOldestIndex, cursorIsForActiveSlot, loadingOlder, chatConfig, activeSlotRemoteBound, automationId, setAutomationOpen, handleArtifactOpen, handleFileOpen,
  handleFolderOpen, handleOpenDiff, handleSubagentPanelOpen, handleCrewWorkersOpen, sessionTitles, handleFork, forkDirectionChoice, highlightTs, selectSessionTab, linkPreviewsOn, socialShareOn, connectionsUiOn, planTaskId, showRefusedPress,
  approve, dismissApproval, revealAppInPanel, lastTextIdx, regenerating, handleRegenerate, replacingMids, continuable, continuing, freshSession, handleContinue, handleFreshSession, repairImages, handleRepairImages, interrupted,
  openDefaultModelSetting, openKiroSignIn, openMemberCapabilities, openModelPickerFromError, openTurnLimitSetting, quoteWholeMessage, handleAsk, handleQuote, handleEditResend, editLast, farmPassRef,
  handleEditConsumed, renderUserContentCb, transcriptHot, toggleAct, handleSpeak, handleApplyPlan, renderedDisplayItems, stableMsgKey, handleLoadEarlier, isPinned, handleJumpToQuote,
  handleTogglePinForMessage, reactionsFor, handleToggleReactionForMessage, reactionErrorFor, clearReactionError,
  handleSteerAction, steerActionPending, steerActionRefusals, steerActionRequestFailures,
}: TranscriptRenderingOptions) {
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
  // Copy on select is device-only, so it is not in chatConfig (which syncs to
  // other browsers); Settings → Chat writes the same key.
  const [copyOnSelect] = usePersistedBool('mc-chat-copy-on-select', false)
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
  const forkHasLaterMessages = useCallback((forkIndex: number) => forkIndex < visibleIndexMapRef.current.size - 1, [])
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
  // The first answer produced by Regenerate below the previous reply. Reasoning,
  // tools and system notices may arrive first, but Stop cannot preserve a new
  // version until an actual assistant/streaming answer row exists.
  const firstNewReplyIdx = useMemo(() => {
    if (replacingMids.size === 0) return -1
    let lastReplaced = -1
    messages.forEach((m, idx) => {
      const mid = m.meta?.mid
      if (typeof mid === 'string' && replacingMids.has(mid)) lastReplaced = idx
    })
    if (lastReplaced < 0) return -1
    return messages.findIndex((m, idx) => idx > lastReplaced
      && (m.role === 'assistant' || m.role === 'streaming')
      && !isSystemNoticeRow(m)
      && !isHiddenInvisibleAssistantRow(m))
  }, [messages, replacingMids])
  const newReplyStarted = firstNewReplyIdx >= 0
  const slotStateRef2 = useRef(slotState); slotStateRef2.current = slotState

  // Row action handlers that never change identity. Their sources are rebuilt on
  // every chunk flush (handleEditResend and handleRegenerate close over
  // `messages`), and an inline arrow per row is new on every render, so passing
  // either straight through re-rendered every memo(UserMessage) and
  // memo(AssistantMessage) on each streamed chunk. The forwarders call the
  // latest source, so a click still runs today's guards.
  // The returned object is new each render; the functions in it are not.
  const {
    handleEditResend: stableEditResend, handleJumpToQuote: stableJumpToQuote, handleRegenerate: stableRegenerate,
    handleTogglePinForMessage: stableTogglePin, quoteWholeMessage: stableQuoteMessage,
    handleToggleReactionForMessage: stableToggleReaction,
  } = useStableCallbackProps({ handleEditResend, handleJumpToQuote, handleRegenerate, handleTogglePinForMessage, quoteWholeMessage, handleToggleReactionForMessage })
  // One pin, one quote and one reaction-toggle handler per message row. Keyed by the row object,
  // which the store keeps by reference while it is unchanged, so a settled row
  // gets the same pair on every render and a replaced row gets a fresh pair
  // over its new content. Weak, so rows that leave the store take theirs along.
  const rowHandlersRef = useRef(new WeakMap<ChatMessage, MessageRowHandlers>())
  const rowHandlersFor = useCallback((m: ChatMessage, role: 'user' | 'assistant'): MessageRowHandlers => {
    let h = rowHandlersRef.current.get(m)
    if (!h || h.role !== role) {
      const mid = typeof m.meta?.mid === 'string' && m.meta.mid ? m.meta.mid : undefined
      h = {
        role,
        togglePin: () => { if (mid && m.ts) stableTogglePin(mid, m.ts, role, m.content) },
        quote: (shown: string) => stableQuoteMessage(role, shown, m.ts, mid),
        toggleReaction: (emoji: string) => { if (mid) stableToggleReaction(mid, emoji) },
      }
      rowHandlersRef.current.set(m, h)
    }
    return h
  }, [stableTogglePin, stableQuoteMessage, stableToggleReaction])

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
    // The reply a running Regenerate may replace: the new one streams in below it.
    const beingReplaced = !!messageId && replacingMids.has(messageId)
    // Only the first answer row after the previous reply starts the new version;
    // reasoning and tool rows between them keep their own rendering.
    const startsNewReply = i === firstNewReplyIdx
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
              onEditResend={stableEditResend}
              doubleClickToEdit={chatConfig.doubleClickToEdit}
              editRequest={!farmPassRef.current && editLast && editLast.index === i && editLast.ts === (m.ts || '') ? editLast.seq : undefined}
              onEditConsumed={farmPassRef.current ? undefined : handleEditConsumed}
              slotKey={activeSlot || undefined}
              slotTitle={activeSlotTitle}
              mode={mode}
              pinned={m.ts && (m.meta as Record<string, unknown> | undefined)?.mid ? isPinned((m.meta as Record<string, unknown>).mid as string) : false}
              onTogglePin={m.ts && (m.meta as Record<string, unknown> | undefined)?.mid ? rowHandlersFor(m, 'user').togglePin : undefined}
              activeReactions={m.ts && (m.meta as Record<string, unknown> | undefined)?.mid ? reactionsFor((m.meta as Record<string, unknown>).mid as string) : undefined}
              onToggleReaction={m.ts && (m.meta as Record<string, unknown> | undefined)?.mid ? rowHandlersFor(m, 'user').toggleReaction : undefined}
              reactionError={m.ts && (m.meta as Record<string, unknown> | undefined)?.mid ? reactionErrorFor((m.meta as Record<string, unknown>).mid as string) : null}
              onClearReactionError={clearReactionError}
              onQuoteMessage={activeSlot && !activeSlotRemoteBound ? rowHandlersFor(m, 'user').quote : undefined}
              onJumpToQuote={stableJumpToQuote}
              onSteerAction={activeSlotRemoteBound ? undefined : handleSteerAction}
              steerActionPending={messageId ? steerActionPending[messageId] : undefined}
              steerActionRefusal={messageId ? steerActionRefusals[messageId] : undefined}
              steerActionRequestFailed={messageId ? steerActionRequestFailures[messageId] : undefined}
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
            <div className={`flex flex-col gap-0${beingReplaced ? ' opacity-60' : ''}`} data-testid={beingReplaced ? 'regenerate-previous-reply' : undefined}>
              {beingReplaced && <span className="text-muted text-[11px] leading-4 font-medium px-1 mb-1">{i18nT(newReplyStarted ? 'pages.chatPage.regenerate_previous_reply_kept_as_version' : 'pages.chatPage.regenerate_previous_reply')}</span>}
              {startsNewReply && <span className="text-muted text-[11px] leading-4 font-medium px-1 mb-1 border-t border-border pt-2" data-testid="regenerate-new-reply">{i18nT('pages.chatPage.regenerate_new_reply')}</span>}
              <AssistantMessage body={warmBriefBodyOf(m)} revealActions={!!activeSlot && voiceRecoverySlot === activeSlot} suppressSteerAck={turnHadPolicyBlock(messagesRef.current, i)} prevUserText={prevUserTextFor(messagesRef.current, i)} shareEnabled={socialShareOn} linkPreviews={linkPreviewsOn} content={m.content} isStreaming={isStreaming} isRegenerating={regenerating && i === lastTextIdxRef.current} onFileOpen={handleFileOpen} onFolderOpen={handleFolderOpen} onArtifactOpen={handleArtifactOpen} onSessionOpen={selectSessionTab} sessions={connected ? sessionTitles : undefined} activeSession={activeSlot || undefined} onQuote={handleQuote} onAsk={handleAsk} copyOnSelect={copyOnSelect} slotRunning={slotRunning} planTaskId={planTaskId} timestamp={chatConfig.showTimestamps ? msgTime : undefined} timestampTitle={msgTimeFull} messageTs={m.ts} slotKey={activeSlot || undefined} slotTitle={activeSlotTitle} mode={mode} fileChanges={(m.meta as Record<string, unknown> | undefined)?.file_changes as FileChangeEntry[] | undefined} fileChangesOmittedFiles={(m.meta as Record<string, unknown> | undefined)?.file_changes_omitted_files} blockedLinks={(m.meta as Record<string, unknown> | undefined)?.blocked_links} redactions={(m.meta as Record<string, unknown> | undefined)?.redactions} showRedactionCoach={!!m.ts && m.ts === redactionCoachTs} turnStats={chatConfig.showTurnStats ? (m.meta as Record<string, unknown> | undefined)?.turn_stats as TurnStats | undefined : undefined} decisionsStrip={decisionStripFieldOf(m)} onOpenDiff={handleOpenDiff} fileChipStyle={chatConfig.fileChipStyle} artifactPaths={artifactPaths} pinned={m.ts && (m.meta as Record<string, unknown> | undefined)?.mid ? isPinned((m.meta as Record<string, unknown>).mid as string) : false} onTogglePin={m.ts && (m.meta as Record<string, unknown> | undefined)?.mid ? rowHandlersFor(m, 'assistant').togglePin : undefined} activeReactions={m.ts && (m.meta as Record<string, unknown> | undefined)?.mid ? reactionsFor((m.meta as Record<string, unknown>).mid as string) : undefined} onToggleReaction={m.ts && (m.meta as Record<string, unknown> | undefined)?.mid ? rowHandlersFor(m, 'assistant').toggleReaction : undefined} reactionError={m.ts && (m.meta as Record<string, unknown> | undefined)?.mid ? reactionErrorFor((m.meta as Record<string, unknown>).mid as string) : null} onClearReactionError={clearReactionError} onQuoteMessage={activeSlot && !activeSlotRemoteBound && !isStreaming ? rowHandlersFor(m, 'assistant').quote : undefined} showFooter={(() => {
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
              })()} onSpeak={handleSpeak} onRegenerate={i === lastTextIdxRef.current && !slotRunning && !regenerating && activeSlot && !activeSlotRemoteBound ? stableRegenerate : undefined} variants={m.variants} variantIdx={m.variant_idx} onSwitchVariant={i === lastTextIdxRef.current && m.variants && m.variants.length > 1 && activeSlot ? (idx: number) => { api.switchVariant(activeSlot, idx).catch((e: unknown) => {
                showRefusedPress('switch_variant', e)
              }) } : undefined} onFork={embedded && !popout ? undefined : handleFork} forkDirectionChoice={forkDirectionChoice} forkHasLaterMessages={forkHasLaterMessages} forkIndex={forkIndex} forkMessageId={canResolveOnServer ? messageId : undefined} onLoadEarlier={cursorIsForActiveSlot ? handleLoadEarlier : undefined} loadingOlder={loadingOlder} earlierRemaining={slotOldestIndex} onApplyPlan={handleApplyPlan} />
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
      onOpenCrewWorkers: embedded || popout ? undefined : handleCrewWorkersOpen,
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
      onOpenTurnLimit: embedded || popout ? undefined : openTurnLimitSetting,
      // The repeat-start remedy (#13791). The restart itself is owner-gated on
      // the server; a refusal comes back as an ApiError the card shows.
      onRestartGateway: embedded || popout ? undefined : restartGatewayFromError,
      onOpenCapabilities: embedded || popout ? undefined : openMemberCapabilities,
      onFreshSession: handleFreshSession,
      freshSession,
      onRepairImages: handleRepairImages,
      repairImages,
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
  }, [slotRunning, handleFileOpen, handleArtifactOpen, selectSessionTab, sessionTitles, connected, handleFork, forkDirectionChoice, forkHasLaterMessages, handleQuote, handleAsk, stableJumpToQuote, chatConfig, copyOnSelect, activeSlot, regenerating, replacingMids, firstNewReplyIdx, newReplyStarted, activeSlotRemoteBound, stableRegenerate, stableEditResend, rowHandlersFor, reactionsFor, reactionErrorFor, clearReactionError, editLast, handleEditConsumed, slotHasMore, loadingOlder, cursorIsForActiveSlot, slotOldestIndex, handleLoadEarlier, renderUserContentCb, highlightTs, activeSlotTitle, mode, embedded, popout, handleOpenDiff, planTaskId, artifactPaths, automationId, toolDisclosure, setToolDisclosureFor, linkPreviewsOn, socialShareOn, voiceRecoverySlot, handleSubagentPanelOpen, isPinned, showRefusedPress, transcriptHot, revealAppInPanel, continuable, interrupted, continuing, handleContinue, openModelPickerFromError, openDefaultModelSetting, openKiroSignIn, openMemberCapabilities, openTurnLimitSetting, handleFreshSession, freshSession, handleRepairImages, repairImages, handleFolderOpen, handleSpeak, handleApplyPlan, mcpAppPanel, redactionCoachTs, setAutomationOpen, farmPassRef, messagesRef, handleSteerAction, steerActionPending, steerActionRefusals, steerActionRequestFailures, handleCrewWorkersOpen])

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
  // eslint-disable-next-line react-hooks/exhaustive-deps -- messagesRef is a useRef (stable for the page's lifetime) that the rule cannot see across the owner boundary; listing it would also put the flush-volatile `messages` name the dep-hygiene ratchet (TurnBlock.rowMemo.test.tsx) bans back in this array
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
    // `empty:hidden`: a row whose content decides at render time that it draws
    // nothing (an edit folded into the run above it, see editRuns.ts) must not
    // leave its padding behind either.
    return <div key={turnLeadKey(it, stableMsgKey)} data-content-column="" className={`px-4 mx-auto w-full py-1 empty:hidden`} style={{ maxWidth: 'var(--mc-content-width, 900px)' }}>
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
      <div data-content-column="" className={`px-4 mx-auto w-full py-1 empty:hidden`} style={{ maxWidth: 'var(--mc-content-width, 900px)' }}>
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
  }, [buildFarmItem, farmPassRef])
  return {
    activeSlotTitle, renderMessage, renderTurnItem, renderFarmItem,
  }
}
