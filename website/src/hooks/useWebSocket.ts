import { useEffect, useRef, useCallback } from 'react'
import { useQueryClient, type QueryClient } from '@tanstack/react-query'
import { isArtifactEditing } from '../utils/artifactEditGuard'
import { isReconcileNote } from '../lib/noteContract'
import { useAppDispatch } from '../store'
import { store } from '../store'
import { sseStatus, sseConnected, sseDisconnected, sseSlots, sseTodoUpdate, setChannelTrusted, sseSlotTitle, triggerRefresh, fetchSlots, markSlotUnread, setUpdateProgress, sseSubagentStatus, sseSubagentText, touchSlotActivity, patchSlotSourceLinks, type SubagentDetail } from '../store/dashboardSlice'
import { addNotification, ackNotificationByTs, unackNotificationByTs, removeNotificationByTs, clearAllNotifications, fetchNotifications } from '../store/notificationsSlice'
import { MC_NOTIFICATION_EVENT, TURN_DONE_KIND, APPROVAL_KIND, shouldChimeOnTurnDone, type McNotificationDetail } from './notificationEvent'
import { emitThemeSound } from './themeSound'
import {
  fetchHistory, missedChunkMarker, sseChatMessage, sseChatMessageUpdate, sseChatMessagePatchByTs, sseThinkingChunk, refreshSlot, warmSlotCache, sseContextUsage, clearMessages, setVoicePlaying, setVoiceAudio, resolveByApprovalId, clearSubagentsForSnapshot, sseSubagentPending, sseSubagentSpawn, sseSubagentQueued, sseSubagentChunk, sseSubagentTool, sseSubagentStalled, sseSubagentRetrying, sseSubagentDone, sseSubagentSnapshot, sseSubagentBatchUpdate, sseSubagentBatchChunks, sseToolActivity, sseToolResult, sseActivityEvent, sseSideResult, sseWorkflowEvent, setSlotStatusDetail, removeQueuedMessage, appendQueuedMessage, cancelQueuedMessage, editQueuedMessage, reorderQueuedMessages, appendSlotMessage, setQuestionCard, resolveQuestionCard, setFollowupCard, setFolderSuggestion, sseMcpAppRender, setGoalLoops, sseGoalLoop, sseSideQueue
} from '../store/chatSlice'
import { TAB_ID } from '../api/tabId'
import { api } from '../api/client'
import { sanitizeLlmOutput } from '../utils/sanitize'
import { applyStatusDelta, parseStatusDelta } from '../utils/pullRequestStatusDelta'
import type { StatusData, ChatMessage, ChatSlot, ChatFolder, Notification, PullRequestStatusBatch, TodoList } from '../types'
import { i18nT } from '../i18n/t'

type LogCallback = ((data: { level: string; msg: string }) => void) | null

type VoiceProgress = {
  slot: string
  messageId: string
  spokenLen: number
}

function voiceMessageId(message: ChatMessage): string {
  const clientId = message.meta?.clientTs
  if (typeof clientId === 'string' && clientId) return clientId
  if (message.ts) return message.ts
  const serverId = message.meta?.mid
  return typeof serverId === 'string' ? serverId : ''
}

/**
 * Invalidate React Query caches for keys that previously relied on the
 * `refreshTrigger` counter being part of their queryKey.  Calling
 * `invalidateQueries` refetches **in-place** (keeping the cached data visible
 * to the UI) instead of minting a brand-new cache entry with `undefined` data
 * — which is what caused the flash-to-empty bug (#4132, #4179).
 */
function invalidateRefreshQueries(qc: QueryClient): void {
  qc.invalidateQueries({ queryKey: ['cron-jobs'] })
  qc.invalidateQueries({ queryKey: ['cron-history-all'] })
  qc.invalidateQueries({ queryKey: ['spawn-list'] })
  qc.invalidateQueries({ queryKey: ['sessions-context'] })
  qc.invalidateQueries({ queryKey: ['sessions-usage'] })
  qc.invalidateQueries({ queryKey: ['agents-installed'] })
  qc.invalidateQueries({ queryKey: ['mcp-tools'] })
  qc.invalidateQueries({ queryKey: ['kirocrew-agents'] })
  qc.invalidateQueries({ queryKey: ['default-agent'] })
  qc.invalidateQueries({ queryKey: ['workspaces'] })
  qc.invalidateQueries({ queryKey: ['kirocrewConfig'] })
}

/** Single multiplexed WebSocket replacing all SSE + polling connections. */
/** The server-side IDENTITY of a held card: a blocking ask's `ask_id`, or a
 *  stateless card's server-minted `card_id`. Both kinds are listed by
 *  `GET /api/ask-question/pending`, so both can be reconciled against it.
 *
 *  A card with neither (an entry built by a fixture, or delivered before the
 *  server kept a record) has no identity to compare, and absence from the
 *  snapshot says nothing about it — those are skipped rather than reported
 *  stale. */
export function identityOf(card: { ask_id?: string; serverCardId?: string } | undefined): string {
  return card?.ask_id || card?.serverCardId || ''
}

/** Own-property card identities held in the pending-question map, in map order. */
export function askIdsOf(
  map: Record<string, { ask_id?: string; serverCardId?: string } | undefined> | undefined,
): string[] {
  return Object.values(map ?? {})
    .map((card) => identityOf(card))
    .filter((id): id is string => !!id)
}

/** Ask-ids recorded as resolved after `watermark`, newest-agnostic order.
 *
 *  The log is keyed by ask_id with a monotonic sequence rather than an array,
 *  so bounding it cannot shift the watermark's meaning. */
export function resolvedSince(log: Map<string, number>, watermark: number): string[] {
  const out: string[] = []
  for (const [askId, seq] of log) if (seq > watermark) out.push(askId)
  return out
}

/** Decide what a reconnect reconcile should drop and re-add.
 *
 *  Pure so the race can be tested directly. `before` is the pending-question map
 *  captured BEFORE the HTTP snapshot was requested and `after` the map as it
 *  stands once the response arrives; the difference is exactly what live WS
 *  events did while the request was in flight.
 *
 *  - Only ids present in `before` may be dropped. A `question_card` that arrived
 *    during the fetch is absent from the response, and deleting it would leave
 *    the agent blocked until its timeout.
 *  - An id that vanished locally during the fetch was resolved by a WS event, so
 *    the response's copy is already dead and must not be re-added — a
 *    resurrected card can only 404 on submit.
 *
 *  Both kinds of card go through this, keyed by `identityOf`: the server records
 *  and lists stateless cards too, so a tab that was disconnected while its card
 *  was retired or replaced must have it removed, not merely be denied a
 *  duplicate. A card whose identity is absent from the snapshot is stale in
 *  exactly the same sense for both kinds.
 */
export function reconcileQuestions<
  T extends { ask_id?: string; card_id?: string; slot?: string; questions?: unknown[] },
>(
  before: Record<string, { ask_id?: string; serverCardId?: string } | undefined> | undefined,
  after: Record<string, { ask_id?: string; serverCardId?: string } | undefined> | undefined,
  pending: T[],
  resolvedDuringFetch: string[] = [],
): { drop: string[]; add: T[] } {
  const afterIds = new Set(askIdsOf(after))
  // Two independent sources, because neither alone is sufficient:
  //  - the before/after diff catches a card that WAS local and disappeared
  //    (including one this client resolved itself, with no WS event involved);
  //  - the observed resolution log catches an identity this client never held, so
  //    there was nothing for the diff to notice. That is the case where the
  //    snapshot alone would resurrect a dead card.
  const dead = new Set([
    ...askIdsOf(before).filter((id) => !afterIds.has(id)),
    ...resolvedDuringFetch,
  ])
  const beforeIds = new Set(askIdsOf(before))
  /** True when *slot* now holds a DIFFERENT card that the snapshot cannot know
   *  about — one that arrived while the request was in flight (its identity is
   *  absent from `before`). The same ordering argument as the drop side, applied
   *  to adds: one card renders per slot, so adding the snapshot's row would
   *  replace a newer live card with a stale one. */
  const arrivedDuringFetch = (slot: string | undefined, identity: string): boolean => {
    if (!slot) return false
    const held = identityOf(after?.[slot])
    return !!held && held !== identity && !beforeIds.has(held)
  }
  return {
    drop: staleAskIds(before, pending),
    add: pending.filter((q) => {
      const identity = q.ask_id || q.card_id || ''
      return (
        !!q.slot &&
        !!q.questions?.length &&
        !dead.has(identity) &&
        !arrivedDuringFetch(q.slot, identity)
      )
    }),
  }
}

/** Card identities held locally that the server no longer lists as pending.
 *
 *  `question_card` and `question_card_resolved` are one-shot broadcasts, so a
 *  reload or reconnect can miss either one: a card that should be showing is
 *  absent, or one retired while disconnected is still on screen. Reconnect
 *  therefore reconciles in both directions rather than only adding — for a
 *  stateless card as much as a blocking one, since keeping a retired or
 *  superseded card would send its answer against a question the agent has
 *  already moved past.
 *
 *  A card with no identity at all is never reported stale: there is nothing to
 *  compare, so its absence from the response says nothing about it.
 *  Exported so this is unit-testable without standing up a live socket.
 */
export function staleAskIds(
  current: Record<string, { ask_id?: string; serverCardId?: string } | undefined> | undefined,
  pending: { ask_id?: string; card_id?: string }[],
): string[] {
  const live = new Set(pending.map((q) => q.ask_id || q.card_id || '').filter(Boolean))
  return askIdsOf(current).filter((id) => !live.has(id))
}

/* Slot-focus intent signal (resume prefetch). The hook instance owns the
   live socket, but split view needs to report pane focus from a component
   tree that has no access to the hook's return value — so the sender is a
   module-level indirection the hook binds while mounted. Before the hook
   mounts (or after it unmounts) the emitter is a no-op: focus frames are a
   best-effort optimization, never load-bearing. */
let sendSlotFocusedImpl: (slot: string | null) => void = () => {}

export function emitSlotFocused(slot: string | null): void {
  sendSlotFocusedImpl(slot)
}

export function useWebSocket() {
  const dispatch = useAppDispatch()
  const queryClient = useQueryClient()
  const wsRef = useRef<WebSocket | null>(null)
  const closingRef = useRef(false)  // true when cleanup intentionally closes WS
  /* Resolutions observed on the wire, ask_id -> monotonic sequence. Needed
     because a `question_card_resolved` can name a card this client never held
     (it exists only inside an in-flight rehydration snapshot), so the dispatch
     is a no-op and local state carries no trace of it. Bounded, and keyed by id
     with a sequence so trimming never shifts a watermark's meaning. */
  const resolvedAskIdsRef = useRef<Map<string, number>>(new Map())
  const resolvedSeqRef = useRef(0)
  /** Log a RETIRED question identity — a blocking `ask_id` or a stateless
   *  `card_id`, in one map because the snapshot add side asks the same question
   *  of both: "was this retired while my request was in flight?" */
  const recordRetiredId = useCallback((retiredId: string) => {
    if (!retiredId) return
    const log = resolvedAskIdsRef.current
    log.set(retiredId, ++resolvedSeqRef.current)
    if (log.size > 200) {
      // Drop the oldest entries; a reconcile only ever consults recent ones.
      const oldest = [...log.entries()].sort((a, b) => a[1] - b[1]).slice(0, log.size - 200)
      for (const [id] of oldest) log.delete(id)
    }
  }, [])
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout>>()  // pending reconnect timer
  const logCbRef = useRef<LogCallback>(null)
  const subagentSubRef = useRef(false)
  const reconnectRef = useRef(1000)
  const wasConnectedRef = useRef(false)
  const reconnectingRef = useRef(false)  // suppress markSlotUnread during reconnect catch-up
  const lastVersionRef = useRef<string | null>(null)
  const lastGitlabHostsGenRef = useRef<number | null>(null)
  const lastSlotsRawRef = useRef<string | null>(null)
  const lastSlotsArrayRef = useRef<ChatSlot[] | null>(null)
  const voiceQueueRef = useRef<string[]>([])
  const voicePlayingRef = useRef(false)
  const activeAudioRef = useRef<HTMLAudioElement | null>(null)
  const autoSpeakRef = useRef(false)
  // Speech offsets belong to one concrete message. A segment for another slot
  // must not reset the active message, and a new post-tool segment must start
  // from zero even while the previous message remains in the transcript.
  const voiceProgressRef = useRef<VoiceProgress | null>(null)
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

  // Slot-recency coalescing: last ts seen per slot, flushed once per frame, plus
  // whether the burst contained a SETTLING row (a prompt) — one settled event
  // anywhere in the burst settles the flush, since the reducer's settled bump is
  // additive rather than a toggle.
  // Last-seen wins — the reducer is last-write-wins, so this is the burst's end state.
  const slotActivityBufRef = useRef<Map<string, { ts: string; settled: boolean }>>(new Map())
  const slotActivityFlushScheduledRef = useRef(false)
  const slotActivityRafRef = useRef<number | null>(null)
  const slotActivityTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

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

  const voiceProgressFor = useCallback((slot: string, message: ChatMessage): VoiceProgress | null => {
    const messageId = voiceMessageId(message)
    if (!messageId) return null
    const current = voiceProgressRef.current
    if (!current || current.slot !== slot || current.messageId !== messageId) {
      const next = { slot, messageId, spokenLen: 0 }
      voiceProgressRef.current = next
      voiceMutedRef.current = false
      return next
    }
    return current
  }, [])

  const enqueueVoiceSynthesis = useCallback((slot: string, text: string) => {
    synthChainRef.current = synthChainRef.current
      .then(() => api.voiceSynthesize(slot, text))
      .catch(() => {})
  }, [])

  const flushVoiceTail = useCallback((slot: string, message: ChatMessage) => {
    const progress = voiceProgressFor(slot, message)
    if (!progress) return
    const remaining = message.content.slice(progress.spokenLen).trim()
    // Mark the whole message consumed even when its tail is below the speech
    // floor, so a later completion event cannot reconsider or repeat it.
    progress.spokenLen = message.content.length
    if (remaining.length >= 10) enqueueVoiceSynthesis(slot, remaining)
  }, [enqueueVoiceSynthesis, voiceProgressFor])

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

  /** Bumped by every `autonudge_state` frame. A seed captures this before its
   *  fetch and discards the response if a frame landed while it was in flight:
   *  the seed's full-replace would otherwise resurrect a loop whose `removed`
   *  frame we had already applied, and because frames fire only on CHANGE
   *  nothing would ever correct it — leaving a phantom "Loop N/M" that
   *  suppresses that row's unread dot until the next reconnect. */
  const goalLoopGenRef = useRef(0)

  /** Cold-seed the sidebar's goal-loop map. `autonudge_state` only fires on
   *  change, so a loop armed before this client connected would show no progress
   *  until its next cycle fired — which can be minutes. Runs on first connect
   *  and on every reconnect. Silent on failure and on the feature being
   *  disabled (the endpoint answers `{enabled:false, loops:[]}`). */
  const seedGoalLoops = useCallback(() => {
    const gen = goalLoopGenRef.current
    // Fully best-effort, including SYNCHRONOUS failure. This runs early in the
    // connect handler, ahead of notification sync and the subagent subscribe, so
    // an exception escaping here would silently strand those — a cosmetic seed
    // must never be able to do that.
    try {
      api.autonudgeList()
        .then(res => {
          // A live frame superseded this snapshot — it is now stale, so drop it
          // rather than replacing fresher state with older state.
          if (goalLoopGenRef.current !== gen) return
          dispatch(setGoalLoops((res?.loops || []).map(loop => ({
            slot: loop.slot_key,
            active: loop.active === true,
            cycle_count: Number(loop.cycle_count) || 0,
            max_cycles: Number(loop.max_cycles) || 0,
          }))))
        })
        .catch(() => {})
    } catch { /* seed is cosmetic — never break the connect path */ }
  }, [dispatch])

  const syncPendingApprovals = useCallback(async () => {
    try {
      const approvals = await api.approvals()
      const existing = store.getState().notifications.items
      for (const a of approvals) {
        if (existing.some((n: Notification) => n.approval_id === a.id)) continue
        dispatch(addNotification({
          kind: 'approval',
          title: i18nT('hooks.useWebSocket.tool_approval', { name: a.tool || i18nT('hooks.useWebSocket.unknown') }),
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

  /** Reconcile question cards against the server's pending set.
   *  `question_card` and `question_card_resolved` are one-shot broadcasts, so a
   *  reload or reconnect can miss either one: a card that should be showing is
   *  absent, or one resolved while we were disconnected is still on screen.
   *  This is a two-way reconcile rather than an add-only sync for that reason.
   *
   *  The snapshot is taken BEFORE the fetch, and that ordering is the whole
   *  correctness argument. The HTTP response describes the server as it was when
   *  the request was served, so it races live WS events both ways:
   *   - a `question_card` arriving DURING the fetch is absent from the response;
   *     reconciling against post-fetch state would delete it and leave the agent
   *     blocked until timeout. Only ids present before the fetch can be dropped,
   *     and Redux state is immutable, so the pre-fetch reference cannot contain
   *     a later addition.
   *   - a `question_card_resolved` arriving during the fetch leaves a card in the
   *     response that is already dead; re-adding it would resurrect a card whose
   *     submit can only 404. Those ids are skipped on the add side.
   *  Legacy cards (no ask_id) are preserved -- the server has no record of them,
   *  so their absence from the response is not evidence they are stale. */
  const syncPendingQuestions = useCallback(async () => {
    try {
      const before = store.getState().chat.pendingQuestions
      // Watermark the resolution log before the request so the ids that arrive
      // while it is in flight can be identified afterwards. This is the only
      // signal that covers a resolution for a card this client never held —
      // `before`/`after` cannot see it, because there was nothing to remove.
      const resolvedSeen = resolvedSeqRef.current
      const pending = await api.pendingQuestions()
      // ONE reconcile for both kinds. The server records and lists stateless
      // cards, so their absence from the snapshot is evidence in the same way a
      // blocking ask's is: a tab that was disconnected while its card was retired
      // or superseded must lose it, or submitting it answers a question the agent
      // has already moved past.
      const { drop, add } = reconcileQuestions(
        before,
        store.getState().chat.pendingQuestions,
        pending,
        resolvedSince(resolvedAskIdsRef.current, resolvedSeen),
      )
      // Identity-keyed retirement: an entry is dropped by whichever id it holds.
      const heldBefore = Object.values(before ?? {})
      for (const id of drop) {
        const wasBlocking = heldBefore.some((c) => c?.ask_id === id)
        dispatch(resolveQuestionCard(wasBlocking ? { ask_id: id } : { card_id: id }))
      }
      for (const q of add) {
        // A stateless row carries `card_id`; a blocking one carries `ask_id`. The
        // reducer coalesces a structurally identical re-delivery, so re-adding a
        // card this tab already holds keeps the mounted component and the user's
        // half-entered answer rather than churning it.
        dispatch(setQuestionCard({
          slot: q.slot as string,
          ask_id: q.ask_id,
          card_id: q.card_id,
          questions: q.questions as Parameters<typeof setQuestionCard>[0]['questions'],
        }))
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
      const msgs = store.getState().chat.messages
      const streaming = [...msgs].reverse().find(m => m.role === 'streaming')
      if (streaming) {
        const progress = voiceProgressFor(activeSlot, streaming)
        if (!progress) return
        const full = streaming.content
        let lastBound = -1
        const re = /[.!?](?:\s|$)/g
        let match
        while ((match = re.exec(full)) !== null) {
          if (match.index + 1 > progress.spokenLen) lastBound = match.index + 1
        }
        if (lastBound > progress.spokenLen) {
          const newText = full.slice(progress.spokenLen, lastBound).trim()
          if (newText.length >= 10) {
            progress.spokenLen = lastBound
            enqueueVoiceSynthesis(activeSlot, newText)
          }
        }
      }
    }
  }, [dispatch, enqueueVoiceSynthesis, voiceProgressFor])

  const scheduleChunkFlush = useCallback(() => {
    if (chunkFlushScheduledRef.current) return
    chunkFlushScheduledRef.current = true
    if (typeof requestAnimationFrame === 'function') chunkRafRef.current = requestAnimationFrame(() => flushChunks())
    else chunkTimerRef.current = setTimeout(() => flushChunks(), 16)
  }, [flushChunks])

  /** Flush buffered slot-recency bumps: one touchSlotActivity per slot, not per
   *  event. Cancels any pending frame first, mirroring flushChunks. */
  const flushSlotActivity = useCallback(() => {
    if (slotActivityRafRef.current != null && typeof cancelAnimationFrame === 'function') cancelAnimationFrame(slotActivityRafRef.current)
    if (slotActivityTimerRef.current != null) clearTimeout(slotActivityTimerRef.current)
    slotActivityFlushScheduledRef.current = false
    slotActivityRafRef.current = null
    slotActivityTimerRef.current = null
    const buf = slotActivityBufRef.current
    if (buf.size === 0) return
    // A frame firing during reconnect backoff must drop, not dispatch: the on-open
    // refetch is authoritative. Unmount sets closingRef and still flushes deliberately.
    const ws = wsRef.current
    if (!closingRef.current && (!ws || ws.readyState !== WebSocket.OPEN)) { buf.clear(); return }
    // Every buffered bump is dispatched: the "never move a timestamp backwards"
    // rule lives in the reducer, which holds both fields. It has to be per-field —
    // mid-turn `last_ts` runs ahead of `last_turn_ts`, so one shared check would
    // drop a settling bump whose ts is older than the newest streamed row.
    for (const [key, { ts, settled }] of buf) {
      dispatch(touchSlotActivity({ key, ts, settled }))
    }
    buf.clear()
  }, [dispatch])

  const scheduleSlotActivityFlush = useCallback(() => {
    if (slotActivityFlushScheduledRef.current) return
    slotActivityFlushScheduledRef.current = true
    if (typeof requestAnimationFrame === 'function') slotActivityRafRef.current = requestAnimationFrame(() => flushSlotActivity())
    else slotActivityTimerRef.current = setTimeout(() => flushSlotActivity(), 16)
  }, [flushSlotActivity])

  /** Buffer one slot-recency bump for the next frame.
   *  Keeps the NEWEST ts of the burst, and `settled` is sticky: one prompt
   *  anywhere in a burst settles the flush, so the settling row surviving the
   *  agent output it triggered does not depend on arrival order. */
  const bufferSlotActivity = useCallback((slot: string, ts: string, settled: boolean) => {
    const prev = slotActivityBufRef.current.get(slot)
    const newest = prev && Date.parse(prev.ts) > Date.parse(ts) ? prev.ts : ts
    slotActivityBufRef.current.set(slot, { ts: newest, settled: settled || !!prev?.settled })
    scheduleSlotActivityFlush()
  }, [scheduleSlotActivityFlush])

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
      // Forget the last-seen allowlist generation: it is process-local to the
      // gateway, so after a restart an equal number can mean a different
      // allowlist. Clearing it makes the next generation frame refetch.
      lastGitlabHostsGenRef.current = null
      // Forget the last raw slots frame too, so a reconnect whose first frame
      // repeats the last one before it cannot swallow that first frame.
      lastSlotsRawRef.current = null
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
        // Same for pending recency bumps: the fetchSlots below carries authoritative last_ts.
        if (slotActivityRafRef.current != null && typeof cancelAnimationFrame === 'function') cancelAnimationFrame(slotActivityRafRef.current)
        if (slotActivityTimerRef.current != null) clearTimeout(slotActivityTimerRef.current)
        slotActivityRafRef.current = null
        slotActivityTimerRef.current = null
        slotActivityFlushScheduledRef.current = false
        slotActivityBufRef.current.clear()
        dispatch(sseConnected())
        dispatch(fetchSlots()).finally(() => { reconnectingRef.current = false })
        // A summary regenerated while the socket was down pushed a
        // `session_summary` event nobody received, and the panel does not poll,
        // so without this the stale summary persists until the tab remounts.
        // Invalidate every slot's summary (the key is per-slot and we cannot
        // know which ones moved); react-query only refetches the observed ones.
        queryClient.invalidateQueries({ queryKey: ['session-summary'] })
        seedGoalLoops()
        dispatch(fetchNotifications()).then(() => syncPendingApprovals())
      syncPendingQuestions()
        // Re-fetch active slot messages to recover from missed chunks
        const active = store.getState().chat.activeSlot
        if (active) dispatch(refreshSlot(active))
        // Eagerly subscribe to subagent events so chunks arrive even when
        // Activity Panel isn't open — final result still comes via done event.
        dispatch(clearSubagentsForSnapshot())
        ws.send(JSON.stringify({ type: 'subscribe_subagents' }))
        subagentSubRef.current = true
        if (logCbRef.current) ws.send(JSON.stringify({ type: 'subscribe_logs' }))
        // Re-announce focus: the server lost this socket's focus state with
        // the old connection, and the store subscription only fires on change.
        ws.send(JSON.stringify({ type: 'slot_focused', slot: active || null }))
        return
      }
      wasConnectedRef.current = true
      dispatch(sseConnected())
      seedGoalLoops()
      // FIRST connect only: App's mount effect already dispatched fetchSlots,
      // and this handler fires strictly after it, so repeating it here is a
      // redundant round-trip at the worst possible moment. The reconnect branch
      // above still refetches — there it recovers state missed while the socket
      // was down. fetchNotifications IS still dispatched here despite the
      // mount-effect copy: syncPendingApprovals must only run after a
      // notifications fetch has settled, because fetchNotifications.fulfilled
      // replaces membership and ordering wholesale and would wipe any approval
      // notifications synced before it. Its merge preserves local ack flags
      // only, so it is no protection for a row the response does not carry.
      dispatch(fetchNotifications()).then(() => syncPendingApprovals())
      syncPendingQuestions()
      // Eagerly subscribe to subagent events on first connect too.
      dispatch(clearSubagentsForSnapshot())
      ws.send(JSON.stringify({ type: 'subscribe_subagents' }))
      subagentSubRef.current = true
      // Flush a log subscription that was requested before the socket opened.
      // subscribeLogs() stores the callback but returns early when readyState
      // is not OPEN, so a page mounting during the handshake (a cold load of
      // /logs) would otherwise never send subscribe_logs and would show no
      // lines at all until an unrelated reconnect. The reconnect branch above
      // already does this; the two paths must stay symmetric.
      if (logCbRef.current) ws.send(JSON.stringify({ type: 'subscribe_logs' }))
      // Announce the restored active slot so a resumable session prefetches
      // while the user reads its transcript (resume prefetch).
      ws.send(
        JSON.stringify({ type: 'slot_focused', slot: store.getState().chat.activeSlot || null })
      )
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
            // An identical repeat carries identical values for every arm below, but
            // only while no other writer (fetchSlots) has since replaced the list.
            const raw = e.data as string
            if (raw === lastSlotsRawRef.current
                && store.getState().dashboard.slots === lastSlotsArrayRef.current) break
            lastSlotsRawRef.current = raw
            dispatch(sseSlots(data as ChatSlot[]))
            lastSlotsArrayRef.current = store.getState().dashboard.slots
            if (msg.yolo !== undefined) {
              dispatch(sseStatus({ yolo: msg.yolo } as StatusData))
            }
            if (msg.channelTrusted !== undefined) {
              dispatch(setChannelTrusted(msg.channelTrusted))
            }
            // Seed the ['chat-folders'] query cache from the folder tree carried
            // on this frame so the sidebar groups sessions correctly on the FIRST
            // paint. Sessions arrive on this WS frame the instant the socket
            // connects; the folders otherwise come only from a separate HTTP GET,
            // so without this the sidebar renders every session ungrouped (Unfiled)
            // until that GET resolves, then visibly re-shuffles them into folders.
            //
            // Seed ONLY when the cache has no folder data yet (first paint). Two
            // reasons this must not run on later frames, both from the shipped
            // staleTime: Infinity on this query:
            //   1. A `slots` frame fires on routine session activity, so a frame
            //      landing inside an in-flight folder mutation's optimistic window
            //      (collapse / reorder / rename / move) would overwrite the
            //      optimistic cache value with backend state via a direct
            //      setQueryData — which the mutation's cancelQueries cannot cancel
            //      — snapping the folder back to its pre-action state until
            //      onSettled refetches.
            //   2. The WS payload omits per-folder `history_count` (the backend
            //      computes it via a synchronous session scan that must not run on
            //      this hot path). Seeding count-less data marks the query fresh,
            //      so a mount-time query would skip GET /api/chat/folders and the
            //      counts (the "hide when empty" filter's input) would never load.
            // So seed the tree once, then invalidate to let the HTTP GET backfill
            // counts; after the cache is populated, live frames leave it alone and
            // folder create/rename/move propagate through their own mutation +
            // invalidate path as before.
            //
            // Guard on `existing === undefined` (cache NEVER populated), NOT on
            // `!existing || length === 0`: a user with genuinely zero folders has
            // the HTTP GET cache the empty array `[]`, and `[].length === 0` would
            // then re-match on EVERY subsequent slots frame — re-seeding `[]` and
            // re-invalidating in a loop, hammering the session-scanning
            // GET /api/chat/folders. `undefined` fires exactly once, on first paint.
            if (Array.isArray(msg.folders)) {
              const existing = queryClient.getQueryData<ChatFolder[]>(['chat-folders'])
              if (existing === undefined) {
                queryClient.setQueryData<ChatFolder[]>(['chat-folders'], msg.folders as ChatFolder[])
                // Backfill history_count (omitted from the WS payload) — the seed
                // marked the query fresh, so nudge the real GET to run.
                queryClient.invalidateQueries({ queryKey: ['chat-folders'] })
              }
            }
            // Refresh the cached GitLab-hosts allowlist when it may have changed.
            // The generation is PROCESS-local, so a gateway restart can hand out a
            // number equal to the one this client last saw even though the
            // allowlist on disk changed. Treat the first generation frame of each
            // connection as "unknown, refetch" and only compare within a
            // connection — one extra fetch per connect, never a stale allowlist.
            if (typeof msg.gitlabHostsGeneration === 'number') {
              const prevGen = lastGitlabHostsGenRef.current
              lastGitlabHostsGenRef.current = msg.gitlabHostsGeneration
              if (prevGen === null || prevGen !== msg.gitlabHostsGeneration) {
                queryClient.invalidateQueries({ queryKey: ['dashboardConfig'] })
              }
            }
            break
          }
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
          case 'slot_title':
            dispatch(sseSlotTitle(data as { key: string; title: string }))
            break
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
          case 'artifact_update': {
            // Live artifact refresh: the backend broadcasts from the artifact
            // mutation funnel (create / content PATCH / revert / relocate /
            // pull / delete). Invalidate the per-slug queries so any open view
            // — detail page, popout, the companion panel's left pane —
            // re-renders the new version immediately. Every window has its own
            // WS, so no BroadcastChannel is needed. The library list is
            // invalidated too (create/delete change it; content updates bump
            // its updated_at ordering).
            const slug = (data as { slug?: string }).slug
            if (slug) {
              if ((data as { deleted?: boolean }).deleted) {
                // Notify, but deliberately do NOT evict ['artifact', slug].
                // Evicting drops the detail page's query data, which re-renders it
                // into a loading/404 state and unmounts the editor — taking an
                // unsaved edit buffer with it. That would defeat the deletion
                // listener's dirty-page guard, which exists precisely so the user
                // can still copy their work out. A clean page navigates away, and a
                // dirty one keeps its cached content; neither needs the eviction,
                // and a genuine refetch 404s on its own because the artifact is
                // gone server-side.
                window.dispatchEvent(new CustomEvent('kirocrew:artifact-deleted', { detail: { slug } }))
              } else if (isArtifactEditing(slug)) {
                // A human has an unsaved buffer open on this artifact. Refetching
                // would move the editor's baseline while the buffer keeps the older
                // text, so the next Save would overwrite whatever just arrived.
                // Leave the content cache alone; the page reloads it on save or
                // cancel. Comments/events carry no edit buffer, so they still
                // refresh — only content is withheld.
                queryClient.invalidateQueries({ queryKey: ['artifact-events', slug] })
                queryClient.invalidateQueries({ queryKey: ['artifact-comments', slug] })
              } else {
                queryClient.invalidateQueries({ queryKey: ['artifact', slug] })
                queryClient.invalidateQueries({ queryKey: ['artifact-versions', slug] })
                queryClient.invalidateQueries({ queryKey: ['artifact-events', slug] })
                queryClient.invalidateQueries({ queryKey: ['artifact-comments', slug] })
              }
              queryClient.invalidateQueries({ queryKey: ['artifacts'] })
            }
            break
          }
          case 'notification': {
            const n = data as Notification
            dispatch(addNotification(n))
            // Also fire MC_NOTIFICATION_EVENT so useNotificationSound plays the
            // configured sound — the Redux action alone only drives the
            // toast/badge, not the sound.
            // RFC Phase 3: muted-channel (silenced) and passive notes are
            // feed-only — no sound.
            if (!n.silenced && n.priority !== 'passive') {
              try {
                const detail: McNotificationDetail = { kind: n.kind }
                window.dispatchEvent(new CustomEvent(MC_NOTIFICATION_EVENT, { detail }))
              } catch (err) {
                console.warn('mc-notification listener error', err)
              }
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
            queryClient.invalidateQueries({ queryKey: ['global-approvals'] })
            // Approval-blocked chime: the agent is stuck until the user acts.
            // Suppressed during reconnect catch-up (same policy as turn-done).
            if (!reconnectingRef.current) {
              try {
                const detail: McNotificationDetail = { kind: APPROVAL_KIND }
                window.dispatchEvent(new CustomEvent(MC_NOTIFICATION_EVENT, { detail }))
              } catch (err) {
                console.warn('mc-notification listener error', err)
              }
            }
            // Browser notification when tab not focused (permission must be granted via UI interaction elsewhere)
            if (typeof Notification !== 'undefined' && document.hidden && Notification.permission === 'granted') {
              // Android Chrome throws "Illegal constructor" for page-context
              // Notification; an uncaught throw here kills the whole message
              // handler, so the native toast is best-effort.
              try {
                new Notification(i18nT('hooks.useWebSocket.approval_required'), { body: data.tool || i18nT('hooks.useWebSocket.a_task_needs_your_decision'), tag: 'kirocrew-approval' })
              } catch {
                /* unsupported platform */
              }
            }
            dispatch(addNotification({
              kind: 'approval',
              title: i18nT('hooks.useWebSocket.tool_approval', { name: data.tool || i18nT('hooks.useWebSocket.unknown') }),
              body: `**Source:** ${data.source || 'agent'}\n\n${data.tool_input || ''}\n\n${data.tool_purpose || ''}`.trim(),
              ts: String(data.ts || Date.now() / 1000),
              approval_id: data.id,
            } as Notification))
            // Inject inline in the OWNING chat only. An approval with no
            // explicit slot has no owning conversation (an unowned cron /
            // taskrunner command): falling back to activeSlot planted the card
            // in whatever chat the user happened to be viewing, where its
            // Trust control resolved against that innocent slot and the card
            // 404'd as soon as the short background window elapsed. Unowned
            // approvals live on the global surface (notification feed) only —
            // the addNotification above already delivered it there.
            const targetSlot = data.slot || ''
            if (targetSlot) {
              dispatch(sseChatMessage({
                slot: targetSlot,
                role: 'permission',
                content: `[${data.source || 'agent'}] ${data.tool || 'Unknown'}`,
                ts: String(data.ts || Date.now() / 1000),
                meta: { tool_input: data.tool_input || '', approval_id: data.id, source: data.source, ...(data.tool_call_id ? { tool_call_id: data.tool_call_id } : {}) },
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
          case 'approval_resolved': {
            queryClient.invalidateQueries({ queryKey: ['global-approvals'] })
            const items = store.getState().notifications.items
            const match = items.find((n: Notification) => n.approval_id === data.id)
            if (match) dispatch(removeNotificationByTs(match.ts))
            dispatch(resolveByApprovalId({ id: data.id, decision: data.approved ? 'approved' : 'rejected' }))
            // Resolve only in the slot that raised the card. Guessing
            // activeSlot here mirrored the raise-path leak: it wrote
            // subagent spawn/done entries into an unrelated conversation.
            const targetSlot = data.slot || ''
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
            invalidateRefreshQueries(queryClient)
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
            // Re-rank the sidebar the instant a session sees a message, instead of waiting
            // for the next full slots push. `last_ts` moves for agent output too (it feeds
            // "last message" reads); the ORDERING key moves only for an inbound prompt —
            // user or inject — so a running turn holds its position instead of shuffling the
            // list on every tool call. Fallback ts is computed here so the touchSlotActivity
            // reducer stays pure (Redux contract).
            if (data.slot && (data.role === 'user' || data.role === 'inject' || data.role === 'assistant' || data.role === 'tool_call' || data.role === 'tool_result')) {
              bufferSlotActivity(
                data.slot,
                data.ts || new Date().toISOString(),
                data.role === 'user' || data.role === 'inject',
              )
            }
            if (data.slot && data.slot !== store.getState().chat.activeSlot && !reconnectingRef.current) dispatch(markSlotUnread(data.slot))
            // Theme audio: an agent reply arriving is the `message-received`
            // trigger (no-op unless an L2 theme with that manifest sound is
            // active + unmuted). User/tool messages don't chime.
            if (data.role === 'assistant') emitThemeSound('message-received')
            // A note breadcrumb starts no turn, so no chat_done arrives to undo either
            // effect: cutting speech would strand it and a thinking status would never clear.
            const isPassiveNote = data.role === 'inject' && isReconcileNote(data.cls)
            if (!isPassiveNote && (data.role === 'user' || data.role === 'inject' || data.role === 'subagent')) { stopVoice(); voiceProgressRef.current = null; synthChainRef.current = Promise.resolve() }
            if (!isPassiveNote && data.slot && (data.role === 'user' || data.role === 'inject' || data.role === 'subagent')) {
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
            // A send that lands behind a busy turn is still user input, so it
            // settles the session's rank now rather than only when the queue pops
            // — otherwise typing into a working session leaves it where it was.
            if (data.slot) {
              bufferSlotActivity(data.slot, (data as { ts?: string }).ts || new Date().toISOString(), true)
            }
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
            // Steering is the other way to type into a busy session, so it
            // settles the rank exactly like a queued send. The server appends a
            // real `user` row for it, so the authoritative snapshot already
            // agrees — this only avoids waiting for the next slots push.
            if ((data as { slot?: string }).slot) {
              bufferSlotActivity(
                (data as { slot: string }).slot,
                (data as { ts?: string }).ts || new Date().toISOString(),
                true,
              )
            }
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
            syncPendingQuestions()
            break
          case 'queue_edit':
            dispatch(editQueuedMessage(data))
            break
          case 'queue_reorder':
            dispatch(reorderQueuedMessages(data))
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
            // Re-broadcast BEFORE the store dispatches: ChatPage opens the Browser
            // panel from this event, and a reducer that throws on a malformed
            // payload must not also cost the panel its only signal.
            window.dispatchEvent(new CustomEvent('kirocrew-tool-call', { detail: data }))
            dispatch(sseToolActivity({ ...data as { slot: string; tool: string; kind: string; purpose: string; input_preview: string; is_shell?: boolean }, auto: (data as Record<string, unknown>).auto === true, tool_call_id: (data as Record<string, unknown>).tool_call_id as string | undefined, is_update: (data as Record<string, unknown>).is_update === true, is_shell: (data as Record<string, unknown>).is_shell === true }))
            if (data.slot) {
              // A refinement (`is_update`) carries only the fields it refines,
              // so merge it into the live status the way sseToolActivity merges
              // the tool-log entry: an update that omits `purpose` must not
              // replace the purpose the initial tool_call supplied with the raw
              // command, and one that omits `tool` must not blank the title.
              // Without this the session-list row of a running session flips
              // from the agent's purpose to the literal command mid-call.
              // Merging is gated on the tool_call_id matching, so when several
              // tools run in parallel a refinement of one cannot inherit a
              // sibling's purpose.
              //
              // `text` holds the PURPOSE ALONE and stays empty when the agent
              // supplied none — the fallback to the tool title belongs to
              // toolStatusLabel, which owns the label rule. Storing the title
              // in `text` instead would make the two indistinguishable here,
              // and a purpose-less call would then pin the initial stub title
              // ("Terminal") for the whole call instead of advancing to the
              // refined command.
              const tcid = (data as Record<string, unknown>).tool_call_id as string | undefined
              const isUpdate = (data as Record<string, unknown>).is_update === true
              const purpose = sanitizeLlmOutput((data as Record<string, unknown>).purpose as string || '')
              const toolName = sanitizeLlmOutput(data.tool || '')
              const prev = store.getState().chat.slotStatusDetail[data.slot]
              const mergeInto = isUpdate && tcid && prev?.kind === 'tool' && prev.toolCallId === tcid
                ? prev
                : undefined
              dispatch(setSlotStatusDetail({
                slot: data.slot,
                kind: 'tool',
                text: purpose || mergeInto?.text || '',
                toolName: toolName || mergeInto?.toolName || '',
                ...(tcid ? { toolCallId: tcid } : {}),
                ts: Date.now(),
              }))
            }
            // Note: do NOT dispatch sseChatMessage here. The backend persists the
            // tool message via slot.append and broadcasts it as 'chat_message'.
            // Dispatching here would insert a duplicate entry in the message list.
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
            // `fresh` marks a LIVE ask delivery: even if its payload repeats
            // the identical question, it must get its own delivery identity
            // (cardId) — unlike the reconnect re-sync above, which re-dispatches
            // a still-pending card and must keep the existing entry.
            dispatch(setQuestionCard({ ...(data as Parameters<typeof setQuestionCard>[0]), fresh: true }))
            break
          case 'question_card_resolved': {
            const ask = data as { ask_id?: string; card_id?: string }
            // Recorded independently of local state: a resolution can arrive for
            // a card this client never held (empty state, or the card only exists
            // in an in-flight rehydration snapshot), in which case the dispatch
            // below is a no-op and the reconcile would otherwise re-add a dead
            // card. See recordRetiredId. Both identities land in the same log —
            // a blocking ask's `ask_id` and a stateless card's `card_id` — so one
            // watermark covers both kinds on the snapshot add side.
            recordRetiredId(ask.ask_id || ask.card_id || '')
            dispatch(resolveQuestionCard(ask))
            break
          }
          case 'followup_card': {
            // Agent-authored follow-up suggestions. The server caps this at 3
            // items and has already sanitized + redacted every string; the
            // slice keeps only the fields the card renders.
            const raw = data as { slot?: string; items?: Array<Record<string, unknown>>; ts?: number }
            const items = (Array.isArray(raw.items) ? raw.items : [])
              .filter((it) => it && typeof it.title === 'string' && typeof it.prompt === 'string')
              .map((it) => ({
                title: String(it.title),
                description: typeof it.description === 'string' ? it.description : '',
                prompt: String(it.prompt),
                ...(typeof it.branch === 'string' && it.branch ? { branch: it.branch } : {}),
              }))
            if (raw.slot && items.length) {
              dispatch(setFollowupCard({
                slot: raw.slot,
                items,
                ...(typeof raw.ts === 'number' ? { ts: raw.ts } : {}),
              }))
            }
            break
          }
          case 'slot_folder_suggestion': {
            // Post-titling offer to file an unfiled session. Every field is the
            // user's own stored folder data (the backend model call returns an
            // index, not text), but the shape is still validated here so a
            // malformed frame cannot render an empty or half-filled card.
            const raw = data as { slot?: string; folder_id?: string; folder_name?: string; breadcrumb?: string; ts?: number }
            if (raw.slot && typeof raw.folder_id === 'string' && raw.folder_id && typeof raw.folder_name === 'string' && raw.folder_name) {
              dispatch(setFolderSuggestion({
                slot: raw.slot,
                folderId: raw.folder_id,
                folderName: raw.folder_name,
                breadcrumb: typeof raw.breadcrumb === 'string' ? raw.breadcrumb : '',
                ...(typeof raw.ts === 'number' ? { ts: raw.ts } : {}),
              }))
            }
            break
          }
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
            dispatch(sseSubagentSpawn(data as { slot: string; id: string; task: string; agent: string }))
            break
          case 'subagent_queued':
            dispatch(sseSubagentQueued(data as { slot: string; queued: number }))
            break
          case 'subagent_chunk':
            dispatch(sseSubagentChunk(data as { slot: string; id: string; text: string }))
            break
          case 'subagent_tool':
            dispatch(sseSubagentTool(data as { slot: string; id: string; tool: string; turns?: number; tool_count?: number }))
            break
          case 'subagent_stalled':
            dispatch(sseSubagentStalled(data as { slot: string; id: string; stalled: boolean; idle_secs?: number }))
            break
          case 'subagent_retrying':
          case 'subagent_recovering':
            dispatch(sseSubagentRetrying(data as { slot: string; id: string; attempt?: number }))
            break
          case 'subagent_done':
            dispatch(sseSubagentDone(data as { slot: string; id: string; elapsed: number; error?: string; stopped?: boolean; outcome?: 'completed' | 'failed' | 'stopped'; task?: string; agent?: string; result?: string }))
            break
          case 'app_reload':
            // App dev-mode live reload: the gateway watched a dev-flagged app's
            // ui/ dir change. AppHost listens for this and re-imports the bundle.
            window.dispatchEvent(new CustomEvent('mc:app-reload', { detail: data as { app: string } }))
            break
          case 'subagent_snapshot':
            dispatch(sseSubagentSnapshot(data as { id: string; slot: string; task: string; agent: string; streaming: string; last_tool: string; started: number; tool_count?: number; stalled?: boolean }))
            break
          case 'subagent_batch_update':
            // Scale plumbing: one coalesced ~1s frame replacing per-event
            // tool/stalled/retrying frames when many agents run.
            dispatch(sseSubagentBatchUpdate(data as { updates: { id: string; slot: string; tool?: string; tool_count?: number; stalled?: boolean; attempt?: number }[] }))
            break
          case 'subagent_batch_chunks':
            dispatch(sseSubagentBatchChunks(data as { chunks: { id: string; slot: string; text: string }[] }))
            break
          case 'subagent_snapshot_batch': {
            // Reconnect replay collapsed into one frame at scale — fan the
            // items into the existing snapshot/done reducers (React 18
            // batches all dispatches from one message into a single render).
            const items = (data as { items?: { type: string; data: Record<string, unknown> }[] }).items || []
            for (const item of items) {
              if (item.type === 'subagent_snapshot') dispatch(sseSubagentSnapshot(item.data as unknown as Parameters<typeof sseSubagentSnapshot>[0]))
              else if (item.type === 'subagent_done') dispatch(sseSubagentDone(item.data as unknown as Parameters<typeof sseSubagentDone>[0]))
            }
            break
          }
          case 'spawn_batch_started':
          case 'batch_finished':
            // Wave lifecycle markers — no dedicated UI yet; the chip derives
            // its histogram from per-agent state. Reserved for wave grouping.
            break
          case 'workflow_run_event':
            // Dynamic-workflow run events folded into chat.workflowRuns and
            // surfaced by WorkflowProgressBar above the chat input.
            dispatch(sseWorkflowEvent(data as { run_id: string; seq?: number; ts?: number; type: string; data?: Record<string, unknown> }))
            break
          case 'chat.side_result':
            dispatch(sseSideResult(data as { slot: string; run_id: string; role: 'user' | 'assistant'; content: string; ts?: number; final?: boolean; is_error?: boolean; steer?: boolean }))
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
          case 'chat_thinking': {
            // kiro-cli/ACP reasoning (agent_thought_chunk) -> collapsible block.
            dispatch(sseThinkingChunk({ slot: data.slot, content: (data as { content?: string }).content || '' }))
            // Dispatch the status detail only on a genuine kind TRANSITION into
            // 'thinking'. Guarding merely on `!== 'streaming'` would not
            // self-limit — 'thinking' is itself `!== 'streaming'`, so it would
            // re-dispatch on EVERY thought frame with a fresh `ts`. Because
            // setSlotStatusDetail replaces slotStatusDetail[slot] wholesale, that
            // bumps the map identity per frame and re-renders every whole-map
            // subscriber (ChatSidebar, CommandPalette) for the duration of the
            // model's reasoning. The sibling chat_chunk guard above writes
            // 'streaming' and so is naturally idempotent; this is the same shape,
            // made explicit.
            const detailKind = data.slot ? store.getState().chat.slotStatusDetail[data.slot]?.kind : undefined
            if (data.slot && detailKind !== 'streaming' && detailKind !== 'thinking') {
              dispatch(setSlotStatusDetail({ slot: data.slot, kind: 'thinking', text: 'Thinking…', ts: Date.now() }))
            }
            break
          }
          case 'chat_segment': {
            flushChunks()
            const segmentSlot = data.slot as string
            if (autoSpeakRef.current && !voiceMutedRef.current && segmentSlot === store.getState().chat.activeSlot) {
              const streaming = [...store.getState().chat.messages].reverse().find(m => m.role === 'streaming')
              if (streaming) flushVoiceTail(segmentSlot, streaming)
            }
            dispatch(sseChatMessage({ ...data, role: '_segment' }))
            break
          }
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
            // Consume the tail while the streaming row still carries the same
            // identity used by sentence-boundary progress tracking.
            if (autoSpeakRef.current && !voiceMutedRef.current && data.slot === store.getState().chat.activeSlot) {
              const msgs = store.getState().chat.messages
              const last = [...msgs].reverse().find(m => m.role === 'streaming')
                ?? [...msgs].reverse().find(m => m.role === 'assistant')
              if (last) flushVoiceTail(data.slot, last)
            }
            dispatch(sseChatMessage({ ...data, role: '_done' }))
            // Turn-complete chime: sound-only (no feed entry, no toast).
            // Plays on every real turn completion — active or background
            // chat — and never during reconnect catch-up replay.
            // Preset/volume/mute resolve in useNotificationSound via the
            // 'turn' category.
            if (shouldChimeOnTurnDone({
              slot: data.slot,
              reconnecting: reconnectingRef.current,
            })) {
              try {
                const detail: McNotificationDetail = { kind: TURN_DONE_KIND }
                window.dispatchEvent(new CustomEvent(MC_NOTIFICATION_EVENT, { detail }))
              } catch (err) {
                console.warn('mc-notification listener error', err)
              }
            }
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
            if (data.slot) {
              // Turn boundary: the finished turn is the likeliest moment for
              // this session's PRs to have moved (comments, mergeability, a
              // pushed revision) — changes the lightweight status delta does NOT
              // carry. Invalidate the detail/status queries so they refetch.
              // For the ACTIVE slot, refetch now (the panel is on screen). For a
              // BACKGROUND slot, only MARK stale (refetchType: 'none'): its
              // detail query is staleTime:Infinity, so without this it would stay
              // "fresh" forever and render pre-turn data when the user later
              // switches to it — but refetching an off-screen PR every background
              // turn would be wasteful, so defer the fetch to its next mount.
              const isActive = data.slot === store.getState().chat.activeSlot
              const refetchType = isActive ? 'active' : 'none'
              queryClient.invalidateQueries({ queryKey: ['pull-request-source'], refetchType })
              queryClient.invalidateQueries({ queryKey: ['pull-request-statuses'], refetchType })
            }
            if ((!autoSpeakRef.current || voiceMutedRef.current) && data.slot === store.getState().chat.activeSlot) {
              // Re-check config in case it changed
              api.voiceConfig().then(c => { autoSpeakRef.current = !!c.autoSpeak }).catch(() => {})
            }
            break
          case 'autonudge_state': {
            // Broadcast for ChatPage to refresh its autonudge loop state.
            window.dispatchEvent(new CustomEvent('autonudge_state', { detail: data }))
            // Mirror into the store as well, so the sidebar can show progress on
            // EVERY looping row instead of only the active slot (ChatPage's
            // listener filters to `activeSlot`). The service emits one event per
            // fired cycle, which is what makes the cycle counter tick live.
            const nudge = data as unknown as {
              event?: string
              slot?: string
              loop?: { active?: boolean; cycle_count?: number; max_cycles?: number }
            }
            if (nudge.slot) {
              // Bump BEFORE dispatching so an in-flight seed is invalidated even
              // if its .then() runs immediately after this frame is handled.
              goalLoopGenRef.current++
              dispatch(sseGoalLoop({
                slot: nudge.slot,
                // `removed` still carries the loop object (the gateway observer
                // only fires when `loop is not None`), and its `active` flag is
                // whatever it was at removal — so the event name, not the flag,
                // decides a removal.
                active: nudge.event !== 'removed' && nudge.loop?.active === true,
                cycle_count: Number(nudge.loop?.cycle_count) || 0,
                max_cycles: Number(nudge.loop?.max_cycles) || 0,
              }))
            }
            break
          }
          case 'voice_chunk': {
            if (voiceMutedRef.current) break
            if (data.slot !== store.getState().chat.activeSlot) break
            // Queue and play audio chunks as they arrive
            const { audio: b64, audioMime } = data as { audio: string; audioMime?: string }
            if (b64) {
              try {
                const bytes = Uint8Array.from(atob(b64), c => c.charCodeAt(0))
                const blob = new Blob([bytes], { type: audioMime === 'audio/wav' ? 'audio/wav' : 'audio/mpeg' })
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
            invalidateRefreshQueries(queryClient)
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
            invalidateRefreshQueries(queryClient)
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
          case 'source_status': {
            // A pull request's lifecycle/CI status changed on the gateway. Patch
            // the strip's cached batch straight away (no poll wait) and refetch
            // the detail payload so both surfaces — on every owner window — track
            // the same state instead of disagreeing until the next poll.
            const delta = parseStatusDelta(data)
            if (!delta) break
            // Cancel any in-flight batched-status fetch first. Without this, a
            // status poll that started before this delta can resolve AFTER the
            // setQueriesData below and overwrite the authoritative pushed value
            // with its stale snapshot for a full TTL. cancelQueries aborts the
            // pending fetch so it cannot clobber the patch; the retained poll
            // (and the detail invalidation below) reconcile from here.
            queryClient.cancelQueries({ queryKey: ['pull-request-statuses'] })
            queryClient.setQueriesData<PullRequestStatusBatch>(
              { queryKey: ['pull-request-statuses'] },
              batch => applyStatusDelta(batch, delta),
            )
            // Patch the sidebar chips too: those render from the Redux `slots`
            // payload (`source_links[].state/ci`), NOT react-query, so a delta
            // that only touched the query caches would leave the sidebar glyph
            // stale until an unrelated slots broadcast — the same chip↔panel
            // divergence this feature removes, recreated on the sidebar.
            dispatch(patchSlotSourceLinks({ url: delta.url, state: delta.state, ci: delta.ci }))
            // Invalidate the detail payload for EVERY changed delta, regardless
            // of origin. A 'detail'-origin delta is produced by one window's full
            // fetch; only that window received the fresh HTTP payload, so other
            // owner windows must refetch too or their staleTime:Infinity detail
            // query keeps rendering the pre-change lifecycle — the exact chip↔
            // panel divergence this feature fixes, just across windows. The
            // initiating window's refetch is harmless: it hits the gateway's
            // still-warm full-payload cache (same value, no re-projection, no new
            // delta), so there is no feedback loop.
            queryClient.invalidateQueries({ queryKey: ['pull-request-source', delta.url] })
            queryClient.invalidateQueries({ queryKey: ['pull-request-checks', delta.url] })
            break
          }
          case 'computer_use_frame':
            // Computer-use PiP frame — the downscaled JPEG the agent's own
            // computer_get_state call already captured, relayed by the gateway
            // (owner sockets only; suppressed for secure windows and under a
            // screenshot-denying ceiling). Same window-event routing as above so
            // ComputerUseLiveView needs no Redux slice.
            window.dispatchEvent(
              new CustomEvent('kirocrew-computer-use-frame', { detail: data }),
            )
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
  }, [dispatch, flushChunks, scheduleChunkFlush, bufferSlotActivity, playNextVoiceChunk, queryClient, stopVoice, syncPendingApprovals, syncPendingQuestions, seedGoalLoops, recordRetiredId])

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
    // Slot-focus intent signal (resume prefetch). One shared sender for
    // every focus source — Redux activeSlot changes (sidebar, keyboard,
    // deep links, history), tab visibility, and split-view pane focus via
    // emitSlotFocused — so the HTTP and WS notions of "focused" cannot
    // drift. Best-effort: dropped silently while the socket is not OPEN.
    const sendFocus = (slot: string | null) => {
      const ws = wsRef.current
      if (!ws || ws.readyState !== WebSocket.OPEN) return
      ws.send(JSON.stringify({ type: 'slot_focused', slot }))
    }
    sendSlotFocusedImpl = sendFocus
    let lastFocusSent: string | null = null
    const unsubFocus = store.subscribe(() => {
      const active = store.getState().chat.activeSlot
      if (active === lastFocusSent) return  // store.subscribe fires on EVERY action
      lastFocusSent = active
      sendFocus(active)
    })
    const onVisibility = () => {
      // Hidden → blur (cancels a pending prefetch server-side); visible →
      // re-announce the active slot even if unchanged, since the server may
      // have expired the previous prefetch while the tab was away.
      sendFocus(document.hidden ? null : store.getState().chat.activeSlot)
    }
    document.addEventListener('visibilitychange', onVisibility)
    return () => {
      closingRef.current = true
      clearTimeout(reconnectTimerRef.current)
      if (chunkRafRef.current != null && typeof cancelAnimationFrame === 'function') cancelAnimationFrame(chunkRafRef.current)
      if (chunkTimerRef.current != null) clearTimeout(chunkTimerRef.current)
      // Flush rather than drop: the store outlives the hook, so a pending bump would
      // otherwise leave a stale sidebar tint. The flush also cancels the scheduled frame.
      flushSlotActivity()
      wsRef.current?.close()
      wsRef.current = null
      window.removeEventListener('voice-stop', onVoiceStop)
      window.removeEventListener('voice-config-changed', onVoiceConfigChanged)
      document.removeEventListener('visibilitychange', onVisibility)
      unsubFocus()
      sendSlotFocusedImpl = () => {}
    }
  }, [connect, stopVoice, flushSlotActivity])

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
