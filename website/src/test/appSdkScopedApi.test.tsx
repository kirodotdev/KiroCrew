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
    // A full-page app surface is not session-scoped. Sending an empty or invented
    // key would be worse than sending none: the guard matches on the value.
    const api = getScopedApi(['/api/apps/test-app'])
    await api.get('/api/apps/test-app/thing')
    expect(headerOf(fetchMock.mock.calls[0])).toBeNull()
  })

  it('does not clobber a session key the caller set explicitly', async () => {
    const api = getScopedApi(['/api/apps/test-app'], 'dashboard:chat-2')
    await api.get('/api/apps/test-app/thing', {
      headers: { 'X-Session-Key': 'dashboard:explicit' },
    })
    expect(headerOf(fetchMock.mock.calls[0])).toBe('dashboard:explicit')
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
