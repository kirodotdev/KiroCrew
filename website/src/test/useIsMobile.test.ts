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

// The breakpoint list answers only the width query: every other query (the
// touch-first and reduced-motion reads) is a fine-pointer desktop's answer.
const widthOnly = (q: string) => (q.includes('max-width') ? mql : { matches: false })

beforeAll(async () => {
  // Set up mock before importing the hook (module caches mql at load time)
  vi.resetModules()
  Object.defineProperty(window, 'matchMedia', {
    writable: true,
    value: vi.fn(widthOnly),
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
      ;(document as unknown as { startViewTransition: unknown }).startViewTransition = vi.fn((update: () => void) => {
        updates.push(update)
        return { finished: new Promise(() => {}) }
      })
    })
    afterEach(() => {
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

  // A display:none iframe (an inactive remote pane) reports a 0x0 viewport and
  // matches the narrow query. Chromium measured: 0 wide and narrow while hidden,
  // then `resize` plus the query's `change` once shown.
  describe('a frame with no viewport', () => {
    const real = { w: window.innerWidth, h: window.innerHeight }
    const setViewport = (w: number, h: number) => {
      Object.defineProperty(window, 'innerWidth', { configurable: true, value: w })
      Object.defineProperty(window, 'innerHeight', { configurable: true, value: h })
    }
    let transitions: number
    beforeEach(() => {
      transitions = 0
      ;(document as unknown as { startViewTransition: unknown }).startViewTransition = vi.fn((update: () => void) => {
        transitions++
        update()
        return { finished: Promise.resolve() }
      })
    })
    afterEach(() => {
      setViewport(real.w, real.h)
      delete (document as unknown as { startViewTransition?: unknown }).startViewTransition
    })

    it('keeps the desktop form while hidden, and shows without a morph', () => {
      currentMatches = false
      setViewport(1400, 900)
      const { result } = renderHook(() => useIsMobile())
      act(() => { setViewport(0, 0); currentMatches = true; listener?.(); window.dispatchEvent(new Event('resize')) })
      expect(result.current).toBe(false)
      act(() => { setViewport(1400, 900); currentMatches = false; window.dispatchEvent(new Event('resize')); listener?.() })
      expect(result.current).toBe(false)
      expect(transitions).toBe(0)
    })

    it('starts on desktop when first loaded hidden, and swaps instantly when shown narrow', () => {
      setViewport(0, 0)
      currentMatches = true
      const { result } = renderHook(() => useIsMobile())
      expect(result.current).toBe(false)
      act(() => { setViewport(600, 900); window.dispatchEvent(new Event('resize')); listener?.() })
      expect(result.current).toBe(true)
      expect(transitions).toBe(0)
    })

    it('still morphs a real resize across the breakpoint once shown', () => {
      setViewport(1400, 900)
      currentMatches = false
      const { result } = renderHook(() => useIsMobile())
      act(() => { setViewport(600, 900); currentMatches = true; window.dispatchEvent(new Event('resize')) })
      expect(result.current).toBe(true)
      expect(transitions).toBe(1)
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
