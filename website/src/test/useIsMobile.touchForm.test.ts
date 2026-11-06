import { describe, it, expect, beforeAll, beforeEach, afterEach, vi } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

/**
 * On a touch-first device (coarse pointer that cannot hover) the layout form
 * follows the browser's own mobile/desktop mode, not the width: an unfolded
 * foldable phone in landscape is ~951px wide and still a phone. Every other
 * device keeps the 768px width breakpoint.
 */

const media = { width: 951, coarse: false, hoverNone: false }
const listeners = new Set<() => void>()
function mqlFor(query: string): MediaQueryList {
  const matches = () => {
    if (query === '(pointer: coarse)') return media.coarse
    if (query === '(hover: none)') return media.hoverNone
    const max = /max-width:\s*(\d+)px/.exec(query)
    return max ? media.width <= Number(max[1]) : false
  }
  return {
    get matches() { return matches() },
    media: query,
    addEventListener: (_: string, cb: () => void) => { listeners.add(cb) },
    removeEventListener: (_: string, cb: () => void) => { listeners.delete(cb) },
  } as unknown as MediaQueryList
}

const PHONE_UA = 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1'
const DESKTOP_UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15'

let useIsMobile: () => boolean

function setUA(ua: string, hintMobile?: boolean) {
  Object.defineProperty(navigator, 'userAgent', { configurable: true, get: () => ua })
  Object.defineProperty(navigator, 'userAgentData', {
    configurable: true,
    get: () => (hintMobile === undefined ? undefined : { mobile: hintMobile }),
  })
}

beforeAll(async () => {
  vi.resetModules()
  Object.defineProperty(window, 'matchMedia', { writable: true, value: vi.fn((q: string) => mqlFor(q)) })
  useIsMobile = (await import('../hooks/useIsMobile')).useIsMobile
})

describe('useIsMobile on a touch-first device', () => {
  beforeEach(() => {
    Object.assign(media, { width: 951, coarse: false, hoverNone: false })
    listeners.clear()
  })
  afterEach(() => {
    delete (navigator as unknown as Record<string, unknown>).userAgent
    delete (navigator as unknown as Record<string, unknown>).userAgentData
  })

  it('touch + a mobile browser gets the phone layout at 951px', () => {
    Object.assign(media, { coarse: true, hoverNone: true })
    setUA(PHONE_UA)
    const { result } = renderHook(() => useIsMobile())
    expect(result.current).toBe(true)
  })

  it('marks the phone form on <html> so md: CSS stays off at 951px', () => {
    Object.assign(media, { coarse: true, hoverNone: true })
    setUA(PHONE_UA)
    renderHook(() => useIsMobile())
    expect(document.documentElement.dataset.layoutForm).toBe('phone')
  })

  it('marks the desktop form on <html> for a non-touch device at 951px', () => {
    setUA(PHONE_UA)
    renderHook(() => useIsMobile())
    expect(document.documentElement.dataset.layoutForm).toBe('desktop')
  })

  it('touch + UA Client Hints mobile:true wins over a UA string without the Mobile token', () => {
    Object.assign(media, { coarse: true, hoverNone: true })
    setUA(DESKTOP_UA, true)
    const { result } = renderHook(() => useIsMobile())
    expect(result.current).toBe(true)
  })

  it('touch + a desktop-mode browser (iPad Safari, Request desktop site) gets the desktop layout at 951px', () => {
    Object.assign(media, { coarse: true, hoverNone: true })
    setUA(DESKTOP_UA, false)
    const { result } = renderHook(() => useIsMobile())
    expect(result.current).toBe(false)
  })

  it('touch + a desktop-mode browser below 768px (iPad Split View) keeps the phone layout', () => {
    Object.assign(media, { coarse: true, hoverNone: true, width: 500 })
    setUA(DESKTOP_UA)
    const { result } = renderHook(() => useIsMobile())
    expect(result.current).toBe(true)
  })

  it('a non-touch device at 951px keeps the desktop layout, even with a mobile UA', () => {
    setUA(PHONE_UA)
    const { result } = renderHook(() => useIsMobile())
    expect(result.current).toBe(false)
  })

  it('a non-touch device below 768px keeps the phone layout', () => {
    media.width = 700
    setUA(DESKTOP_UA)
    const { result } = renderHook(() => useIsMobile())
    expect(result.current).toBe(true)
  })

  it('a coarse pointer that can still hover (a touch laptop) keeps the width rule', () => {
    Object.assign(media, { coarse: true, hoverNone: false })
    setUA(PHONE_UA)
    const { result } = renderHook(() => useIsMobile())
    expect(result.current).toBe(false)
  })

  it('rotating a mobile-mode touch device across 768px keeps the phone layout', () => {
    Object.assign(media, { coarse: true, hoverNone: true, width: 669 })
    setUA(PHONE_UA)
    const { result } = renderHook(() => useIsMobile())
    expect(result.current).toBe(true)
    act(() => { media.width = 951; window.dispatchEvent(new Event('resize')) })
    expect(result.current).toBe(true)
    act(() => { media.width = 669; window.dispatchEvent(new Event('orientationchange')) })
    expect(result.current).toBe(true)
  })
})

describe('md: and up are the desktop form (tailwind-theme.css)', () => {
  it('each breakpoint variant from md up is off while <html> is marked phone', () => {
    const css = readFileSync(join(__dirname, '..', 'tailwind-theme.css'), 'utf8')
    for (const [name, rem] of [['md', '48rem'], ['lg', '64rem'], ['xl', '80rem'], ['2xl', '96rem']]) {
      const re = new RegExp(`@custom-variant ${name} \\{\\s*@media \\(width >= ${rem}\\) \\{ &:where\\(:root:not\\(\\[data-layout-form="phone"\\]\\) \\*\\) \\{ @slot; \\} \\}`)
      expect(css, `${name}: keyed on the form`).toMatch(re)
    }
  })
})
