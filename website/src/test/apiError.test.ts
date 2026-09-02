import { ApiError, friendlyErrText, toApiError } from '../api/apiError'

/** A minimal Response stand-in: only what toApiError reads. */
const res = (status: number, body: string, headers: Record<string, string> = {}): Response =>
  ({
    ok: status < 400,
    status,
    headers: { get: (k: string) => headers[k] ?? null },
    text: () => Promise.resolve(body),
  }) as unknown as Response

describe('toApiError', () => {
  it('carries the status so callers can branch without matching message text', async () => {
    const e = await toApiError(res(409, 'conflict'))
    expect(e).toBeInstanceOf(ApiError)
    expect(e.status).toBe(409)
  })

  it('unwraps a JSON refusal instead of putting wire JSON in the message', async () => {
    // The bug this replaces: four app clients threw new Error(body), so this
    // body reached the UI verbatim as `{"error":"path is outside the sandbox"}`.
    const body = JSON.stringify({ error: 'path is outside the sandbox' })
    const e = await toApiError(res(403, body))
    expect(e.message).toBe('path is outside the sandbox')
    expect(e.message).not.toContain('{"error"')
    // The envelope is still recoverable for a caller that wants structured fields.
    expect(e.body).toBe(body)
  })

  it('falls back to HTTP <status> on an empty body, as the bare Errors did', async () => {
    const e = await toApiError(res(500, ''))
    expect(e.message).toBe('HTTP 500')
  })

  it('keeps a non-JSON body as the message', async () => {
    const e = await toApiError(res(502, 'upstream said no'))
    expect(e.message).toBe('upstream said no')
  })

  it('flags an auth-expiry refusal so callers can drop futile retries', async () => {
    const e = await toApiError(res(403, 'invalid signature', { 'X-Auth-Required': 'true' }))
    expect(e.authRequired).toBe(true)
    // A plain 403 is not an auth expiry.
    expect((await toApiError(res(403, 'nope'))).authRequired).toBe(false)
  })

  it('survives a body that cannot be read', async () => {
    // A refusal mid-stream must still produce the status rather than throwing
    // something unrelated out of the error path itself.
    const broken = {
      ok: false,
      status: 503,
      headers: { get: () => null },
      text: () => Promise.reject(new Error('stream closed')),
    } as unknown as Response
    const e = await toApiError(broken)
    expect(e.status).toBe(503)
    expect(e.message).toBe('HTTP 503')
  })

  it('survives a Response-like object with no headers', async () => {
    // The failure path must never throw: a stub (or a non-spec Response) without
    // `headers` used to surface "Cannot read properties of undefined" INSTEAD of
    // the backend's own reason, which is strictly worse than the raw-JSON message
    // this change set out to fix. Caught by FileExplorerApiCov80's own stub.
    const headerless = {
      ok: false,
      status: 403,
      text: () => Promise.resolve('zzz outside the allowed roots'),
    } as unknown as Response
    const e = await toApiError(headerless)
    expect(e.message).toBe('zzz outside the allowed roots')
    expect(e.status).toBe(403)
    expect(e.authRequired).toBe(false)
  })

  it('is an Error, so existing e.message fallbacks keep working', async () => {
    const e = await toApiError(res(400, 'bad'))
    expect(e).toBeInstanceOf(Error)
    expect(e.name).toBe('ApiError')
  })
})

describe('api/client re-export', () => {
  it('serves the SAME ApiError identity as the module that now defines it', async () => {
    // The class moved out of client.ts to keep app bundles free of its graph.
    // If the re-export ever became a second class, `instanceof` checks in the
    // dashboard would silently stop matching errors thrown by app clients.
    const client = await import('../api/client')
    expect(client.ApiError).toBe(ApiError)
    expect(client.friendlyErrText).toBe(friendlyErrText)
    expect(await toApiError(res(404, 'gone'))).toBeInstanceOf(client.ApiError)
  })
})
