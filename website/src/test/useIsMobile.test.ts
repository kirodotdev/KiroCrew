import { describe, it, expect, beforeAll, beforeEach, afterEach, vi } from 'vitest'
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

  // These never call `listener`: they assert recovery from a MISSED change event,
  // so they fail against a subscription that only listens to the query itself.
  it('recovers on orientationchange when the change event never fires', () => {
    currentMatches = false
    const { result } = renderHook(() => useIsMobile())
    expect(result.current).toBe(false)

    act(() => {
      currentMatches = true
      window.dispatchEvent(new Event('orientationchange'))
    })
    expect(result.current).toBe(true)
  })

  it('recovers on visibilitychange when the change event never fires', () => {
    currentMatches = false
    const { result } = renderHook(() => useIsMobile())
    expect(result.current).toBe(false)

    act(() => {
      currentMatches = true
      // Fires at the document, not the window — a rotation that happened while
      // the tab was backgrounded surfaces here.
      document.dispatchEvent(new Event('visibilitychange'))
    })
    expect(result.current).toBe(true)
  })

  describe('form-change continuity', () => {
    let updates: Array<() => void>
    beforeEach(() => {
      updates = []
      // This file's matchMedia answers every query with the breakpoint list, so
      // the reduced-motion query would read the breakpoint. Answer it apart.
      vi.mocked(window.matchMedia).mockImplementation(((q: string) =>
        (q.includes('prefers-reduced-motion') ? { matches: false } : mql)) as unknown as typeof window.matchMedia)
      ;(document as unknown as { startViewTransition: unknown }).startViewTransition = vi.fn((update: () => void) => {
        updates.push(update)
        return { finished: new Promise(() => {}) }
      })
    })
    afterEach(() => {
      vi.mocked(window.matchMedia).mockImplementation((() => mql) as unknown as typeof window.matchMedia)
      delete (document as unknown as { startViewTransition?: unknown }).startViewTransition
      delete document.documentElement.dataset.layoutMorph
      delete document.documentElement.dataset.layoutMorphEdge
    })

    it('wraps a resize that crosses the breakpoint in a view transition, flipping every consumer inside it', () => {
      currentMatches = false
      const a = renderHook(() => useIsMobile())
      const b = renderHook(() => useIsMobile())
      expect(document.startViewTransition).not.toHaveBeenCalled() // not on first load

      act(() => { currentMatches = true; window.dispatchEvent(new Event('resize')) })
      expect(document.startViewTransition).toHaveBeenCalledTimes(1)
      // Until the transition's callback runs, every consumer still draws the old
      // form, even one that re-renders for its own reasons in between.
      a.rerender(); b.rerender()
      expect([a.result.current, b.result.current]).toEqual([false, false])
      act(() => { updates[0]() })
      expect([a.result.current, b.result.current]).toEqual([true, true])
    })

    it('does not start one for a resize that stays on the same side of the breakpoint', () => {
      currentMatches = false
      renderHook(() => useIsMobile())
      act(() => { window.dispatchEvent(new Event('resize')) })
      act(() => { listener?.() })
      expect(document.startViewTransition).not.toHaveBeenCalled()
    })

    it('does not start one for a change it only notices when the page comes back', () => {
      currentMatches = false
      const { result } = renderHook(() => useIsMobile())
      act(() => { currentMatches = true; window.dispatchEvent(new Event('pageshow')) })
      expect(document.startViewTransition).not.toHaveBeenCalled()
      expect(result.current).toBe(true)
    })
  })

  it('negative control: an unsubscribed event does not refresh the value', () => {
    // `focus` is unsubscribed, so this proves the two passes above come from the
    // subscription rather than an incidental renderHook re-render.
    currentMatches = false
    const { result } = renderHook(() => useIsMobile())
    expect(result.current).toBe(false)

    act(() => {
      currentMatches = true
      window.dispatchEvent(new Event('focus'))
    })
    expect(result.current).toBe(false)
  })
})
