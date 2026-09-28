import { describe, it, expect, vi, beforeEach } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'

import chatReducer, { switchSlot } from '../store/chatSlice'
import { api } from '../api/client'

vi.mock('../api/client', () => ({
  api: { chatSlotDetail: vi.fn() },
}))

/** The retained per-slot server count is the baseline the switch's coverage check
 *  compares against. When it is ABSENT that check cannot prove the bounded window
 *  overlaps the cache, and it answers by refetching the transcript UNBOUNDED --
 *  measured on a phone as one switch turning 305 loaded messages into 6,203, with
 *  ~287,000px of scroll range and the tab eventually killed by the browser.
 *
 *  So what this file pins is not a number, it is which responses are allowed to
 *  leave a baseline behind. Refusing every RUNNING response manufactured the very
 *  absence the refusal existed to avoid guessing from: a slot that streams for most
 *  of its life never records one, and then every switch into it takes the unbounded
 *  path. The distinction that matters is boundedness, not running-ness -- the
 *  handler collapses chunk runs before it slices, so a BOUNDED count is already in
 *  settled units, while the unbounded branch counts raw rows and a streaming read
 *  there is inflated by rows that fold at turn end. */

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
    // The regression this pins. A bounded read is collapsed by the handler before
    // slicing, so its count is comparable with a settled one -- and refusing it is
    // what left a streaming slot with no baseline and sent the next switch down the
    // unbounded path.
    detail.mockResolvedValue(reply({ running: true }))
    const store = makeStore()
    await store.dispatch(switchSlot('slot-live'))
    expect(totalFor(store, 'slot-live')).toBe(900)
  })

  it('still refuses a running count from the UNBOUNDED coverage retry', async () => {
    // The production shape from the device: a slot with rows cached but no baseline
    // asks a BOUNDED window, the coverage check cannot prove overlap, and the thunk
    // refetches UNBOUNDED. That second response counts raw rows, so a running one
    // must not be retained -- the first (bounded) one is what leaves the baseline,
    // and having it is what stops the retry happening again next time.
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
    expect(limits).toContain(undefined)
    // The retained count is the BOUNDED read's, not the inflated unbounded one.
    expect(totalFor(store, 'slot-retry')).toBe(900)
  })

  it('a settled unbounded read still records, so a fresh slot gets a baseline', async () => {
    detail.mockResolvedValue(reply())
    const store = makeStore()
    await store.dispatch(switchSlot('slot-fresh-settled'))
    expect(totalFor(store, 'slot-fresh-settled')).toBe(900)
  })
})
