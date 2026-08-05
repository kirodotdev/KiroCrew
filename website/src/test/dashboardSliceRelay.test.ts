/**
 * Tests for the unread postMessage relay in dashboardSlice (§5.3). When the
 * dashboard runs embedded (window.parent !== window) a change to
 * `mc-unread-slots` posts {type:'mc-unread-slots', count} to the parent so the
 * Instances hub can badge the chip. Only the non-secret count is sent.
 */
import { describe, it, expect, afterEach, vi } from 'vitest'
import dashboardReducer, { markSlotUnread, markSlotRead } from '../store/dashboardSlice'

const realParent = window.parent

afterEach(() => {
  Object.defineProperty(window, 'parent', { value: realParent, configurable: true })
  vi.restoreAllMocks()
})

describe('dashboardSlice unread relay', () => {
  it('posts the unread count to the parent when embedded', () => {
    const postMessage = vi.fn()
    // Simulate being inside an iframe: parent !== window.
    Object.defineProperty(window, 'parent', {
      value: { postMessage },
      configurable: true,
    })

    let state = dashboardReducer(undefined, { type: '@@INIT' })
    state = dashboardReducer(state, markSlotUnread('slot-1'))

    expect(postMessage).toHaveBeenCalled()
    const [payload] = postMessage.mock.calls[0]
    expect(payload).toMatchObject({ source: 'kirocrew', type: 'mc-unread-slots', count: 1 })

    // Reading clears it -> relays count 0.
    postMessage.mockClear()
    state = dashboardReducer(state, markSlotRead('slot-1'))
    expect(postMessage.mock.calls[0][0]).toMatchObject({ type: 'mc-unread-slots', count: 0 })
    expect(state.unreadSlots).not.toContain('slot-1')
  })

  it('does not post when not embedded (parent === window)', () => {
    const postMessage = vi.fn()
    Object.defineProperty(window, 'parent', { value: window, configurable: true })
    ;(window as unknown as { postMessage: typeof postMessage }).postMessage = postMessage

    let state = dashboardReducer(undefined, { type: '@@INIT' })
    state = dashboardReducer(state, markSlotUnread('slot-1'))
    expect(postMessage).not.toHaveBeenCalled()
    expect(state.unreadSlots).toContain('slot-1')
  })
})
