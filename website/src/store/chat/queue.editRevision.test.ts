import { describe, expect, it } from 'vitest'
import reducer, { appendQueuedMessage, editQueuedMessage, normalizeQueueEntries } from '../chatSlice'
import { hydrateQueuedBubbles } from './queue'

const slot = 'member-order'
const ts = '2026-10-20T08:00:00Z'
const entry = { id: 'q-order', content: 'original' }
const push = { slot, content: entry.content, ts, queue_id: entry.id }
const cold = () => reducer(undefined, { type: '@@INIT' })
const rows = (state: ReturnType<typeof cold>) => state.slotMessages[slot] ?? []
const card = (state: ReturnType<typeof cold>) => rows(state).find(m => m.meta?.queueId === entry.id)

const queued = () => reducer(cold(), appendQueuedMessage(push))

describe('queue edit revision ordering', () => {
  it('records the revision the server assigned', () => {
    const state = reducer(queued(), editQueuedMessage({ slot, queue_id: entry.id, content: 'first', editRev: 1 }))
    expect(card(state)?.content).toBe('first')
    expect(card(state)?.meta?.editRev).toBe(1)
  })

  it('ignores a frame older than the applied revision', () => {
    let state = reducer(queued(), editQueuedMessage({ slot, queue_id: entry.id, content: 'second', editRev: 2 }))
    state = reducer(state, editQueuedMessage({ slot, queue_id: entry.id, content: 'first', editRev: 1 }))
    expect(card(state)?.content).toBe('second')
    expect(card(state)?.meta?.editRev).toBe(2)
  })

  it('applies a frame equal to the applied revision', () => {
    let state = reducer(queued(), editQueuedMessage({ slot, queue_id: entry.id, content: 'raw [attached_file 2]', editRev: 2 }))
    state = reducer(state, editQueuedMessage({ slot, queue_id: entry.id, content: 'server [attached_file 1]', editRev: 2, attachments: { files: ['/b.txt'] } }))
    expect(card(state)?.content).toBe('server [attached_file 1]')
    expect(card(state)?.meta?.files).toEqual(['/b.txt'])
  })

  it('applies a strictly newer frame', () => {
    let state = reducer(queued(), editQueuedMessage({ slot, queue_id: entry.id, content: 'first', editRev: 1 }))
    state = reducer(state, editQueuedMessage({ slot, queue_id: entry.id, content: 'second', editRev: 2 }))
    expect(card(state)?.content).toBe('second')
    expect(card(state)?.meta?.editRev).toBe(2)
  })

  it('applies an unrevisioned frame without changing the applied revision', () => {
    let state = reducer(queued(), editQueuedMessage({ slot, queue_id: entry.id, content: 'first', editRev: 1 }))
    state = reducer(state, editQueuedMessage({ slot, queue_id: entry.id, content: 'unrevisioned' }))
    expect(card(state)?.content).toBe('unrevisioned')
    expect(card(state)?.meta?.editRev).toBe(1)
  })

  it('orders each card separately', () => {
    const other = 'q-other'
    let state = reducer(queued(), appendQueuedMessage({ ...push, queue_id: other, content: 'other original' }))
    state = reducer(state, editQueuedMessage({ slot, queue_id: entry.id, content: 'mine second', editRev: 2 }))
    state = reducer(state, editQueuedMessage({ slot, queue_id: other, content: 'other first', editRev: 1 }))
    expect(card(state)?.content).toBe('mine second')
    expect(rows(state).find(m => m.meta?.queueId === other)?.content).toBe('other first')
  })
})

describe('revision after a slot-detail refetch', () => {
  it('keeps the served revision on the rebuilt row and ignores an older frame', () => {
    const queue = normalizeQueueEntries([{ id: entry.id, content: 'second', editRev: 2 }])
    expect(queue[0].editRev).toBe(2)
    let state = { ...cold(), slotMessages: { [slot]: hydrateQueuedBubbles([], queue) } }
    expect(card(state)?.meta?.editRev).toBe(2)
    state = reducer(state, editQueuedMessage({ slot, queue_id: entry.id, content: 'first', editRev: 1 }))
    expect(card(state)?.content).toBe('second')
  })

  it('drops a served revision that is not an integer', () => {
    expect(normalizeQueueEntries([{ id: entry.id, content: 'x', editRev: '2' }])[0].editRev).toBeUndefined()
  })
})
