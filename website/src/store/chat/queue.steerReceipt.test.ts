import { describe, expect, it } from 'vitest'
import reducer, { appendQueuedMessage, removeQueuedMessage } from '../chatSlice'

const slot = 'steer-receipt'
const ts = '2026-10-19T00:00:00Z'
const strip = { steer: { decision: 'auto', id: 'd-1' } }
const cold = () => reducer(undefined, { type: '@@INIT' })
const rows = (state: ReturnType<typeof cold>) => state.slotMessages[slot] ?? []

describe('queue_pop of a requeued steer', () => {
  it('keeps the decision receipt the pop frame carries on the rebuilt row', () => {
    let state = reducer(cold(), appendQueuedMessage({ slot, content: 'go north', ts, queue_id: 'q-1', meta: { sendId: 's-1' } }))
    state = reducer(state, removeQueuedMessage({ slot, content: 'go north', queue_id: 'q-1', meta: { sendId: 's-1', decisions_strip: strip } }))
    const row = rows(state).find(m => m.content === 'go north')
    expect(row?.role).toBe('user')
    expect(row?.meta?.decisions_strip).toEqual(strip)
  })

  it('ignores a decisions_strip that is not an object', () => {
    let state = reducer(cold(), appendQueuedMessage({ slot, content: 'go south', ts, queue_id: 'q-2', meta: { sendId: 's-2' } }))
    state = reducer(state, removeQueuedMessage({ slot, content: 'go south', queue_id: 'q-2', meta: { sendId: 's-2', decisions_strip: 'forged' } }))
    expect(rows(state).find(m => m.content === 'go south')?.meta?.decisions_strip).toBeUndefined()
  })
})

describe('queue_pop whose sendId matches a steer row', () => {
  it('still rebuilds the drained row, so the text survives the old row being superseded', () => {
    const seeded = structuredClone(cold())
    seeded.slotMessages[slot] = [{
      role: 'user', content: 'go east', ts, cls: 'msg msg-u',
      meta: { mid: 'm-steer', steer: true, steerState: 'requeued', sendId: 's-3' },
    }]
    let state = reducer(seeded, appendQueuedMessage({ slot, content: 'go east', ts: '2026-10-19T00:01:00Z', queue_id: 'q-3', meta: { sendId: 's-3' } }))
    state = reducer(state, removeQueuedMessage({ slot, content: 'go east', queue_id: 'q-3', meta: { sendId: 's-3' } }))
    const users = rows(state).filter(m => m.role === 'user' && m.content === 'go east')
    expect(users).toHaveLength(2)
    expect(users[0].meta?.mid).toBe('m-steer')
    expect(users[1].meta?.steer).toBeUndefined()
  })
})

describe('queue_pop whose sendId matches an optimistic steer bubble', () => {
  it('replaces the bubble, so a steer that raced the turn end shows once', () => {
    const seeded = structuredClone(cold())
    seeded.slotMessages[slot] = [{
      role: 'user', content: 'go west', ts, cls: 'msg msg-u',
      meta: { steer: true, optimistic: true, sendId: 's-4' },
    }]
    // Only the pop: the drain's `queue_pop` reduces before any card for it.
    const state = reducer(seeded, removeQueuedMessage({ slot, content: 'go west', queue_id: 'q-4', meta: { sendId: 's-4' } }))
    const users = rows(state).filter(m => m.role === 'user' && m.content === 'go west')
    expect(users).toHaveLength(1)
    expect(users[0].meta?.optimistic).toBeUndefined()
  })
})
