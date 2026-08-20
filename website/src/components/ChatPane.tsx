import { useState, useRef, useCallback, useEffect, useMemo } from 'react'
import { createPortal } from 'react-dom'
import { useNavigate } from 'react-router-dom'
import { X } from 'lucide-react'
import { SplitGlyph } from './SplitGlyph'
import { useQuery, useMutation } from '@tanstack/react-query'
import { useModelsDegraded } from '../providers/modelListHealth'
import ChatMessageList from '../app-sdk/ChatMessageList'
import { createTranscriptRenderers } from '../pages/chat/transcriptRenderers'
import ChatInput from './ChatInput'
import PendingQuestionCard from './PendingQuestionCard'
import QueueStack, { SubagentDeliveryProgress, splitPaneMessages } from './QueueStack'
import SubagentProgressBar from '../pages/chat/SubagentProgressBar'
import AgentDropdownList, { DefaultAgentRow, ManageAgentsFooter } from './AgentDropdownList'
import { agentSwitchFailureMessage } from '../utils/agentSwitchFeedback'
import ModelDropdownList from './ModelDropdownList'
import { SlotProvider } from '../providers/SlotContext'
import { useProvider } from '../providers'
import { useAgents } from '../hooks/useAgents'
import { useFilteredDropdown } from '../hooks/useFilteredDropdown'
import { useConnectionsUiEnabled } from '../hooks/useConnectionsUi'
import { useAvailableModels } from '../hooks/useAvailableModels'
import { useListboxKeyboard } from '../hooks/useListboxKeyboard'
import { useAppSelector, useAppDispatch, store } from '../store'
import { retireStatelessQuestion, captureStatelessCard, capturePendingAskId, confirmOptimisticSend, selectSlotMessages, selectSlotStreamState, selectComposerBusy, hydrateSlotMessages, appendSlotMessage, requestStop, cancelQueuedMessage, setAgentSwitchNotice } from '../store/chatSlice'
import { confirmedDelivered } from '../utils/sendDelivery'
import { triggerRefresh } from '../store/dashboardSlice'
import { api } from '../api/client'
import { resolveAskAfterSend } from '../lib/resolveAskAfterSend'
import { classifyDrop } from '../utils/dropClassify'
import { serializeDirTokens, spliceDirTokens } from '../utils/fileTokens'
import { displayModel } from '../lib/model'


import { i18nT } from '../i18n/t'
/** Stop waiting on a pane send's response. Mirrors the same bound in
 *  `ChatPage.send`, and carries its meaning too: reaching it means the request
 *  was received and only the reply is late, so the turn's output arrives over
 *  the socket rather than through this promise. It is NOT a failure signal. */
const SEND_ABORT_MS = 10_000

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
}: {
  slotKey: string
  focused?: boolean
  onFocus?: () => void
  onRemove?: () => void
  onSplitRight?: () => void
  onSplitDown?: () => void
}) {
  const dispatch = useAppDispatch()
  const provider = useProvider()
  // Same gate the main chat uses: hide a Connections-owned OAuth banner only
  // while the card that owns that flow is reachable.
  const connectionsUiOn = useConnectionsUiEnabled()
  const [input, setInput] = useState('')
  const [pendingFiles, setPendingFiles] = useState<string[]>([])
  const [dragOver, setDragOver] = useState(false)
  const [agentBtnRect, setAgentBtnRect] = useState<DOMRect | null>(null)
  const [modelBtnRect, setModelBtnRect] = useState<DOMRect | null>(null)
  const endRef = useRef<HTMLDivElement>(null)
  const lastHashRef = useRef('')
  const isAtBottomRef = useRef(true)

  const allMessages = useAppSelector((s) => selectSlotMessages(s, slotKey))
  const streamState = useAppSelector((s) => selectSlotStreamState(s, slotKey))
  const running = streamState !== 'idle'
  // Per-slot context-window usage for the input-bar ring (mirrors ChatPage; the
  // store keys these by slot). Default 0 so the ring always renders, exactly
  // like single chat.
  const contextPct = useAppSelector((s) => s.chat.slotContextPct[slotKey] ?? 0)
  const contextTokens = useAppSelector((s) => s.chat.slotContextTokens?.[slotKey])
  const paneSlot = useAppSelector((s) => s.dashboard.slots.find((x) => x.key === slotKey))
  // One source for both same-meaning markers in the agent pop-up: the row's check and
  // the default-agent row's label.
  const paneAgentName = paneSlot?.agent || 'default'
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

  // Pickers — same hooks/data sources ChatPage uses, but selection targets THIS slot.
  // Subscribes to the store's global refresh so a default-agent write in ANY pane (or
  // in single chat) lands here too; a per-hook refresh would leave sibling pickers stale.
  const agentsRefreshTrigger = useAppSelector((s) => s.dashboard.refreshTrigger ?? 0)
  // This pane takes no project prop, so read THIS slot's project from the store:
  // it scopes which project-local agents exist, so a project change must refetch.
  const paneProject = useAppSelector((s) => s.dashboard.slots.find((x) => x.key === slotKey)?.project || undefined)
  const { agents: installedAgents, defaultAgent } = useAgents(agentsRefreshTrigger, slotKey, paneProject)
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
  // access to. The degraded flag gates it — a cached list served while
  // /api/models fails is stale and cannot disprove entitlement — and is
  // subscribed to, since it can flip while the served list stays identical.
  const _modelsDegraded = useModelsDegraded(provider.id)
  const shownModel = displayModel(paneSlot?.model || '', availableModels, _modelsDegraded)

  // One-time hydrate of this slot's message history via React Query + the api
  // client (caching + cross-pane dedup; staleTime Infinity keeps it one-shot —
  // live updates arrive through the WS store routing, not a refetch).
  const { data: slotDetail } = useQuery({
    queryKey: ['slot-messages', slotKey],
    queryFn: () => api.chatSlotDetail(slotKey),
    staleTime: Infinity,
  })
  useEffect(() => {
    if (slotDetail?.messages) dispatch(hydrateSlotMessages({ slot: slotKey, messages: slotDetail.messages }))
  }, [slotDetail, slotKey, dispatch])

  // Track whether this pane is scrolled to the bottom. The endRef sentinel sits
  // at the bottom of the scroll container (the overflow-y-auto div); when it's
  // intersecting, the user is pinned to the bottom. Mirrors ChatPage's
  // isAtBottom guard so auto-scroll never yanks a user who scrolled up to read
  // earlier messages in a streaming pane.
  useEffect(() => {
    const el = endRef.current
    if (!el || !el.parentElement) return
    const observer = new IntersectionObserver(
      ([entry]) => { isAtBottomRef.current = entry.isIntersecting },
      { root: el.parentElement, threshold: 0.1 },
    )
    observer.observe(el)
    return () => observer.disconnect()
  }, [])

  const msgHash =
    messages.length + ':' + (messages[messages.length - 1]?.content?.length || 0) + ':' + queuedMessages.length
  useEffect(() => {
    if (msgHash !== lastHashRef.current) {
      lastHashRef.current = msgHash
      // Only auto-scroll when the user is already at the bottom — don't drag
      // someone reading history back down on every message hash change.
      if (!isAtBottomRef.current) return
      // Smooth only when idle; during streaming use 'instant' so we don't queue
      // dozens of concurrent smooth-scroll animations per second (jank).
      endRef.current?.scrollIntoView({ behavior: running ? 'instant' : 'smooth' })
    }
  }, [msgHash, running])


  const switchAgent = useCallback((name: string) => {
    dispatch(setAgentSwitchNotice(null))
    api.chatSlotAgent(slotKey, name)
      .catch((e) => dispatch(setAgentSwitchNotice(agentSwitchFailureMessage(e))))
  }, [dispatch, slotKey])
  const switchModel = useCallback((name: string) => { api.chatSlotModel(slotKey, name).catch((e) => console.error('[ChatPane] switchModel failed', e)) }, [slotKey])

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
    onSuccess: (res) => { if (res.paths?.length) setPendingFiles((prev) => [...prev, ...res.paths]) },
  })
  const uploadFiles = useCallback((files: File[]) => {
    if (!files.length || files.length > 20) return
    if (files.find((f) => f.size > 50 * 1024 * 1024)) return
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
  const handleDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault(); e.stopPropagation(); setDragOver(false)
    const { files, dirPaths } = classifyDrop(e.dataTransfer)
    if (dirPaths.length) setInput((prev) => spliceDirTokens(prev, null, dirPaths).value)
    if (files.length) uploadFiles(files)
  }, [uploadFiles])

  /** Put a payload the server never accepted back into the composer.
   *
   *  APPEND, never replace and never DROP: a send is in flight for seconds and
   *  the user can type a fresh message in that window, so neither payload may
   *  overwrite the other — preferring the newer one silently discards the message
   *  the error row is telling them to retry, preferring the older one loses work
   *  they just did. Identical text is not duplicated, and attachments merge as a
   *  set union so a file re-picked mid-flight is not double-attached. Joined
   *  rather than interpolated: the blank line is message structure, not copy.
   *
   *  Shared by both recovery sites in this pane (a failed `doSend` and a failed
   *  question-card fallback) so the pane has ONE spelling of it. */
  const restoreIntoComposer = useCallback((text: string, files: string[] = []) => {
    setInput(prev => {
      const keep = prev.replace(/\s+$/, '')
      if (!keep.trim()) return text
      if (keep.trim() === text.trim()) return prev
      return [keep, text].join('\n\n')
    })
    if (files.length) setPendingFiles(prev => [...prev, ...files.filter(f => !prev.includes(f))])
  }, [])

  const doSend = useCallback(() => {
    const text = input.trim()
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
    setInput('')
    const files = pendingFiles
    setPendingFiles([])
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
    // error row and handed the text back; the pane now does the same, addressed
    // to the slot that OWNS the message rather than the active one — the user
    // can switch panes while the POST is in flight.
    //
    // `reason` is the server's own explanation when there is one (a 409 "slot
    // agent mismatch" is actionable; "check your connection" is not). Absent on
    // the transport-reject path, where no body exists.
    const reportFailedSend = (reason?: string) => {
      dispatch(appendSlotMessage({
        slot: slotKey,
        message: {
          role: 'error',
          content: reason || (i18nT('pages.chatPage.send_failed') as string),
          cls: '',
        },
      }))
      restoreIntoComposer(text, files)
    }
    // Same 10s abort `ChatPage.send` uses. Without it a HUNG (not refused) POST
    // settles neither way until the browser's own network timeout, so the
    // message sits on screen looking sent for minutes with the composer already
    // cleared — the exact window this change exists to close, and the one place
    // the removed 30s notice used to speak sooner.
    const controller = new AbortController()
    const timeout = setTimeout(() => controller.abort(), SEND_ABORT_MS)
    api.sendChat(llm, slotKey, undefined, controller.signal, meta)
      .then(async (r) => {
        clearTimeout(timeout)
        const body = await r.json().catch(() => ({}))
        // The server accepted neither `ok` nor `queued`, so nothing was sent.
        // Reported before the card logic below, which only runs on acceptance.
        if (!body.ok && !body.queued) { reportFailedSend(body.error as string | undefined); return }
        // A `queued` acceptance with no wire text is not an acceptance at all.
        // `chat_handlers` queues `if message:` but returns `{ok, queued}`
        // unconditionally, so an attachment-only send that raced the slot into
        // the busy state was neither queued nor broadcast — nothing carries the
        // attachment, and the composer has already cleared. Reported so the file
        // comes back rather than vanishing. (The same request answers 400 when
        // the slot is idle, which the branch above already handles.)
        if (body.queued && !llm.trim()) { reportFailedSend(); return }
        // The response is the delivery receipt for this pane's optimistic bubble
        // (#4131) — see the same dispatch in ChatPage.send for why no `chat_message`
        // echo is coming. Parsed unconditionally now: the previous early return on
        // "no card and no ask" skipped the body entirely, which would have skipped
        // this confirmation too. `confirmedDelivered` accepts only an IMMEDIATE
        // dispatch: a queued acceptance is not a receipt for this bubble.
        if (confirmedDelivered(body)) dispatch(confirmOptimisticSend({ slot: slotKey, sendId }))
        if (!cardAtSend && !askAtSend) return
        // `ok` only: a QUEUED acceptance is still cancellable — the queued
        // path retires at its queue_pop instead (removeQueuedMessage).
        if (body.ok && !body.queued && cardAtSend) dispatch(retireStatelessQuestion({ slot: slotKey, expected: cardAtSend }))
        void resolveAskAfterSend(body, askAtSend, dispatch)
      })
      .catch((e: unknown) => {
        clearTimeout(timeout)
        // An abort means the request WAS received and only the RESPONSE is late,
        // which is what `ChatPage` records at its own timeout ("message was
        // received, WS will deliver response") — the turn is running and its
        // output arrives over the socket. Reporting that as a failure would hand
        // the payload back and invite a retry that duplicates a turn already in
        // flight, side effects included. Only a rejection that is NOT an abort
        // means the send never left.
        if (e instanceof DOMException && e.name === 'AbortError') return
        reportFailedSend()
      })
  }, [input, pendingFiles, busy, slotKey, dispatch, restoreIntoComposer])

  const onStop = useCallback(() => { dispatch(requestStop({ slotId: slotKey, force: false })) }, [dispatch, slotKey])
  const onCancelQueued = useCallback((queueId: string) => {
    dispatch(cancelQueuedMessage({ slot: slotKey, queue_id: queueId }))
    api.cancelQueuedMessage(slotKey, queueId).catch(() => undefined)
  }, [dispatch, slotKey])
  const onInterruptQueued = useCallback((queueId: string) => { api.interruptSlot(slotKey, queueId).catch(() => undefined) }, [slotKey])
  const onReorderQueued = useCallback((queueId: string, direction: 'next' | 'later') => {
    // Build the order from ALL queued messages in the slot, not just the
    // interactive ones QueueStack renders: hidden system deliveries and
    // recovery continuations are queued too, and submitting only the visible
    // ids would let the backend append the omitted ones at the tail, silently
    // demoting automation. The swap happens between adjacent VISIBLE cards but
    // is expressed inside the complete id sequence.
    const fullIds = allMessages
      .filter(m => m.role === 'queued')
      .map(m => m.meta?.queueId as string)
      .filter(Boolean)
    const visibleIds = queuedMessages.map(m => m.meta?.queueId as string).filter(Boolean)
    const vFrom = visibleIds.indexOf(queueId)
    const vTo = direction === 'next' ? vFrom - 1 : vFrom + 1
    if (vFrom < 0 || vTo < 0 || vTo >= visibleIds.length) return
    const a = fullIds.indexOf(visibleIds[vFrom])
    const b = fullIds.indexOf(visibleIds[vTo])
    if (a < 0 || b < 0) return
    const next = [...fullIds]
    ;[next[a], next[b]] = [next[b], next[a]]
    // No optimistic dispatch: the server commits and broadcasts queue_reorder
    // to every client including this one, and that WS event is the
    // authoritative store update. A local dispatch with rollback-on-failure
    // could restore a stale order when the server committed but the HTTP
    // response was lost, leaving this client in conflict with execution order.
    api.reorderQueuedMessages(slotKey, next).catch(() => undefined)
  }, [slotKey, allMessages, queuedMessages])
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
        className={`flex flex-col h-full min-h-0 rounded-lg overflow-hidden bg-bg border transition-colors ${focused ? 'border-accent' : 'border-border'}`}
        style={{ '--mc-content-width': '100%' } as React.CSSProperties}
      >
        <div className="flex items-center gap-2 px-3 py-2 border-b border-border bg-card shrink-0">
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

        {/* stable theming hook 'chat-container' — see website/docs/theming-contract.md */}
        {/* overflow-x-hidden: `overflow-y-auto` alone leaves overflow-x at
            `visible`, which CSS then forces to compute to `auto` — so any single
            over-wide child (a long unbroken path, a wide code block, a widget)
            gives the WHOLE message list a draggable horizontal scrollbar that
            sits right above the composer. The conversation should never pan
            sideways; wide children scroll within themselves. */}
        <div className="chat-container flex-1 overflow-y-auto overflow-x-hidden py-3 min-h-0">
          {messages.length === 0 && !running && (
            <div className="text-center text-muted text-[13px] py-8">{i18nT('components.chatPane.session_ready_type_a_message_to_start')}</div>
          )}
          <ChatMessageList messages={messages} running={running} renderers={renderers} hideCardOwnedOAuth={connectionsUiOn} />
          <div ref={endRef} />
        </div>

        <SubagentProgressBar slot={slotKey} />

        <SubagentDeliveryProgress count={systemDeliveryCount} />
        {queuedMessages.length > 0 && (
          <QueueStack messages={queuedMessages} onCancel={onCancelQueued} onInterrupt={onInterruptQueued} onReorder={onReorderQueued} />
        )}

        {/* The pending ask_question card renders per pane: in split mode the
            agent that asked may not be the pane the user is looking at, and
            without this its card never appears anywhere, so it waits out its
            full window. */}
        <PendingQuestionCard
          slotKey={slotKey}
          /* doSend() reads the composer state, so the fallback sends directly.
             `sendChat` returns the raw Response, so a non-OK status RESOLVES
             rather than rejecting — both have to be checked. The card is already
             cleared by the time this runs, so a swallowed failure would destroy
             the user's answer outright; on any failure it goes back into the
             composer through the same recovery `doSend` uses. */
          onFallbackSend={(text) => {
            api
              .sendChat(text, slotKey)
              .then((res) => {
                if (!res || !res.ok) throw new Error(`send failed (${res?.status ?? 'no response'})`)
              })
              .catch(() => {
                restoreIntoComposer(text)
              })
          }}
        />

        <ChatInput
          value={input}
          onChange={setInput}
          onSend={doSend}
          isRunning={busy}
          onStop={onStop}
          autoFocusKey={slotKey}
          agentName={paneSlot?.agent || 'default'}
          agentSource={installedAgents.find((a) => a.name === (paneSlot?.agent || 'default'))?.source}
          modelName={shownModel}
          contextPct={contextPct}
          contextUsedTokens={contextTokens?.used}
          contextWindowTokens={contextTokens?.window || provider.getContextWindow(shownModel)}
          onAgentClick={provider.capabilities.agentTemplates ? (rect) => { setAgentBtnRect(rect); agentDD.setOpen(!agentDD.open) } : undefined}
          onModelClick={(rect) => { setModelBtnRect(rect); modelDD.setOpen(!modelDD.open) }}
          approvalMode={displayMode}
          onUploadFiles={uploadFiles}
          pendingFiles={pendingFiles}
          onRemoveFile={(p) => setPendingFiles((prev) => prev.filter((x) => x !== p))}
          uploading={uploadMutation.isPending}
          onDrop={handleDrop}
          dragOver={dragOver}
          onDragOver={(e) => { e.preventDefault(); e.stopPropagation(); setDragOver(true) }}
          onDragLeave={(e) => { if (e.currentTarget === e.target) setDragOver(false) }}
        />

        {/* Agent picker portal — anchored to the input-bar agent button. */}
        {agentDD.open && agentBtnRect && createPortal(
          <div
            ref={agentDD.dropdownRef}
            tabIndex={-1}
            onKeyDown={onAgentListKeyDown}
            className="fixed z-[9999] bg-bg-elevated border border-border rounded-xl shadow-xl min-w-[260px] max-w-[340px] flex flex-col p-1 gap-0.5 animate-slide-up"
            style={(() => { const left = Math.max(8, Math.min(agentBtnRect.left, window.innerWidth - 348)); return { bottom: window.innerHeight - agentBtnRect.top + 4, left } })()}
          >
            <div className="px-1.5 pt-1.5 pb-1">
              <input
                ref={agentDD.inputRef}
                type="text"
                placeholder={i18nT('components.chatPane.type_to_filter')}
                value={agentDD.filter}
                onChange={(e) => agentDD.setFilter(e.target.value)}
                onKeyDown={(e) => { if (e.key === 'Escape') agentDD.setOpen(false); if (e.key === 'Enter' && agentDD.filtered.length === 1) { switchAgent(agentDD.filtered[0].name); agentDD.setOpen(false) } }}
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
          <div
            ref={modelDD.dropdownRef}
            tabIndex={-1}
            onKeyDown={onModelListKeyDown}
            className="fixed z-[9999] bg-bg-elevated border border-border rounded-xl shadow-xl min-w-[252px] max-w-[348px] flex flex-col p-1 gap-0.5 animate-slide-up"
            style={(() => { const left = Math.max(8, Math.min(modelBtnRect.left, window.innerWidth - 348)); return { bottom: window.innerHeight - modelBtnRect.top + 4, left } })()}
          >
            <div className="px-1.5 pt-1.5 pb-1">
              <input
                ref={modelDD.inputRef}
                type="text"
                placeholder={i18nT('components.chatPane.type_to_filter')}
                value={modelDD.filter}
                onChange={(e) => modelDD.setFilter(e.target.value)}
                onKeyDown={(e) => { if (e.key === 'Escape') modelDD.setOpen(false); if (e.key === 'Enter' && modelDD.filtered.length === 1) { switchModel(modelDD.filtered[0].name); modelDD.setOpen(false) } }}
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
