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
import reducer, { setQuestionCard, resolveQuestionCard, clearQuestionCard } from '../store/chatSlice'

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
