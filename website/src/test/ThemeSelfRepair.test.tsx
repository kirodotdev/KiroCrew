import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

// Controllable api mock so loadCustomThemes resolves (customThemesLoaded → true),
// which is the gate the self-repair effect waits on.
const themesFn = vi.fn()
const themeDetailFn = vi.fn()
vi.mock('../api/client', () => ({
  api: {
    themes: () => themesFn(),
    themeDetail: (slug: string) => themeDetailFn(slug),
    themeBoot: () => Promise.resolve({}),
    updateThemeConfig: () => Promise.resolve({}),
  },
}))

import { useTheme, ThemeProvider, registerTheme } from '../hooks/useTheme'

const wrapper = ({ children }: { children: ReactNode }) => {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return (
    <QueryClientProvider client={queryClient}>
      <ThemeProvider>{children}</ThemeProvider>
    </QueryClientProvider>
  )
}

describe('useTheme — self-repair (decision 6)', () => {
  beforeEach(() => {
    localStorage.clear()
    delete document.documentElement.dataset.theme
    window.matchMedia = vi.fn().mockReturnValue({
      matches: true,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    }) as unknown as typeof window.matchMedia
    themesFn.mockReset()
    themeDetailFn.mockReset()
  })

  it('resets a dangling custom-<slug> selection to the default built-in', async () => {
    themesFn.mockResolvedValue({ themes: [] }) // the persisted theme is gone
    localStorage.setItem('mc-color-theme', 'custom-ghost')
    const { result } = renderHook(() => useTheme(), { wrapper })
    await waitFor(() => expect(result.current.colorTheme).toBe('kiro'))
  })

  it('keeps a still-present installed selection', async () => {
    themesFn.mockResolvedValue({
      themes: [{ slug: 'reef', name: 'Reef', emoji: '🐠', source: 'installed' }],
    })
    themeDetailFn.mockResolvedValue({
      slug: 'reef', name: 'Reef', emoji: '🐠', dark: {}, light: {}, level: 0,
    })
    localStorage.setItem('mc-color-theme', 'custom-reef')
    const { result } = renderHook(() => useTheme(), { wrapper })
    // Wait until the theme list has loaded, then assert it was NOT reset.
    await waitFor(() => expect(result.current.customThemes.length).toBe(1))
    expect(result.current.colorTheme).toBe('custom-reef')
  })

  it('resets an unknown built-in selection (e.g. the retired lumon) to the default', async () => {
    themesFn.mockResolvedValue({ themes: [] })
    // A value persisted by an older build whose built-in theme no longer exists.
    localStorage.setItem('mc-color-theme', 'lumon')
    const { result } = renderHook(() => useTheme(), { wrapper })
    await waitFor(() => expect(result.current.colorTheme).toBe('kiro'))
  })

  it('keeps a downstream-registered (edition) theme selection', async () => {
    // Regression: edition themes added via registerTheme() live in
    // REGISTERED_THEMES, not the core THEMES array. Self-repair must treat them
    // as valid built-ins — otherwise selecting LCARS/Lumon/Miami/etc bounced
    // straight back to the default 'kiro'.
    registerTheme([{ value: 'lcars', label: '🖖 LCARS' }])
    themesFn.mockResolvedValue({ themes: [] })
    localStorage.setItem('mc-color-theme', 'lcars')
    const { result } = renderHook(() => useTheme(), { wrapper })
    // Wait until the custom-theme load has run (which flips customThemesLoaded
    // and re-fires the self-repair effect) — if the bug were present the value
    // would have been reset by now.
    await waitFor(() => expect(themesFn).toHaveBeenCalled())
    expect(result.current.colorTheme).toBe('lcars')
  })
})
