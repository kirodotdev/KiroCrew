import { describe, it, expect, vi, beforeEach } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'

import chatReducer, { refreshSlot, setActiveSlot, switchSlot } from '../store/chatSlice'
import { api } from '../api/client'

vi.mock('../api/client', () => ({
  api: { chatSlotDetail: vi.fn() },
}))

/** The retained per-slot server count is the baseline a later WARM compares
 *  against to tell a remote rewind from a page built too early to carry a row.
 *
 *  What this file pins is not a number, it is which responses are allowed to
 *  leave a baseline behind. Refusing every RUNNING response manufactures an
 *  absence: a slot that streams for most of its life never records one, so a
 *  rewind of it is never recognised and the warm re-attaches discarded turns.
 *  Retaining a running count RAW is wrong the other way: the bounded corpus keeps
 *  every PENDING permission card in its count and drops answered ones, so a
 *  mid-turn count exceeds the settled one by the cards resolved at turn end, and
 *  the next settled warm reads that fall as a truncation and drops a live tail.
 *  So a running count is retained only from a BOUNDED read (collapsed by the
 *  handler before it slices) and only in SETTLED units -- the pending cards, which
 *  are in the page, subtracted (`settledBoundedTotal`, #10005). The unbounded
 *  branch's `total` counts RAW rows -- a `done` per finished turn even when
 *  settled -- so it is never used; a COMPLETE unbounded read derives the settled
 *  count from its prepared rows instead (`settledTotalOf`), and a caller that has
 *  a bounded count also carries it through the retry as `comparableTotal`. */

const detail = api.chatSlotDetail as unknown as ReturnType<typeof vi.fn>

const makeStore = () => configureStore({ reducer: { chat: chatReducer } })
const msgs = (n: number) => Array.from({ length: n }, (_, i) => ({ role: 'user', content: `m${i}`, ts: `2026-01-01T00:00:${String(i).padStart(2, '0')}Z` }))

/** The NEWEST `n` rows of a longer transcript, starting at server index `start`.
 *  A bounded read takes the most recent slice, so its oldest row is NEWER than a
 *  longer cache's oldest -- which is the shape that makes a coverage hole real.
 *  `msgs(n)` alone always starts at index 0, so a window built from it overlaps
 *  every cache completely and no hole can be observed. Same timestamp formatting as
 *  `msgs`, deliberately, so the two order consistently against each other. */
const msgsFrom = (start: number, n: number) =>
  Array.from({ length: n }, (_, i) => ({ role: 'user', content: `m${start + i}`, ts: `2026-01-01T00:00:${String(start + i).padStart(2, '0')}Z` }))

const reply = (over: Record<string, unknown> = {}) => ({
  messages: msgs(120), running: false, has_more: true, total: 900, next_before: 780, queue: [], ...over,
})

const totalFor = (store: ReturnType<typeof makeStore>, slot: string) =>
  (store.getState().chat as unknown as { slotServerTotal?: Record<string, number> }).slotServerTotal?.[slot]

describe('retained server total: which responses may leave a baseline', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('records the count from a settled bounded response', async () => {
    detail.mockResolvedValue(reply())
    const store = makeStore()
    await store.dispatch(switchSlot('slot-settled'))
    expect(totalFor(store, 'slot-settled')).toBe(900)
  })

  it('records a RUNNING response too, as long as the read was bounded', async () => {
    // A bounded read is collapsed by the handler before slicing, so with its
    // pending cards subtracted its count is comparable with a settled one -- and
    // refusing it is what left a streaming slot with no baseline.
    detail.mockResolvedValue(reply({ running: true }))
    const store = makeStore()
    await store.dispatch(switchSlot('slot-live'))
    expect(totalFor(store, 'slot-live')).toBe(900)
  })

  it('subtracts the pending permission cards a running bounded page carries', async () => {
    // The bounded corpus keeps pending cards (answered ones are dropped), so a
    // mid-turn `total` is settled + pending. Retaining it raw would make the next
    // settled warm read the cards\' resolution as a truncation.
    const pending = (id: string) => ({ role: 'permission', content: '', ts: '2026-01-01T00:02:00Z', meta: { approval_id: id } })
    detail.mockResolvedValue(reply({ running: true, messages: [...msgs(118), pending('a-1'), pending('a-2')], total: 902 }))
    const store = makeStore()
    await store.dispatch(switchSlot('slot-cards'))
    expect(totalFor(store, 'slot-cards')).toBe(900)
  })

  it('still refuses a running count from the UNBOUNDED coverage retry', async () => {
    // A slot with rows cached asks a BOUNDED window; the coverage check compares
    // rows, observes the hole, and the thunk refetches UNBOUNDED. That second
    // response counts raw rows, so a running one must not be retained -- the
    // first (bounded) one is what leaves the baseline, carried through the retry.
    const store = makeStore()
    store.dispatch({ type: 'chat/hydrateSlotMessages', payload: { slot: 'slot-retry', messages: msgs(305) } })
    let call = 0
    detail.mockImplementation((_slot: string, limit?: number) => {
      call += 1
      // 1st: bounded window == what the tab holds. 2nd: the unbounded retry.
      // The bounded window is the newest 120 of 900, so it sits clear of the 305-row
      // cache and the coverage check OBSERVES the hole. The unbounded retry answers
      // with raw rows, which is the count that must not be retained.
      return Promise.resolve(reply({
        running: true,
        total: call === 1 ? 900 : 6203,
        messages: limit === undefined ? msgs(400) : msgsFrom(780, 120),
      }))
    })
    await store.dispatch(switchSlot('slot-retry'))
    const limits = detail.mock.calls.filter((c: unknown[]) => c[0] === 'slot-retry').map((c: unknown[]) => c[1])
    // Asserted on the RECORDED calls, never inside the mock: an expect() that throws
    // in there rejects the thunk, and an absent total would then "pass" for the
    // wrong reason -- which is exactly how the first draft of this test went green.
    expect(limits[0]).toBeTypeOf('number')
    expect(limits).toContain(undefined)
    // The retained count is the BOUNDED read's, not the inflated unbounded one.
    expect(totalFor(store, 'slot-retry')).toBe(900)
  })

  it('a complete UNBOUNDED read derives its settled count from the prepared rows, never its raw total', async () => {
    // refreshSlot on an EMPTY active view is the one unbounded first read left. Its
    // `total` counts raw rows (a `done` per finished turn), but its `messages` are
    // the whole prepared transcript, so the durable rows among them are the settled
    // count -- here 120 user rows against a raw 900.
    detail.mockResolvedValue(reply({ has_more: false }))
    const store = makeStore()
    store.dispatch(setActiveSlot('slot-fresh-unbounded'))
    await store.dispatch(refreshSlot('slot-fresh-unbounded'))
    const limits = detail.mock.calls.filter((c: unknown[]) => c[0] === 'slot-fresh-unbounded').map((c: unknown[]) => c[1])
    expect(limits).toEqual([undefined])
    expect(totalFor(store, 'slot-fresh-unbounded')).toBe(120)
  })
})
