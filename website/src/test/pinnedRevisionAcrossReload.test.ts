import { describe, expect, it, beforeEach, vi } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'
import { sseSlots } from '../store/dashboardSlice'

/**
 * A reload starts a fresh module, so the per-tab high-water mark begins where the SHARED revision
 * already is. Seeded at zero it would read a persisted revision as a sibling update it had never seen
 * and refuse every settlement for the life of the page, so later pins would inherit the stale rank.
 */
describe('a reload carrying a persisted arrangement revision', () => {
  beforeEach(() => {
    localStorage.clear()
    vi.resetModules()
  })

  it('accepts an authoritative empty snapshot instead of refusing it forever', async () => {
    localStorage.setItem('mc-pinned-session-order', JSON.stringify(['a']))
    localStorage.setItem('mc-pinned-session-order-manual', '1')
    localStorage.setItem('mc-pinned-session-order-revision', '7')

    const { pinnedMarkerListener } = await import('../store/pinnedMarkerOwner')
    const { readPinnedSessionOrderIsManual } = await import('../utils/pinnedSessionOrder')
    const store = configureStore({
      reducer: { dashboard: () => ({ slotsLoaded: true }) },
      middleware: getDefault => getDefault().prepend(pinnedMarkerListener.middleware),
    })

    store.dispatch(sseSlots([{ key: 'a', pinned: false }] as never))

    expect(readPinnedSessionOrderIsManual()).toBe(false)
  })
})
