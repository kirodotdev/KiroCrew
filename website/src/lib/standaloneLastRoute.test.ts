import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  STANDALONE_LAST_ROUTE_KEY,
  isStandaloneDisplay,
  recordStandaloneRoute,
  restoreStandaloneLastRoute,
  sanitizeStandaloneRoute,
} from './standaloneLastRoute'

function setStandalone(on: boolean): void {
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: on && query === '(display-mode: standalone)',
    media: query,
    addEventListener: () => {},
    removeEventListener: () => {},
  }))
}

describe('standaloneLastRoute', () => {
  beforeEach(() => {
    localStorage.clear()
    window.history.replaceState(null, '', '/')
  })
  afterEach(() => {
    vi.unstubAllGlobals()
    window.history.replaceState(null, '', '/')
  })

  it('detects standalone display mode', () => {
    setStandalone(true)
    expect(isStandaloneDisplay()).toBe(true)
    setStandalone(false)
    expect(isStandaloneDisplay()).toBe(false)
  })

  it('reopens the last page when the standalone app launches at /', () => {
    setStandalone(true)
    window.history.replaceState(null, '', '/members?member=Raven')
    recordStandaloneRoute('/members?member=Raven')
    window.history.replaceState(null, '', '/')
    expect(restoreStandaloneLastRoute()).toBe('/members?member=Raven')
    expect(window.location.pathname).toBe('/members')
    expect(window.location.search).toBe('?member=Raven')
  })

  it('leaves a normal browser tab unchanged', () => {
    setStandalone(false)
    recordStandaloneRoute('/members?member=Raven')
    expect(localStorage.getItem(STANDALONE_LAST_ROUTE_KEY)).toBeNull()
    localStorage.setItem(STANDALONE_LAST_ROUTE_KEY, '/members?member=Raven')
    expect(restoreStandaloneLastRoute()).toBeNull()
    expect(window.location.pathname).toBe('/')
  })

  it('does not redirect a standalone launch that already names a page or query', () => {
    setStandalone(true)
    localStorage.setItem(STANDALONE_LAST_ROUTE_KEY, '/members')
    window.history.replaceState(null, '', '/chat')
    expect(restoreStandaloneLastRoute()).toBeNull()
    window.history.replaceState(null, '', '/?token=abc')
    expect(restoreStandaloneLastRoute()).toBeNull()
    expect(window.location.search).toBe('?token=abc')
  })

  it('ignores a tampered stored route instead of navigating off-origin', () => {
    setStandalone(true)
    for (const bad of ['https://evil.example/x', '//evil.example/x', '/\\evil.example', 'javascript:alert(1)', '/popout/chat/a', '/api/x', '/', 'chat']) {
      localStorage.setItem(STANDALONE_LAST_ROUTE_KEY, bad)
      window.history.replaceState(null, '', '/')
      expect(restoreStandaloneLastRoute()).toBeNull()
      expect(window.location.origin + window.location.pathname).toBe(`${window.location.origin}/`)
    }
  })

  it('never stores credential-looking parameters or the hash', () => {
    expect(sanitizeStandaloneRoute('/chat/a?token=secret&view=x')).toBe('/chat/a?view=x')
    expect(sanitizeStandaloneRoute('/chat/a#access_token=x')).toBe('/chat/a')
    expect(sanitizeStandaloneRoute('/sessions')).toBe('/sessions')
    expect(sanitizeStandaloneRoute(42)).toBeNull()
    expect(sanitizeStandaloneRoute(`/${'a'.repeat(3000)}`)).toBeNull()
    expect(sanitizeStandaloneRoute('/chat\n/x')).toBeNull()
  })

  it('does not record while the document is a pop-out window', () => {
    setStandalone(true)
    window.history.replaceState(null, '', '/popout/chat/a')
    recordStandaloneRoute('/chat/a')
    expect(localStorage.getItem(STANDALONE_LAST_ROUTE_KEY)).toBeNull()
  })
})
