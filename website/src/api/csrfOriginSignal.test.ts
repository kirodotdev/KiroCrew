/**
 * `csrfOriginSignal` — detection of the gateway's stale-origin CSRF 403 and the
 * installable recovery handler. The contract is: fire the handler ONLY for a
 * 403 that (a) lacks `X-Auth-Required` and (b) carries the barrier's
 * "request origin not allowed" body, and never for the look-alikes it must not
 * be confused with (auth-expiry 403, an unrelated permission 403, a non-403).
 */
import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  CSRF_ORIGIN_REFUSAL_MARKER,
  installCsrfOriginHandler,
  noteCsrfOriginResponse,
  __resetCsrfOriginHandlerForTests,
} from './csrfOriginSignal'

const CSRF_BODY = 'CSRF check failed: request origin not allowed.'

afterEach(() => {
  __resetCsrfOriginHandlerForTests()
})

describe('noteCsrfOriginResponse', () => {
  it('matches the gateway CSRF 403 and fires the installed handler', () => {
    const handler = vi.fn()
    installCsrfOriginHandler(handler)
    expect(noteCsrfOriginResponse(403, false, CSRF_BODY)).toBe(true)
    expect(handler).toHaveBeenCalledTimes(1)
  })

  it('ignores the auth-expiry 403 (authRequired) so silent refresh keeps it', () => {
    const handler = vi.fn()
    installCsrfOriginHandler(handler)
    // Even with the marker present, an auth-required 403 must not reload.
    expect(noteCsrfOriginResponse(403, true, CSRF_BODY)).toBe(false)
    expect(handler).not.toHaveBeenCalled()
  })

  it('ignores an unrelated 403 whose body is not the CSRF marker', () => {
    const handler = vi.fn()
    installCsrfOriginHandler(handler)
    expect(noteCsrfOriginResponse(403, false, 'permission denied')).toBe(false)
    expect(handler).not.toHaveBeenCalled()
  })

  it('ignores non-403 statuses even with the marker in the body', () => {
    const handler = vi.fn()
    installCsrfOriginHandler(handler)
    expect(noteCsrfOriginResponse(401, false, CSRF_BODY)).toBe(false)
    expect(noteCsrfOriginResponse(500, false, CSRF_BODY)).toBe(false)
    expect(handler).not.toHaveBeenCalled()
  })

  it('matches on a substring, tolerating an appended status line or extra detail', () => {
    const handler = vi.fn()
    installCsrfOriginHandler(handler)
    expect(
      noteCsrfOriginResponse(403, false, `${CSRF_BODY}\n\n403: Forbidden`),
    ).toBe(true)
    expect(handler).toHaveBeenCalledTimes(1)
  })

  it('is a no-op (still reports the match) when no handler is installed', () => {
    // Uninstalled: detection still returns true so a caller keeps its own path;
    // it simply does not throw for the missing handler.
    expect(noteCsrfOriginResponse(403, false, CSRF_BODY)).toBe(true)
  })

  it('exports the exact marker the server raises', () => {
    expect(CSRF_BODY).toContain(CSRF_ORIGIN_REFUSAL_MARKER)
  })
})
