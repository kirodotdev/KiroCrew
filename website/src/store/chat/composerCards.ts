/** The cards that sit above the composer, one per slot: the agent's question
 *  card, follow-up suggestions, and the post-titling folder suggestion. Each
 *  map is keyed by slot and guarded the same way; the frame-driven retirement
 *  of a stateless question card lives with the chat-frame reducer in
 *  chatSlice.ts. */
import type { PayloadAction } from '@reduxjs/toolkit'
import type { ChatState, FollowupItem, QuestionNotice } from './state'
import { isUnsafeKey, safeKey } from './wire'

const QUESTION_SETTLED_LIMIT = 200

const ANSWERED_QUESTION_ENDINGS = new Set(['answered', 'composer', 'queued'])

/** Whether a blocking ask ended because the user's text reached the agent. */
export const isAnsweredQuestionEnding = (reason: unknown): boolean =>
  typeof reason === 'string' && ANSWERED_QUESTION_ENDINGS.has(reason)

/** Retire folder-suggestion cards for slots an authoritative list reports as
 *  already filed.
 *
 *  The card is per-window Redux state, but the question it asks — "file this
 *  unfiled session?" — is answered globally the moment ANY window files the
 *  session: accepting the card, the sidebar row menu, and drag-to-folder all
 *  land in PATCH /api/chat/slots/{slot}/folder, whose push_slots_update
 *  broadcasts the new folder_id to every connected client. Without this pass
 *  every OTHER window keeps offering a move that already happened, and
 *  accepting there re-issues it. Deliberately unconditional on the card's
 *  `ts`: the backend offers at most one card per slot and never re-offers
 *  after filing, so any card for a filed slot is moot regardless of
 *  generation. That holds only for a list that is CURRENT — a session key can
 *  be reused after close, and a stale list then reports the PREVIOUS tenant's
 *  folder_id against the replacement's card — so each caller must ensure its
 *  payload is not stale relative to the suggestion stream: WS frames are
 *  ordered with the suggestion frames on one socket, and the fetch reply is
 *  only trusted before the first live snapshot (see its call site). Declining,
 *  by contrast, writes nothing server-side, so a decline stays window-local
 *  and other windows age their copy out (FOLDER_SUGGESTION_MAX_TURNS) — the
 *  offer is still answerable there. */
export const clearFiledFolderSuggestions = (
  state: ChatState,
  payload: readonly { key: string; folder_id?: string }[],
): void => {
  if (!state.folderSuggestions) return
  for (const s of payload) {
    if (!s.folder_id) continue
    // Both spellings, same as evictSlotState: some writers key through safeKey().
    delete state.folderSuggestions[s.key]
    delete state.folderSuggestions[safeKey(s.key)]
  }
}

/** Read one slot's pending question card, or null.
 *
 *  A bare `map[slot]` lookup is not safe even with guarded writes: for
 *  `__proto__` or `constructor` it returns an INHERITED value that is truthy but
 *  carries no `questions`, so the card renders and crashes. Guarding the key and
 *  requiring an own property makes the read fail closed. Exported so the single-
 *  chat view and the grid panes share one definition. */
export const pendingQuestionFor = (
  map: ChatState['pendingQuestions'] | undefined,
  slot: string | null | undefined,
): ChatState['pendingQuestions'][string] | null => {
  if (!slot || !map || isUnsafeKey(slot)) return null
  return Object.prototype.hasOwnProperty.call(map, slot) ? map[slot] : null
}

/** Capture a slot's pending BLOCKING card's `ask_id` at the send path's ENTRY.
 *  Call SYNCHRONOUSLY, before the first await, so the capture is the card the
 *  user saw when they hit send: captured any later, an await gap lets the
 *  card-submit flow resolve it (capture reads null) or a newer ask land
 *  (capture reads an id this send never answered). Shared by the two send sites
 *  (ChatPage.send / ChatPane.doSend) so their capture logic cannot drift.
 *
 *  A STATELESS card needs no send-time capture: the server owns its lifecycle
 *  and retires the record on the user row this send appends, announcing it
 *  with `question_card_resolved` (handled by `resolveQuestionCard`). A blocking
 *  card cannot be retired that way: an agent is parked on its HTTP request, so
 *  deleting the entry alone leaves that agent waiting out its whole window with
 *  nothing on screen. Sending a composer message instead of using the card
 *  therefore has to resolve it through the answer endpoint, which is why the
 *  send path needs the id rather than just "a card was pending".
 *
 *  Returns null while the card holds an ANSWER IN PROGRESS — a typed custom
 *  answer or a pending option selection — because resolving it unmounts the card
 *  and that work lives only in the component. The same invariant the stateless
 *  path keeps (dropStaleStatelessQuestion), for the same reason: a send must not
 *  silently destroy something the user is part-way through. The agent then stays
 *  blocked, but the card is still on screen with the draft intact, so the user
 *  keeps both affordances for releasing it. A card the user never touched has no
 *  draft and is resolved normally. */
export const capturePendingAskId = (
  map: ChatState['pendingQuestions'] | undefined,
  slot: string | null | undefined,
): string | null => {
  const c = pendingQuestionFor(map, slot)
  if (c?.draftActive) return null
  return c?.ask_id ?? null
}

/** Whether a send's acceptance should resolve the blocking card captured at its
 *  entry. Shared by the two send sites so the rule cannot drift between them.
 *
 *  `queued` counts, which is the difference from the stateless card's rule and
 *  the whole point of this helper: a queued message cannot pop until the turn
 *  ends, and the turn cannot end while the agent is blocked on the card, so
 *  deferring to queue_pop would hold the two against each other for the entire
 *  ask window. A rejected send resolves nothing — the card is the user's only
 *  way to answer, and the session never moved on. */
/** A release-failed notice describes the open card, so it leaves with that card. */
const dropReleaseFailedNotice = (state: ChatState, slot: string): void => {
  if (!slot || isUnsafeKey(slot)) return
  const key = safeKey(slot)
  if (state.restoredQuestionNotices?.[key]?.kind === 'release_failed') delete state.restoredQuestionNotices[key]
}

export const shouldResolveAskOnSend = (
  accepted: { ok?: boolean; queued?: boolean } | null | undefined,
  askAtSend: string | null,
): boolean => !!askAtSend && !!(accepted?.ok || accepted?.queued)

/** User sends a folder-suggestion card survives before it ages out on its own.
 *
 *  The card is an offer, not a task: a user who keeps typing past it has already
 *  answered by conduct, and a permanent card in the composer band is a standing
 *  cost paid by every session the model guessed wrong about. Three is the count
 *  where the offer is still plausibly in view (the user may be mid-thought when
 *  it lands, so one turn is too eager) without becoming furniture.
 *
 *  Counted by an explicit `ageFolderSuggestion` dispatch from the ONE surface
 *  that renders the card (ChatPage's composer band, active slot), and only
 *  after the server confirmed the send was delivered. So: a failed send never
 *  counts (delivery unconfirmed), a send from a surface that does not show the
 *  card (ChatPane companion/embed panes, Slack, cron) never counts (nothing
 *  rendered, nothing dispatched), and a replacement card that landed while the
 *  send was in flight is not aged (ts-pinned to the generation the user saw). */
export const FOLDER_SUGGESTION_MAX_TURNS = 3

export const composerCardReducers = {
  /** Show a question card for a slot. Every card carries the SERVER's identity
   *  — `ask_id` for a blocking ask, `card_id` for a stateless card (the MCP
   *  `ask_question` card and kiro-cli's native `AskUserQuestion` card alike) —
   *  and that identity is the only one this slice keeps: the server owns the
   *  card's lifecycle and names it on every retirement (`question_card_resolved`)
   *  and on `GET /api/ask-question/pending`.
   *
   *  `native` marks the mid-turn kiro-cli card whose answer must STEER into the
   *  turn still waiting on it (see PendingQuestionCard's callers); it rides the
   *  frame and the /pending row so a reload keeps the routing with the card. */
  setQuestionCard(state: ChatState, action: PayloadAction<{ slot: string; ask_id?: string; card_id?: string; native?: boolean; questions: ChatState['pendingQuestions'][string]['questions'] }>) {
    // Defensive init: existing test fixtures build partial preloaded state
    // without this key.
    if (!state.pendingQuestions) state.pendingQuestions = {}
    // Same fail-closed chokepoint as the neighbouring slot-keyed reducers: the
    // slot arrives over the websocket, and `__proto__`/`constructor` would
    // otherwise make a READ return an inherited value that is truthy but has
    // no `questions`, crashing QuestionCard on render.
    if (isUnsafeKey(action.payload.slot)) return
    const key = safeKey(action.payload.slot)
    const prev = state.pendingQuestions[key]
    // Identity comparison, not payload comparison: a websocket reconnect
    // re-lists the SAME still-pending card (syncPendingQuestions), and that is
    // not a new ask — keep the existing entry (and its `draftActive`) so the
    // mounted card and the user's half-entered answer are not churned. A new
    // ask always carries a new server id, even when it repeats a prior
    // question word for word, so it always replaces. An entry with no server
    // identity at all (a fixture) has nothing to compare and is replaced.
    const identity = action.payload.ask_id || action.payload.card_id
    if (prev && identity && (prev.ask_id || prev.serverCardId) === identity) return
    // A new ask whose payload is byte-identical to the card on screen keeps
    // the mounted component: PendingQuestionCard keys it by slot, and
    // QuestionCard resets its selections and typed text only when the
    // questions change. The user's local draft therefore survives the swap,
    // so `draftActive` must survive with it, or the next turn-consuming
    // frame retires a card that still holds unsent work. A DIFFERENT payload
    // resets the component (local draft genuinely gone), so it starts clean.
    const sameShape =
      prev !== undefined &&
      prev.ask_id === action.payload.ask_id &&
      JSON.stringify(prev.questions) === JSON.stringify(action.payload.questions)
    state.pendingQuestions[key] = {
      slot: action.payload.slot,
      ask_id: action.payload.ask_id,
      questions: action.payload.questions,
      // The server's identity for a stateless card. It names the record the
      // dismiss route retires, so a dismissal that lands after a newer card
      // replaced this one is refused instead of clearing the new card's
      // status; and it is what `question_card_resolved` matches against.
      // Absent for a blocking card (its `ask_id` is that identity).
      serverCardId: action.payload.card_id,
      ...(action.payload.native ? { native: true } : {}),
      ...(sameShape && prev.draftActive === true ? { draftActive: true } : {}),
      ...(sameShape && prev.draftAnswers ? { draftAnswers: prev.draftAnswers } : {}),
    }
  },
  /** Take a slot's card off screen, optionally only if it is still the card
   *  named by `card_id` (its server identity). The identity guard is for the
   *  round-trips that clear AFTER the server answers — a dismiss whose response
   *  lands after a newer card replaced the one dismissed must not take that
   *  newer card down with it. Unlike `resolveQuestionCard`, this is the user's
   *  own explicit action, so a draft in progress does not spare the card. */
  clearQuestionCard(state: ChatState, action: PayloadAction<{ slot: string; card_id?: string; restored_notice?: boolean }>) {
    if (isUnsafeKey(action.payload.slot)) return
    const key = safeKey(action.payload.slot)
    if (action.payload.restored_notice) {
      delete state.restoredQuestionNotices?.[key]
      return
    }
    const card = state.pendingQuestions?.[key]
    if (!card) return
    if (action.payload.card_id && card.serverCardId !== action.payload.card_id) return
    delete state.pendingQuestions[key]
    dropReleaseFailedNotice(state, action.payload.slot)
  },
  /** Publish component-local answers for retirement recovery and stateless draft protection. */
  setQuestionDraft(state: ChatState, action: PayloadAction<{ slot: string; answers: Record<string, string> }>) {
    if (isUnsafeKey(action.payload.slot)) return
    const key = safeKey(action.payload.slot)
    const card = state.pendingQuestions?.[key]
    if (!card) return
    if (Object.keys(action.payload.answers).length) {
      card.draftActive = true
      card.draftAnswers = action.payload.answers
    } else {
      delete card.draftActive
      delete card.draftAnswers
    }
  },
  /** A durable notice above a slot's card: unlike a transcript row it survives the refetch a send triggers. */
  setQuestionNotice(state: ChatState, action: PayloadAction<{ slot: string; message: string; kind?: QuestionNotice['kind'] }>) {
    if (!action.payload.slot || isUnsafeKey(action.payload.slot) || !action.payload.message) return
    if (!state.restoredQuestionNotices) state.restoredQuestionNotices = {}
    state.restoredQuestionNotices[safeKey(action.payload.slot)] = {
      message: action.payload.message,
      kind: action.payload.kind ?? 'restored',
    }
  },
  setQuestionRequestInFlight(state: ChatState, action: PayloadAction<{ ask_id: string; inFlight: boolean; releaseReason?: 'composer' | 'queued' }>) {
    if (!action.payload.ask_id || isUnsafeKey(action.payload.ask_id)) return
    if (!state.questionRequestsInFlight) state.questionRequestsInFlight = {}
    const key = safeKey(action.payload.ask_id)
    if (action.payload.inFlight) state.questionRequestsInFlight[key] = action.payload.releaseReason ?? true
    else delete state.questionRequestsInFlight[key]
  },
  markQuestionSettled(state: ChatState, action: PayloadAction<{ ask_id: string }>) {
    if (!action.payload.ask_id || isUnsafeKey(action.payload.ask_id)) return
    if (!state.questionsSettled) state.questionsSettled = {}
    const key = safeKey(action.payload.ask_id)
    delete state.questionsSettled[key]
    state.questionsSettled[key] = true
    const overflow = Object.keys(state.questionsSettled).length - QUESTION_SETTLED_LIMIT
    if (overflow > 0) {
      for (const retired of Object.keys(state.questionsSettled).slice(0, overflow)) {
        delete state.questionsSettled[retired]
      }
    }
  },
  /** Clear the card the backend just retired, matched by IDENTITY.
   *
   *  `ask_id` names a blocking round-trip; `card_id` names a stateless card
   *  (compared against the server identity the card was delivered with).
   *  Matching by identity rather than by slot is what stops a stale retirement
   *  — for a question already replaced by a newer one — from clearing a live
   *  card the user is part-way through.
   *
   *  A STATELESS card with a draft in progress survives, for the same reason
   *  `dropStaleStatelessQuestion` spares it: its server record may retire while
   *  the component-local answer is still being edited. A BLOCKING ask is removed
   *  immediately; the websocket handler restores its published `draftAnswers`
   *  before dispatching this reducer. `settled` is a local caller that has
   *  finished with either kind of card. */
  resolveQuestionCard(state: ChatState, action: PayloadAction<{ ask_id?: string; card_id?: string; settled?: boolean; restored_notice?: string }>) {
    const { ask_id: askId, card_id: cardId, settled, restored_notice: restoredNotice } = action.payload
    if (!askId && !cardId) return
    for (const [slotKey, card] of Object.entries(state.pendingQuestions ?? {})) {
      const hit = askId ? card?.ask_id === askId : card?.serverCardId === cardId
      if (!hit) continue
      if (!settled && cardId && card?.draftActive) continue
      dropReleaseFailedNotice(state, card.slot)
      if (askId && restoredNotice) {
        if (!state.restoredQuestionNotices) state.restoredQuestionNotices = {}
        state.restoredQuestionNotices[safeKey(card.slot)] = {
          message: restoredNotice,
          kind: 'restored',
        }
      }
      delete state.pendingQuestions[slotKey]
    }
  },
  setFollowupCard(state: ChatState, action: PayloadAction<{ slot: string; items: FollowupItem[]; ts?: number }>) {
    const { slot, items, ts } = action.payload
    if (!slot || !items?.length) return
    if (isUnsafeKey(slot)) return  // never index a state map with __proto__/constructor/prototype
    // Defensive: a partial preloaded slice (tests, older persisted state) can
    // arrive without this key.
    if (!state.followups) state.followups = {}
    state.followups[slot] = { items, ts: ts ?? Date.now() / 1000 }
  },
  // `ts` guards the async case: "Start in new worktree" clears the card only
  // after its request resolves, and a NEWER card may have arrived for the same
  // slot meanwhile. Passing the ts the action started with means the newer card
  // survives instead of being clobbered by the older action's completion.
  clearFollowupCard(state: ChatState, action: PayloadAction<{ slot: string; ts?: number }>) {
    const { slot, ts } = action.payload
    if (isUnsafeKey(slot)) return
    const card = state.followups?.[slot]
    if (!card) return
    if (ts != null && card.ts !== ts) return
    delete state.followups[slot]
  },
  // Skip ONE suggestion without discarding the others. The card disappears
  // only once its last item is gone, so skipping the first of three does not
  // silently throw away the other two.
  dismissFollowupItem(state: ChatState, action: PayloadAction<{ slot: string; index: number; ts?: number }>) {
    const { slot, index, ts } = action.payload
    if (isUnsafeKey(slot)) return
    const card = state.followups?.[slot]
    if (!card) return
    // Same staleness guard as `clearFollowupCard`: a replacement card can land
    // between render and click, and an unqualified dismiss would delete that
    // index from a card the user has not seen.
    if (ts != null && card.ts !== ts) return
    const items = card.items.filter((_, i) => i !== index)
    if (items.length) state.followups[slot] = { ...card, items }
    else delete state.followups[slot]
  },
  setFolderSuggestion(state: ChatState, action: PayloadAction<{ slot: string; folderId: string; folderName: string; breadcrumb: string; ts?: number }>) {
    const { slot, folderId, folderName, breadcrumb, ts } = action.payload
    if (!slot || !folderId || !folderName) return
    if (isUnsafeKey(slot)) return  // never index a state map with __proto__/constructor/prototype
    // Defensive: a partial preloaded slice (tests, older persisted state) can
    // arrive without this key.
    if (!state.folderSuggestions) state.folderSuggestions = {}
    state.folderSuggestions[slot] = { folderId, folderName, breadcrumb, ts: ts ?? Date.now() / 1000, turns: 0 }
  },
  // Both answers land here — accepting the move and declining it clear the same
  // way, because the backend keeps no state to resolve and offers at most one
  // card per slot either way. `ts` guards the async case the way
  // `clearFollowupCard` does: the accept path clears after its move request is
  // dispatched, so a card that arrived meanwhile must survive.
  clearFolderSuggestion(state: ChatState, action: PayloadAction<{ slot: string; ts?: number }>) {
    const { slot, ts } = action.payload
    if (isUnsafeKey(slot)) return
    const card = state.folderSuggestions?.[slot]
    if (!card) return
    if (ts != null && card.ts !== ts) return
    delete state.folderSuggestions[slot]
  },
  /** Age the slot's folder-suggestion card by one delivered user send, and
   *  drop it once it has had its run (> FOLDER_SUGGESTION_MAX_TURNS).
   *
   *  Deliberately its OWN action, dispatched ONLY by the render site that
   *  showed the card (ChatPage's composer band, active slot) after the server
   *  confirmed the send was delivered — never baked into a shared send
   *  reducer. The two review rounds that shaped this: counting the optimistic
   *  `startLocalTurn` let FAILED sends burn the one-shot offer, and counting
   *  `confirmOptimisticSend` let surfaces that confirm sends WITHOUT rendering
   *  the card (ChatPane in artifact companion chats, sidebar panes, settings
   *  embeds) expire a card the user never saw. Tying aging to an explicit
   *  dispatch from the renderer makes every "send from a surface that does not
   *  show the card" variant unreachable by construction.
   *
   *  `ts` guards the in-flight-replacement race the way `clearFolderSuggestion`
   *  does: the POST that earns this dispatch was sent while ONE card
   *  generation was visible, and a replacement arriving before the response
   *  must not inherit its age. */
  ageFolderSuggestion(state: ChatState, action: PayloadAction<{ slot: string; ts?: number }>) {
    const { slot, ts } = action.payload
    if (isUnsafeKey(slot)) return
    const suggestion = state.folderSuggestions?.[slot]
    if (!suggestion) return
    if (ts != null && suggestion.ts !== ts) return
    suggestion.turns = (suggestion.turns ?? 0) + 1
    if (suggestion.turns > FOLDER_SUGGESTION_MAX_TURNS) delete state.folderSuggestions[slot]
  },
}
