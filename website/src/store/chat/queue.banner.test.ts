/**
 * A scripted send's `meta.banner` survives the live queue path: the `queue_push`
 * card, cold hydration of the queue, and the `queue_pop` rebuild of the user row
 * (no `chat_message` echo follows a user row, so the rebuild IS the row). An edit
 * that changes the text drops the label, as the server does.
 */
import { describe, expect, it } from 'vitest'
import reducer, { appendQueuedMessage, editQueuedMessage, hydrateSlotMessages, removeQueuedMessage } from '../chatSlice'

const slot = 'banner-slot'
const ts = '2026-11-03T19:00:00Z'
const cold = () => reducer(undefined, { type: '@@INIT' })
const rows = (state: ReturnType<typeof cold>) => state.slotMessages[slot] ?? []

describe('queued banner', () => {
  it('rides the queue card and the rebuilt user row', () => {
    let state = reducer(cold(), appendQueuedMessage({ slot, content: 'full order', ts, queue_id: 'q1', meta: { banner: 'Short label' } }))
    expect(rows(state)[0].meta?.banner).toBe('Short label')

    state = reducer(state, removeQueuedMessage({ slot, content: 'full order', queue_id: 'q1', meta: { banner: 'Short label' } }))
    const [row] = rows(state)
    expect(row.role).toBe('user')
    expect(row.content).toBe('full order')
    expect(row.meta?.banner).toBe('Short label')
  })

  it('comes back from cold queue hydration', () => {
    const state = reducer(cold(), {
      type: hydrateSlotMessages.type,
      payload: { slot, messages: [], queue: [{ id: 'q2', content: 'full order', meta: { banner: 'Short label' } }], bounded: false },
    })
    const queued = rows(state).find(m => m.role === 'queued')
    expect(queued?.meta?.banner).toBe('Short label')
  })

  it('is dropped by an edit that changes the text', () => {
    let state = reducer(cold(), appendQueuedMessage({ slot, content: 'full order', ts, queue_id: 'q3', meta: { banner: 'Short label' } }))
    state = reducer(state, editQueuedMessage({ slot, queue_id: 'q3', content: 'changed order' }))
    expect(rows(state)[0].meta?.banner).toBeUndefined()
  })

  it('counts the cap in code points, as the server does', () => {
    const banner = 'x'.repeat(490) + '😀'.repeat(6)
    const state = reducer(cold(), appendQueuedMessage({ slot, content: 'full order', ts, queue_id: 'q5', meta: { banner } }))
    expect(rows(state)[0].meta?.banner).toBe(banner)
  })

  it('ignores a label that is blank, too long or not text', () => {
    for (const banner of ['   ', 'x'.repeat(501), 7]) {
      const state = reducer(cold(), appendQueuedMessage({ slot, content: 'full order', ts, queue_id: 'q4', meta: { banner } }))
      expect(rows(state)[0].meta?.banner).toBeUndefined()
    }
  })
})
