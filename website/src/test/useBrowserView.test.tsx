import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { waitFor } from '@testing-library/react'

import { renderHookWithProviders } from './helpers'
import { useBrowserView } from '../hooks/useBrowserView'

/**
 * `useBrowserView` against the REAL api client, with only `fetch` stubbed.
 *
 * The behaviour worth pinning is what the hook does with the transport's own
 * failures, and that only exists end-to-end: the 404 → `unavailable` mapping
 * depends on the client throwing an `ApiError` carrying a status, so mocking the
 * client away would test the mapping against a fixture instead of the thing it
 * has to survive. The panel-level states are covered in WebPreviewPanel.test.tsx.
 */

/** Answer one request per path with a real Response. */
function stubFetch(routes: Record<string, { status: number; body?: unknown }>) {
  const calls: string[] = []
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    calls.push(url)
    const route = routes[url] ?? { status: 404, body: 'no such route' }
    const isJson = typeof route.body === 'object' && route.body !== null
    return new Response(isJson ? JSON.stringify(route.body) : String(route.body ?? ''), {
      status: route.status,
      headers: isJson ? { 'Content-Type': 'application/json' } : undefined,
    })
  }))
  return calls
}

const RUNNING = { status: 'running', url: 'http://127.0.0.1:45613/', port: 45613, reason: null }

describe('useBrowserView', () => {
  beforeEach(() => { vi.unstubAllGlobals() })
  afterEach(() => { vi.unstubAllGlobals() })

  it('reports a running view verbatim', async () => {
    stubFetch({ '/api/browser/view': { status: 200, body: RUNNING } })
    const { result } = renderHookWithProviders(() => useBrowserView(true))
    await waitFor(() => expect(result.current.data).toEqual(RUNNING))
    expect(result.current.error).toBeNull()
  })

  it('degrades a MISSING route to unavailable rather than an error', async () => {
    // A gateway that predates the endpoint. "The browser view does not exist
    // here" is the honest reading of a 404 — surfacing it as a failed request
    // would put a transport error in front of the user for a capability that is
    // simply absent.
    stubFetch({ '/api/browser/view': { status: 404, body: 'not found' } })
    const { result } = renderHookWithProviders(() => useBrowserView(true))
    await waitFor(() => expect(result.current.data?.status).toBe('unavailable'))
    expect(result.current.error).toBeNull()
    // `reason` stays null: the field carries the SERVER's words, and inventing
    // one here would be indistinguishable from something it actually reported.
    expect(result.current.data?.reason).toBeNull()
    expect(result.current.data?.url).toBeNull()
  })

  it('keeps any OTHER failure an error instead of claiming the view is absent', async () => {
    // A 500 means the gateway tried and broke. Reporting that as `unavailable`
    // would tell the user the capability does not exist and hide a real fault.
    stubFetch({ '/api/browser/view': { status: 500, body: 'boom' } })
    const { result } = renderHookWithProviders(() => useBrowserView(true))
    await waitFor(() => expect(result.current.error).toBeTruthy())
    expect(result.current.data).toBeUndefined()
  })

  it('does not read the status at all when disabled', async () => {
    const calls = stubFetch({ '/api/browser/view': { status: 200, body: RUNNING } })
    renderHookWithProviders(() => useBrowserView(false))
    await new Promise((r) => setTimeout(r, 20))
    // Filtered rather than asserted empty: the shared render wrapper's
    // ThemeProvider fetches on its own, and folding those into this assertion
    // would make it fail for a reason that has nothing to do with the hook.
    expect(calls.filter((u) => u.startsWith('/api/browser/view'))).toEqual([])
  })

  it('writes the start response straight into the cache (no read-after-write)', async () => {
    // The start endpoint returns the same shape as the GET, so the panel must not
    // show `stopped` for a view that is already up while a second read is in
    // flight.
    const calls = stubFetch({
      '/api/browser/view': { status: 200, body: { status: 'stopped', url: null, port: null, reason: null } },
      '/api/browser/view/start': { status: 200, body: RUNNING },
    })
    const { result } = renderHookWithProviders(() => useBrowserView(true))
    await waitFor(() => expect(result.current.data?.status).toBe('stopped'))
    const before = calls.length
    result.current.start()
    await waitFor(() => expect(result.current.data).toEqual(RUNNING))
    // Exactly one browser-view request was added: the POST. No follow-up GET.
    expect(calls.slice(before).filter((u) => u.startsWith('/api/browser/view')))
      .toEqual(['/api/browser/view/start'])
  })

  it('flags a start that answered 200 without producing a running view', async () => {
    // `POST /start` returns the post-attempt status, so a failed launch is a 200
    // carrying `stopped`. The mutation succeeded; the START did not, and the panel
    // needs to be able to tell those apart or the click looks like a no-op.
    const stopped = { status: 'stopped', url: null, port: null, reason: null }
    stubFetch({
      '/api/browser/view': { status: 200, body: stopped },
      '/api/browser/view/start': { status: 200, body: stopped },
    })
    const { result } = renderHookWithProviders(() => useBrowserView(true))
    await waitFor(() => expect(result.current.data?.status).toBe('stopped'))
    expect(result.current.startDidNotTake).toBe(false)
    result.current.start()
    await waitFor(() => expect(result.current.startDidNotTake).toBe(true))
    expect(result.current.startError).toBeNull()
  })
})
