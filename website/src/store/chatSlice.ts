/** The `chat` slice: the one `createSlice` wiring point and the module every
 *  consumer imports chat state through. The reducer families, thunks and
 *  selectors live in `store/chat/*` and are composed and re-exported here, so
 *  every action type string, action creator, thunk, selector, constant and
 *  type keeps this import path.
 *
 *  Three things stay in this file on purpose: the live `chat_message` frame
 *  reducer (both halves) with the chunk-gap marker and the question-retiring
 *  role set it applies (`test_slot_needs_input_status.py` reads that set from
 *  this file, and the marker is counted by the i18n ledger here), the thunks
 *  that dispatch this slice's own actions (`deleteSlot`, `requestStop`), and
 *  `loadOlderMessages` with its reducers, whose slot capture `chatPins.test.tsx`
 *  reads from this file. */
import { createSlice, createAsyncThunk, type PayloadAction } from '@reduxjs/toolkit'
import { whenScrollQuiet } from '../lib/scrollQuiet'
import { nextActiveAfterClose } from '../lib/sessionTabs'
import { api } from '../api/client'
import { isNotFoundError } from '../api/apiError'
import { releaseCloseHold, confirmCloseHold, removeSlotOptimistic, fetchSlots, slotSurfaceKey } from './dashboardSlice'
import { isChatPageSurface } from '../utils/channelOrigin'
import { isNoteRow } from '../lib/noteContract'
import { gcSessionStorage } from '../utils/storageGc'
import type { RootState } from './index'
import type { ChatMessage } from '../types'
import { SOFT_STOP_DEBOUNCE_MS } from '../pages/chat/types'
import { mergePreservedPastes } from '../utils/pasteTokens'
import { initialState, type ChatState } from './chat/state'
import { filterMessages, isUnsafeKey, safeKey } from './chat/wire'
import { ensureMsgId, finalizeTrailingStreaming, floorForGen, isRedeliveredMessage, mintMsgId, reconcileOptimisticEcho } from './chat/transcript'
import { OLDER_PAGE_LIMIT, OLDER_WALK_PAGE_LIMIT, claimOlderFetchAbort, isSupersededPagingRejection, releaseOlderFetchAbort } from './chat/paging'
import { reinsertThinkingOrphans } from './chat/thinking'
import { bumpRunEpoch, runStateReducers, setRunState, syncOriginRun } from './chat/runState'
import { setPagingCursor, slotCacheReducers } from './chat/slotCache'
import { composerCardReducers } from './chat/composerCards'
import { messageReducers } from './chat/messages'
import { queueReducers } from './chat/queue'
import { activityReducers } from './chat/activity'
import { subagentReducers } from './chat/subagents'
import { automationReducers } from './chat/automations'
import { sideReducers } from './chat/side'
import { workflowReducers } from './chat/workflows'
import { mcpAppReducers } from './chat/mcpApps'
import { addSlotListCases, evictSlotState } from './chat/slotResidue'
import { addSlotSwitchCases, switchSlot } from './chat/slotSwitch'
import { addSlotRefreshCases } from './chat/slotRefresh'
import { addLifecycleCases, historyNoticeReducers } from './chat/lifecycle'

/** Frame roles that retire a slot's pending STATELESS question card.
 *
 *  Exactly one role, `user`, and the narrowness is the whole rule. The card's
 *  contract is "the user's answer arrives as the next message", so the only
 *  frame that consumes that channel is one the HUMAN sent: they answered in the
 *  composer, or said something else, and either way spent their next message.
 *
 *  `nudge` was in this set (PR #2131) on the theory that an auto-nudge cycle
 *  moves the session past the question. It does not consume the answer channel:
 *  a nudge wakes the SAME agent in the SAME conversation, so a message the user
 *  sends ten cycles later still lands on the agent that asked. Retiring on it
 *  deleted the user's only affordance for a question nobody had answered —
 *  observed on a monitored conductor session, where the card was gone by the
 *  time the user came back to it, and the server record went with it so a reload
 *  had nothing to rehydrate. An unanswered card now stays until it is answered
 *  or explicitly DISMISSED; dismissal is a server round-trip that retires the
 *  record too, and it is the control that keeps a genuinely stale card from
 *  lingering — the auto-retire was covering for a control that now exists.
 *
 *  `inject` (cron notifications, recovery resumes) and `subagent` (completion
 *  events) also start turns and are out for the same reason they always were:
 *  they interleave with a question the agent may STILL be waiting on. Extending
 *  coverage is a data edit here, not a code change (per Design Review on PR
 *  #2131), and the backend's `_QUESTION_RETIRING_ROLES` must be edited with it
 *  (parity is pinned by test_slot_needs_input_status.py). */
const QUESTION_RETIRING_ROLES = new Set(['user'])

/** Drop a slot's pending STATELESS question card (no ``ask_id``) when the user's
 *  own frame lands on that slot.
 *
 *  A stateless card's contract is "the user's answer arrives as the next
 *  message" (the agent ended its turn on it — `post_question_card`, no
 *  server-side wait). A `user` row IS that next message, so the card it was
 *  waiting for has arrived and the card is spent. Nothing else retires it —
 *  see `QUESTION_RETIRING_ROLES` for why a nudge does not.
 *
 *  Blocking cards (with `ask_id`) are exempt: their lifecycle is the
 *  `question_card_resolved` broadcast (answered / timed out / cancelled /
 *  slot stop), and a blocked wait can legitimately outlive a mid-turn steer
 *  frame — clearing on it would strand the blocked tool call with no card.
 *
 *  Stateless cards are server-owned too: the server retires the record
 *  on the same user row and broadcasts `question_card_resolved`, which
 *  `resolveQuestionCard` applies by identity. This local drop is kept as
 *  defense in depth for the frame that arrives before that broadcast, so the
 *  card never outlives the row that answered it by even one render.
 *
 *  Shared by the two hand-synced frame appliers (active `sseChatMessage` and
 *  background `applyNonActiveFrame`) so the paths cannot drift; both call it
 *  AFTER their redelivery guard so a replayed old frame cannot wipe a new
 *  card. */
const dropStaleStatelessQuestion = (state: ChatState, slot: string, role: string): void => {
  if (!QUESTION_RETIRING_ROLES.has(role)) return
  const card = state.pendingQuestions?.[safeKey(slot)]
  if (card && !card.ask_id) {
    // Never destroy work in progress: a non-empty custom answer lives only in
    // the card's component state (QuestionCard publishes emptiness flips via
    // setQuestionDraft), so deleting the entry here would unmount the card and
    // silently discard the user's half-typed answer — precisely on monitored
    // sessions, where nudge frames land at unpredictable times. The card stays
    // until the draft is cleared, answered, or manually dismissed; staleness
    // resumes on the next turn-consuming frame after that.
    if (card.draftActive) return
    delete state.pendingQuestions[safeKey(slot)]
  }
}

/** Single-sourced "N chunk(s) missed" degradation marker. Used by the reducer's
 *  defensive non-batched path and by `batchedTextAboveFloor` for the live
 *  batched path, so the marker text and gap arithmetic cannot drift.
 *  Returns '' when the seqs are adjacent (no gap). */
export const missedChunkMarker = (prevSeq: number, curSeq: number): string => {
  const missed = curSeq - prevSeq - 1
  return missed > 0 ? `\n[${missed} chunk(s) missed]\n` : ''
}

/** One chunk inside a batched `sseChatMessage` frame: the text the hook
 *  buffered for it and the seq the WS frame carried; gap markers are derived by
 *  the reducer from the seqs, not carried in the text. */
export type BatchedChunkPart = { seq?: number; text: string }

/** The text of a batched frame that lies ABOVE a slot's chunk-seq floor, with
 *  the gap markers recomputed over the parts that survive. The reducer is the
 *  single owner of that floor (`lastChunkSeq`, raised by a snapshot's trailing
 *  streaming row and by every applied chunk); the hook only batches per
 *  animation frame and has no view of it. A part at or below the floor is text
 *  a snapshot already holds — the duplicated leading fragment seen after a
 *  reconnect or a mid-stream refresh — and is dropped; a part with no seq
 *  cannot be placed against the floor and is kept. Markers are derived HERE,
 *  from the floor and the kept seqs, rather than carried in the parts: a gap
 *  the hook saw on the wire may be exactly what the snapshot filled in, and a
 *  marker inlined at arrival would then flag a chunk that is on screen.
 *  Returns `undefined` when nothing survives so the caller leaves the slot
 *  untouched (no run-state bump, no streaming row). */
export const batchedTextAboveFloor = (parts: BatchedChunkPart[], floor: number | undefined): string | undefined => {
  const kept = parts.filter(p => p.seq === undefined || floor === undefined || p.seq > floor)
  if (kept.length === 0) return undefined
  let prev = floor
  let text = ''
  for (const p of kept) {
    if (p.seq !== undefined) {
      if (prev !== undefined) text += missedChunkMarker(prev, p.seq)
      prev = p.seq
    }
    text += p.text
  }
  return text
}

/** One `chat_message` frame as the WebSocket hook hands it to `sseChatMessage`. */
type ChatFrame = { slot: string; role: string; content: string; ts?: string; seq?: number; gen?: string; cls?: string; meta?: Record<string, unknown>; kind?: string; batched?: boolean; parts?: BatchedChunkPart[] }

/**
 * Path B (native session grid): apply a WS chat frame for a NON-active slot
 * into the per-slot store so a pane rendering that slot streams live. The
 * ACTIVE-slot path in sseChatMessage is intentionally left byte-identical
 * (zero blast radius on the main chat); this mirrors the slotActivity tool
 * pattern already used for tool/subagent events on non-active slots.
 */
function applyNonActiveFrame(
  state: ChatState,
  p: ChatFrame,
) {
  const { slot, role, ts, seq, gen, cls, meta, kind, batched, parts } = p
  let content = p.content
  if (isUnsafeKey(slot)) return  // never index a state map with __proto__/constructor/prototype
  const msgs = (state.slotMessages[safeKey(slot)] ??= [])
  const run = (state.slotRun[safeKey(slot)] ??= { state: 'idle' })
  const sa = (state.slotActivity[safeKey(slot)] ??= { toolLog: [], subagents: {} })

  const effectiveKind = kind ?? (meta?.kind as string | undefined)
  if (effectiveKind === 'stop_event') {
    const id = (meta?.id as string) ?? ''
    const idx = id ? msgs.findIndex(m => m.meta?.id === id) : -1
    const msg: ChatMessage = ensureMsgId({ role, content, cls: cls || '', ts, meta: { ...meta, kind: 'stop_event' }, kind: 'stop_event' })
    if (idx >= 0) msgs[idx] = msg
    else msgs.push(msg)
    return
  }
  if (role === '_segment') {
    finalizeTrailingStreaming(msgs)
    return
  }
  if (role === 'chunk') {
    // Replay floor, owned here. `run.lastChunkSeq` is raised by a snapshot's
    // trailing streaming row (switchSlot / refreshSlot / warmSlotCache) and by
    // every applied chunk. A batched frame carries each chunk's seq in `parts`;
    // only the parts above the floor are appended, and a frame with nothing
    // above it leaves the slot untouched. A direct (non-batched) chunk at or
    // below the floor is a replayed seq and is dropped whole. A frame from
    // another gateway generation replaces the floor first (floorForGen).
    run.lastChunkSeq = floorForGen(run.lastChunkSeq, run.lastChunkGen, gen)
    if (gen !== undefined) run.lastChunkGen = gen
    if (batched && parts) {
      const kept = batchedTextAboveFloor(parts, run.lastChunkSeq)
      if (kept === undefined) return
      content = kept
    }
    if (!batched && seq !== undefined && run.lastChunkSeq !== undefined && seq <= run.lastChunkSeq) {
      return
    }
    if (run.state === 'idle') bumpRunEpoch(state, slot)
    setRunState(run, 'streaming')
    syncOriginRun(state, slot, 'streaming')
    // Drop only the EMPTY thinking placeholder (mirror the active
    // `applyActiveFrame` path below), keeping content-bearing reasoning
    // blocks so a background pane's hydrated reasoning isn't silently deleted by
    // the next streamed chunk.
    if (msgs.some(m => m.role === 'thinking' && !m.content)) {
      const filtered = msgs.filter(m => !(m.role === 'thinking' && !m.content))
      msgs.length = 0
      msgs.push(...filtered)
    }
    let streamIdx = -1
    for (let i = msgs.length - 1; i >= 0; i--) { if (msgs[i].role === 'streaming') { streamIdx = i; break } }
    if (streamIdx >= 0) {
      const msg = msgs[streamIdx]
      // Share missedChunkMarker with the active path so the two cannot drift.
      // Skip on batched frames: `batchedTextAboveFloor` owns gap detection
      // across the chunks a batch merges — it walks the batch's `parts`, calls
      // `missedChunkMarker` between consecutive seqs and inlines the result into
      // the text it returns — while the frame itself carries only the batch's
      // LAST seq. Comparing consecutive batches' last-seqs HERE would therefore
      // treat the batch size as a gap and fabricate a false "[N chunk(s)
      // missed]" marker on every multi-chunk background-pane batch. Mirror the
      // active path, which guards the identical branch with `!batched`.
      if (!batched && seq !== undefined && run.lastChunkSeq !== undefined) {
        msg.content += missedChunkMarker(run.lastChunkSeq, seq)
      }
      msg.content += content
      msg.rawText = msg.content
    } else {
      msgs.push({ role: 'streaming', content, cls: 'msg msg-a', rawText: content, meta: { clientTs: mintMsgId() } })
    }
    if (seq !== undefined) run.lastChunkSeq = seq
    return
  }
  if (role === '_done') {
    setRunState(run, 'idle')
    run.lastChunkSeq = undefined
    syncOriginRun(state, slot, 'idle')
    for (let i = msgs.length - 1; i >= 0; i--) {
      if (msgs[i].role === 'streaming') { msgs[i].role = 'assistant'; msgs[i].rawText = msgs[i].content; break }
    }
    return
  }
  if (role === 'compacting') { if (run.state === 'idle') bumpRunEpoch(state, slot); setRunState(run, 'compacting'); syncOriginRun(state, slot, 'compacting'); return }
  // Permission rows carry request_id/tool_input inside `cls` (JSON); lift it
  // here — BEFORE the guard — so the identity comparison sees the same
  // `tool_call_id` the stored row has.
  let effectiveMeta = meta
  if (role === 'permission' && !meta?.approval_id && cls) {
    try {
      const parsed = JSON.parse(cls)
      if (parsed.request_id) {
        effectiveMeta = { ...meta, approval_id: parsed.request_id, tool_input: parsed.tool_input ?? '', is_read_only: parsed.is_read_only ?? '', ...(parsed.tool_call_id ? { tool_call_id: parsed.tool_call_id } : {}), ...(parsed.resolved ? { resolved: parsed.resolved } : {}) }
      }
    } catch { /* not JSON cls, ignore */ }
  }
  // Idempotent append — ONE chokepoint that dominates every branch below, which
  // is the point: each of those branches creates or mutates a row and returns,
  // so a guard placed after any of them is a guard some frame slips past.
  if (isRedeliveredMessage(msgs, effectiveMeta)) { state._redeliveredFramesDropped += 1; return }
  // A turn-consuming frame makes a pending stateless question card stale —
  // placed after the redelivery guard so a replayed frame cannot clear a
  // live card (see dropStaleStatelessQuestion).
  dropStaleStatelessQuestion(state, slot, role)
  // An inject row (cron, continue, auto-nudge) starts a turn like a user
  // message does — count it (see `ChatState.runEpoch`). A `/note` is also an
  // inject row but is PASSIVE: it starts no turn, so counting it would make a
  // Stop settlement captured a moment earlier read as stale and leave the pane
  // falsely busy (GPT round 10).
  if (role === 'inject' && !isNoteRow({ cls, meta })) bumpRunEpoch(state, slot)
  if (role === 'tool') {
    if (run.state === 'idle') bumpRunEpoch(state, slot)
    setRunState(run, 'tool_running')
    syncOriginRun(state, slot, 'tool_running')
    let insertIdx = msgs.length
    if (insertIdx > 0 && msgs[insertIdx - 1]?.role === 'streaming') insertIdx--
    msgs.splice(insertIdx, 0, ensureMsgId({ role, content, cls: cls || '', ts, meta }))
    return
  }
  if (role === 'thinking') {
    if (!msgs.some(m => m.role === 'thinking')) msgs.push({ role: 'thinking', content: '', cls: '', meta: { clientTs: mintMsgId() } })
    return
  }
  if (role === 'assistant') {
    for (let i = msgs.length - 1; i >= 0; i--) {
      if (msgs[i].role === 'streaming') {
        msgs[i].role = 'assistant'; msgs[i].content = content; if (ts) msgs[i].ts = ts
        // Carry the frame's meta — crucially `mid`, this row's server identity.
        // The row was minted client-side by the first `chunk` and has none until
        // now; without it a later redelivery of THIS frame is unrecognisable and
        // would overwrite whatever is streaming at that moment.
        if (meta) msgs[i].meta = { ...(msgs[i].meta || {}), ...meta }
        return
      }
    }
  }
  if (role === 'user') {
    // A steered message does not start a new turn — skip the "stale permissions"
    // cleanup so the approval bar remains visible and answerable (#1667).
    if (!meta?.steer) {
      bumpRunEpoch(state, slot)
      sa.toolLog = []
      for (const m of msgs) {
        if (m.role === 'permission' && !m.meta?.resolved) { if (m.meta) m.meta.resolved = 'rejected'; else m.meta = { resolved: 'rejected' } }
      }
    }
    // Reconcile the optimistic user bubble (appendSlotMessage) rather than
    // pushing a 2nd identical one when the server echoes the user frame (#2845).
    // Uses shared helper that scans past non-matching pipelined sends (#3898).
    const echoSendId = meta?.sendId as string | undefined
    if (echoSendId && meta?.mid) {
      if (reconcileOptimisticEcho(msgs, echoSendId, meta as Record<string, unknown>, ts)) return
    } else if (meta?.mid) {
      // Fallback: no sendId on the echo — use tail content match for paths
      // that don't generate a sendId (split-pane, queued promotions).
      const last = msgs[msgs.length - 1]
      if (last?.role === 'user' && last.content === content && !last.meta?.mid) {
        if (ts) last.ts = ts
        if (meta) last.meta = { ...(last.meta || {}), ...meta }
        return
      }
    }
  }
  msgs.push(ensureMsgId({ role, content, cls: cls || '', ts, meta: effectiveMeta, kind }))
}

/** The ACTIVE-slot half of `sseChatMessage`, kept apart from the background
 *  half above (`applyNonActiveFrame`) so the main chat's path is unchanged by
 *  it. */
function applyActiveFrame(state: ChatState, p: ChatFrame): void {
  const { slot, role, ts, seq, gen, cls, meta, kind, batched, parts } = p
  let content = p.content
  // stop_event — replace in place by id, or insert new
  const effectiveKind = kind ?? (meta?.kind as string | undefined)
  if (effectiveKind === 'stop_event') {
    const id = (meta?.id as string) ?? ''
    const idx = id ? state.messages.findIndex(m => m.meta?.id === id) : -1
    const msg: ChatMessage = ensureMsgId({ role, content, cls: cls || '', ts, meta: { ...meta, kind: 'stop_event' }, kind: 'stop_event' })
    if (idx >= 0) { state.messages[idx] = msg } else { state.messages.push(msg) }
    return
  }
  // WS segment — finalize streaming into assistant without resetting sequence or slot state
  if (role === '_segment') {
    finalizeTrailingStreaming(state.messages)
    return
  }
  // WS chunk — accumulate into streaming message, preserve rawText
  if (role === 'chunk') {
    // Replay floor, owned here. `state.lastChunkSeq` is raised by a
    // snapshot's trailing streaming row (switchSlot / refreshSlot) and by
    // every applied chunk. A batched frame carries each chunk's seq in
    // `parts`; only the parts above the floor are appended, and a frame with
    // nothing above it leaves the slot untouched. A direct (non-batched)
    // chunk at or below the floor is a replayed seq and is dropped whole.
    // A frame from another gateway generation replaces the floor first
    // (floorForGen).
    state.lastChunkSeq = floorForGen(state.lastChunkSeq, state.lastChunkGen, gen)
    if (gen !== undefined) state.lastChunkGen = gen
    if (batched && parts) {
      const kept = batchedTextAboveFloor(parts, state.lastChunkSeq)
      if (kept === undefined) return
      content = kept
    }
    if (!batched && seq !== undefined && state.lastChunkSeq !== undefined && seq <= state.lastChunkSeq) {
      return
    }
    if (state.slotState === 'idle') bumpRunEpoch(state, slot)
    state.slotState = 'streaming'
    state._wsChunkedDuringFetch = true
    // Drop only the empty "Thinking…" placeholder; keep content-bearing
    // reasoning blocks (from chat_thinking) so they persist as a collapsible
    // trace directly above the streamed answer.
    if (state.messages.some(m => m.role === 'thinking' && !m.content)) {
      state.messages = state.messages.filter(m => !(m.role === 'thinking' && !m.content))
    }
    let streamIdx = -1
    for (let i = state.messages.length - 1; i >= 0; i--) {
      if (state.messages[i].role === 'streaming') { streamIdx = i; break }
    }
    if (streamIdx >= 0) {
      const msg = state.messages[streamIdx]
      // Defensive non-batched gap detection. The live WS path always sets
      // `batched` — the useWebSocket flush buffer owns gap detection across
      // the chunks it merges and inlines the marker into each part's text —
      // so this branch only runs for a direct (test/legacy) non-batched
      // chunk dispatch. It shares missedChunkMarker with the buffer so the
      // two cannot drift.
      if (!batched && seq !== undefined && state.lastChunkSeq !== undefined) {
        msg.content += missedChunkMarker(state.lastChunkSeq, seq)
      }
      msg.content += content
      msg.rawText = msg.content
    } else {
      state.messages.push({ role: 'streaming', content, cls: 'msg msg-a', rawText: content, meta: { clientTs: mintMsgId() } })
    }
    if (seq !== undefined) state.lastChunkSeq = seq
    return
  }
  // WS done — finalize streaming into assistant, rawText preserved for reparse
  if (role === '_done') {
    state.slotState = 'idle'
    state.lastChunkSeq = undefined
    for (let i = state.messages.length - 1; i >= 0; i--) {
      if (state.messages[i].role === 'streaming') {
        const msg = state.messages[i]
        msg.role = 'assistant'
        msg.rawText = msg.content
        break
      }
    }
    state.slotRunning = false
    state.slotStopping = false
    state.slotState = 'idle'
    state.pendingTurnSlot = null
    return
  }
  // Compacting — block input, show footer indicator (no visible message)
  if (role === 'compacting') {
    if (p.slot && p.slot !== state.activeSlot) return
    if (state.slotState === 'idle') bumpRunEpoch(state, slot)
    state.slotState = 'compacting'
    state.slotRunning = true
    return
  }
  // Permission messages carry request_id/tool_input in cls (JSON) — lift into
  // meta here, BEFORE the guard, so the identity comparison sees the same
  // `tool_call_id` the stored row has.
  let effectiveMeta = meta
  if (role === 'permission' && !meta?.approval_id && cls) {
    try {
      const parsed = JSON.parse(cls)
      if (parsed.request_id) {
        effectiveMeta = { ...meta, approval_id: parsed.request_id, tool_input: parsed.tool_input ?? '', is_read_only: parsed.is_read_only ?? '', ...(parsed.tool_call_id ? { tool_call_id: parsed.tool_call_id } : {}), ...(parsed.resolved ? { resolved: parsed.resolved } : {}) }
      }
    } catch { /* not JSON cls, ignore */ }
  }
  // If this permission's tool was already rejected/stopped, mark it resolved immediately
  if (role === 'permission') {
    const tcid = (effectiveMeta?.tool_call_id as string) || ''
    if (tcid) {
      const entry = state.toolLog.findLast(e => e.type === 'tool' && e.tool_call_id === tcid)
      if (entry?.rejected) effectiveMeta = { ...effectiveMeta, resolved: 'rejected' }
    }
  }
  // Idempotent append — ONE chokepoint that dominates every branch below,
  // which is the point: each of those branches creates or MUTATES a row and
  // returns, so a guard placed after any of them is a guard some frame slips
  // past. The `assistant` branch is the sharpest case: it overwrites the
  // trailing `streaming` row, so a late redelivery of an OLD assistant frame
  // would clobber the live content of a NEW segment already streaming.
  if (isRedeliveredMessage(state.messages, effectiveMeta)) { state._redeliveredFramesDropped += 1; return }
  // A turn-consuming frame makes a pending stateless question card stale —
  // placed after the redelivery guard so a replayed frame cannot clear a
  // live card (see dropStaleStatelessQuestion).
  dropStaleStatelessQuestion(state, slot, role)
  // An inject row starts a turn like a user message does (see runEpoch);
  // a passive `/note` does not (GPT round 10).
  if (role === 'inject' && !isNoteRow({ cls, meta })) bumpRunEpoch(state, slot)
  // Tool call — update state, insert before streaming message
  if (role === 'tool') {
    if (state.slotState === 'idle') bumpRunEpoch(state, slot)
    state.slotState = 'tool_running'
    // Insert tool before any trailing streaming message so
    // chat_segment can still find and finalize it with redacted text.
    let insertIdx = state.messages.length
    if (insertIdx > 0 && state.messages[insertIdx - 1]?.role === 'streaming') {
      insertIdx--
    }
    state.messages.splice(insertIdx, 0, ensureMsgId({ role, content, cls: cls || '', ts, meta }))
    return
  }
  // Thinking — deduplicate, only keep one
  if (role === 'thinking') {
    if (state.messages.some(m => m.role === 'thinking')) return
    state.messages.push({ role: 'thinking', content: '', cls: '', meta: { clientTs: mintMsgId() } })
    return
  }
  // Replace streaming placeholder with final assistant message
  if (role === 'assistant') {
    for (let i = state.messages.length - 1; i >= 0; i--) {
      if (state.messages[i].role === 'streaming') {
        state.messages[i].role = 'assistant'; state.messages[i].content = content; if (ts) state.messages[i].ts = ts
        // Carry the frame's meta — crucially `mid`, this row's server
        // identity. The row was minted client-side by the first `chunk` and
        // has none until now; without it a later redelivery of THIS frame is
        // unrecognisable and would overwrite whatever is streaming then.
        if (meta) state.messages[i].meta = { ...(state.messages[i].meta || {}), ...meta }
        return
      }
    }
  }
  // New user message = new turn — clear activity log
  if (role === 'user') {
    // A steered message does not start a new turn — skip the "stale permissions"
    // cleanup so the approval bar remains visible and answerable (#1667).
    if (!meta?.steer) {
      bumpRunEpoch(state, slot)
      state.toolLog = []
      // Auto-resolve any stale permissions from previous turn so they don't block the new turn
      for (const m of state.messages) {
        if (m.role === 'permission' && !m.meta?.resolved) {
          if (m.meta) m.meta.resolved = 'rejected'
          else m.meta = { resolved: 'rejected' }
        }
      }
    }
    // Reconcile the optimistic user bubble rather than pushing a duplicate
    // when the server echoes the user frame (#2845). Uses shared helper that
    // scans past non-matching pipelined sends (#3898).
    const echoSendId = meta?.sendId as string | undefined
    if (echoSendId && meta?.mid) {
      if (reconcileOptimisticEcho(state.messages, echoSendId, meta as Record<string, unknown>, ts)) return
    } else if (meta?.mid) {
      // Fallback: no sendId on the echo — use tail content match for paths
      // that don't generate a sendId (split-pane, queued promotions).
      const last = state.messages[state.messages.length - 1]
      if (last?.role === 'user' && last.content === content && !last.meta?.mid) {
        if (ts) last.ts = ts
        if (meta) last.meta = { ...(last.meta || {}), ...meta }
        return
      }
    }
  }
  state.messages.push(ensureMsgId({ role, content, cls: cls || '', ts, meta: effectiveMeta, kind }))
}

export const deleteSlot = createAsyncThunk<
  string,
  string,
  // `alreadyGone` marks a close the server answered with 404: this request
  // closed nothing, so `deleteSlot.fulfilled` must not tear down the tab's
  // view state for a slot the close that did pop it may still restore.
  { fulfilledMeta: { alreadyGone: boolean } }
>(
  'chat/deleteSlot',
  async (key: string, { dispatch, getState, requestId, fulfillWithValue }) => {
    const root = getState() as RootState
    const deletedSlot = root.dashboard.slots.find(s => s.key === key)
    // Use the surface key (forward-compat alias for `mode`) so a future
    // backend that emits a distinct `slot.surface` keeps "switch to a peer
    // session" pinned to the same nav destination.
    const deletedSurface = deletedSlot ? slotSurfaceKey(deletedSlot) : ''
    // Navigate before removeSlotOptimistic to prevent a useEffect race: the
    // active slot must already name a surviving peer by the time this slot
    // leaves the list.
    //
    // What that ordering constrains is the STATE transitions, not the I/O.
    // `switchSlot.pending` assigns `activeSlot` synchronously as it is
    // dispatched, so the invariant above holds from that call — not from the
    // moment its history fetch resolves. That fetch is unbounded (the peer's
    // whole transcript, megabytes on a long session), so it is carried as a
    // promise rather than awaited here: blocking on it would hold the dismissed
    // tab on screen for the length of an unrelated conversation's load, which
    // reads as a dead close control. The peer paints from the `slotMessages`
    // cache when it has one, or from `slotLoading` behind the already-removed
    // tab when it does not.
    let navigation: Promise<unknown> | undefined
    if (root.chat.activeSlot === key) {
      const sameSurface = new Set(root.dashboard.slots.filter(s => slotSurfaceKey(s) === deletedSurface).map(s => s.key))
      const sidebarSurface = isChatPageSurface(deletedSurface)
        ? new Set(root.dashboard.slots.filter(s => isChatPageSurface(slotSurfaceKey(s))).map(s => s.key))
        : sameSurface
      // Land on the sidebar row below the closed one (above at the bottom): the
      // tab-strip landing rule, applied to the sidebar's displayed order. The
      // sidebar publishes ROW identities, and a remote-bound local session's is
      // `<instance_id>:<peer_key>`, so each row maps back to its slot key first.
      // A closed session the sidebar does not show keeps the recency pick below.
      const keyByRow = new Map(root.dashboard.slots.map(s => [s.row_identity || s.key, s.key]))
      const displayed = [...new Set((root.dashboard.sidebarOrder ?? []).map(row => keyByRow.get(row) ?? row))]
        .filter(k => k === key || sidebarSurface.has(k))
      const landing = nextActiveAfterClose(displayed, key, key)
      const prev = (landing !== key ? landing : null)
        || root.chat.slotHistory.filter(k => k !== key && sameSurface.has(k)).pop()
        || root.dashboard.slots.filter(s => s.key !== key && sameSurface.has(s.key)).map(s => s.key)[0]
      dispatch({ type: 'chat/setActiveSlot', payload: null })
      if (prev) {
        navigation = dispatch(switchSlot(prev)).unwrap().catch(() => dispatch({ type: 'chat/clearSlotState' }))
      } else {
        dispatch({ type: 'chat/clearSlotState' })
      }
    }
    dispatch(removeSlotOptimistic(key))
    // A 404 means the server no longer has this slot (a second tab or a
    // repeat close got there first): that is the end state being asked for,
    // so it completes the close instead of failing it.
    let alreadyGone = false
    try {
      await api.deleteChatSlot(key).catch((err: unknown) => {
        if (!isNotFoundError(err)) throw err
        alreadyGone = true
      })
      if (alreadyGone) {
        // This request closed nothing: another close popped the key, and that
        // close can still fail and put the slot back. Drop the hold rather than
        // confirm it, so the next authoritative list decides, and a restored
        // row shows again instead of staying hidden behind this tombstone.
        // A slot list serialized before the server popped the key may still be
        // in flight, and with the hold gone nothing else would stop its reply
        // re-adding the row: `distrustInFlight` pairs those requests with the
        // key before the release. The refetch after it is the post-pop list
        // that shows the row again if the popping close restored it.
        dispatch(releaseCloseHold({ key, requestId, distrustInFlight: true }))
        dispatch(fetchSlots())
      } else {
        // Confirm the close hold NOW, not on `fulfilled`: that action trails the
        // `await navigation` below, and a peer transcript load that outlasts the
        // in-flight cap would otherwise expire a hold whose close succeeded.
        dispatch(confirmCloseHold({ key, requestId }))
        gcSessionStorage(key)
      }
    } catch {
      // Release the close hold BEFORE refetching: this thunk's `rejected` (which
      // also releases it) fires only after the `await navigation` below, and
      // the refetch reply must not be filtered out by the hold it exists to undo.
      dispatch(releaseCloseHold({ key, requestId }))
      dispatch(fetchSlots())
      throw new Error('save failed')
    } finally {
      // Settle the peer navigation before this thunk reports back, on the
      // failure path too. Callers that await it treat resolution as "the
      // dismissal is done" and then read the store (an app agent tearing its
      // session down, the create-first-then-delete mode switch), so resolving
      // mid-fetch would hand them a half-loaded peer. Rejection is already
      // absorbed by the `.catch` above, so this cannot throw and cannot mask
      // the error being propagated.
      await navigation
    }
    return fulfillWithValue(key, { alreadyGone })
  },
)

export const loadOlderMessages = createAsyncThunk(
  'chat/loadOlder',
  async (_, { getState, rejectWithValue }) => {
    const state = (getState() as { chat: ChatState }).chat
    if (!state.activeSlot || !state.slotHasMore) return null
    if (state.slotOldestIndex <= 0) return null
    const slot = state.activeSlot
    const controller = new AbortController()
    const abort = () => controller.abort()
    claimOlderFetchAbort(abort)
    try {
      // Landing size is a LAYOUT BURST: on a phone (slow CPU, slow network)
      // a 300-row landing is a long task during which the anchor
      // compensation paints late and the reader visibly loses their place
      // ('突然加载一大堆就不在原来的位置'). Narrow viewports take smaller,
      // cheaper landings; the walk simply takes more of them.
      const isNarrow = typeof window !== 'undefined' && typeof window.matchMedia === 'function'
        && window.matchMedia('(max-width: 640px)').matches
      const walkLimit = isNarrow ? OLDER_PAGE_LIMIT : OLDER_WALK_PAGE_LIMIT
      const d = await api.chatSlotDetail(slot, walkLimit, state.slotOldestIndex, controller.signal)
      // LANDING BUFFER: the fetch overlaps the reader's gesture, but the
      // MUTATION must not -- splicing rows mid-glide races the pre-paint
      // anchor machinery against the gesture's own pixel-addressed window
      // recompute (phone rig: kilopixel per-landing jumps whose anchor
      // consume mis-bound and stood down). Hold the payload until the
      // scroller has been quiet for a beat; bounded, so a reader who never
      // pauses still gets the page (see scrollQuiet.ts).
      await whenScrollQuiet(controller.signal)
      if (controller.signal.aborted) throw new DOMException('Aborted', 'AbortError')
      return { slot, nextBefore: d.next_before || 0, messages: filterMessages(d.messages || []), hasMore: d.has_more || false, total: d.total || 0 }
    } catch (e) {
      // Rethrow a cancellation so the reducer can tell it from a real failure;
      // a genuine failure names its slot, because a switch may have moved on.
      if (isSupersededPagingRejection(e)) throw e
      return rejectWithValue({ slot })
    } finally {
      // Only clear our own handle: a newer fetch may already have replaced it.
      releaseOlderFetchAbort(abort)
    }
  },
  {
    // `loadingOlder` must be read HERE: `pending` sets it before the creator runs.
    // The cursor check blocks paging mid-switch, when it still describes the old chat.
    condition: (_, { getState }) => {
      const state = (getState() as { chat: ChatState }).chat
      if (state.loadingOlder) return false
      return state.slotCursorKey === state.activeSlot
    },
  },
)

/** Shape of the `/stop` reply this thunk reads. `info` is set only on the
 *  backend's no-op branch (`not running` / `stop already in progress`); a real
 *  stop answers a bare `{ok: true}`. */
type StopReply = { ok?: boolean; info?: string; already_stopping?: boolean; error?: string; code?: string } | null | undefined

/** A Stop press's failure, for the host that rendered the button: `null` when
 *  the request landed (a real stop, an in-flight cancel, a settled no-op, or a
 *  debounced repeat), otherwise the error message the host must SHOW (#9547
 *  round 2): a swallowed failure is indistinguishable from the dead Stop
 *  button this fix exists to remove. */
export type StopFailure = { error: string } | null

export const requestStop = createAsyncThunk<StopFailure, { slotId: string; force: boolean }>(
  'chat/requestStop',
  async ({ slotId, force }, { getState, dispatch }) => {
    const state = (getState() as { chat: ChatState }).chat
    if (!force) {
      const lastPress = state.stopPressedAt[slotId] ?? 0
      if (Date.now() - lastPress < SOFT_STOP_DEBOUNCE_MS) return null
    }
    // The turn this press is about. A `not running` answer that lands after a
    // NEWER turn started on the slot must not idle that turn.
    const epoch = state.runEpoch?.[safeKey(slotId)] ?? 0
    dispatch(chatSlice.actions.setStopPressedAt({ slotId, ts: Date.now() }))
    let reply: StopReply
    try {
      reply = (force ? await api.stopChatSlotForce(slotId) : await api.stopChatSlot(slotId)) as StopReply
    } catch (e) {
      dispatch(chatSlice.actions.setStopPressedAt({ slotId, ts: 0 }))
      return { error: e instanceof Error ? e.message : String(e) }
    }
    // A 2xx can still carry a refusal — a peer-bound slot whose crew could
    // not be reached answers `{ok: false, error, code}` — and `j()` only
    // throws on non-2xx. That is a failed stop the host must show too.
    if (reply && reply.ok === false) {
      dispatch(chatSlice.actions.setStopPressedAt({ slotId, ts: 0 }))
      return { error: reply.error || reply.code || 'stop refused' }
    }
    // The backend found no turn on the slot. Its answer is authoritative and
    // the client's busy view is what was wrong, so settle it — otherwise the
    // Stop button stays, every press repeats this no-op, and the user reads
    // it as "Stop does not work" (#9547). `already_stopping` is the other
    // no-op (a cancel already in flight) and changes nothing here.
    if (reply?.info === 'not running' && !reply.already_stopping) {
      dispatch(chatSlice.actions.settleStopNotRunning({ slot: slotId, epoch }))
    }
    return null
  },
)

const chatSlice = createSlice({
  name: 'chat',
  initialState,
  reducers: {
    ...runStateReducers,
    ...slotCacheReducers,
    ...composerCardReducers,
    ...messageReducers,
    ...queueReducers,
    ...activityReducers,
    ...subagentReducers,
    ...automationReducers,
    ...sideReducers,
    ...workflowReducers,
    ...mcpAppReducers,
    ...historyNoticeReducers,
    setPendingInput(state, action: PayloadAction<string | null>) { state.pendingInput = action.payload },
    setAgentSwitchNotice(state, action: PayloadAction<string | null>) {
      // Always create a fresh value so repeating the same refusal restarts the
      // App shell's expiry effect instead of inheriting the previous timer.
      state.agentSwitchNotice = action.payload === null ? null : { message: action.payload }
    },
    setVoicePlaying(state, action: PayloadAction<boolean>) { state.voicePlaying = action.payload },
    setVoiceAudio(state, action: PayloadAction<string | null>) { state.voiceAudio = action.payload },
    /** Ask the sidebar to reveal a session row (expand collapsed ancestor
     *  folders, scroll it into view, flash it). Consumed and cleared by
     *  ChatSidebar once it is mounted and ready — see `revealRequest`. */
    requestSlotReveal(state, action: PayloadAction<string>) { state.revealNonce += 1; state.revealRequest = { kind: 'session', target: action.payload, nonce: state.revealNonce } },
    clearSlotReveal(state) { state.revealRequest = null },
    /** Ask the sidebar to reveal a FOLDER row: make it visible, expand it and every
     *  collapsed ancestor, scroll it into view, flash it. Set by the command
     *  palette's Folders provider and the launcher's Folders group ("search a
     *  folder, land on it").
     *
     *  Writes the SAME field as `requestSlotReveal`, tagged `folder`, so the newer
     *  request replaces the older one instead of sitting beside it. Cleared by
     *  `clearSlotReveal`, which is the one consume path for both kinds. */
    requestFolderReveal(state, action: PayloadAction<string>) { state.revealNonce += 1; state.revealRequest = { kind: 'folder', target: action.payload, nonce: state.revealNonce } },
    /** Handle chat messages pushed via global SSE/WS (works after refresh). */
    sseChatMessage(state, action: PayloadAction<ChatFrame>) {
      if (action.payload.slot !== state.activeSlot) { applyNonActiveFrame(state, action.payload); return }
      applyActiveFrame(state, action.payload)
    },
  },
  extraReducers: (builder) => {
    addSlotListCases(builder)
    addSlotSwitchCases(builder)
    addSlotRefreshCases(builder)
    addLifecycleCases(builder)
    builder
      .addCase(deleteSlot.fulfilled, (state, action) => {
        // A 404 close closed nothing: the close that did pop the key can still
        // fail and restore the slot, so its caches stay until an authoritative
        // slot list (`reconcileSlotResidue`) or a `removed` frame evicts them.
        if (action.meta?.alreadyGone) return
        evictSlotState(state, action.payload)
        if (state.activeSlot === action.payload) {
          state.activeSlot = null
          state.messages = []
          state.toolLog = []
          state.subagents = {}
        }
      })
      .addCase(loadOlderMessages.pending, (state) => {
        state.loadingOlder = true
        // A retry clears the red state without re-basing the cursor, so the helper cannot.
        state.slotOlderError = false
      })
      .addCase(loadOlderMessages.fulfilled, (state, action) => {
        state.loadingOlder = false
        if (action.payload && action.payload.slot === state.activeSlot) {
          // Merge paste state into the older messages first, then prepend so
          // historical pastes re-tokenize from localStorage instead of showing
          // as fully-expanded text.
          const merged = mergePreservedPastes(state.messages, action.payload.messages)
          // Invariant, not the fix: virtualKeyFor derives a row key from the
          // message ts, so an overlapping page would reach React as a duplicate
          // key. Identity is meta.mid only -- see isRedeliveredMessage on why a
          // ts tuple cannot express this without dropping legitimate rows.
          const fresh = merged.filter(m => !isRedeliveredMessage(state.messages, m.meta))
          state.messages = [...fresh, ...state.messages]
          // Paging older is exactly when a parked block's anchor becomes loaded.
          const parked = (state.thinkingOrphans ??= {})
          const key = safeKey(action.payload.slot)
          // The payload, not state: setPagingCursor runs below, so state still holds
          // the previous page's value -- true on any page-back.
          const seated = reinsertThinkingOrphans(state.messages, parked[key] ?? [], !action.payload.hasMore)
          state.messages = seated.list
          parked[key] = seated.remaining
          setPagingCursor(state, action.payload.hasMore, action.payload.nextBefore)
        }
      })
      .addCase(loadOlderMessages.rejected, (state, action) => {
        state.loadingOlder = false
        const failed = action.payload as { slot?: string } | undefined
        if (failed?.slot === state.activeSlot) state.slotOlderError = true
      })
  },
})

export const {
  setActiveSlot, clearSlotState, setPendingInput, setAgentSwitchNotice, clearUnresumableResume, clearUndeletableHistory, setQuestionCard, clearQuestionCard, setQuestionDraft, resolveQuestionCard, setFollowupCard, clearFollowupCard, dismissFollowupItem, setFolderSuggestion, clearFolderSuggestion, ageFolderSuggestion, appendMessage, appendSlotMessage, updateStreamingMessage, finalizeAssistant,
  removeThinking, confirmOptimisticSend, markSendUnconfirmed, resolveOptimisticSteer, removeByApprovalId, resolveByApprovalId, clearPendingPermissions, setSlotRunning, setSlotStopping, settleStopNotRunning, startLocalTurn, endLocalTurn, syncSlotRunningFromServer, setSlotState, setSlotStatusDetail, setStopPressedAt, clearMessages, clearSlotCache, truncateAfterIndex, replaceMessages, hydrateSlotMessages, sseChatMessage, sseChatMessageUpdate, sseChatMessagePatchByTs, sseThinkingChunk, removeQueuedMessage, appendQueuedMessage, cancelQueuedMessage, editQueuedMessage, reorderQueuedMessages,
  sseContextUsage, setVoicePlaying, setVoiceAudio,
  toggleActivity, openActivityToTab, openActivityPanel, openActivityToTool, clearFocusToolCallId, requestSlotReveal, clearSlotReveal, requestFolderReveal, clearSubagentsForSnapshot, sseSubagentPending, markSubagentApproving, sseSubagentSpawn, sseSubagentTool, sseSubagentStalled, sseSubagentRetrying, sseSubagentDone, sseSubagentQueued,
  sseSubagentBatchUpdate, sseSubagentBatchChunks, selectSubagent, clearTerminalSubagents,
  setAutomations, sseAutomation, removeAutomation,
  sseSubagentSnapshot, sseToolActivity, sseToolResult, sseActivityEvent,
  sseMcpAppRender,
  sseWorkflowEvent, clearWorkflowRun, reconcileWorkflowRuns,
  sseSideResult, sseSideQueue, sideReleaseConsumed, sideClose, sideOptimisticAppend, sideOptimisticRollback,
} = chatSlice.actions

export { clampToolOutput, TOOL_OUTPUT_MAX_CHARS, queueEntryAttachments, type QueueEntryAttachments } from './chat/wire'
export { floorForGen, raiseChunkSeq, snapshotChunkGen, snapshotChunkSeq, transcriptTsMs } from './chat/transcript'
export {
  OLDER_PAGE_LIMIT, OLDER_WALK_PAGE_LIMIT, SLOT_DETAIL_MAX_LIMIT, PANE_HYDRATE_LIMIT, REFRESH_LIMIT_CEILING,
  slotSwitchFetchLimit, slotCoverageShortfall, countMatchedFetchLimit, isSupersededPagingRejection,
  abortActiveOlderFetch, type CoverageRow,
} from './chat/paging'
export type { FollowupItem, SideMessage, SideQueueEntry, SideState, SlotState, SlotStatusDetail, WorkflowRunProgress } from './chat/state'
export { FOLDER_SUGGESTION_MAX_TURNS, capturePendingAskId, pendingQuestionFor, shouldResolveAskOnSend } from './chat/composerCards'
export { mcpAppKey } from './chat/mcpApps'
export {
  isAwaitingSpawnApproval, selectSidebarApprovalCounts, selectSidebarSubagentCounts, selectSlotPendingSpawnApprovals,
  selectSlotSubagents, selectSlotSubagentsActive, selectSubagentActivityCount,
} from './chat/subagents'
export { WORKFLOW_TERMINAL_STATUSES, isTerminalWorkflowStatus, selectSidebarWorkflowActive, selectSidebarWorkflowActiveKeys } from './chat/workflows'
export { selectAutomationForSlot, selectSidebarAutomationRunningKeys } from './chat/automations'
export { queueEditBroadcastAt } from './chat/side'
export {
  selectActiveSlotProject, selectComposerBusy, selectContinuable, selectSendConfirmed, selectSlotMessages,
  selectSlotPendingApproval, selectSlotRunEpoch, selectSlotStreamState, selectSlotToolLog, selectTrailingSendUnconfirmed,
  selectTurnInterrupted,
} from './chat/selectors'
export { clearSwitchSlotGone, switchSlot, switchSlotNoticeCopy, type SwitchSlotArg } from './chat/slotSwitch'
export { refreshSlot, warmSlotCache } from './chat/slotRefresh'
export { createSlot, deleteHistorySession, fetchHistory, forkSlot, resumeFromHistory } from './chat/lifecycle'

export default chatSlice.reducer
