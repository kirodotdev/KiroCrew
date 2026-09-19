import { describe, expect, it, vi, afterEach } from 'vitest'

import { api } from './client'

/** Capture the POST body sent to /api/chat/slots. /api/dashboard/config is also
 *  answered because createChatSlot resolves the default memory mode from it
 *  before posting. */
function stubFetch() {
  const bodies: Record<string, unknown>[] = []
  const json = (body: unknown) => ({
    ok: true,
    status: 200,
    json: async () => body,
    text: async () => JSON.stringify(body),
    headers: new Headers({ 'content-type': 'application/json' }),
  })
  const spy = vi.fn(async (input: unknown, init?: RequestInit) => {
    const url = String(input)
    if (url.includes('/api/dashboard/config')) return json({}) as unknown as Response
    if (url.includes('/api/chat/slots')) {
      bodies.push(init?.body ? JSON.parse(String(init.body)) : {})
      return json({ key: 'slot-1' }) as unknown as Response
    }
    return json({}) as unknown as Response
  })
  vi.stubGlobal('fetch', spy)
  return { bodies }
}

describe('api.createChatSlot backend field', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('sends the backend when one is picked', async () => {
    const { bodies } = stubFetch()
    await api.createChatSlot(undefined, undefined, undefined, undefined, 'persistent', undefined, undefined, undefined, undefined, undefined, 'claude')
    expect(bodies).toHaveLength(1)
    expect(bodies[0].backend).toBe('claude')
  })

  it('omits the backend field entirely when none is picked', async () => {
    const { bodies } = stubFetch()
    await api.createChatSlot(undefined, undefined, undefined, undefined, 'persistent')
    expect(bodies).toHaveLength(1)
    // Omitted, not sent empty: the server treats absence as "inherit the global
    // default", and an empty string would be a distinct (if equivalent) signal.
    expect('backend' in bodies[0]).toBe(false)
  })
})
