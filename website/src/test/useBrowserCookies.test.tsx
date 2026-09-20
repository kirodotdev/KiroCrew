import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { waitFor } from '@testing-library/react'

import { renderHookWithProviders } from './helpers'
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
const PRESENT = { present: true, summary: SUMMARY, config_path: '/home/u/.kiro/crew/browser-storage-state.json' }
const ABSENT = { present: false, summary: null, config_path: '/home/u/.kiro/crew/browser-storage-state.json' }
const IMPORT_OK = {
  ok: true,
  summary: SUMMARY,
  hot_load: { loaded: ['kc-abc'], failed: {} },
}

describe('useBrowserCookies', () => {
  beforeEach(() => { vi.unstubAllGlobals() })
  afterEach(() => { vi.unstubAllGlobals() })

  it('reports a present cookie set verbatim', async () => {
    stubFetch({ '/api/browser/cookies': { status: 200, body: PRESENT } })
    const { result } = renderHookWithProviders(() => useBrowserCookies(true))
    await waitFor(() => expect(result.current.data).toEqual(PRESENT))
    expect(result.current.forbidden).toBe(false)
    expect(result.current.error).toBeNull()
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
    await new Promise((r) => setTimeout(r, 20))
    expect(calls.filter((c) => c.url.startsWith('/api/browser/cookies'))).toEqual([])
  })

  it('writes the import result straight into the cache (present + fresh summary)', async () => {
    stubFetch({
      'GET /api/browser/cookies': { status: 200, body: ABSENT },
      'POST /api/browser/cookies': { status: 200, body: IMPORT_OK },
    })
    const { result } = renderHookWithProviders(() => useBrowserCookies(true))
    await waitFor(() => expect(result.current.data?.present).toBe(false))
    const res = await result.current.importCookies('{"cookies":[]}', 'export.json')
    expect(res.hot_load.loaded).toEqual(['kc-abc'])
    await waitFor(() => expect(result.current.data?.present).toBe(true))
    expect(result.current.data?.summary).toEqual(SUMMARY)
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

  it('clears the stored set (DELETE) and reflects the empty state', async () => {
    stubFetch({
      'GET /api/browser/cookies': { status: 200, body: PRESENT },
      'DELETE /api/browser/cookies': { status: 200, body: { ok: true, present: false } },
    })
    const { result } = renderHookWithProviders(() => useBrowserCookies(true))
    await waitFor(() => expect(result.current.data?.present).toBe(true))
    await result.current.clear()
    await waitFor(() => expect(result.current.data?.present).toBe(false))
    expect(result.current.data?.summary).toBeNull()
  })
})
