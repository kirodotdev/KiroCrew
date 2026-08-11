import { describe, it, expect, vi, beforeEach } from 'vitest'

vi.mock('../api/client', () => {
  class ApiError extends Error {
    status: number
    constructor(status: number, message = 'api error') { super(message); this.status = status }
  }
  return { api: { answerQuestion: vi.fn(() => Promise.resolve({})) }, ApiError }
})

import { api, ApiError } from '../api/client'
import { resolveAskAfterSend } from '../lib/resolveAskAfterSend'
import { createTestStore } from './helpers'
import { setQuestionCard, pendingQuestionFor } from '../store/chatSlice'

/**
 * Answering in the COMPOSER while a blocking card is on screen.
 *
 * The reported failure is the card outliving that send: it stays on screen and
 * the agent keeps waiting out its window, because a blocking card is deliberately
 * excluded from the stateless card's store-only retirement. Only the endpoint call
 * actually unblocks the agent, so these assert on `answerQuestion` — a revert that
 * merely hid the card would leave the reducer suites green.
 */

const QUESTIONS = [
  { question: 'Where should the package live?', options: [{ label: 'Private repo' }, { label: 'Local only' }] },
]

const answerQuestion = api.answerQuestion as unknown as ReturnType<typeof vi.fn>

function storeWithCard(askId = 'ask-1') {
  const store = createTestStore()
  store.dispatch(setQuestionCard({ slot: 'chat-1', ask_id: askId, questions: QUESTIONS }))
  return store
}

const cardIn = (store: ReturnType<typeof createTestStore>) =>
  pendingQuestionFor((store.getState() as { chat: { pendingQuestions: never } }).chat.pendingQuestions, 'chat-1')

describe('composer send with a blocking card pending', () => {
  beforeEach(() => { answerQuestion.mockClear(); answerQuestion.mockImplementation(() => Promise.resolve({})) })

  it('takes the card off screen and unblocks the agent', async () => {
    const store = storeWithCard()
    expect(cardIn(store)).not.toBeNull()

    const resolved = await resolveAskAfterSend({ ok: true }, 'ask-1', store.dispatch)

    expect(resolved).toBe(true)
    expect(cardIn(store)).toBeNull()
    expect(answerQuestion).toHaveBeenCalledWith('ask-1')
  })

  // Dismissed, not answered: Send promises a chat message, so the typed text has
  // to stay in the transcript rather than becoming a tool result.
  it('dismisses rather than submitting the typed text as the answer', async () => {
    const store = storeWithCard()
    await resolveAskAfterSend({ ok: true }, 'ask-1', store.dispatch)
    expect(answerQuestion).toHaveBeenCalledTimes(1)
    expect(answerQuestion.mock.calls[0]).toEqual(['ask-1'])
  })

  // The queue cannot pop until the turn ends, and the turn cannot end while the
  // agent is blocked on this card — deferring would hold both for the full window.
  it('resolves a QUEUED send too, so the two cannot deadlock', async () => {
    const store = storeWithCard()
    expect(await resolveAskAfterSend({ ok: true, queued: true }, 'ask-1', store.dispatch)).toBe(true)
    expect(cardIn(store)).toBeNull()
    expect(answerQuestion).toHaveBeenCalledWith('ask-1')
  })

  it('keeps the card when the server rejected the send', async () => {
    const store = storeWithCard()
    expect(await resolveAskAfterSend({ ok: false }, 'ask-1', store.dispatch)).toBe(false)
    expect(cardIn(store)).not.toBeNull()
    expect(answerQuestion).not.toHaveBeenCalled()
  })

  // A stateless card has no ask_id and nothing blocked on it; it retires through
  // the store path instead, so this must not fire a network resolution for it.
  it('ignores a stateless card', async () => {
    const store = createTestStore()
    store.dispatch(setQuestionCard({ slot: 'chat-1', questions: QUESTIONS }))
    expect(await resolveAskAfterSend({ ok: true }, null, store.dispatch)).toBe(false)
    expect(cardIn(store)).not.toBeNull()
    expect(answerQuestion).not.toHaveBeenCalled()
  })

  // The agent is only released once the endpoint says so. Removing the card on a
  // failed answer call would leave it blocked for its whole window with nothing
  // pending on screen — a silent stall, and the only repair affordance deleted.
  it('KEEPS the card when the dismissal call fails, since the agent is still blocked', async () => {
    answerQuestion.mockImplementationOnce(() => Promise.reject(new Error('offline')))
    const store = storeWithCard()
    expect(await resolveAskAfterSend({ ok: true }, 'ask-1', store.dispatch)).toBe(false)
    expect(cardIn(store)).not.toBeNull()
  })

  it('keeps the card on a 5xx from the answer endpoint', async () => {
    answerQuestion.mockImplementationOnce(() => Promise.reject(new ApiError(500, 'boom')))
    const store = storeWithCard()
    expect(await resolveAskAfterSend({ ok: true }, 'ask-1', store.dispatch)).toBe(false)
    expect(cardIn(store)).not.toBeNull()
  })

  // A 404 IS proof the wait is already gone (answered, dismissed, timed out, or
  // the slot was reset), so the card is stale and must not be left parked.
  it('retires the card on a 404, which proves the wait is already gone', async () => {
    answerQuestion.mockImplementationOnce(() => Promise.reject(new ApiError(404, 'no such ask')))
    const store = storeWithCard()
    expect(await resolveAskAfterSend({ ok: true }, 'ask-1', store.dispatch)).toBe(true)
    expect(cardIn(store)).toBeNull()
  })
})
