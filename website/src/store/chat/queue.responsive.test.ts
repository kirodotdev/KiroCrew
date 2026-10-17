import { describe, it, expect, vi, afterEach } from 'vitest'
import reducer, { appendSlotMessage, appendQueuedMessage, removeQueuedMessage, cancelQueuedMessage, markSendUnconfirmed, selectSendAccepted, selectSendConfirmed } from '../chatSlice'
import type { RootState } from '../index'
import { hydrateQueuedBubbles } from './queue'
import { normalizeQueueEntries } from './wire'

const SLOT = 'member-exact'
const TS = '2026-10-14T08:12:00Z'
afterEach(() => vi.useRealTimers())

function initial(active: boolean) {
  let state = { ...reducer(undefined, { type: '@@INIT' }), activeSlot: active ? SLOT : 'other' }
  state = reducer(state, appendSlotMessage({ slot: SLOT, message: { role: 'user', content: 'same text', cls: '', ts: TS, meta: { sendId: 'mine', responsiveSend: true } } }))
  return state
}
const rows = (state: ReturnType<typeof initial>) => state.activeSlot === SLOT ? state.messages : state.slotMessages[SLOT]
const root = (state: ReturnType<typeof initial>) => ({ chat: state }) as RootState
const push = { slot: SLOT, content: 'same text', ts: TS, queue_id: 'q1', meta: { sendId: 'mine', files: ['/work/My File.txt'], quote: { role: 'assistant', text: 'quoted' } } }
const pop = { slot: SLOT, content: 'same text', queue_id: 'q1', meta: push.meta }

describe.each([true, false])('exact queued-send identity (active=%s)', (active) => {
  it('queue acceptance replaces pending text without claiming execution; cancel remains available', () => {
    let state = initial(active)
    state = reducer(state, markSendUnconfirmed({ slot: SLOT, sendId: 'mine' }))
    state = reducer(state, appendQueuedMessage(push))
    expect(rows(state)).toHaveLength(1)
    expect(rows(state)[0]).toMatchObject({ role: 'queued', meta: { queueId: 'q1', ...push.meta } })
    expect(rows(state)[0].meta?.deliveryUnconfirmed).toBeUndefined()
    expect(selectSendAccepted(root(state), SLOT, 'mine')).toBe(true)
    expect(selectSendConfirmed(root(state), SLOT, 'mine')).toBe(false)
    state = reducer(state, cancelQueuedMessage({ slot: SLOT, queue_id: 'q1' }))
    expect(rows(state)).toHaveLength(0)
  })

  it('drain proves execution and duplicates or stale pushes do not append again', () => {
    let state = reducer(initial(active), appendQueuedMessage(push))
    state = reducer(state, removeQueuedMessage(pop))
    state = reducer(state, removeQueuedMessage(pop))
    state = reducer(state, appendQueuedMessage(push))
    expect(rows(state)).toHaveLength(1)
    expect(rows(state)[0]).toMatchObject({ role: 'user', meta: push.meta })
    expect(selectSendConfirmed(root(state), SLOT, 'mine')).toBe(true)
  })

  it('a pop can reconcile its exact provisional row even when push was missed', () => {
    const state = reducer(initial(active), removeQueuedMessage(pop))
    expect(rows(state)).toHaveLength(1)
    expect(rows(state)[0].role).toBe('user')
    expect(selectSendConfirmed(root(state), SLOT, 'mine')).toBe(true)
  })

  it('does not deduplicate identical text from a different send', () => {
    const state = reducer(initial(active), appendQueuedMessage({ ...push, meta: { sendId: 'other-send' } }))
    expect(rows(state)).toHaveLength(2)
    expect(selectSendAccepted(root(state), SLOT, 'mine')).toBe(false)
  })

  it('rehydrates the exact queue identity and preserves files and quote', () => {
    vi.setSystemTime(new Date(TS))
    const state = initial(active)
    const queue = normalizeQueueEntries([{ id: 'q1', content: push.content, meta: push.meta }])
    const hydrated = hydrateQueuedBubbles(rows(state), queue)
    expect(hydrated).toHaveLength(1)
    expect(hydrated[0]).toMatchObject({ role: 'queued', meta: { queueId: 'q1', ...push.meta } })
    expect(hydrateQueuedBubbles(hydrated, queue)).toEqual(hydrated)
  })
})

it.each([null, ['mine'], 42, '', 'x'.repeat(129), 'bad id'])('ignores malformed queue correlation %j', (sendId) => {
  const state = reducer(initial(true), appendQueuedMessage({ ...push, meta: { sendId } }))
  expect(rows(state)).toHaveLength(2)
  expect(rows(state)[1].meta?.sendId).toBeUndefined()
})

describe.each([true, false])('a crewmate send keeps its place through queue and drain (active=%s)', (active) => {
  const later = { role: 'assistant', content: 'reply to earlier', cls: '', ts: '2026-10-14T08:12:05Z' } as const
  function sentThenReplied() {
    let state = initial(active)
    state = reducer(state, appendSlotMessage({ slot: SLOT, message: { ...later } }))
    return state
  }

  it('the queue entry takes the bubble\'s index, ts and key; a later reply stays below it', () => {
    const state = reducer(sentThenReplied(), appendQueuedMessage({ ...push, ts: '2026-10-14T08:12:09Z' }))
    expect(rows(state).map(m => m.role)).toEqual(['queued', 'assistant'])
    expect(rows(state)[0]).toMatchObject({ ts: TS, meta: { queueId: 'q1', sendId: 'mine', clientTs: TS, responsiveSend: true } })
  })

  it('the drain rebuilds the user row in the same place with the same ts and key', () => {
    let state = reducer(sentThenReplied(), appendQueuedMessage(push))
    state = reducer(state, removeQueuedMessage(pop))
    expect(rows(state).map(m => [m.role, m.content])).toEqual([['user', 'same text'], ['assistant', 'reply to earlier']])
    expect(rows(state)[0]).toMatchObject({ ts: TS, meta: { sendId: 'mine', clientTs: TS } })
    expect(rows(state)[0].meta?.responsiveSend).toBeUndefined()
    expect(rows(state)[0].meta?.optimistic).toBeUndefined()
  })

  it('a pop that missed its push still drains the bubble in place', () => {
    const state = reducer(sentThenReplied(), removeQueuedMessage(pop))
    expect(rows(state).map(m => m.role)).toEqual(['user', 'assistant'])
    expect(rows(state)[0]).toMatchObject({ ts: TS, meta: { clientTs: TS } })
  })

  it('a refresh that still lists the entry leaves it where it was sent', () => {
    const state = reducer(sentThenReplied(), appendQueuedMessage(push))
    const queue = normalizeQueueEntries([{ id: 'q1', content: push.content, meta: push.meta }])
    const hydrated = hydrateQueuedBubbles(rows(state), queue)
    expect(hydrated.map(m => m.role)).toEqual(['queued', 'assistant'])
    expect(hydrated[0].meta?.clientTs).toBe(TS)
    expect(hydrateQueuedBubbles(rows(state), []).map(m => m.role)).toEqual(['assistant'])
  })

  it('an ordinary (Sessions) optimistic send is still replaced at the end', () => {
    let state = { ...reducer(undefined, { type: '@@INIT' }), activeSlot: active ? SLOT : 'other' }
    state = reducer(state, appendSlotMessage({ slot: SLOT, message: { role: 'user', content: 'same text', cls: '', ts: TS, meta: { sendId: 'mine' } } }))
    state = reducer(state, appendSlotMessage({ slot: SLOT, message: { ...later } }))
    state = reducer(state, appendQueuedMessage(push))
    expect(rows(state).map(m => m.role)).toEqual(['assistant', 'queued'])
    state = reducer(state, removeQueuedMessage(pop))
    expect(rows(state).map(m => m.role)).toEqual(['assistant', 'user'])
  })
})
