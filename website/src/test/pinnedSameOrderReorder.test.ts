import { describe, expect, it, beforeEach } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'
import { pinnedMarkerListener } from '../store/pinnedMarkerOwner'
import { fetchSlots } from '../store/dashboardSlice'
import {
  markPinnedSessionOrderManual,
  noteArrangementRevisionSeen,
  persistPinnedSessionOrder,
  readPinnedSessionOrderIsManual,
} from '../utils/pinnedSessionOrder'

/**
 * A reorder away and back to the identical sequence leaves the stored ORDER byte-identical, so a guard
 * comparing the serialized order sees no change and lets an older reply through. The revision counter
 * moves on every statement, which is what makes the two cases distinguishable.
 */
const REQUEST_ID = 'req-aba'

function ownerStore() {
  return configureStore({
    reducer: { dashboard: () => ({ slotsLoaded: true }) },
    middleware: getDefault => getDefault().prepend(pinnedMarkerListener.middleware),
  })
}

describe('a reorder that ends on the sequence it started from', () => {
  beforeEach(() => {
    localStorage.clear()
    noteArrangementRevisionSeen()
  })

  it('is not erased by a fetch reply that left before it', () => {
    const store = ownerStore()
    persistPinnedSessionOrder(['a', 'b'])
    markPinnedSessionOrderManual()

    store.dispatch({ type: fetchSlots.pending.type, payload: undefined, meta: { requestId: REQUEST_ID, arg: undefined } })

    persistPinnedSessionOrder(['b', 'a'])
    markPinnedSessionOrderManual()
    persistPinnedSessionOrder(['a', 'b'])
    markPinnedSessionOrderManual()

    store.dispatch({
      type: fetchSlots.fulfilled.type,
      payload: [{ key: 'a', pinned: false }, { key: 'b', pinned: false }],
      meta: { requestId: REQUEST_ID, arg: undefined },
    })

    expect(readPinnedSessionOrderIsManual()).toBe(true)
  })
})
