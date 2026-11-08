/**
 * Tests for the unread postMessage relay in dashboardSlice (§5.3). When the
 * dashboard runs embedded (window.parent !== window) a change to
 * `mc-unread-slots` posts {type:'mc-unread-slots', count} to the parent so the
 * Instances hub can badge the chip. Only the non-secret count is sent.
 */
import { describe, it, expect, afterEach, vi } from 'vitest'
import dashboardReducer, { markSlotUnread, markSlotRead } from '../store/dashboardSlice'

const realParent = window.parent
const HUB = 'http://127.0.0.1:8899'

// jsdom has no referrer; the relay posts only to a hub origin it can name.
const setReferrer = (value: string) => {
  Object.defineProperty(document, 'referrer', { value, configurable: true })
}

afterEach(() => {
  Object.defineProperty(window, 'parent', { value: realParent, configurable: true })
  setReferrer('')
  vi.restoreAllMocks()
})

describe('dashboardSlice unread relay', () => {
  it('posts the unread count to the parent when embedded', () => {
    const postMessage = vi.fn()
    setReferrer(`${HUB}/chat`)
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
    expect(postMessage.mock.calls[0][1]).toBe(HUB)

    // Reading clears it -> relays count 0.
    postMessage.mockClear()
    state = dashboardReducer(state, markSlotRead('slot-1'))
    expect(postMessage.mock.calls[0][0]).toMatchObject({ type: 'mc-unread-slots', count: 0 })
    expect(state.unreadSlots).not.toContain('slot-1')
  })

  // The hub zeroes an instance's count when the user selects it, so a new
  // arrival on a slot that is already unread must post the count again for
  // the chip to light.
  it('relays the count on every arrival, including on an already-unread slot', () => {
    const postMessage = vi.fn()
    setReferrer(`${HUB}/`)
    Object.defineProperty(window, 'parent', { value: { postMessage }, configurable: true })

    let state = dashboardReducer(undefined, { type: '@@INIT' })
    state = dashboardReducer(state, markSlotUnread({ slot: 'slot-1', ts: '2026-01-01T00:00:01Z' }))
    state = dashboardReducer(state, markSlotUnread({ slot: 'slot-1', ts: '2026-01-01T00:00:02Z' }))
    state = dashboardReducer(state, markSlotUnread({ slot: 'slot-2', ts: '2026-01-01T00:00:03Z' }))
    expect(postMessage.mock.calls.map(([p]) => p.count)).toEqual([1, 1, 2])
    expect(state.unreadSlots).toEqual(['slot-1', 'slot-2'])
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

  it.each([
    ['no referrer', ''],
    ['a page that is not a hub', 'https://evil.example/frame'],
    ['a public host on a loopback port', 'http://example.com:8899/'],
    ['an opaque referrer', 'about:blank'],
  ])('sends nothing when embedded under %s', (_label, referrer) => {
    const postMessage = vi.fn()
    setReferrer(referrer)
    Object.defineProperty(window, 'parent', { value: { postMessage }, configurable: true })

    let state = dashboardReducer(undefined, { type: '@@INIT' })
    state = dashboardReducer(state, markSlotUnread('slot-1'))
    expect(postMessage).not.toHaveBeenCalled()
    expect(state.unreadSlots).toContain('slot-1')
  })

  it('never posts to the wildcard target', () => {
    const postMessage = vi.fn()
    setReferrer('http://localhost:5173/')
    Object.defineProperty(window, 'parent', { value: { postMessage }, configurable: true })

    dashboardReducer(dashboardReducer(undefined, { type: '@@INIT' }), markSlotUnread('slot-1'))
    expect(postMessage.mock.calls.map(([, target]) => target)).toEqual(['http://localhost:5173'])
  })
})
