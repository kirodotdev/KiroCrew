import { describe, it, expect, beforeAll, beforeEach, afterEach, vi } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { FOLD_TWO_PANE_QUERY } from '../hooks/useViewportSegments'

/**
 * A reported fold at 640px and wider takes the two-pane form ahead of the
 * touch-first phone rule (rfc-foldable-viewport-segments, "A reported fold and
 * the touch-first phone rule"). A flat foldable reports no fold and keeps the
 * phone form, as useIsMobile.touchForm.test.ts pins.
 */

const media = { width: 951, coarse: true, hoverNone: true, folded: false }
const listeners = new Map<string, Set<() => void>>()
function mqlFor(query: string): MediaQueryList {
  const matches = () => {
    if (query === '(pointer: coarse)') return media.coarse
    if (query === '(hover: none)') return media.hoverNone
    if (query === FOLD_TWO_PANE_QUERY) return media.folded && media.width >= 640
    const max = /max-width:\s*(\d+)px/.exec(query)
    return max ? media.width <= Number(max[1]) : false
  }
  return {
    get matches() { return matches() },
    media: query,
    addEventListener: (_: string, cb: () => void) => {
      if (!listeners.has(query)) listeners.set(query, new Set())
      listeners.get(query)!.add(cb)
    },
    removeEventListener: (_: string, cb: () => void) => { listeners.get(query)?.delete(cb) },
  } as unknown as MediaQueryList
}

// The iPhone Duo simulator's Safari, mobile mode.
const DUO_UA = 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/27.2 Mobile/15E148 Safari/604.1'

let useIsMobile: () => boolean

beforeAll(async () => {
  vi.resetModules()
  Object.defineProperty(window, 'matchMedia', { writable: true, value: vi.fn((q: string) => mqlFor(q)) })
  useIsMobile = (await import('../hooks/useIsMobile')).useIsMobile
})

describe('a reported fold and the touch-first phone rule', () => {
  beforeEach(() => {
    Object.assign(media, { width: 951, coarse: true, hoverNone: true, folded: false })
    listeners.clear()
    Object.defineProperty(navigator, 'userAgent', { configurable: true, get: () => DUO_UA })
  })
  afterEach(() => {
    delete (navigator as unknown as Record<string, unknown>).userAgent
  })

  it('the Duo half-folded at 951px in a mobile-mode browser gets the two-pane form', () => {
    media.folded = true
    const { result } = renderHook(() => useIsMobile())
    expect(result.current).toBe(false)
    expect(document.documentElement.dataset.layoutForm).toBe('desktop')
  })

  it('the Duo flat at 951px in a mobile-mode browser keeps the phone form', () => {
    const { result } = renderHook(() => useIsMobile())
    expect(result.current).toBe(true)
    expect(document.documentElement.dataset.layoutForm).toBe('phone')
  })

  it('a reported fold at 669px takes the two-pane form on a non-touch device too', () => {
    Object.assign(media, { width: 669, coarse: false, hoverNone: false, folded: true })
    const { result } = renderHook(() => useIsMobile())
    expect(result.current).toBe(false)
  })

  it('a reported fold below 640px keeps the phone form', () => {
    Object.assign(media, { width: 639, folded: true })
    const { result } = renderHook(() => useIsMobile())
    expect(result.current).toBe(true)
  })

  it('folding at the same width switches the form, with no resize', () => {
    const { result } = renderHook(() => useIsMobile())
    expect(result.current).toBe(true)
    act(() => { media.folded = true; for (const cb of listeners.get(FOLD_TWO_PANE_QUERY) ?? []) cb() })
    expect(result.current).toBe(false)
    act(() => { media.folded = false; for (const cb of listeners.get(FOLD_TWO_PANE_QUERY) ?? []) cb() })
    expect(result.current).toBe(true)
  })
})
