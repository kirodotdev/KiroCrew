import { useState, useRef, useCallback, useEffect, useMemo } from 'react'
import { createPortal } from 'react-dom'
import { useNavigate } from 'react-router-dom'
import { X } from 'lucide-react'
import { SplitGlyph } from './SplitGlyph'
import { useQuery, useMutation } from '@tanstack/react-query'
import { useModelsDegraded } from '../providers/modelListHealth'
import ChatMessageList from '../app-sdk/ChatMessageList'
import { useChatScrollFollow } from '../app-sdk/useChatScrollFollow'
import { EdgeFade, JumpToBottomButton } from '../app-sdk/ChatScrollChrome'
import { createTranscriptRenderers } from '../pages/chat/transcriptRenderers'
import ChatInput from './ChatInput'
import ChatDropOverlay, { useChatFileDrop } from './ChatDropOverlay'
import PendingQuestionCard from './PendingQuestionCard'
import QueueStack, { SubagentDeliveryProgress, splitPaneMessages } from './QueueStack'
import SubagentProgressBar from '../pages/chat/SubagentProgressBar'
import ChatFooter from '../pages/chat/ChatFooter'
import AgentDropdownList, { DefaultAgentRow, ManageAgentsFooter } from './AgentDropdownList'
import { agentSwitchFailureMessage } from '../utils/agentSwitchFeedback'
import ModelDropdownList from './ModelDropdownList'
import { SlotProvider } from '../providers/SlotContext'
import { useProvider } from '../providers'
import { useAgents } from '../hooks/useAgents'
import { useFilteredDropdown } from '../hooks/useFilteredDropdown'
import { useConnectionsUiEnabled } from '../hooks/useConnectionsUi'
import { useAvailableModels } from '../hooks/useAvailableModels'
import { usePlanActionMutation, isPlanAction } from '../hooks/usePlanActionMutation'
import { useQueuedMessageActions, queuedSendStash } from '../hooks/useQueuedMessageActions'
import { useListboxKeyboard } from '../hooks/useListboxKeyboard'
import { useAppSelector, useAppDispatch, store } from '../store'
import { PANE_HYDRATE_LIMIT, retireStatelessQuestion, captureStatelessCard, capturePendingAskId, confirmOptimisticSend, selectSlotMessages, selectSlotStreamState, selectComposerBusy, hydrateSlotMessages, appendSlotMessage, requestStop, setAgentSwitchNotice, pendingQuestionFor } from '../store/chatSlice'
import { deriveFollowUpOptions } from '../app-sdk/protocol'
import { CONTENT_WIDTH, loadChatConfig, type ChatConfig } from '../pages/chat/ChatSettings'
import { tryQuickSend } from '../lib/quickSend'
import { mergeRecoveredDraft } from '../utils/chatDrafts'
import { sendTurn } from '../chat-core/transport/sendTurn'
import { triggerRefresh, updateSlot } from '../store/dashboardSlice'
import { performSlotSwitch } from '../lib/slotSwitch'
import { performAgentSlotSwitch } from '../lib/agentSwitch'
import { api } from '../api/client'
import { resolveAskAfterSend } from '../lib/resolveAskAfterSend'
import { classifyDrop } from '../utils/dropClassify'
import { serializeDirTokens, spliceDirTokens, VIDEO_EXT } from '../utils/fileTokens'
import { displayModel } from '../lib/model'


import { i18nT } from '../i18n/t'

/**
 * ChatPane — one live chat session in the native session grid.
 *
 * Renders the REAL native <ChatInput> inside <SlotProvider> with the full
 * per-slot composer (model/agent/approval-mode pickers, attachments, QueueStack).
 * Messages stream live from the store; per-slot metadata comes from
 * s.dashboard.slots. Server reads/writes go through React Query + the api client.
 */

export default function ChatPane({
  slotKey,
  focused,
  onFocus,
  onRemove,
  onSplitRight,
  onSplitDown,
  onOpenFull,
  agentLocked,
  frameless,
  followContentWidth,
}: {
  slotKey: string
  focused?: boolean
  onFocus?: () => void
  onRemove?: () => void
  onSplitRight?: () => void
  onSplitDown?: () => void
  /** Hands this pane's slot to the full session, leaving split view. Without it
   *  the earlier-messages row is hidden rather than shown inert. The optional ts
   *  anchors the destination near the pane's oldest message, not the newest. */
  onOpenFull?: (slot: string, anchorTs?: string, anchorMid?: string) => void
  /** The host declares the slot's agent server-pinned (member DM threads):
   *  the agent picker is not offered at all, instead of offering a control
   *  whose every selection the backend 409s. */
  agentLocked?: boolean
  /** Embedded-in-a-page mode (member DM threads): the HOST renders the
   *  identity header, so the pane's own title bar and its card chrome
   *  (border, rounded corners) would duplicate it. Split-view panes keep
   *  the chrome — there the bar IS the pane's identity. */
  frameless?: boolean
  /** The pane follows the user's Content width setting (transcript AND
   *  composer, both halves of CONTENT_WIDTH), resolved from the pane's own
   *  live chatConfig. Defaults to false = both variables pinned to '100%':
   *  a split-view pane is already narrow, so capping inside it wastes
   *  width. A full-width host (the Members page's DM column) sets it so
   *  long transcripts keep the same user-configured measure as the main
   *  chat. */
  followContentWidth?: boolean
}) {
  // One instance covers both dropdown filter inputs (never open at once).
  const dispatch = useAppDispatch()
  const provider = useProvider()
  // Same gate the main chat uses: hide a Connections-owned OAuth banner only
  // while the card that owns that flow is reachable.
  const connectionsUiOn = useConnectionsUiEnabled()
  const [input, setInput] = useState('')
  const [pendingFiles, setPendingFiles] = useState<string[]>([])
  const [uploadError, setUploadError] = useState('')
  const [agentBtnRect, setAgentBtnRect] = useState<DOMRect | null>(null)
  const [modelBtnRect, setModelBtnRect] = useState<DOMRect | null>(null)
  // Shared stick-to-bottom follow (same FollowController core as the main
  // chat's virtualizer): RO-driven re-pin on any content growth or collapse,
  // released only by a genuine user scroll up, re-armed at the bottom.
  const follow = useChatScrollFollow({ resetKey: slotKey })

  const allMessages = useAppSelector((s) => selectSlotMessages(s, slotKey))
  const activeSlot = useAppSelector((s) => s.chat.activeSlot)
  const streamState = useAppSelector((s) => selectSlotStreamState(s, slotKey))
  const running = streamState !== 'idle'
  // Per-slot context-window usage for the input-bar ring (mirrors ChatPage; the
  // store keys these by slot). Default 0 so the ring always renders, exactly
  // like single chat.
  const contextPct = useAppSelector((s) => s.chat.slotContextPct[slotKey] ?? 0)
  const contextTokens = useAppSelector((s) => s.chat.slotContextTokens?.[slotKey])
  // Prefer the warm's value: this pane's own query is staleTime:Infinity, so its
  // has_more freezes at mount while a later bounded warm can truncate the cache.
  const warmHasMore = useAppSelector((s) => s.chat.slotPaneHasMore?.[slotKey])
  const paneSlot = useAppSelector((s) => s.dashboard.slots.find((x) => x.key === slotKey))
  // Shared composer-busy rule (chatSlice.selectComposerBusy): main turn
  // streaming OR sub-agents running (dual signal). Drives the queue affordance
  // and skips the optimistic user bubble (the backend returns a "queued"
  // message instead, so an optimistic bubble would render a duplicate).
  const busy = useAppSelector((s) => selectComposerBusy(s, slotKey))
  // Parent link for the "↳ fork of <parent>" tag. forked_from is the parent's
  // history key (dashboard:<slot>); strip the prefix to match the bare slot key.
  const parentKey = paneSlot?.forked_from ? paneSlot.forked_from.replace(/^dashboard:/, '') : null
  const parentTitle = useAppSelector((s) =>
    parentKey ? s.dashboard.slots.find((x) => x.key === parentKey)?.title : undefined,
  )
  const approvalMode = useAppSelector((s) => s.dashboard.approvalMode)
  const title = paneSlot?.title || slotKey
  const displayMode = approvalMode === 'yolo' ? 'yolo' : paneSlot?.trust ? 'trust' : paneSlot?.trust_reads ? 'trust_reads' : 'normal'
  // Queued messages render in the QueueStack, not inline in the message list.
  // System injections are excluded from the interactive stack (isNonInteractiveQueued):
  // sub-agent deliveries collapse into one progress line, and synthetic
  // turn-recovery injections drain automatically and render as a RecoveryCard.
  // Mirrors ChatPage — split view (⌘D) is a second live QueueStack consumer.
  //
  // Memoized on `allMessages`: this pane OWNS the composer `input` state, so it
  // re-renders on every keystroke. Recomputing these in the render body would
  // hand `messages` a fresh array identity per character, defeating the memo()
  // on ChatMessageList and re-running its O(N) turn grouping while the user
  // types.
  const { messages, queuedMessages, systemDeliveryCount } = useMemo(
    () => splitPaneMessages(allMessages),
    [allMessages],
  )
  // EVERY queued row, cards and hidden system deliveries alike. A reorder
  // submits the full sequence — see useQueuedMessageActions — so the
  // non-interactive rows `splitPaneMessages` strips out are still needed here.
  const allQueuedMessages = useMemo(
    () => allMessages.filter(m => m.role === 'queued'),
    [allMessages],
  )

  // Follow-up [OPTIONS:] pills for this pane's composer — the same
  // derive-and-pass wiring ChatPage uses, adapted to the pane's own signals.
  // Derived from `allMessages`, NOT the queued-stripped `messages` above:
  // deriveFollowUpOptions short-circuits on a `queued` row (the user already
  // acted), and splitPaneMessages removes exactly those rows, so deriving from
  // the filtered list would keep stale pills alive past a queued send.
  // The pane's composer-busy rule (main turn streaming OR sub-agents running)
  // stands in for ChatPage's isStreaming as the mid-turn gate: the pane already
  // treats `busy` as its one busy signal everywhere else (queue affordance,
  // optimistic-bubble skip), so the pills follow the same rule rather than
  // introducing a second busy variant. A pending question card suppresses them
  // for the same reason as ChatPage: both would offer the same choices, and
  // only the card can answer the blocked tool call.
  const pendingQuestion = useAppSelector((s) => pendingQuestionFor(s.chat.pendingQuestions, slotKey))
  const { followUpOptions, followUpRecommended, followUpIsPlan, followUpSourceKey } = useMemo(
    () => deriveFollowUpOptions(allMessages, busy, !!pendingQuestion),
    [allMessages, busy, pendingQuestion],
  )
  // Visual-only highlight state; the composer text is the source of truth for
  // what gets sent. Cleared whenever the options list changes (new assistant
  // message) or the pane is re-bound to another slot — both signal a fresh turn.
  const [followUpPicked, setFollowUpPicked] = useState<Set<string>>(() => new Set())
  // Read by the option handler instead of the state: two clicks landing before
  // a re-render would both see the same set and both take the append branch.
  const followUpPickedRef = useRef(followUpPicked); followUpPickedRef.current = followUpPicked
  // Orchestrator plan dispatch (#5893) — same mutation ChatPage uses,
  // targeting THIS pane's slot. The hook owns the latch acknowledgement,
  // keyed on the derived options-row identity passed here; the ref lets the
  // click handler see the in-flight state, not the render it closed over.
  const planActionMutation = usePlanActionMutation(slotKey, followUpSourceKey)
  const planActionMutationRef = useRef(planActionMutation); planActionMutationRef.current = planActionMutation
  // One spelling for every plan-chip gesture (single-click, double-click,
  // Send-now). `sourceKeyAtClick` is the row the gesture started on.
  const dispatchPlanFollowUp = (action: string, sourceKeyAtClick?: string | null): boolean => {
    if (!(followUpIsPlan && isPlanAction(action))) return false
    if (!paneSlot) return true
    if (paneSlot.mode !== 'orchestrator') return false
    planActionMutationRef.current.mutate({ slot: slotKey, action, clickedSourceKey: sourceKeyAtClick })
    return true
  }
  const followUpOptionsKey = followUpOptions.join('\x00')
  useEffect(() => { setFollowUpPicked(new Set()) }, [followUpOptionsKey, slotKey])
  // Quick Send parity with ChatPage: same query key, so the cache is shared
  // with the page and no extra request is made for a pane.
  const { data: dashCfg } = useQuery<{ quick_send?: boolean }>({ queryKey: ['dashboardConfig'], queryFn: () => api.dashboardConfig(), staleTime: 30_000 })
  // Follow-up bar layout: the same persisted setting ChatPage reads, kept live
  // the same way (ChatPage.tsx's reload listener) — a pane is long-lived, so a
  // one-shot read would leave it on the old layout after the user changes the
  // setting while split view is open.
  const [chatConfig, setChatConfig] = useState<ChatConfig>(loadChatConfig)
  useEffect(() => {
    const reload = () => { const next = loadChatConfig(); setChatConfig(prev => JSON.stringify(prev) === JSON.stringify(next) ? prev : next) }
    window.addEventListener('focus', reload)
    window.addEventListener('mc-config-changed', reload)
    return () => { window.removeEventListener('focus', reload); window.removeEventListener('mc-config-changed', reload) }
  }, [])

  // Pickers — same hooks/data sources ChatPage uses, but selection targets THIS slot.
  // Subscribes to the store's global refresh so a default-agent write in ANY pane (or
  // in single chat) lands here too; a per-hook refresh would leave sibling pickers stale.
  const agentsRefreshTrigger = useAppSelector((s) => s.dashboard.refreshTrigger ?? 0)
  // This pane takes no project prop, so read THIS slot's project from the store:
  // it scopes which project-local agents exist, so a project change must refetch.
  const paneProject = useAppSelector((s) => s.dashboard.slots.find((x) => x.key === slotKey)?.project || undefined)
  const { agents: installedAgents, defaultAgent } = useAgents(agentsRefreshTrigger, slotKey, paneProject)
  // One source for every same-meaning marker: the composer chip, the row's
  // check, and the default-agent row's label. An agent-less slot resolves to
  // the configured default (matching what dispatch runs) before the literal
  // 'default' placeholder.
  const paneAgentName = paneSlot?.agent || defaultAgent || 'default'
  const navigate = useNavigate()
  const [defaultAgentFailed, setDefaultAgentFailed] = useState(false)
  // Same contract as ChatPage: set-only, clearing lives on the Templates page.
  const toggleDefaultAgent = useCallback((name: string) => {
    setDefaultAgentFailed(false)
    Promise.resolve(api.setDefaultAgent?.(name))
      .then(() => dispatch(triggerRefresh()))
      .catch(() => setDefaultAgentFailed(true))
  }, [dispatch])
  const agentDD = useFilteredDropdown(installedAgents)
  const availableModels = useAvailableModels()
  const modelDD = useFilteredDropdown(availableModels)
  // See ChatPage: display what will actually run, not a pin the account lost
  // access to. The slot's own `model_withheld` verdict answers that when the
  // backend has one; the degraded flag gates only the list-membership fallback —
  // a cached list served while /api/models fails is stale and cannot disprove
  // entitlement — and is subscribed to, since it can flip while the served list
  // stays identical.
  const _modelsDegraded = useModelsDegraded(provider.id)
  const shownModel = displayModel(
    paneSlot?.model || '',
    availableModels,
    _modelsDegraded,
    paneSlot?.model_withheld,
  )

  // One-time hydrate of this slot's message history via React Query + the api
  // client (caching + cross-pane dedup; staleTime Infinity keeps it one-shot —
  // live updates arrive through the WS store routing, not a refetch).
  // Unbounded while streaming is deliberate, not a raw-row guard: the handler
  // collapses chunk runs BEFORE computing total and slicing, even mid-stream.
  // A background slot's stream state reads idle until an SSE frame arrives, so
  // the slot record is the signal; latch only once unbounded so a turn that starts
  // while the bounded fetch is still in flight can still upgrade it.
  const limitRef = useRef<number | undefined>(PANE_HYDRATE_LIMIT)
  const limitLatched = useRef(false)
  if (!limitLatched.current && (running || paneSlot?.running)) {
    limitRef.current = undefined
    limitLatched.current = true
  }
  const hydrateLimit = limitRef.current
  const { data: slotDetail } = useQuery({
    queryKey: ['slot-messages', slotKey, hydrateLimit],
    queryFn: () => api.chatSlotDetail(slotKey, hydrateLimit),
    staleTime: Infinity,
  })
  useEffect(() => {
    if (slotDetail?.messages) dispatch(hydrateSlotMessages({ slot: slotKey, messages: slotDetail.messages, hasMore: slotDetail.has_more, bounded: hydrateLimit !== undefined, total: slotDetail.total, running: slotDetail.running }))
  }, [slotDetail, slotKey, dispatch, hydrateLimit])

  // Scroll follow (auto-pin, release, jump pill) is owned by useChatScrollFollow
  // above — the ResizeObserver on the content wrapper replaces the old
  // message-hash effect, so growth on EARLIER rows (a tool result updating, a
  // thinking block expanding) and turn-collapse shrink re-pin too.


  const switchAgent = useCallback(async (name: string) => {
    dispatch(setAgentSwitchNotice(null))
    try {
      // Same protocol as switchModel below (#4523): the pane must not depend
      // on the coalesced slots rebroadcast to see its own pick.
      // performAgentSlotSwitch mirrors exactly what the response names.
      await performAgentSlotSwitch(slotKey, name, dispatch)
    } catch (e) {
      dispatch(setAgentSwitchNotice(agentSwitchFailureMessage(e)))
    }
  }, [dispatch, slotKey])
  const switchModel = useCallback(async (name: string) => {
    try {
      // performSlotSwitch owns the whole protocol: serialized dispatch,
      // latest-request-wins adjudication, hung-request timeout, and exactly
      // one store write on the authoritative value (#4523) — the pane must
      // not depend on the coalesced slots rebroadcast to see its own pick.
      await performSlotSwitch('model', slotKey, name,
        async () => {
          const r = await api.chatSlotModel(slotKey, name)
          return r?.model ?? name
        },
        (value) => dispatch(updateSlot({ key: slotKey, model: value })))
    } catch (e) {
      // Same failure surface as switchAgent above: the shared notice toast.
      dispatch(setAgentSwitchNotice(agentSwitchFailureMessage(e)))
      // Keep the rejected backend value available in developer diagnostics.
      // eslint-disable-next-line no-console
      console.error('[ChatPane] switchModel failed', e)
    }
  }, [dispatch, slotKey])

  // Roving-focus keyboard nav for the pickers (mirrors ChatPage / StyledSelect):
  // ArrowUp/Down across options, Enter/Space select, Escape/Tab close + return
  // focus. AgentDropdownList / ModelDropdownList options already carry
  // role="option" + tabIndex={-1}.
  const { onListKeyDown: onAgentListKeyDown } = useListboxKeyboard({
    open: agentDD.open,
    dropdownRef: agentDD.dropdownRef,
    inputRef: agentDD.inputRef,
    hasFilterInput: true,
    filteredCount: agentDD.filtered.length,
    onEnterSingleMatch: () => { switchAgent(agentDD.filtered[0].name); agentDD.setOpen(false) },
    closeToTrigger: () => agentDD.setOpen(false),
  })
  const { onListKeyDown: onModelListKeyDown } = useListboxKeyboard({
    open: modelDD.open,
    dropdownRef: modelDD.dropdownRef,
    inputRef: modelDD.inputRef,
    hasFilterInput: true,
    filteredCount: modelDD.filtered.length,
    onEnterSingleMatch: () => { switchModel(modelDD.filtered[0].name); modelDD.setOpen(false) },
    closeToTrigger: () => modelDD.setOpen(false),
  })

  // File upload as a mutation (isPending replaces a manual `uploading` flag).
  const uploadMutation = useMutation({
    mutationFn: (files: File[]) => api.uploadFiles(files),
    // api.uploadFiles does NOT throw on a server refusal (unsupported type,
    // signature mismatch, over-cap): it resolves with { paths: [], error }.
    // So a refusal lands here in onSuccess, not onError — surface res.error
    // (matching ChatPage) instead of silently doing nothing.
    onSuccess: (res) => {
      if (res.error) { setUploadError(i18nT('pages.chatPage.upload_failed_error', { error: res.error })); return }
      if (res.paths?.length) setPendingFiles((prev) => [...prev, ...res.paths])
    },
    // api.uploadFiles throws for three distinct reasons: a client-side image
    // resize failure, a session expiry, and a transport reject. The first two
    // carry a message worth showing. A fetch reject arrives as a TypeError
    // reading "Failed to fetch", which is not user-facing copy, so that case
    // gets the pane's shared connectivity string instead.
    onError: (err: unknown) => {
      const message = (err as Error)?.message
      const reason = (!message || err instanceof TypeError)
        ? i18nT('pages.chatPage.connection_error')
        : message
      setUploadError(i18nT('pages.chatPage.upload_failed_error', { error: reason }))
    },
  })
  const uploadFiles = useCallback((files: File[]) => {
    if (!files.length) return
    // Clear FIRST, so a refusal from the previous attempt cannot stay on
    // screen and read as the reason this one failed.
    setUploadError('')
    if (files.length > 20) { setUploadError(i18nT('pages.chatPage.too_many_files_max_20')); return }
    // Video is deliberately exempt from this pre-check, exactly as in
    // ChatPage: the server's video ceiling is far higher than 50 MB, so the
    // figure this message states would be a lie for a recording. An over-cap
    // recording's own 413 carries the real cap and surfaces through the
    // res.error branch above -- the route every other server-side refusal
    // already takes, and the one this change just wired to the banner.
    const big = files.find((f) => !VIDEO_EXT.test(f.name) && f.size > 50 * 1024 * 1024)
    if (big) { setUploadError(i18nT('pages.chatPage.file_too_large', { name: big.name })); return }
    uploadMutation.mutate(files)
  }, [uploadMutation])

  // Classify BEFORE acting (issue #743): a dropped folder inserts its path
  // into the composer as an `@path/` token instead of taking the upload
  // route, which cannot ingest a directory. Files keep uploading; a mixed
  // drop takes both routes. The pane has no project context, so the token
  // keeps the absolute path (the picker's own out-of-root fallback form),
  // appended — the pane does not track a live composer caret. In a plain
  // browser no real path is visible, so classifyDrop leaves folders on the
  // upload route there (today's behaviour).
  const handleDrop = useCallback((dataTransfer: DataTransfer) => {
    const { files, dirPaths } = classifyDrop(dataTransfer)
    if (dirPaths.length) setInput((prev) => spliceDirTokens(prev, null, dirPaths).value)
    if (files.length) uploadFiles(files)
  }, [uploadFiles])
  const { active: dragOver, dropTargetProps } = useChatFileDrop(handleDrop)

  /** Put a payload the server never accepted back into the composer.
   *
   *  APPEND, never replace and never DROP: a send is in flight for seconds and
   *  the user can type a fresh message in that window, so neither payload may
   *  overwrite the other — preferring the newer one silently discards the message
   *  the error row is telling them to retry, preferring the older one loses work
   *  they just did. `mergeRecoveredDraft` owns that rule for every recovery site
   *  in the app, this pane's two included (a failed `doSend` and a failed
   *  question-card fallback); attachments merge here as a set union so a file
   *  re-picked mid-flight is not double-attached. */
  const restoreIntoComposer = useCallback((text: string, files: string[] = []) => {
    setInput(prev => mergeRecoveredDraft(prev, text))
    if (files.length) setPendingFiles(prev => [...prev, ...files.filter(f => !prev.includes(f))])
  }, [])

  /** Say, in the transcript that owns the message, that it never went out.
   *
   *  Addressed to the slot the message belongs to rather than the active one —
   *  the user can switch panes while a POST is in flight. `reason` is the
   *  server's own explanation when there is one (a 409 "slot agent mismatch" is
   *  actionable; "check your connection" is not); it is absent on the
   *  transport-reject path, where there is no body to quote.
   *
   *  Component-scoped so BOTH failure sites in this pane speak: the composer's
   *  own send, and the question-card fallback, whose answer is destroyed
   *  outright by a swallowed failure because the card is already gone. */
  const reportSendFailure = useCallback((reason?: string) => {
    dispatch(appendSlotMessage({
      slot: slotKey,
      message: {
        role: 'error',
        content: reason || (i18nT('pages.chatPage.send_failed') as string),
        cls: '',
      },
    }))
  }, [dispatch, slotKey])

  const doSend = useCallback((optionText?: string) => {
    // `optionText` mirrors ChatPage.send's first parameter: the follow-up
    // bar's direct-send gesture (double-click / split button) hands the option
    // label here so it bypasses the setInput race, superseding any composer
    // text exactly as ChatPage does with `optionText || inputRef.current`.
    const text = (optionText || input).trim()
    if (!text && !pendingFiles.length) return
    // Capture the stateless card pending at ENTRY (before any state updates
    // or yields): this send consumes the answer channel of the card the user
    // saw when they hit send. Retired only after the server confirms it
    // accepted the message (ok or queued) — the optimistic append below must
    // not do it, or a failed send (offline, 5xx) deletes the card while the
    // session never moved on.
    const cardAtSend = captureStatelessCard(store.getState().chat.pendingQuestions, slotKey)
    // A blocking card is resolved over the network, not in the store — an agent
    // is parked on its request.
    const askAtSend = capturePendingAskId(store.getState().chat.pendingQuestions, slotKey)
    // Staged text and files belong to the COMPOSER, so only a send that
    // consumes the composer may clear or carry them. An `optionText` send (the
    // follow-up bar's direct-send gesture) supplies its own text and leaves the
    // composer untouched — same invariant as ChatPage.send's `if (!optionText)`
    // gate: no send-without-clear (duplicate) and no clear-without-send (silent
    // loss). Consuming the draft or attachments here would wipe text the user
    // never sent and attach files to a message they never composed.
    const files = optionText ? [] : pendingFiles
    if (!optionText) {
      setInput('')
      setPendingFiles([])
    }
    // Folder tokens take the same wire/bubble split ChatPage uses: the wire
    // text carries `[attached_dir N] path` markers the agent can resolve, the
    // bubble keeps the `@path/` token for the chip, and `meta.dirs` indexes
    // marker N to dirPaths[N-1] for lossless history replay. The pane has no
    // project context, so tokens are absolute and serialize as-is.
    const { llm, dirPaths } = serializeDirTokens(text, '')
    // sendId correlation (same contract as ChatPage): the wire text differs
    // from the bubble text whenever a folder token serialized, so the store's
    // content-equality fallback can never reconcile the server echo against
    // the optimistic bubble — without this id the echo appends a SECOND user
    // bubble carrying the raw marker.
    const sendId = `s-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`
    // Optimistic user bubble: show immediately in the right position (mirrors the
    // single-chat send). Skipped while busy (main turn streaming OR sub-agents
    // running) — the backend returns a "queued" message instead, avoiding a duplicate.
    const meta = {
      ...(files.length ? { files } : {}),
      ...(dirPaths.length ? { dirs: dirPaths } : {}),
      sendId,
    }
    if (!busy && (text || files.length)) {
      dispatch(appendSlotMessage({
        slot: slotKey,
        message: { role: 'user', content: text, cls: 'msg msg-u', ts: new Date().toISOString(), ...(meta ? { meta } : {}) },
      }))
    }
    // A failed send has to say so on the pane it was typed into. This path
    // reported nothing at all: the composer had already cleared and a rejected
    // fetch was swallowed by `.catch(() => undefined)`, so an undelivered
    // message stayed on screen looking sent. `ChatPage` has always appended an
    // error row and handed the text back; the pane now does the same.
    const reportFailedSend = (reason?: string) => {
      reportSendFailure(reason)
      // Only a composer send has anything to hand back: an option send never
      // consumed the draft (see the `!optionText` gate above), so restoring the
      // option label here would CLOBBER the preserved draft with text the user
      // can re-click any time.
      if (!optionText) restoreIntoComposer(text, files)
    }
    // Receipt semantics live in the chat-core transport (sendTurn owns the
    // abort deadline and the shared readSendReceipt classification). This
    // pane only decides how to REACT
    // per status: failures report on the pane that owns the message and hand
    // the payload back. `unknown` proves a 2xx was received, while
    // `response-late` proves no refusal either; restoring either one here could
    // invite a retry that duplicates a turn already in flight, side effects
    // included, so the optimistic composer row stays pending.
    void sendTurn({ message: llm, slot: slotKey, meta }).then((receipt) => {
      if (receipt.status === 'refused' || receipt.status === 'transport-error') {
        reportFailedSend(receipt.reason)
        return
      }
      if (receipt.status === 'unknown' || receipt.status === 'response-late') return
      // The receipt names the queue entry this send became: bind the
      // pre-send composer state to it so cancelling that card restores the
      // TYPED text and re-stages the files (issue #560). This matters MORE
      // here than on ChatPage: the pane sends attachments via `meta.files`,
      // so the queued row's content carries no markers and the parser
      // fallback has nothing to recover the files from. `!optionText`
      // mirrors the composer-consumption gate above -- an option send never
      // consumed the draft, so there is no pre-send state to bind. An empty
      // wire text can never reach here (sendTurn classifies it `refused`),
      // and the guard requires the receipt's `queue_id`.
      if (receipt.status === 'queued' && typeof receipt.body.queue_id === 'string' && receipt.body.queue_id && !optionText) {
        queuedSendStash.set(receipt.body.queue_id, { raw: text, files, sent: llm })
      }
      // The response is the delivery receipt for this pane's optimistic bubble
      // because no `chat_message` echo is coming for a dashboard send. Only
      // an IMMEDIATE dispatch counts: a queued acceptance is not a receipt for
      // this bubble.
      if (receipt.status === 'dispatched') {
        dispatch(confirmOptimisticSend({
          slot: slotKey,
          sendId,
          mid: typeof receipt.body.mid === 'string' ? receipt.body.mid : undefined,
        }))
      }
      if (!cardAtSend && !askAtSend) return
      // Immediate dispatch only: a QUEUED acceptance is still cancellable —
      // the queued path retires at its queue_pop instead (removeQueuedMessage).
      if (receipt.status === 'dispatched' && cardAtSend) dispatch(retireStatelessQuestion({ slot: slotKey, expected: cardAtSend }))
      void resolveAskAfterSend(receipt.body, askAtSend, dispatch)
    })
  }, [input, pendingFiles, busy, slotKey, dispatch, restoreIntoComposer, reportSendFailure])

  const onStop = useCallback(() => { dispatch(requestStop({ slotId: slotKey, force: false })) }, [dispatch, slotKey])
  // The same queue-card recipe the single-chat surface runs (#5891), owned once
  // so the two cannot drift again the way #2240 found them drifted.
  //
  // Restore is this pane's own composer helper, which MERGES rather than
  // assigns: a pane's composer is local state with no per-slot draft store, so
  // clobbering it would destroy whatever the user had started typing. Before
  // this, cancelling here restored nothing at all and the text was simply gone.
  const {
    onCancel: onCancelQueued,
    onInterrupt: onInterruptQueued,
    onEdit: onEditQueued,
    onReorder: onReorderQueued,
    pendingIds: queuePendingIds,
  } = useQueuedMessageActions({
    slot: slotKey,
    allQueued: allQueuedMessages,
    visibleQueued: queuedMessages,
    restoreDraft: restoreIntoComposer,
  })
  // Split-view panes draw the SAME transcript rows as the single-chat surface,
  // through the SDK's row registry: the live ToolCallLine (purpose / input /
  // output / live status), the workflow and sub-agent launch cards, thinking
  // traces, sent files, auto-nudge turns, recovery injects, workflow
  // completions. The SDK's built-in registry is store-free by design and so
  // draws weaker rows — or nothing at all — for most of these; the
  // store-connected set is supplied here as host entries instead, which is the
  // registry's intended extension path and keeps app-sdk/ChatMessageList
  // Redux-free for the embed SDK.
  //
  // The tool rows' expanded state is held ABOVE the rows: a row remounts
  // whenever the message list updates, and would otherwise forget it.
  const [toolDisclosure, setToolDisclosure] = useState<Record<string, boolean>>({})
  const setToolDisclosureFor = useCallback((key: string, expanded: boolean) => {
    setToolDisclosure((prev) => ({ ...prev, [key]: expanded }))
  }, [])
  const renderers = useMemo(
    () => createTranscriptRenderers({
      slot: slotKey,
      toolDisclosure,
      onToolDisclosureChange: setToolDisclosureFor,
    }),
    [slotKey, toolDisclosure, setToolDisclosureFor],
  )

  const ddInputCls = 'w-full px-2 py-1 text-[13px] font-body bg-bg border border-border rounded text-text outline-none focus-visible:border-accent'

  return (
    <SlotProvider slotId={slotKey}>
      <div
        onMouseDownCapture={onFocus}
        /* Focus capture keeps the grid's focused-pane state true under
           KEYBOARD navigation: tabbing into a pane (or into its portaled
           pickers, whose React events propagate through this component tree
           even though their DOM lives under document.body) claims grid focus
           exactly like a click. Without it only mousedown moved the marker,
           and a keyboard user could type into one pane while another stayed
           marked focused. */
        onFocusCapture={onFocus}
        /* Stable pane boundary for focus scoping: `queryComposer()` resolves
           the composer inside the pane that owns `document.activeElement` via
           this attribute, and falls back to the value "focused" — the grid's
           focused pane — when the active element has no pane ancestor (the
           pane's pickers portal to document.body). A data hook, not a class
           name: classes here are styling and can churn without anyone
           auditing focus behaviour. */
        data-chat-pane={focused ? 'focused' : ''}
        {...dropTargetProps}
        className={`relative flex flex-col h-full min-h-0 overflow-hidden bg-bg ${
          frameless
            ? ''
            : `rounded-lg border transition-colors ${focused ? 'border-accent' : 'border-border'}`
        }`}
        style={{
          '--mc-content-width': followContentWidth ? CONTENT_WIDTH[chatConfig.contentWidth].messages : '100%',
          // Split-view panes leave --mc-input-width UNSET so ChatInput keeps
          // its own fallback — byte-for-byte the pre-prop behavior.
          ...(followContentWidth ? { '--mc-input-width': CONTENT_WIDTH[chatConfig.contentWidth].input } : {}),
        } as React.CSSProperties}
      >
        {!frameless && (
        <div className="relative z-50 flex items-center gap-2 px-3 py-2 border-b border-border bg-card shrink-0">
          <span className={`w-2 h-2 rounded-full shrink-0 ${running ? 'bg-ok animate-pulse' : 'bg-accent'}`} />
          <span className="text-[13px] font-semibold text-text-strong truncate min-w-0">{title}</span>
          {parentKey && (
            <span
              className="shrink-0 text-[10px] text-accent bg-accent/10 rounded-full px-1.5 py-0.5 truncate max-w-[38%]"
              title={i18nT('components.chatPane.forked_from', { name: parentTitle || parentKey })}
            >
              ↳ {parentTitle || parentKey}
            </span>
          )}
          <span className="flex-1" />
          {running && <span className="shrink-0 text-[10px] text-ok font-mono">{streamState}</span>}
          {onSplitRight && (
            <button onClick={onSplitRight} title={i18nT('components.chatPane.split_right_d')} aria-label={i18nT('components.chatPane.split_right')} className="shrink-0 p-1 rounded text-muted hover:text-text hover:bg-bg-hover cursor-pointer bg-transparent border-none transition-colors">
              <SplitGlyph />
            </button>
          )}
          {onSplitDown && (
            <button onClick={onSplitDown} title={i18nT('components.chatPane.split_down')} aria-label={i18nT('components.chatPane.split_down')} className="shrink-0 p-1 rounded text-muted hover:text-text hover:bg-bg-hover cursor-pointer bg-transparent border-none transition-colors">
              <SplitGlyph down />
            </button>
          )}
          {onRemove && (
            <button onClick={onRemove} title={i18nT('components.chatPane.close_pane')} aria-label={i18nT('components.chatPane.close_pane')} className="shrink-0 rounded text-muted hover:text-danger hover:bg-danger/10 cursor-pointer p-1 transition-colors bg-transparent border-none">
              <X size={15} />
            </button>
          )}
        </div>
        )}

        <ChatDropOverlay active={dragOver} />

        {/* Zero-height anchor so the top fade overlays the scroller's first
            24px, dissolving content under the header edge (shared chrome —
            see ChatScrollChrome's layout contract). */}
        <div className="relative z-[1]">
          <EdgeFade side="top" />
        </div>

        {/* stable theming hook 'chat-container' — see website/docs/theming-contract.md */}
        {/* overflow-x-hidden: `overflow-y-auto` alone leaves overflow-x at
            `visible`, which CSS then forces to compute to `auto` — so any single
            over-wide child (a long unbroken path, a wide code block, a widget)
            gives the WHOLE message list a draggable horizontal scrollbar that
            sits right above the composer. The conversation should never pan
            sideways; wide children scroll within themselves. */}
        <div ref={follow.scrollerRef} onScroll={follow.onScroll} className="chat-container flex-1 overflow-y-auto overflow-x-hidden py-3 min-h-0">
          <div ref={follow.contentRef}>
          {messages.length === 0 && !running && (
            <div className="text-center text-muted text-[13px] py-8">{i18nT('components.chatPane.session_ready_type_a_message_to_start')}</div>
          )}
          {/* Suppressed on the active slot: that pane renders the store's full
              history, so the bound does not apply and the row would be false. */}
          {warmHasMore && slotKey !== activeSlot && onOpenFull && (
            <button
              onClick={() => onOpenFull(slotKey, messages[0]?.ts, messages[0]?.meta?.mid as string | undefined)}
              className="block w-full text-center text-accent text-[12px] underline py-2 bg-transparent border-none cursor-pointer hover:text-accent-hover transition-colors"
            >
              {i18nT('components.chatPane.earlier_messages_open_session')}
            </button>
          )}
          <ChatMessageList messages={messages} running={running} renderers={renderers} hideCardOwnedOAuth={connectionsUiOn} />
          {/* The same working indicator the full chat page shows (the ghost-pose
              carousel, theme-swappable via themeBranding): a running turn in a
              pane — a member DM, a split pane — was otherwise invisible between
              tool steps. Inside the scroll container, after the last message,
              so it reads as "the reply is coming" exactly where the reply will
              land. Stop/regenerate chrome stays page-level: the pane derives
              the footer's inputs from its own per-slot stream state. */}
          <ChatFooter
            running={running || !!paneSlot?.running}
            stopping={streamState === 'stopping' || !!paneSlot?.stopping}
            state={streamState}
            lastRole={messages[messages.length - 1]?.role ?? ''}
            streamTick={
              messages[messages.length - 1]?.role === 'streaming'
                ? (messages[messages.length - 1]?.content.length ?? 0)
                : 0
            }
          />
          </div>
        </div>
        {/* Bottom fade overlays the scroller's last 24px above the status bars
            and composer (in-flow height cancelled by its own negative margin). */}
        <EdgeFade side="bottom" />

        <div className="relative">
        <JumpToBottomButton visible={!follow.isAtBottom && messages.length > 0} onClick={follow.scrollToBottom} />

        <SubagentProgressBar slot={slotKey} />

        <SubagentDeliveryProgress count={systemDeliveryCount} />
        {queuedMessages.length > 0 && (
          <QueueStack messages={queuedMessages} onCancel={onCancelQueued} onInterrupt={onInterruptQueued} onEdit={onEditQueued} onReorder={onReorderQueued} pendingIds={queuePendingIds} />
        )}

        {/* The pending ask_question card renders per pane: in split mode the
            agent that asked may not be the pane the user is looking at, and
            without this its card never appears anywhere, so it waits out its
            full window. */}
        <PendingQuestionCard
          slotKey={slotKey}
          /* doSend() reads the composer state, so the fallback sends directly
             through the chat-core transport. The card is already cleared by
             the time this runs, so a swallowed failure would destroy the
             user's answer outright; on refusal, transport failure, or
             the abort deadline it goes back into the composer through the
             same recovery `doSend` uses. `response-late` restores HERE unlike
             the composer send: a deadline can fire before the POST ever
             reached the gateway, and with the card gone a silently lost
             answer has no other trace — the worst case is a duplicate answer,
             which the user can see and delete. `unknown` stays silent — a 2xx
             proves the request was accepted, so the answer may well have
             landed, and handing it back would invite a second answer to a
             question already gone. */
          onFallbackSend={(text) => {
            const fail = (reason?: string) => { reportSendFailure(reason); restoreIntoComposer(text) }
            void sendTurn({ message: text, slot: slotKey }).then((receipt) => {
              if (receipt.status === 'refused' || receipt.status === 'transport-error' || receipt.status === 'response-late') {
                fail(receipt.reason)
              }
            })
          }}
        />

        {uploadError && (
          <div className="mx-4 mt-2 mb-0 bg-bg-elevated border rounded-lg p-3 flex items-center gap-3 animate-rise" style={{ borderColor: 'color-mix(in srgb, var(--danger) 45%, transparent)' }}>
            <span className="text-sm text-text flex-1 min-w-0 break-words">{uploadError}</span>
            <button onClick={() => setUploadError('')} aria-label={i18nT('pages.chatPage.dismiss_upload_error')} className="text-muted hover:text-text leading-none p-0.5 shrink-0"><X className="w-4 h-4" /></button>
          </div>
        )}

        <ChatInput
          value={input}
          onChange={setInput}
          onSend={doSend}
          isRunning={busy}
          onStop={onStop}
          autoFocusKey={slotKey}
          agentName={paneAgentName}
          agentSource={installedAgents.find((a) => a.name === paneAgentName)?.source}
          modelName={shownModel}
          contextPct={contextPct}
          contextUsedTokens={contextTokens?.used}
          contextWindowTokens={contextTokens?.window || provider.getContextWindow(shownModel)}
          onAgentClick={!agentLocked && provider.capabilities.agentTemplates ? (rect) => { setAgentBtnRect(rect); agentDD.setOpen(!agentDD.open) } : undefined}
          onModelClick={(rect) => { setModelBtnRect(rect); modelDD.setOpen(!modelDD.open) }}
          approvalMode={displayMode}
          followUpOptions={followUpOptions}
          followUpPicked={followUpPicked}
          followUpLayout={chatConfig.followUpLayout}
          quickSend={dashCfg?.quick_send}
          followUpSourceKey={followUpSourceKey}
          followUpRecommended={followUpRecommended}
          onFollowUpSelect={(o: string, e: React.MouseEvent, sourceKeyAtClick?: string | null) => {
            // Mirrors ChatPage's wiring, plan branch included (#5893). Plan
            // options (Go / Go All / Cancel — the only labels the plan
            // pipeline emits and the only actions the endpoint accepts)
            // dispatch directly against THIS pane's slot — no input fill:
            // the same chip must mean the same thing here as in the main
            // chat. A plan-SHAPED message carrying non-protocol labels keeps
            // the composer path — dispatching those would 400 server-side
            // while also skipping the append, leaving a dead chip.
            // Slot record not yet delivered: dispatchPlanFollowUp no-ops
            // rather than appending an approval label (the reported bug).
            if (dispatchPlanFollowUp(o, sourceKeyAtClick)) return
            // One-click Quick Send takes the same gate as ChatPage: enabled +
            // no shift + not busy + not already in multi-select.
            if (tryQuickSend(o, dashCfg?.quick_send, e.shiftKey, busy, followUpPickedRef.current.size, (t: string) => doSend(t))) return
            // Regular options: toggle. Click unpicked → append + mark; click
            // picked → try to remove the text + unmark (if the user edited the
            // text so it no longer matches, leave the text alone — the chip
            // still un-highlights for consistency).
            if (followUpPickedRef.current.has(o)) {
              const next = new Set(followUpPickedRef.current); next.delete(o)
              followUpPickedRef.current = next
              setInput(prev => {
                // Order matters: try leading ", o" first so "opt, opt" + remove
                // last "opt" doesn't match "opt, " and splice the wrong one.
                // lastIndexOf, not indexOf: the handler appends options at the
                // END, so the last occurrence is the one it created — a draft
                // merely containing ", o" as a substring (draft "Please, Google"
                // + option "Go") must not be spliced mid-word.
                const leading = ', ' + o
                let idx = prev.lastIndexOf(leading)
                if (idx >= 0) return prev.slice(0, idx) + prev.slice(idx + leading.length)
                const trailing = o + ', '
                idx = prev.indexOf(trailing)
                if (idx >= 0) return prev.slice(0, idx) + prev.slice(idx + trailing.length)
                if (prev === o) return ''
                return prev  // user edited — leave text, still unmark below
              })
              setFollowUpPicked(next)
            } else {
              const next = new Set(followUpPickedRef.current); next.add(o)
              followUpPickedRef.current = next
              setInput(prev => prev.trim() ? prev.trimEnd() + ', ' + o : o)
              setFollowUpPicked(next)
            }
          }}
          onFollowUpSend={(text?: string, sourceKeyAtClick?: string | null) => {
            // Double-click and Send-now share dispatchPlanFollowUp with
            // single-click (#6240). `sourceKeyAtClick` is the first-click
            // row — a straddled double-click on a replaced footer is refused.
            if (text && dispatchPlanFollowUp(text, sourceKeyAtClick)) return
            doSend(text)
          }}
          project={paneSlot?.project ?? ''}
          onUploadFiles={uploadFiles}
          pendingFiles={pendingFiles}
          onRemoveFile={(p) => setPendingFiles((prev) => prev.filter((x) => x !== p))}
          uploading={uploadMutation.isPending}
          onDrop={dropTargetProps.onDrop}
          onDragOver={dropTargetProps.onDragOver}
          onDragLeave={dropTargetProps.onDragLeave}
        />
        </div>

        {/* Agent picker portal — anchored to the input-bar agent button. */}
        {agentDD.open && agentBtnRect && createPortal(
          /* The labeled dialog owns roving-focus key handling for its descendants. */
          // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions
          <div
            ref={agentDD.dropdownRef}
            role="dialog"
            aria-label={i18nT('components.chatPane.agent_list')}
            tabIndex={-1}
            onKeyDown={onAgentListKeyDown}
            className="fixed z-[9999] bg-bg-elevated border border-border rounded-xl shadow-xl min-w-[260px] max-w-[340px] flex flex-col p-1 gap-0.5 animate-slide-up"
            style={(() => { const left = Math.max(8, Math.min(agentBtnRect.left, window.innerWidth - 348)); return { bottom: window.innerHeight - agentBtnRect.top + 4, left } })()}
          >
            <div className="px-1.5 pt-1.5 pb-1">
              <input
                ref={agentDD.inputRef}
                type="text"
                aria-label={i18nT('components.chatPane.type_to_filter')}
                placeholder={i18nT('components.chatPane.type_to_filter')}
                value={agentDD.filter}
                onChange={(e) => agentDD.setFilter(e.target.value)}
                /* Enter/Escape live on the portal container's onListKeyDown
                   (useListboxKeyboard), which claims Enter against IME
                   composition internally — a second handler here would give
                   the same keys two dispatch paths. */
                className={ddInputCls}
              />
            </div>
            <div role="listbox" aria-label={i18nT('components.chatPane.agent_list')} className="overflow-y-auto max-h-[280px]">
              <AgentDropdownList agents={agentDD.filtered} activeAgent={paneAgentName} defaultAgent={defaultAgent} onSelect={(name) => { switchAgent(name); agentDD.setOpen(false) }} />
            </div>
            <DefaultAgentRow agentName={paneAgentName} isDefault={paneAgentName === defaultAgent} onSetDefault={() => toggleDefaultAgent(paneAgentName)} />
            <ManageAgentsFooter error={defaultAgentFailed} onManage={() => { agentDD.setOpen(false); navigate('/capabilities?tab=templates') }} />
          </div>,
          document.body,
        )}

        {/* Model picker portal — anchored to the input-bar model button. */}
        {modelDD.open && modelBtnRect && createPortal(
          /* The labeled dialog owns roving-focus key handling for its descendants. */
          // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions
          <div
            ref={modelDD.dropdownRef}
            role="dialog"
            aria-label={i18nT('components.chatPane.model_list')}
            tabIndex={-1}
            onKeyDown={onModelListKeyDown}
            className="fixed z-[9999] bg-bg-elevated border border-border rounded-xl shadow-xl min-w-[252px] max-w-[348px] flex flex-col p-1 gap-0.5 animate-slide-up"
            style={(() => { const left = Math.max(8, Math.min(modelBtnRect.left, window.innerWidth - 348)); return { bottom: window.innerHeight - modelBtnRect.top + 4, left } })()}
          >
            <div className="px-1.5 pt-1.5 pb-1">
              <input
                ref={modelDD.inputRef}
                type="text"
                aria-label={i18nT('components.chatPane.type_to_filter')}
                placeholder={i18nT('components.chatPane.type_to_filter')}
                value={modelDD.filter}
                onChange={(e) => modelDD.setFilter(e.target.value)}
                /* Enter/Escape live on the portal container's onListKeyDown
                   (useListboxKeyboard), which claims Enter against IME
                   composition internally — a second handler here would give
                   the same keys two dispatch paths. */
                className={ddInputCls}
              />
            </div>
            <div role="listbox" aria-label={i18nT('components.chatPane.model_list')} className="overflow-y-auto max-h-[280px]">
              <ModelDropdownList models={modelDD.filtered} activeModel={shownModel} onSelect={(name) => { switchModel(name); modelDD.setOpen(false) }} />
            </div>
          </div>,
          document.body,
        )}

      </div>
    </SlotProvider>
  )
}
