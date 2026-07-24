import { useState, useRef, useCallback, useEffect } from 'react'
import { createPortal } from 'react-dom'
import { X, ShieldCheck, BookOpen, Handshake, Rocket, Check } from 'lucide-react'
import { SplitGlyph } from './SplitGlyph'
import { useQuery, useMutation } from '@tanstack/react-query'
import { modelListRefetchInterval } from '../providers/modelListHealth'
import ChatMessageList from '../app-sdk/ChatMessageList'
import ToolCallLine from '../pages/chat/ToolCallLine'
import type { ChatMessage } from '../types'
import ChatInput from './ChatInput'
import QueueStack from './QueueStack'
import SubagentProgressBar from '../pages/chat/SubagentProgressBar'
import AgentDropdownList from './AgentDropdownList'
import ModelDropdownList from './ModelDropdownList'
import { SlotProvider } from '../providers/SlotContext'
import { useProvider } from '../providers'
import { useAgents } from '../hooks/useAgents'
import { useFilteredDropdown } from '../hooks/useFilteredDropdown'
import { useListboxKeyboard } from '../hooks/useListboxKeyboard'
import { useAppSelector, useAppDispatch } from '../store'
import { selectSlotMessages, selectSlotStreamState, selectComposerBusy, selectSlotSubagentsRunning, hydrateSlotMessages, appendSlotMessage, requestStop, cancelQueuedMessage, openActivityToTab } from '../store/chatSlice'
import { api } from '../api/client'
import { changeApprovalMode } from '../store/dashboardSlice'
import { safeSetItem } from '../utils/safeStorage'

const APPROVAL_SEGMENTS = [
  { key: 'normal' as const, label: 'Normal', icon: <ShieldCheck size={13} />, tooltip: 'KiroCrew asks you before doing anything', desc: 'KiroCrew checks with you before doing anything' },
  { key: 'trust_reads' as const, label: 'Reads', icon: <BookOpen size={13} />, tooltip: 'KiroCrew looks things up on its own, but asks before making changes', desc: 'KiroCrew looks things up on its own, but asks before making any changes' },
  { key: 'trust' as const, label: 'Trust', icon: <Handshake size={13} />, tooltip: 'In this chat, KiroCrew works without asking you first', desc: 'In this chat, KiroCrew works without asking you first' },
  { key: 'yolo' as const, label: 'YOLO', icon: <Rocket size={13} />, tooltip: 'In every chat, KiroCrew works without asking you first', desc: 'In every chat, KiroCrew works without asking you first' },
]

/**
 * ChatPane — one live chat session in the native session grid (Path B S3 + S3d).
 *
 * Renders the REAL native <ChatInput> inside <SlotProvider> with the full
 * per-slot composer (model/agent/approval-mode pickers, attachments, QueueStack).
 * Messages stream live from the store (S1/S2); per-slot metadata comes from
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
  const [yoloDontAsk, setYoloDontAsk] = useState(false)
  const [agentBtnRect, setAgentBtnRect] = useState<DOMRect | null>(null)
  const [modelBtnRect, setModelBtnRect] = useState<DOMRect | null>(null)
  const [approvalDropdown, setApprovalDropdown] = useState(false)
  const [approvalBtnRect, setApprovalBtnRect] = useState<DOMRect | null>(null)
  const [yoloConfirm, setYoloConfirm] = useState(0)
  const approvalDropdownRef = useRef<HTMLDivElement>(null)
  const endRef = useRef<HTMLDivElement>(null)
  const lastHashRef = useRef('')
  const isAtBottomRef = useRef(true)

  const allMessages = useAppSelector((s) => selectSlotMessages(s, slotKey))
  const streamState = useAppSelector((s) => selectSlotStreamState(s, slotKey))
  const running = streamState !== 'idle'
  // Hard-lock this pane's composer while its own sub-agents run (Decision B) —
  // same slot-keyed selector ChatPage uses, so the split-view pane enforces the
  // invariant identically instead of being a silently-unlocked bypass.
  const subagentsRunning = useAppSelector((s) => selectSlotSubagentsRunning(s, slotKey))
  // Per-slot context-window usage for the input-bar ring (mirrors ChatPage; the
  // store already keys these by slot, the pane just never read them). Default 0
  // so the ring always renders, exactly like single chat.
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
  const messages = allMessages.filter((m) => m.role !== 'queued')
  const queuedMessages = allMessages.filter((m) => m.role === 'queued')

  // Pickers — same hooks/data sources ChatPage uses, but selection targets THIS slot.
  const { agents: installedAgents, defaultAgent } = useAgents(0)
  const agentDD = useFilteredDropdown(installedAgents)
  const { data: availableModels = [{ name: 'auto', description: 'Default' }] } = useQuery({
    queryKey: ['available-models', provider.id],
    queryFn: async () => {
      const models = await provider.fetchAvailableModels()
      return [{ name: 'auto', description: 'Default' }, ...models.filter((m) => m.name !== 'auto')]
    },
    refetchInterval: modelListRefetchInterval,
  })
  const modelDD = useFilteredDropdown(availableModels)

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

  // Close the approval dropdown on outside click (agent/model dropdowns handle
  // their own outside-click via useFilteredDropdown).
  useEffect(() => {
    if (!approvalDropdown) return
    const onDown = (e: MouseEvent) => {
      if (approvalDropdownRef.current?.contains(e.target as Node)) return
      setApprovalDropdown(false)
      setYoloConfirm(0)
    }
    document.addEventListener('mousedown', onDown)
    return () => document.removeEventListener('mousedown', onDown)
  }, [approvalDropdown])

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
  // Approval-mode picker has no filter input, so the hook focuses the first
  // mode option on open and roves the four <button role="option">s; Enter/Space
  // fire the button's onClick natively, Escape/Tab close + return focus.
  const approvalNoInputRef = useRef<HTMLElement | null>(null)
  const { onListKeyDown: onApprovalListKeyDown } = useListboxKeyboard({
    open: approvalDropdown,
    dropdownRef: approvalDropdownRef,
    inputRef: approvalNoInputRef,
    hasFilterInput: false,
    filteredCount: 0,
    onEnterSingleMatch: () => {},
    closeToTrigger: () => { setApprovalDropdown(false); setYoloConfirm(0) },
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
              title={`Forked from ${parentTitle || parentKey}`}
            >
              ↳ {parentTitle || parentKey}
            </span>
          )}
          <span className="flex-1" />
          {running && <span className="shrink-0 text-[10px] text-ok font-mono">{streamState}</span>}
          {onSplitRight && (
            <button onClick={onSplitRight} title="Split right (⌘D)" aria-label="Split right" className="shrink-0 p-1 rounded text-muted hover:text-text hover:bg-bg-hover cursor-pointer bg-transparent border-none transition-colors">
              <SplitGlyph />
            </button>
          )}
          {onSplitDown && (
            <button onClick={onSplitDown} title="Split down" aria-label="Split down" className="shrink-0 p-1 rounded text-muted hover:text-text hover:bg-bg-hover cursor-pointer bg-transparent border-none transition-colors">
              <SplitGlyph down />
            </button>
          )}
          {onRemove && (
            <button onClick={onRemove} title="Close pane" aria-label="Close pane" className="shrink-0 rounded text-muted hover:text-danger hover:bg-danger/10 cursor-pointer p-1 transition-colors bg-transparent border-none">
              <X size={15} />
            </button>
          )}
        </div>

        <div className="flex-1 overflow-y-auto py-3 min-h-0">
          {messages.length === 0 && !running && (
            <div className="text-center text-muted text-[13px] py-8">Session ready. Type a message to start.</div>
          )}
          <ChatMessageList messages={messages} running={running} renderTool={renderTool} />
          <div ref={endRef} />
        </div>

        <SubagentProgressBar slot={slotKey} />

        {queuedMessages.length > 0 && (
          <QueueStack messages={queuedMessages} onCancel={onCancelQueued} onInterrupt={onInterruptQueued} />
        )}

        <ChatInput
          value={input}
          onChange={setInput}
          onSend={doSend}
          isRunning={busy}
          onStop={onStop}
          subagentsRunning={subagentsRunning}
          onOpenSideChat={() => {
            // openActivityToTab acts on the GLOBAL active slot, so focus this
            // pane's slot first — otherwise, from a non-active pane, the escape
            // hatch would open the wrong slot's side chat while this pane stays
            // locked. onFocus activates the pane (same path as click/mousedown).
            onFocus?.()
            dispatch(openActivityToTab('side'))
          }}
          autoFocusKey={slotKey}
          agentName={paneSlot?.agent || 'default'}
          agentSource={installedAgents.find((a) => a.name === (paneSlot?.agent || 'default'))?.source}
          modelName={paneSlot?.model || 'auto'}
          contextPct={contextPct}
          contextUsedTokens={contextTokens?.used}
          contextWindowTokens={contextTokens?.window || provider.getContextWindow(paneSlot?.model || 'auto')}
          onAgentClick={provider.capabilities.agentTemplates ? (rect) => { setAgentBtnRect(rect); agentDD.setOpen(!agentDD.open) } : undefined}
          onModelClick={(rect) => { setModelBtnRect(rect); modelDD.setOpen(!modelDD.open) }}
          approvalMode={displayMode}
          onApprovalClick={(rect) => { setApprovalBtnRect(rect); setApprovalDropdown(!approvalDropdown) }}
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
                placeholder="Type to filter…"
                value={agentDD.filter}
                onChange={(e) => agentDD.setFilter(e.target.value)}
                onKeyDown={(e) => { if (e.key === 'Escape') agentDD.setOpen(false); if (e.key === 'Enter' && agentDD.filtered.length === 1) { switchAgent(agentDD.filtered[0].name); agentDD.setOpen(false) } }}
                className={ddInputCls}
              />
            </div>
            <div role="listbox" aria-label="Agent list" className="overflow-y-auto max-h-[280px]">
              <AgentDropdownList agents={agentDD.filtered} activeAgent={paneSlot?.agent || 'default'} defaultAgent={defaultAgent} onSelect={(name) => { switchAgent(name); agentDD.setOpen(false) }} />
            </div>
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
                placeholder="Type to filter…"
                value={modelDD.filter}
                onChange={(e) => modelDD.setFilter(e.target.value)}
                onKeyDown={(e) => { if (e.key === 'Escape') modelDD.setOpen(false); if (e.key === 'Enter' && modelDD.filtered.length === 1) { switchModel(modelDD.filtered[0].name); modelDD.setOpen(false) } }}
                className={ddInputCls}
              />
            </div>
            <div role="listbox" aria-label="Model list" className="overflow-y-auto max-h-[280px]">
              <ModelDropdownList models={modelDD.filtered} activeModel={paneSlot?.model || 'auto'} onSelect={(name) => { switchModel(name); modelDD.setOpen(false) }} />
            </div>
          </div>,
          document.body,
        )}

        {/* Approval-mode picker portal — reuses the existing per-slot control; the
            app-wide YOLO confirm gate (mc-yolo-ack) is preserved, not bypassed. */}
        {approvalDropdown && approvalBtnRect && createPortal(
          <div
            ref={approvalDropdownRef}
            tabIndex={-1}
            onKeyDown={onApprovalListKeyDown}
            className="fixed z-[9999] animate-slide-up flex items-end gap-2"
            style={(() => { const left = Math.max(8, Math.min(approvalBtnRect.left, window.innerWidth - 520)); return { bottom: window.innerHeight - approvalBtnRect.top + 4, left } })()}
          >
            <div role="listbox" aria-label="Approval mode" className="rounded-lg bg-bg-elevated border border-border py-1 w-[280px] shrink-0">
              {APPROVAL_SEGMENTS.map((s) => (
                <button
                  key={s.key}
                  role="option"
                  tabIndex={-1}
                  aria-selected={s.key === displayMode}
                  title={s.tooltip}
                  onClick={() => {
                    const m = s.key
                    if (m === 'yolo') {
                      if (displayMode === 'yolo') return
                      if (localStorage.getItem('mc-yolo-ack')) { dispatch(changeApprovalMode({ mode: m, slot: slotKey })); setApprovalDropdown(false) }
                      else setYoloConfirm((c) => c + 1)
                      return
                    }
                    setYoloConfirm(0)
                    dispatch(changeApprovalMode({ mode: m, slot: slotKey }))
                    setApprovalDropdown(false)
                  }}
                  className={`flex items-center gap-2 w-full px-3 py-2 text-[13px] font-medium cursor-pointer border-none bg-transparent text-left hover:bg-bg-hover ${s.key === displayMode ? 'text-accent' : 'text-text'}`}
                >
                  <span className="shrink-0">{s.icon}</span>
                  <span className="flex flex-col min-w-0 flex-1">
                    <span>{s.label}</span>
                    <span className="text-[11px] font-normal text-muted leading-snug">{s.desc}</span>
                  </span>
                  {s.key === displayMode && <Check size={12} className="shrink-0 text-accent" />}
                </button>
              ))}
            </div>
            {yoloConfirm > 0 && (
              <div className="px-3 py-2 rounded-lg bg-bg-elevated border border-border text-[12px] w-[260px]">
                <p className="font-medium text-text">YOLO mode is an app-wide setting</p>
                <p className="text-muted mt-0.5">All tools will get auto-approved across all sessions.</p>
                <div className="flex items-center gap-2 mt-1.5">
                  {/* Gate fix: commit mc-yolo-ack to localStorage ONLY when the user
                      confirms via Enable — never on the checkbox's onChange, so a
                      check-then-Cancel can't silently disable the confirm. */}
                  <button className="px-2.5 py-1 rounded-md bg-card border border-border text-danger font-medium hover:bg-bg-hover cursor-pointer" onClick={() => { if (yoloDontAsk) safeSetItem('mc-yolo-ack', '1'); dispatch(changeApprovalMode({ mode: 'yolo', slot: slotKey })); setYoloConfirm(0); setApprovalDropdown(false) }}>Enable</button>
                  <button className="px-2.5 py-1 rounded-md text-muted hover:text-text hover:bg-bg-hover cursor-pointer" onClick={(e) => { e.stopPropagation(); setYoloConfirm(0) }}>Cancel</button>
                  <label className="flex items-center gap-1 text-[11px] text-muted cursor-pointer ml-auto">
                    <input type="checkbox" className="rounded" checked={yoloDontAsk} onChange={(e) => setYoloDontAsk(e.target.checked)} />
                    Don't show again
                  </label>
                </div>
              </div>
            )}
          </div>,
          document.body,
        )}
      </div>
    </SlotProvider>
  )
}
