import { describe, expect, it, beforeEach } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'
import { pinnedMarkerListener } from '../store/pinnedMarkerOwner'
import { fetchSlots } from '../store/dashboardSlice'
import {
  markPinnedSessionOrderManual,
  persistPinnedSessionOrder,
  readPinnedSessionOrderIsManual,
} from '../utils/pinnedSessionOrder'

/**
 * Both directions of the stale-reply guard. A reply must not speak for an arrangement stated after
 * it was asked for, and an unchanged arrangement must still let a genuine last-unpin settle. One
 * direction alone would pass a guard that simply never settles, so neither test stands on its own.
 */
function storeWithListener() {
  return configureStore({
    reducer: { dashboard: () => ({ slotsLoaded: true }) },
    middleware: getDefault => getDefault().prepend(pinnedMarkerListener.middleware),
  })
}

const REQUEST_ID = 'req-1'

function pendingAction() {
  return { type: fetchSlots.pending.type, payload: undefined, meta: { requestId: REQUEST_ID, arg: undefined } }
}

function fulfilledAction(payload: unknown) {
  return { type: fetchSlots.fulfilled.type, payload, meta: { requestId: REQUEST_ID, arg: undefined } }
}

describe('a zero-pin fetch reply older than the arrangement', () => {
  beforeEach(() => {
    localStorage.clear()
  })

  it('leaves a marker stated while the request was outstanding', async () => {
    const store = storeWithListener()
    store.dispatch(pendingAction())
    await Promise.resolve()

    persistPinnedSessionOrder(['a', 'b'])
    markPinnedSessionOrderManual()

    store.dispatch(fulfilledAction([{ key: 'a', pinned: true }, { key: 'b', pinned: true }].map(
      slot => ({ ...slot, pinned: false }))))
    await Promise.resolve()

    expect(readPinnedSessionOrderIsManual()).toBe(true)
  })

  it('loses when the reply belongs to a request whose start was never recorded', async () => {
    const store = storeWithListener()
    persistPinnedSessionOrder(['a', 'b'])
    markPinnedSessionOrderManual()

    store.dispatch(fulfilledAction([{ key: 'a', pinned: false }, { key: 'b', pinned: false }]))
    await Promise.resolve()

    expect(readPinnedSessionOrderIsManual()).toBe(true)
  })

  it('still settles when the arrangement did not move while the request was outstanding', async () => {
    const store = storeWithListener()
    persistPinnedSessionOrder(['a'])
    markPinnedSessionOrderManual()
    expect(readPinnedSessionOrderIsManual()).toBe(true)

    store.dispatch(pendingAction())
    await Promise.resolve()

    store.dispatch(fulfilledAction([{ key: 'a', pinned: false }]))
    await Promise.resolve()

    expect(readPinnedSessionOrderIsManual()).toBe(false)
  })
})
