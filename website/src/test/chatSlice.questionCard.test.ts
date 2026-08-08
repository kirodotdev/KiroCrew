/**
 * Tests for the ask_question card reducers.
 *
 * Cards are keyed BY SLOT. A single global card meant two agents calling
 * ask_question in different slots would evict each other, and the loser blocked
 * until its timeout with no card ever rendered.
 *
 * `question_card_resolved` arrives whenever a pending question stops waiting —
 * answered, timed out, or cancelled. It carries the ask_id so a LATE resolved
 * event from an earlier question cannot wipe a newer card.
 */
import { describe, it, expect } from 'vitest'
import reducer, { setQuestionCard, retireStatelessQuestion, captureStatelessCard, resolveQuestionCard, clearQuestionCard, sseChatMessage, appendMessage, appendSlotMessage } from '../store/chatSlice'

const initial = reducer(undefined, { type: '@@INIT' })

const QUESTIONS = [
  { question: 'Which approach?', header: 'SCOPE', options: [{ label: 'A' }, { label: 'B' }] },
]

function withCard(slot: string, askId?: string, state = initial) {
  return reducer(state, setQuestionCard({ slot, ask_id: askId, questions: QUESTIONS }))
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

  it('legacy cards without an ask_id are unaffected by resolve', () => {
    // The pre-existing AskUserQuestion sniff path broadcasts no ask_id.
    const state = reducer(withCard('chat-1', undefined), resolveQuestionCard({ ask_id: 'abc' }))
    expect(state.pendingQuestions['chat-1']).toBeDefined()
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
 * on it) is answered by "the next message". So the frame that starts the
 * slot's next turn — a real user message, or an auto-nudge cycle moving the
 * session on — consumes that answer channel and must drop the card. The
 * observed defect: a monitored session asked via ask_question, the nudge loop
 * injected the next turn, and the orphaned card sat above the composer
 * indefinitely, inviting an answer no turn was waiting for.
 *
 * Server-owned cards (ask_id) are exempt: their lifecycle is the
 * `question_card_resolved` broadcast, and clearing one on a mid-turn steer
 * frame would strand the blocked tool call with no card to answer.
 */
describe('stateless card staleness on turn-consuming frames', () => {
  const legacy = (slot: string, state = initial) => withCard(slot, undefined, state)

  it('a user frame on the slot drops its stateless card (active path)', () => {
    let state = { ...legacy('chat-1'), activeSlot: 'chat-1' }
    state = reducer(state, sseChatMessage({ slot: 'chat-1', role: 'user', content: 'use the assets branch' }))
    expect(state.pendingQuestions['chat-1']).toBeUndefined()
  })

  it('a nudge frame on the slot drops its stateless card (active path)', () => {
    // The reported bug: auto-nudge cycle fires, agent moves on, card lingers.
    let state = { ...legacy('chat-1'), activeSlot: 'chat-1' }
    state = reducer(state, sseChatMessage({ slot: 'chat-1', role: 'nudge', content: '[auto-nudge cycle 3]\ncheck the PR' }))
    expect(state.pendingQuestions['chat-1']).toBeUndefined()
  })

  it('drops the stateless card on the non-active (grid pane) path too', () => {
    let state = { ...legacy('chat-1'), activeSlot: 'other-slot' }
    state = reducer(state, sseChatMessage({ slot: 'chat-1', role: 'nudge', content: '[auto-nudge cycle 2]\ngo' }))
    expect(state.pendingQuestions['chat-1']).toBeUndefined()
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
    state = reducer(state, setQuestionCard({ slot: 'chat-1', questions: QUESTIONS }))
    state = reducer(state, sseChatMessage(frame)) // replay of the SAME mid
    expect(state.pendingQuestions['chat-1']).toBeDefined()
  })

  // The sender's own user frame is NEVER echoed over the wire (slot.append
  // skips the broadcast for `user` rows the composer already rendered
  // optimistically), so the SEND PATH retires the card — but only after the
  // server confirms delivery (retireStatelessQuestion, dispatched on ok or
  // queued). The optimistic appends themselves must NOT retire: a failed
  // send (offline, 5xx) leaves the session unchanged, and deleting the card
  // then would strand a question the agent is still inviting an answer to.
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

  it('confirmed delivery retires the stateless card (retireStatelessQuestion)', () => {
    let state = { ...legacy('chat-1'), activeSlot: 'chat-1' }
    state = reducer(state, retireStatelessQuestion({ slot: 'chat-1', expected: JSON.stringify(QUESTIONS) }))
    expect(state.pendingQuestions['chat-1']).toBeUndefined()
  })

  it('a stale send completion cannot retire a newer card (payload mismatch)', () => {
    // Race: send starts while card A is pending → a new turn replaces it with
    // card B → the slow completion for A must not delete B.
    let state = { ...legacy('chat-1'), activeSlot: 'chat-1' }
    const capturedAtSend = JSON.stringify(QUESTIONS) // card A
    state = reducer(state, setQuestionCard({
      slot: 'chat-1',
      questions: [{ question: 'A newer ask?', options: [{ label: 'Yes' }] }],
    }))
    state = reducer(state, retireStatelessQuestion({ slot: 'chat-1', expected: capturedAtSend }))
    expect(state.pendingQuestions['chat-1']?.questions[0].question).toBe('A newer ask?')
  })

  it('confirmed delivery leaves a server-owned (ask_id) card alone', () => {
    let state = { ...withCard('chat-1', 'blocked-ask'), activeSlot: 'chat-1' }
    state = reducer(state, retireStatelessQuestion({ slot: 'chat-1', expected: JSON.stringify(QUESTIONS) }))
    expect(state.pendingQuestions['chat-1']?.ask_id).toBe('blocked-ask')
  })

  it('confirmed delivery on another slot leaves the card alone', () => {
    let state = legacy('chat-1')
    state = reducer(state, retireStatelessQuestion({ slot: 'chat-2', expected: JSON.stringify(QUESTIONS) }))
    expect(state.pendingQuestions['chat-1']).toBeDefined()
  })

  // The send sites must call this at ENTRY, before their first await — the
  // helper pins what gets captured: only a stateless card produces a payload.
  it('captureStatelessCard serializes a stateless card and nothing else', () => {
    expect(captureStatelessCard(legacy('chat-1').pendingQuestions, 'chat-1')).toBe(JSON.stringify(QUESTIONS))
    expect(captureStatelessCard(withCard('chat-1', 'blocked-ask').pendingQuestions, 'chat-1')).toBeNull()
    expect(captureStatelessCard(initial.pendingQuestions, 'chat-1')).toBeNull()
    expect(captureStatelessCard(legacy('chat-1').pendingQuestions, null)).toBeNull()
  })

  it('capture → clear (card submit) → confirmed completion is a no-op', () => {
    // The stateless submit flow clears the card itself while the answer send
    // is in flight; the completion must not resurrect or double-delete.
    let state = { ...legacy('chat-1'), activeSlot: 'chat-1' }
    const captured = captureStatelessCard(state.pendingQuestions, 'chat-1')
    state = reducer(state, clearQuestionCard({ slot: 'chat-1' }))
    state = reducer(state, retireStatelessQuestion({ slot: 'chat-1', expected: captured! }))
    expect(state.pendingQuestions['chat-1']).toBeUndefined()
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
})

/**
 * Card replacement semantics in setQuestionCard: a websocket reconnect
 * re-dispatches the SAME still-pending card with a freshly parsed (structurally
 * equal, referentially new) questions payload — that is not a new ask and must
 * not churn the entry. A genuinely different ask replaces the card.
 */
describe('card re-dispatch vs replacement', () => {
  it('a reconnect re-dispatch of the same card keeps the entry unchanged', () => {
    const state = withCard('chat-1')
    const state2 = reducer(state, setQuestionCard({ slot: 'chat-1', questions: JSON.parse(JSON.stringify(QUESTIONS)) }))
    expect(state2.pendingQuestions['chat-1']).toBe(state.pendingQuestions['chat-1'])
  })

  it('a genuinely different ask replaces the card', () => {
    let state = withCard('chat-1')
    state = reducer(state, setQuestionCard({
      slot: 'chat-1',
      questions: [{ question: 'A different ask?', options: [{ label: 'Yes' }] }],
    }))
    expect(state.pendingQuestions['chat-1']?.questions[0].question).toBe('A different ask?')
  })
})
