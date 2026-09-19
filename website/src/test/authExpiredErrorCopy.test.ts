/**
 * Tests that an auth denial reaches call sites as a recovery instruction rather
 * than the gateway's cryptographic reason ("invalid signature"), while the raw
 * reason survives on the error for diagnostics.
 *
 * The distinction matters twice over: a 403 that is NOT an auth denial (the
 * instances feature being disabled) must keep its own message, because panels
 * match on that text to render the enable-the-feature state instead of an error.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { api, ApiError, isAuthExpiredError, __resetAuthRecoveryStateForTests } from '../api/client'
import { retryPolicy } from '../api/queryClient'

const authDenial = (reason: string): Response =>
  new Response(JSON.stringify({ error: reason }), {
    status: 403,
    headers: { 'content-type': 'application/json', 'X-Auth-Required': 'true' },
  })

const plainForbidden = (reason: string): Response =>
  new Response(JSON.stringify({ error: reason }), {
    status: 403,
    headers: { 'content-type': 'application/json' },
  })

/** A gate's sign-in page: HTML on a 403, and no `X-Auth-Required` anywhere on it. */
const proxyChallenge = (): Response =>
  new Response(
    '<!DOCTYPE html><html><head><title>Access Required</title></head><body>'
    + '<h1>Access Required</h1>'
    + '<p><a href="https://dash.example/gate-auth?redirect=%2Fapi">Sign in</a></p>'
    + '</body></html>',
    { status: 403, headers: { 'content-type': 'text/html; charset=UTF-8' } },
  )

/** The same shape with nowhere to sign in -- a firewall block, not a lapse. */
const proxyBlockPage = (): Response =>
  new Response(
    '<!DOCTYPE html><html><head><title>Access denied</title></head><body>'
    + '<h1>Sorry, you have been blocked</h1>'
    + '<p>Ray ID: 8f2a1c</p><footer><a href="/authors/jane">Authors</a></footer>'
    + '</body></html>',
    { status: 403, headers: { 'content-type': 'text/html; charset=UTF-8' } },
  )

describe('auth-expired error copy', () => {
  let fetchMock: ReturnType<typeof vi.fn>
  let originalFetch: typeof fetch

  beforeEach(() => {
    __resetAuthRecoveryStateForTests()
    fetchMock = vi.fn()
    originalFetch = globalThis.fetch
    globalThis.fetch = fetchMock as unknown as typeof fetch
  })

  afterEach(() => {
    globalThis.fetch = originalFetch
    __resetAuthRecoveryStateForTests()
    document.getElementById('mc-session-expired')?.remove()
  })

  it('replaces the HMAC reason with a re-auth instruction and flags the error', async () => {
    fetchMock.mockImplementation((url: string) =>
      Promise.resolve(
        url === '/api/auth/refresh'
          ? new Response('{}', { status: 401 })
          : authDenial('invalid signature'),
      ),
    )

    const err = await api.listInstances().then(
      () => null,
      (e: unknown) => e,
    )

    expect(err).toBeInstanceOf(ApiError)
    const apiErr = err as ApiError
    expect(apiErr.status).toBe(403)
    expect(isAuthExpiredError(apiErr)).toBe(true)
    expect(apiErr.message).not.toContain('invalid signature')
    expect(apiErr.message.toLowerCase()).toContain('kirocrew token')
    // The reason is still recoverable for diagnostics even though it is not shown.
    expect(apiErr.body).toContain('invalid signature')
  })

  it('leaves a non-auth 403 message untouched so feature-disabled detection still matches', async () => {
    fetchMock.mockResolvedValue(
      plainForbidden('instances feature is disabled (set instances.enabled=true)'),
    )

    const err = await api.listInstances().then(
      () => null,
      (e: unknown) => e,
    )

    const apiErr = err as ApiError
    expect(isAuthExpiredError(apiErr)).toBe(false)
    expect(apiErr.message).toContain('disabled')
  })

  it('reports a non-ApiError value as not auth-expired', () => {
    expect(isAuthExpiredError(new Error('boom'))).toBe(false)
    expect(isAuthExpiredError(undefined)).toBe(false)
  })

  it('flags a lapsed PROXY session, which carries no gateway header at all', async () => {
    // Routed through the real failure path, so deleting the wiring makes this fail --
    // which a hand-built ApiError cannot.
    fetchMock.mockImplementation((url: string) =>
      Promise.resolve(
        url === '/api/auth/refresh'
          ? new Response('{}', { status: 401 })
          : proxyChallenge(),
      ),
    )

    const err = await api.listInstances().then(() => null, (e: unknown) => e)

    expect(err).toBeInstanceOf(ApiError)
    expect(isAuthExpiredError(err as ApiError)).toBe(true)
    // The narrower flag is what withdraws the retry, and only the proxy case sets it.
    expect(retryPolicy(0, err)).toBe(false)
    expect((err as ApiError).message).toMatch(/access proxy/i)
    // The HTML page must not reach the user as the message.
    expect((err as ApiError).message).not.toMatch(/<!DOCTYPE/i)
  })

  it('treats a page with no way in exactly like one that offers one', async () => {
    // The structural guard at the factory: telling the two apart is what review found
    // misreading four classes of page.
    fetchMock.mockImplementation((url: string) =>
      Promise.resolve(
        url === '/api/auth/refresh'
          ? new Response('{}', { status: 401 })
          : proxyBlockPage(),
      ),
    )

    const err = await api.listInstances().then(() => null, (e: unknown) => e)

    expect(err).toBeInstanceOf(ApiError)
    expect(isAuthExpiredError(err as ApiError)).toBe(true)
    // The gateway never saw the request, so a silent retry a second later reproduces it.
    expect(retryPolicy(0, err)).toBe(false)
    expect((err as ApiError).message).toMatch(/if a sign-in page appears/i)
  })

})
