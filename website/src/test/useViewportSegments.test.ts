import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { FOLD_X_QUERY, useViewportSegments } from '../hooks/useViewportSegments'

// The book-pose reading from the iPhone Duo simulator (rfc-foldable-viewport-segments, Phase 0).
const BOOK = [
  { x: 0, y: 0, width: 475.5, height: 1152 },
  { x: 475.5, y: 0, width: 475.5, height: 1152 },
]

let foldMatches = false
let mqlListeners: (() => void)[] = []
const posture = new EventTarget()

function setViewport(segments: typeof BOOK | null) {
  Object.defineProperty(window, 'viewport', {
    configurable: true,
    value: segments === null ? undefined : { segments },
  })
}

beforeEach(() => {
  foldMatches = false
  mqlListeners = []
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    writable: true,
    value: vi.fn((q: string) => ({
      matches: q === FOLD_X_QUERY ? foldMatches : false,
      addEventListener: (_: string, cb: () => void) => { if (q === FOLD_X_QUERY) mqlListeners.push(cb) },
      removeEventListener: (_: string, cb: () => void) => { mqlListeners = mqlListeners.filter(l => l !== cb) },
    })),
  })
  Object.defineProperty(navigator, 'devicePosture', { configurable: true, value: posture })
  setViewport(null)
})

afterEach(() => {
  delete (window as { viewport?: unknown }).viewport
  delete (navigator as { devicePosture?: unknown }).devicePosture
})

describe('useViewportSegments', () => {
  it('returns the two segments when the fold query matches', () => {
    foldMatches = true
    setViewport(BOOK)
    const { result } = renderHook(() => useViewportSegments())
    expect(result.current).toEqual([{ x: 0, width: 475.5 }, { x: 475.5, width: 475.5 }])
  })

  it('is null when window.viewport is missing, even with the query matched', () => {
    foldMatches = true
    const { result } = renderHook(() => useViewportSegments())
    expect(result.current).toBeNull()
  })

  it('is null when the query does not match, even with two segments reported', () => {
    setViewport(BOOK)
    const { result } = renderHook(() => useViewportSegments())
    expect(result.current).toBeNull()
  })

  it('is null for a flat device that reports one segment', () => {
    foldMatches = true
    setViewport([{ x: 0, y: 0, width: 669, height: 819 }] as typeof BOOK)
    const { result } = renderHook(() => useViewportSegments())
    expect(result.current).toBeNull()
  })

  it('keeps the same array across re-renders of one reading', () => {
    foldMatches = true
    setViewport(BOOK)
    const { result, rerender } = renderHook(() => useViewportSegments())
    const first = result.current
    rerender()
    expect(result.current).toBe(first)
  })

  it('re-reads on the query change, a resize and a posture change', () => {
    const { result } = renderHook(() => useViewportSegments())
    expect(result.current).toBeNull()

    act(() => { foldMatches = true; setViewport(BOOK); for (const l of mqlListeners) l() })
    expect(result.current).toHaveLength(2)

    act(() => { foldMatches = false; window.dispatchEvent(new Event('resize')) })
    expect(result.current).toBeNull()

    act(() => { foldMatches = true; posture.dispatchEvent(new Event('change')) })
    expect(result.current).toHaveLength(2)
  })

  it('drops every listener on unmount', () => {
    const remove = vi.spyOn(window, 'removeEventListener')
    const { unmount } = renderHook(() => useViewportSegments())
    expect(mqlListeners).toHaveLength(1)
    unmount()
    expect(mqlListeners).toHaveLength(0)
    expect(remove).toHaveBeenCalledWith('resize', expect.any(Function))
  })
})
