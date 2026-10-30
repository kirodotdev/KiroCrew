import { act } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { clearMountedMemberThreadSlot, setMountedMemberThreadSlot } from '../../lib/mountedMemberThread'
import { store } from '../../store'
import { setActiveSlot } from '../../store/chatSlice'
import { sseSlots } from '../../store/dashboardSlice'
import type { ChatSlot } from '../../types'
import { emitComposeIntent } from '../../lib/composeIntent'
import { attachFocusRelay, COMPOSE_INTENT_THROTTLE_MS, emitSlotFocused, endSplitPaneFocus, focusedSlotForCurrentSurface } from './attention'
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

  it('a Crew Mode chat on a chat route marks its visibility leave', () => {
    const oldPath = window.location.pathname
    const sendIfOpen = vi.fn()
    const detach = attachFocusRelay({
      socket: { sendIfOpen, reconnectingRef: { current: false } } as unknown as SocketConnection,
      flushSlotActivity: vi.fn(),
      onActiveSlotChange: vi.fn(),
    })
    try {
      store.dispatch(sseSlots([{ key: 'crew-a', crew_mode_root: true } as unknown as ChatSlot]))
      store.dispatch(setActiveSlot('crew-a'))
      window.history.pushState({}, '', '/chat')
      hidden.mockReturnValue(true)
      act(() => document.dispatchEvent(new Event('visibilitychange')))
      expect(sendIfOpen).toHaveBeenLastCalledWith({ type: 'slot_focused', slot: null, hidden: true })
      // Off a chat route the same Crew Mode chat cannot arm one.
      hidden.mockReturnValue(false)
      act(() => document.dispatchEvent(new Event('visibilitychange')))
      window.history.pushState({}, '', '/settings')
      hidden.mockReturnValue(true)
      act(() => document.dispatchEvent(new Event('visibilitychange')))
      expect(sendIfOpen).toHaveBeenLastCalledWith({ type: 'slot_focused', slot: null })
    } finally {
      detach()
      window.history.pushState({}, '', oldPath || '/')
      store.dispatch(setActiveSlot(null))
      store.dispatch(sseSlots([]))
    }
  })

  it('a split-view hide follows the focused pane, not the active slot', () => {
    const oldPath = window.location.pathname
    const sendIfOpen = vi.fn()
    const lastFocus = () => sendIfOpen.mock.calls.map(c => c[0]).filter(f => f.type === 'slot_focused').at(-1)
    const detach = attachFocusRelay({
      socket: { sendIfOpen, reconnectingRef: { current: false } } as unknown as SocketConnection,
      flushSlotActivity: vi.fn(),
      onActiveSlotChange: vi.fn(),
    })
    try {
      store.dispatch(sseSlots([
        { key: 'plain-a' } as unknown as ChatSlot,
        { key: 'crew-b', crew_mode_root: true } as unknown as ChatSlot,
      ]))
      store.dispatch(setActiveSlot('plain-a'))
      window.history.pushState({}, '', '/chat')
      // Pane focus moves to the Crew Mode pane: it arms the hide.
      act(() => emitSlotFocused('crew-b'))
      // The one resolver every announcement uses (reconnect catch-up included)
      // names the focused pane, not activeSlot.
      expect(focusedSlotForCurrentSurface()).toBe('crew-b')
      // A pane change while hidden sends nothing; the reveal announces it.
      hidden.mockReturnValue(true)
      const whileHidden = sendIfOpen.mock.calls.length
      act(() => emitSlotFocused('plain-a'))
      expect(sendIfOpen).toHaveBeenCalledTimes(whileHidden)
      hidden.mockReturnValue(false)
      act(() => emitSlotFocused('crew-b'))
      // An unrelated store change does not drop the pane focus.
      act(() => { store.dispatch(sseSlots([
        { key: 'plain-a' } as unknown as ChatSlot,
        { key: 'crew-b', crew_mode_root: true } as unknown as ChatSlot,
        { key: 'other' } as unknown as ChatSlot,
      ])) })
      expect(focusedSlotForCurrentSurface()).toBe('crew-b')
      hidden.mockReturnValue(true)
      act(() => document.dispatchEvent(new Event('visibilitychange')))
      expect(lastFocus()).toEqual({ type: 'slot_focused', slot: null, hidden: true })
      // The reveal names the focused pane the hide left, not activeSlot.
      hidden.mockReturnValue(false)
      act(() => document.dispatchEvent(new Event('visibilitychange')))
      expect(lastFocus()).toEqual({ type: 'slot_focused', slot: 'crew-b' })
      // A window focus beside the reveal repeats the same pane, never activeSlot.
      act(() => window.dispatchEvent(new Event('focus')))
      expect(lastFocus()).toEqual({ type: 'slot_focused', slot: 'crew-b' })
      // Pane focus on the plain chat: the Crew Mode active slot does not arm it.
      store.dispatch(setActiveSlot('crew-b'))
      act(() => emitSlotFocused('plain-a'))
      hidden.mockReturnValue(true)
      act(() => document.dispatchEvent(new Event('visibilitychange')))
      expect(lastFocus()).toEqual({ type: 'slot_focused', slot: null })
      // Leaving split view hands the reveal back to activeSlot.
      act(() => endSplitPaneFocus())
      hidden.mockReturnValue(false)
      act(() => document.dispatchEvent(new Event('visibilitychange')))
      expect(lastFocus()).toEqual({ type: 'slot_focused', slot: 'crew-b' })
      // So does a store-driven focus change after a pane reported.
      act(() => emitSlotFocused('plain-a'))
      act(() => { store.dispatch(setActiveSlot('plain-a')) })
      act(() => { store.dispatch(setActiveSlot('crew-b')) })
      hidden.mockReturnValue(true)
      act(() => document.dispatchEvent(new Event('visibilitychange')))
      hidden.mockReturnValue(false)
      act(() => document.dispatchEvent(new Event('visibilitychange')))
      expect(lastFocus()).toEqual({ type: 'slot_focused', slot: 'crew-b' })
    } finally {
      detach()
      endSplitPaneFocus()
      window.history.pushState({}, '', oldPath || '/')
      store.dispatch(setActiveSlot(null))
      store.dispatch(sseSlots([]))
    }
  })

  it('leaving split view re-announces the active Crew Mode chat so its hide arms', () => {
    const oldPath = window.location.pathname
    const sendIfOpen = vi.fn()
    const lastFocus = () => sendIfOpen.mock.calls.map(c => c[0]).filter(f => f.type === 'slot_focused').at(-1)
    const detach = attachFocusRelay({
      socket: { sendIfOpen, reconnectingRef: { current: false } } as unknown as SocketConnection,
      flushSlotActivity: vi.fn(),
      onActiveSlotChange: vi.fn(),
    })
    try {
      store.dispatch(sseSlots([
        { key: 'crew-a', crew_mode_root: true } as unknown as ChatSlot,
        { key: 'plain-b' } as unknown as ChatSlot,
      ]))
      store.dispatch(setActiveSlot('crew-a'))
      window.history.pushState({}, '', '/chat')
      act(() => emitSlotFocused('plain-b'))
      // Selecting A's sidebar row ends split view without changing activeSlot.
      act(() => endSplitPaneFocus())
      expect(lastFocus()).toEqual({ type: 'slot_focused', slot: 'crew-a' })
      hidden.mockReturnValue(true)
      act(() => document.dispatchEvent(new Event('visibilitychange')))
      expect(lastFocus()).toEqual({ type: 'slot_focused', slot: null, hidden: true })
      // Ending split view again with no pane focus sends nothing more.
      const before = sendIfOpen.mock.calls.length
      act(() => endSplitPaneFocus())
      expect(sendIfOpen).toHaveBeenCalledTimes(before)
    } finally {
      detach()
      endSplitPaneFocus()
      window.history.pushState({}, '', oldPath || '/')
      store.dispatch(setActiveSlot(null))
      store.dispatch(sseSlots([]))
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

describe('compose intent re-arms the resume prefetch', () => {
  let hidden: ReturnType<typeof vi.spyOn>
  let sendIfOpen: ReturnType<typeof vi.fn>
  let detach: () => void

  beforeEach(() => {
    vi.useFakeTimers()
    clearMountedMemberThreadSlot()
    hidden = vi.spyOn(document, 'hidden', 'get').mockReturnValue(false)
    sendIfOpen = vi.fn()
    detach = attachFocusRelay({
      socket: { sendIfOpen, reconnectingRef: { current: false } } as unknown as SocketConnection,
      flushSlotActivity: vi.fn(),
      onActiveSlotChange: vi.fn(),
    })
  })

  afterEach(() => {
    detach()
    store.dispatch(setActiveSlot(null))
    clearMountedMemberThreadSlot()
    hidden.mockRestore()
    vi.useRealTimers()
  })

  const focusFrames = (slot: string) =>
    sendIfOpen.mock.calls.filter(([f]) => f.type === 'slot_focused' && f.slot === slot).length

  it('re-sends slot_focused for the focused chat when the user composes', () => {
    act(() => { store.dispatch(setActiveSlot('chat-a')) })
    expect(focusFrames('chat-a')).toBe(1)
    act(() => emitComposeIntent('chat-a'))
    expect(focusFrames('chat-a')).toBe(2)
  })

  it('sends at most one compose frame per slot per throttle window', () => {
    act(() => { store.dispatch(setActiveSlot('chat-a')) })
    act(() => emitComposeIntent('chat-a'))
    act(() => emitComposeIntent('chat-a'))
    vi.advanceTimersByTime(COMPOSE_INTENT_THROTTLE_MS - 1)
    act(() => emitComposeIntent('chat-a'))
    expect(focusFrames('chat-a')).toBe(2)
    vi.advanceTimersByTime(1)
    act(() => emitComposeIntent('chat-a'))
    expect(focusFrames('chat-a')).toBe(3)
  })

  it('never moves focus: a slot other than the announced one sends nothing', () => {
    act(() => { store.dispatch(setActiveSlot('chat-a')) })
    const before = sendIfOpen.mock.calls.length
    act(() => emitComposeIntent('chat-b'))
    expect(sendIfOpen).toHaveBeenCalledTimes(before)
  })

  it('leaves crewmate threads and hidden windows alone', () => {
    act(() => setMountedMemberThreadSlot('member-ops'))
    const before = sendIfOpen.mock.calls.length
    act(() => emitComposeIntent('member-ops'))
    expect(sendIfOpen).toHaveBeenCalledTimes(before)
    act(() => clearMountedMemberThreadSlot())
    act(() => { store.dispatch(setActiveSlot('chat-a')) })
    hidden.mockReturnValue(true)
    const hiddenBefore = sendIfOpen.mock.calls.length
    act(() => emitComposeIntent('chat-a'))
    expect(sendIfOpen).toHaveBeenCalledTimes(hiddenBefore)
  })
})
