/** Side Chat state per parent slot (`slotSide`, `slotSideClosed`): the
 *  `chat.side_result` and `chat.side_queue` reducers, the optimistic bubble and
 *  its rollback, released-text hand-off to the composer, and the client-local
 *  record of queue-edit broadcasts. Isolated from the main transcript by
 *  event type and state key; see side.md. */
import type { PayloadAction } from '@reduxjs/toolkit'
import { mergeIntoDraft } from '../../utils/chatDrafts'
import type { ChatState, SideMessage, SideState } from './state'
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

export const sideReducers = {
  sseSideResult(state: ChatState, action: PayloadAction<{ slot: string; run_id: string; role: 'user' | 'assistant'; content: string; ts?: number; is_error?: boolean; final?: boolean; steer?: boolean }>) {
    const { slot, run_id, role, content, ts, is_error, final, steer } = action.payload
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
        const entry: SideMessage = { role: 'user', content, ts: tsIso, run_id, steer: true }
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
        delete row.optimistic
      } else {
        side.messages.push({ role: 'user', content, ts: tsIso, run_id })
      }
      side.lastRunId = run_id
      side.pending = true
      side.streaming = true
      return
    }
    side.pending = false
    side.streaming = !final
    if (is_error) {
      side.messages.push({ role: 'assistant', content, ts: tsIso, run_id, is_error: true })
      side.lastRunId = run_id
      return
    }
    const last = side.messages[side.messages.length - 1]
    if (last?.role === 'assistant' && last.run_id === run_id && !last.is_error) {
      if (content === last.content) return
      last.content = content.startsWith(last.content) ? content : last.content + content
      last.ts = tsIso
      return
    }
    side.messages.push({ role: 'assistant', content, ts: tsIso, run_id })
    side.lastRunId = run_id
  },
  sseSideQueue(state: ChatState, action: PayloadAction<{ slot: string; action: 'push' | 'edit' | 'cancel' | 'drain'; queue_id: string; content?: string; ts?: number; front?: boolean; steer_id?: string; raw?: boolean; suppressRelease?: boolean }>) {
    const { slot, action: kind, queue_id, content, ts, front, steer_id, raw, suppressRelease } = action.payload
    if (isUnsafeKey(slot)) return
    // A queue mutation is never a reason to resurrect a closed side.
    if (!state.slotSide[slot]) {
      if (kind !== 'push' || state.slotSideClosed[slot]) return
      const parentTurnCount = slot === state.activeSlot
        ? state.messages.filter(m => m.role === 'user' || m.role === 'assistant').length
        : 0
      state.slotSide[safeKey(slot)] = { messages: [], openedAtTurnCount: parentTurnCount, createdAt: new Date().toISOString() }
    }
    const side: SideState = state.slotSide[slot]
    if (!side.queue) side.queue = []
    const at = side.queue.findIndex(e => e.id === queue_id)
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
      else if (front) side.queue.unshift({ id: queue_id, content: content ?? '', ts: tsIso, ...(steer_id ? { steerId: steer_id } : {}), ...(raw ? { raw: true } : {}) })
      else side.queue.push({ id: queue_id, content: content ?? '', ts: tsIso, ...(steer_id ? { steerId: steer_id } : {}), ...(raw ? { raw: true } : {}) })
      return
    }
    if (at < 0) return
    if (kind === 'edit') {
      // A broadcast edit arrives scrubbed (`ws.py` redacts before sending) and carries no
      // `raw` marker. Applying it over content this client typed would replace the
      // question with `[REDACTED: credential]`, which every reader of the card — a
      // WS-driven cancel, or an HTTP cancel whose cached copy was evicted — would then
      // release into the composer. Raw content is therefore a one-way ratchet.
      if (raw) {
        side.queue[at].content = content ?? side.queue[at].content
        side.queue[at].raw = true
      } else if (!side.queue[at].raw) {
        side.queue[at].content = content ?? side.queue[at].content
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
        const released = (raw ? content : '') || side.queue[at].content || content || ''
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
        const ownsRawCopy = side.queue[at].raw === true
        if (released && (!suppressRelease || ownsRawCopy)) {
          side.releasedText = mergeIntoDraft(side.releasedText, released)
        }
      }
      side.queue.splice(at, 1)
      // Retire the id so a slower HTTP callback cannot bring it back.
      const retired = side.removedQueueIds ?? []
      retired.push(queue_id)
      // Only the recent past can still be raced by an in-flight request, so a
      // small window is enough and keeps this from growing without bound.
      side.removedQueueIds = retired.slice(-MAX_RETIRED_QUEUE_IDS)
    }
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
    side.pending = false
  },
}
