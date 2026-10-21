/** Side Chat state per parent slot (`slotSide`, `slotSideClosed`): the
 *  `chat.side_result` and `chat.side_queue` reducers, the optimistic bubble and
 *  its rollback, released-text hand-off to the composer, and the client-local
 *  record of queue-edit broadcasts. Isolated from the main transcript by
 *  event type and state key; see side.md. */
import type { PayloadAction } from '@reduxjs/toolkit'
import { current, isDraft } from '@reduxjs/toolkit'
import type { SideSnapshot } from '../../types'
import { mergeIntoDraft } from '../../utils/chatDrafts'
import type { ChatState, SideMessage, SideQueueEntry, SideSendStatus, SideStandingNotice, SideState } from './state'
import { isUnsafeKey, safeKey } from './wire'

const MAX_RETIRED_QUEUE_IDS = 50

/** Monotonic tick, so an observation can be ordered against a request already in flight.
 *  A bare boolean cannot: it could have been set by an earlier, unrelated edit. */
let queueEditBroadcastSeq = 0
/** Nested per slot rather than keyed on a joined string: queue ids are unique only within
 *  their own sidecar, and a joined key would need a separator literal. */
const queueEditBroadcasts = new Map<string, Map<string, number>>()

function noteQueueEditBroadcast(slot: string, queueId: string): void {
  let perSlot = queueEditBroadcasts.get(slot)
  if (!perSlot) {
    perSlot = new Map<string, number>()
    queueEditBroadcasts.set(slot, perSlot)
  }
  perSlot.set(queueId, ++queueEditBroadcastSeq)
}

/** The tick at which the server was last seen broadcasting an edit for this card, or 0.
 *  Client-local: it is evidence about a request, not state worth persisting or syncing. */
export function queueEditBroadcastAt(slot: string, queueId: string): number {
  return queueEditBroadcasts.get(slot)?.get(queueId) ?? 0
}

/** Place `row` after the last row of its run, so a frame or a kept row that lands
 *  late sits with the turn it belongs to rather than under a later one. A row of a
 *  run with no rows yet goes at the end. */
function insertAfterRun(messages: SideMessage[], row: SideMessage): void {
  if (row.run_id) {
    for (let i = messages.length - 1; i >= 0; i--) {
      if (messages[i].run_id === row.run_id) {
        messages.splice(i + 1, 0, row)
        return
      }
    }
  }
  messages.push(row)
}

/** The side state a panel restored from `GET .../side` shows, merged with what
 *  frames already drew in this tab (`local`).
 *
 *  The read and the frames travel separately, so either can be ahead of the
 *  other. A row the gateway stored arrives under one id on both, and is drawn
 *  once. A row it never stores (an error notice) or had not stored when it
 *  answered (a later turn) exists only in `local`, and is kept beside its run.
 *  A question this tab has not sent yet (an optimistic row) cannot be in the
 *  read, since every send waits for it, so it is kept until its receipt or its
 *  user frame settles it.
 *  A streamed answer is kept only for a run the gateway does not know, or one
 *  this tab saw begin. Otherwise its deltas started part-way through, so the
 *  run waits for its final frame (`awaitingFinalRunId`). Every row is copied out,
 *  so no draft of the old state ends up inside the new one. */
function restoredSide(local: SideState | undefined, snapshot: SideSnapshot): SideState {
  const localRows = (local?.messages ?? []).map(row => ({ ...row }))
  const localById = new Map(localRows.filter(row => row.id).map(row => [row.id as string, row]))
  const restoredIds = new Set<string>()
  const messages: SideMessage[] = (snapshot.messages ?? []).map(m => {
    restoredIds.add(m.id)
    return localById.get(m.id) ?? {
      id: m.id, role: m.role, content: m.content, ts: m.ts,
      ...(m.run_id ? { run_id: m.run_id } : {}),
      ...(m.steer ? { steer: true } : {}),
      ...(m.is_error ? { is_error: true } : {}),
    }
  })
  const knownRuns = new Set(messages.map(m => m.run_id).filter((r): r is string => !!r))
  if (snapshot.last_run_id) knownRuns.add(snapshot.last_run_id)
  const sawStart = (runId: string) => localRows.some(m => m.run_id === runId && m.role === 'user' && !m.steer && !m.optimistic)
  const storedAnswer = (runId: string) => messages.some(m => m.run_id === runId && m.role === 'assistant' && !m.is_error && !!m.id)
  let keptOptimistic = false
  for (const row of localRows) {
    if (row.id && restoredIds.has(row.id)) continue
    if (row.optimistic) {
      keptOptimistic = true
      messages.push(row)
      continue
    }
    const streamedAnswer = row.role === 'assistant' && !row.is_error && !row.id
    if (streamedAnswer && row.run_id && knownRuns.has(row.run_id) && (storedAnswer(row.run_id) || !sawStart(row.run_id))) continue
    insertAfterRun(messages, row)
  }

  let lastRunId = snapshot.last_run_id || local?.lastRunId
  let pending = false
  let streaming = false
  let awaitingFinalRunId: string | undefined
  if (local?.lastRunId && !knownRuns.has(local.lastRunId)) {
    // A turn this tab saw begin after the gateway answered the read: its own frames describe it.
    lastRunId = local.lastRunId
    pending = !!local.pending
    streaming = !!local.streaming
  } else if (snapshot.running && snapshot.last_run_id) {
    const runId = snapshot.last_run_id
    const settled = messages.some(m => m.run_id === runId && m.role === 'assistant' && (!!m.id || !!m.is_error))
    if (!settled && sawStart(runId)) {
      pending = !!local?.pending
      streaming = !!local?.streaming
    } else if (!settled) {
      pending = true
      awaitingFinalRunId = runId
    }
  }
  // A question this tab just sent and the gateway has not announced yet keeps the
  // panel busy, as it did before the read landed.
  if (keptOptimistic) pending = true

  const removed = new Set(local?.removedQueueIds ?? [])
  const localQueue = (local?.queue ?? []).map(entry => ({ ...entry }))
  const localEntry = new Map(localQueue.map(entry => [entry.id, entry]))
  const restoredQueueIds = new Set((snapshot.queue ?? []).map(entry => entry.id))
  const localOnlyQueue = localQueue.filter(entry => !restoredQueueIds.has(entry.id))
  // A local head-insert stays ahead of the snapshot it overtook. Ordinary local
  // pushes follow the gateway's older entries, preserving both insertion rules.
  const queue: SideQueueEntry[] = [
    ...localOnlyQueue.filter(entry => entry.front),
    ...(snapshot.queue ?? [])
      .filter(entry => !removed.has(entry.id))
      .map(entry => localEntry.get(entry.id) ?? { id: entry.id, content: entry.content, ts: entry.ts }),
    ...localOnlyQueue.filter(entry => !entry.front),
  ]

  const createdAt = snapshot.created_at || local?.createdAt || new Date().toISOString()

  return {
    ...(local?.releasedText ? { releasedText: local.releasedText } : {}),
    ...(local?.sendStatus ? { sendStatus: { ...local.sendStatus } } : {}),
    ...(local?.removedQueueIds ? { removedQueueIds: [...local.removedQueueIds] } : {}),
    messages,
    ...(queue.length || local?.queue ? { queue } : {}),
    ...(lastRunId ? { lastRunId } : {}),
    pending,
    streaming,
    openedAtTurnCount: local?.openedAtTurnCount ?? 0,
    createdAt,
    countsTurnsSinceCreated: true,
    ...(awaitingFinalRunId ? { awaitingFinalRunId } : {}),
  }
}

export const sideReducers = {
  sseSideResult(state: ChatState, action: PayloadAction<{ slot: string; run_id: string; role: 'user' | 'assistant'; content: string; ts?: number; is_error?: boolean; final?: boolean; steer?: boolean; message_id?: string }>) {
    const { slot, run_id, role, content, ts, is_error, final, steer, message_id } = action.payload
    if (isUnsafeKey(slot)) return
    const tsIso = typeof ts === 'number' ? new Date(ts * 1000).toISOString() : new Date().toISOString()
    // A steer is an echo of the conversation being closed, never a request to re-open it,
    // so it is dropped for a tombstoned slot. Checked BEFORE the re-open branch below:
    // a steer carries `role === 'user'`, so that branch would clear the tombstone and
    // make the late-frame guard unreachable, filing the old steer into the next
    // conversation.
    if (steer && state.slotSideClosed[slot]) return
    // Intentional re-open (new user frame) clears the closed sentinel
    if (role === 'user' && state.slotSideClosed[slot]) {
      delete state.slotSideClosed[slot]
    }
    // Block late assistant chunks after sideClose
    if (!state.slotSide[slot] && state.slotSideClosed[slot]) return
    if (!state.slotSide[slot]) {
      const parentTurnCount = slot === state.activeSlot
        ? state.messages.filter(m => m.role === 'user' || m.role === 'assistant').length
        : 0
      state.slotSide[safeKey(slot)] = { messages: [], openedAtTurnCount: parentTurnCount, createdAt: tsIso }
    }
    const side: SideState = state.slotSide[slot]
    // A row this panel already holds: the restore read it after this frame left
    // the gateway. Nothing to draw, and the busy state now belongs to whatever
    // came after it.
    if (message_id && side.messages.some(m => m.id === message_id)) return
    // A frame for a turn that has already given way to a later one, which only a
    // restore can make arrive (it raced the read). It still adds its row, but it
    // must not clear the later turn's busy state.
    const earlierRun = run_id !== side.lastRunId && !!side.lastRunId
      && side.messages.some(m => m.run_id === run_id && m.role === 'user' && !m.steer)
    if (role === 'user') {
      if (steer) {
        // A steer joins a turn whose answer is already streaming. Land the chip
        // ABOVE that answer: the terminal frame replaces the whole assistant
        // text, so it must still match that row — putting the user bubble after
        // it would strand the reply and make the terminal frame append the full
        // text a second time.
        //
        // Locate the row by run, not by position. The steer RPC can settle after
        // the NEXT queued turn has already started, so this run's answer may no
        // longer be the tail; appending then would file an older steer below a
        // newer turn and scramble the transcript.
        const entry: SideMessage = { role: 'user', content, ts: tsIso, run_id, steer: true, ...(message_id ? { id: message_id } : {}) }
        let answerIdx = -1
        for (let i = side.messages.length - 1; i >= 0; i--) {
          const row = side.messages[i]
          if (row.role === 'assistant' && row.run_id === run_id) {
            answerIdx = i
            break
          }
        }
        if (answerIdx >= 0) {
          side.messages.splice(answerIdx, 0, entry)
        } else {
          // No answer for this run yet — the chip legitimately precedes it.
          side.messages.push(entry)
        }
        // Deliberately touches NEITHER pending/streaming NOR lastRunId. A steer
        // frame can arrive after its turn's terminal frame, or after a later turn
        // has begun; reviving busy state strands the panel (no later frame would
        // clear it) and rewriting lastRunId regresses run identity to a turn that
        // already ended. A steer never STARTS a turn, so it owns neither.
        return
      }
      // Reconcile with the optimistic bubble appended in sideOptimisticAppend,
      // found by its MARKER rather than by position. This frame can arrive after
      // the in-flight turn has already streamed assistant text, so the bubble is
      // often no longer the tail — and a positional check then pushes a second
      // bubble for the same question.
      const pendingIdx = side.messages.findIndex(
        m => m.optimistic && m.role === 'user' && m.content === content,
      )
      if (pendingIdx >= 0) {
        const row = side.messages[pendingIdx]
        row.run_id = run_id
        row.ts = tsIso
        if (message_id) row.id = message_id
        delete row.optimistic
      } else {
        side.messages.push({ role: 'user', content, ts: tsIso, run_id, ...(message_id ? { id: message_id } : {}) })
      }
      side.lastRunId = run_id
      side.pending = true
      side.streaming = true
      return
    }
    if (!is_error && !final) {
      // This run's answer began before this tab joined it, so its deltas start
      // part-way through. The final frame carries the whole text; wait for it.
      if (side.awaitingFinalRunId === run_id) return
      // A settled answer takes no more deltas. One arriving now was overtaken by
      // the restore, which already holds the stored answer.
      if (side.messages.some(m => m.role === 'assistant' && m.run_id === run_id && !m.is_error && m.id)) return
    }
    if (final && side.awaitingFinalRunId === run_id) delete side.awaitingFinalRunId
    if (!earlierRun) {
      side.pending = false
      side.streaming = !final
    }
    if (is_error) {
      insertAfterRun(side.messages, { role: 'assistant', content, ts: tsIso, run_id, is_error: true, ...(message_id ? { id: message_id } : {}) })
      if (!earlierRun) side.lastRunId = run_id
      return
    }
    const last = side.messages[side.messages.length - 1]
    if (last?.role === 'assistant' && last.run_id === run_id && !last.is_error) {
      if (message_id) last.id = message_id
      if (content === last.content) return
      last.content = final ? content : content.startsWith(last.content) ? content : last.content + content
      last.ts = tsIso
      return
    }
    side.messages.push({ role: 'assistant', content, ts: tsIso, run_id, ...(message_id ? { id: message_id } : {}) })
    if (!earlierRun) side.lastRunId = run_id
  },
  sseSideQueue(state: ChatState, action: PayloadAction<{ slot: string; action: 'push' | 'edit' | 'cancel' | 'drain'; queue_id: string; content?: string; ts?: number; front?: boolean; steer_id?: string; raw?: boolean; suppressRelease?: boolean }>) {
    const { slot, action: kind, queue_id, content, ts, front, steer_id, raw, suppressRelease } = action.payload
    if (isUnsafeKey(slot)) return
    // A queue mutation is never a reason to resurrect a closed side.
    if (!state.slotSide[slot]) {
      if (state.slotSideClosed[slot] || kind === 'edit') return
      const parentTurnCount = slot === state.activeSlot
        ? state.messages.filter(m => m.role === 'user' || m.role === 'assistant').length
        : 0
      // A terminal frame can beat the restore read. Keep only the same empty
      // shell SideChat already renders for absent state, plus its retired id.
      state.slotSide[safeKey(slot)] = { messages: [], openedAtTurnCount: parentTurnCount, createdAt: new Date().toISOString() }
    }
    const side: SideState = state.slotSide[slot]
    const queue = side.queue
    const at = queue?.findIndex(e => e.id === queue_id) ?? -1
    const retireQueueId = () => {
      const retired = side.removedQueueIds ?? []
      if (!retired.includes(queue_id)) {
        side.removedQueueIds = [...retired, queue_id].slice(-MAX_RETIRED_QUEUE_IDS)
      }
    }
    if (kind === 'push') {
      // Already drained or cancelled: this push lost the race to the frame that
      // retired it, so materialising a card would show a phantom.
      if (side.removedQueueIds?.includes(queue_id)) return
      const tsIso = typeof ts === 'number' ? new Date(ts * 1000).toISOString() : new Date().toISOString()
      // Replay-safe: a redelivered push must not double the card. It must also
      // not REWRITE it — broadcasts are redacted on the wire, so a late duplicate
      // push carries a scrubbed rendering of text already stored raw from the
      // HTTP response, and overwriting corrupts what a later cancel restores.
      // Content changes arrive as `edit`, never as a second `push`, so ignoring
      // the duplicate's content loses nothing.
      if (at >= 0) return
      // `front` mirrors the backend's own head-insert (a requeued steer, or an
      // entry whose dispatch failed). Appending it instead would show a
      // different next question than the backend will actually run.
      // `steer_id`, when present, says this card is a steer the backend could not
      // confirm and requeued. Kept on the entry because the card's id is new to
      // the client, so this is its only handle for matching the raw text it holds.
      const targetQueue = side.queue ?? (side.queue = [])
      if (front) targetQueue.unshift({ id: queue_id, content: content ?? '', ts: tsIso, front: true, ...(steer_id ? { steerId: steer_id } : {}), ...(raw ? { raw: true } : {}) })
      else targetQueue.push({ id: queue_id, content: content ?? '', ts: tsIso, ...(steer_id ? { steerId: steer_id } : {}), ...(raw ? { raw: true } : {}) })
      return
    }
    if (at < 0) {
      if (kind === 'cancel' || kind === 'drain') retireQueueId()
      return
    }
    if (!queue) return
    if (kind === 'edit') {
      // A broadcast edit arrives scrubbed (`ws.py` redacts before sending) and carries no
      // `raw` marker. Applying it over content this client typed would replace the
      // question with `[REDACTED: credential]`, which every reader of the card — a
      // WS-driven cancel, or an HTTP cancel whose cached copy was evicted — would then
      // release into the composer. Raw content is therefore a one-way ratchet.
      if (raw) {
        queue[at].content = content ?? queue[at].content
        queue[at].raw = true
      } else if (!queue[at].raw) {
        queue[at].content = content ?? queue[at].content
      } else {
        // Swallowed on purpose — but this frame is the only proof the server applied an
        // edit to a card this client owns. Recorded so an editor whose HTTP response never
        // arrived can tell "the edit landed" from "the edit failed": restoring the text in
        // the first case leaves the question both queued and in the composer.
        noteQueueEditBroadcast(slot, queue_id)
      }
    }
    else {
      // A cancel releases the entry's text: it is gone from the queue and gone
      // from the server, so the composer is the only place left to hold it.
      // Stashed here rather than restored by the caller because BOTH
      // convergence paths land in this reducer — the HTTP response and the
      // `chat.side_queue` frame — and a lost HTTP response must not mean lost
      // text. The panel drains and clears it, so it releases exactly once.
      if (kind === 'cancel') {
        // Prefer the card's OWN content over the frame's. Broadcast payloads are
        // redacted on the wire (`ws.py` scrubs credentials before sending), while
        // the card was populated from the raw text the user typed via the HTTP
        // response. Taking the frame first handed the composer a permanently
        // redacted question — the user would have to retype the secret, or not
        // notice and send `[REDACTED: credential]` as their prompt.
        //
        // The frame remains the fallback: if its push never populated a card
        // (HTTP lost and only the WS frame arrived) a redacted release still
        // beats losing the question entirely.
        // `raw` marks content the SUBMITTING client vouches for as unredacted. It
        // outranks the card because the card can itself be a redacted broadcast: the
        // edit endpoint broadcasts through the scrubber, and the edit action sets
        // content unconditionally, so an edited credential-bearing card holds the
        // scrubbed copy. Card next (it is raw whenever an HTTP response populated it),
        // frame last so a lost HTTP response still beats losing the question.
        const released = (raw ? content : '') || queue[at].content || content || ''
        // ACCUMULATE, never assign. Two cancellations can both settle before the
        // panel's effect consumes this field, and an assignment would drop the
        // first one's text for good — the exact loss this whole feature exists to
        // prevent. The panel merges the accumulated value into the composer as a
        // unit and clears it, so the user edits both questions rather than
        // silently losing one.
        // Another tab's cancel: drop the card here and normally leave the question to the
        // tab that cancelled, so one cancellation does not paste the same question into
        // every open dashboard.
        //
        // EXCEPT when this tab holds the unredacted copy. `raw` means the content came from
        // what the user typed here, and the cancelling tab only ever has the scrubbed
        // broadcast — so staying quiet would drop the only good copy of the question and
        // leave a redacted one behind. Owning the text outranks owning the click.
        const ownsRawCopy = queue[at].raw === true
        if (released && (!suppressRelease || ownsRawCopy)) {
          side.releasedText = mergeIntoDraft(side.releasedText, released)
        }
      }
      queue.splice(at, 1)
      // Retire the id so a slower HTTP callback cannot bring it back.
      retireQueueId()
    }
  },
  /** Set or clear (`null`) parts of the slot's send status. */
  sideSendStatus(state: ChatState, action: PayloadAction<{ slot: string; error?: string | null; notice?: SideStandingNotice | null }>) {
    const { slot, error, notice } = action.payload
    if (isUnsafeKey(slot)) return
    // A status is the outcome of a send the user just made, so a panel that
    // has no state yet (a FIRST send into a fresh side panel, whose transcript
    // is only ever created lazily by the first SSE frame) gets one here --
    // otherwise the very first send's "not confirmed" notice would be dropped
    // on the floor, and that is exactly the send with no row to fall back on.
    // Clearing nothing is fine when there is nothing to clear.
    // Optional chaining on purpose: this is dispatched from ChatPage's slash
    // path too, whose test stores seed only the fields they exercise.
    if (!state.slotSide?.[slot]) {
      if (error == null && notice == null) return
      if (state.slotSideClosed) delete state.slotSideClosed[slot]
      const parentTurnCount = slot === state.activeSlot
        ? (state.messages ?? []).filter(m => m.role === 'user' || m.role === 'assistant').length
        : 0
      state.slotSide ??= {}
      state.slotSide[safeKey(slot)] = { messages: [], openedAtTurnCount: parentTurnCount, createdAt: new Date().toISOString() }
    }
    const side = state.slotSide[slot]
    const next: SideSendStatus = { ...side.sendStatus }
    if (error !== undefined) { if (error === null) delete next.error; else next.error = error }
    if (notice !== undefined) { if (notice === null) delete next.notice; else next.notice = notice }
    if (next.error === undefined && next.notice === undefined) delete side.sendStatus
    else side.sendStatus = next
  },
  sideReleaseConsumed(state: ChatState, action: PayloadAction<{ slot: string; consumed: string }>) {
    const { slot, consumed } = action.payload
    const side = state.slotSide[slot]
    if (!side) return
    const current = side.releasedText ?? ''
    // Compare-and-clear, never a blind delete. A cancel can append to this
    // buffer between the consumer's render and its effect, and deleting the
    // whole field then discards text the consumer never saw. Keep whatever was
    // appended after the snapshot it actually drained.
    if (current === consumed || !current.startsWith(consumed)) {
      delete side.releasedText
      return
    }
    side.releasedText = current.slice(consumed.length).replace(/^\s+/, '')
  },
  sideClose(state: ChatState, action: PayloadAction<string>) {
    delete state.slotSide[action.payload]
    if (isUnsafeKey(action.payload)) return
    state.slotSideClosed[safeKey(action.payload)] = true
    // Closed here, so this page knows the gateway holds nothing for the slot: a
    // restore read still in flight describes the conversation just closed.
    state.slotSideRestored ??= {}
    state.slotSideRestored[safeKey(action.payload)] = true
  },
  /** Settle a slot's panel against the gateway's own copy of its side
   *  conversation (`GET .../side`), once per page: what a reload had dropped
   *  comes back, merged with any frames that reached this tab first. */
  sideRestore(state: ChatState, action: PayloadAction<{ slot: string; snapshot: SideSnapshot }>) {
    const { slot, snapshot } = action.payload
    if (isUnsafeKey(slot)) return
    state.slotSideRestored ??= {}
    if (state.slotSideRestored[slot]) return
    state.slotSideRestored[safeKey(slot)] = true
    if (!snapshot.open || state.slotSideClosed?.[slot]) return
    const draft = state.slotSide[slot]
    const local = draft && isDraft(draft) ? current(draft) : draft
    state.slotSide[safeKey(slot)] = restoredSide(local, snapshot)
  },
  sideOptimisticAppend(state: ChatState, action: PayloadAction<{ slot: string; message: SideMessage }>) {
    const { slot, message } = action.payload
    if (isUnsafeKey(slot)) return
    if (state.slotSideClosed[slot]) delete state.slotSideClosed[slot]
    if (!state.slotSide[slot]) {
      const parentTurnCount = slot === state.activeSlot
        ? state.messages.filter(m => m.role === 'user' || m.role === 'assistant').length
        : 0
      state.slotSide[safeKey(slot)] = { messages: [], openedAtTurnCount: parentTurnCount, createdAt: message.ts }
    }
    const side = state.slotSide[slot]
    side.messages.push({ ...message, optimistic: true })
    side.pending = true
  },
  sideOptimisticRollback(state: ChatState, action: PayloadAction<string>) {
    const side = state.slotSide[action.payload]
    if (!side) return
    // By marker, not position: popping "whatever is last" removed a real frame
    // once an in-flight turn's assistant text had landed on top of the bubble.
    const idx = side.messages.findIndex(m => m.optimistic && m.role === 'user')
    if (idx >= 0) side.messages.splice(idx, 1)
    // A restored run still waiting for its final frame keeps the busy state,
    // unless a later turn has begun since: turns run one at a time, so it ended.
    if (!side.awaitingFinalRunId || side.awaitingFinalRunId !== side.lastRunId) side.pending = false
  },
}
