/**
 * Detection has to reach the call sites that built their own `ApiError`.
 *
 * Two factories learned to recognise an interposed proxy's sign-in page, and six
 * sites bypassed both to construct the error by hand: three pasted the response
 * body into the message, so the page arrived in the UI as `<!DOCTYPE html>…`, and
 * the rest answered a bare `HTTP 403`. Reverting any one of them fails a case here.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { api, ApiError } from '../api/client'
import { get as devFleetGet } from '../pages/devFleetApi'

const CHALLENGE_PAGE = '<!DOCTYPE html><html><body><h1>Access Required</h1>'
  + '<p><a href="https://gate.example/login">Sign in</a></p></body></html>'

const respondWith = (body: string, status: number, contentType: string) =>
  vi.spyOn(globalThis, 'fetch').mockResolvedValue(
    new Response(body, { status, headers: { 'content-type': contentType } }),
  )

const failureOf = (call: Promise<unknown>): Promise<ApiError> =>
  call.then(
    () => { throw new Error('expected the call to reject') },
    (e: ApiError) => e,
  )

afterEach(() => { vi.restoreAllMocks() })

describe('a proxy challenge reaching a site that used to build its own error', () => {
  it('names the proxy rather than pasting its markup into the message', async () => {
    respondWith(CHALLENGE_PAGE, 403, 'text/html; charset=UTF-8')
    const err = await failureOf(api.wakatimeExportDownload('2026-09-01', '2026-09-02', 'csv'))
    expect(err.message).not.toMatch(/DOCTYPE|<html/i)
    expect(err.message).toMatch(/access proxy/i)
    expect(err.edgeChallenge).toBe(true)
    expect(err.authRequired).toBe(true)
  })

  it('reaches the Dev Fleet client, which answered the bare status before', async () => {
    respondWith(CHALLENGE_PAGE, 403, 'text/html; charset=UTF-8')
    const err = await failureOf(devFleetGet('/state'))
    expect(err.message).not.toMatch(/^HTTP 403$/)
    expect(err.message).toMatch(/access proxy/i)
    expect(err.edgeChallenge).toBe(true)
  })

  it('is a negative control: an ordinary JSON refusal is not reclassified', async () => {
    respondWith(JSON.stringify({ error: 'quota exhausted' }), 403, 'application/json')
    const err = await failureOf(api.wakatimeExportDownload('2026-09-01', '2026-09-02', 'csv'))
    expect(err.message).toBe('quota exhausted')
    expect(err.edgeChallenge).toBe(false)
  })
})
