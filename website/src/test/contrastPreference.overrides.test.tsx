/**
 * An installed pack's overrides.css stays out of the way of the High Contrast
 * palette: its allowed `body` rules can set colour tokens below the root
 * palette, so the style element is kept inert (a media query that never
 * matches) while contrast is wanted and live again when it is not.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, renderHook, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const { themesFn, themeDetailFn } = vi.hoisted(() => ({
  themesFn: vi.fn(),
  themeDetailFn: vi.fn(),
}))

vi.mock('../api/client', () => ({
  api: {
    themes: () => themesFn(),
    themeDetail: (slug: string) => themeDetailFn(slug),
    themeBoot: () => Promise.resolve({}),
    updateThemeConfig: () => Promise.resolve({}),
  },
}))

import { ThemeProvider, useTheme } from '../hooks/useTheme'
import { useContrastPreference } from '../hooks/useContrastPreference'
import { CONTRAST_STORAGE_KEY } from '../utils/contrastPreference'

const OVERRIDES_ID = 'mc-theme-overrides'

function quietMatchMedia() {
  const original = window.matchMedia
  window.matchMedia = vi.fn().mockReturnValue({
    matches: false,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  }) as unknown as typeof window.matchMedia
  return () => { window.matchMedia = original }
}

describe('installed pack overrides while High Contrast is painted', () => {
  let restoreMedia: () => void
  let origFetch: unknown
  beforeEach(() => {
    localStorage.clear()
    document.getElementById(OVERRIDES_ID)?.remove()
    restoreMedia = quietMatchMedia()
    origFetch = (globalThis as unknown as { fetch: unknown }).fetch
    themesFn.mockReset()
    themeDetailFn.mockReset()
    themesFn.mockResolvedValue({
      themes: [{ slug: 'scoped', name: 'Scoped', emoji: '🎨', source: 'installed' }],
    })
    themeDetailFn.mockResolvedValue({
      slug: 'scoped', name: 'Scoped', emoji: '🎨', dark: {}, light: {}, level: 1,
      assets: { hasOverrides: true },
    })
    localStorage.setItem('mc-color-theme', 'custom-scoped')
    ;(globalThis as unknown as { fetch: unknown }).fetch = vi.fn().mockResolvedValue({
      ok: true,
      text: () => Promise.resolve('body{--bg:#eeeeee;--text:#777777}'),
    })
  })
  afterEach(() => {
    restoreMedia()
    ;(globalThis as unknown as { fetch: unknown }).fetch = origFetch
    localStorage.clear()
    document.getElementById(OVERRIDES_ID)?.remove()
  })

  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <ThemeProvider>{children}</ThemeProvider>
    </QueryClientProvider>
  )

  const overrides = () => document.getElementById(OVERRIDES_ID) as HTMLStyleElement | null

  it('applies the pack under Normal', async () => {
    renderHook(() => useTheme(), { wrapper })
    await waitFor(() => expect(overrides()).toBeTruthy())
    expect(overrides()!.textContent).toContain('--bg')
  })

  it('offers the painted theme: High Contrast while wanted, the stored pick otherwise', async () => {
    const { result } = renderHook(() => ({ theme: useTheme(), contrast: useContrastPreference() }), { wrapper })
    await waitFor(() => expect(result.current.theme.colorTheme).toBe('custom-scoped'))
    expect(result.current.theme.paintedTheme).toBe('custom-scoped')
    act(() => result.current.contrast.setPref('more'))
    expect(result.current.theme.paintedTheme).toBe('highcontrast')
    expect(result.current.theme.colorTheme).toBe('custom-scoped')
    act(() => result.current.contrast.setPref('normal'))
    expect(result.current.theme.paintedTheme).toBe('custom-scoped')
  })

  it('keeps the pack out of the page while contrast is wanted and puts it back afterwards', async () => {
    localStorage.setItem(CONTRAST_STORAGE_KEY, 'more')
    const { result } = renderHook(() => ({ theme: useTheme(), contrast: useContrastPreference() }), { wrapper })
    const fetched = (globalThis as unknown as { fetch: ReturnType<typeof vi.fn> }).fetch
    await waitFor(() => expect(fetched).toHaveBeenCalled())
    await act(async () => { await Promise.resolve() })
    expect(overrides()).toBeNull()
    act(() => result.current.contrast.setPref('normal'))
    expect(overrides()!.textContent).toContain('--bg')
    act(() => result.current.contrast.setPref('more'))
    expect(overrides()).toBeNull()
    expect(fetched).toHaveBeenCalledTimes(1)
  })
})
