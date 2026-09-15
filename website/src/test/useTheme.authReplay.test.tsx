import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ApiError } from '../api/apiError'
import { refreshOnce, __resetRefreshOnceForTests } from '../api/refreshOnce'

// Bare-factory mock, exactly how the bulk of the corpus mocks this module: the
// replay path must work without any export beyond `api`, which is why useTheme
// reaches ApiError and pendingRefresh through leaf modules instead.
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

const wrapper = ({ children }: { children: ReactNode }) => {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return (
    <QueryClientProvider client={queryClient}>
      <ThemeProvider>{children}</ThemeProvider>
    </QueryClientProvider>
  )
}

const SLUG = 'godspeed-mission-control'
const catalog = { themes: [{ slug: SLUG, name: 'Godspeed Mission Control', emoji: '🚀', source: 'installed' }] }
const detail = {
  slug: SLUG,
  name: 'Godspeed Mission Control',
  emoji: '🚀',
  dark: { '--bg': '#141b21' },
  light: { '--bg': '#f4f1de' },
  level: 1,
  assets: { branding: { botName: 'Godspeed' }, hasOverrides: false },
}

/** What `checkSessionExpired` does on a 403: start the silent refresh in the
 *  background and let the ORIGINAL request reject through `j`. */
function authDeniedWithRefreshStarted(): Promise<never> {
  void refreshOnce()
  return Promise.reject(new ApiError(403, 'Session expired', 'Token required', true))
}

/** Deferred control over POST /api/auth/refresh so the test decides when
 *  recovery lands, and with which outcome. */
function installRefreshFetch() {
  let settle!: (status: number) => void
  const gate = new Promise<number>((resolve) => { settle = resolve })
  const fetchFn = vi.fn(async (input: RequestInfo | URL) => {
    const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
    if (!url.includes('/api/auth/refresh')) throw new Error(`unexpected fetch ${url}`)
    const status = await gate
    return new Response(JSON.stringify({}), { status })
  })
  vi.stubGlobal('fetch', fetchFn)
  return { settle, fetchFn }
}

describe('useTheme: installed-theme catalog replay after silent auth recovery', () => {
  beforeEach(() => {
    localStorage.clear()
    document.head.querySelectorAll('style[data-custom-theme]').forEach((n) => n.remove())
    delete document.documentElement.dataset.theme
    window.matchMedia = vi.fn().mockReturnValue({
      matches: true,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    }) as unknown as typeof window.matchMedia
    themesFn.mockReset()
    themeDetailFn.mockReset()
    themeBootFn.mockReset()
    themeBootFn.mockResolvedValue({ mode: 'dark', color: `custom-${SLUG}` })
    themeDetailFn.mockResolvedValue(detail)
    __resetRefreshOnceForTests()
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    __resetRefreshOnceForTests()
  })

  it('replays /api/themes once the refresh lands, so the selected theme loads on the first page load', async () => {
    const { settle, fetchFn } = installRefreshFetch()
    themesFn.mockImplementationOnce(authDeniedWithRefreshStarted).mockResolvedValue(catalog)

    const { result } = renderHook(() => useTheme(), { wrapper })
    await waitFor(() => expect(result.current.colorTheme).toBe(`custom-${SLUG}`))
    await waitFor(() => expect(fetchFn).toHaveBeenCalledTimes(1))

    // Recovery is still in flight: nothing has been replayed yet.
    expect(themesFn).toHaveBeenCalledTimes(1)
    expect(result.current.customThemes).toEqual([])

    settle(200)

    await waitFor(() => expect(themesFn).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(result.current.customThemes).toHaveLength(1))
    await waitFor(() => expect(result.current.brandName).toBe('Godspeed'))
    expect(result.current.customThemeDataMap.get(SLUG)).toEqual(detail)
  })

  it('does not replay when the refresh itself fails — the re-auth banner owns recovery', async () => {
    const { settle } = installRefreshFetch()
    themesFn.mockImplementationOnce(authDeniedWithRefreshStarted).mockResolvedValue(catalog)

    const { result } = renderHook(() => useTheme(), { wrapper })
    await waitFor(() => expect(themesFn).toHaveBeenCalledTimes(1))
    settle(401)

    await new Promise((r) => setTimeout(r, 30))
    expect(themesFn).toHaveBeenCalledTimes(1)
    expect(result.current.customThemes).toEqual([])
  })

  it('replays at most once — a second auth denial is not retried again', async () => {
    const { settle } = installRefreshFetch()
    themesFn
      .mockImplementationOnce(authDeniedWithRefreshStarted)
      .mockImplementationOnce(authDeniedWithRefreshStarted)
      .mockResolvedValue(catalog)

    renderHook(() => useTheme(), { wrapper })
    await waitFor(() => expect(themesFn).toHaveBeenCalledTimes(1))
    settle(200)

    await waitFor(() => expect(themesFn).toHaveBeenCalledTimes(2))
    await new Promise((r) => setTimeout(r, 30))
    expect(themesFn).toHaveBeenCalledTimes(2)
  })

  it('leaves non-auth failures alone (API not available yet)', async () => {
    const { fetchFn } = installRefreshFetch()
    themesFn.mockRejectedValueOnce(new ApiError(503, 'unavailable')).mockResolvedValue(catalog)

    const { result } = renderHook(() => useTheme(), { wrapper })
    await waitFor(() => expect(themesFn).toHaveBeenCalledTimes(1))

    await new Promise((r) => setTimeout(r, 30))
    expect(themesFn).toHaveBeenCalledTimes(1)
    expect(fetchFn).not.toHaveBeenCalled()
    expect(result.current.customThemes).toEqual([])
  })
})
