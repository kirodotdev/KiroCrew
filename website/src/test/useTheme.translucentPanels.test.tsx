/**
 * The boot read of `dashboard.translucent_panels` (GET /api/theme/boot) in
 * `useTheme`: the workspace value is adopted into the browser like `mc-theme`,
 * and a workspace that predates the key ('' = never chosen) keeps the browser's
 * own choice, publishing an ON copy once so a second browser can adopt it.
 * Without this an upgrade, a new gateway port or a second browser read as "the
 * glass turned itself off" (#24704).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const themeBootFn = vi.fn()
const updateThemeConfigFn = vi.fn()
vi.mock('../api/client', () => ({
  api: {
    themes: () => Promise.resolve({ themes: [] }),
    themeDetail: () => Promise.resolve({}),
    themeBoot: () => themeBootFn(),
    updateThemeConfig: (body: unknown) => updateThemeConfigFn(body),
    createTheme: () => Promise.resolve({}),
    deleteTheme: () => Promise.resolve({}),
  },
}))

import { ThemeProvider, useTheme } from '../hooks/useTheme'
import { LIQUID_GLASS_STORAGE_KEY } from '../utils/liquidGlass'

function wrapper({ children }: { children: ReactNode }) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return (
    <QueryClientProvider client={queryClient}>
      <ThemeProvider>{children}</ThemeProvider>
    </QueryClientProvider>
  )
}

function boot(translucent_panels: string) {
  themeBootFn.mockResolvedValue({ mode: '', color: '', translucent_panels, onboarded: true, import_onboarded: true, privacy_acked: true })
  return renderHook(() => useTheme(), { wrapper })
}

async function untilBooted() {
  await waitFor(() => expect(themeBootFn).toHaveBeenCalled())
  // The boot effect runs on the fetched data one tick after the query resolves.
  await waitFor(() => expect(localStorage.getItem('mc-onboarded')).toBe('1'))
}

describe('useTheme adopts the workspace Translucent panels choice at boot', () => {
  beforeEach(() => {
    localStorage.clear()
    updateThemeConfigFn.mockReset().mockResolvedValue({})
    themeBootFn.mockReset()
    window.matchMedia = vi.fn().mockReturnValue({
      matches: true,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    })
  })
  afterEach(() => {
    document.documentElement.removeAttribute('data-reduce-transparency')
  })

  it("'on' turns a fresh browser's glass on: a second browser matches the first", async () => {
    boot('on')
    await untilBooted()
    expect(localStorage.getItem(LIQUID_GLASS_STORAGE_KEY)).toBe('on')
    expect(document.documentElement.dataset.reduceTransparency).toBe('off')
    expect(updateThemeConfigFn).not.toHaveBeenCalledWith(expect.objectContaining({ translucent_panels: expect.anything() }))
  })

  it("'off' wins over a stale browser copy that says on", async () => {
    localStorage.setItem(LIQUID_GLASS_STORAGE_KEY, 'on')
    boot('off')
    await untilBooted()
    expect(localStorage.getItem(LIQUID_GLASS_STORAGE_KEY)).toBeNull()
    expect(document.documentElement.dataset.reduceTransparency).toBe('on')
    expect(updateThemeConfigFn).not.toHaveBeenCalledWith(expect.objectContaining({ translucent_panels: expect.anything() }))
  })

  it("'' with the glass on in this browser keeps it on and publishes the choice once", async () => {
    // The upgrade case: the browser copy is the only record of the choice.
    localStorage.setItem(LIQUID_GLASS_STORAGE_KEY, 'on')
    boot('')
    await untilBooted()
    expect(localStorage.getItem(LIQUID_GLASS_STORAGE_KEY)).toBe('on')
    await waitFor(() => expect(updateThemeConfigFn).toHaveBeenCalledWith({ translucent_panels: 'on' }))
    expect(updateThemeConfigFn.mock.calls.filter(([b]) => (b as { translucent_panels?: string }).translucent_panels !== undefined)).toHaveLength(1)
  })

  it("'' with the glass off in this browser writes nothing: off is also what a fresh browser holds", async () => {
    boot('')
    await untilBooted()
    expect(localStorage.getItem(LIQUID_GLASS_STORAGE_KEY)).toBeNull()
    expect(updateThemeConfigFn).not.toHaveBeenCalledWith(expect.objectContaining({ translucent_panels: expect.anything() }))
  })
})
