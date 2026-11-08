import { describe, expect, it, vi } from 'vitest'
import { createChatEndpoints } from '../api/client/chat'
import type { ClientTransport } from '../api/client/transport'

describe('chat slot list', () => {
  it('returns the bare array without response-header hold metadata', async () => {
    const response = new Response(JSON.stringify([{ key: 'chat-1' }]), {
      headers: { 'Content-Type': 'application/json' },
    })
    const transport = {
      jfetch: vi.fn().mockResolvedValue(response),
      j: (value: Response) => value.json(),
    } as unknown as ClientTransport

    const slots = await createChatEndpoints(transport).slotList.chatSlots()

    expect(Array.isArray(slots)).toBe(true)
    expect(slots.map(slot => slot.key)).toEqual(['chat-1'])
    expect(Object.keys(slots)).toEqual(['0'])
    expect('slotsHeldKeys' in slots).toBe(false)
    expect('slotsHoldChannels' in slots).toBe(false)
  })

  it('asks for every row, a Crew Mode root\'s workers included', async () => {
    // The person's list leaves a conductor's workers out on the server unless
    // `crew=all`; the store keeps every row, so this read must ask for them.
    const response = new Response('[]', { headers: { 'Content-Type': 'application/json' } })
    const jfetch = vi.fn().mockResolvedValue(response)
    const transport = { jfetch, j: (value: Response) => value.json() } as unknown as ClientTransport

    await createChatEndpoints(transport).slotList.chatSlots()

    expect(jfetch).toHaveBeenCalledWith('/api/chat/slots?crew=all')
  })
})
