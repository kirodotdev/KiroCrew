import { useState, useRef, useCallback, useEffect, useMemo } from 'react'
import { createPortal } from 'react-dom'
import { useNavigate } from 'react-router-dom'
import { X } from 'lucide-react'
import { SplitGlyph } from './SplitGlyph'
import { useQuery, useMutation } from '@tanstack/react-query'
import { useModelsDegraded } from '../providers/modelListHealth'
import ChatMessageList from '../app-sdk/ChatMessageList'
import ToolCallLine from '../pages/chat/ToolCallLine'
import type { ChatMessage } from '../types'
import ChatInput from './ChatInput'
import PendingQuestionCard from './PendingQuestionCard'
import QueueStack, { SubagentDeliveryProgress, splitPaneMessages } from './QueueStack'
import SubagentProgressBar from '../pages/chat/SubagentProgressBar'
import AgentDropdownList, { ManageAgentsFooter } from './AgentDropdownList'
import ModelDropdownList from './ModelDropdownList'
import { SlotProvider } from '../providers/SlotContext'
import { useProvider } from '../providers'
import { useAgents } from '../hooks/useAgents'
import { useFilteredDropdown } from '../hooks/useFilteredDropdown'
import { useAvailableModels } from '../hooks/useAvailableModels'
import { useListboxKeyboard } from '../hooks/useListboxKeyboard'
import { useAppSelector, useAppDispatch } from '../store'
import { selectSlotMessages, selectSlotStreamState, selectComposerBusy, hydrateSlotMessages, appendSlotMessage, requestStop, cancelQueuedMessage } from '../store/chatSlice'
import { triggerRefresh } from '../store/dashboardSlice'
import { api } from '../api/client'
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
  const { agents: installedAgents, defaultAgent } = useAgents(agentsRefreshTrigger)
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


  const switchAgent = useCallback((name: string) => { api.chatSlotAgent(slotKey, name).catch((e) => console.error('[ChatPane] switchAgent failed', e)) }, [slotKey])
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

  const doSend = useCallback(() => {
    const text = input.trim()
    if (!text && !pendingFiles.length) return
    setInput('')
    const files = pendingFiles
    setPendingFiles([])
    // Optimistic user bubble: show immediately in the right position (mirrors the
    // single-chat send). Skipped while busy (main turn streaming OR sub-agents
    // running) — the backend returns a "queued" message instead, avoiding a duplicate.
    if (!busy && (text || files.length)) {
      dispatch(appendSlotMessage({
        slot: slotKey,
        message: { role: 'user', content: text, cls: 'msg msg-u', ts: new Date().toISOString(), ...(files.length ? { meta: { files } } : {}) },
      }))
    }
    const meta = files.length ? { files } : undefined
    api.sendChat(text, slotKey, undefined, undefined, meta).catch(() => undefined)
  }, [input, pendingFiles, busy, slotKey, dispatch])

  const onStop = useCallback(() => { dispatch(requestStop({ slotId: slotKey, force: false })) }, [dispatch, slotKey])
  const onCancelQueued = useCallback((queueId: string) => {
    dispatch(cancelQueuedMessage({ slot: slotKey, queue_id: queueId }))
    api.cancelQueuedMessage(slotKey, queueId).catch(() => undefined)
  }, [dispatch, slotKey])
  const onInterruptQueued = useCallback((queueId: string) => { api.interruptSlot(slotKey, queueId).catch(() => undefined) }, [slotKey])
  // Split-view panes render tool calls with the full ToolCallLine (purpose / input /
  // output / live status) instead of the SDK's bare pill. ToolCallLine's slot-aware
  // selectors read THIS slot's per-slot tool log, so a background pane shows the same
  // live tool detail as the main chat view. Injected as a render prop so
  // app-sdk/ChatMessageList stays Redux-free for the embed SDK.
  const renderTool = useCallback((m: ChatMessage) => <ToolCallLine message={m} running={running} slot={slotKey} />, [slotKey, running])

  const ddInputCls = 'w-full px-2 py-1 text-[13px] font-mono bg-bg border border-border rounded text-text outline-none focus:border-accent'

  return (
    <SlotProvider slotId={slotKey}>
      <div
        onMouseDownCapture={onFocus}
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
          <ChatMessageList messages={messages} running={running} renderTool={renderTool} />
          <div ref={endRef} />
        </div>

        <SubagentProgressBar slot={slotKey} />

        <SubagentDeliveryProgress count={systemDeliveryCount} />
        {queuedMessages.length > 0 && (
          <QueueStack messages={queuedMessages} onCancel={onCancelQueued} onInterrupt={onInterruptQueued} />
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
             composer instead. */
          onFallbackSend={(text) => {
            api
              .sendChat(text, slotKey)
              .then((res) => {
                if (!res || !res.ok) throw new Error(`send failed (${res?.status ?? 'no response'})`)
              })
              .catch(() => {
                setInput((prev) => (prev.trim() ? `${prev}\n${text}` : text))
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
          onDrop={(e) => { e.preventDefault(); e.stopPropagation(); setDragOver(false); const f = Array.from(e.dataTransfer.files); if (f.length) uploadFiles(f) }}
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
              <AgentDropdownList agents={agentDD.filtered} activeAgent={paneSlot?.agent || 'default'} defaultAgent={defaultAgent} onSelect={(name) => { switchAgent(name); agentDD.setOpen(false) }} onSetDefault={toggleDefaultAgent} />
            </div>
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
