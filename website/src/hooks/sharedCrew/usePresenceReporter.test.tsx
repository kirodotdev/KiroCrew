import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { setSharedCrewSender } from './sharedCrewSocket'
import { KEEPALIVE_MS, MIN_SPACING_MS, TYPING_HOLD_MS, usePresenceReporter } from './usePresenceReporter'

let sent: Array<Record<string, unknown>>

beforeEach(() => {
  vi.useFakeTimers()
  vi.setSystemTime(new Date('2026-10-16T00:00:00Z'))
  sent = []
  setSharedCrewSender(p => sent.push(p as Record<string, unknown>))
})

afterEach(() => {
  setSharedCrewSender(null)
  vi.useRealTimers()
})

const activities = () => sent.map(f => f.activity)

describe('usePresenceReporter', () => {
  it('reports viewing on open and keeps it alive inside the server expiry', () => {
    renderHook(() => usePresenceReporter('s1', true))
    expect(activities()).toEqual(['viewing'])
    act(() => { vi.advanceTimersByTime(KEEPALIVE_MS) })
    expect(activities()).toEqual(['viewing', 'viewing'])
  })

  it('spaces sends above the server rate limit and settles on the latest activity', () => {
    const { result } = renderHook(() => usePresenceReporter('s1', true))
    act(() => { result.current.noteTyping() })
    // Too soon after the open frame: held, not dropped and not sent early.
    expect(activities()).toEqual(['viewing'])
    act(() => { vi.advanceTimersByTime(MIN_SPACING_MS) })
    expect(activities()).toEqual(['viewing', 'typing'])
    act(() => { vi.advanceTimersByTime(TYPING_HOLD_MS) })
    expect(activities()).toEqual(['viewing', 'typing', 'viewing'])
  })

  it('never sends text or a principal', () => {
    const { result } = renderHook(() => usePresenceReporter('s1', true))
    act(() => { vi.advanceTimersByTime(MIN_SPACING_MS); result.current.noteTyping() })
    for (const f of sent) expect(Object.keys(f).sort()).toEqual(['activity', 'session_key', 'type'])
  })

  it('reports nothing when presence is unavailable', () => {
    const { result } = renderHook(() => usePresenceReporter('s1', false))
    act(() => { result.current.noteTyping(); vi.advanceTimersByTime(KEEPALIVE_MS * 2) })
    expect(sent).toEqual([])
  })

  it('a tab hidden during the typing hold stays idle when the hold ends', () => {
    const state = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible')
    try {
      const { result } = renderHook(() => usePresenceReporter('s1', true))
      act(() => { vi.advanceTimersByTime(MIN_SPACING_MS); result.current.noteTyping() })
      expect(activities()).toEqual(['viewing', 'typing'])
      state.mockReturnValue('hidden')
      act(() => { document.dispatchEvent(new Event('visibilitychange')) })
      expect(activities()).toEqual(['viewing', 'typing', 'idle'])
      act(() => { vi.advanceTimersByTime(TYPING_HOLD_MS) })
      act(() => { vi.advanceTimersByTime(KEEPALIVE_MS) })
      expect(activities().slice(2).every(a => a === 'idle')).toBe(true)
    } finally {
      state.mockRestore()
    }
  })

  it('stops reporting after unmount', () => {
    const { unmount } = renderHook(() => usePresenceReporter('s1', true))
    unmount()
    act(() => { vi.advanceTimersByTime(KEEPALIVE_MS * 3) })
    expect(activities()).toEqual(['viewing'])
  })
})
