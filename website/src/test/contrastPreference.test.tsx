/**
 * The "Contrast" display setting: `normal` keeps the chosen theme (today's
 * behavior), `auto` swaps in the built-in High Contrast palette while the OS
 * asks for more contrast, `more` always does. The swap happens only on the root
 * `data-theme` attribute; the stored theme pick is never rewritten.
 */
import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import type { ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider, useTheme } from '../hooks/useTheme'
import { useContrastPreference } from '../hooks/useContrastPreference'
import {
  CONTRAST_STORAGE_KEY,
  contrastWanted,
  parseContrastPreference,
  persistContrastPreference,
  readContrastPreference,
} from '../utils/contrastPreference'

/** A controllable `prefers-contrast: more` media query; other queries never match. */
function installMatchMedia(initial: boolean) {
  let matches = initial
  const listeners = new Set<() => void>()
  const original = window.matchMedia
  window.matchMedia = ((query: string) => ({
    get matches() { return query === '(prefers-contrast: more)' ? matches : false },
    media: query,
    addEventListener: (_: string, fn: () => void) => { if (query === '(prefers-contrast: more)') listeners.add(fn) },
    removeEventListener: (_: string, fn: () => void) => { listeners.delete(fn) },
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => false,
    onchange: null,
  })) as unknown as typeof window.matchMedia
  return {
    set(next: boolean) {
      matches = next
      listeners.forEach((fn) => fn())
    },
    restore() { window.matchMedia = original },
  }
}

const wrapper = ({ children }: { children: ReactNode }) => (
  <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <ThemeProvider>{children}</ThemeProvider>
  </QueryClientProvider>
)

let media: ReturnType<typeof installMatchMedia>
beforeEach(() => {
  localStorage.clear()
  localStorage.setItem('mc-theme', 'dark')
  localStorage.setItem('mc-color-theme', 'nord')
  media = installMatchMedia(false)
})
afterEach(() => {
  media.restore()
  localStorage.clear()
  document.documentElement.removeAttribute('data-theme')
})

describe('contrast preference utils', () => {
  it('reads anything but auto or more as normal, so an absent key keeps today\'s look', () => {
    expect(CONTRAST_STORAGE_KEY).toBe('mc-contrast')
    expect(readContrastPreference()).toBe('normal')
    localStorage.setItem(CONTRAST_STORAGE_KEY, 'more')
    expect(readContrastPreference()).toBe('more')
    localStorage.setItem(CONTRAST_STORAGE_KEY, 'auto')
    expect(readContrastPreference()).toBe('auto')
    localStorage.setItem(CONTRAST_STORAGE_KEY, 'loud')
    expect(readContrastPreference()).toBe('normal')
    expect(parseContrastPreference(undefined)).toBe('normal')
  })

  it('stores normal as the absence of the key', () => {
    persistContrastPreference('more')
    expect(localStorage.getItem(CONTRAST_STORAGE_KEY)).toBe('more')
    persistContrastPreference('normal')
    expect(localStorage.getItem(CONTRAST_STORAGE_KEY)).toBeNull()
  })

  it('wants the palette for more, and for auto only while the OS asks', () => {
    expect(contrastWanted('normal', true)).toBe(false)
    expect(contrastWanted('normal', false)).toBe(false)
    expect(contrastWanted('auto', true)).toBe(true)
    expect(contrastWanted('auto', false)).toBe(false)
    expect(contrastWanted('more', false)).toBe(true)
  })
})

describe('contrast preference when the store refuses the write', () => {
  afterEach(() => {
    vi.restoreAllMocks()
    persistContrastPreference('normal')
  })

  it('keeps the choice for a reader mounted later, until a write succeeds', () => {
    const refuse = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('quota')
    })
    const first = renderHook(() => useContrastPreference())
    act(() => first.result.current.setPref('more'))
    first.unmount()
    const remounted = renderHook(() => useContrastPreference())
    expect(remounted.result.current.pref).toBe('more')
    remounted.unmount()
    refuse.mockRestore()
    persistContrastPreference('auto')
    expect(readContrastPreference()).toBe('auto')
    expect(localStorage.getItem(CONTRAST_STORAGE_KEY)).toBe('auto')
  })
})

describe('contrast preference on the shell', () => {
  it('leaves the chosen theme alone when the setting is normal, even if the OS asks', () => {
    media.set(true)
    renderHook(() => useTheme(), { wrapper })
    expect(document.documentElement.dataset.theme).toBe('nord-dark')
  })

  it('paints High Contrast under auto only while the OS asks, then restores the pick', () => {
    localStorage.setItem(CONTRAST_STORAGE_KEY, 'auto')
    const { result } = renderHook(() => useTheme(), { wrapper })
    expect(document.documentElement.dataset.theme).toBe('nord-dark')
    act(() => media.set(true))
    expect(document.documentElement.dataset.theme).toBe('highcontrast-dark')
    expect(result.current.colorTheme).toBe('nord')
    act(() => media.set(false))
    expect(document.documentElement.dataset.theme).toBe('nord-dark')
  })

  it('paints High Contrast under more, follows the mode, and goes back when set to normal', () => {
    const { result } = renderHook(() => ({ theme: useTheme(), contrast: useContrastPreference() }), { wrapper })
    act(() => result.current.contrast.setPref('more'))
    expect(document.documentElement.dataset.theme).toBe('highcontrast-dark')
    act(() => result.current.theme.setTheme('light'))
    expect(document.documentElement.dataset.theme).toBe('highcontrast-light')
    act(() => result.current.contrast.setPref('normal'))
    expect(document.documentElement.dataset.theme).toBe('nord-light')
    expect(localStorage.getItem('mc-color-theme')).toBe('nord')
  })
})

describe('contrast preference changed in another tab', () => {
  /** What the browser does in a second tab: the shared store changes, then this tab hears `storage`. */
  function otherTab(key: string | null, newValue: string | null, area: Storage | null = localStorage) {
    act(() => {
      if (key === null) localStorage.clear()
      else if (newValue === null) localStorage.removeItem(key)
      else localStorage.setItem(key, newValue)
      window.dispatchEvent(new StorageEvent('storage', { key, newValue, storageArea: area }))
    })
  }

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('repaints the shell and the setting row when another tab sets, removes or clears it', () => {
    const { result } = renderHook(() => ({ theme: useTheme(), contrast: useContrastPreference() }), { wrapper })
    expect(document.documentElement.dataset.theme).toBe('nord-dark')
    otherTab(CONTRAST_STORAGE_KEY, 'more')
    expect(result.current.contrast.pref).toBe('more')
    expect(document.documentElement.dataset.theme).toBe('highcontrast-dark')
    otherTab(CONTRAST_STORAGE_KEY, null)
    expect(result.current.contrast.pref).toBe('normal')
    expect(document.documentElement.dataset.theme).toBe('nord-dark')
    otherTab(CONTRAST_STORAGE_KEY, 'auto')
    expect(result.current.contrast.pref).toBe('auto')
    otherTab(null, null)
    expect(result.current.contrast.pref).toBe('normal')
    expect(document.documentElement.dataset.theme).toBe('nord-dark')
  })

  it('updates every mounted reader and writes nothing back', () => {
    const first = renderHook(() => useContrastPreference())
    const second = renderHook(() => useContrastPreference())
    const write = vi.spyOn(Storage.prototype, 'setItem')
    const remove = vi.spyOn(Storage.prototype, 'removeItem')
    const announced = vi.fn()
    window.addEventListener('mc-contrast-changed', announced)
    otherTab(CONTRAST_STORAGE_KEY, 'more')
    expect(first.result.current.pref).toBe('more')
    expect(second.result.current.pref).toBe('more')
    write.mockClear()
    remove.mockClear()
    announced.mockClear()
    act(() => {
      window.dispatchEvent(
        new StorageEvent('storage', { key: CONTRAST_STORAGE_KEY, newValue: 'auto', storageArea: localStorage }),
      )
    })
    expect(first.result.current.pref).toBe('auto')
    expect(write).not.toHaveBeenCalled()
    expect(remove).not.toHaveBeenCalled()
    expect(announced).not.toHaveBeenCalled()
    window.removeEventListener('mc-contrast-changed', announced)
  })

  it('ignores other keys and other storage areas', () => {
    const { result } = renderHook(() => useContrastPreference())
    otherTab(CONTRAST_STORAGE_KEY, 'more')
    otherTab('mc-theme', 'light')
    expect(result.current.pref).toBe('more')
    act(() => {
      window.dispatchEvent(
        new StorageEvent('storage', { key: CONTRAST_STORAGE_KEY, newValue: null, storageArea: sessionStorage }),
      )
    })
    expect(result.current.pref).toBe('more')
  })

  it('lets the other tab\'s choice replace one this tab could only keep in memory', () => {
    const refuse = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('quota')
    })
    const mounted = renderHook(() => useContrastPreference())
    act(() => mounted.result.current.setPref('more'))
    refuse.mockRestore()
    otherTab(CONTRAST_STORAGE_KEY, 'auto')
    expect(mounted.result.current.pref).toBe('auto')
    mounted.unmount()
    expect(renderHook(() => useContrastPreference()).result.current.pref).toBe('auto')
    persistContrastPreference('normal')
  })
})
