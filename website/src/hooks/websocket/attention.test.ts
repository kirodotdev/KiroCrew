import { act } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { clearMountedMemberThreadSlot, setMountedMemberThreadSlot } from '../../lib/mountedMemberThread'
import { store } from '../../store'
import { setActiveSlot } from '../../store/chatSlice'
import { attachFocusRelay, emitSlotFocused, focusedSlotForCurrentSurface } from './attention'
import type { SocketConnection } from './connection'

describe('warm task brief focus signal', () => {
  let hidden: ReturnType<typeof vi.spyOn>

  beforeEach(() => {
    clearMountedMemberThreadSlot()
    hidden = vi.spyOn(document, 'hidden', 'get').mockReturnValue(false)
  })

  afterEach(() => {
    clearMountedMemberThreadSlot()
    hidden.mockRestore()
  })

  it('a non-chat route preserves focus but cannot arm a warm brief', () => {
    const oldPath = window.location.pathname
    const sendIfOpen = vi.fn()
    const detach = attachFocusRelay({
      socket: { sendIfOpen, reconnectingRef: { current: false } } as unknown as SocketConnection,
      flushSlotActivity: vi.fn(),
      onActiveSlotChange: vi.fn(),
    })
    try {
      store.dispatch(setActiveSlot('slot-a'))
      window.history.pushState({}, '', '/settings')
      expect(focusedSlotForCurrentSurface()).toBe('slot-a')
      hidden.mockReturnValue(true)
      act(() => document.dispatchEvent(new Event('visibilitychange')))
      expect(sendIfOpen).toHaveBeenLastCalledWith({ type: 'slot_focused', slot: null })
    } finally {
      detach()
      window.history.pushState({}, '', oldPath || '/')
      store.dispatch(setActiveSlot(null))
    }
  })

  it('an ordinary chat clears focus without arming a warm brief', () => {
    const oldPath = window.location.pathname
    const sendIfOpen = vi.fn()
    const detach = attachFocusRelay({
      socket: { sendIfOpen, reconnectingRef: { current: false } } as unknown as SocketConnection,
      flushSlotActivity: vi.fn(),
      onActiveSlotChange: vi.fn(),
    })
    try {
      store.dispatch(setActiveSlot('slot-a'))
      window.history.pushState({}, '', '/chat')
      hidden.mockReturnValue(true)
      act(() => document.dispatchEvent(new Event('visibilitychange')))
      expect(sendIfOpen).toHaveBeenLastCalledWith({ type: 'slot_focused', slot: null })
    } finally {
      detach()
      window.history.pushState({}, '', oldPath || '/')
      store.dispatch(setActiveSlot(null))
    }
  })

  it('uses the visible mounted member thread and marks only visibility leave', () => {
    const sendIfOpen = vi.fn()
    const socket = {
      sendIfOpen,
      reconnectingRef: { current: false },
    } as unknown as SocketConnection
    const detach = attachFocusRelay({
      socket,
      flushSlotActivity: vi.fn(),
      onActiveSlotChange: vi.fn(),
    })

    store.dispatch(setActiveSlot('stale-chat'))
    act(() => setMountedMemberThreadSlot('member-ops'))
    expect(focusedSlotForCurrentSurface()).toBe('member-ops')
    expect(sendIfOpen).toHaveBeenLastCalledWith({ type: 'slot_focused', slot: 'member-ops' })

    act(() => emitSlotFocused(null))
    expect(sendIfOpen).toHaveBeenLastCalledWith({ type: 'slot_focused', slot: null })

    const beforeBlur = sendIfOpen.mock.calls.length
    act(() => window.dispatchEvent(new Event('blur')))
    expect(sendIfOpen).toHaveBeenCalledTimes(beforeBlur)

    hidden.mockReturnValue(true)
    act(() => document.dispatchEvent(new Event('visibilitychange')))
    expect(sendIfOpen).toHaveBeenLastCalledWith({ type: 'slot_focused', slot: null, hidden: true })

    const beforeVisible = sendIfOpen.mock.calls.length
    hidden.mockReturnValue(false)
    act(() => document.dispatchEvent(new Event('visibilitychange')))
    expect(sendIfOpen.mock.calls.slice(beforeVisible)).toContainEqual([
      { type: 'slot_focused', slot: 'member-ops' },
    ])
    detach()
    store.dispatch(setActiveSlot(null))
  })
})
