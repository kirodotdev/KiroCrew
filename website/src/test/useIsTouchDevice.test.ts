import { describe, it, expect, vi, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { useIsTouchDevice } from '../hooks/useIsTouchDevice'

/**
 * A matchMedia stub whose result can be flipped, recording listeners per query
 * so a change can be fired the way a real pointer swap would.
 */
function stubMatchMedia(initial: Record<string, boolean>) {
  const state = { ...initial }
  const listeners = new Map<string, Set<() => void>>()
  const original = window.matchMedia
  Object.defineProperty(window, 'matchMedia', {
    writable: true,
    configurable: true,
    value: (query: string) => ({
      get matches() { return state[query] ?? false },
      media: query,
      addEventListener: (_: string, cb: () => void) => {
        const s = listeners.get(query) ?? new Set()
        s.add(cb)
        listeners.set(query, s)
      },
      removeEventListener: (_: string, cb: () => void) => { listeners.get(query)?.delete(cb) },
      dispatchEvent: () => false,
    }),
  })
  return {
    set(query: string, value: boolean) {
      state[query] = value
      for (const cb of listeners.get(query) ?? []) cb()
    },
    listenerCount: (query: string) => listeners.get(query)?.size ?? 0,
    restore: () => Object.defineProperty(window, 'matchMedia', { writable: true, value: original }),
  }
}

const COARSE = '(pointer: coarse)'
const NO_HOVER = '(hover: none)'

let mm: ReturnType<typeof stubMatchMedia> | null = null
afterEach(() => { mm?.restore(); mm = null })

describe('useIsTouchDevice', () => {
  it('is false on a mouse pointer', () => {
    mm = stubMatchMedia({ [COARSE]: false, [NO_HOVER]: false })
    const { result } = renderHook(() => useIsTouchDevice())
    expect(result.current).toBe(false)
  })

  it('is true on a coarse pointer', () => {
    mm = stubMatchMedia({ [COARSE]: true, [NO_HOVER]: false })
    const { result } = renderHook(() => useIsTouchDevice())
    expect(result.current).toBe(true)
  })

  /** The arm a plain `(pointer: coarse)` check would have missed. */
  it('is true on a hover-less device even when the pointer is not coarse', () => {
    mm = stubMatchMedia({ [COARSE]: false, [NO_HOVER]: true })
    const { result } = renderHook(() => useIsTouchDevice())
    expect(result.current).toBe(true)
  })

  it('re-renders when the pointer kind changes', () => {
    mm = stubMatchMedia({ [COARSE]: false, [NO_HOVER]: false })
    const { result } = renderHook(() => useIsTouchDevice())
    expect(result.current).toBe(false)
    act(() => { mm!.set(COARSE, true) })
    expect(result.current).toBe(true)
  })

  it('unsubscribes both queries on unmount', () => {
    mm = stubMatchMedia({ [COARSE]: false, [NO_HOVER]: false })
    const { unmount } = renderHook(() => useIsTouchDevice())
    expect(mm.listenerCount(COARSE)).toBe(1)
    expect(mm.listenerCount(NO_HOVER)).toBe(1)
    unmount()
    expect(mm.listenerCount(COARSE)).toBe(0)
    expect(mm.listenerCount(NO_HOVER)).toBe(0)
  })

  it('reports false when matchMedia is unavailable', () => {
    const original = window.matchMedia
    Object.defineProperty(window, 'matchMedia', { writable: true, configurable: true, value: undefined })
    const { result } = renderHook(() => useIsTouchDevice())
    expect(result.current).toBe(false)
    Object.defineProperty(window, 'matchMedia', { writable: true, value: original })
  })
})
