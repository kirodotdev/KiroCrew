import { describe, it, expect, beforeEach, vi } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import type { ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useTheme, ThemeProvider } from '../hooks/useTheme'

// Every renderHook in this file goes through the provider so useTheme can
// resolve its context. Mirrors how the hook is consumed in production
// (main.tsx wraps App in <QueryClientProvider><ThemeProvider>).
const wrapper = ({ children }: { children: ReactNode }) => {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return (
    <QueryClientProvider client={queryClient}>
      <ThemeProvider>{children}</ThemeProvider>
    </QueryClientProvider>
  )
}

function renderThemeHook() {
  return renderHook(() => useTheme(), { wrapper })
}

function mockMatchMedia(matches: boolean) {
  const listeners: Array<() => void> = []
  const mql = {
    matches,
    addEventListener: vi.fn((_: string, cb: () => void) => listeners.push(cb)),
    removeEventListener: vi.fn((_: string, cb: () => void) => {
      const i = listeners.indexOf(cb)
      if (i >= 0) listeners.splice(i, 1)
    }),
  }
  window.matchMedia = vi.fn().mockReturnValue(mql)
  return {
    mql,
    listeners,
    setMatches: (v: boolean) => {
      mql.matches = v
      listeners.forEach(cb => cb())
    },
  }
}

describe('useTheme', () => {
  beforeEach(() => {
    localStorage.clear()
    delete document.documentElement.dataset.theme
    delete document.documentElement.dataset.mode
    delete document.documentElement.dataset.modePref
  })

  it('defaults to system preference (dark OS)', () => {
    mockMatchMedia(true)
    const { result } = renderThemeHook()
    expect(result.current.preference).toBe('system')
    expect(result.current.theme).toBe('dark')
  })

  it('defaults to system preference (light OS)', () => {
    mockMatchMedia(false)
    const { result } = renderThemeHook()
    expect(result.current.preference).toBe('system')
    expect(result.current.theme).toBe('light')
  })

  it('reads explicit preference from localStorage', () => {
    mockMatchMedia(true)
    localStorage.setItem('mc-theme', 'light')
    const { result } = renderThemeHook()
    expect(result.current.preference).toBe('light')
    expect(result.current.theme).toBe('light')
  })

  it('cycle rotates system -> light -> dark -> system', () => {
    mockMatchMedia(true)
    const { result } = renderThemeHook()
    expect(result.current.preference).toBe('system')

    act(() => result.current.cycle())
    expect(result.current.preference).toBe('light')

    act(() => result.current.cycle())
    expect(result.current.preference).toBe('dark')

    act(() => result.current.cycle())
    expect(result.current.preference).toBe('system')
  })

  it('persists preference to localStorage', () => {
    mockMatchMedia(true)
    const { result } = renderThemeHook()
    act(() => result.current.setTheme('light'))
    expect(localStorage.getItem('mc-theme')).toBe('light')
  })

  it('reacts to OS theme change when preference is system', () => {
    const { setMatches } = mockMatchMedia(true)
    const { result } = renderThemeHook()
    expect(result.current.theme).toBe('dark')

    act(() => setMatches(false))
    expect(result.current.theme).toBe('light')
  })

  it('ignores OS change when preference is explicit', () => {
    const { setMatches } = mockMatchMedia(true)
    localStorage.setItem('mc-theme', 'dark')
    const { result } = renderThemeHook()

    act(() => setMatches(false))
    expect(result.current.theme).toBe('dark')
  })

  it('sets data-theme on document element', () => {
    mockMatchMedia(true)
    // emerald is the base theme and renders unsuffixed (data-theme = mode).
    // Set it explicitly so this assertion is independent of the default theme.
    localStorage.setItem('mc-color-theme', 'emerald')
    renderThemeHook()
    expect(document.documentElement.dataset.theme).toBe('dark')
  })

  it('exposes the mode PREFERENCE separately from the resolved mode', () => {
    // The Electron shell maps data-mode-pref onto nativeTheme.themeSource.
    // It must see 'system' under Auto: themeSource='dark'/'light' also pins
    // prefers-color-scheme, which is what Auto resolves through, so feeding the
    // resolved mode back froze Auto at whatever it read on first load.
    mockMatchMedia(true)
    const { result } = renderThemeHook()
    expect(document.documentElement.dataset.mode).toBe('dark')
    expect(document.documentElement.dataset.modePref).toBe('system')

    act(() => result.current.setTheme('light'))
    expect(document.documentElement.dataset.mode).toBe('light')
    expect(document.documentElement.dataset.modePref).toBe('light')
  })

  it('pushes the mode preference to the Electron shell on change', () => {
    mockMatchMedia(true)
    const setThemeMode = vi.fn()
    ;(window as unknown as { electronAPI: unknown }).electronAPI = { setThemeMode }
    try {
      const { result } = renderThemeHook()
      expect(setThemeMode).toHaveBeenCalledWith('system')

      act(() => result.current.setTheme('dark'))
      expect(setThemeMode).toHaveBeenLastCalledWith('dark')

      // Back to Auto must push too — otherwise themeSource stays pinned and the
      // media query keeps lying until the next window focus.
      act(() => result.current.setTheme('system'))
      expect(setThemeMode).toHaveBeenLastCalledWith('system')
    } finally {
      delete (window as unknown as { electronAPI?: unknown }).electronAPI
    }
  })

  it('treats previously onboarded users as import-onboarded', () => {    mockMatchMedia(true)
    localStorage.setItem('mc-onboarded', '1')
    const { result } = renderThemeHook()
    expect(result.current.importOnboarded).toBe(true)
  })

  it('marks foreign-agent import onboarding complete', () => {
    mockMatchMedia(true)
    const { result } = renderThemeHook()
    expect(result.current.importOnboarded).toBe(false)

    act(() => result.current.markImportOnboarded())

    expect(result.current.importOnboarded).toBe(true)
    expect(localStorage.getItem('mc-import-onboarded')).toBe('1')
  })
})
