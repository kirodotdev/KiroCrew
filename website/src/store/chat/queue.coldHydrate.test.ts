import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import reducer, { appendQueuedMessage, appendSlotMessage, cancelQueuedMessage, editQueuedMessage, hydrateSlotMessages, removeQueuedMessage, selectSendAccepted, selectSendConfirmed } from '../chatSlice'
import { sseSlots } from '../dashboardSlice'
import type { RootState } from '../index'

const slot = 'member-cold'
const ts = '2026-10-14T08:00:00Z'
const meta = { sendId: 'cold-send', files: ['/work/My File.txt'], dirs: ['/work/My Dir'], quote: { role: 'assistant', text: 'quoted' } }
const entry = { id: 'cold-q', content: 'held request', meta }
const push = { slot, content: entry.content, ts, queue_id: entry.id, meta }
const history = { role: 'assistant', content: 'history', ts, meta: { mid: 'history' } }
const cold = () => reducer(undefined, { type: '@@INIT' })
const rows = (state: ReturnType<typeof cold>) => state.slotMessages[slot] ?? []
// Dispatch the wire payload as a plain action so the red test also compiles
// before hydrateSlotMessages accepts the queue field.
const hydrate = (queue: unknown = [entry], messages = [history], bounded = true) => ({
  type: hydrateSlotMessages.type, payload: { slot, messages, queue, bounded, hasMore: bounded, nextBefore: 50, total: 51 },
})

beforeEach(() => vi.setSystemTime(new Date(ts)))
afterEach(() => vi.useRealTimers())

describe('cold pane queue hydration', () => {
  it('restores a cancellable queue once with exact send identity, attachments and quote', () => {
    let state = reducer(cold(), hydrate())
    expect(rows(state)).toHaveLength(2)
    expect(rows(state)[1]).toMatchObject({ role: 'queued', meta: { queueId: entry.id, ...meta } })
    expect(selectSendAccepted({ chat: state } as RootState, slot, meta.sendId)).toBe(true)
    expect(selectSendConfirmed({ chat: state } as RootState, slot, meta.sendId)).toBe(false)
    state = reducer(state, hydrate())
    state = reducer(state, appendQueuedMessage(push))
    expect(rows(state).filter(m => m.content === entry.content)).toHaveLength(1)
    state = reducer(state, cancelQueuedMessage({ slot, queue_id: entry.id }))
    expect(rows(state)).toEqual([history])
  })

  it('keeps live pushes and edits instead of overwriting them with the snapshot', () => {
    let state = reducer(cold(), appendQueuedMessage(push))
    state = reducer(state, editQueuedMessage({ slot, queue_id: entry.id, content: 'new text', attachments: {} }))
    state = reducer(state, appendQueuedMessage({ ...push, queue_id: 'new-q', content: 'new request', meta: { sendId: 'new-send' } }))
    state = reducer(state, hydrate([entry, { id: 'unseen-q', content: 'still waiting' }]))
    expect(rows(state).filter(m => m.role === 'queued').map(m => m.content)).toEqual(['still waiting', 'new text', 'new request'])
    expect(rows(state).find(m => m.meta?.queueId === entry.id)?.meta?.files).toBeUndefined()
  })

  it.each([false, true])('does not resurrect cancellation before hydration (push=%s)', (hasPush) => {
    let state = hasPush ? reducer(cold(), appendQueuedMessage(push)) : cold()
    state = reducer(state, cancelQueuedMessage({ slot, queue_id: entry.id }))
    state = reducer(state, hydrate([entry, { id: 'other-q', content: 'other request' }]))
    expect(rows(state).filter(m => m.role === 'queued').map(m => m.meta?.queueId)).toEqual(['other-q'])
  })

  it.each([false, true])('keeps a completed send exactly once before hydration (push=%s)', (hasPush) => {
    let state = hasPush ? reducer(cold(), appendQueuedMessage(push)) : cold()
    state = reducer(state, removeQueuedMessage(push))
    state = reducer(state, hydrate())
    expect(rows(state).filter(m => m.content === entry.content)).toHaveLength(1)
    expect(rows(state).find(m => m.content === entry.content)).toMatchObject({ role: 'user', meta })
    expect(selectSendConfirmed({ chat: state } as RootState, slot, meta.sendId)).toBe(true)
    state = reducer(state, removeQueuedMessage(push))
    expect(rows(state).filter(m => m.content === entry.content)).toHaveLength(1)
  })

  it('does not duplicate a popped row that the fetched history already contains', () => {
    const done = { role: 'user', content: entry.content, ts, meta: { mid: 'executed', ...meta } }
    let state = reducer(cold(), removeQueuedMessage(push))
    state = reducer(state, hydrate([entry], [history, done]))
    expect(rows(state)).toEqual([history, done])
  })

  it('does not resurrect a popped system entry whose drain writes its own row', () => {
    let state = reducer(cold(), removeQueuedMessage({ ...push, drain_writes_row: true }))
    state = reducer(state, hydrate())
    expect(rows(state)).toEqual([history])
  })

  it('collapses a provisional row into the fetched queue, never by matching text alone', () => {
    let state = reducer(cold(), appendSlotMessage({ slot, message: { role: 'user', content: entry.content, ts, meta: { sendId: meta.sendId } } }))
    state = reducer(state, appendSlotMessage({ slot, message: { role: 'user', content: entry.content, ts, meta: { sendId: 'other-send' } } }))
    state = reducer(state, hydrate())
    expect(rows(state).filter(m => m.meta?.sendId === meta.sendId)).toHaveLength(1)
    expect(rows(state).find(m => m.meta?.sendId === meta.sendId)?.role).toBe('queued')
    expect(rows(state).find(m => m.meta?.sendId === 'other-send')?.role).toBe('user')
  })

  it('preserves live queue state through the bounded-to-unbounded history upgrade', () => {
    let state = reducer(cold(), hydrate())
    state = reducer(state, cancelQueuedMessage({ slot, queue_id: entry.id }))
    state = reducer(state, appendQueuedMessage({ ...push, queue_id: 'new-q', meta: { sendId: 'new-send' } }))
    state = reducer(state, hydrate([entry], [{ ...history, content: 'older', meta: { mid: 'older' } }, history], false))
    expect(rows(state).map(m => m.content)).toEqual(['older', 'history', entry.content])
    expect(rows(state).filter(m => m.role === 'queued').map(m => m.meta?.queueId)).toEqual(['new-q'])
    expect(state.slotPaneHasMore[slot]).toBe(false)
    expect(state.slotPaneBounded[slot]).toBeUndefined()
  })

  it('leaves active Sessions messages and queue unchanged', () => {
    const state = { ...cold(), activeSlot: slot, messages: [history] }
    expect(reducer(state, hydrate())).toBe(state)
  })

  it('releases pre-hydration retirement state when a slot is removed', () => {
    let state = reducer(cold(), cancelQueuedMessage({ slot, queue_id: entry.id }))
    state = reducer(state, sseSlots([{ key: 'other' }] as never))
    state = reducer(state, hydrate())
    expect(rows(state).filter(m => m.role === 'queued')).toHaveLength(1)
  })
})
