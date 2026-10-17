/**
 * Tests for api.appSessionStatus — the app status route call.
 *
 * This exists because every other test of the session-control feature mocks the
 * api client, which cannot prove the thing that made this method necessary: the
 * call must carry `X-Session-Key`, or the server's ephemeral gate is skipped —
 * a fail-open path. A bare `fetch()` (what this replaced) omits it silently, so
 * the header is asserted against a stubbed global fetch here.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { api } from '../api/client'

describe('api.appSessionStatus', () => {
  let fetchMock: ReturnType<typeof vi.fn>

  beforeEach(() => {
    fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      headers: new Headers(),
      json: async () => ({ state: 'ok', tooltip: 'bound' }),
    })
    vi.stubGlobal('fetch', fetchMock)
  })
  afterEach(() => vi.unstubAllGlobals())

  it('sends X-Session-Key so the server-side ephemeral gate runs', async () => {
    // The whole reason this method exists rather than a bare fetch.
    await api.appSessionStatus('test-app', 'session-status', { session_key: 'chat-1' })
    const [, init] = fetchMock.mock.calls[0]
    expect((init.headers as Record<string, string>)['X-Session-Key']).toBe('dashboard:ui')
  })

  it('builds the app route and encodes the query params', async () => {
    await api.appSessionStatus('test-app', 'session-status', {
      session_key: 'dashboard:chat-2',
      folder_id: '97fff46e',
      folder_name: 'Back End',
    })
    const url = String(fetchMock.mock.calls[0][0])
    expect(url.startsWith('/api/apps/test-app/session-status?')).toBe(true)
    expect(url).toContain('session_key=dashboard%3Achat-2')
    expect(url).toContain('folder_id=97fff46e')
    // A space must not break the query string.
    expect(url).toContain('folder_name=Back+End')
  })

  it('encodes an app name that needs it', async () => {
    await api.appSessionStatus('weird/name', 'p', {})
    expect(String(fetchMock.mock.calls[0][0])).toContain('/api/apps/weird%2Fname/p')
  })

  it('omits the query string entirely when there are no params', async () => {
    await api.appSessionStatus('a', 'p', {})
    expect(String(fetchMock.mock.calls[0][0])).toBe('/api/apps/a/p')
  })

  it('uses the in-gateway hook prefix by default', async () => {
    // An app whose backend is hook-registered is served under /api/apps/<app>/.
    await api.appSessionStatus('a', 'p', { session_key: 'k' })
    expect(String(fetchMock.mock.calls[0][0]).startsWith('/api/apps/a/p?')).toBe(true)
  })

  it('uses the reverse-proxy prefix for a process-backed app', async () => {
    // An app running its OWN backend process is proxied at /apps/<app>/api/.
    // Calling the hook prefix for it answers 502 "no reachable backend", which
    // the chip renders as permanently stateless with nothing saying why —
    // verified live against a running gateway.
    await api.appSessionStatus('a', 'p', { session_key: 'k' }, true)
    expect(String(fetchMock.mock.calls[0][0]).startsWith('/apps/a/api/p?')).toBe(true)
  })

  it('still encodes the app name on the proxy prefix', async () => {
    await api.appSessionStatus('weird/name', 'p', {}, true)
    expect(String(fetchMock.mock.calls[0][0])).toBe('/apps/weird%2Fname/api/p')
  })

  it('refuses a bad statusPath on either prefix', () => {
    // The guard must not be bypassable by asking for the other base.
    expect(() => api.appSessionStatus('a', '../x', {}, true)).toThrow(/invalidAppStatusPath/)
    expect(() => api.appSessionStatus('a', '../x', {}, false)).toThrow(/invalidAppStatusPath/)
  })

  it('leaks no placeholder junk into either prefix', async () => {
    // `ApiClient.coverage.test.tsx` applies these checks to every api method by
    // probing it with generic positional args. This method opts out of that table
    // (a boolean 4th arg cannot survive a string probe), so the checks it gives
    // up are asserted here instead — a forgotten argument or a botched template
    // literal shows up as one of these tokens inside the path.
    for (const processBacked of [false, true]) {
      fetchMock.mockClear()
      await api.appSessionStatus('a', 'p', { session_key: 'k' }, processBacked)
      const url = String(fetchMock.mock.calls[0][0])
      for (const junk of ['undefined', '[object Object]', 'NaN', '/null']) {
        expect(url.includes(junk), `${url} leaked ${junk}`).toBe(false)
      }
    }
  })

  it('refuses a traversal path without issuing a request', async () => {
    // The boundary is enforced here rather than deferred to callers. A caller
    // that forgets safeStatusPath must not be able to reach another app's
    // routes. Regression for AutoSDE f-bec83efd.
    expect(() => api.appSessionStatus('a', 'x/../../other-app/secret', {})).toThrow(
      /invalidAppStatusPath/,
    )
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('refuses a path that would corrupt the query string', () => {
    expect(() => api.appSessionStatus('a', 'p?x=1', {})).toThrow(/invalidAppStatusPath/)
    expect(() => api.appSessionStatus('a', 'p#f', {})).toThrow(/invalidAppStatusPath/)
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('refuses a protocol-relative path and an empty one', () => {
    expect(() => api.appSessionStatus('a', '//evilhost/x', {})).toThrow(/invalidAppStatusPath/)
    expect(() => api.appSessionStatus('a', '', {})).toThrow(/invalidAppStatusPath/)
  })

  it('returns the parsed body', async () => {
    await expect(api.appSessionStatus('a', 'p', {})).resolves.toEqual({
      state: 'ok',
      tooltip: 'bound',
    })
  })

  it('rejects on a non-ok response rather than returning it', async () => {
    // The caller treats a rejection as "no state", which is how the chip fails
    // closed. Returning a Response would tint the chip from an error body.
    fetchMock.mockResolvedValue({
      ok: false,
      status: 503,
      headers: new Headers(),
      text: async () => 'down',
    })
    await expect(api.appSessionStatus('a', 'p', {})).rejects.toThrow()
  })
})
