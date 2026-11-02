/**
 * The Files app's API client surfaces a refusal as the shared typed error, the
 * contract `test/appApiTypedErrors.test.ts` pins for the other app clients.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'

import { ApiError } from '../../api/apiError'
import { fileExplorerApi } from './api'

/** A refusal whose body is the JSON envelope the backend actually sends. */
const REFUSAL = JSON.stringify({ error: 'path is outside the sandbox' })

const refuse = (status = 403, body = REFUSAL) =>
  vi.fn().mockResolvedValue({
    ok: false,
    status,
    headers: { get: () => null },
    text: () => Promise.resolve(body),
    json: () => Promise.reject(new Error('should not be parsed on a refusal')),
  } as unknown as Response)

describe('fileExplorerApi surfaces refusals as ApiError', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('is typed, status-bearing, and carries the unwrapped message', async () => {
    vi.stubGlobal('fetch', refuse())
    const e = (await fileExplorerApi.health().catch((err: unknown) => err)) as ApiError
    expect(e).toBeInstanceOf(ApiError)
    expect(e.status).toBe(403)
    // The human message, not the envelope — this is what a toast renders.
    expect(e.message).toBe('path is outside the sandbox')
    expect(e.message).not.toContain('{"error"')
    // The envelope stays reachable for a caller that wants structured fields.
    expect(e.body).toBe(REFUSAL)
  })

  it('relabels a 429 with the tunnel-edge message instead of the body', async () => {
    vi.stubGlobal('fetch', refuse(429, '{"message":"Rate exceeded","throttlingReasons":null}'))
    const e = (await fileExplorerApi.health().catch((err: unknown) => err)) as ApiError
    expect(e.status).toBe(429)
    expect(e.message).toContain('Rate limited')
    expect(e.message).not.toContain('throttlingReasons')
    expect(e.body).toContain('throttlingReasons')
  })

  it('keeps the HTTP <status> fallback for an empty body', async () => {
    vi.stubGlobal('fetch', refuse(500, ''))
    const e = (await fileExplorerApi.health().catch((err: unknown) => err)) as ApiError
    expect(e).toBeInstanceOf(ApiError)
    expect(e.message).toBe('HTTP 500')
  })
})
