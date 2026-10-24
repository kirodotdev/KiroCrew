import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { act, fireEvent, renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import { renderHookWithProviders } from './helpers'
import { api } from '../api/client'
import { useBrowserCookies } from '../hooks/useBrowserCookies'

/**
 * `useBrowserCookies` against the REAL api client, with only `fetch` stubbed —
 * the same shape as `useBrowserView.test.tsx`. The behaviour worth pinning is
 * end-to-end: the 404 → empty and 403 → hidden mappings depend on the client
 * throwing an `ApiError` carrying a status, so mocking the client away would
 * test the mapping against a fixture rather than the thing it must survive.
 */

/** Answer one request per (path, method) with a real Response. */
function stubFetch(routes: Record<string, { status: number; body?: unknown }>) {
  const calls: { url: string; method: string }[] = []
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    const method = (init?.method ?? 'GET').toUpperCase()
    calls.push({ url, method })
    const route = routes[`${method} ${url}`] ?? routes[url] ?? { status: 404, body: 'no such route' }
    const isJson = typeof route.body === 'object' && route.body !== null
    return new Response(isJson ? JSON.stringify(route.body) : String(route.body ?? ''), {
      status: route.status,
      headers: isJson ? { 'Content-Type': 'application/json' } : undefined,
    })
  }))
  return calls
}

const SUMMARY = {
  cookie_count: 34,
  domains: ['example.com', 'api.example.com'],
  earliest_expiry: 1_800_000_000,
  imported_at: 1_700_000_000,
}
const PRESENT = { present: true, summary: SUMMARY }
const ABSENT = { present: false, summary: null }
const IMPORT_OK = {
  ok: true,
  summary: SUMMARY,
  hot_load: { loaded: ['kc-abc'], failed: {} },
}

describe('useBrowserCookies', () => {
  beforeEach(() => { vi.unstubAllGlobals() })
  afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks() })

  it('reports a present cookie set verbatim', async () => {
    stubFetch({ '/api/browser/cookies': { status: 200, body: PRESENT } })
    const { result } = renderHookWithProviders(() => useBrowserCookies(true))
    await waitFor(() => expect(result.current.data).toEqual(PRESENT))
    expect(result.current.forbidden).toBe(false)
    expect(result.current.error).toBeNull()
  })

  it.each([
    ['import', ABSENT, PRESENT],
    ['clear', PRESENT, ABSENT],
  ])('refetches on window focus after an external %s despite an infinitely fresh cache', async (_operation, initial, updated) => {
    const routes = { '/api/browser/cookies': { status: 200, body: initial } }
    const calls = stubFetch(routes)
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false, staleTime: Infinity } },
    })
    const { result, unmount } = renderHook(() => useBrowserCookies(true, 'slot-focus'), {
      wrapper: ({ children }) => <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>,
    })
    try {
      await waitFor(() => expect(result.current.data).toEqual(initial))
      expect(calls).toHaveLength(1)
      // Another dashboard window changed the stored set, without invalidating
      // this window's infinitely fresh cache. Returning here must refresh it.
      routes['/api/browser/cookies'].body = updated
      const visibility = vi.spyOn(document, 'visibilityState', 'get')
      visibility.mockReturnValue('hidden')
      fireEvent(window, new Event('visibilitychange'))
      visibility.mockReturnValue('visible')
      fireEvent(window, new Event('visibilitychange'))
      await waitFor(() => expect(result.current.data).toEqual(updated))
      expect(calls).toHaveLength(2)
      expect(calls.every((call) => call.method === 'GET')).toBe(true)
    } finally {
      unmount()
      queryClient.clear()
    }
  })

  it('degrades a MISSING route to the empty "no cookies" state', async () => {
    stubFetch({ '/api/browser/cookies': { status: 404, body: 'not found' } })
    const { result } = renderHookWithProviders(() => useBrowserCookies(true))
    await waitFor(() => expect(result.current.data?.present).toBe(false))
    expect(result.current.data?.summary).toBeNull()
    expect(result.current.error).toBeNull()
    expect(result.current.forbidden).toBe(false)
  })

  it('flags a 403 as forbidden (control hidden) rather than an error', async () => {
    stubFetch({ '/api/browser/cookies': { status: 403, body: 'not owner' } })
    const { result } = renderHookWithProviders(() => useBrowserCookies(true))
    await waitFor(() => expect(result.current.forbidden).toBe(true))
    // A forbidden read is not surfaced as an error — the caller hides the control.
    expect(result.current.error).toBeNull()
  })

  it('sends the active slot key as X-Session-Key on every request', async () => {
    const headers: { url: string; method: string; sk: string | null }[] = []
    vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
      const method = (init?.method ?? 'GET').toUpperCase()
      const h = new Headers(init?.headers)
      headers.push({ url, method, sk: h.get('X-Session-Key') })
      const body = method === 'GET' ? ABSENT : method === 'POST' ? IMPORT_OK : { ok: true, present: false }
      return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } })
    }))
    const { result } = renderHookWithProviders(() => useBrowserCookies(true, 'dashboard:slot-7'))
    await waitFor(() => expect(result.current.data?.present).toBe(false))
    await result.current.importCookies('{"cookies":[]}')
    await result.current.clear()
    const cookieCalls = headers.filter((c) => c.url === '/api/browser/cookies')
    expect(cookieCalls.map((c) => c.method)).toEqual(['GET', 'POST', 'DELETE'])
    // The restricted-session guard reads this header; the shared `dashboard:ui`
    // placeholder would answer "not restricted" for an incognito slot.
    expect(cookieCalls.every((c) => c.sk === 'dashboard:slot-7')).toBe(true)
  })

  it('falls back to the shared dashboard:ui key when no slot is given', async () => {
    const seen: (string | null)[] = []
    vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
      if (url === '/api/browser/cookies') seen.push(new Headers(init?.headers).get('X-Session-Key'))
      return new Response(JSON.stringify(ABSENT), { status: 200, headers: { 'Content-Type': 'application/json' } })
    }))
    const { result } = renderHookWithProviders(() => useBrowserCookies(true))
    await waitFor(() => expect(result.current.data?.present).toBe(false))
    expect(seen).toEqual(['dashboard:ui'])
  })

  it('does not read the status at all when disabled', async () => {
    const calls = stubFetch({ '/api/browser/cookies': { status: 200, body: PRESENT } })
    renderHookWithProviders(() => useBrowserCookies(false))
    // A disabled query never schedules a fetch; flush pending effects and microtasks.
    await act(async () => {})
    expect(calls.filter((c) => c.url.startsWith('/api/browser/cookies'))).toEqual([])
  })

  it('writes the import result straight into the cache (present + fresh summary)', async () => {
    stubFetch({
      'GET /api/browser/cookies': { status: 200, body: ABSENT },
      'POST /api/browser/cookies': { status: 200, body: IMPORT_OK },
    })
    const { result } = renderHookWithProviders(() => useBrowserCookies(true))
    await waitFor(() => expect(result.current.data?.present).toBe(false))
    const res = await result.current.importCookies('{"cookies":[]}')
    expect(res.hot_load.loaded).toEqual(['kc-abc'])
    await waitFor(() => expect(result.current.data?.present).toBe(true))
    expect(result.current.data?.summary).toEqual(SUMMARY)
  })

  /** Stub fetch so the status GET is held until the test releases it with a
   * body, while POST/DELETE answer at once.
   *
   * `deliver` releases the held GET and resolves only once the late answer has
   * gone all the way through: the api client's read has settled (the spy holds
   * its promise, so this waits on completion, not on elapsed time), and every
   * React Query write and observer notification that answer could schedule has
   * been drained by advancing fake timers until none is left. */
  function stubHeldStatusRead(mutationBody: Record<string, unknown>) {
    let release: (body: unknown) => void = () => {}
    const held = new Promise<unknown>((resolve) => { release = resolve })
    vi.stubGlobal('fetch', vi.fn(async (_url: string, init?: RequestInit) => {
      const method = (init?.method ?? 'GET').toUpperCase()
      const body = method === 'GET' ? await held : mutationBody[method]
      return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } })
    }))
    const reads: Promise<unknown>[] = []
    const original = api.getBrowserCookies
    vi.spyOn(api, 'getBrowserCookies').mockImplementation((sessionKey?: string) => {
      const read = original(sessionKey)
      reads.push(read)
      return read
    })
    return async (body: unknown) => {
      vi.useFakeTimers()
      try {
        await act(async () => {
          release(body)
          await Promise.allSettled(reads)
          await vi.runAllTimersAsync()
        })
      } finally {
        vi.useRealTimers()
      }
    }
  }

  it('a status read answered after an import does not overwrite the import', async () => {
    const deliverRead = stubHeldStatusRead({ POST: IMPORT_OK })
    const { result } = renderHookWithProviders(() => useBrowserCookies(true))
    // The first read is still in flight when the owner imports.
    await act(async () => { await result.current.importCookies('{"cookies":[]}') })
    await waitFor(() => expect(result.current.data?.present).toBe(true))
    // The read made BEFORE the import now answers with the old, empty set.
    await deliverRead(ABSENT)
    expect(api.getBrowserCookies).toHaveBeenCalledTimes(1)
    expect(result.current.data).toEqual({ present: true, summary: SUMMARY })
  })

  it('a status read answered after a clear does not bring the cleared set back', async () => {
    const deliverRead = stubHeldStatusRead({
      DELETE: { ok: true, present: false, live: { cleared: [], failed: {} } },
    })
    const { result } = renderHookWithProviders(() => useBrowserCookies(true))
    await act(async () => { await result.current.clear() })
    await waitFor(() => expect(result.current.data).toEqual(ABSENT))
    await deliverRead(PRESENT)
    expect(api.getBrowserCookies).toHaveBeenCalledTimes(1)
    expect(result.current.data).toEqual(ABSENT)
  })

  it('surfaces a 400 import as a rejected mutation carrying the server message', async () => {
    stubFetch({
      'GET /api/browser/cookies': { status: 200, body: ABSENT },
      'POST /api/browser/cookies': { status: 400, body: { error: 'That is not a cookie export' } },
    })
    const { result } = renderHookWithProviders(() => useBrowserCookies(true))
    await waitFor(() => expect(result.current.data?.present).toBe(false))
    // friendlyErrText unwraps {"error": …} into ApiError.message.
    await expect(result.current.importCookies('garbage')).rejects.toMatchObject({
      message: 'That is not a cookie export',
    })
    // The failed import did not flip status to present.
    expect(result.current.data?.present).toBe(false)
  })

  it('marks every OTHER slot stale after an import or clear, and leaves its own entry as written', async () => {
    const gets: (string | null)[] = []
    let stored: unknown = ABSENT
    vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
      const method = (init?.method ?? 'GET').toUpperCase()
      const sk = new Headers(init?.headers).get('X-Session-Key')
      let body: unknown
      if (method === 'GET') { gets.push(sk); body = stored }
      else if (method === 'POST') { stored = PRESENT; body = IMPORT_OK }
      else { stored = ABSENT; body = { ok: true, present: false, live: { cleared: [], failed: {} } } }
      return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } })
    }))
    // One query client, two slots: the panel of slot A and the panel of slot B.
    const { result } = renderHookWithProviders(() => ({
      a: useBrowserCookies(true, 'slot-a'),
      b: useBrowserCookies(true, 'slot-b'),
    }))
    await waitFor(() => expect(result.current.b.data?.present).toBe(false))
    await waitFor(() => expect(result.current.a.data?.present).toBe(false))
    await act(async () => { await result.current.a.importCookies('{"cookies":[]}') })
    // B refetched under its own key and now sees the set; A was not refetched.
    await waitFor(() => expect(result.current.b.data?.present).toBe(true))
    expect(gets.filter((k) => k === 'slot-a')).toHaveLength(1)
    expect(gets.filter((k) => k === 'slot-b')).toHaveLength(2)
    await act(async () => { await result.current.a.clear() })
    await waitFor(() => expect(result.current.b.data?.present).toBe(false))
    expect(gets.filter((k) => k === 'slot-a')).toHaveLength(1)
  })

  it('settles a persistent status failure as an error after a bounded number of retries', async () => {
    const calls = stubFetch({ '/api/browser/cookies': { status: 500, body: { error: 'boom' } } })
    const { result } = renderHookWithProviders(() => useBrowserCookies(true))
    await waitFor(() => expect(result.current.error).not.toBeNull(), { timeout: 8000 })
    expect(calls.filter((c) => c.url === '/api/browser/cookies')).toHaveLength(3)
    expect(result.current.forbidden).toBe(false)
  }, 15000)

  it('keeps a partial clear in lastClear after the caller unmounts', async () => {
    stubFetch({ 'GET /api/browser/cookies': { status: 200, body: PRESENT } })
    const partial = { ok: true, present: false, live: { cleared: [], failed: { 'kc-abc': 'timeout' } } }
    let resolveClear!: (value: typeof partial) => void
    vi.spyOn(api, 'clearBrowserCookies').mockReturnValue(new Promise<typeof partial>((resolve) => {
      resolveClear = resolve
    }))
    const { result } = renderHookWithProviders(() => useBrowserCookies(true))
    await waitFor(() => expect(result.current.data).toEqual(PRESENT))
    let pendingClear!: ReturnType<typeof result.current.clear>
    const caller = renderHook(() => result.current.clear)
    act(() => { pendingClear = caller.result.current() })
    caller.unmount()
    await act(async () => {
      resolveClear(partial)
      expect(await pendingClear).toEqual(partial)
    })
    await waitFor(() => expect(result.current.lastClear).toEqual(partial))
    expect(result.current.data).toEqual(ABSENT)
    expect(result.current.clearError).toBeNull()
  })

  it.each(['rejected', 'partial'] as const)('a successful import resets a %s clear outcome', async (outcome) => {
    const partial = { ok: true, present: false, live: { cleared: [], failed: { 'kc-abc': 'timeout' } } }
    stubFetch({
      'GET /api/browser/cookies': { status: 200, body: PRESENT },
      'DELETE /api/browser/cookies': outcome === 'rejected'
        ? { status: 500, body: { error: 'clear failed' } }
        : { status: 200, body: partial },
      'POST /api/browser/cookies': { status: 200, body: IMPORT_OK },
    })
    const { result } = renderHookWithProviders(() => useBrowserCookies(true))
    await waitFor(() => expect(result.current.data).toEqual(PRESENT))
    await act(async () => { await result.current.clear().catch(() => {}) })
    await waitFor(() => {
      if (outcome === 'rejected') expect(result.current.clearError?.message).toBe('clear failed')
      else expect(result.current.lastClear).toEqual(partial)
    })
    await act(async () => { await result.current.importCookies('{"cookies":[]}') })
    await waitFor(() => {
      expect(result.current.clearError).toBeNull()
      expect(result.current.lastClear).toBeNull()
      expect(result.current.data).toEqual(PRESENT)
    })
  })

  it('drops a partial clear outcome after an external import refetch', async () => {
    const partial = { ok: true, present: false, live: { cleared: [], failed: { 'kc-abc': 'timeout' } } }
    stubFetch({ 'GET /api/browser/cookies': { status: 200, body: PRESENT } })
    vi.spyOn(api, 'clearBrowserCookies').mockResolvedValue(partial)
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const { result, unmount } = renderHook(() => useBrowserCookies(true, 'slot-a'), {
      wrapper: ({ children }) => <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>,
    })
    try {
      await waitFor(() => expect(result.current.data).toEqual(PRESENT))
      await act(async () => { await result.current.clear() })
      await waitFor(() => expect(result.current.lastClear).toEqual(partial))
      expect(result.current.data).toEqual(ABSENT)
      await act(async () => { await queryClient.refetchQueries({ queryKey: ['browserCookies', 'slot-a'] }) })
      await waitFor(() => {
        expect(result.current.data).toEqual(PRESENT)
        expect(result.current.lastClear).toBeNull()
      })
    } finally {
      unmount()
      queryClient.clear()
    }
  })

  it.each(['rejected', 'partial'] as const)('hides a %s clear outcome after switching slots while DELETE is pending', async (outcome) => {
    const partial = { ok: true, present: false, live: { cleared: [], failed: { 'kc-abc': 'timeout' } } }
    stubFetch({ 'GET /api/browser/cookies': { status: 200, body: PRESENT } })
    let resolveClear!: (value: typeof partial) => void
    let rejectClear!: (error: Error) => void
    vi.spyOn(api, 'clearBrowserCookies').mockReturnValue(new Promise<typeof partial>((resolve, reject) => {
      resolveClear = resolve
      rejectClear = reject
    }))
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
    const { result, rerender, unmount } = renderHook(({ slot }) => useBrowserCookies(true, slot), {
      initialProps: { slot: 'slot-a' },
      wrapper: ({ children }) => <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>,
    })
    try {
      await waitFor(() => expect(result.current.data).toEqual(PRESENT))
      let pending!: Promise<unknown>
      act(() => { pending = result.current.clear().catch(() => {}) })
      await waitFor(() => expect(result.current.clearing).toBe(true))
      rerender({ slot: 'slot-b' })
      await waitFor(() => expect(result.current.data).toEqual(PRESENT))
      await act(async () => {
        if (outcome === 'partial') resolveClear(partial)
        else rejectClear(new Error('clear failed'))
        await pending
      })
      await waitFor(() => expect(result.current.clearing).toBe(false))
      expect(result.current.clearError).toBeNull()
      expect(result.current.lastClear).toBeNull()
      rerender({ slot: 'slot-a' })
      await waitFor(() => {
        if (outcome === 'partial') expect(result.current.lastClear).toEqual(partial)
        else expect(result.current.clearError?.message).toBe('clear failed')
      })
    } finally {
      unmount()
      queryClient.clear()
    }
  })

  it('keeps clearing true when an import succeeds during a pending DELETE', async () => {
    const cleared = { ok: true, present: false, live: { cleared: [], failed: {} } }
    stubFetch({
      'GET /api/browser/cookies': { status: 200, body: PRESENT },
      'POST /api/browser/cookies': { status: 200, body: IMPORT_OK },
    })
    let resolveClear!: (value: typeof cleared) => void
    vi.spyOn(api, 'clearBrowserCookies').mockReturnValue(new Promise<typeof cleared>((resolve) => {
      resolveClear = resolve
    }))
    const { result } = renderHookWithProviders(() => useBrowserCookies(true, 'slot-a'))
    await waitFor(() => expect(result.current.data).toEqual(PRESENT))
    let pending!: ReturnType<typeof result.current.clear>
    act(() => { pending = result.current.clear() })
    await waitFor(() => expect(result.current.clearing).toBe(true))
    await act(async () => { await result.current.importCookies('{"cookies":[]}') })
    await waitFor(() => expect(result.current.importing).toBe(false))
    expect(result.current.clearing).toBe(true)
    expect(result.current.clearError).toBeNull()
    expect(result.current.lastClear).toBeNull()
    await act(async () => { resolveClear(cleared); await pending })
    await waitFor(() => expect(result.current.clearing).toBe(false))
  })

  it('clears the stored set (DELETE) and reflects the empty state', async () => {
    stubFetch({
      'GET /api/browser/cookies': { status: 200, body: PRESENT },
      'DELETE /api/browser/cookies': {
        status: 200,
        body: { ok: true, present: false, live: { cleared: ['kc-abc'], failed: { 'kc-def': 'timeout' } } },
      },
    })
    const { result } = renderHookWithProviders(() => useBrowserCookies(true))
    await waitFor(() => expect(result.current.data?.present).toBe(true))
    // The live report rides the resolved value, so the caller can say which
    // sessions did not take the clear; the cache still flips to empty.
    const cleared = await result.current.clear()
    expect(cleared.live).toEqual({ cleared: ['kc-abc'], failed: { 'kc-def': 'timeout' } })
    await waitFor(() => expect(result.current.data?.present).toBe(false))
    expect(result.current.data?.summary).toBeNull()
  })
})
