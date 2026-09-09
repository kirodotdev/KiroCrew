import { describe, expect, it, beforeEach } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'
import { pinnedMarkerListener } from '../store/pinnedMarkerOwner'
import { fetchSlots } from '../store/dashboardSlice'
import {
  PINNED_SESSION_ORDER_KEY,
  PINNED_SESSION_ORDER_MANUAL_KEY,
  markPinnedSessionOrderManual,
  readPinnedSessionOrderIsManual,
} from '../utils/pinnedSessionOrder'
import { publishPinMutationKeysInFlight } from '../utils/pinMutationsInFlight'

function store() {
  return configureStore({
    reducer: { dashboard: (state = { slotsLoaded: true }) => state },
    middleware: getDefault => getDefault().prepend(pinnedMarkerListener.middleware),
  })
}

const fulfilled = (payload: unknown, requestId: string) =>
  ({ type: fetchSlots.fulfilled.type, payload, meta: { requestId, arg: undefined } })
const pending = (requestId: string) =>
  ({ type: fetchSlots.pending.type, payload: undefined, meta: { requestId, arg: undefined } })

describe('a reconnect that arrives as a refetch reply', () => {
  beforeEach(() => {
    localStorage.clear()
    publishPinMutationKeysInFlight([])
  })

  it('clears a stale marker when every pinned session went away while offline', () => {
    const s = store()
    localStorage.setItem(PINNED_SESSION_ORDER_KEY, JSON.stringify(['a', 'b']))
    markPinnedSessionOrderManual()

    // The reconnect's first membership read is the refetch, and no live frame follows it.
    s.dispatch(pending('reconnect-1'))
    s.dispatch(fulfilled([{ key: 'c', pinned: false }], 'reconnect-1'))

    expect(localStorage.getItem(PINNED_SESSION_ORDER_MANUAL_KEY)).toBe(null)
    expect(readPinnedSessionOrderIsManual()).toBe(false)
  })

  it('still refuses a pre-snapshot empty reply, so a blip does not discard the arrangement', () => {
    const s = configureStore({
      reducer: { dashboard: (state = { slotsLoaded: false }) => state },
      middleware: getDefault => getDefault().prepend(pinnedMarkerListener.middleware),
    })
    localStorage.setItem(PINNED_SESSION_ORDER_KEY, JSON.stringify(['a', 'b']))
    markPinnedSessionOrderManual()

    s.dispatch(pending('reconnect-2'))
    s.dispatch(fulfilled([], 'reconnect-2'))

    expect(readPinnedSessionOrderIsManual()).toBe(true)
  })

  it('refuses a reply that predates this tab\u2019s unanswered pins', () => {
    const s = store()
    localStorage.setItem(PINNED_SESSION_ORDER_KEY, JSON.stringify(['a', 'b']))
    markPinnedSessionOrderManual()
    publishPinMutationKeysInFlight(['a', 'b'])

    s.dispatch(pending('pre-pin'))
    s.dispatch(fulfilled([{ key: 'a', pinned: false }, { key: 'b', pinned: false }], 'pre-pin'))

    expect(readPinnedSessionOrderIsManual()).toBe(true)
  })

  it('refuses a reply a newer accepted snapshot has overtaken', () => {
    let generation = 0
    const s = configureStore({
      reducer: { dashboard: (state = { slotsLoaded: true, slotsGeneration: 0 }) => ({ ...state, slotsGeneration: generation }) },
      middleware: getDefault => getDefault().prepend(pinnedMarkerListener.middleware),
    })
    localStorage.setItem(PINNED_SESSION_ORDER_KEY, JSON.stringify(['a', 'b']))
    markPinnedSessionOrderManual()

    s.dispatch(pending('stale'))
    // A newer full-slot snapshot lands while the reply is still in flight.
    generation = 1
    s.dispatch({ type: 'noop' })

    s.dispatch(fulfilled([{ key: 'a', pinned: false }, { key: 'b', pinned: false }], 'stale'))

    expect(localStorage.getItem(PINNED_SESSION_ORDER_MANUAL_KEY)).toBe('1')
  })
})
