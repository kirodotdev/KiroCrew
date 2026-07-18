import { useEffect, useRef, useCallback } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { useAppDispatch } from '../store'
import { store } from '../store'
import { sseStatus, sseConnected, sseDisconnected, sseSlots, setChannelTrusted, sseSlotTitle, triggerRefresh, fetchSlots, markSlotUnread, setUpdateProgress, sseSubagentStatus, sseSubagentText, touchSlotActivity, type SubagentDetail } from '../store/dashboardSlice'
import { addNotification, ackNotificationByTs, unackNotificationByTs, removeNotificationByTs, fetchNotifications } from '../store/notificationsSlice'
import { fetchHistory, missedChunkMarker, sseChatMessage, sseChatMessageUpdate, sseChatMessagePatchByTs, sseThinkingChunk, refreshSlot, warmSlotCache, sseContextUsage, clearMessages, setVoicePlaying, setVoiceAudio, resolveByApprovalId, sseSubagentPending, sseSubagentSpawn, sseSubagentChunk, sseSubagentTool, sseSubagentDone, sseSubagentSnapshot, sseToolActivity, sseToolResult, sseActivityEvent, sseSideResult, sseWorkflowEvent, setSlotStatusDetail, removeQueuedMessage, appendQueuedMessage, cancelQueuedMessage, editQueuedMessage, appendSlotMessage, setQuestionCard } from '../store/chatSlice'
import { api } from '../api/client'
import { sanitizeLlmOutput } from '../utils/sanitize'
import type { StatusData, ChatSlot, Notification } from '../types'

type LogCallback = ((data: { level: string; msg: string }) => void) | null

/** Single multiplexed WebSocket replacing all SSE + polling connections. */
export function useWebSocket() {
  const dispatch = useAppDispatch()
  const queryClient = useQueryClient()
  const wsRef = useRef<WebSocket | null>(null)
  const closingRef = useRef(false)  // true when cleanup intentionally closes WS
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout>>()  // pending reconnect timer
  const logCbRef = useRef<LogCallback>(null)
  const subagentSubRef = useRef(false)
  const reconnectRef = useRef(1000)
  const wasConnectedRef = useRef(false)
  const reconnectingRef = useRef(false)  // suppress markSlotUnread during reconnect catch-up
  const lastVersionRef = useRef<string | null>(null)
  const voiceQueueRef = useRef<string[]>([])
  const voicePlayingRef = useRef(false)
  const activeAudioRef = useRef<HTMLAudioElement | null>(null)
  const autoSpeakRef = useRef(false)
  const spokenLenRef = useRef(0)  // chars already sent to TTS during streaming
  const voiceMutedRef = useRef(false)  // suppress incoming chunks after interrupt
  const synthChainRef = useRef<Promise<unknown>>(Promise.resolve())  // serialize TTS calls
  // #1 streaming-chunk coalescing: accumulate per-slot chunk text and flush
  // once per animation frame, so the store updates (and the O(N) displayItems /
  // index-map recomputes each dispatch triggers) happen ~per frame instead of
  // ~per token. lastSeq is carried across flushes so cross-batch gap detection
  // mirrors the reducer's per-chunk "N chunk(s) missed" marker.
  const chunkBufRef = useRef<Map<string, { content: string; lastSeq: number | undefined }>>(new Map())
  const chunkFlushScheduledRef = useRef(false)
  const chunkRafRef = useRef<number | null>(null)
  const chunkTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  const stopVoice = useCallback(() => {
    voiceMutedRef.current = true
    if (activeAudioRef.current) {
      activeAudioRef.current.pause()
      activeAudioRef.current = null
    }
    voiceQueueRef.current.forEach(u => URL.revokeObjectURL(u))
    voiceQueueRef.current = []
    voicePlayingRef.current = false
    dispatch(setVoicePlaying(false))
  }, [dispatch])

  const playNextVoiceChunk = useCallback(() => {
    if (voicePlayingRef.current || voiceQueueRef.current.length === 0) return
    voicePlayingRef.current = true
    const url = voiceQueueRef.current.shift()!
    const audio = new Audio(url)
    activeAudioRef.current = audio
    audio.onended = () => {
      URL.revokeObjectURL(url)
      activeAudioRef.current = null
      voicePlayingRef.current = false
      if (voiceQueueRef.current.length > 0) {
        playNextVoiceChunk()
      } else {
        dispatch(setVoicePlaying(false))
      }
    }
    audio.onerror = () => {
      URL.revokeObjectURL(url)
      activeAudioRef.current = null
      voicePlayingRef.current = false
      playNextVoiceChunk()
    }
    audio.play().catch(() => {
      URL.revokeObjectURL(url)
      activeAudioRef.current = null
      voicePlayingRef.current = false
      playNextVoiceChunk()
    })
  }, [dispatch])

  const syncPendingApprovals = useCallback(async () => {
    try {
      const approvals = await api.approvals()
      const existing = store.getState().notifications.items
      for (const a of approvals) {
        if (existing.some((n: Notification) => n.approval_id === a.id)) continue
        dispatch(addNotification({
          kind: 'approval',
          title: `Tool approval: ${a.tool || 'Unknown'}`,
          body: `**Source:** ${a.source || 'agent'}\n\n${a.tool_input || ''}`.trim(),
          ts: String(a.ts || Date.now() / 1000),
          approval_id: a.id,
        } as Notification))
        const slot = a.slot || ''
        if (slot) {
          dispatch(sseChatMessage({
            slot, role: 'permission',
            content: `[${a.source || 'agent'}] ${a.tool || 'Unknown'}`,
            ts: String(a.ts || Date.now() / 1000),
            meta: { tool_input: a.tool_input || '', approval_id: a.id, source: a.source, ...(a.tool_call_id ? { tool_call_id: a.tool_call_id } : {}) },
          }))
        }
      }
    } catch { /* ignore */ }
  }, [dispatch])

  /** Flush all buffered streaming chunks into the store: one batched dispatch
   *  per slot. Runs once per animation frame (see scheduleChunkFlush) and
   *  synchronously before any finalize/segment/message for ordering. */
  const flushChunks = useCallback(() => {
    // Cancel any pending scheduled frame first: when flushChunks is invoked
    // synchronously (finalize/segment/message paths) an earlier scheduleChunkFlush
    // rAF/timer may still be pending; nulling the refs without cancelling would
    // orphan it (uncancellable by unmount/reconnect cleanup, fires a stale flush).
    // From the rAF callback itself the id has already fired, so cancel is a no-op.
    if (chunkRafRef.current != null && typeof cancelAnimationFrame === 'function') cancelAnimationFrame(chunkRafRef.current)
    if (chunkTimerRef.current != null) clearTimeout(chunkTimerRef.current)
    chunkFlushScheduledRef.current = false
    chunkRafRef.current = null
    chunkTimerRef.current = null
    const buf = chunkBufRef.current
    const activeSlot = store.getState().chat.activeSlot
    let dispatchedActive = false
    for (const [slot, entry] of buf) {
      if (!entry.content) continue
      dispatch(sseChatMessage({ slot, role: 'chunk', content: entry.content, seq: entry.lastSeq, batched: true }))
      entry.content = ''
      if (slot === activeSlot) dispatchedActive = true
    }
    // Auto-speak the active slot's newly-streamed sentences once per flush,
    // after the batched content has landed in the store. (Moved here from the
    // per-chunk path so it reads the post-dispatch streaming content.)
    if (dispatchedActive && autoSpeakRef.current && activeSlot) {
      if (spokenLenRef.current === 0) voiceMutedRef.current = false
      const msgs = store.getState().chat.messages
      const streaming = [...msgs].reverse().find(m => m.role === 'streaming')
      if (streaming) {
        const full = streaming.content
        let lastBound = -1
        const re = /[.!?](?:\s|$)/g
        let match
        while ((match = re.exec(full)) !== null) {
          if (match.index + 1 > spokenLenRef.current) lastBound = match.index + 1
        }
        if (lastBound > spokenLenRef.current) {
          const newText = full.slice(spokenLenRef.current, lastBound).trim()
          if (newText.length >= 10) {
            spokenLenRef.current = lastBound
            synthChainRef.current = synthChainRef.current
              .then(() => api.voiceSynthesize(activeSlot, newText))
              .catch(() => {})
          }
        }
      }
    }
  }, [dispatch])

  const scheduleChunkFlush = useCallback(() => {
    if (chunkFlushScheduledRef.current) return
    chunkFlushScheduledRef.current = true
    if (typeof requestAnimationFrame === 'function') chunkRafRef.current = requestAnimationFrame(() => flushChunks())
    else chunkTimerRef.current = setTimeout(() => flushChunks(), 16)
  }, [flushChunks])

  const connect = useCallback(() => {
    // Guard against double-connect in StrictMode (dev) — if we already
    // have a WS that's open OR still connecting, reuse it.
    const existing = wsRef.current
    if (existing && (existing.readyState === WebSocket.OPEN || existing.readyState === WebSocket.CONNECTING)) return
    if (closingRef.current) return  // component unmounted, don't reconnect
    // closingRef invariant: reset by useEffect before calling connect()
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
    const ws = new WebSocket(`${proto}//${location.host}/api/ws`)
    wsRef.current = ws

    ws.onopen = () => {
      reconnectRef.current = 1000
      // Cache auto-speak preference
      api.voiceConfig().then(c => { autoSpeakRef.current = !!c.autoSpeak }).catch(() => {})
      if (wasConnectedRef.current) {
        // Reconnecting after disconnect — re-fetch state instead of
        // reloading the page.  Preserves unsent messages, scroll
        // position, and form inputs.
        // Suppress markSlotUnread during the post-reconnect catch-up burst.
        // Assumption: the WS replay backlog flushes faster than the fetchSlots
        // HTTP round-trip resolves (gateway pushes buffered events in ms; HTTP
        // response takes tens of ms). If a very large backlog outlasts the
        // round-trip, late catch-up events could still mark slots unread — an
        // acceptable edge case vs. the common-case fix. A server-sent "replay
        // done" marker would make this deterministic but requires gateway changes.
        // Deliberate tradeoff: genuine unreads arriving mid-window are also
        // suppressed (false-negative-over-false-positive for the "just reconnected,
        // user is looking at the screen" scenario).
        reconnectingRef.current = true
        // Cancel any in-flight flush before dropping the buffer, so a chunk
        // arriving right after reconnect can't race a stale scheduled frame
        // into a second concurrent flush (mirrors the unmount cleanup).
        if (chunkRafRef.current != null && typeof cancelAnimationFrame === 'function') cancelAnimationFrame(chunkRafRef.current)
        if (chunkTimerRef.current != null) clearTimeout(chunkTimerRef.current)
        chunkRafRef.current = null
        chunkTimerRef.current = null
        chunkFlushScheduledRef.current = false
        chunkBufRef.current.clear()  // drop pre-disconnect partial chunks; refreshSlot recovers state
        dispatch(sseConnected())
        dispatch(fetchSlots()).finally(() => { reconnectingRef.current = false })
        dispatch(fetchNotifications()).then(() => syncPendingApprovals())
        // Re-fetch active slot messages to recover from missed chunks
        const active = store.getState().chat.activeSlot
        if (active) dispatch(refreshSlot(active))
        // Eagerly subscribe to subagent events so chunks arrive even when
        // Activity Panel isn't open — final result still comes via done event.
        ws.send(JSON.stringify({ type: 'subscribe_subagents' }))
        subagentSubRef.current = true
        if (logCbRef.current) ws.send(JSON.stringify({ type: 'subscribe_logs' }))
        return
      }
      wasConnectedRef.current = true
      dispatch(sseConnected())
      dispatch(fetchSlots())
      dispatch(fetchNotifications()).then(() => syncPendingApprovals())
      // Eagerly subscribe to subagent events on first connect too.
      ws.send(JSON.stringify({ type: 'subscribe_subagents' }))
      subagentSubRef.current = true
    }

    ws.onmessage = (e) => {
      try {
        const msg = JSON.parse(e.data)
        const { type, data } = msg
        switch (type) {
          case 'dashboard': {
            // Detect server version change → full reload (actual update)
            const prev = lastVersionRef.current
            const next = (data as StatusData).version
            if (next) lastVersionRef.current = next
            if (prev && next && prev !== next) {
              window.location.reload()
              return
            }
            dispatch(sseStatus(data as StatusData))
            break
          }
          case 'slots': {
            dispatch(sseSlots(data as ChatSlot[]))
            if (msg.yolo !== undefined) {
              dispatch(sseStatus({ yolo: msg.yolo } as StatusData))
            }
            if (msg.channelTrusted !== undefined) {
              dispatch(setChannelTrusted(msg.channelTrusted))
            }
            break
          }
          case 'slot_title':
            dispatch(sseSlotTitle(data as { key: string; title: string }))
            break
          case 'notification':
            dispatch(addNotification(data as Notification))
            break
          case 'notification_ack':
            dispatch(ackNotificationByTs(data.ts))
            break
          case 'notification_unack':
            dispatch(unackNotificationByTs(data.ts))
            break
          case 'approval': {
            queryClient.invalidateQueries({ queryKey: ['global-approvals'] })
            // Browser notification when tab not focused (permission must be granted via UI interaction elsewhere)
            if (typeof Notification !== 'undefined' && document.hidden && Notification.permission === 'granted') {
              new Notification('Approval Required', { body: data.tool || 'A task needs your decision', tag: 'kirocrew-approval' })
            }
            dispatch(addNotification({
              kind: 'approval',
              title: `Tool approval: ${data.tool || 'Unknown'}`,
              body: `**Source:** ${data.source || 'agent'}\n\n${data.tool_input || ''}\n\n${data.tool_purpose || ''}`.trim(),
              ts: String(data.ts || Date.now() / 1000),
              approval_id: data.id,
            } as Notification))
            // Also inject inline in active chat
            const targetSlot = data.slot || store.getState().chat.activeSlot || ''
            if (targetSlot) {
              dispatch(sseChatMessage({
                slot: targetSlot,
                role: 'permission',
                content: `[${data.source || 'agent'}] ${data.tool || 'Unknown'}`,
                ts: String(data.ts || Date.now() / 1000),
                meta: { tool_input: data.tool_input || '', approval_id: data.id, source: data.source, ...(data.tool_call_id ? { tool_call_id: data.tool_call_id } : {}) },
              }))
              // For spawn approvals, create a pending subagent entry instead of a toolLog approval
              const rid = data.id as string
              if (rid?.startsWith('spawn:')) {
                const agentId = rid.replace('spawn:', '')
                dispatch(sseSubagentPending({ slot: targetSlot, id: agentId, task: (data.tool as string || '').replace('spawn_run(', '').replace(/\)$/, ''), approval_id: rid }))
              } else if (data.source !== 'subagent') {
                dispatch(sseActivityEvent({ slot: targetSlot, kind: 'approval', text: data.tool || 'Unknown', approval_id: data.id, approval_type: 'chat' }))
              }
            }
            break
          }
          case 'approval_resolved': {
            queryClient.invalidateQueries({ queryKey: ['global-approvals'] })
            const items = store.getState().notifications.items
            const match = items.find((n: Notification) => n.approval_id === data.id)
            if (match) dispatch(removeNotificationByTs(match.ts))
            dispatch(resolveByApprovalId({ id: data.id, decision: data.approved ? 'approved' : 'rejected' }))
            const targetSlot = data.slot || store.getState().chat.activeSlot || ''
            const resolvedType = typeof data.id === 'string' && data.id.startsWith('spawn:') ? 'spawn' : 'chat'
            if (targetSlot) {
              const chatState = store.getState().chat
              const log = targetSlot === chatState.activeSlot
                ? chatState.toolLog
                : chatState.slotActivity[targetSlot]?.toolLog ?? []
              const hasMatchingApproval = log.some(e => e.approval_id === data.id && e.type === 'approval')
              if (hasMatchingApproval || resolvedType === 'spawn') {
                dispatch(sseActivityEvent({ slot: targetSlot, kind: 'approval_resolved', text: '', approval_id: data.id, approval_type: resolvedType }))
              }
              if (typeof data.id === 'string' && data.id.startsWith('spawn:')) {
                const agentId = data.id.replace('spawn:', '')
                if (data.approved) {
                  dispatch(sseSubagentSpawn({ slot: targetSlot, id: agentId, task: '', agent: '' }))
                } else {
                  dispatch(sseSubagentDone({ slot: targetSlot, id: agentId, elapsed: 0, error: 'rejected' }))
                }
              }
            }
            break
          }
          case 'refresh': {
            const kinds: string[] = data.kinds || []
            dispatch(triggerRefresh())
            if (kinds.includes('history')) dispatch(fetchHistory(false))
            break
          }
          case 'slot_clear': {
            // /clear command — clear messages in active slot; backend already appended confirmation
            const clearSlot = data.slot as string
            if (clearSlot === store.getState().chat.activeSlot) dispatch(clearMessages())
            break
          }
          case 'slot_agent_switch': {
            // /agent command — refresh slot metadata to pick up new agent label
            dispatch(fetchSlots())
            break
          }
          case 'chat_message':
            flushChunks()
            dispatch(sseChatMessage(data))
            // Re-rank the sidebar recency tint the instant a session sees any message —
            // user sends as well as agent output (assistant/tool) — matching last_ts (last
            // message of any role), instead of waiting for the next full slots push. Fallback
            // ts is computed here so the touchSlotActivity reducer stays pure (Redux contract).
            if (data.slot && (data.role === 'user' || data.role === 'assistant' || data.role === 'tool_call' || data.role === 'tool_result')) {
              dispatch(touchSlotActivity({ key: data.slot, ts: data.ts || new Date().toISOString() }))
            }
            if (data.slot && data.slot !== store.getState().chat.activeSlot && !reconnectingRef.current) dispatch(markSlotUnread(data.slot))
            if (data.role === 'user' || data.role === 'inject' || data.role === 'subagent') { stopVoice(); spokenLenRef.current = 0; synthChainRef.current = Promise.resolve() }
            if (data.slot && (data.role === 'user' || data.role === 'inject' || data.role === 'subagent')) {
              dispatch(setSlotStatusDetail({ slot: data.slot, kind: 'thinking', text: 'Thinking…', ts: Date.now() }))
            }
            break
          case 'chat_message_update':
            // Server emits this for two distinct flows: tool_call_id-keyed
            // updates from claude-agent-acp tool_call_update, and ts-keyed
            // patches for mcp_oauth banner state flips. Route by which key
            // the payload carries.
            if ((data as { tool_call_id?: string }).tool_call_id) {
              dispatch(sseChatMessageUpdate(data as { slot: string; tool_call_id: string; content?: string; meta?: Record<string, unknown> }))
            } else {
              dispatch(sseChatMessagePatchByTs(data as { slot: string; ts: string; meta?: Record<string, unknown>; content?: string }))
            }
            break
          case 'queue_pop':
            dispatch(removeQueuedMessage(data))
            break
          case 'queue_push':
            dispatch(appendQueuedMessage(data))
            break
          case 'steer_push':
            // Mid-turn steer echo: show the user's steered text inline in the
            // target slot's transcript. Uses appendSlotMessage so the bubble
            // appears whether or not the slot is currently active (background
            // tabs). Persisted server-side — survives page reload.
            dispatch(appendSlotMessage({
              slot: (data as { slot?: string }).slot || store.getState().chat.activeSlot || '',
              message: { role: 'user', content: (data as { content?: string }).content || '', cls: 'msg msg-u', meta: { steer: true }, ts: (data as { ts?: string }).ts },
            }))
            break
          case 'queue_cancel':
            dispatch(cancelQueuedMessage(data))
            break
          case 'queue_edit':
            dispatch(editQueuedMessage(data))
            break
          case 'chat_chunk': {
            // #1: buffer the chunk and flush once per frame (see flushChunks),
            // instead of dispatching — and recomputing the O(N) displayItems /
            // index maps — on every token.
            const cs = data.slot
            if (cs) {
              const buf = chunkBufRef.current
              let entry = buf.get(cs)
              if (!entry) { entry = { content: '', lastSeq: undefined }; buf.set(cs, entry) }
              // Cross-chunk gap detection via the shared missedChunkMarker,
              // single-sourced with the reducer so the two copies can't drift.
              if (entry.lastSeq !== undefined && data.seq !== undefined) {
                entry.content += missedChunkMarker(entry.lastSeq, data.seq)
              }
              entry.content += data.content ?? ''
              if (data.seq !== undefined) entry.lastSeq = data.seq
              if (store.getState().chat.slotStatusDetail[cs]?.kind !== 'streaming') {
                dispatch(setSlotStatusDetail({ slot: cs, kind: 'streaming', text: 'Streaming', ts: Date.now() }))
              }
              scheduleChunkFlush()
            }
            break
          }
          case 'tool_call':
            dispatch(sseToolActivity({ ...data as { slot: string; tool: string; kind: string; purpose: string; input_preview: string }, auto: (data as Record<string, unknown>).auto === true, tool_call_id: (data as Record<string, unknown>).tool_call_id as string | undefined, is_update: (data as Record<string, unknown>).is_update === true }))
            if (data.slot) {
              dispatch(setSlotStatusDetail({ slot: data.slot, kind: 'tool', text: sanitizeLlmOutput((data as Record<string, unknown>).purpose as string || data.tool), toolName: sanitizeLlmOutput(data.tool), ts: Date.now() }))
            }
            // Note: do NOT dispatch sseChatMessage here. The backend persists the
            // tool message via slot.append and broadcasts it as 'chat_message'.
            // Dispatching here would insert a duplicate entry in the message list.
            break
          case 'tool_result':
            dispatch(sseToolResult(data as { slot: string; output: string; tool_call_id?: string }))
            break
          case 'question_card':
            dispatch(setQuestionCard(data as Parameters<typeof setQuestionCard>[0]))
            break
          case 'activity_event':
            dispatch(sseActivityEvent(data as { slot: string; kind: string; text: string }))
            break
          case 'subagent_spawn':
            dispatch(sseSubagentSpawn(data as { slot: string; id: string; task: string; agent: string }))
            break
          case 'subagent_chunk':
            dispatch(sseSubagentChunk(data as { slot: string; id: string; text: string }))
            break
          case 'subagent_tool':
            dispatch(sseSubagentTool(data as { slot: string; id: string; tool: string }))
            break
          case 'subagent_done':
            dispatch(sseSubagentDone(data as { slot: string; id: string; elapsed: number; error?: string }))
            break
          case 'subagent_snapshot':
            dispatch(sseSubagentSnapshot(data as { id: string; slot: string; task: string; agent: string; streaming: string; last_tool: string; started: number }))
            break
          case 'workflow_run_event':
            // Dynamic-workflow run events folded into chat.workflowRuns and
            // surfaced by WorkflowProgressBar above the chat input.
            dispatch(sseWorkflowEvent(data as { run_id: string; seq?: number; ts?: number; type: string; data?: Record<string, unknown> }))
            break
          case 'chat.side_result':
            dispatch(sseSideResult(data as { slot: string; run_id: string; role: 'user' | 'assistant'; content: string; ts?: number; final?: boolean; is_error?: boolean }))
            break
          case 'heartbeat':
            // No-op: SessionStatus already ticks elapsed via setInterval.
            // Dispatching here would reset ts and break slow-warning detection.
            break
          case 'context_usage':
            dispatch(sseContextUsage(data as { slot: string; pct: number; used_tokens?: number; window_tokens?: number }))
            break
          case 'chat_thinking':
            // kiro-cli/ACP reasoning (agent_thought_chunk) -> collapsible block.
            dispatch(sseThinkingChunk({ slot: data.slot, content: (data as { content?: string }).content || '' }))
            if (data.slot && store.getState().chat.slotStatusDetail[data.slot]?.kind !== 'streaming') {
              dispatch(setSlotStatusDetail({ slot: data.slot, kind: 'thinking', text: 'Thinking…', ts: Date.now() }))
            }
            break
          case 'chat_segment':
            flushChunks()
            dispatch(sseChatMessage({ ...data, role: '_segment' }))
            spokenLenRef.current = 0
            break
          case 'chat_status':
            if (data.slot && data.status) {
              dispatch(setSlotStatusDetail({ slot: data.slot, kind: 'thinking', text: data.status, ts: Date.now() }))
            }
            break
          case 'chat_variant_switch':
            if (data.slot) dispatch(refreshSlot(data.slot))
            break
          case 'chat_done':
            flushChunks()
            if (data.slot) chunkBufRef.current.delete(data.slot)
            dispatch(sseChatMessage({ ...data, role: '_done' }))
            if (data.slot && data.slot !== store.getState().chat.activeSlot && !reconnectingRef.current) {
              dispatch(markSlotUnread(data.slot))
              // #2: warm the per-slot cache so switching to this background
              // session renders the finished answer instantly (no on-switch fetch).
              dispatch(warmSlotCache(data.slot))
            }
            if (data.slot) {
              dispatch(setSlotStatusDetail({ slot: data.slot, kind: 'idle', text: 'Ready', ts: Date.now() }))
            }
            if (data.slot) dispatch(refreshSlot(data.slot))
            // Auto-speak: speak any remaining unspoken text from streaming
            if (autoSpeakRef.current && !voiceMutedRef.current && data.slot === store.getState().chat.activeSlot) {
              const msgs = store.getState().chat.messages
              const last = [...msgs].reverse().find(m => m.role === 'assistant')
              if (last) {
                const remaining = last.content.slice(spokenLenRef.current).trim()
                if (remaining.length >= 10) {
                  synthChainRef.current = synthChainRef.current
                    .then(() => api.voiceSynthesize(data.slot, remaining))
                    .catch(() => {})
                }
              }
              spokenLenRef.current = 0
            } else if (data.slot === store.getState().chat.activeSlot) {
              // Re-check config in case it changed
              api.voiceConfig().then(c => { autoSpeakRef.current = !!c.autoSpeak }).catch(() => {})
            }
            break
          case 'autonudge_state':
            // Broadcast for ChatPage to refresh its autonudge loop state.
            window.dispatchEvent(new CustomEvent('autonudge_state', { detail: data }))
            break
          case 'voice_chunk': {
            if (voiceMutedRef.current) break
            if (data.slot !== store.getState().chat.activeSlot) break
            // Queue and play audio chunks as they arrive
            const b64 = (data as { audio: string }).audio
            if (b64) {
              try {
                const bytes = Uint8Array.from(atob(b64), c => c.charCodeAt(0))
                const blob = new Blob([bytes], { type: 'audio/mpeg' })
                const url = URL.createObjectURL(blob)
                voiceQueueRef.current.push(url)
                dispatch(setVoicePlaying(true))
                playNextVoiceChunk()
              } catch { /* malformed base64 */ }
            }
            break
          }
          case 'voice_complete': {
            const b64 = (data as { audio: string }).audio
            if (b64) dispatch(setVoiceAudio(b64))
            break
          }
          case 'log':
            logCbRef.current?.(data)
            break
          case 'sessions_restarting':
            // Backend pushed session restart status (restarting/ready)
            dispatch(triggerRefresh())
            break
          case 'update_progress': {
            const prog = data as { step: string; detail: string }
            if (prog.step === 'done') {
              dispatch(setUpdateProgress(null))
            } else {
              dispatch(setUpdateProgress(prog))
            }
            break
          }
          case 'subagent_status':
            if (data.slot) dispatch(sseSubagentStatus(data as { running: number; slot: string; agents?: SubagentDetail[] }))
            break
          case 'subagent_text':
            if (data.slot && data.id) dispatch(sseSubagentText(data as { slot: string; id: string; text: string }))
            break
          case 'refine':
            // Handled by ProjectsPage via Redux
            dispatch(triggerRefresh())
            break
          case 'channel_message':
          case 'channel_agent_status':
          case 'channel_created':
          case 'channel_closed':
          case 'channel_agent_joined':
          case 'channel_agent_left':
            window.dispatchEvent(new CustomEvent('kirocrew-channel', { detail: { type, data } }))
            break
          case 'cron_history':
            window.dispatchEvent(new CustomEvent('cron_history', { detail: data }))
            queryClient.invalidateQueries({ queryKey: ['cron-history'] })
            queryClient.invalidateQueries({ queryKey: ['cron-history-all'] })
            break
          case 'browser_frame':
            // Live mirror frame (a screenshot the agent took, forwarded by the
            // MCP proxy). Routed via a window event so BrowserLiveView can render
            // without a Redux slice.
            window.dispatchEvent(new CustomEvent('kirocrew-browser-frame', { detail: data }))
            break
        }
      } catch { /* ignore malformed */ }
    }

    ws.onclose = () => {
      // Stale WS (e.g. from StrictMode cleanup) — ignore entirely.
      if (wsRef.current !== ws) return

      dispatch(sseDisconnected())
      wsRef.current = null

      if (closingRef.current) return
      const delay = reconnectRef.current
      reconnectRef.current = Math.min(delay * 2, 10000)
      reconnectTimerRef.current = setTimeout(connect, delay)
    }

    ws.onerror = () => { /* onclose will fire */ }
  }, [dispatch, flushChunks, scheduleChunkFlush, playNextVoiceChunk, queryClient, stopVoice, syncPendingApprovals])

  /**
   * Force an immediate reconnect: cancels any pending backoff timer, closes
   * the existing WS (if any), resets the backoff window, and calls connect().
   *
   * Used by `useDashboardHealthProbe` when its periodic /api/status poll
   * succeeds while the dashboard is in `connected: false` state — that's the
   * signal that the gateway came back up. Without this, the next reconnect
   * attempt could be up to 10s away (capped exponential backoff in onclose).
   */
  const forceReconnect = useCallback(() => {
    if (closingRef.current) return
    clearTimeout(reconnectTimerRef.current)
    reconnectRef.current = 1000  // reset backoff window
    const ws = wsRef.current
    if (ws && ws.readyState !== WebSocket.CLOSED) {
      // Detach handlers BEFORE close() so the onclose handler doesn't fire
      // asynchronously and schedule a redundant reconnect on top of our 0ms
      // timer below — that race would briefly create two parallel WebSocket
      // connections. The existing onclose guard (wsRef.current !== ws) also
      // catches this, but explicit detach is cleaner and removes the
      // dispatch(sseDisconnected()) we don't want during a force-reconnect
      // (we're already in connected:false state and forcing a reconnect
      // because the probe just confirmed the gateway is back).
      ws.onclose = null
      ws.onerror = null
      try { ws.close() } catch { /* ignore */ }
    }
    wsRef.current = null
    reconnectTimerRef.current = setTimeout(connect, 0)
  }, [connect])

  useEffect(() => {
    closingRef.current = false  // reset for StrictMode re-mount
    connect()
    const onVoiceStop = () => stopVoice()
    const onVoiceConfigChanged = (e: Event) => {
      const detail = (e as CustomEvent).detail
      autoSpeakRef.current = !!detail?.autoSpeak
    }
    window.addEventListener('voice-stop', onVoiceStop)
    window.addEventListener('voice-config-changed', onVoiceConfigChanged)
    return () => {
      closingRef.current = true
      clearTimeout(reconnectTimerRef.current)
      if (chunkRafRef.current != null && typeof cancelAnimationFrame === 'function') cancelAnimationFrame(chunkRafRef.current)
      if (chunkTimerRef.current != null) clearTimeout(chunkTimerRef.current)
      wsRef.current?.close()
      wsRef.current = null
      window.removeEventListener('voice-stop', onVoiceStop)
      window.removeEventListener('voice-config-changed', onVoiceConfigChanged)
    }
  }, [connect, stopVoice])

  /** Subscribe to log events — call with callback on mount, null on unmount. */
  const subscribeLogs = useCallback((cb: LogCallback) => {
    logCbRef.current = cb
    const ws = wsRef.current
    if (!ws || ws.readyState !== WebSocket.OPEN) return
    if (cb) {
      ws.send(JSON.stringify({ type: 'subscribe_logs' }))
    } else {
      ws.send(JSON.stringify({ type: 'unsubscribe_logs' }))
    }
  }, [])

  const subscribeSubagents = useCallback((subscribe: boolean) => {
    subagentSubRef.current = subscribe
    const ws = wsRef.current
    if (!ws || ws.readyState !== WebSocket.OPEN) return
    ws.send(JSON.stringify({ type: subscribe ? 'subscribe_subagents' : 'unsubscribe_subagents' }))
  }, [])

  return { subscribeLogs, subscribeSubagents, forceReconnect }
}
