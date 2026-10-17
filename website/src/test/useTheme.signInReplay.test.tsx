import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, renderHook, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ApiError } from '../api/apiError'
import { refreshOnce, __resetRefreshOnceForTests } from '../api/refreshOnce'

// Same bare-factory mock as useTheme.authReplay.test.tsx: the hook must work
// with no export beyond `api`, which is how most of the corpus mocks this module.
const themesFn = vi.fn()
const themeDetailFn = vi.fn()
const themeBootFn = vi.fn()
const createThemeFn = vi.fn()
const deleteThemeFn = vi.fn()
vi.mock('../api/client', () => ({
  api: {
    themes: () => themesFn(),
    themeDetail: (slug: string) => themeDetailFn(slug),
    themeBoot: () => themeBootFn(),
    updateThemeConfig: () => Promise.resolve({}),
    createTheme: (data: unknown) => createThemeFn(data),
    deleteTheme: (slug: string) => deleteThemeFn(slug),
  },
}))

import { ThemeProvider, useTheme } from '../hooks/useTheme'

/** One QueryClient per test, handed back so a test can drive the same
 *  invalidation `removeAuthBanner`'s token-paste path runs in api/client. */
function makeWrapper() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>
      <ThemeProvider>{children}</ThemeProvider>
    </QueryClientProvider>
  )
  return { queryClient, wrapper }
}

/** Mirror of api/client's token-paste recovery (`exchangePastedToken` ->
 *  `removeAuthBanner` -> this predicate): refetch every query that never
 *  carried a value. The client module is mocked here, so the test replays it. */
function signInThroughBanner(queryClient: QueryClient) {
  return queryClient.invalidateQueries({
    predicate: (q) => q.state.status === 'error' && q.state.data === undefined,
  })
}

const SLUG = 'pearce-crt'
const catalog = { themes: [{ slug: SLUG, name: 'Pearce CRT', emoji: '📺', source: 'installed' }] }
const detail = {
  slug: SLUG,
  name: 'Pearce CRT',
  emoji: '📺',
  dark: { '--bg': '#0b0f0c' },
  light: { '--bg': '#f2f5f0' },
  level: 1,
  assets: { branding: { botName: 'CRT' }, hasOverrides: false },
}

/** What `checkSessionExpired` does on a 403: start the silent refresh in the
 *  background and let the ORIGINAL request reject through `j`. */
function authDeniedWithRefreshStarted(): Promise<never> {
  void refreshOnce()
  return Promise.reject(new ApiError(403, 'Session expired', 'Token required', true))
}

/** POST /api/auth/refresh answers `status` immediately -- after a gateway
 *  restart the session table is gone, so the silent refresh is a terminal 401. */
function installRefreshFetch(status: number) {
  const fetchFn = vi.fn(async (input: RequestInfo | URL) => {
    const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
    if (!url.includes('/api/auth/refresh')) throw new Error(`unexpected fetch ${url}`)
    return new Response(JSON.stringify({}), { status })
  })
  vi.stubGlobal('fetch', fetchFn)
  return fetchFn
}

function styleTagCount(): number {
  return document.head.querySelectorAll(`style#mc-custom-theme-${SLUG}`).length
}

describe('useTheme: installed-theme catalog after token sign-in and boot races', () => {
  beforeEach(() => {
    localStorage.clear()
    document.head.querySelectorAll(`style#mc-custom-theme-${SLUG}`).forEach((n) => n.remove())
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
    vi.useRealTimers()
    vi.unstubAllGlobals()
    __resetRefreshOnceForTests()
  })

  describe('token sign-in path', () => {
    it('loads the catalog once the banner sign-in refetches errored queries', async () => {
      const fetchFn = installRefreshFetch(401)
      themesFn.mockImplementationOnce(authDeniedWithRefreshStarted).mockResolvedValue(catalog)
      const { queryClient, wrapper } = makeWrapper()

      const { result } = renderHook(() => useTheme(), { wrapper })
      await waitFor(() => expect(result.current.colorTheme).toBe(`custom-${SLUG}`))
      await waitFor(() => expect(fetchFn).toHaveBeenCalledTimes(1))
      await new Promise((r) => setTimeout(r, 30))

      // The refresh was terminal: the query sits in error with no data, the
      // banner owns recovery, and nothing retries an auth denial on a timer.
      expect(themesFn).toHaveBeenCalledTimes(1)
      expect(result.current.customThemes).toEqual([])

      await act(() => signInThroughBanner(queryClient))

      await waitFor(() => expect(themesFn).toHaveBeenCalledTimes(2))
      await waitFor(() => expect(result.current.customThemes).toHaveLength(1))
      await waitFor(() => expect(result.current.brandName).toBe('CRT'))
      expect(result.current.customThemeDataMap.get(SLUG)).toEqual(detail)
      expect(styleTagCount()).toBe(1)
    })

    it('a second auth denial on the replay is not retried on a timer either', async () => {
      installRefreshFetch(200)
      themesFn
        .mockImplementationOnce(authDeniedWithRefreshStarted)
        .mockImplementationOnce(authDeniedWithRefreshStarted)
        .mockResolvedValue(catalog)
      const { wrapper } = makeWrapper()

      const { result } = renderHook(() => useTheme(), { wrapper })
      await waitFor(() => expect(themesFn).toHaveBeenCalledTimes(2))
      await new Promise((r) => setTimeout(r, 50))

      expect(themesFn).toHaveBeenCalledTimes(2)
      expect(result.current.customThemes).toEqual([])
    })

    it('the sign-in refetch leaves a loaded catalog alone', async () => {
      installRefreshFetch(401)
      themesFn.mockResolvedValue(catalog)
      const { queryClient, wrapper } = makeWrapper()

      const { result } = renderHook(() => useTheme(), { wrapper })
      await waitFor(() => expect(result.current.customThemes).toHaveLength(1))
      expect(themesFn).toHaveBeenCalledTimes(1)

      await act(() => signInThroughBanner(queryClient))
      await new Promise((r) => setTimeout(r, 30))

      expect(themesFn).toHaveBeenCalledTimes(1)
      expect(styleTagCount()).toBe(1)
    })
  })

  describe('gateway-still-booting path', () => {
    // Only setTimeout/clearTimeout are faked, so React's scheduler keeps its
    // real clock and only React Query's retry delay is under test control.
    // RTL's polling helper stalls under a faked setTimeout, so these tests
    // flush explicitly. Expected cadence: 1 s, 2 s, 4 s, 8 s, 16 s, then 30 s.
    beforeEach(() => {
      vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    })

    /** Advance the faked clock by `ms` and drain the promise chain behind it. */
    const advance = (ms: number) => act(() => vi.advanceTimersByTimeAsync(ms))
    /** Drain settled promises without moving the clock. */
    const flush = () => advance(0)
    /** Let a resolved query reach its observers: React Query batches the
     *  notification through a zero-delay timer, which is faked here. */
    const settle = async () => { await advance(1); await advance(1) }

    it('retries a network failure on the 1 s / 2 s backoff and loads on the second retry', async () => {
      const fetchFn = installRefreshFetch(401)
      themesFn
        .mockRejectedValueOnce(new TypeError('Failed to fetch'))
        .mockRejectedValueOnce(new TypeError('Failed to fetch'))
        .mockResolvedValue(catalog)
      const { wrapper } = makeWrapper()

      const { result } = renderHook(() => useTheme(), { wrapper })
      await flush()
      expect(themesFn).toHaveBeenCalledTimes(1)

      // Just short of the first step: nothing yet.
      await advance(999)
      expect(themesFn).toHaveBeenCalledTimes(1)
      await advance(1)
      expect(themesFn).toHaveBeenCalledTimes(2)

      // Second step doubles.
      await advance(1999)
      expect(themesFn).toHaveBeenCalledTimes(2)
      await advance(1)
      expect(themesFn).toHaveBeenCalledTimes(3)

      await settle()
      expect(result.current.customThemes).toHaveLength(1)
      expect(result.current.customThemeDataMap.get(SLUG)).toEqual(detail)
      // A network failure is not an auth denial: the refresh path stays untouched.
      expect(fetchFn).not.toHaveBeenCalled()
    })

    it('walks every backoff step, then keeps the 30 s cadence until the fetch succeeds', async () => {
      installRefreshFetch(401)
      themesFn.mockRejectedValue(new TypeError('Failed to fetch'))
      const { wrapper } = makeWrapper()

      const { result } = renderHook(() => useTheme(), { wrapper })
      await flush()
      expect(themesFn).toHaveBeenCalledTimes(1)

      for (const [i, delay] of [1000, 2000, 4000, 8000, 16000].entries()) {
        await advance(delay)
        expect(themesFn).toHaveBeenCalledTimes(i + 2)
      }
      const afterSteps = 6

      // Capped: one call per 30 s, no faster, no unhandled rejection.
      await advance(29_999)
      expect(themesFn).toHaveBeenCalledTimes(afterSteps)
      await advance(1)
      expect(themesFn).toHaveBeenCalledTimes(afterSteps + 1)
      await advance(30_000)
      expect(themesFn).toHaveBeenCalledTimes(afterSteps + 2)
      expect(result.current.customThemes).toEqual([])

      // The gateway comes up: the next tick loads the catalog and the loop ends.
      themesFn.mockResolvedValue(catalog)
      await advance(30_000)
      expect(themesFn).toHaveBeenCalledTimes(afterSteps + 3)
      await settle()
      expect(result.current.customThemes).toHaveLength(1)
      await advance(300_000)
      expect(themesFn).toHaveBeenCalledTimes(afterSteps + 3)
    })

    it('exposes the boot failure while retrying, clears it on success, and hides an auth denial', async () => {
      const fetchFn = installRefreshFetch(401)
      themesFn
        .mockRejectedValueOnce(new TypeError('Failed to fetch'))
        .mockResolvedValue(catalog)
      const { wrapper } = makeWrapper()

      const { result } = renderHook(() => useTheme(), { wrapper })
      await settle()
      // Still retrying, but the failure is already reportable (the Display
      // panel renders it through ErrorNotice) rather than an invisible loop.
      expect(result.current.customThemesLoadError).toBeInstanceOf(TypeError)
      expect(result.current.customThemes).toEqual([])

      await advance(1000)
      await settle()
      expect(result.current.customThemes).toHaveLength(1)
      expect(result.current.customThemesLoadError).toBeNull()

      // An auth denial is the banner's to report, not this surface's.
      themesFn.mockReset()
      themesFn.mockImplementationOnce(authDeniedWithRefreshStarted).mockResolvedValue(catalog)
      const second = renderHook(() => useTheme(), { wrapper: makeWrapper().wrapper })
      await settle()
      await settle()
      expect(fetchFn).toHaveBeenCalled()
      expect(second.result.current.customThemesLoadError).toBeNull()
    })

    it('a retry that answers 403 hands over to the auth path, which does not retry on a timer', async () => {
      const fetchFn = installRefreshFetch(401)
      themesFn
        .mockRejectedValueOnce(new TypeError('Failed to fetch'))
        .mockImplementationOnce(authDeniedWithRefreshStarted)
        .mockResolvedValue(catalog)
      const { wrapper } = makeWrapper()

      renderHook(() => useTheme(), { wrapper })
      await flush()
      expect(themesFn).toHaveBeenCalledTimes(1)

      await advance(1000)
      expect(themesFn).toHaveBeenCalledTimes(2)
      await flush()
      expect(fetchFn).toHaveBeenCalledTimes(1)

      // The auth denial owns this failure: no backoff retry stacks on top of it.
      await advance(120_000)
      expect(themesFn).toHaveBeenCalledTimes(2)
    })

    it('mc-custom-themes-changed during a pending retry replaces the chain, never adds a second', async () => {
      installRefreshFetch(401)
      themesFn.mockRejectedValue(new TypeError('Failed to fetch'))
      const { wrapper } = makeWrapper()

      renderHook(() => useTheme(), { wrapper })
      await flush()
      expect(themesFn).toHaveBeenCalledTimes(1)

      // A reinstall has newer assets than the pending step would fetch, so the
      // pending chain is cancelled and one fresh fetch starts at once; the
      // cancelled step never fires, and the fresh chain retries on its own
      // backoff from step one.
      act(() => { window.dispatchEvent(new Event('mc-custom-themes-changed')) })
      await flush()
      expect(themesFn).toHaveBeenCalledTimes(2)
      await advance(999)
      expect(themesFn).toHaveBeenCalledTimes(2)
      await advance(1)
      expect(themesFn).toHaveBeenCalledTimes(3)
      await advance(2000)
      expect(themesFn).toHaveBeenCalledTimes(4)
    })

    it('stops retrying once the provider unmounts, even mid-request', async () => {
      installRefreshFetch(401)
      let reject!: (e: unknown) => void
      themesFn
        .mockImplementationOnce(() => new Promise((_, rej) => { reject = rej }))
        .mockResolvedValue(catalog)
      const { wrapper } = makeWrapper()

      const { unmount } = renderHook(() => useTheme(), { wrapper })
      await flush()
      expect(themesFn).toHaveBeenCalledTimes(1)

      // Cleanup runs while /api/themes is still pending; it rejects afterwards.
      unmount()
      reject(new TypeError('Failed to fetch'))
      await flush()
      await advance(120_000)
      expect(themesFn).toHaveBeenCalledTimes(1)
    })

    it('the context loadCustomThemes fails fast -- resolves false, never hangs or rejects -- and hands the failure to the query', async () => {
      installRefreshFetch(401)
      themesFn.mockResolvedValueOnce(catalog).mockRejectedValue(new TypeError('Failed to fetch'))
      const { wrapper } = makeWrapper()

      const { result } = renderHook(() => useTheme(), { wrapper })
      await settle()
      expect(result.current.customThemes).toHaveLength(1)

      // A save/install handler awaiting this gets an answer at once: the
      // catalog could not be refreshed, so it must not select against it.
      let outcome: boolean | 'pending' | 'rejected' = 'pending'
      await act(async () => {
        await result.current.loadCustomThemes().then((v) => { outcome = v }, () => { outcome = 'rejected' })
      })
      expect(outcome).toBe(false)
      // The loaded catalog survives the failed refetch ...
      expect(result.current.customThemes).toHaveLength(1)
      // ... and the query is handed the failure: with a catalog already loaded
      // it follows the app-wide policy (one retry), not the boot-time loop.
      await settle()
      const resumed = themesFn.mock.calls.length
      expect(resumed).toBeGreaterThanOrEqual(3)
      await advance(120_000)
      const afterRetry = themesFn.mock.calls.length
      expect(afterRetry).toBeGreaterThan(resumed)
      await advance(120_000)
      expect(themesFn).toHaveBeenCalledTimes(afterRetry)
      expect(result.current.customThemes).toHaveLength(1)
      // Once that retry has settled, the stale list is reported, not hidden.
      await settle()
      expect(result.current.customThemesLoadError).toBeInstanceOf(TypeError)
    })

    it('the context loadCustomThemes resolves true once the catalog reflects the server', async () => {
      installRefreshFetch(401)
      const added = { ...catalog, themes: [...catalog.themes, { slug: 'other', name: 'Other', emoji: '✨', source: 'installed' }] }
      themesFn.mockResolvedValueOnce(catalog).mockResolvedValue(added)
      const { wrapper } = makeWrapper()

      const { result } = renderHook(() => useTheme(), { wrapper })
      await settle()
      expect(result.current.customThemes).toHaveLength(1)

      let outcome: boolean | null = null
      await act(async () => { outcome = await result.current.loadCustomThemes() })
      await settle()
      expect(outcome).toBe(true)
      expect(result.current.customThemes).toHaveLength(2)
    })

    it('a detail that fails commits the catalog without it; the listed selection is kept and flagged', async () => {
      installRefreshFetch(401)
      themesFn.mockResolvedValue(catalog)
      themeDetailFn.mockRejectedValue(new ApiError(503, 'unavailable'))
      const { wrapper } = makeWrapper()

      const { result } = renderHook(() => useTheme(), { wrapper })
      await settle()
      await settle()
      // The catalog row proves the pack is installed: self-repair reads the
      // LISTING, so the missing detail is never read as an uninstall, and the
      // unstyled selection is reported through the derived flag instead of
      // holding every other theme in a retry loop.
      expect(result.current.customThemes).toHaveLength(1)
      expect(result.current.customThemeDataMap.has(SLUG)).toBe(false)
      expect(result.current.colorTheme).toBe(`custom-${SLUG}`)
      expect(localStorage.getItem('mc-color-theme')).toBe(`custom-${SLUG}`)
      expect(result.current.installedThemeLoadFailed).toBe(true)
      expect(result.current.customThemesLoadError).toBeNull()
      await advance(120_000)
      expect(themesFn).toHaveBeenCalledTimes(1)
    })

    it('an auth denial on a detail replays the whole fetch after the refresh, never commits a gap', async () => {
      const fetchFn = installRefreshFetch(200)
      themesFn.mockResolvedValue(catalog)
      themeDetailFn
        .mockImplementationOnce(() => authDeniedWithRefreshStarted())
        .mockResolvedValue(detail)
      const { wrapper } = makeWrapper()

      const { result } = renderHook(() => useTheme(), { wrapper })
      await settle()
      await settle()
      expect(fetchFn).toHaveBeenCalledTimes(1)
      expect(themesFn).toHaveBeenCalledTimes(2)
      expect(result.current.customThemes).toHaveLength(1)
      expect(result.current.customThemeDataMap.get(SLUG)).toEqual(detail)
      expect(result.current.colorTheme).toBe(`custom-${SLUG}`)
    })

    it('an auth denial on a detail whose refresh fails settles the query in error, not a partial catalog', async () => {
      installRefreshFetch(401)
      themesFn.mockResolvedValue(catalog)
      themeDetailFn.mockImplementationOnce(() => authDeniedWithRefreshStarted()).mockResolvedValue(detail)
      const { wrapper } = makeWrapper()

      const { result } = renderHook(() => useTheme(), { wrapper })
      await settle()
      await settle()
      expect(themesFn).toHaveBeenCalledTimes(1)
      expect(result.current.customThemes).toEqual([])
      expect(result.current.colorTheme).toBe(`custom-${SLUG}`)
      // Auth is the banner's to report, not the Display panel notice's.
      expect(result.current.customThemesLoadError).toBeNull()
      await advance(120_000)
      expect(themesFn).toHaveBeenCalledTimes(1)
    })

    it('a 403 whose silent refresh fails TRANSIENTLY keeps retrying and is reported, not treated as a denial', async () => {
      // Gateway restart: the listing 403s, the refresh POST answers 503 (not a
      // 401 verdict on the session). The original auth error must not be what
      // the query settles on -- the retry predicate refuses auth errors and the
      // notice hides them, which would leave the theme unstyled with no
      // recovery path (the banner latches only on 401). Instead the fetch fails
      // as a plain retryable error, the boot loop retries, and the next attempt
      // succeeds.
      const fetchFn = installRefreshFetch(503)
      themesFn.mockImplementationOnce(authDeniedWithRefreshStarted).mockResolvedValue(catalog)
      themeDetailFn.mockResolvedValue(detail)
      const { wrapper } = makeWrapper()

      const { result } = renderHook(() => useTheme(), { wrapper })
      await settle()
      await settle()
      expect(fetchFn).toHaveBeenCalledTimes(1)
      expect(result.current.customThemesLoaded).toBe(false)
      // Reported while the retry runs (a transient failure, not an auth denial).
      expect(result.current.customThemesLoadError).toBeInstanceOf(ApiError)
      expect((result.current.customThemesLoadError as ApiError).authRequired).toBe(false)
      await advance(1_000)
      await settle()
      await settle()
      expect(themesFn.mock.calls.length).toBeGreaterThanOrEqual(2)
      expect(result.current.customThemes).toHaveLength(1)
      expect(result.current.customThemesLoadError).toBeNull()
    })

    it('a detail 403 whose silent refresh fails transiently retries the whole fetch too', async () => {
      installRefreshFetch(503)
      themesFn.mockResolvedValue(catalog)
      themeDetailFn.mockImplementationOnce(() => authDeniedWithRefreshStarted()).mockResolvedValue(detail)
      const { wrapper } = makeWrapper()

      const { result } = renderHook(() => useTheme(), { wrapper })
      await settle()
      await settle()
      expect(result.current.customThemesLoaded).toBe(false)
      expect(result.current.customThemesLoadError).toBeInstanceOf(ApiError)
      await advance(1_000)
      await settle()
      await settle()
      expect(themesFn.mock.calls.length).toBeGreaterThanOrEqual(2)
      expect(result.current.customThemeDataMap.has(SLUG)).toBe(true)
      expect(result.current.customThemesLoadError).toBeNull()
    })

    it('the exposed detail map agrees with the query cache after every mutation path', async () => {
      // `customThemeDataMap` is a `useState` mirror of the cache snapshot's
      // `dataMap` (kept so the render cache and the active pack's early detail
      // can paint ahead of the catalog). Every path that writes one must write
      // the other; this pins that agreement across boot, an imperative refresh,
      // a create whose refresh failed (the seed writes both), and a delete.
      installRefreshFetch(401)
      const OTHER = 'other-pack'
      const otherDetail = { ...detail, slug: OTHER, name: 'Other' }
      const bySlug: Record<string, typeof detail> = { [SLUG]: detail, [OTHER]: otherDetail }
      themeDetailFn.mockImplementation((slug: string) =>
        bySlug[slug] ? Promise.resolve(bySlug[slug]) : Promise.reject(new ApiError(404, 'gone')))
      themesFn.mockResolvedValue(catalog)
      const { wrapper, queryClient } = makeWrapper()
      const { result } = renderHook(() => useTheme(), { wrapper })
      const cacheSlugs = () =>
        [...((queryClient.getQueryData(['custom-themes-catalog']) as { dataMap: Map<string, unknown> }).dataMap.keys())].sort()
      const stateSlugs = () => [...result.current.customThemeDataMap.keys()].sort()

      // Boot.
      await settle(); await settle()
      expect(result.current.customThemesLoaded).toBe(true)
      expect(stateSlugs()).toEqual(cacheSlugs())
      expect(stateSlugs()).toEqual([SLUG])

      // Imperative refresh with a second pack listed.
      themesFn.mockResolvedValue({ themes: [...catalog.themes, { slug: OTHER, name: 'Other', emoji: '🎛️', source: 'installed' }] })
      await act(async () => { await result.current.loadCustomThemes() })
      await settle()
      expect(stateSlugs()).toEqual(cacheSlugs())
      expect(stateSlugs()).toEqual([OTHER, SLUG].sort())

      // Create whose follow-up refresh fails: the seed must land in BOTH copies.
      const created = { ...detail, slug: 'fresh', name: 'Fresh' }
      createThemeFn.mockResolvedValue({ ok: true, theme: created })
      // Persistent, so the query's own retry cannot replace the seed before the
      // agreement is checked.
      themesFn.mockRejectedValue(new TypeError('Failed to fetch'))
      await act(async () => { await result.current.addCustomTheme({ name: 'Fresh', emoji: '✨', dark: {}, light: {} } as never) })
      await settle()
      expect(stateSlugs()).toEqual(cacheSlugs())
      expect(stateSlugs()).toContain('fresh')

      // Delete: the pack leaves both copies once the refresh lands.
      deleteThemeFn.mockResolvedValue({ ok: true })
      themesFn.mockResolvedValue(catalog)
      await act(async () => { await result.current.deleteCustomTheme(OTHER) })
      await settle(); await settle()
      expect(stateSlugs()).toEqual(cacheSlugs())
      expect(stateSlugs()).not.toContain(OTHER)
    })

    it("one pack's own broken detail is skipped without holding the other themes back", async () => {
      installRefreshFetch(401)
      const two = { themes: [...catalog.themes, { slug: 'broken', name: 'Broken', emoji: '💥', source: 'installed' }] }
      themesFn.mockResolvedValue(two)
      themeDetailFn.mockImplementation((slug: string) =>
        slug === 'broken' ? Promise.reject(new ApiError(500, 'failed to read theme')) : Promise.resolve(detail))
      const { wrapper } = makeWrapper()

      const { result } = renderHook(() => useTheme(), { wrapper })
      await settle()
      expect(result.current.customThemes).toHaveLength(2)
      expect(result.current.customThemeDataMap.has(SLUG)).toBe(true)
      expect(result.current.customThemeDataMap.has('broken')).toBe(false)
      await advance(120_000)
      expect(themesFn).toHaveBeenCalledTimes(1)
    })

    it("one pack's data that throws while its CSS is built stays listed, out of the map, unstyled", async () => {
      installRefreshFetch(401)
      const two = { themes: [...catalog.themes, { slug: 'bad-css', name: 'Bad', emoji: '💥', source: 'installed' }] }
      themesFn.mockResolvedValue(two)
      // A detail body the injector cannot consume at all: it throws on it.
      themeDetailFn.mockImplementation((slug: string) =>
        Promise.resolve(slug === 'bad-css' ? { slug: 'bad-css', name: 'Bad', emoji: '💥', dark: null, light: null } : detail))
      const { wrapper } = makeWrapper()

      const { result } = renderHook(() => useTheme(), { wrapper })
      await settle()
      expect(result.current.customThemes).toHaveLength(2)
      expect(result.current.customThemeDataMap.has(SLUG)).toBe(true)
      // Left out of the map so the derived flag can report it, but still
      // listed: self-repair reads the listing and must not persist a reset.
      // It is not the active theme, so nothing is reported for it now.
      expect(result.current.customThemeDataMap.has('bad-css')).toBe(false)
      expect(document.head.querySelector('style#mc-custom-theme-bad-css')).toBeNull()
      expect(result.current.installedThemeLoadFailed).toBe(false)
      act(() => result.current.setColorTheme('custom-bad-css'))
      expect(result.current.installedThemeLoadFailed).toBe(true)
      expect(result.current.colorTheme).toBe('custom-bad-css')
      await advance(120_000)
      expect(themesFn).toHaveBeenCalledTimes(1)
    })

    it('the selected pack keeps its selection when only its own detail fails (500)', async () => {
      installRefreshFetch(401)
      themesFn.mockResolvedValue(catalog)
      themeDetailFn.mockRejectedValue(new ApiError(500, 'failed to read theme'))
      const { wrapper } = makeWrapper()

      const { result } = renderHook(() => useTheme(), { wrapper })
      await settle()
      await settle()
      // Listed by /api/themes, so installed: unstyled, but never reset to the default.
      expect(result.current.customThemes).toHaveLength(1)
      expect(result.current.customThemeDataMap.has(SLUG)).toBe(false)
      expect(result.current.colorTheme).toBe(`custom-${SLUG}`)
      expect(localStorage.getItem('mc-color-theme')).not.toBe('kiro')
      // ... and the failed state is reportable: the active pack is flagged.
      expect(result.current.installedThemeLoadFailed).toBe(true)
      expect(result.current.customThemesLoadError).toBeNull()
    })

    it('a persisted pack that /api/themes no longer lists is still repaired to the default', async () => {
      installRefreshFetch(401)
      themesFn.mockResolvedValue({ themes: [] })
      const { wrapper } = makeWrapper()

      const { result } = renderHook(() => useTheme(), { wrapper })
      await settle()
      await settle()
      expect(result.current.colorTheme).toBe('kiro')
    })

    it('exposes whether a catalog has loaded, so a surface can word a failure correctly', async () => {
      installRefreshFetch(401)
      themesFn.mockRejectedValueOnce(new TypeError('Failed to fetch')).mockResolvedValue(catalog)
      const { wrapper } = makeWrapper()

      const { result } = renderHook(() => useTheme(), { wrapper })
      await settle()
      expect(result.current.customThemesLoaded).toBe(false)
      await advance(1000)
      await settle()
      expect(result.current.customThemesLoaded).toBe(true)
    })

    it('a superseded fetch injects nothing when its details land late', async () => {
      installRefreshFetch(401)
      let resolveDetail!: (d: unknown) => void
      themesFn.mockResolvedValue(catalog)
      themeDetailFn
        .mockImplementationOnce(() => new Promise((res) => { resolveDetail = res }))
        .mockImplementationOnce(() => new Promise((res) => { resolveDetail = res }))
      const { wrapper } = makeWrapper()

      const { unmount } = renderHook(() => useTheme(), { wrapper })
      await settle()
      expect(themeDetailFn).toHaveBeenCalled()
      // The provider is gone before the detail answers: nothing may restyle
      // the page from a fetch nobody owns any more.
      unmount()
      resolveDetail(detail)
      await settle()
      expect(styleTagCount()).toBe(0)
    })

    it('mc-auth-recovered refetches a catalog that holds data but ended in an auth error', async () => {
      installRefreshFetch(401)
      themesFn.mockResolvedValueOnce(catalog)
      const { wrapper } = makeWrapper()

      const { result } = renderHook(() => useTheme(), { wrapper })
      await settle()
      expect(result.current.customThemes).toHaveLength(1)

      // A refetch (change event) meets a terminal auth denial: data kept, query errored.
      themesFn.mockImplementationOnce(authDeniedWithRefreshStarted)
      act(() => { window.dispatchEvent(new Event('mc-custom-themes-changed')) })
      await settle()
      await settle()
      expect(themesFn).toHaveBeenCalledTimes(2)

      // The banner's token paste only refetches DATA-LESS errored queries; this
      // listener covers the data-bearing one.
      themesFn.mockResolvedValue({ themes: [] })
      act(() => { window.dispatchEvent(new CustomEvent('mc-auth-recovered')) })
      await settle()
      await settle()
      expect(themesFn).toHaveBeenCalledTimes(3)
      expect(result.current.customThemes).toEqual([])
    })

    it('serializes overlapping context refreshes so an older fetch cannot land last', async () => {
      installRefreshFetch(401)
      let resolveFirst!: (v: unknown) => void
      themesFn
        .mockResolvedValueOnce(catalog)
        .mockImplementationOnce(() => new Promise((res) => { resolveFirst = res }))
        .mockResolvedValue({ themes: [] })
      const { wrapper } = makeWrapper()

      const { result } = renderHook(() => useTheme(), { wrapper })
      await settle()
      expect(result.current.customThemes).toHaveLength(1)

      // Refresh 1 (slow, pre-delete list) then refresh 2 (post-delete, empty).
      let p1!: Promise<boolean>, p2!: Promise<boolean>
      act(() => { p1 = result.current.loadCustomThemes(); p2 = result.current.loadCustomThemes() })
      await flush()
      expect(themesFn).toHaveBeenCalledTimes(2) // refresh 2 waits for refresh 1
      resolveFirst(catalog)
      await act(async () => { await p1; await p2 })
      await settle()
      expect(themesFn).toHaveBeenCalledTimes(3)
      expect(result.current.customThemes).toEqual([])
    })

    it('a stale boot response cannot overwrite the catalog a mutation just refetched', async () => {
      installRefreshFetch(401)
      const added = { ...catalog, themes: [...catalog.themes, { slug: 'other', name: 'Other', emoji: '✨', source: 'installed' }] }
      let resolveBoot!: (v: unknown) => void
      themesFn
        .mockImplementationOnce(() => new Promise((res) => { resolveBoot = res }))
        .mockResolvedValue(added)
      const { wrapper } = makeWrapper()

      const { result } = renderHook(() => useTheme(), { wrapper })
      await flush()
      expect(themesFn).toHaveBeenCalledTimes(1)

      // The editor installs a theme while the boot request is still pending.
      await act(async () => { await result.current.loadCustomThemes() })
      await settle()
      expect(result.current.customThemes).toHaveLength(2)

      // The slow boot request finally answers with the pre-install list. Its
      // detail responses arrive after the cancel and must not restyle the page.
      themeDetailFn.mockClear()
      document.head.querySelectorAll(`style#mc-custom-theme-${SLUG}`).forEach((n) => n.remove())
      resolveBoot(catalog)
      await settle()
      expect(result.current.customThemes).toHaveLength(2)
      expect(styleTagCount()).toBe(0)
    })
  })
})
