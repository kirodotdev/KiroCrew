/**
 * Tests for the permission-scoped API client created by AppApiProvider
 * (app-sdk/index.ts::createScopedApi).
 *
 * Locks in the SSRF / permission guard: the scoped client MUST reject absolute,
 * protocol-relative, and backslash-authority URLs, reject paths outside the
 * declared allowlist (including `..` traversal that would escape scope), and
 * permit declared paths (with query strings). It must also tolerate 204 /
 * empty-body responses without throwing. These are security- and
 * correctness-sensitive, so they are enforced deterministically here.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, act } from '@testing-library/react'
import React from 'react'
import { AppApiProvider, useAppApi, type AppApi } from '../app-sdk/index'

// Render the provider and hand back the scoped API client it builds.
function getScopedApi(allowedApiPaths: string[], sessionKey?: string): AppApi {
  let captured: AppApi | null = null

  function Probe() {
    captured = useAppApi()
    return null
  }

  act(() => {
    render(
      React.createElement(
        AppApiProvider,
        {
          appName: 'test-app',
          appVersion: '1.0.0',
          allowedApiPaths,
          allowedEvents: [],
          subscribeFn: () => () => {},
          navigateFn: () => {},
          notifyFn: () => {},
          sessionKey,
        },
        React.createElement(Probe),
      ),
    )
  })

  if (!captured) throw new Error('failed to capture scoped api')
  return captured
}

describe('createScopedApi (via AppApiProvider) — session identity', () => {
  let fetchMock: ReturnType<typeof vi.fn>

  beforeEach(() => {
    fetchMock = vi.fn(async () => new Response('{}', {
      status: 200, headers: { 'Content-Type': 'application/json' },
    }))
    vi.stubGlobal('fetch', fetchMock)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  const headerOf = (call: unknown[]) =>
    new Headers((call[1] as RequestInit).headers).get('X-Session-Key')

  it('sends X-Session-Key on a scoped request when the host knows the session', async () => {
    // This is a security boundary, not a convenience. The backend's
    // restricted-session guard reads this header and FAILS OPEN without it, so an
    // incognito or guest chat would be granted the persistent writes it is meant
    // to be denied. Asserted on GET and POST because the guard applies to reads
    // and writes alike.
    const api = getScopedApi(['/api/apps/test-app'], 'dashboard:chat-2')
    await api.get('/api/apps/test-app/thing')
    await api.post('/api/apps/test-app/thing', { a: 1 })
    expect(fetchMock).toHaveBeenCalledTimes(2)
    expect(headerOf(fetchMock.mock.calls[0])).toBe('dashboard:chat-2')
    expect(headerOf(fetchMock.mock.calls[1])).toBe('dashboard:chat-2')
  })

  it('omits the header when the host has no session to declare', async () => {
    // Other provider hosts may remain unbound; absence must not select an
    // arbitrary chat. Routed app pages supply dashboard:ui explicitly.
    const api = getScopedApi(['/api/apps/test-app'])
    await api.get('/api/apps/test-app/thing')
    expect(headerOf(fetchMock.mock.calls[0])).toBeNull()
  })

  it('keeps the host session authoritative over a caller-supplied header', async () => {
    const api = getScopedApi(['/api/apps/test-app'], 'dashboard:chat-2')
    await api.get('/api/apps/test-app/thing', {
      headers: { 'X-Session-Key': 'dashboard:explicit' },
    })
    expect(headerOf(fetchMock.mock.calls[0])).toBe('dashboard:chat-2')
  })

  it('keeps the JSON content type the write verbs set', async () => {
    // The header merge must not drop what the verb helpers already send.
    const api = getScopedApi(['/api/apps/test-app'], 'dashboard:chat-2')
    await api.post('/api/apps/test-app/thing', { a: 1 })
    const sent = new Headers((fetchMock.mock.calls[0][1] as RequestInit).headers)
    expect(sent.get('Content-Type')).toBe('application/json')
    expect(sent.get('X-Session-Key')).toBe('dashboard:chat-2')
  })
})

describe('createScopedApi (via AppApiProvider) — SSRF / permission guard', () => {
  let fetchMock: ReturnType<typeof vi.fn>

  beforeEach(() => {
    fetchMock = vi.fn(async () => new Response('{}', {
      status: 200,
      headers: { 'content-type': 'application/json' },
    }))
    vi.stubGlobal('fetch', fetchMock)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('rejects absolute http(s) URLs (SSRF)', async () => {
    const api = getScopedApi(['/api/apps/test'])
    await expect(api.get('https://evil.example.com/steal')).rejects.toThrow(/Absolute URLs are not allowed/)
    await expect(api.get('http://169.254.169.254/latest/meta-data')).rejects.toThrow(/Absolute URLs are not allowed/)
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('rejects protocol-relative URLs (SSRF)', async () => {
    const api = getScopedApi(['/api/apps/test'])
    await expect(api.get('//evil.example.com/steal')).rejects.toThrow(/Absolute URLs are not allowed/)
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('rejects backslash authority tricks (URL parser treats \\ like /)', async () => {
    const api = getScopedApi(['/api/apps/test'])
    for (const bad of ['\\\\evil.example.com/steal', '/\\evil.example.com', '\\/evil.example.com']) {
      await expect(api.get(bad)).rejects.toThrow(/Absolute URLs are not allowed/)
    }
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('rejects paths outside the declared allowlist', async () => {
    const api = getScopedApi(['/api/apps/test'])
    await expect(api.get('/api/apps/other/secrets')).rejects.toThrow(/not permitted to access/)
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('rejects `..` traversal that escapes the allowlist', async () => {
    const api = getScopedApi(['/api/apps/test'])
    // Normalizes to /api/secret — outside the allowlist.
    await expect(api.get('/api/apps/test/../../secret')).rejects.toThrow(/not permitted to access/)
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('does not treat a sibling prefix as allowed (prefix boundary)', async () => {
    const api = getScopedApi(['/api/apps/test'])
    await expect(api.get('/api/apps/test-evil')).rejects.toThrow(/not permitted to access/)
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('permits a declared path and forwards the normalized path to fetch', async () => {
    const api = getScopedApi(['/api/apps/test'])
    await api.get('/api/apps/test')
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(fetchMock.mock.calls[0][0]).toBe('/api/apps/test')
  })

  it('permits a declared path with a query string', async () => {
    const api = getScopedApi(['/api/apps/test'])
    await api.get('/api/apps/test/items?limit=10')
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(fetchMock.mock.calls[0][0]).toBe('/api/apps/test/items?limit=10')
  })

  it('returns undefined for a 204 No Content response (does not call res.json)', async () => {
    fetchMock.mockResolvedValueOnce(new Response(null, { status: 204 }))
    const api = getScopedApi(['/api/apps/test'])
    await expect(api.del('/api/apps/test/item/1')).resolves.toBeUndefined()
  })

  it('returns undefined for a 200 with content-length 0', async () => {
    fetchMock.mockResolvedValueOnce(new Response('', { status: 200, headers: { 'content-length': '0' } }))
    const api = getScopedApi(['/api/apps/test'])
    await expect(api.post('/api/apps/test/action')).resolves.toBeUndefined()
  })

  it('returns undefined for a 200 with an empty body and NO content-length header', async () => {
    // res.json() would throw SyntaxError here; the client reads text and
    // returns undefined for an empty body regardless of the header.
    fetchMock.mockResolvedValueOnce(new Response('', { status: 200, headers: { 'content-type': 'application/json' } }))
    const api = getScopedApi(['/api/apps/test'])
    await expect(api.post('/api/apps/test/action')).resolves.toBeUndefined()
  })

  it('still parses a normal JSON body', async () => {
    fetchMock.mockResolvedValueOnce(new Response(JSON.stringify({ ok: true, n: 3 }), {
      status: 200, headers: { 'content-type': 'application/json' },
    }))
    const api = getScopedApi(['/api/apps/test'])
    await expect(api.get('/api/apps/test/data')).resolves.toEqual({ ok: true, n: 3 })
  })
})


describe('scoped request options and HTTP errors', () => {
  let fetchMock: ReturnType<typeof vi.fn>

  beforeEach(() => {
    fetchMock = vi.fn(async () => new Response('{}', { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('passes raw request bodies and signals without inventing a JSON content type', async () => {
    const api = getScopedApi(['/api/apps/test'], 'dashboard:chat-2')
    const body = new FormData()
    body.append('title', 'Example')
    const controller = new AbortController()
    await api.request('/api/apps/test/upload', {
      method: 'POST', body, signal: controller.signal,
      headers: new Headers({ 'X-Request-ID': 'sample' }),
    })
    const [path, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(path).toBe('/api/apps/test/upload')
    expect(init.method).toBe('POST')
    expect(init.body).toBe(body)
    expect(init.signal).toBe(controller.signal)
    const headers = new Headers(init.headers)
    expect(headers.get('Content-Type')).toBeNull()
    expect(headers.get('X-Request-ID')).toBe('sample')
    expect(headers.get('X-Session-Key')).toBe('dashboard:chat-2')
  })

  it.each(['post', 'put', 'patch'] as const)('keeps %s JSON arguments authoritative while forwarding request options', async method => {
    const api = getScopedApi(['/api/apps/test'], 'dashboard:chat-2')
    const controller = new AbortController()
    await api[method]('/api/apps/test/item', { value: 2 }, {
      method: 'DELETE', body: 'ignored', signal: controller.signal,
      headers: [['X-Request-ID', 'sample'], ['X-Session-Key', 'dashboard:other']],
    })
    const init = fetchMock.mock.calls[0][1] as RequestInit
    expect(init.method).toBe(method.toUpperCase())
    expect(init.body).toBe('{"value":2}')
    expect(init.signal).toBe(controller.signal)
    const headers = new Headers(init.headers)
    expect(headers.get('Content-Type')).toBe('application/json')
    expect(headers.get('X-Request-ID')).toBe('sample')
    expect(headers.get('X-Session-Key')).toBe('dashboard:chat-2')
  })

  it('does not mutate the caller headers when enforcing host identity', async () => {
    const api = getScopedApi(['/api/apps/test'], 'dashboard:chat-2')
    const headers = new Headers({ 'x-session-key': 'dashboard:other' })
    await api.request('/api/apps/test/item', { headers })
    expect(headers.get('X-Session-Key')).toBe('dashboard:other')
    expect(new Headers(fetchMock.mock.calls[0][1].headers).get('X-Session-Key'))
      .toBe('dashboard:chat-2')
  })

  it('keeps an explicit JSON media type on a patch', async () => {
    const api = getScopedApi(['/api/apps/test'], 'dashboard:chat-2')
    await api.patch('/api/apps/test/item', {}, {
      headers: { 'Content-Type': 'application/merge-patch+json' },
    })
    expect(new Headers(fetchMock.mock.calls[0][1].headers).get('Content-Type'))
      .toBe('application/merge-patch+json')
  })

  it('forwards DELETE options without letting them change the method', async () => {
    const api = getScopedApi(['/api/apps/test'], 'dashboard:chat-2')
    await api.del('/api/apps/test/item', {
      method: 'POST', headers: {
        'X-Request-ID': 'sample', 'X-Session-Key': 'dashboard:other',
      },
    })
    const init = fetchMock.mock.calls[0][1] as RequestInit
    expect(init.method).toBe('DELETE')
    expect(new Headers(init.headers).get('X-Request-ID')).toBe('sample')
    expect(new Headers(init.headers).get('X-Session-Key')).toBe('dashboard:chat-2')
  })

  it('forwards redirect refusal when the caller requires it', async () => {
    const api = getScopedApi(['/api/apps/test'], 'dashboard:chat-2')
    await api.request('/api/apps/test/item', { redirect: 'error' })
    expect(fetchMock.mock.calls[0][1].redirect).toBe('error')
  })

  it.each([
    '/api/apps/other/item', '/api/apps/test/../../secret',
    'https://example.com/api/apps/test', '//example.com/api/apps/test',
    '/\\example.com/api/apps/test',
  ])('retains the scope checks for generic requests: %s', async path => {
    const api = getScopedApi(['/api/apps/test'], 'dashboard:chat-2')
    await expect(api.request(path, { method: 'POST', body: '{}' })).rejects.toThrow()
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('does not activate wildcard grants through the new request method', async () => {
    const api = getScopedApi(['/api/apps/test/*'], 'dashboard:chat-2')
    await expect(api.request('/api/apps/test/item')).rejects.toThrow(/not permitted/)
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('does not let a caller invent a session when the host did not bind one', async () => {
    const api = getScopedApi(['/api/apps/test'])
    await expect(api.request('/api/apps/test/item', {
      headers: { 'x-session-key': 'dashboard:other' },
    })).rejects.toThrow(/host session/)
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('exposes HTTP status and the unparsed body while retaining the existing message', async () => {
    const body = '{"duplicates":[7],"error":"conflict"}'
    fetchMock.mockResolvedValueOnce(new Response(body, { status: 409 }))
    const api = getScopedApi(['/api/apps/test'], 'dashboard:chat-2')
    await expect(api.request('/api/apps/test/item', { method: 'POST' })).rejects.toMatchObject({
      name: 'AppApiError', status: 409, body, message: `API 409: ${body}`,
    })
  })

  it('uses statusText if the error body cannot be read', async () => {
    fetchMock.mockResolvedValueOnce({
      ok: false, status: 503, statusText: 'Unavailable',
      text: vi.fn().mockRejectedValue(new Error('body stream closed')),
    })
    const api = getScopedApi(['/api/apps/test'])
    await expect(api.get('/api/apps/test/item')).rejects.toMatchObject({
      status: 503, body: 'Unavailable', message: 'API 503: Unavailable',
    })
  })

  it('preserves network errors rather than presenting them as HTTP errors', async () => {
    const failure = new TypeError('connection unavailable')
    fetchMock.mockRejectedValueOnce(failure)
    const api = getScopedApi(['/api/apps/test'])
    await expect(api.request('/api/apps/test/item')).rejects.toBe(failure)
  })
})
