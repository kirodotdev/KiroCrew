import { describe, expect, it, beforeEach } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'
import { pinnedMarkerListener } from '../store/pinnedMarkerOwner'
import { sseSlots } from '../store/dashboardSlice'
import {
  PINNED_SESSION_ORDER_REVISION_KEY,
  noteArrangementRevisionSeen,
  persistPinnedSessionOrder,
  readPinnedSessionOrderIsManual,
} from '../utils/pinnedSessionOrder'

/**
 * A sibling that reorders and then closes emits no later frame, so a suppression that does not consume
 * the revision it acted on would refuse every authoritative empty membership for the life of the page.
 * One frame is the intended cost of the race; the frame after it must be honoured.
 */
function tabStore() {
  return configureStore({
    reducer: { dashboard: () => ({ slotsLoaded: true }) },
    middleware: getDefault => getDefault().prepend(pinnedMarkerListener.middleware),
  })
}

describe('a sibling revision this tab never saw catch up on', () => {
  beforeEach(() => {
    localStorage.clear()
    noteArrangementRevisionSeen()
  })

  it('suppresses the racing frame but honours the next authoritative one', () => {
    const store = tabStore()
    persistPinnedSessionOrder(['a'])
    localStorage.setItem('mc-pinned-session-order-manual', '1')
    localStorage.setItem(PINNED_SESSION_ORDER_REVISION_KEY, '9')

    store.dispatch(sseSlots([{ key: 'a', pinned: false }] as never))
    expect(readPinnedSessionOrderIsManual()).toBe(true)

    store.dispatch(sseSlots([{ key: 'a', pinned: false }] as never))
    expect(readPinnedSessionOrderIsManual()).toBe(false)
  })
})
