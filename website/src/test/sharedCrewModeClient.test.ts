import { describe, it, expect, vi } from 'vitest'
import { createSharedCrewEndpoints } from '../api/client/sharedCrew'
import type { ClientTransport } from '../api/client/transport'

function transport(status: number, body: unknown = {}): ClientTransport {
  const j = vi.fn(async (r: Response) => {
    if (!r.ok) throw new Error(`HTTP ${r.status}`)
    return await r.json()
  })
  return {
    get: vi.fn(async () => new Response(JSON.stringify(body), { status })),
    j,
  } as unknown as ClientTransport
}

describe('createSharedCrewEndpoints', () => {
  it('rejects a missing mode route instead of resolving null', async () => {
    const { requests } = createSharedCrewEndpoints(transport(404, { error: 'not found' }))
    await expect(requests.sharedCrewMode()).rejects.toThrow('HTTP 404')
  })

  it('resolves the mode body the route answers', async () => {
    const { requests } = createSharedCrewEndpoints(transport(200, { mode: 'console', crew_id: 'c' }))
    await expect(requests.sharedCrewMode()).resolves.toMatchObject({ mode: 'console', crew_id: 'c' })
  })

  it('keeps every other read degrading to null on 404', async () => {
    const { requests } = createSharedCrewEndpoints(transport(404))
    await expect(requests.sharedCrewPeople()).resolves.toBeNull()
    await expect(requests.sharedCrewCapabilities()).resolves.toBeNull()
  })
})
