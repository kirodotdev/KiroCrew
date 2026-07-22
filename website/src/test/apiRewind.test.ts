/**
 * Tests for api.rewind — the new client method that calls the
 * /api/chat/slots/{slot}/rewind backend endpoint introduced in.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { api } from '../api/client'

describe('api.rewind', () => {
  let fetchSpy: ReturnType<typeof vi.spyOn>

  beforeEach(() => {
    fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ ok: true, at_message_index: 2 }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    )
  })

  afterEach(() => {
    fetchSpy.mockRestore()
  })

  it('POSTs to /api/chat/slots/{slot}/rewind with the ts + content body', async () => {
    const result = await api.rewind('slot-abc', '2026-05-21T16:00:02Z', 'edited content')

    expect(fetchSpy).toHaveBeenCalledOnce()
    const [url, init] = fetchSpy.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/api/chat/slots/slot-abc/rewind')
    expect(init.method).toBe('POST')
    expect(init.body).toBe(JSON.stringify({ ts: '2026-05-21T16:00:02Z', content: 'edited content' }))
    expect(result).toEqual({ ok: true, at_message_index: 2 })
  })

  it('URL-encodes slot keys with special characters', async () => {
    await api.rewind('slot/with spaces & symbols', 'ts', 'content')
    const [url] = fetchSpy.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/api/chat/slots/slot%2Fwith%20spaces%20%26%20symbols/rewind')
  })

  it('throws when the backend returns a non-2xx status', async () => {
    fetchSpy.mockResolvedValueOnce(
      new Response(JSON.stringify({ error: 'slot is running' }), {
        status: 409,
        headers: { 'Content-Type': 'application/json' },
      }),
    )
    await expect(api.rewind('slot-abc', 'ts', 'x')).rejects.toThrow(/slot is running/)
  })
})
