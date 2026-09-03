/** `switchSlot` must bound its initial fetch instead of pulling the whole
 *  chained transcript.
 *
 *  Opening a slot resets the pane's messages and cursor, so the first page is
 *  simply page one of the same pagination `loadOlderMessages` runs — bounded to
 *  `OLDER_PAGE_LIMIT`, with `has_more`/`next_before` from the response seeding
 *  the cursor the older-page walk starts from. The bound must never make older
 *  history unreachable: a truncating first page that could not page back would
 *  be worse than the unbounded fetch it replaces.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'

import { api } from '../api/client'
import chatReducer, { OLDER_PAGE_LIMIT, OLDER_WALK_PAGE_LIMIT, loadOlderMessages, switchSlot } from '../store/chatSlice'

vi.mock('../api/client')

function makeStore(extra: Record<string, unknown> = {}) {
  const base = chatReducer(undefined, { type: '@@INIT' })
  return configureStore({
    reducer: { chat: chatReducer },
    preloadedState: { chat: { ...base, ...extra } },
  })
}

const msg = (content: string, ts: string, mid: string) =>
  ({ role: 'assistant', content, cls: '', ts, meta: { mid } })

const mockDetail = (value: Record<string, unknown>) =>
  (api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue(
    { messages: [], running: false, has_more: false, total: 0, queue: [], ...value },
  )

describe('switchSlot initial load bound', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('bounds the slot-open fetch to the older-page limit', async () => {
    mockDetail({})
    const store = makeStore()
    await store.dispatch(switchSlot('slot-a') as never)
    expect(api.chatSlotDetail).toHaveBeenCalledWith('slot-a', OLDER_PAGE_LIMIT)
    // The backend clamps limit to [1, 500]; a value outside that range would
    // either 400 or silently shrink, so the constant must stay inside it.
    expect(OLDER_PAGE_LIMIT).toBeGreaterThan(0)
    expect(OLDER_PAGE_LIMIT).toBeLessThanOrEqual(500)
  })

  // The bound is only acceptable because the remainder stays reachable: the
  // bounded response's cursor must arm loadOlderMessages, not strand it.
  it('seeds the older-paging cursor from the bounded first page', async () => {
    mockDetail({
      messages: [msg('newest window', '2026-08-23T10:00:00Z', 'm-2')],
      has_more: true,
      next_before: 250,
      total: 251,
    })
    const store = makeStore()
    await store.dispatch(switchSlot('slot-a') as never)
    const st = store.getState().chat
    expect(st.slotHasMore).toBe(true)
    expect(st.slotOldestIndex).toBe(250)
    // And the walk actually starts from that cursor.
    mockDetail({ messages: [], has_more: false, next_before: 0, total: 251 })
    await store.dispatch(loadOlderMessages() as never)
    expect(api.chatSlotDetail).toHaveBeenLastCalledWith(
      'slot-a', OLDER_WALK_PAGE_LIMIT, 250, expect.anything(),
    )
  })

  it('older paging walks the bounded first page back to the oldest message', async () => {
    mockDetail({
      messages: [msg('newer', '2026-08-23T10:00:00Z', 'm-2')],
      has_more: true,
      next_before: 1,
      total: 2,
    })
    const store = makeStore()
    await store.dispatch(switchSlot('slot-a') as never)
    mockDetail({
      messages: [msg('oldest', '2026-08-23T09:00:00Z', 'm-1')],
      has_more: false,
      next_before: 0,
      total: 2,
    })
    await store.dispatch(loadOlderMessages() as never)
    const st = store.getState().chat
    expect(st.messages.map(m => m.content)).toEqual(['oldest', 'newer'])
    // The walk terminates: nothing older is claimed once the head is reached.
    expect(st.slotHasMore).toBe(false)
    expect(st.slotOldestIndex).toBe(0)
  })

  it('loads a slot shorter than the limit whole, with no earlier-page claim', async () => {
    mockDetail({
      messages: [
        msg('one', '2026-08-23T09:00:00Z', 'm-1'),
        msg('two', '2026-08-23T10:00:00Z', 'm-2'),
        msg('three', '2026-08-23T11:00:00Z', 'm-3'),
      ],
      has_more: false,
      next_before: 0,
      total: 3,
    })
    const store = makeStore()
    await store.dispatch(switchSlot('slot-a') as never)
    const st = store.getState().chat
    expect(st.messages.map(m => m.content)).toEqual(['one', 'two', 'three'])
    expect(st.slotHasMore).toBe(false)
    expect(st.slotOldestIndex).toBe(0)
  })

  // A streaming slot gets the larger WALK page, never the unbounded corpus:
  // on a busy session the unbounded switch measured 6.2MB / ~1s server-side
  // per switch, and the kept-head cut reconciles a bounded page against the
  // (usually fresh) cache.
  // Reconciled with upstream's shrink contract (boundedRefetchShrink.test.ts):
  // a STREAMING or PAINTED slot switches unbounded — a bounded page is a
  // window, and unseen server growth can push it clear of a small cache. Only
  // a fresh slot (nothing painted) takes the one-page bound.
  it('switches to a streaming slot unbounded', async () => {
    mockDetail({ running: true })
    const store = makeStore({ slotRun: { 'slot-a': { state: 'streaming' } } })
    await store.dispatch(switchSlot('slot-a') as never)
    // fetchSlotDetail omits the limit argument entirely when unbounded.
    expect(api.chatSlotDetail).toHaveBeenCalledWith('slot-a')
  })

  it('switches to a fresh idle slot bounded to one page', async () => {
    mockDetail({})
    const store = makeStore({ slotRun: { 'slot-a': { state: 'idle' } } })
    await store.dispatch(switchSlot('slot-a') as never)
    expect(api.chatSlotDetail).toHaveBeenCalledWith('slot-a', OLDER_PAGE_LIMIT)
  })
})
