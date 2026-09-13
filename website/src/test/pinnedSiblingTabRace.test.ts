import { describe, expect, it, beforeEach } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'
import { pinnedMarkerListener } from '../store/pinnedMarkerOwner'
import { sseSlots } from '../store/dashboardSlice'
import {
  PINNED_SESSION_ORDER_REVISION_KEY,
  markPinnedSessionOrderManual,
  noteArrangementRevisionSeen,
  persistPinnedSessionOrder,
  readPinnedSessionOrderIsManual,
} from '../utils/pinnedSessionOrder'

/**
 * A sibling tab's arrangement is invisible to this tab's in-flight record but not to the shared
 * revision. The second case is the discriminator that keeps the guard honest: an arrangement THIS tab
 * stated advances its own high-water mark too, so its own zero-pin frame must still settle.
 */
function tabStore() {
  return configureStore({
    reducer: { dashboard: () => ({ slotsLoaded: true }) },
    middleware: getDefault => getDefault().prepend(pinnedMarkerListener.middleware),
  })
}

describe('a zero-pin frame racing an arrangement in another tab', () => {
  beforeEach(() => {
    localStorage.clear()
    noteArrangementRevisionSeen()
  })

  it('leaves the arrangement the sibling tab just stated', () => {
    const store = tabStore()
    persistPinnedSessionOrder(['a', 'b'])
    localStorage.setItem('mc-pinned-session-order-manual', '1')
    localStorage.setItem(PINNED_SESSION_ORDER_REVISION_KEY, '7')

    store.dispatch(sseSlots([{ key: 'a', pinned: false }, { key: 'b', pinned: false }] as never))

    expect(readPinnedSessionOrderIsManual()).toBe(true)
  })

  it('still settles a frame following an arrangement stated in THIS tab', () => {
    const store = tabStore()
    persistPinnedSessionOrder(['a'])
    markPinnedSessionOrderManual()
    expect(readPinnedSessionOrderIsManual()).toBe(true)

    store.dispatch(sseSlots([{ key: 'a', pinned: false }] as never))

    expect(readPinnedSessionOrderIsManual()).toBe(false)
  })
})
