import { describe, it, expect, vi, beforeEach } from 'vitest'

vi.mock('../api/client', async () => {
  const { ApiError } = await vi.importActual<typeof import('../api/apiError')>('../api/apiError')
  return { api: { answerQuestion: vi.fn(() => Promise.resolve({})) }, ApiError }
})

import { api, ApiError } from '../api/client'
import { resolveAskAfterSend } from '../lib/resolveAskAfterSend'
import { createTestStore } from './helpers'
import { setQuestionCard, setQuestionDraft, pendingQuestionFor } from '../store/chatSlice'
import { registerMainComposer } from '../utils/composerRestore'

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
    expect(store.getState().chat.questionsSettled['ask-1']).toBe(true)
    expect(answerQuestion).toHaveBeenCalledWith('ask-1', undefined, 'composer')
  })

  // Dismissed, not answered: Send promises a chat message, so the typed text has
  // to stay in the transcript rather than becoming a tool result. It is marked a
  // COMPOSER reply so the blocked tool tells the agent to read the next message
  // instead of reporting the question as declined.
  it('dismisses rather than submitting the typed text as the answer', async () => {
    const store = storeWithCard()
    await resolveAskAfterSend({ ok: true }, 'ask-1', store.dispatch)
    expect(answerQuestion).toHaveBeenCalledTimes(1)
    expect(answerQuestion.mock.calls[0]).toEqual(['ask-1', undefined, 'composer'])
  })

  // The queue cannot pop until the turn ends, and the turn cannot end while the
  // agent is blocked on this card — deferring would hold both for the full window.
  // The reason is 'queued', not 'composer': the message does NOT follow next, and
  // not a bare dismissal either, which would read as the user declining.
  it('resolves a QUEUED send too, so the two cannot deadlock', async () => {
    const store = storeWithCard()
    expect(await resolveAskAfterSend({ ok: true, queued: true }, 'ask-1', store.dispatch)).toBe(true)
    expect(cardIn(store)).toBeNull()
    expect(answerQuestion).toHaveBeenCalledWith('ask-1', undefined, 'queued')
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
    expect(store.getState().chat.questionsSettled['ask-1']).toBeUndefined()
  })

  it('keeps the card on a 5xx from the answer endpoint', async () => {
    answerQuestion.mockImplementationOnce(() => Promise.reject(new ApiError(500, 'boom')))
    const store = storeWithCard()
    expect(await resolveAskAfterSend({ ok: true }, 'ask-1', store.dispatch)).toBe(false)
    expect(cardIn(store)).not.toBeNull()
  })

  it('reports a failed release as a durable notice above the card, not a transcript row', async () => {
    answerQuestion.mockImplementationOnce(() => Promise.reject(new ApiError(500, 'boom')))
    const store = storeWithCard()
    expect(await resolveAskAfterSend({ ok: true }, 'ask-1', store.dispatch, 'chat-1')).toBe(false)
    const chat = (store.getState() as { chat: { slotMessages: Record<string, { role: string }[]>; restoredQuestionNotices: Record<string, { message: string; kind: string }> } }).chat
    expect(chat.restoredQuestionNotices['chat-1']).toEqual({
      message: 'The question card is still waiting. Answer or dismiss it. Your message was sent.',
      kind: 'release_failed',
    })
    // A transcript row is replaced by the refetch the send itself triggers, so the notice must not live there.
    expect((chat.slotMessages['chat-1'] ?? []).some((message) => message.role === 'error')).toBe(false)
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

  it.each(['answered', 'composer', 'queued'])(
    'does not restore a stale card draft when another window settled it as %s',
    async (reason) => {
      const body = JSON.stringify({ code: 'question_not_found', reason })
      answerQuestion.mockImplementationOnce(() => Promise.reject(new ApiError(404, 'no such ask', body)))
      const store = storeWithCard()
      store.dispatch(setQuestionDraft({ slot: 'chat-1', answers: { 'Where should the package live?': 'Local only' } }))
      const got: string[] = []
      const off = registerMainComposer((slot, text) => { got.push(`${slot}:${text}`); return true })
      try {
        expect(await resolveAskAfterSend({ ok: true }, 'ask-1', store.dispatch, 'chat-1', store.getState)).toBe(true)
      } finally { off() }
      expect(cardIn(store)).toBeNull()
      expect(store.getState().chat.questionsSettled['ask-1']).toBe(true)
      expect(got).toEqual([])
      expect(store.getState().chat.restoredQuestionNotices['chat-1']).toBeUndefined()
    },
  )

  it.each([
    { ending: 'expired', body: JSON.stringify({ code: 'question_not_found', reason: 'expired' }) },
    { ending: 'without a reason', body: JSON.stringify({ code: 'question_not_found' }) },
  ])('restores a stale card draft without settling when the ask ended $ending', async ({ body }) => {
    answerQuestion.mockImplementationOnce(() => Promise.reject(new ApiError(404, 'no such ask', body)))
    const store = storeWithCard()
    store.dispatch(setQuestionDraft({ slot: 'chat-1', answers: { 'Where should the package live?': 'Local only' } }))
    const got: string[] = []
    const off = registerMainComposer((slot, text) => { if (slot === 'chat-1') { got.push(text); return true } return false })
    try {
      expect(await resolveAskAfterSend({ ok: true }, 'ask-1', store.dispatch, 'chat-1', store.getState)).toBe(true)
    } finally { off() }
    expect(got).toEqual(['Local only'])
    expect(store.getState().chat.questionsSettled['ask-1']).toBeUndefined()
    expect(store.getState().chat.restoredQuestionNotices['chat-1']?.kind).toBe('restored')
  })

  // Picks made in the card while the send was in flight would otherwise vanish
  // with the card: the release hands them back the way a retired card does.
  it('hands a half-entered card answer back to the composer with a notice', async () => {
    const store = storeWithCard()
    store.dispatch(setQuestionDraft({ slot: 'chat-1', answers: { 'Where should the package live?': 'Local only' } }))
    const got: string[] = []
    const off = registerMainComposer((slot, text) => { if (slot === 'chat-1') { got.push(text); return true } return false })
    try {
      expect(await resolveAskAfterSend({ ok: true }, 'ask-1', store.dispatch, 'chat-1', store.getState)).toBe(true)
    } finally { off() }
    expect(cardIn(store)).toBeNull()
    expect(got).toEqual(['Local only'])
    expect(store.getState().chat.restoredQuestionNotices['chat-1']).toEqual({
      message: 'The agent stopped waiting for "Where should the package live?", so your answers weren\'t sent. They\'re in the composer; send them if you still want to.',
      kind: 'restored',
    })
  })

  it('restores nothing and shows no notice when the card held no picks', async () => {
    const store = storeWithCard()
    const got: string[] = []
    const off = registerMainComposer((slot, text) => { got.push(`${slot}:${text}`); return true })
    try {
      expect(await resolveAskAfterSend({ ok: true }, 'ask-1', store.dispatch, 'chat-1', store.getState)).toBe(true)
    } finally { off() }
    expect(cardIn(store)).toBeNull()
    expect(got).toEqual([])
    expect(store.getState().chat.restoredQuestionNotices['chat-1']).toBeUndefined()
  })
})
