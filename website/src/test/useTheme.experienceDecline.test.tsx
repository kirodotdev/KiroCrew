import { beforeEach, describe, expect, it, vi } from 'vitest'
import { act, renderHook, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

// "Keep colors only" is remembered per slug until the user picks the theme in
// the picker again. useTheme's setColorTheme is that pick; the boot restore of a
// persisted selection is not, and must leave the answer alone.
const themesFn = vi.fn()
const themeDetailFn = vi.fn()
const themeBootFn = vi.fn()
vi.mock('../api/client', () => ({
  api: {
    themes: () => themesFn(),
    themeDetail: (slug: string) => themeDetailFn(slug),
    themeBoot: () => themeBootFn(),
    updateThemeConfig: () => Promise.resolve({}),
  },
}))

import { ThemeProvider, useTheme } from '../hooks/useTheme'

const SLUG = 'haunted'
const DECLINE_KEY = `mc-theme-experience-declined-${SLUG}`
const catalog = { themes: [{ slug: SLUG, name: 'Haunted', emoji: '🎃', source: 'installed' }] }
const detail = {
  slug: SLUG,
  name: 'Haunted',
  emoji: '🎃',
  dark: { '--bg': '#100818' },
  light: { '--bg': '#f5f0fa' },
  level: 2,
  assets: { hasPersona: true, hasOverrides: false },
}

function wrapper({ children }: { children: ReactNode }) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return (
    <QueryClientProvider client={queryClient}>
      <ThemeProvider>{children}</ThemeProvider>
    </QueryClientProvider>
  )
}

describe('useTheme: the "Keep colors only" answer', () => {
  beforeEach(() => {
    localStorage.clear()
    window.matchMedia = vi.fn().mockReturnValue({
      matches: true,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    }) as unknown as typeof window.matchMedia
    themesFn.mockReset().mockResolvedValue(catalog)
    themeDetailFn.mockReset().mockResolvedValue(detail)
    themeBootFn.mockReset()
  })

  it('is forgotten when the user picks the theme', async () => {
    themeBootFn.mockResolvedValue({ mode: 'dark', color: 'kiro' })
    localStorage.setItem(DECLINE_KEY, 'consented-v2')
    const { result } = renderHook(() => useTheme(), { wrapper })
    await waitFor(() => expect(result.current.customThemes).toHaveLength(1))
    act(() => result.current.setColorTheme(`custom-${SLUG}`))
    expect(result.current.colorTheme).toBe(`custom-${SLUG}`)
    expect(localStorage.getItem(DECLINE_KEY)).toBeNull()
  })

  it('survives the boot restore of the same theme', async () => {
    themeBootFn.mockResolvedValue({ mode: 'dark', color: `custom-${SLUG}` })
    localStorage.setItem(DECLINE_KEY, 'consented-v2')
    const { result } = renderHook(() => useTheme(), { wrapper })
    await waitFor(() => expect(result.current.colorTheme).toBe(`custom-${SLUG}`))
    await waitFor(() => expect(result.current.customThemeDataMap.get(SLUG)).toEqual(detail))
    expect(localStorage.getItem(DECLINE_KEY)).toBe('consented-v2')
  })

  it('is not touched by picking a built-in theme', async () => {
    themeBootFn.mockResolvedValue({ mode: 'dark', color: `custom-${SLUG}` })
    localStorage.setItem(DECLINE_KEY, 'consented-v2')
    const { result } = renderHook(() => useTheme(), { wrapper })
    await waitFor(() => expect(result.current.colorTheme).toBe(`custom-${SLUG}`))
    act(() => result.current.setColorTheme('kiro'))
    expect(localStorage.getItem(DECLINE_KEY)).toBe('consented-v2')
  })
})
