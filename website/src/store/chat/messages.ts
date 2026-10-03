/** Edits to a transcript array outside the live `chat_message` frame: the
 *  optimistic send and its confirmation, steer bubbles, streaming and final
 *  assistant text, approval rows, reasoning chunks, in-place patches by
 *  tool-call id / `mid` / `ts`, a background pane's one-time hydrate, and
 *  clears. */
import type { PayloadAction } from '@reduxjs/toolkit'
import type { ChatMessage } from '../../types'
import { isRejectedDecision } from '../../utils/approvalDecision'
import type { ChatState } from './state'
import { isUnsafeKey, safeKey } from './wire'
import { RECONCILE_WINDOW, ensureMsgId, finalizeTrailingStreaming, mintMsgId, tailNotInPage } from './transcript'
import { isOutOfBandRow, isTurnBoundaryUser, mergePreservedThinking } from './thinking'
import { retainServerTotal, seedRedactionHostsGen, setPagingCursor, writeSlotPage } from './slotCache'
import { evictMcpApps } from './mcpApps'
import { applyRowPatch, logRowPatch } from './rowPatch'

/** The `boundedRead` a background hydrate hands `retainServerTotal`. A SETTLED
 *  hydrate says which read it was, so the retained count carries its units (an
 *  unbounded hydrate counts raw rows). A RUNNING hydrate says nothing: its count is
 *  refused whichever read it was, so a later collapse at turn end is not read as a
 *  rewind. */
function hydrateBoundedRead(running: boolean | undefined, bounded: boolean | undefined): boolean | undefined {
  return running ? undefined : bounded
}

export const messageReducers = {
  appendMessage(state: ChatState, action: PayloadAction<ChatMessage>) {
    // Finalize-on-steer: a mid-turn steer bubble (ChatPage steer(), meta.steer)
    // must freeze the live streaming message BEFORE it is pushed, or the
    // chunk reducer keeps appending the rest of the segment into the stranded
    // streaming message ABOVE the bubble (stuck streaming marker at the steer
    // point). The backend cuts the segment at the same boundary (see
    // _run_chat's steer segment cut), so the frozen order matches the
    // persisted transcript and the chat_done refresh doesn't reorder it.
    const m = action.payload
    // Retiring the slot's stateless question card on this OPTIMISTIC append
    // is deliberately NOT done here: the send can still fail (offline, 5xx),
    // and the card must survive a failed send. The server retires the card
    // when the user row actually lands and announces it with
    // `question_card_resolved`, which every window — this one included —
    // applies through resolveQuestionCard.
    if (m.role === 'user' && m.meta?.steer) finalizeTrailingStreaming(state.messages)
    // Non-steer user bubbles carry a `sendId` in meta (set by ChatPage at
    // send time) that serves as both the optimistic marker and the correlation
    // ID for reconciliation. The `optimistic` flag is kept as a simple boolean
    // so the reconcile scan knows this bubble is pending confirmation.
    if (m.role === 'user' && !m.meta?.steer && m.meta?.sendId) {
      m.meta = { ...(m.meta || {}), optimistic: true }
    }
    state.messages.push(ensureMsgId(m))
  },
  /** Optimistically append a message to a specific slot's store — global
   *  `messages` when it's the active slot, else `slotMessages[slot]`. Lets a
   *  grid pane show a just-sent user message immediately in the right place. */
  appendSlotMessage(state: ChatState, action: PayloadAction<{ slot: string; message: ChatMessage }>) {
    const { slot, message } = action.payload
    if (isUnsafeKey(slot)) return
    // Same reasoning as appendMessage: no card retirement on an optimistic
    // append — the server announces the retirement once the user row lands.
    const msgs = slot === state.activeSlot ? state.messages : (state.slotMessages[safeKey(slot)] ??= [])
    // Reconcile a steer echo (server 'steer_push', meta.steer, no optimistic
    // flag) against the optimistic bubble that steer() added client-side
    // (meta.optimistic). Update it in place rather than pushing a duplicate
    // user message — mirrors the user-frame reconcile in applyMessageToArray.
    //
    // The optimistic bubble is NOT necessarily the last message: a steer is
    // by definition sent mid-turn, so streaming/thinking/tool messages keep
    // landing between the optimistic append and the WS echo. A tail-only
    // check loses that race and renders a duplicate "Steered into the
    // running turn" card. Resolution (#6075) pairs strictly by id CLASS:
    // an echo carrying a `sendId` matches by id ONLY, and an ID-LESS echo
    // pairs only with ID-LESS bubbles. The gateway serves this SPA bundle,
    // so client and gateway do not skew: an id-less echo does not mean "an
    // old gateway stripped the id" — it means the POST carried none (a
    // scene-interaction steer, a non-minting caller), i.e. a DIFFERENT send
    // whose echo can never name this tab's id-bearing bubble. Consuming
    // across classes shows the wrong message twice over: the id-bearing
    // bubble adopts the foreign echo's text, and its own later exact-id
    // echo is then suppressed by the redelivery guard. An unmatched echo
    // inserts instead — over-insert is the recoverable direction. A
    // NON-optimistic user row already carrying an id-bearing echo's id
    // means the row was ALREADY installed (the chat_done refresh can
    // replace the bubble with the persisted row before a delayed echo is
    // processed) — that echo is a redelivery and inserts nothing. Within
    // the id-less pairing, prefer exactly matching content, else the most
    // recent id-less optimistic STEER bubble (a plain optimistic user
    // message with coincidentally identical text must never be consumed;
    // server-side redaction can alter the echoed content, so an exact match
    // isn't guaranteed).
    if (message.role === 'user' && message.meta?.steer && !message.meta?.optimistic) {
      const echoSid = typeof message.meta?.sendId === 'string' && message.meta.sendId ? message.meta.sendId : ''
      const floor = Math.max(0, msgs.length - 50)
      let target: ChatMessage | undefined
      let fallback: ChatMessage | undefined
      for (let i = msgs.length - 1; i >= floor; i--) {
        const m = msgs[i]
        if (m.role !== 'user') continue
        const rowSid = typeof m.meta?.sendId === 'string' && m.meta.sendId ? m.meta.sendId : ''
        if (echoSid && rowSid === echoSid && !m.meta?.optimistic) return
        if (!m.meta?.optimistic || !m.meta?.steer) continue
        if (echoSid) {
          // Id-bearing echo: the match is exact or there is no match.
          if (rowSid === echoSid) { target = m; break }
          continue
        }
        // Id-less echo: an id-bearing bubble belongs to a send whose own
        // exact-id echo is still coming — never consume it here.
        if (rowSid) continue
        if (message.content && m.content === message.content) { target = m; break }
        if (!fallback) fallback = m
      }
      const bubble = target ?? fallback
      if (bubble) {
        if (message.content) bubble.content = message.content
        // Preserve the optimistic (client-generated) ts as meta.clientTs
        // BEFORE overwriting with the server ts. The chat renderer keys
        // rows by `meta.clientTs ?? ts`; without this stash the ts change
        // would change the React key, remounting the bubble and replaying
        // the one-shot steer entrance animation (visible flicker).
        if (message.ts && bubble.ts && message.ts !== bubble.ts) {
          bubble.meta = { ...(bubble.meta || {}), clientTs: bubble.ts }
        }
        if (message.ts) bubble.ts = message.ts
        bubble.meta = { ...(bubble.meta || {}), ...(message.meta || {}) }
        delete (bubble.meta as Record<string, unknown>).optimistic
        return
      }
      // No optimistic bubble to reconcile — this tab did not initiate the
      // steer (another tab / a scene-interaction steer). Finalize-on-steer
      // before pushing, same as appendMessage: inserting the bubble below a
      // live streaming message strands the streaming marker above it. Only
      // done on the insert path — after a reconcile a NEW post-steer
      // streaming message may already be live below the bubble, and freezing
      // it here would wrongly finalize the in-flight stream.
      finalizeTrailingStreaming(msgs)
    }
    // Optimistic steer bubble from a pane-scoped composer: same freeze as the
    // appendMessage (active-slot) path.
    if (message.role === 'user' && message.meta?.steer && message.meta?.optimistic) {
      finalizeTrailingStreaming(msgs)
    }
    // Mark non-steer user bubbles as optimistic so the sseChatMessage
    // reconcile can distinguish them from channel-replayed messages (#2845).
    if (message.role === 'user' && !message.meta?.steer && message.meta?.sendId) {
      message.meta = { ...(message.meta || {}), optimistic: true }
    }
    msgs.push(ensureMsgId(message))
  },
  updateStreamingMessage(state: ChatState, action: PayloadAction<string>) {
    const last = state.messages[state.messages.length - 1]
    if (last?.role === 'streaming') { last.content = action.payload }
    else { state.messages.push({ role: 'streaming', content: action.payload, cls: 'msg msg-a', meta: { clientTs: mintMsgId() } }) }
  },
  finalizeAssistant(state: ChatState, action: PayloadAction<string | { content: string; ts?: string }>) {
    const payload = typeof action.payload === 'string' ? { content: action.payload } : action.payload
    const last = state.messages[state.messages.length - 1]
    if (last?.role === 'streaming') { last.role = 'assistant'; last.content = payload.content; if (payload.ts) last.ts = payload.ts }
    else { state.messages.push({ role: 'assistant', content: payload.content, cls: 'msg msg-a', ts: payload.ts }) }
  },
  removeThinking(state: ChatState) { state.messages = state.messages.filter(m => m.role !== 'thinking') },
  /** Retire a bubble's "pending confirmation" state once the send's own HTTP
   *  response accepted it as an immediate turn. A correlated user echo can
   *  also confirm it; the receipt remains useful if that echo was missed.
   *  Never insert here: the user echo supplies a skipped bubble before the
   *  reply, independently of receipt timing.
   *
   *  Clears only the pending-confirmation flags and deliberately KEEPS
   *  `sendId`: a later echo needs that id to update this row in place
   *  instead of pushing a duplicate bubble.
   *
   *  Scans BOTH arrays rather than resolving the slot's own: `appendMessage`
   *  pushes into the active `messages` while `appendSlotMessage` may have used
   *  `slotMessages[slot]`, and the user can switch sessions while the POST is
   *  in flight. `sendId` is unique per send, so scanning both cannot mis-hit. */
  confirmOptimisticSend(state: ChatState, action: PayloadAction<{ slot: string; sendId: string; mid?: string }>) {
    const { slot, sendId, mid } = action.payload
    if (isUnsafeKey(slot)) return
    const confirm = (msgs: ChatMessage[] | undefined): boolean => {
      if (!msgs) return false
      const floor = Math.max(0, msgs.length - RECONCILE_WINDOW)
      for (let i = msgs.length - 1; i >= floor; i--) {
        const m = msgs[i]
        if (m.role !== 'user' || m.meta?.sendId !== sendId) continue
        const meta = { ...(m.meta || {}) }
        delete meta.optimistic
        // A receipt that arrives after all is the confirmation the deadline
        // mark said was missing.
        delete meta.deliveryUnconfirmed
        // Stamp the server-minted row id the receipt carried back. The bubble
        // was appended client-side with only a `sendId` (no server identity),
        // so either the user echo or this receipt can supply its identity.
        // The message-pin control is gated on `meta.mid`, so without it the
        // just-sent message cannot be pinned for the whole turn. Only set when
        // the row has none yet — never overwrite a `mid` a refresh already
        // reconciled (identity must not change once assigned).
        if (mid && !meta.mid) meta.mid = mid
        m.meta = meta
        return true
      }
      return false
    }
    if (!confirm(state.messages)) confirm(state.slotMessages[safeKey(slot)])
  },
  /** Record that a send's own receipt never came: the transport deadline
   *  fired (`response-late`) and no correlated echo has confirmed the row, so
   *  the bubble stays `optimistic` and only a late echo can still clear it.
   *  Stamps `meta.deliveryUnconfirmed` on that bubble; the row's pending line
   *  is drawn from THIS mark, never from `optimistic` alone, because the flag
   *  also survives a `refused` or `transport-error` send (whose error row and
   *  restored composer already say what happened) and a `queued` receipt
   *  (whose card owns the text), so a line keyed on it would claim a wait on
   *  rows nobody is waiting for. Client-minted like `optimistic`, never sent,
   *  and cleared by the same two doors (`confirmOptimisticSend`, the echo
   *  reconcile). Scans BOTH arrays as `confirmOptimisticSend` does; a row an
   *  echo already confirmed is left alone. */
  markSendUnconfirmed(state: ChatState, action: PayloadAction<{ slot: string; sendId: string }>) {
    const { slot, sendId } = action.payload
    if (isUnsafeKey(slot)) return
    const mark = (msgs: ChatMessage[] | undefined): boolean => {
      if (!msgs) return false
      const floor = Math.max(0, msgs.length - RECONCILE_WINDOW)
      for (let i = msgs.length - 1; i >= floor; i--) {
        const m = msgs[i]
        if (m.role !== 'user' || m.meta?.sendId !== sendId) continue
        if (m.meta?.optimistic) m.meta = { ...m.meta, deliveryUnconfirmed: true }
        return true
      }
      return false
    }
    if (!mark(state.messages)) mark(state.slotMessages[safeKey(slot)])
  },
  /** Resolve an optimistic steer bubble against the steer POST's own receipt.
   *
   *  `meta.steer` draws the "Steered into the running turn" badge, so it is an
   *  affirmative claim, and only `steered: true` makes it true. `queued: true`
   *  means the text sits in the slot queue, and EVERY arm reporting it has
   *  already broadcast a `queue_push` — including the turn teardown, which is
   *  why the requeued arm does not re-broadcast. That card owns the text, so
   *  the bubble is REMOVED or the same message renders twice. A receipt with
   *  neither flag raced `chat_done` onto a new turn: only the flag drops. No
   *  receipt in time: the caller takes the `queued` (drop) arm -- see below.
   *
   *  Both modes need the bubble still `optimistic` — once an echo or
   *  `confirmOptimisticSend` cleared that, the server owns the row. Scans BOTH
   *  arrays as `confirmOptimisticSend` does; `sendId` is unique per send. */
  resolveOptimisticSteer(state: ChatState, action: PayloadAction<{ slot: string; sendId: string; outcome: 'queued' | 'turn' }>) {
    const { slot, sendId, outcome } = action.payload
    if (isUnsafeKey(slot)) return
    const resolve = (msgs: ChatMessage[] | undefined): boolean => {
      if (!msgs) return false
      const floor = Math.max(0, msgs.length - RECONCILE_WINDOW)
      for (let i = msgs.length - 1; i >= floor; i--) {
        const m = msgs[i]
        if (m.role !== 'user' || m.meta?.sendId !== sendId) continue
        if (!m.meta?.optimistic) return true
        // The drop arm. Also taken for a steer whose receipt never came (the
        // transport's deadline aborted the POST and the text went back to the
        // composer): a bubble left standing would read as delivered, and a
        // late `steer_push` echo that does arrive re-creates the row from the
        // server's copy (reconcileOptimisticEcho appends when no row carries
        // the sendId). A NON-steer optimistic bubble (the pane's question-card
        // answer sent as an ordinary next turn) is dropped the same way, so a
        // queued/failed answer never leaves an orphan row beside its
        // QueueStack card or the restored composer text.
        if (outcome === 'queued') { msgs.splice(i, 1); return true }
        // The `turn` arm: the answer landed on a fresh turn. A STEER bubble
        // sheds only its `steer` badge and stays `optimistic` so its later
        // `steer_push` echo still reconciles it (unchanged). A NON-steer
        // bubble (the pane's question-card answer sent as an ordinary next
        // turn) sheds `optimistic` too -- the same effect as
        // confirmOptimisticSend, marking the row server-owned.
        const meta = { ...(m.meta || {}) }
        const wasSteer = !!meta.steer
        delete meta.steer
        if (!wasSteer) delete meta.optimistic
        m.meta = meta
        return true
      }
      return false
    }
    if (!resolve(state.messages)) resolve(state.slotMessages[safeKey(slot)])
  },
  removeByApprovalId(state: ChatState, action: PayloadAction<string>) { state.messages = state.messages.filter(m => m.meta?.approval_id !== action.payload) },
  resolveByApprovalId(state: ChatState, action: PayloadAction<{ id: string; slot?: string; decision?: string; registry?: string }>) {
    const { id, slot, registry } = action.payload
    if (!slot || isUnsafeKey(slot)) return
    const messages = slot === state.activeSlot
      ? state.messages
      : state.slotMessages[safeKey(slot)]
    const matches = messages?.filter(message => message.meta?.approval_id === id)
    const m = (registry
      ? matches?.find(message => message.meta?.registry === registry)
      : undefined) ?? matches?.[0]
    const decision = action.payload.decision || 'approved'
    // A 'stale' retirement carries no outcome (an expired wait, a 404, or a
    // reconcile snapshot that no longer lists the id), so it may only settle
    // a row that is still pending — the same only-if-pending rule as the
    // switchSlot sweep and the backend marker. The reconcile retire-loop
    // walks the pre-fetch provenance map, so a card decided while that read
    // was in flight (by a live frame or by this tab's own Allow click) is
    // retired a second time as 'stale'; without this guard that second
    // write downgraded the decision. The reverse direction stays open: a
    // real decision landing after 'stale' is new information and overwrites.
    if (m?.meta && !(decision === 'stale' && m.meta.resolved)) m.meta.resolved = decision
    // If rejected, mark the matching toolLog entry so the pill can show a rejection icon.
    // Every rejection token counts: a reject-once that missed this would leave
    // the pill unmarked, and ToolCallLine then reads its 🚫 sibling as an
    // auto-deny and paints a human refusal as a policy block.
    const toolCallId = m?.meta?.tool_call_id as string | undefined
    if (isRejectedDecision(decision) && toolCallId) {
      const log = slot === state.activeSlot
        ? state.toolLog
        : state.slotActivity[safeKey(slot)]?.toolLog ?? []
      for (let i = log.length - 1; i >= 0; i--) {
        if (log[i].type === 'tool' && log[i].tool_call_id === toolCallId) {
          log[i].rejected = true; break
        }
      }
    }
  },
  /** Mark all unresolved permission messages as resolved (e.g. when stop is pressed). */
  clearPendingPermissions(state: ChatState) {
    for (const m of state.messages) {
      if (m.role === 'permission' && !m.meta?.resolved) {
        if (m.meta) m.meta.resolved = 'rejected'
        else m.meta = { resolved: 'rejected' }
      }
    }
    // Mark all incomplete toolLog entries as rejected so pills show the right icon
    for (const e of state.toolLog) {
      if (e.type === 'tool' && e.output == null && !e.rejected) e.rejected = true
    }
  },
  clearMessages(state: ChatState) { state.messages = []; setPagingCursor(state, false, 0); state.voiceAudio = null; state.voicePlaying = false; if (state.activeSlot) delete state.thinkingOrphans?.[safeKey(state.activeSlot)]; if (state.activeSlot) evictMcpApps(state, state.activeSlot); if (state.activeSlot) writeSlotPage(state, state.activeSlot, [], false) },
  /** A server-confirmed clear for a slot that is NOT the active view. The
   *  active-slot case routes through `clearMessages`; this one exists so a
   *  background slot's cached page cannot outlive its authoritative clear --
   *  the failed-switch restore re-hydrates from that cache, and a grid pane
   *  reads it directly, so a survivor resurrects a transcript the backend
   *  already discarded (#6364 review). */
  clearSlotCache(state: ChatState, action: PayloadAction<string>) {
    const slot = action.payload
    if (isUnsafeKey(slot)) return
    writeSlotPage(state, slot, [], false)
    delete state.thinkingOrphans?.[safeKey(slot)]
    evictMcpApps(state, slot)
  },
  truncateAfterIndex(state: ChatState, action: PayloadAction<number>) { state.messages = state.messages.slice(0, action.payload) },
  replaceMessages(state: ChatState, action: PayloadAction<ChatMessage[]>) { state.messages = action.payload },
  /** The server now serves already-loaded rows differently: for every slot (a
   *  redaction host allowed or revoked, workspace-wide) or for the named ones (a
   *  reply's variant switched). Every affected slot is marked, the OPEN one
   *  included, so no later refresh, switch or warm can keep an unverified head
   *  (see `slotHeadUnverified`). The open slot is marked rather than trusted to a
   *  re-read in flight: switching away drops that re-read's answer and caches the
   *  view it would have replaced. */
  markLoadedRowsChanged(state: ChatState, action: PayloadAction<{ slot?: string } | undefined>) {
    state.loadedRowsEpoch = (state.loadedRowsEpoch ?? 0) + 1
    if (!state.slotHeadUnverified) state.slotHeadUnverified = {}
    const named = action.payload?.slot
    const keys = named !== undefined
      ? (isUnsafeKey(named) ? [] : [named])
      : [...Object.keys(state.slotMessages ?? {}), ...(state.activeSlot ? [state.activeSlot] : [])]
    for (const k of keys) {
      state.slotHeadUnverified[safeKey(k)] = state.loadedRowsEpoch
      ;(state.slotRowsChangedAt ??= {})[safeKey(k)] = state.loadedRowsEpoch
    }
  },
  /** Path B: seed a non-active slot's message history into the per-slot store
   *  (one-time hydrate on pane mount). Prepends the server history BEFORE any
   *  frames that already arrived live: applyNonActiveFrame seeds slotMessages
   *  via `??= []` on the first WS frame, so `cur` can be non-empty before this
   *  hydrate fetch resolves. A dedicated `slotHydrated` flag makes it fire
   *  exactly once, so a racing frame can't make us silently drop history.
   *
   *  One exception to "exactly once": a pane that mounts idle fetches a BOUNDED
   *  page, and the slot can start a turn before that page lands. The pane then
   *  refetches unbounded, and a flat one-shot would discard the wider result and
   *  strand the pane on 50 rows. So a bounded page may be superseded once by an
   *  unbounded one. The reverse is refused, and a superseded slot cannot upgrade
   *  again, so this cannot loop.
   *  No-op for the active slot (its mirror is already live). */
  hydrateSlotMessages(state: ChatState, action: PayloadAction<{ slot: string; messages: ChatMessage[]; hasMore?: boolean; bounded?: boolean; total?: number; running?: boolean; redactionGen?: string }>) {
    const { slot, messages, hasMore, bounded, total, running, redactionGen } = action.payload
    if (isUnsafeKey(slot)) return
    if (slot === state.activeSlot) return
    const k = safeKey(slot)
    // Only retainer that can seed a BACKGROUND slot -- the others sit behind an
    // activeSlot guard. Accept paths only: a declined page is not evidence.
    if (state.slotHydrated?.[slot]) {
      // Keep the rows the bounded page never fetched: it was written as
      // [page, ...priorRows], so everything past its length is a live tail.
      const boundedLen = state.slotPaneBounded?.[k]
      if (bounded || boundedLen === undefined) return
      const prior = state.slotMessages[k] ?? []
      // The wider page is a fresh server snapshot, so it can already carry rows
      // that tail holds -- a just-sent row persists before its send is acked.
      const tail = tailNotInPage(prior.slice(boundedLen), messages)
      // Reasoning is broadcast-only so the wider page never carries it back.
      // Scoped to the REPLACED region: `tail` already keeps the live tail's own.
      writeSlotPage(state, slot, mergePreservedThinking(prior.slice(0, boundedLen), [...messages, ...tail], messages), hasMore)
      retainServerTotal(state, slot, total, running, undefined, hydrateBoundedRead(running, bounded))
      seedRedactionHostsGen(state, redactionGen)
      return
    }
    const cur = state.slotMessages[slot] ?? []
    if (!state.slotHydrated) state.slotHydrated = {}
    state.slotHydrated[k] = true
    // Only a page write records a marker, so its presence means `cur` is a
    // loaded transcript, and prepending a bounded tail onto that reorders it.
    if (state.slotPaneHasMore?.[k] !== undefined) return
    // Seeded frames are NEWER rows appended after the page, so the page's
    // has-more still describes what precedes it; dropping it hid the marker.
    writeSlotPage(state, slot, [...messages, ...cur], hasMore, bounded ? messages.length : undefined)
    retainServerTotal(state, slot, total, running, undefined, hydrateBoundedRead(running, bounded))
    seedRedactionHostsGen(state, redactionGen)
  },
  sseChatMessageUpdate(state: ChatState, action: PayloadAction<{ slot: string; tool_call_id?: string; ts?: string; content?: string; meta?: Record<string, unknown> }>) {
    const { slot, tool_call_id: tcid, ts, content, meta } = action.payload
    if (!slot) return
    if (!tcid && !ts) return
    const patch = { slot, tcid, ts: tcid ? undefined : ts, content, meta }
    if (slot === state.activeSlot) applyRowPatch(state.messages, patch)
    const cached = state.slotMessages[slot]
    if (cached) applyRowPatch(cached, patch)
    // Logged for every slot: a read of it in flight, open or background, replays it.
    logRowPatch(state, patch)
  },
  /** Patch an existing message, identified by `mid` when the server sends one and
   * by `ts` otherwise. Used by the `chat_message_update` server event to flip an
   * mcp_oauth banner from "needs auth" to "authenticated" after kiro-cli emits
   * server_initialized, and to retire a banner a newer request superseded.
   * Patches both the active messages array and the slotMessages cache so a slot
   * the user isn't currently viewing still shows the correct banner state on
   * switch-back.
   *
   * `ts` is NOT a row identity — two restored rows can carry the same one (see
   * `meta.mid`, which exists for exactly this reason) — so a ts-keyed lookup
   * resolves the first match and two patches for two colliding rows would both
   * land on one of them, leaving the other stale. `mid` is preferred where
   * present; `ts` stays as the fallback for legacy rows written before the id
   * existed and for callers that do not send one. */
  sseChatMessagePatchByTs(state: ChatState, action: PayloadAction<{ slot: string; ts: string; mid?: string; meta?: Record<string, unknown>; content?: string }>) {
    const { slot, ts, mid, meta, content } = action.payload
    if (!slot || (!ts && !mid)) return
    const patch = { slot, mid, ts: mid ? undefined : ts, content, meta }
    if (slot === state.activeSlot) applyRowPatch(state.messages, patch)
    const cached = state.slotMessages[slot]
    if (cached) applyRowPatch(cached, patch)
    logRowPatch(state, patch)
  },
  /** A reply variant switch (`chat_variant_switch`) applied to the one row it
   *  rewrote, found by the caller (`noteVariantSwitch`): its shown text and
   *  variant index, in the open view and the cache, logged like any row patch so
   *  a read in flight across it replays it. */
  sseVariantSwitch(state: ChatState, action: PayloadAction<{ slot: string; mid?: string; ts?: string; index: number; content: string }>) {
    const { slot, mid, ts, index, content } = action.payload
    if (!slot || (!mid && !ts)) return
    const patch = { slot, mid, ts: mid ? undefined : ts, content, variantIdx: index }
    if (slot === state.activeSlot) applyRowPatch(state.messages, patch)
    const cached = state.slotMessages[slot]
    if (cached) applyRowPatch(cached, patch)
    logRowPatch(state, patch)
  },
  /** Accumulate streamed model reasoning (`chat_thinking` WS event) into a
   *  content-bearing `thinking`-role message — ONE BLOCK PER REASONING BURST.
   *  A turn that reasons, calls a tool, then reasons again therefore renders
   *  two blocks, each above the step it explains. Scanning back to the turn
   *  boundary instead appends every later burst into the FIRST burst's block,
   *  so a multi-tool turn collapsed all of its reasoning under the opening one.
   *
   *  Placement is anchored on the turn's open `streaming` row, located
   *  directly rather than inferred from the array tail: a turn's visible text
   *  accumulates into ONE row that stays open across tool calls (the backend
   *  flushes each segment without broadcasting), and reasoning belongs ABOVE
   *  it, exactly as the `tool` branch inserts ahead of it. The tail is NOT the
   *  end of the turn — an approval row, a queued bubble, a stop event, an
   *  error card and a `file` card are all appended BELOW that open row, so
   *  measuring from `length` would drop the block beneath the answer it
   *  explains. With no open row (reasoning before any text) the block appends,
   *  which is also correct: it lands after whatever opened the turn.
   *
   *  A CONFIRMED steer is injected into the running turn, so reasoning after
   *  it continues the burst it interrupted; an unconfirmed (optimistic) steer
   *  is a raced real turn and does close the burst — see isTurnBoundaryUser.
   *
   *  A `tool` row BELOW that open text row means the rows are already out of
   *  emission order: the `tool` branch steps back over a trailing `streaming`
   *  row but not over an approval row, so an approval-gated call lands beneath
   *  the text. The tool is then the turn's latest step, so the burst that
   *  preceded it is closed and the new one belongs after it — appending is the
   *  only placement that satisfies both, and it is what stops a post-tool
   *  burst being concatenated into the pre-tool block. */
  sseThinkingChunk(state: ChatState, action: PayloadAction<{ slot: string; content: string }>) {
    const { slot, content } = action.payload
    if (slot !== state.activeSlot || !content) return
    let at = state.messages.length
    for (let i = state.messages.length - 1; i >= 0; i--) {
      if (state.messages[i].role === 'streaming') { at = i; break }
    }
    for (let i = at; i < state.messages.length; i++) {
      if (state.messages[i].role === 'tool') { at = state.messages.length; break }
    }
    // Extend the burst the model is still emitting: an out-of-band row (an
    // approval, a queued bubble) and a confirmed steer both interrupt it
    // without ending it, so look through them for the open block.
    let prev = at
    while (prev > 0) {
      const m = state.messages[prev - 1]
      if (isOutOfBandRow(m) || (m.role === 'user' && !isTurnBoundaryUser(m))) { prev--; continue }
      break
    }
    const open = prev > 0 ? state.messages[prev - 1] : undefined
    if (open?.role === 'thinking') { open.content += content; return }
    state.messages.splice(at, 0, { role: 'thinking', content, cls: '', meta: { clientTs: mintMsgId() } })
  },
}
