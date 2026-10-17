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

// Cutting a sleeping `wait` short is the same shape of turn intervention as an
// interrupt: a POST on the slot naming what to cancel. `wait_id` is mandatory —
// the backend answers 409 for a stale one, which is how a click on a leftover
// countdown is rejected instead of ending a LATER wait.
describe('api.endWait', () => {
  let fetchSpy: ReturnType<typeof vi.spyOn>

  beforeEach(() => {
    fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ ok: true }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    )
  })

  afterEach(() => { fetchSpy.mockRestore() })

  it('POSTs the wait_id to /end-wait', async () => {
    await api.endWait('slot-1', 'wait-abc123')
    const [url, init] = fetchSpy.mock.calls[0] as [string, RequestInit]
    expect(url).toContain('/api/chat/slots/slot-1/end-wait')
    expect(init.method).toBe('POST')
    expect(JSON.parse(init.body as string)).toEqual({ wait_id: 'wait-abc123' })
  })

  it('encodes a slot key containing URL-unsafe characters', async () => {
    // Slot keys carry surface prefixes (`slack:C123/167.9`), so an unencoded key
    // would split the path and hit the wrong route.
    await api.endWait('slack:C0123/1700.5', 'wait-xyz')
    const [url] = fetchSpy.mock.calls[0] as [string, RequestInit]
    expect(url).toContain('/api/chat/slots/slack%3AC0123%2F1700.5/end-wait')
  })
})
