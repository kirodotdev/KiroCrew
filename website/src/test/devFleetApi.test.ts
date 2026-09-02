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
    mockResponse('<html>502 Bad Gateway</html>', 502)
    const err = await failureOf(api.get('/fleet'))
    expect(err.status).toBe(502)
    expect(err.message).toContain('502 Bad Gateway')
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
