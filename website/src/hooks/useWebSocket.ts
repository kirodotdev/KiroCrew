import { useEffect, useRef, useCallback } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { useAppDispatch } from '../store'
import { sseTodoUpdate, sseMcpReportUpdate, sseSlotTitle, triggerRefresh, fetchSlots, remoteSlotRead, sseSubagentStatus, sseSubagentText, type SubagentDetail } from '../store/dashboardSlice'
import { addNotification, ackNotificationByTs, unackNotificationByTs, clearAllNotifications } from '../store/notificationsSlice'
import { dispatchMcNotification, dispatchLiveNotification } from './notificationEvent'
import {
  fetchHistory, sseChatMessage, sseChatMessageUpdate, sseChatMessagePatchByTs, refreshSlot, sseContextUsage, clearMessages, clearSlotCache, sseSubagentPending, sseSubagentSpawn, sseSubagentQueued, sseSubagentTool, sseSubagentStalled, sseSubagentRetrying, sseSubagentDone, sseSubagentSnapshot, sseSubagentBatchUpdate, sseSubagentBatchChunks, sseToolResult, sseActivityEvent, sseSideResult, sseWorkflowEvent, setSlotStatusDetail, removeQueuedMessage, appendQueuedMessage, cancelQueuedMessage, editQueuedMessage, reorderQueuedMessages, sseMcpAppRender, sseSideQueue, queueEntryAttachments, isTerminalWorkflowStatus,
} from '../store/chatSlice'
import { store } from '../store'
import { TAB_ID } from '../api/tabId'
import type { Notification, TodoList, McpSessionReport } from '../types'
import { i18nT } from '../i18n/t'
import { teamRoots } from '../pages/chat/command-center/model'
import { useSocketConnection } from './websocket/connection'
import { decodeFrame } from './websocket/frames'
import { useStreamBuffers } from './websocket/streamBuffers'
import { useVoicePlayback } from './websocket/voicePlayback'
import { useApprovalRegistry } from './websocket/approvals'
import { useComposerCards } from './websocket/composerCards'
import { useAutomationSeed } from './websocket/automationSeed'
import { useWorkflowRunReconcile } from './websocket/workflowRuns'
import { useSlotListSync } from './websocket/slotList'
import { useBundleReload, handleUpdateProgress } from './websocket/bundleReload'
import {
  handleArtifactUpdate,
  handleCredentialRedactionChanged,
  handleMemberProjection,
  handleMembersSubscribed,
  handleSourceStatus,
  handleThreadReply,
  invalidateRefreshQueries,
} from './websocket/serverState'
import { useChatStream } from './websocket/chatStream'
import { useTurnCompletion } from './websocket/turnCompletion'
import { attachFocusRelay } from './websocket/attention'
import { emitAppReload, emitChannelEvent, emitComputerUseFrame, emitCronHistory } from './websocket/browserEvents'
import { runFirstConnect, runReconnectCatchUp } from './websocket/reconnectCatchUp'

/* The dashboard's single multiplexed WebSocket. `useWebSocket` composes the
   owners under ./websocket — connection, stream buffers, voice, approvals,
   composer cards, automations, workflow runs, the slot list, status and
   update reloads, server-state caches, the transcript stream, turn
   completion, attention and browser events — and keeps the wiring between
   them: the connect handlers, the frame routing table, the mount and unmount
   order, the transcript rows the router synthesizes (the `approval`,
   `chat_segment` and `chat_done` arms), and the silence watchdog, whose
   constants and thaw grace test/test_ws_status_cadence_contract.py reads
   from this file. Every export below keeps its original import path. */
export { __resetRedactionHealForTests, healRedactionSwitchAfterReconnect } from './websocket/serverState'
export { identityOf, askIdsOf, reconcileQuestions, staleAskIds } from './websocket/composerCards'
export { resolvedSince } from './websocket/retiredIds'
export { UPDATE_RESTART_LATCH_KEY, UPDATE_RESTART_LATCH_TTL_MS, consumeUpdateRestartLatch } from './websocket/bundleReload'
export { emitSlotFocused } from './websocket/attention'

/** A socket that has delivered nothing for this long while the page is visible
 *  is treated as dead, even when its `readyState` still reads OPEN. The gateway
 *  pushes a `dashboard` status frame on every socket every 5s
 *  (`_WS_STATUS_INTERVAL` in `dashboard/ws.py`), so this is four missed ticks. */
export const WS_SILENCE_MS = 20_000
/** How often the silence check runs: the status frame's own cadence. */
export const WS_SILENCE_CHECK_MS = 5_000
/** Ceiling for the silence window after repeated silent reconnects. */
export const WS_SILENCE_MAX_MS = 300_000

/** Single multiplexed WebSocket replacing all SSE + polling connections. */
export function useWebSocket() {
  const dispatch = useAppDispatch()
  const queryClient = useQueryClient()
  const socket = useSocketConnection()
  const { reconnectingRef } = socket
  // Silence watchdog state (see WS_SILENCE_MS), kept in visible time: when the
  // current socket last delivered a frame, when its silence clock started (at
  // open), when the page was hidden (0 while visible), until when a socket
  // thawed by a return is spared, and how many silent sockets in a row the
  // watchdog has replaced.
  const lastFrameAtRef = useRef(0)
  const silenceClockStartedAtRef = useRef(0)
  const hiddenAtRef = useRef(0)
  const graceUntilRef = useRef(0)
  const silentReconnectsRef = useRef(0)
  const voice = useVoicePlayback(dispatch)
  const automations = useAutomationSeed(dispatch, queryClient)
  const approvals = useApprovalRegistry(dispatch, queryClient)
  const cards = useComposerCards(dispatch)
  const syncWorkflowRuns = useWorkflowRunReconcile(dispatch, queryClient)
  const buffers = useStreamBuffers({ dispatch, socket, onActiveSlotFlushed: voice.speakStreamedDelta })
  const slotList = useSlotListSync(dispatch, queryClient)
  const bundle = useBundleReload(dispatch)
  const chatStream = useChatStream({ dispatch, buffers, voice, reconnectingRef })
  const turnCompletion = useTurnCompletion({ dispatch, queryClient, reconnectingRef })

  /** Reconcile coordinator approvals against the authority on every open.
   *  The registry decides what to retire and adopt; the permission row an
   *  adopted approval raises in its chat is written here, beside the live
   *  `approval` arm's copy of the same row. Both rows carry the untranslated
   *  `[source] tool` fallback, so they stay put: moved, the line would be new
   *  untranslated copy, and translating it would change the row every
   *  non-English locale shows. */
  const syncPendingApprovals = useCallback(async () => {
    try {
      await approvals.reconcilePending((a, slot) => {
        if (slot) {
          dispatch(sseChatMessage({
            slot, role: 'permission',
            content: `[${a.source || 'agent'}] ${a.tool || 'Unknown'}`,
            ts: String(a.ts || Date.now() / 1000),
            meta: { tool_input: a.tool_input || '', approval_id: a.id, source: a.source, registry: 'coordinator', ...(a.tool_call_id ? { tool_call_id: a.tool_call_id } : {}) },
          }))
        }
      })
    } catch { /* ignore */ }
  }, [dispatch, approvals])

  const connect = useCallback(() => {
    const ws = socket.open()
    if (!ws) return

    ws.onopen = () => {
      socket.resetBackoff()
      silenceClockStartedAtRef.current = Date.now()
      lastFrameAtRef.current = silenceClockStartedAtRef.current
      slotList.resetForConnection()
      // Cache auto-speak preference
      voice.refreshAutoSpeak()
      const catchUp = {
        dispatch,
        queryClient,
        socket,
        buffers,
        seedAutomations: automations.seedAutomations,
        syncPendingApprovals,
        syncPendingQuestions: cards.syncPendingQuestions,
        syncWorkflowRuns,
      }
      if (socket.wasConnectedRef.current) {
        runReconnectCatchUp(ws, catchUp)
        return
      }
      runFirstConnect(ws, catchUp)
    }

    // The routing table: one arm per frame type. An arm whose frame family
    // has an owner calls it and hands it exactly what the frame needs, so no
    // handler context is shared; the other arms are written inline. One
    // try/catch covers the table: a throw ends that frame's effects and the
    // next frame routes normally.
    ws.onmessage = (e) => {
      if (socket.wsRef.current === ws) lastFrameAtRef.current = Date.now()
      try {
        const { type, data, msg } = decodeFrame(e.data)
        switch (type) {
          case 'dashboard':
            bundle.onDashboardStatus(data)
            break
          case 'slots':
            slotList.onSlots(msg, data, e.data as string)
            break
          case 'credential_redaction_changed':
            handleCredentialRedactionChanged(queryClient, msg.data as { enabled?: unknown; changed_at?: unknown } | undefined)
            break
          case 'skills.pending_changed': {
            // A skill candidate (new or an update proposal) was just staged for
            // review. Refresh the pending queue so an already-open Skills tab
            // shows it without a reload; ['skills'] is invalidated too because
            // the panel's visibility depends on the pending count.
            queryClient.invalidateQueries({ queryKey: ['skills-pending'] })
            queryClient.invalidateQueries({ queryKey: ['skills'] })
            break
          }
          case 'todo_update': {
            const d = data as unknown as { slot?: string; todo?: TodoList | null }
            if (d.slot) dispatch(sseTodoUpdate({ slot: d.slot, todo: d.todo ?? null }))
            break
          }
          case 'mcp_report_update': {
            // A null report is a real value here, not a missing one: the gateway
            // sends it when a session reset invalidates the previous report.
            const d = data as unknown as { slot?: string; mcp_report?: McpSessionReport | null }
            if (d.slot) {
              dispatch(sseMcpReportUpdate({ slot: d.slot, mcp_report: d.mcp_report ?? null }))
            }
            break
          }
          case 'slot_title':
            dispatch(sseSlotTitle(data as { key: string; title: string }))
            break
          case 'slot_patch':
            slotList.onSlotPatch(data)
            break
          case 'dashboard_card': {
            const { slot, removed } = data as { slot?: string; removed?: boolean }
            if (slot) {
              // Invalidating alone leaves stale content available on remount.
              // Ordinary updates retain last-good content; removals must not.
              if (removed) queryClient.resetQueries({ queryKey: ['dashboard-card', slot] })
              else queryClient.invalidateQueries({ queryKey: ['dashboard-card', slot] })
            }
            break
          }
          case 'session_summary': {
            // A turn finished and the backend regenerated this session's intent
            // summary. Invalidate so the panel picks it up immediately.
            //
            // This event is why the summary panel does not poll: the summary is
            // deliberately a pull-friendly artifact — a panel on an interval
            // would reward refreshing, which is the checking loop the feature
            // exists to remove. Push-on-change gives freshness without it.
            const key = (data as { key?: string }).key
            if (key) {
              queryClient.invalidateQueries({ queryKey: ['session-summary', key] })
            }
            break
          }
          case 'pins_changed': {
            // A pin was created or deleted on another tab (or via the API).
            // Invalidate only the affected slot's cache so the pin affordance
            // and pin list stay in sync without a remount. The payload carries
            // slot_key only — no pin content — so nothing sensitive crosses the
            // WebSocket to any listener.
            const slotKey = (data as { slot_key?: string }).slot_key
            if (slotKey) {
              queryClient.invalidateQueries({ queryKey: ['chat-pins', slotKey] })
            }
            break
          }
          case 'artifact_update':
            handleArtifactUpdate(queryClient, data)
            break
          case 'notification': {
            const n = data as Notification
            dispatch(addNotification(n))
            // Also fire MC_NOTIFICATION_EVENT so useNotificationSound plays the
            // configured sound — the Redux action alone only drives the
            // toast/badge, not the sound.
            // RFC Phase 3: muted-channel (silenced) and passive notes are
            // feed-only — no sound.
            if (!n.silenced && n.priority !== 'passive') {
              dispatchMcNotification(n.kind)
            }
            // The in-app banner hears LIVE arrivals only. A reconnect catch-up
            // replays every frame missed while the socket was down, and those
            // notes are already in the bell (the reconnect refetch lands them);
            // bannering them would re-announce history as news — the same
            // suppression the turn-done chime applies via `reconnectingRef`.
            // The boot snapshot never reaches here at all (it arrives through
            // `fetchNotifications`, not this frame), so mount replay is
            // excluded by construction.
            if (!reconnectingRef.current) dispatchLiveNotification(n)
            break
          }
          case 'panel_published': {
            // A crew replaced its webview. The drawer's query sets no finite
            // staleTime (the client's default is Infinity, freshness by push), so
            // without this the operator kept looking at the first snapshot read
            // when the drawer opened -- and the "23m ago" chip froze with it.
            //
            // Invalidated by SLUG PREFIX, so it reaches the ['member-panel', slug,
            // member] key without the frame having to carry the crew's name. The
            // frame is slug-only on purpose: the ownership digest must not reach a
            // client, and the refetch re-asks the server, which re-checks ownership.
            const slug = String((data as { slug?: unknown }).slug || '')
            if (slug) {
              queryClient.invalidateQueries({ queryKey: ['member-panel', slug] })
            }
            break
          }
          case 'notification_ack':
            dispatch(ackNotificationByTs(data.ts))
            break
          case 'notification_unack':
            dispatch(unackNotificationByTs(data.ts))
            break
          case 'notifications_clear':
            // Another view cleared the inbox; drop this view's copy so the
            // bell badge (derived from items) converges to 0. Idempotent.
            dispatch(clearAllNotifications())
            break
          case 'approval': {
            // The approvals module refreshes the shared `global-approvals` cache,
            // which the command center reads too.
            // The registry, the chime, the feed note and the live banner; the
            // owning slot comes back ('' for an unowned approval).
            const targetSlot = approvals.onApprovalFrame(data, reconnectingRef.current)
            // Inject inline in the OWNING chat only. An approval with no
            // explicit slot has no owning conversation (an unowned cron /
            // taskrunner command): falling back to activeSlot planted the card
            // in whatever chat the user happened to be viewing, where its
            // Trust control resolved against that innocent slot and the card
            // 404'd as soon as the short background window elapsed. Unowned
            // approvals live on the global surface (notification feed) only —
            // the feed note above already delivered it there.
            if (targetSlot) {
              dispatch(sseChatMessage({
                slot: targetSlot,
                role: 'permission',
                content: `[${data.source || 'agent'}] ${data.tool || 'Unknown'}`,
                ts: String(data.ts || Date.now() / 1000),
                meta: { tool_input: data.tool_input || '', approval_id: data.id, source: data.source, registry: 'coordinator', ...(data.tool_call_id ? { tool_call_id: data.tool_call_id } : {}) },
              }))
              // For spawn approvals, create a pending subagent entry instead of a toolLog approval.
              // Require an explicit slot from the event: falling back to activeSlot would
              // misattribute cards from other sessions/crons to whatever chat the user is
              // viewing (ghost "Starting…" cards with empty input that never resolve).
              const rid = data.id as string
              if (rid?.startsWith('spawn:')) {
                if (data.slot) {
                  const agentId = rid.replace('spawn:', '')
                  dispatch(sseSubagentPending({ slot: data.slot, id: agentId, task: (data.tool as string || '').replace('spawn_run(', '').replace(/\)$/, ''), approval_id: rid }))
                }
              } else if (data.source !== 'subagent') {
                dispatch(sseActivityEvent({ slot: targetSlot, kind: 'approval', text: data.tool || i18nT('hooks.useWebSocket.unknown'), approval_id: data.id, approval_type: 'chat' }))
              }
            }
            break
          }
          case 'approval_resolved':
            // The approvals module refreshes the shared `global-approvals` cache,
            // which the command center reads too.
            approvals.onApprovalResolved(data)
            break
          case 'refresh': {
            const kinds: string[] = data.kinds || []
            dispatch(triggerRefresh())
            invalidateRefreshQueries(queryClient)
            if (kinds.includes('history')) dispatch(fetchHistory(false))
            break
          }
          case 'slot_clear': {
            // /clear command — backend already appended its confirmation row.
            // Active slot clears the live pane; a background slot clears its
            // cached page instead, so neither a grid pane nor a failed-switch
            // restore can resurrect the discarded transcript (#6364 review).
            const clearSlot = data.slot as string
            // Drop the slot's buffered stream text (content + thinking) too:
            // an entry buffered before the /clear would otherwise flush on the
            // next frame and resurrect discarded text into the just-cleared
            // pane. Keyed delete — other slots' in-flight buffers are theirs.
            buffers.dropSlotChunks(clearSlot)
            if (clearSlot === store.getState().chat.activeSlot) dispatch(clearMessages())
            else dispatch(clearSlotCache(clearSlot))
            break
          }
          case 'slot_agent_switch': {
            // /agent command — refresh slot metadata to pick up new agent label
            dispatch(fetchSlots())
            break
          }
          case 'member_projection':
            handleMemberProjection(data)
            break
          case 'members_subscribed':
            handleMembersSubscribed(queryClient, data)
            break
          case 'chat_message':
            chatStream.onChatMessage(data)
            break
          case 'chat_message_update':
            // Server emits this for two distinct flows: tool_call_id-keyed
            // updates from claude-agent-acp tool_call_update, and row-keyed
            // patches for mcp_oauth banner state flips. Route by which key
            // the payload carries. The row-keyed branch prefers `mid` (the
            // server-minted row identity) over `ts`, which two restored rows
            // can share.
            if ((data as { tool_call_id?: string }).tool_call_id) {
              dispatch(sseChatMessageUpdate(data as { slot: string; tool_call_id: string; content?: string; meta?: Record<string, unknown> }))
            } else {
              dispatch(sseChatMessagePatchByTs(data as { slot: string; ts: string; mid?: string; meta?: Record<string, unknown>; content?: string }))
            }
            break
          case 'queue_pop':
            dispatch(removeQueuedMessage(data))
            break
          case 'queue_push':
            dispatch(appendQueuedMessage(data))
            // A send that lands behind a busy turn is still user input, so it
            // settles the session's rank now rather than only when the queue pops
            // — otherwise typing into a working session leaves it where it was.
            if (data.slot) {
              buffers.bufferSlotActivity(data.slot, (data as { ts?: string }).ts || new Date().toISOString(), true)
            }
            break
          case 'steer_push':
            chatStream.onSteerPush(data)
            break
          case 'queue_cancel':
            dispatch(cancelQueuedMessage(data))
            // A cancelled queued message is an answer that never lands. The
            // card was cleared optimistically when it was submitted, so without
            // this the slot would keep reporting needs_input with nothing on
            // screen to answer or dismiss. Re-syncing brings the card back from
            // the server's own record — the question is genuinely unanswered
            // again. Harmless when the cancelled message was not an answer: the
            // snapshot then lists nothing for the slot and adds nothing.
            cards.syncPendingQuestions()
            break
          case 'queue_edit':
            // The frame's `meta` is the entry's post-edit attachment lists;
            // `attachments` (present even when empty) tells the reducer this
            // is the server's word on them, not an optimistic local edit.
            dispatch(editQueuedMessage({ ...data, attachments: queueEntryAttachments((data as { meta?: unknown }).meta) }))
            break
          case 'queue_reorder':
            dispatch(reorderQueuedMessages(data))
            break
          case 'chat_chunk':
            chatStream.onChatChunk(data)
            break
          case 'tool_call':
            chatStream.onToolCall(data)
            break
          case 'tool_result':
            dispatch(sseToolResult(data as { slot: string; output: string; tool_call_id?: string }))
            break
          case 'mcp_app_render':
            // MCP App (SEP-1865) render payload from the gateway. Stored by
            // tool_call_id; ToolCallLine mounts an McpAppFrame below the row.
            dispatch(sseMcpAppRender(data as Parameters<typeof sseMcpAppRender>[0]))
            break
          case 'question_card':
            queryClient.invalidateQueries({ queryKey: ['command-center', 'questions'] })
            cards.onQuestionCard(data, reconnectingRef.current)
            break
          case 'question_card_resolved':
            queryClient.invalidateQueries({ queryKey: ['command-center', 'questions'] })
            cards.onQuestionCardResolved(data)
            break
          case 'followup_card':
            cards.onFollowupCard(data)
            break
          case 'slot_read': {
            // Another window read this slot (relayed via the gateway): retire
            // the bubble here too, honoring the read watermark — a badge lit
            // by a message NEWER than what the reader saw stays lit, and a
            // manual mark-as-unread is never cleared remotely. remoteSlotRead
            // (never the emitter) so a relayed read can't echo back out.
            const r = data as { slot?: string; read_ts?: string }
            if (typeof r.slot === 'string' && r.slot) {
              dispatch(remoteSlotRead({ slot: r.slot, readTs: typeof r.read_ts === 'string' && r.read_ts ? r.read_ts : undefined }))
            }
            break
          }
          case 'slot_folder_suggestion':
            cards.onFolderSuggestion(data)
            break
          case 'activity_event': {
            const ev = data as { slot: string; kind: string; text: string; spawned?: boolean }
            // A session was just created or resumed, which is the ONLY moment the
            // backend learns what this account is entitled to run (it comes from
            // session/new's advertised list). /api/models narrows its catalog to
            // that set, so refetch it then — a cold gateway answered the first
            // fetch from the unnarrowed catalog and, being a live 200, stopped
            // the self-heal poll, leaving the picker offering models no turn can
            // use for the rest of the page's life.
            //
            // Gated on `spawned`, not on the frame's presence: this frame is also
            // emitted on warm turns, where nothing was respawned and the
            // advertised list cannot have changed. /api/models SPAWNS
            // `kiro chat --list-models`, so refetching per prompt would run a
            // subprocess on every message. An absent flag is treated as "not
            // spawned" so an unexpected emitter cannot reintroduce that.
            if (ev.kind === 'session' && ev.spawned === true) {
              queryClient.invalidateQueries({ queryKey: ['available-models'] })
            }
            dispatch(sseActivityEvent(ev))
            break
          }
          case 'subagent_spawn':
            dispatch(sseSubagentSpawn(data as { slot: string; id: string; task: string; agent: string; model?: string; requested_model?: string }))
            break
          case 'subagent_queued':
            // The count plus the gate's optional `reason` label (absent from an
            // older gateway); the reducer parses the label.
            dispatch(sseSubagentQueued(data as { slot: string; queued: number; reason?: string; available_gb?: number; required_gb?: number }))
            break
          case 'subagent_chunk': {
            // Buffer and flush per-frame, mirroring chat_chunk.
            const { slot: chunkSlot, id: chunkId, text: chunkText } = data as { slot: string; id: string; text: string }
            if (chunkSlot && chunkId && chunkText) buffers.bufferSubagentChunk(chunkSlot, chunkId, chunkText)
            break
          }
          case 'subagent_tool':
            dispatch(sseSubagentTool(data as { slot: string; id: string; tool: string; turns?: number; tool_count?: number }))
            break
          case 'subagent_stalled':
            dispatch(sseSubagentStalled(data as { slot: string; id: string; stalled: boolean; idle_secs?: number }))
            break
          case 'subagent_retrying':
          case 'subagent_recovering':
            // Flush any buffered chunks before the retry event, so a stale
            // chunk flush cannot land after the retry dispatch and clear it.
            buffers.flushSubagentChunks()
            dispatch(sseSubagentRetrying(data as { slot: string; id: string; attempt?: number }))
            break
          case 'subagent_done':
            // Flush any buffered chunks before the done event, so the final
            // streaming text is visible before the agent transitions to done.
            buffers.flushSubagentChunks()
            dispatch(sseSubagentDone(data as { slot: string; id: string; elapsed: number; credits?: number; error?: string; stopped?: boolean; outcome?: 'completed' | 'failed' | 'stopped'; task?: string; agent?: string; model?: string; requested_model?: string; result?: string }))
            break
          case 'app_reload':
            emitAppReload(data as { app: string })
            break
          case 'subagent_snapshot': {
            // Clear any buffered chunks for this agent — the snapshot's streaming
            // field is authoritative and already includes any in-flight text.
            const snapData = data as { id: string; slot: string; task: string; agent: string; model?: string; requested_model?: string; streaming: string; last_tool: string; started: number; tool_count?: number; stalled?: boolean }
            buffers.dropSubagentKey(snapData.slot, snapData.id)
            dispatch(sseSubagentSnapshot(snapData))
            break
          }
          case 'subagent_batch_update': {
            // Per-key flush for retry items: a deferred chunk must land before
            // the retry flag is set, else the chunk flush clears retrying.
            const updates = (data as { updates?: { id: string; slot: string; attempt?: number }[] }).updates || []
            for (const u of updates) {
              if (typeof u.attempt === 'number' && u.slot && u.id) buffers.flushSubagentKey(u.slot, u.id)
            }
            dispatch(sseSubagentBatchUpdate(data as { updates: { id: string; slot: string; tool?: string; tool_count?: number; stalled?: boolean; attempt?: number }[] }))
            break
          }
          case 'subagent_batch_chunks':
            // chunks must dispatch before newer server-batched chunks arrive.
            buffers.flushSubagentChunks()
            dispatch(sseSubagentBatchChunks(data as { chunks: { id: string; slot: string; text: string }[] }))
            break
          case 'subagent_snapshot_batch': {
            // Reconnect replay collapsed into one frame at scale — fan the
            // items into the existing snapshot/done reducers (React 18
            // batches all dispatches from one message into a single render).
            const items = (data as { items?: { type: string; data: Record<string, unknown> }[] }).items || []
            for (const item of items) {
              if (item.type === 'subagent_snapshot') {
                // Clear any buffered chunks for this agent — the snapshot's streaming
                // field is authoritative and already includes any in-flight text.
                const snapItem = item.data as { slot?: string; id?: string }
                if (snapItem.slot && snapItem.id) buffers.dropSubagentKey(snapItem.slot, snapItem.id)
                dispatch(sseSubagentSnapshot(item.data as unknown as Parameters<typeof sseSubagentSnapshot>[0]))
              }
              else if (item.type === 'subagent_done') {
                // Per-key flush: emit only this agent's chunk, not all agents'.
                // A whole-buffer flush would race with later snapshot items.
                const doneItem = item.data as { slot?: string; id?: string }
                if (doneItem.slot && doneItem.id) buffers.flushSubagentKey(doneItem.slot, doneItem.id)
                dispatch(sseSubagentDone(item.data as unknown as Parameters<typeof sseSubagentDone>[0]))
              }
            }
            break
          }
          case 'spawn_batch_started':
          case 'batch_finished':
            // Wave lifecycle markers — no dedicated UI yet; the chip derives
            // its histogram from per-agent state. Reserved for wave grouping.
            break
          case 'slot_projection': {
            // A slot's crew log grew. A work board folds its conductor's units
            // with its bound workers', so the boards that move are this slot's
            // and every ancestor's. A read in flight is never cancelled -- a
            // steady stream of frames would restart a whole-log fold forever --
            // but it may have folded before this growth, so one more read
            // follows it once it settles.
            if (typeof data.slot !== 'string') break
            for (const root of teamRoots(store.getState().dashboard.slots, data.slot)) {
              const key = ['command-center', root, 'work']
              const inFlight = queryClient.getQueryCache().find({ queryKey: key, exact: true })
              if (inFlight?.state.fetchStatus === 'fetching' && inFlight.promise) {
                void inFlight.promise.catch(() => undefined).finally(() => { void queryClient.invalidateQueries({ queryKey: key, exact: true }, { cancelRefetch: false }) })
              } else queryClient.invalidateQueries({ queryKey: key, exact: true }, { cancelRefetch: false })
            }
            break
          }
          case 'workflow_run_event': {
            // Dynamic-workflow run events folded into chat.workflowRuns and
            // surfaced by WorkflowProgressBar above the chat input.
            const event = data as { run_id: string; seq?: number; ts?: number; type: string; data?: Record<string, unknown> }
            dispatch(sseWorkflowEvent(event))
            // The command center lays live runs over its REST snapshot and does
            // not poll it; once a finished run's live entry is cleared, the
            // snapshot must already say it finished.
            // A run's terminal events are `run_<status>` for the terminal statuses.
            if (event.type.startsWith('run_') && isTerminalWorkflowStatus(event.type.slice('run_'.length))) {
              queryClient.invalidateQueries({ queryKey: ['command-center', 'workflows'] })
            }
            break
          }
          case 'chat.side_result':
            dispatch(sseSideResult(data as { slot: string; run_id: string; role: 'user' | 'assistant'; content: string; ts?: number; final?: boolean; is_error?: boolean; steer?: boolean }))
            break
          case 'chat.thread_reply':
            handleThreadReply(queryClient, data)
            break
          case 'chat.side_queue': {
            // `raw` marks content the LOCAL client typed; broadcast payloads are scrubbed by
            // definition. Stripped rather than merely left out of the cast, so a future
            // server-side field of the same name could never vouch for redacted text.
            const { raw: _wireRaw, ...sideQueueFrame } = data as Record<string, unknown>
            // An echo of THIS tab's own cancel, or of another tab's. Only the tab that
            // cancelled takes the question back; every tab still drops the card. An absent
            // origin releases, which keeps a lone frame (HTTP response lost) from losing it.
            const frameOrigin = sideQueueFrame.origin_client
            const foreignCancel = typeof frameOrigin === 'string' && frameOrigin !== TAB_ID
            dispatch(sseSideQueue({
              ...(sideQueueFrame as unknown as { slot: string; action: 'push' | 'edit' | 'cancel' | 'drain'; queue_id: string; content?: string; ts?: number; front?: boolean; steer_id?: string }),
              ...(foreignCancel ? { suppressRelease: true } : {}),
            }))
            break
          }
          case 'heartbeat':
            break
          case 'context_usage':
            dispatch(sseContextUsage(data as { slot: string; pct: number; used_tokens?: number; window_tokens?: number; reset?: boolean }))
            break
          case 'chat_thinking':
            chatStream.onChatThinking(data)
            break
          case 'chat_segment': {
            buffers.flushChunks()
            voice.speakSegmentTail(data.slot as string)
            dispatch(sseChatMessage({ ...data, role: '_segment' }))
            break
          }
          case 'chat_status':
            if (data.slot && data.status) {
              dispatch(setSlotStatusDetail({ slot: data.slot, kind: 'thinking', label: data.status, ts: Date.now() }))
            }
            break
          case 'chat_variant_switch':
            if (data.slot) dispatch(refreshSlot(data.slot))
            break
          case 'chat_done': {
            buffers.flushChunks()
            if (data.slot) buffers.dropSlotChunks(data.slot)
            voice.speakTurnTail(data.slot)
            dispatch(sseChatMessage({ ...data, role: '_done' }))
            turnCompletion.afterDone(data)
            voice.refreshAutoSpeakIfSilent(data.slot)
            break
          }
          case 'autonudge_state':
            automations.onAutonudgeState(data)
            break
          case 'voice_chunk':
            voice.onVoiceChunk(data)
            break
          case 'voice_complete':
            voice.onVoiceComplete(data)
            break
          case 'voice_error':
            voice.onVoiceError(data)
            break
          case 'log':
            socket.logCbRef.current?.(data)
            break
          case 'sessions_restarting':
            // Backend pushed session restart status (restarting/ready)
            dispatch(triggerRefresh())
            invalidateRefreshQueries(queryClient)
            break
          case 'update_progress':
            handleUpdateProgress(dispatch, data)
            break
          case 'subagent_status':
            if (data.slot) dispatch(sseSubagentStatus(data as { running: number; slot: string; agents?: SubagentDetail[] }))
            break
          case 'subagent_text':
            if (data.slot && data.id) dispatch(sseSubagentText(data as { slot: string; id: string; text: string }))
            break
          case 'refine':
            // Handled by ProjectsPage via Redux
            dispatch(triggerRefresh())
            invalidateRefreshQueries(queryClient)
            break
          case 'channel_message':
          case 'channel_agent_status':
          case 'channel_created':
          case 'channel_closed':
          case 'channel_agent_joined':
          case 'channel_agent_left':
            emitChannelEvent(type, data)
            break
          case 'cron_history':
            emitCronHistory(data)
            queryClient.invalidateQueries({ queryKey: ['cron-history'] })
            queryClient.invalidateQueries({ queryKey: ['cron-history-all'] })
            break
          case 'source_status':
            handleSourceStatus(dispatch, queryClient, data)
            break
          case 'computer_use_frame':
            emitComputerUseFrame(data)
            break
        }
      } catch { /* ignore malformed */ }
    }

    ws.onclose = () => socket.handleClose(ws, dispatch, voice.releaseVoiceOnSocketLoss, connect)

    ws.onerror = () => { /* onclose will fire */ }
  }, [dispatch, queryClient, socket, reconnectingRef, voice, automations, approvals, cards, syncWorkflowRuns, buffers, slotList, bundle, chatStream, turnCompletion, syncPendingApprovals])

  /** Replace the socket now (see `SocketConnection.forceReconnect`): the
   *  health probe's recovery path and the silence watchdog's remedy. */
  const forceReconnect = useCallback(() => {
    socket.forceReconnect(voice.releaseVoiceOnSocketLoss, connect)
  }, [socket, voice, connect])

  /** Replace a socket that is OPEN but has stopped delivering.
   *
   *  A browser can keep a WebSocket whose transport is gone -- a phone that
   *  changed networks, or a tab resumed after the OS froze it -- without ever
   *  firing `onclose`. Nothing else notices: the reconnect catch-up only runs on
   *  close, and the health probe only polls while `connected` is false. Every
   *  one-shot frame is then lost until a manual reload; an agent-armed
   *  `autonudge_state` is the visible case, since no local mutation writes that
   *  record. The gateway's 5s `dashboard` frame is the liveness signal: while the
   *  page is visible, a socket silent for WS_SILENCE_MS is torn down through
   *  `forceReconnect`, whose catch-up re-reads every frame family.
   *
   *  Silence is measured in visible time only. Hidden pages are skipped (timers
   *  are throttled there, and a suspended tab processed nothing); on the return
   *  every stamp moves past the hidden interval, so silence adds up across tab
   *  switches while a frame that arrived in the background counts as arriving
   *  at the return. A thawed socket then gets two checks to deliver its next
   *  status frame before it can be replaced -- unless its visible silence had
   *  already passed the window when the page came back, in which case the
   *  first check replaces it, so a dead socket cannot outlive a run of glances
   *  each shorter than that grace. Each consecutive silent replacement doubles
   *  the window up to WS_SILENCE_MAX_MS, so a gateway that stopped sending
   *  status on a live socket costs one reconnect per window rather than one
   *  every 20s; a socket observed live for a whole window of visible time since
   *  it opened resets the count. */
  useEffect(() => {
    const silenceWindowMs = () => Math.min(
      WS_SILENCE_MS * 2 ** silentReconnectsRef.current,
      WS_SILENCE_MAX_MS,
    )
    const check = () => {
      if (document.hidden) return
      const ws = socket.wsRef.current
      if (!ws || ws.readyState !== WebSocket.OPEN) return
      const now = Date.now()
      const silenceMs = silenceWindowMs()
      if (now - lastFrameAtRef.current <= silenceMs) {
        if (now - silenceClockStartedAtRef.current > silenceMs) silentReconnectsRef.current = 0
        return
      }
      if (now < graceUntilRef.current) return
      silentReconnectsRef.current += 1
      forceReconnect()
    }
    const onVisibility = () => {
      const now = Date.now()
      if (document.hidden) {
        hiddenAtRef.current = now
        return
      }
      // Move each stamp past only the hidden time after it, and never past
      // now: a stamp from before the hide keeps its visible age, and a frame
      // that arrived while hidden counts as arriving at the return.
      const skipHidden = (stamp: number) => stamp + now - Math.max(hiddenAtRef.current, stamp)
      lastFrameAtRef.current = skipHidden(lastFrameAtRef.current)
      silenceClockStartedAtRef.current = skipHidden(silenceClockStartedAtRef.current)
      hiddenAtRef.current = 0
      // Two checks of grace for a thawed socket, none for one whose visible
      // silence had already passed the window before it was hidden.
      graceUntilRef.current = now - lastFrameAtRef.current > silenceWindowMs()
        ? 0
        : now + WS_SILENCE_CHECK_MS * 2
    }
    const timer = setInterval(check, WS_SILENCE_CHECK_MS)
    document.addEventListener('visibilitychange', onVisibility)
    return () => {
      clearInterval(timer)
      document.removeEventListener('visibilitychange', onVisibility)
    }
  }, [socket, forceReconnect])

  useEffect(() => {
    socket.resume()
    connect()
    const detachVoice = voice.attachWindowEvents()
    const detachFocus = attachFocusRelay({
      socket,
      flushSlotActivity: buffers.flushSlotActivity,
      onActiveSlotChange: voice.resetForNewTurn,
    })
    return () => {
      socket.beginClosing()
      buffers.flushForUnmount()
      socket.closeForUnmount()
      voice.dispose()
      detachVoice()
      detachFocus()
    }
  }, [socket, connect, voice, buffers])

  return { subscribeLogs: socket.subscribeLogs, subscribeSubagents: socket.subscribeSubagents, forceReconnect }
}
