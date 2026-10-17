/**
 * Regression tests for the Dev Fleet API client's error shape.
 *
 * A refusal is not always a failure to report: the sync single-flight 409 names
 * the run already in flight, and the page attaches its progress stepper to it.
 * The client used to throw a bare Error carrying only the response TEXT, which
 * left the caller with nothing to branch on and put a raw JSON blob in a toast.
 * It now raises the dashboard's own `ApiError`, so there is one error shape
 * rather than a Dev-Fleet-only second spelling of it.
 */
import { describe, it, expect, afterEach, vi } from 'vitest'
import * as api from '../pages/devFleetApi'
import { ApiError } from '../api/client'

afterEach(() => { vi.restoreAllMocks() })

function mockResponse(body: string, status: number) {
  vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(body, { status }))
}

async function failureOf(call: Promise<unknown>): Promise<ApiError> {
  return call.then(
    () => { throw new Error('expected the request to throw') },
    (e: ApiError) => e,
  )
}

describe('devFleetApi error shape', () => {
  it('raises an ApiError carrying the status and raw body for a 409 refusal', async () => {
    const wire = JSON.stringify({ ok: false, error: 'sync already running', run_id: 'run-99' })
    mockResponse(wire, 409)
    const err = await failureOf(api.post('/sync', {}))
    expect(err).toBeInstanceOf(ApiError)
    expect(err.status).toBe(409)
    // The body survives verbatim, which is how the caller reads `run_id`.
    expect(JSON.parse(err.body).run_id).toBe('run-99')
    // The human sentence, not the wire JSON -- this string reaches a toast.
    expect(err.message).toBe('sync already running')
  })

  it('falls back to the raw text when the body is not JSON', async () => {
    mockResponse('upstream refused the connection', 502)
    const err = await failureOf(api.get('/fleet'))
    expect(err.status).toBe(502)
    expect(err.message).toBe('upstream refused the connection')
  })

  it('shows HTTP <status> for an edge HTML error page rather than its markup', async () => {
    mockResponse('<html>502 Bad Gateway</html>', 502)
    const err = await failureOf(api.get('/fleet'))
    expect(err.status).toBe(502)
    expect(err.message).toBe('HTTP 502')
    // The page itself stays readable for diagnostics.
    expect(err.body).toContain('502 Bad Gateway')
  })

  it('falls back to the status when the body is empty', async () => {
    mockResponse('', 500)
    const err = await failureOf(api.get('/fleet'))
    expect(err.message).toBe('HTTP 500')
  })

  it('returns the parsed body on success', async () => {
    mockResponse(JSON.stringify({ ok: true, run_id: 'run-1' }), 200)
    await expect(api.post('/sync', {})).resolves.toEqual({ ok: true, run_id: 'run-1' })
  })
})

describe('devFleetApi namespaces', () => {
  // The live-target cutover and the gateway restart are served by the GATEWAY
  // process, not the sandboxed backend: the pointer they touch is masked from
  // that backend and everything it spawns. `postGateway` must therefore aim at
  // `/api/apps/dev-fleet/...` while everything else keeps the reverse-proxied
  // `/apps/dev-fleet/api/...`. A regression here would put the request back on
  // a route the backend no longer serves (404) -- or, worse, one it should not.
  it('postGateway targets the in-gateway namespace', async () => {
    const spy = vi.spyOn(globalThis, 'fetch').mockImplementation(() =>
      Promise.resolve(new Response('{"ok":true}', { status: 200 })),
    )
    await api.postGateway('/make-live', { path: '/wt' })
    await api.postGateway('/restart-gateway', {})
    const urls = spy.mock.calls.map((c) => (typeof c[0] === 'string' ? c[0] : (c[0] as Request).url))
    expect(urls).toEqual(['/api/apps/dev-fleet/make-live', '/api/apps/dev-fleet/restart-gateway'])
    expect(api.GATEWAY_BASE).toBe('/api/apps/dev-fleet')
  })

  it('post keeps the reverse-proxied backend namespace', async () => {
    const spy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response('{"ok":true}', { status: 200 }))
    await api.post('/sync', {})
    expect(spy.mock.calls[0][0]).toBe('/apps/dev-fleet/api/sync')
  })
})
