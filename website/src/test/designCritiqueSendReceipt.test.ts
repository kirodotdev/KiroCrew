/**
 * Design Critique's `send` goes through the chat-core transport over an
 * app-local wire, and the RECEIPT decides the outcome.
 *
 * The old send hit the bare `/api/chat` (an SSE stream, not a JSON receipt),
 * then swallowed the resulting parse error as success -- so a `{ok:false}`
 * refusal inside a 200 never surfaced, and the critique polled a slot whose
 * turn had never started. Now:
 *
 *   - the wire posts to `/api/chat?ws=1` and keeps the slot-recreation fields
 *     (`agent`, `memory_mode`, `mode`) the app depends on after a gateway restart;
 *   - `refused` and `transport-error` REJECT (nothing is running; the page's
 *     failWith reports it and drops the pending critique, as it did for a non-2xx);
 *   - `unknown` (accepted, receipt unreadable) and `queued` RESOLVE -- the
 *     request was taken and the poll that follows finds out.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'

import { designCritiqueApi } from '../apps/design-critique/api'

const jsonResponse = (body: unknown, status = 200): Response =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('designCritiqueApi.send — the transport receipt decides', () => {
  it('posts the JSON-receipt form with the slot-recreation fields, under the deadline signal', async () => {
    const fetchMock = vi.fn(async () => jsonResponse({ ok: true }))
    vi.stubGlobal('fetch', fetchMock)

    await expect(designCritiqueApi.send('dc-1', 'critique this')).resolves.toBeUndefined()
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/api/chat?ws=1')
    expect(init.method).toBe('POST')
    expect(init.credentials).toBe('same-origin')
    expect(init.signal).toBeInstanceOf(AbortSignal)
    expect(JSON.parse(String(init.body))).toEqual({
      message: 'critique this',
      slot: 'dc-1',
      agent: 'kirocrew',
      memory_mode: 'temporary',
      mode: 'design-critique',
    })
  })

  it('rejects a {ok:false} refusal inside a 200 with the server reason (the old swallow)', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({ ok: false, error: 'slot is stopping' })))
    await expect(designCritiqueApi.send('dc-1', 'x')).rejects.toThrow(/slot is stopping/)
  })

  it('rejects a non-2xx status, as before', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('gateway down', { status: 503 })))
    await expect(designCritiqueApi.send('dc-1', 'x')).rejects.toThrow()
  })

  it('rejects when the fetch itself fails, as before', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => { throw new TypeError('Failed to fetch') }))
    await expect(designCritiqueApi.send('dc-1', 'x')).rejects.toThrow()
  })

  it('resolves a queued send — the server took custody', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({ ok: true, queued: true })))
    await expect(designCritiqueApi.send('dc-1', 'x')).resolves.toBeUndefined()
  })

  it('resolves an accepted 2xx whose body is not JSON — indeterminate, never a refusal', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('data: {"ok":true}', { status: 200 })))
    await expect(designCritiqueApi.send('dc-1', 'x')).resolves.toBeUndefined()
  })
})
