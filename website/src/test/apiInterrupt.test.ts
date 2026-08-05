import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'

describe('api.interruptSlot', () => {
  let fetchSpy: ReturnType<typeof vi.spyOn>

  beforeEach(() => {
    fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ ok: true, outcome: 'soft' }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    )
  })

  afterEach(() => { fetchSpy.mockRestore() })

  it('POSTs to /interrupt without queue_id', async () => {
    await api.interruptSlot('slot-1')
    const [url, init] = fetchSpy.mock.calls[0] as [string, RequestInit]
    expect(url).toContain('/api/chat/slots/slot-1/interrupt')
    expect(JSON.parse(init.body as string)).toEqual({})
  })

  it('POSTs to /interrupt with queue_id', async () => {
    await api.interruptSlot('slot-1', 'q42')
    const [url, init] = fetchSpy.mock.calls[0] as [string, RequestInit]
    expect(url).toContain('/api/chat/slots/slot-1/interrupt')
    expect(JSON.parse(init.body as string)).toEqual({ queue_id: 'q42' })
  })
})
