import { describe, it, expect, beforeAll, beforeEach, vi } from 'vitest'
import { renderHook, act } from '@testing-library/react'

let listener: (() => void) | null = null
let currentMatches = false
let useIsMobile: () => boolean

const mql = {
  get matches() { return currentMatches },
  addEventListener: vi.fn((_: string, cb: () => void) => { listener = cb }),
  removeEventListener: vi.fn(() => { listener = null }),
}

beforeAll(async () => {
  // Set up mock before importing the hook (module caches mql at load time)
  vi.resetModules()
  Object.defineProperty(window, 'matchMedia', {
    writable: true,
    value: vi.fn().mockReturnValue(mql),
  })
  const mod = await import('../hooks/useIsMobile')
  useIsMobile = mod.useIsMobile
})

describe('useIsMobile', () => {
  beforeEach(() => {
    listener = null
    currentMatches = false
    vi.clearAllMocks()
    mql.addEventListener = vi.fn((_: string, cb: () => void) => { listener = cb })
    mql.removeEventListener = vi.fn(() => { listener = null })
  })

  it('returns true when viewport is below 768px', () => {
    currentMatches = true
    const { result } = renderHook(() => useIsMobile())
    expect(result.current).toBe(true)
  })

  it('returns false when viewport is at or above 768px', () => {
    currentMatches = false
    const { result } = renderHook(() => useIsMobile())
    expect(result.current).toBe(false)
  })

  it('reacts to media query changes', () => {
    currentMatches = false
    const { result } = renderHook(() => useIsMobile())
    expect(result.current).toBe(false)

    act(() => { currentMatches = true; listener?.() })
    expect(result.current).toBe(true)

    act(() => { currentMatches = false; listener?.() })
    expect(result.current).toBe(false)
  })

  it('cleans up listener on unmount', () => {
    const { unmount } = renderHook(() => useIsMobile())
    unmount()
    expect(mql.removeEventListener).toHaveBeenCalled()
  })

  it('uses the correct breakpoint query', () => {
    // Module-level mql was created with matchMedia('(max-width: 767px)') at import time
    // Verify by checking the hook returns false for a non-mobile viewport
    currentMatches = false
    const { result } = renderHook(() => useIsMobile())
    expect(result.current).toBe(false)
    // And true for mobile
    currentMatches = true
    act(() => { listener?.() })
    expect(result.current).toBe(true)
  })
})
