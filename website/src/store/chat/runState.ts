/** The active-slot handover and the run state both halves of a slot carry:
 *  the active mirror (`slotRunning` / `slotState` / `slotStopping`), the keyed
 *  `slotRun` entry with its receipt tick, the turn-start epoch, the failed-
 *  switch origin snapshot, and the navigation MRU. */
import type { PayloadAction } from '@reduxjs/toolkit'
import type { ChatState, SlotState, SlotStatusDetail } from './state'
import { isUnsafeKey, safeKey } from './wire'
import { finalizeTrailingStreaming } from './transcript'
import { parkActiveTranscript, setPagingCursor } from './slotCache'

export function pushHistory(history: string[], key: string): string[] {
  const deduped = history.filter(k => k !== key)
  deduped.push(key)
  return deduped.length > 50 ? deduped.slice(-50) : deduped
}

/** Mirror a NON-ACTIVE slot's run transition into the failed-switch origin
 *  snapshot, when that slot is the origin. The snapshot is captured by
 *  `switchSlot.pending` and applied verbatim by `rejected`'s restore; without
 *  this event-time write, a turn that settles mid-switch through a SAME-VALUE
 *  round trip (idle -> [runs and completes] -> idle for a queued turn) is
 *  indistinguishable from "never moved" after the fact, and the restore would
 *  resurrect the stale busy state (#6364 review). Every `slotRun` state
 *  writer for non-active slots routes through here, so the snapshot ages the
 *  same way the per-slot entry does. */
export function syncOriginRun(state: ChatState, slot: string, runState: SlotState): void {
  const o = state.slotSwitchOrigin
  if (!o || safeKey(o.key) !== safeKey(slot)) return
  o.run = { state: runState, running: runState !== 'idle', stopping: runState === 'stopping' }
}

/** Count one turn START for `slot` (see `ChatState.runEpoch`). */
export function bumpRunEpoch(state: ChatState, slot: string | null): void {
  if (!slot || isUnsafeKey(slot)) return
  if (!state.runEpoch) state.runEpoch = {}
  const k = safeKey(slot)
  state.runEpoch[k] = (state.runEpoch[k] ?? 0) + 1
}

/** The ONE writer of an OBSERVED `slotRun` state transition (see the field's
 *  doc on `ChatState.slotRun`): the write and its receipt-tick bump are a
 *  single operation, so the "every observed writer bumps" invariant is
 *  structural rather than a convention each new writer has to remember -- a
 *  writer that assigned `state` directly would be invisible to
 *  `warmSlotCache.fulfilled`. A same-value write (idle over idle) bumps too:
 *  that is exactly the transition a snapshot cannot see on its own. Callers:
 *  the live frame writers, the two background settlements, and the active
 *  mirror's hand-back. A warm's own write goes through `applyWarmRunState`. */
export function setRunState(run: { state: SlotState; tick?: number }, next: SlotState): void {
  run.state = next
  run.tick = (run.tick ?? 0) + 1
}

/** A warm's run-state write: records the verdict and the warm's own order
 *  (`runWarmSeq`) WITHOUT consuming the receipt tick. The tick orders a
 *  snapshot against observed transitions; a snapshot is not one, and two
 *  warms for one slot resolve in any order -- the older one landing first
 *  must not make the newer one read as stale (that ordering is `runWarmSeq`'s,
 *  checked by the caller). Also used to record a verdict that changed nothing
 *  (a running snapshot over a busier entry), so an older warm cannot land
 *  after it and overwrite. */
export function applyWarmRunState(run: { state: SlotState; runWarmSeq?: number }, next: SlotState | null, warmSeq: number | undefined): void {
  if (next !== null) run.state = next
  if (typeof warmSeq === 'number') run.runWarmSeq = Math.max(run.runWarmSeq ?? 0, warmSeq)
}

/** THE way `activeSlot` moves to `target`: hands the outgoing slot's run
 *  mirror back to its keyed entry, assigns, and records the entry epoch.
 *  Fused into one setter so a future `activeSlot` writer cannot skip the
 *  hand-back -- the same structural shape `setRunState` gives the tick bump.
 *
 *  Why the hand-back exists: while a slot is active every frame writes the
 *  mirror (`slotRunning` / `slotState`) and not its `slotRun` entry, so
 *  without it the entry keeps whatever its last BACKGROUND frame wrote: a
 *  turn that ended on screen leaves the entry busy, and a warm dispatched for
 *  the slot BEFORE it became active cannot see that its run state moved --
 *  the active writers never touched the entry's tick -- so a stale
 *  `running: true` fulfillment would relock the finished pane. The hand-back
 *  is therefore an OBSERVED write of the entry (`setRunState`, tick bumped)
 *  -- but only when the mirror observed something: a mirror whose state
 *  equals the entry's and whose `runEpoch` is unchanged since entry (a
 *  provisional switch in and straight out) is not an observation, and
 *  consuming the tick for it would discard a pending warm that is still the
 *  newest view. A same-value round trip always counts a turn start, so the
 *  epoch check tells it apart from "never moved".
 *
 *  A running mirror that has not streamed yet hands over 'streaming', the
 *  reading the tick-ordered warm gives a turn with no frame -- EXCEPT while
 *  that turn is still an unconfirmed local send (`pendingTurnSlot`): the POST
 *  may yet come back refused, and the `endLocalTurn` that follows clears only
 *  the mirror, so a busy entry parked here would keep the background pane's
 *  indicator on and its composer locked for a turn that never started. Such a
 *  send parks idle; its first frame (or a warm that finds it running)
 *  promotes the entry through the ordered writers.
 *
 *  Two `activeSlot` writers are deliberately NOT routed here, and leave no
 *  slot worth handing back to: `deleteSlot.fulfilled` (the outgoing slot was
 *  just evicted; a hand-back would recreate its entry) and the
 *  `switchSlot.rejected` origin restore (the slot being left is the 404'd
 *  target, which has no entry and never will). */
export function enterActiveSlot(state: ChatState, target: string | null): void {
  const outgoing = state.activeSlot
  if (outgoing !== null && outgoing !== target && !isUnsafeKey(outgoing)) {
    const runs = (state.slotRun ??= {})
    const entryState = runs[safeKey(outgoing)]?.state ?? 'idle'
    const unconfirmedSend = state.pendingTurnSlot === outgoing && state.slotState === 'idle'
    const mirrorState: SlotState = state.slotRunning && !unconfirmedSend ? (state.slotState === 'idle' ? 'streaming' : state.slotState) : 'idle'
    const observed = mirrorState !== entryState
      || (state.runEpoch?.[safeKey(outgoing)] ?? 0) !== (state.activeRunEpochAtEntry ?? 0)
    if (observed) setRunState(runs[safeKey(outgoing)] ??= { state: 'idle' }, mirrorState)
  }
  state.activeSlot = target
  state.activeRunEpochAtEntry = target !== null && !isUnsafeKey(target) ? (state.runEpoch?.[safeKey(target)] ?? 0) : 0
}

export const runStateReducers = {
  setActiveSlot(state: ChatState, action: PayloadAction<string | null>) {
    // New Chat clears the selection here before `createSlot` lands, so this is
    // the last point at which the chat being left can be cached.
    if (action.payload !== state.activeSlot) parkActiveTranscript(state)
    enterActiveSlot(state, action.payload); state.slotState = 'idle'; state.pendingTurnSlot = null
  },
  clearSlotState(state: ChatState) { state.messages = []; state.toolLog = []; state.subagents = {}; state.activityTab = 'changes'; state.slotRunning = false; state.slotStopping = false; state.slotState = 'idle'; setPagingCursor(state, false, 0); state.loadingOlder = false; state.lastChunkSeq = undefined; state.lastChunkGen = undefined; state._wsChunkedDuringFetch = false; state.slotStatusDetail = {}; state.voicePlaying = false; state.voiceAudio = null; if (state.activeSlot) delete state.pendingQuestions?.[state.activeSlot]; state.pendingTurnSlot = null },
  setSlotRunning(state: ChatState, action: PayloadAction<boolean>) {
    state.slotRunning = action.payload
    if (!action.payload) state.pendingTurnSlot = null
  },
  /** Optimistically start a turn for `slot` after a local send. Marks it
   *  pending so the slots-sync won't clobber running=true before the server
   *  catches up. Only the active slot drives the visible footer. */
  startLocalTurn(state: ChatState, action: PayloadAction<string>) {
    const slot = action.payload
    state.pendingTurnSlot = slot
    bumpRunEpoch(state, slot)
    if (slot === state.activeSlot) state.slotRunning = true
  },
  /** A turn the gateway started on `slot` at this tab's request, with no
   *  transcript row to announce it (Mate's first greeting). Until its first
   *  live frame the slot reads idle, so a send made meanwhile would mint an
   *  optimistic bubble the server then queues: the user's words drawn twice.
   *  `epoch` is the slot's turn count when the request was made; any frame
   *  since has already moved the run, so a stale answer changes nothing. */
  startServerTurn(state: ChatState, action: PayloadAction<{ slot: string; epoch: number }>) {
    const { slot, epoch } = action.payload
    if (isUnsafeKey(slot)) return
    if ((state.runEpoch?.[safeKey(slot)] ?? 0) !== epoch) return
    bumpRunEpoch(state, slot)
    if (slot === state.activeSlot) {
      state.pendingTurnSlot = slot
      state.slotRunning = true
      return
    }
    const run = (state.slotRun[safeKey(slot)] ??= { state: 'idle' })
    if (run.state !== 'idle') return
    setRunState(run, 'streaming')
    syncOriginRun(state, slot, 'streaming')
  },
  /** The inverse of `startLocalTurn` for a send that did NOT start a turn
   *  (refused, or never left). Slot-keyed like its counterpart: only the slot
   *  the send was for loses its pending mark, and only when that slot is the
   *  active one does the visible footer change -- a failure that lands after
   *  the user switched to a RUNNING session must not clear that session's
   *  running state (which `setSlotRunning(false)` would). */
  endLocalTurn(state: ChatState, action: PayloadAction<string>) {
    const slot = action.payload
    if (state.pendingTurnSlot === slot) state.pendingTurnSlot = null
    if (slot === state.activeSlot) state.slotRunning = false
  },
  /** Reconcile the active slot's running state from a WS slots broadcast.
   *  running=true is always trusted (also catches Slack/cron-initiated turns);
   *  running=false is ignored while a local turn is pending confirmation, since
   *  the snapshot may predate the send. Turn end is owned by _done/refreshSlot. */
  syncSlotRunningFromServer(state: ChatState, action: PayloadAction<{ slot: string; running: boolean; stopping: boolean; epoch?: number }>) {
    const { slot, running, stopping, epoch } = action.payload
    if (slot !== state.activeSlot) {
      // A BACKGROUND slot (a member DM thread, a split pane) keeps its run
      // state in `slotRun`, written only by ordered live frames (chunk /
      // tool -> busy, _done -> idle). Nothing else ever idled it: a `_done`
      // that never reached this tab — a turn that died with the gateway, a
      // frame lost across a socket drop — left the pane busy for good, so
      // its composer kept offering a Stop button for a turn the backend had
      // long finished, and every press came back `not running` (#9547).
      // The slots snapshot IS the server's answer, so take the idle
      // direction from it. Only that direction: the running direction stays
      // with the live frames and the tick-ordered warm: a slots broadcast
      // has no dispatch point to order its snapshot against (see
      // warmSlotCache.fulfilled for the ordering a promotion needs).
      if (isUnsafeKey(slot)) return
      if (running) return
      // The snapshot answered about the turn the caller OBSERVED running.
      // A turn that started since — its first live frame bumped the epoch —
      // is not that turn, and idling it here would finalize its streaming
      // row mid-reply and split it (GPT round 7). Same guard as
      // `settleStopNotRunning`.
      if (epoch !== undefined && (state.runEpoch?.[safeKey(slot)] ?? 0) !== epoch) return
      // Optional: tests and older persisted shapes preload a partial state.
      const run = state.slotRun?.[safeKey(slot)]
      if (!run || run.state === 'idle') return
      // `stopping` is ignored on purpose: a slot that is not running has
      // nothing left to stop, whatever flag the cancel left behind.
      setRunState(run, 'idle')
      run.lastChunkSeq = undefined
      syncOriginRun(state, slot, 'idle')
      // The `_done` this settlement stands in for would also have finalized
      // the trailing streaming row; a reply left as `streaming` hides its
      // final-only rendering and actions (GPT round 3).
      finalizeTrailingStreaming(state.slotMessages?.[safeKey(slot)] ?? [])
      return
    }
    if (running) {
      if (!state.slotRunning) bumpRunEpoch(state, slot)
      state.slotRunning = true
      state.slotStopping = stopping
      state.pendingTurnSlot = null
    } else if (state.pendingTurnSlot !== slot) {
      state.slotRunning = false
      state.slotStopping = stopping
    }
    // Pending turn: ignore both fields so a leftover stopping=true from a
    // prior turn can't falsely show a "stopping" state on the new turn.
  },
  setSlotStopping(state: ChatState, action: PayloadAction<boolean>) { state.slotStopping = action.payload },
  /** The backend answered a Stop press with `not running`: nothing is in
   *  flight on that slot, so whatever made this tab think otherwise is
   *  stale. Settle the client's own view to match, on whichever path holds
   *  it (the active mirror or a background slot's `slotRun`), so the
   *  composer stops offering a Stop button that can never do anything and
   *  the press has a visible result (#9547). */
  settleStopNotRunning(state: ChatState, action: PayloadAction<{ slot: string; epoch?: number }>) {
    const { slot, epoch } = action.payload
    if (isUnsafeKey(slot)) return
    // A turn that STARTED after the press was made is not the one the
    // backend answered about: a delayed reply must not idle it.
    if (epoch !== undefined && (state.runEpoch?.[safeKey(slot)] ?? 0) !== epoch) return
    if (slot === state.activeSlot) {
      // A send still awaiting its first frame owns this slot's running state:
      // the backend answered "not running" because the turn had not been
      // registered yet, not because it is gone. Settling here would reopen
      // the composer mid-send and invite a duplicate turn; the same guard
      // syncSlotRunningFromServer applies to a stale snapshot applies to this
      // answer. startLocalTurn/endLocalTurn and the first live frame own the
      // mark's lifecycle.
      if (state.pendingTurnSlot === slot) return
      state.slotRunning = false
      state.slotStopping = false
      state.slotState = 'idle'
      state.lastChunkSeq = undefined
      finalizeTrailingStreaming(state.messages)
      return
    }
    const run = state.slotRun?.[safeKey(slot)]
    if (!run || run.state === 'idle') return
    setRunState(run, 'idle')
    run.lastChunkSeq = undefined
    syncOriginRun(state, slot, 'idle')
    // Stand-in for the `_done` that never came: finalize the trailing
    // streaming row as that frame would have (GPT round 3).
    finalizeTrailingStreaming(state.slotMessages?.[safeKey(slot)] ?? [])
  },
  setStopPressedAt(state: ChatState, action: PayloadAction<{ slotId: string; ts: number }>) {
    if (isUnsafeKey(action.payload.slotId)) return
    state.stopPressedAt[safeKey(action.payload.slotId)] = action.payload.ts
  },
  setSlotState(state: ChatState, action: PayloadAction<SlotState>) { state.slotState = action.payload },
  /** Replace a slot's live status line wholesale. A `tool` phase may carry the
   *  `toolCallId` it describes so a later refinement of the SAME call can be
   *  merged into it (see the `tool_call` case in useWebSocket) without a
   *  refinement of one call inheriting a sibling's purpose when tools run in
   *  parallel. */
  setSlotStatusDetail(state: ChatState, action: PayloadAction<SlotStatusDetail & { slot: string }>) {
    const { slot, ...detail } = action.payload
    if (isUnsafeKey(slot)) return
    state.slotStatusDetail[safeKey(slot)] = detail
  },
}
