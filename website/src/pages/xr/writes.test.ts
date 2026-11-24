import { beforeEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({
  memberThread: vi.fn(),
  createChatSlot: vi.fn(),
  sendChat: vi.fn(),
  decideApproval: vi.fn(),
  answerQuestion: vi.fn(),
}))
vi.mock('../../api/client', () => ({ api }))

import { boardAnswer, endedAsk, cardChatLeft, cardSend, performWrite, xrMessage } from './writes'
import type { XrNeed } from './model'

const JUNO = { bot: 'juno', thread: 'member-juno' }
const MIRA = { bot: 'mira', thread: 'member-mira' }

describe('the new chat card’s send', () => {
  beforeEach(() => {
    for (const f of Object.values(api)) f.mockReset()
    api.sendChat.mockResolvedValue({ ok: true, status: 200 })
    api.memberThread.mockResolvedValue({ slot_key: 'member-juno' })
    api.createChatSlot.mockResolvedValue({ key: 'chat-new' })
  })

  it('goes to the Bot’s own thread with the id as the request’s own key, and a second call with that id sends nothing', async () => {
    expect(await cardSend('hello', { bot: 'juno', thread: '' }, 'card-1')).toBe('member-juno')
    expect(await cardSend('hello', { bot: 'juno', thread: '' }, 'card-1')).toBe('member-juno')
    expect(api.memberThread).toHaveBeenCalledTimes(1)
    expect(api.sendChat).toHaveBeenCalledTimes(1)
    expect(api.sendChat.mock.calls[0][0]).toBe(xrMessage('hello'))
    expect(api.sendChat.mock.calls[0][1]).toBe('member-juno')
    expect(api.sendChat.mock.calls[0][7]).toEqual({ idempotencyKey: 'send-card-1' })
  })

  it('a call while the first is under way gets the first’s own promise: one request, and the first’s failure is both’s', async () => {
    let answer: (r: { ok: boolean; status: number }) => void = () => undefined
    api.sendChat.mockReturnValue(new Promise(done => { answer = done }))
    const first = cardSend('hello', JUNO, 'card-2')
    const second = cardSend('hello', JUNO, 'card-2')
    expect(second).toBe(first)
    answer({ ok: false, status: 503 })
    await expect(first).rejects.toThrow('send refused (503)')
    await expect(second).rejects.toThrow('send refused (503)')
    expect(api.sendChat).toHaveBeenCalledTimes(1)
  })

  it('tried again after a failure, sends the same words to the same thread under the same key', async () => {
    api.sendChat.mockResolvedValueOnce({ ok: false, status: 503 })
    await expect(cardSend('hello', 'new', 'card-3')).rejects.toThrow('send refused (503)')
    expect(cardChatLeft('card-3')).toBe('chat-new')
    expect(await cardSend('hello', 'new', 'card-3')).toBe('chat-new')
    expect(api.createChatSlot).toHaveBeenCalledTimes(1)
    expect(api.sendChat).toHaveBeenCalledTimes(2)
    expect(api.sendChat.mock.calls[1].slice(0, 2)).toEqual([xrMessage('hello'), 'chat-new'])
    expect(api.sendChat.mock.calls[1][7]).toEqual(api.sendChat.mock.calls[0][7])
    expect(cardChatLeft('card-3')).toBe('')
  })

  it('a retry goes only to the card’s own target: another target under the id sends nothing', async () => {
    api.sendChat.mockResolvedValueOnce({ ok: false, status: 503 })
    await expect(cardSend('hello', MIRA, 'card-4')).rejects.toThrow('send refused (503)')
    // Thrown at Mira and refused; then asked to go into the open, or to another Bot: neither is sent anywhere.
    await expect(cardSend('hello', 'new', 'card-4')).rejects.toThrow('sent as something else')
    await expect(cardSend('hello', JUNO, 'card-4')).rejects.toThrow('sent as something else')
    expect(api.sendChat).toHaveBeenCalledTimes(1)
    expect(api.createChatSlot).not.toHaveBeenCalled()
    expect(await cardSend('hello', MIRA, 'card-4')).toBe('member-mira')
    expect(api.sendChat.mock.calls[1][1]).toBe('member-mira')
  })

  it('a new chat that failed never goes to a Bot afterwards', async () => {
    api.sendChat.mockResolvedValueOnce({ ok: false, status: 503 })
    await expect(cardSend('hello', 'new', 'card-5')).rejects.toThrow('send refused (503)')
    await expect(cardSend('hello', { bot: 'juno', thread: '' }, 'card-5')).rejects.toThrow('sent as something else')
    expect(api.sendChat).toHaveBeenCalledTimes(1)
    expect(api.memberThread).not.toHaveBeenCalled()
  })

  it('other words under a used id send nothing, after a failure and after a send', async () => {
    api.sendChat.mockResolvedValueOnce({ ok: false, status: 503 })
    await expect(cardSend('hello', JUNO, 'card-6')).rejects.toThrow('send refused (503)')
    await expect(cardSend('hello there', JUNO, 'card-6')).rejects.toThrow('sent as something else')
    expect(await cardSend(' hello ', JUNO, 'card-6')).toBe('member-juno')
    await expect(cardSend('goodbye', JUNO, 'card-6')).rejects.toThrow('sent as something else')
    expect(api.sendChat).toHaveBeenCalledTimes(2)
  })

  it('never sends an empty card, one with no id, or one with no target', async () => {
    await expect(cardSend('   ', JUNO, 'card-7')).rejects.toThrow('nothing to send')
    await expect(cardSend('hello', JUNO, '')).rejects.toThrow('nothing to send')
    await expect(cardSend('hello', { bot: '', thread: 'x' }, 'card-8')).rejects.toThrow('nothing to send')
    expect(api.sendChat).not.toHaveBeenCalled()
  })

  it('a Bot’s thread that cannot be opened fails the send, and a later try opens it', async () => {
    api.memberThread.mockRejectedValueOnce(new Error('403'))
    await expect(cardSend('hello', { bot: 'juno', thread: '' }, 'card-9')).rejects.toThrow('403')
    expect(api.sendChat).not.toHaveBeenCalled()
    expect(cardChatLeft('card-9')).toBe('')
    expect(await cardSend('hello', { bot: 'juno', thread: '' }, 'card-9')).toBe('member-juno')
  })
})

describe('the land’s send is as it was', () => {
  beforeEach(() => {
    for (const f of Object.values(api)) f.mockReset()
    api.sendChat.mockResolvedValue({ ok: true, status: 200 })
  })

  it('sends each time it is called, with no key, and never with no Bot', async () => {
    await performWrite({ kind: 'send', bot: 'juno', slot: 'member-juno', text: 'hello' })
    await performWrite({ kind: 'send', bot: 'juno', slot: 'member-juno', text: 'hello' })
    expect(api.sendChat).toHaveBeenCalledTimes(2)
    expect(api.sendChat.mock.calls[0]).toEqual(['hello', 'member-juno'])
    await expect(performWrite({ kind: 'send', bot: '', slot: '', text: 'hello' })).rejects.toThrow('nothing to send')
  })
})

describe('a question board\'s answer', () => {
  const need = { kind: 'question', askId: 'ask-1', questions: [{ header: '', question: 'A?', options: ['x'] }] } as unknown as XrNeed
  const gone = (reason?: string) => Object.assign(new Error('404'), { status: 404, body: JSON.stringify({ code: 'question_not_found', ...(reason ? { reason } : {}) }) })
  beforeEach(() => api.answerQuestion.mockReset())

  it('reads how a wait had already ended from the gateway\'s refusal', () => {
    expect(endedAsk(gone('answered'))).toBe('answered')
    expect(endedAsk(gone('dismissed'))).toBe('dismissed')
    for (const reason of ['expired', 'withdrawn', undefined]) expect(endedAsk(gone(reason)), reason).toBe('ended')
    // Any other failure is a failure: the board takes the stamp again.
    expect(endedAsk({ status: 503, body: '' })).toBeNull()
    expect(endedAsk({ status: 404, body: JSON.stringify({ code: 'other' }) })).toBeNull()
  })

  it('carries his reply id, and reads an answer someone else gave first as answered elsewhere (review, 07:46)', async () => {
    api.answerQuestion.mockResolvedValue({ ok: true })
    expect(await boardAnswer(need, { 'A?': 'x' }, 'resp-9')).toBe('approved')
    expect(api.answerQuestion).toHaveBeenLastCalledWith('ask-1', { 'A?': 'x' }, undefined, 'resp-9')
    const lost = { status: 409, body: JSON.stringify({ code: 'already_resolved', decision: { decision_id: 'q:ask-1', outcome: 'answered' } }) }
    api.answerQuestion.mockRejectedValue(lost)
    expect(await boardAnswer(need, { 'A?': 'x' }, 'resp-9')).toBe('ended')
    expect(await boardAnswer(need, null, 'no-1')).toBe('ended')
    api.answerQuestion.mockReset()
  })

  it('takes an answer whose first reply was lost as answered, as the stamp takes a lost race (review small 2)', async () => {
    api.answerQuestion.mockResolvedValue({ ok: true })
    expect(await boardAnswer(need, { 'A?': 'x' })).toBe('approved')
    api.answerQuestion.mockRejectedValue(gone('answered'))
    expect(await boardAnswer(need, { 'A?': 'x' })).toBe('approved')
    api.answerQuestion.mockRejectedValue(gone('expired'))
    expect(await boardAnswer(need, { 'A?': 'x' })).toBe('ended')
    api.answerQuestion.mockReset()
  })
})
