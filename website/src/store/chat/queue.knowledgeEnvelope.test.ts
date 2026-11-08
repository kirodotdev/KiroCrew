/**
 * A queued send's knowledge envelope mark (`meta.knowledgeEnvelope`) rides the
 * `queue_push` card and cold hydration of the queue, so the card can find the
 * envelope by the mark rather than by parsing the text. A malformed mark is
 * dropped.
 */
import { describe, expect, it } from 'vitest'
import reducer, { appendQueuedMessage, hydrateSlotMessages } from '../chatSlice'

const slot = 'kb-slot'
const ts = '2026-11-07T10:00:00Z'
const mark = { len: 42, hash: '1x2y3z' }
const cold = () => reducer(undefined, { type: '@@INIT' })
const queued = (state: ReturnType<typeof cold>) => (state.slotMessages[slot] ?? []).find(m => m.role === 'queued')

describe('queued knowledge envelope mark', () => {
  it('rides the queue_push card and cold queue hydration', () => {
    const pushed = reducer(cold(), appendQueuedMessage({ slot, content: 'wire', ts, queue_id: 'q1', meta: { knowledgeEnvelope: mark } }))
    expect(queued(pushed)?.meta?.knowledgeEnvelope).toEqual(mark)
    const hydrated = reducer(cold(), {
      type: hydrateSlotMessages.type,
      payload: { slot, messages: [], queue: [{ id: 'q2', content: 'wire', meta: { knowledgeEnvelope: mark } }], bounded: false },
    })
    expect(queued(hydrated)?.meta?.knowledgeEnvelope).toEqual(mark)
  })

  it('drops a mark that is not an integer length and a string hash', () => {
    for (const knowledgeEnvelope of [{ len: 1.5, hash: 'a' }, { len: 3, hash: 7 }, 'mark', null]) {
      const state = reducer(cold(), appendQueuedMessage({ slot, content: 'wire', ts, queue_id: 'q3', meta: { knowledgeEnvelope } }))
      expect(queued(state)?.meta?.knowledgeEnvelope).toBeUndefined()
    }
  })
})
