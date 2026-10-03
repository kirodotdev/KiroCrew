/** Slot-scoped read selectors (a pane reads its own slot, falling back to the
 *  active mirror when it IS the active slot), the composer's busy rule, and the
 *  Continue / Resume predicates that mirror the backend's `_is_interrupted` /
 *  `is_turn_interrupted`. */
import type { RootState } from '../index'
import type { ChatMessage, ToolActivity } from '../../types'
import { slotIsRemoteBound } from '../dashboardSlice'
import { isSystemNoticeKind } from '../../lib/systemNotice'
import { isStopEvent } from '../../lib/stopEvent'
import { TURN_INJECT_KINDS } from './turnInject'
import type { SlotState } from './state'
import { safeKey } from './wire'
import { selectSlotSubagentsActive } from './subagents'

/** Path B selectors: read a slot's messages / stream-state, falling back to the
 *  global active mirror when the slot IS the currently-active one. */
const EMPTY_MESSAGES: ChatMessage[] = []
export const selectSlotMessages = (state: RootState, slot: string): ChatMessage[] =>
  slot === state.chat.activeSlot ? state.chat.messages : (state.chat.slotMessages[slot] ?? EMPTY_MESSAGES)
/** Only a server-confirmed row for THIS send proves delivery, even if the POST
 *  subsequently fails. An optimistic bubble or identical text proves nothing. */
export const selectSendConfirmed = (state: RootState, slot: string, sendId: string): boolean =>
  selectSlotMessages(state, slot).some(m => m.role === 'user' && m.meta?.sendId === sendId && !m.meta?.optimistic)
export const selectSlotStreamState = (state: RootState, slot: string): SlotState =>
  slot === state.chat.activeSlot ? state.chat.slotState : (state.chat.slotRun[slot]?.state ?? 'idle')
/** The turn-start count for `slot` (see `ChatState.runEpoch`): the identity a
 *  settlement captures so a late answer about one turn cannot idle the next. */
export const selectSlotRunEpoch = (state: RootState, slot: string): number =>
  state.chat.runEpoch?.[safeKey(slot)] ?? 0

const EMPTY_TOOLLOG: ToolActivity[] = []
/** Per-slot tool log, falling back to the global active mirror. */
export const selectSlotToolLog = (state: RootState, slot: string | null): ToolActivity[] =>
  slot && slot !== state.chat.activeSlot ? (state.chat.slotActivity[slot]?.toolLog ?? EMPTY_TOOLLOG) : state.chat.toolLog

/** Per-slot pending tool-approval (unresolved permission after the slot's last
 *  user message) — slot-aware version of ChatInput's old selectPendingApproval,
 *  so each grid pane's approval bar reflects ITS slot, not the global active one. */
export const selectSlotPendingApproval = (state: RootState, slot: string | null): ChatMessage | null => {
  const msgs = slot ? selectSlotMessages(state, slot) : state.chat.messages
  // Find the last NON-steer user message — steered messages don't start a new
  // turn, so they must not hide a pending approval bar (#1667).
  let lastUserIdx = -1
  for (let i = msgs.length - 1; i >= 0; i--) { if (msgs[i].role === 'user' && !msgs[i].meta?.steer) { lastUserIdx = i; break } }
  for (let i = msgs.length - 1; i > lastUserIdx; i--) {
    const m = msgs[i]
    if (m.role === 'permission' && !m.meta?.resolved && m.meta?.approval_id) return m
  }
  return null
}

/**
 * Single source of truth for "is this slot's composer busy" — the signal that
 * queues the next message (busy affordance) and skips the optimistic user
 * bubble (the backend returns a "queued" message instead, so an optimistic
 * bubble would render a duplicate). Busy = main turn running OR background
 * sub-agents running, with two redundant sub-agent signals OR'd
 * (conservative): the live WS-derived signal (real-time, self-heals on
 * sub-agent crash via the reaper's done event) and the slots-stream snapshot
 * field (covers the first frames after reload/reconnect before WS events
 * replay). Used by ChatPage (main route) and ChatPane (split view) — keep both
 * routes on this selector so the rule cannot drift.
 */
export const selectComposerBusy = (state: RootState, slot: string | null): boolean => {
  if (!slot) return state.chat.slotRunning
  if (selectSlotStreamState(state, slot) !== 'idle') return true
  if (slot === state.chat.activeSlot && state.chat.slotRunning) return true
  if (selectSlotSubagentsActive(state, slot)) return true
  const dashSlot = state.dashboard.slots.find((sl) => sl.key === slot)
  // A running autopilot plan keeps the composer "busy" so a mid-plan message
  // queues (chip card) instead of rendering an optimistic bubble that would
  // duplicate the backend's queued message. slot.running reads False between
  // stages, so orchestrating is the durable signal here. Kept until the backend
  // stops sending the field; the StageBoundary cleanup removes both reads.
  return !!(dashSlot?.subagents_running || dashSlot?.orchestrating)
}

/** Roles the continue scans walk past: they are not the conversation's floor.
 *  Mirrors `_is_interrupted` / `_has_conversation` in
 *  `src/kiro_crew/dashboard/chat_handlers.py`, which likewise only read
 *  `user` / `assistant` / `error` rows. Keep them in sync — these predicates
 *  decide whether to OFFER Continue and what to call it, those decide whether to
 *  authorize it and what to tell the model. */
const CONTINUE_SCAN_SKIP = new Set(['queued', 'tool_call', 'tool_result', 'inject', 'subagent', 'permission', 'nudge'])

/** Project directory of the ACTIVE chat session's slot, or undefined when no
 *  session is selected or the selected session has no project set. Used by the
 *  bottom terminal panel so a freshly opened terminal starts in the selected
 *  session's working tree instead of the server default. */
export const selectActiveSlotProject = (state: RootState): string | undefined => {
  const key = state.chat.activeSlot
  if (!key) return undefined
  return state.dashboard.slots.find((sl) => sl.key === key)?.project || undefined
}

/**
 * True when the active slot can be handed back to the agent — i.e. Continue is
 * worth offering on an empty composer.
 *
 * The rule is "the slot is idle and has a conversation under it", except for
 * a current typed member-memory setup refusal that requires the owner editor. It is
 * NOT limited to turns that visibly died, because a transcript cannot reliably
 * show that they did: a force-quit or force-exit runs no cleanup, so no error
 * row is ever written and a killed turn reads exactly like a finished one (see
 * ``_has_conversation`` in `src/kiro_crew/dashboard/chat_handlers.py`, which
 * authorizes the press under the slot lock). Offering it on every idle slot
 * covers those invisible interruptions, and doubles as a plain "keep going"
 * nudge — the one thing an empty composer's dead send button could never do.
 *
 * Everything that makes a continuation UNSAFE still returns false: a live turn,
 * a stop in flight, an optimistic local turn, a mid-plan autopilot slot, a
 * running subagent, or a queued message the runner is about to pick up itself.
 *
 * Computed locally on purpose: `messages`, `slotRunning`, `slotStopping` and the
 * queue are all already in this store, so no server field is needed to decide
 * what to SHOW. The server re-checks under the slot lock when the button is
 * actually pressed — this view is a lagging WS snapshot, so it cannot be the
 * authority for dispatching a turn.
 *
 * An empty transcript returns false, which keeps a brand-new chat's send button
 * disabled exactly as it is today.
 */
export const selectContinuable = (state: RootState): boolean => {
  const c = state.chat
  if (c.slotRunning || c.slotStopping || c.pendingTurnSlot) return false
  // An autopilot plan reads `running` False BETWEEN stages while still mid-plan,
  // so `running` alone would offer Continue on a slot the server refuses with
  // `slot_orchestrating`. Mirrors the same guard in `api_chat_slot_continue`.
  // Kept until the backend stops sending the field (StageBoundary cleanup).
  const dashSlot = state.dashboard.slots.find((sl) => sl.key === c.activeSlot)
  if (dashSlot?.orchestrating || dashSlot?.subagents_running) return false
  // A crew-bound session has NO local continue: `remote_bound_refusal` rejects
  // `executor === 'remote'` with 409 `remote_action_unsupported` ahead of every
  // guard above, because the synthetic turn Continue queues would dispatch on
  // THIS machine and diverge from the peer's transcript. Without the same guard
  // here the offer is self-defeating on the one path that guarantees the state:
  // `relay_remote_turn`'s failure path appends a trailing `error` row, which is
  // exactly the shape `selectTurnInterrupted` reads as an interruption, so a
  // dropped tunnel leaves a Resume whose only possible answer is that 409.
  // Typing is unaffected; a plain send DOES relay.
  // Keyed on `executor`, not `instance_id`: a half-open binding (marker set,
  // triple incomplete) is refused server-side too, so it must not offer here.
  if (slotIsRemoteBound(dashSlot)) return false
  const msgs = c.messages
  if (!msgs.length) return false
  for (let i = msgs.length - 1; i >= 0; i--) {
    const m = msgs[i]
    // A pending queued message means the backend is about to run the thread on
    // its own — offering Continue would double-fire the turn.
    if (m.role === 'queued') return false
    if (CONTINUE_SCAN_SKIP.has(m.role)) continue
    if ((m.role === 'user' || m.role === 'assistant') && m.content) {
      // System notices (compaction, session reload) are assistant-role status
      // messages, not the floor.
      if (m.role === 'assistant' && isSystemNoticeKind((m.meta as { kind?: string } | undefined)?.kind)) continue
      return true
    }
  }
  return false
}

/** The characters Python's no-argument `str.split()` splits on. JS `\s` is NOT
 *  the same set: it adds U+FEFF and lacks U+0085 and U+001C-001F, so a `\s`
 *  scan of the same content can produce a different first token than the
 *  backend's `content.split()[0]` -- the rule `is_turn_interrupted` and the
 *  runner's `user_requested_compaction` key on. */
const PYTHON_WHITESPACE_RE = /[\t\n\v\f\r\u001c-\u001f \u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+/

/** First whitespace-separated token by PYTHON's splitting rule, or undefined
 *  for all-whitespace content. Mirrors `content.split()[:1]` in
 *  `src/kiro_crew/dashboard/state.py`. */
const firstPythonToken = (content: string): string | undefined =>
  content.split(PYTHON_WHITESPACE_RE).find(Boolean)

/**
 * True when the transcript SHOWS the last turn ending without the assistant
 * handing the floor back — the user's row is last, or an `error` row trails the
 * assistant's.
 *
 * Gates the composer's Resume button (composed with `selectContinuable` in
 * ChatPage) and selects the continuation body handed to the model. Mirrors
 * `_is_interrupted` in `src/kiro_crew/dashboard/chat_handlers.py` — the two must
 * agree, or the button promises one thing and the agent is told another.
 *
 * A false result means "nothing in the transcript proves an interruption", never
 * "the turn definitely finished": the force-quit case leaves no evidence.
 *
 * A `/compact` answered by its compaction notice (the assistant row tagged
 * `meta.kind="compaction"`) reads as FINISHED: the slash command IS the whole
 * request and the notice IS its result. The tag alone cannot decide -- an
 * automatic compaction can write the same tagged row inside an ordinary turn
 * whose real reply never arrived, and that tail is a genuine interruption --
 * so the rule needs BOTH halves, matching `is_turn_interrupted` in
 * `src/kiro_crew/dashboard/state.py`.
 */
export const selectTurnInterrupted = (state: RootState): boolean => {
  const msgs = state.chat.messages
  let sawTrailingError = false
  let sawCompactionResult = false
  for (let i = msgs.length - 1; i >= 0; i--) {
    const m = msgs[i]
    // A deliberate Stop ENDS the turn; it does not interrupt it. This must be
    // tested before the user/assistant check, because pressing Stop before the
    // reply produced any text leaves `[user, stop_event]` — shape-identical to
    // "the gateway died before anything came back", which is what this scan
    // would otherwise read it as. Without this branch the same visible action
    // (pressing Stop) offered Resume or not depending purely on whether a
    // segment had flushed first, i.e. on invisible timing the user cannot
    // predict. The user chose to stop; the floor is theirs, so the composer
    // shows Send. Reached only for the NEWEST turn's terminator — an older stop
    // card deeper in history is never scanned, because a later user/inject/
    // assistant row returns first.
    if (isStopEvent(m)) return false
    if (m.role === 'error') { sawTrailingError = true; continue }
    // An inject row that DISPATCHED a turn (a queued continuation, a recovery,
    // a synthesis, a cron prompt) opens it exactly as a user row does, so one
    // with no reply after it is an interruption -- and an OLDER Stop card
    // behind it must not be reached and mask it. Only the structurally tagged
    // kinds qualify: a `/note` breadcrumb, a Stop-hook halt card or a refusal
    // notice is appended as `inject` too but ran nothing, and offering Resume
    // on a deliberately halted run would be wrong. Decided before
    // CONTINUE_SCAN_SKIP, where `inject` stays for the selectors that look
    // through continuations to the prior user floor. Mirrors
    // `is_turn_interrupted` in `dashboard/state.py`.
    if (m.role === 'inject' && m.content && TURN_INJECT_KINDS.has((m.meta as { injectKind?: unknown } | undefined)?.injectKind)) return true
    // A monitor loop's cycle row always dispatches a turn; unanswered, it is
    // the same shape as an unanswered user row. Decided before the skip set,
    // where `nudge` stays for the selectors that look through it.
    if (m.role === 'nudge' && m.content) return true
    if (CONTINUE_SCAN_SKIP.has(m.role)) continue
    if ((m.role === 'user' || m.role === 'assistant') && m.content) {
      const meta = m.meta as { kind?: string; notice?: string } | undefined
      if (m.role === 'assistant' && isSystemNoticeKind(meta?.kind)) {
        // Remember a compaction RESULT row on the newest turn; whether it
        // completes the turn depends on the user row it leads back to. The
        // recycle and stuck-turn notices borrow `kind="compaction"` and mark
        // themselves with `meta.notice`; they report no compaction, so they
        // must not complete one.
        if (meta?.kind === 'compaction' && !meta?.notice) sawCompactionResult = true
        continue
      }
      if (m.role !== 'user') return sawTrailingError
      // A user row still carrying `meta.optimistic` is the composer's own
      // bubble, minted at send time and cleared only by the send's receipt
      // (`confirmOptimisticSend`) or a correlated echo. Until then nothing
      // proves the server ever received the text -- a POST that hit the
      // transport deadline before leaving the browser draws exactly this row --
      // so no turn was opened and none was interrupted. Reading it as one
      // offers a Resume whose bare continue runs against a transcript that
      // never held the message, and the agent answers the request before it.
      // An error row behind it is the send's own failure, not a reply that
      // died, so it does not revive the verdict. This branch is invisible to
      // the `is_turn_interrupted` mirror by construction: the flag is minted
      // client-side and never sent, so no transcript the server can produce
      // carries a row for it to read, and the two cannot disagree.
      if (m.meta?.optimistic) return false
      // A `/compact` answered by its compaction notice is a FINISHED turn --
      // unless an error row trails the notice, the same evidence the
      // plain-assistant branch honors. First-whitespace-token match using
      // PYTHON's whitespace set (the backend rule is `content.split()`, and
      // JS `\s` / `trim()` disagree with it on U+FEFF and U+0085), so the two
      // mirrors cannot split the same content differently.
      if (sawCompactionResult && firstPythonToken(m.content) === '/compact') return sawTrailingError
      return true
    }
  }
  // Ran off the start of the loaded window with no conversational row: a long
  // turn can push its opener and reply into the frozen prefix, leaving only
  // tool rows here. A trailing error row is still the evidence the assistant
  // branch honors, so it decides the same way.
  return sawTrailingError
}

/**
 * True while the newest send in the active transcript is a plain-send bubble
 * whose receipt never came (`meta.deliveryUnconfirmed`, stamped by
 * `markSendUnconfirmed` on `response-late`).
 *
 * Gates the footer's plain running indicator. The send did start a local turn,
 * so `slotRunning` is true and "Thinking…" would draw directly under a bubble
 * that says "Delivery pending…" over a notice that says the delivery is not
 * confirmed -- the UI claiming the agent is working on a message nothing proves
 * it received. Keyed on the deadline MARK, never on `optimistic` alone: the
 * flag is also true for the ordinary in-flight second before a receipt, and
 * hiding the indicator there would make every send flicker.
 *
 * Reads the same tail `selectTurnInterrupted` reads, with the same skips and
 * the same terminators, so the two never disagree about which row is newest:
 * the WARN notice under the bubble and the roles in `CONTINUE_SCAN_SKIP` are
 * looked through; a Stop card, a dispatching inject row or a nudge row ends the
 * walk (each means the agent is, or was, visibly at work on something else, and
 * the indicator is theirs); the first user or assistant row decides. The
 * error/compaction bookkeeping that selector carries has no bearing on a
 * boolean about the bubble, so this walk does not repeat it.
 *
 * Client-local by construction, like the mark: nothing the server sends can
 * carry it, and both confirmation doors (`confirmOptimisticSend`, the echo
 * reconcile) clear it, so the indicator returns on its own.
 */
export const selectTrailingSendUnconfirmed = (state: RootState): boolean => {
  const msgs = state.chat.messages
  for (let i = msgs.length - 1; i >= 0; i--) {
    const m = msgs[i]
    if (isStopEvent(m)) return false
    if (m.role === 'inject' && m.content && TURN_INJECT_KINDS.has((m.meta as { injectKind?: unknown } | undefined)?.injectKind)) return false
    if (m.role === 'nudge' && m.content) return false
    if (CONTINUE_SCAN_SKIP.has(m.role)) continue
    if ((m.role === 'user' || m.role === 'assistant') && m.content) {
      if (m.role === 'assistant' && isSystemNoticeKind((m.meta as { kind?: string } | undefined)?.kind)) continue
      return m.role === 'user' && !!m.meta?.deliveryUnconfirmed
    }
  }
  return false
}
