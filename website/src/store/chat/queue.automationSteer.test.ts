/**
 * A work-ledger wake the running turn did not take comes back as a queue card
 * marked `automationSteer`, on the live `queue_push` and on cold hydration alike.
 * A crewmate's chat draws a queued card as the person's own bubble unless it is
 * injected, so without the mark the wake drew as a message the person typed.
 */
import { describe, expect, it } from 'vitest'
import reducer, { appendQueuedMessage, editQueuedMessage, hydrateSlotMessages } from '../chatSlice'
import { withQueuedAsBubbles } from '../../components/chat/crewmateBubbles'

const slot = 'bot-slot'
const ts = '2026-11-20T20:59:27Z'
const wake = '[work-ledger wake] item=it_1 status=question\n\n[work-ledger] read the ledger'
const cold = () => reducer(undefined, { type: '@@INIT' })
const rows = (state: ReturnType<typeof cold>) => state.slotMessages[slot] ?? []
const drawn = (state: ReturnType<typeof cold>) => {
  const all = rows(state)
  return withQueuedAsBubbles(all, all.filter(m => m.role === 'queued')).map(m => [m.role, m.content])
}

describe('requeued automation steer in a crewmate chat', () => {
  it('draws no bubble from the live queue card', () => {
    const state = reducer(cold(), appendQueuedMessage({ slot, content: wake, ts, queue_id: 'q1', meta: { requeuedSteer: true, automationSteer: true } }))
    expect(rows(state)[0].meta?.automationSteer).toBe(true)
    expect(drawn(state)).toEqual([])
  })

  it('draws no bubble from cold queue hydration', () => {
    const state = reducer(cold(), {
      type: hydrateSlotMessages.type,
      payload: { slot, messages: [], queue: [{ id: 'q2', content: wake, meta: { automationSteer: true } }], bounded: false },
    })
    expect(rows(state).find(m => m.role === 'queued')?.meta?.automationSteer).toBe(true)
    expect(drawn(state)).toEqual([])
  })

  it('still draws the person\'s own requeued steer', () => {
    const state = reducer(cold(), appendQueuedMessage({ slot, content: 'my words', ts, queue_id: 'q3', meta: { requeuedSteer: true } }))
    expect(drawn(state)).toEqual([['user', 'my words']])
  })

  it('becomes the editor\'s words after an edit, as on the server', () => {
    let state = reducer(cold(), appendQueuedMessage({ slot, content: wake, ts, queue_id: 'q4', meta: { requeuedSteer: true, automationSteer: true } }))
    state = reducer(state, editQueuedMessage({ slot, queue_id: 'q4', content: 'my words' }))
    expect(rows(state)[0].meta?.automationSteer).toBeUndefined()
    expect(drawn(state)).toEqual([['user', 'my words']])
  })
})
