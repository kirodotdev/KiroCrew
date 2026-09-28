/**
 * Tests for the ask_question card reducers.
 *
 * Cards are keyed BY SLOT. A single global card meant two agents calling
 * ask_question in different slots would evict each other, and the loser blocked
 * until its timeout with no card ever rendered.
 *
 * Every card carries its SERVER identity: `ask_id` for a blocking ask, `card_id`
 * for a stateless card (the MCP `ask_question` card and kiro-cli's native
 * `AskUserQuestion` card alike). `question_card_resolved` arrives whenever
 * a pending question stops waiting — answered, timed out, cancelled, or retired
 * by the user's next message — and carries that identity, so a LATE resolved
 * event from an earlier question cannot wipe a newer card.
 */
import { describe, it, expect } from 'vitest'
import reducer, { setQuestionCard, setQuestionDraft, resolveQuestionCard, clearQuestionCard, sseChatMessage, appendMessage, appendSlotMessage, appendQueuedMessage, removeQueuedMessage, cancelQueuedMessage } from '../store/chatSlice'
import { reconcileQuestions } from '../hooks/useWebSocket'

const initial = reducer(undefined, { type: '@@INIT' })

const QUESTIONS = [
  { question: 'Which approach?', header: 'SCOPE', options: [{ label: 'A' }, { label: 'B' }] },
]

function withCard(slot: string, askId?: string, state = initial) {
  return reducer(state, setQuestionCard({ slot, ask_id: askId, questions: QUESTIONS }))
}

/** A STATELESS card as the server delivers it: a `card_id`, no `ask_id`. */
function stateless(slot: string, cardId = 'card-1', state = initial) {
  return reducer(state, setQuestionCard({ slot, card_id: cardId, questions: QUESTIONS }))
}

describe('question card state', () => {
  it('stores the card under its slot key', () => {
    const state = withCard('chat-1', 'abc')
    expect(state.pendingQuestions['chat-1']?.ask_id).toBe('abc')
    expect(state.pendingQuestions['chat-1']?.slot).toBe('chat-1')
  })

  it('concurrent cards on two slots coexist', () => {
    // The defect this guards: a single global card meant the second broadcast
    // evicted the first, blocking that agent until timeout.
    let state = withCard('chat-1', 'first')
    state = withCard('chat-2', 'second', state)
    expect(state.pendingQuestions['chat-1']?.ask_id).toBe('first')
    expect(state.pendingQuestions['chat-2']?.ask_id).toBe('second')
  })

  it('resolveQuestionCard clears only the matching ask_id', () => {
    let state = withCard('chat-1', 'first')
    state = withCard('chat-2', 'second', state)
    state = reducer(state, resolveQuestionCard({ ask_id: 'first' }))
    expect(state.pendingQuestions['chat-1']).toBeUndefined()
    expect(state.pendingQuestions['chat-2']?.ask_id).toBe('second')
  })

  it('a stale resolution leaves a newer card on another slot intact', () => {
    const state = reducer(withCard('chat-2', 'newer'), resolveQuestionCard({ ask_id: 'older' }))
    expect(state.pendingQuestions['chat-2']?.ask_id).toBe('newer')
  })

  it('resolveQuestionCard is a no-op with no pending cards', () => {
    const state = reducer(initial, resolveQuestionCard({ ask_id: 'abc' }))
    expect(state.pendingQuestions).toEqual({})
  })

  it('a stateless card is unaffected by a blocking-ask resolve', () => {
    // The two identities are disjoint: an `ask_id` never names a `card_id` card.
    const state = reducer(stateless('chat-1'), resolveQuestionCard({ ask_id: 'abc' }))
    expect(state.pendingQuestions['chat-1']).toBeDefined()
  })

  it('resolveQuestionCard clears a stateless card by its card_id', () => {
    let state = stateless('chat-1', 'card-a')
    state = stateless('chat-2', 'card-b', state)
    state = reducer(state, resolveQuestionCard({ card_id: 'card-a' }))
    expect(state.pendingQuestions['chat-1']).toBeUndefined()
    expect(state.pendingQuestions['chat-2']?.serverCardId).toBe('card-b')
  })

  it('a stale card_id resolution leaves a newer card in the same slot intact', () => {
    // Card A was superseded by card B; A's retirement arrives late.
    let state = stateless('chat-1', 'card-a')
    state = stateless('chat-1', 'card-b', state)
    state = reducer(state, resolveQuestionCard({ card_id: 'card-a' }))
    expect(state.pendingQuestions['chat-1']?.serverCardId).toBe('card-b')
  })

  it('records the native mark only when the server sets it', () => {
    // The mark is what routes a native card's answer as a steer into the live
    // turn; the MCP card must never acquire it.
    let state = reducer(initial, setQuestionCard({ slot: 'chat-1', card_id: 'card-n', native: true, questions: QUESTIONS }))
    expect(state.pendingQuestions['chat-1']?.native).toBe(true)
    state = reducer(initial, setQuestionCard({ slot: 'chat-1', card_id: 'card-m', native: false, questions: QUESTIONS }))
    expect(state.pendingQuestions['chat-1']).not.toHaveProperty('native')
    state = stateless('chat-1', 'card-p')
    expect(state.pendingQuestions['chat-1']).not.toHaveProperty('native')
  })

  it('clearQuestionCard clears just that slot', () => {
    let state = withCard('chat-1', 'a')
    state = withCard('chat-2', 'b', state)
    state = reducer(state, clearQuestionCard({ slot: 'chat-1' }))
    expect(state.pendingQuestions['chat-1']).toBeUndefined()
    expect(state.pendingQuestions['chat-2']).toBeDefined()
  })

  it('tolerates the key being absent from preloaded state', () => {
    // Existing fixtures build partial state without pendingQuestions.
    const partial = { ...initial } as Record<string, unknown>
    delete partial.pendingQuestions
    const state = reducer(
      partial as typeof initial,
      setQuestionCard({ slot: 'chat-1', ask_id: 'x', questions: QUESTIONS }),
    )
    expect(state.pendingQuestions['chat-1']?.ask_id).toBe('x')
  })
})


/**
 * A STATELESS card (no ask_id — `post_question_card`, the agent ended its turn
 * on it) is answered by "the next message the USER sends". So a `user` frame is
 * that answer channel being spent, and it retires the card.
 *
 * A `nudge` frame is not. It used to be in the retiring set, and the defect that
 * cost was worse than the one it fixed: a monitored session's card was deleted
 * out from under the user by the next auto-nudge cycle, while the question was
 * still unanswered and the same agent was still there to read the answer. An
 * unanswered card is retired by the user answering it or by Dismiss — nothing
 * else.
 *
 * Blocking cards (ask_id) are exempt from this path entirely: their lifecycle
 * is the `question_card_resolved` broadcast, and clearing one on a mid-turn
 * steer frame would strand the blocked tool call with no card.
 *
 * The server owns the stateless card's lifecycle too and announces its
 * retirement with `question_card_resolved`; this local drop is defense in depth
 * for the frame that arrives before that broadcast.
 */
describe('stateless card staleness on turn-consuming frames', () => {
  const legacy = (slot: string, state = initial) => stateless(slot, 'card-1', state)

  it('a user frame on the slot drops its stateless card (active path)', () => {
    let state = { ...legacy('chat-1'), activeSlot: 'chat-1' }
    state = reducer(state, sseChatMessage({ slot: 'chat-1', role: 'user', content: 'use the assets branch' }))
    expect(state.pendingQuestions['chat-1']).toBeUndefined()
  })

  it('a nudge frame on the slot KEEPS its stateless card (active path)', () => {
    // The reported bug this replaces the old expectation: the nudge loop fired,
    // the card vanished, and the user came back to a question they could no
    // longer answer. A nudge wakes the same agent in the same conversation, so
    // the answer channel is intact and the card must stay.
    let state = { ...legacy('chat-1'), activeSlot: 'chat-1' }
    state = reducer(state, sseChatMessage({ slot: 'chat-1', role: 'nudge', content: '[auto-nudge cycle 3]\ncheck the PR' }))
    expect(state.pendingQuestions['chat-1']).toBeDefined()
  })

  it('drops the stateless card on the non-active (grid pane) path too', () => {
    let state = { ...legacy('chat-1'), activeSlot: 'other-slot' }
    state = reducer(state, sseChatMessage({ slot: 'chat-1', role: 'user', content: 'answered from the pane' }))
    expect(state.pendingQuestions['chat-1']).toBeUndefined()
  })

  it('a nudge frame KEEPS the card on the non-active (grid pane) path too', () => {
    // Both hand-synced appliers must agree, or a pane and the single-chat view
    // disagree about whether the same question is still answerable.
    let state = { ...legacy('chat-1'), activeSlot: 'other-slot' }
    state = reducer(state, sseChatMessage({ slot: 'chat-1', role: 'nudge', content: '[auto-nudge cycle 2]\ngo' }))
    expect(state.pendingQuestions['chat-1']).toBeDefined()
  })

  it('a server-owned (ask_id) card survives user and nudge frames', () => {
    // Its lifecycle is question_card_resolved; a steer frame mid-block must
    // not strand the waiting tool call by deleting its card.
    let state = { ...withCard('chat-1', 'blocked-ask'), activeSlot: 'chat-1' }
    state = reducer(state, sseChatMessage({ slot: 'chat-1', role: 'user', content: 'steer text' }))
    state = reducer(state, sseChatMessage({ slot: 'chat-1', role: 'nudge', content: '[auto-nudge cycle 1]\ngo' }))
    expect(state.pendingQuestions['chat-1']?.ask_id).toBe('blocked-ask')
  })

  it('frames on another slot leave the card alone', () => {
    let state = { ...legacy('chat-1'), activeSlot: 'chat-2' }
    state = reducer(state, sseChatMessage({ slot: 'chat-2', role: 'user', content: 'unrelated turn' }))
    expect(state.pendingQuestions['chat-1']).toBeDefined()
  })

  it('non-turn frames (assistant, tool, chunk) leave the card alone', () => {
    let state = { ...legacy('chat-1'), activeSlot: 'chat-1' }
    state = reducer(state, sseChatMessage({ slot: 'chat-1', role: 'chunk', content: 'stream', seq: 1 }))
    state = reducer(state, sseChatMessage({ slot: 'chat-1', role: 'tool', content: '🔧 read', meta: { tool_call_id: 't1' } }))
    state = reducer(state, sseChatMessage({ slot: 'chat-1', role: 'assistant', content: 'done' }))
    expect(state.pendingQuestions['chat-1']).toBeDefined()
  })

  it('a redelivered user frame cannot drop a newer card', () => {
    // Frame replay (reconnect catch-up) is identified by meta.mid. The card
    // was posted AFTER the original delivery of this frame; its replay must
    // not clear it — the drop sits behind the redelivery guard.
    let state = { ...initial, activeSlot: 'chat-1' }
    const frame = { slot: 'chat-1', role: 'user', content: 'earlier turn', meta: { mid: 'm-1' } }
    state = reducer(state, sseChatMessage(frame))
    state = reducer(state, setQuestionCard({ slot: 'chat-1', card_id: 'card-1', questions: QUESTIONS }))
    state = reducer(state, sseChatMessage(frame)) // replay of the SAME mid
    expect(state.pendingQuestions['chat-1']).toBeDefined()
  })

  // The optimistic appends must NOT retire the card: a failed send (offline,
  // 5xx) leaves the session unchanged, and deleting the card then would strand
  // a question the agent is still inviting an answer to. The server retires the
  // card when the user row actually lands and announces it with
  // `question_card_resolved` (tested below), to every window including the one
  // that sent the answer.
  it('an optimistic composer append leaves the stateless card in place (appendMessage)', () => {
    let state = { ...legacy('chat-1'), activeSlot: 'chat-1' }
    state = reducer(state, appendMessage({ role: 'user', content: 'answering in the composer', cls: 'msg msg-u' }))
    expect(state.pendingQuestions['chat-1']).toBeDefined()
  })

  it('an optimistic pane append leaves the stateless card in place (appendSlotMessage)', () => {
    let state = { ...legacy('chat-1'), activeSlot: 'other-slot' }
    state = reducer(state, appendSlotMessage({ slot: 'chat-1', message: { role: 'user', content: 'pane answer', cls: 'msg msg-u' } }))
    expect(state.pendingQuestions['chat-1']).toBeDefined()
  })


  it('the server retirement broadcast retires the stateless card (question_card_resolved)', () => {
    // What the composer send path relies on: the user row lands on the server,
    // which clears the record and announces its card_id.
    let state = { ...legacy('chat-1'), activeSlot: 'chat-1' }
    state = reducer(state, resolveQuestionCard({ card_id: 'card-1' }))
    expect(state.pendingQuestions['chat-1']).toBeUndefined()
  })

  it('a stale retirement cannot retire a newer card (identity mismatch)', () => {
    // Race: the answer to card A is sent → a new turn replaces it with card B →
    // A's retirement broadcast arrives after B was rendered and must not delete B.
    let state = { ...legacy('chat-1'), activeSlot: 'chat-1' }
    state = reducer(state, setQuestionCard({
      slot: 'chat-1',
      card_id: 'card-2',
      questions: [{ question: 'A newer ask?', options: [{ label: 'Yes' }] }],
    }))
    state = reducer(state, resolveQuestionCard({ card_id: 'card-1' }))
    expect(state.pendingQuestions['chat-1']?.questions[0].question).toBe('A newer ask?')
  })

  it('a stale retirement cannot retire a newer IDENTICAL question (server identity)', () => {
    // The agent repeats the exact same question. Payload comparison cannot tell
    // the two cards apart — the server's card_id, minted per ask, can.
    let state = { ...legacy('chat-1'), activeSlot: 'chat-1' }
    state = reducer(state, setQuestionCard({ slot: 'chat-1', card_id: 'card-2', questions: JSON.parse(JSON.stringify(QUESTIONS)) }))
    expect(state.pendingQuestions['chat-1'].serverCardId).toBe('card-2')
    state = reducer(state, resolveQuestionCard({ card_id: 'card-1' }))
    expect(state.pendingQuestions['chat-1']).toBeDefined()
    // The live card is still retirable under its own identity.
    state = reducer(state, resolveQuestionCard({ card_id: 'card-2' }))
    expect(state.pendingQuestions['chat-1']).toBeUndefined()
  })

  it('a card_id retirement leaves a blocking (ask_id) card alone', () => {
    let state = { ...withCard('chat-1', 'blocked-ask'), activeSlot: 'chat-1' }
    state = reducer(state, resolveQuestionCard({ card_id: 'card-1' }))
    expect(state.pendingQuestions['chat-1']?.ask_id).toBe('blocked-ask')
  })

  it('an identity-guarded clear (dismiss) takes only the card it names', () => {
    // PendingQuestionCard.dismissStateless clears after the server confirmed the
    // dismissal; a newer card that replaced the dismissed one mid-flight stays.
    let state = legacy('chat-1')
    state = reducer(state, clearQuestionCard({ slot: 'chat-1', card_id: 'card-stale' }))
    expect(state.pendingQuestions['chat-1']).toBeDefined()
    state = reducer(state, clearQuestionCard({ slot: 'chat-1', card_id: 'card-1' }))
    expect(state.pendingQuestions['chat-1']).toBeUndefined()
  })

  it('an explicit clear (dismiss) does not spare a draft in progress', () => {
    // Unlike the server's retirement broadcast, Dismiss is the user's own
    // decision about this card, typed text included.
    let state = legacy('chat-1')
    state = reducer(state, setQuestionDraft({ slot: 'chat-1', active: true }))
    state = reducer(state, clearQuestionCard({ slot: 'chat-1', card_id: 'card-1' }))
    expect(state.pendingQuestions['chat-1']).toBeUndefined()
  })

  it('card submit clears the card; the later retirement broadcast is a no-op', () => {
    // The stateless submit flow clears the card itself while the answer send is
    // in flight; the server's retirement must not resurrect or double-delete.
    let state = { ...legacy('chat-1'), activeSlot: 'chat-1' }
    state = reducer(state, clearQuestionCard({ slot: 'chat-1' }))
    state = reducer(state, resolveQuestionCard({ card_id: 'card-1' }))
    expect(state.pendingQuestions['chat-1']).toBeUndefined()
  })

  it('queue_pop converts the row but never retires the card; cancel keeps it too', () => {
    // GPT rounds 4/7/8/12: queue_pop must NEVER retire the card. Four review
    // rounds each found a way sender-side queued retirement deletes the wrong
    // card (cancellable acceptance, indistinguishable hydrated system items,
    // duplicate rows from the hydration race, and a newer card arriving before
    // the pop). The server owns the retirement: the popped entry lands there as
    // a live user row, which retires the record and broadcasts
    // `question_card_resolved` by identity.
    let state = { ...legacy('chat-1'), activeSlot: 'chat-1' }
    state = reducer(state, appendQueuedMessage({ slot: 'chat-1', content: 'my queued answer', ts: 't1', queue_id: 'q-1' }))
    // Cancel path: card must survive.
    let cancelled = reducer(state, cancelQueuedMessage({ slot: 'chat-1', queue_id: 'q-1' }))
    expect(cancelled.pendingQuestions['chat-1']).toBeDefined()
    // Dispatch path: queue_pop converts the row and ALSO leaves the card —
    // the frame applier owns retirement (the popped turn's user frame).
    state = reducer(state, removeQueuedMessage({ slot: 'chat-1', content: 'my queued answer', queue_id: 'q-1' }))
    expect(state.messages.some(m => m.role === 'queued')).toBe(false)
    expect(state.pendingQuestions['chat-1']).toBeDefined()
  })

  it('a queue_pop with no matching client queued row leaves the card alone', () => {
    // Dequeued cron/subagent items never had a client queued row (no
    // queue_push for them) — their pops are inert for the card either way.
    let state = { ...legacy('chat-1'), activeSlot: 'chat-1' }
    state = reducer(state, removeQueuedMessage({ slot: 'chat-1', content: '[Cron notification] nightly', queue_id: 'q-cron' }))
    expect(state.pendingQuestions['chat-1']).toBeDefined()
  })

  it('a hydrated queued row converts at queue_pop without touching the card', () => {
    // The slot-detail `queue` field hydrates EVERY backend queue entry into a
    // client row — including queued cron/recovery items. Their pops convert
    // the row; the card is untouched (retirement is the frame applier's job).
    let state = { ...legacy('chat-1'), activeSlot: 'chat-1' }
    state = { ...state, messages: [...state.messages, { role: 'queued', content: '[Cron notification] nightly', cls: 'msg msg-queued', ts: 't1', meta: { queueId: 'q-hyd' } }] }
    state = reducer(state, removeQueuedMessage({ slot: 'chat-1', content: '[Cron notification] nightly', queue_id: 'q-hyd' }))
    expect(state.messages.some(m => m.role === 'queued')).toBe(false)
    expect(state.pendingQuestions['chat-1']).toBeDefined()
  })

  it('a delayed queue_push after hydration does not duplicate the row', () => {
    // GPT round-8: hydration can land BEFORE the queue_push for the same
    // entry. Appending blindly would leave two rows for one queue item.
    let state = { ...legacy('chat-1'), activeSlot: 'chat-1' }
    state = { ...state, messages: [...state.messages, { role: 'queued', content: 'my queued answer', cls: 'msg msg-queued', ts: 't1', meta: { queueId: 'q-1' } }] }
    state = reducer(state, appendQueuedMessage({ slot: 'chat-1', content: 'my queued answer', ts: 't2', queue_id: 'q-1' }))
    expect(state.messages.filter(m => m.role === 'queued')).toHaveLength(1)
  })

  it('inject and subagent frames deliberately leave the card alone', () => {
    // These roles start turns too (cron notifications, recovery resumes,
    // subagent completion events), but they interleave with a question the
    // agent may STILL be waiting on — an agent that spawns work, asks, and
    // ends its turn absorbs completion events while the question is live.
    // Clearing on them would delete the user's only UI for answering. Pinned
    // so widening QUESTION_RETIRING_ROLES is a deliberate decision, not drift.
    let state = { ...legacy('chat-1'), activeSlot: 'chat-1' }
    state = reducer(state, sseChatMessage({ slot: 'chat-1', role: 'inject', content: '[cron] nightly report ready' }))
    state = reducer(state, sseChatMessage({ slot: 'chat-1', role: 'subagent', content: '[Subagent completion event] done' }))
    expect(state.pendingQuestions['chat-1']).toBeDefined()
  })

  it('an active custom-answer draft blocks retirement; clearing it resumes', () => {
    // GPT round-9 / UX: the typed custom answer lives only in QuestionCard's
    // component state, so a retirement mid-typing would silently destroy the
    // user's work. While draftActive, even a `user` frame leaves the card;
    // once the draft is cleared, retirement resumes on the next one.
    let state = { ...legacy('chat-1'), activeSlot: 'chat-1' }
    state = reducer(state, setQuestionDraft({ slot: 'chat-1', active: true }))
    state = reducer(state, sseChatMessage({ slot: 'chat-1', role: 'user', content: 'something else entirely' }))
    expect(state.pendingQuestions['chat-1']).toBeDefined()
    // Draft cleared -> the next user frame retires the card.
    state = reducer(state, setQuestionDraft({ slot: 'chat-1', active: false }))
    state = reducer(state, sseChatMessage({ slot: 'chat-1', role: 'user', content: 'and another' }))
    expect(state.pendingQuestions['chat-1']).toBeUndefined()
  })


  it('a same-id re-delivery preserves draft protection', () => {
    // A reconnect re-lists the SAME card (/pending) while the user is typing a
    // custom answer: the entry — and its draftActive — must survive untouched,
    // or the next user frame silently destroys the draft.
    let state = { ...legacy('chat-1'), activeSlot: 'chat-1' }
    const questions = state.pendingQuestions['chat-1'].questions
    state = reducer(state, setQuestionDraft({ slot: 'chat-1', active: true }))
    state = reducer(state, setQuestionCard({ slot: 'chat-1', card_id: 'card-1', questions: JSON.parse(JSON.stringify(questions)) }))
    expect(state.pendingQuestions['chat-1'].draftActive).toBe(true)
    state = reducer(state, sseChatMessage({ slot: 'chat-1', role: 'user', content: 'unrelated send' }))
    expect(state.pendingQuestions['chat-1']).toBeDefined()
  })

  it('a new-id but identical replacement preserves draft protection; a different payload starts clean', () => {
    // The agent re-asks the identical question under a fresh card_id while the
    // user is typing. The component stays mounted (keyed by slot, and
    // QuestionCard resets only on a payload change), so the typed text
    // SURVIVES — the store must keep saying so, or the next user frame retires
    // a card that still holds unsent work. A different payload resets the
    // component (draft genuinely gone), so its entry starts clean.
    let state = { ...legacy('chat-1'), activeSlot: 'chat-1' }
    const questions = state.pendingQuestions['chat-1'].questions
    state = reducer(state, setQuestionDraft({ slot: 'chat-1', active: true }))
    state = reducer(state, setQuestionCard({ slot: 'chat-1', card_id: 'card-2', questions: JSON.parse(JSON.stringify(questions)) }))
    expect(state.pendingQuestions['chat-1'].serverCardId).toBe('card-2')
    expect(state.pendingQuestions['chat-1'].draftActive).toBe(true)
    state = reducer(state, sseChatMessage({ slot: 'chat-1', role: 'user', content: 'unrelated send' }))
    expect(state.pendingQuestions['chat-1']?.serverCardId).toBe('card-2')
    // Different payload: clean slate.
    const other = [{ question: 'Different?', options: [{ label: 'Yes' }] }]
    state = reducer(state, setQuestionCard({ slot: 'chat-1', card_id: 'card-3', questions: other }))
    expect(state.pendingQuestions['chat-1'].draftActive).toBeUndefined()
    // A draft that was already cleared is not resurrected by an identical re-ask.
    state = reducer(state, setQuestionDraft({ slot: 'chat-1', active: false }))
    state = reducer(state, setQuestionCard({ slot: 'chat-1', card_id: 'card-4', questions: other }))
    expect(state.pendingQuestions['chat-1'].draftActive).toBeUndefined()
  })
})


/**
 * Card replacement semantics in setQuestionCard: a websocket reconnect
 * re-dispatches the SAME still-pending card (same server identity, freshly
 * parsed questions payload) — that is not a new ask and must not churn the
 * entry. A card with a different identity replaces it.
 */
describe('card re-dispatch vs replacement', () => {
  it('a reconnect re-dispatch of the same card keeps the entry unchanged', () => {
    const state = stateless('chat-1', 'card-a')
    const state2 = reducer(state, setQuestionCard({ slot: 'chat-1', card_id: 'card-a', questions: JSON.parse(JSON.stringify(QUESTIONS)) }))
    expect(state2.pendingQuestions['chat-1']).toBe(state.pendingQuestions['chat-1'])
  })

  it('the same holds for a blocking ask re-listed by its ask_id', () => {
    const state = withCard('chat-1', 'ask-a')
    const state2 = reducer(state, setQuestionCard({ slot: 'chat-1', ask_id: 'ask-a', questions: JSON.parse(JSON.stringify(QUESTIONS)) }))
    expect(state2.pendingQuestions['chat-1']).toBe(state.pendingQuestions['chat-1'])
  })

  it('a genuinely different ask replaces the card', () => {
    let state = stateless('chat-1', 'card-a')
    state = reducer(state, setQuestionCard({
      slot: 'chat-1',
      card_id: 'card-b',
      questions: [{ question: 'A different ask?', options: [{ label: 'Yes' }] }],
    }))
    expect(state.pendingQuestions['chat-1']?.questions[0].question).toBe('A different ask?')
    expect(state.pendingQuestions['chat-1']?.serverCardId).toBe('card-b')
  })
})


/**
 * The reported defect, end to end: a monitored conductor session called
 * `ask_question`, ~10 auto-nudge cycles ran, and by the time the user came back
 * the card was gone and the question had never been answered.
 *
 * Two halves, one cause. The nudge frame retired the card in this reducer, and
 * the same role set on the server deleted the `/api/ask-question/pending`
 * record — so a reload had nothing to rehydrate from either. These tests pin
 * both halves: later turns leave the card alone, and the reload reconcile brings
 * it back into a fresh store.
 */
describe('an unanswered stateless card survives later turns and a reload', () => {
  const rehydrateFromPending = (
    slot: string,
    cardId: string,
    state: typeof initial,
  ) => {
    // What useWebSocket.syncPendingQuestions does with a /pending row: decide
    // through the real reconcile, then dispatch the add it returns.
    const { add, drop } = reconcileQuestions(
      state.pendingQuestions,
      state.pendingQuestions,
      [{ slot, card_id: cardId, questions: QUESTIONS }],
      [],
    )
    expect(drop).toEqual([])
    let next = state
    for (const q of add) {
      next = reducer(next, setQuestionCard({
        slot: q.slot as string,
        card_id: q.card_id,
        questions: q.questions as typeof QUESTIONS,
      }))
    }
    return next
  }

  it('survives ten nudge cycles and the assistant turns between them', () => {
    let state = { ...stateless('chat-1', 'card-a'), activeSlot: 'chat-1' }
    for (let cycle = 1; cycle <= 10; cycle++) {
      state = reducer(state, sseChatMessage({
        slot: 'chat-1', role: 'nudge', content: `[auto-nudge cycle ${cycle}]\ncheck the PR`,
        meta: { mid: `n-${cycle}` },
      }))
      state = reducer(state, sseChatMessage({
        slot: 'chat-1', role: 'assistant', content: `cycle ${cycle}: still red`,
        meta: { mid: `a-${cycle}` },
      }))
    }
    expect(state.pendingQuestions['chat-1']).toBeDefined()
    expect(state.pendingQuestions['chat-1'].questions).toEqual(QUESTIONS)
  })

  it('comes back into a fresh store from the /pending snapshot (reload)', () => {
    // A reload starts with an empty store: the card is a one-shot broadcast, so
    // this reconcile is the ONLY thing that can put it back on screen.
    const state = rehydrateFromPending('chat-1', 'card-a', { ...initial, activeSlot: 'chat-1' })
    expect(state.pendingQuestions['chat-1']).toBeDefined()
    expect(state.pendingQuestions['chat-1'].serverCardId).toBe('card-a')
  })

  it('stays after a reload that lands mid-nudge-loop', () => {
    // The exact sequence the user hit: ask, nudges, switch away (reload of the
    // pane state), come back — and then more nudges.
    let state = { ...stateless('chat-1', 'card-a'), activeSlot: 'chat-1' }
    state = reducer(state, sseChatMessage({ slot: 'chat-1', role: 'nudge', content: '[auto-nudge cycle 1]\ngo', meta: { mid: 'n-1' } }))
    state = rehydrateFromPending('chat-1', 'card-a', { ...initial, activeSlot: 'chat-1' })
    state = reducer(state, sseChatMessage({ slot: 'chat-1', role: 'nudge', content: '[auto-nudge cycle 2]\ngo', meta: { mid: 'n-2' } }))
    expect(state.pendingQuestions['chat-1']).toBeDefined()
  })

  it('is still retired by the user answering it', () => {
    // The fix must not turn the card into something that never goes away: the
    // user's own next message is exactly what it was waiting for.
    let state = { ...stateless('chat-1', 'card-a'), activeSlot: 'chat-1' }
    state = reducer(state, sseChatMessage({ slot: 'chat-1', role: 'nudge', content: '[auto-nudge cycle 1]\ngo' }))
    state = reducer(state, sseChatMessage({ slot: 'chat-1', role: 'user', content: 'use the assets branch' }))
    expect(state.pendingQuestions['chat-1']).toBeUndefined()
  })

  it('is still retired by an explicit dismiss after nudges', () => {
    // Dismiss (PendingQuestionCard.dismissStateless) is the other exit, and the
    // one that now carries the "stale card" case the nudge retirement covered.
    let state = { ...stateless('chat-1', 'card-a'), activeSlot: 'chat-1' }
    state = reducer(state, sseChatMessage({ slot: 'chat-1', role: 'nudge', content: '[auto-nudge cycle 1]\ngo' }))
    state = reducer(state, clearQuestionCard({ slot: 'chat-1', card_id: 'card-a' }))
    expect(state.pendingQuestions['chat-1']).toBeUndefined()
  })
})