/** Optional Shared Crew routes degrade only on 404; real failures reach their UI owners. */
import { describe, expect, it, vi } from 'vitest'
import { createSharedCrewEndpoints } from '../../api/client/sharedCrew'
import type { ClientTransport } from '../../api/client/transport'

function endpoints(status: number, body = '{}') {
  const get = vi.fn(async () => new Response(body, { status }))
  const j = vi.fn(async (response: Response) => {
    if (!response.ok) throw new Error(`HTTP ${response.status}`)
    return response.json()
  })
  return createSharedCrewEndpoints({ get, j } as unknown as ClientTransport).requests
}

const reads = ['sharedCrewCapabilities', 'sharedCrewViewer', 'sharedCrewPeople', 'sharedCrewCapacity', 'sharedCrewAccess', 'tailscaleInvites'] as const

describe('Shared Crew read failure contract', () => {
  it.each(reads)('%s preserves intentional 404 degradation', async method => {
    await expect(endpoints(404)[method]()).resolves.toBeNull()
  })
  it.each(reads)('%s propagates HTTP failures instead of an unavailable fallback', async method => {
    await expect(endpoints(500)[method]()).rejects.toThrow('HTTP 500')
  })
  it('keeps a successful viewer response with no principal as null', async () => {
    await expect(endpoints(200).sharedCrewViewer()).resolves.toBeNull()
    await expect(endpoints(200, 'null').sharedCrewViewer()).resolves.toBeNull()
  })
  it('rejects a malformed viewer response instead of clearing the cached viewer', async () => {
    await expect(endpoints(200, 'not JSON').sharedCrewViewer()).rejects.toThrow()
  })
  it('returns a reported viewer without changing its identity', async () => {
    await expect(endpoints(200, JSON.stringify({ principal: { id: 'p_me', kind: 'human' } })).sharedCrewViewer()).resolves.toEqual({ id: 'p_me', kind: 'human' })
  })
})
